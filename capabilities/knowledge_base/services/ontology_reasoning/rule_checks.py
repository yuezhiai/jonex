"""规则模型规范化 + 语法/引用/类型校验（T6，纯函数）。"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Iterable, Optional

from .axiom_checks import AxiomIndex, build_index
from .contract import AXIOM_ORIGINS, AXIOM_STATUSES
from .rule_parse import parse_rule_text
from .schema_context import SchemaContext

RULE_STATUSES = AXIOM_STATUSES
RULE_ORIGINS = AXIOM_ORIGINS


def rule_issue(
    code: str, severity: str, rule_key: str = "", *, field: str = "",
    params: Optional[dict] = None,
) -> dict:
    target = {"kind": "rule", "id": rule_key or ""}
    if field:
        target["field"] = field
    out = {"code": code, "severity": severity, "target": target}
    if params:
        out["params"] = params
    return out


def _canonical_json(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _norm_var(value) -> str:
    return str(value or "").strip().lstrip("?")


def _resolve_relation(context: SchemaContext, name: str) -> str:
    key = context.resolve_relation(name)
    return (context.relations.get(key) or {}).get("name") or name if key else name


def _resolve_entity(context: SchemaContext, name: str) -> str:
    key = context.resolve_entity(name)
    return (context.entities.get(key) or {}).get("name") or name if key else name


def _normalize_atom(atom, context: SchemaContext, key: str) -> tuple[Optional[dict], list[dict]]:
    issues: list[dict] = []
    if not isinstance(atom, dict):
        issues.append(rule_issue("checks.rule.syntax", "Violation", key, field="atom"))
        return None, issues
    kind = str(atom.get("kind") or "").strip()
    s = _norm_var(atom.get("s"))
    if kind == "rel":
        p = str(atom.get("p") or "").strip()
        o = _norm_var(atom.get("o"))
        if not s or not p or not o:
            issues.append(rule_issue("checks.rule.syntax", "Violation", key, field="atom"))
            return None, issues
        return {"kind": "rel", "s": s, "p": _resolve_relation(context, p), "o": o}, issues
    if kind == "type":
        cls = str(atom.get("cls") or "").strip()
        if not s or not cls:
            issues.append(rule_issue("checks.rule.syntax", "Violation", key, field="atom"))
            return None, issues
        return {"kind": "type", "s": s, "cls": _resolve_entity(context, cls)}, issues
    issues.append(rule_issue("checks.rule.syntax", "Violation", key, field="atom",
                             params={"actualKind": kind}))
    return None, issues


def ast_variables(ast: dict) -> set[str]:
    variables: set[str] = set()
    for atom in (ast.get("premises") or []):
        if atom.get("s"):
            variables.add(atom["s"])
        if atom.get("o"):
            variables.add(atom["o"])
    conclusion = ast.get("conclusion") or {}
    if conclusion.get("s"):
        variables.add(conclusion["s"])
    if conclusion.get("o"):
        variables.add(conclusion["o"])
    return variables


def canonical_ast(ast: dict, *, rename_vars: bool = False) -> dict:
    premises = list(ast.get("premises") or [])
    conclusion = ast.get("conclusion") or {}
    if rename_vars:
        mapping: dict[str, str] = {}
        def remap(atom: dict) -> dict:
            out = dict(atom)
            for field in ("s", "o"):
                if out.get(field):
                    var = out[field]
                    if var not in mapping:
                        mapping[var] = f"v{len(mapping) + 1}"
                    out[field] = mapping[var]
            return out
        premises = [remap(a) for a in premises]
        conclusion = remap(conclusion)
    return {"premises": premises, "conclusion": conclusion}


def rule_content_hash(rule: dict) -> str:
    payload = {
        "name": rule.get("name"),
        "text": rule.get("text"),
        "premises": rule.get("premises") or [],
        "conclusion": rule.get("conclusion") or {},
        "status": rule.get("status"),
        "description": rule.get("description"),
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def normalize_rule(
    raw, context: SchemaContext, *, existing_active: Iterable[dict] = (),
) -> tuple[dict, list[dict]]:
    issues: list[dict] = []
    if not isinstance(raw, dict):
        key = f"r_{uuid.uuid4().hex}"
        return {"rule_key": key, "name": "", "text": "", "premises": [], "conclusion": {},
                "status": "active", "enabled": True, "origin": "imported"}, [
            rule_issue("checks.rule.syntax", "Violation", key, params={"reason": "not_an_object"}),
        ]
    key = str(raw.get("rule_key") or raw.get("id") or "").strip() or f"r_{uuid.uuid4().hex}"
    name = str(raw.get("name") or "").strip()
    text = str(raw.get("text") or "")
    status = str(raw.get("status") or "active").strip().lower()
    enabled = raw.get("enabled") is not False
    if "enabled" in raw and not isinstance(raw.get("enabled"), bool):
        issues.append(rule_issue("checks.rule.syntax", "Violation", key,
                                 field="enabled", params={"enabled": raw.get("enabled")}))
    origin = str(raw.get("origin") or "imported").strip().lower()
    if status not in RULE_STATUSES:
        issues.append(rule_issue("checks.rule.syntax", "Violation", key,
                                 params={"status": status}))
    if origin not in RULE_ORIGINS:
        issues.append(rule_issue("checks.rule.syntax", "Violation", key,
                                 params={"origin": origin}))

    parsed_ast: Optional[dict] = None
    if text.strip():
        parsed_ast, parse_issues = parse_rule_text(text, context, rule_key=key)
        issues.extend(parse_issues)

    premise_raw = raw.get("premises")
    conclusion_raw = raw.get("conclusion")
    if isinstance(premise_raw, list) and isinstance(conclusion_raw, dict):
        premises: list[dict] = []
        for atom in premise_raw:
            normalized, atom_issues = _normalize_atom(atom, context, key)
            issues.extend(atom_issues)
            if normalized:
                premises.append(normalized)
        conclusion, atom_issues = _normalize_atom(conclusion_raw, context, key)
        issues.extend(atom_issues)
        if conclusion is None:
            conclusion = {}
        if not premises and not conclusion and parsed_ast:
            premises = parsed_ast["premises"]
            conclusion = parsed_ast["conclusion"]
    elif parsed_ast:
        premises = parsed_ast["premises"]
        conclusion = parsed_ast["conclusion"]
    else:
        premises, conclusion = [], {}
        issues.append(rule_issue("checks.rule.syntax", "Violation", key,
                                 field="premises", params={"reason": "missing"}))

    ast = {"premises": premises, "conclusion": conclusion}
    if parsed_ast and premises and conclusion:
        if canonical_ast(parsed_ast) != canonical_ast(ast):
            issues.append(rule_issue(
                "checks.rule.text_ast_drift", "Warning", key,
                params={"reason": "text_and_ast_differ"},
            ))

    rule = {
        "rule_key": key,
        "name": name or key,
        "text": text,
        "premises": premises,
        "conclusion": conclusion,
        "status": status,
        "enabled": enabled,
        "origin": origin,
        "module": str(raw.get("module") or "").strip() or None,
        "description": str(raw.get("description") or "").strip() or None,
    }
    base_revision = raw.get("base_revision")
    if isinstance(base_revision, int) and base_revision >= 0:
        rule["base_revision"] = base_revision
    rule["content_hash"] = rule_content_hash(rule)

    # 单条校验
    issues.extend(_check_one(rule, context, existing_active))
    return rule, _dedupe(issues)


def _dedupe(issues: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for item in issues:
        signature = _canonical_json(item)
        if signature not in seen:
            seen.add(signature)
            out.append(item)
    return out


def _check_one(
    rule: dict, context: SchemaContext, existing_active: Iterable[dict],
) -> list[dict]:
    issues: list[dict] = []
    key = rule.get("rule_key")
    premises = rule.get("premises") or []
    conclusion = rule.get("conclusion") or {}

    if len(premises) > 4:
        issues.append(rule_issue("checks.rule.limits", "Violation", key,
                                 params={"premises": len(premises), "max": 4}))
    variables = ast_variables({**rule, "premises": premises})
    if len(variables) > 4:
        issues.append(rule_issue("checks.rule.limits", "Violation", key,
                                 params={"variables": len(variables), "max": 4}))
    if not premises or not conclusion:
        return issues

    conclusion_vars = {v for v in (conclusion.get("s"), conclusion.get("o")) if v}
    premise_vars = ast_variables({"premises": premises, "conclusion": {}})
    unsafe = conclusion_vars - premise_vars
    if unsafe:
        issues.append(rule_issue("checks.rule.unsafe_variable", "Violation", key,
                                 params={"variables": sorted(unsafe)}))

    for atom in [*premises, conclusion]:
        if atom.get("kind") == "rel":
            name = atom.get("p") or ""
            resolved = context.resolve_relation(name)
            if not resolved or resolved in context.deprecated_relations:
                issues.append(rule_issue("checks.rule.ref_unknown", "Violation", key,
                                         params={"relation": name}))
        elif atom.get("kind") == "type":
            name = atom.get("cls") or ""
            resolved = context.resolve_entity(name)
            if not resolved or resolved in context.deprecated_entities:
                issues.append(rule_issue("checks.rule.ref_unknown", "Violation", key,
                                         params={"class": name}))

    issues.extend(_check_type_mismatch(rule, context, existing_active))
    return issues


def _relation_endpoints(context: SchemaContext, relation_name: str, side: str) -> set[str]:
    key = context.resolve_relation(relation_name)
    relation = context.relations.get(key or "") or {}
    values = [str(v).strip() for v in (relation.get(f"{side}_types") or []) if str(v).strip()]
    if values:
        return set(values)
    single = str(relation.get(side) or "").strip()
    return {single} if single else set()


def _check_type_mismatch(
    rule: dict, context: SchemaContext, existing_active: Iterable[dict],
) -> list[dict]:
    """变量在不同原子里的类型约束是否互斥（端点 + subClassOf 闭包）。"""
    issues: list[dict] = []
    key = rule.get("rule_key")
    index: AxiomIndex = build_index(context, existing_active)
    constraints: dict[str, list[set[str]]] = {}
    for atom in rule.get("premises") or []:
        if atom.get("kind") == "type":
            constraints.setdefault(atom.get("s"), []).append({atom.get("cls")})
        elif atom.get("kind") == "rel":
            source = _relation_endpoints(context, atom.get("p"), "source")
            target = _relation_endpoints(context, atom.get("p"), "target")
            if source:
                constraints.setdefault(atom.get("s"), []).append(source)
            if target:
                constraints.setdefault(atom.get("o"), []).append(target)

    for var, groups in constraints.items():
        found = False
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                compatible = any(
                    index.is_subclass_or_equal(a, b) or index.is_subclass_or_equal(b, a)
                    for a in groups[i] for b in groups[j]
                )
                if not compatible and all(
                    index.classes_disjoint(a, b) for a in groups[i] for b in groups[j]
                ):
                    found = True
                    break
            if found:
                break
        if found:
            issues.append(rule_issue("checks.rule.type_mismatch", "Violation", key,
                                     params={"variable": var}))
    return issues


def check_rules(
    raw_rules: Iterable[dict], context: SchemaContext,
    existing_active: Iterable[dict] = (),
) -> tuple[list[dict], list[dict]]:
    normalized: list[dict] = []
    issues: list[dict] = []
    existing_active = list(existing_active)
    for raw in raw_rules:
        rule, rule_issues = normalize_rule(raw, context, existing_active=existing_active)
        normalized.append(rule)
        issues.extend(rule_issues)
    return normalized, _dedupe(issues)


def has_violation(issues: Iterable[dict]) -> bool:
    return any(item.get("severity") == "Violation" for item in issues)


__all__ = [
    "RULE_ORIGINS",
    "RULE_STATUSES",
    "ast_variables",
    "canonical_ast",
    "check_rules",
    "has_violation",
    "normalize_rule",
    "rule_content_hash",
    "rule_issue",
]
