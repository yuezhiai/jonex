#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Repository for KnowledgeInfo."""

from sqlalchemy import text

from jonex_core.common.repository import BaseRepository

from ..models.knowledge_info import KnowledgeInfo

#: KB 删除时要软删的 KB 级配置表（列口径一致：tenant_id + knowledge_base_id + is_deleted）。
_SOFT_DELETE_TABLES = (
    "knowledge_data_sources",
    "knowledge_parser_settings",
    "folders",
    "tags",
)

#: KB 删除时要硬删的派生表（可重建，不属于业务事实）。
_HARD_DELETE_TABLES = (
    "ontology_template_bindings",
    "ontology_compiled_schemas",
    "llm_wiki_schemas",
)


async def cleanup_kb_residue(session, tenant_id: str, kb_id: str) -> dict:
    """清理 KB 级残留：文档行软删、配置软删、派生表硬删。

    返回 `{documents, document_tags, storage, soft_deleted, deleted}`：
    - `documents`：本次软删的文档行数；
    - `document_tags`：硬删的文档-标签关联数；
    - `storage`：去重后的 `[{key, backend}]`，由调用方 best-effort 删对象存储；
    - `soft_deleted` / `deleted`：各表影响行数（便于审计与日志）。

    **不删** `knowledge_info` 本身，也**不碰**检索索引（LightRAG/引擎投影/Neo4j）：
    索引清理是文档删除链路的职责，KB 级软删由任务看门狗收口在途任务。
    幂等：重复调用时没有未删行，返回全 0 / 空 storage，不报错。
    """
    doc_rows = (
        await session.execute(
            text("""
                SELECT id, storage_key, storage_backend
                  FROM knowledge_base.knowledge_documents
                 WHERE tenant_id = :tenant_id AND knowledge_base_id = :kb_id
            """),
            {"tenant_id": tenant_id, "kb_id": kb_id},
        )
    ).mappings().all()
    doc_ids = [row["id"] for row in doc_rows]
    storage = [
        {"key": row["storage_key"], "backend": row["storage_backend"]}
        for row in doc_rows
        if row["storage_key"]
    ]

    counts: dict = {
        "documents": 0,
        "document_tags": 0,
        "storage": storage,
        "soft_deleted": {},
        "deleted": {},
    }

    if doc_ids:
        result = await session.execute(
            text("""
                DELETE FROM knowledge_base.document_tags
                 WHERE document_id = ANY(:doc_ids)
            """),
            {"doc_ids": doc_ids},
        )
        counts["document_tags"] = result.rowcount or 0

    result = await session.execute(
        text("""
            UPDATE knowledge_base.knowledge_documents
               SET is_deleted = 1,
                   status = 'deleted',
                   archived_at = NULL,
                   updated_at = now()
             WHERE tenant_id = :tenant_id
               AND knowledge_base_id = :kb_id
               AND is_deleted = 0
        """),
        {"tenant_id": tenant_id, "kb_id": kb_id},
    )
    counts["documents"] = result.rowcount or 0

    for table in _SOFT_DELETE_TABLES:
        result = await session.execute(
            text(f"""
                UPDATE knowledge_base.{table}
                   SET is_deleted = 1, updated_at = now()
                 WHERE tenant_id = :tenant_id
                   AND knowledge_base_id = :kb_id
                   AND is_deleted = 0
            """),
            {"tenant_id": tenant_id, "kb_id": kb_id},
        )
        counts["soft_deleted"][table] = result.rowcount or 0

    for table in _HARD_DELETE_TABLES:
        result = await session.execute(
            text(f"""
                DELETE FROM knowledge_base.{table}
                 WHERE tenant_id = :tenant_id AND knowledge_base_id = :kb_id
            """),
            {"tenant_id": tenant_id, "kb_id": kb_id},
        )
        counts["deleted"][table] = result.rowcount or 0

    return counts


class KnowledgeInfoRepository(BaseRepository[KnowledgeInfo]):
    model = KnowledgeInfo