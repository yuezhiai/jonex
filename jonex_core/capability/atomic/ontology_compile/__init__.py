# -*- coding: utf-8 -*-
"""[jonex] 本体编译原子能力（atomic.ontology_compile.v1）。

P1：灌 LightRAG（A18 前置段）+ 抽本体（含 Row-as-Object）+ custom-kg 回写的代码
从 raganything 搬入本包；宿主依赖经 CompileDeps 显式注入。
P2：加 capability/worker/独立容器；P3/P4 接解析切换与 raganything 退役。

红线：本包不得 import raganything（KB 容器无该依赖）。
"""
from __future__ import annotations

from .deps import CompileDeps, FlowHooks, TableGrid, build_deps
from .ingest import ingest_engine_chunks
from .sanitize import (
    _fragment_piece_form,
    _fragments_match_content,
    _sanitize_for_dedupe,
)
from .service import CompileCtx, run_ontology_only
from .table_context import (
    _as_int,
    _assemble_table_item,
    _load_table_context,
    _merge_row_fragments,
    _parse_anchor_kv,
    _persist_table_context,
    _split_cells,
    _strip_selfdesc_cells,
    _sweep_table_context,
    _table_context_dir,
    _table_context_items,
    _table_context_items_from_chunks,
    _table_context_path,
    _write_table_context,
)

__all__ = [
    "OntologyCompileCapability",
    "CompileCtx",
    "CompileDeps",
    "FlowHooks",
    "TableGrid",
    "build_deps",
    "ingest_engine_chunks",
    "run_ontology_only",
    "_fragment_piece_form",
    "_fragments_match_content",
    "_sanitize_for_dedupe",
    "_as_int",
    "_assemble_table_item",
    "_load_table_context",
    "_merge_row_fragments",
    "_parse_anchor_kv",
    "_persist_table_context",
    "_split_cells",
    "_strip_selfdesc_cells",
    "_sweep_table_context",
    "_table_context_dir",
    "_table_context_items",
    "_table_context_items_from_chunks",
    "_table_context_path",
    "_write_table_context",
]

# ⚠️ 必须在**末尾** re-export：`deploy/start_capability.py` 按**包**取类
# (`getattr(import_module("jonex_core.capability.atomic.ontology_compile"), "OntologyCompileCapability")`)，
# 不 re-export 则 AttributeError → 容器启动崩循环。
# 放末尾是因为 `capability` 子模块会 import `task_store` / `worker`，先跑完上面的 import 更稳。
from .capability import OntologyCompileCapability  # noqa: E402,F401
