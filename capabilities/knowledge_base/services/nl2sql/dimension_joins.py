#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""维度标签 join（2026-11-01）。

失败分析：`table` 桶 20 条里 18 条 join 数不同，缺的表集中在 continent/teams/
country/province/city——模型拿到事实表的 code 列就直接投影/过滤，不 join 维度表
取人类可读名称。这里只挑**事实→维度**的边（维度表有 name/title/city 等标签列），
并在问句要标签时注入提示；不是通用 join 图。
"""
from __future__ import annotations

import re

MAX_DIMENSION_JOINS = 8
_WORD = re.compile(r"[A-Za-z0-9_]+")
_LABEL_WORDS = re.compile(
    r"\b(name|names|which|who|whom|whose|list|state|give|find|show|"
    r"team|teams|country|countries|city|cities|province|continent|player|"
    r"customer|vendor|department|employee|company|school|movie|film)\b",
    re.IGNORECASE,
)
_LABEL_EXACT = {
    "name", "names", "title", "label", "fullname", "full_name", "nickname",
    "firstname", "lastname", "first_name", "last_name", "namegiven",
    "namenick", "city", "country", "province", "continent", "state", "region",
}
_GENERIC_JOIN_COLUMNS = {
    "id", "year", "date", "type", "status", "code", "value", "amount",
    "count", "number", "position", "rank", "time", "created", "updated",
    "modified", "start", "end", "first", "last",
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


def _is_label_column(name: str) -> bool:
    key = str(name or "").casefold().strip()
    if not key:
        return False
    if key in _LABEL_EXACT:
        return True
    return key.endswith("_name") or key.endswith("name") or key in ("title", "label")


def _label_columns(e: dict) -> list[str]:
    cols = []
    for logical, physical in _attrs(e):
        if _is_label_column(logical) or _is_label_column(physical):
            cols.append(physical)
    return cols


def label_question(question: str) -> bool:
    return bool(_LABEL_WORDS.search(str(question or "")))


def _question_words(question: str) -> set[str]:
    return {w.casefold() for w in _WORD.findall(str(question or "")) if len(w) >= 3}


def _selected(e: dict, sel: set[str]) -> bool:
    table = _entity_table(e).casefold()
    tail = table.split(".")[-1].strip().strip(chr(34))
    return _entity_name(e).casefold() in sel or table in sel or tail in sel


def _make_edge(fact: dict, dim: dict, fact_col: str, dim_col: str,
               score: int) -> dict:
    labels = _label_columns(dim) or [dim_col]
    return {
        "from_table": _entity_table(fact),
        "from_column": fact_col,
        "to_table": _entity_table(dim),
        "to_column": dim_col,
        "label_column": labels[0],
        "label_columns": labels,
        "_score": score,
    }


def build_dimension_joins(entity_types: list[dict] | None,
                          relation_types: list[dict] | None,
                          selected: set[str] | None,
                          question: str, *, anchor: str = "",
                          max_joins: int = MAX_DIMENSION_JOINS) -> list[dict]:
    """返回事实→维度的候选边（带 `label_column`）；无标签问句直接空。"""
    if not label_question(question):
        return []
    sel = {str(s).casefold() for s in (selected or set()) if str(s).strip()}
    entities = [e for e in (entity_types or [])
                if isinstance(e, dict) and _entity_name(e)]
    if not entities:
        return []
    if sel:
        entities = [e for e in entities if _selected(e, sel)]
    if len(entities) < 2:
        return []
    words = _question_words(question)
    by_name = {_entity_name(e).casefold(): e for e in entities}
    edges: list[dict] = []
    seen: set[tuple] = set()

    def add(fact: dict, dim: dict, fc: str, dc: str) -> None:
        if not fc or not dc or fact is dim:
            return
        key = (_entity_table(fact).casefold(), fc.casefold(),
               _entity_table(dim).casefold(), dc.casefold())
        if key in seen:
            return
        seen.add(key)
        score = 0
        dim_name = _entity_name(dim).casefold()
        if dim_name in words:
            score += 100
        if any(w and w in dim_name for w in words):
            score += 40
        if any(col.casefold() in words for col in _label_columns(dim)):
            score += 20
        edges.append(_make_edge(fact, dim, fc, dc, score))

    for rel in relation_types or []:
        if not isinstance(rel, dict):
            continue
        s = str(rel.get("source") or "").casefold()
        t = str(rel.get("target") or "").casefold()
        sc = str(rel.get("source_column") or "").strip()
        tc = str(rel.get("target_column") or "").strip()
        if s not in by_name or t not in by_name or not sc or not tc:
            continue
        source, target = by_name[s], by_name[t]
        # 哪边有标签列，哪边就是维度；两边都有时保留两个方向。
        if _label_columns(target):
            add(source, target, sc, tc)
        if _label_columns(source):
            add(target, source, tc, sc)

    # 没有声明关系时：按「维度标签列同名」保守补边（如 country/city）。
    if not edges:
        for dim in entities:
            labels = {c.casefold() for c in _label_columns(dim)}
            # 维度实体名/表尾也常是连接列（city.country = country.name）。
            labels.add(_entity_name(dim).casefold())
            labels.add(_entity_table(dim).split(".")[-1].casefold())
            if not labels:
                continue
            for fact in entities:
                if fact is dim:
                    continue
                for logical, physical in _attrs(fact):
                    key = logical.casefold()
                    if key in _GENERIC_JOIN_COLUMNS or key not in labels:
                        continue
                    add(fact, dim, physical, physical)

    edges.sort(key=lambda e: e.get("_score", 0), reverse=True)
    out = edges[:max(1, int(max_joins))]
    for edge in out:
        edge.pop("_score", None)
    return out


__all__ = ["build_dimension_joins", "label_question", "MAX_DIMENSION_JOINS"]
