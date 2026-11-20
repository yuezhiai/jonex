"""检索侧层级扩展（T7）：类型/关系/属性展开，上限 64。"""
from __future__ import annotations

import os
from dataclasses import dataclass

from .view import ReasoningView

MAX_EXPANDED_NAMES = 64


def hierarchy_expansion_enabled() -> bool:
    return os.getenv("ONTOLOGY_HIERARCHY_EXPANSION_ENABLED", "false").lower() \
        not in ("0", "false", "no", "off", "")


@dataclass
class Expansion:
    values: list[str]
    truncated: bool = False


def _cap(values, limit: int = MAX_EXPANDED_NAMES) -> Expansion:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = str(value or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return Expansion(values=out[:limit], truncated=len(out) > limit)


def expand_entity_types(
    view: ReasoningView, names, limit: int = MAX_EXPANDED_NAMES,
) -> Expansion:
    values: list[str] = []
    for name in names or []:
        name = str(name or "").strip()
        if not name:
            continue
        values.extend(sorted(view.class_descendants.get(name, {name})))
    return _cap(values, limit)


def expand_relation_types(
    view: ReasoningView, names, limit: int = MAX_EXPANDED_NAMES,
) -> Expansion:
    values: list[str] = []
    for name in names or []:
        name = str(name or "").strip()
        if not name:
            continue
        values.extend(sorted(view.relation_descendants.get(name, {name})))
    return _cap(values, limit)


def expand_attribute_names(
    view: ReasoningView, entity: str, names, limit: int = MAX_EXPANDED_NAMES,
) -> Expansion:
    values: list[str] = []
    owners = [entity, *view.class_ancestors.get(entity, [])]
    for name in names or []:
        name = str(name or "").strip()
        if not name:
            continue
        values.append(name)
        for owner in owners:
            key = (owner, name)
            descendants = view.attribute_descendants.get(key)
            if descendants:
                values.extend(sorted(attr for _, attr in descendants))
    return _cap(values, limit)


__all__ = [
    "Expansion",
    "MAX_EXPANDED_NAMES",
    "expand_attribute_names",
    "expand_entity_types",
    "expand_relation_types",
    "hierarchy_expansion_enabled",
]
