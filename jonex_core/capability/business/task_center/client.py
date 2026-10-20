#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""任务中心 Client：`TaskCenterClient` 抽象 + 本机 / 远程 / 桩实现 + 工厂。

设计出处：`docs/superpowers/specs/2026-09-09-task-center-design.md` §4.1、§4.2、§4.4。

**接入方拿的是这个，不是任务中心的内部实现**：§4.3 那一整套内部函数（`def.*` /
`trig.*` / `task.list` / `task.retry` …）**一个都不在这里** —— 它们是任务中心自己的
service 方法，别的模块 import 不到，也不经这个 client 走（§4.4 明写）。

**这个 client 是「任务中心这一个能力的双向契约」** —— 它把两个方向的约定放在同一个
ABC 里：**方法名都在，但"谁实现、谁调用"是反的**，别搞混：

| 方向 | 方法 | 谁实现 | 谁调用 | 归哪节 |
| --- | --- | --- | --- | --- |
| **运行方 → 任务中心** | `init_task` / `report_status` / `retry_subtask` / `retry_subtasks` | 任务中心（管理方） | **运行方** | §4.2 |
| **任务中心 → 运行方** | `execute` / `remove_task` / `task_stopped` | **运行方** | 任务中心（管理方） | §4.1 |
| **任务中心 → 运行方**（读） | `query_step_status` | **运行方**（可不实现） | 任务中心（管理方） | §4.1 ④ |

- **运行方**：拿 `get_task_center_client()` **调** `init_task` / `report_status`
  / `retry_subtask` / `retry_subtasks`；同时**在自己那边实现** `execute` / `remove_task`（§4.1，这是接入门槛）；
- **管理方调 §4.1 那两个**：**不直接经 `TaskCenterClient`** —— 而是经进程内的「运行方适配器」
  `capabilities/task_center/flows/runner_adapter.py` 拿到具体运行方再调。所以本文件的
  Local / Remote / Mock 对这两个方法**只回一个"方向不对"的失败壳**（见 `TaskCenterClient`
  里那两个方法的说明），它们的存在只是为了把契约放齐、让运行方照着实现。

**每个方法都返回统一响应壳 `ApiResponse`**（§4.0）：先看 `success` 判断这次调用通没通，
再取 `data`。所以**调用不成的业务情形（找不到 / 参数非法 / 冲突）不抛异常**，
而是 `success=false` + `code` / `message` 回来。

**判「这事成没成」只看 `code`**（§4.0）：`success` 不带原因，所以**结论标签**看
`TASK_CENTER_RETRYABLE_CODES` 这个集合，**不认 `message` 文案**。

⚠️ **但「发不发第二次」不看这个集合** —— SDK 的重试循环只对**成功那一档**收手，
`REJECTED` 与 `RETRYABLE_UNKNOWN` 一样会重发满预算（见 `runtime.py` 四个方向的循环）。
**码改的是回给调用方的结论，不是发几次**。别照上面那句去自己实现重试包装。

**说话方式跟 `get_rag_client()` 一模一样**，由能力清单决定：

- `LOCAL`：进程内直调（任务中心跟调用方在同一进程）；
- `REMOTE`：POST Sidecar `/invoke`，反代到独立部署的任务中心进程；
- `MOCK`：桩实现，离线开发 / 单测用，不碰库。

租户：每次调用进门前都经 `_resolve_tenant` 收口一次，收不出合法租户（为空 / 默认租户 /
含非法字符）就**按契约回 `success=false` 的失败壳**，不抛 `TenantIsolationError` ——
不会出现「没租户也能调」的口子，也不会逼调用方套 try。

> **这是故意的设计（不是这期偷懒）**：任务中心不提供面向浏览器 / 前端的网络接口。
> 以后前端要看任务，由接入方（宿主功能模块）经自己的后端拉数据再展示。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from jonex_core.capability.locator import CapabilityMode, get_locator
from jonex_core.capability.models import CapabilityRequest
from jonex_core.capability.registry import get_capability_registry
from jonex_core.capability.response import ApiResponse
from jonex_core.common.config import get_config
from jonex_core.common.exceptions import (
    InvalidParameterError,
    OperationNotSupportedError,
    PermissionDeniedError,
    RateLimitExceededError,
    ResourceNotFoundError,
    ServiceTimeoutError,
    ServiceUnavailableError,
    TenantIsolationError,
    UpstreamServiceError,
)
from jonex_core.common.i18n import transmit_locale_header
from jonex_core.common.tenant import TenantContext, require_tenant
from jonex_core.task_protocol.results import TASK_CENTER_RETRYABLE_CODES

if TYPE_CHECKING:
    # 只为本文件的返回类型标注服务（文件头开了 `from __future__ import annotations`，
    # 标注在运行期是字符串）。**故意不在运行期 import**：接入方（如知识库）拿这个
    # client 时不该被动把任务中心的实现包整条链拉起来。
    #
    # 标注的来源是**协议层**（`jonex_core/task_protocol/models.py`）而不是
    # `capabilities.task_center.dtos.*`：内核不该反向依赖某个业务能力的 DTO 包，
    # 而这两个报文的定义本就该只有一份（运行方 SDK 用的也是同一份）。
    from jonex_core.task_protocol.models import ExecutionDispatch, TaskItemResponse

logger = logging.getLogger(__name__)

# 能力清单里任务中心的能力 ID（§4.4）。
TASK_CENTER_CAPABILITY_ID = "business.task_center.v1"

# `TASK_CENTER_RETRYABLE_CODES`（§4.0 的可重试码表）**定义在 `jonex_core/task_protocol/results.py`**，
# 本模块顶部把它 re-export 进来 —— 客户端与运行方 SDK 必须用同一份判定，而协议层不得
# 反向 import `jonex_core.capability.*`（门禁），所以常量只能住协议层。逐条码值表与
# 「未知 ≠ 拒绝」那条判定规则都在那边。


def _resolve_tenant(tenant_id: Optional[str]) -> str:
    """把「方法入参 → 当前上下文」这条回落链收口成一个合法租户。

    显式入参优先；没给就取 `TenantContext`（请求进来时由中间件 / `tenant_scope` 绑好）。
    这条回落链就是文件头承诺的那句「带 `tenant_id` 的照传，没带的取当前上下文」——
    收不出合法租户时 `require_tenant` 抛 `TenantIsolationError`，由调用方翻成失败壳。
    """
    return require_tenant(tenant_id or TenantContext.get())


