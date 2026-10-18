# -*- coding: utf-8 -*-
"""[jonex] ingest_timing 落点（P2 自带，替代 raganything 宿主）。

自 raganything/service/task_manager.py 逐字搬迁（锚点定位，非行号 carve）：
仅两处变换 —— 函数改名 `_log_ingest_timing_v2` → `log_ingest_timing`，
文件顶部补 import；函数体内其他内容（注释、日志文案、extra 键名与顺序）
一个字符都不许改。逐字校验脚本见 `.superpowers/sdd/p2s1-task-3-brief.md` Step 4。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


# ── v2 ingest_timing logger ────────────────────────────────────────────
# [jonex] §11 增补：把 v2 task.timeline 各 stage 的 elapsed_seconds 摊平成
# {stage}_ms + worker_total_ms，打一条 event=ingest_timing 结构化日志
#（message 内嵌 + extra 双写，对齐 §3.4 A 方案甲）。

_V2_TIMING_STAGES = frozenset({
    "created", "queued", "parse", "text_insert", "multimodal",
    "push_chunks", "ontology_extract", "pipeline_done",
})

# [jonex] P1-1：created/queued 是排队等待，不算入 worker 端到端耗时
_QUEUE_KEYS = frozenset({"created", "queued"})


def timing_enabled() -> bool:
    """摄入耗时埋点总开关。默认开启；显式设为 0/false/no 时关闭。

    与 `jonex_core.common.timing.timing_enabled` 同语义（此处内联是为让本包不 import
    `jonex_core.common` —— 那个包的 `__init__` 会 eager 拉起 pymilvus/neo4j/redis 等重依赖）。
    ⚠️ 若上游改了开关语义（env 名 / 取值），**两处都要改**。
    """
    return os.getenv("INGEST_TIMING_ENABLED", "true").strip().lower() not in {"0", "false", "no"}


def log_ingest_timing(task, status: str, *, force_ontology_only: bool = False, error: str = "") -> None:
    """Log v2 ingest_timing structured log from task.timeline stage timings.

    Args:
        task: TaskInfo with timeline populated.
        status: ``completed`` / ``failed`` / ``cancelled``.
        force_ontology_only: True when execution_mode==ontology_only.
        error: failure reason for failed/cancelled paths.
    """
    if not timing_enabled():
        return

    # [jonex] §11 缺口 D：失败/cancel 路径下回调不触发的 stage 没有 ended_at，
    # 此处兜底 close，确保"卡在哪个阶段"数据不丢失。
    now = datetime.now(timezone.utc)
    for s in (task.timeline or []):
        if s.ended_at is None and s.started_at is not None:
            s.ended_at = now
            s.elapsed_seconds = (now - s.started_at).total_seconds()

    stages: dict = {}
    worker_total_s = 0.0
    for s in (task.timeline or []):
        stage_name = s.stage
        if not stage_name or stage_name not in _V2_TIMING_STAGES:
            continue
        try:
            secs = float(s.elapsed_seconds or 0)
        except (TypeError, ValueError):
            secs = 0.0
        stages[f"{stage_name}_ms"] = int(secs * 1000)
        # [jonex] P1-1：created/queued 是排队，不算入 worker 端到端
        if stage_name not in _QUEUE_KEYS:
            worker_total_s += secs
    worker_total_ms = int(worker_total_s * 1000)

    # message 内嵌关键数字供 stdout/grep（方案甲）
    sorted_parts = " ".join(f"{k}={v}" for k, v in sorted(stages.items()))
    logger.info(
        "ingest_timing pipeline_version=v2 task=%s status=%s"
        " force_ontology_only=%s worker_total_ms=%s error=%s %s",
        task.task_id, status, force_ontology_only,
        worker_total_ms or "0", error or "", sorted_parts,
        extra={
            "event": "ingest_timing",
            "pipeline_version": "v2",
            "task_id": task.task_id,
            "tenant_id": getattr(task, "tenant_id", ""),
            "knowledge_base_id": getattr(task, "kb_id", ""),
            "document_id": getattr(task, "document_id", ""),
            "status": status,
            "force_ontology_only": force_ontology_only,
            "worker_total_ms": worker_total_ms,
            "error": error,
            **stages,
        },
    )
