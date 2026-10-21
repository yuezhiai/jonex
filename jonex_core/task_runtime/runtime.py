#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""`TaskRuntime`：运行方拿到的 SDK 门面（§4.2 四个方向）+ 进程单例入口。

```python
# 应用启动入口（每个进程只建一次）
from jonex_core.task_runtime import build_task_runtime
build_task_runtime(service_name="<运行方标识>")

# 业务代码里（不要随手 build，取就行）
from jonex_core.task_runtime import get_task_runtime
runtime = get_task_runtime()
result = await runtime.submit(TaskSubmission(
    definition_id="...", space_id="...", inputs={...},
    task_id="运行方自己生成的 id",   # 带上它 → 失败会自动重试，且重发幂等
))
```

**`submit` 的重试只在自带 `task_id` 时启用**（详见 `submit` 的 docstring）：自带 id 使
重发幂等 —— 重发撞上自己上一次建的那条会回 `REUSED`（含义是「上一次其实已经建成了，
只是响应没回来」），当场收工，而不是建出第二条。

**上报（`report_terminal`）则是一直在重试、不带任何开关**：重复上报在服务端是安全的
（收件箱 `subtask_reports` 不加唯一约束，重复那条会被「已是终态」那道闸吸收掉），
重发不会造成任何副作用。两者共用同一套预算：3 次尝试 / 退避 0.5s、1s —— 这套数字
**定义在协议层**（`task_protocol.results`），因为管理方的派活重发（§4.1 ①）用的是
同一套，而它不能反向依赖本模块。

**重试某一步（`retry_subtask`）也重发，但理由跟前两者都不同**：不是"重发幂等所以安全"，
而是**管理方对这条路自己就是幂等的** —— 上一次其实重试成了、只是响应没回来时，重发撞上
的是「那一步已经在队列里了」，管理方据此回成功而不是报「它还没结束」。所以调用方拿到
`RETRIED` 就理解成「这一步会再跑一次」，**不必分辨是不是这一次调用促成的**（§4.2 ③）。

**重试某几步（`retry_subtasks`）与单步是同一档**（同一个 disposition 词汇表、同一套
重试预算、同一种幂等），唯一多出来的是**顺序**：`node_ids` 给的顺序就是执行顺序
（第 i 步等第 i-1 步成功），管理方把它落成依赖边。要一次重跑几步、而这几步在编排里
**不互为直接上下游**时（例如「解析 + 编译」中间还隔着「入库」），分别调两次单步重试
会让它们同时开跑 —— 那正是这个口子存在的唯一理由（§4.2 ④）。

**两条上报路径，别混**（这是本模块最要紧的一句话）：

| 场景 | 用哪个 | 租户 / 定位字段从哪来 |
| --- | --- | --- |
| handler 内部（在 `acquire_execution` 作用域里） | `RunnerBase.report_success` / `report_failed` | 从 `ExecutionContext`（ContextVar）自动取 |
| 巡检 / 后台循环（**没有那一帧**） | `runtime.report_terminal(carrier, terminal)` | **必须显式传 `carrier`** |

巡检那条路走 ContextVar **必然抛 `ExecutionContextMissingError`** —— 它不是从
`acquire_execution` 里长出来的，那一帧压根不存在。所以 `report_terminal` 的第一个参数
是必填的凭据，签名上就不留"忘了传还能跑"的口子。

**`build_task_runtime` 与 `get_task_runtime` 的分工是刻意的**：每个进程只建一个 runtime、
在启动入口建好再注入各业务服务，**业务代码里不要随手 `build_task_runtime()`**。所以
`get_task_runtime()` 没建过时**抛错、不做懒建** —— 懒建就等于随手建，正是那条要求要避免的。
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from jonex_core.capability.response import ApiResponse
from jonex_core.common.monitor_collector import (
    attempt_span,
    emit_logical_call,
    make_request_id,
)
from jonex_core.common.monitor_models import CallFinalStatus
from jonex_core.common.tenant import TenantContext, require_tenant
from jonex_core.task_protocol.errors import TaskProtocolError, TaskRuntimeNotBuiltError
from jonex_core.task_protocol.models import (
    ExecutionDispatch,
    TASK_SOURCES,
    TaskSubmission,
    check_error_reason,
    check_input_refs,
    check_output_refs,
    check_task_source,
    ERROR_CODE_MAX_LEN,
    ERROR_PARAMS_MAX_BYTES,
    INPUT_REFS_MAX_BYTES,
    INPUT_REFS_MAX_ITEMS,
    INPUT_REF_KINDS,
    INPUT_REF_PARAMS,
    OUTPUT_REFS_MAX_BYTES,
    OUTPUT_REFS_MAX_ITEMS,
    OUTPUT_REF_KINDS,
    OUTPUT_REF_PARAMS,
)
from jonex_core.task_protocol.results import (
    RETRY_BACKOFF_SEC,
    RETRY_MAX_ATTEMPTS,
    SubmissionDisposition,
    SubmissionResult,
    SubtaskRetryDisposition,
    SubtaskRetryResult,
    TaskRef,
    TerminalSubmitDisposition,
    TerminalSubmitResult,
    retry_delay,
    submission_disposition,
    subtask_retry_disposition,
    terminal_disposition,
)
from jonex_core.task_runtime.client import LegacyControlPlaneAdapter
from jonex_core.task_runtime.context import ExecutionContext
from jonex_core.task_runtime.ports import ControlPlaneClient

logger = logging.getLogger(__name__)

#: 运行方只报得了这两种（§4.2 ①）—— `running` 在报文层面就关掉了。
_TERMINAL_SUCCESS = "success"
_TERMINAL_FAILED = "failed"

# 重试预算**定义在协议层**（`jonex_core.task_protocol.results`）：管理方的派活重发
# （`capabilities/task_center/`）要用同一套数字，而它 import 本模块（运行方 SDK）方向
# 是反的。这里只留私有别名，让本模块内的用法一字不用改。
#
# ⚠️ **这几个名字必须继续挂在本模块上**：既有测试用
# `monkeypatch.setattr(runtime_module, "_RETRY_BACKOFF_SEC", (0, 0))` 把退避清零
# （`tests/unit/task_runtime/test_runtime_submit.py` 4 处、
# `test_runtime_runner_report.py` 3 处）。改名或删名 = 那 7 个用例全碎。
_RETRY_MAX_ATTEMPTS = RETRY_MAX_ATTEMPTS
_RETRY_BACKOFF_SEC = RETRY_BACKOFF_SEC


def _retry_delay(attempt: int) -> float:
    """第 `attempt` 次尝试失败后等多久。

    ⚠️ **不能直接 `_retry_delay = retry_delay`**：协议层那个函数读的是**它自己模块**的
    `RETRY_BACKOFF_SEC`，于是 `monkeypatch.setattr(runtime_module, "_RETRY_BACKOFF_SEC", ...)`
    这个 patch 面**是一个死名字** —— 看着改掉了退避，实际一秒没省，那 7 个用例全在真睡
    0.5s + 1s（曾经如此）。所以这里**必须自己读本模块的全局**。

    没被 patch 时（`_RETRY_BACKOFF_SEC` 还是协议层那一个对象）转发给协议层实现 ——
    **公式只有一份**，将来改退避算法不会两边分叉；被 patch 后就地取本模块的序列。
    """
    if _RETRY_BACKOFF_SEC is RETRY_BACKOFF_SEC:  # 没被 patch：走协议层那一份公式
        return retry_delay(attempt)
    return _RETRY_BACKOFF_SEC[min(attempt - 1, len(_RETRY_BACKOFF_SEC) - 1)]


def _now_ms() -> int:
    """13 位毫秒时间戳。管理方**只落库留痕**（`subtask_reports.ts` 一列）、不参与任何
    裁决（管理方不信运行方的钟）。"""
    return int(time.time() * 1000)


