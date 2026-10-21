#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""任务执行服务：起任务、收上报、填值、判定、汇总、重跑（§4.3 `task.*`、§六、§七）。

设计出处：`docs/superpowers/specs/2026-09-09-task-center-design.md` §4.1、§4.2、§4.3、
§6.2–§6.4、§7.1–§7.5。

这块是整个任务中心的心脏，几条不能破的线：

- **状态只从 `report_status` 这一条路被运行方推动**（§4.2 ①、§7.1）。管理方内部的派活
  抢占、超时判定、结果汇总走各自内部路径，同样遵守 §7.2 的 CAS 约束。
- **上报是「同步受理 + 入队消化」**（§4.2 ①、§7.5）：`report_status` 只做**报文级**
  校验（`status` 取值、条件必填 —— 不查库、不拿锁），过了就存一行收件箱、回「受理」；
  真正的裁决与落库由后台循环那一趟 `absorb_report` 完成。于是**拿不到锁不再回可重试码**
  （那只是「本轮跳过、下轮再来」），上报能不能落地**不再取决于运行方愿不愿意重发**。
  代价是「返回 `success=true`」从「已落库」变成了「已受理」。
- **结束态改不回去**。所有推进都走 `transition_cas`，`expect_statuses` 里永远不含结束态，
  所以迟到的反向上报只会命中 0 行、被静默拒绝（§7.2、§7.3）。
- **只认「当前运行」**。`tasks` 是稳定身份 + 最新一次运行的快照，最新那次由
  `tasks.current_attempt` 指向；旧运行那套 `subtasks` 是冻结的历史——迟到的上报对不上
  当前运行就整条丢弃（§7.2、§7.3）。
- **`node_id` 是节点身份，`step_code` 是「用的哪个内置步骤」**（§3.5）：连线
  （`depends_on` / `next_steps`）、引用（`@<node_id>.outputs.x`）、上报定位、重试删除，
  **一律按 `node_id`**；`step_code` 只在查内置清单时用（schema / `runner_code` / 并发上限）。
  同一个 `step_code` 可以挂在多个节点上，拿它当身份会指错行。
- **派活先抢后派、派不出就认栽**。`pending → running` 用 `claim_pending` 抢占（同一条
  UPDATE 里回填 `execution_id`、`state_version+1`），抢到才 `execute`；**运行方明确收不了
  （回执 `success=false` / 调用抛异常）→ 该子任务直接置终态 `failed`**（终态、不重派，
  下游**hold**，§4.1 ①、§7.5 ①）。只有「别副本先抢到」「并发到顶」「依赖还没满足」
  才是本轮不派、等下一轮。
- **两表时间同一个值**（§7.2）：派活置 `running` 时 `subtasks.started_at` 与这次尝试在
  `subtask_attempts.started_at` 用同一个时刻；结束置终态时两侧 `finished_at` 同理。
- **填值跟上游产出在同一个事务里**（§6.2）：上游一到「成功」，紧接着就把因此变就绪的
  下游步骤入参算出来落库，读的人看不到「引用了但没填」的半成品。
- **失败 / 超时一律 hold**（§6.3）：上游 `failed` / `timeout` 之后，下游**原地不动**——
  既不置失败、也不置跳过（**一期没有 `skipped` 这个状态**）、更不会被派活，一直 `pending`，
  由任务收 `failed` 兜住。唯一的出路是人工 `retry_subtask`。
- **一期没有自动重试**（§十二「待规划」）：超时就地置 `timeout`、失败就地置 `failed`，
  都是终态，没有「代跑 `retry_subtask`」这一层。手动重试仍在。
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.config import get_config
from jonex_core.common.exceptions import (
    InvalidParameterError,
    ResourceAlreadyExistsError,
    ResourceConflictError,
    ResourceNotFoundError,
    UpstreamServiceError,
)
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant
from jonex_core.task_protocol.models import (
    TASK_SOURCES,
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
    RETRY_MAX_ATTEMPTS,
    DispatchDisposition,
    DispatchResult,
)

from ..dtos.console import SubtaskConsoleItem, TaskConsoleItem, TaskConsolePage
from ..dtos.execution import (
    SubtaskAttemptItem,
    SubtaskAttemptList,
    SubtaskItemResponse,
    TaskAttemptItem,
    TaskAttemptList,
    TaskItemResponse,
    TaskList,
)
from ..dtos.runner import ExecutionDispatch, RemoveTaskDispatch, TaskStoppedDispatch
from ..flows.registry import RunnerRegistry
from ..flows.runner_adapter import (
    RunnerAdapter,
    call_with_retry,
    has_dispatch_target,
)
from ..models import (
    AttemptStatus,
    DefinitionScope,
    Subtask,
    SubtaskAttemptStatus,
    SubtaskStatus,
    Task,
    TaskAttempt,
    TaskDefinitionVersion,
    TaskStatus,
    VersionStatus,
)
from ..models.enums import (
    RUNTIME_REPORTABLE_STATUSES,
    SUBTASK_FAILED_STATUSES,
    SUBTASK_SATISFIED_STATUSES,
    SUBTASK_TERMINAL_STATUSES,
)
from ..models.report import SubtaskReport
from ..repository.catalog_repository import TaskCatalogStepRepository
from ..repository.definition_repository import (
    TaskDefinitionRepository,
    TaskDefinitionVersionRepository,
)
from ..repository.execution_repository import (
    SubtaskAttemptRepository,
    SubtaskRepository,
    TaskAttemptRepository,
    TaskRepository,
)
from ..repository.report_repository import SubtaskReportRepository
from .flow_validator import _detect_cycles, evaluate_bindings, normalize_by_schema
from .error_reason import (
    TASK_AGGREGATE_FAILED,
    build_fail_reason,
    render_error_reason,
)
from .space_guard import (
    require_owned_space,
    require_space_visible,
    visible_space_ids_for,
)
from .task_lock import task_lock

logger = logging.getLogger(__name__)

# 任务状态的合法取值（§4.4 管理端的状态筛选与页签角标都按这一组来）。
# 取枚举的定义顺序，改枚举这里跟着变，不用手工同步。
_TASK_STATUS_VALUES: tuple = tuple(status.value for status in TaskStatus)

# 任务到了这三个状态就算「跑完了」：不能再重跑之外的动作（§4.3 retry / delete 的前提）。
# `stopped` 在册 —— 被用户停掉的任务**确实是结束了**，它就是该能重跑、能删；
# 漏掉它，用户停错之后会发现「停止的任务重跑不了、也删不掉」。
TASK_TERMINAL_STATUSES = (
    TaskStatus.COMPLETED.value,
    TaskStatus.FAILED.value,
    TaskStatus.STOPPED.value,
)


def _duration_seconds(
    started: Optional[datetime], finished: Optional[datetime]
) -> Optional[int]:
    """耗时（整秒）。**没跑完就是 None** —— 不猜「算到现在」，那个数字秒秒在变。"""
    if started is None or finished is None:
        return None
    return int((finished - started).total_seconds())


def _normalize_status_filter(statuses: Optional[List[str]]) -> Optional[List[str]]:
    """把管理端传来的状态筛选归一化成合法值列表；没给就是 `None`（= 全部）。

    值不在册的直接拒（1001）：静默忽略会让人以为筛上了、拿到的却是全量，比报错难查。
    顺带去重 —— `?status=pending&status=pending` 不该拼成 `IN ('pending','pending')`。
    """
    if not statuses:
        return None
    normalized: List[str] = []
    for raw in statuses:
        value = str(raw).strip()
        if value not in _TASK_STATUS_VALUES:
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.task.bad_filter_status",
                    params={"status": value, "allowed": "/".join(_TASK_STATUS_VALUES)},
                    fallback=(
                        f"任务状态只能是 {'/'.join(_TASK_STATUS_VALUES)}，收到 {value}"
                    ),
                ),
                details={"status": value},
            )
        if value not in normalized:
            normalized.append(value)
    return normalized or None

# 「运行方能报哪些状态」（`RUNTIME_REPORTABLE_STATUSES`）与「哪些算终态」
# （`SUBTASK_TERMINAL_STATUSES`）都定义在 `models.enums` —— 那里是状态机口径的**唯一出处**，
# 本模块只引用、不另立一份，免得两处集合悄悄分叉。


def _check_report_payload(
    status: Optional[str],
    outputs: Optional[Dict[str, Any]],
    error_message: Optional[str],
    output_refs: Optional[List[Dict[str, Any]]] = None,
    error_code: Optional[str] = None,
    error_params: Optional[Dict[str, Any]] = None,
    input_refs: Optional[List[Dict[str, Any]]] = None,
    *,
    node_id: Optional[str] = None,
) -> str:
    """判一条上报的**报文本身**合不合法，返回归一化后的 `status`（§4.2 ① 的 ④⑤ 两道闸）。

    这两道闸**不查库、不拿锁**，所以能在同步阶段就判完：调用方报错了东西，当场拒掉
    —— 不然它会拿到「受理」却永远消化不掉（队列里躺着一条注定被扔的记录）。

    判据**只有这一份实现**，调用点也**只有 `report_status`（同步受理）一个**。

    ⚠️ **`_absorb`（异步消化）那次不是调它，是另写的一小段「廉价复核」**（`_absorb` 的
    ④ 段），而且**只复核 `status` 的取值、不复核条件必填**（`success` 必带 dict
    `outputs` / `failed` 必带 `error_message`）—— 分工是刻意的：真正的把关在同步受理
    那一关，异步那段只防「有人绕过入口手工往表里塞」这种脏数据，且**绝不能抛异常**
    （抛了会被后台当成消化失败、每轮重试、永远出不去），所以它只丢弃 + 告警。
    代价是那一段**比这里弱**：一条手工塞进去的 `status=success` + 非 dict `outputs`
    会一路写进 `subtasks.outputs`（脏数据，不会炸 —— 下游取值处有 `isinstance` 兜着）。

    报错的是 `InvalidParameterError`（1001）—— 属「不可重试」那一类（§4.0）：报文坏了，
    原样重发结果一样，让运行方改了再来。

    `node_id` **只进 `details`，不进消息正文**：同步阶段还没定位到子任务，压根不知道是哪个
    节点；消息里塞个 `None` 更糟。`translate` 缺参数时会把未格式化的模板原样吐出来
    （`{node_id}` 字面量会露出来），所以模板里干脆不带它。

    **`status` 不是字符串也走「非法取值」这一档**（`isinstance` 判，不是
    `(status or "").lower()`）：报文是 JSON，`status` 完全可能是数字 / 列表 / 对象 ——
    直接 `.lower()` 抛的是 `AttributeError`，在同步受理阶段就是一个 500，而它本来只是
    一条「参数不合法」（1001，可重试性也完全不同）。`None`（字段整个缺失）同理。

    `outputs` 要求**是个对象**：`{}` 合法（= 真的没产出，内置的 `compile` 步报的就是它），
    但 `[1, 2]` / `"done"` 不合法 —— 它要落 `subtasks.outputs`（JSONB），下游 `@节点.outputs.x`
    的取值也按对象取，放一个非对象进去只会在离出错点很远的地方炸。
    """
    normalized = status.lower() if isinstance(status, str) else ""
    if normalized not in RUNTIME_REPORTABLE_STATUSES:
        raise InvalidParameterError(
            message=translate(
                "err.task_center.subtask.bad_event_status",
                params={"status": status},
                fallback=(
                    f"上报的状态 {status} 不是合法取值（运行方只能报 success / failed）"
                ),
            ),
            details={"status": status, "node_id": node_id},
        )
    if normalized == SubtaskStatus.SUCCESS.value and not isinstance(outputs, dict):
        raise InvalidParameterError(
            message=translate(
                "err.task_center.subtask.outputs_required",
                fallback="上报成功时必须带 outputs（一个 JSON 对象）",
            ),
            details={"node_id": node_id, "missing": "outputs"},
        )
    if normalized == SubtaskStatus.FAILED.value and not error_message:
        raise InvalidParameterError(
            message=translate(
                "err.task_center.subtask.error_message_required",
                fallback="上报失败时必须带 error_message",
            ),
            details={"node_id": node_id, "missing": "error_message"},
        )
    _check_output_refs(output_refs, node_id=node_id)
    # 输入侧那份引用（可选）：同一套内核、另一张表 —— 见 `_check_input_refs`。
    _check_input_refs(input_refs, node_id=node_id)
    # 失败原因的代号 + 参数（可选）：**形状 + 枚举**都卡（见 `_check_error_reason`）。
    # 成功那一档带了也无妨 —— 渲染侧只在失败时用它。
    _check_error_reason(error_code, error_params, node_id=node_id)
    return normalized


#: `check_output_refs` 的 `reason_code` → 中文回落文案（`err.task_center.subtask.output_ref.*`
#: 缺词条时用它）。**只在这里出现一次**：SDK 那份另有一张英文表，两边共用同一套 reason_code。
_OUTPUT_REF_REASON_FALLBACK: Dict[str, str] = {
    "not_array": "上报的 output_refs 必须是「对象数组」（每个元素一个对象）",
    "too_large": (
        f"上报的 output_refs 太大（超过 {OUTPUT_REFS_MAX_BYTES} 字符）："
        "它只放「去哪儿看」的引用，不要放产物正文"
    ),
    "too_many": f"上报的 output_refs 条数太多（上限 {OUTPUT_REFS_MAX_ITEMS} 条）",
    "kind_missing": "每一条引用都要有 kind",
    "kind_invalid": f"kind 取值不合法：可选 {list(OUTPUT_REF_KINDS)}",
    "params_not_object": "params 必须是对象",
    "params_missing_key": f"params 缺少该 kind 需要的参数（见 {OUTPUT_REF_PARAMS}）",
    "params_bad_value": "params 的值必须是非空标量（字符串 / 数字）",
}


def _check_output_refs(
    output_refs: Optional[List[Dict[str, Any]]], *, node_id: Optional[str] = None
) -> None:
    """卡 `output_refs` 的**规则**（§4.2 ①）。

    **判据只有一份实现** —— 协议层的 `check_output_refs`
    （`jonex_core/task_protocol/models.py`），SDK 出站前那道调的也是它。这里只做一件事：
    把 `reason_code` 翻成 i18n 文案 + `InvalidParameterError`（1001，当场拒）。

    规则（2026-10-09 用户拍板）：引用只有 `kind` + `params` 两个字段 —— `kind` 写死枚举
    `knowledge_base.chunks / knowledge_base.ontology / knowledge_base.wiki`（前端按它挑渲染方式、**并据它知道自己该调哪个接口**；⚠️ **值必须带「运行方前缀」** —— 格式 `{运行方 code}.{产物类型}`：任务中心是**多运行方**的，裸 `chunks` 迟早和别人撞），
    `params` 带那个接口要的字段（每个 kind 要哪些，写死在协议层 `OUTPUT_REF_PARAMS` 里）。
    **未知键放行。**

    ⚠️ 引用里**不带地址** —— 这是刻意的：运行方碰不到 url，也就填不出外站地址 / `javascript:` /
    目录穿越 / `DELETE` 那一类东西，**注入面归零**。

    ⚠️ 与 `outputs` 的分工：`outputs` 是**契约字段**（下游 `@节点.outputs.x` 取它、发版校验
    按它比），这一列**只给前端做入口**，**绝不参与取参**。
    """
    problem = check_output_refs(output_refs)
    if problem is None:
        return
    reason, details = problem
    raise InvalidParameterError(
        message=translate(
            f"err.task_center.subtask.output_ref.{reason}",
            # 两个上限**一次全给**：每条文案只挑自己用得着的占位符渲染，
            # 省得在映射表里再维护一份「哪个 reason 要哪个参数」。
            params={
                "max_bytes": OUTPUT_REFS_MAX_BYTES,
                "max_items": OUTPUT_REFS_MAX_ITEMS,
            },
            fallback=_OUTPUT_REF_REASON_FALLBACK.get(reason, "上报的 output_refs 不合法"),
        ),
        details={"node_id": node_id, "reason": reason, **details},
    )


#: `check_input_refs` 的 `reason_code` → 中文回落文案（词条见
#: `err.task_center.subtask.input_ref.*`）。与 output 那份**同形、各一张表** —— 加新 kind
#: 时两边的 `kind_invalid` 都要能说清"合法取值有哪些"。
_INPUT_REF_REASON_FALLBACK: Dict[str, str] = {
    "not_array": "上报的 input_refs 必须是「对象数组」（每个元素一个对象）",
    "too_large": (
        f"上报的 input_refs 太大（超过 {INPUT_REFS_MAX_BYTES} 字符）："
        "它只放「从哪儿来」的引用，不要放正文"
    ),
    "too_many": f"上报的 input_refs 条数太多（上限 {INPUT_REFS_MAX_ITEMS} 条）",
    "kind_missing": "每一条引用都要有 kind",
    "kind_invalid": f"kind 取值不合法：可选 {list(INPUT_REF_KINDS)}",
    "params_not_object": "params 必须是对象",
    "params_missing_key": f"params 缺少该 kind 需要的参数（见 {INPUT_REF_PARAMS}）",
    "params_bad_value": "params 的值必须是非空标量（字符串 / 数字）",
}


def _check_input_refs(
    input_refs: Optional[List[Dict[str, Any]]], *, node_id: Optional[str] = None
) -> None:
    """卡 `input_refs` 的**规则**（§4.2 ①），与 `_check_output_refs` 同一个内核。

    **判据只有一份实现** —— 协议层的 `check_input_refs`（与服务端 `_check_report_payload`、
    SDK 出站前那道是**同一个函数**）。这里只做一件事：把 `reason_code` 翻成 i18n 文案 +
    `InvalidParameterError`（1001，当场拒）。

    规则：引用只有 `kind` + `params` 两个字段 —— `kind` 写死枚举（协议层 `INPUT_REF_PARAMS`：
    `knowledge_base.document` / `knowledge_base.chunks`，值必须带运行方前缀），`params` 带
    该 kind 要的字段。**未知键放行。**

    ⚠️ **可选**：`None`（老运行方不上报）直接放行 —— 设成必填会把「失败上报」本身拒掉。
    知识库那条运行方当前**总是带**（成功、失败都带），但那是运行方实现的口径，
    **不是协议层的必填**：将来会出现真没有输入引用的任务。
    """
    problem = check_input_refs(input_refs)
    if problem is None:
        return
    reason, details = problem
    raise InvalidParameterError(
        message=translate(
            f"err.task_center.subtask.input_ref.{reason}",
            # 两个上限一次全给（同 output 那份）：文案各挑自己用得着的占位符。
            params={
                "max_bytes": INPUT_REFS_MAX_BYTES,
                "max_items": INPUT_REFS_MAX_ITEMS,
            },
            fallback=_INPUT_REF_REASON_FALLBACK.get(reason, "上报的 input_refs 不合法"),
        ),
        details={"node_id": node_id, "reason": reason, **details},
    )


#: `check_error_reason` 的 `reason_code` → 中文回落文案（缺词条时用它）。
_ERROR_REASON_FALLBACK: Dict[str, str] = {
    "code_invalid": "上报的 error_code 必须是非空字符串",
    "code_too_long": f"上报的 error_code 太长（上限 {ERROR_CODE_MAX_LEN} 字符）",
    "code_unknown": (
        "上报的 error_code 不在登记过的代号里（见协议层 ERROR_REASON_CODES）—— "
        "代号只有一份写死的枚举，运行方不许自创"
    ),
    "params_not_object": "上报的 error_params 必须是对象",
    "params_not_serializable": "上报的 error_params 必须能转成 JSON",
    "params_too_large": (
        f"上报的 error_params 太大（超过 {ERROR_PARAMS_MAX_BYTES} 字符）："
        "它只放消息模板的实参，不要放失败正文"
    ),
}


def _check_error_reason(
    error_code: Optional[str],
    error_params: Optional[Dict[str, Any]],
    *,
    node_id: Optional[str] = None,
) -> None:
    """卡失败原因**代号 + 参数**的**形状**（§4.2 ① 的扩展，2026-10-10 加）。

    **判据只有一份实现** —— 协议层的 `check_error_reason`，SDK 出站前那道调的也是它。
    这里只把 `reason_code` 翻成 i18n 文案 + `InvalidParameterError`（1001，当场拒）。

    ✅ **要校验「代号认不认识」**：代号必须 ∈ 协议层的 `ERROR_REASON_CODES`
    （写死枚举）。⚠️ **这一条与 `output_refs` 的「未知键放行」相反，是刻意的**：
    引用里多带一份数据无害，而失败原因**要渲染给人看** —— 放行未知代号等于允许运行方
    往管理方界面上写任意文本。加新代号 = 枚举里加一行 + 两个 yaml 各加一条。
    """
    problem = check_error_reason(error_code, error_params)
    if problem is None:
        return
    reason, details = problem
    raise InvalidParameterError(
        message=translate(
            f"err.task_center.subtask.error_reason.{reason}",
            params={
                "max_code": ERROR_CODE_MAX_LEN,
                "max_params_bytes": ERROR_PARAMS_MAX_BYTES,
            },
            fallback=_ERROR_REASON_FALLBACK.get(reason, "上报的失败原因代号不合法"),
        ),
        details={"node_id": node_id, "reason": reason, **details},
    )


#: `check_task_source` 的 `reason_code` → 中文回落文案（缺词条时用它）。
_TASK_SOURCE_REASON_FALLBACK: Dict[str, str] = {
    "missing": "发起任务必须给出任务来源（task_source）",
    "invalid": f"任务来源取值不合法：可选 {list(TASK_SOURCES)}",
}


def _check_task_source(task_source: Optional[str]) -> None:
    """卡 `init_task` 必传的**任务来源**（2026-10-13 加）。

    **判据只有一份实现** —— 协议层的 `check_task_source`，SDK 出站前那道调的也是它。
    这里只把 `reason_code` 翻成 i18n 文案 + `InvalidParameterError`（1001，当场拒）。

    为什么服务端还要再判一次（SDK 明明已经判过）：`task.init_task` 这个 action 也**能
    被直接调到**（不经 SDK，比如以后别的管理端链路），而它才是落库的守门人 —— 与
    `_check_output_refs` / `_check_error_reason` 完全同一条理由。

    ⚠️ **`None` 在这里是不通过**：这条路（运行方发起）必传。平台侧自己建的任务
    （定时触发 / 人工立即触发）走的是 `fire`，**不经过这里**，它们的 `task_source`
    留空是合法的（见 `Task.task_source` 列注释）。
    """
    problem = check_task_source(task_source)
    if problem is None:
        return
    reason, details = problem
    raise InvalidParameterError(
        message=translate(
            f"err.task_center.task.task_source.{reason}",
            params={"valid": "/".join(TASK_SOURCES)},
            fallback=_TASK_SOURCE_REASON_FALLBACK.get(reason, "任务来源不合法"),
        ),
        details={"reason": reason, **details},
    )


def _localized_console_row(row) -> Dict[str, Any]:
    """控制台两行 DTO 的入参：把失败原因**按当前请求语言重渲**之后再摊给 DTO。

    为什么必须在这一处做：库里那份 `error_message` 是**写入时**渲染的快照 —— 写它的多半是
    后台循环（超时 / 防僵尸 / 派活判死），那些帧里只有默认语言，**直接透出去就是把语言冻住**，
    而读侧此刻是有 `X-Lang` 的（中间件已把它放进 `LocaleContext`）。有代号就现渲、没有
    （引擎原文 / 老数据 / 老运行方）就回落到快照 —— 见 `render_error_reason`。

    ⚠️ **只覆盖 `error_message` 一个键**，其余照原样摊开（就是 `row.to_dict()`）。
    `error_code` / `error_params` **不进 DTO**：它们是给渲染用的底料，不是前端的契约字段
    （进来只会多两个前端不认识、也不该依赖的键）。
    """
    data = row.to_dict()
    data["error_message"] = render_error_reason(
        data.get("error_message"), data.get("error_code"), data.get("error_params")
    )
    # **显式摘掉底料两列**，不指望 DTO 的 `extra="ignore"` 兜 —— 那层是"静默丢弃"，
    # 而静默丢弃正是本仓库栽过的坑（`node_id` 那次）。这里摘掉，谁读这段代码都看得见
    # 「这两列不是给前端的」。
    data.pop("error_code", None)
    data.pop("error_params", None)
    return data


