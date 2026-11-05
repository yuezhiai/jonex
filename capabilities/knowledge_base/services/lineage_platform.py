#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""OpenMetadata / DataHub 推送事件 → 列级四元组（纯函数，无 DB）。

两家平台的 webhook payload 形状不同：

- **OpenMetadata**：`ChangeEvent(eventType=entityLineageAdded/Updated/Deleted)`。
  `entity` 是 `{fromEntity, toEntity, lineageDetails}`（也可能被 JSON 编码成字符串）；
  列级血缘在 `entity.lineageDetails.columnsLineage[]`（`fromColumns[]` → `toColumn`）。
  `entityLineageDeleted` 只带两端引用 → 产出「删除这对表的 platform 边」。
- **DataHub**：`MetadataChangeLog(aspectName="upstreamLineage")`，可能包在
  `{event: ...}` / `{payload: ...}` 里；`aspect` 可能是 GenericAspect
  （`{value: "<json>", contentType: "application/json"}`）。列级在
  `aspect.fineGrainedLineages[]`（`upstreams` / `downstreams` 为 schemaField URN）。
  `changeType=DELETE` → 删除该下游表的 platform 边。

**表名归一**：平台 FQN 通常带 service/catalog（`svc.db.public.orders`），而本地编译
schema 用 `schema.table` / `table`。优先按目标源**已知表**做后缀 + 大小写匹配；找不到
才回退最后两段（导入阶段仍会按编译 schema 严格校验，写错会如实报错）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from jonex_core.common.exceptions import InvalidParameterError

_OM_LINEAGE_ADD = frozenset({"entityLineageAdded", "entityLineageUpdated"})
_OM_LINEAGE_DELETE = frozenset({"entityLineageDeleted"})
_DH_LINEAGE_ASPECT = "upstreamLineage"


@dataclass(frozen=True)
class ParsedLineage:
    """一次推送解析出的写入（rows）与删除（deletes）。

    `deletes` 的每一项：
    - `{"from_table": ..., "to_table": ...}`：删这对表之间的 platform 边（OM）；
    - `{"to_table": ...}`：删指向该表的 platform 边（DataHub aspect 整体删除）。
    """

    rows: list[dict] = field(default_factory=list)
    deletes: list[dict] = field(default_factory=list)


# ── 通用形状校验 ──────────────────────────────────────────────────────────

def _as_dict(value, path: str) -> dict:
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise InvalidParameterError(f"{path} 不是合法 JSON：{exc}") from exc
    if not isinstance(value, dict):
        raise InvalidParameterError(f"{path} 必须是 JSON 对象")
    return value