#: 本模块四个方向补发的 N1 的「目标对象」—— 打给的都是任务中心控制平面。
#: 取能力 ID（与 Sidecar 侧的 `target` 同口径）。
_N1_TARGET = "business.task_center.v1"


# ── N2/N1 关联 ID（超长时摘要，保证 ≤ VARCHAR(64)）──
#
# 四个方向的 request_id 都是「前缀 + 若干业务 id」拼出来的。业务 id（尤其
# `definition_id` / `node_id` / 批量名单）没有长度上限，拼出来会超过
# `monitor.call_log.request_id` 的 64 字符列宽；采集侧此前不做长度校验，
# 一条超长 ID 会让**整批** INSERT 被 PG 拒绝、整批静默丢失。
# 统一走 `make_request_id`：短 ID 逐字不变，超长时摘要到定长（截断会撞 UNIQUE）。


def _submit_request_id(submission: "TaskSubmission") -> str:
    return make_request_id(
        "rt-submit",
        submission.space_id,
        submission.definition_id,
        submission.task_id,
    )


def _terminal_request_id(execution_id: str) -> str:
    return make_request_id("rt-terminal", execution_id)


def _retry_subtask_request_id(task_id: str, node_id: str) -> str:
    return make_request_id("rt-retry-subtask", task_id, node_id)


def _retry_subtasks_request_id(task_id: str, node_ids: List[str]) -> str:
    return make_request_id("rt-retry-subtasks", task_id, ",".join(node_ids))


def _mark_attempt_final(att) -> None:
    """把本次 N2 Attempt 标成「这次逻辑调用的**最后一次**尝试」。

    冻结接口（`AttemptSpan`）只在**构造时**收 `is_final`，而「这次会不会成」只有拿到
    回包才知道 —— 提前成功那一次才是真正的最后一次（预算里剩下的尝试不会再发生）。
    埋点规则要求「成功那次必 True」，故拿到成功结论后用 ``mark_final()`` 翻那一格，
    而不是去动它的内部状态。
    """
    att.mark_final()


def _collect_logical_call(
    request_id: str,
    tenant_id: str,
    *,
    status: str,
    started_at: datetime,
    error_code: Optional[Any] = None,
    error_message: Optional[Any] = None,
) -> None:
    """给本模块四个方向补发一条 N1（`emit_logical_call` 的薄封装，只补齐本模块的默认值）。

    原实现只采 N2，平台里于是没有以同一 `request_id` 建的 N1 ⇒ `attempt_count` 的
    COUNT 补正永远回填不上（`retry_count` 恒 0）。`target` 四个方向一致，故在这里补。

    **必须由调用方放在重试循环之外**（`finally` 里）—— 理由见 `emit_logical_call`
    的 docstring：循环内逐次投递会因 `UNIQUE(request_id)` 只留第一次尝试的结局。
    """
    emit_logical_call(
        request_id=request_id,
        tenant_id=tenant_id,
        status=status,
        target=_N1_TARGET,
        started_at=started_at,
        error_code=error_code,
        error_message=error_message,
    )


def _as_execution_context(carrier: Union[ExecutionContext, ExecutionDispatch]) -> ExecutionContext:
    """凭据既可以是 `ExecutionContext`，也可以是手头正好有的派活报文。

    ⚠️ **两者都不是就直接抛 `TaskProtocolError`**，不要放它进 `from_dispatch()` ——
    那样传 `None` / 传个裸 dict 得到的是 `AttributeError: 'NoneType' object has no
    attribute 'tenant_id'`：看不出是谁的错、也接不住（它绕过 SDK 的统一异常）。而
    「忘了传凭据」正是把 `carrier` 设成必填要防的那件事，报出来得是 SDK 自己的话。
    """
    if isinstance(carrier, ExecutionContext):
        return carrier
    if isinstance(carrier, ExecutionDispatch):
        return ExecutionContext.from_dispatch(carrier)
    raise TaskProtocolError(
        message=(
            "carrier 只能是 ExecutionContext 或 ExecutionDispatch，"
            f"收到 {type(carrier).__name__}"
        ),
        details={"carrier_type": type(carrier).__name__},
    )


#: `check_output_refs` 的 `reason_code` → 给运行方开发看的英文文案（SDK 不做 i18n）。
_OUTPUT_REF_REASON_TEXT: Dict[str, str] = {
    "not_array": "output_refs must be an array of objects (one dict per entry)",
    "too_large": (
        f"output_refs is too large (over {OUTPUT_REFS_MAX_BYTES} chars): "
        "it carries references only, never the artifact content"
    ),
    "too_many": f"output_refs has too many entries (max {OUTPUT_REFS_MAX_ITEMS})",
    "kind_missing": "every output_ref needs a 'kind'",
    "kind_invalid": f"'kind' must be one of {list(OUTPUT_REF_KINDS)}",
    "params_not_object": "'params' must be an object",
    "params_missing_key": (
        f"a required param is missing — see OUTPUT_REF_PARAMS for the params your "
        f"'kind' must carry ({OUTPUT_REF_PARAMS})"
    ),
    "params_bad_value": "'params' values must be non-empty scalars (str/int/float)",
}


def _check_output_refs(refs: Optional[List[Dict[str, Any]]]) -> None:
    """本地上报前先卡一道 `output_refs`（§4.2 ①）。

    **判据只有一份** —— 协议层的 `check_output_refs`（`jonex_core/task_protocol/models.py`），
    服务端 `_check_report_payload` 调的是**同一个函数**。这里只做两件事：出站前先炸
    （别让调用方白等两轮退避），以及把 `reason_code` 翻成一句能看懂的英文。

    规则见协议层那段 docstring：`kind` 写死枚举、`params` 按 `OUTPUT_REF_PARAMS` 里该 kind
    声明的键给。**引用里不带地址** —— 「该调哪个接口」是前端的映射，运行方碰不到，
    也就填不出外站地址 / `javascript:` / 目录穿越那一类东西。
    """
    problem = check_output_refs(refs)
    if problem is None:
        return
    reason, details = problem
    raise TaskProtocolError(
        message=f"{_OUTPUT_REF_REASON_TEXT.get(reason, 'invalid output_ref')} "
        f"(reason={reason}, details={details})",
        details={"reason": reason, **details},
    )


#: `check_input_refs` 的 `reason_code` → 给运行方开发看的英文文案（SDK 不做 i18n）。
#: 与 `_OUTPUT_REF_REASON_TEXT` **同形、各一份**：`reason_code` 是两条链共用的那套字符串，
#: 但文案要说清坏的是哪一份（"input_refs ..."），否则调用方拿着提示去改错的字段。
_INPUT_REF_REASON_TEXT: Dict[str, str] = {
    "not_array": "input_refs must be an array of objects (one dict per entry)",
    "too_large": (
        f"input_refs is too large (over {INPUT_REFS_MAX_BYTES} chars): "
        "it carries references only, never the content itself"
    ),
    "too_many": f"input_refs has too many entries (max {INPUT_REFS_MAX_ITEMS})",
    "kind_missing": "every input_ref needs a 'kind'",
    "kind_invalid": f"'kind' must be one of {list(INPUT_REF_KINDS)}",
    "params_not_object": "'params' must be an object",
    "params_missing_key": (
        f"a required param is missing — see INPUT_REF_PARAMS for the params your "
        f"'kind' must carry ({INPUT_REF_PARAMS})"
    ),
    "params_bad_value": "'params' values must be non-empty scalars (str/int/float)",
}


