"""Knowledge Base business capability (business.knowledge_base.v1)."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from sqlalchemy.exc import DBAPIError, OperationalError

from neo4j.exceptions import AuthError as Neo4jAuthError
from neo4j.exceptions import ServiceUnavailable as Neo4jServiceUnavailable

from jonex_core.capability import BaseCapability
from jonex_core.capability.models import (
    CapabilityMetadata,
    CapabilityRequest,
    CapabilityResponse,
    CapabilityType,
)
from jonex_core.common.exceptions import InvalidParameterError, TenantIsolationError, JonexException
from jonex_core.common.neo4j_client import close_neo4j_driver, ensure_ontology_schema
from jonex_core.common.tenant import require_tenant

from .dtos.ontology_crud import (
    CreateOntologyInstanceRequest,
    CreateOntologyRelationRequest,
    DeleteOntologyInstanceRequest,
    DeleteOntologyRelationRequest,
    UpdateOntologyInstanceRequest,
    UpdateOntologyRelationRequest,
)
from .dtos.ontology_query import OntologyEntitySearchRequest
from .services import (
    DocumentTagService,
    DomainServiceService,
    KnowledgeBaseService,
    SpaceService,
    TagService,
)

logger = logging.getLogger(__name__)

ActionHandler = Callable[[CapabilityRequest, dict[str, Any]], Awaitable[dict]]

# ---------------------------------------------------------------------------
# 模块级常量：每个 action 的必填字段（纯静态数据，避免每次调用重建 ~90 键 dict）
# 与 _build_dispatch() 的 key 集合保持一致，由 test_capability_dispatch_consistency 验证。
# ---------------------------------------------------------------------------
_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "get_document": ("document_id", "knowledge_base_id"),
    "delete_document": ("document_id", "knowledge_base_id"),
    "upload_document": ("knowledge_base_id",),
    "list_documents": ("knowledge_base_id",),
    "documents_stats": (),
    "search": ("query",),
    "search_enhanced": ("query", "knowledge_base_id"),
    "query_with_ontology": ("query",),
    "search_llmwiki": ("query",),     # [jonex]
    "search_mix": ("query",),          # [jonex]
    "deep_query": ("query",),
    "get_search_overview": (),
    "list_search_history": (),
    "save_search_history": ("query",),
    "delete_search_history": ("history_id",),
    "clear_search_history": (),
    "get_parse_result_summary": ("knowledge_base_id",),
    "get_parse_result_documents": ("knowledge_base_id",),
    "get_parse_result_entities": ("knowledge_base_id",),
    "get_parse_result_relationships": ("knowledge_base_id",),
    "get_parse_result_graph_summary": ("knowledge_base_id",),
    "get_parse_result_graph": ("knowledge_base_id",),
    "get_document_parse_result": ("document_id", "knowledge_base_id"),
    "get_wiki_page": ("knowledge_base_id", "path"),
    "list_wiki_contents": ("knowledge_base_id",),  # document_id 不进必填（服务层可选）
    "retry_ontology_extract": ("document_id", "knowledge_base_id"),
    "get_editor_state": ("knowledge_base_id",),
    "get_compiled_schema": ("knowledge_base_id",),
    "save_compiled_schema": ("knowledge_base_id",),
    "reseed_compiled_schema": ("knowledge_base_id", "template_scenario_id"),
    "recompile_schema": ("knowledge_base_id",),
    "export_compiled_schema_yaml": ("knowledge_base_id",),
    "import_compiled_schema_yaml": ("knowledge_base_id", "yaml_text", "expected_schema_version"),
    "reextract_kb_documents": ("knowledge_base_id",),
    # ── 文件夹 ──
    "list_folders": ("knowledge_base_id",),
    "create_folder": ("knowledge_base_id", "name"),
    # ── 标签 ──
    "list_tags": ("knowledge_base_id",),
    "create_tag": ("knowledge_base_id", "name"),
    "update_tag": ("tag_id", "knowledge_base_id"),
    "delete_tag": ("tag_id", "knowledge_base_id"),
    # ── 文档-标签关联 ──
    "set_document_tags": ("document_id", "knowledge_base_id"),
    "get_document_tags": ("document_id", "knowledge_base_id"),
    "add_document_tag": ("document_id", "knowledge_base_id", "tag_id"),
    "remove_document_tag": ("document_id", "knowledge_base_id", "tag_id"),
    "rename_folder": ("folder_id", "knowledge_base_id", "name"),
    "delete_folder": ("folder_id", "knowledge_base_id"),
    # ── 本体同义词 ──
    "list_synonyms": ("knowledge_base_id",),
    "create_synonym": ("knowledge_base_id",),
    "update_synonym": ("synonym_id",),
    "delete_synonym": ("synonym_id",),
    "import_synonyms": ("knowledge_base_id",),
    # ── 知识库信息管理 ──
    "list_knowledge_info": (),
    "create_knowledge_info": (),
    "get_knowledge_info": ("kb_id",),
    "update_knowledge_info": ("kb_id",),
    "delete_knowledge_info": ("kb_id",),
    # ── 领域空间 ──
    "list_spaces": (),
    "create_space": ("name",),
    "get_space": ("space_id",),
    "update_space": ("space_id",),
    "delete_space": ("space_id",),
    "get_space_permissions": ("space_id",),
    "set_space_permissions": ("space_id",),
    # ── 领域服务 ──
    "list_services": (),
    "create_service": ("space_id", "name"),
    "get_service": ("service_id",),
    "update_service": ("service_id",),
    "delete_service": ("service_id",),
    "enable_service": ("service_id",),
    "disable_service": ("service_id",),
    "rotate_service_api_key": ("service_id",),
    "list_service_api_keys": ("service_id",),
    "create_service_api_key": ("service_id",),
    "delete_service_api_key": ("service_id", "key_id"),
    "get_service_configs": ("service_id",),
    "update_service_configs": ("service_id",),
    "get_service_permissions": ("service_id",),
    "set_service_permissions": ("service_id",),
    "search_service": ("service_id", "query"),
    "set_document_folder": ("document_id", "knowledge_base_id"),
    "generate_upload_url": ("knowledge_base_id", "file_name"),
    "get_raw_url": ("document_id",),
    "get_raw_content": ("document_id",),
    "get_raw_location": ("document_id",),
    "get_document_chunks": ("document_id",),
    "get_chunk": ("document_id", "chunk_id"),
    "reparse_document": ("document_id",),
    # ── 引用富化 ──
    "resolve_references": (),
    # ── 本体查询统计 ──
    "get_ontology_statistics": ("knowledge_base_id",),
    "get_ontology_graph": ("knowledge_base_id",),
    "expand_ontology_neighbors": ("knowledge_base_id", "entity_type", "canonical_name"),
    "list_ontology_instances": ("knowledge_base_id",),
    "list_ontology_relations": ("knowledge_base_id",),
    "list_ontology_entity_types": ("knowledge_base_id",),
    "list_ontology_relation_types": ("knowledge_base_id",),
    # ── 搜索结果反馈 ──
    "submit_search_feedback": ("session_id", "query", "feedback_type", "knowledge_base_ids"),
    "cancel_search_feedback": ("session_id", "feedback_type", "knowledge_base_ids"),
    "list_search_feedback": ("knowledge_base_id",),
    "toggle_search_feedback_adopted": ("feedback_id",),
    "get_search_feedback_stats": ("knowledge_base_id",),
    # ── 数据源接入 ──
    "list_data_sources": ("knowledge_base_id",),
    "get_data_source": ("ds_id",),
    "create_data_source": ("knowledge_base_id", "access_type", "name"),
    "update_data_source": ("ds_id",),
    "delete_data_source": ("ds_id",),
    "test_data_source": ("ds_id",),
    "sync_data_source": ("ds_id",),
    "reset_ingest_key": ("ds_id",),
    # ── 本体实例/关系 创建、编辑与删除 ──
    "create_ontology_instance": ("knowledge_base_id", "entity_type", "name"),
    "update_ontology_instance": ("knowledge_base_id", "entity_type", "canonical_name", "updates"),
    "delete_ontology_instance": ("knowledge_base_id", "entity_type", "canonical_name"),
    "create_ontology_relation": ("knowledge_base_id", "source_entity_type", "source_canonical_name",
                                 "relation_type", "target_entity_type", "target_canonical_name"),
    "update_ontology_relation": ("knowledge_base_id", "source_entity_type", "source_canonical_name",
                                 "relation_type", "target_entity_type", "target_canonical_name", "updates"),
    "delete_ontology_relation": ("knowledge_base_id", "source_entity_type", "source_canonical_name",
                                 "relation_type", "target_entity_type", "target_canonical_name"),
    # ── 本体实体搜索 ──
    "search_ontology_entities": ("knowledge_base_id", "keyword"),
    # ── 解析引擎设置 ──
    "list_parser_settings": ("knowledge_base_id",),
    "create_parser_setting": ("knowledge_base_id", "parser_type"),
    "update_parser_setting": ("setting_id",),
    "delete_parser_setting": ("setting_id",),
    "ingest_push": ("ds_id", "ingest_key", "storage_key", "file_name"),
    # ── MCP 文档上传状态查询 ──
    "get_document_status": ("document_id",),
}


class KnowledgeBaseCapability(BaseCapability):
    """Knowledge Base capability adapter."""

    _INTERNAL_ACTIONS = {"ingest_push"}

    def __init__(self):
        self.service = KnowledgeBaseService()
        self._space = SpaceService()
        self._domain_service = DomainServiceService()
        self._tags = TagService()
        self._document_tags = DocumentTagService()
        self._dispatch = self._build_dispatch()
        self._reconcile_task: asyncio.Task | None = None
        super().__init__()

    def register_routes(self, app):
        """注册知识库 REST API 路由"""
        from capabilities.knowledge_base.api import create_router

        router = create_router()
        app.include_router(router, prefix="/api/v1")

    async def initialize(self) -> None:
        try:
            await ensure_ontology_schema()
        except Neo4jAuthError:
            logger.critical("Neo4j authentication failed, refusing to start")
            raise
        except Exception as exc:
            logger.warning("Neo4j schema init failed (will retry), ontology queries may degrade: %s", exc)

        self._reconcile_task = asyncio.create_task(self._reconcile_loop())
        logger.info("Knowledge Base capability initialized (reconciliation loop started)")

    async def shutdown(self) -> None:
        if self._reconcile_task:
            self._reconcile_task.cancel()
            try:
                await self._reconcile_task
            except asyncio.CancelledError:
                pass
        await close_neo4j_driver()
        logger.info("Knowledge Base capability shutdown complete")

    async def _reconcile_loop(self) -> None:
        """每 30 秒扫描 PARSING 文档和本体对账（启动时立即执行一次）。"""
        logger.info("Reconciliation loop started (first run immediately)")
        first_run = True
        while True:
            try:
                if not first_run:
                    await asyncio.sleep(30)
                first_run = False

                logger.debug("Reconciliation cycle begin")
                doc_result = await self.service.reconciliation.reconcile_documents(limit=50)
                logger.info("对账→文档: %s", doc_result)
                onto_result = await self.service.reconciliation.reconcile_ontology(limit=50)
                logger.info("对账→本体: %s", onto_result)
                patrol_result = await self.service.reconciliation.patrol_parsing_timeout(limit=50)
                if patrol_result.get("timed_out", 0) > 0 or patrol_result.get("waiting", 0) > 0:
                    logger.info("对账→超时巡检: %s", patrol_result)
                # [jonex] LLM-Wiki 编译巡检：轮询任务状态回写 PG + stale 补提交 + 超时判死
                okb_result = await self.service.reconciliation.patrol_openkb_compile(limit=50)
                if okb_result.get("timed_out", 0) > 0 or okb_result.get("compiled", 0) > 0 \
                        or okb_result.get("failed", 0) > 0 or okb_result.get("stale_submitted", 0) > 0:
                    logger.info("对账→LLM-Wiki 编译巡检: %s", okb_result)
            except asyncio.CancelledError:
                logger.info("Reconciliation loop cancelled, exiting")
                break
            except Exception as e:
                # 数据库连接异常（可恢复）→ 等后重试；其它异常 → 记录完整堆栈
                if isinstance(e, (OperationalError, DBAPIError)):
                    logger.warning("Reconciliation loop: DB connection lost, will retry. %s", e)
                else:
                    logger.exception("Reconciliation loop error")
                await asyncio.sleep(10)

    def _build_dispatch(self) -> dict[str, ActionHandler]:
        docs = self.service.documents
        search = self.service.search
        history = self.service.history
        feedback = self.service.feedback
        parse = self.service.parse_results
        ontology = self.service.ontology
        reconcile = self.service.reconciliation
        kbinfo = self.service.knowledge_infos
        compiler = self.service.compiler
        folders = self.service.folders
        syn = self.service.synonyms
        oq = self.service.ontology_query
        ds = self.service.data_sources
        ps = self.service.parser_settings
        s = self._space
        sv = self._domain_service

        return {
            # ── 文档管理 ──
            "upload_document": lambda r, d: docs.upload_document(r.tenant_id, d, user_id=r.user_id, username=r.username, ip=r.ip),
            "list_documents": lambda r, d: docs.list_documents(r.tenant_id, d),
            "documents_stats": lambda r, d: docs.documents_stats(r.tenant_id, d),
            "get_document": lambda r, d: docs.get_document(r.tenant_id, d["document_id"], d),
            "get_document_status": lambda r, d: docs.get_document_status(r.tenant_id, d["document_id"], d.get("kb_ids", [])),
            "delete_document": lambda r, d: docs.delete_document(r.tenant_id, d["document_id"], d, user_id=r.user_id, username=r.username, ip=r.ip),
            "generate_upload_url": lambda r, d: docs.generate_upload_url(
                r.tenant_id, d["knowledge_base_id"], d["file_name"], d.get("content_type"),
            ),
            "get_raw_url": lambda r, d: _url_result(docs.get_raw_url(r.tenant_id, d.get("knowledge_base_id", ""), d["document_id"], user_id=r.user_id, username=r.username, ip=r.ip, mcp_key_id=(r.context or {}).get("mcp_key_id"))),
            "get_raw_content": lambda r, d: docs.get_raw_content(r.tenant_id, d.get("knowledge_base_id", ""), d["document_id"], user_id=r.user_id, username=r.username, ip=r.ip, mcp_key_id=(r.context or {}).get("mcp_key_id")),
            "get_raw_location": lambda r, d: docs.get_raw_location(r.tenant_id, d.get("knowledge_base_id", ""), d["document_id"]),
            "get_document_chunks": lambda r, d: docs.get_document_chunks(r.tenant_id, d["document_id"]),
            "get_chunk": lambda r, d: docs.get_chunk(r.tenant_id, d["document_id"], d["chunk_id"]),
            "reparse_document": lambda r, d: docs.reparse_document(r.tenant_id, d["document_id"], user_id=r.user_id, username=r.username, ip=r.ip, force=d.get("force", False)),
            "set_document_folder": lambda r, d: docs.set_document_folder(
                r.tenant_id, d["document_id"], d
            ),
            # ── 检索 ──
            "search": lambda r, d: search.search(r.tenant_id, _user_id(r), d, trace_id=r.request_id or str(uuid4())),
            "search_enhanced": lambda r, d: search.enhanced_search(r.tenant_id, _user_id(r), d, trace_id=r.request_id or str(uuid4())),
            "query_with_ontology": self._query_with_ontology,
            "deep_query": self._deep_query,
            # [jonex] openkb 分流
            "search_llmwiki": self._search_llmwiki,
            "search_mix": self._search_mix,
            "get_search_overview": lambda r, d: history.get_overview(r.tenant_id, _user_id(r), d),
            "list_search_history": lambda r, d: history.list_history(r.tenant_id, _user_id(r), d),
            "save_search_history": lambda r, d: history.save_history(r.tenant_id, _user_id(r), d),
            "delete_search_history": lambda r, d: history.delete_history(
                r.tenant_id, _user_id(r), d["history_id"], d
            ),
            "clear_search_history": lambda r, d: history.clear_history(r.tenant_id, _user_id(r), d),
            # ── 搜索结果反馈 ──
            "submit_search_feedback": lambda r, d: feedback.submit_feedback(r.tenant_id, _user_id(r), d),
            "cancel_search_feedback": lambda r, d: feedback.cancel_feedback(r.tenant_id, _user_id(r), d),
            "list_search_feedback": lambda r, d: feedback.list_feedback(r.tenant_id, d),
            "toggle_search_feedback_adopted": lambda r, d: feedback.toggle_adopted(r.tenant_id, d),
            "get_search_feedback_stats": lambda r, d: feedback.get_stats(r.tenant_id, d),
            # ── 解析结果 ──
            "get_parse_result_summary": lambda r, d: parse.get_summary(r.tenant_id, d),
            "get_parse_result_documents": lambda r, d: parse.list_documents(r.tenant_id, d),
            "get_parse_result_entities": lambda r, d: parse.list_entities(r.tenant_id, d),
            "get_parse_result_relationships": lambda r, d: parse.list_relationships(r.tenant_id, d),
            "get_parse_result_graph_summary": lambda r, d: parse.get_graph_summary(r.tenant_id, d),
            "get_parse_result_graph": lambda r, d: parse.get_graph(r.tenant_id, d),
            "get_document_parse_result": lambda r, d: parse.get_document_parse_result(r.tenant_id, d),
            # ── [jonex] OpenKB Wiki 阅读模式（编译结果页）──
            "get_wiki_page": lambda r, d: parse.get_wiki_page(
                r.tenant_id, d["knowledge_base_id"], d["path"]),
            "list_wiki_contents": lambda r, d: parse.get_wiki_contents(
                r.tenant_id, d["knowledge_base_id"], d.get("document_id", "")),
            # ── 本体 ──
            "retry_ontology_extract": lambda r, d: ontology.retry_extract(
                r.tenant_id,
                d["document_id"],
                d["knowledge_base_id"],
            ),
            # ── 本体编译 schema 编辑 ──
            "get_editor_state": lambda r, d: compiler.get_editor_state(
                r.tenant_id, d["knowledge_base_id"],
            ),
            "get_compiled_schema": lambda r, d: compiler.get_compiled_schema(
                r.tenant_id, d["knowledge_base_id"],
            ),
            "save_compiled_schema": lambda r, d: compiler.save_compiled_schema(
                r.tenant_id, d["knowledge_base_id"],
                d.get("entity_types", []), d.get("relation_types", []),
                edited_by=r.user_id,
                constraints=(d["constraints"] if "constraints" in d else None),
                expected_schema_version=d.get("expected_schema_version"),
            ),
            "reseed_compiled_schema": lambda r, d: compiler.reseed_from_template(
                r.tenant_id, d["knowledge_base_id"],
                d["template_scenario_id"], d.get("template_domain_id"),
                d.get("source_type", "business_template"),
                apply_to_documents=d.get("apply_to_documents", False),
            ),
            # ── 重新编译 schema（P2-H：遇 manual_edited 返回 409，reseed 才丢弃人工编辑）──
            "recompile_schema": lambda r, d: compiler.recompile_for_knowledge_base(
                r.tenant_id, d["knowledge_base_id"],
                force=d.get("force", False),
                apply_to_documents=d.get("apply_to_documents", False),
            ),
            # ── [jonex] compiled schema YAML 导入导出 ──
            "export_compiled_schema_yaml": lambda r, d: compiler.export_compiled_schema_yaml(
                d["knowledge_base_id"], r.tenant_id,
            ),
            "import_compiled_schema_yaml": lambda r, d: compiler.import_compiled_schema_yaml(
                d["knowledge_base_id"], r.tenant_id,
                d["yaml_text"], d["expected_schema_version"],
                dry_run=d.get("dry_run", True),
            ),
            # ── KB 级按新 schema 批量重抽本体（C→B 联动，走对账被动）──
            "reextract_kb_documents": lambda r, d: ontology.reextract_kb_documents(
                r.tenant_id, d["knowledge_base_id"],
                document_ids=d.get("document_ids"),
                only_outdated=d.get("only_outdated", False),
                only_ready=d.get("only_ready", True),
            ),
            # ── 文件夹 ──
            "list_folders": lambda r, d: folders.list_folders(
                r.tenant_id, d["knowledge_base_id"]
            ),
            "create_folder": lambda r, d: folders.create_folder(r.tenant_id, d),
            "rename_folder": lambda r, d: folders.rename_folder(
                r.tenant_id, d["folder_id"], d["knowledge_base_id"], d["name"]
            ),
            "delete_folder": lambda r, d: _deleted(folders.delete_folder(
                r.tenant_id, d["folder_id"], d["knowledge_base_id"]
            )),
            # ── 标签 ──
            "list_tags": lambda r, d: self._tags.list_tags(
                r.tenant_id, d["knowledge_base_id"]
            ),
            "create_tag": lambda r, d: self._tags.create_tag(r.tenant_id, d),
            "update_tag": lambda r, d: self._tags.update_tag(
                r.tenant_id, d["tag_id"], d["knowledge_base_id"], d
            ),
            "delete_tag": lambda r, d: _deleted(self._tags.delete_tag(
                r.tenant_id, d["tag_id"], d["knowledge_base_id"]
            )),
            # ── 文档-标签关联 ──
            "set_document_tags": lambda r, d: self._document_tags.set_document_tags(
                r.tenant_id, d["document_id"], d["knowledge_base_id"], d["tag_ids"]
            ),
            "get_document_tags": lambda r, d: self._document_tags.get_document_tags(
                r.tenant_id, d["document_id"], d["knowledge_base_id"]
            ),
            "add_document_tag": lambda r, d: self._document_tags.add_document_tag(
                r.tenant_id, d["document_id"], d["knowledge_base_id"], d["tag_id"]
            ),
            "remove_document_tag": lambda r, d: self._document_tags.remove_document_tag(
                r.tenant_id, d["document_id"], d["knowledge_base_id"], d["tag_id"]
            ),
            # ── 本体同义词 ──
            "list_synonyms": lambda r, d: syn.list(
                r.tenant_id, d["knowledge_base_id"], d.get("page", 1), d.get("page_size", 20)
            ),
            "create_synonym": lambda r, d: syn.create(r.tenant_id, d),
            "update_synonym": lambda r, d: syn.update(r.tenant_id, d["synonym_id"], d),
            "delete_synonym": lambda r, d: _deleted(syn.delete(r.tenant_id, d["synonym_id"])),
            "import_synonyms": lambda r, d: syn.batch_import(
                r.tenant_id, d["knowledge_base_id"], d.get("groups", [])
            ),
            # ── 知识库信息管理 ──
            "list_knowledge_info": lambda r, d: kbinfo.list(
                r.tenant_id, d.get("space_id"), d.get("status"),
                d.get("keyword"), d.get("offset", 0), d.get("limit", 20),
            ),
            "create_knowledge_info": lambda r, d: kbinfo.create(r.tenant_id, d),
            "get_knowledge_info": lambda r, d: kbinfo.get(d["kb_id"], r.tenant_id),
            "update_knowledge_info": lambda r, d: kbinfo.update(d["kb_id"], r.tenant_id, d),
            "delete_knowledge_info": lambda r, d: kbinfo.delete(d["kb_id"], r.tenant_id),
            # ── 领域空间 ──
            "list_spaces": lambda r, d: s.list(r.tenant_id, d.get("offset", 0), d.get("limit", 20)),
            "create_space": lambda r, d: s.create(r.tenant_id, d),
            "get_space": lambda r, d: s.get(d["space_id"], r.tenant_id),
            "update_space": lambda r, d: s.update(d["space_id"], r.tenant_id, d),
            "delete_space": lambda r, d: _deleted(s.delete(d["space_id"], r.tenant_id)),
            "get_space_permissions": lambda r, d: _list_result(s.get_permissions(d["space_id"], r.tenant_id)),
            "set_space_permissions": lambda r, d: _updated(
                s.set_permissions(d["space_id"], r.tenant_id, d.get("permissions", []))
            ),
            # ── 领域服务 ──
            "list_services": lambda r, d: sv.list(
                r.tenant_id, d.get("space_id"), d.get("offset", 0), d.get("limit", 20)
            ),
            "create_service": lambda r, d: sv.create(r.tenant_id, d),
            "get_service": lambda r, d: sv.get(d["service_id"], r.tenant_id),
            "update_service": lambda r, d: sv.update(d["service_id"], r.tenant_id, d),
            "delete_service": lambda r, d: _deleted(sv.delete(d["service_id"], r.tenant_id)),
            "enable_service": lambda r, d: sv.enable(d["service_id"], r.tenant_id),
            "disable_service": lambda r, d: sv.disable(d["service_id"], r.tenant_id),
            "rotate_service_api_key": lambda r, d: sv.rotate_api_key(d["service_id"], r.tenant_id),
            "list_service_api_keys": lambda r, d: sv.list_api_keys(d["service_id"], r.tenant_id),
            "create_service_api_key": lambda r, d: sv.create_api_key(d["service_id"], r.tenant_id, d),
            "delete_service_api_key": lambda r, d: _deleted(
                sv.delete_api_key(d["key_id"], d["service_id"], r.tenant_id)
            ),
            "get_service_configs": lambda r, d: sv.get_configs(d["service_id"], r.tenant_id),
            "update_service_configs": lambda r, d: _updated(
                sv.update_configs(d["service_id"], r.tenant_id, d.get("configs", {}))
            ),
            "get_service_permissions": lambda r, d: _list_result(sv.get_permissions(d["service_id"], r.tenant_id)),
            "set_service_permissions": lambda r, d: _updated(
                sv.set_permissions(d["service_id"], r.tenant_id, d.get("permissions", []))
            ),
            "search_service": lambda r, d: sv.search(d["service_id"], r.tenant_id, d["query"]),
            # ── 引用富化 ──
            "resolve_references": lambda r, d: search.resolve_references(
                r.tenant_id,
                doc_ids=d.get("doc_ids"),
                refs=d.get("refs"),
            ),
            # ── 本体查询统计 ──
            "get_ontology_statistics": lambda r, d: oq.get_kb_statistics(r.tenant_id, d),
            "list_ontology_instances": lambda r, d: oq.list_instances(r.tenant_id, d),
            "list_ontology_relations": lambda r, d: oq.list_relations(r.tenant_id, d),
            "get_ontology_graph": lambda r, d: oq.get_kb_graph(r.tenant_id, d),
            "expand_ontology_neighbors": lambda r, d: oq.expand_ontology_neighbors(r.tenant_id, d),
            "list_ontology_entity_types": lambda r, d: oq.list_entity_types(r.tenant_id, d),
            "list_ontology_relation_types": lambda r, d: oq.list_relation_types(r.tenant_id, d),
            # ── 数据源接入 ──
            "list_data_sources": lambda r, d: ds.list_sources(r.tenant_id, d["knowledge_base_id"]),
            "get_data_source": lambda r, d: ds.get_source(r.tenant_id, d["ds_id"]),
            "create_data_source": lambda r, d: ds.create_source(r.tenant_id, d),
            "update_data_source": lambda r, d: ds.update_source(r.tenant_id, d["ds_id"], d),
            "delete_data_source": lambda r, d: ds.delete_source(r.tenant_id, d["ds_id"]),
            "test_data_source": lambda r, d: ds.test_source(r.tenant_id, d["ds_id"]),
            "sync_data_source": lambda r, d: ds.sync_source(r.tenant_id, d["ds_id"]),
            "reset_ingest_key": lambda r, d: ds.reset_ingest_key(r.tenant_id, d["ds_id"]),
            # ── 解析引擎设置 ──
            "list_parser_settings": lambda r, d: ps.list_settings(r.tenant_id, d["knowledge_base_id"]),
            "create_parser_setting": lambda r, d: ps.create_setting(r.tenant_id, d),
            "update_parser_setting": lambda r, d: ps.update_setting(r.tenant_id, d["setting_id"], d),
            "delete_parser_setting": lambda r, d: ps.delete_setting(r.tenant_id, d["setting_id"]),
            # ── 本体实例/关系 创建、编辑与删除 ──
            "create_ontology_instance": lambda r, d: oq.create_instance(
                r.tenant_id, CreateOntologyInstanceRequest(**d),
            ),
            "update_ontology_instance": lambda r, d: oq.update_instance(
                r.tenant_id, UpdateOntologyInstanceRequest(**d),
            ),
            "delete_ontology_instance": lambda r, d: oq.delete_instance(
                r.tenant_id, DeleteOntologyInstanceRequest(**d),
            ),
            "create_ontology_relation": lambda r, d: oq.create_relation(
                r.tenant_id, CreateOntologyRelationRequest(**d),
            ),
            "update_ontology_relation": lambda r, d: oq.update_relation(
                r.tenant_id, UpdateOntologyRelationRequest(**d),
            ),
            "delete_ontology_relation": lambda r, d: oq.delete_relation(
                r.tenant_id, DeleteOntologyRelationRequest(**d),
            ),
            # ── 本体实体搜索（用于表单字段搜索选择） ──
            "search_ontology_entities": lambda r, d: oq.search_entities(
                r.tenant_id, OntologyEntitySearchRequest(**d),
            ),
            # 入站推送（内部 action，tenant 由 ds 推导，不校验 require_tenant）
            "ingest_push": lambda r, d: ds.ingest_push(
                d["ds_id"], d["ingest_key"],
                storage_key=d["storage_key"], file_name=d["file_name"],
                mime_type=d.get("mime_type"), file_size=d.get("file_size", 0),
                external_id=d.get("external_id"),
            ),
        }

    async def _resolve_service_id_to_kb_ids(self, data: dict, tenant_id: str) -> list[str]:
        """从 service_id 解析 knowledge_base_ids 列表。

        已有 knowledge_base_ids → 原样透传（兼容 Gateway REST 端点直传）。
        仅有 service_id → 通过 DomainServiceService.get() 解析为 kb_ids。
        service_id 不存在或 kb_ids 为空 → 抛出异常。
        """
        if data.get("knowledge_base_ids"):
            require_tenant(tenant_id)  # 防御性租户校验
            kb_ids = [k for k in data["knowledge_base_ids"] if k and str(k).strip()]
            if not kb_ids:
                logger.warning("knowledge_base_ids 全为空值")
                raise InvalidParameterError(message="knowledge_base_ids 不能全为空值")
            logger.debug("knowledge_base_ids already present in data, passing through: %s", kb_ids)
            return kb_ids

        service_id = data.get("service_id")
        if not service_id:
            logger.warning("Missing both knowledge_base_ids and service_id in data")
            raise InvalidParameterError(message="缺少 service_id 或 knowledge_base_ids 参数")

        svc = await self._domain_service.get(service_id, tenant_id)
        kb_ids = svc.get("kb_ids", [])
        if not kb_ids:
            logger.warning("service_id %s resolved to empty kb_ids", service_id)
            raise InvalidParameterError(
                message=f"领域服务 {service_id} 下无关联知识库",
                details={"service_id": service_id},
            )

        logger.debug("Resolved service_id=%s to kb_ids=%s", service_id, kb_ids)
        return kb_ids

    def _make_search_handler(self, search_method_name: str):
        """创建 search handler：解析 service_id → kb_ids → 调用对应的 search 方法。

        消除 ``_query_with_ontology`` / ``_deep_query`` / ``_search_llmwiki`` 三者的结构重复。
        """
        async def _handler(request, data):
            kb_ids = await self._resolve_service_id_to_kb_ids(data, request.tenant_id)
            data = dict(data, knowledge_base_ids=kb_ids)
            method = getattr(self.service.search, search_method_name)
            return await method(
                request.tenant_id, _user_id(request), data,
                trace_id=request.request_id or str(uuid4()),
            )
        return _handler

    async def _query_with_ontology(self, request, data):
        return await self._make_search_handler("query_with_ontology")(request, data)

    async def _deep_query(self, request, data):
        return await self._make_search_handler("deep_query")(request, data)

    async def _search_llmwiki(self, request, data):
        return await self._make_search_handler("search_llmwiki")(request, data)

    async def _search_mix(self, request, data):
        return await self._make_search_handler("search_mix")(request, data)

    def _build_metadata(self) -> CapabilityMetadata:
        return CapabilityMetadata(
            capability_id="knowledge_base",
            capability_name="知识库能力",
            capability_type=CapabilityType.BUSINESS,
            version="v1",
            description="知识文档、检索、检索历史、解析结果和本体抽取能力",
            author="jonex",
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": sorted(self._dispatch.keys()),
                    },
                    "data": {"type": "object"},
                },
                "required": ["action"],
            },
            output_schema={"type": "object"},
            tags=["知识库", "文档管理", "语义检索", "本体"],
        )

    @staticmethod
    def _build_required_fields() -> dict[str, tuple[str, ...]]:
        """返回每个 action 所需字段的映射（向后兼容快捷方法，实际数据在模块级 _REQUIRED_FIELDS）。"""
        return _REQUIRED_FIELDS

    async def validate_input(self, request: CapabilityRequest) -> bool:
        action = request.payload.get("action")
        if action not in self._dispatch:
            return False
        data = request.payload.get("data") or {}
        if not isinstance(data, dict):
            return False
        return all(data.get(field) is not None for field in _REQUIRED_FIELDS.get(action, ()))

    async def execute(self, request: CapabilityRequest) -> CapabilityResponse:
        action = request.payload.get("action")
        data = request.payload.get("data") or {}
        handler = self._dispatch.get(action)

        if handler is None:
            return CapabilityResponse.error(request.request_id, 400, f"不支持的操作: {action}")

        try:
            if action not in self._INTERNAL_ACTIONS:
                request.tenant_id = require_tenant(request.tenant_id)
            result = await handler(request, data)
            return CapabilityResponse.ok(request.request_id, result)
        except TenantIsolationError as exc:
            return CapabilityResponse.error(request.request_id, exc.code, exc.message)
        except JonexException as exc:
            return CapabilityResponse.error(request.request_id, exc.code, exc.message)
        except Exception as exc:
            # Pydantic ValidationError → 400 Bad Request（消息脱敏，不暴露字段名/类型）
            if "ValidationError" in type(exc).__name__:
                return CapabilityResponse.error(
                    request.request_id, 400, "参数校验失败",
                )
            # 通用执行异常 → 500（消息脱敏，不暴露 SQL/路径/堆栈）
            logger.exception("Knowledge Base capability action failed: %s", action)
            return CapabilityResponse.error(request.request_id, 500, "执行失败")


def _user_id(request: CapabilityRequest) -> str:
    return request.user_id or "anonymous"


async def _deleted(coro) -> dict:
    result = await coro
    return {"deleted": result}


async def _updated(coro) -> dict:
    result = await coro
    return {"updated": result}


async def _list_result(coro) -> dict:
    """包装 list 返回值，使 CapabilityResponse.data 保持 dict 类型"""
    result = await coro
    return {"permissions": result} if isinstance(result, list) else result


async def _url_result(coro) -> dict:
    """包装 get_raw_url 的 str 返回值，使 CapabilityResponse.data 保持 dict 类型"""
    url = await coro
    return {"url": url}
