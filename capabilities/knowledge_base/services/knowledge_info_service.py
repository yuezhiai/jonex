#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""KnowledgeInfo CRUD service."""
import uuid

from sqlalchemy import select

from jonex_core.common import get_db_session
from jonex_core.common.tenant import require_tenant

from ..models.knowledge_info import KnowledgeInfo
from ..repository.knowledge_info_repository import KnowledgeInfoRepository


class KnowledgeInfoService:
    """知识库信息 CRUD"""

    async def create(self, tenant_id: str, data: dict) -> dict:
        tenant_id = require_tenant(tenant_id)
        space_id = data["space_id"]
        kb_name = data["name"]

        # [jonex] kb_type 白名单校验：裸 dict 透传无类型校验，非法值会静默降级
        from .kb_type_service import DEFAULT_KB_TYPE, VALID_KB_TYPES
        raw_kb_type = (data.get("kb_type") or "").strip() or DEFAULT_KB_TYPE
        if raw_kb_type not in VALID_KB_TYPES:
            from jonex_core.common.exceptions import InvalidParameterError
            from jonex_core.common.i18n import translate
            raise InvalidParameterError(
                message=translate(
                    "err.kb.invalid_kb_type",
                    params={"value": raw_kb_type, "valid": ", ".join(sorted(VALID_KB_TYPES))},
                    fallback=f"知识库类型不合法：{raw_kb_type}，可选值：{', '.join(sorted(VALID_KB_TYPES))}",
                ),
            )

        async with get_db_session() as session:
            repo = KnowledgeInfoRepository(session)
            obj = await repo.create(
                id=uuid.uuid4().hex,
                tenant_id=tenant_id,
                space_id=space_id,
                name=kb_name,
                description=data.get("description"),
                data_source_types=data.get("data_source_types", []),
                status=data.get("status", "synced"),
                owner_id=data.get("owner_id"),
                kb_type=raw_kb_type,  # [jonex]
            )

            # ── 自动绑定：确保该 space 下的知识库对知识检索可见 ──
            # 检查 space 下是否已有 DomainService，有则绑定，无则创建默认服务再绑定。
            from ..repository.domain_service_repository import (
                DomainServiceRepository,
                ServiceKnowledgeBaseRepository,
            )
            from ..models.domain_service import DomainService

            ds_repo = DomainServiceRepository(session)
            svc_kb_repo = ServiceKnowledgeBaseRepository(session)

            existing = await ds_repo.list_all(
                tenant_id, 0, 1,
                extra_conditions=[DomainService.space_id == space_id],
            )

            if existing:
                service = existing[0]
            else:
                service = await ds_repo.create(
                    id=uuid.uuid4().hex,
                    tenant_id=tenant_id,
                    space_id=space_id,
                    name=kb_name,
                    description=f"Auto-created for knowledge base: {kb_name}",
                )

            await svc_kb_repo.create(
                id=uuid.uuid4().hex,
                tenant_id=tenant_id,
                service_id=service.id,
                kb_id=obj.id,
            )

            await session.commit()
            return obj.to_dict()

    async def get(self, kb_id: str, tenant_id: str) -> dict:
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            from ..models.space import Space

            repo = KnowledgeInfoRepository(session)
            obj = await repo.get_required(kb_id, tenant_id)

            space_name = ""
            space_row = await session.execute(
                select(Space.name).where(
                    Space.id == obj.space_id,
                    Space.tenant_id == tenant_id,
                    Space.is_deleted == 0,
                )
            )
            result = space_row.scalar()
            if result:
                space_name = result

            # document_count 按文档表实时统计
            from ..repository.document_repository import KnowledgeDocumentRepository
            doc_count_map = await KnowledgeDocumentRepository(session).count_by_knowledge_bases(
                tenant_id, [obj.id]
            )
            data = obj.to_dict(space_name=space_name)
            data["document_count"] = doc_count_map.get(obj.id, 0)

        # [jonex] openkb 管线：本体统计走 OpenKB Wiki（Neo4j 里恒空，读了会误报 0）
        if data.get("kb_type") == "openkb":
            from .openkb_service import KnowledgeCompilerService
            try:
                graph = await KnowledgeCompilerService().get_graph(
                    kb_name=kb_id, tenant_id=tenant_id, kb_id=kb_id,
                ) or {}
                data["entity_count"] = len(graph.get("entities") or [])
                data["relation_count"] = len(graph.get("relationships") or [])
                data["ontology_degraded"] = False
            except Exception:  # noqa: BLE001
                data["entity_count"] = 0
                data["relation_count"] = 0
                data["ontology_degraded"] = True
            return data

        # 集成本体 kb 维度统计：本体实例数 / 关系数 / 降级标记。
        # get_kb_statistics 自带 Neo4j 优雅降级（不可用时计数为 0 + degraded=True）；
        # 这里再包一层防御，确保任何统计异常都不阻塞知识库详情主体返回。
        from .ontology_query_service import OntologyQueryService
        try:
            stats = await OntologyQueryService().get_kb_statistics(
                tenant_id, {"knowledge_base_id": kb_id}
            )
            data["entity_count"] = stats.get("ontology_instance_count", 0)
            data["relation_count"] = stats.get("ontology_relation_count", 0)
            data["ontology_degraded"] = stats.get("ontology_degraded", False)
        except Exception:  # noqa: BLE001
            data["entity_count"] = 0
            data["relation_count"] = 0
            data["ontology_degraded"] = True

        return data

    async def list(self, tenant_id: str, space_id: str = None, status: str = None,
                   keyword: str = None, offset: int = 0, limit: int = 20) -> dict:
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            from ..models.space import Space

            repo = KnowledgeInfoRepository(session)
            conditions = []
            if space_id:
                conditions.append(KnowledgeInfo.space_id == space_id)
            if status:
                conditions.append(KnowledgeInfo.status == status)

            items = await repo.list_all(tenant_id, offset, limit, extra_conditions=conditions)
            total = await repo.count(tenant_id, extra_conditions=conditions)

            # document_count 按文档表实时统计（冗余 document_count 列会漂移，不作准）
            from ..repository.document_repository import KnowledgeDocumentRepository
            doc_count_map = await KnowledgeDocumentRepository(session).count_by_knowledge_bases(
                tenant_id, [o.id for o in items]
            )

            # Batch fetch space names
            space_ids = list({o.space_id for o in items})
            space_name_map: dict[str, str] = {}
            if space_ids:
                space_rows = await session.execute(
                    select(Space.id, Space.name).where(
                        Space.id.in_(space_ids),
                        Space.tenant_id == tenant_id,
                        Space.is_deleted == 0,
                    )
                )
                space_name_map = {row[0]: row[1] for row in space_rows.all()}

            result_items = []
            for o in items:
                d = o.to_dict(space_name=space_name_map.get(o.space_id, ""))
                d["document_count"] = doc_count_map.get(o.id, 0)
                result_items.append(d)
            if keyword:
                kw = keyword.lower()
                result_items = [
                    i for i in result_items
                    if kw in (i.get("name") or "").lower()
                    or kw in (i.get("description") or "").lower()
                ]

            return {
                "items": result_items,
                "total": len(result_items) if keyword else total,
                "offset": offset, "limit": limit,
            }

    async def update(self, kb_id: str, tenant_id: str, data: dict) -> dict:
        tenant_id = require_tenant(tenant_id)
        # [jonex] kb_type 创建后不可变：改类型意味着旧数据全部失效（openkb→lightrag
        # 要清空 Wiki 重推 LightRAG；lightrag→openkb 要清空 Neo4j 重编译），
        # 没有安全的原地切换路径。如需更换请新建知识库。
        # 连旧名 pipeline_type 一起拦：防止调用方误以为旧名仍有效
        if "kb_type" in data or "pipeline_type" in data:
            from jonex_core.common.exceptions import InvalidParameterError
            from jonex_core.common.i18n import translate
            raise InvalidParameterError(
                message=translate(
                    "err.kb.kb_type_immutable",
                    fallback="知识库类型创建后不可修改，如需更换请新建知识库",
                ),
            )
        async with get_db_session() as session:
            repo = KnowledgeInfoRepository(session)
            await repo.get_required(kb_id, tenant_id)
            updatable = {"name", "description", "data_source_types", "status", "space_id", "owner_id"}
            values = {k: v for k, v in data.items() if k in updatable and v is not None}
            if values:
                obj = await repo.update(kb_id, tenant_id, **values)
                await session.commit()
                return obj.to_dict()
            obj = await repo.get_required(kb_id, tenant_id)
            return obj.to_dict()

    async def delete(self, kb_id: str, tenant_id: str) -> dict:
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = KnowledgeInfoRepository(session)
            await repo.get_required(kb_id, tenant_id)
            await repo.delete_soft(kb_id, tenant_id)

            # 级联：同事务内软删除该 KB 的所有同义词组
            from ..repository.ontology_synonym_repository import OntologySynonymRepository
            syn_repo = OntologySynonymRepository(session)
            for group in await syn_repo.list_all_by_kb(tenant_id, kb_id):
                await syn_repo.delete_soft(group)

            await session.commit()
            return {"deleted": True}