def _check_input_refs(refs: Optional[List[Dict[str, Any]]]) -> None:
    """本地上报前先卡一道 `input_refs`（§4.2 ①）。

    **判据只有一份** —— 协议层的 `check_input_refs`（`jonex_core/task_protocol/models.py`），
    服务端 `_check_report_payload` 调的是**同一个函数**（与 `output_refs` 共用内核
    `_check_refs`）。这里只做两件事：出站前先炸（别让调用方白等两轮退避），以及把
    `reason_code` 翻成一句能看懂的英文。

    规则见协议层那段 docstring：`kind` 写死枚举（`INPUT_REF_PARAMS`）、`params` 按该 kind
    声明的键给。**引用里不带地址** —— 「该调哪个接口」是前端的映射，运行方碰不到。
    """
    problem = check_input_refs(refs)
    if problem is None:
        return
    reason, details = problem
    raise TaskProtocolError(
        message=f"{_INPUT_REF_REASON_TEXT.get(reason, 'invalid input_ref')} "
        f"(reason={reason}, details={details})",
        details={"reason": reason, **details},
    )


#: `check_error_reason` 的 `reason_code` → 给运行方开发看的英文文案（SDK 不做 i18n）。
_ERROR_REASON_TEXT: Dict[str, str] = {
    "code_invalid": "'error_code' must be a non-empty string",
    "code_too_long": f"'error_code' is too long (max {ERROR_CODE_MAX_LEN} chars)",
    "code_unknown": (
        "'error_code' is not a registered reason code — see ERROR_REASON_CODES in "
        "jonex_core/task_protocol/models.py (do not invent your own)"
    ),
    "params_not_object": "'error_params' must be an object",
    "params_not_serializable": "'error_params' must be JSON-serializable",
    "params_too_large": (
        f"'error_params' is too large (over {ERROR_PARAMS_MAX_BYTES} chars): it carries "
        "the message-template arguments only, never the failure text itself"
    ),
}


def _check_error_reason(code: Optional[str], params: Optional[Dict[str, Any]]) -> None:
    """本地上报前先卡一道 `error_code` / `error_params`（§4.2 ① 的扩展）。

    **判据只有一份** —— 协议层的 `check_error_reason`，服务端调的是**同一个函数**。
    这里只做出站前先炸 + 把 `reason_code` 翻成一句能看懂的英文（与 `_check_output_refs`
    同一形状）。

    ✅ **要校验代号认不认识**（2026-10-10 起）：代号是协议层那张**写死枚举**
    `ERROR_REASON_CODES` 里的一个 —— 形状错、或不在枚举里，都**当场炸**。见协议层那段
    docstring：不放行未知代号，因为失败原因是要渲染给人看的。
    """
    problem = check_error_reason(code, params)
    if problem is None:
        return
    reason, details = problem
    raise TaskProtocolError(
        message=f"{_ERROR_REASON_TEXT.get(reason, 'invalid error reason')} "
        f"(reason={reason}, details={details})",
        details={"reason": reason, **details},
    )


#: `check_task_source` 的 `reason_code` → 给运行方开发看的英文文案（SDK 不做 i18n）。
_TASK_SOURCE_TEXT: Dict[str, str] = {
    "missing": (
        "'task_source' is required on submit — it says where this task came from "
        "(e.g. which ingestion entry point created it)"
    ),
    "invalid": (
        "'task_source' is not a registered value — see TASK_SOURCES in "
        "jonex_core/task_protocol/models.py (do not invent your own)"
    ),
}


def _check_task_source(value: Optional[str]) -> None:
    """**提交前**先卡一道 `task_source`（2026-10-13 加）。

    **判据只有一份** —— 协议层的 `check_task_source`，服务端 `init_task` 调的是**同一个
    函数**。这里只做出站前先炸 + 把 `reason_code` 翻成一句能看懂的英文（与
    `_check_output_refs` / `_check_error_reason` 同一形状）。

    为什么在**出站前**就炸：来源是 `init_task` 的**必传项**，缺了/填错服务端一样会拒
    （1001）—— 但那样要白等满整轮退避才拿到结论（`submit` 对非成功响应一律重发）。
    一次本地判断就能省掉那段等待。

    **为什么由 SDK 判而不是靠 Pydantic 必填**：`TaskSubmission.task_source` 留着 `None`
    默认值，是为了不破坏「先构造、再补字段」的写法；必填是**这条路的约定**，用这个
    判据表达（见协议层那段 docstring）。
    """
    problem = check_task_source(value)
    if problem is None:
        return
    reason, details = problem
    raise TaskProtocolError(
        message=f"{_TASK_SOURCE_TEXT.get(reason, 'invalid task_source')} "
        f"(reason={reason}, details={details})",
        details={"reason": reason, **details, "valid": list(TASK_SOURCES)},
    )


