"""标准场景发布快照的纯函数 diff。

输入是 `business_domain.template_scenario_versions.snapshot` 的两种版本
（结构见 `business_domain.services.template_publish_service.build_scenario_snapshot`）：

```text
{scenario: {...}, objects: [{... , attributes: [...]}], relations: [...], constraints: [...]}
```

输出按实体分组：added / removed / changed；changed 里逐字段给 `from` / `to`。
本模块不读库、不做权限 —— 由 service 负责取数、登录与版本存在性。
"""
from __future__ import annotations

from typing import Any

# 参与比较的标量字段（顺序即展示顺序）。嵌套/大字段（config_json、constraints_json）
# 不进 diff：它们要么是自由结构，要么已经在 constraints 表里结构化表达。
_SCENARIO_FIELDS = ("name", "description")
_OBJECT_FIELDS = ("name", "description", "status", "ontology_code")
_ATTRIBUTE_FIELDS = (
    "attr_name", "description", "attr_type", "is_required",
    "is_primary_key", "ontology_code",
)
_RELATION_FIELDS = (
    "name", "description", "relation_type", "status", "ontology_code",
    "source_object_id", "target_object_id",
)
_CONSTRAINT_FIELDS = (
    "name", "target_type", "target_id", "constraint_type", "expression",
    "suggestion",
)

_GROUPS = ("objects", "attributes", "relations", "constraints")


def _scalar(value: Any) -> Any:
    """统一比较口径：None 保持 None；其余原样（0/1 与 bool 不做折叠）。"""
    return value


def _changed_fields(
    before: dict, after: dict, fields: tuple[str, ...],
) -> list[dict]:
    changes = []
    for field in fields:
        old = _scalar(before.get(field))
        new = _scalar(after.get(field))
        if old != new:
            changes.append({"field": field, "from": old, "to": new})
    return changes


def _index(items: list[dict] | None) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        if item_id:
            out[str(item_id)] = item
    return out


def _flatten_attributes(objects: list[dict] | None) -> tuple[dict[str, dict], dict[str, str]]:
    """attr_id -> attribute；attr_id -> 所属对象标签（name 或 id）。"""
    attrs: dict[str, dict] = {}
    owners: dict[str, str] = {}
    for obj in objects or []:
        if not isinstance(obj, dict):
            continue
        owner_id = str(obj.get("id") or "")
        owner_label = obj.get("name") or owner_id
        for attr in obj.get("attributes") or []:
            if not isinstance(attr, dict):
                continue
            attr_id = attr.get("id")
            if attr_id:
                attrs[str(attr_id)] = attr
                owners[str(attr_id)] = owner_label
    return attrs, owners


def _diff_entities(
    before: dict[str, dict],
    after: dict[str, dict],
    fields: tuple[str, ...],
    label_of,
) -> dict:
    added = []
    removed = []
    changed = []

    for key, item in after.items():
        if key not in before:
            added.append({"id": key, "label": label_of(item), **_pick(item, fields)})
    for key, item in before.items():
        if key not in after:
            removed.append({"id": key, "label": label_of(item), **_pick(item, fields)})
        else:
            changes = _changed_fields(item, after[key], fields)
            if changes:
                changed.append({
                    "id": key,
                    "label": label_of(item),
                    "fields": changes,
                })
    return {"added": added, "removed": removed, "changed": changed}


def _pick(item: dict, fields: tuple[str, ...]) -> dict:
    return {field: item.get(field) for field in fields}


def _label(item: dict) -> str:
    return str(item.get("name") or item.get("attr_name") or item.get("id") or "")


def diff_snapshots(
    before_snapshot: dict,
    after_snapshot: dict,
    *,
    from_version: int,
    to_version: int,
) -> dict:
    """对比两份发布快照，返回分组差异 + 汇总计数。"""
    before_snapshot = before_snapshot or {}
    after_snapshot = after_snapshot or {}

    before_objects = _index(before_snapshot.get("objects"))
    after_objects = _index(after_snapshot.get("objects"))
    before_attrs, before_owners = _flatten_attributes(before_snapshot.get("objects"))
    after_attrs, after_owners = _flatten_attributes(after_snapshot.get("objects"))
    before_relations = _index(before_snapshot.get("relations"))
    after_relations = _index(after_snapshot.get("relations"))
    before_constraints = _index(before_snapshot.get("constraints"))
    after_constraints = _index(after_snapshot.get("constraints"))

    scenario_before = before_snapshot.get("scenario") or {}
    scenario_after = after_snapshot.get("scenario") or {}

    result: dict[str, Any] = {
        "from_version": from_version,
        "to_version": to_version,
        "scenario": {
            "changed": _changed_fields(
                scenario_before, scenario_after, _SCENARIO_FIELDS,
            ),
        },
        "objects": _diff_entities(
            before_objects, after_objects, _OBJECT_FIELDS, _label,
        ),
        "attributes": _diff_entities(
            before_attrs, after_attrs, _ATTRIBUTE_FIELDS,
            lambda item: (
                f"{before_owners.get(str(item.get('id'))) or after_owners.get(str(item.get('id')), '')}"
                f".{_label(item)}"
            ),
        ),
        "relations": _diff_entities(
            before_relations, after_relations, _RELATION_FIELDS, _label,
        ),
        "constraints": _diff_entities(
            before_constraints, after_constraints, _CONSTRAINT_FIELDS, _label,
        ),
    }

    summary: dict[str, int] = {
        "scenario_changed": len(result["scenario"]["changed"]),
    }
    for group in _GROUPS:
        for kind in ("added", "removed", "changed"):
            summary[f"{group}_{kind}"] = len(result[group][kind])
    result["summary"] = summary
    return result
