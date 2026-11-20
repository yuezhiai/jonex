"""读时展开：属性/约束继承、端点扩展、过滤 deprecated（T5）。

这是派生字段的**唯一入口**。输出不落库；保存/导入入口会剥离派生键。
"""
from __future__ import annotations

import copy
from typing import Iterable, Optional

from .view import ReasoningView


def _endpoints(relation: dict, side: str) -> list[str]:
    values = [str(v).strip() for v in (relation.get(f"{side}_types") or []) if str(v).strip()]
    if values:
        return values
    single = str(relation.get(side) or "").strip()
    return [single] if single else []


def _constraint_type(constraint: dict) -> str:
    return str(
        constraint.get("constraint_type") or constraint.get("type") or ""
    ).strip().lower()


def _should_copy_constraint(constraint: dict) -> bool:
    ctype = _constraint_type(constraint)
    if "unique" in ctype:
        return False
    if ctype in {"relation_cardinality", "cardinality", "cardinality_relation"}:
        return False
    return True


def _copy_constraint(
    constraint: dict, *, name: str, target_code: str, inherited_from: str,
) -> dict:
    copied = copy.deepcopy(constraint)
    copied["name"] = name
    copied["target_code"] = target_code
    copied["inherited_from"] = inherited_from
    return copied


def _expand_constraints(
    constraints: Iterable[dict],
    expanded_entities: list[dict],
    view: ReasoningView,
) -> list[dict]:
    entity_map = {e.get("name"): e for e in expanded_entities if e.get("name")}
    attr_names = {
        name: {a.get("name") for a in (entity.get("attributes") or []) if a.get("name")}
        for name, entity in entity_map.items()
    }
    result: list[dict] = []
    seen_names: set[str] = set()

    for constraint in constraints or []:
        if not isinstance(constraint, dict):
            continue
        name = str(constraint.get("name") or "")
        target_type = str(constraint.get("target_type") or "").strip().lower()
        target_code = str(constraint.get("target_code") or "").strip()
        if not name or not target_type or not target_code:
            result.append(copy.deepcopy(constraint))
            continue

        if target_type == "entity":
            if target_code not in entity_map:
                continue  # 目标是 deprecated/已删除实体：整条丢弃
            result.append(copy.deepcopy(constraint))
            seen_names.add(name)
            if not _should_copy_constraint(constraint):
                continue
            for descendant in view.class_descendants.get(target_code, set()):
                if descendant == target_code or descendant not in entity_map:
                    continue
                copy_name = f"{name}@{descendant}"
                if copy_name in seen_names:
                    continue
                seen_names.add(copy_name)
                result.append(_copy_constraint(
                    constraint, name=copy_name, target_code=descendant,
                    inherited_from=target_code,
                ))
            continue

        if target_type == "attribute":
            owner, _, attr = target_code.partition(".")
            if not owner or not attr or owner not in entity_map:
                continue
            if attr not in attr_names.get(owner, set()):
                continue
            result.append(copy.deepcopy(constraint))
            seen_names.add(name)
            if not _should_copy_constraint(constraint):
                continue

            # 1) 所属实体是祖先：子类继承的同名属性。
            for descendant in view.class_descendants.get(owner, set()):
                if descendant == owner or descendant not in entity_map:
                    continue
                if attr not in attr_names.get(descendant, set()):
                    continue
                child_code = f"{descendant}.{attr}"
                copy_name = f"{name}@{descendant}"
                if copy_name in seen_names:
                    continue
                seen_names.add(copy_name)
                result.append(_copy_constraint(
                    constraint, name=copy_name, target_code=child_code,
                    inherited_from=target_code,
                ))

            # 2) 数据属性层次：父属性约束下放到子属性。
            for child_owner, child_attr in view.attribute_descendants.get(
                (owner, attr), set(),
            ):
                if (child_owner, child_attr) == (owner, attr):
                    continue
                if child_owner not in entity_map:
                    continue
                if child_attr not in attr_names.get(child_owner, set()):
                    continue
                child_code = f"{child_owner}.{child_attr}"
                copy_name = f"{name}@{child_owner}.{child_attr}"
                if copy_name in seen_names:
                    continue
                seen_names.add(copy_name)
                result.append(_copy_constraint(
                    constraint, name=copy_name, target_code=child_code,
                    inherited_from=target_code,
                ))
            continue

        if target_type == "relation":
            if target_code in (view.deprecated_relations or set()):
                continue
            # 关系基数靠端点扩展自然生效，约束不复制；原声明保留。
            result.append(copy.deepcopy(constraint))
            seen_names.add(name)
            continue

        result.append(copy.deepcopy(constraint))
    return result


