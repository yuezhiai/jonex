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
from jonex_core.common import get_db_session
from jonex_core.common.exceptions import (
    InvalidParameterError,
    JonexException,
    PermissionDeniedError,
    TenantIsolationError,
)
from jonex_core.common.i18n import translate
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
    "documents_stats": ("knowledge_base_id",),
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
    # ── [jonex] LLM-Wiki Schema 编译设置（方案 llmwiki-schema-settings-execution-plan §9）──
    "get_llm_wiki_schema": ("knowledge_base_id",),
    "save_llm_wiki_schema": ("knowledge_base_id", "expected_schema_version"),
    "apply_llm_wiki_schema": ("knowledge_base_id",),
    "export_llm_wiki_schema_yaml": ("knowledge_base_id",),
    "import_llm_wiki_schema_yaml": ("knowledge_base_id", "yaml_text", "expected_schema_version"),
    "recompile_llm_wiki_schema_outdated_documents": ("knowledge_base_id",),
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
    "get_kb_permissions": ("kb_id",),
    "set_kb_permissions": ("kb_id",),
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
    "batch_set_document_folder": ("knowledge_base_id", "document_ids"),
    "generate_upload_url": ("knowledge_base_id", "file_name"),
    "get_raw_url": ("document_id",),
    "get_raw_content": ("document_id",),
    "get_raw_location": ("document_id",),
    # [jonex] §image-refs P2-3: 图片资产原文位置（ext 可选——缺省时按
    # build_asset_key 白名单兜底 png）
    "get_asset_raw_location": ("document_id", "image_idx"),
    "get_document_chunks": ("document_id",),
    "get_chunk": ("document_id", "chunk_id"),
    "reparse_document": ("document_id",),
    # [jonex] §table-grid-v2 O7: 新旧格式残留扫描与定点清理
    "scan_stale_chunks": ("document_id",),
    "purge_stale_chunks": ("document_id",),
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

# ══════════════════════════════════════════════════════════════
# [jonex] 空间权限判定分组（设计 docs/superpowers/specs/2026-08-19-space-permission-design.md §3）
# 完备性由 test_all_actions_classified 保证：dispatch 全部 action 必须落在六组之一。
# ══════════════════════════════════════════════════════════════

# 豁免（不判定）：
# - list_spaces / get_space：判定在 service 层（成员过滤落 SQL / 非成员 404）
# - create_space：纯豁免——校验完全下沉 service（owner 注入 + service:write，双入口都覆盖）
# - 历史/反馈：已按 _user_id(r) 用户级隔离；ingest_push：内部链路；resolve_references：无空间语义
_SPACE_EXEMPT_ACTIONS = frozenset({
    "list_spaces", "create_space", "get_space",
    "get_search_overview", "list_search_history", "save_search_history",
    "delete_search_history", "clear_search_history",
    "submit_search_feedback", "cancel_search_feedback",
    "list_search_feedback", "toggle_search_feedback_adopted",
    "get_search_feedback_stats",
    "ingest_push",
    "resolve_references",
})

# 空间级管理：仅 owner 或租户管理员（tenant:write）。
# 显式声明，不得落入前缀推导的 write 语义（manager 删空间必须 403）。
_SPACE_LEVEL_ACTIONS = frozenset({
    "update_space", "delete_space", "set_space_permissions",
})

# KB 权限管理（2026-08-20 设计）：仅空间 owner/manager 或租户管理员。
# 不能进 _SPACE_WRITE_ACTIONS（editor 授权兜底会放行）、不能进 _SPACE_LEVEL_ACTIONS
# （require_space_manage 口径是 owner/tenant_admin、不含 manager——比本组窄）。
_KB_MANAGE_ACTIONS = frozenset({
    "set_kb_permissions",
})

# KB 授权兜底排除（设计 §2.3）：delete_knowledge_info 不适用 KB 授权——
# editor 授权不能删 KB（write 兜底对其不生效，空间判定照旧 owner/manager）
_KB_GRANT_EXCLUDED_ACTIONS = frozenset({
    "delete_knowledge_info",
})

