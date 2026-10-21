#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""运行方适配器：把「管理方 → 运行方」的两个动作转发给具体运行方（§4.1、§5.2、§7.5 ①）。

设计出处：`docs/superpowers/specs/2026-09-09-task-center-design.md` §4.1、§7.5、§九。

管理方扫到「能跑的子任务」后，按它绑定的运行方（内置步骤清单里的 `runner`）经由
`RunnerRegistry` 换成运行方对外的 `capability_id`，再由本适配器把报文发过去。§4.1
约定运行方必须实现两个动作：

- `execute`（① 派活）—— 后台循环发现子任务能跑时调；一期在册的运行方只有知识库；
- `remove_task`（② 删整个任务）—— `task.delete` 走的就是它（§4.3）。

两个动作**走完全相同的分发通道**（MOCK / LOCAL / REMOTE），只换动作名与报文形状。

**方向别搞反**：这里全是「管理方 → 运行方」。反方向的「运行方 → 管理方」是
`report_status`（上报），在 `task_service` 里，不在这儿。

**返回值是 `DispatchResult`（三档），不是一句 `success=False`** —— 因为「没送出去」这句话
底下藏着两种**处置相反**的情形：

| 档 | 含义 | 谁会造成 |
| --- | --- | --- |
| `ACCEPTED` | 运行方收下了，后面等它的 `report_status` | 拿到回执壳、里面 `success=true` |
| `REJECTED` | 运行方**明确不收** | 拿到回执壳、里面 `success=false`（含「不认识的 `step_code`」）；落地方式配错；**清单说 LOCAL、本进程注册中心里却没有它** |
| `DELIVERY_UNKNOWN` | **压根不知道对方收没收到** | 连不上 / 读超时 / 响应不是 JSON / LOCAL 超时 / **Sidecar 回 5xx** / **能力服务自己回了失败码**（后两种额外标 `dead_after_retry`，见下）/ **LOCAL 通道回了个「不是那套三层壳 dict、也不是 `None`」的东西**（pydantic 对象 / 裸字符串… = 运行方没按 `bind()` 装配，见 `_dispatch_local` 那段） |

**⚠️ 「清单说 LOCAL、能力却不在本进程注册中心里」归 `REJECTED`（判死），不是 `DELIVERY_UNKNOWN`** ——
它与「远端回 1002（能力没注册在 Sidecar 那侧）」是**同一类事实**（装配 / 配置错了，人不去改永远好不了），
所以两条通道给同一个判决。这跟 `has_dispatch_target()` 对 LOCAL 那支的「定位不到」也是同一口径
（那里把它当「本地就能确定没有目标」）。**代价是吃掉「应用刚启动、能力还没注册好」这个瞬态** ——
调度循环确实可能先于注册跑起来；判死的取舍是：**同一件事在两条通道上不该有两种结局**，
而真错配（清单漏删、能力改名、装配漏注册）远比启动竞态常见。

**⚠️ 但两条通道的判决时机不一样，这不是不一致**：远端那条路上，「能力没配端点」**压根不是以 1002
回来的** —— Sidecar 在代理层就抛了（`CapabilityNotFoundError` 2001 → `/invoke` 的
`except Exception` 转成 `CapabilityInvokeError` 2002），到这边是 **HTTP 500**；而 500 与
「网关抖了一下」（502/503/504）在这里**分不开**（`raise_for_status()` 先抛，body 里的 `code`
没读）。所以远端先按「这一侧在报错」重发、**把预算试满还是这样才判死**（标 `dead_after_retry` 交给
派活侧收尾，见下），本地则是**第 1 次就判死**（它压根没有「请求可能已经送达」这个可能）。
**同一个判决，远端晚约 7 秒到**（重发预算用尽，退避 2s + 5s）。详见 `_dispatch_remote` 的 5xx 那一支。

**⚠️ 别把这条一致性读成「拿 `code` 判 LOCAL 那条路」**：LOCAL 这一支是**本地就知道**没这个能力，
压根没有回执壳、也没有 `code` 可看 —— 它是直接构造判决的，与 `_from_receipt` 那条「只看 `success`」各走各的。

**⚠️ 「回执不是三层壳」那条重发的代价（写在这里备查）**：判 `DELIVERY_UNKNOWN` 就会**原地重发**
（`call_with_retry`，最多 3 次）。运行方**用了 `bind()`** 时这是纯防御 —— `bind()` 每条路径都回裸壳
dict，走不到这一支。而**手写 `RunnerPort` / 直接挂 `dispatch["execute"] = runner.execute`** 的运行方
走得到：它回 pydantic 对象时第 1 次 handler 其实已经跑完，却会被再调 2 次；那种运行方通常**也没有**
`ExecutionRegistry`（去重是 `RunnerBase` 内置的），所以那 2 次是**真的重复执行**（重复建下游 / 重复扣配额）。
取舍：与「把一次明确拒绝读成已受理、那一步挂到 7 小时超时」相比，重发的方向更保守；**装配方式是契约**
（CLAUDE.md：运行方只写一行 `dispatch.update(runner.bind())`），走岔了本来就会在别处出问题。

这个区分就是**重发的判据**（§4.1 ①②）：只有 `DELIVERY_UNKNOWN` 才重发，`REJECTED`
不重发（对方已经表了态，再发一百次还是这句话）。两个动作**共用 `call_with_retry`**
（本模块）—— 派活侧在 `services/scheduler_service.py` 的 ③ 段调，删除侧在
`services/task_service.py` 的 `task.delete` 里调，**重发范围、次数、退避完全一致**
（3 次尝试 / 退避 2s、5s，见 `OUTBOUND_RETRY_BACKOFF_SEC`）。只有「用完预算之后怎么处置」
由各自的调用方定：
派活侧**分两档**（`dead_after_retry` 那一类试满预算 → **判死**；其余未知 → 子任务停在
`running` 交超时巡检）、删除侧**算整体失败**（§4.3，任务中心一个字段都不改，用户重新点一次）。
**判死那一档由 `TaskService.finalize_dispatch` 决定，不在这儿、也不在重发循环里** ——
适配器只如实记下「这一次未知属于哪一类」（`DispatchResult.dead_after_retry`）。

**适配器把出站能出的错都翻成判决，不主动抛** —— 但**能漏出去的只剩「请求肯定没落到
运行方」的情形**（客户端没构造出来、报文 DTO 校验不过），调用方按 `DELIVERY_UNKNOWN`
同一档处理。

三种运行模式的落地方式跟 `get_rag_client()` 同构（`capability_runtime.yaml` 的 `business:` 段
决定用哪种）：