def _as_list(value, path: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise InvalidParameterError(f"{path} 必须是数组")
    return value


def _as_str(value) -> str:
    return str(value).strip() if value is not None else ""


# ── 表名 / 列名归一 ────────────────────────────────────────────────────────

def _clean_segment(part: str) -> str:
    cleaned = part.strip()
    for ch in "[]`\"'":
        cleaned = cleaned.replace(ch, "")
    return cleaned


def _segments(name: str) -> list[str]:
    return [p for p in (_clean_segment(s) for s in (name or "").strip().split(".")) if p]


def _columns_for(known_tables: Mapping[str, Iterable[str]] | None,
                 table: str) -> set[str] | None:
    if not known_tables:
        return None
    if table in known_tables:
        return set(known_tables[table])
    lower = table.lower()
    for key, cols in known_tables.items():
        if str(key).lower() == lower:
            return set(cols)
    return None


def resolve_table_name(raw: str,
                       known_tables: Mapping[str, Iterable[str]] | None = None) -> str:
    """把平台 FQN 归一成本地编译 schema 的表名（优先已知表，回退最后两段）。"""
    segments = _segments(raw)
    if not segments:
        return ""
    fallback = ".".join(segments[-2:]) if len(segments) >= 2 else segments[-1]
    if not known_tables:
        return fallback

    lowered = {str(key).lower(): str(key) for key in known_tables}
    if fallback.lower() in lowered:
        return lowered[fallback.lower()]

    raw_lower = ".".join(segments).lower()
    suffix = [orig for low, orig in lowered.items()
              if raw_lower == low or raw_lower.endswith("." + low)]
    if suffix:
        # 多个已知表都可能是后缀时取最长的那个（更具体）
        return max(suffix, key=len)

    last = segments[-1].lower()
    by_last = [orig for low, orig in lowered.items() if low.rsplit(".", 1)[-1] == last]
    if len(by_last) == 1:
        return by_last[0]
    return fallback


def resolve_column_name(raw: str, table: str,
                        known_columns: Iterable[str] | None = None) -> str:
    """列名取最后一段，并按已知列做大小写归一。"""
    segments = _segments(raw)
    col = segments[-1] if segments else _as_str(raw)
    if not col or not known_columns:
        return col
    lowered = {str(c).lower(): str(c) for c in known_columns}
    return lowered.get(col.lower(), col)


# ── OpenMetadata ──────────────────────────────────────────────────────────

def _om_tables(entity: dict, known_tables) -> tuple[str, str]:
    from_ref = _as_dict(entity.get("fromEntity"), "entity.fromEntity")
    to_ref = _as_dict(entity.get("toEntity"), "entity.toEntity")
    from_fqn = _as_str(from_ref.get("fullyQualifiedName"))
    to_fqn = _as_str(to_ref.get("fullyQualifiedName"))
    if not from_fqn or not to_fqn:
        raise InvalidParameterError(
            "OpenMetadata 血缘事件的端点缺少 fullyQualifiedName")
    return (resolve_table_name(from_fqn, known_tables),
            resolve_table_name(to_fqn, known_tables))


def parse_openmetadata_event(event, *, known_tables=None) -> ParsedLineage:
    if not isinstance(event, dict):
        raise InvalidParameterError("OpenMetadata 事件必须是 JSON 对象")
    event_type = _as_str(event.get("eventType"))
    if event_type not in _OM_LINEAGE_ADD | _OM_LINEAGE_DELETE:
        return ParsedLineage()

    entity_raw = event.get("entity")
    if entity_raw in (None, ""):
        # 兜底：部分投递只带 `fromFQN--upstream-->toFQN`
        fqn = _as_str(event.get("entityFullyQualifiedName"))
        from_fqn, sep, to_fqn = fqn.partition("--upstream-->")
        if not sep:
            raise InvalidParameterError("OpenMetadata 血缘事件缺少 entity")
        from_table = resolve_table_name(from_fqn, known_tables)
        to_table = resolve_table_name(to_fqn, known_tables)
        if event_type in _OM_LINEAGE_DELETE:
            return ParsedLineage(deletes=[{"from_table": from_table,
                                           "to_table": to_table}])
        return ParsedLineage()

    entity = _as_dict(entity_raw, "entity")
    from_table, to_table = _om_tables(entity, known_tables)
    if event_type in _OM_LINEAGE_DELETE:
        return ParsedLineage(deletes=[{"from_table": from_table,
                                       "to_table": to_table}])

    details = _as_dict(entity.get("lineageDetails"), "entity.lineageDetails")
    columns = _as_list(details.get("columnsLineage"),
                       "entity.lineageDetails.columnsLineage")
    from_cols = _columns_for(known_tables, from_table)
    to_cols = _columns_for(known_tables, to_table)
    rows: list[dict] = []
    for idx, item in enumerate(columns):
        item = _as_dict(item, f"entity.lineageDetails.columnsLineage[{idx}]")
        to_raw = _as_str(item.get("toColumn"))
        if not to_raw:
            continue
        from_list = _as_list(item.get("fromColumns"),
                             f"entity.lineageDetails.columnsLineage[{idx}].fromColumns")
        for from_raw in from_list:
            from_col = resolve_column_name(_as_str(from_raw), from_table, from_cols)
            if not from_col:
                continue
            rows.append({
                "from_table": from_table,
                "from_column": from_col,
                "to_table": to_table,
                "to_column": resolve_column_name(to_raw, to_table, to_cols),
            })
    return ParsedLineage(rows=rows)


# ── DataHub ───────────────────────────────────────────────────────────────

def _find_mcl(node, depth: int = 0) -> dict | None:
    """在 `{event: ...}` / `{payload: ...}` 等常见信封里找到 MCL 本体。"""
    if depth > 3 or not isinstance(node, dict):
        return None
    if "aspectName" in node or ("entityUrn" in node and "aspect" in node):
        return node
    for key in ("event", "payload", "message", "body", "data"):
        found = _find_mcl(node.get(key), depth + 1)
        if found is not None:
            return found
    return None


def _generic_aspect(value) -> dict:
    aspect = _as_dict(value, "aspect")
    if "value" in aspect and ("contentType" in aspect or len(aspect) == 1):
        inner = aspect.get("value")
        if isinstance(inner, str):
            try:
                inner = json.loads(inner)
            except ValueError as exc:
                raise InvalidParameterError(f"aspect.value 不是合法 JSON：{exc}") from exc
        if not isinstance(inner, dict):
            raise InvalidParameterError("aspect.value 必须是 JSON 对象")
        return inner
    return aspect


def _dataset_name_from_urn(urn: str) -> str:
    """`urn:li:dataset:(platform,name,env)` → `name`；非 URN 原样返回。"""
    prefix = "urn:li:dataset:("
    if urn.startswith(prefix) and urn.endswith(")"):
        inner = urn[len(prefix):-1]
        parts = inner.split(",")
        if len(parts) >= 3:
            return parts[1]
        if len(parts) == 2:
            return parts[-1]
        return inner
    return urn


def _schema_field_parts(urn: str) -> tuple[str, str]:
    """schemaField URN → (dataset 名, 列名)；也接受裸 `table.column`。"""
    prefix = "urn:li:schemaField:("
    if urn.startswith(prefix) and urn.endswith(")"):
        inner = urn[len(prefix):-1]
        dataset_urn, sep, field = inner.rpartition(",")
        if sep:
            return _dataset_name_from_urn(dataset_urn), field
    if "." in urn:
        table, _, column = urn.rpartition(".")
        return table, column
    return "", urn


def parse_datahub_event(event, *, known_tables=None) -> ParsedLineage:
    if not isinstance(event, dict):
        raise InvalidParameterError("DataHub 事件必须是 JSON 对象")
    mcl = _find_mcl(event) or event
    if _as_str(mcl.get("aspectName")) != _DH_LINEAGE_ASPECT:
        return ParsedLineage()
    downstream_urn = _as_str(mcl.get("entityUrn"))
    if not downstream_urn:
        raise InvalidParameterError("DataHub 血缘事件缺少 entityUrn")
    downstream_table = resolve_table_name(
        _dataset_name_from_urn(downstream_urn), known_tables)
    if _as_str(mcl.get("changeType")).upper() == "DELETE":
        return ParsedLineage(deletes=[{"to_table": downstream_table}])

    aspect = _generic_aspect(mcl.get("aspect"))
    fine = _as_list(aspect.get("fineGrainedLineages"),
                    "aspect.fineGrainedLineages")
    rows: list[dict] = []
    for idx, entry in enumerate(fine):
        entry = _as_dict(entry, f"aspect.fineGrainedLineages[{idx}]")
        upstreams = _as_list(entry.get("upstreams"),
                             f"aspect.fineGrainedLineages[{idx}].upstreams")
        downstreams = _as_list(entry.get("downstreams"),
                               f"aspect.fineGrainedLineages[{idx}].downstreams")
        for up in upstreams:
            up_table_raw, up_col_raw = _schema_field_parts(_as_str(up))
            up_table = resolve_table_name(up_table_raw, known_tables)
            up_cols = _columns_for(known_tables, up_table)
            for down in downstreams:
                down_table_raw, down_col_raw = _schema_field_parts(_as_str(down))
                down_table = resolve_table_name(
                    down_table_raw or downstream_table, known_tables)
                down_cols = _columns_for(known_tables, down_table)
                rows.append({
                    "from_table": up_table,
                    "from_column": resolve_column_name(up_col_raw, up_table, up_cols),
                    "to_table": down_table,
                    "to_column": resolve_column_name(
                        down_col_raw, down_table, down_cols),
                })
    return ParsedLineage(rows=rows)


# ── 分发 ──────────────────────────────────────────────────────────────────

def parse_platform_event(kind: str, event, *, known_tables=None) -> ParsedLineage:
    """按 ETL 来源的 `etl_kind` 分发到对应平台解析器。"""
    if kind == "openmetadata":
        return parse_openmetadata_event(event, known_tables=known_tables)
    if kind == "datahub":
        return parse_datahub_event(event, known_tables=known_tables)
    raise InvalidParameterError(f"不支持的推送类型：{kind}")


__all__ = [
    "ParsedLineage",
    "parse_datahub_event",
    "parse_openmetadata_event",
    "parse_platform_event",
    "resolve_column_name",
    "resolve_table_name",
]
