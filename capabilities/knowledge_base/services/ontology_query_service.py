"""Ontology query service: kb statistics, instance list, relation list."""

import asyncio
import logging
import os
from typing import Optional

from jonex_core.common.audit import schedule_emit
from jonex_core.common.audit_enums import ResourceType
from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import InvalidParameterError
from jonex_core.common.i18n import translate
from jonex_core.common.neo4j_client import get_neo4j_driver, is_neo4j_unavailable

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
from .document_service import _audit_user_id, _payload
from .reconciliation_service import _stub_ratio_alert  # [jonex] 改动 52（S2）

logger = logging.getLogger(__name__)

# 更新实例时允许出现的键。超出白名单一律按参数错误拒绝，不静默忽略。
_ALLOWED_INSTANCE_UPDATE_KEYS = frozenset(
    {"entity_type", "name", "aliases", "description", "attributes"}
)

# 实体类型长度上限（与契约一致）
_MAX_ENTITY_TYPE_LENGTH = 128

def _emit_ontology_audit(
    action: str,
    *,
    tenant_id: str,
    user_id: Optional[str],
    resource: str,
    resource_id: str,
    knowledge_base_id: str,
    changed_fields: list,
    entity_type_from: Optional[str] = None,
    entity_type_to: Optional[str] = None,
) -> None:
    """写出本体写动作的审计条目。

    审计是 best-effort：任何失败只记日志，不影响已经完成的业务写入。
    `request_params` 只记录定位键与变更字段名，不写入描述、属性等字段的值。
    """
    try:
        request_params = {
            "knowledge_base_id": knowledge_base_id,
            "changed_fields": list(changed_fields),
        }
        if entity_type_from is not None and entity_type_to is not None:
            request_params["entity_type_from"] = entity_type_from
            request_params["entity_type_to"] = entity_type_to
        schedule_emit({
            "tenant_id": tenant_id,
            "user_id": _audit_user_id(user_id),
            "log_type": "OPERATION",
            "action": action,
            "outcome": "SUCCESS",
            "service_name": "knowledge_base",
            "resource": resource,
            "resource_id": resource_id,
            "request_params": request_params,
        })
    except Exception:
        logger.warning("ontology audit emit failed: action=%s", action, exc_info=True)


def _is_neo4j_down(exc: BaseException) -> bool:
    """异常（含 cause/context 链）是否表示「Neo4j 不可用」。

    neo4j 驱动对不同故障形态抛的异常并不统一：

    - 进程在、端口拒绝/超时 → ``ServiceUnavailable`` 等 DriverError；
    - 存储层 critical（如事务无法启动）→ ``DatabaseError``；
    - 容器停止、主机名从网络消失 → ``ValueError("Cannot resolve address ...")``，
      ``__cause__`` 为 ``socket.gaierror``。

    具体判据统一收在内核 ``is_neo4j_unavailable``：黑名单式，只把基础设施故障
    （连接层 / 存储层 critical / 认证失效 / DNS）当作不可用，语法错误、约束
    冲突、权限拒绝这类调用方自己能修的错误仍上抛 500。
    """
    return is_neo4j_unavailable(exc)


_DEGRADED_REASON = "图数据库（Neo4j）暂不可用，知识图谱无法展示，基础知识库能力不受影响"

# 多跳（D11）：单次执行超时与降级文案。上限复用问答链路的同名 env，
# 但在此独立读取，避免 import search_service 把两条链路耦合起来。
_MULTIHOP_TIMEOUT_REASON = "多跳邻居展开超时，已返回空结果（可缩小 depth 或 limit 后重试）"


def _multihop_depth_max() -> int:
    """同 KB 多跳深度上限；非法值回落 3。"""
    try:
        value = int(os.getenv("ONTOLOGY_NEIGHBOR_DEPTH_MAX", "3"))
    except (TypeError, ValueError):
        return 3
    return value if value >= 1 else 3


def _multihop_timeout() -> float:
    """单次多跳执行超时（秒）；非法值回落 10。"""
    try:
        value = float(os.getenv("ONTOLOGY_COLUMN_BROWSE_TIMEOUT", "10"))
    except (TypeError, ValueError):
        return 10.0
    return value if value > 0 else 10.0