class TaskCenterClient(ABC):
    """任务中心接入契约（§4.4）：**就这 9 个方法**（§4.1 三个 + §4.2 四个 + §4.3 两个读 `get_subtask` / `get_task`），§4.3 其余的内部函数一个都不在这里。

    「可重试」的码见 `TASK_CENTER_RETRYABLE_CODES` —— **它的定义在
    `jonex_core/task_protocol/results.py`**，本模块只是把它 re-export 出来（理由见上面
    那条注释：客户端与运行方 SDK 必须共用同一份判定，而协议层不能反向依赖
    `jonex_core.capability.*`）。**调用方据此读「成没成」**，`message` 只给人看；
    ⚠️ **不要据此自己实现重发**（发几次由 SDK 的重试循环定，见模块 docstring 里那条 ⚠️）。
    """

    # ========================================================
    # §4.1 我（管理方）调运行方 —— 三个，**由运行方实现**
    # ========================================================

    @abstractmethod
    async def execute(self, execution: ExecutionDispatch) -> ApiResponse[None]:
        """派活：让运行方跑某个节点的活（§4.1 ①）。

        **方向是「任务中心 → 运行方」，实现方是运行方**：任务中心的后台循环扫到某个子
        任务能跑了（`depends_on` 里每个节点都 `success`），经 `runner_adapter.py` 找到
        这一步绑定的运行方、调它这个方法。`data` 为空。

        报文里的 `step_code` 是**内置步骤名**（parse / ingest / compile）、不是编排
        节点 id；运行方按它分流到不同执行模式。`execution_id` 由管理方生成、随派活传
        下来，运行方上报时要原样带回。

        **超时不归本 client 管**：真正把报文发出去的是管理方的 `runner_adapter`，
        那边的超时是 `TASK_CENTER_RUNNER_TIMEOUT`——跟接入方调用用的
        `TASK_CENTER_CLIENT_TIMEOUT` 是**两个方向的两条配置**，别混。
        """

    @abstractmethod
    async def remove_task(
        self, tenant_id: str, space_id: str, task_id: str
    ) -> ApiResponse[None]:
        """把整个任务在运行方侧的残留一次清干净（§4.1 ②）。

        **方向同上、实现方是运行方**。管理方删任务时调的就是它（§4.3 `task.delete`）：
        **按 `task_id` 一把清** —— 运行方最清楚自己按什么维度存数据（可能按任务建目录、
        按任务建表），"清一个任务"是它内部一次自然的操作。`data` 为空。

        **超时同 `execute`，不归本 client 管**：走的是管理方 `runner_adapter` 的
        `TASK_CENTER_RUNNER_TIMEOUT`。
        """

    # ========================================================
    # §4.2 运行方调我（管理方）—— 只有这四个写（下面的两个「读」是 §4.3 开出来的口子）
    # ========================================================

    @abstractmethod
    async def task_stopped(
        self, tenant_id: str, space_id: str, task_id: str
    ) -> ApiResponse[None]:
        """**通知**运行方：这条任务被用户停了（§4.1 ③，2026-09-30 加）。

        ⚠️ **与 `execute` / `remove_task` 同**：方向是「管理方 → 运行方」，**由运行方实现**、
        管理方经 `runner_adapter` 调用 —— 它在这个 ABC 里只是为了「两个方向的约定放在同一处」，
        **不经过 `TaskCenterClient`**（这里调它只会拿到一个「方向不对」的失败壳）。

        契约本体（幂等要求、为什么要有它、运行方该怎么做）在
        `jonex_core/task_runtime/runner.py` 的 `RunnerBase.task_stopped`。
        """

    # ── 读（§4.3 `task.subtask_get` / `task.get`，2026-09-30 开给运行方）──

    @abstractmethod
    async def get_subtask(
        self,
        tenant_id: str,
        task_id: str,
        node_id: str,
        *,
        attempt: Optional[int] = None,
    ) -> ApiResponse[Dict[str, Any]]:
        """**读**某一步现在的状态（§4.3 `task.subtask_get`）。**纯只读。**

        ── 为什么要有它 ──

        运行方从前**没有任何通道**能读管理方的状态：这个 client 的四个方法
        （`init_task` / `report_status` / `retry_subtask` / `retry_subtasks`）**全是写口子**。
        而文档里 §4.2 又明叫运行方「要读状态请用 §4.3 的 `task.subtask_get`」，§4.3 却写着
        「不进 `TaskCenterClient`、运行方调不到」—— **被指了路、却没有路**。于是运行方只能
        「猜」：它没法知道「我这一步是不是已经被管理方判死了」，只能靠自己的超时硬等
        （典型是 30 分钟、最长 6 小时）。

        典型用法：**在放弃之前问一句**。看门狗发现某一步迟迟没动静、准备按自己的超时
        交还时先读一次这里 —— 如果管理方那边它早就是终态了，就不用再等那 30 分钟。

        ── 两个定位字段 ──

        - `task_id` + `node_id` 定位那一步（`node_id` 是**编排里的节点名**，如 `parse_1`；
          同一个 `step_code` 允许在编排里出现多次，唯一的是它 —— 与派活报文里那个同源）；
        - `attempt` 不传 = **当前那次运行**；给了 = 那一次运行（跨运行会重复出现同名节点，
          要指认历史某次必须带上它）。

        ── 返回 ──

        `data` 是**裸 dict**（形状同 `SubtaskItemResponse`：`node_id` / `status` /
        `error_message` / `current_attempt` / `execution_id` / `outputs` …），**不是模型对象**
        —— `ApiResponse.from_dict` 不做子模型校验，且 `task_runtime` / `task_protocol`
        **不得 import `capabilities.*`**（有测试钉住那道门禁），而那个 DTO 住在能力层。
        所以这里按 `init_task` 的先例回 dict、**由调用方自己投影需要的字段**
        （`TaskRef.from_payload` 就是这么做的）。

        **`status` 就是这一步的当前状态**：`pending` / `running` / `success` / `failed` /
        `timeout`。**看到后三个就说明管理方那边它已经定局了** —— 运行方据此可以停止等待。

        ⚠️ **读接口不判权**（与 §4.3 其余读函数一致）：租户与身份由 Sidecar 保证，
        越权按「找不到」（1002）处理。
        """

    @abstractmethod
    async def get_task(
        self, tenant_id: str, task_id: str
    ) -> ApiResponse[Dict[str, Any]]:
        """读**整条任务**现在的状态（§4.3 `task.get`）。**纯只读。**

        与 `get_subtask`（读某一步）是一对，**但两者不能互相替代** —— 运行方要判
        「这条编排是不是已经结束了」，光看**某一步**是不够的：

        ⚠️ **典型反例（这正是加它的原因）**：用户把任务**停了**。`task.stop` 只判死
        **还没开始**的那些步骤，**正在跑的那一步原地不动**（运行方没有停止能力，杀了也
        白杀）—— 所以那一刻「那一步」的状态**仍然是 `running`**，而「这条任务」已经是
        `failed` 了。只看步骤就永远看不出「停了」，只能死等自己的硬超时（最长 6 小时）。

        `data` 是裸 dict（形状同 `TaskItemResponse`，含 `status` / `current_attempt` /
        `error_message` …），理由与 `get_subtask` 逐字相同。
        """

    @abstractmethod
    async def init_task(
        self,
        tenant_id: str,
        definition_id: str,
        space_id: str,
        inputs: Optional[Dict[str, Any]] = None,
        *,
        version_no: Optional[int] = None,
        name: Optional[str] = None,
        operator_id: Optional[str] = None,
        task_id: Optional[str] = None,
        task_source: Optional[str] = None,
    ) -> ApiResponse[TaskItemResponse]:
        """**初始化一条任务**（§4.2 ②）：`data` 是 `TaskItemResponse`。

        运行方要起一条新任务时调它 —— 典型场景：知识库上传完文件，要发起一条"文档处理"
        任务。**它就是"对外的 `fire`"**：跟 §4.3 的 `fire` 同一套校验、同一套落库，
        只是换个对外名字（改内部实现不该影响对外契约）。

        **不经过触发器**：`tasks.trigger_id` 为 NULL。**不做幂等去重** —— 调两次就建两条
        独立任务，要防重复发起得由调用方自己按业务标识去重。

        **`task_id` 是可选的自带 id**（发起方自己生成）：给了就用它，**不传由管理方生成**。
        发起方要先把 id 落到自己的行上（不然只能等这个调用回来才知道 id 写什么）就填它。

        **撞了已有任务回 `ResourceAlreadyExistsError`（1006）**：它**不是「明确拒绝」**。
        对自带 id 的调用方，它意味着「上一次其实已经建成了，只是响应没回来」—— 重发撞上
        的正是自己上次建的那条，所以按「复用既有任务」收工（`SubmissionDisposition.REUSED`），
        **不要换 id 重来**。SDK 也正是靠这条把重试做成幂等的（见 `TaskRuntime.submit`）。

        调用完就不用管了：任务中心的后台调度循环会自动接手（扫到 → 抢占 → 派给运行方
        `execute`）。`version_no` 不传 = 用该定义"已发布的、版本号最大"的那一版
        （**绝不悄悄回落到草稿**）。

        **`space_id` = 这次运行落在哪个空间**，必须是 `scope=space` 的那个版本自带的
        空间（传不一致直接失败）。

        **`name` = 这次任务的名字**（可选）：给了就用，**没给、或只给空白，退回定义名**
        （`task_definitions.name`）。它只是给人看的标签，不影响跑哪一版 —— 因为
        `init_task` 不做幂等去重，同一个定义可以起多条任务，光看定义名分不清谁是谁。

        **`operator_id` = 发起这次上传的人**（可选，`user_id`）：管理方原样落
        `tasks.created_by`。**纯记账** —— 不校验、不参与任何判定，拿不到就留空
        （触发器自动触发、内部系统调用本来就没有人，空着是对的，不是漏记）。

        **`task_source` = 任务来源**（2026-10-13 加）：∈ 协议层 `TASK_SOURCES`
        （写死枚举，如 `knowledge_base.file.manual_upload`），管理方原样落 `tasks.task_source`。
        标注 `Optional` 只是签名兼容；**这条路（`init_task`）上它是必传的** ——
        `TaskRuntime.submit` 出站前判一次、管理方收到再判一次（域外值 1001）。
        ⚠️ **它必须走 `data` 这条通道**（同 `operator_id` / `task_id`）：local 模式下
        `data` 是三层 client 唯一共用的通道，塞报文体顶层会丢。

        **超时**（只对 `mode: remote` 有意义）：用 `TASK_CENTER_CLIENT_INIT_TASK_TIMEOUT`
        （默认 5 秒），**不是** `TASK_CENTER_CLIENT_TIMEOUT`（默认 30 秒）—— 后者管的是
        `report_status` 那条。单独给一个更短的值，是因为**上传路径是在事务里调它的**：
        等它多久，那笔事务（连同 `quota:doc:{租户}` 的 advisory lock）就持有多久，
        同一个租户的其它上传会一起排在这把锁上。
        """

    @abstractmethod
    async def report_status(
        self,
        tenant_id: str,
        space_id: str,
        execution_id: str,
        task_id: str,
        status: str,
        outputs: Optional[Dict[str, Any]] = None,
        error_message: Optional[str] = None,
        ts: Optional[int] = None,
        output_refs: Optional[List[Dict[str, Any]]] = None,
        error_code: Optional[str] = None,
        error_params: Optional[Dict[str, Any]] = None,
        input_refs: Optional[List[Dict[str, Any]]] = None,
    ) -> ApiResponse[None]:
        """上报结果（**运行方改状态的唯一入口**，§4.2 ①）：`data` 为空。

        ⚠️ **`error_code` / `error_params`（2026-10-10 加）**：失败原因的**代号 + 参数**。
        代号取自协议层写死的枚举 `ERROR_REASON_CODES`（**域外值当场拒**，不允许运行方乱填），
        管理方存下来、**读的时候**按请求的 `X-Lang` 现场渲染 —— 写上报的往往是后台循环，
        那一刻没有请求上下文、`translate()` 只能拿到默认语言，所以「写的那一刻就翻译」
        会把语言冻住（这是它存在的唯一理由）。`error_message` 仍然要带（引擎自由文本 /
        排障快照 / 老数据兜底）。

        **入参全部平铺**（没有 `event` 这层嵌套），`execution_id` 是定位符 ——
        管理方按 `tenant_id` + `space_id` + `execution_id`（三者缺一不可）定位到唯一那一行
        `subtasks`，再往下核「是不是当前运行」「是不是已经终态」；任何一道不过就
        **丢弃这条上报、仍回 `success=true`**（幂等吸收，让运行方别重试）。

        - `status`：`success` / `failed` —— **运行方只报得了这 2 种**。
          `running` 不再是合法取值：`execution_id` 是派活抢占时跟「`pending → running`」
          写在同一条 UPDATE 里的，运行方拿到凭证时子任务**必然已经在跑**，再报一句
          `running` 什么也改变不了，所以这条路整个关掉；
          `pending` / `timeout` 由管理方自己置（诞生或重试、超时巡检）；
        - `outputs`：`status=success` 时必带（**契约字段**：下游 `@节点.outputs.x` 取的就是它）；
        - `output_refs`：可选，产出「**去哪儿看**」的引用（对象数组，展示用、**不参与取参**）——
          管理方只搬运、原样回给控制台；`kind` / `label` / `href` 由运行方自定义，未知键忽略；
        - `input_refs`：可选，输入「**从哪儿来**」的引用（同款对象数组，展示用、**不参与取参**）——
          元素只有 `kind` + `params`，`kind` 必须 ∈ 协议层 `INPUT_REF_PARAMS`（写死枚举，
          **值要带运行方前缀**，如 `knowledge_base.document`）；管理方只搬运、原样回给控制台。
          **可选**是给老运行方留的：不带照常受理（设成必填会把失败上报本身拒掉）；
        - `error_message`：`status=failed` 时必带；
        - `ts`：运行方侧的事件时间（13 位毫秒时间戳）—— 管理方**只落库留痕**
          （`subtask_reports.ts` 一列，**不打日志**）、**不参与任何裁决**
          （运行方的钟不可信）；
        - `task_id`：**不参与定位**，是**交叉校验**（见下一段）。

        ── `task_id`：同步阶段的一致性校验，对不上**当场拒** ──

        定位明明用不着它（凭证本身就唯一），为什么还要？因为**报文的这几个标识必须互相对
        得上**：管理方在**受理之前**（同步阶段）拿它做两道核对 ——

        1. 报文声称的「任务 ↔ 空间」是不是真的：查 `tasks`，`task.space_id` 与报文
           `space_id` 不符 → 拒（`ResourceNotFoundError`，1002）；
        2. 这张凭证是不是真属于这条任务：按 `(tenant_id, space_id, execution_id)` 定位
           `subtasks`，命中行的 `task_id` 与报文 `task_id` 不符 → 拒（1002）。

        拒 = **`success=false` 且这条上报不入队**（连收件箱都不进）。它落进 SDK 的
        `PERMANENT_REJECTED` 那一档 —— 注意 SDK **照样重发 3 次**（判据是 `code` 落在哪张
        表，不是"重发有没有意义"），3 次都不成才把失败交回调用方。

        ⚠️ **两道核对都"查不到"时按查不到办，不按对不上办**：
        - **任务查不到**（已被删 / id 压根不存在）→ **照旧受理**（后台再丢弃）。这两种在
          库里长得一模一样，分不出来，而"任务删了之后运行方迟到上报"是正常生命周期；
        - **凭证查不到**（重试把旧 `execution_id` 清掉了 / 迟到很久）→ **照旧受理**。这条
          是既有明文规定的幂等吸收路径，**绝不能拒** —— 否则重试之后的旧凭证上报会收到
          一个硬错。

        一句话：**报文自相矛盾 → 拒；报文只是过期 → 不拒。**

        ⚠️ 这次核对**要看一眼库**（按主键 / 唯一索引单行查一次），所以同步阶段那条边界
        从「不查库」改成了「**不拿锁、不写库**」。读到的是没拿锁的旧值，只用来拒明显矛盾
        的报文，**不作为任何裁决**（裁决仍在后台拿锁那次重读）。

        **返回 `success=true` 只代表「已受理」，不代表「已经落库」**：管理方收到后先把这条
        存进收件箱，真正的裁决与落库由后台调度循环那一趟完成（§7.5 ②）—— 所以运行方收到
        成功响应后**立刻**查任务状态，可能还看不到变化。这条上报最终是接受、还是因重复 /
        过期 / 任务已删被丢弃，同样由管理方内部裁决，不通过响应回给运行方。

        **报文本身不合法会当场拒**（`status` 取值不对、`success` 缺 `outputs`、
        `failed` 缺 `error_message`）：那是 `success=false` + `InvalidParameterError` 的码，
        **原样重发结果一样** —— 这条上报**根本不会被受理**，得改报文。（SDK 在出站前就把
        这三种在本地挡住了，见 `TerminalReport`，所以它们不会真的发到这里来。）

        只要**被受理**就**一定被处理到底**：拿不到任务锁只是管理方「本轮跳过、下轮再来」，
        **不再回可重试码**（§7.5 ②）。

        **重发由 SDK 兜**：`TaskRuntime.report_terminal` 对「没被受理」的响应会自动重发
        （重复上报在服务端是安全的 —— 收件箱不加唯一约束，重复那条会被「已是终态」那道闸
        吸收）。所以**运行方自己不需要实现任何重试逻辑**。SDK 那一层仍然可能把失败壳交
        回来（3 次都没被受理），那时怎么处置由调用方定。

        **超时**（只对 `mode: remote` 有意义）：用 `TASK_CENTER_CLIENT_TIMEOUT`
        （默认 30 秒）。它不在任何事务里等，所以不需要像 `init_task` 那样单独收窄。
        """

    @abstractmethod
    async def retry_subtask(
        self,
        tenant_id: str,
        space_id: str,
        task_id: str,
        node_id: str,
        *,
        operator_id: Optional[str] = None,
    ) -> ApiResponse[None]:
        """**重跑某一步**（§4.2 ③）：`data` 为空 —— 成败看 `success` / `code`。

        **按 `node_id` 寻址，不按 `step_code`**：同一个 `step_code` 可以在编排里出现多次
        （`parse_1` / `parse_2` 都是 `parse`），编排里能唯一指认一步的只有 `node_id`。
        它是**不透明标识** —— 原样传回来就行，别解析它的形状。

        **`space_id` 是一道一致性核对**（不是定位符，定位靠 `task_id` 这个主键）：跟那条
        任务实际所属的空间不符 → 按**找不到**处理（1002）。口径跟 `report_status` 那道
        「任务 ↔ 空间」核对一致，两个写口子一个形状。

        **`operator_id` = 是谁点的这次重试**（`user_id`）：管理方落 `tasks.updated_by`。
        **纯记账** —— 不校验、不参与任何判定。拿不到就传 `None`（空值是合法状态，不是漏记）；
        这里做成必填的关键字参数是为了逼调用方显式想一下"这是谁"，而不是让它悄悄漏掉。

        ── 三条前提（不满足就**明确拒绝**，不是可重试）──

        1. **任务已经跑完**（`completed` / `failed`）—— 跑着的时候一律不许重试单步
           （运行期靠"这批子任务的状态"判断能不能收尾，中途动一个会把收尾判定搅乱）；
        2. **那一步也已经结束**（`success` / `failed` / `timeout`）—— 还没结束的没什么
           可重试的；
        3. **它的上游全都 `success`** —— 上游没成功时这一步要的入参根本填不出来，重试也是
           白跑，直接拒，让调用方先去解决上游。

        `success` 那一步**也允许重试**（「重跑这一步」就是这么用的）：新的产出会**覆盖**
        旧的；**依赖它的下游会跟着一起重跑**（见下）。

        ── **连带下游**（2026-10-11 起）──

        点某一步重跑时，**依赖它的下游会被一起重置、按原顺序重跑**。因为下游的入参是从
        上游产出里按 `bind_inputs` 填出来的：上游出了新产出、下游还用旧的，整条链就是
        「上游新、下游旧」，而调用方**看不出来**（下游那一步还是 `success`）。

        前提因此变成**两条**：

        · **上游必须 `success`**（原来那条，没变）；
        · **下游必须都已经是终态**（`success` / `failed` / `timeout`）—— 有任何一个下游
          还在 `pending` / `running`，整次重跑被**明确拒绝**（它正拿着上一轮的输入在跑，
          这时候把上游重做一遍，两边就对不上了）。

        返回的仍是**起点那一步**的快照；被连带重置的下游不在返回里（要看它们的新状态
        请查子任务列表）。

        验证与重置**全程在任务锁内、同一个事务**：要么整批重置、要么一个字不动。

        ── 幂等：**上一次其实成了、只是响应没回来，也一样回成功** ──

        重试成功后那一步回到 `pending`（`current_attempt` 已 +1）。所以第二次重发撞上的
        是「任务在跑、那一步已经结束了吗」这个问题 —— 管理方据此**回成功**而不是报
        「那一步还没结束」（判据：任务被拉回跑着、那一步的 `current_attempt` 已经大于 1、**且那一步还没跑完**
        —— 三条同时成立才判「重试生效」。⚠️ **「还没跑完」这半句不能省**：那一步若
        已经重跑完、又回到终态（而任务里别的步骤还在跑、任务本身没终态），重发撞的是
        「任务不是终态」那道前提、回 1003，客户端会误判成「没重试成功」而让调用方换参数
        再点一次。三条判据的唯一出处是 `jonex_core/task_protocol/results.py` 的
        `SubtaskRetryDisposition.RETRIED`，服务端 `_retry_already_in_flight` 同口径）。**这是 SDK 敢在这条路上重发的前提** —— 没有它，
        一次网络抖动就会让调用方看到"重试失败"、而实际上重试已经生效。

        **超时**同 `report_status`（`TASK_CENTER_CLIENT_TIMEOUT`，默认 30 秒）。
        """

    @abstractmethod
    async def retry_subtasks(
        self,
        tenant_id: str,
        space_id: str,
        task_id: str,
        node_ids: List[str],
        *,
        operator_id: Optional[str] = None,
    ) -> ApiResponse[None]:
        """**重跑某几步**（§4.2 ④）：`data` 为空 —— 成败看 `success` / `code`。

        **`node_ids` 的顺序就是执行顺序**（第 i 步等第 i-1 步成功）。这是它与「连着调两次
        `retry_subtask`」的全部区别：那两次会把两步**同时**放回队列。什么时候非它不可 ——
        要重跑的两步在编排里**不互为直接上下游**时（例如三步链里的「解析」与「编译」，
        中间隔着「入库」）：直接上游都还 `success` 着，两步就会一起跑起来。管理方把顺序
        落成依赖边（`depends_on`），调度侧一个字都不用改。

        **一次调用 = 一批 = 一个事务**：要么这几步全回队列，要么一个字没动，没有部分成功。

        **前提**与单步逐条相同（任务已跑完 / 每一步都已结束 / 每一步**批外**的上游都已
        成功），只是对名单里每一步各查一遍。「批外」是刻意的：批内的上游正要跟着重跑，
        要求它「现在就 `success`」反而是错的。

        **名单本身坏了也是明确拒绝**（1001）：空名单 / 有重复（重复等于让某一步等自己）/
        顺序跟编排的依赖方向拧着来（会成环 → 那几步互相等、永远跑不出来）。

        **`space_id` / `operator_id`** 与 `retry_subtask` 逐字同一条口径（一致性核对；
        是谁点的这次重跑，落 `tasks.updated_by`，纯记账、拿不到就传 `None`）。

        ── 幂等：**上一次其实成了、只是响应没回来，也一样回成功** ──

        判据是**整批**都已经排上队（每步 `current_attempt > 1` 且 `pending` / `running`）——
        批量是一个事务，不存在"一半落库了"，所以只有"整批都生效了"才回成功；有一步还是
        老样子就说明上一次整批都没生效，照常走前提。

        **超时**同 `report_status`（`TASK_CENTER_CLIENT_TIMEOUT`，默认 30 秒）。
        """