def expand_compiled_schema(
    schema: Optional[dict], view: Optional[ReasoningView],
) -> Optional[dict]:
    """返回展开后的副本；不改入参，不写库。

    实体：补 `super_types`、父类属性（`inherited_from`）与 `super_attributes`；
    关系：补 `derived_source_types / derived_target_types`；
    约束：为后代类/子属性生成派生副本（`inherited_from`，`unique_attribute` 不复制）；
    deprecated 实体/关系及其约束被过滤。
    """
    if not isinstance(schema, dict):
        return schema
    view = view or ReasoningView()

    original_entities = [
        e for e in (schema.get("entity_types") or []) if isinstance(e, dict)
    ]
    entity_map = {e.get("name"): e for e in original_entities if e.get("name")}
    deprecated = set(view.deprecated_types or set())

    expanded_entities: list[dict] = []
    for entity in original_entities:
        name = entity.get("name")
        if not name or name in deprecated:
            continue
        attrs = [copy.deepcopy(a) for a in (entity.get("attributes") or []) if isinstance(a, dict)]
        seen_attrs = {a.get("name") for a in attrs if a.get("name")}
        for ancestor in view.class_ancestors.get(name, []):
            ancestor_entity = entity_map.get(ancestor) or {}
            for attr in ancestor_entity.get("attributes") or []:
                if not isinstance(attr, dict):
                    continue
                attr_name = attr.get("name")
                if not attr_name or attr_name in seen_attrs:
                    continue
                copied = copy.deepcopy(attr)
                copied["inherited_from"] = ancestor
                attrs.append(copied)
                seen_attrs.add(attr_name)

        for attr in attrs:
            attr_name = attr.get("name")
            owner = attr.get("inherited_from") or name
            if not attr_name:
                continue
            supers = view.attribute_ancestors.get((owner, attr_name), [])
            if supers:
                attr["super_attributes"] = list(supers)

        expanded = copy.deepcopy(entity)
        expanded["attributes"] = attrs
        ancestors = view.class_ancestors.get(name, [])
        if ancestors:
            expanded["super_types"] = list(ancestors)
        expanded_entities.append(expanded)

    expanded_relations: list[dict] = []
    for relation in schema.get("relation_types") or []:
        if not isinstance(relation, dict):
            continue
        name = relation.get("name")
        if not name or name in (view.deprecated_relations or set()):
            continue
        expanded = copy.deepcopy(relation)
        for side in ("source", "target"):
            declared = _endpoints(relation, side)
            derived: set[str] = set()
            for endpoint in declared:
                if endpoint in (view.deprecated_types or set()):
                    continue
                derived.add(endpoint)
                derived.update(
                    name for name in view.class_descendants.get(endpoint, set())
                    if name not in (view.deprecated_types or set())
                )
            if derived:
                expanded[f"derived_{side}_types"] = sorted(derived)
        expanded_relations.append(expanded)

    expanded_constraints = _expand_constraints(
        schema.get("constraints") or [], expanded_entities, view,
    )

    result = copy.deepcopy(schema)
    result["entity_types"] = expanded_entities
    result["relation_types"] = expanded_relations
    result["constraints"] = expanded_constraints
    return result


__all__ = ["expand_compiled_schema"]
