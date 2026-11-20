"""推理视图：由 schema + active 公理计算闭包（T5，纯函数 + 进程内 LRU）。"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field

from .axiom_checks import RELATION_CHARACTERISTICS
from .context import ReasoningContext

_MAX_CACHE = 64
_VIEW_CACHE: "OrderedDict[str, ReasoningView]" = OrderedDict()


@dataclass
class ReasoningView:
    fingerprint: str = ""
    class_descendants: dict[str, set[str]] = field(default_factory=dict)
    class_ancestors: dict[str, list[str]] = field(default_factory=dict)
    relation_descendants: dict[str, set[str]] = field(default_factory=dict)
    attribute_descendants: dict[tuple[str, str], set[tuple[str, str]]] = field(
        default_factory=dict,
    )
    attribute_ancestors: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    deprecated_types: set[str] = field(default_factory=set)
    deprecated_relations: set[str] = field(default_factory=set)
    characteristics: dict[str, set[str]] = field(default_factory=dict)
    inverse_of: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _entity_name(context, ref) -> str | None:
    key = context.resolve_entity(ref)
    if not key:
        return None
    return (context.entities.get(key) or {}).get("name") or None


def _relation_name(context, ref) -> str | None:
    key = context.resolve_relation(ref)
    if not key:
        return None
    return (context.relations.get(key) or {}).get("name") or None


def _attribute_name(context, ref) -> tuple[str, str] | None:
    if not isinstance(ref, dict):
        return None
    owner = _entity_name(context, ref.get("owner"))
    if not owner:
        return None
    resolved = context.resolve_attribute(ref.get("owner"), ref.get("ref"))
    if not resolved:
        return None
    attr = (context.attributes.get(resolved) or {}).get("name")
    return (owner, attr) if attr else None


def _children(parents: dict) -> dict:
    children: dict = {}
    for child, parents_set in parents.items():
        for parent in parents_set:
            children.setdefault(parent, set()).add(child)
    return children


def _descendants(parents: dict, include_self: bool = True) -> dict:
    children = _children(parents)
    result: dict = {}
    for node in set(parents) | set(children):
        seen: set = set()
        stack = [node]
        while stack:
            current = stack.pop()
            for child in children.get(current, set()):
                if child not in seen:
                    seen.add(child)
                    stack.append(child)
        if include_self:
            seen.add(node)
        result[node] = seen
    return result


def _ancestor_depths(parents: dict) -> dict:
    result: dict = {}
    for node in set(parents) | {p for s in parents.values() for p in s}:
        depth: dict = {}
        queue = [(parent, 1) for parent in parents.get(node, set())]
        while queue:
            current, level = queue.pop(0)
            if current in depth and depth[current] <= level:
                continue
            depth[current] = level
            for parent in parents.get(current, set()):
                queue.append((parent, level + 1))
        result[node] = depth
    return result


def _has_cycle(parents: dict) -> bool:
    state: dict = {}

    def visit(node) -> bool:
        state[node] = 1
        for parent in parents.get(node, set()):
            if state.get(parent) == 1:
                return True
            if parent not in state and visit(parent):
                return True
        state[node] = 2
        return False

    return any(node not in state and visit(node) for node in parents)


def _build_view(ctx: ReasoningContext) -> ReasoningView:
    context = ctx.schema_context
    view = ReasoningView(fingerprint=ctx.fingerprint)
    class_parents: dict[str, set[str]] = {}
    equivalent_parents: dict[str, set[str]] = {}
    relation_parents: dict[str, set[str]] = {}
    attribute_parents: dict[tuple[str, str], set[tuple[str, str]]] = {}

    view.deprecated_types = {
        (context.entities.get(key) or {}).get("name") or key
        for key in context.deprecated_entities
    }
    view.deprecated_relations = {
        (context.relations.get(key) or {}).get("name") or key
        for key in context.deprecated_relations
    }

    for relation in context.relations.values():
        name = relation.get("name")
        inverse = relation.get("inverse_of")
        if name and inverse:
            view.inverse_of[name] = inverse

    for axiom in ctx.axioms:
        kind = str(axiom.get("kind") or "")
        subject = axiom.get("subject") or {}
        object_ref = axiom.get("object") or {}
        if kind in {"subClassOf", "equivalentClass"}:
            child = _entity_name(context, subject.get("ref"))
            parent = _entity_name(context, object_ref.get("ref"))
            if not child or not parent:
                continue
            if child in view.deprecated_types or parent in view.deprecated_types:
                continue
            if kind == "subClassOf":
                class_parents.setdefault(child, set()).add(parent)
            else:
                equivalent_parents.setdefault(child, set()).add(parent)
                equivalent_parents.setdefault(parent, set()).add(child)
        elif kind == "subPropertyOf":
            if subject.get("kind") == "relation" and object_ref.get("kind") == "relation":
                child = _relation_name(context, subject.get("ref"))
                parent = _relation_name(context, object_ref.get("ref"))
                if not child or not parent:
                    continue
                if child in view.deprecated_relations or parent in view.deprecated_relations:
                    continue
                relation_parents.setdefault(child, set()).add(parent)
            elif subject.get("kind") == "attribute" and object_ref.get("kind") == "attribute":
                child = _attribute_name(context, subject)
                parent = _attribute_name(context, object_ref)
                if not child or not parent:
                    continue
                if child[0] in view.deprecated_types or parent[0] in view.deprecated_types:
                    continue
                attribute_parents.setdefault(child, set()).add(parent)
        elif kind in RELATION_CHARACTERISTICS:
            relation = _relation_name(context, subject.get("ref"))
            if relation and relation not in view.deprecated_relations:
                view.characteristics.setdefault(relation, set()).add(kind)

    # 闭包：等价类按双向 subClassOf 并入（R10）。
    closure_parents: dict[str, set[str]] = {}
    for child, parents in class_parents.items():
        closure_parents.setdefault(child, set()).update(parents)
    for child, parents in equivalent_parents.items():
        closure_parents.setdefault(child, set()).update(parents)

    view.class_descendants = _descendants(closure_parents, include_self=True)
    depth = _ancestor_depths(closure_parents)
    for node, levels in depth.items():
        view.class_ancestors[node] = [
            name for name, _ in sorted(levels.items(), key=lambda item: (item[1], item[0]))
        ]

    view.relation_descendants = _descendants(relation_parents, include_self=True)

    attr_desc = _descendants(attribute_parents, include_self=True)
    view.attribute_descendants = attr_desc
    attr_depth = _ancestor_depths(attribute_parents)
    for node, levels in attr_depth.items():
        view.attribute_ancestors[node] = [
            f"{owner}.{attr}"
            for (owner, attr), _ in sorted(
                levels.items(), key=lambda item: (item[1], item[0][0], item[0][1]),
            )
        ]

    for name, parents in (
        ("subClassOf", class_parents),
        ("subPropertyOf(relation)", relation_parents),
        ("subPropertyOf(attribute)", attribute_parents),
    ):
        if _has_cycle(parents):
            view.warnings.append(f"hierarchy cycle detected: {name}")
    return view


def build_reasoning_view(ctx: ReasoningContext) -> ReasoningView:
    """按 fingerprint 缓存的推理视图；环只记 warning，不抛错。"""
    key = ctx.fingerprint or "empty"
    cached = _VIEW_CACHE.get(key)
    if cached is not None:
        _VIEW_CACHE.move_to_end(key)
        return cached
    view = _build_view(ctx)
    _VIEW_CACHE[key] = view
    if len(_VIEW_CACHE) > _MAX_CACHE:
        _VIEW_CACHE.popitem(last=False)
    return view


def clear_view_cache() -> None:
    _VIEW_CACHE.clear()


__all__ = ["ReasoningView", "build_reasoning_view", "clear_view_cache"]