# ============================================================
# 动作拼装：本机直调与远程共用一份
# ============================================================

# §4.2 四个动作名。**只在这里定义一次** —— `_TaskCenterActions` 发送时用它、
# `RemoteTaskCenterClient` 按它选超时也用它，免得两处各写各的字符串、改一处漏一处。
# （与 `TaskCenterCapability._build_dispatch` 的键一一对应。）
_ACTION_INIT_TASK = "task.init_task"
_ACTION_REPORT_STATUS = "task.report_status"
_ACTION_SUBTASK_RETRY = "task.subtask_retry"
_ACTION_SUBTASK_RETRY_BATCH = "task.subtask_retry_batch"
# 读（§4.3，2026-09-30 开给运行方）：
_ACTION_SUBTASK_GET = "task.subtask_get"
# 读整条任务（同上，2026-09-30）：运行方的「退路」必须能问出**任务**是不是终态，
# 只看某一步是不够的（见 `TaskRuntime.get_task` 的说明）。
_ACTION_TASK_GET = "task.get"


def _timeout_for(action: str) -> float:
    """这次出站调用该等多久（秒）—— 按动作取，定义在 `TaskCenterSettings`。

    **只有 `REMOTE` 通道用得上**：`local` 是进程内直调、`mock` 是桩，都不走 HTTP，
    没有「超时」这个概念。

    `init_task` 单独一个更短的值（`TASK_CENTER_CLIENT_INIT_TASK_TIMEOUT`，默认 5 秒），
    其余方法用 `TASK_CENTER_CLIENT_TIMEOUT`（默认 30 秒）。分开的理由：**上传路径是在
    事务里调 `init_task` 的**，等它多久那笔事务（连同 `quota:doc:{租户}` 的
    advisory lock）就持有多久，同一个租户的其它上传会一起排在这把锁上。
    """
    config = get_config()
    if action == _ACTION_INIT_TASK:
        return float(config.TASK_CENTER_CLIENT_INIT_TASK_TIMEOUT)
    return float(config.TASK_CENTER_CLIENT_TIMEOUT)