# 空间内写（owner / manager）
_SPACE_WRITE_ACTIONS = frozenset({
    "create_knowledge_info", "update_knowledge_info", "delete_knowledge_info",
    "upload_document", "delete_document", "reparse_document",
    "set_document_folder", "set_document_tags", "add_document_tag", "remove_document_tag",
    "scan_stale_chunks", "purge_stale_chunks", "generate_upload_url",
    "create_folder", "rename_folder", "delete_folder",
    # 领域服务写（2026-08-19 终版）：owner/manager 可增删改服务（与 KB/文档同口径）
    "create_service", "update_service", "delete_service",
    "update_service_configs", "enable_service", "disable_service",
    "create_service_api_key", "rotate_service_api_key", "delete_service_api_key",
    "set_service_permissions",
    "create_data_source", "update_data_source", "delete_data_source",
    "sync_data_source", "test_data_source", "reset_ingest_key",
    "create_ontology_instance", "update_ontology_instance", "delete_ontology_instance",
    "create_ontology_relation", "update_ontology_relation", "delete_ontology_relation",
    "create_synonym", "update_synonym", "delete_synonym", "import_synonyms",
    "create_tag", "update_tag", "delete_tag",
    "create_parser_setting", "update_parser_setting", "delete_parser_setting",
    "save_compiled_schema", "import_compiled_schema_yaml", "reseed_compiled_schema",
    "recompile_schema", "reextract_kb_documents", "retry_ontology_extract",
    "save_llm_wiki_schema", "import_llm_wiki_schema_yaml",
    "apply_llm_wiki_schema", "recompile_llm_wiki_schema_outdated_documents",
})

# 空间内读（成员：owner / manager / viewer）
_SPACE_READ_ACTIONS = frozenset({
    "get_knowledge_info", "list_knowledge_info",
    "get_document", "list_documents", "documents_stats",
    "get_document_status", "get_document_chunks", "get_document_parse_result",
    "get_chunk", "get_raw_content", "get_raw_location", "get_raw_url",
    "get_asset_raw_location",  # [jonex] §image-refs P2-3
    "get_document_tags",
    "list_folders",
    # 领域服务读：成员可见（viewer 可查看，不可操作——写归 _SPACE_LEVEL_ACTIONS）
    "get_service", "list_services", "get_service_configs", "list_service_api_keys",
    "get_service_permissions", "search_service",
    "get_data_source", "list_data_sources",
    "list_ontology_instances", "list_ontology_relations",
    "list_ontology_entity_types", "list_ontology_relation_types",
    "get_ontology_graph", "get_ontology_statistics",
    "expand_ontology_neighbors", "search_ontology_entities",
    "get_compiled_schema", "export_compiled_schema_yaml", "export_llm_wiki_schema_yaml",
    "get_llm_wiki_schema", "list_synonyms", "list_tags", "list_parser_settings",
    "get_wiki_page", "list_wiki_contents", "get_editor_state",
    "get_parse_result_documents", "get_parse_result_entities",
    "get_parse_result_graph", "get_parse_result_graph_summary",
    "get_parse_result_relationships", "get_parse_result_summary",
    "get_space_permissions", "get_kb_permissions",
})

# 检索类（过滤无权限 KB，不整体拒绝）
_SEARCH_ACTIONS = frozenset({
    "search", "search_enhanced", "deep_query", "search_mix",
    "query_with_ontology", "search_llmwiki",
})

# 空间解析路径（action 组 → space_id 数据来源）
_SPACE_ID_DIRECT_ACTIONS = frozenset({
    "update_space", "delete_space", "set_space_permissions",
    "get_space_permissions", "create_knowledge_info",
    "list_knowledge_info", "list_services",
})
_KB_ID_FIELD_ACTIONS = frozenset({
    "get_knowledge_info", "update_knowledge_info", "delete_knowledge_info",
    "get_kb_permissions", "set_kb_permissions",
})  # 用 kb_id 字段
_DOC_ID_ACTIONS = frozenset({
    "reparse_document", "scan_stale_chunks", "purge_stale_chunks",
    "get_chunk", "get_raw_content", "get_asset_raw_location",  # [jonex] §image-refs P2-3
    "get_document_parse_result",
})
_DS_ID_ACTIONS = frozenset({
    "get_data_source", "update_data_source", "delete_data_source",
    "test_data_source", "sync_data_source", "reset_ingest_key",
})
_SYN_ID_ACTIONS = frozenset({"update_synonym", "delete_synonym"})
_SETTING_ID_ACTIONS = frozenset({"update_parser_setting", "delete_parser_setting"})
_SERVICE_ID_ACTIONS = frozenset({
    "get_service", "update_service", "delete_service", "enable_service", "disable_service",
    "rotate_service_api_key", "list_service_api_keys", "create_service_api_key",
    "delete_service_api_key", "get_service_configs", "update_service_configs",
    "get_service_permissions", "set_service_permissions", "search_service",
})