@dataclass(frozen=True)
class TerminalReport:
    """要上报的终态。用两个工厂造，别直接构造。

    §4.2 ① 对报文本身有硬要求：`status=success` 时**必带** `outputs`、`status=failed` 时
    **必带** `error_message`。缺了会被任务中心当场拒（`InvalidParameterError`，属
    **明确拒绝**那一档 —— 原样重发结果一样）。所以这两个约束在**本地**就用 `__post_init__`
    挡住：与其发出去换一个注定被拒的回包（还要白等两轮退避），不如在出站前就炸。

    这是**调用方自己写错了**，不是任务中心的裁决 —— 所以抛 `TaskProtocolError`，
    不伪装成 `TerminalSubmitDisposition.PERMANENT_REJECTED`（那会被当成业务结论写进死信）。

    ── 本地这道闸与服务端**逐条对齐**，只有两处故意更严 ──

    服务端判据在 `task_service.py` 的 `_check_report_payload`：`not isinstance(outputs,
    dict)` / `not error_message` / 非法 `status`。这里**逐条同口径** —— `outputs={}` 是
    **合法**的（内置的 `compile` 步 `output_schema` 就是 `{}`：空 = 真的没产出，见 §3.4），
    早先写成 `if not self.outputs:` 会把「无产出步骤报成功」这条唯一的正常路径堵死。

    故意更严的两条，别当成不一致：

    | 这里 | 服务端 | 为什么 |
    | --- | --- | --- |
    | `error_message` 拒纯空白 | `"   "` 照收 | 空白等于没写原因，而它是人看失败原因的唯一来源；当场炸比落库一条空白强 |
    | `status` 只认小写字面量 | 先 `.lower()` | 报文只有这一种正确写法（两个工厂）；多认几种大小写只会让「到底哪个对」变模糊 |
    """

    status: str
    outputs: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    ts: Optional[int] = None
    # 产出「去哪儿看」的引用（展示用，**不参与下游取参**）：一个数组，元素只有 `kind` + `params`
    # （形状是**定死的**：`kind` 带运行方前缀、∈ 协议层 `OUTPUT_REF_PARAMS`；`params` 带那个接口
    # 要的字段）。**不带地址** —— 「调哪个接口」是前端的映射，运行方碰不到，所以填不出外站地址 /
    # `javascript:` / 目录穿越 / `DELETE` 那一类东西。管理方只搬运、未知键放行。
    # 可选 —— 不传就是空（前端不显示「查看产出」入口）。
    output_refs: Optional[List[Dict[str, Any]]] = None
    # 输入「**从哪儿来**」的引用（展示用，**不参与下游取参**）：与 `output_refs` 对称的
    # 那一份 —— 那边说「这一步的成果去哪儿看」，这边说「这一步吃的是什么」。
    # 形状同款（元素只有 `kind` + `params`，`kind` ∈ 协议层 `INPUT_REF_PARAMS`）、同样不带地址。
    # 可选 —— 不传就是空（前端不显示「查看输入」入口）；**老运行方不传照常受理**。
    input_refs: Optional[List[Dict[str, Any]]] = None
    # 失败原因的**代号 + 参数**（可选，2026-10-10 加）：管理方存下来，**读的时候**按
    # 请求的 `X-Lang` 现场渲染 —— 所以「哪国语言」不再由写的那一刻决定（写它的多半是
    # 后台循环，那些帧里只有默认语言）。形状：`error_code` 是消息键（如
    # `err.kb.task_center.runner_abandoned`）、`error_params` 是它的占位符实参。
    # `error_message` **仍然必填**（它是原文槽 + 排障快照），代号是**补充**不是替换。
    # 老运行方不传这两个 → 行为一字不变。
    error_code: Optional[str] = None
    error_params: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        _check_output_refs(self.output_refs)
        _check_input_refs(self.input_refs)
        _check_error_reason(self.error_code, self.error_params)
        if self.status == _TERMINAL_SUCCESS:
            # ⚠️ **判据与服务端逐字一致：`not isinstance(outputs, dict)`**，不是 `is None`。
            # 两处含义不同，别改回去：
            #   · `{}` **是合法产出**（空 = 真的没产出）—— 无产出步骤（内置的 `compile`）
            #     报成功时传的就是它，所以**不能**写成 `not self.outputs`；
            #   · 但 `["a"]` / `"x"` / `0` 这类**非 dict** 一律非法 —— 服务端
            #     （`task_service._check_report_payload`）判的就是 `isinstance`，本地从前
            #     只看 `is None`、把这一档漏过去了：handler 传个列表会被**本地放行**，
            #     到服务端才回 1001，客户端白等 1.5 秒重发 3 次才把结论交回调用方。
            if not isinstance(self.outputs, dict):
                raise TaskProtocolError(
                    message="上报成功必须带 outputs，且必须是个对象（dict）（§4.2 ①）",
                    details={"status": self.status, "outputs_type": type(self.outputs).__name__},
                )
        elif self.status == _TERMINAL_FAILED:
            if not self.error_message or not str(self.error_message).strip():
                raise TaskProtocolError(
                    message="上报失败必须带 error_message（§4.2 ①）",
                    details={"status": self.status},
                )
        else:
            # `running` 已经不是合法取值（§4.2 ①）：运行方拿到凭据时子任务必然已经在跑，
            # 再报一句 running 什么也改变不了。pending / timeout 由管理方自己置。
            raise TaskProtocolError(
                message=f"运行方只能上报 success / failed，收到 {self.status!r}（§4.2 ①）",
                details={"status": self.status},
            )

    @classmethod
    def success(
        cls,
        outputs: Dict[str, Any],
        ts: Optional[int] = None,
        output_refs: Optional[List[Dict[str, Any]]] = None,
        input_refs: Optional[List[Dict[str, Any]]] = None,
    ) -> "TerminalReport":
        """跑完了。`outputs` 必填；`output_refs` / `input_refs` 可选（都是展示用的引用）。"""
        return cls(
            status=_TERMINAL_SUCCESS,
            outputs=outputs,
            ts=ts,
            output_refs=output_refs,
            input_refs=input_refs,
        )

    @classmethod
    def failed(
        cls,
        error_message: str,
        ts: Optional[int] = None,
        output_refs: Optional[List[Dict[str, Any]]] = None,
        input_refs: Optional[List[Dict[str, Any]]] = None,
        *,
        error_code: Optional[str] = None,
        error_params: Optional[Dict[str, Any]] = None,
    ) -> "TerminalReport":
        """没跑成。`error_message` 必填 —— 它是人看失败原因的唯一来源。

        `output_refs` 可选：失败时也**可能**有东西可看（半成品、部分切片、错误详情页），
        由运行方自己决定给不给。

        ── `error_code` / `error_params`（可选，2026-10-10 加，**关键字传**）──

        给得出代号就一并带上，管理方会按**读的人的语言**渲染它，比冻住一句话强。
        两条约定：

        · **外层包装走代号，内嵌的原文走 `error_message`**。典型写法是
          `failed(f"{detail}", error_code="err.kb.parse_failed", error_params={"detail": detail})`
          —— 可翻译的是包装（"解析失败：{detail}"），`detail`（引擎原文）原样带出。
          翻不了的东西**不假装能翻**，这是设计的一部分。
        · 代号是**字符串消息键**（不是整数码），且**必须 ∈ 协议层的 `ERROR_REASON_CODES`**
          （写死枚举）—— 填不在枚举里的，本地当场炸、管理方也会拒（1001）。
        """
        return cls(
            status=_TERMINAL_FAILED,
            error_message=error_message,
            ts=ts,
            output_refs=output_refs,
            input_refs=input_refs,
            error_code=error_code,
            error_params=error_params,
        )