class _TaskCenterActions(ABC):
    """§4.2 四个方法：只管「参数怎么装、动作叫什么」，不管怎么送出去。

    本机直调与经 Sidecar 反代用的是**同一串动作名**（与 `TaskCenterCapability._build_dispatch`
    的键一一对应），差别只在 `_call` 的实现，所以这份参数拼装只写一遍。

    **§4.1 那两个不在这里** —— 它们的方向是「管理方 → 运行方」，不由本 client 送出。
    """

    @abstractmethod
    async def _call(
        self, tenant_id: Optional[str], action: str, data: Dict[str, Any]
    ) -> ApiResponse[Any]:
        """把一次调用送出去。

        `tenant_id` 为 None 时由实现经 `_resolve_tenant` 回落到当前上下文；
        收不出合法租户、能力不在注册中心、传送层失败——一律回 `success=false` 的失败壳
        （带具体 `code`），**不抛异常**：调用方只看 `success`（§4.0）。

        三条失败路各自落在哪一档（调用方按 `code` 分类，见 `TASK_CENTER_*_CODES`）：
        租户收不出来 = 3005（拒绝）、**本进程没装这个能力 = 1002（拒绝 —— 装配错，
        重发不会好）**、传送层失败 = 5xxx（可重发 —— 结果未知）。
        """

    # ── §4.1 两个：方向不对，只回失败壳 ──
    #
    # 这两个是**运行方**要实现的（任务中心出站去调）。本 client 是给运行方"调管理方"
    # 用的，运行方自己不会来调 `execute` / `remove_task`，管理方调它们
    # 也不走 client（走 `runner_adapter.py`）。放在这里是为了**把双向契约凑齐、让运行方
    # 照着这份签名实现**。
    #
    # 真被调到时**回一个说清原因的失败壳、不抛异常**（跟本文件其它方法一个口径：
    # 调用不成就是 `success=false`），免得"静默成功"把方向搞反这件事糊过去。

    async def execute(self, execution: Any) -> ApiResponse[None]:
        return self._wrong_direction("execute")

    async def remove_task(
        self, tenant_id: str, space_id: str, task_id: str
    ) -> ApiResponse[None]:
        return self._wrong_direction("remove_task")

    async def task_stopped(
        self, tenant_id: str, space_id: str, task_id: str
    ) -> ApiResponse[None]:
        return self._wrong_direction("task_stopped")

    @staticmethod
    def _wrong_direction(method: str) -> ApiResponse[None]:
        message = (
            f"{method} 的方向是「任务中心 → 运行方」，由运行方实现、管理方经 "
            f"runner_adapter 调用，不经过 TaskCenterClient"
        )
        logger.warning("TaskCenterClient.%s 被调用了，但方向不对：%s", method, message)
        return ApiResponse.fail(message)

    # ── §4.2 四个 ──

    async def init_task(
        self,
        tenant_id,
        definition_id,
        space_id,
        inputs=None,
        *,
        version_no=None,
        name=None,
        operator_id=None,
        task_id=None,
        task_source=None,
    ):
        return await self._call(
            tenant_id,
            _ACTION_INIT_TASK,
            {
                "definition_id": definition_id,
                "space_id": space_id,
                "inputs": inputs,
                "version_no": version_no,
                "name": name,
                # 发起这次上传的人（`user_id`），管理方落 `tasks.created_by`。放在 `data`
                # 里而不是塞进报文体顶层：这条路不走网关，`data` 是三层 client（local /
                # remote / mock）唯一共用的通道，塞别处 local 模式下就丢了。
                "operator_id": operator_id,
                # 调用方自己生成的 id（不传/传 `None` = 管理方生成）。同样只有 `data`
                # 这一条通道 —— 塞报文体顶层在 local 模式下会丢。
                "task_id": task_id,
                # 任务来源（协议层写死枚举），管理方落 `tasks.task_source`。
                # 同上：只有 `data` 这一条通道。
                "task_source": task_source,
            },
        )

    async def report_status(
        self,
        tenant_id,
        space_id,
        execution_id,
        task_id,
        status,
        outputs=None,
        error_message=None,
        ts=None,
        output_refs=None,
        error_code=None,
        error_params=None,
        input_refs=None,
    ):
        return await self._call(
            tenant_id,
            _ACTION_REPORT_STATUS,
            {
                "space_id": space_id,
                "execution_id": execution_id,
                # 不参与定位（定位靠凭证），是管理方同步阶段的一致性核对：报文声称的
                # 「任务 ↔ 空间」与「凭证属于哪条任务」都要对得上，对不上当场拒。
                "task_id": task_id,
                "status": status,
                "outputs": outputs,
                "error_message": error_message,
                # 失败原因的代号 + 参数（可选）：管理方存下来、读时按 `X-Lang` 渲染。
                # 不传（老运行方）就只落 `error_message`，行为一字不变。
                "error_code": error_code,
                "error_params": error_params,
                "ts": ts,
                "output_refs": output_refs,
                # 输入「从哪儿来」的引用（可选，与 `output_refs` 对称的那份）。
                # 新字段一律加在末尾：上面那几个是**承重的位置参数**（见 `ports.py`），
                # 插在中间就等于重排。
                "input_refs": input_refs,
            },
        )

    async def retry_subtask(
        self,
        tenant_id: str,
        space_id: str,
        task_id: str,
        node_id: str,
        *,
        operator_id: Optional[str] = None,
    ) -> ApiResponse[None]:
        return await self._call(
            tenant_id,
            _ACTION_SUBTASK_RETRY,
            {
                "space_id": space_id,
                "task_id": task_id,
                # 编排里唯一能指认「哪一步」的就是它（`step_code` 允许重复）。不透明标识，
                # 管理方那边按它定位那一行 `subtasks`。
                "node_id": node_id,
                # 是谁点的这次重试，管理方落 `tasks.updated_by`。走 `data` 这条通道 ——
                # `data` 是三层 client 唯一共用的那条，塞报文体顶层在 local 模式下会丢
                # （`init_task` 的 `operator_id` 同理）。
                "operator_id": operator_id,
            },
        )

    async def retry_subtasks(
        self,
        tenant_id: str,
        space_id: str,
        task_id: str,
        node_ids: List[str],
        *,
        operator_id: Optional[str] = None,
    ) -> ApiResponse[None]:
        return await self._call(
            tenant_id,
            _ACTION_SUBTASK_RETRY_BATCH,
            {
                "space_id": space_id,
                "task_id": task_id,
                # **顺序就是执行顺序**（第 i 步等第 i-1 步成功），管理方按原样落成依赖边。
                # ⚠️ 这里**绝不许排序或去重**：排序会改掉「先后」，去重会替调用方把一个
                # 拼错的名单悄悄抹平（重复该由管理方判成 1001 明确拒）。原样传。
                "node_ids": node_ids,
                # 是谁点的这次重跑（同 `retry_subtask`）。
                "operator_id": operator_id,
            },
        )


    async def get_task(
        self, tenant_id: str, task_id: str
    ) -> ApiResponse[Dict[str, Any]]:
        """读整条任务现在的状态（§4.3 `task.get`）。**纯只读、无幂等要求。**"""
        return await self._call(tenant_id, _ACTION_TASK_GET, {"task_id": task_id})

    async def get_subtask(
        self,
        tenant_id: str,
        task_id: str,
        node_id: str,
        *,
        attempt: Optional[int] = None,
    ) -> ApiResponse[Dict[str, Any]]:
        """读某一步现在的状态（§4.3 `task.subtask_get`）。**纯只读、无幂等要求。**"""
        data: Dict[str, Any] = {"task_id": task_id, "node_id": node_id}
        # `attempt` 只在给了的时候才塞：不传 = 「当前运行」。
        # ⚠️ **别把「不塞」写成「有语义」** —— 管理方那侧（`capabilities/task_center/capability.py`
        # 的 `_attempt_param`）对「键不在 / 显式 `null` / 空串」**一律**按「没给」处置，
        # 所以塞不塞 `None` 结果一样。这里不塞只是让报文干净。
        if attempt is not None:
            data["attempt"] = attempt
        return await self._call(tenant_id, _ACTION_SUBTASK_GET, data)