def _steps_by_node(config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """编排里的节点表：`node_id` → 节点对象（那份编排 dict 的 `steps[]` 逐条摊平）。

    两个地方读它：`RunContext.steps`（跑起来要按节点取 `bind_inputs` / `sort`）与
    下游联动开关（`downstream_on_retry` / `downstream_on_stop`）。
    **两处各写一遍迟早分叉** —— 将来编排里多一种节点形状，一边过滤一边不过滤就会变成
    「同一个节点，调度认、开关不认」。
    """
    return {
        step.get("node_id"): step
        for step in (config.get("steps") or [])
        if isinstance(step, dict)
    }


class RunContext:
    """一次运行的上下文：编排 + 那一刻的那套子任务。

    填值、判就绪、汇总任务终态都靠它，避免每个动作各查一遍库。

    **三份索引各按各的键**，别串了（§3.5）：

    - `by_key` / `subtasks` —— 按 **`node_id`** 索引（节点身份：连线、引用、定位都用它）；
    - `steps` —— 也按 **`node_id`**（编排里那个节点对象，含它的 `bind_inputs`）；
    - `catalog_steps` —— 按 **`step_code`**（内置步骤清单，给 schema / `runner_code` / 并发上限）。
    """

    def __init__(
        self,
        *,
        task: Task,
        attempt: TaskAttempt,
        config: Dict[str, Any],
        catalog_steps: Dict[str, Any],
        subtasks: List[Subtask],
    ):
        self.task = task
        self.attempt = attempt
        self.config = config or {}
        self.catalog_steps = catalog_steps
        self.subtasks = subtasks
        self.by_key: Dict[str, Subtask] = {s.node_id: s for s in subtasks}
        self.steps: Dict[str, Dict[str, Any]] = _steps_by_node(self.config)

    @property
    def tenant_id(self) -> str:
        return self.task.tenant_id

    def step_sort(self) -> Dict[str, int]:
        """节点稳定排序表：多来源合并按它排（§6.2）。**键是 `node_id`**。"""
        return {
            key: (step.get("sort") if isinstance(step.get("sort"), int) else 0)
            for key, step in self.steps.items()
        }

    def upstream_outputs(self, subtask: Subtask) -> Dict[str, Dict[str, Any]]:
        """本节点各上游的产出，**按 `node_id`**（取不到就是空对象，交给表达式按 MISSING 处理）。

        ⚠️ **「上游」= 本节点 `depends_on` 里那几个节点**，就是这个字典的全部来源 ——
        所以 `bind_inputs` 里写了 `@B.outputs.x`、而 `B` **不在** `depends_on` 里时，
        这次取值**必然 MISSING**（表达式按缺值落 `default` / 空值，静默、不报错）。
        发版校验**不会**拦这种编排（N8 只要求被引用的节点**存在**，见
        `flow_validator._check_ref`）——「引用不是依赖」是刻意的设计，
        `depends_on` 管调度顺序、引用管数据来源，所以两边允许不一致。
        推理链与「要拿到值就得把 B 写进 `depends_on`」这条用法写在设计文档 §6.2 的
        「引用一个『不是本步骤上游』的节点怎么办」。
        """
        outputs: Dict[str, Dict[str, Any]] = {}
        for dep in subtask.depends_on or []:
            sibling = self.by_key.get(dep)
            if sibling is not None and isinstance(sibling.outputs, dict):
                outputs[dep] = sibling.outputs
        return outputs

    def deps_satisfied(self, subtask: Subtask) -> bool:
        """这一步的依赖是不是都满足了（§6.3：`depends_on` 里每个都 `success`）。"""
        return _deps_satisfied(subtask, self.by_key)

    def downstream_closure(self, subtask: Subtask) -> List[Subtask]:
        """顺着 `next_steps` 摸出所有（传递）下游（按 `node_id`）。"""
        seen: set[str] = set()
        queue = list(subtask.next_steps or [])
        ordered: List[Subtask] = []
        while queue:
            key = queue.pop(0)
            if key in seen:
                continue
            seen.add(key)
            node = self.by_key.get(key)
            if node is None:
                continue
            ordered.append(node)
            queue.extend(node.next_steps or [])
        return ordered


@dataclass
class PreparedDispatch:
    """派活前半段的产物：交给「出站 + 后半段」用的**全部信息**（§7.2「锁不跨出站调用」）。

    刻意只装**不依赖会话的快照**（字符串 + 报文），**不装 `RunContext` 里的 ORM 行** ——
    前半段那个事务一提交、锁一放，那些行就过期了，拿过期行去写库就是写脏。后半段重新
    拿锁之后**重读**一遍，再按 `execution_id` 认「还是不是我抢下的那一行」。
    """

    tenant_id: str
    task_id: str
    task_attempt_id: str
    node_id: str
    execution_id: str
    capability_id: str
    dispatch: ExecutionDispatch


class TaskService:
    """任务实例服务：起任务 / 收上报 / 查询 / 重跑 / 后台循环用的单步动作。"""

    def __init__(
        self,
        session: AsyncSession,
        *,
        runner_adapter: Optional[RunnerAdapter] = None,
    ):
        self.session = session
        self._tasks = TaskRepository(session)
        self._attempts = TaskAttemptRepository(session)
        self._subtasks = SubtaskRepository(session)
        self._subtask_attempts = SubtaskAttemptRepository(session)
        self._reports = SubtaskReportRepository(session)
        self._definitions = TaskDefinitionRepository(session)
        self._versions = TaskDefinitionVersionRepository(session)
        self._catalog_steps = TaskCatalogStepRepository(session)
        self._runners = RunnerRegistry(session)
        self._adapter = runner_adapter or RunnerAdapter()

    # ========================================================
    # 起任务（§4.3 task.fire）
    # ========================================================

    async def fire(
        self,
        tenant_id: str,
        definition_id: str,
        space_id: str,
        inputs: Optional[Dict[str, Any]] = None,
        *,
        version_no: Optional[int] = None,
        trigger_id: Optional[str] = None,
        name: Optional[str] = None,
        origin: str = "manual",
        operator_id: Optional[str] = None,
        task_id: Optional[str] = None,
        task_source: Optional[str] = None,
    ) -> TaskItemResponse:
        """按锁定版本生成任务（`pending`）+ 首次运行 + 一套子任务（§4.3 `task.fire`）。

        **定义没有任何已发布版本时直接拒绝**（没东西可跑）；`version_no` 不填就用
        `current_version_no`，填了但那一版不存在 / 未发布也直接报错（不悄悄回落）。

        **`space_id` 是「这次运行落在哪个空间」**（§3.5）：所用版本是 `scope=space` 的，
        就必须用版本自带的那个空间——调用方传的跟它不一致**直接报错**（静默忽略会让调用方
        以为落在 A、实际落在 B）；是 `system` / `tenant` 级的就用调用方传的这个。

        **`name` 是这次任务实例的名字**（落 `tasks.task_name`）：给了就用，**没给、或只给
        空白，都退回定义名 `task_definitions.name`**。它只是给人看的标签 —— 不参与任何校验、
        不参与派活，改了它不影响跑的哪一版、也不影响子任务图。

        建出来是 `pending`：等后台循环抢占派活。`started_at` 此刻留空 —— 它是「首次**开始**
        时间」，等第一个子任务真被派出去、任务转 `running` 时才写（§7.2）。

        **`operator_id` 是建这条任务的人**（`user_id`，落 `tasks.created_by`，只写这一次）：
        不填就是「不是人点的」——触发器自动触发、内部系统调用都是这个情形，留空是**对的**，
        不是漏记。它纯记账，不参与任何校验或权限判定。

        **`task_id` 是调用方自己生成的 id**（可选）：给了就用它当主键，不传由模型默认值
        生成。判重的口径与理由见 `init_task` 的说明（这里只做，不复述）。

        **`task_source` 是本条任务的来源**（可选，2026-10-13 加）：原样落
        `tasks.task_source`，取值 ∈ 协议层 `TASK_SOURCES`。**这一层不判「必传」也不校验
        取值** —— 只有 `init_task` 那条对外路要求必传且合法（它在进来之前就判完了）；
        内部触发路径（`fire_trigger` 的定时 / 人工立即触发）**不传**，落 NULL 是对的。
        """
        tenant_id = require_tenant(tenant_id)
        space_id = require_tenant(space_id)
        await require_owned_space(self.session, tenant_id, space_id)
        # 自带 id 能不能用：**必须在任何写库动作之前判**（下面还有版本校验、入参校验、
        # 算运行方名单一堆事）—— id 被占是「前提不成立」，没必要往下做无用功。
        if task_id:
            if await self._tasks.exists_by_id(task_id):
                raise ResourceAlreadyExistsError(
                    message=translate(
                        "err.task_center.task.id_taken",
                        params={"task_id": task_id},
                        fallback=f"任务 id {task_id} 已经被占用了（若这个 id 是你自己带上的，"
                                 f"说明上一次已经建成了，按成功处理、不要换 id）",
                    ),
                    details={"task_id": task_id},
                )
        definition = await self._definitions.get_visible(definition_id, tenant_id)
        if definition is None:
            raise ResourceNotFoundError(
                message=translate(
                    "err.task_center.def.not_found",
                    params={"definition_id": definition_id},
                    fallback=f"任务定义 {definition_id} 不存在",
                ),
                details={"definition_id": definition_id},
            )

        locked_no = version_no if version_no is not None else definition.current_version_no
        if not locked_no:
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.task.no_published_version",
                    params={"definition_id": definition_id},
                    fallback=f"定义 {definition_id} 还没有任何已发布版本，没东西可跑",
                ),
                details={"definition_id": definition_id},
            )
        version = await self._versions.get_version(definition_id, locked_no)
        if version is None:
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.task.version_not_found",
                    params={"definition_id": definition_id, "version_no": locked_no},
                    fallback=f"定义 {definition_id} 没有版本 {locked_no}",
                ),
                details={"definition_id": definition_id, "version_no": locked_no},
            )
        if version.status != VersionStatus.PUBLISHED.value:
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.def.version_not_published",
                    params={"definition_id": definition_id, "version_no": locked_no},
                    fallback=f"定义 {definition_id} 的版本 {locked_no} 还没发版，跑不了",
                ),
                details={"definition_id": definition_id, "version_no": locked_no},
            )

        task_space_id = self._resolve_task_space(definition_id, version, space_id)

        config = version.config or {}
        # 任务级入参按这版编排的 input_schema 收口：必填补齐 / 可选值卡过 / 默认值填过。
        normalized, issues = normalize_by_schema(
            config.get("input_schema") or {},
            inputs or {},
            path=f"任务 {definition_id} 的入参",
        )
        if issues:
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.task.inputs_invalid",
                    params={"definition_id": definition_id, "count": len(issues)},
                    fallback=f"任务入参没通过校验（{len(issues)} 条问题）",
                ),
                details={"definition_id": definition_id, "issues": issues},
            )

        # 任务实例名：调用方给了就用它（只给空白当没给），没给退回定义名。
        # `name` 不是字符串（JSON 里传了数字 / 对象）也当没给 —— 它只是给人看的标签，
        # 不为它单开一个错误码，但也绝不让一个显示用的字段把整次调用炸掉。
        given_name = name.strip() if isinstance(name, str) else ""
        task_name = given_name or definition.name

        # 运行方名单在**这里**钉死。只有这一刻版本必然还在（上面刚 `get_version` 过），
        # 而 `task.delete` 需要在「定义已经被删掉」的情况下照样知道该找谁清残留 ——
        # 详见 `_runner_capabilities_for_task` 的说明。
        runner_capability_ids = await self._runner_capabilities(
            definition.task_catalog_code, config
        )

        now = datetime.now()
        task_values: dict[str, Any] = {
            "tenant_id": tenant_id,
            "definition_id": definition_id,
            "definition_version_no": locked_no,
            # 编排快照 + 内置清单编码快照：钉在这里，此后推进这条任务**再不反查**
            # 版本与定义（理由见 `Task.definition_config` 的说明）。
            "definition_config": config,
            "task_catalog_code": definition.task_catalog_code,
            "space_id": task_space_id,
            "task_name": task_name,
            "trigger_id": trigger_id,
            # 来源原样落库（`None` = 平台侧自己发起的任务 / 老行，见列注释）
            "task_source": task_source,
            "inputs": normalized,
            "runner_capability_ids": runner_capability_ids,
            "current_attempt": 1,
            "status": TaskStatus.PENDING.value,
            "state_version": 0,
            "created_by": operator_id,
        }
        if task_id:
            # 调用方自带的 id 显式落到主键上。**不能无条件写 `id=task_id`** —— `None` 会
            # 覆盖掉列默认的 `str(uuid4())`，INSERT 带一个 NULL 主键直接报错。
            task_values["id"] = task_id
        task = await self._tasks.create(**task_values)
        attempt = await self._attempts.create(
            tenant_id=tenant_id,
            space_id=task_space_id,
            task_id=task.id,
            attempt=1,
            status=AttemptStatus.PENDING.value,
            created_at=now,
        )

        ctx = await self._build_context(
            tenant_id=tenant_id,
            task=task,
            attempt=attempt,
            config=config,
        )
        # 入口步骤的依赖天然满足，诞生时就把它们的值填好，后台循环下一轮即可派活。
        for subtask in ctx.subtasks:
            if ctx.deps_satisfied(subtask):
                await self._fill_inputs(ctx, subtask)

        logger.info(
            "[TaskCenter] 起任务 task=%s definition=%s version=%s space=%s steps=%d origin=%s",
            task.id,
            definition_id,
            locked_no,
            task_space_id,
            len(ctx.subtasks),
            origin,
        )
        return TaskItemResponse(**task.to_dict())

    @staticmethod
    def _resolve_task_space(
        definition_id: str, version: TaskDefinitionVersion, space_id: str
    ) -> str:
        """定「这次运行落在哪个空间」（§3.5、§4.3 `task.fire`）。

        - 版本是 `scope=space` 的 → 用**版本自带的** `space_id`；调用方传的不一致就报错
          （别静默忽略：那会让调用方以为落在 A、实际落在 B）；
        - 版本是 `system` / `tenant` 级（不绑空间）→ 用调用方传的那个。
        """
        if version.scope != DefinitionScope.SPACE.value:
            return space_id
        if version.space_id != space_id:
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.task.space_mismatch",
                    params={
                        "definition_id": definition_id,
                        "space_id": space_id,
                        "version_space_id": version.space_id,
                    },
                    fallback=(
                        f"定义 {definition_id} 的版本 {version.version_no} 是空间级的，"
                        f"它属于空间 {version.space_id}，跟这次传的 {space_id} 对不上"
                    ),
                ),
                details={
                    "definition_id": definition_id,
                    "version_no": version.version_no,
                    "space_id": space_id,
                    "version_space_id": version.space_id,
                },
            )
        return version.space_id

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
    ) -> TaskItemResponse:
        """**运行方发起一条新任务**（§4.2 ②）—— 典型场景：知识库传完文件，要起一条文档处理任务。

        **它就是"对外的 `fire`"**：内部实现完全复用 `fire`（同一套校验 A4~A15、同一套落库），
        只是换个对外名字。**为什么不让运行方直接调 `fire`**：`fire` 是任务中心的内部实现，
        改内部不该影响对外契约；对外只留 `init_task` 一个口子，将来要给「运行方发起的任务」
        加名额 / 频率限制也只改它。

        **不经过触发器**：`trigger_id` 为 NULL、`origin` 是 `manual`（§3.5 `tasks.trigger_id`）。

        **`name` 是这次任务的名字**（落 `tasks.task_name`，可选）：给了就用，**没给、或只给
        空白，退回定义名 `task_definitions.name`**。它只是给人看的标签，不参与校验也不影响
        跑哪一版 —— 跟「一次触发跑出多条任务」的场景配套：`init_task` 不做幂等去重，同一
        定义可以起多条，光看定义名分不清谁是谁。

        **不做幂等去重**：调两次就建两条独立任务。要防重复发起，得由调用方自己按业务标识
        去重（比如「这个 `storage_key` 是否已经起过任务」）。

        **`task_id` 是「调用方自己生成的 id」**（可选）：给了就用它，**不传由这里生成**
        （`str(uuid4())`）。给调用方这个口子的理由是**它得先把 id 落到自己的行上**：等这个
        调用回来才知道 id 是什么的话，它那张表就只能等提交之后再补，中间失败就留一条没有
        归属的记录。带上 id 反过来，调用方先落库、再提交，两边从第一刻起就对得上。

        **撞了已有任务 → `ResourceAlreadyExistsError`（1006）**：判的是**主键占没占**
        （`tasks.id` 是全局主键，别的租户的、已经假删的都算占）。判定放在整条校验链的
        **最前面** —— id 被占是「前提不成立」，没必要往下做无用功。

        ⚠️ **1006 的语义是「这条任务早就在了」，不是「这次请求有问题」**：对自带 id 的
        调用方，它意味着**上一次调用其实已经建成了、只是响应没回来** —— 重发撞上的正是
        自己上次建的那一条。所以协议层判成 `SubmissionDisposition.REUSED`（复用既有任务），
        调用方**当成成功收工、不要换 id 重来**。判成拒绝会让「带 id 重发」把同一条任务建
        第二遍，恰好是重试本要防的那件事。

        **`operator_id` 是「发起这次上传的人」**（可选，`user_id`，落 `tasks.created_by`）：
        运行方那边从请求上下文拿得到就顺手带上来，拿不到就留空。同样纯记账 —— 空着不报错，
        也不改变任何行为。

        **`task_source` 是「这条任务从哪个入口来的」**（2026-10-13 加，**必传**）：
        取值 ∈ 协议层写死的 `TASK_SOURCES`（格式 `{运行方 code}.{对象}.{通道}`，如
        `knowledge_base.file.manual_upload`），**域外值 / 缺值当场拒（1001）**。
        ⚠️ 判据只有一份 —— 协议层 `check_task_source`，SDK 出站前（`TaskRuntime.submit`）
        调的是同一个函数（这里相当于再守一道门，因为 `task.init_task` 这个 action 也能
        被直接调到）。
        ⚠️ **它只是记账标签**：不进 `inputs`、不决定跑哪条编排、不参与任何判定。
        平台侧自己发的任务（定时触发 / 人工立即触发）走 `fire`、不传它，落 NULL 是**对的**。

        调用完就不用管了 —— 后台调度循环会自动接手（扫到、抢占、派给运行方 `execute`）。
        """
        _check_task_source(task_source)
        return await self.fire(
            tenant_id,
            definition_id,
            space_id,
            inputs,
            version_no=version_no,
            trigger_id=None,
            name=name,
            origin="manual",
            operator_id=operator_id,
            task_id=task_id,
            task_source=task_source,
        )

    # ========================================================
    # 运行方上报（§4.2 ①，运行方改状态的唯一入口）
    # ========================================================

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
    ) -> None:
        """收一次运行方上报：**报文级校验当场做，其余入队等后台消化**（§4.2 ①）。

        这条路分两半，界线是「**不拿锁、不写库能不能判**」：

        - **同步（这里）**：报文本身合不合法 —— ④ `status` 取值、⑤ 条件必填项；
          再加上 ⑥ **身份一致性**（报文里的任务 / 空间 / 凭证必须互相对得上，见
          `_require_report_identity`）。判不过**当场拒**（1001 或 1002），**不入队**；
        - **异步（后台循环那一趟调的 `absorb_report`）**：这条上报在**当前数据状态下**
          算不算数 —— ① 定位那行 `subtasks`、② 是不是「当前运行」、③ 是不是已终态，
          然后拿任务锁、落库、填下游、收尾判定。

        ⚠️ ⑥ 那道**要读一眼库**（按主键 / 唯一索引单行读），所以同步阶段的边界从原来的
        「不查库」放宽成「**不拿锁、不写库**」：读到的是没拿锁的旧值，只用来拒明显自相矛盾
        的报文，**不作为任何裁决** —— 裁决仍全在后台拿锁那次（`refresh=True` 重读）。

        **为什么把后一半拿出去**：后一半全都要拿任务锁，而锁是要等的（窗口 ≈3.15 秒，
        §7.2 ①）。从前的处置是「等不到就回可重试码、让运行方自己重发」—— 上报落不落地
        取决于运行方愿不愿意实现重试。改成入队后，**只要被受理就一定被处理到底**
        （拿不到锁只是「本轮跳过、下轮再来」），运行方**不需要为「拿不到锁」写任何重试**。
        （传送层那种「压根没送到」另说 —— 那是 SDK 的事，它对「没被受理」的响应会统一
        重发，见 §4.5。）

        **返回 `success=true` 只代表「受理」**，不代表已经落库：运行方收到响应后立刻查
        任务状态，可能还看不到变化（要等后台循环那一轮）。这跟从前「返回即已落库」不一样。

        ①②③ 三道裁决闸现在都在后台。**①③ 是丢弃**（定位不到 / 已是终态 → 不推进状态、
        不报错，运行方看到的仍然是「受理」，§7.3 幂等吸收）；**② 不是丢弃** —— 不属于
        当前运行的那条照样落库，只是「只收它自己那一行」（§4.4：那条 `running` 占着并发
        名额，丢掉就永远收不回来）。

        ⚠️ 一处随之而来的语义变化：**重复上报 + 坏报文，从「静默吸收」变成「当场拒」**。
        从前 ③（已终态）**最先**拦，轮不到 ④⑤；现在 ④⑤ 在同步阶段，而 ③ 在后台 ——
        一条重复上报如果报文本身是坏的，会被拒。判断是更合理（报文坏了就是坏了，跟是不是
        重复无关；运行方重发带的是同一个 payload，正常重发踩不到）。

        `ts` 是运行方侧的事件时间，**只落库留痕（本表 `ts` 一列）、不参与任何裁决**
        （运行方的钟不可信，乱序判定本来就靠 `execution_id` 与 `state_version`）。
        ⚠️ 它**不进日志** —— 管理方侧没有任何一条 logger 带它，别去日志里找。
        """
        tenant_id = require_tenant(tenant_id)
        space_id = require_tenant(space_id)

        # ④⑤ 报文级校验：不拿锁、不写库就能判。判不过当场拒 —— 连队列都不进。
        normalized = _check_report_payload(
            status,
            outputs,
            error_message,
            output_refs,
            error_code,
            error_params,
            # 输入「从哪儿来」的引用（可选，2026-10-16 加）：与 `output_refs` 同一条哨口、
            # 同一个内核，只是换表。**关键字传**，不挤进前面那条位置参数序列。
            input_refs=input_refs,
        )

        # ⑥ 身份一致性核对：报文里那几个标识必须互相指得上。它要读一眼库，但**仍不拿锁、
        #    不写库**（两道单行读）；对不上当场拒、不入队（见方法的 docstring）。
        await self._require_report_identity(tenant_id, space_id, task_id, execution_id)

        # 受理：落一行「未消化」就算收下了，后台循环保证处理到底。
        await self._reports.enqueue(
            tenant_id=tenant_id,
            space_id=space_id,
            execution_id=execution_id,
            task_id=task_id,
            status=normalized,
            outputs=outputs,
            error_message=error_message,
            # 失败原因的代号 + 参数：**必须进收件箱** —— 消化是**另一趟**（后台循环），
            # 那一刻拿不到原始报文；不存下来就退化成「只有渲染好的文本」，
            # 而渲染好的文本正是这次要治的那个「语言被冻住」的问题。
            error_code=error_code,
            error_params=error_params,
            ts=ts,
            output_refs=output_refs,
            # 输入引用的理由同上：消化在另一趟，那一刻只有收件箱这一份报文。
            input_refs=input_refs,
        )

    async def _require_report_identity(
        self, tenant_id: str, space_id: str, task_id: str, execution_id: str
    ) -> None:
        """⑥ 身份一致性核对：报文里那几个标识必须互相指得上（§4.2 ①）。

        两道，都**不拿锁、不写库**（按主键 / 唯一索引单行读）：

        1. **「任务 ↔ 空间」是不是真的**：查 `tasks`，`task.space_id` 与报文 `space_id`
           不符 → 拒（1002）；
        2. **这张凭证是不是真属于这条任务**：按 `(tenant_id, space_id, execution_id)`
           定位 `subtasks`，命中行的 `task_id` 与报文 `task_id` 不符 → 拒（1002）。

        **两道都只在「查得到」时才有判据**：查不到就放行、去后台按老规矩办。

        ── 为什么"查不到"不拒（这条别顺手改掉）──

        - **任务查不到**（已被删 / id 压根不存在）：两种成因在库里长得一模一样，分不出来；
          而「任务删了之后运行方迟到的上报」是正常生命周期 —— 拒它等于给一条正常路径
          回硬错；
        - **凭证查不到**（重试把旧 `execution_id` 清掉了 / 迟到很久）：这是既有明文规定的
          **幂等吸收**路径，**绝不能拒** —— 否则重试之后的旧凭证上报会收到一个硬错。

        一句话：**报文自相矛盾 → 拒；报文只是过期 → 不拒。**

        ⚠️ **两道有先后**：「任务 ↔ 空间」判在「凭证 ↔ 任务」之前。两道的判据**同时
        成立**时（任务在、空间不符，**并且**那张凭证也不属于报文里的 `task_id`），报出来
        的是**第一道**那个拒。顺序只影响**报哪一条文案**（两条 `message` 不同）；两档
        都是 **1002**，对运行方的重发行为没有差别。

        ⚠️ **两道的 `details` 都只放调用方自己给的那几个值（回显），不带真相**：
        第一道**不回 `task.space_id`**、第二道**不回凭证真正归属的那条 `task_id`** ——
        码已经统一按「找不到」给了，`details` 里再补一句真话等于把那条承诺从后门放掉
        （而它正是防越权探测的全部意义）。**别顺手加回来** —— 这两个键
        （`task_space_id` / `credential_task_id`）这里从前都有，是**刻意删掉**的，
        理由与 `_require_space_match` 的 docstring 同一条。

        ⚠️ **删第二道那个键不损失排查能力**（这是它敢删的理由，不是「反正没用」）：
        那条信息调用方**自己手上就有** —— 凭证是管理方派活时发给它的，而 §7.3 要求它
        在调下游之前先把这条登记落库，「这张凭证属于哪条任务」它自己一查便知。管理方
        把答案写进回包，等于替它查了一遍它本该自己查的东西，代价却是把「找不到」这个
        码从后门放掉。

        ⚠️ 拒的码用 **1002（找不到）**，不用 1001：形状跟平台既有的「不属于当前租户 /
        空间的**一律按找不到处理**」一致（也是防越权探测的同一手法），而且任务中心里
        「任务 / 子任务找不到」本来就用 1002。两档都在 `TASK_CENTER_REJECTED_CODES` 里，
        对运行方的重发行为没有差别 —— 差别只在排查方向。
        """
        task = await self._tasks.get_task(tenant_id, task_id)
        if task is not None and task.space_id != space_id:
            raise ResourceNotFoundError(
                message=translate(
                    "err.task_center.report.space_mismatch",
                    params={"task_id": task_id, "space_id": space_id},
                    fallback=(
                        f"任务 {task_id} 不属于空间 {space_id}，"
                        "这条上报自相矛盾，已当场丢弃"
                    ),
                ),
                details={
                    "task_id": task_id,
                    "space_id": space_id,
                },
            )

        row = await self._subtasks.get_by_execution_id(
            tenant_id, space_id, execution_id
        )
        if row is not None and row.task_id != task_id:
            raise ResourceNotFoundError(
                message=translate(
                    "err.task_center.report.credential_mismatch",
                    params={"task_id": task_id, "execution_id": execution_id},
                    fallback=(
                        f"受理凭证 {execution_id} 不属于任务 {task_id}，"
                        "这条上报自相矛盾，已当场丢弃"
                    ),
                ),
                details={
                    "task_id": task_id,
                    "execution_id": execution_id,
                },
            )

    async def absorb_report(self, report: SubtaskReport) -> None:
        """消化一条入队的上报（后台循环调，§7.5）。

        就是 `_absorb` 的公开入口 —— 后台循环不该去碰私有方法。**它自己不管「标记已
        消化」**：那一步必须跟这里的落库在**同一个事务**里做，由调用方（`_report_one`）
        在拿到锁之后一并收尾。

        `running` 上报已经下线（§4.2 ①），所以传进来的 `status` 必然是 success / failed。
        """
        await self._absorb(
            tenant_id=report.tenant_id,
            space_id=report.space_id,
            execution_id=report.execution_id,
            status=report.status,
            outputs=report.outputs,
            error_message=report.error_message,
            error_code=report.error_code,
            error_params=report.error_params,
            ts=report.ts,
            output_refs=report.output_refs,
            input_refs=report.input_refs,
        )

    async def _absorb(
        self,
        *,
        tenant_id: str,
        space_id: str,
        execution_id: str,
        status: str,
        outputs: Optional[Dict[str, Any]],
        error_message: Optional[str],
        ts: Optional[int],
        output_refs: Optional[List[Dict[str, Any]]] = None,
        error_code: Optional[str] = None,
        error_params: Optional[Dict[str, Any]] = None,
        input_refs: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """消化一条上报：① 重新定位 → ② 判「当前运行」→ ③ 判终态 → 落库（§4.2 ①）。

        **② 只影响「收完这一行之后还汇总不汇总」，不影响收不收**：旧运行的行照样落库
        （把那个 `running` 收成终态、把并发名额还回去），只是碰不到任务快照 —— 见下面
        闸② 那段与 `_aggregate` 开头那道闸。

        **调用方必须已经持有这个任务的任务锁**（从前是本方法自己 `async with`，
        现在由 `_report_one` 在外面拿 —— 因为「标记已消化」要跟这里的落库同事务）。
        """
        # ① 重申定位：拿锁前读到的那行这会儿可能已经变了（重跑换了一套子任务）。
        #    `refresh=True` 不能省 —— 锁外那次读已经把同一行装进过这个会话了。
        subtask = await self._subtasks.get_by_execution_id(
            tenant_id, space_id, execution_id, refresh=True
        )
        if subtask is None:
            logger.info(
                "[TaskCenter] 上报被丢弃（锁内重读定位不到）execution=%s", execution_id
            )
            return

        ctx = await self._load_context(tenant_id, subtask.task_id, subtask.task_attempt_id)
        if ctx is None:
            logger.info(
                "[TaskCenter] 上报被丢弃（运行上下文取不到）task=%s execution=%s",
                subtask.task_id,
                execution_id,
            )
            return
        # ② **归属哪一次运行**（§4.2 ①、§7.3）。判据是「这条上报挂的那次运行，是不是
        #    `tasks.current_attempt` 指的那次」——**不能**拿 `ctx.attempt.id` 跟
        #    `subtask.task_attempt_id` 比：`ctx` 正是按后者装出来的，两者恒等，比了等于没判。
        #
        #    **不属于当前运行的上报从前是直接丢弃的**，理由是不许它去动任务快照。但丢弃
        #    等于把一条真的跑完的活记成「还在跑」——**并发名额永远收不回来**（
        #    `count_running_by_catalog_step` 只数 `running`、不分运行）。它会积在哪个场景：
        #    任务被「停止」时那个正在跑的子任务杀不掉、留在 `running`（§4.4），用户随后
        #    `retry_task` 换了运行，旧那行就再没有任何一趟捞得到它。
        #
        #    现在改成**放行**，安全性由两件事保证、而不是靠丢弃：
        #    - `_advance` 的 CAS 带 `expect_execution_id`（旧凭证对不上就 0 行）+ 闸③ 终态，
        #      能改到的**只有它自己那一行**；
        #    - `_aggregate` 开头那道「只有当前运行才汇总」的闸，让这次消化**碰不到任务快照**。
        #    一句话：**它只收自己那一行（把名额还回去），不改任务、不改别人。**
        if ctx.attempt.attempt != ctx.task.current_attempt:
            logger.info(
                "[TaskCenter] 收到旧运行的上报 task=%s 当前运行=#%s 上报运行=#%s"
                "（只收它自己那一行，不汇总）",
                subtask.task_id,
                ctx.task.current_attempt,
                ctx.attempt.attempt,
            )
        current = ctx.by_key.get(subtask.node_id)
        if current is None or current.id != subtask.id:
            logger.info(
                "[TaskCenter] 上报被丢弃（子任务已被重跑替换）execution=%s", execution_id
            )
            return
        # ③ 终态最先拦：重复上报、迟到的反向上报全在这里被丢掉（这就是幂等吸收）。
        #    从前还有「迟到的 `running`」一种，现在运行方报不了它了（受理时就拦下）。
        if current.status in SUBTASK_TERMINAL_STATUSES:
            logger.info(
                "[TaskCenter] 上报被丢弃（已经是终态）subtask=%s 当前=%s 收到=%s",
                current.id,
                current.status,
                status,
            )
            return

        # ④ 报文级校验（取值 + 条件必填）**在受理时已经过了**（`_check_report_payload`，
        #    也就是入队之前）。这里只留一道廉价复核：队列里的记录理论上都被放行过，真出现
        #    非法值（有人手工往表里塞过？）就**丢弃 + 告警**，**绝不能抛异常** —— 抛了会被
        #    后台当成「消化失败」留在队列里，每轮重试、永远出不去。
        normalized = (status or "").lower()
        if normalized not in RUNTIME_REPORTABLE_STATUSES:
            logger.warning(
                "[TaskCenter] 上报被丢弃（状态取值非法，受理时本该拦下）"
                "execution=%s 收到=%s",
                execution_id,
                status,
            )
            return

        await self._advance(
            ctx,
            current,
            status=normalized,
            outputs=outputs,
            error_message=error_message,
            error_code=error_code,
            error_params=error_params,
            ts=ts,
            output_refs=output_refs,
            input_refs=input_refs,
        )

    async def _advance(
        self,
        ctx: RunContext,
        subtask: Subtask,
        *,
        status: str,
        outputs: Optional[Dict[str, Any]],
        error_message: Optional[str],
        error_code: Optional[str] = None,
        error_params: Optional[Dict[str, Any]] = None,
        ts: Optional[int] = None,
        output_refs: Optional[List[Dict[str, Any]]] = None,
        input_refs: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """消化一条上报的第 4 步：按 `status` 二选一落库 + 收尾判定（§4.2 ①）。

        - `success` → 本子任务 **CAS 置 `success`（连产出一起写）** → 给还不是终态的下游填值；
        - `failed` → CAS 置 `failed` + 写 `error_message` → **下游保持原样（hold）**。

        ⚠️ **两条的顺序是承重的，别照着直觉调换**：填下游那一步（`_fill_downstream` →
        `_fill_inputs` → `evaluate_bindings`）要读**上游那一行的产出**，而那个值只在
        CAS 之后由 `_apply_local` 写进内存对象（`RunContext.upstream_outputs` 读的就是
        这一份）。把「填下游」挪到 CAS 之前，下游拿到的就是**上一轮**的产出（首次为
        `None`），按「缺值」算 —— 那正是「重跑上游、下游却按旧产出跑」这个已经专门
        修过的 bug 换一种形式回来。

        **没有 `running` 这一支**：运行方报不了它（`RUNTIME_REPORTABLE_STATUSES` 只有
        两种，受理时就拦下了）。理由 —— `execution_id` 是派活抢占时跟
        「`pending → running`」写在同一条 UPDATE 里的（`claim_pending`），所以运行方
        拿到凭证时状态**必然已经是 `running`**，它再报一句 `running` 改变不了任何东西
        （既不推进状态、也没有任何判据在读那个时间戳），干脆整条路关掉（§4.2 ①）。
        """
        now = datetime.now()
        values: Dict[str, Any] = {"finished_at": now}
        # 产出「去哪儿看」的引用：**跟这次 CAS 一起落库**（`None` 就不动这一列 ——
        # 老运行方不上报时保持原值，重跑后上一次的引用不会被凭空清掉）。
        if output_refs is not None:
            values["output_refs"] = output_refs
        # 输入「从哪儿来」的引用：与产出那份**同一条口径** —— 带上才写、`None` 就不动这一列
        # （老运行方不上报时保持原值，重跑后上一次的引用不会被凭空清掉）。
        if input_refs is not None:
            values["input_refs"] = input_refs
        if status == SubtaskStatus.SUCCESS.value:
            # `outputs` 走到这里**必然是个 dict**（受理那一步 `_check_report_payload` 已经把
            # 类型收口了；`{}` 合法 = 真的没产出）。这里只做「`None` 兜成空对象」一件事 ——
            # 从前写的 `outputs or {}` 会把**任何**假值（`[]` / `""` / `0`）一并悄悄换成 `{}`，
            # 看着像「反正落进去也是空」，实则把「报文不合法」这个事实抹掉了。
            values["outputs"] = outputs if outputs is not None else {}
        else:
            values["error_message"] = error_message
            # 失败原因的代号 + 参数：与 `error_message` **同一次 CAS 一起落**（三个一起
            # 才是完整的一条原因）。这里**无条件覆盖**（连 `None` 一起写）—— 上一次
            # 尝试的原因不该在新一次失败上残留。
            values["error_code"] = error_code
            values["error_params"] = error_params

        changed = await self._subtasks.transition_cas(
            subtask.tenant_id,
            subtask.id,
            expect_statuses=[SubtaskStatus.RUNNING.value],
            next_status=status,
            expect_version=subtask.state_version,
            expect_execution_id=subtask.execution_id,
            **values,
        )
        if not changed:
            # 走到这里说明在 ③ 之后、这次 CAS 之前被别的副本抢了先（或 `state_version`
            # 被上报之外的路推过）：结束态没有反向路径，静默拒绝（§7.3）。
            logger.info(
                "[TaskCenter] 上报被拒（状态不合前驱 / 版本落后）subtask=%s 当前=%s 收到=%s",
                subtask.id,
                subtask.status,
                status,
            )
            return

        _apply_local(subtask, status=status, **values)
        # 历史行同一次尝试跟着收口，`finished_at` 与 `subtasks.finished_at` 同值（§7.2）。
        await self._sync_attempt_row(
            subtask,
            status,
            outputs=outputs if status == SubtaskStatus.SUCCESS.value else None,
            error_message=error_message if status == SubtaskStatus.FAILED.value else None,
            # 历史行也要带上代号 + 参数（失败时），否则「尝试历史」里只有冻结的文本，
            # 而它恰恰是最该能跟着语言走的那一份。
            error_code=error_code if status == SubtaskStatus.FAILED.value else None,
            error_params=error_params if status == SubtaskStatus.FAILED.value else None,
            # 入参快照与输入引用**成功失败都往历史行上写**（与运行方那侧「两条路径都带」
            # 同口径）：失败那次尝试吃的是什么，恰恰是最该查得到的。
            inputs=subtask.inputs or {},
            input_refs=input_refs,
            output_refs=output_refs,
            finished_at=now,
        )

        if status == SubtaskStatus.SUCCESS.value:
            # ⓪ **旧运行不填下游**（判据与 `_aggregate` 开头那道闸同一句：本行运行是不是
            #    当前运行）。`_fill_downstream` 写的是**这一整套**子任务的 `inputs` —— 旧运行
            #    那一套是冻结的历史，往里写等于事后改掉「当时这条下游到底拿什么跑的」。
            #    典型来源：迟到很久的旧凭证上报（`_require_report_identity` 明写「只是过期
            #    的报文不拒」，`_absorb` 放行、只收它自己那一行）。
            #    本行自己的 CAS 照做（上面那段）—— 被闸住的只有**跨行**的这两处写入
            #    （下游 inputs 与任务快照），任务快照那处由 `_aggregate` 自己挡。
            if ctx.attempt.attempt == ctx.task.current_attempt:
                await self._fill_downstream(ctx, subtask)
            else:
                logger.info(
                    "[TaskCenter] 旧运行不填下游 task=%s 本行运行=#%s 当前运行=#%s"
                    "（只收它自己那一行）",
                    ctx.task.id,
                    ctx.attempt.attempt,
                    ctx.task.current_attempt,
                )
        else:
            # 失败**不处置下游**（§6.3）：下游原地 hold —— 既不改状态、也不会被派活，
            # 一直 `pending`，由 `_aggregate` 把任务收成 `failed` 兜住。
            # 早先那版会按 `fail_policy` 把下游整片置失败 / 置跳过，已随 `fail_policy` 一起删掉。
            logger.info(
                "[TaskCenter] 节点 %s 失败，下游原地 hold（§6.3）", subtask.node_id
            )

        await self._aggregate(ctx)

    # ========================================================
    # 查询（§4.3 task.list / get / subtask_* / attempt_*）
    # ========================================================

    async def list_tasks(
        self,
        tenant_id: str,
        filters: Optional[Dict[str, Any]] = None,
        *,
        space_id: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> TaskList:
        """列任务（§4.3 `task.list`）。

        **空间筛选走顶层 `space_id`，不走 `filters`**：它跟别的筛选条件不是一回事 ——
        传了要**校验归属**（§4.0 点名了 `task.list`），而校验失败必须当场报错，
        不能静默筛出一个空列表。
        """
        tenant_id = require_tenant(tenant_id)
        filters = await self._normalize_task_filters(tenant_id, filters, space_id)
        offset = max(page - 1, 0) * page_size
        rows = await self._tasks.list_tasks(tenant_id, filters, offset, page_size)
        total = await self._tasks.count_tasks(tenant_id, filters)
        return TaskList(
            items=[TaskItemResponse(**row.to_dict()) for row in rows],
            page=page,
            page_size=page_size,
            total=total,
        )

    async def _normalize_task_filters(
        self,
        tenant_id: str,
        filters: Optional[Dict[str, Any]],
        space_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """收口 `task.list` 的筛选条件 —— **四道校验**（§4.3 `task.list` 卡片）。

        ① **`space_id` 只认顶层那个参数**：写在 `filters` 里一律拒（1001）——
           静默忽略会让调用方以为筛了、实际拿到全部空间的任务；
        ② **顶层 `space_id` 传了值 → 校验归属**：不能拿别的租户的空间 ID 当查询条件
           （§4.0 那条通用规则点名了 `task.list`，见该节「`space_id` 也校验归属」
           一段；实现与 `fire` / `trig.create` / `trig.list` / `def.create` 共用
           `space_guard.require_owned_space`）；
        ③ **`status` 传了值 → 必须是合法任务状态**，非法直接拒（1001）；
        ④ **`task_source` 传了值 → 必须 ∈ 协议层 `TASK_SOURCES`**（写死枚举，2026-10-13 加），
           非法同样直接拒（1001）—— 理由与 ③ 相同。

        **都只在「传了值」时才判**：不传就没有可校验的对象 —— 同 §4.0 那条
        「有传值才校验、没传就不校验」。

        ⚠️ **别指望仓库那层兜住**：`TaskRepository._filter_conditions` 是把白名单字段
        直接拼成等值条件，**没有这三道闸** —— 别的租户的 `space_id`、拼错的状态名，
        都只会得到一个**空列表**。调用方分不清「确实没有」和「我筛错了」，
        也看不出自己越了租户。
        """
        misplaced = (filters or {}).get("space_id")
        if misplaced:
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.task.space_id_misplaced",
                    params={"space_id": misplaced},
                    fallback=(
                        f"空间筛选请用顶层的 space_id 参数（收到的是 filters.space_id="
                        f"{misplaced}），别放在 filters 里"
                    ),
                ),
                details={"space_id": misplaced},
            )
        if not filters and not space_id:
            return filters
        filters = dict(filters or {})
        if space_id:
            await require_owned_space(self.session, tenant_id, space_id)
            # 校验过了才拼进等值条件（仓库那层只认 `filters`）
            filters["space_id"] = space_id
        if filters.get("status") is not None:
            # 复用管理端那条筛选规则（同一套合法值、同一句文案）。单值筛选取回来的
            # 归一化结果就是它本身，这里要的只是它「非法值当场拒」这一步。
            _normalize_status_filter([filters["status"]])
        if filters.get("task_source"):
            # 2026-10-13 加：第四道闸。来源是**写死枚举**，域外值一律拒（1001）——
            # 与 status 同一条口径：静默筛出空列表比报错难查得多，而拼错的来源码
            # 恰好是最像「确实没有」的那种。判据复用协议层那一份。
            #
            # ⚠️ **判据用真值、不用 `is not None`**（与上面的 `status` 刻意不同）：
            # 空串在**下游两处**本来就等于「没传」—— 网关是 `if task_source:`、
            # 仓储 `_filter_conditions` 是 `if value:`。这里用 `is not None` 的话，
            # 一个空串会撞出「发起任务必须给出任务来源」这句与筛选毫不相干的文案。
            _check_task_source(filters["task_source"])
        return filters

    async def get_task(self, task_id: str, tenant_id: Optional[str] = None) -> TaskItemResponse:
        tenant_id = require_tenant(tenant_id)
        task = await self._require_task(tenant_id, task_id)
        return TaskItemResponse(**task.to_dict())

    async def list_subtasks(
        self, task_id: str, attempt: Optional[int] = None, tenant_id: Optional[str] = None
    ) -> List[SubtaskItemResponse]:
        """列子任务；不给 `attempt` 默认列**最新一次运行**的（§4.3）。

        **回裸数组、不回对象**：`task.subtask_list` 在 §4.0「分页」的「不设分页」名单里
        （`runner.list` / `def.catalog_list` / `def.catalog_steps` 也在那个名单），§4.3
        它自己的「返回」写的正是「`data` = 子任务数组（不分页）」。包一层
        `{"items": [...]}` 会让照文档写的调用方取不到东西。
        """
        tenant_id = require_tenant(tenant_id)
        await self._require_task(tenant_id, task_id)
        if attempt is None:
            rows = await self._subtasks.list_current_by_task(tenant_id, task_id)
        else:
            row = await self._require_attempt(tenant_id, task_id, attempt)
            rows = await self._subtasks.list_by_attempt(tenant_id, row.id)
        return [SubtaskItemResponse(**row.to_dict()) for row in rows]

    # ========================================================
    # 管理端只读列表（§4.4）
    # ========================================================
    #
    # 给前端页面的两个接口**不复用上面的 `list_tasks` / `list_subtasks`**：那两个是
    # 内部契约（返回 `execution.py` 那套字段给全的 DTO，口径定稿在 §4.3），这里要的是
    # 「按页面挑字段 + 算耗时 + 各状态数量」。硬合并会让接入方跟着动。

    async def page_tasks(
        self,
        tenant_id: str,
        *,
        keyword: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        statuses: Optional[List[str]] = None,
        space_id: Optional[str] = None,
        task_source: Optional[str] = None,
        user_id: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> TaskConsolePage:
        """前端任务列表（§4.4）：关键字 / 建任务时间范围 / 状态多值 / 空间 / 任务来源。

        **各状态数量是单独一趟查询、且不带 `status` 条件**（§4.4 的口径）：它是页签
        角标，切页签时数字得稳住、只随其它筛选条件变 —— 所以不能拿 `total` 当它用。
        （`space_id` 属于「其它筛选条件」，所以三个查询**都要带**，角标才跟着它变。）

        ── 空间维度：**归属 + 权限**（2026-10-10）──

        `space_id` **不是必填**，两种情形走两条路：

        | 传了 `space_id` | 走哪条 |
        | --- | --- |
        | 有 | 先 `require_owned_space`（这个空间是不是本租户的），再
          `require_space_visible`（这个人有没有这个空间的角色）—— **任一不过就拒**：
          归属不过 1001、权限不够 **403**。过了才按它精确筛 |
        | 无 | **不是**「看全部」，而是收敛到 `visible_space_ids_for` 给出的
          **这个人有权限的空间集合**（`None` = 不过滤，只有租户/平台管理员与
          无用户身份的内部链路会拿到 `None`） |

        ⚠️ 「不传就不过滤」在**这里**是错的：任务列表是租户内跨空间的视图，不收敛就等于
        任何登录用户都能看到所有空间的任务。收敛必须落在 **SQL** 里（`IN (...)`），
        不能取一批再在 Python 里 `continue` —— 那会把分页额度吃光、页面上看着像「没数据」。

        ── `task_source`（2026-10-13 加，**非必填**）──

        按任务来源筛（∈ 协议层 `TASK_SOURCES`）。**非法值当场拒（1001）**，与 `status`
        同一条口径（`_normalize_status_filter`）：静默筛出空列表分不清「确实没有」和
        「筛错了」。它与 `space_id` 一样属于「其它筛选条件」，所以**三个查询都要带**
        —— 页签角标才跟着它变。
        """
        tenant_id = require_tenant(tenant_id)
        # ⚠️ **时间范围倒挂当场拒**（2026-10-13 补）：两个边界是**各自独立**拼进 WHERE 的
        # （`_console_conditions`），从不比较先后 —— 传反了（复制粘贴、或前端两个选择器
        # 各选各的）查询**恒为空、而且不报错**，用户会以为「这段时间确实没有任务」。
        # 属「不报错但结果不对」那一类，最难排查。口径与 `status` / `task_source` 的
        # 「非法值当场拒（1001）」一致：静默筛出空列表分不清「确实没有」和「筛错了」。
        if start_time is not None and end_time is not None and start_time > end_time:
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.param.invalid",
                    params={
                        "errors": f"start_time ({start_time}) 晚于 end_time ({end_time})"
                    },
                    fallback=(
                        f"参数没通过校验：start_time ({start_time}) 晚于 "
                        f"end_time ({end_time})"
                    ),
                ),
                details={"start_time": str(start_time), "end_time": str(end_time)},
            )
        statuses = _normalize_status_filter(statuses)
        if task_source is not None:
            # 只判「在不在册」。空串 / 空白当没传 —— `None` 也是「没传」。
            task_source = task_source.strip() or None
            if task_source is not None:
                _check_task_source(task_source)

        # ── 空间：归属 / 权限 / 可见范围，判完才拼进查询条件 ──
        allowed_space_ids: Optional[List[str]] = None
        if space_id:
            await require_owned_space(self.session, tenant_id, space_id)
            await require_space_visible(self.session, tenant_id, space_id, user_id)
        else:
            allowed_space_ids = await visible_space_ids_for(
                self.session, tenant_id, user_id
            )

        offset = max(page - 1, 0) * page_size
        common: Dict[str, Any] = {
            "keyword": keyword,
            "start_time": start_time,
            "end_time": end_time,
            "space_id": space_id,
            "allowed_space_ids": allowed_space_ids,
            "task_source": task_source,
        }
        rows = await self._tasks.page_console_tasks(
            tenant_id, **common, statuses=statuses, offset=offset, limit=page_size
        )
        total = await self._tasks.count_console_tasks(
            tenant_id, **common, statuses=statuses
        )
        raw_counts = await self._tasks.count_console_by_status(tenant_id, **common)
        return TaskConsolePage(
            items=[
                TaskConsoleItem(
                    **_localized_console_row(row),
                    duration_seconds=_duration_seconds(row.started_at, row.finished_at),
                )
                for row in rows
            ],
            page=page,
            page_size=page_size,
            total=total,
            # 四个键恒在（没数据的补 0）：前端角标不用自己兜底判空
            status_counts={
                status: raw_counts.get(status, 0) for status in _TASK_STATUS_VALUES
            },
        )

    async def list_subtasks_console(
        self,
        task_id: str,
        attempt: Optional[int] = None,
        tenant_id: Optional[str] = None,
        *,
        user_id: Optional[str] = None,
    ) -> List[SubtaskConsoleItem]:
        """前端子任务列表（§4.4）。

        归属校验、以及「不给 `attempt` 就取最新一次运行」的口径跟 `list_subtasks`
        逐字一致，只是**没调它**：那个回的是内部契约 DTO，时间已经序列化成 ISO 字符串，
        算不出耗时。所以这里重走一趟仓储拿 ORM 行，换个前端要的薄外壳。

        ⚠️ **两道闸与 `page_tasks` / `get_task_console` 一字不差**（2026-10-13 补）：
        `_require_task`（租户归属）+ `require_space_visible`（**这个人能不能看这个任务所在的
        空间**）。从前**只判租户** —— 同租户里对空间 S 无角色的用户，只要拿到 S 里某条任务的
        `id`，就能读到它的子任务明细（`inputs` / `outputs` / `error_message`），从旁边绕开
        列表那道可见空间收敛。这一批与两个写接口一起补齐（原先这里是「另记在 §4.4」的缺口）。

        没传 `user_id`（内部链路 / 测试 token）时守卫按既有口径放行 —— 与列表同一条口径。
        """
        tenant_id = require_tenant(tenant_id)
        task = await self._require_task(tenant_id, task_id)
        await require_space_visible(self.session, tenant_id, task.space_id, user_id)
        if attempt is None:
            rows = await self._subtasks.list_current_by_task(tenant_id, task_id)
        else:
            row = await self._require_attempt(tenant_id, task_id, attempt)
            rows = await self._subtasks.list_by_attempt(tenant_id, row.id)
        return [
            SubtaskConsoleItem(
                **_localized_console_row(row),
                duration_seconds=_duration_seconds(row.started_at, row.finished_at),
            )
            for row in rows
        ]

    async def get_task_console(
        self, tenant_id: str, task_id: str, *, user_id: Optional[str] = None
    ) -> TaskConsoleItem:
        """前端**任务详情**（§4.4）：**返回体与任务列表里的一行完全一致**
        （`TaskConsoleItem`，含 `duration_seconds`）。

        ⚠️ **两道闸与列表一字不差**，少一道这个口子就是旁路：

        - `_require_task` —— 租户归属（别的租户的 `task_id` 按「找不到」拒，1002）；
        - `require_space_visible` —— **这个人能不能看这个任务所在的空间**
          （列表那侧是靠 `visible_space_ids_for` 收敛的，这里没有「列表」可收敛，
          所以必须显式判一次，否则「猜一个 task_id 就能读到」正好绕过列表的收敛）。

        没传 `user_id`（内部链路 / 测试 token）时守卫按既有口径放行 —— 与列表同一条口径。
        """
        tenant_id = require_tenant(tenant_id)
        row = await self._require_task(tenant_id, task_id)
        await require_space_visible(self.session, tenant_id, row.space_id, user_id)
        return TaskConsoleItem(
            **_localized_console_row(row),
            duration_seconds=_duration_seconds(row.started_at, row.finished_at),
        )

    async def get_subtask_console(
        self,
        task_id: str,
        node_id: str,
        attempt: Optional[int] = None,
        tenant_id: Optional[str] = None,
        *,
        user_id: Optional[str] = None,
    ) -> SubtaskConsoleItem:
        """前端**子任务详情**（§4.4）：**返回体与子任务列表里的一行完全一致**
        （`SubtaskConsoleItem`，含 `duration_seconds`）。

        寻址与那两条子任务级写接口**同口径**：按 **`node_id`（节点名，如 `parse_1`）**，
        **不是**子任务行的主键 `id`（页面手上就只有节点名）。`attempt` 不传 = 当前那次运行
        （与 `task.subtask_get` 逐字一致：定位是「哪一次运行 + 哪个节点」）。

        ⚠️ **三道闸**（2026-10-13 补上中间那道空间闸）：`_require_task`（租户归属）+
        `require_space_visible`（**这个人能不能看这个任务所在的空间**）+ 运行定位。
        从前这里与它的列表一样**只判租户归属**，并自记为「另记在 §4.4」的缺口 ——
        **这一批与两个写接口一起补齐了**，四处（两个读 + 两个写）现在同一条口径。

        没传 `user_id`（内部链路 / 测试 token）时守卫按既有口径放行 —— 与列表同一条口径。
        """
        tenant_id = require_tenant(tenant_id)
        task = await self._require_task(tenant_id, task_id)
        await require_space_visible(self.session, tenant_id, task.space_id, user_id)
        task_attempt = await self._attempt_of(tenant_id, task, attempt)
        row = await self._subtasks.get_subtask(tenant_id, task_attempt.id, node_id)
        row = self._require_subtask_row(row, task_id, node_id)
        return SubtaskConsoleItem(
            **_localized_console_row(row),
            duration_seconds=_duration_seconds(row.started_at, row.finished_at),
        )

    async def get_subtask(
        self,
        task_id: str,
        node_id: str,
        attempt: Optional[int] = None,
        tenant_id: Optional[str] = None,
    ) -> SubtaskItemResponse:
        """取某一次运行里的某个**节点**的子任务（§4.3 `task.subtask_get`）。

        定位是「**哪一次运行 + 哪个 `node_id`**」：`node_id` 只在同一次运行内唯一，
        跨运行会重复出现（`retry_task` 之后新那套里同一个 `parse_1` 又在），所以必须
        带上 `attempt`。**不传 `attempt` 就是"当前那次运行"**。
        """
        tenant_id = require_tenant(tenant_id)
        task = await self._require_task(tenant_id, task_id)
        task_attempt = await self._attempt_of(tenant_id, task, attempt)
        row = await self._subtasks.get_subtask(tenant_id, task_attempt.id, node_id)
        return SubtaskItemResponse(
            **self._require_subtask_row(row, task_id, node_id).to_dict()
        )

    async def list_task_attempts(
        self,
        task_id: str,
        page: int = 1,
        page_size: int = 20,
        tenant_id: Optional[str] = None,
    ) -> TaskAttemptList:
        """分页列某任务的运行历史（每次运行一条，只追加；查历史用它）。

        分页按 §4.0 的统一约定：`page` 从 1 起、`page_size` 不设上限，返回里带 `total`，
        **不做静默截断**。
        """
        tenant_id = require_tenant(tenant_id)
        await self._require_task(tenant_id, task_id)
        rows = await self._attempts.list_by_task(
            tenant_id, task_id, max(page - 1, 0) * page_size, page_size
        )
        total = await self._attempts.count_by_task(tenant_id, task_id)
        return TaskAttemptList(
            items=[TaskAttemptItem(**row.to_dict()) for row in rows],
            page=page,
            page_size=page_size,
            total=total,
        )

    async def list_subtask_attempts(
        self,
        task_id: str,
        node_id: str,
        page: int = 1,
        page_size: int = 20,
        attempt: Optional[int] = None,
        tenant_id: Optional[str] = None,
    ) -> SubtaskAttemptList:
        """分页列某节点子任务的尝试历史（每次尝试一条，只追加；查历史用它，§4.3）。

        跟 `task.subtask_get` 同一套定位：**`attempt` + `task_id` + `node_id`**，
        不给 `attempt` 就是"当前那次运行"（`node_id` 跨运行会重复，所以运行号不能省）。
        「这个节点试了几次」看的正是这里 —— 别拿 `subtasks.current_attempt` 当历史，
        那只是个指针。

        分页按 §4.0 的统一约定，跟同组的 `task.attempt_list` 一致。
        """
        tenant_id = require_tenant(tenant_id)
        task = await self._require_task(tenant_id, task_id)
        task_attempt = await self._attempt_of(tenant_id, task, attempt)
        subtask = await self._subtasks.get_subtask(tenant_id, task_attempt.id, node_id)
        self._require_subtask_row(subtask, task_id, node_id)
        rows = await self._subtask_attempts.list_by_subtask(
            tenant_id, subtask.id, max(page - 1, 0) * page_size, page_size
        )
        total = await self._subtask_attempts.count_by_subtask(tenant_id, subtask.id)
        return SubtaskAttemptList(
            items=[SubtaskAttemptItem(**row.to_dict()) for row in rows],
            page=page,
            page_size=page_size,
            total=total,
        )

    # ========================================================
    # 停止 / 重跑 / 删除（task.stop、§4.3 task.retry / task.subtask_retry /
    # task.subtask_retry_batch / task.delete）
    # ========================================================

    async def _commit_locked_write(self) -> None:
        """**在放锁之前**把改动落库（§7.2 的纪律：锁的租期必须覆盖到提交为止）。

        `task_lock` 一旦放掉，另一个副本就能读到还没提交的旧像。它按旧 `state_version`
        做的 CAS 大多会命中 0 行（乐观锁兜住了绝大多数），但「删任务时整批假删子任务」
        这条路径的旧像足以让派活把一条**已删任务的子任务**抢成 `running`、真的派给运行方
        去跑 —— 而任务已经删了，没人收它的结果。

        **六个**「用户主动写」的 action（`retry_task` / `retry_subtask` / `retry_subtasks` /
        `stop_task` / `stop_subtask` / `delete_task`）都要在退出 `task_lock` 之前调它。
        **这不是 `_execute` 能代劳的**：`_execute` 的 `get_db_session()` 在 action 返回
        **之后**才提交，那时锁早放掉了。⚠️ **加新的写 action 时这里和 `task_lock.py` 顶上
        那份清单要一起改** —— 漏了一个不会报错，只会让那条路径留一个「已改库但还没提交、
        锁已经放了」的窗口。
        """
        await self.session.commit()

    async def retry_task(
        self,
        task_id: str,
        tenant_id: Optional[str] = None,
        *,
        user_id: Optional[str] = None,
    ) -> TaskItemResponse:
        """重跑整个任务（§4.3 `task.retry`）—— 先拿任务锁，再走 `_do_retry_task`。

        **调用方主动发起的写操作**：锁拿不到绝不跳过（§7.2 ①），`patient=True` **有心等**（有界窗口 ≈3.15 秒，**不是等到底**），
        仍拿不到就抛可重试码让调用方重发。

        ⚠️ **空间可见性**（2026-10-13 补，**只由前端面 `task.console_task_retry` 传**）：
        `user_id` 为空时守卫放行 —— 所以内部 / 运维那条 `task.retry`（不带用户身份）**行为一字不变**；
        只有前端面会真判「这个人能不能操作这个任务所在的空间」，补上「同租户里对空间 S 无角色
        的用户拿到 `id` 就能**重跑整条任务**」这个缺口。与 `page_tasks` / `get_task_console` 同一道闸。
        ⚠️ 这一判在**拿锁之前**（锁外）。
        """
        tenant_id = require_tenant(tenant_id)
        task = await self._require_task(tenant_id, task_id)
        await require_space_visible(self.session, tenant_id, task.space_id, user_id)
        async with task_lock(task_id, patient=True):
            result = await self._do_retry_task(tenant_id, task_id)
            await self._commit_locked_write()
            return result

    async def _do_retry_task(self, tenant_id: str, task_id: str) -> TaskItemResponse:
        """`retry_task` 的锁内部分：插一条新运行、为它生成一套全新子任务、快照重置。

        **只对已终态（`completed` / `failed` / `stopped`）的任务开放**（`pending` / `running` 得先等它
        跑完）。五步（文档 §4.3 的顺序）：① 新插一条 `task_attempts`（`attempt` = 原值 + 1、
        `pending`）；② 把 `tasks` 快照重置（`current_attempt` 指向新那条、`status` 回
        `pending`、清 `error_message` / `finished_at`，`started_at` 不动）；③ 为这次运行
        **新建一整套 `subtasks`**（挂新的 `task_attempt_id`，每条 `pending`、
        `current_attempt=1`）；④ 每个新子任务再插一条 `subtask_attempts`（`attempt=1`、
        `pending`）；⑤ 复用 `tasks.inputs`、沿用同一个 `definition_version_no`。

        **旧的运行 / 旧的子任务 / 旧的尝试历史全部原样保留**（不改成新的、也不删）。
        """
        task = await self._require_task(tenant_id, task_id)
        self._require_terminal(task, "retry_not_terminal")

        # **这里从前要检查「定义 / 版本还在吗」**（`def.delete` 假删之后取不到就拒绝重跑）。
        # 现在不需要了：编排快照就钉在任务行上（`definition_config`），重跑用的是它。
        # 那条检查等于说「管理员删了个定义，这条任务就再也重跑不了」—— 正是冗余快照要治的事。
        config = task.definition_config or {}

        new_attempt_no = await self._attempts.next_attempt_no(task.id)
        # 先把快照 CAS 到「待执行、指向新运行」：抢不到说明别的副本刚重跑过，本次作废
        # （连点 / 并发下不会插出两套运行；入库顺序与文档 §4.3 的 ①② 相反，但同在一个
        #  事务里，外面看不到中间态，效果等价且不必依赖唯一索引报错来兜）。
        reset = await self._tasks.reset_for_task_retry(
            tenant_id, task.id, task.state_version, new_attempt_no
        )
        if not reset:
            raise ResourceConflictError(
                message=translate(
                    "err.task_center.task.retry_race",
                    params={"task_id": task_id},
                    fallback=f"任务 {task_id} 的快照刚被别处改过，重跑没生效",
                ),
                details={"task_id": task_id},
            )
        _apply_local(
            task,
            current_attempt=new_attempt_no,
            status=TaskStatus.PENDING.value,
            error_message=None,
            error_code=None,
            error_params=None,
            finished_at=None,
        )

        now = datetime.now()
        attempt = await self._attempts.create(
            tenant_id=tenant_id,
            space_id=task.space_id,
            task_id=task.id,
            attempt=new_attempt_no,
            status=AttemptStatus.PENDING.value,
            created_at=now,
        )

        ctx = await self._build_context(
            tenant_id=tenant_id,
            task=task,
            attempt=attempt,
            config=config,
        )
        for subtask in ctx.subtasks:
            if ctx.deps_satisfied(subtask):
                await self._fill_inputs(ctx, subtask)
        logger.info("[TaskCenter] 重跑任务 task=%s attempt=%s", task.id, new_attempt_no)
        return TaskItemResponse(**task.to_dict())

    async def retry_subtask(
        self,
        task_id: str,
        node_id: str,
        tenant_id: Optional[str] = None,
        *,
        space_id: str,
        operator_id: Optional[str] = None,
    ) -> SubtaskItemResponse:
        """重跑单个子任务（§4.3 `task.subtask_retry`）—— 先拿任务锁，再走 `_do_retry_subtask`。

        **这是「调用方主动发起的写操作」，锁拿不到绝不能跳过**（§7.2 ①）：跳过 = 页面显示
        重试成功、实际什么都没发生。所以要 `patient=True` 拉长窗口等；仍拿不到就抛
        `TaskLockBusy`（可重试码），让调用方自己重发。

        **`space_id` 必传**（关键字参数，不带默认值）：它是「这条任务确实在这个空间里」的
        核对凭据，不符就按「找不到」拒（1002，见 `_require_space_match`）。任务中心里凡是
        「租户 + 空间」两级定位的路径都得把空间带上 —— 只按 `task_id` 找的话，同一个租户下
        传错空间也能改到别的空间的任务上。

        **`operator_id` 是发起这次重试的那个人**（`user_id`，落 `tasks.updated_by`）：
        **纯记账**，不校验、不参与任何判定；拿不到就传 `None`（空值是合法状态，不是漏记）。
        跟 `created_by` 不同，`updated_by` **会被覆盖**；`None` 时**不写这一列**（保持原值），
        不会把它清空。

        ── **连带下游**（2026-10-11 用户定的口径，2026-10-15 起可配）──

        「如果点了某一子任务重跑，那就依赖他的都重跑」。所以这一步**不再是「只动它自己」**：
        展开下游闭包（`_downstream_chain`），有下游就转走 `_do_retry_subtasks` 那条**批量**
        路径（顺序由它写进依赖），没有下游才走原来的单步路径。

        ⚠️ **展开与否由编排里起点那个节点的 `downstream_on_retry` 决定**（缺省 `true`
        = 上面那条口径，也是库里几条预置编排今天的行为）。为 `false` 时名单只剩它自己，
        于是走单步路径 —— 这是留给「下游与上游没有产出依赖」那种编排的口子，
        见下面「为什么必须连带」那段里写明的代价。

        ⚠️ **展开这件事在 `_do_retry_subtask` 里做**（不是在这个方法里）：那一步手上已经有
        `by_key` —— 它为「上游必须成功」那条前提本来就要查一次全量子任务，往外挪等于
        **白多查一次**；而且「任务终态」「这一步终态」两道前提**先**把大部分乱点拒掉了，
        为它们白展开一遍依赖图不划算（单测钉住了这一点：任务还在跑时被拒的那条路上，
        子任务仓储只该被调 `get_subtask` 一次）。

        **为什么必须连带**：下游的入参是从上游产出里按 `bind_inputs` 填的。上游重跑出新产出、
        下游还用着旧产出的话，整条链就是「上游是新的、下游是旧的」，而任务中心**照样收口**
        （下游那一行还是上一次的 `success`，依赖一直满足、不会再被派活）。知识库那条链上
        后果最重：解析重推了切片、本体却还是旧切片抽的，接口上却显示「任务完成」。
        以前只有「整条 `retry_task`」能刷新下游，代价是连上游也一起重跑 —— 而用户要的
        恰恰是「只重跑这一步、但它下面的都跟着刷新」。

        ⚠️ **把 `downstream_on_retry` 置 `false` 就是主动接受上面那个后果**：上游换成新的、
        下游留着旧的，而任务照样收口成成功。所以它只适合**下游不依赖上游产出**的编排
        （两步各跑各的，或下游只用自己的入参）。这条代价写在字段那一侧，别只在这里记着。

        ── **两道前提**（用户 2026-10-11 一并定的）──

        · **上游必须成功**：起点那步的 `depends_on` 里每个节点都得是 `success`
          （原有前提③，没动）。
        · **下游必须没有运行中的**：展开出来的下游**全都进了批量名单**，而批量路径会
          **逐行**检查「已终态」—— 任何一个下游还停在 `pending` / `running`，这次重跑就
          被整体拒掉，错误信息里点名是哪一个下游。
          这条是刻意的：下游正在跑，说明它**正拿着上一轮的输入在干活**；这时候把上游
          重做一遍，两边就对不上了（而它自己那份还要不要、由谁收尾，没人说得清）。
        """
        tenant_id = require_tenant(tenant_id)
        async with task_lock(task_id, patient=True):
            result = await self._do_retry_subtask(
                tenant_id, task_id, node_id, space_id, operator_id
            )
            await self._commit_locked_write()
            return result

    @staticmethod
    def _downstream_chain(
        by_key: Dict[str, Any],
        node_id: str,
        *,
        steps: Dict[str, Dict[str, Any]],
        switch: str,
    ) -> List[str]:
        """点某一步时要一起处理的名单：**起点 + 依赖它的下游**（拓扑序），逐边看开关。

        `by_key` 是「这次运行的全部子任务」（`node_id` → 子任务行）—— 由调用方给现成的，
        这里不查库（调用方本来就要为「上游必须成功」那条前提查一次）。
        `steps` 是编排里的节点表（`_steps_by_node(task.definition_config)` 或 `ctx.steps`），
        `switch` 是要读的那个开关名：**重跑**读 `downstream_on_retry`、
        **停止**读 `downstream_on_stop`（2026-10-15 加，见设计文档 §6 的编排 schema）。

        ## 顺序不是装饰

        返回的名字直接喂 `_do_retry_subtasks`，而那个方法**按名单顺序把「先后」写进依赖**
        （第 i 个等第 i-1 个成功）—— 顺序写反会被它当环拒掉（`_detect_cycles`）。
        这里用「依赖已被选入的下游逐层展开」，天然就是拓扑序（起点在前）。

        ## 判据是「**任一**上游被选中**、且它说了跟**」而不是「所有上游都被选中」

        多上游的节点（`C.depends_on = [A, B]`）在只重跑 `A` 时**也要跟着重跑** ——
        用户的口径是「依赖他的都重跑」，`C` 依赖 `A` 就成立。要求「所有上游都在名单里」
        会让这类节点被漏掉，而那正是「上游新、下游旧」的来源。

        **开关加在这一层**：一条边（U → D）跟不跟，只看 **U 自己**那个字段。所以 `C` 要
        纳入，至少得有一条入边的上游在名单里、并且那个上游说了「跟」。B 说不跟、A 说跟时
        C 仍然纳入 —— 这不是打架，是「任一」那条口径的自然延伸。真正说不清的是**反过来**
        那种配置（点 A 时从 A 看该跟、从 B 看不该跟），它由发版校验按「同一节点的所有上游
        必须同值」直接拦掉（见 `flow_validator`），到不了这里。

        ## 只有起点时返回 `[node_id]`

        单步路径原样保留（那条路控制台与 SDK 都在走、被一整组单测钉着）。节点压根不存在
        时也返回 `[node_id]`，让后面的「找不到这个节点」去拒（错误信息不在这里造）。
        """
        deps = {key: set(row.depends_on or []) for key, row in by_key.items()}
        if node_id not in deps:
            return [node_id]

        # ── ① 集合：**顺着开关为 true 的出边**展开到不动点 ──
        #
        # 缺省（编排里没写这个字段）= `true`：库里那几条预置编排一个字都没改，行为必须与
        # 从前逐字相同。取不到节点对象时同样按 `true` 走 —— 那是保守兜底（`steps` 与
        # `by_key` 来自同一份编排快照 / 同一次运行，正常都在），不是一条正常路径。
        def _enabled(key: str) -> bool:
            step = steps.get(key) or {}
            return bool(step.get(switch, True))

        wanted = {node_id}
        changed = True
        while changed:
            changed = False
            for candidate in sorted(deps):
                if candidate in wanted:
                    continue
                if any(dep in wanted and _enabled(dep) for dep in deps[candidate]):
                    wanted.add(candidate)
                    changed = True

        # ── ② 顺序：**拓扑分层** —— 每轮只收「**所有**上游都已进名单」的那些 ──
        #
        # ⚠️ **集合与顺序的判据刻意不同，别合并成一趟**：
        #   集合要「**任一**上游被选中」（用户口径：依赖它的都重跑）；
        #   顺序要「**全部**上游已入」（真拓扑序）。
        #   一趟贪心（边遍历边增长 `picked`）会让**名字排序靠前**的多父节点抢在它的
        #   某个父节点前面被收进来 —— 返回的顺序就不是拓扑序，而下游
        #   `_do_retry_subtasks` 会**按这个顺序把「先后」写进依赖**（第 i 个等第 i-1 个
        #   成功），顺序一错就**必然成环** → 一次合法的「重跑某步」被误报成
        #   1001「名单连成了环」，而名单是**系统自己排的**、用户根本没给。
        order = [node_id]
        rest = wanted - {node_id}
        while rest:
            layer = sorted(n for n in rest if deps[n] <= set(order))
            if not layer:
                # 剩下的互相依赖（数据里本来就有环）—— 原样收进来，交给下游的
                # `_detect_cycles` 去拒（那边能指出是哪些节点连成了环，信息更准）。
                order.extend(sorted(rest))
                break
            order.extend(layer)
            rest -= set(layer)
        return order

    def _stop_cascade_targets(self, ctx: RunContext, node_id: str) -> List[Subtask]:
        """停某一步时要一起置停的名单：**起点 + 按 `downstream_on_stop` 展开的下游**。

        展开走的是与重跑同一把尺子（`_downstream_chain`），只是读的开关名不同 ——
        同一张依赖图上「谁跟着谁」只该有一套说法，两处各写一遍迟早对不上。

        返回**行对象**而不是 `node_id`：调用方要按状态筛（只挑 `pending`），还要逐行 CAS。
        `ctx.by_key` 里取不到的名字直接跳过 —— 展开只在**这次运行已有的子任务**之间发生，
        编排里有、这次运行里却没有的节点（理论上不该出现）不该让一次停止整单失败。
        """
        chain = self._downstream_chain(
            ctx.by_key,
            node_id,
            steps=ctx.steps,
            switch="downstream_on_stop",
        )
        return [ctx.by_key[key] for key in chain if key in ctx.by_key]

    async def retry_subtasks(
        self,
        task_id: str,
        node_ids: List[str],
        tenant_id: Optional[str] = None,
        *,
        space_id: str,
        operator_id: Optional[str] = None,
    ) -> List[SubtaskItemResponse]:
        """重跑某几步（§4.3 `task.subtask_retry_batch`，运行方经 §4.2 ④ 调用）。

        **进门规矩与单步逐条相同**：锁拿不到绝不跳过（`patient=True`，跳过 = 页面显示
        重试成功、实际什么都没发生）、`space_id` 必传并核对归属（不符按「找不到」拒）、
        `operator_id` 纯记账落 `tasks.updated_by`（有人才写）。差别只在两处：一次动几步，
        以及**这几步之间的先后**（见 `_do_retry_subtasks`）。

        **为什么要有它**：有些「重来一次」天生是两步 —— 上游重做产出、下游跟着重做。
        拆成两次调用的话，两次之间是一段没人兜的窗口：第一次成了、第二次没成，就留下
        「上游重跑了、下游没重跑」的半成品，而调用方那侧看起来只是一次普通失败。一批
        提交（一个事务）把这段窗口消掉。

        **返回**：每一步重置后的快照（内部调用方要看）；对外回包的 `data` 仍是空
        （§4.2 ④ 与单步同一个口径：成不成由 `success` / `code` 讲完）。
        """
        tenant_id = require_tenant(tenant_id)
        async with task_lock(task_id, patient=True):
            result = await self._do_retry_subtasks(
                tenant_id, task_id, node_ids, space_id, operator_id
            )
            await self._commit_locked_write()
            return result

    async def _do_retry_subtask(
        self,
        tenant_id: str,
        task_id: str,
        node_id: str,
        space_id: str,
        operator_id: Optional[str] = None,
    ) -> SubtaskItemResponse:
        """`retry_subtask` 的锁内部分。

        **进门前两道**（都排在三条前提之前）：

        - **空间核对**：`task.space_id` 与传进来的 `space_id` 不符 → 按「找不到」拒
          （1002，见 `_require_space_match`）；
        - **幂等档**：任务**不是**终态、而这一步已经是「重试过的样子」（`current_attempt > 1`
          且状态是 `pending` / `running`）→ **当成功返回**，一个字都不改
          （见 `_retry_already_in_flight`）。

        **前提三条**：① **任务已到终态**（`completed` / `failed` / `stopped`）——任务跑着的时候一律
        不许重试子任务（**有意为之**：运行期靠「这批子任务的状态」判断能不能收尾，中途动
        其中一个会把收尾判定搅乱）；② **那个子任务也已终态**（`success` / `failed` /
        `timeout` / `stopped`）—— `pending` / `running` 还没结束，没什么可重试的；③ **它的上游全都
        `success`**（`depends_on` 里每个节点都 `success`）—— 上游没成功时这步要的输入
        根本拿不到，重试也是白跑，所以**直接拒绝**，让调用方先去解决上游（§4.3）。

        **`success` 也允许重试**，那是有意保留的：「重跑这一步」。新的 `outputs` 会
        **覆盖**旧的；**下游由调用层连带重跑** —— `retry_subtask` 会先展开
        `_downstream_chain`，有下游就走 `_do_retry_subtasks` 的批量路径
        （2026-10-11 用户口径：点了某一步重跑，依赖它的都重跑）。

        ⚠️ **本方法自己仍然只动一个节点**：连带那部分在它外面。想确认「下游也被重跑了」
        要看 `subtasks` 里下游行的 `current_attempt`，别从这里的重置范围去推。

        第③条拦在这里、而不是在派活侧给重试开个「不看依赖」的口子：那样会让派活侧的
        「永远不派」和收尾侧的「一直等它」同时成立，任务就永远停在 `running`。

        **四步，整体一个事务**：① 把任务（连同它当前那次运行）拉回「跑着」，清
        `finished_at` 与 `error_message`；② `subtasks` 行重置成最新（`current_attempt`+1、
        状态回 `pending`、清 `outputs` / `output_refs` / `error_message` / `execution_id`
        / `started_at` / `finished_at`——**旧的产出引用同产出正文一起清**，否则一个还没跑的
        新尝试上会挂着上一轮的「查看产物」入口；旧凭证也不能再用）；
        ③ **按上游现在的产出把这一步的 `inputs` 重算一遍**；④ 往 `subtask_attempts`
        插一条新历史（排队态）。

        **第③步为什么不能省**：这一步上次跑用的入参是从**上一轮**上游产出算出来的，而
        它的上游多半刚被重跑过（用户就是嫌上一轮的产出才重跑的）。不重算就等于是拿旧
        入参重跑一遍，结果与上一轮完全相同、重试失去意义。`inputs` **不单独清空**——
        直接覆盖，避免留下「清空了但没人填」的中间态（见代码里那段说明）。

        之后按正常派活流程重新派出去（`claim_pending` 仍是唯一进「跑着」的路）。
        旧尝试的记录原样保留。**它不调运行方** —— 重试就是重新跑，不负责清运行方侧数据。

        ── 幂等档**必须排在前提①之前**，理由见 `_retry_already_in_flight` ──

        上一次重试若真的落库了，任务就已经被它拉回 `running` —— 正是前提①要拒的那个状态。
        把幂等档放在前提①**之后**，等于「撞上重发必定收到 1003 拒绝」（`_require_terminal`
        抛的是 `ResourceConflictError`），那道档就永远走不到。
        所以顺序是：**空间核对 → 幂等档 → 前提三条 → 四步**。

        **`operator_id` 写进 `tasks.updated_by`**（`None` 就不写）——它挂在第①步那条
        `reset_for_subtask_retry` 的 UPDATE 上（见那个仓储方法的说明），不另开一次写。
        """
        task = await self._require_task(tenant_id, task_id)
        self._require_space_match(task, space_id)

        # 幂等档：上一次其实重试成了、只是响应没回来 → 按成功收工、一个字都不改。
        # **必须判在 `_require_terminal` 之前**（见 docstring）。
        if task.status not in TASK_TERMINAL_STATUSES:
            already = await self._retry_already_in_flight(tenant_id, task, node_id)
            if already is not None:
                return already

        self._require_terminal(task, "subtask_retry_task_not_terminal")

        task_attempt = await self._attempt_of(tenant_id, task, None)
        subtask = await self._subtasks.get_subtask(tenant_id, task_attempt.id, node_id)
        self._require_subtask_row(subtask, task_id, node_id)
        if subtask.status not in SUBTASK_TERMINAL_STATUSES:
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.subtask.retry_status_invalid",
                    params={"node_id": node_id, "status": subtask.status},
                    fallback=(
                        f"节点 {node_id} 当前是 {subtask.status}，"
                        "只有已结束（success/failed/timeout/stopped）的能重试"
                    ),
                ),
                details={"task_id": task_id, "node_id": node_id, "status": subtask.status},
            )

        # ③ 上游必须都已经 `success`（§4.3 第③条前提）：这步的入参是从上游产出里
        #    按 `bind_inputs` 填的，上游没成功就压根填不出来，重试也是白跑。
        by_key = {
            node.node_id: node
            for node in await self._subtasks.list_by_attempt(tenant_id, task_attempt.id)
        }

        # ── 连带下游（2026-10-11 用户口径）：点了某一步重跑，依赖它的都重跑 ──
        # 位置刻意：① 排在「任务终态」「这一步终态」两道前提**之后** —— 那两道先把大部分
        # 乱点拒掉，不该为它们白展开一遍依赖图；② `by_key` 到这里是现成的（第③条前提本来
        # 就要它），往外挪会白多查一次全量子任务。
        # 有下游 → 转批量路径：它会把「先后」写进依赖，并**逐行**检查名单里每一步都是终态
        # —— 这正好落实用户那条「上下游都是终态才能重试」（下游也在名单里，谁在跑就拒谁）；
        # 没有下游 → 原样走下面的单步路径。
        # ⚠️ **编排的 `downstream_on_retry` 能把这层展开掐掉**（2026-10-15 加）：起点那个
        #    节点说 false 时名单只剩它自己，于是自然走回单步路径 —— 那条路本来就在，
        #    这里不需要为「不带下游」另写一套。开关的取值从任务行那份编排快照里读
        #    （建任务时抄的），不去反查定义版本。
        chain = self._downstream_chain(
            by_key,
            node_id,
            steps=_steps_by_node(task.definition_config or {}),
            switch="downstream_on_retry",
        )
        if len(chain) > 1:
            results = await self._do_retry_subtasks(
                tenant_id, task_id, chain, space_id, operator_id
            )
            # 回给调用方的仍是**起点那一步**的快照（单步接口的返回类型不变）。
            return next((row for row in results if row.node_id == node_id), results[0])

        blocked = [
            dep
            for dep in subtask.depends_on or []
            if by_key.get(dep) is None
            or by_key[dep].status not in SUBTASK_SATISFIED_STATUSES
        ]
        if blocked:
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.subtask.retry_upstream_unmet",
                    params={"node_id": node_id, "upstream": "、".join(blocked)},
                    fallback=(
                        f"节点 {node_id} 的上游还没成功，重试跑不起来；"
                        "请先把上游重试成功，或整条任务重跑"
                    ),
                ),
                details={
                    "task_id": task_id,
                    "node_id": node_id,
                    "unsatisfied_upstream": blocked,
                },
            )

        # ① 任务 + 它当前那次运行一起拉回「跑着」：重试意味着「又有活要跑了」，
        #    两边都不该停在终态（运行那一行不拉回来，重试完就再也收不了口）。
        #    `operator_id` 一并带上（有人才写 `updated_by`，见仓储那处的说明）。
        reopened = await self._tasks.reset_for_subtask_retry(
            tenant_id, task.id, task.state_version, operator_id
        )
        if not reopened:
            raise ResourceConflictError(
                message=translate(
                    "err.task_center.task.retry_race",
                    params={"task_id": task_id},
                    fallback=f"任务 {task_id} 的快照刚被别处改过，重试没生效",
                ),
                details={"task_id": task_id},
            )
        _apply_local(
            task,
            status=TaskStatus.RUNNING.value,
            # ⚠️ 与 `reset_for_subtask_retry` 一处都不能漏：内存这份跟库里那份必须一起清，
            # 否则同一个事务里后半段读到的还是上一轮的失败原因（§4.3 卡片 / A.9 I4）。
            # 失败原因是**一组三个**（message / code / params），清就清一组。
            error_message=None,
            error_code=None,
            error_params=None,
            finished_at=None,
        )
        await self._attempts.reopen_for_subtask_retry(tenant_id, task_attempt.id)

        # ② `subtasks` 行重置回排队（`current_attempt`+1、清产出与旧凭证）。
        new_attempt = subtask.current_attempt + 1
        reset = await self._subtasks.reset_for_retry(
            tenant_id, subtask.id, subtask.state_version
        )
        if not reset:
            # **CAS 命中 0 行 = 并发冲突（1003），不是参数错（1001）** —— 与同一个函数
            # 上面那处 `reset_for_subtask_retry` 落空（`task.retry_race`）保持同一档，
            # 也是文档反复点名的口径（A.11 `delete_raced`、A.12 `version_insert_race`、
            # `trigger.update_race` 全归 1003）。调用方改任何参数都没用：是别人抢先改了
            # 同一行。两者都在 `TASK_CENTER_REJECTED_CODES` 里，重发行为不变，
            # 差别在排查方向与 HTTP 状态。
            raise ResourceConflictError(
                message=translate(
                    "err.task_center.subtask.retry_conflict",
                    params={"node_id": node_id},
                    fallback=f"节点 {node_id} 的状态刚被别处改过，重试没生效",
                ),
                details={"task_id": task_id, "node_id": node_id},
            )
        _apply_local(
            subtask,
            status=SubtaskStatus.PENDING.value,
            current_attempt=new_attempt,
            execution_id=None,
            outputs=None,
            error_message=None,
            error_code=None,
            error_params=None,
            started_at=None,
            finished_at=None,
        )

        # ③ 入参按**上游现在的产出**重算一遍（§6.2）。
        #
        #    **不清空、直接重算**：`_fill_inputs` 本来就是「按上游产出 + task.inputs 现算」，
        #    算出来直接覆盖这一列 —— 比「先清空、再等谁来填」少一个中间态（清空之后要是
        #    没人填，这一步就带着空入参跑出去了，那比重算失败严重得多）。
        #
        #    **为什么重试时必须重算**：这个节点可能是在**上一轮上游产出**上跑过一次的，
        #    而它的上游多半刚被重跑过（用户就是嫌上一轮的产出才重跑的，§4.3）。不重算的话
        #    `subtasks.inputs` 里躺的还是上一轮那份，重试出来的结果跟上一轮一模一样。
        #    重算之后：上游没动 → 算出来跟原来一样（等于没改）；上游动过 → 拿到新的。
        #    两种情形都对，不需要调用方分辨。
        #
        #    ⚠️ 这里说的是**入参重算**，跟「连带重跑下游」是两回事：前者管「这一步拿到
        #    的输入是不是新的」，后者管「下游要不要跟着重跑一遍」。连带由调用层做
        #    （`retry_subtask` 先展开下游、再走本方法或批量路径，见那里的 docstring）。
        ctx = await self._load_context(tenant_id, task_id, task_attempt.id)
        if ctx is None:
            # 够不着：这条路径全程持有任务锁，`task.delete`（也拿同一把锁）进不来，
            # 所以任务行与运行行必然都还在。真到这儿只可能是别处把运行行假删了 ——
            # 记一笔、照常把重试做完（入参留在原样，不比今天更差）。
            logger.warning(
                "[TaskCenter] 重试子任务时凑不齐上下文，跳过入参重算：task=%s attempt=%s node=%s",
                task_id,
                task_attempt.id,
                node_id,
            )
        else:
            await self._fill_inputs(ctx, subtask)

        # ④ 新尝试的排队历史（`created_at` 从此存在，派活排队排序要用，§6.3）。
        await self._open_attempt_row(
            tenant_id,
            task.space_id,
            task_id,
            subtask,
            created_at=datetime.now(),
        )
        return SubtaskItemResponse(**subtask.to_dict())

    async def _do_retry_subtasks(
        self,
        tenant_id: str,
        task_id: str,
        node_ids: List[str],
        space_id: str,
        operator_id: Optional[str] = None,
    ) -> List[SubtaskItemResponse]:
        """`retry_subtasks` 的锁内部分。

        **与 `_do_retry_subtask`（单步）刻意不合并**：那条路是控制台与 SDK 都在走的、
        已被一整组单测钉住的路径；合并等于让两条路各自承担对方的分支（单步要在循环里
        多判一次长度、批量要在单元素时假装没有顺序）。这里与它**逐条对齐**的只有进门
        两道与三条前提，对照着看即可。

        ## 这一批的「先后」

        调用方给的 `node_ids` **顺序就是执行顺序**：第 i 个节点要等第 i-1 个成功之后才被
        派活。做法是**把这条先后写进依赖**（把前一个追加进后一个的 `depends_on`，与「重置」
        同一条 UPDATE）——「依赖」本来就是「我得等它们 `success` 才能跑」的那一列，
        派活、收尾、超时三处都照着它走，所以**调度侧一个字都不用改**。

        ⚠️ **为什么不能「一起放回队列」**：编排里的依赖是**直连**的（三步链写作
        `parse_1 → ingest_1 → compile_1`，`compile_1.depends_on` 只有 `ingest_1`），派活的
        「能不能跑」也只认直接上游。把 `parse_1` 与 `compile_1` 一起重置，`compile_1`
        的直接上游 `ingest_1` 还是 `success` → 它**当场就能跑**，跟正在重做产出的 `parse_1`
        同时跑。这就是这个接口非要有「顺序」的原因，也是它与单步重试最大的不同。

        ⚠️ **顺序跟编排方向冲突（会成环）直接拒**（1001）：把「前一个」写进「后一个」的
        依赖里之前先用 `_detect_cycles` 查一遍全图（发版校验 N1 用的同一份实现）——
        反向给（`compile_1` 在前、`parse_1` 在后）会让两行互相等、永远派不出去。宁可在
        门口拒掉，不留这种行。

        ## 前提（与单步逐条对齐，只是对**每一步**各做一遍）

        ① **任务已到终态**（`completed` / `failed` / `stopped`）；② **每一步都已终态**
        （`success` / `failed` / `timeout` / `stopped`）；③ **每一步「批外」的上游都 `success`**。

        ③ **只查批外**：批内的上游正要跟着重跑，它们之间的先后由上面那条顺序兜住 ——
        这时候要求它们「现在就是 `success`」反而是错的（它们就是被嫌了才要重跑的）。
        批外的不查不行：上游没成功时这一步的入参根本填不出来，重试也是白跑。

        批量版**没有**「任务还在跑的时候逐条吸收」这回事 —— 一批要么全重置、要么一个字
        不动（一个事务），所以幂等档只剩两档：**整批都已排上队** → 回成功；**不止这些**
        （含「已经重跑完又回到终态」的）→ 交给三条前提处置（多半被①拒），与单步那条
        「`PERMANENT_REJECTED` 不等于没重跑」的口径逐字相同。
        """
        ordered = _require_batch_node_ids(task_id, node_ids)

        task = await self._require_task(tenant_id, task_id)
        self._require_space_match(task, space_id)

        task_attempt = await self._attempt_of(tenant_id, task, None)
        by_key = {
            node.node_id: node
            for node in await self._subtasks.list_by_attempt(tenant_id, task_attempt.id)
        }
        rows = [
            self._require_subtask_row(by_key.get(node_id), task_id, node_id)
            for node_id in ordered
        ]

        # 幂等档（批量版）：**必须判在前提①之前**，理由与单步那条逐字相同 ——
        # 上一次若真的落库了，任务已经被它拉回 `running`，正是前提①要拒的状态。
        if task.status not in TASK_TERMINAL_STATUSES:
            in_flight = _batch_in_flight_snapshots(rows)
            if in_flight is not None:
                logger.info(
                    "[TaskCenter] 批量重跑判为重发（上一次已生效）：task=%s nodes=%s",
                    task.id,
                    ordered,
                )
                return in_flight

        self._require_terminal(task, "subtask_retry_task_not_terminal")

        for row in rows:
            if row.status not in SUBTASK_TERMINAL_STATUSES:
                raise InvalidParameterError(
                    message=translate(
                        "err.task_center.subtask.retry_status_invalid",
                        params={"node_id": row.node_id, "status": row.status},
                        fallback=(
                            f"节点 {row.node_id} 当前是 {row.status}，"
                            "只有已结束（success/failed/timeout/stopped）的能重试"
                        ),
                    ),
                    details={
                        "task_id": task_id,
                        "node_id": row.node_id,
                        "status": row.status,
                    },
                )

        # ③ 每一步**批外**的上游都得 `success`（批内的见 docstring）。
        batch = set(ordered)
        for row in rows:
            blocked = [
                dep
                for dep in (row.depends_on or [])
                if dep not in batch
                and (
                    by_key.get(dep) is None
                    or by_key[dep].status not in SUBTASK_SATISFIED_STATUSES
                )
            ]
            if blocked:
                raise InvalidParameterError(
                    message=translate(
                        "err.task_center.subtask.retry_upstream_unmet",
                        params={
                            "node_id": row.node_id,
                            "upstream": "、".join(blocked),
                        },
                        fallback=(
                            f"节点 {row.node_id} 的上游还没成功，重试跑不起来；"
                            "请先把上游重试成功，或整条任务重跑"
                        ),
                    ),
                    details={
                        "task_id": task_id,
                        "node_id": row.node_id,
                        "unsatisfied_upstream": blocked,
                    },
                )

        # 批内先后：第 i 步等第 i-1 步。**已经在依赖里就不重复写**（单元素批、或编排
        # 本来就写着这条边时都走这一档）。
        #
        # ⚠️ **两张图要一起补（`depends_on` 与 `next_steps`）**：调度的「谁先谁后」看
        #    `depends_on`，而「上游成功后重算下游入参」看 `next_steps`
        #    （`RunContext.downstream_closure`）。只补前者会静默出错 —— 下游**会等**上游、
        #    却**不会拿到**上游的新产出，带着**上一轮的旧入参**跑。
        #    什么时候真会踩到：这一批里靠后的那步引用了靠前那步的产出
        #    （`@A.outputs.x`），而它在编排里**本来不是 A 的下游**（发版校验只要求被引用
        #    的节点存在，不要求它是上游）。现役几条链是线性且逐级直连的，踩不到；
        #    这两行是给那种编排兜底的。
        added: Dict[str, List[str]] = {}
        added_next: Dict[str, List[str]] = {}
        for index in range(1, len(ordered)):
            previous, node_id = ordered[index - 1], ordered[index]
            deps = list(by_key[node_id].depends_on or [])
            if previous not in deps:
                deps.append(previous)
                added[node_id] = deps
            # 对偶边：把下游补进上游的 `next_steps`（只补新增的那条，原有顺序不动）。
            #
            # ⚠️ **独立于上面那个 `if` 判**（别挪进去）：`depends_on` 已经有了、而
            # `next_steps` 还差这一条 —— 正是**上一版代码留下的半拉子数据**
            # （那一版的 `reset_for_retry` 还没有 `next_steps` 参数，只写了 `depends_on`）。
            # 挂在里面就永远修不上，而那个状态恰恰是本注释开头说的「下游会等上游、
            # 却拿不到上游的新产出」—— 静默出错，且只有再重跑一次这批才会暴露。
            nxt = list(by_key[previous].next_steps or [])
            if node_id not in nxt:
                nxt.append(node_id)
                added_next[previous] = nxt
        _require_no_cycle_after_adding(task_id, by_key, added)

        # ① 任务 + 它当前那次运行一起拉回「跑着」（与单步那处逐字一致；`operator_id`
        #    挂在这条 CAS UPDATE 上，有人才写 `updated_by`）。
        reopened = await self._tasks.reset_for_subtask_retry(
            tenant_id, task.id, task.state_version, operator_id
        )
        if not reopened:
            raise ResourceConflictError(
                message=translate(
                    "err.task_center.task.retry_race",
                    params={"task_id": task_id},
                    fallback=f"任务 {task_id} 的快照刚被别处改过，重试没生效",
                ),
                details={"task_id": task_id},
            )
        _apply_local(
            task,
            status=TaskStatus.RUNNING.value,
            # 内存这份跟库里那份必须一起清（与单步那处同一句注释）：不清的话，同一个
            # 事务里后半段读到的还是上一轮的失败原因。
            error_message=None,
            error_code=None,
            error_params=None,
            finished_at=None,
        )
        await self._attempts.reopen_for_subtask_retry(tenant_id, task_attempt.id)

        # ② 逐行重置回排队 —— 批内先后**在同一条 UPDATE 里**写进去，不留「重置了但顺序
        #    还没连上」的中间态（那个中间态里下游的依赖当场是满足的，下一轮派活就会把
        #    它跟上游一起派出去，正好是要防的事）。
        for node_id in ordered:
            row = by_key[node_id]
            # ⚠️ **必须在这条 CAS 之前读出来**（单步那处也是这个顺序）：CAS 走的是 ORM
            # 的 `update(Model)`、`synchronize_session='auto'`，执行那一刻内存里这一行的
            # `current_attempt` 就已经是「原值 + 1」了（`_apply_local` 的 docstring 有
            # 实测记录）。在这之后再 `+1`，内存就比库大 1 —— 立刻错两处：④ 那条排队历史
            # 会写成 `attempt=原值+2`（指向一次不存在的尝试），回给调用方的快照也跟着多 1。
            new_attempt = row.current_attempt + 1
            reset = await self._subtasks.reset_for_retry(
                tenant_id,
                row.id,
                row.state_version,
                depends_on=added.get(node_id),
                # 对偶边（给上游补下游）—— 只对**本批里被加了新下游**的那些行有值。
                # 两张图必须一起改，理由见上面构造 `added` / `added_next` 那段。
                next_steps=added_next.get(node_id),
            )
            if not reset:
                # CAS 命中 0 行 = 并发冲突（1003），与单步那处同一档、同一理由。
                raise ResourceConflictError(
                    message=translate(
                        "err.task_center.subtask.retry_conflict",
                        params={"node_id": node_id},
                        fallback=f"节点 {node_id} 的状态刚被别处改过，重试没生效",
                    ),
                    details={"task_id": task_id, "node_id": node_id},
                )
            _apply_local(
                row,
                status=SubtaskStatus.PENDING.value,
                current_attempt=new_attempt,
                execution_id=None,
                outputs=None,
                error_message=None,
                error_code=None,
                error_params=None,
                started_at=None,
                finished_at=None,
            )
            if node_id in added:
                _apply_local(row, depends_on=added[node_id])
            if node_id in added_next:
                _apply_local(row, next_steps=added_next[node_id])

        # ③ 入参按上游现在的产出重算一遍（§6.2）。**批内还没跑完的那些会被跳过** ——
        #    `deps_satisfied` 为假就不填，等它们各自的上游成功时由
        #    `_fill_downstream` 补上（首次运行时是同一个机制，不是这里
        #    少做了一步）。所以这里通常只填到链头那一个。
        ctx = await self._load_context(tenant_id, task_id, task_attempt.id)
        if ctx is None:
            # 够不着：整段持有任务锁，`task.delete` 进不来，所以只可能是别处把运行行
            # 假删了 —— 记一笔、照常把重试做完（与单步那处同一条后路）。
            logger.warning(
                "[TaskCenter] 批量重跑凑不齐上下文，跳过入参重算：task=%s attempt=%s",
                task_id,
                task_attempt.id,
            )
        else:
            for node_id in ordered:
                node = ctx.by_key.get(node_id)
                if node is not None and ctx.deps_satisfied(node):
                    await self._fill_inputs(ctx, node)

        # ④ 每一步各插一条新的排队历史（`created_at` 从此存在，派活排队排序要用，§6.3）。
        final = {
            node_id: (ctx.by_key.get(node_id) if ctx is not None else None)
            or by_key[node_id]
            for node_id in ordered
        }
        for node_id in ordered:
            await self._open_attempt_row(
                tenant_id,
                task.space_id,
                task_id,
                final[node_id],
                created_at=datetime.now(),
            )
        logger.info(
            "[TaskCenter] 批量重跑子任务 task=%s nodes=%s added_deps=%s",
            task.id,
            ordered,
            added,
        )
        return [SubtaskItemResponse(**final[node_id].to_dict()) for node_id in ordered]

    async def stop_task(
        self,
        task_id: str,
        tenant_id: Optional[str] = None,
        operator_id: Optional[str] = None,
        *,
        user_id: Optional[str] = None,
    ) -> TaskItemResponse:
        """停止任务（`task.stop`）—— 先拿任务锁，再走 `_do_stop_task`。

        **这是「调用方主动发起的写操作」**（前端任务页面点「停止」），锁拿不到绝不跳过：
        `patient=True` **有心等**（有界窗口 ≈3.15 秒，**不是等到底**），仍拿不到就抛可重试码让调用方自己重发（§7.2 ①）。

        **`operator_id` 是点「停止」的那个人**（`user_id`，落 `tasks.updated_by`）。
        跟 `created_by` 不同，这一列**会被覆盖**；不填就不写（保持原值），不会把它清空。

        ⚠️ **空间可见性**（2026-10-13 补）：这是**写**接口，缺了这道闸，同租户里对空间 S
        无角色的用户拿到 S 中某条任务的 `id` 就能**停掉整条任务**。与 `page_tasks` /
        `get_task_console` 同一道闸；`user_id` 为空（内部链路 / 运维）时放行。
        ⚠️ 这一判在**拿锁之前**（锁外）—— 任务行的 `space_id` 不会被改写，先判一次即可，
        不必把一次权限查询塞进锁内。
        """
        tenant_id = require_tenant(tenant_id)
        task = await self._require_task(tenant_id, task_id)
        await require_space_visible(self.session, tenant_id, task.space_id, user_id)
        async with task_lock(task_id, patient=True):
            result = await self._do_stop_task(tenant_id, task_id, operator_id)
            # 运行方名单**在锁内取**（纯读库、不出站）：出了锁这一段就提交了，而发通知要用它。
            # 取不到（任务行读不到）就当空名单 —— 下面那个方法对空名单直接收工。
            task = await self._tasks.get_task(tenant_id, task_id)
            capabilities = (
                await self._runner_capabilities_for_task(tenant_id, task)
                if task is not None
                else []
            )
            await self._commit_locked_write()
        # ⚠️ **出站放在锁外**（§7.2：锁不跨出站）—— 上面那把锁已经放掉了才发。
        await self._notify_stopped(tenant_id, result, capabilities)
        return result

    async def _notify_stopped(
        self,
        tenant_id: str,
        stopped: TaskItemResponse,
        capabilities: List[str],
    ) -> None:
        """**尽力而为**地通知运行方「这条任务被用户停了」（§4.1 ③，2026-09-30 加）。

        为什么要有它：`task.stop` 从前**只在任务中心内部办、不出站**，于是运行方不知道任务
        已经终止、继续盯着那份被它接管的文档 —— 用户在文档页**重新解析 / 删除文档 /
        整库重置**三个入口**全被拦**，要等那个在跑的活自己结束（再等 30 分钟兜底）、
        或最长到 6 小时硬超时。用户看到的是「任务页说已停、文档页说还在处理中」。

        ⚠️ **它绝不抛异常**：调用方是用户点「停止」那条**同步请求**，而停止本身**已经
        落库了** —— 为一次「通知」把用户的停止报成失败是错的。发不出去只记 warning，
        那份文档退回看门狗那条 30 分钟的兜底，**不会比今天更差**。

        ⚠️ **单次发送、不重发**（不走 `call_with_retry`）、超时也短（`TASK_CENTER_NOTIFY_TIMEOUT`）：
        不能让用户为一次通知干等 30 秒 × 3。

        ⚠️ **这是通知，不是命令** —— 运行方停不停、怎么停由它自己决定（见 `TaskStoppedDispatch`）。
        """
        if not capabilities:
            return
        # ⚠️ **`space_id` 兜一个空串**：`TaskStoppedDispatch.space_id` 是必填 `str`，而
        # `TaskItemResponse.space_id` 是 `Optional[str]` —— 传 `None` 会让 pydantic 抛
        # `ValidationError`，而**这行在 `for` / `try` 之外**：那异常会一路冒出 `stop_task`，
        # 用户收到「停止失败」，可**停止本身已经落库了**（下面那句「绝不抛」就成了空话）。
        # 今天 DB 侧 `tasks.space_id` 是 `NOT NULL`、取不到 `None`，所以不可达 ——
        # 但这条承诺值得用一行兜住（真到那天没人有空改它）。
        dispatch = TaskStoppedDispatch(
            tenant_id=tenant_id, space_id=stopped.space_id or "", task_id=stopped.id
        )
        for capability_id in capabilities:
            try:
                result = await self._adapter.notify_stopped(capability_id, dispatch)
            except Exception as exc:  # noqa: BLE001 —— 适配器自己炸了也算「没发出去」
                logger.warning(
                    "[TaskCenter] 停止通知没能发给运行方 %s task=%s：%s",
                    capability_id,
                    stopped.id,
                    exc,
                )
                continue
            if result.disposition is DispatchDisposition.ACCEPTED:
                logger.info(
                    "[TaskCenter] 已通知运行方 %s：任务 %s 被用户停止（收口后）",
                    capability_id,
                    stopped.id,
                )
            else:
                logger.warning(
                    "[TaskCenter] 停止通知运行方 %s 没收下 task=%s：%s",
                    capability_id,
                    stopped.id,
                    result.message,
                )

    async def _do_stop_task(
        self,
        tenant_id: str,
        task_id: str,
        operator_id: Optional[str] = None,
    ) -> TaskItemResponse:
        """`stop_task` 的锁内部分：判可停 → 把该停的子任务置停 → 任务就地收 `stopped`。

        **这是一条管理方自己发起的终止路径，运行方不参与判定** —— 本方法**只动任务中心
        自己的库**：运行方侧的处置**全在锁外那一步**（`stop_task` 收口之后调
        `_notify_stopped`，尽力而为地发一条 `task_stopped` 通知，§4.1 ③），而且那是
        **通知不是命令**，本方法一个字都不等它。
        运行方侧那些正在跑的子任务**杀不掉**（运行方没有「停止」这个动作），本方法也
        **一律不动它们的状态**：它们会一直跑到自己结束，届时照常上报，`_advance` 照常
        把状态按上报填进去（`running → success/failed` 这条 CAS 只看子任务自己，不看任务）。

        那条还在跑的行**必须有出口，否则它会一直占着并发名额**（`count_running_by_catalog_step`
        只数 `running`、不分运行）。两个出口都给足了：**跑完上报能收它**（即使用户停完又
        `retry_task` 换了运行 —— `_absorb` 不再丢弃旧运行的上报，只收它自己那一行）；**真卡死
        就靠超时巡检兜底**（`list_timed_out_for_patrol` 不按运行筛）。两条路都碰不到任务快照
        —— `_aggregate` 开头那道「只有当前运行才汇总」的闸挡着，「已停止」这个结论不会被改写。

        判定与处置（**判据是「还有没有没开始的子任务」**，不是任务快照 —— 见 `_stop_targets`）：

        | 停止时的情况 | 动哪些子任务 |
        | --- | --- |
        | 还有没开始的（`pending`）子任务 | 那些置停（正常就是全选）；`running` 的一个都不动 |
        | 一个没开始的都没有，**但底下有活在跑**（任务已 `running`，或有子任务 `running`） | 拒绝：「不存在未开始的子任务，无法停止任务」 |
        | 一个没开始的都没有，底下也没有活（空任务） | **照收**：没有子任务可派，拒掉就再没别的办法收掉它 |
        | 状态已终态（`completed` / `failed` / `stopped`） | 拒绝：已经结束了，停不了（`_require_stoppable` 判在最前面） |

        三种能停的情形都**只碰 `pending`**：`running` 的子任务在任何情况下都不去写它
        （见 `_stop_targets`）。

        ⚠️ **整条停止不看 `downstream_on_stop`** —— 那个开关管的是「停某一步时下游跟不跟」
        （`_do_stop_subtask`）。整条都停了，底下每一个还没开始的步骤都是「不跑了」，
        没有「跟不跟」可言。

        「一个没开始的都没有、**但底下有活在跑**」时拒绝：这时能停的只剩那些已经派出去、
        正在跑的活，而它们杀不掉 —— 收下这次停止等于给调用方一个「停了」的假象，
        实际什么都没停。

        置停之后**必须自己把任务和当前运行一起收口**（`_settle_stopped`），不能指望
        `_aggregate`：它只从「消化上报 / 超时巡检 / 防僵尸」三条路上被调，停止不经过
        任何一条 —— 尤其在「只剩 `running` 子任务」的情形下，任务会一直挂在 `running`
        直到那个子任务上报，用户点了停止却看不到任何变化。
        """
        task = await self._require_task(tenant_id, task_id)
        self._require_stoppable(task)

        # 当前那次运行 —— 锁已在手，`current_attempt` 不会在这中间被 `retry_task` 推走
        # （那条路拿的是同一把锁）。
        attempt = await self._attempt_of(tenant_id, task, None)
        ctx = await self._load_context(tenant_id, task.id, attempt.id)
        if ctx is None:
            # 拿锁期间任务被删了（`delete_task` 也拿同一把锁，这里只是兜一下）。
            raise ResourceNotFoundError(
                message=translate(
                    "err.task_center.task.not_found",
                    params={"task_id": task_id},
                    fallback=f"任务 {task_id} 不存在",
                ),
                details={"task_id": task_id},
            )

        targets = self._stop_targets(ctx.task, ctx.subtasks)
        # 一个时刻算一次：这一批子任务与任务快照的 `finished_at` 必须是同一个值。
        now = datetime.now()
        # 数**真的改到了的**那几个：`_close_subtask_row` 是 CAS，别处刚动过这行就命中 0 行。
        # 日志按候选数报会虚高，排障时对不上库里的实际情况。
        # ⚠️ **不再写失败原因**（2026-10-15）：状态本身就是「已停止」这个结论，
        #    再灌一句「用户手动停止」是与它重复的信息；读接口那边也不再有代号可渲染。
        stopped_count = 0
        for subtask in targets:
            if await self._close_subtask_row(
                subtask, None, next_status=SubtaskStatus.STOPPED.value, now=now
            ):
                stopped_count += 1

        final = await self._settle_stopped(ctx, now, operator_id)
        logger.info(
            "[TaskCenter] 停止任务 task=%s 置停子任务 %s 个（候选 %s 个）status=%s",
            task_id,
            stopped_count,
            len(targets),
            final.status,
        )
        return TaskItemResponse(**final.to_dict())

    async def _settle_stopped(
        self,
        ctx: RunContext,
        now: datetime,
        operator_id: Optional[str] = None,
    ) -> Task:
        """把「被停掉」的任务与它当前那次运行**就地收口**成 `stopped`，返回收口后的任务行。

        **运行那一行必须一起收**（`finish_attempt_cas`），两个理由：

        - **保住「已停止」这个结论**：那个还在跑的子任务以后上报时，`_advance` 末尾照旧调
          `_aggregate`；`_aggregate` 开头**先看这次运行还是不是当前运行**（不是就 `return`，
          ⓪ 那道）、**再看任务快照是不是已终态**（是就 `return`，① 那道）。⚠️ **2026-09-29 起
          第一道读的是运行号、不是运行行** —— 运行行那道（②）从「是终态就 return」改成了
          「**记住并继续**」（补收口那一趟要靠它补写任务快照，见 `_aggregate` ② 的注释）。
          这两道拦得住它，不会把「已停止」改写成按节点拼接的那种自动结论 —— 尤其第二道：用户停完**又 `retry_task`** 之后，那次运行连
          「当前运行」都不是了，全靠它挡。**这一列因此必须收口**，不能指望任务快照那边。
        - **别把重跑搅乱**：不收的话，迟到的上报会带着**旧运行**的 ctx 走到
          `_update_task_snapshot` —— 而那时 `tasks` 这一行可能已经是 `retry_task` 之后
          新一轮运行的了，它会被旧运行的一句汇总**凭空改成 `failed`**。运行行收了口，
          这条路上的一切都被 `_aggregate` 那道闸门挡死。

        **两道防线都不覆盖已定的终态**：运行行 `finish_attempt_cas` 只认 `pending` /
        `running` 前驱，任务快照写之前再确认一次当前状态 —— 万一同一个瞬间后台那一趟
        把运行正常收口了（最后一个子任务恰好在此时上报），它是权威，本方法只把
        运行行按幂等收一遍、不动任务快照。

        **不再写失败原因（2026-10-15）**：这个动作的结论就写在 `status` 那一列上。
        从前会往 `error_message` + 代号 + 参数三件里灌一句「用户手动停止」，现在
        **三件一律不写**：状态标签已经说「已停止」了，再配一句同义的话是重复；况且任务级
        原因从来没有「哪一步挂了」可拼（停在 `pending` 的任务一步都没跑，拼出来只会让人
        以为某一步跑挂了）。相应地，读接口在这一档也不再有代号可渲染。

        **`updated_by` 只在 `operator_id` 有值时才带上这一列**：`_update_task_snapshot`
        是「传了什么就写什么」，带一个 `None` 进去会把上一次记的人抹掉 —— 那是丢信息，
        不是「没记」。

        **返回值的口径**：这份返回会原样变成 `task.stop` 回给前端的任务快照，所以
        **任何情况下都返回一份任务行、绝不在这里抛** —— 置停子任务与收口运行行都已经
        落了库，抛出去只会让调用方以为「没停成」。三处降级一律返回「手上最新的那一份」：
        任务行查不到（`task is None`）与重读落空（`fresh is None`）各返回比它早的那份
        快照。**这两个分支在同一把任务锁下正常不可达**（唯一会假删任务行的 `task.delete`
        拿的是同一把锁，且它要求任务空闲、过不了「还有个在跑的子任务」那一关），真发生
        只能是**锁提前释放**或**有人绕过 service 改库** —— 两处都打 `warning`
        （见各自分支上的注释）。
        """
        await self._attempts.finish_attempt_cas(
            ctx.tenant_id,
            ctx.attempt.id,
            # 运行行与任务快照写**同一个终态**（`AttemptStatus.STOPPED` 与
            # `TaskStatus.STOPPED` 逐字相同）—— 排障时不会撞见「任务说已停止、
            # 运行记录说已失败」。三个原因字段一律不写，理由见 docstring。
            AttemptStatus.STOPPED.value,
            finished_at=now,
        )
        task = await self._tasks.get_task_fresh(ctx.tenant_id, ctx.task.id)
        if task is None:
            # ⚠️ **正常路径到不了这里，真到了就是锁没兜住。**
            # `get_task_fresh` 自己就带 `is_deleted == 0` —— 跟 `_require_task` 走的
            # 那条路**同一口径**（`get_by_id` 也过 `_soft_delete_conditions()`；两者
            # 的唯一差别是前者多一个 `populate_existing`、用来绕开会话身份映射），
            # 所以 `None` 只可能出自「任务行被假删了」；而全仓唯一会把任务行假删的是
            # `task.delete`，
            # 它拿的是**同一把任务锁** —— 我们进来时锁已经在手，本方法又是 `stop_task`
            # 锁内那一段的一部分，它插不进来。它另有一道自己的关：删除要求任务**空闲**
            # （`_require_deletable`），而停止这条路上**正在跑的那个子任务一个都不碰**
            # （`_stop_targets` 只选 `pending`），只要它还在跑，删除就过不来。
            # 剩下两种可能：**锁的 `PX` 到期、被提前释放**（`task_lock.py` 不续期，
            # 同一把锁被另一个持有者拿走），或**有人绕过 service 直改了库**。
            # 这时**返回手里这份 `ctx.task`（过期快照）而不是抛异常**：停止该做的事在
            # 库上已经做完了（子任务置停 + 运行行收口都落了库），回一份粗一点的状态只是
            # 显示问题；抛出去会让调用方以为「停止失败」、再点一次又是同一套置停，而任务
            # 其实已经停了。**把这一次记下来** —— 它不该发生，值得有人看一眼。
            logger.warning(
                "[TaskCenter] 停止任务 %s 时任务行查不到了（同锁下不可达，锁疑似提前"
                "释放 / 行被外部假删），降级返回手里的快照 status=%s",
                ctx.task.id,
                ctx.task.status,
            )
            return ctx.task
        if task.status in TASK_TERMINAL_STATUSES:
            return task
        snapshot: Dict[str, Any] = {
            "status": TaskStatus.STOPPED.value,
            "finished_at": now,
        }
        if operator_id:
            snapshot["updated_by"] = operator_id
        # 同上：闸门必须写进 `WHERE`（`task` 是刚 `get_task_fresh` 读的最新版本号，
        # 而「该不该收口」判的是手上那份上下文）。
        changed = await self._update_task_snapshot(
            task,
            require_active=True,
            expect_current_attempt=ctx.attempt.attempt,
            **snapshot,
        )
        if changed:
            return task
        # CAS 未命中：**锁没能把这一小段兜住**。改 `tasks` 的每一处（`task.stop` /
        # `retry_*` / 后台收口 / `_mark_run_started`）都在这把锁下，本方法又不是
        # 「读—判—写」一步到底（中间还查了 `get_task_fresh`、算过 `TASK_TERMINAL_STATUSES`），
        # 而 `task_lock` **不续期**（`PX = TASK_CENTER_LOCK_TIMEOUT`，默认 30 秒）——
        # 只要中间某一步（DB 慢查询 / 连接池排队）拖过了剩余租期，锁就会被下一个持有者
        # 拿走，它进来说不定正好把这行收口掉。
        # 此时 `_update_task_snapshot` **不会** `_apply_local`，手里这份 `task` 已经不代表
        # 库里的真相了 —— 直接回出去会让 `stop_task` 把一个过期的状态（典型是 `running`）
        # 报给前端：用户点了停止，界面显示「还在跑」。**重读**再返回，调用方拿到的必须是
        # 库里的实况。
        # （⚠️ 与 `_aggregate` 那处不同：那里 CAS 落空会按 `logger.error` 报出来
        # ——它带着「这把锁被提前释放过」这个信号，值得有人看一眼；这里落空是
        # **可以就地自解**的 —— 重读一次就能拿到实况，所以只重读、不再报错。
        # ⚠️ 两处现在都带了 `require_active` / `expect_current_attempt` 两个闸门，
        # 所以「这中间用户点了『重跑』、任务已经换成新一轮」也成了落空的正常成因：
        # 那种情况下本次停止**本就不该**再去动它，CAS 拦下正是对的，重读返回的会是
        # 新一轮的实况。）
        fresh = await self._tasks.get_task_fresh(ctx.tenant_id, ctx.task.id)
        if fresh is None:
            # 这一步正常同样到不了（推理见上面 `task is None` 那一段）。而且**能走到这里
            # 本身就说明锁已经不稳了** —— 上面那次 CAS 落空，在「同一把任务锁护着所有
            # `tasks` 写路径」的前提下，只可能是锁被提前释放、另一个持有者先改了。
            # 降级返回重读前那份 `task`：它至少是这一轮真实读出来的状态，比抛异常对
            # 调用方有用（停止已经落库）。**记 warning** —— 锁不稳是要人看的事。
            logger.warning(
                "[TaskCenter] 停止任务 %s 重读任务行也查不到（锁疑似过期 + 行被外部"
                "假删），降级返回重读前的快照 status=%s",
                ctx.task.id,
                task.status,
            )
        return fresh if fresh is not None else task

    async def console_retry_subtask(
        self,
        task_id: str,
        node_id: str,
        tenant_id: Optional[str] = None,
        operator_id: Optional[str] = None,
        *,
        user_id: Optional[str] = None,
    ) -> TaskItemResponse:
        """重跑某一步（`task.console_subtask_retry`，**前端面**）—— 回一份任务快照。

        与 `retry_subtask` 的区别只有一处，是「前端面」带来的：**`space_id` 从任务行推导**，
        不要求调用方传。

        那条 `space_id` 是 `retry_subtask` 的**一致性核对**（`task.space_id` 与它不符按
        「找不到」拒 1002），本意是让**运行方**证明「我操作的是我那个空间的这条任务」；
        浏览器没有空间上下文，硬要它传只有两条路 —— 放宽核对（削弱运行方那条契约）或让
        前端瞎编一个（更糟）。这里先把任务读出来、拿它自己的 `space_id` 回填，核对于是
        恒真，**既有的 `retry_subtask` / `task.subtask_retry` 一个字都不改**（运行方那条路
        照旧要求传，那道核对该有的效力一点没少）。

        另一点不同是**回任务快照而不是空**：`task.subtask_retry` 回空有它的理由（那一档是
        运行方契约，成败看 `success` / `code`，塞内部快照等于把它升成对外契约）；而这一档
        给前端页面用，重跑完最需要的恰恰是「这条任务现在什么样」—— 与 `task.stop` /
        `stop_subtask` 同形。

        三小步走的是**同一个会话**（`_execute` 开的那个），但 ⚠️ **不是同一个事务**：
        `retry_subtask` 内部会在放锁之前自己提交一次（`_commit_locked_write`，§7.2 对所有
        「用户主动写」的硬要求），所以事务在那一步中间就落过库了。这**不影响本方法的正确性**
        —— 第一步纯粹是读，第三小步重新查一遍就是，但**别把这句读成「三小步要么全有要么
        全无」**：真出错时，重试那一步是可能已经落了库的，此时返回的是异常而不是快照。

        `get_task` 自带租户过滤与「找不到」判定（1002），所以任务不存在 / 不属于本租户
        两种情况在第一步就拦住了。

        ⚠️ **拿锁交给被复用的 `retry_subtask`，本方法绝不自己再拿一次**：`task_lock` 是
        按 `task_id` 抢 Redis 键，**不可重入** —— 外面先拿、里面再拿同一个 id，里面那层
        会等外面那层已经握着的锁，白等满 `PATIENT` 那个窗口（默认 3.15 秒）再抛
        `TaskLockBusy`（3007）。所以这里只有 `retry_subtask` 那一层持锁。
        """
        task = await self.get_task(task_id, tenant_id)
        # ⚠️ **空间可见性**（2026-10-13 补）：这是**写**接口 —— 缺了这道闸，同租户里对空间 S
        # 无角色的用户，拿到 S 中某条任务的 `id` 就能**重跑**它的步骤。与 `get_task_console`
        # / `stop_subtask` 同一道闸；`user_id` 为空（内部链路 / 测试 token）时放行。
        # ⚠️ 这一判放在 `retry_subtask`（它自己会拿任务锁）之前 —— 校验在锁外做。
        await require_space_visible(self.session, tenant_id, task.space_id, user_id)
        await self.retry_subtask(
            task_id,
            node_id,
            tenant_id=tenant_id,
            space_id=task.space_id,
            operator_id=operator_id,
        )
        return await self.get_task(task_id, tenant_id)

    async def stop_subtask(
        self,
        task_id: str,
        node_id: str,
        tenant_id: Optional[str] = None,
        operator_id: Optional[str] = None,
        *,
        user_id: Optional[str] = None,
    ) -> TaskItemResponse:
        """停止某一个子任务（`task.console_subtask_stop`）—— 先拿任务锁，再走 `_do_stop_subtask`。

        **这是「调用方主动发起的写操作」**（前端任务页面点某一步的「停止」），锁拿不到绝不
        跳过：`patient=True` **有心等**（有界窗口 ≈3.15 秒，**不是等到底**），仍拿不到就抛可重试码让调用方自己重发（§7.2 ①）。

        `operator_id` 是点「停止」的那个人（`user_id`，落 `tasks.updated_by`）；不填就不写，
        绝不用空值把上一次的操作人盖掉。

        ⚠️ **空间可见性**（2026-10-13 补）：这是**写**接口 —— 缺了这道闸，同租户里对空间 S
        无角色的用户，拿到 S 中某条任务的 `id` 就能**停掉**它还没开始的步骤。与 `page_tasks`
        / `get_task_console` 同一道闸；`user_id` 为空（内部链路 / 测试 token）时放行。
        ⚠️ **这一判放在拿锁之前**（锁外）：任务行的 `space_id` 不会被改写，先判一次即可，
        不必把一次权限查询塞进锁内白占锁持有时间。

        ── **收口之后会通知运行方**（2026-10-15 加）──

        停完之后**任务若已到终态**，锁外补发一条 `task_stopped`（与 `task.stop` 同一个
        `_notify_stopped`）。触发这一条的是本轮那个「连带下游」：`parse_1` 一停、`compile_1`
        跟着停，整条链当场收口，而运行方**毫不知情** —— 它的接管行留在 `managed`、文档卡在
        中间态，要等看门狗 30 分钟的兜底才交还（这期间用户连文档都删不掉）。

        **任务还在跑就不发**（只停了一条支路，别的分支还在继续）—— 那正是这条路从前
        「从不通知」时想守住的场景，守住的只是那一半。
        """
        tenant_id = require_tenant(tenant_id)
        task = await self._require_task(tenant_id, task_id)
        await require_space_visible(self.session, tenant_id, task.space_id, user_id)
        async with task_lock(task_id, patient=True):
            result = await self._do_stop_subtask(tenant_id, task_id, node_id, operator_id)
            # 运行方名单**在锁内取**（纯读库、不出站）：出了锁这一段就提交了，而发通知要用它。
            # 与 `stop_task` 逐字同款 —— 两处都要在**任务确实收了口**时通知运行方。
            task = await self._tasks.get_task(tenant_id, task_id)
            capabilities = (
                await self._runner_capabilities_for_task(tenant_id, task)
                if task is not None
                else []
            )
            await self._commit_locked_write()
        # ⚠️ **出站放在锁外**（§7.2：锁不跨出站）—— 上面那把锁已经放掉了才发。
        #
        # ── 为什么**收了口才通知**（2026-10-15 加）──
        #
        # 这条路从前**从不通知**，理由是「只停一步时任务多半还在跑，别的分支还在继续，
        # 不该惊动运行方」。那个理由对「还有分支在跑」成立，但**对「一步就把整条链停掉了」
        # 不成立**：本轮起停某一步会按 `downstream_on_stop` 连带停下游，于是「停下这一步
        # ⇒ 整条任务收口」变得常见 —— 而此时运行方**毫不知情**，它那份接管行留在
        # `managed`、文档卡在中间态，要等看门狗那条 30 分钟的兜底才交还（这期间用户连
        # 文档都删不掉，会撞 409）。
        #
        # 判据就是「**任务已到终态**」：到了才发（运行方该交还了），还在跑就一个字都不发
        # —— 那才是「只停了一条支路」的正常情形。
        # ⚠️ 与 `stop_task` 共用同一个 `_notify_stopped`：尽力而为、单次发送、绝不抛。
        if result.status in TASK_TERMINAL_STATUSES:
            await self._notify_stopped(tenant_id, result, capabilities)
        return result

    async def _do_stop_subtask(
        self,
        tenant_id: str,
        task_id: str,
        node_id: str,
        operator_id: Optional[str] = None,
    ) -> TaskItemResponse:
        """`stop_subtask` 的锁内部分：判可停 → 置停这一步（含连带的下游）→ 按收尾表收敛。

        **与 `stop_task` 是同一个家族，但只停一条支路，而且不立刻收口。**

        - **起点只能是 `pending`，`running` 的拒绝（1003）** —— 与 `stop_task` 完全同
          口径：运行方没有「停止」这个动作，已经派出去的活平台杀不掉，把它写成「已停止」
          等于把「运行方还在跑、平台说它被用户停了」写进库里。终态的一步也拒
          （本来就停完了，没有可停的东西）。
        - **连带下游（2026-10-15 加）**：起点 + 按 **`downstream_on_stop`** 展开的下游里，
          **还是 `pending` 的**一并置停。从前下游是留在 `pending` 干挂着的（被上游 hold、
          永远不跑），控制台上显示的却仍是「等待中」—— 同一件事说成了两种样子。
          ⚠️ 开关**逐边**读，只看上游那个节点自己的字段；与重跑用的是同一把尺子
          （`_downstream_chain`），只是读的开关名不同。
        - **收口走 `_aggregate`，不走 `_settle_stopped`。**
          这是与 `stop_task` 唯一实质的分歧，理由很硬：`_settle_stopped` 是**无条件**把任务
          与当次运行一起收进终态，那是「整条任务停止」的语义；而停某一步时，**同一次
          运行里可能还有别的分支在跑、或等着跑**，那种情况任务必须继续跑，收口就是误判。
          `_aggregate` 按 §7.2 那张收尾表判：还有 `running` / 还能跑的 `pending` 就保持
          `running`，都没有了才收敛 —— 收敛到 `failed` 还是 `stopped` 由「有没有失败」
          那一档决定（这轮新加）。

        ⚠️ **不能「置停这几步、然后什么都不做」** —— 任务会卡在非终态。虽然 `_aggregate`
        有**五个**来路（上报消化 / 派活判死 / 超时巡检 / 防僵尸 / **补收口**），但前四个的
        候选**全是「当次运行里还有 `running` / 能跑的 `pending`」**，而这里恰恰是**它们全都
        够不着**的那一格（这几步刚被置停、当次运行可能就此定局）；补收口那一趟虽然会对
        「当次运行一步没剩」的任务动手，但它要等任务行**静置 `TASK_CENTER_SETTLE_STALE_SEC`
        （默认 60 秒）**才进候选。少了这次主动汇总，任务既不会立刻被后台收掉，也过不了
        `retry_task` / `retry_subtask` 的终态前置（一律 1003）—— 用户要干等到那一趟兜底
        为止（真出了岔子则**只剩 `task.delete` 一条出路**）。

        **返回一份任务行**（同 `task.stop` 的形状）：汇总可能没走到写快照那一步（同一次运行
        还有别的分支在跑），所以这里一律重读一次最新行、返回实况 —— 与 `_settle_stopped`
        末尾同一个写法。

        ⚠️ **判定顺序：任务状态闸（`_require_stoppable`）排在「这个节点存不存在」之前**
        —— 与 `retry_subtask` **顺序**一样（那边也是 `_require_terminal` 在前、
        `_require_subtask_row` 在后）。但**两道状态闸判的是相反的事**（这边只放行
        `pending` / `running`，那边只放行终态），所以**两条接口在同一类输入下给的码本来
        就相反**，这是**刻意的**、不是漏判 —— 别照「两边必须给同一个答案」去理解：

        | 输入 | `stop_subtask`（本方法） | `retry_subtask` |
        | --- | --- | --- |
        | 任务已终态 + 节点名给错 | **1003**「任务已结束」（走不到节点查找） | **1002**「节点不存在」（终态是允许的，继续走到查找） |
        | 任务非终态 + 节点名给错 | **1002**（节点查找在状态闸之后） | **1003**「任务还没跑完」 |

        想验本方法的 1002 那条路，任务得是**非终态**的（那时才会走到节点查找）。
        """
        task = await self._require_task(tenant_id, task_id)
        # 任务已终态 → 停单步无从谈起（与 `stop_task` 共用这道闸）。**放在节点查找之前**，
        # 理由见 docstring 末尾那段 ⚠️。
        self._require_stoppable(task)

        attempt = await self._attempt_of(tenant_id, task, None)
        ctx = await self._load_context(tenant_id, task.id, attempt.id)
        if ctx is None:
            # 拿锁期间任务被删了（`delete_task` 也拿同一把锁，这里只是兜一下）。
            raise ResourceNotFoundError(
                message=translate(
                    "err.task_center.task.not_found",
                    params={"task_id": task_id},
                    fallback=f"任务 {task_id} 不存在",
                ),
                details={"task_id": task_id},
            )

        subtask = await self._subtasks.get_subtask(tenant_id, attempt.id, node_id)
        self._require_subtask_row(subtask, task_id, node_id)
        if subtask.status != SubtaskStatus.PENDING.value:
            raise ResourceConflictError(
                message=translate(
                    "err.task_center.subtask.stop_status_invalid",
                    params={"node_id": node_id, "status": subtask.status},
                    fallback=(
                        f"子任务 {node_id} 当前是 {subtask.status}，停不了"
                        "（只能停还没开始的 pending）"
                    ),
                ),
                details={"task_id": task_id, "node_id": node_id, "status": subtask.status},
            )

        # 停哪些：起点 + 按 `downstream_on_stop` 展开的下游，且**只挑还没开始的**。
        # 筛状态与 `_stop_targets` 同口径 —— `running` 的杀不掉、已经是终态的不该被改写。
        targets = [
            row
            for row in self._stop_cascade_targets(ctx, node_id)
            if row.status == SubtaskStatus.PENDING.value
        ]
        # 一个时刻算一次：这几行的 `finished_at` 必须是同一个值（同 `_do_stop_task`）。
        now = datetime.now()
        stopped_count = 0
        for row in targets:
            # ⚠️ **不写失败原因**（三个 `None`）：被停不是一次失败，原因那一列留给
            #    「为什么没跑成」，而这件事已经写在 `status` 上了。
            if await self._close_subtask_row(
                row, None, next_status=SubtaskStatus.STOPPED.value, now=now
            ):
                stopped_count += 1

        # 汇总一次，让任务按收尾表收敛（**不能省**，理由见 docstring 末段）。
        await self._aggregate(ctx, operator_id=operator_id)
        logger.info(
            "[TaskCenter] 停止子任务 task=%s 起点=%s 置停 %s 个（含下游，候选 %s 个）任务=%s",
            task_id,
            node_id,
            stopped_count,
            len(targets),
            ctx.task.status,
        )
        fresh = await self._tasks.get_task_fresh(tenant_id, task_id)
        return TaskItemResponse(**(fresh if fresh is not None else ctx.task).to_dict())

    async def delete_task(self, task_id: str, tenant_id: Optional[str] = None) -> None:
        """删实例：**先请运行方清掉它侧的残留，成功后才把任务与子任务置假删**（§4.3）。

        五步（顺序不能反，§4.3、A.11）：

        1. **无锁预检**——权限 + **空闲**（K1：还有 `running` 子任务、或「非终态且还有
           `pending` 子任务」→ 拒绝，判据在子任务上、**不看任务快照是不是终态**，理由见
           `_require_deletable`）。这一步读到的 `state_version` **只是"看一眼"，不作为
           后面 CAS 的基准**；
        2. **出站调运行方的 `remove_task`**（§4.1 ②，与派活同一条分发通道，只换动作名与
           报文）——**按 `task_id` 一把清干净**（运行方最清楚自己按什么维度存数据）。
           **这一步 `不持锁`**（§7.2「锁不跨出站调用」）；**内含传送层重发**（没够着运行方
           时最多 3 次 / 退避 2s、5s，与派活共用 `call_with_retry` 那一份）；
           **任何一个运行方清不掉 → 整个删除失败、任务中心侧一个字段都不改**（K2）；
        3. **重新抢任务锁 → 锁内重读 `tasks`**（`get_task_fresh`，绕开会话身份映射）→
           **锁内复查一次 K1（`_require_deletable`）** → 拿**锁内读到的** `state_version`
           做条件更新（K2b）——**必须用锁内那个值**，步骤 1 的值可能已经过期，拿它当 CAS
           基准会误中止一次本该成功的删除。⚠️ **锁内那次 K1 复查不是多余的**：出站那一段
           不持锁（最长 ≈ 97 秒），期间可能有人 `retry_task` 把它拉回去、或有子任务刚被
           抢占成 `running`，都得在这里拦下；
           **命中 0 行 → 中止删除**（这中间有人动过这一行；运行方侧虽然已清干净，任务中心
           这边不删，跑完以后再删一次即可）。⚠️ **成因只有「有人 `retry_task`」这一条** ——
           「别的副本先删了」到不了这儿：`get_task_fresh` 带 `is_deleted == 0`，那种情况走
           的是上面「按『找不到』处理」（1002）；
        4. **同一事务**里把该任务下**所有** `subtasks.is_deleted = 1`；
        5. 释放锁 → 返回成功。

        `task_attempts` / `subtask_attempts` 历史原样保留（K4，供追溯）。**它不是幂等
        成功**：重复调第二次按「找不到」处理（跟 `trig.delete` 不同）。

        **拿不到锁不清空跳过**：这是「调用方主动发起的写操作」，`patient=True` **有心等**（有界窗口 ≈3.15 秒，**不是死等** —— 窗口内拿不到照样抛 3007），
        仍拿不到就回可重试码让调用方自己重试（§7.2 ①）。
        """
        tenant_id = require_tenant(tenant_id)

        # ① 无锁预检：权限 + 「没有活会再动」（K1，判据在**子任务**上，不是任务快照的状态
        #    —— 理由见 `_require_deletable`）。
        task = await self._require_task(tenant_id, task_id)
        await self._require_deletable(tenant_id, task)

        # ⚠️ **出站之前先把这次只读事务结束掉**（2026-10-09 补）：上面两次 SELECT 已经在
        # `self.session` 上开了一个事务，而下面那次出站**自带传送层重发、最坏约 97 秒**
        # （见下一段的代价说明）—— 事务一直挂着就是 `idle in transaction`，一条池连接被
        # 白占近 100 秒；并发删几个任务就能把连接池占干。
        # **读到的结果上面已经用完**（第 ③ 步会重新拿锁、重新读最新行当 CAS 基准），
        # 所以这里提交掉不改变任何语义，只是把「只读事务的生命周期」收回到它该有的长度。
        # ⚠️ **不违反「锁必须覆盖到提交为止」那条纪律** —— 任务锁第 ③ 步才拿，
        # 这里提交发生在**拿锁之前**。
        await self.session.commit()

        dispatch = RemoveTaskDispatch(
            tenant_id=tenant_id,
            space_id=task.space_id,
            task_id=task.id,
        )

        # ② 出站请运行方删除（K2）：清不掉就在这里 fail-fast，管理方侧分毫不动。
        #    **不持锁** —— §7.2「锁不跨出站调用」。
        #    **重发范围与派活完全一致**（`call_with_retry`：3 次 / 退避 2s、5s）：只有
        #    「没够着运行方」（`DELIVERY_UNKNOWN`）才重发；运行方明确回「清不掉」
        #    （`REJECTED`）**不重发** —— 重发一百次还是那句话，白等一段退避。
        #    重发用到这里是**安全的**：`remove_task` 必须幂等（**任务不存在 / 已经删掉了
        #    都要回成功**，§4.1 ②）—— 上一次可能其实已经删干净了、只是回执没回来。
        #    **代价**：这是用户同步请求路径（`task.delete`），最坏让用户干等
        #    `3 × 出站超时 + 7s`（默认 97 秒）。有意接受 —— 删除是低频操作、
        #    「没够着」本身罕见，换来的是用户不用自己再点一次。
        for capability_id in await self._runner_capabilities_for_task(tenant_id, task):
            result = await call_with_retry(
                # 默认参数把循环变量绑死（本循环是顺序 await、现在不出问题，
                # 但将来若改成 gather 就会读到下一轮的值）。
                lambda cid=capability_id: self._adapter.remove_task(cid, dispatch),
                what=f"清理任务 task={task_id} capability={capability_id}",
                # 采集上下文：删除方向**没有 `execution_id`**（这个动作按 `task_id`
                # 一把清、报文里也只有 `task_id`）—— 取 `task_id` 当 `request_id`：
                # 它正是这一路重发的幂等键（重发的就是同一个 `task_id`，
                # 见 `call_with_retry` / `remove_task` 的 docstring）。
                # 租户已在方法开头 `require_tenant` 归一化过。`target` 记这次请哪个
                # 运行方清理 —— N1 的「目标对象」列就是它。
                request_id=dispatch.task_id,
                tenant_id=tenant_id,
                target=capability_id,
            )
            if result.disposition is not DispatchDisposition.ACCEPTED:
                raise UpstreamServiceError(
                    message=translate(
                        "err.task_center.task.runner_delete_failed",
                        params={"task_id": task_id, "capability_id": capability_id},
                        fallback=(
                            f"运行方 {capability_id} 没清掉自己侧的残留，任务 {task_id} 删除失败"
                        ),
                    ),
                    details={"task_id": task_id, "capability_id": capability_id},
                )

        # ③④ 重新抢锁 → 锁内重读 → **锁内复查 K1** → 用锁内读到的那一版做条件更新
        #     → 同一事务级联假删。
        async with task_lock(task_id, patient=True):
            fresh = await self._tasks.get_task_fresh(tenant_id, task_id)
            if fresh is None or fresh.is_deleted:
                raise ResourceNotFoundError(
                    message=translate(
                        "err.task_center.task.not_found",
                        params={"task_id": task_id},
                        fallback=f"任务 {task_id} 不存在",
                    ),
                    details={"task_id": task_id},
                )
            # **必须复查**：上面那次是没拿锁的预检，覆盖不到出站那一段时间（那一趟内含
            # 传送层重发，单条最坏 3 × 超时 + 退避 ≈ 97 秒）里新派出去的活 —— 出站期间
            # 有人 `retry_task` 把它拉回去、或者有子任务刚被抢占成 `running`，都得在这里
            # 拦下。检查与更新在**同一把锁**里，中间没有窗口，所以 `mark_deleted` 只需要
            # 比版本号。
            await self._require_deletable(tenant_id, fresh)
            marked = await self._tasks.mark_deleted(
                tenant_id, task_id, expect_version=fresh.state_version
            )
            if not marked:
                # 命中 0 行 = 有人在这两次拿锁之间动过这一行（`state_version` 变了）。
                # ⚠️ **旧口径里最典型的「`retry_task` 把它拉回去跑了」现在拦在上面那道
                # 复查里**（拉回去 = 多了没开始的子任务、任务也不是终态），走到这里的
                # 概率已经很低，留着是防御。
                # ⚠️ **「别的副本先删了」到不了这里，也到不了上面那道复查** ——
                # `get_task_fresh` 带 `is_deleted == 0`，那种情况走的是再上面那一支
                # （按「找不到」处理，1002）。
                # **运行方侧已经清干净了也不补删** —— 不能把"还有活要跑的任务"的记录删掉；
                # 这条任务跑完以后再删一次即可。
                raise ResourceConflictError(
                    message=translate(
                        "err.task_center.task.delete_raced",
                        params={"task_id": task_id},
                        fallback=(
                            f"任务 {task_id} 在删除过程中被别的操作改过，本次删除已中止，请稍后重试"
                        ),
                    ),
                    details={"task_id": task_id},
                )
            await self._subtasks.mark_deleted_by_task(tenant_id, task_id)
            # **在放锁之前落库**：这一单改的是「任务假删 + 整批子任务假删」，而删掉的
            # 子任务在未提交时看起来还是 `pending` —— 另一个副本扫到它就能抢成 `running`
            # 并真派给运行方，可任务已经删了、结果没人收。别把这句挪到 `logger.info`
            # 后面，那时锁已经放掉了（见 `_commit_locked_write` 的说明）。
            await self._commit_locked_write()

        logger.info("[TaskCenter] 删除任务 task=%s", task_id)

    # ========================================================
    # 后台循环用的单步动作（§7.4、§7.5 ①）
    # ========================================================

    async def claim_for_dispatch(self, subtask_id: str) -> Optional[PreparedDispatch]:
        """派活**前半段**（§7.2「锁不跨出站调用」的第 1 步）：判就绪 → 判并发上限
        → 抢占 → 回填 `execution_id`。**只读改库、不出站**。

        **调用方必须先拿到这把任务锁**（`task_center:lock:task:{task_id}`），并负责提交
        与释放：进来之前抢不到锁就**跳过这条、看下一条**（§7.2 ② 第 2 层 —— 这是扫描类
        调用，下一轮还会扫到，天然可重来）。锁在这里只护「读—改—写」，**绝不能跨着
        紧接着那次出站调用拿** —— 运行方卡三十秒，锁就自动过期、等于没锁（§7.2）。

        抢占（`pending → running`）在**同一条 UPDATE 里**回填 `execution_id` 并
        `state_version+1`，`started_at` 一次算好、两侧（`subtasks` 与 `subtask_attempts`）
        写同一个值（§7.2、§7.5 ①）。

        返回 `None` = 这条这轮不该派（没就绪 / 没抢到 / 已经就地判死）；抢到了就回
        `PreparedDispatch`。**只回快照、不回 ORM 行** —— 事务一提交那些行就过期了，
        后半段得重读（§7.2「值变了就重读再来、不会写脏」）。
        """
        located = await self._subtasks.get_any_by_id(subtask_id)
        if located is None:
            return None
        ctx = await self._load_context(
            located.tenant_id, located.task_id, located.task_attempt_id
        )
        if ctx is None:
            return None
        subtask = ctx.by_key.get(located.node_id)
        if subtask is None or subtask.status != SubtaskStatus.PENDING.value:
            return None
        # 从扫到到派活之间任务可能刚被删，那一单就不该再派出去了（§7.5 ①）；
        # 也要挡住「扫到的是旧运行留下的行」——判据同 `report_status`：这条子任务挂的
        # 运行得是 `tasks.current_attempt` 指的那次（旧运行那套是冻结的历史）。
        # ⚠️ 前半（`ctx.task.is_deleted`）当前**恒为假**，见 `_load_context`。
        if ctx.task.is_deleted or ctx.attempt.attempt != ctx.task.current_attempt:
            return None
        if not ctx.deps_satisfied(subtask):
            return None

        # 并发上限（§6.3）：这步全局在跑数到顶就跳过它、本轮不派，等有结束腾出名额。
        if await self._over_concurrency(ctx, subtask):
            return None

        capability_id = await self._resolve_runner_capability(ctx, subtask)
        if capability_id is None:
            # 这步绑的运行方不在册（或内置清单里压根没这一条）：没有可派的对象，活没人接。
            # **判死而不是留在 `pending` 等下一轮**——没有别的副本会突然变出一个运行方来，
            # 留着的唯一效果是占着队头、还让任务永远收不了口（§4.1 ①）。
            # 判死是**写库**，留在这半段里（还在锁内），别挪到出站之后去。
            logger.warning(
                "[TaskCenter] 节点 %s 没绑到在册的运行方，直接判失败", subtask.node_id
            )
            await self._fail_subtask(
                ctx,
                subtask,
                **build_fail_reason(
                    "err.task_center.subtask.runner_unbound",
                    {"node_id": subtask.node_id},
                    f"节点 {subtask.node_id} 没绑到在册的运行方，无法派活",
                ),
            )
            return None

        # 先抢占再派活（§7.5 ①）：`execution_id` 在抢占前就生成好，抢占时一并落库。
        execution_id = str(uuid4())
        now = datetime.now()
        claimed = await self._subtasks.claim_pending(
            subtask.tenant_id,
            subtask.id,
            subtask.state_version,
            execution_id,
            started_at=now,
        )
        if not claimed:
            return None  # 别的副本先抢到了，本轮跳过
        _apply_local(
            subtask,
            status=SubtaskStatus.RUNNING.value,
            execution_id=execution_id,
            started_at=now,
        )
        # 历史行同一次尝试也跟着置「跑着」，`execution_id` / `started_at` 跟子任务行同值（§7.2）。
        await self._subtask_attempts.set_status_cas(
            subtask.tenant_id,
            subtask.id,
            subtask.current_attempt,
            expect_statuses=[SubtaskAttemptStatus.PENDING.value],
            next_status=SubtaskAttemptStatus.RUNNING.value,
            execution_id=execution_id,
            started_at=now,
        )

        return PreparedDispatch(
            tenant_id=ctx.tenant_id,
            task_id=ctx.task.id,
            task_attempt_id=ctx.attempt.id,
            node_id=subtask.node_id,
            execution_id=execution_id,
            capability_id=capability_id,
            dispatch=ExecutionDispatch(
                tenant_id=ctx.tenant_id,
                space_id=ctx.task.space_id,
                task_id=ctx.task.id,
                step_code=subtask.step_code,
                node_id=subtask.node_id,
                execution_id=execution_id,
                inputs=subtask.inputs or {},
            ),
        )

    async def finalize_dispatch(
        self, prepared: PreparedDispatch, result: DispatchResult
    ) -> bool:
        """派活**后半段**（§7.2 的第 2 步）：按运行方回执收尾。返回是否真派出去了。

        **调用方必须重新拿到同一把任务锁**再进来。出站那一段锁是**放掉**的（§7.2），
        别的副本完全可能已经动过这条任务 —— 所以这里**重读**上下文，再按「还是不是我
        抢下的那一行」复核（`execution_id` 是我们这次生成的，对得上才是同一次抢占）；
        对不上就整单作废、不覆盖别人的判定。这就是文档说的「第二次拿锁后仍带
        `WHERE state_version = :原值`，值变了就重读再来、不会写脏」。

        按 `result.disposition` **三档**收口（三档的含义与判据见 `RunnerAdapter`）：

        - `ACCEPTED` → 任务（连同这次运行）转「跑着」（§7.2：一旦有子任务被派出去，任务
          就转「跑着」）；
        - `REJECTED` → 该子任务置终态 `failed`（终态、**不重派**，下游一律 hold，§4.1 ①、
          §6.3）；
        - `DELIVERY_UNKNOWN` → 走到这里说明 ③ 段的重发预算已经用尽，**再分两种**（判据是
          `result.dead_after_retry`）：
          - **`dead_after_retry` 那一类 → 判死**（同 `REJECTED` 的处置）。它含两种成因：
            **网关 / 传送层连撞 5xx**（这一侧压根没通）、**运行方自己明确回了失败码**
            （HTTP 2xx + **合法信封**（带 `success` 键的 dict）且 `success=false`，请求到了、
            对方说它失败了；码是可重试那档还是未分类码都算）。重发用满还是这样，说明短时间
            好不了，再挂着无非是等超时巡检。⚠️ **HTTP 2xx 但 body 不是这套壳**（非 dict /
            没有 `success` 键的 dict）**不带这个标记**，归下面「其余未知」那一档；
          - **其余未知 → 一个字都不改**，子任务留在 `running` 由超时巡检兜底（§7.4）。

        ⚠️ **加码判死只给 `dead_after_retry` 那一类，别推广到整个 `DELIVERY_UNKNOWN`**：
        其余几档（连不上 / 读超时 / 429 / 响应不是 JSON / **2xx 但 body 不是这套壳**）都
        可能是「**请求其实已经到了运行方**、只是回执没回来」，判死会把一个可能正在跑的活
        做成孤儿 —— 那是本轮体检专门堵掉的坑。两类事实的差别与取舍见 `DispatchResult.dead_after_retry` 的 docstring
        与 `RunnerAdapter` 模块 docstring。
        """
        ctx = await self._load_context(
            prepared.tenant_id, prepared.task_id, prepared.task_attempt_id
        )
        if ctx is None:
            return False
        subtask = ctx.by_key.get(prepared.node_id)
        if (
            subtask is None
            or subtask.execution_id != prepared.execution_id
            or subtask.status != SubtaskStatus.RUNNING.value
        ):
            # 出站期间这行被别的动作接管了（重跑 / 收口 / 更早的判死）：整单作废。
            logger.info(
                "[TaskCenter] 派活收尾时节点 %s 已不是本单抢占的那一行，作废",
                prepared.node_id,
            )
            return False

        if result.disposition is DispatchDisposition.DELIVERY_UNKNOWN:
            # ③ 段的重发预算已经用尽，**最后一次属于哪一类**决定怎么收口（判据是
            # `dead_after_retry`，适配器在那个分支里标的；取「最后一次」而不是「出现过
            # 没有」，理由见 `call_with_retry` 的 docstring）：
            #
            # **① `dead_after_retry` 那一类 → 判死**（与下面 `REJECTED` 同一处置）。它含
            #    两种成因，方向相反、结论相同 —— **都不是「对方可能已经在跑、只是回包丢了」**：
            #    - **网关 / 传送层连撞 5xx**：这一侧压根没通，不存在「请求其实到了运行方」
            #      这个可能（与「连不上 / 读超时」正相反）。5xx 里还混着「能力没在 Sidecar
            #      那侧配端点」这类**装配错**（Sidecar 代理层抛 `CapabilityNotFoundError`
            #      2001，被 `/invoke` 的 `except Exception` 转成 2002，回的是 HTTP 500）——
            #      本地通道对同一类事实判 `REJECTED`，这里判死才与它一致（判决一致、时机
            #      不同：本地第 1 次就判，这边要试满预算，晚约 7 秒）。
            #    - **运行方自己回了失败码**（HTTP 2xx + **合法信封**（带 `success` 键的
            #      dict）且 `success=false`，如 5003）：请求**到了**，是它（或它依赖的
            #      东西）在业务层失败 —— 对方**明确表了态**，与内层回执壳那一档同类，
            #      只是它的码落在可重发那一档（或压根是 `classify_code` 不认识的未分类
            #      码）、所以先重发几次。⚠️ body **不是这套壳**（非 dict / 没有 `success`
            #      键的 dict）时适配器**不打这个标记** —— 那归下面「其余未知」那一档。
            #
            #    **代价（有意接受）**：真·临时故障（502/503/504 抖那么一下、运行方自己
            #    依赖的东西正好在重启）也会在约 7 秒内被判死。换来的是不用挂满
            #    `timeout_seconds`（内置步骤 25200 秒）才有人过问，而且并发名额当场释放
            #    （`count_running_by_catalog_step` 只认 `running`）。
            if result.dead_after_retry:
                logger.error(
                    "[TaskCenter] 派活连撞 %d 次都没成，判死 node=%s capability=%s：%s",
                    RETRY_MAX_ATTEMPTS,
                    prepared.node_id,
                    prepared.capability_id,
                    result.message,
                )
                await self._fail_subtask(
                    ctx,
                    subtask,
                    **build_fail_reason(
                        "err.task_center.subtask.dispatch_exhausted",
                        {
                            "node_id": prepared.node_id,
                            "capability_id": prepared.capability_id,
                            "attempts": RETRY_MAX_ATTEMPTS,
                            # ⚠️ `reason` 是**运行方回包里的原文**（对方那侧的文案，可能
                            # 是另一国语言）—— 它在**参数**里，所以外层包装照旧可翻译、
                            # 这段原文原样带出。翻不了的不假装能翻。
                            "reason": result.message or "对方没有说明原因",
                        },
                        (
                            f"节点 {prepared.node_id} 的活没派出去"
                            f"（连 {RETRY_MAX_ATTEMPTS} 次都没成），判定失败"
                        ),
                    ),
                )
                return False

            # **② 其余未知**（连不上 / 超时 / 响应不是 JSON / 429），③ 段的重发预算也用尽了
            # → **一个字都不改**，子任务留在 `running`，由超时巡检兜底（§7.4）。
            #
            # 不判死：运行方可能**已经在跑了**（请求其实到了，只是回包丢了或来晚了），
            # 判 `failed` 会把那一步做成孤儿 —— 它之后上报成功也会被终态闸丢掉，等于把
            # 一次成功记成失败。这跟「运行方没响应」是同一个兜底口径。
            # 也不回退 `pending`：那会每 3 秒重试一次、永远重试下去。
            logger.warning(
                "[TaskCenter] 派活没够着运行方 node=%s capability=%s（重发已用尽），"
                "子任务留在 running 等超时巡检：%s",
                prepared.node_id,
                prepared.capability_id,
                result.message,
            )
            return False

        now = datetime.now()
        if result.disposition is DispatchDisposition.ACCEPTED:
            # 有子任务真派出去了 → 任务（连同这次运行）转「跑着」（§7.2）。
            await self._mark_run_started(ctx, now)
            return True

        # `REJECTED`：运行方**明确收不了**（回执 `success=false`，含「不认识的 `step_code`」）
        # → 该子任务直接置终态 `failed`（终态、**不重派**，下游一律 hold，§4.1 ①、§6.3）。
        # 这一档**不重发**：运行方已经表了态，再发一百次还是这句话。
        logger.warning(
            "[TaskCenter] 派活被运行方拒收 node=%s capability=%s: %s",
            prepared.node_id,
            prepared.capability_id,
            result.message,
        )
        await self._fail_subtask(
            ctx,
            subtask,
            **build_fail_reason(
                "err.task_center.subtask.dispatch_failed",
                {
                    "node_id": prepared.node_id,
                    "capability_id": prepared.capability_id,
                    # 文案里 `{reason}` 是嵌在括号里的，别让它是空的——运行方明确回了
                    # `success=false` 却没带 `message` 时给一句兜底，别印出个空括号。
                    # ⚠️ 它是**运行方那侧的原文**（对方可能用另一国语言的消息键渲染过）——
                    # 放进**参数**里，所以外层可翻译、这段原样带出（翻不了的别假装能翻）。
                    "reason": result.message or "运行方未说明原因",
                },
                (
                    f"节点 {prepared.node_id} 的活没派出去"
                    f"（运行方 {prepared.capability_id} 没收下），判定失败"
                ),
            ),
        )
        return False

    async def _fail_subtask(
        self,
        ctx: RunContext,
        subtask: Subtask,
        error_message: str,
        *,
        error_code: Optional[str] = None,
        error_params: Optional[Dict[str, Any]] = None,
    ) -> None:
        """把一个子任务就地判死：置终态 `failed`、收口历史行、**下游一律 hold**（§4.1 ①、§6.3）。

        用在「这条派活路走不通」的两种情形：运行方明确收不了、或这步压根没绑到在册的运行方。
        **都是终态、都不重派**——一期没有自动重试（§十二「待规划」），留在 `pending` 空转
        只会既占着队头、又让任务永远收不了口。`execution_id` 保留着，便于追溯（§7.5 ①）。

        **下游怎么处置：什么都不做**（§6.3 的固定语义）——不上改下游状态、不置跳过
        （一期也没有 `skipped`）、更不会被派活，它们原地留在 `pending`，由任务收 `failed`
        兜住。**唯一的出路是人工 `retry_subtask`** —— 重试那一刻上游若已转 `success`，
        下游自然就又能被派活了。**别在这里给下游写任何状态**：那会跟「失败只能靠重试救」
        这件事矛盾，也会把下游的 `updated_at` 推后、干扰防僵尸（§7.5 D2b）。

        `expect_statuses` 取内存里那个前驱状态（没绑运行方时是 `pending`、派活失败时是
        `running`），再配 `state_version` 一起 CAS：别处刚改过就整单作废，不覆盖别人的判定。

        **三个调用点全是后台判死**（运行方不在册 / 明确拒收 / 重发预算用尽），**都没有操作人**：
        汇总时 `tasks.updated_by` 一个字节都不动 —— 与 CLAUDE.md 那条「后台循环推进状态
        一律不写 `updated_by`」同一条纪律（最后一个自动巡检会把真正的操作人盖掉）。
        ⚠️ **2026-10-15 起「用户主动停某一步」不再走这里**（`_do_stop_subtask` 直接
        `_close_subtask_row` + `_aggregate`，因为它要一次置停**多行**）—— 所以本方法
        不再收 `operator_id`。那条路上的操作人由 `_do_stop_subtask` 自己带进 `_aggregate`。
        """
        failed = await self._close_subtask_row(
            subtask, error_message, error_code=error_code, error_params=error_params
        )
        if not failed:
            return
        logger.info(
            "[TaskCenter] 子任务判死 task=%s node=%s attempt=%s: %s",
            ctx.task.id,
            subtask.node_id,
            subtask.current_attempt,
            error_message,
        )
        # 下游 hold（**什么都不做**，§6.3）：直接汇总任务终态。
        await self._aggregate(ctx)

    async def _close_subtask_row(
        self,
        subtask: Subtask,
        error_message: Optional[str],
        *,
        error_code: Optional[str] = None,
        error_params: Optional[Dict[str, Any]] = None,
        now: Optional[datetime] = None,
        next_status: str = SubtaskStatus.FAILED.value,
    ) -> bool:
        """**单行落库**：把一个子任务就地收进某个终态（+ 收口历史行），**不汇总任务**。

        ## 两个终态共用它（`next_status`）

        - `failed`（默认）—— **判死**：这步绑的运行方不在册、派活被明确拒收，
          也就是「这条派活路走不通」的那些收场；
        - `stopped` —— **置停**（2026-10-15 加）：用户停整条任务、或停某一步时，
          那些**还没开始**的步骤写这个。它与 `failed` 分开，是为了让控制台上
          「已停止」与「已失败」不再长一个样（语义见 `models/enums.py` 的 `TaskStatus`）。

        两个终态在库里只差一个字面量：CAS 前驱、`now` 共用、历史行同刻收口三件事完全同构，
        所以**不给置停另开一个函数** —— 拆开等于把下面这些纪律再抄一遍。
        也正因为同构，`error_*` 三件在置停时**一律传 `None`**：被停不是一次失败，
        没有原因可写（写了读侧会渲染出「用户手动停止」，与状态标签说的是同一件事）。

        ## 为什么「不汇总任务」

        从 `_fail_subtask` 里拆出来的那一半，多一个调用方：`task.stop` 要一次置停**一批**
        子任务，汇总必须等这一批全落库之后**只做一次**。每收一行就汇总一次的话，
        `_aggregate` 会在批中途把任务收口、`error_message` 被拼成「节点 A: …; 节点 B: …」
        那种自动文案（见 `_settle_stopped`）。

        `now` 由调用方一次算好传进来：一批要共用同一个时刻，各取一次 `datetime.now()`
        会让同一次停止里几行子任务的 `finished_at` 各不相同。

        前驱两种都可能，**调用点是四个，每个各带一道自己的上游闸门**（别把某一道的结论
        套到别的调用点上 —— 它们方向不同）：

        - `pending` ← `claim_for_dispatch` 的「这步绑的运行方不在册」那一支（上游闸门
          要求扫到的就是 `pending`，不是就整个返回 `None` —— 见 `_resolve_runner_capability`
          与它下面那段判死）；
        - `pending` ← `task.stop` 批量置停，`_stop_targets` **只挑 `pending`**；
        - `pending` ← `stop_subtask` 停某一步（**含按 `downstream_on_stop` 连带停的下游**），
          那里先核过「必须是 `pending`」才调进来；
        - `running` ← `finalize_dispatch` 的**两支**（「运行方明确拒收」与
          「`DELIVERY_UNKNOWN` 用尽预算且带 `dead_after_retry`」），上游闸门都要求这一行
          还是本单抢占的那一行、状态为 `running`。

        所以历史行两个非终态前驱都收。

        ⚠️ **别拿上面这段去「收窄」下面的 `expect_statuses`**：它取的是内存里那个前驱
        状态（`previous = subtask.status`），照「只可能是 `running`」改会恰好把
        「运行方不在册」那条判死路径打空 —— CAS 命中 0 行 → 返回 `False` → 下游 hold、
        任务永远收不了口。


        返回是否真的改到了（命中 0 行 = 别处刚改过这行，整单作废、不覆盖别人的判定）。
        """
        previous = subtask.status
        now = now or datetime.now()
        changed = await self._subtasks.transition_cas(
            subtask.tenant_id,
            subtask.id,
            expect_statuses=[previous],
            next_status=next_status,
            expect_version=subtask.state_version,
            finished_at=now,
            error_message=error_message,
            # 三件**一起落**（快照 + 代号 + 参数）：只落快照就退化成"语言冻在写的这一刻"，
            # 而这正是这次要治的病。`None` 也要显式写 —— 收进终态是一次全新的写入，
            # 上一次尝试残留的代号/参数该被这次覆盖掉（置停那次传的就是三个 `None`）。
            error_code=error_code,
            error_params=error_params,
        )
        if not changed:
            return False
        _apply_local(
            subtask,
            status=next_status,
            finished_at=now,
            error_message=error_message,
            error_code=error_code,
            error_params=error_params,
        )
        # 历史行在同一时刻收成同一个终态（§7.2）。
        await self._sync_attempt_row(
            subtask,
            next_status,
            from_statuses=[
                SubtaskAttemptStatus.PENDING.value,
                SubtaskAttemptStatus.RUNNING.value,
            ],
            error_message=error_message,
            error_code=error_code,
            error_params=error_params,
            finished_at=now,
        )
        return True

    async def handle_timeout(self, subtask: Subtask, now: Optional[datetime] = None) -> bool:
        """跑超时了：置 `timeout`（终态）并收敛（§7.4）。返回是否处理了本次。

        超时判定读的是 `subtask.started_at` + `subtask.timeout_seconds`（扫描侧已筛过），
        这里只负责 CAS `running → timeout` 并汇总。**下游一律 hold**：既不上改状态、
        也不置跳过（一期没有 `skipped`）、更不会被派活（§6.3 固定就这一种处置），
        任务本身推 `failed`。**跟 `_fail_subtask` 是同一种收场**，唯一的区别是前驱状态
        与终态取值。

        **只处理任务没被软删的行**（扫描侧 `list_timed_out_for_patrol` 也照着筛）。

        **动手前补一次「让路」复核**（§7.5 ③）：扫描那一趟已经筛掉了「挂着待消化上报」
        的行，但扫到与动手之间隔着取锁、重读上下文 —— 这期间队列随时可能又收到一条上报。
        不补这一次复核，就会把一条**还在排队等消化的 `success`** 判成 `timeout`（终态），
        那条上报到了也只能丢：一步真跑成了、却被记成超时。命中的这一轮不判，下一轮
        （队列处理掉那条、`is_deleted` 置 1 之后）正常判 —— 让路是自解的。

        ⚠️ 但**不看 `Task.status`**：被「停止」的任务已经收成 `stopped`，那个正在跑的子任务
        杀不掉（§4.4：停止只发一条**通知**、一个 `running` 的行都不动），正常运行方跑完会上报收口 ——
        真卡死时就**只剩这一趟能兜住它**。加一道「任务得活跃」的闸，它就会永远挂着。

        ⚠️ **也不看「是不是当前运行」**（从前看）。旧运行遗留的 `running` 一样要收：它
        同样占着 `count_running_by_catalog_step` 数出来的并发名额（那个统计**只认目录码、
        `step_code` 与 `running`，不分运行**），而旧运行这一行已经没有任何别的出路 —— 派活只捞当前
        运行的 `pending`、防僵尸只捞 `pending`、控制台只列当前运行。不收就成了一个谁也
        看不见、却一直在吃名额的僵尸，攒够 `max_concurrency` 条就再也派不出这一步的活。
        典型来源：任务被「停止」时那个杀不掉的 `running`（§4.4），用户随后 `retry_task`
        换了运行。**汇总那一步不受影响** —— `_aggregate` 开头的闸只让当前运行写任务快照，
        所以这里仍然「只收它自己那一行」。
        """
        ctx = await self._load_context(
            subtask.tenant_id, subtask.task_id, subtask.task_attempt_id
        )
        if ctx is None:
            return False
        subtask = ctx.by_key.get(subtask.node_id)
        if subtask is None or subtask.status != SubtaskStatus.RUNNING.value:
            return False
        # ⚠️ 当前**恒为假**（`ctx.task` 由 `_load_context` 过滤过假删，见那里）；留着是防御。
        if ctx.task.is_deleted:
            return False

        # **让路复核（§7.5 ③）**：判据与扫描侧 `list_timed_out_for_patrol` 逐条同口径，
        # 见 `SubtaskReportRepository.has_pending_before`。放在 CAS **之前**、紧跟重读
        # 之后 —— 中间任何一次 await 都可能让队列收到新的一条。
        if subtask.started_at is not None:
            deadline = subtask.started_at + timedelta(
                seconds=subtask.timeout_seconds or 0
            )
            if await self._reports.has_pending_before(
                subtask.tenant_id,
                subtask.space_id,
                subtask.execution_id,
                received_before=deadline,
            ):
                # 这一轮不判。**不是「等它一下」的礼貌**：那条上报真跑成了，判死等于丢结果。
                # 让路自解 —— 队列消化或判死信都会把 `is_deleted` 置 1，下一轮正常判。
                return False

        now = now or datetime.now()
        reason_args = build_fail_reason(
            "err.task_center.subtask.timed_out",
            {"seconds": subtask.timeout_seconds},
            f"跑了超过 {subtask.timeout_seconds} 秒还没结果，判定超时",
        )
        changed = await self._subtasks.transition_cas(
            subtask.tenant_id,
            subtask.id,
            expect_statuses=[SubtaskStatus.RUNNING.value],
            next_status=SubtaskStatus.TIMEOUT.value,
            expect_version=subtask.state_version,
            finished_at=now,
            **reason_args,
        )
        if not changed:
            return False  # 别的副本已经判过了，重复巡检不致错（§7.5 ③）
        _apply_local(
            subtask,
            status=SubtaskStatus.TIMEOUT.value,
            finished_at=now,
            **reason_args,
        )
        await self._sync_attempt_row(
            subtask, SubtaskStatus.TIMEOUT.value, finished_at=now, **reason_args
        )
        logger.info(
            "[TaskCenter] 子任务超时 task=%s node=%s attempt=%s",
            ctx.task.id,
            subtask.node_id,
            subtask.current_attempt,
        )

        # 下游 hold（§6.3 固定「保持原样」），直接汇总。
        await self._aggregate(ctx)
        return True

    async def handle_stuck_pending(
        self, subtask: Subtask, now: Optional[datetime] = None
    ) -> bool:
        """**防僵尸**：一个「就绪的 `pending`」卡太久没派出去 → 置 `timeout`（终态）并收敛（§7.5 D2b）。

        判据由扫描侧把关（`list_stuck_pending`：`现在 − subtasks.updated_at >
        timeout_seconds × TASK_CENTER_PENDING_STUCK_TIMEOUT`，倍数默认 3），这里只负责
        CAS `pending → timeout`。

        **为什么必须有这一步**：就绪的 `pending` 因为并发上限被别的任务长期占满名额时，
        它既不 `running`（超时巡检只扫 `running`、管不到 `pending`）、也没有终态 →
        收尾判定永远认为「还有能跑的」，任务永远 `running`；而 `task.delete` /
        `retry_task` / `retry_subtask` **全都要求任务已终态** → 这条任务彻底无法处置。

        **扫描侧必须复用与派活完全一致的 `_runnable_pending_stmt()`**（依赖已满足 +
        任务在跑），否则会把「上游失败留下的、故意 hold 的 `pending`」也判超时——
        那等于悄悄改掉 §6.3 的 hold 语义。**这里只处理扫得到的行**，不自己重判。

        `expect_statuses` 只给 `pending`：别处刚把它派出去（转 `running`）就命中 0 行，
        整单作废、不覆盖别人的派活。
        """
        ctx = await self._load_context(
            subtask.tenant_id, subtask.task_id, subtask.task_attempt_id
        )
        if ctx is None:
            return False
        subtask = ctx.by_key.get(subtask.node_id)
        if subtask is None or subtask.status != SubtaskStatus.PENDING.value:
            return False
        # ⚠️ 前半（`ctx.task.is_deleted`）当前**恒为假**，见 `_load_context`；后半是真判据。
        if ctx.task.is_deleted or ctx.attempt.attempt != ctx.task.current_attempt:
            return False

        now = now or datetime.now()
        # 「倍数」跟扫描侧（`list_stuck_pending`）读的是**同一个配置项**：阈值必须处处一致，
        # 否则会出现「扫出来了、对不上话」或者反过来漏判。
        multiplier = get_config().TASK_CENTER_PENDING_STUCK_TIMEOUT
        stuck_after = subtask.timeout_seconds * multiplier
        reason_args = build_fail_reason(
            "err.task_center.subtask.pending_stuck",
            {"seconds": stuck_after},
            f"就绪后超过 {stuck_after} 秒一直没派出去，判定超时",
        )
        changed = await self._subtasks.transition_cas(
            subtask.tenant_id,
            subtask.id,
            expect_statuses=[SubtaskStatus.PENDING.value],
            next_status=SubtaskStatus.TIMEOUT.value,
            expect_version=subtask.state_version,
            finished_at=now,
            **reason_args,
        )
        if not changed:
            return False  # 别的副本 / 派活刚好动过它，本轮作废（§7.5 ①）
        _apply_local(
            subtask,
            status=SubtaskStatus.TIMEOUT.value,
            finished_at=now,
            **reason_args,
        )
        # 历史行还在 `pending`（这次尝试压根没跑起来），收成同一个终态（§7.2）。
        # **前驱必须显式带上 `pending`**：`_sync_attempt_row` 默认只认 `running`（那是
        # 「跑着的时候被收口」的通行情形），而这条路的子任务上面刚卡过闸门
        # `status != PENDING → return`，所以尝试行必定还是排队态 —— 不传的话那条
        # UPDATE 命中 0 行，留下 `subtasks.timeout` / `subtask_attempts.pending` 的
        # 畸形快照（子任务已死、历史行还在排队）。
        # 连 `running` 一起收是照派活失败判死那条路的做法（见 `finalize_dispatch` 的
        # REJECTED 分支）：
        # 两个都是非终态前驱，收成都不会盖掉真实结果。
        await self._sync_attempt_row(
            subtask,
            SubtaskStatus.TIMEOUT.value,
            from_statuses=[
                SubtaskAttemptStatus.PENDING.value,
                SubtaskAttemptStatus.RUNNING.value,
            ],
            finished_at=now,
            **reason_args,
        )
        logger.warning(
            "[TaskCenter] 就绪的 pending 卡死了 task=%s node=%s attempt=%s",
            ctx.task.id,
            subtask.node_id,
            subtask.current_attempt,
        )

        # 立刻重判一次收尾（D2b：`running` 分支回到 D2、`pending` 分支走 C7 —— 也就是
        # 「一到终态就走一次收尾」这条总则，跟它原本的触发原因无关）。
        await self._aggregate(ctx)
        return True

    # ========================================================
    # 内部：上下文与建树
    # ========================================================

    async def _load_context(
        self, tenant_id: str, task_id: str, task_attempt_id: str
    ) -> Optional[RunContext]:
        """按「某一次运行」把上下文凑齐（编排快照 / 那一次的整套子任务）。

        **编排从任务行上的快照读，不反查版本与定义**（理由见 `Task.definition_config`
        的说明）—— 那两张表都会被「删定义」带成假删，反查等于把「管理员删了个定义」
        变成「还在跑的任务永远收不了尾」。快照是建任务那一刻抄的，跟着任务走。

        ⚠️ **`ctx.task` 一定是没过假删的那一行**（`get_task` → `get_by_id` 走`_soft_delete_conditions()`），所以下游拿 `ctx.task.is_deleted` 判的守卫**当前恒为假**（`claim_for_dispatch` / `handle_timeout` / `handle_stuck_pending` 三处）—— **留着是防御**（这条读法哪天改成不过滤就立刻生效），不是活判据。
        """
        task = await self._tasks.get_task(tenant_id, task_id)
        if task is None:
            return None
        attempt = await self._attempts.get_by_id(task_attempt_id, tenant_id)
        if attempt is None:
            return None
        subtasks = await self._subtasks.list_by_attempt(tenant_id, task_attempt_id)
        catalog_steps = await self._catalog_steps.map_by_step_code(
            task.task_catalog_code or ""
        )
        return RunContext(
            task=task,
            attempt=attempt,
            config=task.definition_config or {},
            catalog_steps=catalog_steps,
            subtasks=subtasks,
        )

    async def _build_context(
        self,
        *,
        tenant_id: str,
        task: Task,
        attempt: TaskAttempt,
        config: Dict[str, Any],
    ) -> RunContext:
        """为**新的一次运行**建一整套子任务 + 各一条排队历史，并返回它的上下文。

        子任务**一诞生**就先插一条 `attempt=1` 的 `pending` 历史行（§7.2）：这样「什么
        时候进的队列」从这一刻就有据可查，派活排队排序（§6.3）用的正是它的 `created_at`。

        **身份是 `node_id`，`step_code` 只用来查内置清单**（§3.5）：`subtask_name` 按
        `step_code` 去 `task_catalog_steps` 取人名，`node_id` 原样落库做节点身份。
        """
        # 内置清单编码取任务行上的快照（建任务时抄的），不反查定义 —— 理由同
        # `Task.task_catalog_code` 的说明。
        catalog_steps = await self._catalog_steps.map_by_step_code(
            task.task_catalog_code or ""
        )

        rows = []
        for step in config.get("steps") or []:
            node_id = step.get("node_id")
            if not node_id:
                continue
            step_code = step.get("step_code")
            catalog_step = catalog_steps.get(step_code) if step_code else None
            rows.append(
                {
                    "tenant_id": tenant_id,
                    "space_id": task.space_id,
                    "task_id": task.id,
                    "task_attempt_id": attempt.id,
                    "node_id": node_id,
                    "step_code": step_code,
                    "subtask_name": catalog_step.name if catalog_step else step_code,
                    "status": SubtaskStatus.PENDING.value,
                    "depends_on": step.get("depends_on") or [],
                    "next_steps": step.get("next_steps") or [],
                    "sort": step.get("sort"),
                    "timeout_seconds": _timeout_of(step, catalog_step),
                    "inputs": {},
                    "current_attempt": 1,
                    "state_version": 0,
                }
            )
        subtasks = await self._subtasks.create_subtasks(rows)
        now = datetime.now()
        await self._subtask_attempts.create_attempts(
            [
                {
                    "tenant_id": tenant_id,
                    "space_id": task.space_id,
                    "task_id": task.id,
                    "subtask_id": subtask.id,
                    "attempt": subtask.current_attempt,
                    "execution_id": None,
                    "status": SubtaskAttemptStatus.PENDING.value,
                    "created_at": now,
                }
                for subtask in subtasks
            ]
        )

        return RunContext(
            task=task,
            attempt=attempt,
            config=config,
            catalog_steps=catalog_steps,
            subtasks=subtasks,
        )

    async def _fill_inputs(self, ctx: RunContext, subtask: Subtask) -> None:
        """按 `bind_inputs` 算出这步的真实入参，落库（§6.2）。

        跟「上游产出落库」在同一个事务里：上游一到成功/跳过，紧跟着就把因它而就绪的
        下游填上，读的人看不到「引用了但没填」的半成品。
        """
        step = ctx.steps.get(subtask.node_id)
        if step is None:
            return
        # schema 按 `step_code` 查（同一个步骤被多个节点复用，schema 是步骤级的、不是节点级的）。
        catalog_step = ctx.catalog_steps.get(subtask.step_code)
        if catalog_step is None:
            # 这一步在**内置清单**里没有定义 —— 编排（建任务时的快照）跟清单漂了：清单里的
            # 步骤名下线 / 改名之后，已发布版本里指向旧名的 `bind_inputs` 走的就是这条路。
            # **发版校验拦不住这种情况**：N21 只在**发版那一刻**比对当时的清单，发过版的编排
            # 不会再被校验一次。
            #
            # 后果分两层，**一静一响**，别只看第一层：
            #
            # 1. **静默的那层**：`evaluate_bindings` 收不到 schema，而
            #    `normalize_by_schema` **只装 schema 声明过的键**，算出来的值会被整批收口
            #    成 `{}`（连常量都留不下一个）；
            # 2. **响亮的那层**：派活那一步按**同一个 `step_code` 查同一份清单**
            #    （`_resolve_runner_capability`），查不到就把这条子任务**就地判死**
            #    （`runner_unbound`「没绑到在册的运行方，无法派活」）—— 所以那个空 `{}`
            #    **到不了运行方**，用户看到的是任务收成 `failed`。
            #
            # 既然第二层已经响亮，这里为什么还要吼：**不是每个节点都会走到派活**。卡在失败
            # 上游后面的节点两条路都不经过（填值只在依赖满足时发生），漂移会一条日志都没有。
            # ⚠️ 下面这句别读成「运行方会收到一份空入参」—— 它指的是这一列被收口成了空值。
            logger.warning(
                "[TaskCenter] 步骤 %s（节点 %s）在内置清单 %s 里没有定义，"
                "本步入参将按无 schema 收口（值可能整批丢空）；"
                "该步在派活时也会因同一原因判死: task=%s",
                subtask.step_code,
                subtask.node_id,
                ctx.task.task_catalog_code,
                ctx.task.id,
            )
        inputs = evaluate_bindings(
            step,
            task_inputs=ctx.task.inputs or {},
            upstream_outputs=ctx.upstream_outputs(subtask),
            step_sort=ctx.step_sort(),
            input_schema=getattr(catalog_step, "input_schema", None),
        )
        subtask.inputs = inputs
        # 显式推一次时间。`inputs` 算出来跟库里**完全相同**时（典型是两边都是 `{}`：该步
        # 既没有 `bind_inputs` 也不引用上游产出），SQLAlchemy 判定属性无变化、不会发
        # UPDATE，`TimestampMixin.updated_at` 的 `onupdate` 也就不触发。而防僵尸
        # （§7.5 D2b）的判据正是「现在 − `subtasks.updated_at` > `timeout_seconds` × 倍数」，
        # 设计文档把这一列定义为「**变成就绪的时刻**」—— 不推时间，这个倒计时就从
        # 「行创建时刻」起算，会把一步刚就绪的 `pending` 提前判成僵尸。
        subtask.updated_at = datetime.now()
        await self.session.flush()

    async def _fill_downstream(self, ctx: RunContext, subtask: Subtask) -> None:
        """本步成功后，把因此变就绪的下游填上值（§6.3 扇出激活）。

        **判据只有两条：还是 `pending`、依赖都满足了。** 两条都过就**重算一遍**，不看
        它是不是已经有 `inputs`。

        ⚠️ **这里从前还有第三条 `if node.inputs: continue`（已经有就别重算），是错的** ——
        它假定「一个节点的 `inputs` 只会被算一次」，而**重试把这个假定打破了**：
        上游 A 跑成功 → 下游 B 填上 A 的产出 → 用户重跑 A（对产出不满意，§4.3
        `task.subtask_retry`）→ A 带着**新**产出再次成功 → 走到这里。此时 B 还 `pending`
        （没派出去），`if node.inputs` 命中 → **跳过** → B 拿着 A **上一轮**的产出跑。
        用户重跑上游本来就是嫌上一轮的产出不对，结果下游照样按上一轮跑，重跑白跑。

        去掉之后不会多算：正常流程里一个上游只会成功一次，而下游要等**所有**上游
        `success` 才够得着 `deps_satisfied` —— 也就是说它**本来只会被填一次**（填它的那次
        就是最后一个上游成功的那次）。换句话说那条闸在正常流程里从来没生效过，
        只在重试这条路上生效，而生效的方向恰好是反的。

        **`pending` 且已经派过活的不会被误伤**：派出去的节点状态已经离开 `pending`，
        第一条判据就把它挡住了（`_fill_inputs` 只写 `inputs`，改不了已经在跑的那份）。

        **已经有 `success` 的下游不会因为这里被重算**（状态不是 `pending`，直接跳过）——
        「上游重跑后下游要不要跟着跑」是调用方的事：`retry_subtask` 会把依赖它的下游
        一起重置回 `pending` 重来（2026-10-11 起，见那里的 docstring），整条
        `retry_task` 则是全量重来。
        """
        for node in ctx.downstream_closure(subtask):
            if node.status != SubtaskStatus.PENDING.value:
                continue
            if ctx.deps_satisfied(node):
                await self._fill_inputs(ctx, node)

    # ========================================================
    # 内部：并发上限与运行方
    # ========================================================

    async def _over_concurrency(self, ctx: RunContext, subtask: Subtask) -> bool:
        """这一步在**本目录内**的在跑数是否已达 `max_concurrency`（§6.3）。没配就是不限。

        **统计口径＝`task_catalog_code` + `step_code`**（2026-10-14 改，原文写的是
        「全局只看 `step_code`」）：限流管的是「**本目录里这类活**最多同时几个」。

        改的原因实测过：`file_processing_jonex_wiki` 的 10 个 `compile` 卡住不结束，
        **另一个目录 `file_processing_ontology` 的 `compile` 就一个都派不出去** ——
        两个目录互不相干，却共用一份名额，队头一堵全体饿死。改完两个目录各有一份。

        **同目录内不变的那部分**：同一个 `step_code` 挂在多个节点上
        （`parse_1` / `parse_2`）**仍然共享**这一个上限，跟 `node_id` 无关。

        上限**数值**取本节点所属目录配的那个：`ctx.catalog_steps` 是按本任务的
        `task_catalog_code` 查出来的（`_load_context` / `_build_context` 都这么装），所以
        `catalog_steps.get(step_code)` 拿到的正是它自己那一行的 `max_concurrency`。

        ⚠️ **总量会翻**：全平台最大并发从「N」变成「Σ(每个目录各 N)」，这是明确接受的
        代价（上限数值仍按目录各配 10）。

        ⚠️ **它是「软上限」：多副本下最多可被超出「副本数 − 1」条**。判据是「数 → 判 →
        抢占」三步，而抢占那条 UPDATE（`claim_pending`）里**没有任何并发数条件**；两个副本
        各自扫到**不同任务**的同一步时拿的是两把不同的任务锁（`task_lock` 按 `task_id`
        串行化），互不阻塞，于是可能双双读到「还差一个名额」并都提交。**这是已知的取舍，
        不是漏判**：真要做成硬上限，得在 `step_code` 上取一个事务级 advisory lock 把
        「数 + 抢占」串起来（只把计数塞进 UPDATE 的 WHERE **不够** —— READ COMMITTED 下两个
        事务的快照相同，照样双双通过）。今天的部署画像里任务中心是单副本，穿不到。

        ⚠️ **`max_concurrency` 配成 `0` / 负数等于「不限」，不是「全停」**（DDL 列注释写的
        是「空 = 不限」）。真要临时停派某一步，别置 `0` —— 那会得到**相反**的效果。
        """
        catalog_step = ctx.catalog_steps.get(subtask.step_code)
        limit = getattr(catalog_step, "max_concurrency", None) if catalog_step else None
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            return False
        catalog_code = ctx.task.task_catalog_code
        running = await self._subtasks.count_running_by_catalog_step(
            subtask.step_code, catalog_code
        )
        if running >= limit:
            logger.info(
                "[TaskCenter] 目录 %s 的步骤 %s 并发已达上限 %d（在跑 %d），本轮跳过",
                catalog_code,
                subtask.step_code,
                limit,
                running,
            )
            return True
        return False

    async def _resolve_runner_capability(
        self, ctx: RunContext, subtask: Subtask
    ) -> Optional[str]:
        """按步骤绑定的运行方换出对外能力 ID（§4.1 ①）。**按 `step_code` 查内置清单**。"""
        catalog_step = ctx.catalog_steps.get(subtask.step_code)
        if catalog_step is None:
            return None
        try:
            return await self._runners.resolve_capability_id(catalog_step.runner_code)
        except InvalidParameterError as exc:
            # **只有「这个 `runner_code` 不在内置清单里」才是判死的理由** ——
            # `require_runner` 唯一主动抛的就是它（1001）。
            #
            # ⚠️ **别把它放宽成 `except Exception`**：那样这条链上那句 SELECT 撞上的
            # DB / 连接错也会被当成「运行方不在册」，于是调用方（`claim_for_dispatch`）
            # 把这条子任务**永久判死**（终态不可逆、下游全 hold，只能人工重试救）——
            # 基础设施抖一下不该有这个后果。同模块开头自己写着「本轮跳过的理由只有三种
            # （别副本先抢到 / 并发到顶 / 依赖还没满足）」，基础设施错不在其中；
            # 隔壁 `finalize_dispatch` 更是在「运行方可能已经在跑」时**特意拒绝**判死，
            # 方向一致。让别的异常照常往上冒，这一轮派活作废、下一轮重扫即可。
            logger.warning(
                "[TaskCenter] 节点 %s（步骤 %s）的运行方 %s 不在册: %s",
                subtask.node_id,
                subtask.step_code,
                catalog_step.runner_code,
                exc,
            )
            return None

    async def _runner_capabilities(
        self, task_catalog_code: str, config: Dict[str, Any]
    ) -> List[str]:
        """这份编排用到哪些运行方（去重后的 capability_id 列表）。

        **内置清单编码由调用方给**（建任务时用定义的、删任务回落时用任务行上的快照），
        本方法不反查 —— 反查在这条路上会断（删定义之后定义和版本一起取不到）。

        运行方不在册的跳过 —— 没有可删的目标，不该因此拦下删除。编排里引用了清单里
        没有的 `step_code` 同理（`catalog_step is None`）。
        """
        catalog_steps = await self._catalog_steps.map_by_step_code(
            task_catalog_code or ""
        )
        capabilities: List[str] = []
        for step in config.get("steps") or []:
            catalog_step = catalog_steps.get(step.get("step_code"))
            if catalog_step is None:
                continue
            try:
                capability_id = await self._runners.resolve_capability_id(
                    catalog_step.runner_code
                )
            except Exception as exc:
                logger.warning(
                    "[TaskCenter] 运行方 %s 不在册: %s", catalog_step.runner_code, exc
                )
                continue
            if capability_id and capability_id not in capabilities:
                capabilities.append(capability_id)
        return capabilities

    async def _runner_capabilities_for_task(self, tenant_id: str, task: Task) -> List[str]:
        """`task.delete` 要请哪些运行方清掉自己侧的残留（§4.3）。

        **优先读任务上那份快照**（`tasks.runner_capability_ids`，建任务时钉的）。为什么不
        在这儿现推：**删定义会把它的版本全部假删**（`def.delete`，见 `TaskDefinitionRepository`），
        而 `get_version` / `get_visible` 都把 `is_deleted=1` 当「找不到」—— 于是
        「先删定义、再删已经到终态的任务」这条完全正常的顺序下，这里推出来的是空列表，
        下面 `delete_task` 那个 for 一次都不进：**一次 `remove_task` 都发不出去**，
        管理方把任务删干净了、运行方侧的残留（文档 / 向量 / 图 / 原文件）一件没清，
        而且两边都不报错。快照让这条路径不再依赖定义还在不在。

        快照为空（这一列是后来才加的、老行没有）才回落到按编排快照现推。**推不出 / 推出来
        却用不上，都得吼一声** —— 这时确实没法知道该找谁清，但沉默着把任务删掉才是最坏的
        结果。三种成因，都**不涉及**版本（回落路径不查版本，见下面那行注释）：

        1. 编排快照本身是空的（`definition_config` 缺失 —— 那一列也是后加的）；
        2. 快照里有步骤，但它们的 `step_code` 在**内置清单**里一个都找不到
           （`_runner_capabilities` 里 `catalog_step is None` 那一支）；
        3. 推出来的 `capability_id` **现在本地定位不到**（清单里这一条没了 / 清单说 LOCAL
           却没注册进本进程）—— 见 `_filter_dispatchable`，**快照路径与回落路径都会走这一道**。

        第 2 条是**最像会咬人的**那条：编排快照指着一个已经从清单里下线的步骤名时，
        表现和「本来就没有运行方」一模一样（空列表、不报错、任务照删），而运行方侧的
        残留一件都没清。
        """
        snapshot = [c for c in (task.runner_capability_ids or []) if c]
        if snapshot:
            return self._filter_dispatchable(snapshot, task.id)

        # 回落：读任务行上的编排快照现推。**不查版本** —— 那条路正是快照要绕开的（定义
        # 被删之后版本一并假删，现查只会得到「找不到」）。
        config = task.definition_config or {}
        if not config:
            logger.warning(
                "[TaskCenter] 删任务既没有运行方快照、也没有编排快照，**本次不会清理运行方残留**: "
                "task=%s def=%s ver=%s",
                task.id, task.definition_id, task.definition_version_no,
            )
            return []
        capabilities = await self._runner_capabilities(task.task_catalog_code, config)
        if not capabilities:
            # 第 2 种成因（见 docstring）：编排快照里有步骤，却一个 capability_id 都解
            # 不出来 —— 多半是那些 `step_code` 已经从内置清单里下线了。**必须吼**：
            # 不吼的话「运行方残留一件没清」和「本来就没运行方」长得一模一样。
            logger.warning(
                "[TaskCenter] 删任务按编排快照推不出该请哪些运行方清残留"
                "（快照里 %d 个步骤，在内置清单 %s 里一个都没对上）"
                "——**本次不会清理运行方残留**: task=%s",
                len(config.get("steps") or []),
                task.task_catalog_code,
                task.id,
            )
            return []
        return self._filter_dispatchable(capabilities, task.id)

    @staticmethod
    def _filter_dispatchable(capability_ids: List[str], task_id: str) -> List[str]:
        """把**本地已经定位不到**的运行方挑出去：记 warning、**照常往下删**（§4.3 K1b）。

        判据在 `has_dispatch_target` —— **只有「清单说 LOCAL、本进程却没有这个能力」这一种
        本地就判得出来**（REMOTE 一律算「有」，它可达不可达只能靠真去调）。

        **为什么是跳过、不是中止**：一个下架的运行方**没有任何可调的对象**，把它算成
        「清不掉」会让**所有**快照里含它的任务**永久删不掉**（每次都是失败，重发一百次
        一样）；而回落路径遇到同类情况（运行方不在册）本来就是跳过的 —— 两条路不能一个
        跳、一个不跳。

        ⚠️ 「跳过」= **它那边的残留这次不清**，所以要吼一句。整批都跳光时这里不额外吼，
        由调用方补那条汇总 warning（「本次不会清理运行方残留」）。
        """
        kept: List[str] = []
        for capability_id in capability_ids:
            if has_dispatch_target(capability_id):
                kept.append(capability_id)
                continue
            logger.warning(
                "[TaskCenter] 删任务时运行方 %s 现在定位不到（不在 capability_runtime 清单 / "
                "清单说 LOCAL 却不在本进程注册中心），**跳过它、它那边的残留这次不清**: task=%s",
                capability_id,
                task_id,
            )
        if capability_ids and not kept:
            logger.warning(
                "[TaskCenter] 删任务要请的运行方**一个都定位不到**（%d 个全部跳过）"
                "——**本次不会清理运行方残留**: task=%s",
                len(capability_ids),
                task_id,
            )
        return kept

    # ========================================================
    # 内部：汇总
    # ========================================================

    async def settle_stale_task(self, tenant_id: str, task_id: str) -> bool:
        """补收口一条「该收却没收到口」的任务；返回**这次有没有真的把它收成终态**（§7.5）。

        扫描侧负责找出候选（`TaskRepository.list_tasks_needing_settle`），这里只负责
        「拿上下文 → 交给 `_aggregate`」。**刻意不另写一套终态判定** —— 那会把「终态怎么算」
        复制到第二个地方，两处迟早分叉（`_run_settled` 与 §6.3 的 hold 语义都住在那边）。

        ⚠️ **本方法仍可能为「看了不用管」的任务空跑**（返回 `False`），所以**不在这里**
        自己再判一遍终态 —— 闸门在 `_aggregate` 里：① 任务快照已是终态就返回、
        `_run_settled` 为假就返回。

        什么时候会空跑：**只有「扫描到动手之间状态被并发改过」这一种**（扫描那一步要的是
        「当次运行那一行**已经收口**、且一个 `running` 都没有」，所以进得来的时候
        `_run_settled` 恒真、闸① 只在任务已被别人收口时才拦）。
        ⚠️ **别把判据改成「有 `failed` 就进来」—— 那正是 2026-09-29 试过又撤掉的那一支**：
        它会把「下游被停掉、上游还在排队」这种**完全健康**的形态每轮扫进来拿一次任务锁
        （跟派活抢同一把），而那种任务本来就该继续跑。
        ⚠️ **「刚建好、正等派活」那种任务进不了候选** —— 它的当次运行那一行还是
        `pending` / `running`，而候选要的是「运行行**已经收口**」（早先这里写的是
        「它也会被扫到」，是候选查询收紧前的口径）。
        ⚠️ **当次运行还剩被上游 hold 住的 `pending` 也进得来**（2026-10-09 起）——
        那正是残格最常见的子形态；从前那条「当次运行一个 `pending` 都没有」把它挡在外面，
        只能靠用户点「停止」。

        调用方必须**持任务锁**（与另外四条收口路同一条纪律：`tasks` 行的写入者都在锁内）。
        """
        task = await self._require_task(tenant_id, task_id)
        attempt = await self._attempt_of(tenant_id, task, None)
        ctx = await self._load_context(tenant_id, task.id, attempt.id)
        if ctx is None:
            return False
        before = ctx.task.status
        await self._aggregate(ctx)
        # `_aggregate` 最后那步改的就是 `ctx.task` 这个实例（`get_task_fresh` 带
        # `populate_existing`，刷新的还是会话身份映射里那一份），所以直接比状态即可。
        return ctx.task.status != before

    async def _aggregate(self, ctx: RunContext, *, operator_id: Optional[str] = None) -> None:
        """汇总「当前运行」的子任务，按 §7.2 那张收尾表判定任务终态。

        先看「还能不能推进」，推不动了才定终态：

        | 情况 | 判定 |
        | --- | --- |
        | 有任意子任务 `running` | 还在跑，保持 `running` |
        | 无 `running`，但有**能跑的 `pending`**（① 首次就绪的、② 重试中的） | 还有活没派出去，保持 `running` |
        | 再没有，且有 `failed` / `timeout` | 收敛为 `failed`（带上原因） |
        | 再没有，其余全是 `success` | 收敛为 `completed` |

        两条要点：**「还能推进」看的是「能跑的 `pending`」** —— 上游失败 / 超时之后
        被 **hold** 下来的那些 `pending`（依赖永远不满足）不算「能跑」，所以它们不会把任务
        永远卡在 `running`；**`timeout` 算失败**（一期没有 `skipped` 这个状态，§6.4）。

        能跑到这里就意味着「没有 `running`、也没有能跑的 `pending`」了 —— 剩下的
        `pending` 只可能是被 hold 的（§6.3），所以上面的表一定能落到 `failed` 或
        `completed` 二者之一。

        **收口必须先串行化，再重读**（§7.5「CAS / 幂等收敛在多副本下依然成立」）。同一
        运行的两个子任务几乎同时上报时，两个事务会各开一个会话、各自读到「对面那个兄弟
        还在 `running`」，双双判定「还没收敛」而放弃收口——此后没有任何机制再汇总（派活
        扫描只捞 `pending`、超时巡检只捞 `running`，都扫不到它），任务就永远停在
        `running`，而重跑 / 删除都要求任务终态，只能人工改库。做法是两步：先锁住「本次
        运行」那一行把收口串起来，再绕过会话身份映射重读子任务。
        """
        # ⓪ **只有「当前运行」有资格汇总任务**（§4.4 停止 / §7.5）。
        #
        #    旧运行的行今天确实还会被两条路推到终态：迟到很久的上报（`_absorb` 会放行、
        #    只收它自己那一行）、超时巡检（`list_timed_out_for_patrol` 不再按当前运行筛）。
        #    那两条路都需要走到本方法末尾才算「收口」，而本方法最后一步 `_update_task_snapshot`
        #    改的是**任务快照那一行**（`tasks` 是全局唯一一行，不分运行）——旧运行一旦走到
        #    那里，就会拿一次作废运行的汇总去改**当前**任务：典型是把正在跑的新一轮
        #    凭空改成 `failed`。所以闸门放在最前面：**不取锁、不重读、直接返回**。
        #
        #    被「停止」的任务同样受益：`_settle_stopped` 早就把运行行收了口，下面那道
        #    `locked.status` 闸本来就拦得住 —— 这里只是把「谁有权改任务」提前说成一句判据，
        #    不再借「运行行已终态」这个副作用来挡。
        if ctx.attempt.attempt != ctx.task.current_attempt:
            logger.info(
                "[TaskCenter] 旧运行不汇总 task=%s 本行运行=#%s 当前运行=#%s"
                "（只收它自己那一行）",
                ctx.task.id,
                ctx.attempt.attempt,
                ctx.task.current_attempt,
            )
            return

        # ① 任务快照已经是终态 → 没有可补的，直接返回（**幂等**）。
        #
        #    这道闸是给「补收口」那条入口（`settle_stale_task` / 后台的补收口趟次）用的，
        #    正常收口路上它恒为假。不加它的话，那一趟会每轮对**已经收好的**任务再写一遍
        #    快照 —— 白推 `state_version`，还可能把已经定下的 `error_message` 重算成别的样。
        if ctx.task.status in TASK_TERMINAL_STATUSES:
            return

        # ② 抢「本次运行」的行锁。先到的把活干完、提交后才释放；后到的拿到锁时对方
        #    必然已提交，于是看得见对方的终态，由它完成收口。抢不到只是等，不是失败。
        locked = await self._attempts.get_for_update(ctx.tenant_id, ctx.attempt.id)
        if locked is None:
            return
        # 锁到手了还要看它是不是已经收过口。
        #
        #    ⚠️ **「运行行已收口」不等于「直接返回」** —— 运行行收了、任务快照没跟上，
        #    是一个**真实存在**的状态（收口那一刻任务快照那次 CAS 落空会留下它，见 ④ 段）。
        #    那种状态下任务会**永远**停在非终态：除「补收口」外的四趟一趟都不碰它（派活找
        #    当前运行的 `pending`、巡检找 `running`、防僵尸找当前运行的 `pending`、上报要有
        #    人上报），而 `retry_*` / `task.delete` / `task.stop` 三条路也各自拒它 ——
        #    只能靠补收口那一趟（或人工改库）把它捡回来。
        #
        #    所以这里改成「**记住运行行收过口、跳过 ④ 那次写（`finish_attempt_cas`）、
        #    但继续把任务快照补上**」。
        #    补的那条路由后台的「补收口」趟次（`_settle_pass`）驱动，见 `settle_stale_task`。
        run_already_closed = locked.status not in (
            AttemptStatus.PENDING.value,
            AttemptStatus.RUNNING.value,
        )

        # ③ 重读子任务（`refresh=True` 绕过身份映射）。不刷新的话，读到的还是
        #    `_load_context` 当时装进会话的那份旧对象，照样判不出收敛。
        subtasks = await self._subtasks.list_by_attempt_run(
            ctx.tenant_id, ctx.task.id, ctx.attempt.id, refresh=True
        )
        if not _run_settled(subtasks):
            return

        failed = [s for s in subtasks if s.status in SUBTASK_FAILED_STATUSES]
        # ⚠️ **`stopped` 单独一档**（2026-10-15 加，插在失败与成功之间）：
        #    它既不在失败那一族里（`SUBTASK_FAILED_STATUSES` 刻意不收它），
        #    也不该被当成「跑完了」——「这条没跑完、是被人停的」正是用户要看到的结论。
        #    顺序有意义：**有失败就是失败**（一条链里既有停的又有挂的，看的人最需要先知道
        #    「挂了」那件事），否则有停的就是停止，都没有才是完成。
        stopped = [s for s in subtasks if s.status == SubtaskStatus.STOPPED.value]
        next_status = (
            TaskStatus.FAILED.value
            if failed
            else TaskStatus.STOPPED.value
            if stopped
            else TaskStatus.COMPLETED.value
        )
        error_message = None
        error_code: Optional[str] = None
        error_params: Optional[Dict[str, Any]] = None
        if failed:
            # 不截断：这列是 `TEXT`，每个失败节点的原因都要看得到（写进来的本来
            # 就只有「节点: 原因」这种一行文本，不含 traceback）。
            # **这是快照**（老读点 + 排障直查库用），形态与从前**一字不变**。
            error_message = "; ".join(
                f"{s.node_id}: {s.error_message or s.status}" for s in failed
            )
            # 结构化那一份：**零件数组**。任务级的原因天生不是一句原子文案，而是
            # 「哪几个节点挂了、各自为什么」—— 所以外壳是一个固定代号，零件放参数里，
            # 读的时候**逐个按各自的语言重渲染再拼**（每个零件自己可能还有代号）。
            # ⚠️ 不拆零件的话，这条罐装句子会把语言冻死 —— 与本次改造要治的病是同一个。
            error_code = TASK_AGGREGATE_FAILED
            error_params = {
                "parts": [
                    {
                        "node_id": s.node_id,
                        # 节点自己带代号就用代号渲染；没有（老运行方 / 引擎原文）就回落
                        # 它那句文本。两个都留给读侧，由它挑。
                        "code": s.error_code,
                        "params": s.error_params or {},
                        "message": s.error_message or s.status,
                    }
                    for s in failed
                ]
            }

        # ⚠️ **运行行已经收过口时，原因以运行行为准**（不要用上面重算的那份覆盖它）。
        # 那一行是**当时那个收口者**写下的权威原因 —— `_advance` / `_fail_subtask` 写的是
        # 节点级那条拼接（如「parse_1: 引擎原文…」）。照节点重算会把它换成一个可能
        # 更含糊的版本（节点自己那一行的原因可能已被后来的重跑清掉）。
        # 这条路**不是理论**：补收口那一趟走的正是它（运行行早收了口、而任务快照那次
        # CAS 落空 → 本趟补写快照），迟到很久的旧凭证上报也走它。
        # ⚠️ **被用户停掉的那条路到不了这里**（2026-10-15 起）：`_settle_stopped` 不再写
        # 原因，运行行的 `error_message` 是 `None`，下面这个 `and` 直接为假 ——
        # 而「已停止」的结论本来就写在任务的 `status` 上，不需要一句话来替它说话。
        if run_already_closed and locked.error_message is not None:
            # 三件**一起取运行行那份**：只取文本会把它已经带上的代号丢了，
            # 于是这条原因又退化成一句冻住的话。
            error_message = locked.error_message
            error_code = locked.error_code
            error_params = locked.error_params

        # ④ 收这次运行。锁已在手，这里的 CAS 只是兜底，正常必中。
        #    **运行行已经收过口时整段跳过**（见 ②）：对一条终态的行再 CAS 必然命中 0 行，
        #    照旧走的话会在这一步就 `return`，任务快照永远补不上 —— 那正是「卡死」的成因。
        if not run_already_closed:
            finished = await self._attempts.finish_attempt_cas(
                ctx.attempt.tenant_id,
                ctx.attempt.id,
                next_status,
                error_message=error_message,
                error_code=error_code,
                error_params=error_params,
            )
            if not finished:
                return

        # ⑤ 任务快照：先取一次新鲜的任务行再 CAS。`ctx.task` 是本事务早先读的，
        #    它的 `state_version` 可能已经被并发派活的 `mark_running` 推过，
        #    直接拿它去 CAS 会白挨一次 0 行、留下「运行已收口、任务还在 running」。
        task = await self._tasks.get_task_fresh(ctx.tenant_id, ctx.task.id)
        if task is None:
            logger.warning(
                "[TaskCenter] 运行已收口但任务行取不到 task=%s attempt=%s",
                ctx.task.id,
                ctx.attempt.id,
            )
            return
        # `operator_id` 只有「用户主动停某一步」那条路（`stop_subtask` → `_do_stop_subtask`，
        # 2026-10-15 起它自己传）进得来；**为空时这一列完全不参与更新**
        # （`_update_task_snapshot` 是「传什么写什么」，带一个 `None` 进去会把上一次记的人
        # 抹掉 —— 那是丢信息，不是「没记」）。
        # 四个后台调用点（上报消化 / 派活判死 / 超时巡检 / 防僵尸）都不传，
        # 所以后台推进状态永远不会把真正的操作人盖掉（CLAUDE.md 那条纪律）。
        snapshot: Dict[str, Any] = {
            "status": next_status,
            "error_message": error_message,
            "error_code": error_code,
            "error_params": error_params,
            "finished_at": datetime.now(),
        }
        if operator_id:
            snapshot["updated_by"] = operator_id
        # ⚠️ **两个闸门必须带**（`require_active` / `expect_current_attempt`）：上面那次
        # `get_task_fresh` 取的是**最新版本号**，而「该不该收口」判的是手上这份可能几秒前
        # 读的上下文 —— 只要任务锁在这个窗口里提前失效，别的副本（典型是用户的
        # `retry_task`）就能把任务推到新一轮去，而这次汇总会命中最新版本号、**把新一轮
        # 当场写回终态**。闸门写进 `WHERE` 才拦得住，详见
        # `TaskRepository.update_snapshot_cas` 的 docstring。
        changed = await self._update_task_snapshot(
            task,
            require_active=True,
            expect_current_attempt=ctx.attempt.attempt,
            **snapshot,
        )
        if not changed:
            # 收口本该已串行化 —— **`_aggregate` 的五个调用点（上报消化 / 派活判死 /
            # 超时巡检 / 防僵尸 / 补收口）全部持任务锁**，`tasks` 行的每一个写入者也都在
            # 锁内。所以命中 0 行只有两种成因，都值得看一眼：
            #
            # ① **本单已经不作数了**（上面那两个闸门拦下的）：这把任务锁在本次收口期间
            #    被提前释放 / 过期（`task_lock` 不续期，租期 30 秒），另一个持有者把任务
            #    推到了别处 —— 最典型是用户点了「重跑」（任务换成新一轮）。**那是正确的
            #    行为**，本次汇总本就该作废，不留任何需要收拾的东西；
            # ② **任务快照该补而没补上**（版本号对不上那种）：任务停在非终态、当次运行却
            #    已经收了口。这一格从前没救（只能人工改库），现在后台有一趟「补收口」
            #    （`_settle_pass` → `settle_stale_task`）会捡回来 —— 它重走本方法，② 认出
            #    运行行已收口、跳过 ④，于是任务快照这一步能再跑一次。
            #
            # 两类都按 ERROR 报：能自愈 / 本该如此，都不等于「锁提前失效」这件事没问题
            # （锁外写 `tasks` 那条路更要查）。
            logger.error(
                "[TaskCenter] 任务快照 CAS 落空 task=%s attempt=%s —— "
                "要么本单已被别处推到新一轮（本次汇总作废、无需收拾），"
                "要么任务暂留非终态、等后台「补收口」趟次捡回"
                "（⚠️ 当次运行还剩被上游 hold 住的 pending 也扫得到 —— 候选判据是"
                "「当次运行那一行已经收口」，2026-10-09 起；从前那版会漏掉这种形态）；"
                "两种情况都没有错误结果，但都说明这把任务锁被提前释放过，值得查一眼",
                ctx.task.id,
                ctx.attempt.id,
            )
            return
        logger.info(
            "[TaskCenter] 运行收口 task=%s attempt=%s status=%s",
            ctx.task.id,
            ctx.attempt.attempt,
            next_status,
        )

    async def _mark_run_started(self, ctx: RunContext, now: datetime) -> None:
        """有子任务被派出去 → 任务（连同当前这次运行）转「跑着」（§7.2）。

        `started_at` 是「首次开始时间」，靠 `coalesce` 只写第一次：任务重跑回来时它仍是
        最初那一刻。任务快照走 CAS + **状态闸门**（只从 `pending` / `running` 出发），
        抢不到有两种情形，都不当作错误：

        1. 别的副本刚改过（大概率也已经置成 running）；
        2. **任务在出站那段时间里已经被收成终态** —— 最典型是用户在派活窗口里点了
           「停止」（§4.4 的 `stop_task`）。此时子任务确实已经在运行方那边跑起来了，
           但任务不该被翻回 `running`（见
           `TaskRepository.mark_running` 的说明）。子任务保持 `running`，后续按运行方
           上报的结果收口 —— 正是停止功能要的语义。

        第 2 种情形要留日志：任务已被收口、运行方却真的收到了活，这是需要被看见的窗口。

        ⚠️ **本条目与「停止」无关、单独进闸**：本方法**只对「当前运行」的派活收尾**
        （判据与 `_aggregate` ⓪ / `_absorb` 闸② / `_advance` 填下游闸 / `claim_for_dispatch`
        / `handle_stuck_pending` 同一句：`ctx.attempt.attempt == ctx.task.current_attempt`）。
        缺了它就成了**全模块唯一一处没闸的收尾写**：出站那段窗口（最长 ≈97 秒）里用户
        先「停止」、再 `retry_task` 的话，这条**旧运行**的 `ACCEPTED` 回执会把**新一轮**
        那套全 `pending` 的子任务所属的任务快照从 `pending` 翻成 `running` —— 新一轮
        一步都还没派出去，快照却已经在「跑着」。后果偏轻（新一轮照常派活、
        `mark_running` 再调是 no-op、补收口因当次运行还有 `pending` 不会误判），
        但既然别处都认这一句，这里也必须认。
        """
        # ⓪ 旧运行的派活收尾：**不碰任务快照**（见 docstring 末尾那段）。子任务那边
        #    该由 `finalize_dispatch` 做完的（状态推进）已经在它自己那条路上做完了，
        #    这里少的只是「把任务翻成 running」—— 那是新一轮的事。
        if ctx.attempt.attempt != ctx.task.current_attempt:
            logger.info(
                "[TaskCenter] 旧运行的派活不翻任务快照 task=%s 本行运行=#%s 当前运行=#%s"
                "（本单只作废「翻 running」这一步）",
                ctx.task.id,
                ctx.attempt.attempt,
                ctx.task.current_attempt,
            )
            return
        # ⚠️ `first_started_at` **必须在改库之前**取出来。`mark_running` 写
        #    `started_at` 用的是 `func.coalesce(Task.started_at, ...)`，SQLAlchemy 的
        #    `synchronize_session='evaluate'` 评估不了这个表达式，于是**把这一列作废**
        #    （expired）等下次访问再回读 —— 而「下次访问」就是一次**同步** lazy-load，
        #    异步 session 下直接抛 `MissingGreenlet`（greenlet_spawn has not been called）。
        #    同一次 `_apply_local` 里的 `ctx.task.id` / `state_version` 不受影响，只有被
        #    `coalesce` 写过的 `started_at` 会被作废，所以这一处是唯一的坑。
        #    顺带：这里取到的值与库里的 `coalesce` 结果**语义相同**（首次开始时间不覆盖）。
        first_started_at = ctx.task.started_at or now

        await self._attempts.mark_running(ctx.tenant_id, ctx.attempt.id, started_at=now)
        changed = await self._tasks.mark_running(
            ctx.tenant_id, ctx.task.id, ctx.task.state_version, started_at=now
        )
        if changed:
            _apply_local(
                ctx.task,
                status=TaskStatus.RUNNING.value,
                started_at=first_started_at,
            )
        elif ctx.task.status in TASK_TERMINAL_STATUSES:
            logger.warning(
                "[TaskCenter] 派活已送达但任务已被收口，不翻回 running "
                "task=%s attempt=%s status=%s（子任务保持 running，等运行方上报收口）",
                ctx.task.id,
                ctx.attempt.attempt,
                ctx.task.status,
            )

    async def _update_task_snapshot(
        self,
        task: Task,
        *,
        require_active: bool = False,
        expect_current_attempt: Optional[int] = None,
        **values: Any,
    ) -> bool:
        """带版本号更新任务快照；成功就把内存里的行也跟上，避免读到旧版本号。

        两个可选闸门（`require_active` / `expect_current_attempt`）会一并写进 CAS 的
        `WHERE` —— **汇总 / 收口那两条路必须带**，理由见
        `TaskRepository.update_snapshot_cas` 的 docstring（「版本号取最新、状态判据却是
        陈旧的」那条缝，锁一提前失效就能把刚被重跑的任务当场写回 `failed`）。
        """
        changed = await self._tasks.update_snapshot_cas(
            task.tenant_id,
            task.id,
            task.state_version,
            require_active=require_active,
            expect_current_attempt=expect_current_attempt,
            **values,
        )
        if changed:
            _apply_local(task, **values)
        return changed

    async def _sync_attempt_row(
        self,
        subtask: Subtask,
        status: str,
        *,
        from_statuses: Optional[List[str]] = None,
        outputs: Optional[Dict[str, Any]] = None,
        error_message: Optional[str] = None,
        error_code: Optional[str] = None,
        error_params: Optional[Dict[str, Any]] = None,
        inputs: Optional[Dict[str, Any]] = None,
        input_refs: Optional[List[Dict[str, Any]]] = None,
        output_refs: Optional[List[Dict[str, Any]]] = None,
        finished_at: Optional[datetime] = None,
    ) -> None:
        """把这次尝试的历史行收口（`subtask_attempts` 只追加不覆盖，§7.2）。

        `finished_at` 传的就是写进 `subtasks.finished_at` 的那个值；派活置 `running`
        时走的是 `set_status_cas(..., started_at=同值)`，这边只处理结束置终态。

        `from_statuses` 是这条历史行的合法前驱，默认只认「跑着」——运行方上报、超时巡检
        都从 `running` 收口。**派活失败判死那条路例外**：那一步可能连抢占都没走到
        （没绑运行方时还是排队态），所以要连 `pending` 一起收，由调用方显式指定。
        """
        values: Dict[str, Any] = {}
        if finished_at is not None:
            values["finished_at"] = finished_at
        if outputs is not None:
            values["outputs"] = outputs
        # **入参快照**（2026-10-16 加）：与 `outputs` 对称的那一格 —— 产出按次存、入参却只在
        # `subtasks` 上（那行是「最新一次尝试」的现状，重试会被 `_fill_inputs` 重算）。
        # 不存这一份，「第 N 次尝试吃的是什么」重试之后就查不到了。
        if inputs is not None:
            values["inputs"] = inputs
        # 输入「从哪儿来」的引用（2026-10-16 加）：**每次尝试各存一份** —— 重试会把
        # `subtasks.inputs` 按上游现在的产出重算，所以「第 2 次尝试吃的是什么」只能在这条
        # 历史行上看到（`subtasks` 那行永远只反映最新一次尝试）。
        if input_refs is not None:
            values["input_refs"] = input_refs
        # **产出引用**（2026-10-16 加）：产出正文本来就在这条历史上按次存着（`outputs`），
        # 「它的产物在哪儿看」这个入口从前只挂在 `subtasks` 那行 —— 同样的理由，按次补一份。
        if output_refs is not None:
            values["output_refs"] = output_refs
        if error_message is not None:
            values["error_message"] = error_message
        # 代号 + 参数与 `error_message` 同一档：**给了才写**（老运行方只给文本时
        # 这两个保持 NULL，行为与加列之前一字不变）。
        if error_code is not None:
            values["error_code"] = error_code
        if error_params is not None:
            values["error_params"] = error_params
        await self._subtask_attempts.set_status_cas(
            subtask.tenant_id,
            subtask.id,
            subtask.current_attempt,
            expect_statuses=from_statuses or [SubtaskAttemptStatus.RUNNING.value],
            next_status=status,
            **values,
        )

    async def _open_attempt_row(
        self,
        tenant_id: str,
        space_id: str,
        task_id: str,
        subtask: Subtask,
        *,
        created_at: datetime,
    ) -> None:
        """为 `subtask` 当前那次尝试补一条 `pending` 历史行（重试时用，§7.2）。

        子任务诞生时就插过一条；每次重试（`current_attempt` 已 +1）再插一条，
        每次尝试的轨迹因此完整、`created_at` 也记得下它是什么时候重新进的队列。
        """
        await self._subtask_attempts.create(
            tenant_id=tenant_id,
            space_id=space_id,
            task_id=task_id,
            subtask_id=subtask.id,
            attempt=subtask.current_attempt,
            execution_id=None,
            status=SubtaskAttemptStatus.PENDING.value,
            created_at=created_at,
        )

    # ========================================================
    # 内部：取数
    # ========================================================

    async def _require_task(self, tenant_id: str, task_id: str) -> Task:
        task = await self._tasks.get_task(tenant_id, task_id)
        if task is None:
            raise ResourceNotFoundError(
                message=translate(
                    "err.task_center.task.not_found",
                    params={"task_id": task_id},
                    fallback=f"任务 {task_id} 不存在",
                ),
                details={"task_id": task_id},
            )
        return task

    @staticmethod
    def _require_space_match(task: Task, space_id: str) -> None:
        """任务必须真的属于这个空间，否则**一律按「找不到」处理**（1002）。

        **两个调用点：`_do_retry_subtask`（§4.3 `task.subtask_retry`）与 `_do_retry_subtasks`
        （§4.2 ④ 批量重跑）** —— 那两条路都按 `space_id` 寻址任务，而任务自己带
        `space_id`，不核对就会改到同一租户下别的空间的任务上。⚠️ **单步与批量都在核，
        别以为批量那路不核**（它同样要传 `space_id`）。（`retry_task` / `stop_task` /
        `delete_task` 与三个控制台读接口 **不调它** —— 它们不走 `space_id`，只按
        `tenant_id` + `task_id` 定位；上报那条路由 `_require_report_identity` 核对
        「报文里的 `space_id` 与任务是否一致」。）

        不给「不属于你」这种细节提示，是因为**「不属于当前空间」与「压根不存在」在调用方
        那侧不该有区别** —— 一致按找不到拒，也就顺带不泄露「这个 id 是存在的」。

        ⚠️ **这条承诺 `details` 也要守**：`details` 里只放调用方自己给的 `task_id` /
        `space_id`（回显），**`task.space_id` 一个字都不带**。码给 1002 却在 `details`
        里回一句真话，等于把上面那句从后门放掉。**别顺手加回来**（这里从前带过
        `task_space_id`，是刻意删掉的）。

        ⚠️ 它只在**查得到任务**时才有判据（任务查不到时 `_require_task` 已经先拒了）。
        """
        if task.space_id == space_id:
            return
        raise ResourceNotFoundError(
            message=translate(
                "err.task_center.task.subtask_retry_space_mismatch",
                params={"task_id": task.id, "space_id": space_id},
                fallback=(
                    f"空间 {space_id} 里没有任务 {task.id}（任务不在这个空间）"
                ),
            ),
            details={
                "task_id": task.id,
                "space_id": space_id,
            },
        )

    async def _retry_already_in_flight(
        self, tenant_id: str, task: Task, node_id: str
    ) -> Optional[SubtaskItemResponse]:
        """这一步是不是**已经重试过了**（上一次其实成了、只是响应没回来）？

        是就返回它现在这份快照 —— 调用方据此按「已经重试了」收工；不是就返回 `None`，
        让调用方照常走三条前提。

        ── 三个判据，全中才算 ──

        1. **任务已经不是终态**（由调用方保证：只有不是终态时才会走到这里）—— 反过来说，
           上一次重试若真的落了库，任务必然被它拉回 `running`（`reset_for_subtask_retry`）；
        2. **这一步在当前运行里的 `current_attempt > 1`** —— `_do_retry_subtask` 是**唯一**
           会让它 +1 的地方（`retry_task` 会整套重建子任务、`current_attempt` 回到 1），
           所以「大于 1」就是「这一步被重试过」的指纹；
        3. **状态是 `pending` / `running`** —— 重试把它放回了队列（`pending`），派活之后再
           转 `running`。**已经跑完、又回到终态的这一步不认**（`success` / `failed` /
           `timeout`）：那说明上一次重试连同它的执行都已经结束了，这次请求对不上「响应丢了」
           这个剧本 —— 交给三条前提正常处置（任务若已回到终态，那就是一次正常的重试；
           没回到就被前提①拒）。

        ── 为什么必须有这一档（它不是补洞，是 SDK 敢对这条路重发的前提）──

        首次重试成功后任务已经变成 `running`；调用方那侧只看到「响应没回来」再发一次时，
        撞上的正是前提①（任务非终态 → 1003 `subtask_retry_task_not_terminal`）。没有这一档，
        一次网络抖动就会让调用方看到「重试失败」，而重试其实已经生效 —— 那是最坏的一种误报。
        有了它，重发撞上的是「那一步已经排上队 / 正在跑了」，据此**回成功**（§4.2 ③）。
        所以这一档**必须判在前提①之前**：判在它之后等于永远走不到。

        ⚠️ **它一个字都不改**：返回的是**读来的**快照，不是这次调用改出来的。调用方看到的
        `current_attempt` 已经是 +1 那一份、状态也已经是 `pending` / `running` —— 恰好就是
        「上一次调用已经把它排上队了」这个事实本身。**不要在这里补写任何东西**：真正落库
        的那一次已经把四步做完了，再写一遍就是把同一个重试做第二次。
        """
        if not task.current_attempt:
            return None
        attempt = await self._attempts.get_by_attempt_no(
            tenant_id, task.id, task.current_attempt
        )
        if attempt is None:
            return None
        row = await self._subtasks.get_subtask(tenant_id, attempt.id, node_id)
        if row is None or row.current_attempt <= 1:
            return None
        if row.status not in (SubtaskStatus.PENDING.value, SubtaskStatus.RUNNING.value):
            return None
        logger.info(
            "[TaskCenter] 子任务重试判为重发（上一次已生效）：task=%s node=%s status=%s attempt=%s",
            task.id,
            node_id,
            row.status,
            row.current_attempt,
        )
        return SubtaskItemResponse(**row.to_dict())

    async def _require_attempt(
        self, tenant_id: str, task_id: str, attempt: int
    ) -> TaskAttempt:
        row = await self._attempts.get_by_attempt_no(tenant_id, task_id, attempt)
        if row is not None:
            return row
        raise ResourceNotFoundError(
            message=translate(
                "err.task_center.task.attempt_not_found",
                params={"task_id": task_id, "attempt": attempt},
                fallback=f"任务 {task_id} 没有第 {attempt} 次运行",
            ),
            details={"task_id": task_id, "attempt": attempt},
        )

    async def _attempt_of(
        self, tenant_id: str, task: Task, attempt: Optional[int]
    ) -> TaskAttempt:
        """不给 `attempt` 就取任务**当前那次运行**（`tasks.current_attempt` 指向的）。"""
        if attempt is None:
            return await self._require_attempt(tenant_id, task.id, task.current_attempt)
        return await self._require_attempt(tenant_id, task.id, attempt)

    @staticmethod
    def _require_subtask_row(
        row: Optional[Subtask], task_id: str, node_id: str
    ) -> Subtask:
        """取不到就按「找不到」处理。定位一律按 `node_id`（§3.5、§4.3）。"""
        if row is None:
            raise ResourceNotFoundError(
                message=translate(
                    "err.task_center.subtask.not_found",
                    params={"task_id": task_id, "node_id": node_id},
                    fallback=f"任务 {task_id} 里没有节点 {node_id}",
                ),
                details={"task_id": task_id, "node_id": node_id},
            )
        return row

    async def _require_deletable(self, tenant_id: str, task: Task) -> None:
        """删任务的前置：**这条任务没有任何活会再动**（§4.3 K1）。

        判据两条：

        1. **一个 `running` 子任务都没有** —— 有就拒。`running` 意味着运行方那边可能正在
           跑：抢占（`pending → running`）在**锁内**、出站在**锁外**，所以「一个 `running`
           都没有」就等于「没有正在跑的、也没有正在出站的派活」；
        2. **任务快照是终态 或 一个 `pending` 子任务都没有** —— 两者都不是，说明**这条任务
           还会继续跑**（`retry_task` / `retry_subtask` 刚把它拉回去，或者它是刚建好还没
           派活的），删了就是把一票活生生要跑的活掐掉。

        ⚠️ **两条里只有①完全落在子任务上**；②的**前半截读的就是任务快照的状态**
        （`task.status in TASK_TERMINAL_STATUSES`）—— 别照「判据全在子任务上」去理解它
        （本节下面那段「为什么不用『任务快照是终态』」讲的是**替换掉旧 K1**，不是
        「代码里已经不读快照了」）。⚠️ **②里那个 `pending` 是跨全部运行的计数**
        （`count_unfinished_by_task` 刻意不分运行）：旧运行里被 hold 住的 `pending`
        **永远不会再跑**（派活只扫当前运行、`retry_subtask` 也只认当前运行），
        却照样会让②的析取全假 —— 这一条与「任务快照卡在非终态」凑在一起时，删除会被挡住，
        详见 `_stop_targets` 那边记的同一处。**⚠️ 能救它的是谁，取决于那些 `pending` 落在
        哪一次运行 —— 别一律说成「补收口兜底」，那句话只对一半**：

        - 挡路的 `pending` 在**旧运行**里（当次运行一步没剩）→ **后台第五趟「补收口」**
          扫得到，静置 60 秒后把快照补成终态；
        - 挡路的 `pending` 在**当次运行**里（收口那次任务快照 CAS 落空留下的残格，那些
          `pending` 被上游 hold 住）→ **补收口也扫得到**：候选判据是「当次运行那一行已经
          收口」（`list_tasks_needing_settle`，2026-10-09 起；从前那版要求「当次运行一个
          `pending` 都没有」，正好把这一形态排除在外）。另外 **`task.stop` 也救得动**
          （它的 `_stop_targets` 数的是**全部** `pending`），不必等那 60 秒。

        （两种形态的判据与取舍写在 `list_tasks_needing_settle` 的 docstring 上。）

        ── 为什么不用「任务快照是终态」那一条（这是本轮改的口径）──

        那是**从前**的 K1。它两头都堵不住：

        - **堵不住「卡死」**：子任务全都结束了、任务快照却还停在 `pending` / `running`
          的那一档（`_aggregate` 的 CAS 落空会留下它）→ 要求快照终态 = 这种任务**删不掉**，
          而 `task.stop` 又因为「没有未开始的子任务」拒它，**两道门一起锁死**。
          ⚠️ **2026-09-29 起这一档有兜底了**（后台第五趟「补收口」`_settle_pass`，60 秒内
          把它补成终态），所以「永久」只在那趟之前成立 —— 但**判据该松还是得松**：
          删任务的要求本来就是「没有活会再动」，而不是「快照好看」；
        - **堵不住「先停止、再删除」**：`task.stop` **故意不碰正在跑的子任务**（只把它留成
          `running`）、同时把任务收进终态（`failed` / `stopped`）—— 那正是旧判据放行的状态，
          于是「运行方还在跑、管理方却把记录删了」，恰是 K1 声称要防的那一档。

        换成上面两条之后，判据落在**可观测的事实**（子任务到底结束了没有）上：卡死的读得
        出来（全结束 → 放行）、还在跑的也读得出来（有 `running` → 拒）。

        ⚠️ **第 ③ 步拿锁之后必须再调一次**（同一把锁内复查）：上面那趟是**没拿锁的预检**，
        覆盖不到出站那一段时间（那一趟内含传送层重发，单条最坏 3 × 超时 + 退避
        ≈ 97 秒）里新派出去的活。
        """
        running, pending = await self._subtasks.count_unfinished_by_task(
            tenant_id, task.id
        )
        if not running and (pending == 0 or task.status in TASK_TERMINAL_STATUSES):
            return
        raise ResourceConflictError(
            message=translate(
                "err.task_center.task.delete_not_idle",
                params={
                    "task_id": task.id,
                    "status": task.status,
                    "running": running,
                    "pending": pending,
                },
                fallback=(
                    f"任务 {task.id} 现在不能删：还有 {running} 个子任务在跑、"
                    f"{pending} 个还没开始（任务当前是 {task.status}）"
                ),
            ),
            details={
                "task_id": task.id,
                "status": task.status,
                "running_subtasks": running,
                "pending_subtasks": pending,
            },
        )

    @staticmethod
    def _require_terminal(task: Task, message_key: str) -> None:
        """任务必须已到终态才允许这个动作（`retry` / `subtask_retry` 共用）。

        ⚠️ **`task.delete` 不再用它** —— 删任务的前置换成了 `_require_deletable`（判据在
        子任务上），理由见那里。这里留着的**三个**调用方（`retry_task` / `retry_subtask` /
        `retry_subtasks`）要的是另一件事：「没跑完的不许重来」。
        """
        if task.status in TASK_TERMINAL_STATUSES:
            return
        raise ResourceConflictError(
            message=translate(
                f"err.task_center.task.{message_key}",
                params={"task_id": task.id, "status": task.status},
                fallback=(
                    f"任务 {task.id} 当前是 {task.status}，"
                    "得等它结束（completed / failed / stopped）才能做这个操作"
                ),
            ),
            details={"task_id": task.id, "status": task.status},
        )

    @staticmethod
    def _require_stoppable(task: Task) -> None:
        """只有还没结束的任务停得掉（`task.stop` 的前置判定）—— `_require_terminal` 的反面。

        **只有 `pending` / `running` 能停**：`completed` / `failed` / `stopped` 都已经结束了，
        停止无从谈起 —— 它们想要重来的动作是 `retry`（`stopped` 也在内：停过的任务
        照样能重跑，这正是「停错了还能救」那条路）。

        这一档只管「**状态**允不允许停」，**不保证一定有活可停**：「停得掉什么」是下一步
        `_stop_targets` 的事，而 `running` 的任务完全可能一个 `pending` 子任务都没有
        （那时由它拒掉）。别把「状态可停」读成「一定有子任务要置停」。
        """
        if task.status in (TaskStatus.PENDING.value, TaskStatus.RUNNING.value):
            return
        raise ResourceConflictError(
            message=translate(
                "err.task_center.task.stop_not_stoppable",
                params={"task_id": task.id, "status": task.status},
                fallback=(
                    f"任务 {task.id} 当前是 {task.status}，已经结束了，停不了"
                    "（要重来请用「重跑」）"
                ),
            ),
            details={"task_id": task.id, "status": task.status},
        )

    @staticmethod
    def _stop_targets(task: Task, subtasks: List[Subtask]) -> List[Subtask]:
        """这次停止该置停哪些子任务；`running` 任务一个都判不了就抛冲突（`task.stop` 的核心判定）。

        **两个分支都只挑 `pending`，`running` 的一个都不碰**（用户口径）：正在跑的活
        杀不掉（运行方没有「停止」这个动作 —— 收口之后那条 `task_stopped` 是**通知**，
        不是去掐它），把它们写成「已停止」
        等于把「运行方还在跑、平台说它被用户停了」写进库里 —— 它们的状态一律**留给后续
        上报按实际结果填**（`_advance` 那条 `running → success/failed` 的 CAS 只看子任务
        自己，不看任务）。任务 `pending` 时正常全是 `pending`，这个筛选等于是全选。

        **判据是「还有没有能停的」**（`pending` 子任务），**不是任务快照**：

        - **还有 `pending` 的**：置停这些还没开始的，正在跑的一个都不动。
        - **一个 `pending` 都没有、而底下确实有活**（任务已在 `running`，**或者**有子任务
          `running`）→ **拒绝**：能停的只剩已经派出去的活，而它们杀不掉，收下这次停止
          等于给调用方一个「停了」的假象。
        - **一个 `pending` 都没有、底下也没有活**（一行子任务都没有的空任务）→ **照收**：
          没有「没有可停的」这一说 —— 这种任务**没有任何子任务可派**，谁也推不动它，
          拒掉它就再没有别的办法能收掉它（会永远停在 `pending`）。
        - ⚠️ **「底下确实有活」那半条是本轮收口加的**：从前这一支判的是「任务快照是不是
          `running`」，于是「快照还是 `pending`、底下已经有一步被抢占」那段窗口拦不住
          （`claim_for_dispatch` 先抢占、`finalize_dispatch` 拿到回执才把任务转
          `running`，中间隔着**整段出站**，最坏约 97 秒）。**那一格本身是被设计接受的**
          —— 停止收下之后 `_mark_run_started` 的两道闸会挡住「把已收口的任务翻回
          `running`」，另打一条 warning（见那里）。这里换判据是为了让「拒绝」这条规则
          **用它自己声明的那件事当判据**，不是为了改那一格的行为。
        """
        targets = [s for s in subtasks if s.status == SubtaskStatus.PENDING.value]
        # 「能停的只剩已经派出去的活」= 一个 `pending` 都没有、**且底下确实有活**。
        # 两个条件缺一不可：只看前半，会把「一行子任务都没有的空任务」也拒掉（那种任务
        # 没有子任务可派，拒了就是死结）；只看任务状态就是从前那版，窗口里拦不住。
        #
        # ⚠️ **快照可能撒谎（记在这里备查）**：`task.status == RUNNING` 这一支读的是
        #    **任务快照**，它停在 `running` 而底下其实全结束了是**真实存在**的一格 ——
        #    成因是 `_aggregate` 收口那一刻任务快照那次 CAS 落空（`_update_task_snapshot`
        #    的注释里承认这条存在）。那时：
        #      · 本方法 —— `targets` 空、`still_running` 假，却因为快照是 `running` 而拒绝；
        #      · `delete_task` —— 旧运行里被 hold 的 `pending` 让 `_require_deletable` 全假，也拒；
        #      · 三个 `retry_*` —— 任务非终态，一律 1003。
        #    合起来那几秒里**六条操作全拒**，而**除补收口外的四趟后台确实捞不到它**
        #    （派活/防僵尸只认当前运行的 `pending`、巡检只认 `running`）。
        #    ⚠️ **但它不是死结**：第五趟「补收口」（`_settle_pass` → `settle_stale_task`）的
        #    候选判据正是「当次运行一步没剩、任务快照却还在活跃态」，任务行静置
        #    `TASK_CENTER_SETTLE_STALE_SEC`（默认 60 秒）之后它会把快照补成终态，
        #    上面三扇门随即全开 —— **代价是等待，不是「只能人工改库」**。
        #    （想让它更快可以改这一支的判据、或改 `_require_deletable` ② 里那个 `pending`
        #    的口径，二者任一即可；但那只是缩短等待，不必为此加一条新路径。）
        still_running = any(s.status == SubtaskStatus.RUNNING.value for s in subtasks)
        if not targets and (
            task.status == TaskStatus.RUNNING.value or still_running
        ):
            raise ResourceConflictError(
                message=translate(
                    "err.task_center.task.stop_no_pending_subtask",
                    params={"task_id": task.id},
                    fallback="不存在未开始的子任务，无法停止任务",
                ),
                details={"task_id": task.id, "status": task.status},
            )
        return targets