class KnowledgeBaseCapability(BaseCapability):
    """Knowledge Base capability adapter."""

    _INTERNAL_ACTIONS = {"ingest_push"}

    # [jonex] 双向门禁（方案 §15）：本体 schema/抽取类 action 拒绝 openkb KB。
    # 清单与方案 §15 一致——这些 action 当前都在 dispatch 直接分发、无 kb_type 校验。
    _ONTOLOGY_GATED_ACTIONS = {
        "get_compiled_schema",
        "save_compiled_schema",
        "reseed_compiled_schema",
        "recompile_schema",
        "export_compiled_schema_yaml",
        "import_compiled_schema_yaml",
        "reextract_kb_documents",
        # [jonex] retry_ontology_extract 不在此列：它是双用途 action，
        # openkb → KnowledgeCompilerService.recompile_openkb（重新编译），
        # lightrag → 本体重抽。kb_type 路由在 ontology_service.retry_extract 内处理。
        "get_editor_state",
    }

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
        lws = self.service.llm_wiki_schemas  # [jonex] LLM-Wiki Schema
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
            # [jonex] §image-refs P2-3: 图片资产原文位置（cos=预签名 / local=storage_key）
            "get_asset_raw_location": lambda r, d: docs.get_asset_raw_location(
                r.tenant_id, d["document_id"], d["image_idx"],
                ext=d.get("ext", ""), knowledge_base_id=d.get("knowledge_base_id", ""),
            ),
            "get_document_chunks": lambda r, d: docs.get_document_chunks(r.tenant_id, d["document_id"]),
            "get_chunk": lambda r, d: docs.get_chunk(r.tenant_id, d["document_id"], d["chunk_id"]),
            "reparse_document": lambda r, d: docs.reparse_document(r.tenant_id, d["document_id"], user_id=r.user_id, username=r.username, ip=r.ip, force=d.get("force", False)),
            # [jonex] §table-grid-v2 O7: 新旧格式残留扫描与定点清理
            "scan_stale_chunks": lambda r, d: docs.scan_stale_chunks(r.tenant_id, d["document_id"]),
            "purge_stale_chunks": lambda r, d: docs.purge_stale_chunks(r.tenant_id, d["document_id"]),
            "set_document_folder": lambda r, d: docs.set_document_folder(
                r.tenant_id, d["document_id"], d
            ),
            "batch_set_document_folder": lambda r, d: docs.batch_set_document_folder(
                r.tenant_id, d
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
            # ── [jonex] LLM-Wiki Schema 编译设置 ──
            "get_llm_wiki_schema": lambda r, d: lws.get_schema(
                r.tenant_id, d["knowledge_base_id"]),
            "save_llm_wiki_schema": lambda r, d: lws.save_schema(
                r.tenant_id, d, user_id=r.user_id),
            "apply_llm_wiki_schema": lambda r, d: lws.apply_to_openkb(
                r.tenant_id, d["knowledge_base_id"]),
            "export_llm_wiki_schema_yaml": lambda r, d: lws.export_yaml(
                r.tenant_id, d["knowledge_base_id"]),
            "import_llm_wiki_schema_yaml": lambda r, d: lws.import_yaml(
                r.tenant_id, d, user_id=r.user_id),
            "recompile_llm_wiki_schema_outdated_documents": lambda r, d: lws.recompile_outdated_documents(
                r.tenant_id, d),
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
                user_id=r.user_id,
            ),
            "create_knowledge_info": lambda r, d: kbinfo.create(r.tenant_id, d),
            "get_knowledge_info": lambda r, d: kbinfo.get(d["kb_id"], r.tenant_id, user_id=r.user_id),
            "update_knowledge_info": lambda r, d: kbinfo.update(d["kb_id"], r.tenant_id, d),
            "delete_knowledge_info": lambda r, d: kbinfo.delete(d["kb_id"], r.tenant_id),
            "get_kb_permissions": lambda r, d: _list_result(
                kbinfo.get_permissions(d["kb_id"], r.tenant_id, user_id=r.user_id)
            ),
            "set_kb_permissions": lambda r, d: _updated(
                kbinfo.set_permissions(
                    d["kb_id"], r.tenant_id, d.get("permissions", []), user_id=r.user_id
                )
            ),
            # ── 领域空间 ──
            "list_spaces": lambda r, d: s.list(r.tenant_id, d.get("offset", 0), d.get("limit", 20), user_id=r.user_id),
            "create_space": lambda r, d: s.create(r.tenant_id, d, owner_id=r.user_id),
            "get_space": lambda r, d: s.get(d["space_id"], r.tenant_id, user_id=r.user_id),
            "update_space": lambda r, d: s.update(d["space_id"], r.tenant_id, d, user_id=r.user_id),
            "delete_space": lambda r, d: _deleted(s.delete(d["space_id"], r.tenant_id, user_id=r.user_id)),
            "get_space_permissions": lambda r, d: s.get_permissions(d["space_id"], r.tenant_id, user_id=r.user_id),
            "set_space_permissions": lambda r, d: _updated(
                s.set_permissions(d["space_id"], r.tenant_id, d.get("permissions", []), user_id=r.user_id)
            ),
            # ── 领域服务 ──
            "list_services": lambda r, d: sv.list(
                r.tenant_id, d.get("space_id"), d.get("offset", 0), d.get("limit", 20),
                user_id=r.user_id,
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

        try:
            svc = await self._domain_service.get(service_id, tenant_id)
        except Exception as e:
            logger.warning("service_id=%s 查询失败: %s", service_id, e)
            raise InvalidParameterError(
                message=f"领域服务不存在或无权访问: {service_id}",
                details={"service_id": service_id},
            ) from e

        kb_ids = [k for k in svc.get("kb_ids", []) if k and str(k).strip()]
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

    async def _query_with_ontology(self, request: CapabilityRequest, data: dict[str, Any]) -> dict:
        return await self._make_search_handler("query_with_ontology")(request, data)

    async def _deep_query(self, request: CapabilityRequest, data: dict[str, Any]) -> dict:
        return await self._make_search_handler("deep_query")(request, data)

    async def _search_llmwiki(self, request: CapabilityRequest, data: dict[str, Any]) -> dict:
        return await self._make_search_handler("search_llmwiki")(request, data)

    async def _search_mix(self, request: CapabilityRequest, data: dict[str, Any]) -> dict:
        return await self._make_search_handler("search_mix")(request, data)

    async def _resolve_space_context(
        self, action: str, data: dict[str, Any], tenant_id: str
    ) -> tuple[str | None, str | None, str | None]:
        """解析 action 的 (space_id, kb_type, kb_id)；无空间语义/解析不到返回 (None, None, None)。

        - 空间类：data["space_id"] 直取（create_knowledge_info 的 space_id 也直取）
        - KB 类：knowledge_base_id / kb_id → 单查询拿 space_id + kb_type + id
          （与 _ONTOLOGY_GATED_ACTIONS 的 kb_type 查询合并为一次往返）
        - 服务/数据源/文档/同义词/设置类：按各自 id 字段查表解析
        """
        from sqlalchemy import text

        if action in _SPACE_EXEMPT_ACTIONS or action in _SEARCH_ACTIONS:
            return None, None, None
        if action in _SPACE_ID_DIRECT_ACTIONS:
            return data.get("space_id"), None, None

        async def _one(sql: str, params: dict) -> tuple[str | None, str | None, str | None]:
            async with get_db_session() as session:
                row = (await session.execute(text(sql), params)).first()
            if not row:
                return None, None, None
            return row[0], row[1] if len(row) > 1 else None, row[2] if len(row) > 2 else None

        if action in _SERVICE_ID_ACTIONS:
            sid = data.get("service_id")
            if not sid:
                return None, None, None
            return await _one(
                "SELECT space_id, NULL, NULL FROM knowledge_base.services "
                "WHERE id=:sid AND tenant_id=:t AND is_deleted=0",
                {"sid": sid, "t": tenant_id},
            )
        if action in _DOC_ID_ACTIONS:
            did = data.get("document_id")
            if not did:
                return None, None, None
            return await _one(
                "SELECT ki.space_id, ki.kb_type, ki.id FROM knowledge_base.knowledge_documents d "
                "JOIN knowledge_base.knowledge_info ki ON ki.id = d.knowledge_base_id "
                "  AND ki.is_deleted = 0 "
                "WHERE d.id=:did AND d.tenant_id=:t AND d.is_deleted=0",
                {"did": did, "t": tenant_id},
            )
        if action in _DS_ID_ACTIONS:
            ds_id = data.get("ds_id")
            if not ds_id:
                return None, None, None
            return await _one(
                "SELECT ki.space_id, ki.kb_type, ki.id FROM knowledge_base.knowledge_data_sources ds "
                "JOIN knowledge_base.knowledge_info ki ON ki.id = ds.knowledge_base_id "
                "  AND ki.is_deleted = 0 "
                "WHERE ds.id=:ds AND ds.tenant_id=:t AND ds.is_deleted=0",
                {"ds": ds_id, "t": tenant_id},
            )
        if action in _SYN_ID_ACTIONS:
            syn_id = data.get("synonym_id")
            if not syn_id:
                return None, None, None
            return await _one(
                "SELECT ki.space_id, ki.kb_type, ki.id FROM knowledge_base.ontology_synonyms s "
                "JOIN knowledge_base.knowledge_info ki ON ki.id = s.knowledge_base_id "
                "  AND ki.is_deleted = 0 "
                "WHERE s.id=:sid AND s.tenant_id=:t AND s.is_deleted=0",
                {"sid": syn_id, "t": tenant_id},
            )
        if action in _SETTING_ID_ACTIONS:
            set_id = data.get("setting_id")
            if not set_id:
                return None, None, None
            return await _one(
                "SELECT ki.space_id, ki.kb_type, ki.id FROM knowledge_base.knowledge_parser_settings p "
                "JOIN knowledge_base.knowledge_info ki ON ki.id = p.knowledge_base_id "
                "  AND ki.is_deleted = 0 "
                "WHERE p.id=:sid AND p.tenant_id=:t AND p.is_deleted=0",
                {"sid": set_id, "t": tenant_id},
            )
        # KB 类：knowledge_base_id 或 kb_id
        kb_id = data.get("kb_id") if action in _KB_ID_FIELD_ACTIONS else data.get("knowledge_base_id")
        if not kb_id:
            return None, None, None
        return await _one(
            "SELECT space_id, kb_type, id FROM knowledge_base.knowledge_info "
            "WHERE id=:kb AND tenant_id=:t AND is_deleted=0",
            {"kb": kb_id, "t": tenant_id},
        )

    async def _filter_search_kbs(
        self, request: CapabilityRequest, data: dict[str, Any]
    ) -> dict[str, Any]:
        """检索类 action 的 KB 过滤：移除无权限的 KB，不整体拒绝。

        KB 入参形态分三组（设计 §3）：
        - knowledge_base_id 单个：无权限 → 403（明确拒绝；**只判断、不改写原值**）
        - knowledge_base_ids 数组：过滤后回写（可为空，handler 返回空结果。
          validate_input 无调用方、过滤发生在 execute 内，空数组不会撞必填校验）
        - domain_space_id 空间维度：按空间角色整体 403（非成员）
        性能：可见空间一次 SQL 批量查（get_visible_space_ids），零 N+1。
        """
        from .services.space_permission_service import get_visible_space_ids

        user_id = _user_id(request)
        if data.get("domain_space_id"):
            space_id = data["domain_space_id"]
            visible = await get_visible_space_ids(request.tenant_id, user_id)
            if visible is not None and space_id not in visible:
                raise PermissionDeniedError(
                    message=translate(
                        "err.space.insufficient_role",
                        params={"space_id": space_id},
                        fallback=f"无权操作空间: {space_id}",
                    )
                )
            return data
        kb_ids = data.get("knowledge_base_ids") or []
        single = False
        if not kb_ids and data.get("knowledge_base_id"):
            kb_ids = [data["knowledge_base_id"]]
            single = True
        if not kb_ids:
            return data  # 无 KB 参数（如 search_mix 无 KB 维度）：交由 handler 处理
        # 一次查 KB → space_id 映射；再一次查可见空间集合（None = 全可见）
        from sqlalchemy import text

        async with get_db_session() as session:
            rows = (await session.execute(
                text(
                    "SELECT ki.id, ki.space_id FROM knowledge_base.knowledge_info ki "
                    "WHERE ki.id = ANY(:ids) AND ki.tenant_id=:t AND ki.is_deleted=0"
                ),
                {"ids": kb_ids, "t": request.tenant_id},
            )).all()
        visible = await get_visible_space_ids(request.tenant_id, user_id)
        visible_set = set(visible) if visible is not None else None
        accessible = [
            kb_id for kb_id, space_id in rows
            if visible_set is None or space_id in visible_set
        ]
        # KB 授权叠加：仅当存在被过滤掉的 KB 时才补查一次授权集合（成员检索零额外查询）
        if visible_set is not None and len(accessible) < len(rows):
            from .services.kb_permission_service import get_granted_kb_ids

            granted_set = set(await get_granted_kb_ids(request.tenant_id, user_id))
            accessible = [
                kb_id for kb_id, space_id in rows
                if space_id in visible_set or kb_id in granted_set
            ]
        if single:
            if not accessible:
                raise PermissionDeniedError(
                    message=translate(
                        "err.space.insufficient_role",
                        params={"space_id": data["knowledge_base_id"]},
                        fallback=f"无权操作空间: {data['knowledge_base_id']}",
                    )
                )
            return data  # 原值有效，不改写
        data["knowledge_base_ids"] = accessible
        return data

    async def _check_kb_manage(
        self, tenant_id: str, space_id: str, kb_id: str, actor: str | None
    ) -> None:
        """set_kb_permissions 判定：空间 owner/manager 或租户管理员；无 KB 授权兜底。

        （设计 §2.7：editor 授权不能设权限——本方法刻意不查 get_kb_grant_role。）
        模块级依赖注入点：has_space_role / is_tenant_admin（经判定服务模块属性）。
        """
        from jonex_core.common.exceptions import PermissionDeniedError
        from jonex_core.common.i18n import translate
        from .services.space_permission_service import has_space_role, is_tenant_admin

        if (
            await has_space_role(tenant_id, space_id, actor, "owner", "manager")
            or await is_tenant_admin(tenant_id, actor)
        ):
            return
        raise PermissionDeniedError(
            message=translate(
                "err.kb_permission.manage_required",
                params={"kb_id": kb_id or ""},
                fallback=f"仅空间 owner/manager 或租户管理员可管理知识库权限: {kb_id or ''}",
            )
        )

    def _build_metadata(self) -> CapabilityMetadata:
        return CapabilityMetadata(
            # FIXME(L9): capability_id 不符合 kind.name.v{major} 格式；
            # Gateway internal.py 中使用 business.knowledge_base.v1 不一致。
            # 修改需同步评估 Gateway→Sidecar→capability dispatch 所有调用链。
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
            kb_type = None
            # [jonex] 空间权限前置判定（设计 2026-08-19-space-permission-design §3）
            # 统一 bypass：无 user_id / "anonymous"（测试 token、内部链路）或 MCP Key 认证
            # （api_gateway/routes/internal.py 只传 context.mcp_key_id）→ 不走空间判定。
            # 设计 §3「MCP Key 认证调用 → 不走空间判定」「无 user_id 口径放行」。
            # create_space 虽在豁免组，其 owner/service:write 校验在 service 层，不受影响。
            actor = request.user_id
            is_mcp = (request.context or {}).get("mcp_key_id")
            skip_space_check = (not actor or actor == "anonymous") or bool(is_mcp)
            # _kb_granted 防客户端注入：任何判定之前无条件 pop（客户端塞 true 只会
            # 收紧自己、不会放开，但必须保证「空间成员行为零变化」——成员路径不得
            # 残留注入标记导致字段被意外裁剪）
            data.pop("_kb_granted", None)
            if not skip_space_check:
                from .services.space_permission_service import (
                    has_space_role,
                    require_space_manage,
                    require_space_role,
                )
                from .services.kb_permission_service import get_kb_grant_role

                if action in _SEARCH_ACTIONS:
                    data = await self._filter_search_kbs(request, data)
                elif action not in _SPACE_EXEMPT_ACTIONS:
                    space_id, kb_type, kb_id = await self._resolve_space_context(
                        action, data, request.tenant_id
                    )
                    if space_id:
                        if action in _SPACE_LEVEL_ACTIONS:
                            await require_space_manage(request.tenant_id, space_id, actor)
                        elif action in _KB_MANAGE_ACTIONS:
                            await self._check_kb_manage(
                                request.tenant_id, space_id, kb_id or "", actor
                            )
                        elif action in _SPACE_WRITE_ACTIONS:
                            if not await has_space_role(
                                request.tenant_id, space_id, actor, "owner", "manager"
                            ):
                                # KB 授权兜底：editor 覆盖「写」
                                if (
                                    kb_id
                                    and action not in _KB_GRANT_EXCLUDED_ACTIONS
                                    and await get_kb_grant_role(
                                        request.tenant_id, kb_id, actor
                                    ) == "editor"
                                ):
                                    data["_kb_granted"] = True
                                else:
                                    await require_space_role(
                                        request.tenant_id, space_id, actor, "owner", "manager"
                                    )
                        elif action in _SPACE_READ_ACTIONS:
                            if not await has_space_role(
                                request.tenant_id, space_id, actor,
                                "owner", "manager", "viewer",
                            ):
                                # KB 授权兜底：editor/viewer 覆盖「读」
                                granted = (
                                    await get_kb_grant_role(request.tenant_id, kb_id, actor)
                                    if kb_id else None
                                )
                                if granted in ("editor", "viewer"):
                                    data["_kb_granted"] = True
                                else:
                                    await require_space_role(
                                        request.tenant_id, space_id, actor,
                                        "owner", "manager", "viewer",
                                    )


            # [jonex] 双向门禁（方案 §15）：本体 schema/抽取类 action 拒绝 openkb KB——
            # openkb 不跑本体，调了会存没人读的 ontology_compiled_schemas 行。
            # kb_type 已由上方 _resolve_space_context 单查询带回（kb 类 action）；
            # 未命中时保持原有自查（单条兜底，不叠加 action 分组条件——
            # _ONTOLOGY_GATED_ACTIONS 与 _SEARCH_ACTIONS/_SPACE_EXEMPT_ACTIONS 本无交集）
            if action in self._ONTOLOGY_GATED_ACTIONS:
                if kb_type is None:
                    fallback_kb_id = (data or {}).get("knowledge_base_id", "")
                    if fallback_kb_id:
                        from .services.kb_type_service import get_kb_type

                        kb_type = await get_kb_type(request.tenant_id, fallback_kb_id)
                if kb_type == "openkb":
                    return CapabilityResponse.error(
                        request.request_id, 400,
                        "该知识库为 llm-wiki 类型，不支持本体 schema/抽取操作",
                        details={"error_code": "ONTOLOGY_ACTION_KB_TYPE_MISMATCH",
                                 "kb_type": kb_type},
                    )
            result = await handler(request, data)
            return CapabilityResponse.ok(request.request_id, result)
        except TenantIsolationError as exc:
            return CapabilityResponse.error(request.request_id, exc.code, exc.message, details=exc.details or None)
        except JonexException as exc:
            return CapabilityResponse.error(request.request_id, exc.code, exc.message, details=exc.details or None)
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
