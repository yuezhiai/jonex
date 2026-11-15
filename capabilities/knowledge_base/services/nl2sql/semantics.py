#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""N1-5：列级语义条目的来源（provenance）、优先级与 digest。

同一列的一个语义字段可能来自多路：

- `derived`：从库表结构/注释/命名推断出来的（生成器产出，**最低优先**）；
- `annotated`：人工或上游标注、但未经本项目人工确认（如外部元数据导入）；
- `manual`：本项目里人工确认过的（抽象 schema 落点、编译器编辑器保存，**最高优先**）。

规则（与路线图 §3.16 N1-5 的验收门一致）：

1. **人工确认必胜**：`manual > annotated > derived`；同源冲突按规范化值的字典序
   取最小（确定性，不依赖输入顺序），输掉的值留在 `semantic_conflicts` 里可审计。
2. **digest 只覆盖语义字段**（`role/unit/grain/values/description`，按列名排序）：
   结构（类型/物理列名）变了由 schema 版本管；语义变了则 payload 指纹与评测 series
   必须能区分。
3. 没有显式来源的旧行按 `derived` 处理（行为与引入前一致：值仍然生效，只是来源最低）。

本模块纯函数：不读库、不调模型、不改入参。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Optional

__all__ = [
    "FIELDS",
    "SOURCE_RANK",
    "attribute_digest",
    "canonical_value",
    "columns_digest",
    "merge_contribution",
    "resolve_attribute",
    "resolve_field",
    "row_digest",
]

#: 参与来源/优先级/digest 的语义字段（顺序即 digest 的规范化顺序）。
FIELDS = ("role", "unit", "grain", "values", "description")

#: 来源优先级：数值大者胜。
SOURCE_RANK = {"derived": 0, "annotated": 1, "manual": 2}

_MAX_TEXT = 64
_MAX_DESCRIPTION = 500
_MAX_VALUES = 50


def canonical_value(field: str, value: Any) -> Any:
    """字段值的规范化形状：字符串 strip/截断；`values` 去空去重保序（前 50）。"""
    if field == "values":
        seen: set[str] = set()
        out: list[str] = []
        for item in value or ():
            text = str(item or "").strip()[:_MAX_TEXT]
            if text and text not in seen:
                seen.add(text)
                out.append(text)
        return out[:_MAX_VALUES]
    if value is None:
        return ""
    limit = _MAX_DESCRIPTION if field == "description" else _MAX_TEXT
    return str(value).strip()[:limit]


def _is_empty(value: Any) -> bool:
    return value in ("", [], None)


def _entries(attr: dict, field: str) -> list[dict]:
    """该字段的全部来源贡献；旧行（无 `semantic_sources`）按 `derived` 补一条。"""
    raw = (attr.get("semantic_sources") or {}).get(field)
    if isinstance(raw, dict):
        raw = [raw]
    entries: list[dict] = []
    for item in raw or ():
        if not isinstance(item, dict):
            continue
        value = canonical_value(field, item.get("value"))
        if _is_empty(value):
            continue
        source = str(item.get("source") or "derived").strip().lower()
        if source not in SOURCE_RANK:
            source = "derived"
        entries.append({"source": source, "value": value})
    if not entries:
        legacy = canonical_value(field, attr.get(field))
        if not _is_empty(legacy):
            entries.append({"source": "derived", "value": legacy})
    return entries


def resolve_field(attr: dict, field: str) -> Optional[dict]:
    """单个字段的胜出值 + 来源 + 落败贡献；无贡献返回 `None`。"""
    entries = _entries(attr, field)
    if not entries:
        return None
    best_rank = max(SOURCE_RANK[e["source"]] for e in entries)
    top = [e for e in entries if SOURCE_RANK[e["source"]] == best_rank]
    top.sort(key=lambda e: json.dumps(e["value"], ensure_ascii=False, sort_keys=True))
    winner = top[0]
    conflicts = [e for e in entries if e is not winner]
    return {"value": winner["value"], "source": winner["source"],
            "conflicts": conflicts}


def resolve_attribute(attr: dict) -> dict:
    """一个属性（列）的生效语义：`{field: {value, source, conflicts}}`。"""
    out: dict = {}
    for field in FIELDS:
        resolved = resolve_field(attr, field)
        if resolved is not None:
            out[field] = resolved
    return out


def merge_contribution(attr: dict, field: str, value: Any, source: str) -> dict:
    """追加一条来源贡献并重新结算普通字段；返回**新 dict**（不改入参）。

    `semantic_sources[field]` 保留全部贡献（去重同值同源）；`semantic_conflicts[field]`
    记录落败值（供审计/调试），普通字段写胜出值 —— 下游（提示词/payload）只读普通字段。
    """
    if field not in FIELDS:
        raise ValueError(f"unsupported semantic field: {field}")
    src = str(source or "derived").strip().lower()
    if src not in SOURCE_RANK:
        raise ValueError(f"unsupported semantic source: {source}")
    value = canonical_value(field, value)
    if _is_empty(value):
        return dict(attr)
    out = dict(attr)
    sources = dict(out.get("semantic_sources") or {})
    items = sources.get(field)
    if isinstance(items, dict):
        items = [items]
    items = [dict(i) for i in (items or ()) if isinstance(i, dict)]
    if not any(str(i.get("source") or "derived").lower() == src
               and canonical_value(field, i.get("value")) == value for i in items):
        items.append({"source": src, "value": value})
    sources[field] = items
    out["semantic_sources"] = sources

    resolved = resolve_field(out, field)
    if resolved is not None:
        out[field] = resolved["value"]
        conflicts = dict(out.get("semantic_conflicts") or {})
        if resolved["conflicts"]:
            conflicts[field] = resolved["conflicts"]
        else:
            conflicts.pop(field, None)
        if conflicts:
            out["semantic_conflicts"] = conflicts
        else:
            out.pop("semantic_conflicts", None)
    return out


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def attribute_digest(attr: dict) -> str:
    resolved = resolve_attribute(attr)
    payload = {field: item["value"] for field, item in resolved.items()}
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def row_digest(entity_types: Iterable[dict]) -> str:
    """整行编译 schema 的语义 digest：按 `(entity, attr)` 排序，只含生效语义。"""
    payload: list[dict] = []
    for entity in entity_types or ():
        if not isinstance(entity, dict):
            continue
        name = str(entity.get("name") or "")
        for attr in entity.get("attributes") or ():
            if not isinstance(attr, dict):
                continue
            resolved = resolve_attribute(attr)
            if not resolved:
                continue
            payload.append({
                "entity": name,
                "attribute": str(attr.get("name") or ""),
                "fields": {field: item["value"] for field, item in resolved.items()},
            })
    payload.sort(key=lambda item: (item["entity"], item["attribute"]))
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def columns_digest(columns: Iterable[dict]) -> str:
    """喂模型的列清单的语义 digest（按 `logical_name/name` 排序）。

    与 `row_digest` 的区别：只覆盖本次真正进 payload 的列（可能被锚点/预算裁剪），
    供 payload 指纹与评测 series key 使用。
    """
    payload: list[dict] = []
    for column in columns or ():
        if not isinstance(column, dict):
            continue
        name = str(column.get("logical_name") or column.get("name") or "")
        fields = {field: canonical_value(field, column.get(field))
                  for field in FIELDS if not _is_empty(canonical_value(field, column.get(field)))}
        if not fields:
            continue
        payload.append({"column": name, "fields": fields})
    payload.sort(key=lambda item: item["column"])
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