class TaskRuntime:
    """SDK 门面。**不要自己 new** —— 经 `build_task_runtime()` / `get_task_runtime()`。
    """

    def __init__(
        self,
        *,
        service_name: str,
        control_plane: Optional[ControlPlaneClient] = None,
    ) -> None:
        if not service_name or not service_name.strip():
            raise TaskProtocolError(message="service_name 不能为空：它标识这个进程是哪个运行方")
        self._service_name = service_name.strip()
        # 不传就走 §4.4 的 `get_task_center_client()`（能力清单决定 LOCAL / REMOTE / MOCK）。
        # 包一层适配器：把「方向不对」的 `execute` / `remove_task` 挡在 SDK 视野之外。
        self._control_plane: ControlPlaneClient = (
            control_plane if control_plane is not None else LegacyControlPlaneAdapter()
        )

    @property
    def service_name(self) -> str:
        return self._service_name

    @property
    def control_plane(self) -> ControlPlaneClient:
        return self._control_plane

    # ── §4.2 ②：提交 ──

    async def submit(
        self,
        submission: TaskSubmission,
        *,
        tenant_id: Optional[str] = None,
    ) -> SubmissionResult:
        """起一条新任务（§4.2 ②）。**自带 `task_id` 时会在本方法内自动重试。**

        ── `submission.task_source`（**必传**，2026-10-13 加）──

        说清「这条任务是哪个入口来的」（∈ 协议层 `TASK_SOURCES` 写死枚举）。出站前
        本地判一次（`_check_task_source`），缺值 / 域外值**当场抛 `TaskProtocolError`** ——
        服务端 `init_task` 一样会拒（1001），但那样要白等满整轮退避。

        **租户**：`tenant_id` 显式传优先，没传取 `TenantContext`。**后台投递器那条路
        必须显式传** —— 它不是请求路径，那一帧里没有上下文。

        收不出合法租户时**抛 `TenantIsolationError`，不回 `REJECTED`**：这是调用方
        装配错了，不是任务中心的裁决。伪装成 `REJECTED` 会让人把「没租户」当成
        「服务端拒绝了这次提交」写进死信，真实的装配 bug 就此消失。

        ── 重试：**只有自带 `task_id` 才重试** ──

        | 这次提交 | 失败后重试吗 | 为什么 |
        | --- | --- | --- |
        | 带了 `task_id` | **重试**（首次 + 2 次，退避 0.5s / 1s） | 自带 id 使重发**幂等**：重发最多撞上自己上一次建的那条（回 `REUSED`），绝不会多建一条 |
        | 没带 `task_id` | **一次都不重试** | 服务端 `init_task` 不做幂等去重，重发就是**新建第二条任务** —— 一次网络抖动换一条重复任务 |

        **重试范围 = 除「成功」与「`REUSED`」之外的全部**：`RETRYABLE_UNKNOWN`、
        `REJECTED`（参数错误这类）都会重试。代价是参数错误也要等满退避才返回；换来的是
        不会因为服务端某一刻回了个奇怪的码，就放弃一次本可成功的提交。

        ── `REUSED`：**当成成功收工** ──

        它表示**上一次调用其实已经建成了，只是响应没回来** —— 这次重发撞上的正是自己
        上次建的那一条（`task_id` 是发起方自己生成的，所以「id 被占」只可能是这个成因）。
        因此它**当场返回、不再往下试**（再试只会一遍遍撞同一个 id），调用方也**不要换
        id 重来**（换 id 就是把同一条任务建第二遍，正是重试本要防的事）。

        成功时返回的 `task` 是**最小标识** `TaskRef`（只有 id + `definition_version_no`），
        不是完整快照 —— 理由见 `TaskRef` 的说明。
        """
        resolved = _resolve_tenant(tenant_id)
        # 来源是这条路的**必传项**：出站前先炸（理由见 `_check_task_source`）——
        # 不在这里拦的话，缺值要白等满整轮退避才拿到服务端那句 1001。
        _check_task_source(submission.task_source)
        # 只有自带 id 才重试：不带 id 时每次重发都可能建出一条新任务（服务端不做幂等
        # 去重），重试等于凭空多建；带了 id 则重发安全 —— 撞上就是 REUSED。
        max_attempts = _RETRY_MAX_ATTEMPTS if submission.task_id else 1
        last: Optional[SubmissionResult] = None

        # —— N2 采集（`monitor.call_attempt`）——
        # request_id 取名依据：**同一次 `submit` 的几次重试共用一个值**，所以只用
        # `submission` 上那几个字段拼（同一逻辑调用传的是同一份值，逐次尝试不变）。
        # 带上 `space_id` / `definition_id`：**没带 `task_id`** 的提交只试一次
        # （`attempt_no` 恒为 1），光用 `task_id` 会拼出 "…:None"，让两次不同的提交
        # 撞上 UNIQUE(request_id, attempt_no) 而互相顶掉。
        #
        # ⛔ **`task_id` 必填，是这条 request_id 不撞号的前提** —— 别为「省一个参数」
        # 把它去掉 / 改成可选。拼串里 `space_id` 与 `definition_id` 在一个空间里是常量，
        # 退化成 `rt-submit:{space}:{definition}:None` 后**同一空间的每次提交都同名**：
        #   * N2（`call_attempt`，UNIQUE 含 attempt_no）尚能靠 attempt_no 勉强分开；
        #   * N1（`call_log`，**UNIQUE(request_id)** 单列）分不开 —— 后写的那条
        #     会被 `ON CONFLICT DO NOTHING` **静默丢弃**，监控里这次提交凭空消失、不报错。
        # 今天的「不可达」不靠本函数守：唯一入口 `submit_ontology_task` 把 `task_id`
        # 定为必填 `str`，两个调用点（`document_service` / 看门狗 `task_watch_service`）
        # 都传自己生成且先落库的 `str(uuid4())`。本注释即那层前提的登记位。
        # 超长时摘要（定长、确定性），短串逐字不变 —— 见 `_submit_request_id`。
        request_id = _submit_request_id(submission)
        wait_before_ms: Optional[int] = None
        # N1 补发状态：**整个重发循环只让这里决定那一条 N1 的结局**（在 `finally` 里发）。
        n1_started_at = datetime.now(timezone.utc)
        n1_status = CallFinalStatus.FAILED.value
        n1_error_code: Optional[str] = None
        n1_error_message: Optional[str] = None

        try:
            for attempt in range(1, max_attempts + 1):
                with attempt_span(
                    request_id=request_id,
                    tenant_id=resolved,
                    attempt_no=attempt,
                    is_final=attempt >= max_attempts,
                    wait_before_ms=wait_before_ms,
                ) as att:
                    response = await self._control_plane.init_task(
                        resolved,
                        submission.definition_id,
                        submission.space_id,
                        submission.inputs,
                        version_no=submission.version_no,
                        name=submission.name,
                        operator_id=submission.operator_id,
                        # 发起方自己生成的 id（不传就是 `None`，管理方自己生成）。透传，不在这里校验 ——
                        # 「这个 id 能不能用」只有管理方能判（它的库、它的主键），撞了会回 1006。
                        task_id=submission.task_id,
                        # 任务来源（必传，上面已判过）。管理方原样落 `tasks.task_source`。
                        task_source=submission.task_source,
                    )
                    disposition = submission_disposition(response.success, response.code)

                    if disposition is SubmissionDisposition.CREATED:
                        # `ApiResponse.from_dict` **不做子模型校验**：`data` 是原样塞进去的裸 dict。
                        # 所以这一步必须自己解析 —— 不解析的话调用方拿到的是 dict，而类型标注说它是
                        # `TaskRef`，谁都不会报错，直到有人去取 `.id`。
                        _mark_attempt_final(att)
                        n1_status = CallFinalStatus.SUCCESS.value
                        return SubmissionResult(
                            disposition=disposition,
                            task=TaskRef.from_payload(response.data),
                            message=response.message,
                        )

                    if disposition is SubmissionDisposition.REUSED:
                        # 上一次其实建成了、只是响应没回来 —— 这正是重试要落到的终点。当场收工：
                        # 再试只会一遍遍撞同一个 id，而且这一档服务端不回 data，没有可解析的东西。
                        _mark_attempt_final(att)
                        n1_status = CallFinalStatus.SUCCESS.value
                        return SubmissionResult(
                            disposition=disposition, message=response.message
                        )

                    last = SubmissionResult(
                        disposition=disposition, message=response.message
                    )
                    # 这次没成：标注结局（判决码 + 服务端原话）。不标注的话默认是按成功记的。
                    att.fail(error_code=response.code, error_message=response.message)

                if attempt < max_attempts:
                    delay = _retry_delay(attempt)
                    logger.warning(
                        "任务提交未成功（第 %d/%d 次，code=%s），%.1fs 后重试：%s",
                        attempt,
                        max_attempts,
                        response.code,
                        delay,
                        response.message,
                    )
                    # 退避睡在 `with` **之外**：睡进 attempt 的时长里会把「等待」记成本次尝试的耗时。
                    await asyncio.sleep(delay)
                    # 下一次尝试的 `wait_before_ms` = 刚才那次退避
                    wait_before_ms = int(round(delay * 1000))

            # max_attempts >= 1，循环至少跑一次，`last` 必然已被赋值
            assert last is not None
            n1_error_code = last.disposition.value
            n1_error_message = last.message
            return last
        except asyncio.CancelledError:
            # 中途取消：与「没发出去」区分开（N1 记 cancelled，N2 由 attempt_span 记）。
            n1_status = CallFinalStatus.CANCELLED.value
            raise
        finally:
            _collect_logical_call(
                request_id,
                resolved,
                status=n1_status,
                started_at=n1_started_at,
                error_code=n1_error_code,
                error_message=n1_error_message,
            )

    # ── §4.2 ①：上报终态（巡检 / 后台循环那条路） ──

    async def report_terminal(
        self,
        carrier: Union[ExecutionContext, ExecutionDispatch],
        terminal: TerminalReport,
    ) -> TerminalSubmitResult:
        """上报终态。**给没有执行上下文的调用方用**（巡检 / 后台循环）。

        handler 内部请用 `RunnerBase.report_success` / `report_failed` —— 那两个从
        ContextVar 取定位字段，不用手传。这里反过来：凭据必须显式传。

        无论走哪条路，最终都是**同一个 `report_status` 调用**（参考文档那条「最终都要走
        同一个 `report_terminal()`」），不存在两条上报实现。

        **返回 `DURABLY_ACCEPTED` 只代表「已受理」，不代表「已经落库」**：任务中心先把
        上报存进收件箱，真正的裁决由它的后台调度循环完成（§7.5 ②）。所以收到
        `DURABLY_ACCEPTED` 之后立刻查任务状态，可能还看不到变化。

        ── 重试：**不需要任何前提，一直都在做** ──

        重复上报在服务端是**安全的**：收件箱 `subtask_reports` **不加任何唯一约束**，
        同一张凭证重复到达会被「已是终态」那道闸吸收掉（§4.2 ①）。所以这里可以放心
        重发 —— 跟 `submit` 不一样，**上报没有「不带 id 就会多建一条」的风险**，
        也就没有开关。

        范围 = **除 `DURABLY_ACCEPTED` 之外的全部**：`RETRYABLE_UNKNOWN` 与
        `PERMANENT_REJECTED` 都会重试。预算与 `submit` 共用（3 次 / 退避 0.5s、1s）。

        **本地挡掉的非法报文不会进这个循环**：`TerminalReport.__post_init__` 把
        「success 缺 `outputs` 或 `outputs` 不是 dict / failed 缺 `error_message` /
        `status` 非法」在出站前就拦了（判据与服务端逐字同口径，2026-09-30 对齐 ——
        原先本地只看 `is None`、漏了「非 dict」那一档）。能走到重试的
        `PERMANENT_REJECTED`，是服务端另判出来的东西（比如租户 / 空间对不上）。
        """
        context = _as_execution_context(carrier)
        # 同一次上报的几次尝试共用一个事件时间：重发的是**同一个事件**，`ts` 不该跟着
        # 重发时刻漂移（它要落 `subtask_reports.ts`）。
        ts = terminal.ts if terminal.ts is not None else _now_ms()
        last: Optional[TerminalSubmitResult] = None

        # —— N2 采集（`monitor.call_attempt`）——
        # request_id 取名依据：`execution_id` 是这次执行的全局唯一标识，同一次上报的
        # 几次重发共用一个值（`execution_id` 逐次不变）。租户取 `context.tenant_id`；
        # 它若非法，采集侧 `_normalize` 会把整条丢弃（**不造假租户兜底**）。
        # 超长时摘要（定长、确定性）—— 见 `_terminal_request_id`。
        request_id = _terminal_request_id(context.execution_id)
        wait_before_ms: Optional[int] = None
        # N1 补发状态：**整个重发循环只让这里决定那一条 N1 的结局**（在 `finally` 里发）。
        n1_started_at = datetime.now(timezone.utc)
        n1_status = CallFinalStatus.FAILED.value
        n1_error_code: Optional[str] = None
        n1_error_message: Optional[str] = None

        try:
            for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
                with attempt_span(
                    request_id=request_id,
                    tenant_id=context.tenant_id,
                    attempt_no=attempt,
                    is_final=attempt >= _RETRY_MAX_ATTEMPTS,
                    wait_before_ms=wait_before_ms,
                ) as att:
                    response = await self._control_plane.report_status(
                        context.tenant_id,
                        context.space_id,
                        context.execution_id,
                        # `task_id` 从上下文里取（**不是**从 `carrier` 现摘）：管理方拿它跟
                        # `space_id` 一起做交叉校验，运行方业务代码一行都不用动。
                        context.task_id,
                        terminal.status,
                        terminal.outputs,
                        terminal.error_message,
                        ts,
                        terminal.output_refs,
                        # ⚠️ **必须关键字传**：`report_status` 那几个位置参数是**承重**的
                        # （`ports.py` 写明「顺序写反了不会有任何东西报错」），所以新加的这几个
                        # 一律走关键字 —— 位置对齐的风险一点都别承担。
                        error_code=terminal.error_code,
                        error_params=terminal.error_params,
                        input_refs=terminal.input_refs,
                    )
                    disposition = terminal_disposition(response.success, response.code)

                    if disposition is TerminalSubmitDisposition.DURABLY_ACCEPTED:
                        _mark_attempt_final(att)
                        n1_status = CallFinalStatus.SUCCESS.value
                        return TerminalSubmitResult(
                            disposition=disposition, message=response.message
                        )

                    last = TerminalSubmitResult(
                        disposition=disposition, message=response.message
                    )
                    # 这次没被受理：标注结局（判决码 + 服务端原话）。
                    att.fail(error_code=response.code, error_message=response.message)

                if attempt < _RETRY_MAX_ATTEMPTS:
                    delay = _retry_delay(attempt)
                    logger.warning(
                        "终态上报未被受理（第 %d/%d 次，code=%s），%.1fs 后重发：%s",
                        attempt,
                        _RETRY_MAX_ATTEMPTS,
                        response.code,
                        delay,
                        response.message,
                    )
                    await asyncio.sleep(delay)
                    wait_before_ms = int(round(delay * 1000))

            # _RETRY_MAX_ATTEMPTS >= 1，循环至少跑一次，`last` 必然已被赋值
            assert last is not None
            n1_error_code = last.disposition.value
            n1_error_message = last.message
            return last
        except asyncio.CancelledError:
            n1_status = CallFinalStatus.CANCELLED.value
            raise
        finally:
            _collect_logical_call(
                request_id,
                context.tenant_id,
                status=n1_status,
                started_at=n1_started_at,
                error_code=n1_error_code,
                error_message=n1_error_message,
            )

    # ── §4.2 ③：重试某一步 ──

    async def retry_subtask(
        self,
        tenant_id: str,
        space_id: str,
        task_id: str,
        node_id: str,
        *,
        operator_id: Optional[str] = None,
    ) -> SubtaskRetryResult:
        """要求重跑某一步（§4.2 ③）。**没被受理会在本方法内自动重发。**

        **按 `node_id` 寻址**：同一个 `step_code` 可以在编排里出现多次（`parse_1` /
        `parse_2` 都是 `parse`），能唯一指认一步的只有 `node_id`。它是**不透明标识** ——
        原样传回去就行，别解析它的形状。

        **形参顺序与端口层（`ControlPlaneClient.retry_subtask`）、与设计文档 §4.2 ③
        完全一致**：`tenant_id` / `space_id` / `task_id` / `node_id` 四个位置参数，
        `operator_id` 关键字。这不是风格问题 —— 四个都是短 id 串，靠位置认人最容易传反，
        三处顺序只要不一致就是接入方第一次调用必踩的坑。

        **租户**：`tenant_id` 显式传优先，没传取 `TenantContext`（跟 `submit` 走的是
        同一个 `_resolve_tenant`，三层 client 也是同一套规则）。**不带请求上下文的调用
        （后台投递 / 定时那类）必须显式传** —— 那一帧里没有上下文，「没传」就等于收不出
        租户。收不出合法租户时**抛 `TenantIsolationError`、不回 `REJECTED`** —— 那是
        装配错了，不是任务中心的裁决（伪装成 `REJECTED` 会让人把装配 bug 当成业务结论）。

        **`operator_id`**：是谁点的这次重试，管理方落 `tasks.updated_by`，**纯记账**
        （不校验、不参与任何判定）。拿不到就传 `None` —— 空值是合法状态，不是漏记。

        ⚠️ **它没有默认值之外的保护**：三处签名（端口 `ControlPlaneClient` / 本门面 /
        `client.py` 的 `_TaskCenterActions`）都写作 `operator_id: Optional[str] = None`，
        **漏传不报错**，只会静默记成空值。契约要求它显式出现，**那是接入方的义务，不是
        运行时拦得下来的事**。所以：别指望「不传会炸」，也**别把它改成必填** —— 那会在
        不改契约的前提下破坏 SDK 的向后兼容。没有人的场景就明写 `operator_id=None`。

        ── 重试：**一直在做，且这里敢做是因为服务端幂等** ──

        范围 = **除 `RETRIED` 之外的全部**：`RETRYABLE_UNKNOWN`（超时 / 连不上 / 抢不到
        任务锁）与 `PERMANENT_REJECTED`（任务还没跑完、上游没成功这类）**都会重发** ——
        判据是 `code` 落在哪张表，不是"重发有没有意义"（同 `report_terminal`）。预算与
        另外四个方向共用：3 次尝试 / 退避 0.5s、1s。

        敢重发的前提是**管理方对这条路幂等**：上一次其实重试成了、只是响应没回来时，
        重发撞上的是「那一步已经在 `pending` / `running` 了」，管理方据此**回成功**而不是
        报「那一步还没结束」。没有这条，一次网络抖动就会让调用方看到"重试失败"、
        而实际上重试已经生效 —— 那是最坏的一种误报。

        ⚠️ **但服务端那道幂等档只覆盖「还在队列里」这一段窗口**：响应丢掉之后那一步若
        **已经重跑完并回到终态**、而任务本身还没终态（同一个任务里别的步骤还在跑），重发
        会撞「任务不是终态」那道前提、回 `1003` → 这里判成 `PERMANENT_REJECTED`。
        **所以拿到 `PERMANENT_REJECTED` 不等于「这一步没被重跑」** —— 要真相就按
        `task_id` 去查那一步的状态。别为了消掉这个误报去放宽服务端的判据：那会把一次
        **真正的新重试请求**吞掉（§4.2 ③）。
        """
        resolved = _resolve_tenant(tenant_id)
        last: Optional[SubtaskRetryResult] = None

        # —— N2 采集（`monitor.call_attempt`）——
        # request_id 取名依据：`node_id` **只有在这条任务里才唯一**（不同任务的
        # `parse_1` 会重名），所以必须把 `task_id` 一起带上 —— 同一次单步重试的几次
        # 重发共用这一个值（两个入参逐次不变）。租户取 `resolved`（已过 `require_tenant`）。
        # 超长时摘要（`node_id` 由编排自定义、长度无上限）—— 见 `_retry_subtask_request_id`。
        request_id = _retry_subtask_request_id(task_id, node_id)
        wait_before_ms: Optional[int] = None
        # N1 补发状态：**整个重发循环只让这里决定那一条 N1 的结局**（在 `finally` 里发）。
        n1_started_at = datetime.now(timezone.utc)
        n1_status = CallFinalStatus.FAILED.value
        n1_error_code: Optional[str] = None
        n1_error_message: Optional[str] = None

        try:
            for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
                with attempt_span(
                    request_id=request_id,
                    tenant_id=resolved,
                    attempt_no=attempt,
                    is_final=attempt >= _RETRY_MAX_ATTEMPTS,
                    wait_before_ms=wait_before_ms,
                ) as att:
                    response = await self._control_plane.retry_subtask(
                        resolved,
                        space_id,
                        task_id,
                        node_id,
                        operator_id=operator_id,
                    )
                    disposition = subtask_retry_disposition(response.success, response.code)

                    if disposition is SubtaskRetryDisposition.RETRIED:
                        _mark_attempt_final(att)
                        n1_status = CallFinalStatus.SUCCESS.value
                        return SubtaskRetryResult(
                            disposition=disposition, message=response.message
                        )

                    last = SubtaskRetryResult(
                        disposition=disposition, message=response.message
                    )
                    # 这次没被受理：标注结局（判决码 + 服务端原话）。
                    att.fail(error_code=response.code, error_message=response.message)

                if attempt < _RETRY_MAX_ATTEMPTS:
                    delay = _retry_delay(attempt)
                    logger.warning(
                        "子任务重试未被受理（第 %d/%d 次，code=%s），%.1fs 后重发：%s",
                        attempt,
                        _RETRY_MAX_ATTEMPTS,
                        response.code,
                        delay,
                        response.message,
                    )
                    await asyncio.sleep(delay)
                    wait_before_ms = int(round(delay * 1000))

            # _RETRY_MAX_ATTEMPTS >= 1，循环至少跑一次，`last` 必然已被赋值
            assert last is not None
            n1_error_code = last.disposition.value
            n1_error_message = last.message
            return last
        except asyncio.CancelledError:
            n1_status = CallFinalStatus.CANCELLED.value
            raise
        finally:
            _collect_logical_call(
                request_id,
                resolved,
                status=n1_status,
                started_at=n1_started_at,
                error_code=n1_error_code,
                error_message=n1_error_message,
            )

    # ── §4.2 ④：重试某几步（按顺序）──

    async def retry_subtasks(
        self,
        tenant_id: str,
        space_id: str,
        task_id: str,
        node_ids: List[str],
        *,
        operator_id: Optional[str] = None,
    ) -> SubtaskRetryResult:
        """要求重跑某几步（§4.2 ④）。**没被受理会在本方法内自动重发。**

        **`node_ids` 的顺序就是执行顺序**：第 i 个节点要等第 i-1 个成功之后才被派活。
        这是它跟「连着调两次 `retry_subtask`」的全部区别 —— 那两次会把两步**同时**放回
        队列（编排里的依赖是直连的，要重跑的两步多半不互为直接上下游），一起跑起来。
        名字里那个「s」承载的就是这件事，**顺序不承载任何其他含义**（不是优先级、不是
        排序提示，就是先后）。

        **形参顺序与端口层（`ControlPlaneClient.retry_subtasks`）、与设计文档 §4.2 ④
        完全一致**：`tenant_id` / `space_id` / `task_id` / `node_ids` 四个位置参数，
        `operator_id` 关键字。理由与单步那处逐字相同：都是短 id 串，靠位置认人最容易
        传反，三处顺序不一致就是接入方第一次调用必踩的坑。

        **租户**：与 `retry_subtask` 同一个 `_resolve_tenant` —— 显式传优先、没传取
        `TenantContext`；不带请求上下文的调用必须显式传。收不出合法租户时**抛
        `TenantIsolationError`、不回 `REJECTED`**（那是装配错了，不是任务中心的裁决）。

        **`operator_id`**：是谁点的这次重跑，管理方落 `tasks.updated_by`，**纯记账**。
        拿不到就传 `None` —— 空值是合法状态，不是漏记。与单步那处同一条 ⚠️：三处签名
        都写作 `Optional[str] = None`，**漏传不报错**，只会静默记成空值。

        **返回值沿用 `SubtaskRetryResult`（同一个 disposition 词汇表）**：这一批**不引入
        任何新的失败形态** —— 能拒的理由还是那几种（任务没跑完 / 名单里某一步没结束 /
        批外上游没成功 / 参数坏），`(success, code)` 的三种落点与单步一一对应。再立一套
        并行的枚举就是把同一套判据抄三份、早晚分叉。

        ── 重试：**一直在做，且这里敢做是因为服务端幂等** ──

        范围、预算、退避与单步那处**逐字相同**（除 `RETRIED` 外全重发；3 次尝试；
        0.5s、1s）。敢重发的理由也一样，只是幂等档换成了批量版：上一次其实生效了、
        只是响应没回来时，**整批**都已经排上队，管理方据此回成功。

        ⚠️ 同样地：**拿到 `PERMANENT_REJECTED` 不等于「这几步没被重跑」** —— 响应丢掉
        之后那一批若已经跑完、而任务还没终态，重发会撞「任务不是终态」那道前提、回
        `1003` → 这里判成 `PERMANENT_REJECTED`。要真相就按 `task_id` 去查那几步的状态。

        ⚠️ **名单本身坏了也是 `PERMANENT_REJECTED`，而且照样重发 3 次**（空名单 / 有
        重复 / 顺序与依赖方向拧着来 → 1001）：判据是 `code` 落在哪张表，不是"重发有没有
        意义"（同 `report_terminal`）。所以调用方**别指望这里替它拦参数** —— 名单是它自己
        拼的，一次拼错就要多等两个退避。代价有界，且比"把一次瞬时失败判成终局"划算。
        """
        resolved = _resolve_tenant(tenant_id)
        last: Optional[SubtaskRetryResult] = None

        # —— N2 采集（`monitor.call_attempt`）——
        # request_id 取名依据：`task_id` + `node_ids`（顺序即执行顺序，同样承载语义），
        # 同一次批量重试的几次重发共用这一个值。带上名单是为了让**同一条任务上的两次
        # 不同批量**不撞 UNIQUE(request_id, attempt_no) 而互相顶掉。
        # 超长时摘要（常见「重新解析」两节点名单就有 72 字符）—— 见
        # `_retry_subtasks_request_id`。
        request_id = _retry_subtasks_request_id(task_id, node_ids)
        wait_before_ms: Optional[int] = None
        # N1 补发状态：**整个重发循环只让这里决定那一条 N1 的结局**（在 `finally` 里发）。
        n1_started_at = datetime.now(timezone.utc)
        n1_status = CallFinalStatus.FAILED.value
        n1_error_code: Optional[str] = None
        n1_error_message: Optional[str] = None

        try:
            for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
                with attempt_span(
                    request_id=request_id,
                    tenant_id=resolved,
                    attempt_no=attempt,
                    is_final=attempt >= _RETRY_MAX_ATTEMPTS,
                    wait_before_ms=wait_before_ms,
                ) as att:
                    response = await self._control_plane.retry_subtasks(
                        resolved,
                        space_id,
                        task_id,
                        node_ids,
                        operator_id=operator_id,
                    )
                    disposition = subtask_retry_disposition(response.success, response.code)

                    if disposition is SubtaskRetryDisposition.RETRIED:
                        _mark_attempt_final(att)
                        n1_status = CallFinalStatus.SUCCESS.value
                        return SubtaskRetryResult(
                            disposition=disposition, message=response.message
                        )

                    last = SubtaskRetryResult(
                        disposition=disposition, message=response.message
                    )
                    # 这次没被受理：标注结局（判决码 + 服务端原话）。
                    att.fail(error_code=response.code, error_message=response.message)

                if attempt < _RETRY_MAX_ATTEMPTS:
                    delay = _retry_delay(attempt)
                    logger.warning(
                        "子任务批量重试未被受理（第 %d/%d 次，code=%s），%.1fs 后重发：%s",
                        attempt,
                        _RETRY_MAX_ATTEMPTS,
                        response.code,
                        delay,
                        response.message,
                    )
                    await asyncio.sleep(delay)
                    wait_before_ms = int(round(delay * 1000))

            # _RETRY_MAX_ATTEMPTS >= 1，循环至少跑一次，`last` 必然已被赋值
            assert last is not None
            n1_error_code = last.disposition.value
            n1_error_message = last.message
            return last
        except asyncio.CancelledError:
            n1_status = CallFinalStatus.CANCELLED.value
            raise
        finally:
            _collect_logical_call(
                request_id,
                resolved,
                status=n1_status,
                started_at=n1_started_at,
                error_code=n1_error_code,
                error_message=n1_error_message,
            )

    # ── 生命周期 ──

    # ── §4.3 读：某一步现在的状态（2026-09-30 开给运行方）──

    async def get_subtask(
        self,
        tenant_id: str,
        task_id: str,
        node_id: str,
        *,
        attempt: Optional[int] = None,
    ) -> ApiResponse[Dict[str, Any]]:
        """**读**某一步现在的状态（§4.3 `task.subtask_get`）。**纯只读。**

        运行方从前**没有任何通道**能读管理方的状态，只能靠自己的超时硬等 —— 典型问题是
        「我这一步是不是已经被管理方判死了」问不出来。典型用法：**放弃之前先问一句**
        （看门狗准备按自己的超时交还时先读一次；看到 `success` / `failed` / `timeout`
        就说明那边已经定局、不用再等）。

        ⚠️ **这里刻意不重试**（与四个写方向都不同）：它是个**查询**，读不到就是读不到 ——
        调用方（看门狗）本来就在自己的循环里，下一轮再读即可。在 SDK 里再叠一层重发，
        只会把「读一次」变成「卡 1.5 秒」。

        ⚠️ **租户必须显式传**（与写方向不同，这里**不回落** `TenantContext`）：读的调用点
        多在后台看门狗那种没有请求上下文的地方，回落只会拿到空值、报一个与真因无关的错。

        `data` 是**裸 dict**（形状同 `SubtaskItemResponse`），投影由调用方自己做 ——
        协议层 / 本层不得 import 能力层的 DTO（有测试钉住那道门禁），见端口层那条说明。
        """
        return await self._control_plane.get_subtask(
            tenant_id, task_id, node_id, attempt=attempt
        )

    # ── §4.3 读：整条任务现在的状态（2026-09-30 开给运行方）──

    async def get_task(
        self, tenant_id: str, task_id: str
    ) -> ApiResponse[Dict[str, Any]]:
        """**读**整条任务现在的状态（§4.3 `task.get`）。**纯只读。**

        与 `get_subtask`（读某一步）是一对，**但两者不能互相替代** —— 要判「这次编排
        是不是已经结束了」，**光看某一步是不够的**：

        ⚠️ **典型反例（这正是它的用途）**：用户把任务**停了**。`task.stop` 只判死
        **还没开始**的那些步骤，**正在跑的那一步原地不动**（运行方没有停止能力，杀了也
        白杀）—— 所以那一刻**那一步仍是 `running`**，而**这条任务**已经是 `failed` 了。
        只读步骤就永远看不出「停了」，只能死等自己的硬超时（最长 6 小时）。

        ⚠️ **这里刻意不重试**（与四个写方向都不同）：它是个**查询**，读不到就是读不到 ——
        调用方（看门狗）本来就在自己的循环里，下一轮再读即可。

        ⚠️ **租户必须显式传**（不回落 `TenantContext`）：读的调用点多在后台看门狗那种
        没有请求上下文的地方，回落只会拿到空值、报一个与真因无关的错。

        `data` 是**裸 dict**（形状同 `TaskItemResponse`），投影由调用方自己做 ——
        协议层 / 本层不得 import 能力层的 DTO。
        """
        return await self._control_plane.get_task(tenant_id, task_id)

    async def aclose(self) -> None:
        """释放控制面连接池。**探测式调用** —— 端口上故意不定义 `aclose`（见 `client.py`）。

        应用关停时调一次。之后不要再用这个实例。
        """
        aclose = getattr(self._control_plane, "aclose", None)
        if aclose is not None:
            await aclose()


