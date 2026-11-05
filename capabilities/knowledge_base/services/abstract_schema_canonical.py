# -*- coding:utf-8 -*-
"""抽象 schema 的**共享序列化器**（P1 余项：abstract 漂移）。

为什么需要它：`AbstractSchemaService.get()` 是**读时注解**投影 —— 实体落点加
`source_name` / `stale` / `stale_reason`，属性来源与关系背书加
`stale` / `stale_reason`（`abstract_schema_service._annotate_schema_staleness`）。
演化台的版本快照与 ② 的漂移判定都拿「存库真相」做内容指纹，两侧必须走同一个
序列化器，否则同一份内容有两个 hash（P1-1 对 abstract 硬关漂移就是后果）。

契约：
- `canonical_abstract_schema(get() 的 schema) == canonical_abstract_schema(存库行)`；
- 输出**恰好** `{entities, relations, metrics}`，**有非空 `tools` 覆盖时**才多一个
  `tools`（镜像 `get()` 的顶层形状，未知顶层键被丢弃；实体/属性/关系里的其余键
  原样保留；tools 条目只投影 `name`/`description`/`enabled`）；
- 只剥 `DERIVED_LANDING_KEYS`，不做任何归一化 —— 归一化仍归 `normalize_schema`；
- 纯函数：不改入参。
"""
from __future__ import annotations

from .evolution.version_store import canonical_snapshot_hash

#: 读时注解在落点/背书上加的派生键（与 `_annotate_schema_staleness` 一一对应）。
DERIVED_LANDING_KEYS = ("source_name", "stale", "stale_reason")

_EMPTY = {"entities": [], "relations": [], "metrics": []}

#: tools 条目的合法键（与 `concept_expansion._normalize_tools` 同序同集）。
_TOOL_KEYS = ("name", "description", "enabled")


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _strip_landings(items) -> list[dict]:
    return [
        {k: v for k, v in item.items() if k not in DERIVED_LANDING_KEYS}
        for item in _list(items) if isinstance(item, dict)
    ]


def _tools(value) -> list[dict]:
    """`tools` 覆盖块的形状投影（**不归一化**，只丢未知键 / 无名条目）。

    映射的是 `get()` / `normalize_schema` 的形状（`{name, description?, enabled?}`），
    内容原样 —— 归一化（去重、截断、上限）仍只发生在 `normalize_schema`。
    """
    out: list[dict] = []
    for item in _list(value):
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        out.append({k: item[k] for k in _TOOL_KEYS if k in item})
    return out


def canonical_abstract_schema(schema) -> dict:
    """v2 抽象 schema → 规范内容（`get()` 形状，剥读时派生键）。"""
    if not isinstance(schema, dict):
        return {key: list(value) for key, value in _EMPTY.items()}
    entities: list[dict] = []
    for raw in _list(schema.get("entities")):
        if not isinstance(raw, dict):
            continue
        entity = {**raw}
        entity["sources"] = _strip_landings(raw.get("sources"))
        entity["attributes"] = [
            {**attr, "sources": _strip_landings(attr.get("sources"))}
            for attr in _list(raw.get("attributes")) if isinstance(attr, dict)
        ]
        entities.append(entity)
    relations = [
        {**rel, "backings": _strip_landings(rel.get("backings"))}
        for rel in _list(schema.get("relations")) if isinstance(rel, dict)
    ]
    out = {"entities": entities, "relations": relations,
           "metrics": list(_list(schema.get("metrics")))}
    tools = _tools(schema.get("tools"))
    if tools:
        out["tools"] = tools
    return out


def canonical_abstract_snapshot_hash(value) -> str:
    """快照/版本行 → 规范化 sha256（abstract 专用）；不可用 → `""`。

    两种入参形状都收：DB 版本行（`value["snapshot"]` 是快照）与快照本体
    （`{scope, kb, ds, content:{schema, status}}`）。
    """
    snapshot = value
    if isinstance(value, dict) and isinstance(value.get("snapshot"), dict):
        snapshot = value["snapshot"]
    if not isinstance(snapshot, dict):
        return ""
    content = snapshot.get("content")
    if not isinstance(content, dict):
        return ""
    canonical = {**content,
                 "schema": canonical_abstract_schema(content.get("schema"))}
    return canonical_snapshot_hash({**snapshot, "content": canonical})


__all__ = [
    "DERIVED_LANDING_KEYS",
    "canonical_abstract_schema",
    "canonical_abstract_snapshot_hash",
]
