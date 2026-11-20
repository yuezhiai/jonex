"""推理上下文：合并视图 + active 公理 + 缓存指纹（T5）。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Optional

from jonex_core.common.database import get_db_session
from jonex_core.common.tenant import require_tenant

from ...repository.ontology_axiom_repository import OntologyAxiomRepository
from ..ontology_compiler import OntologyCompiler
from .schema_context import SchemaContext


def _hash(payload) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class ReasoningContext:
    merged_schema: dict
    axioms: list[dict]
    schema_context: SchemaContext
    fingerprint: str


async def resolve_reasoning_context(
    tenant_id: str,
    knowledge_base_id: str,
    *,
    compiler: Optional[OntologyCompiler] = None,
    session_cm=get_db_session,
    repo_cls=OntologyAxiomRepository,
) -> ReasoningContext:
    """取 S1–S4 共用的上下文。

    只调用 **raw** schema 读取（未展开），否则展开入口会自己调用本函数，形成递归。
    """
    tenant_id = require_tenant(tenant_id)
    compiler = compiler or OntologyCompiler()

    schema = await compiler._get_merged_schema_raw(tenant_id, knowledge_base_id)
    if not schema:
        schema = await compiler._get_compiled_schema_raw(
            tenant_id, knowledge_base_id, auto_compile=True,
        )
    schema = schema if isinstance(schema, dict) else {}

    async with session_cm() as session:
        rows = await repo_cls(session).list_by_kb(
            tenant_id, knowledge_base_id, status="active", limit=10_000,
        )
    axioms = [row.to_dict() for row in rows]

    schema_fp = _hash({
        "schema_version": schema.get("schema_version"),
        "source_hash": schema.get("source_hash"),
        "sources": schema.get("sources"),
        "entities": [
            [e.get("name"), e.get("status"), e.get("aliases")]
            for e in schema.get("entity_types") or []
        ],
        "relations": [
            [r.get("name"), r.get("status"), r.get("source"), r.get("target"),
             r.get("source_types"), r.get("target_types"), r.get("inverse_of")]
            for r in schema.get("relation_types") or []
        ],
        "constraints": [
            [c.get("name"), c.get("target_type"), c.get("target_code"),
             c.get("constraint_type"), c.get("enabled")]
            for c in schema.get("constraints") or []
        ],
    })
    axiom_fp = _hash([
        [a.get("axiom_key"), a.get("content_revision"), a.get("content_hash")]
        for a in axioms
    ])
    return ReasoningContext(
        merged_schema=schema,
        axioms=axioms,
        schema_context=SchemaContext.from_schema(schema),
        fingerprint=f"{schema_fp}:{axiom_fp}",
    )


__all__ = ["ReasoningContext", "resolve_reasoning_context"]
