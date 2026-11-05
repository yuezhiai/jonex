#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""CTE 选中表之间的结构化 join 边（2026-10-31）。

只在 `NL2SQL_CTE_JOIN_EDGES=1` 且 CTE 选择器生效时使用：
- 编译 schema 已有 relation_types：取两端都在选中表里的边（物理表/列）
- 没有 relation_types（如 mondial_geo）：按选中表之间的**同名列**做保守补边，
  只认长度 ≥4 且不在泛化词表的列（country/city/continent/river/lake/...），
  每个同名列以锚点（或第一张表）为星心，避免 O(n²) 爆炸。

输出与 payload `joins` 同形：`[{from_table, from_column, to_table, to_column}]`，
只作为生成提示，安全闸仍按真实 schema 校验。
"""
from __future__ import annotations

MAX_EDGES = 30
_GENERIC = {
    "id", "name", "year", "date", "description", "url", "code", "type",
    "status", "title", "text", "value", "amount", "count", "number",
    "position", "rank", "points", "time", "created", "updated", "modified",
    "start", "end", "first", "last", "key", "display", "comment", "note",
}


def _entity_table(e: dict) -> str:
    return str(e.get("source_object_id") or e.get("name") or "").strip()


def _entity_name(e: dict) -> str:
    return str(e.get("name") or "").strip()


def _attrs(e: dict) -> list[tuple[str, str]]:
    out = []
    for a in e.get("attributes") or []:
        if not isinstance(a, dict):
            continue
        logical = str(a.get("name") or "").strip()
        physical = str(a.get("physical_name") or "").strip() or logical
        if physical:
            out.append((logical, physical))
    return out


def selected_join_edges(entity_types: list[dict], relation_types: list[dict],
                        selected: set[str], *, anchor: str = "",
                        max_edges: int = MAX_EDGES) -> list[dict]:
    sel = {str(s).casefold() for s in (selected or set()) if str(s).strip()}
    entities = {(_entity_name(e).casefold()): e for e in (entity_types or [])
                if isinstance(e, dict) and _entity_name(e)}
    if not entities or not sel:
        return []
    # 选中集合可能是物理表名或实体名；两种都认
    def is_sel(e: dict) -> bool:
        table = _entity_table(e).casefold()
        tail = table.split(".")[-1].strip().strip(chr(34))
        return _entity_name(e).casefold() in sel or table in sel or tail in sel
    chosen = [e for e in entities.values() if is_sel(e)]
    by_name = {_entity_name(e).casefold(): e for e in chosen}
    out: list[dict] = []
    seen: set[tuple] = set()
    # 1) 已声明的 relation_types
    for r in relation_types or []:
        if not isinstance(r, dict):
            continue
        s = str(r.get("source") or "").casefold()
        t = str(r.get("target") or "").casefold()
        if s not in by_name or t not in by_name:
            continue
        sc = str(r.get("source_column") or "").strip()
        tc = str(r.get("target_column") or "").strip()
        if not sc or not tc:
            continue
        key = (s, sc, t, tc)
        if key in seen:
            continue
        seen.add(key)
        out.append({"from_table": _entity_table(by_name[s]), "from_column": sc,
                    "to_table": _entity_table(by_name[t]), "to_column": tc})
        if len(out) >= max_edges:
            return out
    if out:
        return out
    # 2) 无关系图：同名列保守补边（星心优先锚点）
    anchor_key = str(anchor or "").casefold()
    hub = by_name.get(anchor_key) or (chosen[0] if chosen else None)
    if hub is None:
        return []
    hub_name = _entity_name(hub).casefold()
    hub_attrs = {logical: physical for logical, physical in _attrs(hub)}
    for e in chosen:
        name = _entity_name(e).casefold()
        if name == hub_name:
            continue
        for logical, physical in _attrs(e):
            key = logical.casefold()
            if len(key) < 4 or key in _GENERIC or key not in hub_attrs:
                continue
            hc = hub_attrs[key]
            k = (hub_name, hc, name, physical)
            if k in seen:
                continue
            seen.add(k)
            out.append({"from_table": _entity_table(hub), "from_column": hc,
                        "to_table": _entity_table(e), "to_column": physical})
            if len(out) >= max_edges:
                return out
    return out


__all__ = ["selected_join_edges", "MAX_EDGES"]
