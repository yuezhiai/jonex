# -*- coding: utf-8 -*-
"""W4.6 结果契约（MVP）：表格 / 图表 / 文本块的统一结构。

设计：docs/superpowers/specs/2026-11-01-w4.6-result-contract-design.md（工作区文档，不入库）。

契约只做**归一化与派生**，不改变既有顶层字段（`columns` / `rows` / `row_count`）——
聊天、报告、API 共用同一份 `blocks`，老调用方零改动。

```json
{"contract_version": "1",
 "blocks": [
   {"type": "text", "text": "销售额：42"},
   {"type": "chart", "chart_type": "line", "x": "month",
    "series": [{"name": "amount", "field": "amount"}]},
   {"type": "table", "columns": [{"name": "month", "type": "date"}],
    "rows": [["2026-01", 42]], "row_count": 1, "truncated": false}
 ]}
```

- 图表是**确定性派生**：时间列 → 折线；分类列 + 数值列 → 柱状；
  没有维度或没有数值列 → 不造图（宁可没有，不猜）。
- 类型只按值抽样推断（`integer` / `number` / `date` / `boolean` / `string` / `null`），
  不读数据库元数据。
"""
from __future__ import annotations

import datetime
import re

CONTRACT_VERSION = "1"

_TIME_NAME = re.compile(
    r"(date|time|day|month|week|quarter|year|日期|时间|月份|年月|季度|年度)",
    re.IGNORECASE,
)
_DATE_TEXT = re.compile(r"^\d{4}-\d{2}(-\d{2})?([ T]\d{2}:\d{2}(:\d{2})?)?$")


def _column_names(columns) -> list[str]:
    out: list[str] = []
    for index, column in enumerate(columns or []):
        if isinstance(column, dict):
            name = str(column.get("name") or column.get("field") or "").strip()
        else:
            name = str(column or "").strip()
        out.append(name or f"col_{index}")
    return out


def _value_type(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, (datetime.date, datetime.datetime)):
        return "date"
    text = str(value).strip()
    if _DATE_TEXT.match(text):
        return "date"
    return "string"


def _infer_type(values) -> str:
    kinds = {_value_type(value) for value in values if value is not None}
    if not kinds:
        return "null"
    for kind in ("integer", "number", "date", "boolean"):
        if kind in kinds:
            return kind
    return "string"


def normalize_table(columns, rows) -> dict:
    """列名/行归一化：列支持 `str` 或 `dict`；行支持 `list` 或 `dict`。"""
    names = _column_names(columns)
    normalized_rows: list[list] = []
    for row in rows or []:
        if isinstance(row, dict):
            normalized_rows.append([row.get(name) for name in names])
        else:
            normalized_rows.append(list(row or []))
    return {
        "columns": [
            {"name": name,
             "type": _infer_type([row[index] for row in normalized_rows
                                  if index < len(row)])}
            for index, name in enumerate(names)
        ],
        "rows": normalized_rows,
        "row_count": len(normalized_rows),
    }


def _is_numeric(column: dict) -> bool:
    return column["type"] in ("integer", "number")


def infer_chart(table: dict) -> dict | None:
    """确定性图表派生；不满足条件 → None（不猜）。"""
    columns = table.get("columns") or []
    rows = table.get("rows") or []
    if len(columns) < 2 or not rows:
        return None
    numeric = [column for column in columns if _is_numeric(column)]
    if not numeric:
        return None
    dimensions = [column for column in columns if not _is_numeric(column)]
    if not dimensions:
        return None
    x_column = dimensions[0]
    series = [{"name": column["name"], "field": column["name"]}
              for column in numeric]
    is_time = (x_column["type"] == "date"
               or bool(_TIME_NAME.search(x_column["name"])))
    return {
        "chart_type": "line" if is_time else "bar",
        "x": x_column["name"],
        "series": series,
    }


def build_blocks(*, text: str = "", table: dict | None = None,
                 chart: dict | None = None) -> list[dict]:
    blocks: list[dict] = []
    if text:
        blocks.append({"type": "text", "text": str(text)})
    if chart:
        blocks.append({"type": "chart", **chart})
    if table:
        blocks.append({"type": "table", **table})
    return blocks


def summary_text(columns, rows) -> str:
    """1×1 数值结果 → `值`；其它形状 → 空串（不编造自然语言）。"""
    if not rows or len(rows) != 1 or len(rows[0] or []) != 1:
        return ""
    value = rows[0][0]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value)


def attach_blocks(result: dict, *, text: str = "") -> dict:
    """给成功响应附加 `contract_version` + `blocks`；缺表格时只出文本块。"""
    if not isinstance(result, dict):
        return result
    if result.get("contract_version"):
        return result
    table = None
    if result.get("columns") is not None and result.get("rows") is not None:
        table = normalize_table(result.get("columns"), result.get("rows"))
        table["truncated"] = bool(result.get("truncated"))
    chart = infer_chart(table) if table else None
    result["contract_version"] = CONTRACT_VERSION
    result["blocks"] = build_blocks(text=text, table=table, chart=chart)
    return result


__all__ = [
    "CONTRACT_VERSION", "attach_blocks", "build_blocks", "infer_chart",
    "normalize_table", "summary_text",
]