# ============================================================
# 本机直调
# ============================================================


class LocalTaskCenterClient(_TaskCenterActions, TaskCenterClient):
    """本机直调：任务中心与调用方在同一进程（能力清单里 `mode: local`）。"""

    def __init__(self, capability_id: str = TASK_CENTER_CAPABILITY_ID) -> None:
        self._capability_id = capability_id

    async def _call(
        self, tenant_id: Optional[str], action: str, data: Dict[str, Any]
    ) -> ApiResponse[Any]:
        try:
            tenant_id = _resolve_tenant(tenant_id)
        except TenantIsolationError as exc:
            # 收不出合法租户：按契约回失败壳，**不抛**（§4.0）。`exc.message` 本身就
            # 分得清「默认租户」与「含非法字符」，`exc.code`（3005）一并回给调用方。
            logger.warning("任务中心调用缺合法租户 action=%s：%s", action, exc.message)
            return ApiResponse.fail(exc.message, code=exc.code)
        registry = get_capability_registry()
        if registry.get_capability(self._capability_id) is None:
            # ⚠️ **这一档必须带码，而且必须落在「明确拒绝」那一档** —— 不能让它吃
            # `ApiResponse.fail()` 的默认码 `-1`。回 `-1` 时 `classify_code` 判
            # `CLASS_UNKNOWN`（保守可重发），SDK 会把 3 次预算全烧在一条**永远不会好**
            # 的调用上：这是**装配错**（清单说 `mode: local`，但没人把它注册进来、
            # 或者名字改了），人不去改，重发多少次结果都一样。日志也只看得出「未知」。
            #
            # 码取 `ResourceNotFoundError`（1002，在拒绝码表里）：与平台里「能力找不到」
            # 的既有口径同族（Sidecar 那侧能力未配置回 `CapabilityNotFoundError`，也是
            # 「找不到」），也与派活侧对**同一个情形**的判决一致
            # （`capabilities/task_center/flows/runner_adapter.py::_dispatch_local` 判
            # `DispatchDisposition.REJECTED` = 当场判死；它不出站，所以没有码这一层）。
            message = f"任务中心 {self._capability_id} 不在本进程的能力注册中心里"
            logger.error(
                "任务中心调用被拒 action=%s（清单说是 local，但注册中心里没有 %s）",
                action,
                self._capability_id,
            )
            return ApiResponse.fail(message, code=ResourceNotFoundError.code)
        response = await registry.invoke(
            self._capability_id,
            CapabilityRequest(
                tenant_id=tenant_id,
                capability_id=self._capability_id,
                # `data.operator_id` 提到 `user_id` 上，让「本机直调」与「经 Sidecar
                # 反代」两条路在能力侧看到的东西一致 —— 那边一律读 `request.user_id`，
                # 不因为部署画像不同而读到不同字段（§4.0 的 mode 可切约定）。
                user_id=data.get("operator_id") or "",
                payload={"action": action, "data": data},
            ),
        )
        # `code` 必须一路带回去：§4.0 要求失败时给具体错误码，运行方按 `code` 走
        # `TASK_CENTER_RETRYABLE_CODES` 判重试、按语言包翻文案，只回 `message` 会让
        # 调用方拿不到可判定的码。
        return ApiResponse(
            success=response.success,
            code=response.code,
            message=response.message,
            data=response.data,
        )


