"""规则冲突检测：规则与规则、规则与公理（T6）。"""
from __future__ import annotations

import json
from typing import Iterable

from .axiom_checks import AxiomIndex, build_index
from .rule_checks import canonical_ast, rule_issue
from .schema_context import SchemaContext


def _json(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _conclusion_key(rule: dict) -> str:
    return _json(canonical_ast(rule, rename_vars=True)["conclusion"])


def _premise_set(rule: dict) -> set[str]:
    ast = canonical_ast(rule, rename_vars=True)
    return {_json(atom) for atom in ast["premises"]}


def _predicate(atom: dict) -> str:
    if atom.get("kind") == "rel":
        return f"rel:{atom.get('p')}"
    return f"type:{atom.get('cls')}"


def _transitive_form(rule: dict) -> bool:
    premises = rule.get("premises") or []
    conclusion = rule.get("conclusion") or {}
    if len(premises) != 2 or conclusion.get("kind") != "rel":
        return False
    if any(atom.get("kind") != "rel" for atom in premises):
        return False
    p = conclusion.get("p")
    if not p or premises[0].get("p") != p or premises[1].get("p") != p:
        return False
    first, second = premises
    # 正向：?a P ?b AND ?b P ?c ⇒ ?a P ?c
    forward = (
        first.get("s") == conclusion.get("s")
        and second.get("o") == conclusion.get("o")
        and first.get("o") == second.get("s")
    )
    # 逆名归一后可能变成反序：?c P ?b AND ?b P ?a ⇒ ?c P ?a
    backward = (
        second.get("s") == conclusion.get("s")
        and first.get("o") == conclusion.get("o")
        and second.get("o") == first.get("s")
    )
    return forward or backward


def _rule_active(rule: dict) -> bool:
    return (
        str(rule.get("status") or "") == "active"
        and rule.get("enabled") is not False
    )


def _disjoint_conflicts(rule: dict, context: SchemaContext, index: AxiomIndex) -> list[dict]:
    issues: list[dict] = []
    key = rule.get("rule_key") or ""
    conclusion = rule.get("conclusion") or {}
    if conclusion.get("kind") != "type":
        return issues
    cls = conclusion.get("cls")
    var = conclusion.get("s")
    if not cls or not var:
        return issues

    def check(candidate: str | None) -> None:
        if candidate and candidate != cls and index.classes_disjoint(candidate, cls):
            issues.append(rule_issue(
                "checks.rule.disjoint_conflict", "Violation", key,
                params={"variable": var, "class": cls, "conflicts_with": candidate},
            ))

    for atom in rule.get("premises") or []:
        if atom.get("kind") == "type" and atom.get("s") == var:
            check(atom.get("cls"))
        elif atom.get("kind") == "rel":
            side = "source" if atom.get("s") == var else "target" if atom.get("o") == var else None
            if not side:
                continue
            for endpoint in context.relation_endpoints(atom.get("p") or "", side):
                check(endpoint)
    return issues


def _characteristic_conflicts(
    rule: dict, context: SchemaContext, index: AxiomIndex,
) -> list[dict]:
    issues: list[dict] = []
    key = rule.get("rule_key") or ""
    conclusion = rule.get("conclusion") or {}
    if conclusion.get("kind") != "rel":
        return issues
    relation = conclusion.get("p") or ""
    characteristics = index.characteristics.get(relation, set())
    if conclusion.get("s") == conclusion.get("o") and "irreflexive" in characteristics:
        issues.append(rule_issue(
            "checks.rule.characteristic_conflict", "Violation", key,
            params={"relation": relation, "reason": "irreflexive_self_loop"},
        ))
    if "asymmetric" in characteristics:
        for atom in rule.get("premises") or []:
            if (
                atom.get("kind") == "rel"
                and atom.get("p") == relation
                and atom.get("s") == conclusion.get("o")
                and atom.get("o") == conclusion.get("s")
            ):
                issues.append(rule_issue(
                    "checks.rule.characteristic_conflict", "Violation", key,
                    params={"relation": relation, "reason": "asymmetric_reverse"},
                ))
                break
    return issues


def _cardinality_risk(
    rule: dict, context: SchemaContext, index: AxiomIndex,
) -> list[dict]:
    conclusion = rule.get("conclusion") or {}
    if conclusion.get("kind") != "rel":
        return []
    relation = conclusion.get("p") or ""
    key_name = context.resolve_relation(relation)
    rel_def = context.relations.get(key_name or "") or {}
    characteristics = index.characteristics.get(
        (rel_def.get("name") or relation), set(),
    )
    cardinality = str(rel_def.get("cardinality") or "").lower()
    if "functional" in characteristics or cardinality == "one_to_one":
        return [rule_issue(
            "checks.rule.cardinality_risk", "Warning", rule.get("rule_key") or "",
            params={"relation": relation},
        )]
    return []


def _recursion(rules: list[dict]) -> list[dict]:
    by_key = {rule.get("rule_key"): rule for rule in rules}
    conclusion_predicates: dict[str, set[str]] = {}
    for rule in rules:
        conclusion = rule.get("conclusion") or {}
        if conclusion:
            conclusion_predicates.setdefault(_predicate(conclusion), set()).add(
                rule.get("rule_key"),
            )

    edges: dict[str, set[str]] = {key: set() for key in by_key}
    for rule in rules:
        key = rule.get("rule_key")
        for premise in rule.get("premises") or []:
            for other in conclusion_predicates.get(_predicate(premise), set()):
                if other == key and _transitive_form(rule):
                    continue
                edges[key].add(other)

    state: dict[str, int] = {}
    stack: list[str] = []
    cycle_nodes: set[str] = set()

    def visit(node: str) -> None:
        state[node] = 1
        stack.append(node)
        for nxt in edges.get(node, set()):
            if state.get(nxt) == 1:
                try:
                    index = stack.index(nxt)
                    cycle_nodes.update(stack[index:])
                except ValueError:
                    cycle_nodes.add(nxt)
            elif nxt not in state:
                visit(nxt)
        stack.pop()
        state[node] = 2

    for node in edges:
        if node not in state:
            visit(node)

    return [
        rule_issue("checks.rule.recursion", "Violation", key,
                   params={"rule": key})
        for key in sorted(cycle_nodes)
    ]


def _depends_on_inactive(
    rule: dict, context: SchemaContext, rejected_axioms: Iterable[dict],
) -> list[dict]:
    issues: list[dict] = []
    key = rule.get("rule_key") or ""
    rejected_refs: set[str] = set()
    for axiom in rejected_axioms or []:
        subject = axiom.get("subject") or {}
        ref = str(subject.get("ref") or "")
        if ref:
            rejected_refs.add(ref)
    for atom in rule.get("premises") or []:
        if atom.get("kind") == "rel":
            relation = atom.get("p") or ""
            resolved = context.resolve_relation(relation)
            if resolved in context.deprecated_relations or relation in rejected_refs:
                issues.append(rule_issue(
                    "checks.rule.depends_on_inactive", "Warning", key,
                    params={"relation": relation},
                ))
        elif atom.get("kind") == "type":
            cls = atom.get("cls") or ""
            if cls in rejected_refs:
                issues.append(rule_issue(
                    "checks.rule.depends_on_inactive", "Warning", key,
                    params={"class": cls},
                ))
    return issues


def check_rule_conflicts(
    rules: Iterable[dict],
    context: SchemaContext,
    *,
    active_axioms: Iterable[dict] = (),
    rejected_axioms: Iterable[dict] = (),
) -> list[dict]:
    """只对 active && enabled 的规则做冲突检测。"""
    active_rules = [rule for rule in rules if _rule_active(rule)]
    active_axioms = list(active_axioms)
    index = build_index(context, active_axioms)
    issues: list[dict] = []

    # duplicate / subsumed
    by_key = {rule.get("rule_key"): rule for rule in active_rules}
    keys = list(by_key)
    for i, left_key in enumerate(keys):
        left = by_key[left_key]
        left_ast = canonical_ast(left, rename_vars=True)
        left_premises = _premise_set(left)
        for right_key in keys[i + 1:]:
            right = by_key[right_key]
            right_ast = canonical_ast(right, rename_vars=True)
            if left_ast == right_ast:
                issues.append(rule_issue(
                    "checks.rule.duplicate", "Violation", right_key,
                    params={"other": left_key},
                ))
                continue
            if left_ast.get("conclusion") != right_ast.get("conclusion"):
                continue
            right_premises = _premise_set(right)
            if left_premises < right_premises:
                issues.append(rule_issue(
                    "checks.rule.subsumed", "Warning", right_key,
                    params={"other": left_key},
                ))
            elif right_premises < left_premises:
                issues.append(rule_issue(
                    "checks.rule.subsumed", "Warning", left_key,
                    params={"other": right_key},
                ))

    for rule in active_rules:
        key = rule.get("rule_key") or ""
        conclusion = rule.get("conclusion") or {}
        relation = conclusion.get("p") if conclusion.get("kind") == "rel" else None
        if relation and "transitive" in index.characteristics.get(relation, set()) \
                and _transitive_form(rule):
            issues.append(rule_issue(
                "checks.rule.redundant_with_axiom", "Warning", key,
                params={"relation": relation},
            ))
        issues.extend(_disjoint_conflicts(rule, context, index))
        issues.extend(_characteristic_conflicts(rule, context, index))
        issues.extend(_cardinality_risk(rule, context, index))
        issues.extend(_depends_on_inactive(rule, context, rejected_axioms))

    issues.extend(_recursion(active_rules))
    unique: list[dict] = []
    seen: set[str] = set()
    for item in issues:
        signature = _json(item)
        if signature not in seen:
            seen.add(signature)
            unique.append(item)
    return unique


__all__ = ["check_rule_conflicts"]