# ============================================================
# 模块级小工具
# ============================================================


def _timeout_of(step: Dict[str, Any], catalog_step: Any) -> int:
    """子任务超时时间：编排里写的优先，没写就取内置步骤清单的默认值（§7.4）。

    发版校验（§6.5 第 7 条）已经卡死 `timeout_seconds` 必填正整数，这里的回落只是给
    「清单 / 编排被手工改过」的异常数据留个兜底。
    """
    value = step.get("timeout_seconds")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    fallback = getattr(catalog_step, "timeout_seconds", None)
    return fallback if isinstance(fallback, int) and fallback > 0 else 300


def _deps_satisfied(subtask: Subtask, by_key: Dict[str, Subtask]) -> bool:
    """这一步的依赖是不是都满足了（§6.3：`depends_on` 里每个都 `success`）。

    这是**派活侧与收尾侧共用的同一把尺子**（另一处在 `_is_runnable_pending`），
    后台扫描的 SQL（`execution_repository._runnable_pending_stmt`）也照着它写，
    否则会出现「永远不派、也永远不收尾」的死锁。

    ⚠️ 唯一的例外是下面 `sibling is None` 那一支：SQL 侧没有对应条件，判的是
    「能跑」。那是一处**刻意留下的差异**，理由与影响写在
    `execution_repository._runnable_pending_stmt` 的 `has_unsatisfied_dep` 上。

    **`by_key` 按 `node_id` 索引**（§3.5：`depends_on` 里写的就是 `node_id`）。
    """
    for dep in subtask.depends_on or []:
        sibling = by_key.get(dep)
        if sibling is None:
            # 依赖一个不存在的节点——发版校验 N2（`flow_validator`）本该打回，
            # 只有手工改库 / 改 JSON 才碰得到。运行期按「不满足」保守处理。
            # SQL 侧在这一支判「能跑」；差异良性：那条行进锁后被这里拒掉，不会真派出去，
            # 也**不影响收尾**（下面 `_is_runnable_pending` 调的就是本函数，判 False）。
            # **保守方向是对的，别为了跟 SQL 对齐把它改成「满足」。**
            # ⚠️ 那处差异的**全部影响**写在 `execution_repository._runnable_pending_stmt`
            # 的注释上 —— 别把它读成「防僵尸会兜底清掉那条行」。
            return False
        if sibling.status not in SUBTASK_SATISFIED_STATUSES:
            return False
    return True