- `remote`：POST Sidecar `/invoke`，`payload = {"action": <动作名>, "data": <报文>}`；
- `local`：进程内直连（运行方已注册进本进程的能力注册中心）；
- `mock`：桩实现，收下就回执——离线开发 / 单测用，不真跑。
"""

import asyncio
import contextlib
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Optional

import httpx

from jonex_core.capability import ApiResponse, get_capability_registry
from jonex_core.capability.locator import CapabilityMode, get_locator
from jonex_core.capability.models import CapabilityRequest
from jonex_core.common.config import get_config
from jonex_core.common.monitor_collector import attempt_span, emit_logical_call
from jonex_core.common.monitor_models import CallFinalStatus
from jonex_core.common.tenant import require_tenant
from jonex_core.task_protocol.models import (
    STEP_STATUS_VALUES,
    ExecutionEnvelope,
    StepStatusQuery,
)
from jonex_core.task_protocol.results import (
    OUTBOUND_RETRY_BACKOFF_SEC,
    RETRY_MAX_ATTEMPTS,
    DispatchDisposition,
    DispatchResult,
    dispatch_disposition,
    retry_delay,
)

from ..dtos.runner import ExecutionDispatch, RemoveTaskDispatch, TaskStoppedDispatch

logger = logging.getLogger(__name__)

# 运行方那边「被调度执行一步」的动作名（§4.1 ①）。
RUNNER_EXECUTE_ACTION = "execute"

# 运行方那边「清掉自己侧整个任务残留」的动作名（§4.1 ②）。
RUNNER_REMOVE_TASK_ACTION = "remove_task"

# 运行方那边「这条任务被用户停了」的**通知**动作名（§4.1 ③，2026-09-30 加）。
# ⚠️ 是通知不是命令 —— 停不停由运行方自己决定，见 `TaskStoppedDispatch`。
RUNNER_TASK_STOPPED_ACTION = "task_stopped"

# 运行方那边「这个子任务现在什么情况」的**查询**动作名（§4.1 ④，2026-10-11 加）。
# ⚠️ 与上面三个都不同：它**要拿数据**（不是只判收没收下），所以不走 `_send`/`DispatchResult`。
# 详见 `RunnerAdapter.query_step_status`。
RUNNER_QUERY_STEP_STATUS_ACTION = "query_step_status"


def has_dispatch_target(capability_id: str) -> bool:
    """这个 `capability_id` 现在**本地能不能判出「有个可派活的对象」**？（§4.3 用它跳过）

    **只有一种情况本地就能确定「没有目标」**：清单说它是 `LOCAL`（**含「清单里压根没
    声明这个能力」** —— `get_locator().get_spec()` 对未声明的会静默回落到 LOCAL 默认值），
    而本进程的能力注册中心里**没有**它。成因通常是这三种：清单里这一条被删了（运行方
    下架 / 改名）、装配漏了（清单说 LOCAL 却没人注册进来）、能力 ID 换了名字。

    **REMOTE / MOCK / 认不出的落地方式一律算「有」**：REMOTE 的可达性只能靠真去调，
    本地分不出「对方永久没了」和「这会儿连不上」—— 把连不上也当成「没有目标」，等于让
    一次网络抖动静默跳过清理（残留还留在对面）。落地方式配错同理：那是配置该修的问题，
    不该被这里吞掉。

    **调用方（`task.delete`）拿它做「跳过」判据**：判出 `False` 的运行方**不请**、记一条
    warning，但**照常把任务删掉**。这与回落路径「运行方不在册就跳过」是同一口径
    （`_runner_capabilities`）—— 否则一个早就下架的运行方会把**所有**快照里含它的任务
    永久钉在「删不掉」上（每次都是明确/未知的失败，重发一百次也一样）。

    ⚠️ **派活侧（`_dispatch_local`）对同一个判据给的是同一个结论**：清单说 LOCAL、
    本进程注册中心里却没有它 → 判 `REJECTED`（当场判死），不再当「没够着」重发。
    两边一致是刻意的 —— 同一件事（这个运行方根本没在册）在删除与派活两条路上
    不该给出两种处置。**改这里的时候两处一起看。**
    """
    spec = get_locator().get_spec(capability_id)
    if spec.mode == CapabilityMode.LOCAL:
        return get_capability_registry().get_capability(capability_id) is not None
    return True


async def call_with_retry(
    call: Callable[[], Awaitable[DispatchResult]],
    *,
    what: str,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    request_id: str = "",
    tenant_id: str = "",
    target: str = "",
) -> DispatchResult:
    """出站调用 + **传送层重发**。**本函数不抛。**

    §4.1 的**两个出站动作共用这一份循环**（① 派活 / ② 删除）—— 「什么时候该重发」的口径
    只有一处，两个方向不会悄悄分叉。

    只有「没够着运行方」（`DELIVERY_UNKNOWN`）才重发；`ACCEPTED` / `REJECTED` 都当场收工
    —— 三档的含义与判据见本模块 docstring。

    **为什么重发是安全的**：运行方那侧必须幂等，而这条义务正好两个方向都有 ——
    派活重发的是**同一个 `execution_id`**（`ExecutionRegistry` 认得出、直接回成功、不重复
    干活，§4.5）；删除重发的是**同一个 `task_id`**（`remove_task` 必须幂等：**任务不存在 /
    已经删掉了，都要回成功**，§4.1 ②）。**这条幂等义务正是重发能成立的前提。**

    ── 预算，以及用完了怎么办 ──

    **次数**与运行方 SDK 那三个方向（`submit` / `report_terminal` / `retry_subtask`）共用同一
    套（`RETRY_MAX_ATTEMPTS` = 3 次尝试）；**退避是两套**（都定义在协议层 `task_protocol.results`）：

    | 方向 | 退避 | 为什么 |
    | --- | --- | --- |
    | SDK 三个方向 | `RETRY_BACKOFF_SEC` = 0.5s、1s | 它们穿的是运行方自己的进程与本地网络，抖一下就是抖一下 |
    | **本函数的两个方向**（派活 / 删除） | `OUTBOUND_RETRY_BACKOFF_SEC` = **2s、5s** | 出站要先穿 Sidecar（再可能穿网关），撞上的是「Sidecar 正在重启」这类**按秒计**的故障 —— 0.5s/1s 会让三次尝试全落在同一段故障里（首尾相隔才 1.5 秒），「试满预算」退化成「在 1.5 秒里撞三次」 |

    3 次都没够着就**不再重发**，把**最后一次**那个 `DELIVERY_UNKNOWN` 原样交回调用方
    （连同它的 `dead_after_retry` 标记）—— **怎么处置由调用方定，本函数不替它决定、也一个字都
    不改判决**：派活侧**分两档**（`dead_after_retry` 那一类试满预算 → **判死**；其余未知 →
    子任务停在 `running`，由超时巡检兜底 —— 运行方可能已经在跑了，判死会杀掉一个正在跑的活）；
    删除侧**算整体失败**（任务中心一个字段都不改，用户重新点一次）。两处都在
    `finalize_dispatch` / `task.delete` 里。

    ⚠️ **判死的判据取「最后一次」的结果，不是「有没有出现过」**：三次里只要最后一次仍带那个
    标记就判死；最后一次若落回别的未知档（429 / 读超时 / 响应不是 JSON），照旧不判死。
    这条由调用方按 `last.dead_after_retry` 读，**循环本身不参与**。

    ⚠️ **`REJECTED` 一次都不重发**：运行方已经明确表了态（「不认识的 `step_code`」/「清不掉」
    这类），重发一百次还是这句话 —— 白等一段退避、白占着一轮调用。

    ── 已知代价（同步循环，单条最坏 3 × 超时 + 7s ≈ 97 秒）──

    - **派活侧阻塞后台循环**：`_dispatch_pass` 是顺序遍历，一轮最坏 `BATCH × 那个数`
      （默认 10 × 97s ≈ 16 分钟）；阻塞的条**算占用本轮派活名额**（有上界）。代价是同一轮里
      触发抢占 / 消化上报 / 超时巡检都被推后 —— 这是「循环内同步重发」这个选择的代价
      （换成跨轮把状态回退 `pending` 的话，就变成每 3 秒重试一次、永远重试下去）。
    - **删除侧是用户请求路径**（`task.delete` → 同步 HTTP）：**用户在那次请求里干等**。
      这个代价是有意接受的 —— 删除是低频操作，而「没够着运行方」本身罕见；换来的是
      「用户不用自己再点一次删除」。

    ── 为什么 `call` 是 thunk、不是 (adapter, 报文) ──

    两个动作的报文不同（派活是 `ExecutionDispatch`、删除是 `RemoveTaskDispatch`），把
    适配器与报文都塞进来的话签名就得为每个动作长一个参数。传一个已经绑好的零参调用，
    循环只关心「它返回哪个 disposition」—— 重发逻辑与报文形状无关。

    ── 采集：N1（一条逻辑调用）+ N2（每次循环迭代 = 一次**物理尝试**）──

    `request_id` / `tenant_id` / `target` 是**可选**的采集上下文（前两个默认空 =
    不采集）：本函数是共享内核里那两个 ID 都拿不到的地方，所以由调用方透传进来。
    **两个 ID 都非空**才采 —— 拿不到合法租户就**让它不采**，绝不填个假值兜底
    （采集侧对 `default` / `system` / `unknown` / 空串同样整条丢弃，两处口径一致）。

    **N2**（`attempt_span`，每次迭代一条）：

    - `attempt_no` = 循环变量 `attempt`（1 起，与 `RETRY_MAX_ATTEMPTS` 同一套）；
    - `wait_before_ms` = **进入本次尝试前**等的毫秒数（首次 `None`；就是上一轮那句
      `sleep(delay)` 的值，不是本次调用自己的耗时）；
    - `is_final` = 「本次是这条逻辑调用的最后一次尝试」：`ACCEPTED` / `REJECTED` 当场
      就是最后一次（本次给的那次调用到此结束）；`DELIVERY_UNKNOWN` 只有用满预算那次
      才是 —— ⚠️ 冻结接口的 `is_final` 在构造时定形，而这一档**只有拿到判决才知道**，
      所以在 span 投递前就地改对（见下面那句注释），不改任何重发判定；
    - 结局：`ACCEPTED` 什么都不标（默认 `success`）；`REJECTED` /
      `DELIVERY_UNKNOWN` 标 `att.fail(...)`，`error_code` 取 disposition 名、
      `error_message` 取 `DispatchResult.message`（都是现成的，不另造文案）。

    **N1**（`emit_logical_call`，**整个循环只一条**，在 `finally` 里发）：这条出站链路
    原先只采 N2，而 N1 是 per-逻辑调用的 —— 缺了它平台里就没有以同一个 `request_id` 建的 N1，
    `attempt_count` 的 COUNT 补正永远回填不上（`retry_count` 恒 0）。所在位置与结局：

    - **必须放在循环之外**：放进循环会因 `UNIQUE(request_id)` + `ON CONFLICT DO NOTHING`
      只留第一次尝试那条，`final_status` 就记反了；
    - 结局与 N2 对齐（不另立口径）：`ACCEPTED` → `success`；`REJECTED` /
      `DELIVERY_UNKNOWN`（含试满预算）→ `failed`（`error_code` 取 disposition 名）；
      被取消 → `cancelled`（`CancelledError` 上抛前标好）；
    - `target` 由调用方给（派活 / 删除两处传的都是运行方的 `capability_id`），
      `direction` / `trigger_type` 取 `emit_logical_call` 的默认值。

    **采集是纯旁路**：`attempt_span` 不吞业务异常（异常照常上抛、只是记一笔），
    `emit_logical_call` 与投递失败一律吞掉 —— 本函数的重发判定、退避、返回值
    **一个字节都不因采集而变**。
    """
    last: Optional[DispatchResult] = None
    # N2 采集守卫：两个 ID 都非空才采（拿不到上下文就不采，不造假值兜底 —— 见 docstring）。
    collect = bool(request_id) and bool(tenant_id)
    waited_ms: Optional[int] = None  # 进入本次尝试前等的毫秒数（首次 None）
    # N1 补发状态：**整个重发循环只让这里决定那一条 N1 的结局**（在 `finally` 里发）。
    # 循环内逐次投递会因 `UNIQUE(request_id)` + `ON CONFLICT DO NOTHING` 只留下第一次
    # 尝试那条 —— 「试满预算、最后一次才够着」的调用会被记成失败。
    n1_started_at = datetime.now(timezone.utc)
    n1_status = CallFinalStatus.FAILED.value
    n1_error_code: Optional[str] = None
    n1_error_message: Optional[str] = None
    try:
        for attempt in range(1, RETRY_MAX_ATTEMPTS + 1):
            span_cm = (
                attempt_span(
                    request_id=request_id,
                    tenant_id=tenant_id,
                    attempt_no=attempt,
                    # 占位，拿到判决后改对（见下面那句注释）
                    is_final=False,
                    wait_before_ms=waited_ms,
                )
                if collect
                else contextlib.nullcontext()
            )
            with span_cm as att:
                try:
                    last = await call()
                except Exception as exc:
                    # 出站调用自己炸了。能漏到这里的 = **请求肯定没落到运行方**（客户端没构造出来 /
                    # LOCAL 进程内抛 / 报文 DTO 校验不过）→ 与「没够着」同档，进同一个循环重发。
                    # 真实适配器已经把 `httpx.HTTPError`、非 JSON 响应、LOCAL 超时翻成判决了。
                    logger.warning("[TaskCenter] %s 出站调用抛异常：%s", what, exc)
                    last = DispatchResult(
                        disposition=DispatchDisposition.DELIVERY_UNKNOWN,
                        message=f"{what} 调用失败：{exc}",
                    )

                if att is not None:
                    # `is_final` 构造时只能按「还剩几次重试」预判；终态判决
                    # （`ACCEPTED`/`REJECTED`）当场就是最后一次，故拿到判决后用冻结
                    # 接口的公开入口 `mark_final()` 就地改写。
                    att.mark_final(
                        last.disposition is not DispatchDisposition.DELIVERY_UNKNOWN
                        or attempt >= RETRY_MAX_ATTEMPTS
                    )
                    if last.disposition is not DispatchDisposition.ACCEPTED:
                        # 默认 success；失败的两档显式标注（`error_code` 取 disposition 名，
                        # `error_message` 取现成的那句原因）。`error_message` 由采集侧脱敏 + 截断。
                        att.fail(
                            error_code=last.disposition.value,
                            error_message=last.message,
                        )

            if last.disposition is not DispatchDisposition.DELIVERY_UNKNOWN:
                # `ACCEPTED` / `REJECTED` 都是「运行方给了话」，当场收工、不烧剩余预算。
                if last.disposition is DispatchDisposition.ACCEPTED:
                    n1_status = CallFinalStatus.SUCCESS.value
                else:
                    n1_error_code = last.disposition.value
                    n1_error_message = last.message
                return last

            if attempt < RETRY_MAX_ATTEMPTS:
                delay = retry_delay(attempt, OUTBOUND_RETRY_BACKOFF_SEC)
                logger.warning(
                    "[TaskCenter] %s 没够着运行方（第 %d/%d 次），%.1fs 后重发：%s",
                    what,
                    attempt,
                    RETRY_MAX_ATTEMPTS,
                    delay,
                    last.message,
                )
                await sleep(delay)
                # 记成**下一次尝试前**等的毫秒数（本次这次调用自己的耗时不算在内）。
                waited_ms = int(delay * 1000)

        assert last is not None  # RETRY_MAX_ATTEMPTS >= 1，循环至少跑一次
        # 耗尽预算。能走到这里的必然是 `DELIVERY_UNKNOWN`（`ACCEPTED` / `REJECTED` 在上面的
        # 循环体内就 return 了），如实记失败 + 最后一次那一档的成因。
        n1_error_code = last.disposition.value
        n1_error_message = last.message
        logger.warning(
            "[TaskCenter] %s 连续 %d 次没够着运行方，交回调用方处置：%s",
            what,
            RETRY_MAX_ATTEMPTS,
            last.message,
        )
        return last
    except asyncio.CancelledError:
        # 中途取消：与「压根没发出去」区分开（记 cancelled，不是 failed）。
        n1_status = CallFinalStatus.CANCELLED.value
        raise
    finally:
        # —— N1 补发（`monitor.call_log`）——
        # 这一路的重发**是同一个逻辑调用**（重发的就是同一个 `execution_id` / `task_id`），
        # 故 N1 只在循环收工后发一条，`request_id` 与 N2 逐字相同 ⇒ 两表关联得上、
        # `attempt_count` 的 COUNT 补正能回填。**必须放在循环之外**（理由见
        # `emit_logical_call` 的 docstring）。
        # 采集是纯旁路：投递失败一律吞掉，本函数的返回值与异常传播一个字节都不变。
        if collect:
            emit_logical_call(
                request_id=request_id,
                tenant_id=tenant_id,
                status=n1_status,
                target=target,
                started_at=n1_started_at,
                error_code=n1_error_code,
                error_message=n1_error_message,
            )


class RunnerAdapter:
    """运行方派活适配器：`execute(execution)` 的发送侧（§4.1 ①）。"""

    def __init__(self, *, timeout: Optional[float] = None):
        # 派活是「投递 + 回执」，不等运行方跑完，所以超时给短一点；
        # 真正的执行耗时由子任务的 timeout_seconds 在超时巡检里管（§7.4）。
        # 不传就用配置里的 `TASK_CENTER_RUNNER_TIMEOUT`（默认 30 秒）。
        self._timeout = (
            timeout if timeout is not None else get_config().TASK_CENTER_RUNNER_TIMEOUT
        )
        self._clients: dict[str, httpx.AsyncClient] = {}

    # ── 对外 ──────────────────────────────────────────────

    async def dispatch(
        self, capability_id: str, execution: ExecutionDispatch
    ) -> DispatchResult:
        """把一次派活发给运行方，返回**三档判决**（§4.1 ① 的返回）。

        报文里带 `space_id`——运行方按空间落数据时要用（§4.1 ①）。

        `DELIVERY_UNKNOWN` 由调用方重发（`call_with_retry`，本模块）；`ACCEPTED` /
        `REJECTED` 都是「运行方给了话」，当场收工 —— 判据与理由见模块 docstring。
        """
        return await self._send(
            capability_id,
            RUNNER_EXECUTE_ACTION,
            execution.dict(),
            identity=f"task={execution.task_id}",
        )

    async def remove_task(
        self, capability_id: str, execution: RemoveTaskDispatch
    ) -> DispatchResult:
        """让运行方清掉自己侧**整个任务**的残留（§4.1 ②、§4.3 `task.delete`）。

        `task.delete` 的「先调运行方删除」就是它：**按 `task_id` 一把清干净** —— 运行方
        最清楚自己按什么维度存数据（可能按任务建目录、按任务建表），"清一个任务"是它
        内部一次自然的操作（§4.1 ②）。`tenant_id` 用于租户隔离，`space_id` 供运行方
        按空间清数据。

        **只负责「一次」调用** —— 重发不在这儿，在 `call_with_retry`（本模块）里，由
        `task.delete` 调用。所以拿到 `DELIVERY_UNKNOWN` 别当成最终结论：那只是「这一次
        没够着」，调用方还会再试。

        **失败即整体失败**：不是 `ACCEPTED` 时（连不上 / 运行方不收）`task.delete`
        什么都不会改，整个删除算失败（§4.3）。

        ⚠️ **运行方必须幂等**：**任务不存在 / 已经被删掉了，都要当成功返回**（§4.1 ②）。
        这不只是「重复调不报错」的礼貌 —— 它是**重发能成立的前提**：`task.delete` 在没够着
        运行方时会重发同一个 `task_id`（最多 3 次），上一次可能其实已经删干净了、只是回执
        没回来。那一次要是回失败，用户就会看到「删除失败」，可任务早清没了。
        """
        return await self._send(
            capability_id,
            RUNNER_REMOVE_TASK_ACTION,
            execution.dict(),
            identity=f"task={execution.task_id}",
        )

    async def notify_stopped(
        self, capability_id: str, execution: TaskStoppedDispatch
    ) -> DispatchResult:
        """**通知**运行方「这条任务被用户停了」（§4.1 ③、`task.stop` 收口之后发）。

        ⚠️ **它是通知，不是命令** —— 管理方只说事实（用户点了停止、任务已就地收成
        `failed`），**停不停、怎么停由运行方自己决定**（能停就停，停不掉就只把接管的
        文档交还出去，那也是合法的回应）。理由见 `TaskStoppedDispatch` 的说明。

        ⚠️ **与 `remove_task` 的两处区别都是刻意的**：

        - **只发一次、调用方不重发**（不走 `call_with_retry`）；
        - **超时短**（`TASK_CENTER_NOTIFY_TIMEOUT`，默认 5 秒，而不是那 30 秒）。

        两条的理由同一条：它跑在**用户点「停止」那条同步请求**里，不能让用户为一次
        「通知」干等 30 秒 × 3。发不出去也不影响停止本身 —— 那份文档退回看门狗那条
        30 分钟的兜底，不会比今天更差。

        本方法**只负责发一次**；「不重发」「失败只告警」都是调用方的处置（见
        `TaskService._notify_stopped`）。
        """
        return await self._send(
            capability_id,
            RUNNER_TASK_STOPPED_ACTION,
            execution.dict(),
            identity=f"task={execution.task_id}",
            timeout=get_config().TASK_CENTER_NOTIFY_TIMEOUT,
        )

    async def query_step_status(
        self, capability_id: str, envelope: ExecutionEnvelope
    ) -> Optional[StepStatusQuery]:
        """问运行方「这个子任务现在什么情况」（§4.1 ④，2026-10-11 加）。

        ## 与另外三个动作的三处不同，都是刻意的

        1. **要拿数据**，不只要判决 —— 所以**不走 `_send` 那套 `DispatchResult`**
           （它把 `data` 丢掉，那是给「写动作」用的：写动作只关心收没收下）。
        2. **不重发** —— 它是**幂等只读**的：问不到就**下一拍再问**，
           重发只会把一次对账变成三倍流量，收益为零。
        3. **失败一律回 `None`**、不抛也不判死 —— 对账循环拿到 `None` 就是
           「这次没问到」，**不做任何处置**。这正是「拉」比「推」稳的地方：
           推丢了要有人兜，拉丢了下一拍就是兜。

        ## 通道

        按 `capability_runtime.yaml` 的落地方式分流，与派活走**同一个
        `capability_id`**、同一套 `get_locator()`。

        ⚠️ **`LOCAL` 模式暂不支持**（回 `None` = 「这个运行方不提供查询」）：
        `LOCAL` 那条通道的分发是按「回执壳 → 判决」写的，拿数据要另一套。
        当前部署（`business:` 走 `REMOTE`）用不到它；真需要时再补，
        **而在此之前 `LOCAL` 的部署行为与从前一字不变**（回 `None` = 管理方走老路）。
        """
        data = envelope.dict()
        spec = get_locator().get_spec(capability_id)

        if spec.mode != CapabilityMode.REMOTE:
            # MOCK 是桩（答不出）、LOCAL 见 docstring —— 都回「不提供查询」。
            return None

        try:
            # ⚠️ **`require_tenant` 也在 try 里**（2026-10-13 修）：envelope 的 `tenant_id`
            # 缺失 / 非法时会抛 `TenantIsolationError` —— 那也要落「这次没问到」，
            # **不能冒出去**破坏本方法「失败一律回 `None`、不抛也不判死」的承诺（docstring 第 3 条）。
            tenant_id = require_tenant(data.get("tenant_id"))
            client = self._get_client(spec.endpoint)
            resp = await client.post(
                "/invoke",
                json={
                    "capability_id": capability_id,
                    "payload": {
                        "action": RUNNER_QUERY_STEP_STATUS_ACTION,
                        "data": data,
                    },
                    "tenant_id": tenant_id,
                },
                headers={"X-Tenant-ID": tenant_id},
                timeout=get_config().TASK_CENTER_RUNNER_TIMEOUT,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            # **不抛、不判死**：这次没问到，下一拍再来（§本方法 docstring 第 3 条）。
            logger.warning(
                "[TaskCenter] 查询运行方状态没问到：capability=%s task=%s node=%s err=%s",
                capability_id, envelope.task_id, envelope.node_id, exc,
            )
            return None

        if not isinstance(payload, dict):
            # ⚠️ **body 不是 JSON 对象**（数组 / 字符串 / 标量）时不能直接 `.get` ——
            # 那会抛 `AttributeError` 冒出去，同样破坏「失败一律回 `None`」的承诺
            # （2026-10-13 修）。按「这次没问到」处置。
            logger.warning(
                "[TaskCenter] 查询运行方状态回了非对象 body：capability=%s type=%s",
                capability_id, type(payload).__name__,
            )
            return None
        if not payload.get("success"):
            logger.warning(
                "[TaskCenter] 查询运行方状态被拒（Sidecar 外层）：capability=%s task=%s node=%s msg=%s",
                capability_id, envelope.task_id, envelope.node_id,
                str(payload.get("message"))[:200],
            )
            return None
        # ⚠️ **两层壳**（与 `_dispatch_remote` 那条逐句同形，别自己另发明一套）：
        #   外层是 **Sidecar** 的（`payload["success"]` 只说 Sidecar 代理成没成）；
        #   `payload["data"]` 是**能力层**自己的回执壳；
        #   **再里面的 `data` 才是运行方回的内容**。
        # 少了这一层，就会把整个壳当成运行方的回答 —— 表现是「形状认不出、静默丢结果」。
        receipt = payload.get("data")
        if isinstance(receipt, dict) and "success" in receipt:
            if not receipt.get("success"):
                # 运行方**明确**失败了（比如报文不合法）—— 同样按「没问到」处置。
                logger.warning(
                    "[TaskCenter] 查询运行方状态被拒（能力层）：capability=%s task=%s node=%s msg=%s",
                    capability_id, envelope.task_id, envelope.node_id,
                    str(receipt.get("message"))[:200],
                )
                return None
            raw = receipt.get("data")
        else:
            # 没有内层壳的通道（比如将来补 LOCAL）—— 那 `payload["data"]` 就是答案本身。
            raw = receipt
        if not isinstance(raw, dict) or raw.get("status") not in STEP_STATUS_VALUES:
            # 回了个认不出的东西 —— 当作「没问到」，**别硬解释**。
            logger.warning(
                "[TaskCenter] 查询运行方状态回了认不出的形状：capability=%s task=%s data=%r",
                capability_id, envelope.task_id, str(raw)[:200],
            )
            return None
        try:
            # ⚠️ **构造也要在 try 里**：`status` 合法不代表整个报文合法
            # （`report` 不是 dict、`detail` 超 `max_length` …）—— pydantic v1 会抛
            # `ValidationError`。本方法对外承诺「**失败一律回 `None`**、不抛也不判死」，
            # 让它冒出去会破坏契约，而且那个错**每拍都会重放一次**（调度循环按拍调它）。
            return StepStatusQuery(**raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[TaskCenter] 查询运行方状态的报文过不了 DTO 校验（当作没问到）："
                "capability=%s task=%s err=%s data=%r",
                capability_id, envelope.task_id, exc, str(raw)[:200],
            )
            return None

    async def close(self) -> None:
        """释放连接池（进程退出 / 测试收尾时调）。"""
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()

    # ── 分发（execute / remove_task 共用一条道）──

    async def _send(
        self,
        capability_id: str,
        action: str,
        data: Dict[str, Any],
        *,
        identity: str,
        timeout: Optional[float] = None,
    ) -> DispatchResult:
        """把一次出站调用发给运行方：`action` 决定是哪个动作，通道完全一样。

        `timeout` 给 `None` = 用适配器自己的那个（`TASK_CENTER_RUNNER_TIMEOUT`）；
        传值 = **这一次调用**用它（给「停止通知」那种不该让用户干等的动作留的口子，
        见 `notify_stopped`）。
        """
        tenant_id = require_tenant(data.get("tenant_id"))
        spec = get_locator().get_spec(capability_id)

        if spec.mode == CapabilityMode.MOCK:
            logger.info(
                "[TaskCenter] MOCK 运行方 %s 收下 %s %s",
                capability_id,
                action,
                identity,
            )
            return DispatchResult(
                disposition=DispatchDisposition.ACCEPTED,
                message=f"MOCK 运行方已收下 {action}",
            )

        if spec.mode == CapabilityMode.LOCAL:
            return await self._dispatch_local(
                capability_id, action, data, tenant_id, identity, timeout=timeout
            )

        if spec.mode == CapabilityMode.REMOTE:
            return await self._dispatch_remote(
                capability_id, spec.endpoint, action, data, tenant_id, identity,
                timeout=timeout,
            )

        # 不认识的落地方式 = `capability_runtime.yaml` 配错了。**这不是传送层失败**：
        # 请求压根没构造出来，重发一百次还是这句话 → `REJECTED`，让调用方当场判死
        # （而不是白等 3 轮退避、再在 `running` 上挂 7 小时等超时巡检）。
        logger.error(
            "[TaskCenter] %s 落地方式不支持 capability=%s mode=%s %s",
            action,
            capability_id,
            spec.mode,
            identity,
        )
        return DispatchResult(
            disposition=DispatchDisposition.REJECTED,
            message=f"不支持的运行方落地方式：{spec.mode}",
        )

    @staticmethod
    def _from_receipt(receipt: ApiResponse[Any]) -> DispatchResult:
        """**拿到了运行方的回执壳** → 判决。

        这一档**只看 `success`，绝不看 `code`** —— 这是本适配器最容易判反的一处：

        运行方的拒绝壳一律由 `ApiResponse.fail()` 生成，`code` 恒为 `-1`（`CODE_FAIL`）。
        而 `-1` 在 `classify_code` 里落 `CLASS_UNKNOWN`（它的语义是「未分类的失败」，
        不是「明确拒绝」）。所以拿 `dispatch_disposition(False, -1)` 来判，会把「运行方
        **明确**拒收」误判成「结果未知」→ 派活侧无谓重发 3 次，正好与「明确拒收不重发」
        相反。

        **拿到了壳 = 拿到了对方的话**：`success=false` 就是拒绝。（Sidecar 那层外层信封
        是另一回事，它才需要看 `code` —— 见 `dispatch_disposition` 的说明。）
        """
        return DispatchResult(
            disposition=(
                DispatchDisposition.ACCEPTED
                if receipt.success
                else DispatchDisposition.REJECTED
            ),
            message=receipt.message or "",
        )

    # ── 各落地方式 ─────────────────────────────────────────

    async def _dispatch_remote(
        self,
        capability_id: str,
        endpoint: Optional[str],
        action: str,
        data: Dict[str, Any],
        tenant_id: str,
        identity: str,
        *,
        timeout: Optional[float] = None,
    ) -> DispatchResult:
        """经 Sidecar 反代到独立部署的运行方进程。

        `timeout` 是**按请求**的（客户端是按 endpoint 缓存复用的，超时烘在客户端上、
        改不动）—— ⚠️ 给 httpx 传的是它自己的 `USE_CLIENT_DEFAULT` 哨兵，**不是 `None`**：
        传 `None` 在 httpx 里是「**关掉**这个请求的超时」，与「用客户端默认值」是两回事。
        """
        client = self._get_client(endpoint)
        body = {
            "capability_id": capability_id,
            "payload": {"action": action, "data": data},
            "tenant_id": tenant_id,
        }
        try:
            resp = await client.post(
                "/invoke",
                json=body,
                headers={"X-Tenant-ID": tenant_id},
                timeout=(
                    timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT
                ),
            )
            resp.raise_for_status()
            payload = resp.json()
        except httpx.HTTPStatusError as exc:
            # **带状态码回来 = 请求到达了、被明确回了**。4xx 与 5xx 在这里必须分开，
            # 判据还是那一句「重发有没有可能改变结局」：
            #
            # - **4xx → `REJECTED`**（**429 除外，它不是这一类**）：内部 API Key 配错
            #   （401/403）、Sidecar 路径错（404）—— 请求压根没进运行方的业务层，**原样
            #   重发结果一样**，子任务当场判死。
            # - **429 → `DELIVERY_UNKNOWN`**：它是**治理层限流**回的，不是运行方的表态 ——
            #   过一会儿再来就能进，原样重发**有可能改变结局**，与 401/404 不是一类事实。
            #   而 `RATE_LIMIT_TENANT_PER_MINUTE` 默认 300/分钟、`RATE_LIMIT_ENABLED` 默认开，
            #   一批任务同时派活/上报是真会撞上的；把它归进「明确拒绝」就是**因为「这一秒
            #   太挤」把一条本来能跑的子任务永久杀掉**（终态 `failed`、下游全部 hold），
            #   而它下一秒就能跑。
            # - **5xx → `DELIVERY_UNKNOWN` + `dead_after_retry=True`**：网关 / 上游在报错，
            #   重发是有意义的（502/503/504 那类抖动），当场不能当明确拒绝。
            #   ⚠️ 但 **500 不全是临时故障**：Sidecar 自己的代理层出问题（最典型的是
            #   **能力没在它那侧配端点** —— `CapabilityNotFoundError` 2001 被
            #   `/invoke` 的 `except Exception` 转成 `CapabilityInvokeError` 2002）回的
            #   也是 500，那是一条**装配错、重发不会好**的事实。**这两类在这里分不开**
            #   （`raise_for_status()` 先抛，body 里的 `code` 压根没读）—— 所以不硬分，
            #   而是**把它标出来**（`dead_after_retry=True`）：派活侧的收尾
            #   （`TaskService.finalize_dispatch`）按这个标记加码，**预算试满、最后一次
            #   仍是 5xx 就判死**。于是本地通道那条「配置错当场判死」的判决，远端也有了
            #   —— 同一个判决，只晚约 7 秒（本地第 1 次就判、这边要试满 3 次），
            #   而不是原先那个「挂满 `timeout_seconds`（内置步骤 25200 秒）等超时巡检」的未知。
            #
            # 另一条通道（`jonex_core/capability/business/task_center/client.py` 的 4xx，
            # **方向相反**：运行方 → 管理方）**处置必然不同** —— 那边回的是交给**调用方**
            # 的失败壳，重不重发由 SDK 的重试循环定，那里没有「判死谁」这回事；只有这一侧
            # 才把「明确拒绝」落成子任务的终态。
            #
            # ⚠️ 这一档**必须写在 `except httpx.HTTPError` 之前**：`HTTPStatusError` 是它的
            # 子类，被它先兜住就全落进「没够着」那一档 —— 一次配错 key 会变成重发 3 轮、
            # 子任务挂在 `running` 上白等超时巡检（内置步骤 25200 秒），日志还只是 warning，
            # 排查时看不出这是装配错。
            #
            # ⚠️ **429 那一支必须写在 `< 500` 之前** —— 写成后面就是「限流 → 判死」，
            # 正是上面说的「因为这一秒太挤而永久杀掉一条能跑的子任务」。
            if exc.response.status_code == 429:
                # Sidecar 的 429 带 `Retry-After`（`jonex_core/sidecar/main.py`），
                # 记进日志：重发是当轮固定退避（2s / 5s），**不等这个值** —— 运维要
                # 判断「为什么 3 次都没进去」时，得看得见上游让我们等多久。
                logger.warning(
                    "[TaskCenter] %s 被限流 HTTP 429 capability=%s %s（Retry-After=%s）：%s",
                    action,
                    capability_id,
                    identity,
                    exc.response.headers.get("Retry-After", "?"),
                    exc,
                )
                return DispatchResult(
                    disposition=DispatchDisposition.DELIVERY_UNKNOWN,
                    message=(
                        f"调用 {capability_id} 的 {action} 被限流：HTTP 429"
                    ),
                )
            if exc.response.status_code < 500:
                logger.error(
                    "[TaskCenter] %s 被拒 HTTP %s capability=%s %s：%s",
                    action,
                    exc.response.status_code,
                    capability_id,
                    identity,
                    exc,
                )
                return DispatchResult(
                    disposition=DispatchDisposition.REJECTED,
                    message=(
                        f"调用 {capability_id} 的 {action} 被拒："
                        f"HTTP {exc.response.status_code}"
                    ),
                )
            logger.warning(
                "[TaskCenter] %s 网关报 %s capability=%s %s：%s",
                action,
                exc.response.status_code,
                capability_id,
                identity,
                exc,
            )
            return DispatchResult(
                disposition=DispatchDisposition.DELIVERY_UNKNOWN,
                message=(
                    f"调用 {capability_id} 的 {action} 网关报错："
                    f"HTTP {exc.response.status_code}"
                ),
                # 派活侧的收尾按它加码：试满预算仍是 5xx → 判死（见上面这条注释与
                # `TaskService.finalize_dispatch`）。删除侧不看它（那边非 ACCEPTED 一律
                # 算整体失败）。**不重发这一档**别写成「5xx 不重发」—— 它照样重发。
                dead_after_retry=True,
            )
        except httpx.HTTPError as exc:
            # 连不上 / 读超时：**不知道运行方收没收到**（可能请求到了、只是回包丢了或
            # 来晚了）→ `DELIVERY_UNKNOWN`，派活侧会重发（运行方那侧的幂等闸认出重派就
            # 回成功）。判成明确失败会把一个可能真在跑的子任务做成孤儿。
            logger.warning(
                "[TaskCenter] %s 失败 capability=%s %s: %s",
                action,
                capability_id,
                identity,
                exc,
            )
            return DispatchResult(
                disposition=DispatchDisposition.DELIVERY_UNKNOWN,
                message=f"调用 {capability_id} 的 {action} 失败：{exc}",
            )
        except ValueError as exc:  # 响应不是 JSON
            logger.warning("[TaskCenter] %s 响应无法解析：%s", action, exc)
            return DispatchResult(
                disposition=DispatchDisposition.DELIVERY_UNKNOWN,
                message=f"调用 {capability_id} 的 {action} 响应无法解析：{exc}",
            )

        # 外面这层是 Sidecar /invoke 的信封（{success, code, message, data}）：
        # 它是 false 说明**没拿到运行方的回执**。⚠️ 能走到这里，HTTP 一定是 2xx ——
        # 也就是**能力服务自己**回的 `success=false`（Sidecar 把它的 `code` 原样透传，
        # 如 5003）。**Sidecar 自己发现的问题到不了这里**：它在代理层就抛了，回的是
        # HTTP 500（见上面 5xx 那一档），被 `raise_for_status()` 先拦下。
        # 这层是唯一需要看 `code` 的地方，交给协议层的 `dispatch_disposition` 分档
        # （内层回执壳的规则不一样，别混 —— 见 `_from_receipt`）。
        if not isinstance(payload, dict) or not payload.get("success", False):
            envelope = payload if isinstance(payload, dict) else {}
            message = envelope.get("message") or "（对方没有说明原因）"
            verdict = dispatch_disposition(False, envelope.get("code", -1))
            # ⚠️ **这一档也标 `dead_after_retry`**（与上面 5xx 共用同一个标记）：请求
            # **已经到了运行方**，是它（或它依赖的东西）在业务层失败 —— 与「连不上 / 读
            # 超时」那类「不知道收没收到」正相反，**没有「对方可能已经在跑、只是回包丢了」
            # 这个可能**，判死做不出孤儿。成因与 5xx 相反、处置相同，理由见
            # `DispatchResult.dead_after_retry`。
            # ⚠️ **打标的前提是「合法信封」**（`is_envelope`）：是 dict **且带 `success`
            # 键**。`code` 是什么都行 —— 含可重试码（5001…）与**一切未分类码**
            # （`classify_code` 兜底判 `CLASS_UNKNOWN` 的那些）：能读到这层就说明 HTTP
            # 一定是 2xx、这句话是能力服务说的，判死做不出孤儿。
            # ⚠️ **body 不是这套壳**（非 dict / 是 dict 但没有 `success` 键）时**不打
            # 标记**：那说明响应根本不是这套契约，连「对方收没收到、听没听懂」都不确定，
            # 与「连不上」同档 —— 按纯未知保守处理（打标会在试满后判死一个可能真在跑的
            # 子任务，那是孤儿）。
            # ⚠️ **标记只跟 `DELIVERY_UNKNOWN` 绑**：信封若回的是明确拒绝码（1001 / 1002 /
            # 3005 / 3006…），`dispatch_disposition` 已经判 `REJECTED`、当场收工。
            is_envelope = isinstance(payload, dict) and "success" in payload
            return DispatchResult(
                disposition=verdict,
                message=(
                    f"调用 {capability_id} 的 {action} 未拿到运行方回执：{message}"
                ),
                dead_after_retry=(
                    is_envelope and verdict is DispatchDisposition.DELIVERY_UNKNOWN
                ),
            )

        # 里面这层才是运行方自己的回执（§4.1 ① 的返回，同样三层壳）。
        # 运行方只回 `{success: true, message: "", data: null}`，`data` 恒为 null——
        # 所以不能拿 `data` 当回执本体，得先看它是不是那个壳。
        receipt = payload.get("data")
        if isinstance(receipt, dict) and "success" in receipt:
            return self._from_receipt(ApiResponse.from_dict(receipt))
        return DispatchResult(
            disposition=DispatchDisposition.ACCEPTED,
            message=str(payload.get("message") or ""),
        )

    async def _dispatch_local(
        self,
        capability_id: str,
        action: str,
        data: Dict[str, Any],
        tenant_id: str,
        identity: str,
        *,
        timeout: Optional[float] = None,
    ) -> DispatchResult:
        """进程内直连：运行方已注册进本进程的能力注册中心。

        `timeout` 按请求覆盖（不传就用适配器默认值）—— 下面三处（`wait_for` 与两条
        消息）必须用**同一个** `effective`，否则日志里报的秒数会与实际等的不一样。
        """
        effective_timeout = timeout if timeout is not None else self._timeout
        registry = get_capability_registry()
        if registry.get_capability(capability_id) is None:
            # 这个能力**不在本进程里**：装配漏了（清单说 LOCAL、却没人把它注册进来）、
            # 清单里这一条被删了（运行方下架 / 改名），或者还没注册好。
            #
            # **判 `REJECTED`（当场判死）、不判 `DELIVERY_UNKNOWN`** —— 这类事实
            # （配置 / 装配错了）人不去改，重发多少次都一样，而留在 `running` 上等超时
            # 巡检是一条没人会去查的路。也与本模块 `has_dispatch_target()` 对 LOCAL 那支
            # 的「定位不到」同一口径。
            #
            # ⚠️ **远端通道对同一个事实走的不是这儿，但判决已经拉平了**：那边 Sidecar
            # 发现「能力没在它那侧配端点」时在代理层就抛了（`CapabilityNotFoundError`
            # 2001 → `/invoke` 的 `except Exception` 转成 `CapabilityInvokeError` 2002），
            # 到这边是 **HTTP 500** —— 而 500 与「网关抖了一下」（502/503/504）在那儿
            # **分不开**（`raise_for_status()` 先抛，body 里的 `code` 压根没读），所以
            # 不硬分：先按未知重发，**试满预算、最后一次仍是 5xx 才判死**
            # （`dead_after_retry` 标记交给 `TaskService.finalize_dispatch`）。
            # 于是**判决一致、时机不同**：本地第 1 次就判死（它压根没有「请求可能已经
            # 送达」这个可能），远端晚约 7 秒（3 次尝试 + 退避 2s/5s）。**改这一支时两处一起看。**
            #
            # **代价（有意接受）**：吃掉「应用刚启动、能力还没注册好」这个真·瞬态 ——
            # 调度循环确实可能先于注册跑起来，从前那一档重发 3 次正好兜住它。取舍是
            # **真错配远比起动竞态常见**，而真错配留在 `running` 上挂 7 小时等超时巡检，
            # 是一条没人会去查的路（日志只是 warning，看不出这是装配错）。
            logger.error(
                "[TaskCenter] %s 本地运行方不在册 capability=%s %s",
                action,
                capability_id,
                identity,
            )
            return DispatchResult(
                disposition=DispatchDisposition.REJECTED,
                message=f"运行方 {capability_id} 不在本进程的能力注册中心里",
            )
        try:
            # **进程内调用同样要有界**：远端那条路的界由 httpx 的 `timeout` 给，这条路
            # 一个网络 await 都没有 —— 运行方 handler 卡住就是**永久卡住**，而且卡的是
            # 后台循环（锁与事务都被它拖着）。所以套同一个 `TASK_CENTER_RUNNER_TIMEOUT`。
            #
            # ⚠️ `wait_for` 只承诺**协作式**有界：超时会 `cancel()` 内层协程，但 handler
            # 若不响应取消（中间没有 await 点）就不会真被打断 —— 这次调用的真实工作可能
            # 继续跑完。**这不是缺陷**：那个「其实跑起来了」的执行，正是运行方
            # `ExecutionRegistry` 要认出来的重派（它回 `ALREADY_TAKEN` = 已受理），
            # 派活侧正好把它读成「收下了」。更强的有界化（watchdog 裁决）不在本轮。
            response = await asyncio.wait_for(
                registry.invoke(
                    capability_id,
                    CapabilityRequest(
                        tenant_id=tenant_id,
                        capability_id=capability_id,
                        payload={"action": action, "data": data},
                    ),
                ),
                timeout=effective_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[TaskCenter] %s 本地调用超时 capability=%s %s（%.1fs）",
                action,
                capability_id,
                identity,
                effective_timeout,
            )
            return DispatchResult(
                disposition=DispatchDisposition.DELIVERY_UNKNOWN,
                message=f"调用 {capability_id} 的 {action} 超时（{effective_timeout}s）",
            )

        # 运行方本地回执同样是三层壳，可能为 None（§4.1 ① 的 `data` 恒为 null）。
        # **与走 Sidecar 的远端通道用同一把尺子**：`data` 确实是那个壳才拿它当回执——
        # 否则运行方在能力层 success=True、`data` 里却回了别的 dict（没有 `success` 键）
        # 时，会被误判成「没送达」→ 派活侧重发 → 运行方被重复调用。
        # **先于下面那句成败判定**：壳才是运行方自己的话，能力层那层是包装。
        receipt = response.data
        if isinstance(receipt, dict) and "success" in receipt:
            return self._from_receipt(ApiResponse.from_dict(receipt))

        # ⚠️ **`data` 不是 `None`、又不是那套壳 = 运行方没按契约应答 → 「不知道收没收到」**
        # （`DELIVERY_UNKNOWN`，派活侧据此重发，最多 3 次）。
        #
        # 从前这一档落进下面的 `ACCEPTED`（因为能力层那层 envelope 是 ok），**那是判反了**：
        # 运行方回的是 `ApiResponse.fail(...)` 这种 pydantic 对象 / 裸字符串 / 其它形状时，
        # 平台会**把一次明确的拒绝读成「已受理」** —— 子任务没人再派活、也没人回执，一直挂到
        # `timeout_seconds`（内置步骤 25200 秒）被超时巡检判死，而任务页显示「跑着」。
        # 远端通道对同一件事的判决是 `REJECTED`（JSON 往返把 pydantic 对象还原成了带
        # `success` 的 dict），所以这**也是两条通道对同一事实的第一个分歧点** —— 归到
        # 「未知」而不是「拒绝」是**保守方向**：运行方很可能真的干了活（它只是回错了形状），
        # 判死会把它做成孤儿；重发则由运行方自己的 `ExecutionRegistry` 认出重派、回成功。
        #
        # 契约本体（CLAUDE.md / `jonex_core/task_runtime/runner.py`）：运行方**只写一行
        # `dispatch.update(runner.bind())`**，`bind()` 保证回的是 `.to_dict()` 过的**裸三层壳
        # dict**。所以走到这一支一定是装配方式不对，日志要说清怎么改。
        if receipt is not None:
            logger.warning(
                "[TaskCenter] %s 的回执不是三层壳 dict（收到 %s）capability=%s %s："
                "运行方应当用 `dispatch.update(runner.bind())` 装配，回裸 dict —— "
                "本次按「不知道对方收没收到」重发",
                action,
                type(receipt).__name__,
                capability_id,
                identity,
            )
            return DispatchResult(
                disposition=DispatchDisposition.DELIVERY_UNKNOWN,
                message=(
                    f"运行方 {capability_id} 的 {action} 回执不是三层壳 dict"
                    f"（收到 {type(receipt).__name__}）"
                ),
            )

        if not response.success:
            # 能力被**调到了**（`registry.invoke` 自己吞掉所有异常，返回失败壳），
            # 它明确回不收 —— 运行方收到了报文并拒绝，或能力层自己挡下的。
            # **不重发**：重发一百次还是这句。注意这条路上 `code` 可能是 `-1`、也可能是
            # 能力层自己的码，**都不能当「未知」的信号**（见 `_from_receipt`）。
            return DispatchResult(
                disposition=DispatchDisposition.REJECTED,
                message=response.message or f"运行方 {capability_id} 收不了",
            )
        return DispatchResult(
            disposition=DispatchDisposition.ACCEPTED,
            message=response.message or "",
        )

    # ── 连接池 ────────────────────────────────────────────

    def _get_client(self, endpoint: Optional[str]) -> httpx.AsyncClient:
        base_url = (endpoint or get_config().SIDECAR_URL or "http://localhost:8001").rstrip("/")
        client = self._clients.get(base_url)
        if client is None:
            client = httpx.AsyncClient(
                base_url=base_url,
                headers={"X-API-Key": get_config().GATEWAY_API_KEY},
                timeout=self._timeout,
            )
            self._clients[base_url] = client
        return client


__all__ = [
    "RUNNER_EXECUTE_ACTION",
    "RUNNER_QUERY_STEP_STATUS_ACTION",
    "RUNNER_REMOVE_TASK_ACTION",
    "RUNNER_TASK_STOPPED_ACTION",
    "RunnerAdapter",
    "call_with_retry",
    "has_dispatch_target",
]
