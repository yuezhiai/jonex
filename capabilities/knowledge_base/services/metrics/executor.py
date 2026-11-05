# -*- coding: utf-8 -*-
"""编译后指标的只读执行 helper（W3.3 M2）。

与 ② 的最终执行同一套安全口径：方言归一 → `guard_sql`（只读、单语句、
表白名单、禁止同查、行数上限）→ `run_readonly`（语句级超时）。

**治理改写不在本层**：本层只服务影子对比（M4，只记录不替换）；若要把结果
返回给人，M4 在调用侧挂 `governance.sql_rewrite.apply_governance`，与 ② 一致。
"""
from __future__ import annotations

from jonex_core.common.config import config

from ..nl2sql.guard import guard_sql, normalize_dialect_sql
from ..nl2sql.runner import run_readonly
from .compiler import CompiledMetric


class MetricExecutionRejected(Exception):
    """被安全闸拒绝；`reason` 是 guard 的中文原因，原样供观测/诊断。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def execute_metric(
    compiled: CompiledMetric,
    *,
    source,
    cfg: dict,
    engine: str,
    max_rows: int | None = None,
    forbidden_pairs=None,
    timeout: float | None = None,
) -> dict:
    """执行一条编译好的指标 SQL，返回适配器的 `{columns, rows, row_count}`。"""
    try:
        limit = int(max_rows if max_rows is not None else config.NL2SQL_MAX_ROWS)
    except (TypeError, ValueError):
        limit = int(config.NL2SQL_MAX_ROWS)
    sql = normalize_dialect_sql(compiled.sql, engine)
    guarded = guard_sql(
        sql,
        engine=engine,
        max_rows=max(1, limit),
        allowed_tables=set(compiled.tables),
        forbidden_pairs=forbidden_pairs,
    )
    if not guarded.ok:
        raise MetricExecutionRejected(guarded.reason)
    return await run_readonly(source, cfg, guarded.sql, timeout=timeout)


__all__ = ["MetricExecutionRejected", "execute_metric"]