# ============================================================
# 远程（经 Sidecar 反代）
# ============================================================


class RemoteTaskCenterClient(_TaskCenterActions, TaskCenterClient):
    """经 Sidecar `/invoke` 反代到独立部署的任务中心进程（能力清单里 `mode: remote`）。

    **超时按动作取**：构造函数上挂的是默认值 `TASK_CENTER_CLIENT_TIMEOUT`（30 秒），
    每次请求再用 `_timeout_for(action)` 覆写一次 —— `init_task` 走更短的
    `TASK_CENTER_CLIENT_INIT_TASK_TIMEOUT`（5 秒），其余方法走默认值。
    两条配置各自管哪些方法、为什么这么分，见 `TaskCenterSettings` 里的说明。
    """

    def __init__(
        self,
        endpoint: str,
        capability_id: str = TASK_CENTER_CAPABILITY_ID,
        options: Optional[Dict[str, Any]] = None,
    ) -> None:
        import httpx

        # 这里挂的是**默认值**（`TASK_CENTER_CLIENT_TIMEOUT`，30 秒），不再是 os.getenv 直读。
        # 真正用哪个值由 `_call` 按动作经 `_timeout_for` 逐请求覆写，别在这里写死。
        timeout = float(get_config().TASK_CENTER_CLIENT_TIMEOUT)
        self._client = httpx.AsyncClient(
            base_url=endpoint.rstrip("/"),
            headers={"X-API-Key": get_config().GATEWAY_API_KEY},
            timeout=(options or {}).get("timeout", timeout),
        )
        self._capability_id = capability_id
        logger.info("任务中心 Client 初始化：REMOTE 模式，endpoint=%s", endpoint)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(
        self, tenant_id: Optional[str], action: str, data: Dict[str, Any]
    ) -> ApiResponse[Any]:
        """POST Sidecar `/invoke`，把返回的响应壳原样还原成 `ApiResponse`。

        Sidecar 的响应体本身就是 `{request_id, success, code, message, data, details, params}`
        ——**只有一层**（不像派活那条路，运行方的回执还套在里面一层 data 里）。
        连不上 / 不是 JSON 这种传送层问题，也按 `success=false` 回，不抛异常：
        client 的契约就是「看 success」，调用方不必为一次调用套 try。

        **传送层失败也要带对 `code`**（这点不能让默认的 `-1` 兜着）—— 调用方判「要不要
        原样重发」只认 `TASK_CENTER_RETRYABLE_CODES`，而传送层失败几乎全是**结果未知**：
        请求可能已经送到、被受理了，只是响应没回来。回 `-1`（不可重试）的后果是运行方
        把协调信封一清，这个终态**永远不再重报**，管理方只能等自己的看门狗把子任务判超时
        ——真实成功被记成超时。所以这里按时限 / 连不上 / 响应不合法分别映射成 5004 / 5001 /
        5003，只有「收到了明确的拒绝（HTTP 4xx）」才留在不可重试那一边。

        **超时逐请求定**：`action` 经 `_timeout_for` 换成这次该等的秒数（`init_task`
        比其余方法短），覆写构造时挂的默认值 —— 构造那一个只兜底、不决定实际行为。
        """
        import httpx

        try:
            tenant_id = _resolve_tenant(tenant_id)
        except TenantIsolationError as exc:
            # 同本机直调：收不出合法租户就回失败壳，不抛（§4.0）。
            logger.warning("任务中心调用缺合法租户 action=%s：%s", action, exc.message)
            return ApiResponse.fail(exc.message, code=exc.code)
        body = {
            "capability_id": self._capability_id,
            "payload": {"action": action, "data": data},
            "tenant_id": tenant_id,
        }
        # 身份的**唯一**承载字段是顶层 `user_id`（Sidecar `/invoke` 认它，见
        # `jonex_core/sidecar/main.py`）。这里把 `data.operator_id` 提上来，是为了让
        # 「经 Sidecar 反代」和「本机直调」在能力侧落到同一个字段 —— 那边只读
        # `request.user_id`，不按部署画像分叉（§4.0 的 mode 可切约定）。
        operator_id = data.get("operator_id")
        if operator_id:
            body["user_id"] = operator_id
        headers = {"X-Tenant-ID": tenant_id}
        # `X-Lang` 必须透传：失败时 `message` 是任务中心按这个 header 翻好的文案（`translate`
        # 走的是任务中心进程里的 locale），接入方拿到多半直接展示给人看。不透传的话，
        # 英文界面的用户会收到中文报错——`jonex_core/sidecar/proxy.py` 转发时也是这么做的。
        transmit_locale_header(headers)
        try:
            resp = await self._client.post(
                "/invoke", json=body, headers=headers, timeout=_timeout_for(action)
            )
            resp.raise_for_status()
            payload = resp.json()
        except httpx.TimeoutException as exc:
            # 超时：请求**很可能已经送到并被受理了**，只是响应没回来 —— 这次的结果是不
            # 确定的。按 `ServiceTimeoutError`（5004，可重试）回，让调用方保留协调信封
            # 原样重发；管理方那边按 `execution_id` 幂等吸收，重发不会重复计算。
            logger.warning("任务中心调用超时 action=%s：%s", action, exc)
            return ApiResponse.fail(
                f"调用任务中心 {action} 超时：{exc}", code=ServiceTimeoutError.code
            )
        except httpx.HTTPStatusError as exc:
            # 收到了 HTTP 响应，但状态码不是 2xx —— 注意这是**传送层**的失败，不是业务
            # 答复（业务答复恒为 200 + `success=false`，那条路从最后 `from_dict` 出）。
            if exc.response.status_code >= 500:
                # 对端自己坏了（Sidecar 500 / 网关 502）。请求进去了没有、做到哪一步，
                # 从这一个响应里判不出来 → 同样按「结果未知」可重试（5001）。
                logger.warning("任务中心调用失败 action=%s：%s", action, exc)
                return ApiResponse.fail(
                    f"调用任务中心 {action} 失败：{exc}",
                    code=ServiceUnavailableError.code,
                )
            # 4xx **要分两个码，别一律不给码**：
            #
            # - **429 → `RateLimitExceededError`（3007）**：Sidecar 的治理层限流回的
            #   （`jonex_core/sidecar/main.py` 的 `/invoke`，`RATE_LIMIT_TENANT_PER_MINUTE`
            #   默认 300/分钟、`RATE_LIMIT_ENABLED` 默认开）—— **过一会儿再来就能进**，
            #   属可重试那一档。不给码会落 `-1`（`CODE_FAIL`）→ `classify_code` 判
            #   `CLASS_UNKNOWN`，结论一样是「可重发」，但**日志和调用方都看不出这是限流**。
            # - **其余 4xx → `PermissionDeniedError`（3006）**：401/403（内部 API Key 配错）、
            #   404（Sidecar 路径配错）都是**这一侧的装配 / 身份不对，原样重发结果一样**，
            #   落拒绝那一档。⚠️ 用同一个码是**故意的**：码在这里的作用是让调用方与日志
            #   一眼看出「**这是被拒，不是未知**」，不是精确复述 HTTP 语义 —— 真正的原因
            #   在 `message` 里（带 URL 与状态码），照 `message` 排查即可。
            #
            # ⚠️ **别把这里读成「码决定重发次数」**：SDK 的重试循环**只对「成功」收手**
            # （`jonex_core/task_runtime/runtime.py` 的 `submit` / `report_terminal` /
            # 重试子任务三处，都只判成功档），`REJECTED` 与 `RETRYABLE_UNKNOWN` 一样会重发
            # 满 3 次。码改的是**回给调用方的结论标签**，不是发不发。
            status = exc.response.status_code
            code = (
                RateLimitExceededError.code
                if status == 429
                else PermissionDeniedError.code
            )
            log = logger.warning if status == 429 else logger.error
            log("任务中心调用被拒 action=%s HTTP %s：%s", action, status, exc)
            return ApiResponse.fail(
                f"调用任务中心 {action} 被拒：{exc}", code=code
            )
        except httpx.HTTPError as exc:
            # 连不上 / DNS 解析不了 / 连接中途断了：请求要么没送出去，要么送出去了但不知道
            # 对面收没收到 —— 也是「结果未知」，按可重试回（`ServiceUnavailableError` 5001）。
            logger.warning("任务中心调用失败 action=%s：%s", action, exc)
            return ApiResponse.fail(
                f"调用任务中心 {action} 失败：{exc}",
                code=ServiceUnavailableError.code,
            )
        except ValueError as exc:
            # 响应不是合法 JSON：对端回的报文不符合契约，这一次有没有被受理判不出来。
            # 归到 `UpstreamServiceError`（5003，上游产出不对），同样是可重试那一档。
            logger.warning("任务中心响应无法解析 action=%s：%s", action, exc)
            return ApiResponse.fail(
                f"任务中心 {action} 的响应无法解析：{exc}",
                code=UpstreamServiceError.code,
            )
        except TypeError as exc:
            # ⚠️ **报文压根构造不出来**：`json=body` 的编码发生在 `httpx.post()` 内部，
            # 遇到不可 JSON 化的值（`UUID` / `datetime` / `set` / dataclass 实例…）抛的是
            # `TypeError` —— 上面四个 except 一个都不覆盖它，它会**穿出本方法**，再穿出
            # `TaskRuntime` 那四个重试循环（那里全是 `for` + `await`、没有 `try`），
            # 最后以未预期的异常交到调用方手上 —— 而按本模块的契约，出站失败**一律**回
            # 失败壳、不抛（调用点常常在事务里，异常会把整笔事务带走）。
            #
            # 归 `InvalidParameterError`（1001）：真因是**调用方给的 `inputs` / 节点名单
            # 里有不可序列化的值**，是「参数非法」而不是「对端坏了」—— 原样重发结果一样。
            # （管理方那一侧对同一类事实走的是 `call_with_retry` 的 `except Exception`
            # → `DELIVERY_UNKNOWN`；方向相反、结论不必相同：那边要防的是「运行方可能已经
            # 在跑」，这边是「请求根本没构造出来」。）
            logger.error("任务中心报文无法构造 action=%s：%s", action, exc)
            return ApiResponse.fail(
                f"调用任务中心 {action} 的报文无法构造（参数里有不可序列化的值）：{exc}",
                code=InvalidParameterError.code,
            )
        # ⚠️ **`from_dict` 也可能抛**（`code=int(payload.get("code", ...))`：`code` 是
        # `null`/字符串会抛 `TypeError`/`ValueError`；`payload` 不是对象还会 `AttributeError`），
        # 而它在上面那四个 except **之外** —— 抛出去同样会击穿「出站失败一律回壳、不抛」这条
        # 契约。正常路径上这一层只有 Sidecar，它的 `InvokeResult.code: int` 会让 FastAPI 在
        # 响应校验期就拦掉坏 `code`（变成 500 → 走上面的 `HTTPStatusError` 分支）；这里兜的是
        # 「中间还有一层自建代理 / 网关错误页」那种形态。**判据与 `resp.json()` 失败同一档**
        # （上游产出不对、可重试），不另开一档。
        if not isinstance(payload, dict):
            logger.warning(
                "任务中心响应不是 JSON 对象 action=%s：%s", action, type(payload).__name__
            )
            return ApiResponse.fail(
                f"任务中心 {action} 的响应不是 JSON 对象（收到 {type(payload).__name__}）",
                code=UpstreamServiceError.code,
            )
        try:
            return ApiResponse.from_dict(payload)
        except (TypeError, ValueError) as exc:
            logger.warning("任务中心响应信封不合法 action=%s：%s", action, exc)
            return ApiResponse.fail(
                f"任务中心 {action} 的响应信封不合法：{exc}",
                code=UpstreamServiceError.code,
            )


