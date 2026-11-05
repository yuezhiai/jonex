"""治理服务的 KB/编译 schema 公共校验。"""
from __future__ import annotations

from jonex_core.common.exceptions import InvalidParameterError, ResourceNotFoundError


async def ensure_kb(session, kb_id: str, tenant_id: str) -> None:
    if not str(kb_id or "").strip():
        raise ResourceNotFoundError(message="知识库不存在", details={"id": kb_id})
    from ...repository.knowledge_info_repository import KnowledgeInfoRepository
    await KnowledgeInfoRepository(session).get_required(kb_id, tenant_id)


async def schema_tables(session, tenant_id: str, kb_id: str,
                        data_source_id: str) -> dict[str, set[str]]:
    from ...repository.ontology_schema_repository import OntologySchemaRepository
    rows = await OntologySchemaRepository(session).list_compiled_schemas(tenant_id, kb_id)
    row = next((r for r in rows if r.data_source_id == data_source_id), None)
    if row is None:
        raise InvalidParameterError(f"该数据源没有可用的编译 schema：{data_source_id}")
    out: dict[str, set[str]] = {}
    for e in (row.entity_types or []):
        if isinstance(e, dict) and str(e.get("source_object_id") or "").strip():
            out[str(e["source_object_id"]).strip()] = {
                str(a.get("name") or "") for a in (e.get("attributes") or [])
                if isinstance(a, dict)
            }
    return out
