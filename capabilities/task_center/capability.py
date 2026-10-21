#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""任务中心能力入口（`business.task_center.v1`）。

设计出处：`docs/superpowers/specs/2026-09-09-task-center-design.md` §4.3、§九、§十第 7 步。

**这个能力自己不监听任何 HTTP 业务端点。** 它的调用面有三个方向：

- **运行方**经 `TaskCenterClient`（本机直调或经 Sidecar `/invoke`）调
  **§4.2 那四个写**（`init_task` 发起任务、`report_status` 上报结果、`retry_subtask`
  要求重跑某一步、`retry_subtasks` 要求重跑某几步）**与 §4.3 那两个读**（`get_subtask` /
  `get_task`），走的是能力注册中心的 `execute`，落到本文件的
  `task.init_task` / `task.report_status` / `task.subtask_retry` /
  `task.subtask_retry_batch` / `task.subtask_get` / `task.get`；
- **后台循环**自己去出站（§7.5：⓪ 触发抢占、① 派活、② 消化上报、③ 超时巡检 —— 这三趟会
  出站，其中 ③ 是 2026-10-16 起「**判死之前先问一次运行方**」；④ 补收口不出站），由
  `scheduler_service` 负责；派活走 §4.1 ① 的 `execute`；
- **前端任务页面**（§4.4）经 Gateway → Sidecar `/invoke` 调**八个**接口：`task.console_page` /
  `task.console_subtask_list` 两个列表 + `task.console_detail` / `task.console_subtask_detail`
  两个**详情**（都只读，返回体与各自的列表行一致），外加四个写接口 ——
  `task.stop`（停止整条任务）、`task.console_subtask_stop`（停止某一步）、
  `task.console_task_retry`（整条任务原地重跑）、`task.console_subtask_retry`（重跑某一步）。
  这是 §4.4 那条「不提供面向浏览器/前端的网络接口」的边界**开的口子**。
  **删除 / 详情这些写操作仍然一概不开** —— 加新的前端接口之前先回设计文档确认 §4.4 的边界；
  加**写**接口还要同步登记 `_ACTION_PERMISSIONS`，漏了就是「悄悄放行」。

下面 `_build_dispatch` 里那三组（`def.*` / `trig.*` / `runner.*` 以及 §4.3 的 `task.*`）
是**对内的函数集**：由任务中心自己的后台循环、管理端调用方使用，**不经过
`TaskCenterClient`**（那个 client 里只有 §4.1 **三个** + §4.2 **四个** + §4.3 的**两个读**
（`get_subtask` / `get_task`），再加一个纯内部
的 `_call`（远端调用的实现细节，不是契约），§4.3 其余的一个都不进）。
给前端的那**八个**（四个只读 —— 两个列表 `task.console_page` / `task.console_subtask_list`
加两个详情 `task.console_detail` / `task.console_subtask_detail`；另加四个写接口
`task.stop` / `task.console_subtask_stop` / `task.console_task_retry` /
`task.console_subtask_retry`）同样注册在这个 dispatch 里。

**管理端调用方里有一类特殊的：平台自己。** 建系统级定义（`def.create` +
`scope='system'`，§3.1「平台自己的，无租户、无空间」）时调用方**本来就没有租户**，
所以 `def.create` 是唯一一个**允许不带租户进来**的操作（名单见 `_PLATFORM_ACTIONS`），
其余操作在入口就要求有租户。

所以这里**仍然不覆写 `register_routes`**（基类默认空实现）：任务中心不自己开 REST
路由。给前端的那几个只读接口走的是**能力注册中心的 `execute`**（`task.console_*`），
由 Sidecar `/invoke` 转进来，不是本能力监听的端点 —— 前端也不许直连能力服务
（CLAUDE.md 编辑要求）。要加 REST 路由，先回设计文档确认 §4.4 的边界。

每个 action 一个独立事务：`_execute` 开一个会话、交给 service、退出即提交。
service 抛 `JonexException` 时由 `execute` 统一转成错误响应（错误码原样带回）。