def _is_runnable_pending(subtask: Subtask, by_key: Dict[str, Subtask]) -> bool:
    """这个 `pending` 是不是「还能跑」（§6.3、§7.2 收尾表第 2 条）。

    判据跟派活侧**同源**：`depends_on` 里每一步都 `success`。两处必须一致——派活侧按它
    决定派不派、收尾侧按它决定等不等，一旦分叉就成了死锁：派活侧永远不派、收尾侧却一直
    等，任务永远停在 `running`，而重试和删除都要求任务已终态，只能人工改库。

    **`current_attempt > 1`（重试中的）不再单独豁免**：`retry_subtask` 已经把「上游全是
    `success`」列为前提（§4.3），够格重试的子任务必然满足依赖，不需要在收尾这边开口子。

    **上游失败 / 超时 hold 下来的 `pending`**（依赖永远满足不了）不算「还能跑」——它不进
    候选，也就不会把任务永远卡在 `running`（§7.2 收尾表第 2 条）。
    """
    return _deps_satisfied(subtask, by_key)


def _run_settled(subtasks: List[Subtask]) -> bool:
    """本次运行是不是已经定局（可以推任务终态了，§7.2 收尾表）。

    「定局」= 没有 `running`，且没有「能跑的 `pending`」。剩下的只可能是各终态，
    以及**上游失败 / 超时 hold 下来、注定跑不起来**的 `pending`。
    """
    by_key = {node.node_id: node for node in subtasks}
    for node in subtasks:
        if node.status == SubtaskStatus.RUNNING.value:
            return False
        if node.status == SubtaskStatus.PENDING.value and _is_runnable_pending(node, by_key):
            return False
    return True


