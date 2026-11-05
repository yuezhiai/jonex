# -*- coding: utf-8 -*-
"""W4.3b 单元格级溯源（MVP）：输出单元格 → 指标/输入/落点/字段 + SQL 快照。

设计：docs/superpowers/specs/2026-11-01-w4.3b-cell-provenance-design.md（工作区文档，不入库）。

边界：
- **不抓源行**：聚合结果与源行不是一一映射，逐行抓取会再造一条查询、且无法证明
  「这一行参与了这次聚合」。MVP 给的是**可复算的溯源描述**：单元格 → 指标定义 →
  输入指标（展开到叶子）→ 落点表/实体 → 度量字段/聚合 → SQL 哈希 + 执行时刻。
- 只对**数值列（最后一列）**生成单元格；分组维度放在 `group` 里，便于按行定位。
- 单元格上限 `METRIC_CELL_PROVENANCE_MAX_CELLS`（默认 200）；超出显式 `truncated`。
"""
from __future__ import annotations

import datetime
import decimal
import hashlib
import logging

from jonex_core.common.config import config

logger = logging.getLogger(__name__)


def _column_name(column) -> str:
    if isinstance(column, dict):
        return str(column.get("name") or column.get("field") or "")
    return str(column or "")


def _jsonable(value):
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _index(metrics) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for metric in metrics or []:
        if isinstance(metric, dict) and metric.get("name"):
            out[str(metric["name"])] = metric
    return out


def _landing(metric: dict, data_source_id: str) -> dict:
    landings = [item for item in metric.get("landings") or []
                if isinstance(item, dict)]
    chosen = next((item for item in landings
                   if item.get("data_source_id") == data_source_id), None)
    if chosen is None and landings:
        chosen = landings[0]
    if chosen is None:
        return {}
    return {"data_source_id": chosen.get("data_source_id") or "",
            "entity_name": chosen.get("entity_name") or ""}


def _node(metric: dict, *, data_source_id: str, index: dict[str, dict],
          seen: set[str], depth: int = 0) -> dict:
    name = str(metric.get("name") or "")
    executable = metric.get("executable") or {}
    metric_type = str(executable.get("metric_type") or "")
    node = {"name": name, "metric_type": metric_type,
            "landing": _landing(metric, data_source_id)}
    if depth >= 8 or name in seen:
        node["degraded"] = "cycle_or_depth"
        return node
    seen = {*seen, name}
    if metric_type == "simple":
        node["measure"] = executable.get("measure") or {}
        return node
    inputs = []
    for item in executable.get("inputs") or []:
        if not isinstance(item, dict):
            continue
        child = index.get(str(item.get("name") or ""))
        if child is None:
            inputs.append({"name": str(item.get("name") or ""),
                           "unresolved": True})
            continue
        inputs.append(_node(child, data_source_id=data_source_id,
                            index=index, seen=seen, depth=depth + 1))
    node["inputs"] = inputs
    return node


def _leaf_names(node: dict) -> list[str]:
    if not isinstance(node, dict):
        return []
    children = node.get("inputs")
    if not children:
        return [node.get("name")] if node.get("name") else []
    leaves: list[str] = []
    for child in children:
        leaves.extend(_leaf_names(child))
    return leaves


def build_cell_provenance(*, metric: dict, metrics, data_source_id: str,
                          columns, rows, sql: str, executed_at: str,
                          max_cells: int | None = None) -> dict:
    """构造单元格级溯源；无可用数值列 → `available=false`（不伪造）。"""
    names = [_column_name(column) for column in columns or []]
    if not names or not rows:
        return {"available": False, "reason": "no_numeric_column",
                "cells": [], "truncated": False}
    value_index = len(names) - 1
    value_column = names[value_index]
    limit = int(max_cells if max_cells is not None
                else config.METRIC_CELL_PROVENANCE_MAX_CELLS)
    if limit <= 0:
        limit = 200
    index = _index(metrics)
    root = _node(metric, data_source_id=data_source_id, index=index, seen=set())
    sql_hash = hashlib.sha256(str(sql or "").encode("utf-8")).hexdigest()
    cells: list[dict] = []
    for row_index, row in enumerate(rows):
        if row_index >= limit:
            break
        values = list(row or [])
        if value_index >= len(values):
            continue
        group = {names[i]: _jsonable(values[i])
                 for i in range(value_index) if i < len(values)}
        cells.append({
            "row": row_index,
            "column": value_column,
            "value": _jsonable(values[value_index]),
            "group": group,
            "source_inputs": _leaf_names(root),
        })
    return {
        "available": True,
        "value_column": value_column,
        "column_index": value_index,
        "total_rows": len(rows),
        "cells": cells,
        "truncated": len(rows) > limit,
        "sql_sha256": sql_hash,
        "executed_at": executed_at,
        "lineage": {
            "metric": root,
            "landing": _landing(metric, data_source_id),
        },
    }


__all__ = ["build_cell_provenance"]
