"""推理契约常量（与 contracts/reasoning_rules/ 保持一致）。"""
from __future__ import annotations

CONTRACT_VERSION = 1
RULE_GRAMMAR_VERSION = 1

#: 与 `contracts/reasoning_rules/axioms.schema.json` 的 kind 枚举逐字一致。
AXIOM_KINDS = (
    "subClassOf",
    "disjointWith",
    "equivalentClass",
    "subPropertyOf",
    "transitive",
    "symmetric",
    "functional",
    "inverseFunctional",
    "reflexive",
    "irreflexive",
    "asymmetric",
    "propertyChain",
    "datatypeFacet",
)

AXIOM_STATUSES = ("proposed", "active", "rejected")
AXIOM_SOURCES = ("manual", "imported", "extracted")
AXIOM_ORIGINS = ("studio", "platform", "imported")


def contract_payload() -> dict:
    """`GET /ontology/reasoning/contract` 的响应体。"""
    return {
        "contract_version": CONTRACT_VERSION,
        "axiom_kinds": list(AXIOM_KINDS),
        "rule_grammar_version": RULE_GRAMMAR_VERSION,
        "axiom_statuses": list(AXIOM_STATUSES),
    }


__all__ = [
    "AXIOM_KINDS",
    "AXIOM_ORIGINS",
    "AXIOM_SOURCES",
    "AXIOM_STATUSES",
    "CONTRACT_VERSION",
    "RULE_GRAMMAR_VERSION",
    "contract_payload",
]