def _apply_local(obj: Any, **values: Any) -> None:
    """把「这一次改了哪些字段」显式同步到内存对象上。

    **绝不在这里动 `state_version`。** 仓储层的 CAS 走的是 ORM 的 `update(Model)`
    （`transition_cas` / `update_snapshot_cas` / `mark_running` / `reopen_for_subtask_retry`
    —— 全模块 10 个调用点，前置的 CAS 都是这几个里的某一个），
    SQLAlchemy 默认 `synchronize_session='auto'`，执行那一刻就已经把 identity map 里
    这一行的 `state_version` 同步成新值了 —— 实测：CAS 之后内存与库同为「原值 + 1」。

    这里要是再 +1，内存就比库大 1，紧接着那次拿 `expect_version=<内存值>` 的 CAS
    必然命中 0 行。`_fail_subtask` 正是这么让「派活明确失败 → 置 `failed`」恒落空的：
    运行方拒收时子任务标不上 `failed`、一直卡在 `running`，直到超时巡检把它收成
    `timeout` —— 日志上只剩下一条与真实原因无关的「超时」。

    显式 `setattr` 保留下来，只是让「这次改了哪些字段」在调用处一眼可读；这些字段
    同样已被 `.values()` 同步过，写或不写都不改变正确性。
    """
    for key, value in values.items():
        setattr(obj, key, value)


