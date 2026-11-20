"""公理规范化与校验（纯函数，不读库）。

规则 id 与 `contracts/reasoning_rules/rule-ids.md` 对齐；引用解析用
`SchemaContext`，层级闭包由当前 active 公理 + 本批 active 公理共同构成。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .contract import AXIOM_KINDS, AXIOM_ORIGINS, AXIOM_SOURCES, AXIOM_STATUSES
from .schema_context import SchemaContext

ENTITY_AXIOM_KINDS = frozenset({"subClassOf", "disjointWith", "equivalentClass"})
RELATION_CHARACTERISTICS = frozenset({
    "transitive", "symmetric", "functional", "inverseFunctional",
    "reflexive", "irreflexive", "asymmetric",
})
ATTRIBUTE_AXIOM_KINDS = frozenset({"datatypeFacet"})
_OVERLAP_RELATIONS = frozenset({"part_of", "belongs_to"})


def _issue(
    code: str,
    severity: str,
    axiom_key: str = "",
    *,
    field_name: str = "",
    params: Optional[dict] = None,
) -> dict:
    target: dict = {"kind": "axiom", "id": axiom_key or ""}
    if field_name:
        target["field"] = field_name
    out = {"code": code, "severity": severity, "target": target}
    if params:
        out["params"] = params
    return out


def _norm(value) -> str:
    return str(value or "").strip()


def _norm_ref(raw, axiom_key: str, field_name: str, issues: list[dict]) -> Optional[dict]:
    if not isinstance(raw, dict):
        issues.append(_issue(
            "checks.axiom.missingRef", "Violation", axiom_key,
            field_name=field_name, params={"reason": "missing"},
        ))
        return None
    kind = _norm(raw.get("kind")).lower()
    ref = _norm(raw.get("ref") or raw.get("id") or raw.get("name"))
    if kind not in {"entity", "relation", "attribute"} or not ref:
        issues.append(_issue(
            "checks.axiom.missingRef", "Violation", axiom_key,
            field_name=field_name,
            params={"kind": kind, "ref": ref},
        ))
        return None
    out = {"kind": kind, "ref": ref}
    owner = _norm(raw.get("owner"))
    if kind == "attribute":
        if not owner:
            issues.append(_issue(
                "checks.axiom.missingRef", "Violation", axiom_key,
                field_name=field_name, params={"reason": "attribute_owner_missing"},
            ))
            return out
        out["owner"] = owner
    return out


def normalize_axiom(raw) -> tuple[dict, list[dict]]:
    """把 YAML/DB 形状归一为契约形状；返回 (axiom, issues)。"""
    issues: list[dict] = []
    if not isinstance(raw, dict):
        key = f"ax_{uuid.uuid4().hex}"
        issues.append(_issue(
            "checks.axiom.kind_whitelist", "Violation", key,
            params={"reason": "not_an_object"},
        ))
        return {"axiom_key": key, "kind": "", "status": "active"}, issues

    axiom_key = _norm(raw.get("axiom_key") or raw.get("id")) or f"ax_{uuid.uuid4().hex}"
    kind = _norm(raw.get("kind"))
    if kind not in AXIOM_KINDS:
        issues.append(_issue(
            "checks.axiom.kind_whitelist", "Violation", axiom_key,
            params={"kind": kind},
        ))

    status = _norm(raw.get("status")).lower() or "active"
    if status not in AXIOM_STATUSES:
        issues.append(_issue(
            "checks.axiom.status_invalid", "Violation", axiom_key,
            params={"status": status},
        ))
    source = _norm(raw.get("source")).lower() or "imported"
    if source not in AXIOM_SOURCES:
        issues.append(_issue(
            "checks.axiom.source_invalid", "Violation", axiom_key,
            params={"source": source},
        ))
    origin = _norm(raw.get("origin")).lower() or "imported"
    if origin not in AXIOM_ORIGINS:
        issues.append(_issue(
            "checks.axiom.origin_invalid", "Violation", axiom_key,
            params={"origin": origin},
        ))

    subject = _norm_ref(raw.get("subject"), axiom_key, "subject", issues)
    object_ref = _norm_ref(raw.get("object"), axiom_key, "object", issues) \
        if raw.get("object") is not None else None
    chain: list[dict] = []
    raw_chain = raw.get("chain")
    if isinstance(raw_chain, list):
        for index, item in enumerate(raw_chain):
            ref = _norm_ref(item, axiom_key, f"chain[{index}]", issues)
            if ref:
                chain.append(ref)
    facets = raw.get("facets") if isinstance(raw.get("facets"), list) else []

    axiom = {
        "axiom_key": axiom_key,
        "kind": kind,
        "subject": subject,
        "object": object_ref,
        "chain": chain,
        "facets": facets,
        "status": status,
        "source": source,
        "origin": origin,
        "module": _norm(raw.get("module")) or None,
        "description": _norm(raw.get("description")) or None,
    }
    base_revision = raw.get("base_revision")
    if isinstance(base_revision, int) and base_revision >= 0:
        axiom["base_revision"] = base_revision
    axiom["content_hash"] = axiom_content_hash(axiom)
    return axiom, issues


def axiom_content_hash(axiom: dict) -> str:
    """内容指纹：只覆盖语义字段，不含 revision/origin 等元数据。"""
    payload = {
        "kind": axiom.get("kind"),
        "subject": axiom.get("subject"),
        "object": axiom.get("object"),
        "chain": axiom.get("chain") or [],
        "facets": axiom.get("facets") or [],
        "status": axiom.get("status"),
        "source": axiom.get("source"),
        "module": axiom.get("module"),
        "description": axiom.get("description"),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def axiom_identity(axiom: dict) -> str:
    payload = {
        "kind": axiom.get("kind"),
        "subject": axiom.get("subject"),
        "object": axiom.get("object"),
        "chain": axiom.get("chain") or [],
        "facets": axiom.get("facets") or [],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass
class AxiomIndex:
    context: SchemaContext
    sub_class_parents: dict[str, set[str]] = field(default_factory=dict)
    equivalent_parents: dict[str, set[str]] = field(default_factory=dict)
    sub_prop_parents: dict[tuple, set[tuple]] = field(default_factory=dict)
    disjoint_pairs: set[frozenset] = field(default_factory=set)
    characteristics: dict[str, set[str]] = field(default_factory=dict)
    relation_axiom: dict[str, str] = field(default_factory=dict)
    sub_prop_axiom: dict[tuple, str] = field(default_factory=dict)
    _ancestors: dict[str, set[str]] = field(default_factory=dict)

    def _class_key(self, name: str) -> str:
        return self.context.resolve_entity(name) or self.context._key(name)

    def ancestors(self, name: str) -> set[str]:
        """subClassOf + equivalentClass 的祖先闭包（含自身）。"""
        key = self._class_key(name)
        if key in self._ancestors:
            return self._ancestors[key]
        result: set[str] = {key}
        stack = [key]
        while stack:
            current = stack.pop()
            for parent in self.sub_class_parents.get(current, set()):
                if parent not in result:
                    result.add(parent)
                    stack.append(parent)
            for parent in self.equivalent_parents.get(current, set()):
                if parent not in result:
                    result.add(parent)
                    stack.append(parent)
        self._ancestors[key] = result
        return result

    def is_subclass_or_equal(self, child: str, parent: str) -> bool:
        return self._class_key(parent) in self.ancestors(child)

    def classes_disjoint(self, a: str, b: str) -> bool:
        if not a or not b:
            return False
        for left in self.ancestors(a):
            for right in self.ancestors(b):
                if frozenset((left, right)) in self.disjoint_pairs:
                    return True
        return False

    def relation_endpoints(self, relation: str, side: str) -> set[str]:
        return self.context.relation_endpoints(relation, side)


def _relation_key(ref: Optional[dict]) -> Optional[str]:
    if not ref or ref.get("kind") != "relation":
        return None
    return ref.get("ref") or ""


def _attribute_key(ref: Optional[dict]) -> Optional[tuple]:
    if not ref or ref.get("kind") != "attribute":
        return None
    return (ref.get("owner") or "", ref.get("ref") or "")


def build_index(context: SchemaContext, axioms: Iterable[dict]) -> AxiomIndex:
    index = AxiomIndex(context=context)
    for axiom in axioms:
        if _norm(axiom.get("status")) != "active":
            continue
        kind = _norm(axiom.get("kind"))
        subject = axiom.get("subject") or {}
        object_ref = axiom.get("object") or {}
        key = _norm(axiom.get("axiom_key"))

        if kind in ENTITY_AXIOM_KINDS:
            child = context.resolve_entity(subject.get("ref"))
            parent = context.resolve_entity(object_ref.get("ref"))
            if not child or not parent:
                continue
            if kind == "subClassOf":
                index.sub_class_parents.setdefault(child, set()).add(parent)
            elif kind == "equivalentClass":
                index.equivalent_parents.setdefault(child, set()).add(parent)
                index.equivalent_parents.setdefault(parent, set()).add(child)
            elif kind == "disjointWith":
                index.disjoint_pairs.add(frozenset((child, parent)))

        elif kind == "subPropertyOf":
            child_r = _relation_key(subject)
            parent_r = _relation_key(object_ref)
            if child_r and parent_r:
                child = context.resolve_relation(child_r)
                parent = context.resolve_relation(parent_r)
                if child and parent:
                    index.sub_prop_parents.setdefault(
                        ("relation", child), set(),
                    ).add(("relation", parent))
                    index.sub_prop_axiom[(("relation", child), ("relation", parent))] = key
            child_a = _attribute_key(subject)
            parent_a = _attribute_key(object_ref)
            if child_a and parent_a:
                owner = context.resolve_entity(child_a[0])
                attr = context.resolve_attribute(child_a[0], child_a[1])
                parent_owner = context.resolve_entity(parent_a[0])
                parent_attr = context.resolve_attribute(parent_a[0], parent_a[1])
                if owner and attr and parent_owner and parent_attr:
                    child = ("attribute", owner, attr[1])
                    parent = ("attribute", parent_owner, parent_attr[1])
                    index.sub_prop_parents.setdefault(child, set()).add(parent)
                    index.sub_prop_axiom[(child, parent)] = key

        elif kind in RELATION_CHARACTERISTICS:
            relation = context.resolve_relation(subject.get("ref"))
            if relation:
                index.characteristics.setdefault(relation, set()).add(kind)
                index.relation_axiom[f"{relation}:{kind}"] = key
    return index


def _check_refs(axiom: dict, context: SchemaContext) -> list[dict]:
    issues: list[dict] = []
    kind = axiom.get("kind")
    key = axiom.get("axiom_key") or ""
    subject = axiom.get("subject")
    object_ref = axiom.get("object")

    if kind in ENTITY_AXIOM_KINDS:
        expected_subject, expected_object = "entity", "entity"
    elif kind == "subPropertyOf":
        expected_subject = subject.get("kind") if isinstance(subject, dict) else None
        expected_object = expected_subject if expected_subject in {"relation", "attribute"} else None
    elif kind in RELATION_CHARACTERISTICS or kind == "propertyChain":
        expected_subject, expected_object = "relation", None
    elif kind == "datatypeFacet":
        expected_subject, expected_object = "attribute", None
    else:
        return issues

    def check_ref(ref, expected_kind, field_name, required=True):
        if not isinstance(ref, dict):
            return
        actual = ref.get("kind") or ""
        if expected_kind and actual != expected_kind:
            issues.append(_issue(
                "checks.axiom.missingRef", "Violation", key,
                field_name=field_name,
                params={"expectedKind": expected_kind, "actualKind": actual},
            ))
            return
        if actual == "entity":
            resolved = context.resolve_entity(ref.get("ref"))
            if not resolved:
                issues.append(_issue(
                    "checks.axiom.missingRef", "Violation", key,
                    field_name=field_name, params={"ref": ref.get("ref") or ""},
                ))
            elif resolved in context.deprecated_entities:
                issues.append(_issue(
                    "checks.ref.entity_exists", "Violation", key,
                    field_name=field_name,
                    params={"ref": ref.get("ref") or "", "deprecated": True},
                ))
        elif actual == "relation":
            raw_key = context._key(ref.get("ref"))
            resolved = context.resolve_relation(ref.get("ref"))
            # inverse_of 是虚拟名：引用存在，但它是「方向相反」，由语义检查报
            # checks.hierarchy.sub_of_inverse，不在这里当 missingRef。
            if not resolved and raw_key not in context.inverse_names:
                issues.append(_issue(
                    "checks.axiom.missingRef", "Violation", key,
                    field_name=field_name, params={"ref": ref.get("ref") or ""},
                ))
            elif resolved and resolved in context.deprecated_relations:
                issues.append(_issue(
                    "checks.ref.relation_exists", "Violation", key,
                    field_name=field_name,
                    params={"ref": ref.get("ref") or "", "deprecated": True},
                ))
        elif actual == "attribute":
            resolved = context.resolve_attribute(ref.get("owner"), ref.get("ref"))
            if not resolved:
                issues.append(_issue(
                    "checks.axiom.missingRef", "Violation", key,
                    field_name=field_name,
                    params={"ref": ref.get("ref") or "", "owner": ref.get("owner") or ""},
                ))

    check_ref(subject, expected_subject, "subject")
    if object_ref is not None:
        check_ref(object_ref, expected_object, "object")
    elif kind in ENTITY_AXIOM_KINDS or kind == "subPropertyOf":
        issues.append(_issue(
            "checks.axiom.missingRef", "Violation", key,
            field_name="object", params={"reason": "missing"},
        ))

    if kind == "propertyChain":
        chain = axiom.get("chain") or []
        if not chain:
            issues.append(_issue("checks.axiom.chainInvalid", "Violation", key,
                                 field_name="chain", params={"reason": "empty"}))
        elif len(chain) < 2:
            issues.append(_issue("checks.axiom.chainInvalid", "Violation", key,
                                 field_name="chain", params={"reason": "too_short"}))
        for position, ref in enumerate(chain):
            if (ref or {}).get("kind") != "relation":
                issues.append(_issue(
                    "checks.axiom.chainInvalid", "Violation", key,
                    field_name=f"chain[{position}]",
                    params={"actualKind": (ref or {}).get("kind") or ""},
                ))
            else:
                check_ref(ref, "relation", f"chain[{position}]")
    if kind == "datatypeFacet":
        facets = axiom.get("facets") or []
        if not facets:
            issues.append(_issue("checks.axiom.facetInvalid", "Violation", key,
                                 field_name="facets", params={"reason": "empty"}))
    return issues


def _check_semantics(axiom: dict, index: AxiomIndex) -> list[dict]:
    issues: list[dict] = []
    key = axiom.get("axiom_key") or ""
    kind = axiom.get("kind")
    subject = axiom.get("subject") or {}
    object_ref = axiom.get("object") or {}
    context = index.context

    if kind in {"subClassOf", "subPropertyOf"}:
        same = (
            subject.get("kind") == object_ref.get("kind")
            and subject.get("ref") == object_ref.get("ref")
            and subject.get("owner") == object_ref.get("owner")
        )
        if same:
            issues.append(_issue("checks.axiom.selfReference", "Violation", key))

    if kind == "equivalentClass":
        child = context.resolve_entity(subject.get("ref"))
        parent = context.resolve_entity(object_ref.get("ref"))
        if child and parent and frozenset((child, parent)) in index.disjoint_pairs:
            issues.append(_issue("checks.axiom.conflict", "Violation", key,
                                 params={"reason": "disjoint_vs_equivalent"}))

    if kind == "subPropertyOf":
        child_key = context._key(subject.get("ref"))
        parent_key = context._key(object_ref.get("ref"))
        child_name = context.resolve_relation(subject.get("ref"))
        parent_name = context.resolve_relation(object_ref.get("ref"))
        if child_name:
            if parent_name:
                child_chars = index.characteristics.get(child_name, set())
                parent_chars = index.characteristics.get(parent_name, set())
                if "irreflexive" in parent_chars and "reflexive" in child_chars:
                    issues.append(_issue("checks.axiom.conflict", "Violation", key,
                                         params={"reason": "sub_reflexive_parent_irreflexive"}))
                if "asymmetric" in parent_chars and "symmetric" in child_chars:
                    issues.append(_issue("checks.axiom.conflict", "Violation", key,
                                         params={"reason": "sub_symmetric_parent_asymmetric"}))
            # 方向相反被当成包含：R subPropertyOf S，且 S 是 R 的 inverse_of。
            child_rel = context.relations.get(child_name) or {}
            inverse_child = context._key(child_rel.get("inverse_of"))
            inverse_parent = ""
            if parent_name:
                parent_rel = context.relations.get(parent_name) or {}
                inverse_parent = context._key(parent_rel.get("inverse_of"))
            if (inverse_child and inverse_child == parent_key) \
                    or (inverse_parent and inverse_parent == child_key):
                issues.append(_issue(
                    "checks.hierarchy.sub_of_inverse", "Warning", key,
                ))

    if kind == "subClassOf":
        child = context.resolve_entity(subject.get("ref"))
        parent = context.resolve_entity(object_ref.get("ref"))
        if child and parent:
            # 关系重叠提示：类层次被误写成实例关系。
            for rel_name, rel in context.relations.items():
                if _norm(rel.get("name")).lower() not in _OVERLAP_RELATIONS:
                    continue
                src = context.resolve_entity(rel.get("source"))
                tgt = context.resolve_entity(rel.get("target"))
                if {src, tgt} == {child, parent}:
                    issues.append(_issue(
                        "checks.hierarchy.overlaps_relation", "Warning", key,
                        params={"relation": rel.get("name") or rel_name},
                    ))
                    break

    if kind == "subClassOf":
        # 不可满足类：自身闭包里出现互斥对。只在 subClassOf 上报告，
        # 避免 disjointWith/equivalentClass 的冲突被重复报成 unsatisfiable。
        child = context.resolve_entity(subject.get("ref"))
        if child:
            ancestors = index.ancestors(child)
            found = False
            for left in ancestors:
                for right in ancestors:
                    if left < right and frozenset((left, right)) in index.disjoint_pairs:
                        found = True
                        break
                if found:
                    break
            if found:
                issues.append(_issue(
                    "checks.hierarchy.unsatisfiable", "Violation", key,
                    params={"class": subject.get("ref") or ""},
                ))
    return issues


def _check_property_axioms(axiom: dict, index: AxiomIndex) -> list[dict]:
    if _norm(axiom.get("kind")) != "subPropertyOf":
        return []
    issues: list[dict] = []
    key = axiom.get("axiom_key") or ""
    context = index.context
    subject = axiom.get("subject") or {}
    object_ref = axiom.get("object") or {}

    if subject.get("kind") == "relation" and object_ref.get("kind") == "relation":
        child = context.resolve_relation(subject.get("ref"))
        parent = context.resolve_relation(object_ref.get("ref"))
        if not child or not parent:
            return issues
        for side in ("source", "target"):
            child_types = index.relation_endpoints(child, side)
            parent_types = index.relation_endpoints(parent, side)
            if not child_types or not parent_types:
                continue
            compatible = any(
                index.is_subclass_or_equal(c, p)
                for c in child_types for p in parent_types
            )
            if not compatible:
                issues.append(_issue(
                    "checks.hierarchy.property_domain_range", "Violation", key,
                    field_name=side,
                    params={"child": sorted(child_types), "parent": sorted(parent_types)},
                ))
            elif not all(
                any(index.is_subclass_or_equal(c, p) for p in parent_types)
                for c in child_types
            ):
                issues.append(_issue(
                    "checks.hierarchy.property_domain_range", "Warning", key,
                    field_name=side,
                    params={"child": sorted(child_types), "parent": sorted(parent_types)},
                ))
        return issues

    if subject.get("kind") == "attribute" and object_ref.get("kind") == "attribute":
        child_owner = subject.get("owner") or ""
        parent_owner = object_ref.get("owner") or ""
        child_entity = context.resolve_entity(child_owner)
        parent_entity = context.resolve_entity(parent_owner)
        if not child_entity or not parent_entity:
            return issues
        if not index.is_subclass_or_equal(child_entity, parent_entity):
            if index.classes_disjoint(child_entity, parent_entity):
                issues.append(_issue(
                    "checks.hierarchy.property_domain_range", "Violation", key,
                    field_name="owner",
                    params={"child": child_owner, "parent": parent_owner},
                ))
            else:
                issues.append(_issue(
                    "checks.hierarchy.property_domain_range", "Warning", key,
                    field_name="owner",
                    params={"child": child_owner, "parent": parent_owner},
                ))
        child_type = context.attribute_type(child_owner, subject.get("ref") or "")
        parent_type = context.attribute_type(parent_owner, object_ref.get("ref") or "")
        if child_type and parent_type and not _datatype_compatible(child_type, parent_type):
            issues.append(_issue(
                "checks.hierarchy.property_datatype", "Violation", key,
                params={"child": child_type, "parent": parent_type},
            ))
    return issues


def _datatype_compatible(child: str, parent: str) -> bool:
    if child == parent:
        return True
    # 文本类内部互通；其余按精确匹配，避免把 number→string 当成可解释。
    return {child, parent} <= {"string", "text"}


def _check_cycles(axioms: Iterable[dict], context: SchemaContext) -> list[dict]:
    issues: list[dict] = []
    for kind in ("subClassOf", "subPropertyOf"):
        edges: dict[str, set[str]] = {}
        edge_axiom: dict[tuple[str, str], str] = {}
        for axiom in axioms:
            if _norm(axiom.get("status")) != "active" or _norm(axiom.get("kind")) != kind:
                continue
            subject = axiom.get("subject") or {}
            object_ref = axiom.get("object") or {}
            if kind == "subClassOf":
                child = context.resolve_entity(subject.get("ref"))
                parent = context.resolve_entity(object_ref.get("ref"))
            elif subject.get("kind") == "relation" and object_ref.get("kind") == "relation":
                child = context.resolve_relation(subject.get("ref"))
                parent = context.resolve_relation(object_ref.get("ref"))
            elif subject.get("kind") == "attribute" and object_ref.get("kind") == "attribute":
                child = context.resolve_attribute(subject.get("owner"), subject.get("ref"))
                parent = context.resolve_attribute(object_ref.get("owner"), object_ref.get("ref"))
                if child:
                    child = f"attr:{child[0]}:{child[1]}"
                if parent:
                    parent = f"attr:{parent[0]}:{parent[1]}"
            else:
                child = parent = None
            if not child or not parent or child == parent:
                # 自反由 checks.axiom.selfReference 负责，不当成环。
                continue
            edges.setdefault(child, set()).add(parent)
            edge_axiom[(child, parent)] = axiom.get("axiom_key") or ""

        state: dict[str, int] = {}
        reported: set[tuple[str, str]] = set()

        def visit(node: str) -> None:
            state[node] = 1
            for nxt in edges.get(node, set()):
                if state.get(nxt) == 1:
                    edge = (node, nxt)
                    if edge not in reported:
                        reported.add(edge)
                        issues.append(_issue(
                            "checks.hierarchy.cycle", "Violation",
                            edge_axiom.get(edge, ""),
                            params={"subject": node, "object": nxt, "kind": kind},
                        ))
                elif nxt not in state:
                    visit(nxt)
            state[node] = 2

        for node in sorted(edges):
            if node not in state:
                visit(node)
    return issues


def _check_characteristic_conflicts(axioms: Iterable[dict]) -> list[dict]:
    issues: list[dict] = []
    by_relation: dict[str, dict[str, str]] = {}
    for axiom in axioms:
        if _norm(axiom.get("status")) != "active":
            continue
        kind = _norm(axiom.get("kind"))
        if kind not in RELATION_CHARACTERISTICS:
            continue
        ref = (axiom.get("subject") or {}).get("ref") or ""
        if ref:
            by_relation.setdefault(ref, {})[kind] = axiom.get("axiom_key") or ""
    for ref, chars in by_relation.items():
        if "symmetric" in chars and "asymmetric" in chars:
            issues.append(_issue("checks.axiom.conflict", "Violation",
                                 chars["asymmetric"],
                                 params={"ref": ref, "reason": "symmetric_asymmetric"}))
        if "reflexive" in chars and "irreflexive" in chars:
            issues.append(_issue("checks.axiom.conflict", "Violation",
                                 chars["irreflexive"],
                                 params={"ref": ref, "reason": "reflexive_irreflexive"}))
        if "transitive" in chars and ({"functional", "inverseFunctional"} & set(chars)):
            issues.append(_issue("checks.axiom.characteristic_risk", "Warning",
                                 chars["transitive"],
                                 params={"ref": ref, "characteristics": sorted(chars)}))
    return issues


def _check_duplicates(axioms: list[dict]) -> list[dict]:
    issues: list[dict] = []
    seen: dict[str, str] = {}
    for axiom in axioms:
        identity = axiom_identity(axiom)
        key = axiom.get("axiom_key") or ""
        if identity in seen and seen[identity] != key:
            issues.append(_issue(
                "checks.axiom.duplicate", "Violation", key,
                params={"other": seen[identity]},
            ))
        else:
            seen.setdefault(identity, key)
    return issues


def check_axioms(
    raw_axioms: Iterable[dict],
    context: SchemaContext,
    existing_active: Optional[Iterable[dict]] = None,
) -> tuple[list[dict], list[dict]]:
    """规范化并校验一批公理；返回 (normalized, issues)。

    `existing_active` 是库里已有的 active 公理，用来检测跨批冲突/环；
    issues 只针对本批公理给出（read-time 列表会单独跑单条校验）。
    """
    normalized: list[dict] = []
    issues: list[dict] = []
    for raw in raw_axioms:
        axiom, axiom_issues = normalize_axiom(raw)
        normalized.append(axiom)
        issues.extend(axiom_issues)

    existing = [a for a in (existing_active or []) if _norm(a.get("status")) == "active"]
    incoming_active = [a for a in normalized if _norm(a.get("status")) == "active"]

    for axiom in normalized:
        issues.extend(_check_refs(axiom, context))

    index = build_index(context, [*existing, *incoming_active])
    for axiom in incoming_active:
        issues.extend(_check_semantics(axiom, index))
        issues.extend(_check_property_axioms(axiom, index))
    issues.extend(_check_characteristic_conflicts([*existing, *incoming_active]))
    issues.extend(_check_cycles([*existing, *incoming_active], context))
    issues.extend(_check_duplicates([*existing, *normalized]))

    # 去重（同一 code/severity/target/field/params），避免同一问题重复刷屏。
    unique: list[dict] = []
    seen: set[str] = set()
    for item in issues:
        signature = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if signature in seen:
            continue
        seen.add(signature)
        unique.append(item)
    return normalized, unique


def has_violation(issues: Iterable[dict]) -> bool:
    return any(item.get("severity") == "Violation" for item in issues)


__all__ = [
    "AxiomIndex",
    "ENTITY_AXIOM_KINDS",
    "RELATION_CHARACTERISTICS",
    "axiom_content_hash",
    "axiom_identity",
    "build_index",
    "check_axioms",
    "has_violation",
    "normalize_axiom",
]