# ============================================================
# 桩（离线开发 / 单测）
# ============================================================


class MockTaskCenterClient(_TaskCenterActions, TaskCenterClient):
    """桩实现：只回「收到了」，不碰库也不派活（能力清单里 `mode: mock`）。

    给离线开发和单测用——接入方代码接上它就能跑通调用链，不需要起任务中心进程。

    **§4.1 那两个同样回"方向不对"的失败壳**（跟真实现一致）：它们属于运行方那一侧，
    桩也不例外。
    """

    async def _ack(self, data: Any = None) -> ApiResponse[Any]:
        return ApiResponse.ok(data=data, message="MOCK 任务中心已受理")

    @staticmethod
    def _task(
        task_id: str,
        definition_id: str = "mock-def",
        space_id: Optional[str] = None,
        status: str = "pending",
        task_name: Optional[str] = None,
        task_source: Optional[str] = None,
    ) -> Dict[str, Any]:
        """凑一个 `TaskItemResponse` 形状（键跟 DTO 对齐，接入方离线联调取值得走得通）。"""
        return {
            "id": task_id,
            "tenant_id": None,
            "definition_id": definition_id,
            "definition_version_no": 1,
            "space_id": space_id,
            "task_name": task_name,
            "trigger_id": None,
            "task_source": task_source,
            "inputs": {},
            "current_attempt": 1,
            "status": status,
            "error_message": None,
            "state_version": 0,
            "created_by": None,
            "updated_by": None,
            "created_at": None,
            "started_at": None,
            "finished_at": None,
            "updated_at": None,
        }

    @staticmethod
    def _subtask(
        task_id: str,
        node_id: str,
        *,
        attempt: int = 1,
        step_code: str = "parse",
        status: str = "pending",
    ) -> Dict[str, Any]:
        """凑一个 `SubtaskItemResponse` 形状（键跟 DTO 对齐，理由同 `_task`）。"""
        return {
            "id": f"{task_id}-{node_id}",
            "tenant_id": None,
            "space_id": None,
            "task_id": task_id,
            "task_attempt_id": f"{task_id}-attempt-{attempt}",
            "node_id": node_id,
            "step_code": step_code,
            "subtask_name": None,
            "status": status,
            "depends_on": [],
            "next_steps": [],
            "sort": None,
            "timeout_seconds": 300,
            "inputs": {},
            "outputs": None,
            "current_attempt": attempt,
            "execution_id": None,
            "state_version": 0,
            "error_message": None,
            "is_deleted": 0,
            "created_at": None,
            "started_at": None,
            "finished_at": None,
            "updated_at": None,
        }

    async def _call(
        self, tenant_id: Optional[str], action: str, data: Dict[str, Any]
    ) -> ApiResponse[Any]:
        """桩的传送层：租户照样要收口（不然离线跑通了、上线才发现没租户），
        收得出来就按动作回一个形状对得上的假数据。"""
        try:
            _resolve_tenant(tenant_id)
        except TenantIsolationError as exc:
            logger.warning("任务中心调用缺合法租户 action=%s：%s", action, exc.message)
            return ApiResponse.fail(exc.message, code=exc.code)
        if action == _ACTION_INIT_TASK:
            # ⚠️ **自带的 `task_id` 要原样回**：真实现把它落到主键上、原样回给调用方
            # （§4.2 ②）。桩不认这个键的话，接入方离线联调会看到「自带的 id 与回包 id
            # 对不上」—— 而「自己先生成就不用等回包才知道写什么」正是这个字段存在的
            # 全部理由，那条路在 mock 下就验不了（`remote` / `local` 两个实现没这问题）。
            task_id = data.get("task_id") or f"mock-task-{data.get('definition_id')}"
            return await self._ack(
                self._task(
                    task_id,
                    definition_id=data.get("definition_id") or "mock-def",
                    space_id=data.get("space_id"),
                    task_name=data.get("name"),
                    # 来源也原样回（同 `task_id` 那条理由：真实现落库后原样回，
                    # 桩不回的话接入方在 mock 下取不到值）。
                    task_source=data.get("task_source"),
                )
            )
        # ⚠️ **两个读也必须回形状对得上的 dict，`data=None` 是错的** —— 与上面
        # `init_task` 的分支同一个理由：`get_task` / `get_subtask` 的契约明写「`data` 是
        # **裸 dict**（形状同 `TaskItemResponse` / `SubtaskItemResponse`）」
        # （`jonex_core/task_runtime/ports.py`）。回 `None` 的代价不是报错、而是
        # **静默失效**：照文档取值的调用方 `data.get("status")` 直接 `AttributeError`；
        # 而用了 `isinstance(data, dict)` 守卫的调用方（知识库看门狗那条「管理方定局了吗」
        # 的探查）会把 MOCK 下的「读到了」当成「还没定局」，**一条日志都没有**。
        if action == _ACTION_TASK_GET:
            return await self._ack(
                self._task(data.get("task_id") or "mock-task")
            )
        if action == _ACTION_SUBTASK_GET:
            return await self._ack(
                self._subtask(
                    task_id=data.get("task_id") or "mock-task",
                    node_id=data.get("node_id") or "mock_node",
                    attempt=data.get("attempt") or 1,
                )
            )
        return await self._ack()


