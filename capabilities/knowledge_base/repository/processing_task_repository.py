#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""处理任务事件埋点落库（解析 / 抽取 / 编译三阶段）。

与 `search_event_repository.py` 的分工：
- 检索线（请求/步骤/命中/采用）的事件由 `services/search_event_writer.py` 组装；
- **处理线只有本模块**，没有对应的 service 层转发 —— 5 个接线点全部直接调用
  下面的 `record_processing_task`。不在 service 层再套一层同名转发：那是没有
  任何调用者的死代码，只会让「埋点写在哪」多出一个需要同步的副本。

⚠️ **事务纪律（最容易写错的一处）**：`session` 是**必填的第一个形参**，必须复用
调用方当前已有的 session —— 这 5 个落点全都跑在别人已经开着的事务里。在这里
`async with get_db_session()` 另开一个 session 会与外层事务争同一行锁 → 死锁，
或把外层的未提交更新读成旧值 → 丢更新。

埋点失败**绝不影响主流程**：内部吞掉全部异常，但必须留日志（否则永久静默）。
"""

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.document import DocStatus, OntologyStatus
from ..models.search_event import ProcessingTaskEvent
from .search_event_repository import SearchEventRepository

logger = logging.getLogger(__name__)

# ── task_type 取值（与前端 DoStage / i18n dataOperations.stage.* 一一对应）──
TASK_TYPE_PARSE = "parse"
TASK_TYPE_EXTRACT = "extract"
TASK_TYPE_COMPILE = "compile"

# ── 通用状态取值（事件表 status 列）──
TASK_STATUS_RUNNING = "running"
TASK_STATUS_SUCCESS = "success"
TASK_STATUS_FAILED = "failed"

# error 列截断宽度：与既有编译错误落库口径一致（openkb_service / reconciliation_service
# 都是 error[:1000]），不要在这里另立一套。
_ERROR_MAX_LEN = 1000

# ── 状态映射表（源状态 → 事件表 status）──
# 只映射「处理中 / 终态」，其余源状态（pending / deleting / deleted 等）**不落事件**：
# pending 是「排队待处理」而不是「处理已开始」，落 running 会产生永久僵尸行
# （后续没有对应终态来收它，而 running 会被 pipeline_task_stats.running 永远算进去）。
PARSE_STATUS_MAP = {
    DocStatus.PARSING.value: TASK_STATUS_RUNNING,
    DocStatus.READY.value: TASK_STATUS_SUCCESS,
    DocStatus.FAILED.value: TASK_STATUS_FAILED,
}
EXTRACT_STATUS_MAP = {
    OntologyStatus.EXTRACTING.value: TASK_STATUS_RUNNING,
    OntologyStatus.READY.value: TASK_STATUS_SUCCESS,
    OntologyStatus.FAILED.value: TASK_STATUS_FAILED,
}
# 编译状态是裸字符串列（llm_wiki_compile_status，无 Enum）：
# 只能是 compiling / compiled / failed 三档参与任务；
# **`stale` 不进表** —— 它是 reparse 打的「待重编」标记、不是终态，
# 落成 failed 是错的，落成 running 则没有终态来收（变僵尸）。
COMPILE_STATUS_MAP = {
    "compiling": TASK_STATUS_RUNNING,
    "compiled": TASK_STATUS_SUCCESS,
    "failed": TASK_STATUS_FAILED,
}


def build_processing_task_id(asset_id: str, task_type: str, content_generation: int | None) -> str:
    """确定性组合键 `{asset_id}:{task_type}:{content_generation}`。

    不用 RAG / 编译任务 id：那几个可空（提交前就失败时根本没有）、reparse 会换，
    且解析与抽取可能共用同一个 `rag_task_id` —— 拿它做幂等键会互相覆盖。
    `content_generation` 是 reparse 代次，天然区分「同一资产的第 n 轮处理」，
    且它正是 `claim_ontology_for_retry` 的 CAS 条件之一，语义一致。

    长度：64 + 1 + 7 + 1 + ≤4 ≈ 77 ≤ VARCHAR(128) ✔
    """
    return f"{asset_id}:{task_type}:{int(content_generation or 0)}"


async def record_processing_task(
    session: AsyncSession,
    tenant_id: str,
    *,
    asset_id: str,
    task_type: str,
    status: str,
    doc_id: str | None = None,
    kb_id: str | None = None,
    content_generation: int | None = 0,
    error: str | None = None,
    started_at=None,
    finished_at=None,
) -> bool:
    """写一行处理任务事件（同一 processing_task_id 幂等 upsert）。

    返回 True = 已写；False = 被门禁挡下 / 写入失败（调用方不需要关心，
    埋点成败不影响主流程）。

    时间戳口径：**不传时一律让 DB 用 `func.now()` 填**，不在 Python 侧
    `datetime.now()` —— `processing_task_event.started_at/finished_at` 是
    `TIMESTAMPTZ`，而 asyncpg 对 naive datetime 按 **UTC** 编码，本地（+08）
    naive 值写进去会差 8 小时。让 DB 时钟说话与其余事件列（`received_at` 等
    全部默认 `NOW()`）口径一致。回填脚本要覆盖历史时刻时才显式传值。
    """
    # 门禁：缺租户 / 缺资产 / 缺阶段一律不写（绝不落脏行）
    if not tenant_id or not asset_id or not task_type:
        logger.warning(
            "[jonex] 处理任务埋点缺关键字段，已跳过：tenant=%r asset=%r task_type=%r",
            tenant_id, asset_id, task_type,
        )
        return False

    try:
        if status == TASK_STATUS_RUNNING:
            # 开工：带 started_at，且**显式清空** finished_at / error ——
            # 同一 processing_task_id 可能被重跑（reparse 走的是新代次，但
            # 同一代次内重复进入处理中的情形仍要保证不会残留上一轮终态）。
            _started = started_at if started_at is not None else func.now()
            _finished = None
            _error = None
        elif status in (TASK_STATUS_SUCCESS, TASK_STATUS_FAILED):
            # 收工：started_at 不传（仓储层用 coalesce 保住第一次写的开工时间）
            _started = started_at
            _finished = finished_at if finished_at is not None else func.now()
            _error = (error or None)
            if _error is not None:
                _error = _error[:_ERROR_MAX_LEN]
        else:
            logger.warning(
                "[jonex] 处理任务埋点收到非法 status=%r，已跳过（task_type=%s asset=%s）",
                status, task_type, asset_id,
            )
            return False

        repo = SearchEventRepository(session)
        await repo.upsert_processing_task_event(
            tenant_id,
            processing_task_id=build_processing_task_id(
                doc_id or asset_id, task_type, content_generation
            ),
            asset_id=asset_id,
            task_type=task_type,
            status=status,
            error=_error,
            started_at=_started,
            finished_at=_finished,
            kb_id=kb_id,
        )
        return True
    except Exception:  # noqa: BLE001 — 埋点失败不得影响主流程，但必须留日志
        logger.warning(
            "[jonex] 处理任务埋点写入失败（不影响主流程）：asset=%s task_type=%s status=%s",
            asset_id, task_type, status, exc_info=True,
        )
        return False


async def record_compile_task(
    session: AsyncSession,
    tenant_id: str,
    *,
    document_id: str,
    compile_status: str,
    kb_id: str | None = None,
    content_generation: int | None = 0,
    error: str | None = None,
) -> bool:
    """编译状态（`llm_wiki_compile_status` 的裸字符串）→ 事件行的薄封装。

    三个回写点（`openkb_service._record_openkb_status`、
    `reconciliation_service._record_openkb_status` / `_record_openkb_status_by_id`）
    的映射口径只住这里一处：

    - `compiling/compiled/failed` → running/success/failed；
    - **`stale` 一律不落事件** —— 它是 reparse 打的「待重编」标记、不是终态。
      落 failed 是错的（它没失败）；落 running 则没有终态来收它，会变成
      `pipeline_task_stats.running` 里永远清不掉的僵尸行。

    `error` 只在 failed 档传给事件行（success 带一段旧错误会误导排障）。
    """
    task_status = COMPILE_STATUS_MAP.get(compile_status)
    if not task_status:
        return False
    return await record_processing_task(
        session, tenant_id,
        asset_id=document_id, task_type=TASK_TYPE_COMPILE, status=task_status,
        doc_id=document_id, kb_id=kb_id, content_generation=content_generation,
        error=error if task_status == TASK_STATUS_FAILED else None,
    )


__all__ = [
    "COMPILE_STATUS_MAP",
    "EXTRACT_STATUS_MAP",
    "PARSE_STATUS_MAP",
    "TASK_STATUS_FAILED",
    "TASK_STATUS_RUNNING",
    "TASK_STATUS_SUCCESS",
    "TASK_TYPE_COMPILE",
    "TASK_TYPE_EXTRACT",
    "TASK_TYPE_PARSE",
    "build_processing_task_id",
    "record_compile_task",
    "record_processing_task",
]