def _require_batch_node_ids(task_id: str, node_ids: Any) -> List[str]:
    """把「这一批重跑哪些节点」收口成一份**有序、无重复、元素非空**的名单（§4.2 ④）。

    **顺序是有意义的**（调用方给的那个顺序就是执行顺序，见 `_do_retry_subtasks`），
    所以这里只做校验与去重检查、**一个字都不许重排** —— 不要顺手 `sorted()`，
    那会把「先后」丢掉或改成另一套。

    三种坏报文都在门口拒（1001）：① 不是列表 / 空列表；② 元素不是非空字符串；
    ③ 有重复。③ 单列一条是因为它的成因与后果都更具体 —— 同一个节点在名单里出现两次
    时「第 i 个等第 i-1 个」会算出**自己等自己**这条自环，不拦的话当场成环、两行互等，
    而那本来只是调用方拼参数时的一次手滑。
    """
    if not isinstance(node_ids, list) or not node_ids:
        raise InvalidParameterError(
            message=translate(
                "err.task_center.subtask.retry_batch_empty",
                fallback="要重跑的节点名单 node_ids 不能为空",
            ),
            details={"task_id": task_id, "node_ids": node_ids},
        )
    for node_id in node_ids:
        if not isinstance(node_id, str) or not node_id.strip():
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.subtask.retry_batch_empty",
                    fallback="要重跑的节点名单 node_ids 不能为空",
                ),
                details={"task_id": task_id, "node_ids": node_ids},
            )
    seen: set[str] = set()
    duplicated: List[str] = []
    for node_id in node_ids:
        if node_id in seen and node_id not in duplicated:
            duplicated.append(node_id)
        seen.add(node_id)
    if duplicated:
        raise InvalidParameterError(
            message=translate(
                "err.task_center.subtask.retry_batch_duplicated",
                params={"node_ids": "、".join(duplicated)},
                fallback=f"节点 {'、'.join(duplicated)} 在名单里出现了多次",
            ),
            details={"task_id": task_id, "duplicated_node_ids": duplicated},
        )
    return list(node_ids)