# ============================================================
# 工厂：按能力清单返回对应 Client（§4.4）
# ============================================================


def get_task_center_client(
    capability_id: str = TASK_CENTER_CAPABILITY_ID,
) -> TaskCenterClient:
    """获取任务中心 Client（接入方统一用这个入口，不 new 具体实现）。

    跟 `get_rag_client()` 同一套写法：`capability_runtime.yaml` 决定本机直调还是
    经 Sidecar 反代；清单里没写这个能力就按「本机直调」兜底（`get_spec` 不抛异常）。
    """
    spec = get_locator().get_spec(capability_id)

    if spec.mode == CapabilityMode.MOCK:
        return MockTaskCenterClient()

    if spec.mode == CapabilityMode.LOCAL:
        return LocalTaskCenterClient(capability_id)

    if spec.mode == CapabilityMode.REMOTE:
        config = get_config()
        return RemoteTaskCenterClient(
            endpoint=spec.endpoint or config.SIDECAR_URL or "http://localhost:8001",
            capability_id=capability_id,
        )

    # 清单里的 mode 取到了个不认识的取值：算部署配置错，统一走 JonexException 子类，
    # 不抛裸 ValueError（backend-development-standard.md 第 8 节）。
    raise OperationNotSupportedError(
        message=f"不支持的任务中心能力模式：{spec.mode}",
        details={"capability_id": capability_id, "mode": str(spec.mode)},
    )


__all__ = [
    "LocalTaskCenterClient",
    "MockTaskCenterClient",
    "RemoteTaskCenterClient",
    "TASK_CENTER_CAPABILITY_ID",
    "TASK_CENTER_RETRYABLE_CODES",
    "TaskCenterClient",
    "get_task_center_client",
]
