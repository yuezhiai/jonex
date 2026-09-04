"""Ontology query service: kb statistics, instance list, relation list."""

import logging

from neo4j.exceptions import AuthError, ServiceUnavailable, SessionExpired, TransientError

from jonex_core.common.database import get_db_session
from jonex_core.common.neo4j_client import get_neo4j_driver

from jonex_core.common.tenant import require_tenant

from ..dtos import (
    OntologyEntitySearchRequest,
    OntologyGraphRequest,
    OntologyInstanceListRequest,
    OntologyNeighborRequest,
    OntologyRelationListRequest,
    OntologyStatsRequest,
)
from ..dtos.ontology_crud import (
    CreateOntologyInstanceRequest,
    CreateOntologyRelationRequest,
    DeleteOntologyInstanceRequest,
    DeleteOntologyRelationRequest,
    UpdateOntologyInstanceRequest,
    UpdateOntologyRelationRequest,
)
from ..models import KnowledgeDocument, KnowledgeInfo
from ..repository import KnowledgeDocumentRepository, OntologyGraphRepository
from ..repository.knowledge_info_repository import KnowledgeInfoRepository
from .document_service import _payload
from .reconciliation_service import _stub_ratio_alert  # [jonex] 改动 52（S2）

logger = logging.getLogger(__name__)

# Neo4j 不可用类异常：触发图谱查询优雅降级（不抛 500，返回空图 + degraded 标记）
_NEO4J_DOWN_ERRORS = (ServiceUnavailable, SessionExpired, TransientError, AuthError, OSError)
_DEGRADED_REASON = "图数据库（Neo4j）暂不可用，知识图谱无法展示，基础知识库能力不受影响"