def _batch_in_flight_snapshots(
    rows: List[Subtask],
) -> Optional[List[SubtaskItemResponse]]:
    """这一批是不是**全都已经排上队了**（上一次其实成了、只是响应没回来）？

    是就返回它们现在这份快照（调用方按「已经重跑了」收工）；不是就返回 `None`，
    照常走三条前提。

    判据逐条与单步那档（`TaskService._retry_already_in_flight`）对齐，只是**要求整批
    都满足**：`current_attempt > 1` 且状态是 `pending` / `running`。

    ⚠️ **必须是「全中」而不是「中一个」**：批量是**一个事务**，不存在「一半落库了」。
    只要有一个还是老样子（`current_attempt == 1`），就说明**上一次整批都没生效**，
    这次该照常走前提去拒或去跑 —— 拿「有一个像那么回事」当成功，会把一次真没生效的
    重跑谎报成「已排上队」。

    ⚠️ 与单步那处同一句忠告：**它一个字都不改**，返回的是读来的快照。真正落库的那一次
    已经把该写的都写了；这里再写一遍就是把同一个重跑做第二次。
    """
    for row in rows:
        if row.current_attempt <= 1:
            return None
        if row.status not in (SubtaskStatus.PENDING.value, SubtaskStatus.RUNNING.value):
            return None
    return [SubtaskItemResponse(**row.to_dict()) for row in rows]