**一份适配器，整模块共用**：出站调运行方的 `RunnerAdapter` 在本能力实例上只建一份，
所有 action（尤其 `task.delete` 要出站请运行方删除）都复用它，连接池因此复用；
能力关闭时随 `shutdown` 一起释放。
"""

import logging
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

try:  # pydantic>=1.10 的 v1 兼容命名空间优先（与各 DTO 的引入口径一致）
    from pydantic.v1 import ValidationError
except ImportError:  # pragma: no cover
    from pydantic import ValidationError


from jonex_core.capability import BaseCapability
from jonex_core.capability.models import (
    CapabilityMetadata,
    CapabilityRequest,
    CapabilityResponse,
    CapabilityType,
)
from jonex_core.common import get_db_session
from jonex_core.common.exceptions import (
    CacheError,
    DatabaseError,
    InternalError,
    InvalidParameterError,
    JonexException,
    PermissionDeniedError,
    ServiceTimeoutError,
    ServiceUnavailableError,
    TenantIsolationError,
)
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant

from .dtos.execution import ReportStatusRequest
from .dtos.trigger import TaskTriggerCreate, TaskTriggerUpdate
from .flows.registry import RunnerRegistry
from .flows.runner_adapter import RunnerAdapter
from .services import TaskDefinitionService, TaskService, TaskTriggerService

logger = logging.getLogger(__name__)


#: `action` → 需要的权限码。**§4.4 开给前端的八个口全部登记**（2026-10-08 定、2026-10-13 补两个详情）：
#: 四个只读接口用 `:read`、四个写接口用 `:write`（写的就 write、只读的就 read）——
#: 与前端一致：`shell/src/utils/taskCenterEntry.ts` 的菜单码是 `task_center:read`，
#: `pages/TaskCenter/index.tsx` 的 `canRead` / `canStop` 分别按这两个码收敛。
#:
#: 内部函数集（`def.*` / `trig.*` / `runner.*` 以及 `task.*` 里那几个）**不登记**：
#: 它们由后台循环、管理端调用方与运行方调用，本来就没有 `user_id`，
#: 而 `_check_action_permission` 的口径是「无 `user_id` 即放行」——写进来也命中不到。
#:
#: ⚠️ 加新的**写**接口时，这里必须同步登记 —— 漏登记的后果是「悄悄放行」（与网关侧
#: 漏传 `user_id` 是同一类问题，见 `CLAUDE.md` 那条纪律）。
_ACTION_PERMISSIONS: Dict[str, str] = {
    # 两个只读接口（租户内只读视图）
    "task.console_page": "task_center:read",
    "task.console_subtask_list": "task_center:read",
    # 详情两个（2026-10-13 加）：返回体与各自的**列表行完全一致**，所以同样只需读码。
    "task.console_detail": "task_center:read",
    "task.console_subtask_detail": "task_center:read",
    # 四个写接口共用同一个写码：粒度到「能不能改任务中心」为止，不再往下分
    # 「停止」与「重试」两档 —— 分开只会多出一批授权行，而实际授权口径是一样的
    # （见 `deploy/postgres/update/043_task_center_permission.sql`）。
    "task.stop": "task_center:write",
    "task.console_subtask_stop": "task_center:write",
    "task.console_task_retry": "task_center:write",
    "task.console_subtask_retry": "task_center:write",
}


async def _check_action_permission(request) -> None:
    """invoke dispatch 前置校验：action 映射权限码 → 用户权限集合判定。

    照抄 `capabilities/business_domain/capability.py::_check_action_permission`
    （invoke 路径上**唯一**的既有先例），user_id 口径与它逐字一致：

    - `CapabilityRequest.user_id` 是 `Optional[str]`（gateway 仅在 JWT 可解时透传 sub；
      测试 token 解不出 JWT → `user_id` 为 None，不是 `"0"`）。
    - **无 user_id → 放行（不拦截）**：能到达 capability 的请求已过 Sidecar 认证，
      权限码校验是**租户内细化授权、而非边界防线**；测试 token 依赖此口径。

    失败抛 `PermissionDeniedError`（**3006**）—— 它已经在 `TASK_CENTER_REJECTED_CODES` 里，
    调用方会判成「明确拒绝、不重发」（这正是想要的：没权限重发一万次还是没权限）。
    """
    action = request.payload.get("action")
    code = _ACTION_PERMISSIONS.get(action)
    if not code:
        return
    if not request.user_id:
        return
    from jonex_core.security.permission import has_permission

    if not await has_permission(request.tenant_id, int(request.user_id), code):
        raise PermissionDeniedError(
            message=translate(
                "err.auth.insufficient_permission",
                params={"required": code},
                fallback=f"权限不足：需要权限 {code}",
            )
        )


class TaskCenterCapability(BaseCapability):
    """任务中心能力：编排定义 / 触发 / 任务执行 三条线的对内函数集。"""

    # 平台专用操作：**允许不带租户调用**（其余操作在入口一律先过 `require_tenant`）。
    #
    # 为什么只有 `def.create` 一个：它要支持 `scope='system'`（§3.1 / §4.3 —— 系统级
    # 定义是**平台自己的**，没有租户也没有空间）。平台调用方本来就拿不出租户，被入口
    # 那道无条件 `require_tenant` 一拦，下面 `_resolve_scope` 里的 `system` 分支在真实
    # 调用里**根本走不到**，文档写明的这条路等于不存在。
    #
    # **放行的只是「入口不拦」，不是「不做校验」**：进来之后由
    # `TaskDefinitionService._resolve_scope` 按 `scope` 逐个把关 —— 写 `system` 要求
    # `tenant_id` / `space_id` 都空；写 `tenant` / `space` 时那两个分支自己
    # `require_tenant`，没租户照旧拒。租户调用方（带租户进来）写 `system` 也会被拒。
    #
    # ⚠️ 以后要再加平台专用操作，往这个集合里添。**别把带租户的业务操作加进来** ——
    # 那等于把「所有业务数据必须有合法 tenant_id」这条总规则绕过去了（CLAUDE.md 租户规则）。
    _PLATFORM_ACTIONS: frozenset = frozenset({"def.create"})

    # 每个操作**必须**给的参数。入口先照这张表查一遍（`_require_keys`），缺了回 1001。
    #
    # **为什么非要有这张表**：下面 `_build_dispatch` 里到处是 `d["xxx"]` 这种裸取键，
    # 漏传一个就 `KeyError` —— 它不是 `JonexException`，会漏到 `execute` 的兜底
    # `except`，变成「执行失败」+ 一个按类型给的码（4xxx / 5xxx / 1000，见
    # `_internal_error_code`）+ 一段内部文案。而「你少给了个
    # 参数」是**调用方自己就能改**的事，正确答案是 1001 参数非法（§4.0）。
    #
    # **这里只查"在不在"、不查"对不对"**：类型 / 取值 / 业务合法性一律留给 service
    # —— 只有那里才知道 `scope` 该配什么、`version_no` 该不该存在、`inputs` 对不对得上
    # 定义的入参 schema。入口重复一遍等于把同一套规则写两处，早晚分叉。
    #
    # ⚠️ **新增操作时顺手补这一行**。漏补不会出错（只是那个操作漏参数时又回到
    # 「执行失败」），但补上才叫一致。`trig.create` 那几个必填项同时也写在
    # `TaskTriggerCreate` 里 —— 这张表先命中，所以调用方看到的是这条更直白的文案。
    #
    # `trig.create` 的 `cron_expr` / `misfire_policy` **刻意不列在这一行**：它们是
    # 「触发器只等于定时」下的必填项，但"缺哪个"由服务层的 `cron_fields_incomplete`
    # 报 —— 那句会说清「缺了它就没法决定到点发现错过了怎么办」，比在这里列一个
    # 「缺少必填参数 cron_expr」有用得多。
    _REQUIRED_KEYS: Dict[str, tuple] = {
        # ── 定义 def.* ──
        "def.catalog_steps": ("task_catalog_code",),
        "def.create": ("scope", "task_catalog_code", "name"),
        "def.get": ("definition_id",),
        "def.version_create": ("definition_id",),
        "def.version_update": ("definition_id", "version_no", "config"),
        "def.version_get": ("definition_id", "version_no"),
        "def.version_publish": ("definition_id", "version_no"),
        "def.delete": ("definition_id",),
        # ── 触发 trig.* ──
        "trig.create": ("space_id", "definition_id", "trigger_key", "name"),
        "trig.get": ("trigger_id",),
        "trig.update": ("trigger_id",),
        "trig.fire": ("trigger_id",),
        "trig.pause": ("trigger_id",),
        "trig.resume": ("trigger_id",),
        "trig.delete": ("trigger_id",),
        "trig.runs": ("trigger_id",),
        # ── 运行方 runner.* ──
        "runner.get": ("code",),
        # ── 执行 task.* ──
        "task.fire": ("definition_id", "space_id"),
        "task.get": ("task_id",),
        "task.subtask_list": ("task_id",),
        "task.subtask_get": ("task_id", "node_id"),
        "task.attempt_list": ("task_id",),
        "task.subtask_attempt_list": ("task_id", "node_id"),
        "task.retry": ("task_id",),
        "task.subtask_retry": ("space_id", "task_id", "node_id"),
        # 批量重跑（§4.2 ④）：`node_ids` **顺序有意义**（调用方给的顺序就是执行顺序）——
        # 缺了它这一档没有别的路能表达「先跑这个再跑那个」。
        "task.subtask_retry_batch": ("space_id", "task_id", "node_ids"),
        "task.stop": ("task_id",),
        "task.delete": ("task_id",),
        # `task_source` **必传**（2026-10-13 加）：这条路的语义就是「运行方发起」，
        # 来源是它的身份信息之一。⚠️ 这里只查「键在不在」；**取值对不对由 service 的
        # `_check_task_source` 判**（协议层那份判据，非法值 1001）—— 与这张表的分工一致。
        "task.init_task": ("definition_id", "space_id", "task_source"),
        "task.report_status": ("space_id", "execution_id", "task_id", "status"),
        # `task.list` 也**全参数可选**（`filters` / `space_id` / 分页都不传 = 首页全量），
        # 所以不在表里 —— 跟下面 `task.console_page` 同一个道理。
        # ── 管理端（§4.4）──
        # `task.console_page` 全参数可选，所以不在表里。⚠️ 但它**不是**「不传就不过滤」：
        # `space_id` 不传时收敛到「调用者有权访问的空间集合」（见 `page_tasks` 的
        # 空间维度那段），必填列表管的是「参数在不在」，管不了这种权限收敛。
        "task.console_subtask_list": ("task_id",),
        # 详情两个（2026-10-13 加）。子任务详情与上面那条列表**同口径寻址**：
        # `node_id`（节点名，如 `parse_1`）—— **不是**子任务行的主键 `id`。
        "task.console_detail": ("task_id",),
        "task.console_subtask_detail": ("task_id", "node_id"),
        # 三个写接口（2026-09-29 新增，2026-09-29 起前端可调）：
        #   · `node_id` 寻址（同一个 `step_code` 允许在编排里出现多次，唯一的是 `node_id`）；
        #   · **不需要 `space_id`** —— 那是 `task.subtask_retry` 给运行方的一致性核对，
        #     前端没有空间上下文，由能力侧从任务行推导（见 `_console_subtask_retry_action`）。
        "task.console_subtask_stop": ("task_id", "node_id"),
        "task.console_task_retry": ("task_id",),
        "task.console_subtask_retry": ("task_id", "node_id"),
    }

    def __init__(self):
        # 出站调运行方的适配器整模块共用一份（连接池复用）；`task.delete` 要经它请
        # 运行方删除，「派活」则由后台循环持有自己那份（`scheduler_service`）。
        self._adapter = RunnerAdapter()
        self._dispatch = self._build_dispatch()
        super().__init__()

    # ── 生命周期 ──────────────────────────────────────────

    async def initialize(self) -> None:
        """起后台循环（触发抢占 → 派活 → 消化上报 → 超时巡检 → 补收口，§7.4、§7.5）。

        **恒开、不设开关**：这五趟是这个服务的心脏，没有「关掉」这个状态；
        多副本下不选主、靠 CAS 抢占，所以每一份都跑。
        """
        # 调用明细采集写口（monitor.call_log / monitor.call_attempt）。
        # 采集器住在共享内核（`jonex_core/common/monitor_collector.py`），**每个进程
        # 各自起一份**（单例 + 独立队列 + 独立连接）—— 所以本能力得自己起，不能指望
        # 别的进程替它采。⚠️ **不起 = 埋点静默失效**：`put_call_attempt` 在采集器未启动
        # 时直接返回（那条「旁路不得影响主链路」的保证），派活重试的 N2 一条都落不了库，
        # 而业务侧看不出任何异常。
        # **排在 `start_scheduler()` 之前**：后台循环一起跑就会出站派活、投 N2，采集器
        # 得先就位，否则那一段窗口里的量直接丢。
        # 与 platform 同一口径：采集是旁路，**起不来绝不能拖垮任务中心本体**（只丢一行
        # ERROR，N2 不落库而已）。
        try:
            from jonex_core.common.monitor_collector import get_monitor_collector
            get_monitor_collector().start()
            logger.info("MonitorCollector 已启动")
        except Exception:
            logger.exception("MonitorCollector 启动失败（不影响任务中心功能）")

        from .services import start_scheduler

        await start_scheduler()

    async def shutdown(self) -> None:
        from .services import stop_scheduler

        await stop_scheduler()
        # 采集器**最后停**：后台循环这一轮已经投进队列的 N2 才有机会落库
        # （`stop()` 会 flush 剩余队列再退出）。同样只记日志，不挡停机。
        try:
            from jonex_core.common.monitor_collector import get_monitor_collector
            await get_monitor_collector().stop()
            logger.info("MonitorCollector 已停止")
        except Exception:
            logger.exception("MonitorCollector 停止失败")
        # 释放本能力共用适配器的连接池。
        await self._adapter.close()

    # ── 能力契约 ──────────────────────────────────────────

    def _build_metadata(self) -> CapabilityMetadata:
        return CapabilityMetadata(
            capability_id="task_center",
            capability_name="任务中心",
            capability_type=CapabilityType.BUSINESS,
            version="v1",
            description="任务编排（定义 / 版本 / 发版）、触发、任务执行与子任务调度",
            author="jonex",
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": sorted(self._dispatch.keys()),
                        "description": "操作类型",
                    },
                    "data": {"type": "object", "description": "操作数据"},
                },
                "required": ["action"],
            },
            output_schema={"type": "object"},
            tags=["任务中心", "编排", "调度"],
        )

    async def validate_input(self, request: CapabilityRequest) -> bool:
        return True

    async def execute(self, request: CapabilityRequest) -> CapabilityResponse:
        action = request.payload.get("action")
        data = request.payload.get("data") or {}
        if not isinstance(data, dict):
            # ⚠️ **`data` 必须是对象**（§4.0）：非对象（字符串 / 数组 / 标量）会让下面
            # `_require_keys` / handler 里的 `data.get(...)` 抛 `AttributeError` →
            # 落 `except Exception` → 1000（`CLASS_UNKNOWN` =「未知、可原样重发」），
            # 而它其实是**报文形态错误**，正解是 1001（明确拒绝、**不重发**）——
            # 不拦的话，一个永久无效的请求会被反复原样重发（2026-10-13 修）。
            # 与上面「不认识的 action」同一个处置口径、同一个码。
            return CapabilityResponse.error(
                request.request_id,
                InvalidParameterError.code,
                translate(
                    "err.task_center.param.invalid",
                    params={
                        "errors": f"payload.data 必须是对象，收到 {type(data).__name__}"
                    },
                    fallback=(
                        "参数没通过校验：payload.data 必须是对象，"
                        f"收到 {type(data).__name__}"
                    ),
                ),
            )

        handler = self._dispatch.get(action)
        if handler is None:
            # **回平台码、不自造**（§4.0「本模块不自造码」）：以前这里回的是字面量 `400`,
            # 那不是任何一个异常的错误码 —— 调用方拿 `classify_code` 一判会落进
            # `CLASS_UNKNOWN`（三张码表一张都没中）被当成「结果未知、可以原样重发」，
            # 而「不认识的 action」是**永久拒绝**，重发一万次还是这句。这与下面
            # `except Exception` 分支里那次「字面量 500 → 按异常类型给码」是同一类
            # 问题的漏网之鱼。
            # 取 `InvalidParameterError`（1001）：action 名字给错了，是参数问题；
            # 它在 `TASK_CENTER_REJECTED_CODES` 里，判成「明确拒绝、不重发」。
            return CapabilityResponse.error(
                request.request_id,
                InvalidParameterError.code,
                translate(
                    "err.task_center.capability.action_unsupported",
                    params={"action": action},
                    fallback=f"不支持的操作：{action}",
                ),
            )

        try:
            # 先查必填参数（§4.0）：漏传是「参数非法」（1001），不是「执行失败」。
            # **放在租户闸前面**是故意的 —— 这一步纯看报文、不需要任何上下文，而且
            # `def.create` 那条平台路根本不进租户闸，先查这一步两条路才一样。
            self._require_keys(action, data)

            # 平台专用操作（目前只有 `def.create`）允许不带租户进来——它要能建
            # `scope='system'` 的定义，而那种定义本来就没有租户。**剩下的一律先过这道闸**：
            # 这里是"所有业务数据必须有合法 tenant_id"的入口落点，别在这里放松。
            # 放行的那一个也不等于免检：进去后由 service 按 `scope` 再判（见 `_PLATFORM_ACTIONS`）。
            if action not in self._PLATFORM_ACTIONS:
                request.tenant_id = require_tenant(request.tenant_id)
            # 权限闸排在租户闸**之后**：`has_permission` 要拿归一化过的 `request.tenant_id`
            # 去查权限集合，没先归一化会拿着调用方原样传来的值去查。必填参数闸排在更前面
            # 是另一回事（它纯看报文、不需要上下文，且 `def.create` 那条平台路不进租户闸）。
            # 未登记的 action 不判权（内部函数集与运行方路径没有 `user_id`，本就命中不到）；
            # `PermissionDeniedError` 由下面的 `except JonexException` 兜住、按 3006 回给调用方。
            await _check_action_permission(request)
            result = await handler(request, data)
            return CapabilityResponse.ok(request.request_id, result)
        except TenantIsolationError as exc:
            return CapabilityResponse.error(request.request_id, exc.code, exc.message)
        except JonexException as exc:
            # 参数非法 / 找不到 / 冲突这类都在这条路上：错误码与 details 原样带回，
            # 调用方靠 `code` 判「要不要重试」（`TASK_CENTER_RETRYABLE_CODES`）、
            # 按 `message` 知道「为什么没成」（§4.0）。
            return CapabilityResponse.error(
                request.request_id, exc.code, exc.message, details=exc.details
            )
        except Exception as exc:
            # 兜底：**文案只进日志、不回给调用方**——异常原文可能带着表名、SQL 片段、
            # 连接串这类内部细节，透出去等于白送一份内部结构；回出去的文案走 i18n，
            # 跟同层其它错误一样能按 `X-Lang` 出不同语言。
            #
            # **但 `code` 要按异常的实际类型给**（§4.0「响应结构」：数据库 → `DatabaseError`、
            # Redis → `CacheError`、依赖不可达 → 5xxx 那几个，**别一律拿 `InternalError`
            # 兜**）。**这里以前回的是字面量 `500`** —— 那不是任何一个异常的错误码，调用方
            # 拿它既判不了「该不该重试」、也对不上任何一处文档，等于把分类直接丢了。
            # 分类见 `_internal_error_code`。
            logger.exception("执行任务中心能力失败: %s", exc)
            return CapabilityResponse.error(
                request.request_id,
                _internal_error_code(exc),
                translate(
                    "err.task_center.capability.execute_failed",
                    fallback="执行失败，请稍后重试或联系管理员",
                ),
            )

    # ── 入参把关 ──────────────────────────────────────────

    def _require_keys(self, action: str, data: Dict[str, Any]) -> None:
        """按 `_REQUIRED_KEYS` 查一遍必填参数，缺了就报「参数非法」（§4.0）。

        **判据是「键不在、或显式传了 `null`」** —— 后者等于没给：放进服务里只会在更
        深处炸（比如 `config=null` 写进 `NOT NULL` 的列）。键在、值也非空的一律放行，
        「值对不对」不归这里管（见 `_REQUIRED_KEYS` 的注释）。
        """
        missing = [
            name for name in self._REQUIRED_KEYS.get(action, ()) if data.get(name) is None
        ]
        if missing:
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.param.required",
                    params={"action": action, "names": "、".join(missing)},
                    fallback=f"操作 {action} 缺必填参数：{'、'.join(missing)}",
                ),
                details={"action": action, "missing": missing},
            )

    # ── action 分发（§4.3 四组函数）────────────────────────

    def _build_dispatch(self) -> Dict[str, Callable[[CapabilityRequest, Dict], Any]]:
        return {
            # ── 定义 def.* ──
            "def.catalog_list": lambda r, d: self._execute(
                lambda s: TaskDefinitionService(s).list_catalogs()
            ),
            "def.catalog_steps": lambda r, d: self._execute(
                lambda s: TaskDefinitionService(s).list_catalog_steps(d["task_catalog_code"])
            ),
            "def.create": lambda r, d: self._execute(
                lambda s: TaskDefinitionService(s).create_definition(
                    d["scope"],
                    d["task_catalog_code"],
                    d["name"],
                    tenant_id=r.tenant_id,
                    space_id=d.get("space_id"),
                    description=d.get("description"),
                    initial_config=d.get("initial_config"),
                )
            ),
            "def.list": lambda r, d: self._execute(
                lambda s: TaskDefinitionService(s).list_definitions(
                    r.tenant_id,
                    d.get("scope"),
                    **_page_params(d),
                )
            ),
            "def.get": lambda r, d: self._execute(
                lambda s: TaskDefinitionService(s).get_definition(
                    d["definition_id"], r.tenant_id
                )
            ),
            "def.version_create": lambda r, d: self._execute(
                lambda s: TaskDefinitionService(s).create_version(
                    d["definition_id"],
                    d.get("space_id"),
                    base_version_no=d.get("base_version_no"),
                    config=d.get("config"),
                    tenant_id=r.tenant_id,
                )
            ),
            "def.version_update": lambda r, d: self._execute(
                lambda s: TaskDefinitionService(s).update_version(
                    d["definition_id"], d["version_no"], d["config"], r.tenant_id
                )
            ),
            "def.version_get": lambda r, d: self._execute(
                lambda s: TaskDefinitionService(s).get_version(
                    d["definition_id"], d["version_no"], r.tenant_id
                )
            ),
            "def.version_publish": lambda r, d: self._execute(
                lambda s: TaskDefinitionService(s).publish_version(
                    d["definition_id"], d["version_no"], r.tenant_id
                )
            ),
            "def.delete": lambda r, d: self._execute(
                lambda s: TaskDefinitionService(s).delete_definition(
                    d["definition_id"], r.tenant_id
                )
            ),
            # ── 触发 trig.* ──
            "trig.create": lambda r, d: self._execute(
                lambda s: TaskTriggerService(s).create_trigger(
                    r.tenant_id, d["space_id"], _from_request(TaskTriggerCreate, d)
                )
            ),
            "trig.list": lambda r, d: self._execute(
                # ⚠️ 这里**一律关键字传参**：`trigger_type` 那个筛选项已随「触发器只等于
                # 定时」一并删除，而 `**` 展开**不会**替位置实参报「参数错位」——
                # 少一个位置实参时，`page` / `page_size` 会安静地对到别的形参上去。
                lambda s: TaskTriggerService(s).list_triggers(
                    r.tenant_id,
                    space_id=d.get("space_id"),
                    definition_id=d.get("definition_id"),
                    status=d.get("status"),
                    **_page_params(d),
                )
            ),
            "trig.get": lambda r, d: self._execute(
                lambda s: TaskTriggerService(s).get_trigger(d["trigger_id"], r.tenant_id)
            ),
            "trig.update": lambda r, d: self._execute(
                lambda s: TaskTriggerService(s).update_trigger(
                    d["trigger_id"],
                    r.tenant_id,
                    # 先过一遍 DTO：可改字段白名单在它那儿（`space_id` 按 §4.3 压根改不了），
                    # 同时把 `tenant_id` 这类"调用上下文"从报文里摘掉——带着它进 `**fields`
                    # 会撞上 `update_trigger` 的 `tenant_id` 形参，直接 TypeError。
                    # `exclude_unset` 保住「没给的不动」这个语义。
                    # ⚠️ 它还额外保住了**「键在不在」这个信息**，服务层的 `fixed_version`
                    # 就靠它区分「没传」（不动）与「显式传 null」（清空、切回最新已发布版）——
                    # 别把这行改成手拼 `{k: d.get(k) for k in ...}`，那会把两者折叠成一支。
                    # `_from_request` 让 pydantic 的字段校验失败也回 1001（见它的注释）。
                    **_from_request(TaskTriggerUpdate, d).dict(exclude_unset=True),
                )
            ),
            "trig.fire": lambda r, d: self._execute(
                lambda s: TaskTriggerService(s).fire_trigger(d["trigger_id"], r.tenant_id)
            ),
            "trig.pause": lambda r, d: self._execute(
                lambda s: TaskTriggerService(s).pause_trigger(d["trigger_id"], r.tenant_id)
            ),
            "trig.resume": lambda r, d: self._execute(
                lambda s: TaskTriggerService(s).resume_trigger(d["trigger_id"], r.tenant_id)
            ),
            "trig.delete": lambda r, d: self._execute(
                lambda s: TaskTriggerService(s).delete_trigger(d["trigger_id"], r.tenant_id)
            ),
            "trig.runs": lambda r, d: self._execute(
                lambda s: TaskTriggerService(s).list_trigger_runs(
                    d["trigger_id"], **_page_params(d), tenant_id=r.tenant_id
                )
            ),
            # ── 运行方 runner.* ──
            "runner.list": lambda r, d: self._execute(
                lambda s: RunnerRegistry(s).list_runners()
            ),
            "runner.get": lambda r, d: self._execute(
                lambda s: RunnerRegistry(s).get_runner(d["code"])
            ),
            # ── 执行 task.* ──
            "task.fire": lambda r, d: self._execute(
                lambda s: self._task_service(s).fire(
                    r.tenant_id,
                    d["definition_id"],
                    d["space_id"],
                    d.get("inputs") or {},
                    version_no=d.get("version_no"),
                    # **刻意不传 `trigger_id`**：`task.fire` 是「不经触发器」那条路，
                    # 报文里的 `trigger_id` 一律不收（§4.3 `task.fire` 的「前置条件」：「因此它**没有
                    # `trigger_id` 参数** —— 走这条路生成的任务，`tasks.trigger_id`
                    # 一律为 NULL」）。以前在这里透传 `d.get("trigger_id")`，等于让调用方
                    # 能凭空给任务挂一个触发器 ID，把「两条路各自记什么」的界限抹掉了。
                    # 要给 `tasks.trigger_id` 赋值只有一条路：`trig.fire`（它传 `row.id`）。
                    #
                    # `origin` 是**日志标签**（「这次是谁扣的扳机」），一个字节都不落库：
                    # 裸字面量 `"manual"` 只是它的默认值，**跟触发器类型无关** ——
                    # `TriggerType` 这个枚举已经随「触发器只等于定时」整个删掉了。
                    origin=d.get("origin", "manual"),
                    # 调用方自带的 id（可选）：给了就用它当主键，撞了已有任务抛
                    # `ResourceAlreadyExistsError`（1006）。**这一档要在文档里说清是
                    # 「成功」而不是「失败」** —— `task.fire` 走能力 invoke，**没有
                    # SDK 的重试层**（`init_task` 那条有，`TaskCenterClient` 会把 1006
                    # 解读成 `SubmissionDisposition.REUSED`）。所以这里的调用方拿到
                    # 1006 得自己明白「上一次其实建成了」—— 换 id 重来就是把它建第二遍。
                    task_id=d.get("task_id") or None,
                    # **`name` 刻意不透传**：`task.fire` 这条路的实例名一律取定义名
                    # （`fire` 里「没给就退回 `task_definitions.name`」那一段）。
                    # 注意 `fire()` 的 `name` 参数**不能删** —— `task.init_task` 和
                    # `fire_trigger` 都共用它，运行方 SDK 明确在传。
                )
            ),
            "task.list": lambda r, d: self._execute(
                lambda s: self._task_service(s).list_tasks(
                    r.tenant_id,
                    d.get("filters"),
                    # 空间筛选**只走顶层这个参数**（`filters` 里出现 space_id 会被当场
                    # 拒掉，1001，§4.3）—— 它要校验归属，不是普通筛选条件。
                    space_id=d.get("space_id"),
                    **_page_params(d),
                )
            ),
            "task.get": lambda r, d: self._execute(
                lambda s: self._task_service(s).get_task(d["task_id"], r.tenant_id)
            ),
            "task.subtask_list": lambda r, d: self._execute(
                lambda s: self._task_service(s).list_subtasks(
                    d["task_id"], _attempt_param(d), r.tenant_id
                )
            ),
            "task.subtask_get": lambda r, d: self._execute(
                lambda s: self._task_service(s).get_subtask(
                    d["task_id"], d["node_id"], _attempt_param(d), r.tenant_id
                )
            ),
            "task.attempt_list": lambda r, d: self._execute(
                lambda s: self._task_service(s).list_task_attempts(
                    d["task_id"], **_page_params(d), tenant_id=r.tenant_id
                )
            ),
            "task.subtask_attempt_list": lambda r, d: self._execute(
                lambda s: self._task_service(s).list_subtask_attempts(
                    d["task_id"],
                    d["node_id"],
                    **_page_params(d),
                    attempt=_attempt_param(d),
                    tenant_id=r.tenant_id,
                )
            ),
            "task.retry": lambda r, d: self._execute(
                lambda s: self._task_service(s).retry_task(d["task_id"], r.tenant_id)
            ),
            # 重试单个子任务：**按 `node_id` 寻址**（同一个 `step_code` 允许在编排里
            # 出现多次，唯一的是 `node_id`）；`space_id` 是一道一致性核对（对不上按
            # 找不到处理）；`operator_id` 取 `r.user_id`，落 `tasks.updated_by` ——
            # 同 `task.stop` / `task.init_task` 的口径：纯记账，拿不到就空，不是漏记。
            #     `data` 是**空**的（见 `_retry_subtask_action`）：成败看 `success` / `code`。
            "task.subtask_retry": lambda r, d: self._execute(
                lambda s: self._retry_subtask_action(s, r, d)
            ),
            # 批量重跑（§4.2 ④）：一次点名几个节点、**按给的那个顺序依次跑**（第 i 步等
            # 第 i-1 步成功）。存在的理由很具体：「重新解析」这类操作要重做的不止一步
            # （解析 + 编译），而那两步在编排里不是直连的（`compile_1.depends_on` 是
            # `ingest_1`，不是 `parse_1`）—— 分别调两次单步重试会让它们**同时**开跑。
            #     `node_ids` 的顺序由 service 落成依赖边（见 `TaskService.retry_subtasks`），
            # 这里原样往下传、不重排。
            "task.subtask_retry_batch": lambda r, d: self._execute(
                lambda s: self._retry_subtasks_action(s, r, d)
            ),
            # 停止任务：管理方自己就能办（判死没开始的子任务 + 就地收口），收口之后
            # **尽力而为**地给运行方发一条 `task_stopped` 通知（§4.1 ③，单发不重发、
            # 失败只记 warning）—— 但正在跑的那个子任务**杀不掉**（通知不是命令），
            # 留给它的上报按实际结果填。
            "task.stop": lambda r, d: self._execute(
                lambda s: self._task_service(s).stop_task(
                    d["task_id"], r.tenant_id, r.user_id or None,
                    # 空间可见性判定要身份（同 `task.console_page` / `task.console_detail`）。
                    # 拿不到身份（内部链路 / 测试 token）就是 `None` → 守卫按既有口径放行。
                    user_id=r.user_id,
                )
            ),
            "task.delete": lambda r, d: self._execute(
                lambda s: self._task_service(s).delete_task(d["task_id"], r.tenant_id)
            ),
            # ── 发起任务 init_task（§4.2 ②，运行方调的口子）──
            # `operator_id` 取 `r.user_id`：运行方是把请求上下文里的
            # `user_id` 一起带上来的，落 `tasks.created_by`。拿不到就是空 —— 触发器
            # 和内部系统调用本来就没人，留空是对的，不是漏记。
            "task.init_task": lambda r, d: self._execute(
                lambda s: self._task_service(s).init_task(
                    r.tenant_id,
                    d["definition_id"],
                    d["space_id"],
                    d.get("inputs") or {},
                    version_no=d.get("version_no"),
                    name=d.get("name"),
                    operator_id=r.user_id or None,
                    # 调用方自带的 id（可选）：不传 = 管理方生成。撞了已有任务抛
                    # `ResourceAlreadyExistsError`（1006）—— 对自带 id 的调用方那是
                    # 「复用既有任务」而不是拒绝（`SubmissionDisposition.REUSED`）。
                    task_id=d.get("task_id") or None,
                    # 任务来源（**必传**，已在 `_REQUIRED_KEYS` 里查过「键在不在」；
                    # 取值合不合法由 service 判，非法 1001）。
                    task_source=d.get("task_source"),
                )
            ),
            # ── 运行方上报（§4.2 ①，运行方改子任务状态的唯一入口）──
            #     `report_status` 只做**报文级校验**（`status` 取值 + 条件必填）就回来
            #     报「受理」—— 真正的裁决、落库、填下游、收尾由后台循环那一趟消化
            #     （§7.5 ②）。所以 **`success=true` 的含义是「已受理」、不是「已落库」**：
            #     运行方收到成功响应后立刻查任务状态，可能还看不到变化（要等后台那一轮）。
            #     `data` 仍是空（`report_status` 返回 `None`，`_plain(None)` → `None`）。
            # 上报：**报文先过一遍类型外壳**（`ReportStatusRequest`，2026-09-30 接进来）
            # —— 它与必填白名单是两件事，顺序与分工见 `_report_status_action`。
            "task.report_status": lambda r, d: self._execute(
                lambda s: self._report_status_action(s, r, d)
            ),
            # ── 管理端只读（§4.4，前端任务页面调的两个）──
            #     挂在 `task.*` 命名空间下，但**不是内部契约**：它们是 §4.4 破例开给
            #     前端的两个只读接口，字段口径按页面来（见 `dtos/console.py`）。
            #     写成独立 action、而不是让网关拿 `task.list` 自己拼：网关是薄代理，
            #     不做业务拼装（CLAUDE.md 后端基线）。
            "task.console_page": lambda r, d: self._execute(
                lambda s: self._task_service(s).page_tasks(
                    r.tenant_id,
                    keyword=d.get("keyword"),
                    start_time=_parse_iso_time(d.get("start_time"), "start_time"),
                    end_time=_parse_iso_time(d.get("end_time"), "end_time"),
                    statuses=_flatten_status_param(d.get("status")),
                    space_id=d.get("space_id"),
                    # 任务来源筛选（2026-10-13 加，**非必填**）：∈ 协议层 `TASK_SOURCES`，
                    # 非法值由 service 当场拒（1001）。不传 = 不按来源筛。
                    task_source=d.get("task_source"),
                    # 空间权限判定要身份（成员 / 租户管理员两档，见 `space_guard`）。
                    # 身份只从 invoke 报文来（网关 `identity.py` 解 JWT 后透传），
                    # 拿不到就是 `None` → 守卫按既有口径放行（内部链路 / 测试 token）。
                    user_id=r.user_id,
                    **_page_params(d),
                )
            ),
            "task.console_subtask_list": lambda r, d: self._execute(
                lambda s: self._task_service(s).list_subtasks_console(
                    d["task_id"], _attempt_param(d), r.tenant_id,
                    # 空间可见性判定要身份（2026-10-13 补齐，与 `task.console_page` 同口径）。
                    user_id=r.user_id,
                )
            ),
            # ── 详情两个（§4.4，2026-10-13 加）──
            #     返回体与上面两个列表**各取一行完全一致**，闸门也照各自的列表镜像：
            #     两处都带**空间可见性判定**（2026-10-13 补齐）—— 列表那侧是靠收敛可见空间
            #     集合实现的，详情没有「列表」可收敛，必须显式判一次；子任务那侧同理
            #     （原先它「只判租户归属」，是自记在 §4.4 的缺口）。
            "task.console_detail": lambda r, d: self._execute(
                lambda s: self._task_service(s).get_task_console(
                    r.tenant_id, d["task_id"], user_id=r.user_id
                )
            ),
            "task.console_subtask_detail": lambda r, d: self._execute(
                lambda s: self._task_service(s).get_subtask_console(
                    d["task_id"], d["node_id"], tenant_id=r.tenant_id,
                    user_id=r.user_id,
                )
            ),
            # ── 管理端写（§4.4，2026-09-29 新增的三个）──
            #     三个都返回**任务对象**（同 `task.stop` 的形状），前端拿它刷新那一行。
            #     `operator_id` 一律取 `r.user_id`（网关解 JWT 填进 invoke 报文体），
            #     落 `tasks.updated_by`；拿不到就空 —— 纯记账，不参与任何判定。
            #
            # 停止子任务：**只能停还没开始的**（`pending`），`running` / 终态一律拒 1003
            # —— 运行方没有「停止」动作，已派出去的活平台杀不掉（与 `task.stop` 同口径，
            # 见 `TaskService._do_stop_subtask`）。判死之后**必须自己让任务收口**。
            "task.console_subtask_stop": lambda r, d: self._execute(
                lambda s: self._task_service(s).stop_subtask(
                    d["task_id"], d["node_id"], r.tenant_id, r.user_id or None,
                    user_id=r.user_id,
                )
            ),
            # 重试整条任务：同一条任务**原地重跑**（新建一次运行、任务 id 不换），不是
            # 新建一条任务。底层就是 §4.3 的 `retry_task`，这里只是给它开了个前端面
            # —— **空间可见性那道闸就由 `user_id` 触发**（内部那条 `task.retry` 不传，
            # 行为一字不变）。
            "task.console_task_retry": lambda r, d: self._execute(
                lambda s: self._task_service(s).retry_task(
                    d["task_id"], r.tenant_id, user_id=r.user_id
                )
            ),
            # 重试某一步：底层是 §4.2 ③ / §4.3 的 `retry_subtask`，差在 `space_id` ——
            # 那条契约要运行方传它做一致性核对，前端没有空间上下文，由 service 从任务行
            # 推导（见 `TaskService.console_retry_subtask`），**既有 action 一个字不动**。
            "task.console_subtask_retry": lambda r, d: self._execute(
                lambda s: self._task_service(s).console_retry_subtask(
                    d["task_id"], d["node_id"], r.tenant_id, r.user_id or None,
                    user_id=r.user_id,
                )
            ),
        }

    # ── 会话与返回值 ──────────────────────────────────────

    def _task_service(self, session: Any) -> TaskService:
        """任务实例服务：统一注入本能力共用的运行方适配器。

        `TaskService` 平时只跑编排、收上报、填值、汇总这些纯库内动作；`task.delete`
        例外 —— 它要经适配器出站请运行方清掉自己侧整个任务的残留（§4.3）。适配器
        整模块共用一份（`self._adapter`），连接池因此复用，随能力关闭一起释放。
        """
        return TaskService(session, runner_adapter=self._adapter)

    async def _execute(self, factory: Callable[[Any], Any]) -> Optional[Dict[str, Any]]:
        """开一个事务跑一个 action，并把 service 的返回值转成可 JSON 化的裸数据。

        **派活不在这里**：后台循环（`scheduler_service`）自己持有适配器与连接池，
        发现子任务能跑了才出站调运行方的 `execute`（§7.5 ①）；这里经 `_task_service`
        拿到的适配器只服务于 `task.delete` 的出站删除。
        """
        async with get_db_session() as session:
            result = await factory(session)
        return _plain(result)

    async def _report_status_action(
        self, session: Any, request: CapabilityRequest, data: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """`task.report_status` 的动作体：**先把报文过一遍类型外壳**（2026-09-30 接进来）。

        `ReportStatusRequest`（`dtos/execution.py`）从前是「声明了但全仓没人构造」的类型
        外壳。这里让它真管一件事：**字段的类型**（那几个定位 / 状态字段必须是字符串、
        `ts` 必须是整数、`outputs` 必须是对象…）。

        ⚠️ **三件事互补，谁也替不了谁**：

        - `_REQUIRED_KEYS`（在 `execute()` 里，**比这里早**）：判「键在不在」。
          缺 `space_id` 之类仍然是 `1001 + details.missing` —— **那条报错契约没变**；
        - **这个 DTO**：判「类型对不对」；
        - `_check_report_payload`（service 层）：判「**条件必填**与取值」
          （`success` 必带 dict `outputs` / `failed` 必带 `error_message` /
          `status` 在白名单里）—— **一个字节都不能删**，它才是那几条规则的唯一实现
          （DTO 里那三个字段是 `Optional`，管不着）。

        ⚠️ **`tenant_id` 不在 `data` 里**：它是**身份**（`request.tenant_id`），不是报文里的
        业务字段，所以 `_REQUIRED_KEYS` 里**故意没有**它、而这个 DTO **声明了**它。
        必须先显式补进去再解析，否则**正常的派活报文会必挂在这一步**。
        """
        payload = _from_request(
            ReportStatusRequest, {**data, "tenant_id": request.tenant_id}
        )
        return await self._task_service(session).report_status(
            request.tenant_id,
            payload.space_id,
            payload.execution_id,
            payload.task_id,
            payload.status,
            outputs=payload.outputs,
            error_message=payload.error_message,
            # 失败原因的代号 + 参数（2026-10-10 加）。⚠️ **必须在这里透传** ——
            # DTO 的 `extra="ignore"` 会静默吃掉没声明的键，而"漏传"的方向是**功能静默失效**
            # （代号写不进库 → 读侧只能显示冻住的那句话），不报错、测试也不红。
            error_code=payload.error_code,
            error_params=payload.error_params,
            ts=payload.ts,
            output_refs=payload.output_refs,
            # 输入「从哪儿来」的引用（2026-10-16 加）：**同样必须在这里透传**。
            # 这一层是**逐字段显式传参**的 —— DTO 收到了、service 也认这个参数，
            # 中间漏一笔就是「报文明明带了、库里却是 NULL」，而且不报错、测试也不红。
            input_refs=payload.input_refs,
        )

    async def _retry_subtask_action(
        self, session: Any, request: CapabilityRequest, data: Dict[str, Any]
    ) -> None:
        """`task.subtask_retry` 的动作体：**返回值一定是 `None`（回包 `data` 为空）**。

        §4.2 ③ 是「运行方 → 管理方」的写口子，运行方看的就是 `success` / `code` 那一个
        结论。service 那边会把重试后的子任务快照返回上来（内部调用方如控制台要它），
        但**这条路上把它丢掉**：重试成没成跟「那一步现在长什么样」是两件事，把它塞进回包
        等于把内部快照升成对外契约，以后想改都改不动。要看那一步的现状就调
        `task.console_subtask_list`（§4.4 那两个只读口子）。
        """
        await self._task_service(session).retry_subtask(
            data["task_id"],
            data["node_id"],
            tenant_id=request.tenant_id,
            space_id=data["space_id"],
            operator_id=request.user_id or None,
        )

    async def _retry_subtasks_action(
        self, session: Any, request: CapabilityRequest, data: Dict[str, Any]
    ) -> None:
        """`task.subtask_retry_batch` 的动作体：**返回值一定是 `None`（回包 `data` 为空）**。

        与 `_retry_subtask_action` 逐字同一条理由，只是把事情从一步扩到几步：§4.2 ④ 也是
        「运行方 → 管理方」的写口子，运行方看的就是 `success` / `code`；service 返回的那份
        快照（内部调用方要）在这条路上**同样丢掉** —— 一次批量重跑成没成，跟「这几步现在
        长什么样」是两件事。要看现状就调 `task.console_subtask_list`（§4.4）。

        `node_ids` **原样往下传**：它的顺序就是执行顺序，这里不许排序、不许去重（去重该
        在 service 里判成 1001 拒掉，不是悄悄替调用方抹平）。
        """
        await self._task_service(session).retry_subtasks(
            data["task_id"],
            data["node_ids"],
            tenant_id=request.tenant_id,
            space_id=data["space_id"],
            operator_id=request.user_id or None,
        )

def _internal_error_code(exc: Exception) -> int:
    """把「内部异常」按**实际类型**映射成错误码（§4.0「响应结构」）。

    文档把这条写死了：**「内部异常」→ 按实际类型给**（数据库 → `DatabaseError`、
    Redis → `CacheError`、依赖不可达 → 5xxx 那几个），**别一律拿 `InternalError` 兜**
    —— 它太笼统，调用方没法据此判断该不该重试。

    按类型认领，认不出来的才落到 `InternalError`（它是真正的最后一档，不是默认档）：

    - `SQLAlchemyError` → `DatabaseError`（4001，4xxx 数据类、可重试）
    - `RedisError` → `CacheError`（4002，同上）—— **只认 Redis 自己的异常类**
      （`redis.exceptions.RedisError` 及其子类，它的 MRO 里**没有**内建
      `ConnectionError` / `OSError`，实测如此），所以这一个分支是明确的「缓存故障」
    - `TimeoutError` → `ServiceTimeoutError`（5004）—— 「依赖调用超时」
    - `ConnectionError` / `OSError` → `ServiceUnavailableError`（5001）——
      「依赖服务不可用」。**对象存储、外部 HTTP 这类依赖连不上抛的就是它们**
    - 其余 → `InternalError`（1000）

      ⚠️ **1000 不在任何一张码表里** —— 调用方 `classify_code` 会把它判成
      `CLASS_UNKNOWN`，于是这次失败被归进「**未知 → 可以原样重发**」。上面那句「这四个
      码全在 `TASK_CENTER_RETRYABLE_CODES` 里、所以这次分档不改变重试行为」**只对
      4001 / 4002 / 5001 / 5004 成立**，不含这一档。就「未知」的字面意思重发可辩护
      （真因还没查明），所以**保持现状**；写在这里是免得读的人以为兜底档也「不影响重试」。

    ⚠️ **顺序不能动**：`ConnectionError` 与 `TimeoutError` **都是 `OSError` 的子类**，
    `TimeoutError` 要排在 `ConnectionError` / `OSError` **前面**，否则超时会被报成
    「不可用」；`RedisError` 排在最前是因为它最具体（而且它跟内建那几个**没有**继承
    关系，见上）。

    **这四个码（4001 / 4002 / 5001 / 5004）全在 `TASK_CENTER_RETRYABLE_CODES` 里**，
    所以这次分档**不改变调用方的重试行为**，只把 `code` 与文案对准真实成因 —— 从前
    `OSError` 一律并进 4002，对象存储连不上会被报成「缓存操作失败」，排查方向被带偏到
    Redis 上。⚠️ **`task_lock.py` 那道翻译不一样、不要跟着这里改**：那边是「抢锁路上」
    的翻译，那一段**只碰 Redis 一个依赖**，四种异常都是它抛的，所以统一翻成 `CacheError`
    是对的；这里是「漏到最外层」的兜底，异常可能来自任何一个依赖，才需要按类型拆开。

    **回调用的只是 `code`**：`details` 里那些内部细节不外传（`execute` 的注释写了）。
    """
    if isinstance(exc, SQLAlchemyError):
        return DatabaseError.code
    if isinstance(exc, RedisError):
        return CacheError.code
    if isinstance(exc, TimeoutError):
        return ServiceTimeoutError.code
    if isinstance(exc, (ConnectionError, OSError)):
        return ServiceUnavailableError.code
    return InternalError.code


def _from_request(model: Any, data: Dict[str, Any]) -> Any:
    """用**请求报文**构造 DTO —— 校验不通过就是「参数非法」，不是「执行失败」。

    `trig.create` / `trig.update` 的参数要先过一遍各自的 DTO（字段白名单在那边）。
    pydantic 校验不过抛的是 `ValidationError`，**它不是 `JonexException`** ——
    会一路漏到 `execute` 的兜底 `except`，回一句「执行失败」+ 一个按类型给的码
    （4xxx / 5xxx / 1000，见 `_internal_error_code`），而正确答案是 **1001 参数非法**
    （调用方自己就能改）。

    **只包「请求报文 → DTO」这一步**：服务内部把**库里的行**转成 DTO 时校验不过，
    那是数据问题、是 bug，不该被说成"你参数给错了" —— 所以这是一个就地的小包装，
    不是 `execute` 里一条大范围的 `except ValidationError`。
    """
    try:
        return model(**data)
    except ValidationError as exc:
        raise _invalid_from_validation(exc) from exc


def _invalid_from_validation(exc: Exception) -> InvalidParameterError:
    """把 pydantic 的 `ValidationError` 翻成 1001，字段名和要求原样带给调用方。

    **字段名不算内部细节** —— 那是调用方自己给的字段，报出来他才知道改哪儿。
    """
    errors = [
        {
            "field": ".".join(str(part) for part in err.get("loc", ())),
            "message": err.get("msg", ""),
        }
        for err in exc.errors()
    ]
    summary = "；".join(f"{e['field']}: {e['message']}" for e in errors)
    return InvalidParameterError(
        message=translate(
            "err.task_center.param.invalid",
            params={"errors": summary},
            fallback=f"参数没通过校验：{summary}",
        ),
        details={"errors": errors},
    )


def _plain(value: Any) -> Any:
    """把 DTO / DTO 列表转成裸数据，让响应能直接 JSON 化（§4.3：`data` 可为对象或列表）。"""
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if hasattr(value, "dict"):
        return value.dict()
    return value


def _page_params(data: Dict[str, Any]) -> Dict[str, int]:
    """解析并校验分页参数（§4.0）：`page` 从 1 起、`page_size` 必须是正整数。

    七个"列一批"的 action 共用这一处（`def.list` / `trig.list` / `trig.runs` /
    `task.list` / `task.attempt_list` / `task.subtask_attempt_list` /
    `task.console_page`），口径才不会各写各的。

    **`page_size` 在这一层不设上限**：想一次全拉出来就传大一点（比如 1000），能力入口
    不拦也不报错——这是设计文档明写的，不是漏了。但 0 / 负数 / 非整数一律当「参数非法」
    拒掉：负的 `LIMIT` 会被 PostgreSQL 直接抛错（冒出一个 500），在入口挡住才叫讲道理。
    键不在、或显式传 `null`，都算「没给」、走默认值。

    **网关侧同样不设上限**：`api_gateway/routes/task_center.py` 只有 `GET /tasks` 一个
    路由收 `page_size`（子任务列表不分页、回裸数组，§4.0），它原先照抄别处卡了 `le=100`，
    已去掉 —— 页大小只该由调用方按自己一次能渲染多少行决定，后台循环的 `BATCH` 是另一
    回事、不受这里影响。
    """
    page = data.get("page")
    page_size = data.get("page_size")
    return {
        "page": _require_positive_int(1 if page is None else page, "page"),
        "page_size": _require_positive_int(
            20 if page_size is None else page_size, "page_size"
        ),
    }


def _require_positive_int(value: Any, name: str) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    raise InvalidParameterError(
        message=translate(
            "err.task_center.param.positive_int_required",
            params={"name": name},
            fallback=f"参数 {name} 必须是正整数",
        ),
        details={"param": name},
    )


def _attempt_param(data: Dict[str, Any]) -> Optional[int]:
    """解析并校验可选的 `attempt`（第几次运行）：不给 = 当前那次运行。

    **必须在入口校验**，不能像 `filters` 那样原样透传给 service：坏值会一路走到
    `get_by_attempt_no` 的 SQL，PostgreSQL 抛 `invalid input syntax for type integer`，
    被 `_internal_error_code` 归成 `SQLAlchemyError` → `DatabaseError`(4001) —— 而 4001
    **在 `TASK_CENTER_RETRYABLE_CODES` 里**。按 §4.0 的判定规则，调用方会保留信封、
    下一轮重发这个**永远不可能成功**的请求；一个纯参数错误就这样被记成了依赖故障。

    口径与 `_page_params` 完全一致（同一个 `_require_positive_int`）：
    键不在 / 显式 `null` / 空串都算「没给」，其余非正整数一律 1001。
    """
    attempt = data.get("attempt")
    if attempt is None or attempt == "":
        return None
    return _require_positive_int(attempt, "attempt")


def _parse_iso_time(value: Any, name: str) -> Optional[datetime]:
    """把 ISO 8601 字符串解析成**本地时间的 naive datetime**（§4.4 的时间范围筛选）。

    库里存的是 naive 本地时间，`models/base.py` 的 `iso()` 也是按本地时区加偏移输出；
    所以带时区（含 `Z`）的入参要先换算到本地、再抹掉 tzinfo，否则跟库里的值比会偏。
    解析不了的回 1001「参数非法」，不静默当没给 —— 时间筛错了结果会全量，很难发现。
    """
    if value is None or value == "":
        return None
    text = str(value).strip()
    if text[-1:] in ("Z", "z"):  # `datetime.fromisoformat` 在部分版本不认裸 `Z`
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise InvalidParameterError(
            message=translate(
                "err.task_center.param.bad_iso_time",
                params={"name": name, "value": value},
                fallback=f"参数 {name} 不是合法的时间（要 ISO 8601 格式）：{value}",
            ),
            details={"param": name},
        )
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _flatten_status_param(values: Any) -> Optional[list]:
    """把 `status` 参数摊平成字符串列表（重复键与逗号分隔两种写法都认）。

    前端按 §4.4 的约定传重复键（`?status=pending&status=running`，配合 axios 的
    `paramsSerializer: { indexes: null }`）；逗号分隔（`status=pending,running`）
    顺手也认 —— 写法不同就静默查空，属于「不报错但结果不对」，最难查。

    **取值合法性不在这儿判**：入口只查「在不在」，取值归 service（§4.2 的口径）。
    """
    if values is None:
        return None
    raw_items = values if isinstance(values, (list, tuple)) else [values]
    flattened: list = []
    for item in raw_items:
        for part in str(item).split(","):
            part = part.strip()
            if part and part not in flattened:
                flattened.append(part)
    return flattened or None


__all__ = ["TaskCenterCapability"]
