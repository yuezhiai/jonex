"""落标校验：已发布标准快照 vs KB compiled schema。

纯函数：输入是 `template_scenario_versions.snapshot` 与
`ontology_compiled_schemas.entity_types`，输出状态 + 结构差异清单。

状态：
- `not_published`：场景还没有任何发布版本；
- `no_schema`：KB 没有 compiled schema（调用方只在有 schema 的行上调用，
  该状态用于防御性兜底）；
- `no_source_version`：compiled 行没有来源版本（人工编辑/历史行）；
- `outdated`：source_version < 最新发布版本；
- `current`：版本一致且结构一致；
- `mismatch`：版本一致但结构有差异（或 source_version 超前）。
"""
from __future__ import annotations

from typing import Any

_ATTR_FIELDS = ("type", "required", "name")


def _norm_required(value: Any) -> bool:
    return bool(int(value)) if isinstance(value, (int, bool)) else bool(value)


def _norm_type(value: Any) -> str:
    return str(value or "").strip().lower()


def _standard_objects(snapshot: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for obj in (snapshot or {}).get("objects") or []:
        if isinstance(obj, dict) and obj.get("id"):
            out[str(obj["id"])] = obj
    return out


def _compiled_objects(compiled: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for obj in (compiled or {}).get("entity_types") or []:
        if not isinstance(obj, dict):
            continue
        key = obj.get("source_object_id") or obj.get("name")
        if key:
            out[str(key)] = obj
    return out


def _std_attributes(obj: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for attr in obj.get("attributes") or []:
        if isinstance(attr, dict) and attr.get("id"):
            out[str(attr["id"])] = attr
    return out


def _compiled_attributes(obj: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for attr in obj.get("attributes") or []:
        if not isinstance(attr, dict):
            continue
        key = attr.get("source_attribute_id") or attr.get("name")
        if key:
            out[str(key)] = attr
    return out


def _attr_label(std: dict | None, comp: dict | None) -> str:
    if std:
        return str(std.get("attr_name") or std.get("id"))
    if comp:
        return str(comp.get("display_name") or comp.get("name"))
    return ""


def _object_label(std: dict | None, comp: dict | None) -> str:
    if std:
        return str(std.get("name") or std.get("id"))
    if comp:
        return str(comp.get("display_name") or comp.get("name"))
    return ""


def _attr_field_changes(std: dict, comp: dict) -> list[dict]:
    changes = []
    std_type = _norm_type(std.get("attr_type"))
    comp_type = _norm_type(comp.get("type"))
    if std_type and comp_type and std_type != comp_type:
        changes.append({"field": "type", "standard": std_type, "compiled": comp_type})
    std_required = _norm_required(std.get("is_required", 0))
    comp_required = _norm_required(comp.get("required", False))
    if std_required != comp_required:
        changes.append({
            "field": "required", "standard": std_required, "compiled": comp_required,
        })
    std_code = std.get("ontology_code")
    comp_name = comp.get("name")
    if std_code and comp_name and std_code != comp_name:
        changes.append({
            "field": "name", "standard": std_code, "compiled": comp_name,
        })
    return changes


def evaluate_compliance(
    snapshot: dict | None,
    compiled: dict | None,
    *,
    latest_version: int | None,
) -> dict:
    """返回 `{status, source_version, latest_version, ...diffs}`。"""
    source_version = (compiled or {}).get("source_version")
    base = {
        "latest_version": latest_version,
        "source_version": source_version,
        "missing_objects": [],
        "extra_objects": [],
        "missing_attributes": [],
        "extra_attributes": [],
        "changed_attributes": [],
    }

    if latest_version is None:
        return {**base, "status": "not_published"}
    if compiled is None:
        return {**base, "status": "no_schema"}
    if source_version is None:
        status = "no_source_version"
    elif source_version == latest_version:
        status = "current"
    elif source_version < latest_version:
        status = "outdated"
    else:
        status = "mismatch"

    std_objects = _standard_objects(snapshot or {})
    comp_objects = _compiled_objects(compiled)

    missing_objects = [
        {"id": key, "label": _object_label(obj, None)}
        for key, obj in std_objects.items() if key not in comp_objects
    ]
    extra_objects = [
        {"id": key, "label": _object_label(None, obj)}
        for key, obj in comp_objects.items() if key not in std_objects
    ]

    missing_attributes: list[dict] = []
    extra_attributes: list[dict] = []
    changed_attributes: list[dict] = []

    for key, std_obj in std_objects.items():
        comp_obj = comp_objects.get(key)
        if comp_obj is None:
            continue
        object_label = _object_label(std_obj, comp_obj)
        std_attrs = _std_attributes(std_obj)
        comp_attrs = _compiled_attributes(comp_obj)
        for attr_key, std_attr in std_attrs.items():
            comp_attr = comp_attrs.get(attr_key)
            if comp_attr is None:
                missing_attributes.append({
                    "object": object_label,
                    "id": attr_key,
                    "label": _attr_label(std_attr, None),
                })
                continue
            changes = _attr_field_changes(std_attr, comp_attr)
            if changes:
                changed_attributes.append({
                    "object": object_label,
                    "id": attr_key,
                    "label": _attr_label(std_attr, comp_attr),
                    "fields": changes,
                })
        for attr_key, comp_attr in comp_attrs.items():
            if attr_key not in std_attrs:
                extra_attributes.append({
                    "object": object_label,
                    "id": attr_key,
                    "label": _attr_label(None, comp_attr),
                })

    has_diff = bool(
        missing_objects or extra_objects or missing_attributes
        or extra_attributes or changed_attributes
    )
    if status == "current" and has_diff:
        status = "mismatch"

    return {
        **base,
        "status": status,
        "missing_objects": missing_objects,
        "extra_objects": extra_objects,
        "missing_attributes": missing_attributes,
        "extra_attributes": extra_attributes,
        "changed_attributes": changed_attributes,
    }


def summarize(items: list[dict]) -> dict:
    summary = {
        "total": len(items),
        "current": 0,
        "outdated": 0,
        "mismatch": 0,
        "no_schema": 0,
        "no_source_version": 0,
        "not_published": 0,
    }
    for item in items:
        status = item.get("status")
        if status in summary:
            summary[status] += 1
    return summary