class OntologyQueryService:
    """kb 维度统计 / 本体实例列表 / 本体关系列表查询服务。"""

    async def get_kb_statistics(self, tenant_id: str, request) -> dict:
        """获取 kb 维度统计：源文件数（PG）+ 本体实例/关系数（Neo4j）。

        注意：跨 PG + Neo4j 两源读取，无跨库事务，语义为最终一致。
        本体抽取走异步对账循环，PG 文档落库与 Neo4j 本体写入存在时间差，
        统计三值可能短暂不同步，属可接受 tradeoff。
        """
        tenant_id = require_tenant(tenant_id)
        req = OntologyStatsRequest(**_payload(request))
        kb_id = req.knowledge_base_id

        async with get_db_session() as session:
            doc_count = await KnowledgeDocumentRepository(session).count(
                tenant_id,
                extra_conditions=[KnowledgeDocument.knowledge_base_id == kb_id],
            )
            kb_info = await KnowledgeInfoRepository(session).get_by_id(kb_id, tenant_id)

        gdao = OntologyGraphRepository(get_neo4j_driver())
        try:
            instance_count = await gdao.count_entities(tenant_id, kb_id)
            relation_count = await gdao.count_relations(tenant_id, kb_id)
            # [jonex] 改动 52（S2）：stub 占比随统计接口暴露，KB 详情可见
            stub_stats = await gdao.stub_entity_stats(tenant_id, kb_id)
            ontology_degraded = False
        except _NEO4J_DOWN_ERRORS as e:
            # Neo4j 不可用：本体计数降级为 0，仍返回 PG 侧源文件数等基础统计
            logger.warning("[ontology] 统计降级：Neo4j 不可用 kb=%s err=%s", kb_id, e)
            instance_count = 0
            relation_count = 0
            stub_stats = {"total": 0, "stub": 0, "ratio": 0.0}
            ontology_degraded = True

        stub_alert = _stub_ratio_alert(stub_stats["total"], stub_stats["stub"])
        if stub_alert:
            logger.warning(
                "[ontology] stub 实体占比超阈值 kb=%s ratio=%.1f%% stub=%d total=%d",
                kb_id, stub_stats["ratio"] * 100,
                stub_stats["stub"], stub_stats["total"],
            )

        return {
            "knowledge_base_id": kb_id,
            "knowledge_base_name": kb_info.name if kb_info else "",
            "last_update_time": kb_info.updated_at.isoformat() if kb_info and kb_info.updated_at else None,
            "source_file_count": doc_count,
            "ontology_instance_count": instance_count,
            "ontology_relation_count": relation_count,
            "ontology_degraded": ontology_degraded,
            # [jonex] 改动 52（S2）：抽取管线质量信号（stub = 无描述且无属性的空壳）
            "stub_entity_count": stub_stats["stub"],
            "stub_entity_ratio": round(stub_stats["ratio"], 4),
            "stub_ratio_alert": stub_alert,
        }

    async def list_instances(self, tenant_id: str, request) -> dict:
        """分页查询 kb 内本体实例列表。"""
        tenant_id = require_tenant(tenant_id)
        req = OntologyInstanceListRequest(**_payload(request))
        offset = (req.page - 1) * req.page_size
        gdao = OntologyGraphRepository(get_neo4j_driver())
        items, total = await gdao.list_entities(
            tenant_id,
            req.knowledge_base_id,
            offset,
            req.page_size,
            entity_type=req.entity_type,
            keyword=req.keyword,
            include_unknown=req.include_unknown,
            document_id=req.document_id,
        )
        return {
            "items": items,
            "total": total,
            "page": req.page,
            "page_size": req.page_size,
        }

    async def list_relations(self, tenant_id: str, request) -> dict:
        """分页查询 kb 内本体关系列表。"""
        tenant_id = require_tenant(tenant_id)
        req = OntologyRelationListRequest(**_payload(request))
        offset = (req.page - 1) * req.page_size
        gdao = OntologyGraphRepository(get_neo4j_driver())
        items, total = await gdao.list_relations(
            tenant_id,
            req.knowledge_base_id,
            offset,
            req.page_size,
            relation_type=req.relation_type,
            source_name=req.source_name,
            target_name=req.target_name,
            source_type=req.source_type,
            target_type=req.target_type,
            keyword=req.keyword,
            document_id=req.document_id,
        )
        return {
            "items": items,
            "total": total,
            "page": req.page,
            "page_size": req.page_size,
        }

    async def list_entity_types(self, tenant_id: str, request) -> dict:
        """实体类型列表 = compiled schema 类型定义 × Neo4j 实例数聚合。"""
        tenant_id = require_tenant(tenant_id)
        req = OntologyStatsRequest(**_payload(request))
        kb_id = req.knowledge_base_id

        from .ontology_compiler import OntologyCompiler

        schema = await OntologyCompiler().get_compiled_schema(tenant_id, kb_id) or {}
        gdao = OntologyGraphRepository(get_neo4j_driver())
        counts = await gdao.count_entities_by_type(tenant_id, kb_id)
        if not req.include_unknown:
            counts.pop("unknown", None)

        items = []
        known = set()
        for et in schema.get("entity_types", []):
            name = et["name"]
            known.add(name)
            cnt = counts.get(name, 0)
            items.append({
                "name": name,
                "display_name": et.get("display_name") or name,
                "description": et.get("description", ""),
                "status": et.get("status", "active"),
                "build_status": "built" if cnt > 0 else "empty",
                "instance_count": cnt,
                "attributes": et.get("attributes", []),
                "source_object_id": et.get("source_object_id"),
            })
        # schema 未定义但 Neo4j 已存在的游离类型
        for t, c in counts.items():
            if t not in known:
                items.append({
                    "name": t, "display_name": t, "description": "",
                    "status": "unschematized", "build_status": "built",
                    "instance_count": c, "attributes": [], "source_object_id": None,
                })
        return {"items": items, "total": len(items)}

    async def get_kb_graph(self, tenant_id: str, request) -> dict:
        """获取 KB 图谱数据（nodes + edges + 统计），用于前端力导向图渲染。

        Neo4j 不可用时优雅降级：返回空图 + degraded 标记，不抛 500。
        """
        tenant_id = require_tenant(tenant_id)
        req = OntologyGraphRequest(**_payload(request))
        gdao = OntologyGraphRepository(get_neo4j_driver())
        try:
            result = await gdao.get_kb_graph(
                tenant_id,
                req.knowledge_base_id,
                limit=req.limit,
                entity_types=req.entity_types,
                document_id=req.document_id,
            )
            result["degraded"] = False
            return result
        except _NEO4J_DOWN_ERRORS as e:
            logger.warning("[ontology] 图谱查询降级：Neo4j 不可用 kb=%s err=%s", req.knowledge_base_id, e)
            return {
                "nodes": [], "edges": [],
                "total_nodes": 0, "total_relations": 0,
                "type_counts": {}, "returned_nodes": 0, "returned_edges": 0,
                "truncated": False, "limit": req.limit,
                "degraded": True, "degraded_reason": _DEGRADED_REASON,
            }

    async def expand_ontology_neighbors(self, tenant_id: str, request) -> dict:
        """展开某实体的一跳邻居（nodes + edges），用于前端双击节点增量扩展。

        Neo4j 不可用时优雅降级：返回空增量 + degraded 标记，不抛 500。
        """
        tenant_id = require_tenant(tenant_id)
        req = OntologyNeighborRequest(**_payload(request))
        gdao = OntologyGraphRepository(get_neo4j_driver())
        try:
            result = await gdao.expand_neighbors(
                tenant_id,
                req.knowledge_base_id,
                req.entity_type,
                req.canonical_name,
                limit=req.limit,
            )
            result["degraded"] = False
            return result
        except _NEO4J_DOWN_ERRORS as e:
            logger.warning("[ontology] 邻居展开降级：Neo4j 不可用 kb=%s err=%s", req.knowledge_base_id, e)
            return {"nodes": [], "edges": [], "degraded": True, "degraded_reason": _DEGRADED_REASON}

    async def list_relation_types(self, tenant_id: str, request) -> dict:
        """关系类型列表 = compiled schema 关系定义 × Neo4j 关系数聚合。"""
        tenant_id = require_tenant(tenant_id)
        req = OntologyStatsRequest(**_payload(request))
        kb_id = req.knowledge_base_id

        from .ontology_compiler import OntologyCompiler

        schema = await OntologyCompiler().get_compiled_schema(tenant_id, kb_id) or {}
        gdao = OntologyGraphRepository(get_neo4j_driver())
        counts = await gdao.count_relations_by_type(tenant_id, kb_id)

        entity_display = {
            et["name"]: (et.get("display_name") or et["name"])
            for et in schema.get("entity_types", [])
        }

        items = []
        known = set()
        for rt in schema.get("relation_types", []):
            name = rt["name"]
            known.add(name)
            cnt = counts.get(name, 0)
            src, tgt = rt.get("source"), rt.get("target")
            items.append({
                "name": name,
                "display_name": rt.get("display_name") or name,
                "description": rt.get("description", ""),
                "source": src,
                "target": tgt,
                "source_display_name": entity_display.get(src, src),
                "target_display_name": entity_display.get(tgt, tgt),
                "cardinality": rt.get("cardinality", "custom"),
                "status": rt.get("status", "active"),
                "build_status": "built" if cnt > 0 else "empty",
                "instance_count": cnt,
                "source_relation_id": rt.get("source_relation_id"),
            })
        for t, c in counts.items():
            if t not in known:
                items.append({
                    "name": t, "display_name": t, "description": "",
                    "source": None, "target": None,
                    "source_display_name": None, "target_display_name": None,
                    "cardinality": "custom",
                    "status": "unschematized", "build_status": "built",
                    "instance_count": c, "source_relation_id": None,
                })
        return {"items": items, "total": len(items)}

    # ══════════════════════════════════════════════════════════════════
    # 本体实例/关系 创建、编辑与删除
    # ══════════════════════════════════════════════════════════════════

    async def create_instance(self, tenant_id: str, req: CreateOntologyInstanceRequest) -> dict:
        """创建本体实例（实体节点）。"""
        tenant_id = require_tenant(tenant_id)
        gdao = OntologyGraphRepository(get_neo4j_driver())
        return await gdao.create_entity(
            tenant_id,
            kb_id=req.knowledge_base_id,
            entity_type=req.entity_type,
            canonical_name=req.name,
            aliases=req.aliases,
            description=req.description or "",
            attributes=req.attributes,
        )

    async def create_relation(self, tenant_id: str, req: CreateOntologyRelationRequest) -> dict:
        """创建本体关系（实体间关系边）。"""
        tenant_id = require_tenant(tenant_id)
        gdao = OntologyGraphRepository(get_neo4j_driver())
        return await gdao.create_relation(
            tenant_id,
            kb_id=req.knowledge_base_id,
            source_entity_type=req.source_entity_type,
            source_canonical_name=req.source_canonical_name,
            relation_type=req.relation_type,
            target_entity_type=req.target_entity_type,
            target_canonical_name=req.target_canonical_name,
            attributes=req.attributes,
        )

    async def update_instance(self, tenant_id: str, req: UpdateOntologyInstanceRequest) -> dict:
        """更新本体实例（实体）的字段。"""
        tenant_id = require_tenant(tenant_id)
        gdao = OntologyGraphRepository(get_neo4j_driver())
        await gdao.update_entity(
            tenant_id,
            req.knowledge_base_id,
            req.entity_type,
            req.canonical_name,
            req.updates,
        )
        return {"updated": True}

    async def delete_instance(self, tenant_id: str, req: DeleteOntologyInstanceRequest) -> dict:
        """删除本体实例（实体）及其关联关系。"""
        tenant_id = require_tenant(tenant_id)
        gdao = OntologyGraphRepository(get_neo4j_driver())
        await gdao.delete_entity(
            tenant_id,
            req.knowledge_base_id,
            req.entity_type,
            req.canonical_name,
        )
        return {"deleted": True}

    async def search_entities(self, tenant_id: str, req: OntologyEntitySearchRequest) -> dict:
        """模糊搜索本体实例（用于表单字段的搜索选择）。

        有关键词时：委托 OntologyGraphRepository.search_entities 使用 Neo4j 全文索引
        ont_entity_ft（cjk analyzer）实现中文模糊匹配。

        无关键词时：回退到 list_entities 返回前 limit 条，确保搜索选择器初始有数据。

        Returns:
            dict: {"items": list[dict]} — items 为匹配/默认的实体列表
        """
        tenant_id = require_tenant(tenant_id)
        gdao = OntologyGraphRepository(get_neo4j_driver())
        if not req.keyword.strip():
            items, _ = await gdao.list_entities(
                tenant_id,
                kb_id=req.knowledge_base_id,
                offset=0,
                limit=req.limit,
            )
            return {"items": items}
        items = await gdao.search_entities(
            tenant_id,
            kb_ids=[req.knowledge_base_id],
            query=req.keyword,
            limit=req.limit,
        )
        return {"items": items}

    async def update_relation(self, tenant_id: str, req: UpdateOntologyRelationRequest) -> dict:
        """更新本体关系的类型或属性。"""
        tenant_id = require_tenant(tenant_id)
        gdao = OntologyGraphRepository(get_neo4j_driver())
        await gdao.update_relation(
            tenant_id,
            req.knowledge_base_id,
            req.source_entity_type,
            req.source_canonical_name,
            req.relation_type,
            req.target_entity_type,
            req.target_canonical_name,
            req.updates,
        )
        return {"updated": True}

    async def delete_relation(self, tenant_id: str, req: DeleteOntologyRelationRequest) -> dict:
        """删除本体关系（不删除两端实体）。"""
        tenant_id = require_tenant(tenant_id)
        gdao = OntologyGraphRepository(get_neo4j_driver())
        await gdao.delete_relation(
            tenant_id,
            req.knowledge_base_id,
            req.source_entity_type,
            req.source_canonical_name,
            req.relation_type,
            req.target_entity_type,
            req.target_canonical_name,
        )
        return {"deleted": True}