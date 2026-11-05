#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""小 schema 预览（P2-2）：给「所有其他表 + 列名」，不灌全量列细节。

目标桶：BIRD 表集合错 84（缺 39 / 多 22 / 换 23）。当前 payload 只给锚点 + 1 跳，
漏掉 2 跳/未注入表时模型会猜表名。对**小 schema**（≤ N 表）给一份紧凑预览，
让模型知道这些表存在、可以 join；详细列/样例仍以锚点 + 1 跳为主。

安全：
- 只来自该源编译 schema（平台认识的表），预览表会并入安全闸白名单；
- 有治理策略（`gov_active`）时**不生成预览**（列名可能敏感，先不做列级过滤）；
- 大 schema（> N 表）直接不生成，避免 prompt 撑爆。
"""
from __future__ import annotations

import re

from ..evidence_probe import evidence_key

MAX_PREVIEW_TABLES = 15
MAX_COLUMNS_PER_TABLE = 20
MAX_PREVIEW_CHARS = 3000

#: P2-4a：预览列示例值（`NL2SQL_PREVIEW_EXAMPLES`）。只读**已存证据**，不查源库。
MAX_EXAMPLES_PER_COLUMN = 3
EXAMPLES_CHAR_BUDGET = 3000
MAX_EXAMPLE_VALUE_CHARS = 48

#: 预览列明细（2026-10-30）：列带 `type` + 截断后的 `description`（来自编译 schema）。
MAX_DESC_CHARS = 80
DETAILS_CHAR_BUDGET = 4000

#: P2-4a v2（2026-10-29）：预览示例先剔除噪声 —— ID 类高基数列整体不给、
#: 其他高基数列只留 1 个格式样例、同一值不在多列/多表重复出现。
ID_LIKE_DISTINCT = 20
HIGH_CARDINALITY_DISTINCT = 50
_ID_LIKE = re.compile(r"(^id$|_id$|^id_)", re.IGNORECASE)


def _table_key(entity: dict) -> str:
    return (str(entity.get("source_object_id") or "").strip()
            or str(entity.get("name") or "").strip())


def _column_entries(entity: dict) -> list[dict]:
    """列明细（物理名/逻辑名/类型/描述），按物理名去重、保持 schema 顺序。"""
    entries: list[dict] = []
    seen: set[str] = set()
    for attr in entity.get("attributes") or []:
        if not isinstance(attr, dict):
            continue
        logical = str(attr.get("name") or "").strip()
        physical = str(attr.get("physical_name") or "").strip() or logical
        if not physical or physical in seen:
            continue
        seen.add(physical)
        entries.append({
            "name": physical,
            "logical": logical or physical,
            "type": str(attr.get("type") or "").strip(),
            "description": str(attr.get("description") or "").strip()[:MAX_DESC_CHARS],
        })
        if len(entries) >= MAX_COLUMNS_PER_TABLE:
            break
    return entries


def _column_examples(entity: dict, entries: list[dict],
                     tables_evidence: dict | None, *, limit: int,
                     budget: int, seen_values: set[str]) -> dict[str, list]:
    """从该表的**已存证据**里取示例值；预算用完即停（按表序→列序，确定性）。

    v2 噪声剔除：ID 类高基数列整体不给；其他高基数列只留 1 个格式样例；
    `seen_values` 跨列/跨表全局去重（同一个 "1" 不再在每个 id 列重复）。
    """
    entry = ((tables_evidence or {}).get(evidence_key(entity)) or {})
    ev_columns = entry.get("columns") or {}
    picked: dict[str, list] = {}
    remaining = max(0, int(budget))
    for entry_col in entries:
        physical = entry_col["name"]
        logical = entry_col["logical"]
        ev = ev_columns.get(logical) or ev_columns.get(physical) or {}
        try:
            distinct = int(ev.get("distinct_in_sample") or 0)
        except (TypeError, ValueError):
            distinct = 0
        if _ID_LIKE.search(physical) and distinct > ID_LIKE_DISTINCT:
            continue                      # ID 类高基数列的样例对 join 没有信息量
        cap = 1 if distinct > HIGH_CARDINALITY_DISTINCT else max(1, int(limit))
        values = list(ev.get("samples") or [])[:cap]
        chosen: list[str] = []
        for raw in values:
            text = str(raw).strip()
            if not text or text in seen_values:
                continue
            text = text[:MAX_EXAMPLE_VALUE_CHARS]
            if text in seen_values:
                continue
            cost = len(text) + 2          # JSON 里一对引号
            if cost > remaining:
                if chosen:
                    picked[physical] = chosen
                    seen_values.update(chosen)
                return picked
            chosen.append(text)
            seen_values.add(text)
            remaining -= cost
        if chosen:
            picked[physical] = chosen
    return picked


def build_schema_preview(entity_types: list, *, anchor: str,
                         related_tables: list | None = None,
                         max_tables: int = MAX_PREVIEW_TABLES,
                         max_chars: int = MAX_PREVIEW_CHARS,
                         include_examples: bool = False,
                         include_details: bool = False,
                         tables_evidence: dict | None = None,
                         example_limit: int = MAX_EXAMPLES_PER_COLUMN,
                         example_budget: int = EXAMPLES_CHAR_BUDGET,
                         detail_budget: int = DETAILS_CHAR_BUDGET
                         ) -> tuple[list[dict], set[str]]:
    """`(preview, tables)`；空列表 = 不注入（payload 与关档逐字节相同）。

    `include_examples=True` 时，每张预览表额外带 `column_examples: {物理列: [值]}`；
    值只来自**已存证据**（编译行的 `evidence.tables`），不新增源库查询。
    """
    entities = [e for e in (entity_types or []) if isinstance(e, dict)]
    if not entities or len(entities) > max(1, int(max_tables)):
        return [], set()
    skip = {str(anchor or "").casefold()}
    for table in related_tables or []:
        skip.add(str((table or {}).get("table") or "").casefold())
    preview: list[dict] = []
    tables: set[str] = set()
    used = 0
    remaining_examples = max(0, int(example_budget))
    remaining_details = max(0, int(detail_budget))
    seen_values: set[str] = set()
    for entity in entities:
        table = _table_key(entity)
        name = str(entity.get("name") or "").strip()
        if not table or table.casefold() in skip or name.casefold() in skip:
            continue
        column_entries = _column_entries(entity)
        if not column_entries:
            continue
        if include_details:
            columns = []
            detail_cost = 0
            for col in column_entries:
                item = {"name": col["name"]}
                if col["type"]:
                    item["type"] = col["type"]
                if col["description"]:
                    item["description"] = col["description"]
                columns.append(item)
                detail_cost += sum(len(str(v)) + 6 for v in item.values())
            if detail_cost > remaining_details:
                break
            remaining_details -= detail_cost
        else:
            columns = [col["name"] for col in column_entries]
        entry = {"table": table, "entity": name, "columns": columns}
        cost = len(table) + len(name) + 8
        cost += (sum(len(str(v)) for c in columns for v in c.values())
                 if include_details else sum(len(c) for c in columns))
        if used + cost > max(1, int(max_chars)):
            break
        if include_examples:
            examples = _column_examples(
                entity, column_entries, tables_evidence,
                limit=example_limit, budget=remaining_examples,
                seen_values=seen_values)
            if examples:
                entry["column_examples"] = examples
                remaining_examples -= sum(
                    len(v) + 2 for vals in examples.values() for v in vals)
        preview.append(entry)
        tables.add(table)
        used += cost
    return preview, tables


__all__ = ["build_schema_preview", "MAX_PREVIEW_TABLES",
           "MAX_COLUMNS_PER_TABLE", "MAX_PREVIEW_CHARS",
           "MAX_EXAMPLES_PER_COLUMN", "EXAMPLES_CHAR_BUDGET",
           "MAX_EXAMPLE_VALUE_CHARS", "MAX_DESC_CHARS", "DETAILS_CHAR_BUDGET"]