def _multihop_degraded(req: "OntologyNeighborRequest", reason: str) -> dict:
    """多跳降级出口：空图 + 契约字段齐全（depth/limit/returned/truncated）。"""
    return {
        "nodes": [],
        "edges": [],
        "degraded": True,
        "degraded_reason": reason,
        "depth": req.depth,
        "limit": req.limit,
        "returned": 0,
        "truncated": False,
    }


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
        except Exception as e:
            if not _is_neo4j_down(e):
                raise
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
        """分页查询 kb 内本体实例列表。Neo4j 不可用时降级为空页，不抛 500。"""
        tenant_id = require_tenant(tenant_id)
        req = OntologyInstanceListRequest(**_payload(request))
        offset = (req.page - 1) * req.page_size
        gdao = OntologyGraphRepository(get_neo4j_driver())
        entity_types = None
        entity_type = req.entity_type
        if entity_type:
            from .ontology_reasoning.expand import (
                expand_entity_types, hierarchy_expansion_enabled,
            )
            if hierarchy_expansion_enabled():
                try:
                    from .ontology_reasoning.context import resolve_reasoning_context
                    from .ontology_reasoning.view import build_reasoning_view
                    ctx = await resolve_reasoning_context(
                        tenant_id, req.knowledge_base_id,
                    )
                    view = build_reasoning_view(ctx)
                    key = ctx.schema_context.resolve_entity(entity_type)
                    canonical = (
                        ctx.schema_context.entities.get(key) or {}
                    ).get("name") or entity_type
                    entity_types = expand_entity_types(view, [canonical]).values
                    entity_type = None
                except Exception:  # noqa: BLE001 — 扩展失败退回单类型
                    entity_types = None
        try:
            items, total = await gdao.list_entities(
                tenant_id,
                req.knowledge_base_id,
                offset,
                req.page_size,
                entity_type=entity_type,
                entity_types=entity_types,
                keyword=req.keyword,
                include_unknown=req.include_unknown,
                document_id=req.document_id,
            )
        except Exception as e:
            if not _is_neo4j_down(e):
                raise
            logger.warning(
                "[ontology] 实例列表降级：Neo4j 不可用 kb=%s err=%s",
                req.knowledge_base_id, e,
            )
            return {
                "items": [], "total": 0,
                "page": req.page, "page_size": req.page_size,
                "degraded": True, "degraded_reason": _DEGRADED_REASON,
            }
        return {
            "items": items,
            "total": total,
            "page": req.page,
            "page_size": req.page_size,
        }

    async def list_relations(self, tenant_id: str, request) -> dict:
        """分页查询 kb 内本体关系列表。Neo4j 不可用时降级为空页，不抛 500。"""
        tenant_id = require_tenant(tenant_id)
        req = OntologyRelationListRequest(**_payload(request))
        offset = (req.page - 1) * req.page_size
        gdao = OntologyGraphRepository(get_neo4j_driver())
        relation_types = None
        relation_type = req.relation_type
        if relation_type:
            from .ontology_reasoning.expand import (
                expand_relation_types, hierarchy_expansion_enabled,
            )
            if hierarchy_expansion_enabled():
                try:
                    from .ontology_reasoning.context import resolve_reasoning_context
                    from .ontology_reasoning.view import build_reasoning_view
                    ctx = await resolve_reasoning_context(
                        tenant_id, req.knowledge_base_id,
                    )
                    view = build_reasoning_view(ctx)
                    key = ctx.schema_context.resolve_relation(relation_type)
                    canonical = (
                        ctx.schema_context.relations.get(key) or {}
                    ).get("name") or relation_type
                    relation_types = expand_relation_types(view, [canonical]).values
                    relation_type = None
                except Exception:  # noqa: BLE001
                    relation_types = None
        try:
            items, total = await gdao.list_relations(
                tenant_id,
                req.knowledge_base_id,
                offset,
                req.page_size,
                relation_type=relation_type,
                relation_types=relation_types,
                source_name=req.source_name,
                target_name=req.target_name,
                source_type=req.source_type,
                target_type=req.target_type,
                keyword=req.keyword,
                document_id=req.document_id,
            )
        except Exception as e:
            if not _is_neo4j_down(e):
                raise
            logger.warning(
                "[ontology] 关系列表降级：Neo4j 不可用 kb=%s err=%s",
                req.knowledge_base_id, e,
            )
            return {
                "items": [], "total": 0,
                "page": req.page, "page_size": req.page_size,
                "degraded": True, "degraded_reason": _DEGRADED_REASON,
            }
        return {
            "items": items,
            "total": total,
            "page": req.page,
            "page_size": req.page_size,
        }

    async def list_entity_types(self, tenant_id: str, request) -> dict:
        """实体类型列表 = compiled schema 类型定义 × Neo4j 实例数聚合。

        Neo4j 不可用时仍返回 compiled schema 里的类型定义（实例计数置 0、
        ``build_status="empty"``）并标记 degraded；schema 自身读取失败时
        返回空列表 + degraded，均不抛 500。
        """
        tenant_id = require_tenant(tenant_id)
        req = OntologyStatsRequest(**_payload(request))
        kb_id = req.knowledge_base_id

        from .ontology_compiler import OntologyCompiler

        try:
            schema = await OntologyCompiler().get_compiled_schema(tenant_id, kb_id) or {}
        except Exception as e:
            if not _is_neo4j_down(e):
                raise
            logger.warning(
                "[ontology] 实体类型列表降级：compiled schema 读取失败 kb=%s err=%s",
                kb_id, e,
            )
            return {
                "items": [], "total": 0,
                "degraded": True, "degraded_reason": _DEGRADED_REASON,
            }

        gdao = OntologyGraphRepository(get_neo4j_driver())
        try:
            counts = (
                await gdao._count_entities_by_type_doc(tenant_id, kb_id, req.document_id)
                if req.document_id
                else await gdao.count_entities_by_type(tenant_id, kb_id)
            )
            degraded = False
        except Exception as e:
            if not _is_neo4j_down(e):
                raise
            logger.warning(
                "[ontology] 实体类型列表降级：Neo4j 不可用 kb=%s err=%s", kb_id, e,
            )
            counts = {}
            degraded = True

        if not req.include_unknown:
            counts.pop("unknown", None)

        items = []
        known = set()
        for et in schema.get("entity_types", []):
            name = et["name"]
            known.add(name)
            cnt = counts.get(name, 0)
            # 降级时计数不可信，文档级过滤会把类型目录清空，故跳过该过滤
            if not degraded and req.document_id and cnt == 0:
                continue
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
        result = {"items": items, "total": len(items)}
        if degraded:
            result["degraded"] = True
            result["degraded_reason"] = _DEGRADED_REASON
        return result

    async def get_kb_graph(self, tenant_id: str, request) -> dict:
        """获取 KB 图谱数据（nodes + edges + 统计），用于前端力导向图渲染。

        Neo4j 不可用时优雅降级：返回空图 + degraded 标记，不抛 500。
        """
        tenant_id = require_tenant(tenant_id)
        req = OntologyGraphRequest(**_payload(request))
        gdao = OntologyGraphRepository(get_neo4j_driver())
        entity_types = req.entity_types
        if entity_types:
            from .ontology_reasoning.expand import (
                expand_entity_types, hierarchy_expansion_enabled,
            )
            if hierarchy_expansion_enabled():
                try:
                    from .ontology_reasoning.context import resolve_reasoning_context
                    from .ontology_reasoning.view import build_reasoning_view
                    ctx = await resolve_reasoning_context(
                        tenant_id, req.knowledge_base_id,
                    )
                    view = build_reasoning_view(ctx)
                    canonical = []
                    for name in entity_types:
                        key = ctx.schema_context.resolve_entity(name)
                        canonical.append(
                            (ctx.schema_context.entities.get(key) or {}).get("name") or name
                        )
                    entity_types = expand_entity_types(view, canonical).values
                except Exception:  # noqa: BLE001
                    entity_types = req.entity_types
        try:
            result = await gdao.get_kb_graph(
                tenant_id,
                req.knowledge_base_id,
                limit=req.limit,
                entity_types=entity_types,
                document_id=req.document_id,
            )
            result["degraded"] = False
            return result
        except Exception as e:
            if not _is_neo4j_down(e):
                raise
            logger.warning("[ontology] 图谱查询降级：Neo4j 不可用 kb=%s err=%s", req.knowledge_base_id, e)
            return {
                "nodes": [], "edges": [],
                "total_nodes": 0, "total_relations": 0,
                "type_counts": {}, "returned_nodes": 0, "returned_edges": 0,
                "truncated": False, "limit": req.limit,
                "degraded": True, "degraded_reason": _DEGRADED_REASON,
            }

    async def expand_ontology_neighbors(self, tenant_id: str, request) -> dict:
        """展开某实体的邻居（nodes + edges），用于前端双击节点增量扩展。

        ``depth = 1`` 走既有单跳链路，响应逐字节不变；``depth > 1`` 走三元组锚定
        的多跳查询，独立 10s 超时，超时/Neo4j 不可用都降级为空增量（不抛 500）。
        """
        tenant_id = require_tenant(tenant_id)
        req = OntologyNeighborRequest(**_payload(request))
        depth_max = _multihop_depth_max()
        if req.depth < 1 or req.depth > depth_max:
            raise InvalidParameterError(f"depth 必须在 1-{depth_max} 之间")
        gdao = OntologyGraphRepository(get_neo4j_driver())

        if req.depth > 1:
            # document_id 只对单跳有明确语义；组合会产生「过滤哪一跳」歧义。
            if req.document_id:
                raise InvalidParameterError("document_id 与 depth>1 不能同时使用")
            try:
                # 先于降级判据捕获：TimeoutError 也是 OSError 子类。
                raw = await asyncio.wait_for(
                    gdao.expand_neighbors_multihop(
                        tenant_id,
                        req.knowledge_base_id,
                        req.entity_type,
                        req.canonical_name,
                        depth=req.depth,
                        limit=req.limit,
                    ),
                    timeout=_multihop_timeout(),
                )
            except asyncio.TimeoutError as e:
                logger.warning(
                    "[ontology] 多跳邻居展开超时：kb=%s depth=%s err=%s",
                    req.knowledge_base_id, req.depth, e,
                )
                return _multihop_degraded(req, _MULTIHOP_TIMEOUT_REASON)
            except Exception as e:
                if not _is_neo4j_down(e):
                    raise
                logger.warning(
                    "[ontology] 多跳邻居展开降级：Neo4j 不可用 kb=%s err=%s",
                    req.knowledge_base_id, e,
                )
                return _multihop_degraded(req, _DEGRADED_REASON)

            # 仓储内部字段不外泄；对外只给契约字段（顺序固定）。
            edges = raw.get("edges") or []
            return {
                "nodes": raw.get("nodes") or [],
                "edges": edges,
                "degraded": False,
                "depth": req.depth,
                "limit": req.limit,
                "returned": raw.get("returned_edges", len(edges)),
                "truncated": bool(raw.get("truncated", False)),
            }

        try:
            result = await gdao.expand_neighbors(
                tenant_id,
                req.knowledge_base_id,
                req.entity_type,
                req.canonical_name,
                limit=req.limit,
                document_id=req.document_id,
            )
            result["degraded"] = False
            return result
        except Exception as e:
            if not _is_neo4j_down(e):
                raise
            logger.warning("[ontology] 邻居展开降级：Neo4j 不可用 kb=%s err=%s", req.knowledge_base_id, e)
            return {"nodes": [], "edges": [], "degraded": True, "degraded_reason": _DEGRADED_REASON}

    async def list_relation_types(self, tenant_id: str, request) -> dict:
        """关系类型列表 = compiled schema 关系定义 × Neo4j 关系数聚合。

        Neo4j 不可用时仍返回 compiled schema 里的关系定义（实例计数置 0、
        ``build_status="empty"``）并标记 degraded；schema 自身读取失败时
        返回空列表 + degraded，均不抛 500。
        """
        tenant_id = require_tenant(tenant_id)
        req = OntologyStatsRequest(**_payload(request))
        kb_id = req.knowledge_base_id

        from .ontology_compiler import OntologyCompiler

        try:
            schema = await OntologyCompiler().get_compiled_schema(tenant_id, kb_id) or {}
        except Exception as e:
            if not _is_neo4j_down(e):
                raise
            logger.warning(
                "[ontology] 关系类型列表降级：compiled schema 读取失败 kb=%s err=%s",
                kb_id, e,
            )
            return {
                "items": [], "total": 0,
                "degraded": True, "degraded_reason": _DEGRADED_REASON,
            }

        gdao = OntologyGraphRepository(get_neo4j_driver())
        try:
            counts = (
                await gdao.count_relations_by_type_doc(tenant_id, kb_id, req.document_id)
                if req.document_id
                else await gdao.count_relations_by_type(tenant_id, kb_id)
            )
            degraded = False
        except Exception as e:
            if not _is_neo4j_down(e):
                raise
            logger.warning(
                "[ontology] 关系类型列表降级：Neo4j 不可用 kb=%s err=%s", kb_id, e,
            )
            counts = {}
            degraded = True

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
            # 降级时计数不可信，文档级过滤会把类型目录清空，故跳过该过滤
            if not degraded and req.document_id and cnt == 0:
                continue
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
        result = {"items": items, "total": len(items)}
        if degraded:
            result["degraded"] = True
            result["degraded_reason"] = _DEGRADED_REASON
        return result

    # ══════════════════════════════════════════════════════════════════
    # 本体实例/关系 创建、编辑与删除
    # ══════════════════════════════════════════════════════════════════

    async def create_instance(
        self,
        tenant_id: str,
        req: CreateOntologyInstanceRequest,
        user_id: Optional[str] = None,
    ) -> dict:
        """创建本体实例（实体节点）。

        `req.document_id` 有值（从文档结果页创建）→ 写进 `doc_ids`，该文档的本体视图可见；
        为空（KB 级编译资产页创建）→ `doc_ids=[]`，属于全库实例。
        """
        tenant_id = require_tenant(tenant_id)
        gdao = OntologyGraphRepository(get_neo4j_driver())
        result = await gdao.create_entity(
            tenant_id,
            kb_id=req.knowledge_base_id,
            entity_type=req.entity_type,
            canonical_name=req.name,
            aliases=req.aliases,
            description=req.description or "",
            attributes=req.attributes,
            doc_id=req.document_id,
        )
        changed_fields = ["entity_type", "name"]
        if req.aliases is not None:
            changed_fields.append("aliases")
        if req.description is not None:
            changed_fields.append("description")
        if req.attributes is not None:
            changed_fields.append("attributes")
        _emit_ontology_audit(
            "create_ontology_instance",
            tenant_id=tenant_id,
            user_id=user_id,
            resource=ResourceType.ONTOLOGY_INSTANCE.value,
            resource_id=f"{req.entity_type}/{req.name}",
            knowledge_base_id=req.knowledge_base_id,
            changed_fields=changed_fields,
        )
        return result

    async def create_relation(
        self,
        tenant_id: str,
        req: CreateOntologyRelationRequest,
        user_id: Optional[str] = None,
    ) -> dict:
        """创建本体关系（实体间关系边）。`req.document_id` 口径同 `create_instance`。"""
        tenant_id = require_tenant(tenant_id)
        gdao = OntologyGraphRepository(get_neo4j_driver())
        result = await gdao.create_relation(
            tenant_id,
            kb_id=req.knowledge_base_id,
            source_entity_type=req.source_entity_type,
            source_canonical_name=req.source_canonical_name,
            relation_type=req.relation_type,
            target_entity_type=req.target_entity_type,
            target_canonical_name=req.target_canonical_name,
            attributes=req.attributes,
            doc_id=req.document_id,
        )
        changed_fields = ["relation_type"]
        if req.attributes is not None:
            changed_fields.append("attributes")
        _emit_ontology_audit(
            "create_ontology_relation",
            tenant_id=tenant_id,
            user_id=user_id,
            resource=ResourceType.ONTOLOGY_RELATION.value,
            resource_id=(
                f"{req.source_entity_type}/{req.source_canonical_name}"
                f":{req.relation_type}:"
                f"{req.target_entity_type}/{req.target_canonical_name}"
            ),
            knowledge_base_id=req.knowledge_base_id,
            changed_fields=changed_fields,
        )
        return result

    async def update_instance(
        self,
        tenant_id: str,
        req: UpdateOntologyInstanceRequest,
        user_id: Optional[str] = None,
    ) -> dict:
        """更新本体实例（实体）的字段。

        `updates` 只接受白名单内的键；其中 `entity_type` 允许原地修改实体类型，
        仓储层在目标键已存在时完成撞名合并，并通过返回值里的 `merged` 告知调用方。
        """
        tenant_id = require_tenant(tenant_id)
        if not (req.canonical_name or "").strip():
            raise InvalidParameterError(
                message=translate("err.ontology.empty_canonical_name")
            )

        unknown_keys = sorted(set(req.updates) - _ALLOWED_INSTANCE_UPDATE_KEYS)
        if unknown_keys:
            raise InvalidParameterError(
                message=translate(
                    "err.ontology.unknown_update_field",
                    params={"fields": ", ".join(unknown_keys)},
                )
            )

        updates = dict(req.updates)
        new_type: Optional[str] = None
        if "entity_type" in updates:
            raw_type = updates["entity_type"]
            if (
                not isinstance(raw_type, str)
                or not raw_type.strip()
                or len(raw_type.strip()) > _MAX_ENTITY_TYPE_LENGTH
            ):
                raise InvalidParameterError(
                    message=translate("err.ontology.invalid_entity_type")
                )
            new_type = raw_type.strip()
            updates["entity_type"] = new_type
            if new_type != req.entity_type:
                await self._warn_type_outside_schema(
                    tenant_id, req.knowledge_base_id, req.entity_type, new_type
                )

        gdao = OntologyGraphRepository(get_neo4j_driver())
        result = await gdao.update_entity(
            tenant_id,
            req.knowledge_base_id,
            req.entity_type,
            req.canonical_name,
            updates,
            merge_target=req.merge_target,
        )

        final_type = new_type or req.entity_type
        final_name = updates.get("name", req.canonical_name)
        type_changed = new_type is not None and new_type != req.entity_type
        _emit_ontology_audit(
            "update_ontology_instance",
            tenant_id=tenant_id,
            user_id=user_id,
            resource=ResourceType.ONTOLOGY_INSTANCE.value,
            resource_id=f"{final_type}/{final_name}",
            knowledge_base_id=req.knowledge_base_id,
            changed_fields=sorted(updates.keys()),
            entity_type_from=req.entity_type if type_changed else None,
            entity_type_to=new_type if type_changed else None,
        )

        merged = bool(result.get("merged")) if isinstance(result, dict) else False
        return {"updated": True, "merged": merged}

    async def _warn_type_outside_schema(
        self, tenant_id: str, kb_id: str, old_type: str, new_type: str
    ) -> None:
        """目标类型不在该知识库编译模式中时记录警告，不拒绝请求。

        编译模式取不到（不存在或查询失败）时无法判断，跳过警告，不影响写入。
        """
        try:
            from .ontology_compiler import OntologyCompiler

            schema = await OntologyCompiler().get_compiled_schema(
                tenant_id, kb_id, auto_compile=False
            ) or {}
            defined_types = {
                et.get("name")
                for et in (schema.get("entity_types") or [])
                if isinstance(et, dict) and et.get("name")
            }
            if defined_types and new_type not in defined_types:
                logger.warning(
                    "ontology entity type outside compiled schema: "
                    "tenant_id=%s kb_id=%s old_type=%s new_type=%s",
                    tenant_id, kb_id, old_type, new_type,
                )
        except Exception as exc:
            logger.warning(
                "compiled schema lookup failed while checking entity type: "
                "tenant_id=%s kb_id=%s error=%s", tenant_id, kb_id, exc,
            )

    async def delete_instance(
        self,
        tenant_id: str,
        req: DeleteOntologyInstanceRequest,
        user_id: Optional[str] = None,
    ) -> dict:
        """删除本体实例（实体）及其关联关系。"""
        tenant_id = require_tenant(tenant_id)
        gdao = OntologyGraphRepository(get_neo4j_driver())
        await gdao.delete_entity(
            tenant_id,
            req.knowledge_base_id,
            req.entity_type,
            req.canonical_name,
        )
        _emit_ontology_audit(
            "delete_ontology_instance",
            tenant_id=tenant_id,
            user_id=user_id,
            resource=ResourceType.ONTOLOGY_INSTANCE.value,
            resource_id=f"{req.entity_type}/{req.canonical_name}",
            knowledge_base_id=req.knowledge_base_id,
            changed_fields=[],
        )
        return {"deleted": True}

    async def search_entities(self, tenant_id: str, req: OntologyEntitySearchRequest) -> dict:
        """模糊搜索本体实例（用于表单字段的搜索选择）。

        有关键词时：委托 OntologyGraphRepository.search_entities 使用 Neo4j 全文索引
        ont_entity_ft（cjk analyzer）实现中文模糊匹配。

        无关键词时：回退到 list_entities 返回前 limit 条，确保搜索选择器初始有数据。

        `req.document_id` 有值时两条分支都只在**该文档关联的实体**里选（判据
        `$doc_id IN e.doc_ids`，与文档本体视图一致）；为空时保持全库候选。

        Returns:
            dict: {"items": list[dict]} — items 为匹配/默认的实体列表

        Neo4j 不可用时降级为空候选 + degraded 标记，不抛 500。
        """
        tenant_id = require_tenant(tenant_id)
        gdao = OntologyGraphRepository(get_neo4j_driver())
        try:
            if not req.keyword.strip():
                items, _ = await gdao.list_entities(
                    tenant_id,
                    kb_id=req.knowledge_base_id,
                    offset=0,
                    limit=req.limit,
                    document_id=req.document_id,
                )
                return {"items": items}
            items = await gdao.search_entities(
                tenant_id,
                kb_ids=[req.knowledge_base_id],
                query=req.keyword,
                limit=req.limit,
                doc_id=req.document_id,
            )
            return {"items": items}
        except Exception as e:
            if not _is_neo4j_down(e):
                raise
            logger.warning(
                "[ontology] 实体搜索降级：Neo4j 不可用 kb=%s err=%s",
                req.knowledge_base_id, e,
            )
            return {
                "items": [],
                "degraded": True,
                "degraded_reason": _DEGRADED_REASON,
            }

    async def update_relation(
        self,
        tenant_id: str,
        req: UpdateOntologyRelationRequest,
        user_id: Optional[str] = None,
    ) -> dict:
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
        final_relation_type = req.updates.get("relation_type", req.relation_type)
        changed_fields = sorted(
            set(req.updates) & {"relation_type", "attributes"}
        )
        _emit_ontology_audit(
            "update_ontology_relation",
            tenant_id=tenant_id,
            user_id=user_id,
            resource=ResourceType.ONTOLOGY_RELATION.value,
            resource_id=(
                f"{req.source_entity_type}/{req.source_canonical_name}"
                f":{final_relation_type}:"
                f"{req.target_entity_type}/{req.target_canonical_name}"
            ),
            knowledge_base_id=req.knowledge_base_id,
            changed_fields=changed_fields,
        )
        return {"updated": True}

    async def delete_relation(
        self,
        tenant_id: str,
        req: DeleteOntologyRelationRequest,
        user_id: Optional[str] = None,
    ) -> dict:
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
        _emit_ontology_audit(
            "delete_ontology_relation",
            tenant_id=tenant_id,
            user_id=user_id,
            resource=ResourceType.ONTOLOGY_RELATION.value,
            resource_id=(
                f"{req.source_entity_type}/{req.source_canonical_name}"
                f":{req.relation_type}:"
                f"{req.target_entity_type}/{req.target_canonical_name}"
            ),
            knowledge_base_id=req.knowledge_base_id,
            changed_fields=[],
        )
        return {"deleted": True}