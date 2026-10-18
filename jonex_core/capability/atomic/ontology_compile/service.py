# -*- coding: utf-8 -*-
"""[jonex] 本体编译编排入口（ontology_only 路径）。

自 raganything/service/task_manager.py L3797-3888 搬迁（P1）。行为逐字不变，
触点改写见 docs/superpowers/plans/2026-10-15-ontology-compile-p1-code-move.md §1.3。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .deps import CompileDeps
from .table_context import _load_table_context

logger = logging.getLogger(__name__)


@dataclass
class CompileCtx:
    """ontology_only / compile 步的轻量上下文。

    字段是搬迁前 raganything.pipeline.base.PipelineContext 在编译路径上被
    实际读写的子集（_run_ontology_extraction 与 _merge_table_objects 共读
    ctx.content_list / ctx.table_stats / ctx.ontology_only / ctx.file_name），
    默认值与之一致（content_list=None、table_stats={}）。
    """

    file_path: str = ""
    file_name: str = ""
    tenant_id: str = ""
    kb_id: str = ""
    doc_id: str = ""
    document_id: str = ""
    cancel_event: Any = None
    config_snapshot: dict = field(default_factory=dict)
    started_at: float = 0.0
    ontology_only: bool = False
    content_list: Optional[list] = None
    table_stats: dict = field(default_factory=dict)


async def run_ontology_only(deps: CompileDeps, task, cancel_event) -> None:
    """[jonex] P0-A: ontology-only 执行 — 只重抽本体，跳过 parse/multimodal/push。

    直接按 document_id 从 LightRAG 读实体/关系做本体归类，不需要原文件，也不
    重新解析。要求任务携带 compiled schema（否则无法归类 → 失败）。

    [jonex] P0-b：表格上下文不重新解析而是回放首轮存档——重新解析既慢、又
    必然 miss 解析缓存（下载路径含随机 uuid、缓存键含绝对路径+mtime，见
    _table_context_dir 上方注释）；只有回放才能保证重抽与首轮同口径
    （即同样产出 Row-as-Object 表格对象）。
    """
    if cancel_event.is_set():
        raise deps.cancelled_error()

    if not task.ontology_schema:
        await deps.fail_task(
            task, deps.error_config_invalid,
            "ontology_only 任务缺少 compiled schema，无法归类",
        )
        # [jonex] §11 v2 ingest_timing（ontology_only 失败路径）
        deps.log_ingest_timing(task, "failed", force_ontology_only=True,
                             error=getattr(task, "error_message", ""))
        return

    # 轻量 ctx：不解析原文件，_run_ontology_extraction 按 document_id 读
    # LightRAG chunk 原文；content_list 的表格子集由首轮存档回放（P0-b）。
    ctx = CompileCtx(
        file_path=task.file_path,
        file_name=task.name,
        tenant_id=task.tenant_id,
        kb_id=task.kb_id,
        doc_id=task.document_id or "",
        document_id=task.document_id or "",
        cancel_event=cancel_event,
        config_snapshot=task.config_snapshot or {},
    )
    ctx.started_at = time.time()
    # [jonex] P0-b：标记重抽语义——_run_ontology_extraction 的 P0-b 闸门据
    # 此把「无表格上下文存档 + 本文档确有表格 chunk」判为拒绝（而非降级）。
    ctx.ontology_only = True

    # [jonex] P0-b：回放首轮落盘的表格上下文（详见 _table_context_dir 上方
    # 注释）。命中 → ctx.content_list 非空 → table_handled /
    # _merge_table_objects / P10 批 2.4 零产出兜底三条既有逻辑按与首轮相同
    # 的口径生效，重抽结果同样含 Row-as-Object 表格对象。
    # 空列表是有效值（首轮确认无表格），正常跑；None（无存档）不在这里
    # 裁决——交 _run_ontology_extraction 的 P0-b 闸门按 chunk 判据处理
    # （本文档确有表格 chunk → 拒绝重抽并落 FAILED；无表格 → 正常跑）。
    restored = _load_table_context(task)
    if restored is None:
        logger.info(
            "[jonex][P0-b] ontology_only 无表格上下文存档（上线前入库的老文档），"
            "交本体阶段按 chunk 判据裁决: doc=%s",
            task.document_id,
        )
    else:
        ctx.content_list = restored
        logger.info(
            "[jonex][P0-b] ontology_only 回放表格上下文 %d 条（首轮口径）: doc=%s",
            len(restored), task.document_id,
        )

    await deps.hooks.run_ontology_extraction(
        task, ctx, task.document_id or "",
    )

    # ontology-only 复用已有 LightRAG doc，不产新 doc；状态收尾
    task.status = deps.status_completed
    task.progress = 1.0
    task.current_step = "done"
    await deps.save_task(task)
    # [jonex] §11 v2 ingest_timing（ontology_only 成功路径）
    deps.log_ingest_timing(task, "completed", force_ontology_only=True)
    logger.info(
        f"Task {task.task_id} completed (ontology_only): "
        f"doc={task.document_id} ontology_status={task.ontology_status}"
    )