def _resolve_tenant(tenant_id: Optional[str]) -> str:
    """显式入参优先，否则回落 `TenantContext`；都收不出来就抛 `TenantIsolationError`。

    这条回落链跟 §4.4 client 内部的 `_resolve_tenant` 是**同一套规则**，但这里要抛
    而不是回失败壳 —— 见 `submit` 的说明。
    """
    return require_tenant(tenant_id or TenantContext.get())


# ============================================================
# 进程单例
# ============================================================

_runtime: Optional[TaskRuntime] = None


def build_task_runtime(
    *,
    service_name: str,
    control_plane_client: Optional[ControlPlaneClient] = None,
) -> TaskRuntime:
    """造 runtime 并注册成进程单例 —— **在应用启动入口调，别在业务代码里调**。

    重复调用会覆盖上一个（后建的赢），不会报错：应用重启 / 测试里重建是正常用法。
    """
    global _runtime
    _runtime = TaskRuntime(service_name=service_name, control_plane=control_plane_client)
    logger.info(
        "任务运行时已创建：service_name=%s control_plane=%s",
        _runtime.service_name,
        type(_runtime.control_plane).__name__,
    )
    return _runtime


def get_task_runtime() -> TaskRuntime:
    """取进程单例；**没建过就抛 `TaskRuntimeNotBuiltError`，不做懒建**。

    懒建等于随手建（见模块 docstring），所以宁可在这里炸得清清楚楚。
    """
    if _runtime is None:
        raise TaskRuntimeNotBuiltError()
    return _runtime


__all__ = [
    "TaskRuntime",
    "TerminalReport",
    "build_task_runtime",
    "get_task_runtime",
]