def _require_no_cycle_after_adding(
    task_id: str,
    by_key: Dict[str, Subtask],
    added: Dict[str, List[str]],
) -> None:
    """把「批内先后」连成依赖之后，整张依赖图**不许出现环**（成环 → 1001）。

    调的就是发版校验查 N1 用的那份实现（`flow_validator._detect_cycles`，DFS 三色）——
    **不另抄一份**：环的判法只该有一处，两处迟早会分叉（一处认自环、一处不认）。

    ## 为什么门口就得拒

    反向给名单（把下游排在前面）会让两行**互相等**：A 等 B 成功、B 等 A 成功，
    两边都不是 `success`，派活侧（`_deps_satisfied`）永远判「依赖没满足」，
    这两步永远不跑、任务永远收不了尾。这跟发版时把编排连成环是同一类错误 ——
    只是这次环是**调用方这一次请求**连出来的，编排本身没问题。

    ## 为什么只查「新加的边」也要拿全图查

    `added` 里只有这次新连的那几条，但**环可能绕经没被改动的节点**：编排里本来就写着
    `compile_1 → parse_1` 时（反向编排），把 `parse_1` 排在 `compile_1` 前面并不会新连
    什么边 —— 环早就存在，只是平时顺序跑不到。所以 `edges` 用的是**每个节点的完整依赖**
    （改动过的用新的、其余用现有的），不是只有 `added`。
    """
    edges: Dict[str, List[str]] = {
        node_id: list(added.get(node_id) or (row.depends_on or []))
        for node_id, row in by_key.items()
    }
    issues = _detect_cycles(edges)
    if not issues:
        return
    raise InvalidParameterError(
        message=translate(
            "err.task_center.subtask.retry_batch_cycle",
            params={"node_ids": " → ".join(added.keys()), "cycle": "；".join(issues)},
            fallback=(
                "按这个顺序重跑会让节点互相等待、永远跑不起来："
                f"{'；'.join(issues)}；请按编排里依赖的方向给名单（上游在前）"
            ),
        ),
        details={"task_id": task_id, "added_depends_on": added, "issues": issues},
    )


__all__ = ["RunContext", "TaskService"]
