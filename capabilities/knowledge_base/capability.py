"""Knowledge Base business capability (business.knowledge_base.v1)."""

import asyncio
import base64
import binascii
import datetime
import json
import logging
import math
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from sqlalchemy import bindparam, text
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
from jonex_core.common.config import get_config
from jonex_core.common.exceptions import (
    InternalError,
    InvalidParameterError,
    JonexException,
    OperationNotSupportedError,
    PermissionDeniedError,
    ResourceConflictError,
    ResourceNotFoundError,
    TenantIsolationError,
)
from jonex_core.common.i18n import translate
from jonex_core.common.neo4j_client import (
    close_neo4j_driver,
    ensure_ontology_schema,
    get_neo4j_driver,
)
from jonex_core.common.quota import get_system_quota
from jonex_core.common.security_guards import assert_secure_jwt_secret
from jonex_core.common.tenant import require_tenant
from jonex_core.common.tenant_status import require_active_tenant

from .repository.ontology_graph_repository import OntologyGraphRepository
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
from .services.quota_service import QuotaService
from .services.analytics_service import AnalyticsService
from .services.data_source_connection_service import DataSourceConnectionService
# 数据资产子图只读查询（P1.5a Task 4）：快照 + 纯过滤，无状态。
from .services import ontology_data_assets as oda
# 血缘路径评分查询（P2b Task 1）：复用 ② NL→SQL 的边池与纯函数路径搜索。
from .services import ontology_lineage_paths as olp
from .services import lineage_coverage as lcov
from .services import lineage_impact as lig
# 跨模态对齐（P1.5b Task 2）：建议生成 + 人审状态流转。
from .services.ontology_data_alignment import OntologyDataAlignmentService
from .services.document_similarity_service import DocumentSimilarityService
# [jonex] KB_ENABLED_TYPES：本部署允许创建哪些 KB 类型（部署级白名单，随 env 走）
from .services.kb_type_service import enabled_kb_types, kb_type_options
# 「更新模型」：文档行操作 → nexonto 再解析 → 草稿 → 人工确认导入。
from .services.model_update import ModelUpdateService

logger = logging.getLogger(__name__)

ActionHandler = Callable[[CapabilityRequest, dict[str, Any]], Awaitable[dict]]

#: 「这一趟对账没跑成」的哨兵（`_reconcile_pass` 的返回值）。
#: 必须与「跑成了、只是结果是空的」区分开 —— 后者是正常回执（如 `{}`、`None`、
#: `{"timed_out": 0}`），拿假值判失败会把每一轮都记成失败、退化成一味退避。
_PASS_FAILED = object()

# ---------------------------------------------------------------------------
# 模块级常量：每个 action 的必填字段（纯静态数据，避免每次调用重建 ~90 键 dict）
# 与 _build_dispatch() 的 key 集合保持一致（`validate_input` 对缺席者当空元组，
# 所以「dispatch 接得住但这里没登记」= **一个必填字段都不校验**）。
#
# 守卫在 tests/unit/test_kb_action_permissions.py 的两条不变量：
#   - test_every_action_is_reachable_by_some_gate（每条 action 都归进了某张闸门/清单集合）
#   - test_dispatch_and_required_fields_agree_on_the_key_set（两边的键集一致）
# ⚠️ 注释里点名的测试**必须真实存在**（这条债绊过审计）：本文件有
#    test_notes_in_capability_py_do_not_point_at_missing_tests 专门盯死指针。
# ---------------------------------------------------------------------------
_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "get_document": ("document_id", "knowledge_base_id"),
    "list_document_versions": ("document_id",),
    "upload_document_version": ("document_id", "knowledge_base_id", "storage_key"),
    "get_document_version_url": ("document_id", "version_no"),
    "activate_document_version": ("document_id", "version_no"),
    "discard_document_version_upload": ("document_id", "storage_key"),
    # ── 解析后相似检测（建议 / 忽略 / 合并为新版本）──
    "get_document_similarity_suggestions": ("document_id",),
    "dismiss_document_similarity_suggestion": ("document_id", "suggestion_id"),
    "merge_document_version": ("document_id", "suggestion_id"),
    "get_document_quality_report": ("document_id", "knowledge_base_id"),
    "get_document_quality_reports": ("document_ids", "knowledge_base_id"),
    "delete_document": ("document_id", "knowledge_base_id"),
    # 第 3 期生命周期：归档 / 恢复 / 销毁（KB 执行）
    "archive_document": ("document_id", "knowledge_base_id"),
    "restore_document": ("document_id", "knowledge_base_id"),
    "destroy_document": ("document_id", "knowledge_base_id"),
    "upload_document": ("knowledge_base_id",),
    "list_documents": ("knowledge_base_id",),
    "documents_stats": ("knowledge_base_id",),
    "search": ("query",),
    "search_enhanced": ("query", "knowledge_base_id"),
    "query_with_ontology": ("query",),
    "search_llmwiki": ("query",),     # [jonex]
    "start_jonex_wiki_search": ("query",),
    "poll_jonex_wiki_search": ("session_id",),
    "cancel_jonex_wiki_search": ("session_id",),
    "resolve_wiki_evidence": ("knowledge_base_id", "query_id", "handle"),
    "search_jonex_wiki": ("query",),  # [jonex]
    "search_mix": ("query",),          # [jonex]
    # 网关流式检索的写前预检（B2）：只判权限/密级，不读内容。
    "assert_kb_searchable": ("knowledge_base_id",),
    # 流式检索埋点（gateway 在发流前调用；权限已由 assert_kb_searchable 预检）
    "record_stream_search": ("knowledge_base_id", "query"),
    # 标准侧发布联动（business_domain 经 Sidecar 调用）
    "mark_template_scenario_published": ("scenario_id",),
    "list_impacted_knowledge_bases": ("scenario_id",),
    "deep_query": ("query",),
    "get_search_overview": (),
    "list_search_history": (),
    "save_search_history": ("query",),
    "delete_search_history": ("history_id",),
    "clear_search_history": (),
    # ── 以图问知识（图片转查询 + 查看）──
    "image_to_query": ("images",),
    "get_query_image_ticket": ("image_id",),
    "get_query_image_location": ("image_id",),
    "get_parse_result_summary": ("knowledge_base_id",),
    "get_parse_result_documents": ("knowledge_base_id",),
    "get_parse_result_entities": ("knowledge_base_id",),
    "get_parse_result_relationships": ("knowledge_base_id",),
    "get_parse_result_graph_summary": ("knowledge_base_id",),
    "get_parse_result_graph": ("knowledge_base_id",),
    "get_document_parse_result": ("document_id", "knowledge_base_id"),
    "get_wiki_page": ("knowledge_base_id", "path"),
    "list_wiki_contents": ("knowledge_base_id",),  # document_id 不进必填（服务层可选）
    "get_wiki_compilation_state": ("knowledge_base_id",),
    "pause_wiki_compilation": ("knowledge_base_id",),
    "resume_wiki_compilation": ("knowledge_base_id",),
    # ── [jonex] Wiki 阅读侧：反向链接 / claim 级溯源审计（仅 jonex_wiki）──
    "get_wiki_backlinks": ("knowledge_base_id", "path"),
    "get_wiki_claims": ("knowledge_base_id",),  # path / document_id 至少一个，适配器校验
    # ── [jonex] Wiki 阅读目录（LLM 起草 + LLM 调整；仅 jonex_wiki）──
    "get_wiki_outline": ("knowledge_base_id",),
    "draft_wiki_outline": ("knowledge_base_id",),
    "adjust_wiki_outline": ("knowledge_base_id", "instruction"),
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
    "get_kb_compiled_schema_status": ("knowledge_base_id",),
    "preview_kb_compiled_schema_recompile": ("knowledge_base_id",),
    # [jonex] 原子抽取入口（atomic-rag Stage4）：等同 get_compiled_schema **+ 注入
    # KB 级同义词**。原先这条语义只存在于容器 REST 层
    # （api/__init__.py 的 GET /ontology/compiled-schema，见其注释「仅在对外下发
    # （atomic-rag 拉取入口）注入 KB 级同义词」），而 invoke 侧的 get_compiled_schema
    # **有意不注入**（避免污染查询链路）。为让 atomic-rag 摆脱对容器 REST 层的依赖，
    # 把该语义显式提成一个 action，而不是复用 get_compiled_schema。
    "get_compiled_schema_for_extraction": ("knowledge_base_id",),
    "save_compiled_schema": ("knowledge_base_id",),
    "reseed_compiled_schema": ("knowledge_base_id", "template_scenario_id"),
    "recompile_schema": ("knowledge_base_id",),
    "export_compiled_schema_yaml": ("knowledge_base_id",),
    "import_compiled_schema_yaml": ("knowledge_base_id", "yaml_text", "expected_schema_version"),
    "reextract_kb_documents": ("knowledge_base_id",),
    # ── 更新模型（文档行操作 → nexonto 再解析 → 草稿 → 人工确认导入）──
    "start_model_update": ("knowledge_base_id", "document_id"),
    "get_model_update_status": ("knowledge_base_id", "job_id"),
    "apply_model_update": ("knowledge_base_id", "job_id"),
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
    # ── 血缘边（KB 设置 → 血缘页签；逐条 CRUD，无「整份覆盖」语义）──
    "list_lineage_edges": ("knowledge_base_id",),
    # 血缘路径评分查询（P2b Task 1）：按 KB + 源 + 锚点表取路径；target 可选。
    "list_lineage_paths": ("knowledge_base_id", "data_source_id", "anchor"),
    "analyze_lineage_impact": ("knowledge_base_id", "data_source_id", "table"),
    "get_lineage_coverage": ("knowledge_base_id", "data_source_id"),
    "create_lineage_edge": ("knowledge_base_id",),
    # ⚠️ update/delete **也要 `knowledge_base_id`**（值不是 `kb_id`）：服务层只按
    # `(tenant_id, edge_id)` 定位 ⇒ 同租户内**另一个 KB 的边**也能被改/删（审查 M-8）。
    # 列进必填键，handler 才能把 KB 传进服务并入行定位条件；少写这一项不会报错，
    # 只会静默放行（`validate_input` 只检查登记过的字段）。
    "update_lineage_edge": ("edge_id", "knowledge_base_id"),
    "delete_lineage_edge": ("edge_id", "knowledge_base_id"),
    # ── 血缘禁止同查表对（KB 设置 → 血缘 → 禁止同查）──
    "list_lineage_forbidden_pairs": ("knowledge_base_id",),
    "create_lineage_forbidden_pair": ("knowledge_base_id",),
    "update_lineage_forbidden_pair": ("pair_id", "knowledge_base_id"),
    "delete_lineage_forbidden_pair": ("pair_id", "knowledge_base_id"),
    "suggest_lineage_forbidden_pairs": ("knowledge_base_id", "ds_id"),
    # ── 数据治理控制面 P1（#11）──
    "list_governance_labels": ("knowledge_base_id",),
    "create_governance_label": ("knowledge_base_id",),
    # W2.7a 敏感扫描：扫描只读；采纳是写动作（items 1..200 由服务层校验）
    "governance_sensitive_scan": ("knowledge_base_id",),
    "governance_adopt_sensitive_scan": ("knowledge_base_id", "items"),
    "update_governance_label": ("label_id", "knowledge_base_id"),
    "delete_governance_label": ("label_id", "knowledge_base_id"),
    "list_governance_tags": ("knowledge_base_id",),
    "create_governance_tag": ("knowledge_base_id",),
    "update_governance_tag": ("tag_id", "knowledge_base_id"),
    "delete_governance_tag": ("tag_id", "knowledge_base_id"),
    "list_governance_clearance": ("knowledge_base_id",),
    "set_governance_clearance": ("user_id", "level", "knowledge_base_id"),
    "list_governance_policies": ("knowledge_base_id",),
    "create_governance_policy": ("knowledge_base_id",),
    "update_governance_policy": ("policy_id", "knowledge_base_id"),
    "delete_governance_policy": ("policy_id", "knowledge_base_id"),
    "list_governance_audit": ("knowledge_base_id",),
    "evaluate_governance_policy": ("knowledge_base_id", "data_source_id", "table"),
    # ── 本体实例 → 可执行工具绑定 / 执行前解析 ──
    "list_tool_bindings": ("knowledge_base_id",),
    "list_bindable_tools": ("knowledge_base_id",),
    "create_tool_binding": ("knowledge_base_id", "entity_type", "canonical_name", "tool_name"),
    "update_tool_binding": ("binding_id",),
    "delete_tool_binding": ("binding_id",),
    "resolve_action_execution": ("knowledge_base_id", "action_name"),
    # 执行器代真实用户取执行计划（按知识库写权限判定，无用户身份一律拒绝）
    "prepare_action_execution": ("knowledge_base_id", "action_name"),
    # ── 知识库工具（KB 级工具目录）──
    "list_kb_tools": ("knowledge_base_id",),
    "create_kb_tool": ("knowledge_base_id", "tool_name", "name"),
    "update_kb_tool": ("tool_id",),
    "delete_kb_tool": ("tool_id",),
    "test_kb_tool": ("tool_id",),
    # 编辑对象弹窗试跑抽取期工具声明（真实调用一次工具，不写图）
    "preview_extract_tool": ("knowledge_base_id", "entity_type", "group", "declaration"),
    # 内部：本体动作执行器调用本库工具（无 Gateway 路由，带用户身份的调用一律拒绝）
    "run_kb_tool": ("knowledge_base_id", "tool_name"),
    # ── 知识库信息管理 ──
    "list_knowledge_info": (),
    "get_quota_view": (),  # [jonex] 系统配额查询（无空间语义）
    "get_kb_type_options": (),  # [jonex] 本部署可创建的 KB 类型（无空间语义、无入参）
    "create_knowledge_info": (),
    "get_knowledge_info": ("kb_id",),
    "update_knowledge_info": ("kb_id",),
    "delete_knowledge_info": ("kb_id",),
    "get_kb_permissions": ("kb_id",),
    "set_kb_permissions": ("kb_id",),
    "get_kb_permission_candidates": ("kb_id",),
    # ── 领域空间 ──
    "list_spaces": (),
    "create_space": ("name",),
    "get_space": ("space_id",),
    "update_space": ("space_id",),
    "delete_space": ("space_id",),
    "get_space_permissions": ("space_id",),
    "set_space_permissions": ("space_id",),
    "get_space_permission_candidates": ("space_id",),
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
    # 引用原文坐标（bbox 定位；chunk_index/expect 可选，缺了服务层降级为只跳页）
    "get_document_source_spans": ("document_id",),
    # [jonex] 第2期 · 任务5/6：数据治理血缘读取（只读）
    "get_document_provenance": ("document_id", "knowledge_base_id"),
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
    "get_wiki_graph_types": ("knowledge_base_id",),
    "query_wiki_resources": ("knowledge_base_id", "operation"),
    "list_wiki_scenarios": ("knowledge_base_id",),
    "read_wiki_scenario_resources": ("knowledge_base_id", "scenario_id"),
    "select_wiki_scenario_resources": ("knowledge_base_id", "scenario_id"),
    "read_wiki_scenario_materials": ("knowledge_base_id", "scenario_id"),
    "read_wiki_scenario_material": ("knowledge_base_id", "scenario_id"),
    "annotate_wiki_scenario": ("knowledge_base_id", "scenario_id"),
    "configure_wiki_scenario": ("knowledge_base_id", "scenario_id"),
    "run_wiki_scenario": ("knowledge_base_id", "scenario_id"),
    "review_wiki_scenario": ("knowledge_base_id", "scenario_id"),
    "search_wiki_nodes": ("knowledge_base_id",),
    "get_wiki_node": ("knowledge_base_id", "node_id", "graph_revision"),
    "get_wiki_neighbors": ("knowledge_base_id", "node_id", "graph_revision"),
    "get_wiki_graph_evidence": ("knowledge_base_id", "graph_revision"),
    "get_wiki_media": ("knowledge_base_id", "graph_revision", "document_id", "asset_id"),
    "expand_ontology_neighbors": ("knowledge_base_id", "entity_type", "canonical_name"),
    "list_ontology_instances": ("knowledge_base_id",),
    "list_ontology_relations": ("knowledge_base_id",),
    "list_ontology_entity_types": ("knowledge_base_id",),
    "list_ontology_relation_types": ("knowledge_base_id",),
    # ── 跨数据源抽象 schema（概念对齐层）──
    "get_abstract_schema": ("knowledge_base_id",),
    "generate_abstract_schema": ("knowledge_base_id",),
    "preview_focus_abstract_schema": ("knowledge_base_id", "prompt"),
    "construct_abstract_schema": ("knowledge_base_id",),
    # status 必填：缺省值在 save() 里是 "proposed"，漏传会把已 active 的对齐降级。
    "save_abstract_schema": ("knowledge_base_id", "schema", "status", "expected_schema_version"),
    # dry_run 是可选键（不在必填列）；正式导入还必须带 expected_schema_version，
    # 那道闸在服务层（首次导入传 0）。
    "import_abstract_schema": ("knowledge_base_id", "yaml_text"),
    # 保存历史（2026-10-27）：列表按 KB；详情/还原也要求 knowledge_base_id ——
    # 能力动作的空间判定靠它就够了（只给 snapshot_id 时 `_resolve_space_context`
    # 解析不出 KB，等于跳过空间权限）。还原另需 expected_schema_version（乐观锁）。
    "list_abstract_schema_snapshots": ("knowledge_base_id",),
    "get_abstract_schema_snapshot": ("knowledge_base_id", "snapshot_id"),
    "restore_abstract_schema_snapshot": (
        "knowledge_base_id", "snapshot_id", "expected_schema_version",
    ),
    # ── 搜索结果反馈 ──
    "submit_search_feedback": ("session_id", "query", "feedback_type", "knowledge_base_ids"),
    "cancel_search_feedback": ("session_id", "feedback_type", "knowledge_base_ids"),
    "list_search_feedback": ("knowledge_base_id",),
    "toggle_search_feedback_adopted": ("feedback_id",),
    "get_search_feedback_stats": ("knowledge_base_id",),
    # ── 回答级反馈（history_id 锚点）──
    "submit_answer_feedback": ("history_id", "operation_id", "version", "feedback_type"),
    "get_answer_feedback": ("history_id",),
    "list_answer_feedback": ("knowledge_base_id",),
    "get_answer_feedback_stats": ("knowledge_base_id",),
    "toggle_answer_feedback_adopted": ("feedback_id",),
    "delete_answer_feedback": ("feedback_id",),
    # ── MCP 检索反馈（request_id 锚点）──
    # 注意：这张表只是声明，execute 不做必填校验（validate_input 无调用方）；
    # 真正的必填校验在 AnswerFeedbackService.submit_mcp 内。
    "submit_mcp_feedback": ("request_id", "service_id", "feedback_type"),
    # ── 数据源接入 ──
    "list_data_sources": ("knowledge_base_id",),
    "get_data_source": ("ds_id",),
    "create_data_source": ("knowledge_base_id", "access_type", "name"),
    "update_data_source": ("ds_id",),
    "delete_data_source": ("ds_id",),
    "test_data_source": ("ds_id",),
    "sync_data_source": ("ds_id",),
    "reset_ingest_key": ("ds_id",),
    # ETL 来源的血缘边文件导入（JSON/CSV）；内容走 content_b64，文件名可选
    "import_lineage_file": ("ds_id",),
    # 数据库数据源：只读 catalog 出库表结构（表/列/主键/外键/注释）
    "read_data_source_catalog": ("ds_id",),
    # 由 catalog 生成**该数据源自己那份** schema（写操作）
    "generate_ds_schema": ("ds_id",),
    # 手工重建数据资产子图（P1.5a）：KB 必填，data_source_id 可选（只重建单源）
    "rebuild_ontology_data_assets": ("knowledge_base_id",),
    # 数据资产子图只读查询（P1.5a Task 4）：按 KB + 源取节点/边。
    # data_source_id 必填 —— 空串会让作用域查询退化成「同 KB 全部源」。
    "list_ontology_data_assets": ("knowledge_base_id", "data_source_id"),
    # ── 跨模态对齐（P1.5b）：文档实体类型 ↔ 数据源实体，人审 ──
    # 生成写库（proposed）；列举是读；审批按 alignment_id + KB 收口 ——
    # `knowledge_base_id` 同样必填，缺失时会去改/审同租户别的 KB 的对齐行。
    "suggest_ontology_alignments": ("knowledge_base_id",),
    "list_ontology_alignments": ("knowledge_base_id",),
    "set_ontology_alignment_status": ("alignment_id", "knowledge_base_id"),
    # ── ② NL→SQL 分源查询（一问一源一条 SQL，spec 2026-09-28）──
    # 只读（与 read_data_source_catalog 同级：都解密凭据连外部库）。
    # 会调一次 LLM —— 成本走 llm-gateway 计量，不靠权限位拦。
    "ask_data_source": ("knowledge_base_id", "question"),
    # 纯数据检索（/search/data）：router 数据路线的进程内实现。
    "search_data": ("query",),
    # 本体 + 数据（/search/ontology-data）：本体检索后按候选补 ②。
    "search_ontology_data": ("query",),
    # W4.1：确定性指标问数（不调 LLM，只读）。
    "ask_metric": ("knowledge_base_id", "question"),
    # W4.5：血缘检索（只读；词面召回 + 一跳血缘扩展）。
    "search_lineage_assets": ("knowledge_base_id", "query"),
    # W4.7：澄清池（只读、不执行）。
    "clarify_metric": ("knowledge_base_id", "question"),
    # W4.8：指标级调用审批（发布/下线/调用）。
    "list_metric_call_approvals": ("knowledge_base_id",),
    "request_metric_call_approval": ("knowledge_base_id", "metric_name"),
    "approve_metric_call": ("knowledge_base_id", "metric_name"),
    "suspend_metric_call": ("knowledge_base_id", "metric_name"),
    # ── 领域级连接池（按 space_id 隔离，主键是 conn_id）──
    "list_ds_connections": ("space_id",),
    "create_ds_connection": ("space_id", "name", "config_json"),
    "get_ds_connection": ("conn_id", "space_id"),
    "update_ds_connection": ("conn_id", "space_id"),
    "delete_ds_connection": ("conn_id", "space_id"),
    "test_ds_connection": ("conn_id", "space_id"),
    "read_ds_connection_catalog": ("conn_id", "space_id"),
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
    # ── KB 级模型配置（解析/编译阶段的模型覆盖）──
    "list_model_settings": ("knowledge_base_id",),
    "save_model_settings": ("knowledge_base_id", "items"),
    "ingest_push": ("ds_id", "ingest_key", "storage_key", "file_name"),
    # ETL OpenLineage 事件（内部 action，event 体可为空 ⇒ 只查 ds_id）
    "ingest_lineage_event": ("ds_id",),
    # ── 演化台服务化（②a）：runs / ledgers / corpus ──
    # 全部租户管理员专属（execute 里 `_require_evolution_admin` 单独判），
    # 因而没有空间/KB 语义 —— 进 `_SPACE_EXEMPT_ACTIONS`，KB 由各自动作校验。
    "evolution_status": (),
    "evolution_list_runs": (),
    "evolution_get_run": ("run_id",),
    "evolution_run_log": ("run_id",),
    # 信任闭环 P0（Task 7）：版本查询。`kb` 静态必填；scope/ds/分页由 handler 收口。
    "evolution_list_versions": ("kb",),
    "evolution_get_version": ("version_id",),
    # kind = alias / query_rule / relation / constraint / mapping / tool_schema；
    # compiled 家族（query_rule / relation / constraint）还必须带 ds_id，
    # abstract 家族（alias / mapping / tool_schema）不接受 ds
    # （单跑时执行侧校验，因为「带不带 ds」取决于 kind，
    # 不是静态必填）。
    "evolution_start_run": ("kind", "kb"),
    "evolution_list_ledgers": (),
    "evolution_list_corpus_cases": (),
    # 语料创建：服务层直接取 data["question"] / ["kb_id"] / ["ds_id"]，
    # 缺键会变成 500，所以在必填表里登记。
    "evolution_create_corpus_case": (
        "name", "question", "expect", "kb_id", "ds_id"),
    "evolution_update_corpus_case": ("case_id",),
    "evolution_delete_corpus_case": ("case_id",),
    # P1-2 Task 1：ad-hoc 验证通过后提升为语料。只按 run 定位；`case_ids`
    # 可选（缺省=全部 verified），形状校验在服务层（1001）。
    "evolution_promote_adhoc": ("run_id",),
    # 人审写操作（Task 8b）：全部按 run_id 定位；条目调整再按 entry_key 定位。
    # `confirm` / `picks` / `alias` / `expression` 由 handler 或服务层校验。
    "evolution_adopt_run": ("run_id",),
    # 人工 ad-hoc 验真（Task 4）：run + case 定位；`picks` 可选（形状由
    # `_evolution_picks` 收口，非法 1001）。
    "evolution_adhoc_evidence": ("run_id", "case_id"),
    # 批量采纳（P1）：`run_ids` 形状/去重/上限/同 KB/每链一个由服务层校验
    # （`services/evolution/batch_adopt.py`）；override_reason / adhoc_truths 可选。
    "evolution_adopt_runs": ("run_ids",),
    "evolution_reject_run": ("run_id",),
    "evolution_rollback_run": ("run_id",),
    "evolution_adjust_entry": ("run_id", "entry_key"),
    "evolution_edit_entry": ("run_id", "entry_key"),
    # ②b 诊断（2026-10-18）：窗口由 `days` 决定（默认 7，入口收口 1..365），
    # 三个动作都没有静态必填字段 —— 窗口是唯一的输入且可缺省。
    "evolution_list_failures": (),
    "evolution_run_diagnosis": (),
    "evolution_latest_diagnosis": (),
    # ②b 点踩诊断 / 抽象层选项 / 采纳（Task 3）：写操作的必填字段全部登记；
    # `days` 可缺省（默认 30），由 handler 收口。
    "evolution_list_dislikes": (),
    "evolution_dislike_context": ("feedback_id",),
    "evolution_replay_dislike": ("feedback_id",),
    "evolution_adopt_dislike_alias": ("feedback_id", "concept", "alias"),
    "evolution_metric_options": ("kb",),
    "evolution_concept_options": ("kb",),
    # 演化台 `tool_schema`（D3）：多轮工具名单（代码默认 ⊕ active 覆盖）
    "evolution_tool_options": ("kb",),
    "evolution_adopt_metric": ("id", "metric", "alias", "kb"),
    # Task 9：采纳/基准指标（只读，窗口 `days` 可选）
    "evolution_metrics": (),
    # 语料治理 Task 5：覆盖率（租户级只读列表，无必填字段）
    "evolution_corpus_coverage": (),
    # 语料治理 Task 6：自动播种。`mode=schema|cases`；`cases` 仅 cases 模式读取，
    # `dry_run` 可选（只算不写）。
    "evolution_seed_corpus": ("kb", "ds", "mode"),
    # ── MCP 文档上传状态查询 ──
    "get_document_status": ("document_id",),
    # [jonex] B6 §5.2：网关 view-ticket 的只读校验口（只收 document_id、不回正文）
    "assert_document_readable": ("document_id",),
    # ── 数据运营（Data Operations）──
    "get_data_operations_overview": (),
    "get_data_assets_analytics": (),
    "get_knowledge_assets_analytics": (),
    "get_search_analytics": (),
    # 异常资产下钻（分页/筛选，全部可选，故必填字段为空）
    "get_abnormal_assets_analytics": (),
    # 解析质量异常（域空间/知识库筛选，全部可选）
    "get_quality_anomalies": (),
    # ── 任务中心接入（运行方**三个**入口：execute / remove_task / task_stopped，
    #    由 `dispatch.update(runner.bind())` 装配）──
    # ⚠️ **`execute` 是六个键，别漏 `node_id`**：`ExecutionEnvelope` 的字段是
    # `tenant_id / space_id / task_id / step_code / node_id / execution_id`。
    # 这几个键确实落在 `payload.data` 这一层（任务中心 `flows/runner_adapter.py`
    # 就是从 `data.get("tenant_id")` 取的），所以放在这里校验是对的层。
    "execute": ("tenant_id", "space_id", "task_id", "step_code", "node_id", "execution_id"),
    "remove_task": ("tenant_id", "space_id", "task_id"),
    # §4.1 ③ 那条**通知**（2026-09-30 加）：字段与 `remove_task` 逐字相同（同一份定位键）。
    "task_stopped": ("tenant_id", "space_id", "task_id"),
    # ── 数据运营 · 列表钻取详情（三期，原型「点名称进抽屉」）──
    # 定位键是**真实键**，不是列表里展示用的 id：本体那个 `object_id` 是
    # sha256(tenant|kb|type|name) 的单向哈希，反解不回三元组，所以接口 B 收三元组。
    "get_data_asset_detail": ("asset_id",),
    "get_data_ontology_entity_detail": ("kb_id", "entity_type", "canonical_name"),
    "get_data_kb_detail": ("kb_id",),
    "get_data_service_detail": ("service_id",),
    # §4.1 ④：管理方主动问「这一步现在什么情况」。入参就是 `ExecutionEnvelope`
    # （派活报文去掉 `inputs`），所以必填集与它一致。
    "query_step_status": (
        "tenant_id", "space_id", "task_id", "step_code", "node_id", "execution_id",
    ),
}

#: 结构化数据支持闸门：KB 开关关闭时这些 action 一律拒绝（1004）。
#: ⚠️ `ask_data_source` **不在此列** —— 它走软失败（见 Nl2SqlService），
#: 否则 ③ 的多源扇出会被一条 4xx 打断。
#: ⚠️ compiled-schema 系列（get/save/reseed/recompile/export/import）**也不在此列**：
#: 它们是「编译结果」页签（文档本体对象/关系/约束）的支撑动作，不属于数据源
#: 结构化能力；Schema 页签（`schema`）仍由前端按开关隐藏。
#: ⚠️ **数据源管理也不在此列**（list/get/create/update/delete/test/sync/
#: reset_ingest_key 与 `ingest_push`）：接数据（文件上传 / API 拉取 / 对象存储 /
#: 入站推送）是知识库的基础能力，前端「数据源」页签恒定展示。闸门只管
#: **结构化派生能力**：catalog 读取、schema 生成、血缘/ETL、治理、②。
_STRUCTURED_ACTIONS = frozenset({
    # 数据源的结构化派生能力（源连接本身不设闸）
    "import_lineage_file", "read_data_source_catalog", "generate_ds_schema",
    "ingest_lineage_event",
    # 抽象 schema / 对齐 / 数据资产 / 路径
    "get_abstract_schema", "generate_abstract_schema",
    "preview_focus_abstract_schema", "construct_abstract_schema",
    "save_abstract_schema", "import_abstract_schema",
    "list_abstract_schema_snapshots", "get_abstract_schema_snapshot",
    "restore_abstract_schema_snapshot",
    "rebuild_ontology_data_assets", "list_ontology_data_assets",
    "suggest_ontology_alignments", "list_ontology_alignments",
    "set_ontology_alignment_status", "list_lineage_paths",
    "analyze_lineage_impact", "get_lineage_coverage",
    # 血缘边 / 禁止同查
    "list_lineage_edges", "create_lineage_edge", "update_lineage_edge",
    "delete_lineage_edge", "list_lineage_forbidden_pairs",
    "create_lineage_forbidden_pair", "update_lineage_forbidden_pair",
    "delete_lineage_forbidden_pair", "suggest_lineage_forbidden_pairs",
    # 治理
    "list_governance_labels", "create_governance_label",
    "update_governance_label", "delete_governance_label",
    "list_governance_tags", "create_governance_tag",
    "update_governance_tag", "delete_governance_tag",
    "list_governance_clearance", "set_governance_clearance",
    "list_governance_policies", "create_governance_policy",
    "update_governance_policy", "delete_governance_policy",
    "list_governance_audit", "evaluate_governance_policy",
    # W2.7a 敏感扫描（结构化库能力）
    "governance_sensitive_scan", "governance_adopt_sensitive_scan",
})


async def _is_structured_enabled(session, tenant_id: str, kb_id: str) -> bool:
    row = (await session.execute(
        text("SELECT structured_enabled FROM knowledge_base.knowledge_info "
             "WHERE id=:kb AND tenant_id=:t AND is_deleted=0"),
        {"kb": kb_id, "t": tenant_id},
    )).first()
    return bool(row and row[0])


async def _kb_id_for_data_source(session, tenant_id: str, ds_id: str) -> str:
    row = (await session.execute(
        text("SELECT knowledge_base_id FROM knowledge_base.knowledge_data_sources "
             "WHERE id=:ds AND tenant_id=:t AND is_deleted=0"),
        {"ds": ds_id, "t": tenant_id},
    )).first()
    return str(row[0]) if row else ""


# 演化台服务化（②a）：动作清单。全部是**租户管理员专属**，由 execute() 在鉴权后
# 统一过 `_require_evolution_admin`；它们不绑任何知识库/空间，因而同时进
# `_SPACE_EXEMPT_ACTIONS`（避免 `_resolve_space_context` 去默认 KB 分支找键）。
# ⚠️ 新动作只加进下面这个集合、忘了把 handler 接进 dispatch = 落进「不支持的操作」；
# 反过来只在 dispatch 加而不进本集合 = **管理员闸门静默失效**。
_EVOLUTION_ACTIONS = frozenset({
    "evolution_status",
    "evolution_list_runs",
    "evolution_get_run",
    "evolution_run_log",
    # 信任闭环 P0（Task 7）：版本查询（只读，仍沿用管理员闸门）
    "evolution_list_versions",
    "evolution_get_version",
    "evolution_start_run",
    "evolution_list_ledgers",
    "evolution_list_corpus_cases",
    "evolution_create_corpus_case",
    "evolution_update_corpus_case",
    "evolution_delete_corpus_case",
    # P1-2 Task 1：ad-hoc → 语料（写操作，仍沿用管理员闸门）
    "evolution_promote_adhoc",
    # Task 8b：采纳 / 否决 / 回滚 / 条目调整
    "evolution_adopt_run",
    # 人工 ad-hoc 验真（Task 4）：现场试跑取证据（只读辅助，不写台账）
    "evolution_adhoc_evidence",
    # P1：批量采纳（同 KB、每链一个 run）
    "evolution_adopt_runs",
    "evolution_reject_run",
    "evolution_rollback_run",
    "evolution_adjust_entry",
    "evolution_edit_entry",
    # ②b 诊断：失败观测清单 / 跑诊断 / 最新报告（全部租户管理员）
    "evolution_list_failures",
    "evolution_run_diagnosis",
    "evolution_latest_diagnosis",
    # ②b 点踩诊断 / 抽象层选项 / 采纳（Task 3）
    "evolution_list_dislikes",
    "evolution_dislike_context",
    "evolution_replay_dislike",
    "evolution_adopt_dislike_alias",
    "evolution_metric_options",
    "evolution_concept_options",
    # 演化台 `tool_schema`（D3）：多轮工具名单（只读，KB 管理员可用）
    "evolution_tool_options",
    "evolution_adopt_metric",
    # Task 9：采纳/基准指标（只读，仍沿用管理员闸门）
    "evolution_metrics",
    # 语料治理 Task 5：覆盖率（只读，仍沿用管理员闸门）
    "evolution_corpus_coverage",
    # 语料治理 Task 6：自动播种（写操作，仍沿用管理员闸门）
    "evolution_seed_corpus",
})

#: 演化台 **KB 范围**动作：租户管理员之外，**目标 KB 的 `kb_manager`** 也可执行。
#: 名单见 docs/superpowers/specs/2026-10-26-evolution-kb-scope-batch-payload-design.md §D1。
#: 其余演化动作（status / ledgers / failures / 诊断 / 点踩 / metrics）没有 KB 维度或
#: 是跨 KB 聚合，v1 仍仅租户管理员。
#: ⚠️ 设计稿写的 `evolution_corpus_seed` 是笔误，实际动作名是 `evolution_seed_corpus`。
_EVOLUTION_KB_SCOPED_ACTIONS = frozenset({
    "evolution_start_run",
    "evolution_get_run",
    "evolution_run_log",
    "evolution_list_runs",
    "evolution_adopt_run",
    "evolution_adhoc_evidence",
    # P1：批量采纳按 run_ids 解析目标 KB（缺任意一条 run → 1002）
    "evolution_adopt_runs",
    "evolution_reject_run",
    "evolution_rollback_run",
    "evolution_adjust_entry",
    "evolution_edit_entry",
    "evolution_promote_adhoc",
    "evolution_list_versions",
    "evolution_get_version",
    "evolution_metric_options",
    "evolution_concept_options",
    "evolution_tool_options",
    "evolution_adopt_metric",
    "evolution_list_corpus_cases",
    "evolution_create_corpus_case",
    "evolution_update_corpus_case",
    "evolution_delete_corpus_case",
    "evolution_corpus_coverage",
    "evolution_seed_corpus",
})

#: KB 范围内**没有显式目标**也允许的列表动作：结果集由 handler 按授权 KB 过滤
#: （`_evolution_allowed_kb_ids`；读路径过滤已实现）。
_EVOLUTION_KB_LIST_ACTIONS = frozenset({
    "evolution_list_runs",
    "evolution_list_corpus_cases",
    "evolution_corpus_coverage",
})

#: 每个 KB 范围动作的**权威目标键**。解析只认这一个键，body 里多带的其它 id 一律忽略：
#: 这既是语义（handler 用哪个键定位），也是安全边界 —— 否则 KB 管理员给
#: `update_corpus_case` 多塞一个 `kb_id=<自己的 KB>` 就能把「按 case_id 定位」的
#: 写操作盖成自己管理的 KB（设计 §4 失败模式「KB 管理员越权写语料到别的 KB」）。
#: kb 类动作的值就是**闸门解析用的键**，必须与 handler 实际读取的键一致：
#: `create_corpus_case` 的 handler 只读 `kb_id`（不是 `kb`），故单独标成 `kb_id`。
#: ⚠️ 新动作进 `_EVOLUTION_KB_SCOPED_ACTIONS` 时必须在这里归类，否则解析不出目标、
#: 恒 403（`test_every_kb_scoped_action_has_target_kind` 钉住）。
_EVOLUTION_KB_TARGET_KIND = {
    "evolution_start_run": "kb",
    "evolution_list_runs": "kb",  # 可选；缺省交给 handler 过滤
    "evolution_list_versions": "kb",
    "evolution_metric_options": "kb",
    "evolution_concept_options": "kb",
    "evolution_tool_options": "kb",
    "evolution_adopt_metric": "kb",
    # handler/`CorpusService._build_row` 只读 `kb_id`：闸门必须绑同一个键，
    # 否则 `{"kb": 自己的, "kb_id": 别人的}` 会「闸门看 kb、写入看 kb_id」。
    "evolution_create_corpus_case": "kb_id",
    "evolution_seed_corpus": "kb",
    "evolution_list_corpus_cases": "kb",  # 可选
    "evolution_corpus_coverage": "kb",  # 可选
    # 按 run 定位
    "evolution_get_run": "run_id",
    "evolution_run_log": "run_id",
    "evolution_adopt_run": "run_id",
    "evolution_adhoc_evidence": "run_id",
    "evolution_reject_run": "run_id",
    "evolution_rollback_run": "run_id",
    "evolution_adjust_entry": "run_id",
    "evolution_edit_entry": "run_id",
    "evolution_promote_adhoc": "run_id",
    # 批量采纳（P1）：闸门按所有 run_ids 的 KB 集合判定（必须全部落在授权集合内）
    "evolution_adopt_runs": "run_ids",
    # 按语料 case 定位（写操作：绝不能信 body 里的 kb 键）
    "evolution_update_corpus_case": "case_id",
    "evolution_delete_corpus_case": "case_id",
    # 按语义版本定位（活跃表 ∪ 归档表）
    "evolution_get_version": "version_id",
}

#: body 里所有可能表示「知识库」的键。同时出现多个**不同**值 → 1001（不允许
#: 任何键静默胜出：闸门与 handler 各读一个键时，歧义就是一条越权通道）。
_EVOLUTION_KB_LIKE_KEYS = ("kb", "kb_id", "knowledge_base_id")

#: kb 类动作**闸门实际解析的键**（默认 = `_EVOLUTION_KB_TARGET_KIND` 里的那个）。
#: 例外：`evolution_list_runs` 的 handler 同时认 `kb` 与 `knowledge_base_id`。
_EVOLUTION_KB_ACTION_KEYS: dict[str, tuple[str, ...]] = {
    "evolution_list_runs": ("kb", "knowledge_base_id"),
}


def _evolution_kb_target_keys(action: str) -> tuple[str, ...]:
    """该动作闸门解析用的 kb 键（与 handler 读的键同口径）。"""
    kind = _EVOLUTION_KB_TARGET_KIND.get(action, "")
    return _EVOLUTION_KB_ACTION_KEYS.get(action, (kind,))


def _evolution_explicit_kb(
    data: dict | None, keys: tuple[str, ...] = ("kb",)
) -> str:
    """读取动作的权威 kb 键；body 里互相冲突的 kb-like 键一律 1001。

    `keys` 必须与 handler 实际读取的键一致（见 `_EVOLUTION_KB_TARGET_KIND` /
    `_EVOLUTION_KB_ACTION_KEYS`）。冲突检测覆盖**全部**三个别名，而不只是
    `keys`：`{"kb": 自己的, "kb_id": 别人的}` 这种 body 在语义上就是歧义的，
    不能靠「闸门读哪个键」决定谁赢。
    """
    data = data or {}
    provided = {
        name: str(data.get(name) or "").strip()
        for name in _EVOLUTION_KB_LIKE_KEYS
        if str(data.get(name) or "").strip()
    }
    if len(set(provided.values())) > 1:
        raise InvalidParameterError(
            message="知识库标识冲突（kb / kb_id / knowledge_base_id 必须一致）："
                    + "、".join(f"{k}={v}" for k, v in provided.items()))
    for key in keys:
        if key in provided:
            return provided[key]
    return ""


async def _assert_evolution_ds_in_kb(
    session, tenant_id: str, kb_id: str, ds_id: str
) -> None:
    """语料写入口的归属校验：数据源必须存在且属于目标 KB（忽略软删）。

    KB 管理员闸门只校验过 KB；`ds_id` 是 body 里自由填的，不校验就能把语料
    挂到**别的 KB 的**数据源上（播种同样）。不存在 / 跨 KB / 软删 → 1001。
    """
    if not kb_id or not ds_id:
        raise InvalidParameterError(message="数据源不属于该知识库")
    row = (await session.execute(
        text("SELECT 1 FROM knowledge_base.knowledge_data_sources "
             "WHERE tenant_id=:t AND knowledge_base_id=:kb AND id=:ds "
             "AND is_deleted=0"),
        {"t": tenant_id, "kb": kb_id, "ds": ds_id},
    )).first()
    if not row:
        raise InvalidParameterError(message="数据源不属于该知识库")


async def _evolution_target_kb_ids(
    action: str, data: dict | None, *, tenant_id: str
) -> set[str] | None:
    """解析动作的目标 KB 集合；`None` = 无显式目标的列表动作（交给 handler 过滤）。

    ⚠️ 调用方（`_require_evolution_admin`）**必须先确认调用方至少管理一个 KB**
    再进这里：否则 run/case/version id 会变成「这个 KB 里有没有这个东西」的
    存在性探测器（设计 §D1 失败模式）。
    ⚠️ 查无行沿用各动作原有 1002 语义（`ResourceNotFoundError`），不是 403 ——
    能走到这里的调用方已经有权管理某个 KB，缺的只是目标本身。
    """
    data = data or {}
    kind = _EVOLUTION_KB_TARGET_KIND.get(action, "")

    if kind in _EVOLUTION_KB_LIKE_KEYS:
        explicit = _evolution_explicit_kb(data, _evolution_kb_target_keys(action))
        if explicit:
            return {explicit}
        # 列表动作没有显式目标：本层不判，handler 用授权集合过滤结果集。
        return None if action in _EVOLUTION_KB_LIST_ACTIONS else set()
    if kind not in ("run_id", "case_id", "version_id", "run_ids"):
        # 未归类的动作 / 畸形调用：fail-closed（新动作忘了归类也走这里）。
        return set()

    run_id = str(data.get("run_id") or "").strip() if kind == "run_id" else ""
    case_id = str(data.get("case_id") or "").strip() if kind == "case_id" else ""
    version_id = (
        str(data.get("version_id") or "").strip()
        if kind == "version_id" else ""
    )
    run_ids: list[str] = []
    if kind == "run_ids":
        raw_run_ids = data.get("run_ids")
        if isinstance(raw_run_ids, (list, tuple, set)):
            run_ids = list(dict.fromkeys(
                str(item).strip() for item in raw_run_ids if str(item).strip()
            ))
        # 与服务层上限同口径：超长列表在发 `IN (...)` 之前就 1001，别把请求
        # 放大成一条巨查询再让 handler 拒。上限常量从 `batch_adopt` 延迟导入
        # （同一条事实只留一处定义，避免两处漂移）。
        from .services.evolution.batch_adopt import MAX_BATCH_RUNS

        if len(run_ids) > MAX_BATCH_RUNS:
            raise InvalidParameterError(
                message=f"批量采纳一次最多 {MAX_BATCH_RUNS} 条")

    if not (run_id or case_id or version_id or run_ids):
        # 定位键缺失（畸形 body）：非列表动作 fail-closed 403。
        return set()

    async with get_db_session() as session:
        if run_id:
            row = (await session.execute(
                text("SELECT knowledge_base_id FROM knowledge_base.evolution_runs "
                     "WHERE tenant_id=:t AND id=:r"),
                {"t": tenant_id, "r": run_id},
            )).first()
            if not row or not row[0]:
                raise ResourceNotFoundError(message="演化运行不存在")
            return {str(row[0])}

        if case_id:
            row = (await session.execute(
                text("SELECT kb_id FROM knowledge_base.evolution_corpus_cases "
                     "WHERE tenant_id=:t AND id=:c"),
                {"t": tenant_id, "c": case_id},
            )).first()
            if not row or not row[0]:
                raise ResourceNotFoundError(message="演化语料不存在")
            return {str(row[0])}

        if version_id:
            # 版本归档后仍在历史里可查：活跃表 ∪ 归档表。
            row = (await session.execute(
                text("SELECT knowledge_base_id FROM knowledge_base.semantic_versions "
                     "WHERE tenant_id=:t AND id=:v "
                     "UNION "
                     "SELECT knowledge_base_id FROM knowledge_base.semantic_versions_archive "
                     "WHERE tenant_id=:t AND id=:v"),
                {"t": tenant_id, "v": version_id},
            )).first()
            if not row or not row[0]:
                raise ResourceNotFoundError(message="语义版本不存在")
            return {str(row[0])}

        # 批量（≤10）：一条 IN 查完；缺任意一个 id → 1002。
        rows = (await session.execute(
            text("SELECT knowledge_base_id FROM knowledge_base.evolution_runs "
                 "WHERE tenant_id=:t AND id IN :ids").bindparams(
                     bindparam("ids", expanding=True)),
            {"t": tenant_id, "ids": run_ids},
        )).all()
        if len(rows) != len(run_ids):
            raise ResourceNotFoundError(message="演化运行不存在")
        return {str(row[0]) for row in rows if row and row[0]}


async def _evolution_allowed_kb_ids(
    request: CapabilityRequest,
) -> list[str] | None:
    """调用方可操作的 KB 集合：租户管理员 `None`（不过滤），否则 `kb_manager` 授权列表。

    空列表 = 一个 KB 都管不了（读路径应返回空结果，而不是放开）。
    """
    from .services.kb_permission_service import get_granted_manager_kb_ids
    from .services.space_permission_service import is_tenant_admin

    if await is_tenant_admin(request.tenant_id, request.user_id):
        return None
    granted = await get_granted_manager_kb_ids(request.tenant_id, request.user_id)
    return list(granted)

# ══════════════════════════════════════════════════════════════
# [jonex] 空间权限判定分组（设计 docs/superpowers/specs/2026-08-19-space-permission-design.md §3）
#
# 归类完备性由 tests/unit/test_kb_action_permissions.py 的
# test_every_action_is_reachable_by_some_gate 保证：dispatch 里每条 action 都必须落在
# 下面这些集合之一 —— 一条都不落 = 未分类，`execute()` 那串 if/elif 全不命中，
# 解析出 space_id 时**一个校验都不做**（「六组」是当年的说法，现在集合比六张多，
# 判据以那条测试实际遍历的清单为准）。
# ══════════════════════════════════════════════════════════════

# 豁免（不判定）：
# - list_spaces / get_space：判定在 service 层（成员过滤落 SQL / 非成员 404）
# - create_space：纯豁免——校验完全下沉 service（owner 注入 + service:write，双入口都覆盖）
# - 历史/反馈：已按 _user_id(r) 用户级隔离；ingest_push / ingest_lineage_event：内部链路；
#   resolve_references：无空间语义
_SPACE_EXEMPT_ACTIONS = frozenset({
    "list_spaces", "create_space", "get_space",
    "get_search_overview", "list_search_history", "save_search_history",
    "delete_search_history", "clear_search_history",
    # [jonex] 以图问知识：三个都是**用户级**隔离（图片按 tenant+user 存与查，D10），
    # 没有任何空间语义 —— 判据全在 service 层（归不归本人 / 还有没有引用）。
    # ⚠️ **不能进 `_SEARCH_ACTIONS`**：那是「过滤无权限 KB」的分组，而这三个
    # action 压根不读任何 KB 数据（D8 明确 `image_to_query` 不接收 KB 范围）。
    # 塞进去会让 `_filter_search_kbs` 拿到空 kb 列表白跑一趟，还会触发检索埋点
    # （`_record_search_request` 认 `_SEARCH_ACTIONS`）—— 识别不是检索行为，
    # 计进去会把「检索次数」这个指标污染掉。
    "image_to_query", "get_query_image_ticket", "get_query_image_location",
    "submit_search_feedback", "cancel_search_feedback",
    "list_search_feedback", "toggle_search_feedback_adopted",
    "get_search_feedback_stats",
    "submit_answer_feedback", "get_answer_feedback",
    "list_answer_feedback", "get_answer_feedback_stats",
    "toggle_answer_feedback_adopted", "delete_answer_feedback",
    "submit_mcp_feedback",
    "ingest_push",
    "ingest_lineage_event",
    # [jonex] B6 §5.2：view-ticket 的文档可读校验口。它**自己**在 service 层做
    # KB 空间/KB 授权读权限判定（has_space_role / get_kb_grant_role），没有空间语义
    # 可供 capability 统一判定；密级过滤仍由 classification_access_guard 承担。
    "assert_document_readable",
    # 流式检索埋点：不读 KB 内容，权限已由 assert_kb_searchable 预检过；
    # 再走空间判定会因 action 无空间语义而误拒。
    "record_stream_search",
    "resolve_references", "poll_jonex_wiki_search", "cancel_jonex_wiki_search",
    "get_quota_view",
    # [jonex] KB_ENABLED_TYPES 的下发口：报文体里没有任何 id（没有 space_id、没有 kb_id），
    # 走常规判定只会「解析不到空间、什么也不判」。豁免是把这份「不判」写成意图而不是巧合
    # —— 与 get_quota_view 同款：部署级只读事实，与「是不是这个空间的成员」无关。
    # ⚠ 它不敏感（就是一张 UI 选项清单），所以不另设权限码；真正的闸门在 create 那一侧。
    "get_kb_type_options",
    # [jonex] 任务中心接入的两个入口：**空间成员判定对它们无意义，写死豁免**。
    # 派活/删除是**内部调用**（管理方发出、没有终端用户在场），`actor` 常常是空的 ——
    # 真跑一遍空间判定只会以「调用方不是这个空间的成员」把每一次派活 403 掉。
    # 报文体里也确实没有可判的东西：`knowledge_base_id` 在 `inputs` 里（派活报文的
    # 顶层只有 tenant_id / space_id / task_id / step_code / node_id / execution_id），
    # 而 `_resolve_space_context` 只认顶层那个键 —— 所以今天走常规判定会**解析不到空间、
    # 什么也不判**。豁免是把这份「不判」写成意图而不是巧合：哪天有人把 KB id 提到报文
    # 顶层，判定会突然开始生效、而且是以一个与业务无关的理由（调用方不是空间成员）。
    # ⚠️ **绝不能改放进 `_INTERNAL_ACTIONS`**（那条路跳过的是**租户闸**）：派活报文带的
    # 正是必须走 `require_tenant()` 收口的可信租户，跳过它等于放弃租户隔离 —— 两件事
    # 别混：这里豁免的是「是不是这个空间的成员」，不是「是不是这个租户的」。
    "execute", "remove_task", "task_stopped", "query_step_status",
    # [jonex] 模板场景联动（标准侧→知识库）：business_domain 发布/变更场景后经
    # Sidecar invoke 调用，报文只有 `scenario_id`、没有用户身份，也解析不出空间。
    # 与 execute/remove_task 同款：豁免的是「是不是这个空间的成员」，**不是租户闸**。
    "mark_template_scenario_published", "list_impacted_knowledge_bases",
}) | _EVOLUTION_ACTIONS

# 空间级管理：仅 owner 或租户管理员（tenant:write）。
# 显式声明，不得落入前缀推导的 write 语义（manager 删空间必须 403）。
_SPACE_LEVEL_ACTIONS = frozenset({
    "update_space", "delete_space", "set_space_permissions",
    "get_space_permission_candidates",
})

# [jonex] 禁用空间下仍允许的动作：它们操作的是**空间自身**，不是空间内的业务数据。
# - update_space：必须放行，否则无法把 status 改回 active（禁用即永久锁死）
# - delete_space：管理员清理禁用空间
# - 权限三件套：查看/设置成员、拉候选（与 update/delete 同属「管理空间本身」）
#
# 其余能解析出 space_id 的业务 action 一律先过 require_space_active。
# ⚠ 这条豁免**不做租户管理员扩展** —— 「管理员可见但不可用」正是本次需求。
_SPACE_USABILITY_EXEMPT_ACTIONS = frozenset({
    "update_space", "delete_space",
    "get_space_permissions", "set_space_permissions",
    "get_space_permission_candidates",
})

# KB 权限管理（2026-08-20 设计）：仅空间 owner/manager 或租户管理员。
# 不能进 _SPACE_WRITE_ACTIONS（editor 授权兜底会放行）、不能进 _SPACE_LEVEL_ACTIONS
# （require_space_manage 口径是 owner/tenant_admin、不含 manager——比本组窄）。
_KB_MANAGE_ACTIONS = frozenset({
    "set_kb_permissions", "get_kb_permission_candidates",
})

# 服务内检索：空间成员 OR 该服务存在 accessible kb（判定与 KB 过滤合成一次查询）。
# 不能留在 _SERVICE_ID_ACTIONS —— 其解析 SQL 返回 kb_id=None，KB 授权兜底恒跳过，
# 纯 KB 授权用户会在 execute 层 403，与 scope="search" 列表可见口径矛盾。
_SERVICE_SEARCH_ACTIONS = frozenset({"search_service"})

# KB 授权兜底排除（设计 §2.3）：delete_knowledge_info 不适用 KB 授权——
# editor 授权不能删 KB（write 兜底对其不生效，空间判定照旧 owner/manager）
_KB_GRANT_EXCLUDED_ACTIONS = frozenset({
    "delete_knowledge_info",
})

# 空间内写（owner / manager）
_SPACE_WRITE_ACTIONS = frozenset({
    "configure_wiki_scenario", "run_wiki_scenario", "review_wiki_scenario", "annotate_wiki_scenario",
    "create_knowledge_info", "update_knowledge_info", "delete_knowledge_info",
    "upload_document", "delete_document", "reparse_document",
    "upload_document_version", "activate_document_version",
    "discard_document_version_upload",
    # 相似建议：忽略 / 合并为新版本（按空间写口径）
    "dismiss_document_similarity_suggestion", "merge_document_version",
    # 第 3 期生命周期：归档/恢复按空间写口径；销毁的租户管理员硬校验在 service 内
    "archive_document", "restore_document", "destroy_document",
    "set_document_folder", "batch_set_document_folder", "set_document_tags", "add_document_tag", "remove_document_tag",
    "scan_stale_chunks", "purge_stale_chunks", "generate_upload_url",
    "create_folder", "rename_folder", "delete_folder",
    # 领域服务写（2026-08-19 终版）：owner/manager 可增删改服务（与 KB/文档同口径）
    "create_service", "update_service", "delete_service",
    "update_service_configs", "enable_service", "disable_service",
    "create_service_api_key", "rotate_service_api_key", "delete_service_api_key",
    "create_data_source", "update_data_source", "delete_data_source",
    "sync_data_source", "test_data_source", "reset_ingest_key",
    # ETL 文件导入写血缘边（与 create_lineage_edge 同权限级）
    "import_lineage_file",
    # 生成会写 ontology_compiled_schemas，属写操作（与 read_data_source_catalog 的只读不同级）
    "generate_ds_schema",
    # 手工重建数据资产子图（写 Neo4j；data_source_id 可选，不影响空间解析）
    "rebuild_ontology_data_assets",
    # 跨模态对齐：建议生成与审批都写库（proposed / 状态流转），与血缘写同权限级
    "suggest_ontology_alignments", "set_ontology_alignment_status",
    "create_ontology_instance", "update_ontology_instance", "delete_ontology_instance",
    "create_ontology_relation", "update_ontology_relation", "delete_ontology_relation",
    "create_synonym", "update_synonym", "delete_synonym", "import_synonyms",
    # 血缘边逐条 CRUD：三个写动作都要进本表 —— 缺了不会报错，只会让空间写闸门
    # （execute() 的 `elif action in _SPACE_WRITE_ACTIONS`）收不到它们（静默不判权）。
    "create_lineage_edge", "update_lineage_edge", "delete_lineage_edge",
    # W4.8 指标调用审批：申请写库；审批/下线再叠服务内租户管理员校验。
    "request_metric_call_approval", "approve_metric_call",
    "suspend_metric_call",
    # 禁止同查表对：创建/状态流转/删除/生成建议都写库（建议也是写操作）
    "create_lineage_forbidden_pair", "update_lineage_forbidden_pair",
    "delete_lineage_forbidden_pair", "suggest_lineage_forbidden_pairs",
    # 治理 P1：标签/字典/密级/策略都写库（读动作不进本表）
    "create_governance_label", "update_governance_label", "delete_governance_label",
    # W2.7a：采纳扫描建议 = 写标签（与标签写同级）
    "governance_adopt_sensitive_scan",
    "create_governance_tag", "update_governance_tag", "delete_governance_tag",
    "set_governance_clearance",
    "create_governance_policy", "update_governance_policy", "delete_governance_policy",
    "create_tool_binding", "update_tool_binding", "delete_tool_binding",
    # 知识库工具：登记 / 改 / 删 / 试运行都按写口径（试运行会真的调对方接口）；
    # run_kb_tool 只给内部执行器用，放在写组里是兜底（带用户身份的调用在 dispatch 里已先拒绝）
    "create_kb_tool", "update_kb_tool", "delete_kb_tool", "test_kb_tool", "run_kb_tool",
    # 试跑抽取期工具声明：同试运行，会真的调对方接口
    "preview_extract_tool",
    # 执行本体动作前的计划解析：执行会真的调对方接口，所以按写口径判定调用人；
    # 无用户身份的调用在 dispatch 里先拒绝（_user_only），不会落到「无身份即跳过」的放行口
    "prepare_action_execution",
    "create_tag", "update_tag", "delete_tag",
    "create_parser_setting", "update_parser_setting", "delete_parser_setting",
    # KB 级模型配置：写与解析设置同口径（空间管理者/租户管理员/kb_manager 授权）
    "save_model_settings",
    "save_compiled_schema", "import_compiled_schema_yaml", "reseed_compiled_schema",
    # 抽象 schema：生成与确认都写库，与 save_compiled_schema 同级
    "generate_abstract_schema", "save_abstract_schema", "construct_abstract_schema",
    "import_abstract_schema",
    # 快照还原会走公开保存（写库 + 版本推进）；列表/详情是读动作，不进本表。
    "restore_abstract_schema_snapshot",
    # 聚焦解析虽不落库，但调 LLM 有成本，按写口径收（同 draft_wiki_outline 先例）
    "preview_focus_abstract_schema",
    "recompile_schema", "reextract_kb_documents", "retry_ontology_extract",
    # 更新模型：发起再解析调 LLM（有成本），导入草稿写 compiled schema —— 都按写口径
    "start_model_update", "apply_model_update",
    "save_llm_wiki_schema", "import_llm_wiki_schema_yaml",
    "apply_llm_wiki_schema", "recompile_llm_wiki_schema_outdated_documents",
    # [jonex] Wiki 阅读目录生成/调整（LLM 调用有成本，按写口径：KB 管理者/空间管理者）
    "draft_wiki_outline", "adjust_wiki_outline",
    "pause_wiki_compilation", "resume_wiki_compilation",
})

# [jonex] 权限重构 B4（方案 §5 Gap 8 / 执行文档 §5.4）：
# **KB「浏览」类读 action** —— 收紧到 KB 成员口径（空间管理者 / 租户管理员 / KB 授权成员），
# 空间 `member` 不再自动可见。这与 KB 列表/详情的收紧口径一致：
# 「能否浏览这个知识库」受 KB 成员身份约束。
#
# ⚠️ **不含检索→原文链路**（方案 D8：「能打开，只是看不到知识库」）：
#   get_raw_content / get_raw_url / get_chunk / get_document_chunks / get_raw_location
#   等仍留在 _SPACE_READ_ACTIONS，维持空间成员可见 —— 空间 member 检索到内容后
#   仍能点开原文。
# ⚠️ 也**不含**本体/schema/wiki/synonym 等既是管理页 tab、又被检索增强复用的读 action，
#   收紧它们会牵连检索体验，本批不动，留待业务确认（执行文档 §5.4 边界批）。
#
# 只收这些「纯浏览知识库」的入口，判据是**每个都能从 knowledge_base_id/kb_id
# 解析出 kb_id**（见 _resolve_space_context），否则 KB 授权兜底无从查起。
# `get_document_similarity_suggestions` 走 `_DOC_ID_ACTIONS`，同样带回 kb_id。
_KB_MEMBER_READ_ACTIONS = frozenset({
    "list_wiki_scenarios", "read_wiki_scenario_resources", "select_wiki_scenario_resources",
    "read_wiki_scenario_materials", "read_wiki_scenario_material",
    "list_documents", "documents_stats", "list_folders",
    "get_document_tags", "get_kb_permissions",
    # 相似建议读取：document_id 可经 _DOC_ID_ACTIONS 解析出 kb_id
    "get_document_similarity_suggestions",
})

# 空间内读（成员：owner / manager / viewer）
_SPACE_READ_ACTIONS = frozenset({
    # [jonex] B4：get_knowledge_info（详情）与 list_knowledge_info（列表）的 KB 成员
    # 收紧发生在 **service 层**（knowledge_info_service.get 404 / .list 过滤），
    # capability 层仍按空间成员放行 —— 空间隔离在这层，KB 收紧在下层，分两处是有意的：
    # capability 只认「你能不能进这个空间」，service 才认「这个空间里哪些 KB 归你」。
    "get_knowledge_info", "list_knowledge_info",
    "get_document",
    "get_document_status", "get_document_quality_report",
    "get_document_quality_reports",
    "get_document_provenance",  # [jonex] 第2期 · 任务5/6：数据治理血缘读取
    "get_document_chunks", "get_document_parse_result",
    "get_chunk", "get_raw_content", "get_raw_location", "get_raw_url",
    "get_asset_raw_location",  # [jonex] §image-refs P2-3
    "get_document_source_spans",  # 引用 bbox 定位（只读，成员可见）
    # 文档版本链（只读）：与 get_document / get_raw_url 同口径，成员可见
    "list_document_versions", "get_document_version_url",
    # 领域服务读：成员可见（viewer 可查看，不可操作——写归 _SPACE_LEVEL_ACTIONS）
    "get_service", "list_services", "get_service_configs", "list_service_api_keys",
    "get_data_source", "list_data_sources",
    # 读 catalog 是只读操作（只查 information_schema），与 get_data_source 同权限级
    "read_data_source_catalog",
    # 数据资产子图只读查询（P1.5a Task 4）：只读 Neo4j，与 read_data_source_catalog 同权限级
    "list_ontology_data_assets",
    # 跨模态对齐列举（P1.5b）：只读 PG 对齐行
    "list_ontology_alignments",
    # ② NL→SQL 问答：同样只读（只连只读账号、只跑一条 SELECT），
    # 与 read_data_source_catalog 同权限级 —— 见 _REQUIRED_FIELDS 那处注释。
    "ask_data_source",
    # W4.1 指标问数：只读、不调 LLM，与 ask_data_source 同权限级。
    "ask_metric",
    # W4.5 血缘检索：只读 schema + 血缘边，不连外部库。
    "search_lineage_assets",
    # W4.7 澄清池：只读 schema，不执行、不写库。
    "clarify_metric",
    # W4.8 审批列表：只读审批表。
    "list_metric_call_approvals",
    "list_ontology_instances", "list_ontology_relations",
    "list_ontology_entity_types", "list_ontology_relation_types",
    "get_ontology_graph", "get_ontology_statistics",
    "expand_ontology_neighbors", "search_ontology_entities",
    "get_compiled_schema", "export_compiled_schema_yaml", "export_llm_wiki_schema_yaml",
    # 更新模型任务状态：只读远端任务（dry-run 也只算不写），与 schema 读同级
    "get_model_update_status",
    # KB 级 schema 生命周期读取与重编译预览（只读，不触发自动编译）：与
    # get_compiled_schema 同权限级；预览只做 diff，不写库。
    "get_kb_compiled_schema_status", "preview_kb_compiled_schema_recompile",
    "get_abstract_schema",
    # 保存历史读动作（2026-10-27）：与 `get_abstract_schema` 同权限级。
    # ⚠️ 漏登记 = 不在任何闸门集合里 = execute 的 if/elif 全不命中 ⇒ **静默不判权**
    # （任何租户用户都能读别的 KB 的快照）。
    "list_abstract_schema_snapshots", "get_abstract_schema_snapshot",
    "get_compiled_schema_for_extraction",  # [jonex] atomic-rag 抽取入口（含同义词注入）
    "get_llm_wiki_schema", "list_synonyms", "list_tags", "list_parser_settings",
    "list_lineage_edges",  # 血缘边读取（KB 设置 → 血缘页签）
    "list_lineage_forbidden_pairs",  # 禁止同查表对读取（血缘页签第三个视图）
    "list_governance_labels", "list_governance_tags",
    "list_governance_clearance", "list_governance_policies",
    "list_governance_audit", "evaluate_governance_policy",
    # W2.7a 敏感扫描是只读分析（不写标签）；采纳（adopt）才走写闸门。
    "governance_sensitive_scan",
    "list_lineage_paths",  # 血缘路径评分查询（P2b）：纯只读，与 list_ontology_data_assets 同级
    "analyze_lineage_impact",  # 血缘影响分析（W2.4a）：纯只读展开，不写库
    "get_lineage_coverage",  # 血缘覆盖率（W2.4a）：读时聚合，不写库
    # 工具绑定读 + 执行前解析（只读；执行器经 Sidecar 无用户身份调用时不走空间判定）
    "list_tool_bindings", "list_bindable_tools", "resolve_action_execution",
    "list_kb_tools",
    "list_model_settings",
    "resolve_wiki_evidence", "get_wiki_page", "list_wiki_contents", "get_wiki_outline", "get_editor_state",
    "get_wiki_compilation_state",
    "get_wiki_backlinks", "get_wiki_claims",
    "get_wiki_graph_types", "search_wiki_nodes", "get_wiki_node",
    "get_wiki_neighbors", "get_wiki_graph_evidence", "get_wiki_media", "query_wiki_resources",
    "list_wiki_scenarios", "read_wiki_scenario_resources", "select_wiki_scenario_resources",
    "read_wiki_scenario_materials", "read_wiki_scenario_material",
    "get_parse_result_documents", "get_parse_result_entities",
    "get_parse_result_graph", "get_parse_result_graph_summary",
    "get_parse_result_relationships", "get_parse_result_summary",
    "get_space_permissions",
})

# 检索类（过滤无权限 KB，不整体拒绝）
_SEARCH_ACTIONS = frozenset({
    "search", "search_enhanced", "deep_query", "search_mix",
    # 纯数据 / 本体+数据：复用同一套 KB 可见性过滤。
    "search_data",
    "search_ontology_data",
    "query_with_ontology", "search_llmwiki", "search_jonex_wiki", "start_jonex_wiki_search",
    # [jonex] B2：网关流式检索的写前预检。走 `_filter_search_kbs` 的
    # 「空间 member / KB 授权」口径（与真实检索一致），不套 KB 浏览的成员口径。
    "assert_kb_searchable",
})

# 检索埋点排除名单：这些 action 虽在 `_SEARCH_ACTIONS`（复用同一套 KB 权限过滤），
# 但**不是检索行为**。`assert_kb_searchable` 是网关流式检索的写前预检（只判权限/
# 密级、不读内容），计进检索次数会把「预检 + 真实检索」算成两次。
_METERING_EXCLUDED_SEARCH_ACTIONS = frozenset({"assert_kb_searchable"})

# 数据运营聚合（空间级聚合，非用户级）：domain_space_id 传则空间成员，
# 不传则仅租户管理员（execute 内单独判定，不进 _SPACE_EXEMPT_ACTIONS）。
_ANALYTICS_ACTIONS = frozenset({
    "get_data_operations_overview", "get_data_assets_analytics",
    "get_knowledge_assets_analytics", "get_search_analytics",
    "get_abnormal_assets_analytics", "get_quality_anomalies",
    # 三期：列表钻取详情（与列表同一个权限口子 —— 它们读的是同一批数据，
    # 单独放宽就等于给「点开抽屉」开了一条绕过列表范围的旁路）
    "get_data_asset_detail", "get_data_ontology_entity_detail", "get_data_kb_detail",
    # 抽屉 D（领域服务详情）：同一个口子。它**不抛 404**（详见 `ServiceDetail` 的
    # DTO 注释）—— 权限仍由本组的空间判定 + service 层的空间范围过滤承担。
    "get_data_service_detail",
})

# 空间解析路径（action 组 → space_id 数据来源）
_SPACE_ID_DIRECT_ACTIONS = frozenset({
    "update_space", "delete_space", "set_space_permissions", "get_space_permission_candidates",
    "get_space_permissions", "create_knowledge_info", "create_service",
    "list_knowledge_info", "list_services",
})
_KB_ID_FIELD_ACTIONS = frozenset({
    "get_knowledge_info", "update_knowledge_info", "delete_knowledge_info",
    "get_kb_permissions", "set_kb_permissions", "get_kb_permission_candidates",
})  # 用 kb_id 字段
_DOC_ID_ACTIONS = frozenset({
    "reparse_document", "scan_stale_chunks", "purge_stale_chunks",
    "get_chunk", "get_raw_content", "get_asset_raw_location",  # [jonex] §image-refs P2-3
    "get_document_parse_result",
    "get_document_source_spans",  # 引用 bbox 定位：靠 document_id 解析空间
    # 文档版本链（第 2 项）
    "list_document_versions", "upload_document_version", "get_document_version_url",
    "activate_document_version", "discard_document_version_upload",
    # 相似建议（读 / 忽略 / 合并）
    "get_document_similarity_suggestions",
    "dismiss_document_similarity_suggestion", "merge_document_version",
})
_DS_ID_ACTIONS = frozenset({
    "get_data_source", "update_data_source", "delete_data_source",
    "test_data_source", "sync_data_source", "reset_ingest_key",
    "read_data_source_catalog", "generate_ds_schema",
    # ⚠️ 导入的必填键只有 ds_id —— 不放进这里，`_resolve_space_context` 会去默认的
    # KB 分支找 `knowledge_base_id`（缺席）⇒ 返回 (None, None, None) ⇒ 空间写闸门
    # **整段跳过**（与 `_DS_CONN_WRITE_ACTIONS` 那句注解同型的静默放行）。
    "import_lineage_file",
})
# ── 平台级连接池 ──
#
# ⚠ 为什么**不**塞进 _SPACE_WRITE_ACTIONS：那个集合只在 `space_id` 解析出来时
# 才生效（见 execute() 的 `if space_id:`）。连接不属于任何知识库 / 空间，
# 解析器返回 (None, None, None)，塞进去等于**一个校验都不做**。
# 所以单列两个集合，由 execute() 显式判定。
_DS_CONN_READ_ACTIONS = frozenset({
    # 列表/详情放开给登录用户：core-business 空间成员要在「添加数据源」下拉里选到连接
    "list_ds_connections", "get_ds_connection",
    # ⚠ 本集合只用于**文档与测试断言**（可读清单 / 与 _SPACE_WRITE_ACTIONS 不相交），
    # 运行期**不读它**：读 action 靠 `_resolve_space_context` 返回 None 自然放行，
    # 闸门只认 _DS_CONN_WRITE_ACTIONS。往这里加 action **不会**强制任何校验或豁免。
    # 读路径**故意不受**那道无条件闸门约束（列表/详情必须留给普通登录用户的下拉框）。
})
_DS_CONN_WRITE_ACTIONS = frozenset({
    # 会碰凭证（测连接要先解密再连库、读 catalog 同理）或改池子内容 → 租户管理员。
    # ⚠ 这五个在 execute() 里是**无条件**判定（在 skip_space_check 之外，无 user_id
    # 也拒绝），与块内其余分支的「无身份即跳过」口径故意不同 —— 见那边的注释。
    "create_ds_connection", "update_ds_connection", "delete_ds_connection",
    "test_ds_connection", "read_ds_connection_catalog",
})
_SYN_ID_ACTIONS = frozenset({"update_synonym", "delete_synonym"})
# 血缘边的 update/delete：入参是 `edge_id`（不是 knowledge_base_id）。
# ⚠️ 与 `_SYN_ID_ACTIONS` 不同，本集合**运行期不读**（同 `_DS_CONN_READ_ACTIONS` 的注解）：
# `_resolve_space_context` 对这两条走**默认的 KB 分支**（`knowledge_base_id` 已在
# `_REQUIRED_FIELDS` 里必填）—— 按调用方**声明的 KB** 解析空间，再用服务层的
# `knowledge_base_id` 过滤把「声明的 KB」与「边真正所属的 KB」钉在一起（审查 M-8）。
# 若改成按 `edge_id` 反查，解析出的会是**边真实所属**的 KB，与声明值脱钩，
# 反而让「拿 A 库权限去改 B 库的边」多绕一圈才被挡住。本集合只服务登记面与断言。
_LINEAGE_ID_ACTIONS = frozenset({"update_lineage_edge", "delete_lineage_edge"})
# 禁止同查表对的 update/delete：入参是 `pair_id`；与 `_LINEAGE_ID_ACTIONS` 同款，
# 运行期不读，只服务登记面与断言（空间解析走声明的 `knowledge_base_id`）。
_LINEAGE_PAIR_ID_ACTIONS = frozenset({
    "update_lineage_forbidden_pair", "delete_lineage_forbidden_pair",
})
# 治理 P1 的 update/delete：入参是 label_id / tag_id / policy_id。
_GOVERNANCE_ID_ACTIONS = frozenset({
    "update_governance_label", "delete_governance_label",
    "update_governance_tag", "delete_governance_tag",
    "update_governance_policy", "delete_governance_policy",
})
_TOOL_BINDING_ID_ACTIONS = frozenset({"update_tool_binding", "delete_tool_binding"})
_KB_TOOL_ID_ACTIONS = frozenset({"update_kb_tool", "delete_kb_tool", "test_kb_tool"})
_SETTING_ID_ACTIONS = frozenset({"update_parser_setting", "delete_parser_setting"})
_SERVICE_ID_ACTIONS = frozenset({
    "get_service", "update_service", "delete_service", "enable_service", "disable_service",
    "rotate_service_api_key", "list_service_api_keys", "create_service_api_key",
    "delete_service_api_key", "get_service_configs", "update_service_configs",
})


class KnowledgeBaseCapability(BaseCapability):
    """Knowledge Base capability adapter."""

    _INTERNAL_ACTIONS = {"ingest_push", "ingest_lineage_event"}

    # [jonex] 双向门禁（方案 §15）：本体 schema/抽取类 action 拒绝 openkb KB。
    # 清单与方案 §15 一致——这些 action 当前都在 dispatch 直接分发、无 kb_type 校验。
    _ONTOLOGY_GATED_ACTIONS = {
        "get_compiled_schema",
        # KB 级生命周期状态 / 重编译预览：同为「本体 schema 消费方」，openkb KB 一律拒绝
        "get_kb_compiled_schema_status",
        "preview_kb_compiled_schema_recompile",
        # [jonex] 抽取入口同为「本体 schema 消费方」，openkb KB 一律拒绝（同 get_compiled_schema）
        "get_compiled_schema_for_extraction",
        "save_compiled_schema",
        "reseed_compiled_schema",
        "recompile_schema",
        "export_compiled_schema_yaml",
        "import_compiled_schema_yaml",
        "reextract_kb_documents",
        # 更新模型以 compiled schema 为种子，openkb 没有本体 schema 可更新
        "start_model_update", "get_model_update_status", "apply_model_update",
        # 工具绑定只对本体型 KB 有意义（绑定目标是本体实例）
        "create_tool_binding",
        "resolve_action_execution",
        "prepare_action_execution",
        # 知识库工具只能绑定到本体实例，Wiki 型库登记了也用不上
        "create_kb_tool",
        # 抽取期工具作用于本体实体，Wiki 型库没有
        "preview_extract_tool",
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
        self._backfill_task: asyncio.Task | None = None
        super().__init__()

    def register_routes(self, app):
        """**不注册任何并行 REST 层** —— 该层已于 2026-10 物理删除。

        历史背景（2026-10 渗透测试 RC-A）：knowledge-base 除 `/invoke`（生产链路，
        强制内部服务 JWT）外，还额外挂了一层并行 REST（`/api/v1/knowledge-info`、
        `/api/v1/documents` 等 84 条）。该层**不在生产链路上** —— Gateway 对
        knowledge_base 一律经 Sidecar `/invoke`，前端只打网关前缀
        `/api/v1/knowledge-base/**`；却**无任何鉴权**且按客户端 `X-Tenant-ID`
        决定租户，容器端口一旦可达（RC-B 宿主端口发布）即为跨租户读写面
        （报告 3.2/3.4）。

        处置过程：
        1. P1 先加 `CONTAINER_REST_ENABLED` 开关（默认 `false`）→ 生产不挂载，
           攻击面 137 → 0；
        2. 随后**物理删除**该层实现与开关本身（纯清理，生产行为不变：此前默认
           也不挂载），使「误挂载」这一类风险从根上消失。

        ⚠️ **不要再往这里加路由**。需要新的对外能力时：
        - 在 `_build_dispatch` 里加 invoke action（`/invoke` 注册在
          `deploy/start_capability.py`，与本方法无关）
        - 同步 `api_gateway/routes/knowledge_base.py` 与 Sidecar 契约

        原子抽取侧的 compiled schema 读取走 `/invoke` 的
        `get_compiled_schema_for_extraction`（见 `_build_dispatch`）。
        """
        return super().register_routes(app)

    async def initialize(self) -> None:
        # 安全守卫：JWT_SECRET 必须为真实随机值（**生产档 ENV=prod 硬拦**，非生产只告警；
        # 见 jonex_core/common/security_guards.py 约束 2）。本服务用 _sign_payload
        # 计算 ingest key 哈希，泄漏即可伪造 ingest key 向任意租户知识库注入数据。
        assert_secure_jwt_secret()
        # [jonex] 配额：启动时解析校验，配置非法抛 QuotaConfigError 拒绝启动。
        get_system_quota()

        # [jonex] KB_ENABLED_TYPES：启动时解析校验，并记一条**生效值**。
        # 配置非法（最典型的是 `jonex-wiki` 把下划线写成连字符）在这里直接拒绝启动 ——
        # 它的失败方式很隐蔽：不报错，只是客户现场的弹窗**少一个选项**，没人知道为什么。
        # 顺带把生效值打进日志：排查时不用再去猜容器里那个 env 到底读到没有。
        logger.info("KB_ENABLED_TYPES 生效：%s", ", ".join(sorted(enabled_kb_types())))

        # [jonex] 任务中心接入：**执行登记必须是持久实现**（缺失即拒绝启动）。
        #
        # 为什么值得在这里拒启动：`RunnerBase.__init__` 默认给的是
        # `InMemoryExecutionRegistry`，而**漏传 registry 不报错**、只打一条 warning 就
        # 静默降级。它的失效方式很隐蔽 —— 任务中心出站没够着运行方时**会重派同一个
        # `execution_id`**，内存实现重启即丢、多副本互相看不见，认不出重派就会重复建
        # 下游 atomic-rag 任务、重复扣配额、产出重复向量。而任务一跑就是几十分钟
        # （`RAG_TASK_HARD_TIMEOUT_SEC` 默认 21600 秒），重启窗口是真实存在的。
        # 这里拦不住的话，那句话只会在某次重启后的重复向量里才被人发现。
        if not getattr(self.service.task_runner.registry, "persistent", False):
            raise InternalError(
                message=translate(
                    "err.kb.task_center.registry_not_persistent",
                    fallback="知识库任务执行登记必须是持久实现（当前是内存实现，重启/多副本会重复干活）",
                )
            )

        try:
            await ensure_ontology_schema()
        except Neo4jAuthError:
            logger.critical("Neo4j authentication failed, refusing to start")
            raise
        except Exception as exc:
            logger.warning("Neo4j schema init failed (will retry), ontology queries may degrade: %s", exc)

        # 存量手动实体回填：补 extraction_method='manual' 标记与 embedding（幂等）。
        # 后台执行不阻塞启动；失败仅告警，与 schema init 降级语义一致。
        self._backfill_task = asyncio.create_task(self._backfill_manual_entities())

        self._reconcile_task = asyncio.create_task(self._reconcile_loop())

        # [jonex] 第 3 期 · 任务 5/8：保留策略扫描（归档到期 / 销毁宽限到期）。
        # `RETENTION_SCHEDULER_INTERVAL_SECONDS<=0` 时 start() 自己 no-op。
        # ⚠️ 调度器起不来**不能拖垮服务启动**（保留策略是后台治理能力，不是服务
        # 可用性的前置条件）；失败只记 warning，下一轮重启再试。
        try:
            from .services.retention_scheduler import start_scheduler as _start_retention

            await _start_retention()
        except Exception as exc:  # noqa: BLE001 —— 保护服务启动
            logger.warning("保留策略调度器启动失败（服务继续启动）: %s", exc)

        # [jonex] 删库索引补偿：软删 KB 的文档行没有逐文档删除入口，
        # 检索索引（LightRAG/OpenKB/jonex_wiki/Neo4j）由该后台循环补清。
        try:
            from .services.index_cleanup_scheduler import (
                start_scheduler as _start_index_cleanup,
            )

            await _start_index_cleanup()
        except Exception as exc:  # noqa: BLE001 —— 保护服务启动
            logger.warning("删库索引补偿器启动失败（服务继续启动）: %s", exc)

        # [jonex] 相似签名周期回填：开关开启的 KB 里，缺签名 / unavailable 哨兵
        # 超时的文档分批补算。`DOCUMENT_SIMILARITY_BACKFILL_INTERVAL_SECONDS<=0`
        # 时循环自己 no-op。起不来不拖垮服务启动（与上面几个后台循环同一条纪律）。
        try:
            from .services.similarity_backfill_scheduler import (
                start_scheduler as _start_similarity_backfill,
            )

            await _start_similarity_backfill()
        except Exception as exc:  # noqa: BLE001 —— 保护服务启动
            logger.warning("相似签名回填调度器启动失败（服务继续启动）: %s", exc)

        # 调用明细采集器（MonitorCollector）由 deploy/start_capability.py 的 lifespan
        # 统一启动（早于本 initialize），本能力不再自己启动。

        logger.info("Knowledge Base capability initialized (reconciliation loop started)")

    async def shutdown(self) -> None:
        # [jonex] 第 3 期 · 任务 5/8：先停保留扫描（它与对账循环无共享状态，
        # 顺序只是为了在关 Neo4j 驱动前收掉所有后台任务）。
        try:
            from .services.retention_scheduler import stop_scheduler as _stop_retention

            await _stop_retention()
        except Exception as exc:  # noqa: BLE001 —— 关停失败不阻塞其余清理
            logger.warning("保留策略调度器停止失败: %s", exc)

        try:
            from .services.index_cleanup_scheduler import (
                stop_scheduler as _stop_index_cleanup,
            )

            await _stop_index_cleanup()
        except Exception as exc:  # noqa: BLE001 —— 关停失败不阻塞其余清理
            logger.warning("删库索引补偿器停止失败: %s", exc)

        try:
            from .services.similarity_backfill_scheduler import (
                stop_scheduler as _stop_similarity_backfill,
            )

            await _stop_similarity_backfill()
        except Exception as exc:  # noqa: BLE001 —— 关停失败不阻塞其余清理
            logger.warning("相似签名回填调度器停止失败: %s", exc)

        if self._backfill_task:
            self._backfill_task.cancel()
            try:
                await self._backfill_task
            except asyncio.CancelledError:
                pass
        if self._reconcile_task:
            self._reconcile_task.cancel()
            try:
                await self._reconcile_task
            except asyncio.CancelledError:
                pass

        # 所有会「新起扫描任务」的后台循环都停掉/取消之后，再 drain 一次 ——
        # 否则 drain 快照之后由循环新起的任务会逃逸到关停之后。
        try:
            from .services.document_similarity_service import (
                drain_pending_similarity_tasks,
            )

            await drain_pending_similarity_tasks(timeout=5)
        except Exception:  # noqa: BLE001 —— 收尾失败不阻塞关停
            logger.warning("等待相似签名任务收尾失败", exc_info=True)

        # 调用明细采集器由 deploy/start_capability.py 在本 shutdown() 返回后统一停止并 flush。

        await close_neo4j_driver()
        logger.info("Knowledge Base capability shutdown complete")

    async def _backfill_manual_entities(self) -> None:
        """后台回填存量手动实体：补 extraction_method='manual' 标记与 embedding。

        幂等、失败仅告警不阻塞启动。shutdown 时取消。
        """
        try:
            backfilled = await OntologyGraphRepository(
                get_neo4j_driver()
            ).backfill_manual_entities_all()
            if backfilled:
                logger.info("存量手动实体回填完成：%d 个", backfilled)
        except asyncio.CancelledError:
            logger.info("Manual entity backfill cancelled")
            raise
        except Exception as exc:
            logger.warning("存量手动实体回填失败（不阻塞启动）: %s", exc)

    def _image_query_sweep_due(self) -> bool:
        """查询图片兜底清理该不该跑（B12）。按 `IMAGE_QUERY_SWEEP_INTERVAL` 节流。

        状态放在实例上（`_image_query_swept_at`），懒初始化 —— 第一次调用即到期，
        这样重启后不用等满一个周期才扫第一遍。
        """
        import time

        try:
            interval = int(get_config().IMAGE_QUERY_SWEEP_INTERVAL)
        except Exception:  # noqa: BLE001 —— 配置读不出来不该让对账循环整趟崩
            interval = 3600
        if interval <= 0:
            # 配 0 / 负数 = 关掉这趟（不是"每轮都跑"），给运维一个显式的关闭开关
            return False
        now = time.monotonic()
        last = getattr(self, "_image_query_swept_at", None)
        if last is not None and (now - last) < interval:
            return False
        self._image_query_swept_at = now
        return True

    async def _reconcile_pass(self, label: str, run: Callable[[], Awaitable[Any]]) -> Any:
        """跑一趟对账/巡检，**只兜自己这一趟的异常**；没跑成返回 `_PASS_FAILED`。

        为什么逐趟包、而不是像原来那样整轮包一个 try/except：这几趟之间**没有数据依赖**
        （各自扫各自的状态、写各自的表），而被跳过的代价**并不等价** —— 排在最后的任务中心
        看门狗是被接管文档**唯一**的解封点（用户中途要接管会被拦住，见
        `services/task_watch_service.py` 的模块 docstring），它被前面某趟的事故连坐，等于
        用户可能被**一直**拦着。整轮一个 except 会把「Neo4j 抖一下」放大成「整条对账停摆」，
        连另两类 Wiki 库的文档也跟着不再被推进。

        ⚠️ **隔离不等于免疫共同依赖**：坏的是「只有某一趟才用到的东西」（Neo4j、某趟自己的
        客户端）时，其余趟照跑、看门狗照样能给用户解封；坏的是**多趟共用的东西**时
        （典型：`041` 没应用 → 看门狗自己 `list_active` 也抛），看门狗一样每轮失败 ——
        那种情况要连根因一起修，不是这里能兜的。

        日志口径与原来一致（DB 类异常 warning、其它记完整堆栈），只是多带一个趟名。
        `CancelledError` **原样上抛** —— shutdown 靠它退出，不能被这里吃掉。
        """
        try:
            return await run()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            # 数据库连接异常（可恢复）→ 等后重试；其它异常 → 记录完整堆栈
            if isinstance(e, (OperationalError, DBAPIError)):
                logger.warning("对账→%s: DB connection lost, will retry. %s", label, e)
            else:
                logger.exception("对账→%s error", label)
            return _PASS_FAILED

    async def _reconcile_loop(self) -> None:
        """每 30 秒扫描 PARSING 文档和本体对账（启动时立即执行一次）。

        另挂一趟**低频**的查询图片兜底清理（以图问知识，B12）：它不需要 30 秒的
        节拍，由 `_image_query_sweep_due` 按 `IMAGE_QUERY_SWEEP_INTERVAL` 自己节流。
        """
        logger.info("Reconciliation loop started (first run immediately)")
        first_run = True
        while True:
            try:
                if not first_run:
                    await asyncio.sleep(30)
                first_run = False

                logger.debug("Reconciliation cycle begin")
                # 逐趟隔离（`_reconcile_pass`）：某一趟崩了只跳过它自己。这一轮的失败趟名
                # 收进 `failed`，末尾照旧退避一轮 —— 那正是原来整轮 except 里那个 sleep 的
                # 语义，只是不再以「跳过后面的趟」为代价。
                failed: list[str] = []

                doc_result = await self._reconcile_pass(
                    "文档",
                    lambda: self.service.reconciliation.reconcile_documents(limit=50),
                )
                if doc_result is _PASS_FAILED:
                    failed.append("文档")
                else:
                    logger.info("对账→文档: %s", doc_result)

                onto_result = await self._reconcile_pass(
                    "本体",
                    lambda: self.service.reconciliation.reconcile_ontology(limit=50),
                )
                if onto_result is _PASS_FAILED:
                    failed.append("本体")
                else:
                    logger.info("对账→本体: %s", onto_result)

                patrol_result = await self._reconcile_pass(
                    "超时巡检",
                    lambda: self.service.reconciliation.patrol_parsing_timeout(limit=50),
                )
                if patrol_result is _PASS_FAILED:
                    failed.append("超时巡检")
                elif patrol_result.get("timed_out", 0) > 0 or patrol_result.get("waiting", 0) > 0:
                    logger.info("对账→超时巡检: %s", patrol_result)

                # [jonex] LLM-Wiki 编译巡检：轮询任务状态回写 PG + stale 补提交 + 超时判死
                okb_result = await self._reconcile_pass(
                    "LLM-Wiki 编译巡检",
                    lambda: self.service.reconciliation.patrol_openkb_compile(limit=50),
                )
                if okb_result is _PASS_FAILED:
                    failed.append("LLM-Wiki 编译巡检")
                elif okb_result.get("timed_out", 0) > 0 or okb_result.get("compiled", 0) > 0 \
                        or okb_result.get("failed", 0) > 0 or okb_result.get("stale_submitted", 0) > 0:
                    logger.info("对账→LLM-Wiki 编译巡检: %s", okb_result)

                # [jonex] Phase 5：jonex_wiki 编译巡检（状态机同 openkb，kb_type 过滤 jonex_wiki）
                jw_result = await self._reconcile_pass(
                    "jonex_wiki 编译巡检",
                    lambda: self.service.reconciliation.patrol_jonex_wiki_compile(limit=50),
                )
                if jw_result is _PASS_FAILED:
                    failed.append("jonex_wiki 编译巡检")
                elif jw_result.get("timed_out", 0) > 0 or jw_result.get("compiled", 0) > 0 \
                        or jw_result.get("failed", 0) > 0:
                    logger.info("对账→jonex_wiki 编译巡检: %s", jw_result)

                # [jonex] 任务中心看门狗：编排里每一步的终态由它上报（见 task_watch_service）。
                # **挂在同一轮循环里、复用这里的 30 秒节拍，不新起线程也不另拉循环** ——
                # 两件事本来就是同一个职责：「让在途文档往前走一步」。
                # 放在最后只是因为它与其他几趟无耦合；**它不再被前面几趟的失败连坐**
                # （逐趟隔离见 `_reconcile_pass`）—— 它是被接管文档唯一的解封点。
                watch_result = await self._reconcile_pass(
                    "任务中心看门狗",
                    lambda: self.service.task_watch.sweep(limit=50),
                )
                if watch_result is _PASS_FAILED:
                    failed.append("任务中心看门狗")
                elif watch_result:
                    logger.info("对账→任务中心看门狗: %s", watch_result)

                # [jonex] 以图问知识：查询图片兜底清理（B12）。
                #
                # 它补的是**同步清理漏掉的那部分**（删历史时删对象失败、进程在两步之间
                # 退出）—— 那些图片不进这条路就永远留在桶里，而对象存储没有 TTL（F13）。
                #
                # ⚠️ **必须自己节流**：本循环 30 秒一轮，而这个活按小时算就够
                # （`IMAGE_QUERY_SWEEP_INTERVAL` 默认 3600）。不节流等于每 30 秒
                # 扫一次库 + 可能删一次对象，纯浪费。
                # ⚠️ **多副本并发跑是安全的**：删对象与软删都幂等（删不存在的对象视为
                # 成功），所以不选主 —— 与这个循环里的其它趟同一个口径。
                if self._image_query_sweep_due():
                    sweep_result = await self._reconcile_pass(
                        "查询图片兜底清理",
                        lambda: self.service.query_image.sweep_orphans(),
                    )
                    if sweep_result is _PASS_FAILED:
                        failed.append("查询图片兜底清理")
                    elif sweep_result and (sweep_result.get("orphans") or sweep_result.get("objects")):
                        logger.info("对账→查询图片兜底清理: %s", sweep_result)

                if failed:
                    await asyncio.sleep(10)
            except asyncio.CancelledError:
                logger.info("Reconciliation loop cancelled, exiting")
                break
            except Exception as e:
                # 逐趟的异常已在 `_reconcile_pass` 里就地兜住，能落到这里的只有「循环骨架
                # 自己」的异常（组装趟列表、算失败趟名等）。留着它纯粹是最后一道网：
                # 没有它，一个骨架级的意外会让整个协程静默死掉、后台再无对账。
                logger.exception("Reconciliation loop error")
                await asyncio.sleep(10)

    @staticmethod
    def _search_question_and_kbs(d: dict) -> tuple[str, list[str]]:
        from jonex_core.common.exceptions import InvalidParameterError
        from jonex_core.common.i18n import translate

        question = str(d.get("query") or "").strip()
        if not question:
            raise InvalidParameterError(message=translate(
                "err.search.query_required", fallback="缺少 query"))
        raw = d.get("knowledge_base_ids")
        if raw is None:
            if d.get("knowledge_base_id"):
                raw = [d["knowledge_base_id"]]
            else:
                raise InvalidParameterError(message=translate(
                    "err.search.kb_ids_required",
                    fallback="缺少 knowledge_base_ids",
                ))
        if not isinstance(raw, list):
            raise InvalidParameterError(message="knowledge_base_ids 必须是数组")
        # 权限过滤后可能为空（`_filter_search_kbs` 已回写）：按「没有可查源」回空结果，
        # 不再抛「缺少参数」——那不是调用方的拼装错误。
        return question, [str(k) for k in raw if str(k or "").strip()]

    @staticmethod
    async def _search_data_action(r, d: dict, nl) -> dict:
        from .services.nl2sql.data_search import run_data_search

        question, kb_ids = KnowledgeBaseCapability._search_question_and_kbs(d)
        return await run_data_search(
            nl, r.tenant_id, kb_ids, question,
            pins=d.get("entity_pins") or {},
            user_id=r.user_id or "", username=r.username or "", ip=r.ip or "",
        )

    async def _search_ontology_data_action(self, r, d: dict, nl) -> dict:
        from .services.nl2sql.ontology_data import run_ontology_data_search

        question, kb_ids = self._search_question_and_kbs(d)
        pins = d.get("entity_pins") or {}
        # 本体侧不该看到编排字段与点名 pin：pin 只用于数据补充段。
        ontology_body = {
            k: v for k, v in d.items()
            if k not in ("entity_pins", "evolution_run_id")
        }
        ontology_body["skip_enum_force_rag"] = True

        async def _ontology_call():
            return await self._query_with_ontology(r, ontology_body)

        return await run_ontology_data_search(
            _ontology_call, nl, r.tenant_id, kb_ids, question,
            pins=pins,
            user_id=r.user_id or "", username=r.username or "", ip=r.ip or "",
        )

    def _build_dispatch(self) -> dict[str, Any]:
        docs = self.service.documents
        search = self.service.search
        history = self.service.history
        feedback = self.service.feedback
        # [jonex] 以图问知识：识别（图片→文本）与图片生命周期（落盘/查看/清理）
        image_query = self.service.image_query
        query_image = self.service.query_image
        parse = self.service.parse_results
        ontology = self.service.ontology
        reconcile = self.service.reconciliation
        sim = DocumentSimilarityService()
        kbinfo = self.service.knowledge_infos
        compiler = self.service.compiler
        lws = self.service.llm_wiki_schemas  # [jonex] LLM-Wiki Schema
        wo = self.service.wiki_outlines  # [jonex] Wiki 阅读目录
        folders = self.service.folders
        syn = self.service.synonyms
        tb = self.service.tool_bindings
        kt = self.service.kb_tools
        oq = self.service.ontology_query
        ds = self.service.data_sources
        nl = self.service.nl2sql  # ② NL→SQL 分源查询
        from .services.nl2sql import data_search as dsrch  # noqa: PLC0415
        from .services.nl2sql import ontology_data as odata  # noqa: PLC0415
        from .services.nl2sql import metric_call_approval as mca  # noqa: PLC0415
        ps = self.service.parser_settings
        abs_schema = self.service.abstract_schemas  # 跨数据源抽象 schema（概念对齐层）
        ms = self.service.model_settings
        s = self._space
        sv = self._domain_service
        quota = QuotaService()
        analytics = AnalyticsService()
        dsc = DataSourceConnectionService()
        align = OntologyDataAlignmentService()  # 跨模态对齐（P1.5b）
        # 更新模型复用同一批服务实例：导出 schema 走 compiler、正文走 documents。
        model_update = ModelUpdateService(
            compiler=compiler, documents=docs, knowledge_infos=kbinfo,
        )

        # 试跑抽取期工具声明：空 KB 必须在能力层拒掉（空串会让空间闸门整段跳过）
        async def _preview_extract_tool(r, d):
            from .services.extract_tool_service import ExtractToolService  # noqa: PLC0415

            _require_nonblank(d, "knowledge_base_id")
            return await ExtractToolService().preview(
                r.tenant_id, d["knowledge_base_id"], d["entity_type"], d["group"],
                d["declaration"], d.get("canonical_name") or None,
                user_id=r.user_id, username=r.username, request_id=getattr(r, "request_id", None),
            )

        # 保存历史（2026-10-27）：三个动作都先 `_require_nonblank` 再进服务。
        # ⚠️ 不能只靠 required fields：`_resolve_space_context` 把空串当「没给」
        # ⇒ 空间闸门整段跳过（lineage I-1 同一个缝）。空 KB 必须在能力层按 1001
        # 拒掉，既不能落到服务层去猜租户，也不能让读动作静默放行。
        async def _history_list(r, d):
            _require_nonblank(d, "knowledge_base_id")
            return await abs_schema.list_snapshots(
                r.tenant_id, d["knowledge_base_id"],
                limit=d.get("limit", 20),  # 服务层再 clamp 1..50
            )

        async def _history_get(r, d):
            _require_nonblank(d, "knowledge_base_id", "snapshot_id")
            return await abs_schema.get_snapshot(
                r.tenant_id, d["snapshot_id"], kb_id=d["knowledge_base_id"],
            )

        async def _history_restore(r, d):
            _require_nonblank(d, "knowledge_base_id", "snapshot_id")
            # `expected_schema_version` **不能**走 `_require_nonblank`：它用
            # `not str(value or "").strip()` 判空，`0` 会被当成「没给」——而 0 是
            # 合法版本号（首次导入语义）。只拒绝「没给 / 布尔 / 非整数」。
            version = d.get("expected_schema_version")
            if version is None or isinstance(version, bool) or not isinstance(version, int):
                raise InvalidParameterError(message=translate(
                    "err.abstract_schema.invalid_expected_version",
                    fallback="expected_schema_version 必须是整数版本号",
                ))
            return await abs_schema.restore_snapshot(
                r.tenant_id, d["snapshot_id"],
                expected_version=version,
                user_id=r.user_id or "",
                kb_id=d["knowledge_base_id"],
            )

        # 先摆业务 action，任务中心的两个入口在末尾 `dispatch.update(...)` 补进去
        # （SDK 的装配约定，理由见函数尾那一段）。
        dispatch: dict[str, ActionHandler] = {
            # ── 文档管理 ──
            "upload_document": lambda r, d: docs.upload_document(r.tenant_id, d, user_id=r.user_id, username=r.username, ip=r.ip),
            "list_documents": lambda r, d: docs.list_documents(r.tenant_id, d),
            "documents_stats": lambda r, d: docs.documents_stats(r.tenant_id, d),
            "get_document": lambda r, d: docs.get_document(r.tenant_id, d["document_id"], d),
            "list_document_versions": lambda r, d: docs.list_document_versions(
                r.tenant_id, d["document_id"],
            ),
            "upload_document_version": lambda r, d: docs.upload_document_version(
                r.tenant_id, d["document_id"], d,
                user_id=r.user_id, username=r.username, ip=r.ip,
            ),
            "get_document_version_url": lambda r, d: docs.get_document_version_url(
                r.tenant_id, d["document_id"], d["version_no"],
            ),
            "activate_document_version": lambda r, d: docs.activate_document_version(
                r.tenant_id, d["document_id"], d["version_no"],
                user_id=r.user_id, username=r.username, ip=r.ip,
            ),
            "discard_document_version_upload": lambda r, d: (
                docs.discard_document_version_upload(
                    r.tenant_id, d["document_id"], d["storage_key"],
                    d.get("storage_backend"),
                )
            ),
            # ── 解析后相似检测：建议 / 忽略 / 合并为新版本 ──
            "get_document_similarity_suggestions": (
                lambda r, d: sim.get_suggestions(
                    r.tenant_id, d["document_id"], user_id=r.user_id,
                )
            ),
            "dismiss_document_similarity_suggestion": (
                lambda r, d: sim.dismiss_suggestion(
                    r.tenant_id, d["suggestion_id"], d["document_id"],
                    user_id=r.user_id,
                )
            ),
            "merge_document_version": (
                lambda r, d: sim.merge_suggestion(
                    r.tenant_id, d["suggestion_id"], d["document_id"],
                    user_id=r.user_id, username=r.username, ip=r.ip,
                )
            ),
            "get_document_status": lambda r, d: docs.get_document_status(r.tenant_id, d["document_id"], d.get("kb_ids", [])),
            # [jonex] B6 §5.2：仅校验文档可读（租户 + KB 空间/授权），不返回正文
            "assert_document_readable": lambda r, d: docs.assert_document_readable(
                r.tenant_id, r.user_id, d["document_id"],
            ),
            "get_document_quality_report": lambda r, d: docs.get_document_quality_report(
                r.tenant_id, d["document_id"], d.get("knowledge_base_id", ""),
            ),
            # B6 §5.1：批量质量报告全被密级过滤时，服务会因空 list 撞 1..100 入参闸，
            # 由守卫打标记后在这里直接返回空 items。
            "get_document_quality_reports": self._get_document_quality_reports,
            "delete_document": lambda r, d: docs.delete_document(r.tenant_id, d["document_id"], d, user_id=r.user_id, username=r.username, ip=r.ip),
            # ── 第 3 期生命周期：归档 / 恢复 / 销毁（KB 执行）──
            "archive_document": lambda r, d: docs.archive_document(
                r.tenant_id, d["document_id"], d["knowledge_base_id"],
                reason=d.get("reason", "manual"),
                user_id=r.user_id, username=r.username, ip=r.ip,
            ),
            "restore_document": lambda r, d: docs.restore_document(
                r.tenant_id, d["document_id"], d["knowledge_base_id"],
                user_id=r.user_id, username=r.username, ip=r.ip,
            ),
            "destroy_document": lambda r, d: docs.destroy_document(
                r.tenant_id, d["document_id"], d["knowledge_base_id"],
                user_id=r.user_id, username=r.username, ip=r.ip,
            ),
            # [jonex] file_size 必须透传到服务层做上限预检与 Content-Length 签名；
            # 缺省 / 0 一律视为「未知」（MCP 契约：0 = 未知），避免历史 0 变成新的非法值。
            "generate_upload_url": lambda r, d: docs.generate_upload_url(
                r.tenant_id, d["knowledge_base_id"], d["file_name"], d.get("content_type"),
                d.get("file_size") or None,
            ),
            "get_raw_url": lambda r, d: _url_result(docs.get_raw_url(r.tenant_id, d.get("knowledge_base_id", ""), d["document_id"], user_id=r.user_id, username=r.username, ip=r.ip, mcp_key_id=(r.context or {}).get("mcp_key_id"))),
            "get_raw_content": lambda r, d: docs.get_raw_content(r.tenant_id, d.get("knowledge_base_id", ""), d["document_id"], user_id=r.user_id, username=r.username, ip=r.ip, mcp_key_id=(r.context or {}).get("mcp_key_id")),
            "get_raw_location": lambda r, d: docs.get_raw_location(
                r.tenant_id, d.get("knowledge_base_id", ""), d["document_id"],
                download=d.get("download", False),
            ),
            # [jonex] §image-refs P2-3: 图片资产原文位置（cos=预签名 / local=storage_key）
            "get_asset_raw_location": lambda r, d: docs.get_asset_raw_location(
                r.tenant_id, d["document_id"], d["image_idx"],
                ext=d.get("ext", ""), knowledge_base_id=d.get("knowledge_base_id", ""),
            ),
            "get_document_chunks": lambda r, d: docs.get_document_chunks(r.tenant_id, d["document_id"]),
            # 引用原文坐标（bbox 定位）：chunk_index / expect 缺省时服务层降级为只跳页
            "get_document_source_spans": lambda r, d: docs.get_document_source_spans(
                r.tenant_id, d["document_id"], d.get("chunk_index"),
                expect=d.get("expect") or {},
            ),
            # [jonex] 第2期 · 任务5/6：limit 原样交给服务层校验（dispatch 不裸 int()）
            "get_document_provenance": lambda r, d: docs.get_document_provenance(
                r.tenant_id, d["document_id"], d["knowledge_base_id"],
                limit=d.get("limit", 100),
            ),
            "get_chunk": lambda r, d: docs.get_chunk(r.tenant_id, d["document_id"], d["chunk_id"]),
            "reparse_document": lambda r, d: docs.reparse_document(r.tenant_id, d["document_id"], user_id=r.user_id, username=r.username, ip=r.ip, force=d.get("force", False)),
            # [jonex] §table-grid-v2 O7: 新旧格式残留扫描与定点清理
            "scan_stale_chunks": lambda r, d: docs.scan_stale_chunks(r.tenant_id, d["document_id"]),
            "purge_stale_chunks": lambda r, d: docs.purge_stale_chunks(r.tenant_id, d["document_id"]),
            "set_document_folder": lambda r, d: docs.set_document_folder(
                r.tenant_id, d["document_id"], d, user_id=r.user_id, username=r.username, ip=r.ip
            ),
            "batch_set_document_folder": lambda r, d: docs.batch_set_document_folder(
                r.tenant_id, d, user_id=r.user_id, username=r.username, ip=r.ip
            ),
            # ── 检索 ──
            # D3：request_id 已在 execute 开头规范化，这里不再二次生成
            "search": lambda r, d: search.search(r.tenant_id, _user_id(r), d, trace_id=r.request_id),
            "search_enhanced": lambda r, d: search.enhanced_search(r.tenant_id, _user_id(r), d, trace_id=r.request_id),
            "query_with_ontology": self._query_with_ontology,
            "deep_query": self._deep_query,
            # [jonex] openkb 分流
            "search_llmwiki": self._search_llmwiki,
            "search_jonex_wiki": self._search_jonex_wiki,
            "start_jonex_wiki_search": self._start_wiki_search,
            "poll_jonex_wiki_search": self._poll_wiki_search,
            "cancel_jonex_wiki_search": self._cancel_wiki_search,
            "resolve_wiki_evidence": self._resolve_wiki_evidence,
            "search_mix": self._search_mix,
            # 网关流式检索预检：权限/密级在 execute 前置阶段已完成，这里只回执。
            "assert_kb_searchable": self._assert_kb_searchable,
            "record_stream_search": self._record_stream_search,
            "mark_template_scenario_published": self._mark_template_scenario_published,
            "list_impacted_knowledge_bases": self._list_impacted_knowledge_bases,
            # ── 数据运营（Data Operations）──
            "get_data_operations_overview": lambda r, d: analytics.overview(r.tenant_id, _user_id(r), d),
            "get_data_assets_analytics": lambda r, d: analytics.data_assets(r.tenant_id, _user_id(r), d),
            "get_knowledge_assets_analytics": lambda r, d: analytics.knowledge_assets(r.tenant_id, _user_id(r), d),
            "get_search_analytics": lambda r, d: analytics.search_analytics(r.tenant_id, _user_id(r), d),
            "get_abnormal_assets_analytics": lambda r, d: analytics.abnormal_assets(r.tenant_id, _user_id(r), d),
            "get_quality_anomalies": lambda r, d: analytics.quality_anomalies(r.tenant_id, _user_id(r), d),
            # 三期：列表钻取详情。窗口/范围参数与列表共用同一组字段
            # （`days` 或 `start_date`+`end_date`，加 `domain_space_id`），
            # 所以这三个 action 不需要自己的 `_REQUIRED_FIELDS` 之外的特殊解析。
            "get_data_asset_detail": lambda r, d: analytics.asset_detail(r.tenant_id, _user_id(r), d),
            "get_data_ontology_entity_detail": lambda r, d: analytics.ontology_entity_detail(r.tenant_id, _user_id(r), d),
            "get_data_kb_detail": lambda r, d: analytics.kb_detail(r.tenant_id, _user_id(r), d),
            "get_data_service_detail": lambda r, d: analytics.service_detail(r.tenant_id, _user_id(r), d),
            "get_search_overview": lambda r, d: history.get_overview(r.tenant_id, _user_id(r), d),
            "list_search_history": lambda r, d: history.list_history(r.tenant_id, _user_id(r), d),
            "save_search_history": lambda r, d: history.save_history(r.tenant_id, _user_id(r), d),
            "delete_search_history": lambda r, d: history.delete_history(
                r.tenant_id, _user_id(r), d["history_id"], d
            ),
            "clear_search_history": lambda r, d: history.clear_history(r.tenant_id, _user_id(r), d),
            # ── 以图问知识 ──
            # 识别：图片 → 查询文本。**它不检索** —— 前端拿到 query 后照常调
            # `/search/mix` 等现有接口（方案 D1 两步式），所以这里没有 kb 参数。
            "image_to_query": lambda r, d: image_query.resolve(
                r.tenant_id, _user_id(r), d, trace_id=r.request_id
            ),
            # 查看票据：校验归属通过后把身份交回 Gateway 签票（签票是接口层的事，
            # service 不知道外部访问地址长什么样）
            "get_query_image_ticket": lambda r, d: self._query_image_ticket(r, d),
            "get_query_image_location": lambda r, d: query_image.get_location(
                r.tenant_id, _user_id(r), d["image_id"]
            ),
            # ── 搜索结果反馈 ──
            "submit_search_feedback": lambda r, d: feedback.submit_feedback(r.tenant_id, _user_id(r), d),
            "cancel_search_feedback": lambda r, d: feedback.cancel_feedback(r.tenant_id, _user_id(r), d),
            "list_search_feedback": lambda r, d: feedback.list_feedback(r.tenant_id, d),
            "toggle_search_feedback_adopted": lambda r, d: feedback.toggle_adopted(r.tenant_id, d),
            "get_search_feedback_stats": lambda r, d: feedback.get_stats(r.tenant_id, d),
            # ── 回答级反馈（history_id 锚点）──
            "submit_answer_feedback": lambda r, d: self.service.answer_feedback.submit(r.tenant_id, _user_id(r), d),
            "get_answer_feedback": lambda r, d: self.service.answer_feedback.get_status(r.tenant_id, _user_id(r), d),
            "list_answer_feedback": lambda r, d: self.service.answer_feedback.list_feedback(r.tenant_id, d),
            "get_answer_feedback_stats": lambda r, d: self.service.answer_feedback.get_stats(r.tenant_id, d),
            "toggle_answer_feedback_adopted": lambda r, d: self.service.answer_feedback.toggle_adopted(r.tenant_id, d),
            "delete_answer_feedback": lambda r, d: self.service.answer_feedback.delete(r.tenant_id, d),
            # ── MCP 检索反馈（request_id 锚点）──
            # 密级过滤复用检索链路同一实现：MCP 无用户身份，按机器身份判定。
            "submit_mcp_feedback": lambda r, d: self.service.answer_feedback.submit_mcp(
                r.tenant_id,
                r.context or {},
                d,
                kb_filter=lambda kb_ids: self._allowed_kb_ids(r.tenant_id, None, kb_ids),
            ),
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
                r.tenant_id, d["knowledge_base_id"], d["path"], actor=str(r.user_id or "")),
            "list_wiki_contents": lambda r, d: parse.get_wiki_contents(
                r.tenant_id, d["knowledge_base_id"], d.get("document_id", ""),
                # view=counts：编译资产 tab 只要三类页数，别搬整棵页面树
                d.get("view", "")),
            "get_wiki_compilation_state": lambda r, d: parse.compilation_state(
                r.tenant_id, d["knowledge_base_id"]),
            "pause_wiki_compilation": lambda r, d: parse.compilation_state(
                r.tenant_id, d["knowledge_base_id"], True),
            "resume_wiki_compilation": lambda r, d: parse.compilation_state(
                r.tenant_id, d["knowledge_base_id"], False),
            # ── [jonex] Wiki 阅读侧：反向链接（链入本页）/ claim 级溯源审计 ──
            "get_wiki_backlinks": lambda r, d: parse.get_wiki_backlinks(
                r.tenant_id, d["knowledge_base_id"], d["path"], d.get("document_id", "")),
            "get_wiki_claims": lambda r, d: parse.get_wiki_claims(
                r.tenant_id, d["knowledge_base_id"], d.get("path", ""),
                d.get("document_id", ""), int(d.get("limit") or 500)),
            # ── [jonex] Wiki 阅读目录（Topic/Section/Entry 纯展示投影）──
            # document_id 非空 = 文档级阅读视图：页面清单按文档过滤 + 目录做过滤投影
            "get_wiki_outline": lambda r, d: wo.get_view(
                r.tenant_id, d["knowledge_base_id"], d.get("document_id", "")),
            "draft_wiki_outline": lambda r, d: wo.draft(
                r.tenant_id, d["knowledge_base_id"], d, user_id=r.user_id),
            "adjust_wiki_outline": lambda r, d: wo.adjust(
                r.tenant_id, d["knowledge_base_id"], d, user_id=r.user_id),
            # ── [jonex] LLM-Wiki Schema 编译设置 ──
            "get_llm_wiki_schema": lambda r, d: lws.get_schema(
                r.tenant_id, d["knowledge_base_id"]),
            "save_llm_wiki_schema": lambda r, d: lws.save_schema(
                r.tenant_id, d, user_id=r.user_id),
            "apply_llm_wiki_schema": lambda r, d: lws.apply_schema_to_engine(
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
                # [jonex] 只用于任务中心那条路的 `updated_by`（「最后是谁动的」）：
                # Ontology 库的「重新编译」现在会转成「让任务中心重跑 compile_1」，
                # 那一列正好要记下是哪个用户点的按钮（与「重新解析」同口径）。
                # 漏传不报错（空值是合法状态），但这里拿得到就传。
                operator_id=r.user_id,
            ),
            # ── 本体编译 schema 编辑 ──
            "get_editor_state": lambda r, d: compiler.get_editor_state(
                r.tenant_id, d["knowledge_base_id"],
            ),
            # data_source_id 不传 = 知识库级（改造前行为）；传了 = 该数据源自己的那份。
            # ⚠️ 这里是**网关实际走的那条路**（sidecar /invoke → execute → dispatch）。
            # api/__init__.py 那套 FastAPI 路由是另一条入口，改了它这条不会跟着变。
            "get_compiled_schema": lambda r, d: compiler.get_compiled_schema(
                r.tenant_id, d["knowledge_base_id"], d.get("data_source_id"),
            ),
            # ── KB 级 schema 生命周期（新鲜度读取 / 重编译预览，均只读）──
            # 新鲜度在读时计算（不落列、不自动重编译）；预览只做 diff 不写库。
            "get_kb_compiled_schema_status": lambda r, d: compiler.get_kb_schema_status(
                r.tenant_id, d["knowledge_base_id"],
            ),
            "preview_kb_compiled_schema_recompile": lambda r, d: compiler.preview_recompile(
                r.tenant_id, d["knowledge_base_id"],
            ),
            # ── 跨数据源抽象 schema（概念对齐层）──
            # 与 compiled schema 的区别：那份是「某个数据源的表结构」，这份是
            # 「把各源的概念抽象起来、记住每个概念在各源里分别叫什么」。
            # 生成/确认都写库（_SPACE_WRITE_ACTIONS），读取是空间内读。
            "get_abstract_schema": lambda r, d: abs_schema.get(
                r.tenant_id, d["knowledge_base_id"],
            ),
            # 聚焦解析：prompt 原样透传（空串由服务层顶回 1001）；不落库、不占守卫
            "preview_focus_abstract_schema": lambda r, d: abs_schema.preview_focus(
                r.tenant_id, d["knowledge_base_id"], d.get("prompt") or "",
                user_id=r.user_id or "",
            ),
            "generate_abstract_schema": lambda r, d: abs_schema.generate(
                r.tenant_id, d["knowledge_base_id"],
                # 只认布尔 true：`{"force": "false"}`（JSON 字符串）在 bool() 下为真，
                # 会悄悄走「覆盖 active」那条路 —— 而 force 的存在就是为了挡住它。
                user_id=r.user_id or "", force=d.get("force") is True,
                # 原样透传（形状/上限校验在服务层）：缺失或空数组 = 现状全量路径
                focus_seeds=d.get("focus_seeds"),
                # replace=全部重新构造；append=只增不删追加（服务层校验）
                mode=d.get("mode", "replace"),
                # 缺省结构化（旧行为）；"document" 是显式的「复用文档本体」选项。
                seed=d.get("seed", "structured"),
                auto_landings=d.get("auto_landings") is True,
                llm_names=d.get("llm_names") if isinstance(d.get("llm_names"), bool) else None,
                # 业务词表（可选的「客户会怎么说」）；解析在服务层，未命中只是 warning。
                alias_hints=d.get("alias_hints") if isinstance(d.get("alias_hints"), str) else "",
            ),
            "construct_abstract_schema": lambda r, d: abs_schema.construct(
                r.tenant_id, d["knowledge_base_id"],
                user_id=r.user_id or "",
                # 布尔口径同 force：只认真 `true`，字符串 "false" 不当真。
                force=d.get("force") is True,
                llm_names=d.get("llm_names") if isinstance(d.get("llm_names"), bool) else None,
                # "document" = 以文档本体为种子；缺省/非法值由服务层决定（非法 1001）。
                seed=d.get("seed", "structured"),
                # 只认真布尔 true（同 force）：字符串 "true" 不当真。
                auto_landings=d.get("auto_landings") is True,
                mode=d.get("mode", "append"),
                focus_seeds=d.get("focus_seeds"),
                alias_hints=d.get("alias_hints") if isinstance(d.get("alias_hints"), str) else "",
            ),
            # status **原样透传、不给默认值**：它是「这次保存算不算确认」的载体。
            # 漏传时若在这里兜成 "proposed"，一次改名就会把已确认（active，正在参与
            # 检索）的对齐**静默降级**，全链路不报错。交给 service 层判：
            # `save()` 的 `status not in ABSTRACT_STATUSES` 会把 None 顶回 1001。
            "save_abstract_schema": lambda r, d: abs_schema.save(
                r.tenant_id, d["knowledge_base_id"], d.get("schema") or {},
                status=d.get("status"),
                edited_by=r.user_id or "",
                expected_version=d.get("expected_schema_version"),
            ),
            # TBox 导入（2026-10-13 线）：YAML → 映射 → 反序合并 → save（乐观锁）。
            # 布尔口径同 force：只认真 `true`；`dry_run` = 只算 diff 不写库（免版本）。
            "import_abstract_schema": lambda r, d: abs_schema.import_tbox(
                r.tenant_id, d["knowledge_base_id"], d.get("yaml_text") or "",
                user_id=r.user_id or "",
                expected_version=d.get("expected_schema_version"),
                dry_run=d.get("dry_run") is True,
            ),
            # 保存历史（2026-10-27）：handler 见 `_build_dispatch` 开头的
            # `_history_*` —— 空 KB 在进服务前按 1001 拒（防空间闸门被空串跳过）。
            "list_abstract_schema_snapshots": _history_list,
            "get_abstract_schema_snapshot": _history_get,
            "restore_abstract_schema_snapshot": _history_restore,
            # [jonex] 抽取入口：get_compiled_schema + 注入 KB 级同义词（语义见
            # _get_compiled_schema_for_extraction）。atomic-rag Stage4 本体抽取走这条，
            # 使它不再依赖容器 REST 层（该层默认已不挂载）。
            "get_compiled_schema_for_extraction": lambda r, d: self._get_compiled_schema_for_extraction(
                r.tenant_id, d["knowledge_base_id"],
            ),
            "save_compiled_schema": lambda r, d: compiler.save_compiled_schema(
                r.tenant_id, d["knowledge_base_id"],
                d.get("entity_types", []), d.get("relation_types", []),
                edited_by=r.user_id,
                constraints=(d["constraints"] if "constraints" in d else None),
                expected_schema_version=d.get("expected_schema_version"),
                data_source_id=d.get("data_source_id"),
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
            # ── 更新模型（文档行操作 → nexonto 再解析 → 草稿 → 人工确认导入）──
            "start_model_update": lambda r, d: model_update.start(
                r.tenant_id, d["knowledge_base_id"], d["document_id"],
                user_id=r.user_id, username=r.username, ip=r.ip,
            ),
            "get_model_update_status": lambda r, d: model_update.get_status(
                r.tenant_id, d["knowledge_base_id"], d["job_id"],
            ),
            "apply_model_update": lambda r, d: model_update.apply(
                r.tenant_id, d["knowledge_base_id"], d["job_id"],
                user_id=r.user_id, username=r.username, ip=r.ip,
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
            # ── 血缘边（KB 设置 → 血缘页签；逐条 CRUD）──
            #
            # ⚠️ 这里**没有** `self.lineage_edges` 属性、也不该有：`KnowledgeBaseCapability`
            # **没有 `self.session`**（`__init__` 只有 `self.service = KnowledgeBaseService()`），
            # 而门面里的服务全是**无参构造**、各自 `async with get_db_session()` 开会话；
            # 血缘服务却是**注入会话**的（`LineageEdgeService(session)`，它对既有单测友好
            # 正因如此，不改）。折中：由模块级 `_with_lineage_service` 按动作开会话再递进去。
            "list_lineage_edges": lambda r, d: _with_lineage_service(
                lambda s: s.list(r.tenant_id, d["knowledge_base_id"],
                                 d.get("data_source_id"))),
            # 路径评分查询（P2b Task 1）：边池/打分复用 ② 的 `search_paths` ——
            # 画布高亮与模型走的必须是同一张图、同一套分数。
            # 数值越界由服务层夹取（1001 只留给非整数/空白锚点），这里只传可选值。
            "list_lineage_paths": lambda r, d: olp.get_lineage_paths(
                r.tenant_id, d["knowledge_base_id"], d["data_source_id"], d["anchor"],
                target=d.get("target"),
                max_hops=d.get("max_hops", 3),
                max_paths=d.get("max_paths", 5),
                min_hops=d.get("min_hops", 1),
            ),
            "analyze_lineage_impact": lambda r, d: lig.analyze_lineage_impact(
                r.tenant_id, d["knowledge_base_id"], d["data_source_id"], d["table"],
                column=d.get("column"),
                direction=d.get("direction", "downstream"),
                hops=d.get("hops", 3),
                min_origin=d.get("min_origin"),
            ),
            "get_lineage_coverage": lambda r, d: lcov.get_lineage_coverage(
                r.tenant_id, d["knowledge_base_id"], d["data_source_id"]),
            # ⚠️ `user_id` 必须带上：`created_by` 记的是**谁录的边**，与人审留痕同口径
            # （漏传不会报错，只会静默留空 —— 属于「悄悄放行」那一类）。
            "create_lineage_edge": lambda r, d: _with_lineage_service(
                lambda s: s.create(r.tenant_id, d,
                                   user_id=getattr(r, "user_id", None))),
            # ⚠️⚠️ **必须带 `knowledge_base_id`**（审查 M-8）：`update`/`delete` 在服务层
            # 原先只按 `(tenant_id, edge_id)` 定位 ⇒ 同租户内**另一个 KB 的边**也能被改/删。
            # 把它列进 `_REQUIRED_FIELDS` 并传进服务（服务侧并入行定位条件）才算收口 ——
            # 服务侧单独加参数而这里不传 = 纸面收口，所以本处与 `_REQUIRED_FIELDS`
            # 那两行是一条链上的两端，改一处必须改另一处。
            # ⚠️ I-1（2026-10-17 修复批 2）：这两条**不再是哑 lambda** —— `validate_input`
            # 只认 `is not None`（`""` 算「已给」⇒ 放行），`_resolve_space_context` 却用
            # `if not kb_id`（`""` 算「没给」⇒ 空间/授权闸门**整段跳过**），空串从缝里穿过
            # 就绕掉闸门 + 行定位不带 KB 条件 ⇒ 同租户内改/删别的 KB 的边（审查端到端复现）。
            # 改走模块级 handler：**先** `_require_nonblank` 把空串按缺参拒掉（1001），
            # **再**进服务；服务侧 `is not None` ⇒ `_ensure_kb("")` ⇒ 1002 是第二层兜底。
            # （list/create 不挂空串检查：它们的 `""` 到不了写路径 —— 服务侧一律先
            # `_ensure_kb` ⇒ 1002；要不要统一成 1001 是另拍的口径，见修复批 1 的记账。）
            "update_lineage_edge": _update_lineage_edge,
            "delete_lineage_edge": _delete_lineage_edge,
            # ── 血缘禁止同查表对 ──
            "list_lineage_forbidden_pairs": lambda r, d: _with_lineage_forbidden_pair_service(
                lambda s: s.list(r.tenant_id, d["knowledge_base_id"],
                                 d.get("data_source_id"), d.get("status"))),
            "create_lineage_forbidden_pair": lambda r, d: _with_lineage_forbidden_pair_service(
                lambda s: s.create(r.tenant_id, d,
                                   user_id=getattr(r, "user_id", None))),
            "update_lineage_forbidden_pair": _update_lineage_forbidden_pair,
            "delete_lineage_forbidden_pair": _delete_lineage_forbidden_pair,
            "suggest_lineage_forbidden_pairs": lambda r, d: _with_lineage_forbidden_pair_service(
                lambda s: s.suggest(r.tenant_id, d["knowledge_base_id"], d["ds_id"],
                                    user_id=getattr(r, "user_id", None))),
            # ── 数据治理控制面 P1（#11）──
            "list_governance_labels": lambda r, d: _with_gov_label_service(
                lambda s: s.list(r.tenant_id, d["knowledge_base_id"],
                                 d.get("data_source_id"), d.get("table_name"))),
            "create_governance_label": lambda r, d: _with_gov_label_service(
                lambda s: s.create(r.tenant_id, d,
                                   user_id=getattr(r, "user_id", None))),
            "update_governance_label": _update_governance_label,
            "delete_governance_label": _delete_governance_label,
            "governance_sensitive_scan": _governance_sensitive_scan,
            "governance_adopt_sensitive_scan": _governance_adopt_sensitive_scan,
            "list_governance_tags": lambda r, d: _with_gov_tag_service(
                lambda s: s.list(r.tenant_id, d["knowledge_base_id"])),
            "create_governance_tag": lambda r, d: _with_gov_tag_service(
                lambda s: s.create(r.tenant_id, d,
                                   user_id=getattr(r, "user_id", None))),
            "update_governance_tag": _update_governance_tag,
            "delete_governance_tag": _delete_governance_tag,
            "list_governance_clearance": lambda r, d: _with_gov_clearance_service(
                lambda s: s.list(r.tenant_id)),
            "set_governance_clearance": lambda r, d: _with_gov_clearance_service(
                lambda s: s.set(r.tenant_id, d,
                                granted_by=getattr(r, "user_id", None))),
            "list_governance_policies": lambda r, d: _with_gov_policy_service(
                lambda s: s.list(r.tenant_id, d["knowledge_base_id"])),
            "create_governance_policy": lambda r, d: _with_gov_policy_service(
                lambda s: s.create(r.tenant_id, d,
                                   user_id=getattr(r, "user_id", None))),
            "update_governance_policy": _update_governance_policy,
            "delete_governance_policy": _delete_governance_policy,
            "list_governance_audit": lambda r, d: _with_gov_audit_service(
                lambda s: s.list(
                    r.tenant_id, d["knowledge_base_id"],
                    user_id=d.get("user_id"), decision=d.get("decision"),
                    action=d.get("action"),
                    resource_table=d.get("resource_table"),
                    offset=d.get("offset", 0), limit=d.get("limit", 50))),
            "evaluate_governance_policy": _evaluate_governance,
            # ── 本体实例 → 可执行工具绑定 / 执行前解析 ──
            "list_tool_bindings": lambda r, d: tb.list(
                r.tenant_id, d["knowledge_base_id"], d.get("page", 1), d.get("page_size", 20),
                entity_type=d.get("entity_type"),
            ),
            "list_bindable_tools": lambda r, d: tb.list_bindable_tools(r.tenant_id, d["knowledge_base_id"]),
            "create_tool_binding": lambda r, d: tb.create(r.tenant_id, d, user_id=r.user_id),
            "update_tool_binding": lambda r, d: tb.update(
                r.tenant_id, d["binding_id"], d, user_id=r.user_id,
            ),
            "delete_tool_binding": lambda r, d: _deleted(tb.delete(r.tenant_id, d["binding_id"])),
            # 执行器唯一入口：只解析、不执行（business_domain 执行器经 Sidecar 调用）
            "resolve_action_execution": lambda r, d: tb.resolve_action_execution(r.tenant_id, d),
            # 执行器代真实用户取计划：与上面同一份解析，但调用人必须有知识库写权限
            # （写组判定在 execute()；无用户身份的调用在这里先拒，不走「无身份即跳过」）
            "prepare_action_execution": lambda r, d: _user_only(
                r, lambda: tb.resolve_action_execution(r.tenant_id, d),
            ),
            # ── 知识库工具（KB 级工具目录）──
            "list_kb_tools": lambda r, d: kt.list(
                r.tenant_id, d["knowledge_base_id"], d.get("page", 1), d.get("page_size", 50),
                keyword=d.get("keyword"),
            ),
            "create_kb_tool": lambda r, d: kt.create(r.tenant_id, d, user_id=r.user_id),
            "update_kb_tool": lambda r, d: kt.update(r.tenant_id, d["tool_id"], d, user_id=r.user_id),
            "delete_kb_tool": lambda r, d: _deleted(kt.delete(r.tenant_id, d["tool_id"])),
            "test_kb_tool": lambda r, d: kt.test(
                r.tenant_id, d["tool_id"], d, user_id=r.user_id, username=r.username,
                request_id=getattr(r, "request_id", None),
            ),
            "run_kb_tool": lambda r, d: _internal_only(r, lambda: kt.run(r.tenant_id, d)),
            "preview_extract_tool": _preview_extract_tool,
            # ── 知识库信息管理 ──
            "list_knowledge_info": lambda r, d: kbinfo.list(
                r.tenant_id, d.get("space_id"), d.get("status"),
                d.get("keyword"), d.get("offset", 0), d.get("limit", 20),
                user_id=r.user_id, scope=d.get("scope") or "manage",
            ),
            # [jonex] 本部署可创建的 KB 类型：前端「新建知识库」弹窗据此过滤选项。
            # 这是**下发口**；真正的闸门在 `kbinfo.create` → `resolve_creatable_kb_type()`。
            # 两边都要有：隐藏选项**不等于**拦住请求（直接 POST kb_type=openkb 一样能提交）。
            # ⚠ handler 必须是 async —— execute() 无条件 await，见 _kb_type_options 的注释。
            "get_kb_type_options": _kb_type_options,
            "create_knowledge_info": lambda r, d: kbinfo.create(r.tenant_id, d),
            "get_quota_view": lambda r, d: quota.get_quota_view(r.tenant_id, d.get("knowledge_base_id")),
            "get_knowledge_info": lambda r, d: kbinfo.get(
                d["kb_id"], r.tenant_id, user_id=r.user_id,
                with_stats=bool(d.get("with_stats", False))),
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
            "get_kb_permission_candidates": lambda r, d: _candidates_result(
                kbinfo.get_permission_candidates(d["kb_id"], r.tenant_id, user_id=r.user_id)
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
            "get_space_permission_candidates": lambda r, d: _candidates_result(
                s.get_permission_candidates(d["space_id"], r.tenant_id, user_id=r.user_id)
            ),
            # ── 领域服务 ──
            "list_services": lambda r, d: sv.list(
                r.tenant_id, d.get("space_id"), d.get("offset", 0), d.get("limit", 20),
                user_id=r.user_id, scope=d.get("scope") or "manage",
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
            # D4：search_service（MCP / API Key 链路）不在 _SEARCH_ACTIONS 里，
            # 但它同样要埋点（否则整条链路漏计）。这里把 trace_id 透传下去，
            # 让一次服务检索落下的 N 条 history 共享同一个 trace
            # （原先 DomainServiceService.search 内部硬编码 trace_id=""，
            #  N 条 history 的 request_id 全为空 → 事件无法关联）。
            "search_service": lambda r, d: sv.search(
                d["service_id"], r.tenant_id, d["query"],
                actor_user_id=r.user_id, trace_id=r.request_id,
            ),
            # ── 引用富化 ──
            "resolve_references": lambda r, d: search.resolve_references(
                r.tenant_id,
                doc_ids=d.get("doc_ids"),
                refs=d.get("refs"),
                actor=str(r.user_id or ""),
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
            # ETL 来源的血缘边文件导入：网关把文件字节编成 content_b64（≤5MB 在网关侧挡）
            "import_lineage_file": lambda r, d: ds.import_lineage_file(
                r.tenant_id, d["ds_id"], d.get("filename") or "",
                _decode_lineage_content(d),
                user_id=getattr(r, "user_id", None)),
            # [jonex] 数据库数据源：读库表结构（只读 information_schema，不取业务数据）
            "read_data_source_catalog": lambda r, d: ds.read_catalog(r.tenant_id, d["ds_id"]),
            # 由 catalog 生成该数据源自己那份 schema。compiler 从 _build_dispatch 开头
            # （`compiler = self.service.compiler`）闭包进来——保存那一步归编译器管，
            # DataSourceService 不持有它，免得两个服务互相 import 出环。
            "generate_ds_schema": lambda r, d: ds.generate_schema(r.tenant_id, d["ds_id"], compiler),
            # 手工重建数据资产子图（P1.5a）：只碰 database 源；data_source_id 可选。
            "rebuild_ontology_data_assets": lambda r, d: ds.rebuild_data_assets(
                r.tenant_id, d["knowledge_base_id"], d.get("data_source_id")),
            # 数据资产子图只读查询（P1.5a Task 4）：table 缺省 = 全部节点/边；
            # direction/hops 的合法性由服务层纯函数统一收口（1001）。
            "list_ontology_data_assets": lambda r, d: oda.get_data_assets(
                r.tenant_id, d["knowledge_base_id"], d["data_source_id"],
                table=d.get("table"), direction=d.get("direction") or "both",
                hops=d.get("hops") or 1,
            ),
            # ── 跨模态对齐（P1.5b）：建议生成 / 列举 / 人审 ──
            # `created_by` 记触发建议的人（漏传不会报错，只会静默留空）。
            "suggest_ontology_alignments": lambda r, d: align.generate(
                r.tenant_id, d["knowledge_base_id"],
                created_by=getattr(r, "user_id", None)),
            "list_ontology_alignments": lambda r, d: align.list(
                r.tenant_id, d["knowledge_base_id"], d.get("status")),
            # status 原样透传、不给默认值：缺省/非法由服务层 `validate_status` 顶回
            # 1001 —— 在这里兜一个 "active" 会让一次漏参静默生效（审批语义最忌）。
            "set_ontology_alignment_status": lambda r, d: align.set_status(
                r.tenant_id, d["knowledge_base_id"], d["alignment_id"],
                d.get("status")),
            # 纯数据检索（/search/data）：KB → database 源 → ② 扇出。
            "search_data": lambda r, d: self._search_data_action(r, d, nl),
            "search_ontology_data": lambda r, d: self._search_ontology_data_action(r, d, nl),
            # ② NL→SQL：一问一源一条 SQL。身份从 CapabilityRequest 带下去 ——
            # 判权那句「没有 user_id 就放行」意味着漏传即静默放行，审计也会缺人。
            "ask_data_source": lambda r, d: nl.ask_data_source(
                r.tenant_id, d["knowledge_base_id"], d["question"],
                data_source_id=d.get("data_source_id") or "",
                entity_name=d.get("entity_name") or "",
                observe_only=bool(d.get("observe_only")),
                # evidence/hints（P0-A2）：str / list[str]，服务层按开关归一化。
                hints=d.get("hints"),
                # 演化基准臂（Task 4）：原样透传 run id（空串 = 普通提问）。
                evolution_run_id=str(d.get("evolution_run_id") or ""),
                offset=d.get("offset") or 0,
                user_id=r.user_id or "", username=r.username or "", ip=r.ip or "",
            ),
            # W4.1 确定性指标问数（不调 LLM）：group_by/time_grain 显式传入。
            "ask_metric": lambda r, d: nl.ask_metric(
                r.tenant_id, d["knowledge_base_id"], d["question"],
                data_source_id=d.get("data_source_id") or "",
                group_by=d.get("group_by"),
                time_grain=d.get("time_grain"),
                limit=d.get("limit"),
                offset=d.get("offset") or 0,
                time_from=d.get("time_from"),
                time_to=d.get("time_to"),
                context=d.get("context"),
                compare=d.get("compare"),
                user_id=r.user_id or "", username=r.username or "", ip=r.ip or "",
            ),
            # W4.5 血缘检索（只读）：问题/概念 → 指标/实体/表/列候选，
            # 命中项沿血缘扩展一跳/两跳；max_hops 由调用方显式给。
            "search_lineage_assets": lambda r, d: nl.search_lineage_assets(
                r.tenant_id, d["knowledge_base_id"], d["query"],
                data_source_id=d.get("data_source_id") or "",
                limit=d.get("limit") if d.get("limit") is not None else 20,
                max_hops=d.get("max_hops") if d.get("max_hops") is not None else 1,
                user_id=r.user_id or "",
            ),
            # W4.7 澄清池（只读）：ask 重算；drop/add 编辑调用方池子。
            "clarify_metric": lambda r, d: nl.clarify_metric(
                r.tenant_id, d["knowledge_base_id"], d["question"],
                data_source_id=d.get("data_source_id") or "",
                pool=d.get("pool"),
                action=d.get("action") or "ask",
                question_id=d.get("question_id") or "",
                text=d.get("text") or "",
                need_time=bool(d.get("need_time")),
                user_id=r.user_id or "",
            ),
            # W4.8 指标级调用审批：列表只读；申请写 pending；
            # 审批/下线在服务内再校验租户管理员。
            "list_metric_call_approvals": lambda r, d: mca.list_approvals(
                r.tenant_id, d["knowledge_base_id"], status=d.get("status")),
            "request_metric_call_approval": lambda r, d: mca.request_approval(
                r.tenant_id, d["knowledge_base_id"], d["metric_name"],
                user_id=r.user_id or "", reason=d.get("reason") or ""),
            "approve_metric_call": lambda r, d: mca.decide_approval(
                r.tenant_id, d["knowledge_base_id"], d["metric_name"],
                "approved", user_id=r.user_id or "",
                reason=d.get("reason") or ""),
            "suspend_metric_call": lambda r, d: mca.decide_approval(
                r.tenant_id, d["knowledge_base_id"], d["metric_name"],
                "suspended", user_id=r.user_id or "",
                reason=d.get("reason") or ""),
            # ── 领域级连接池（按 space_id 隔离）──
            "list_ds_connections": lambda r, d: dsc.list_connections(
                r.tenant_id, d["space_id"]),
            "get_ds_connection": lambda r, d: dsc.get_connection(
                r.tenant_id, d["conn_id"], d["space_id"]),
            "create_ds_connection": lambda r, d: dsc.create_connection(r.tenant_id, d),
            "update_ds_connection": lambda r, d: dsc.update_connection(
                r.tenant_id, d["conn_id"], d, d["space_id"]),
            "delete_ds_connection": lambda r, d: dsc.delete_connection(
                r.tenant_id, d["conn_id"], d["space_id"]),
            "test_ds_connection": lambda r, d: dsc.test_connection(
                r.tenant_id, d["conn_id"], d["space_id"]),
            "read_ds_connection_catalog": lambda r, d: dsc.read_connection_catalog(
                r.tenant_id, d["conn_id"], d["space_id"]),
            # ── 解析引擎设置 ──
            "list_parser_settings": lambda r, d: ps.list_settings(r.tenant_id, d["knowledge_base_id"]),
            "create_parser_setting": lambda r, d: ps.create_setting(r.tenant_id, d),
            "update_parser_setting": lambda r, d: ps.update_setting(r.tenant_id, d["setting_id"], d),
            "delete_parser_setting": lambda r, d: ps.delete_setting(r.tenant_id, d["setting_id"]),
            # ── KB 级模型配置（解析/编译阶段的模型覆盖）──
            "list_model_settings": lambda r, d: ms.list_settings(r.tenant_id, d["knowledge_base_id"]),
            "save_model_settings": lambda r, d: ms.save_settings(
                r.tenant_id, d["knowledge_base_id"], d.get("items") or [], user_id=r.user_id
            ),
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
            # OpenLineage 事件（内部 action，tenant 由网关从 key 解出后带下）
            "ingest_lineage_event": lambda r, d: ds.ingest_lineage_event(
                r.tenant_id, d["ds_id"], d.get("ingest_key") or "", d.get("event") or {}),
            # ── 演化台服务化（②a）：runs / ledgers / corpus ──
            # 一律走模块级 handler：它们是**函数内 import** 的（演化服务依赖 SQLAlchemy
            # 模型与会话），且 handler 名在测试里可替换、能把「闸门之后 handler 真被调用」
            # 与「被拒后一次都没进去」分开钉死。
            "evolution_status": lambda r, d: _evolution_status(r, d),
            "evolution_list_runs": lambda r, d: _evolution_list_runs(r, d),
            "evolution_get_run": lambda r, d: _evolution_get_run(r, d),
            "evolution_run_log": lambda r, d: _evolution_run_log(r, d),
            "evolution_list_versions": lambda r, d: _evolution_list_versions(r, d),
            "evolution_get_version": lambda r, d: _evolution_get_version(r, d),
            "evolution_start_run": lambda r, d: _evolution_start_run(r, d),
            "evolution_list_ledgers": lambda r, d: _evolution_list_ledgers(r, d),
            "evolution_list_corpus_cases": lambda r, d: _evolution_list_corpus_cases(r, d),
            "evolution_create_corpus_case": lambda r, d: _evolution_create_corpus_case(r, d),
            "evolution_update_corpus_case": lambda r, d: _evolution_update_corpus_case(r, d),
            "evolution_delete_corpus_case": lambda r, d: _evolution_delete_corpus_case(r, d),
            "evolution_promote_adhoc": lambda r, d: _evolution_promote_adhoc(r, d),
            "evolution_adopt_run": lambda r, d: _evolution_adopt_run(r, d),
            "evolution_adhoc_evidence": lambda r, d: _evolution_adhoc_evidence(r, d),
            "evolution_adopt_runs": lambda r, d: _evolution_adopt_runs(r, d),
            "evolution_reject_run": lambda r, d: _evolution_reject_run(r, d),
            "evolution_rollback_run": lambda r, d: _evolution_rollback_run(r, d),
            "evolution_adjust_entry": lambda r, d: _evolution_adjust_entry(r, d),
            "evolution_edit_entry": lambda r, d: _evolution_edit_entry(r, d),
            # ── 演化台服务化（②b）：失败诊断 ──
            "evolution_list_failures": lambda r, d: _evolution_list_failures(r, d),
            "evolution_run_diagnosis": lambda r, d: _evolution_run_diagnosis(r, d),
            "evolution_latest_diagnosis": lambda r, d: _evolution_latest_diagnosis(r, d),
            "evolution_list_dislikes": lambda r, d: _evolution_list_dislikes(r, d),
            "evolution_dislike_context": lambda r, d: _evolution_dislike_context(r, d),
            "evolution_replay_dislike": lambda r, d: _evolution_replay_dislike(r, d),
            "evolution_adopt_dislike_alias": lambda r, d: _evolution_adopt_dislike_alias(r, d),
            "evolution_metric_options": lambda r, d: _evolution_metric_options(r, d),
            "evolution_concept_options": lambda r, d: _evolution_concept_options(r, d),
            "evolution_tool_options": lambda r, d: _evolution_tool_options(r, d),
            "evolution_adopt_metric": lambda r, d: _evolution_adopt_metric(r, d),
            # ── 演化信任闭环（Task 9）：采纳/基准指标 ──
            "evolution_metrics": lambda r, d: _evolution_metrics(r, d),
            # ── 演化语料治理（Task 5）：覆盖率（只读） ──
            "evolution_corpus_coverage": lambda r, d: _evolution_corpus_coverage(r, d),
            # ── 演化语料治理（Task 6）：自动播种 ──
            "evolution_seed_corpus": lambda r, d: _evolution_seed_corpus(r, d),
        }

        # [jonex] 任务中心接入：运行方那两个入口（§4.1 ① 派活 `execute` / ② 删除
        # `remove_task`）。**就这一行，别自己往 `dispatch` 里塞方法** —— `bind()` 兜住
        # 两个会静默坏掉的坑（签名必须是两个位置参数 `(request, data)`；回执必须是
        # 裸三层壳 dict）。塞 `runner.execute` 会 `TypeError` → 兜成 500 → 子任务直接
        # 终态 `failed`；回 pydantic 对象会被判成「没送达」→ 传送层重发，同一个
        # `execution_id` 最多被调 3 次（详见 `RunnerBase.bind` 的 docstring）。
        from .dtos.wiki_graph import GRAPH_ACTIONS
        dispatch["query_wiki_resources"] = lambda r, d: parse.query_wiki_resources(r.tenant_id, d, actor=str(r.user_id or ""))
        for action, operation in {"list_wiki_scenarios": "list", "read_wiki_scenario_resources": "resources",
            "configure_wiki_scenario": "configure", "run_wiki_scenario": "interpret", "review_wiki_scenario": "review",
            "select_wiki_scenario_resources": "selection", "read_wiki_scenario_materials": "materials",
            "read_wiki_scenario_material": "material", "annotate_wiki_scenario": "annotate"}.items():
            dispatch[action] = lambda r, d, op=operation: parse.wiki_scenario(r.tenant_id, d, operation=op, actor=str(r.user_id or ""))
        for action, operation in GRAPH_ACTIONS.items():
            dispatch[action] = lambda r, d, op=operation: parse.query_wiki_graph(r.tenant_id, op, d)
        dispatch.update(self.service.task_runner.bind())
        return dispatch

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

    async def _get_document_quality_reports(self, request, data):
        """批量质量摘要（B6 §5.1）。

        全被密级过滤时直接返回空 items：传空 list 给服务会撞它 1..100 的
        入参闸（400），而「全部不可见」对调用方应当等价于空列表。
        """
        if data.get("_classification_all_filtered"):
            return {"items": []}
        return await self.service.documents.get_document_quality_reports(
            request.tenant_id, data["knowledge_base_id"], data["document_ids"],
        )

    async def _allowed_kb_ids(
        self, tenant_id: str, user_id: str | None, kb_ids: list[str],
    ) -> list[str]:
        """按用户可读最高等级过滤 KB（B6 §5.1 多 KB 检索）。

        租户管理员全放行；其余按 KB 内最高生效 rank 与用户 rank 比较。
        保持输入顺序与重复项。

        ⚠ 无 user / "anonymous" **不再原样放行**（B6 收口）：MCP 工具
        （`/internal/kb/invoke`）就是无用户身份的机器调用，原来的早退让整条
        MCP 检索链路绕过了密级过滤。机器身份按「最低启用等级」处理（fail closed），
        与文档级守卫对空用户的既有口径一致。
        """
        kb_ids = list(kb_ids or [])
        if not kb_ids:
            return kb_ids

        from jonex_core.security import classification_access as policy
        from .services.space_permission_service import is_tenant_admin

        if user_id and user_id != "anonymous":
            if await is_tenant_admin(tenant_id, user_id):
                return kb_ids
        async with get_db_session() as session:
            max_ranks = await policy.kb_max_level_rank(session, tenant_id, kb_ids)
            user_rank = await policy.get_user_max_rank(
                session, tenant_id, user_id, is_tenant_admin=False,
            )
        if user_rank is None:
            return kb_ids
        allowed = [
            kb_id for kb_id in kb_ids if max_ranks.get(kb_id, 0) <= user_rank
        ]
        if len(allowed) < len(kb_ids):
            logger.info(
                "检索批量跳过：%d 个知识库超密级（user=%s）",
                len(kb_ids) - len(allowed), user_id,
            )
        return allowed

    def _make_search_handler(self, search_method_name: str):
        """创建 search handler：解析 service_id → kb_ids → 调用对应的 search 方法。

        消除 ``_query_with_ontology`` / ``_deep_query`` / ``_search_llmwiki`` 三者的结构重复。
        """
        async def _handler(request, data):
            kb_ids = await self._resolve_service_id_to_kb_ids(data, request.tenant_id)
            kb_ids = await self._allowed_kb_ids(
                request.tenant_id, _user_id(request), kb_ids,
            )
            if not kb_ids:
                # B6 §5.1：全部 KB 被密级过滤 ⇒ 统一空结果（不把空结果当异常）
                return {
                    "answer": "",
                    "references": [],
                    "knowledge_base_ids": [],
                    "reasoning": {},
                }
            data = dict(data, knowledge_base_ids=kb_ids)
            if search_method_name == "search_jonex_wiki":
                data.pop("service_id", None)
            method = getattr(self.service.search, search_method_name)
            return await method(
                request.tenant_id, _user_id(request), data,
                # D3：request_id 已在 execute 开头规范化，这里不再二次生成
                trace_id=request.request_id,
            )
        return _handler

    async def _query_with_ontology(self, request: CapabilityRequest, data: dict[str, Any]) -> dict:
        return await self._make_search_handler("query_with_ontology")(request, data)

    async def _deep_query(self, request: CapabilityRequest, data: dict[str, Any]) -> dict:
        return await self._make_search_handler("deep_query")(request, data)

    async def _search_llmwiki(self, request: CapabilityRequest, data: dict[str, Any]) -> dict:
        return await self._make_search_handler("search_llmwiki")(request, data)

    async def _search_jonex_wiki(self, request: CapabilityRequest, data: dict[str, Any]) -> dict:
        return await self._make_search_handler("search_jonex_wiki")(request, data)

    async def _start_wiki_search(self, request, data):
        from .services.wiki_search_session_service import WikiSearchSessionService
        kb_ids = await self._resolve_service_id_to_kb_ids(data, request.tenant_id)
        kb_ids = await self._allowed_kb_ids(
            request.tenant_id, _user_id(request), kb_ids,
        )
        if not kb_ids:
            # B6 §5.1：单/多 KB 检索全部被密级过滤 ⇒ 404，不泄露存在性
            raise ResourceNotFoundError(message="知识库不存在或无权访问")
        data = dict(data)
        data.pop("service_id", None)
        return await WikiSearchSessionService().start(
            request.tenant_id, _user_id(request), dict(data, knowledge_base_ids=kb_ids),
            self.service.search, request.request_id)

    async def _poll_wiki_search(self, request, data):
        from .services.wiki_search_session_service import WikiSearchSessionService
        return await WikiSearchSessionService().poll(request.tenant_id, _user_id(request), data["session_id"])

    async def _cancel_wiki_search(self, request, data):
        from .services.wiki_search_session_service import WikiSearchSessionService
        return await WikiSearchSessionService().cancel(request.tenant_id, _user_id(request), data["session_id"])

    async def _resolve_wiki_evidence(self, request, data):
        from .services.wiki_search_session_service import resolve_wiki_evidence
        return await resolve_wiki_evidence(request.tenant_id, data)

    async def _search_mix(self, request: CapabilityRequest, data: dict[str, Any]) -> dict:
        return await self._make_search_handler("search_mix")(request, data)

    async def _assert_kb_searchable(
        self, request: CapabilityRequest, data: dict[str, Any],
    ) -> dict:
        """流式检索网关的写前预检（B2）—— 只回执，不读 KB 内容。

        空间/KB 授权在 execute 的 `_SEARCH_ACTIONS` 分支由 `_filter_search_kbs`
        判定；KB 密级由 `classification_access_guard._KB_ACTIONS` 判定。本 handler
        在两者之后执行，能走到这里即表示「可检索」，返回 kb id 供调用方确认。
        """
        kb_id = data.get("knowledge_base_id")
        if not str(kb_id or "").strip():
            # 纵深防御：execute 不做必填校验（validate_input 无调用方），缺字段会
            # 在下面 KeyError → 500。预检缺 KB 应明确 400，而不是让调用方猜。
            raise InvalidParameterError(message="knowledge_base_id 不能为空")
        return {"knowledge_base_id": kb_id}

    async def _record_stream_search(
        self, request: CapabilityRequest, data: dict[str, Any],
    ) -> dict:
        """流式检索请求埋点（`/documents/search/stream` 直连 Sidecar，绕过 execute 埋点）。

        只写请求事件（检索次数/入口），不含步骤/命中等明细 —— SSE 链路的命中
        在 gateway 解析，能力层拿不到。权限拒绝（预检 404）不会走到这里。
        """
        from .services.search_event_writer import write_request_event

        kb_ids = list(data.get("knowledge_base_ids") or [])
        if not kb_ids and data.get("knowledge_base_id"):
            kb_ids = [data["knowledge_base_id"]]
        await write_request_event(
            tenant_id=request.tenant_id,
            request_id=request.request_id,
            user_id=_user_id(request),
            query=data.get("query"),
            kb_ids=kb_ids,
            domain_space_id=data.get("domain_space_id"),
        )
        return {"recorded": True}

    async def _mark_template_scenario_published(
        self, request: CapabilityRequest, data: dict[str, Any],
    ) -> dict:
        """模板场景发布后：把绑定该场景的 KB schema 标为过期（标准侧联动）。

        business_domain 是独立容器，不能 import knowledge_base —— 该动作供它经
        Sidecar invoke 调用；无用户身份，仅做租户内数据联动。
        """
        from .services.ontology_compiler import OntologyCompiler

        return await OntologyCompiler().react_to_publish(
            request.tenant_id, data["scenario_id"],
        )

    async def _list_impacted_knowledge_bases(
        self, request: CapabilityRequest, data: dict[str, Any],
    ) -> dict:
        """模板场景变更影响哪些 KB（绑定表口径），供 business_domain 查询。"""
        from .services.ontology_compiler import OntologyCompiler

        items = await OntologyCompiler().list_impacted_knowledge_bases(
            request.tenant_id, data["scenario_id"],
        )
        return {"items": items, "total": len(items)}

    async def _query_image_ticket(self, request: CapabilityRequest, data: dict[str, Any]) -> dict:
        """校验查询图片可看，把身份交回 Gateway 签票（以图问知识）。

        能力侧只做**归属校验**（本人 + 仍被未删除历史引用，D10），签票交给 Gateway：
        token 的载荷是 `tenant + user + image_id`，其中 `user_id` 只有这里知道
        （Gateway 解的是它自己的用户 JWT，与服务端存的 user_id 未必同一形态）；
        而「票怎么签、地址长什么样」是接口层的事，service 不该知道。

        返回的 `user_id` 有意用 `_user_id(r)` 的**原值**（含 `anonymous` 那种形态）——
        校验在 service 里已经拒掉匿名，能走到这里说明身份是实的；Gateway 直接拿它签票。
        """
        user_id = _user_id(request)
        await self.service.query_image.check_viewable(request.tenant_id, user_id, data["image_id"])
        return {"image_id": data["image_id"], "user_id": user_id}

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

        if action in _SERVICE_ID_ACTIONS or action in _SERVICE_SEARCH_ACTIONS:
            # search_service 独立分组但同样需要 space_id 做「空间成员」判定；
            # 其 kb_id 恒为 None（KB 授权改由 execute 分支查 accessible kb，不依赖 kb_id 兜底）
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
        if action in _TOOL_BINDING_ID_ACTIONS:
            binding_id = data.get("binding_id")
            if not binding_id:
                return None, None, None
            return await _one(
                "SELECT ki.space_id, ki.kb_type, ki.id FROM knowledge_base.ontology_tool_bindings b "
                "JOIN knowledge_base.knowledge_info ki ON ki.id = b.knowledge_base_id "
                "  AND ki.is_deleted = 0 "
                "WHERE b.id=:bid AND b.tenant_id=:t AND b.is_deleted=0",
                {"bid": binding_id, "t": tenant_id},
            )
        if action in _KB_TOOL_ID_ACTIONS:
            tool_id = data.get("tool_id")
            if not tool_id:
                return None, None, None
            return await _one(
                "SELECT ki.space_id, ki.kb_type, ki.id FROM knowledge_base.kb_tools kt "
                "JOIN knowledge_base.knowledge_info ki ON ki.id = kt.knowledge_base_id "
                "  AND ki.is_deleted = 0 "
                "WHERE kt.id=:tid AND kt.tenant_id=:t AND kt.is_deleted=0",
                {"tid": tool_id, "t": tenant_id},
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
        # ⚠️ 必须先归一空白再判「没给」：服务层 `_require_snapshot_kb` 等会 `.strip()`，
        # 这里若用原值精确匹配 `knowledge_info.id`，`" kb_other "` 查不到 → 返回
        # (None, None, None) → execute 的 `if space_id:` 整段跳过 → 随后按 strip 后
        # 的真实 KB 读到/写入别人的快照（2026-10-27 审查发现的空间权限绕过）。
        # 归一后空间判定与服务层同口径，对所有 KB 类动作一起生效。
        if isinstance(kb_id, str):
            kb_id = kb_id.strip()
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
        from .services.space_permission_service import (
            get_active_space_ids,
            get_visible_space_ids,
        )

        user_id = _user_id(request)
        active_ids = await get_active_space_ids(request.tenant_id)
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
            # [jonex] 空间可见 ≠ 空间可用：管理员 visible=None 也必须被禁用态挡住。
            if space_id not in active_ids:
                raise PermissionDeniedError(
                    message=translate(
                        "err.space.disabled",
                        params={"space_id": space_id},
                        fallback=f"领域空间已禁用: {space_id}",
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
        # [jonex] 先剔除禁用空间下的 KB，**与调用者身份无关**：KB 授权是跨空间的，
        # 若只在 visible/granted 上做过滤，「非成员但有 KB 授权」的用户仍能读到
        # 禁用空间的内容。这一步必须在授权叠加之前。
        disabled_dropped = {row[0] for row in rows if row[1] not in active_ids}
        disabled_space_ids = {row[1] for row in rows if row[1] not in active_ids}
        rows = [row for row in rows if row[1] in active_ids]
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
        # [jonex] 入参 KB 全部落在禁用空间：明确报「空间已禁用」。
        # 否则数组形态会被回写成空列表，handler 抛「缺少 knowledge_base_ids 参数」——
        # 那个报错与真实原因无关，只会把排查引向参数拼装。
        if not accessible and disabled_dropped and len(disabled_dropped) == len(kb_ids):
            space_desc = ", ".join(sorted(disabled_space_ids))
            raise PermissionDeniedError(
                message=translate(
                    "err.space.disabled",
                    params={"space_id": space_desc},
                    fallback=f"领域空间已禁用: {space_desc}",
                )
            )
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
        """set_kb_permissions 判定：空间管理者、租户管理员，或 kb_manager 授权。

        （设计 §2.7：kb_manager 授权可放行；其余 KB 授权不可。）
        模块级依赖注入点：has_space_role / is_tenant_admin / get_kb_grant_role
        （经判定服务模块属性）。

        [jonex] B2（D2）：空间侧判据由 `("owner", "manager")` 改为 `SPACE_MANAGER`。
        """
        from jonex_core.common.exceptions import PermissionDeniedError
        from jonex_core.common.i18n import translate
        from .services.space_permission_service import (
            SPACE_MANAGER,
            has_space_role,
            is_tenant_admin,
        )
        from .services.kb_permission_service import (
            KB_MANAGER,
            get_kb_grant_role,
        )

        if (
            await has_space_role(tenant_id, space_id, actor, SPACE_MANAGER)
            or await is_tenant_admin(tenant_id, actor)
            or (kb_id and await get_kb_grant_role(tenant_id, kb_id, actor) == KB_MANAGER)
        ):
            return
        raise PermissionDeniedError(
            message=translate(
                "err.kb_permission.manage_required",
                params={"kb_id": kb_id or ""},
                fallback=f"仅空间管理者或租户管理员可管理知识库权限: {kb_id or ''}",
            )
        )

    async def _service_kb_ids(self, tenant_id: str, service_id: str) -> list[str]:
        """服务关联的 KB id 列表（search_service 判定用）；服务不存在/无关联 → []。"""
        from sqlalchemy import text

        if not service_id:
            return []
        async with get_db_session() as session:
            rows = (await session.execute(
                text(
                    "SELECT kb_id FROM knowledge_base.service_knowledge_bases "
                    "WHERE service_id=:sid AND tenant_id=:t AND is_deleted=0"
                ),
                {"sid": service_id, "t": tenant_id},
            )).all()
        return [r[0] for r in rows]

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

    async def _require_structured_enabled(self, tenant_id: str, data: dict) -> None:
        """KB 未开启结构化支持 → 1004；解析不出 KB 也拒绝（fail-closed）。"""
        kb_id = str((data or {}).get("knowledge_base_id") or "").strip()
        ds_id = str((data or {}).get("ds_id") or "").strip()
        async with get_db_session() as session:
            if not kb_id and ds_id:
                kb_id = await _kb_id_for_data_source(session, tenant_id, ds_id)
            if not kb_id:
                raise OperationNotSupportedError(
                    message="无法确定知识库，结构化数据能力不可用")
            enabled = await _is_structured_enabled(session, tenant_id, kb_id)
        if not enabled:
            raise OperationNotSupportedError(
                message="该知识库未启用结构化数据支持")

    async def _require_evolution_admin(
        self, request: CapabilityRequest, action: str, data: dict | None
    ) -> None:
        """演化台闸门：租户管理员直通，或目标 KB 的 `kb_manager`（KB 范围动作）。

        `is_tenant_admin` 对无 `user_id` / `anonymous` 恒 False —— 所以这道闸门
        必须放在 `skip_space_check` 块**之外**，否则伪造/残缺的 Authorization
        （网关解不出 user_id）会整段绕过它（同 `_DS_CONN_WRITE_ACTIONS` 的理由）。

        **安全顺序**（设计 §D1 / 失败模式表）：
        租户管理员 → 动作是否 KB 范围 → 调用方是否管理**至少一个** KB → 解析目标。
        「先判 granted 再解析目标」是为了不让非管理员拿 run_id / case_id /
        version_id 探测别的 KB 里有没有这个对象：granted 为空时一个查询都不发。
        """
        from .services.kb_permission_service import get_granted_manager_kb_ids
        from .services.space_permission_service import is_tenant_admin

        if await is_tenant_admin(request.tenant_id, request.user_id):
            return
        if action not in _EVOLUTION_KB_SCOPED_ACTIONS:
            raise PermissionDeniedError(message="演化台仅租户管理员可访问")
        granted = await get_granted_manager_kb_ids(
            request.tenant_id, request.user_id)
        if not granted:
            raise PermissionDeniedError(
                message="演化台仅租户管理员或目标知识库管理员可访问")
        targets = await _evolution_target_kb_ids(
            action, data, tenant_id=request.tenant_id)
        if targets is None:
            # 无目标的列表动作：过滤交给 handler（授权集合由
            # `_evolution_allowed_kb_ids` 提供）。
            return
        if not targets or not targets <= set(granted):
            raise PermissionDeniedError(
                message="无权操作该知识库的演化数据")

    async def _record_search_request(self, request: CapabilityRequest, action: str, data: dict) -> None:
        """[jonex] 数据运营二期：检索入口埋点。

        写在 `handler` **之前**，因此后面 try 内的三类异常
        （TenantIsolationError / JonexException / 兜底 Exception）全部能被记成
        「已接收但失败」—— 有请求事件、没有步骤事件。

        埋点门禁是 `_SEARCH_ACTIONS | {"search_service"}`：**不能**直接用
        `_SEARCH_ACTIONS` —— `search_service`（MCP / API Key 链路）不在那个集合里，
        用它会整条链路漏计；也**不要**把 `search_service` 塞进 `_SEARCH_ACTIONS`
        （那是权限过滤分组，语义不同）。

        kb 列表取 `data` 里**已过权限过滤**的值（`_filter_search_kbs` 在 handler
        之前就把过滤结果写回 `data["knowledge_base_ids"]`）；领域服务形态只有
        `service_id`、拿不到 kb → 空数组，聚合期按 `entry_service_id` 反查
        `service_knowledge_bases` 补齐。

        口径：权限拒绝（403）的请求**不计数** —— 埋点在这之后，那是 403 而非检索行为。
        预检类 action（`_METERING_EXCLUDED_SEARCH_ACTIONS`）同样不计数。

        MCP Key 链路（`request.context.mcp_key_id` 存在）：事件行照常保留（请求量
        与失败观测需要），但 `query` / `query_hash` 一律置空，只落 `mcp_key_id`
        用于归因 —— MCP 不持久化用户查询原文，也不写搜索历史。
        """
        if action in _METERING_EXCLUDED_SEARCH_ACTIONS:
            return
        if action not in _SEARCH_ACTIONS and action not in _SERVICE_SEARCH_ACTIONS:
            return
        from .services.search_event_writer import write_request_event

        kb_ids = (
            data.get("knowledge_base_ids")
            or ([data["knowledge_base_id"]] if data.get("knowledge_base_id") else [])
        )
        mcp_key_id = (request.context or {}).get("mcp_key_id")
        await write_request_event(
            tenant_id=request.tenant_id,
            request_id=request.request_id,
            user_id=_user_id(request),
            # MCP 不落查询原文；query=None 时 query_hash 自然为 None
            query=None if mcp_key_id else data.get("query"),
            kb_ids=list(kb_ids),
            entry_service_id=data.get("service_id"),
            domain_space_id=data.get("domain_space_id"),
            mcp_key_id=mcp_key_id,
        )

    async def _get_compiled_schema_for_extraction(
        self, tenant_id: str, knowledge_base_id: str
    ):
        """[jonex] 对外下发（atomic-rag 抽取入口）的 compiled schema —— 注入 KB 级同义词。

        与 `get_compiled_schema` 的**唯一差别**是多一步 `inject_synonyms`，因此两者
        不可互换：invoke 侧的 `get_compiled_schema` 有意不注入（避免污染查询链路），
        而抽取 prompt 需要同义词才能让 LLM 统一到 canonical 表述。

        本方法承载的正是原先只存在于容器 REST 层的语义（`api/__init__.py` 的
        `GET /ontology/compiled-schema`）—— 该层默认已不挂载（见 register_routes），
        atomic-rag 改经 `/invoke` 走这里，以保证抽取质量零回归。
        """
        schema = await self.service.compiler.get_compiled_schema(
            tenant_id, knowledge_base_id
        )
        if not schema:
            return schema
        return await self.service.compiler.inject_synonyms(
            tenant_id, knowledge_base_id, schema
        )

    async def execute(self, request: CapabilityRequest) -> CapabilityResponse:
        action = request.payload.get("action")
        data = request.payload.get("data") or {}
        # [jonex] 数据运营二期：request_id 规范化（**一处生成、全程复用**）。
        # 原先 `_make_search_handler` 与 `_dispatch["search"]` 各自
        # `request.request_id or str(uuid4())`，与入口是两次独立生成 ——
        # request_id 为空串时三者不同，请求事件与步骤/命中/采用事件永远关联不上。
        # 这里提前定值，下面各处一律直接用 request.request_id。
        # 副作用（有意）：request_id 为空的请求，其 CapabilityResponse 信封的
        # request_id 从 "" 变为生成的 uuid —— 空串本身就是缺陷。
        request.request_id = request.request_id or str(uuid4())
        handler = self._dispatch.get(action)

        if handler is None:
            return CapabilityResponse.error(request.request_id, 400, f"不支持的操作: {action}")

        try:
            # [jonex] B6 内部标记无条件清理（守卫之前）：`_classification_all_filtered`
            # 只能由 classification_access_guard 在「全被过滤」时设置。管理员/匿名/MCP
            # 链路会跳过守卫，客户端若自带该标记就会让批量摘要恒空。守卫内部仍会自己
            # pop（防御仍在），这里是更外层的兜底。
            data.pop("_classification_all_filtered", None)
            if action not in self._INTERNAL_ACTIONS:
                request.tenant_id = require_tenant(request.tenant_id)
            # 所有 action（含 ingest_push）都必须落在 active 租户上。
            # 这是 Web JWT、MCP Key、atomic-rag 内部 JWT 三条入口的**公共闸门**：
            # 上游（Gateway / mcp_server）只覆盖各自路径，无法保证直达 `/invoke`
            # 的调用方也做过租户状态判定。失败走 TenantIsolationError（403/3005），
            # 不区分「不存在 / 软删 / 停用」。
            await require_active_tenant(request.tenant_id)
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
            # [jonex] 连接池写闸门：**故意放在 `if not skip_space_check:` 之外**，
            # 对**所有**调用方无条件执行（actor 为 None / "anonymous" / MCP 一律照判）。
            #
            # 为什么不跟其他分支一起关在块内：那个块的放行口径是「没有 actor 就没有
            # 空间角色可查，所以跳过判定」—— 其余分支都要拿 actor 去查空间成员身份，
            # 无身份时确实无从判起。连接池这条**不需要任何 actor 语境**：判定只认
            # tenant_id（+ conn_id），跳过即等于「谁都能建/删连接」。而 actor 恰好是
            # 唯一一条由网关自己解出、经 invoke 报文体带进来的身份 —— Sidecar 走的是
            # API Key 分支、不认请求头里那份 Authorization，解不出来就是 None。
            # 也就是说，一个残缺/伪造的 `Authorization: Bearer x` 正好落在这个放行口上。
            # 无 user_id 时 is_tenant_admin 恒 False（_coerce_user_id → None）→ 拒绝。
            # ⚠ 这不是笔误，别把它挪回块内 —— 挪回去等于给上面那条链路重开写口子。
            if action in _DS_CONN_WRITE_ACTIONS:
                from .services.space_permission_service import is_tenant_admin

                # 连接池是租户级资源，没有空间/知识库可挂 —— 落到「租户管理员」这一档。
                # 与 _ANALYTICS_ACTIONS 不传 domain_space_id 时的口径一致。
                if not await is_tenant_admin(request.tenant_id, actor):
                    raise PermissionDeniedError(message=translate(
                        "err.datasource.connection_admin_required",
                        fallback="连接池的创建/修改/删除/测试仅租户管理员可操作",
                    ))
            # [jonex] 任务中心派活（execute）走内部链路：actor 通常为空，下面整段空间
            # 判定会被 skip_space_check 跳过。但「资源可用」不依赖调用者身份 ——
            # 禁用空间必须连后台任务一起拒绝，否则禁用只挡住了浏览器，任务仍在写数据。
            # 只拦 execute（派活）；remove_task / task_stopped / query_step_status
            # 属于清理与查询，保留放行，避免禁用空间留下无法回收的任务。
            if action == "execute" and data.get("space_id"):
                from .services.space_permission_service import require_space_active

                await require_space_active(request.tenant_id, data["space_id"])
            if not skip_space_check:
                # is_tenant_admin 仍由块内各分支共用（analytics / search_service /
                # space write|read / kb member read），所以这里保留它 —— 上面那道闸门
                # 另用一份函数内 import，是为了不把导入位置捆死在会跳过的块里。
                from .services.space_permission_service import (
                    SPACE_MANAGER,
                    SPACE_MEMBER,
                    has_space_role,
                    is_tenant_admin,
                    require_space_active,
                    require_space_manage,
                    require_space_role,
                )
                from .services.kb_permission_service import (
                    KB_MANAGER,
                    KB_MEMBER,
                    get_kb_grant_role,
                )

                if action in _SEARCH_ACTIONS:
                    data = await self._filter_search_kbs(request, data)
                elif action in _ANALYTICS_ACTIONS:
                    # 数据运营聚合：domain_space_id 传则空间成员可见；
                    # 不传则仅租户管理员（租户级全量总览）。
                    domain_space_id = data.get("domain_space_id")
                    if domain_space_id:
                        await require_space_role(
                            request.tenant_id, domain_space_id, actor,
                            SPACE_MANAGER, SPACE_MEMBER,
                        )
                        # [jonex] 禁用空间不产出数据运营视图（管理员同样不可用）。
                        await require_space_active(request.tenant_id, domain_space_id)
                    elif not await is_tenant_admin(request.tenant_id, actor):
                        raise PermissionDeniedError(
                            message=translate(
                                "err.analytics.tenant_admin_required",
                                fallback="租户级数据运营总览仅租户管理员可访问",
                            )
                        )
                elif action not in _SPACE_EXEMPT_ACTIONS:
                    space_id, kb_type, kb_id = await self._resolve_space_context(
                        action, data, request.tenant_id
                    )
                    if space_id:
                        # [jonex] 资源可用性闸：与 action 分组解耦，先于角色判定执行。
                        # 「禁用」是资源级事实，管理员同样受限 —— 管理员的豁免只保留在
                        # 可见性（list_spaces / get_space）与「管理空间本身」上。
                        # 放这里而不是每个分支里，是为了避免「某个新 action 忘了加判断」。
                        if action not in _SPACE_USABILITY_EXEMPT_ACTIONS:
                            await require_space_active(request.tenant_id, space_id)
                        if action in _SPACE_LEVEL_ACTIONS:
                            await require_space_manage(request.tenant_id, space_id, actor)
                        elif action in _KB_MANAGE_ACTIONS:
                            await self._check_kb_manage(
                                request.tenant_id, space_id, kb_id or "", actor
                            )
                        elif action in _SERVICE_SEARCH_ACTIONS:
                            if not await has_space_role(
                                request.tenant_id, space_id, actor,
                                SPACE_MANAGER, SPACE_MEMBER,
                            ) and not await is_tenant_admin(request.tenant_id, actor):
                                # 纯 KB 授权用户：该服务至少一个 KB 可访问即放行，
                                # 具体裁剪由 service 层 actor_user_id 过滤承担
                                from .services.space_permission_service import get_visible_space_ids
                                from .services.kb_permission_service import get_accessible_kb_ids
                                visible = await get_visible_space_ids(request.tenant_id, actor)
                                accessible = await get_accessible_kb_ids(
                                    request.tenant_id, actor, visible
                                )
                                svc_kbs = await self._service_kb_ids(
                                    request.tenant_id, data.get("service_id") or ""
                                )
                                if accessible is not None and not (set(svc_kbs) & accessible):
                                    await require_space_role(
                                        request.tenant_id, space_id, actor,
                                        SPACE_MANAGER, SPACE_MEMBER,
                                    )
                        elif action in _SPACE_WRITE_ACTIONS:
                            # 租户管理员（tenant:write）豁免：非成员也能写（成员路径短路，零额外查询）
                            if not await has_space_role(
                                request.tenant_id, space_id, actor, SPACE_MANAGER
                            ) and not await is_tenant_admin(request.tenant_id, actor):
                                # KB 授权兜底：只有 kb_manager 覆盖「写」
                                # [jonex] B3（D1）：原判据是 in ("kb_manager","editor")。
                                # 两级模型下 member 只读，写权限只属于 kb_manager。
                                if (
                                    kb_id
                                    and action not in _KB_GRANT_EXCLUDED_ACTIONS
                                    and await get_kb_grant_role(
                                        request.tenant_id, kb_id, actor
                                    ) == KB_MANAGER
                                ):
                                    data["_kb_granted"] = True
                                else:
                                    await require_space_role(
                                        request.tenant_id, space_id, actor, SPACE_MANAGER
                                    )
                        elif action in _KB_MEMBER_READ_ACTIONS:
                            # [jonex] 权限重构 B4（方案 §5 Gap 8 / 执行文档 §5.4）：
                            # KB 浏览类收紧到 **KB 成员口径** —— 与 _SPACE_READ_ACTIONS
                            # 的唯一差别：空间 `member` 不再自动放行。
                            # 放行者：空间管理者 / 租户管理员 / KB 授权成员（kb_manager|member）。
                            if not await has_space_role(
                                request.tenant_id, space_id, actor, SPACE_MANAGER
                            ) and not await is_tenant_admin(request.tenant_id, actor):
                                granted = (
                                    await get_kb_grant_role(request.tenant_id, kb_id, actor)
                                    if kb_id else None
                                )
                                if granted in (KB_MANAGER, KB_MEMBER):
                                    data["_kb_granted"] = True
                                else:
                                    # 非 KB 成员（含"只是空间 member"）→ 404 防探测，
                                    # 与 knowledge_info_service.get 的详情口径一致（不用 403）。
                                    raise ResourceNotFoundError(
                                        message=translate(
                                            "err.kb.not_found",
                                            params={"kb_id": kb_id or ""},
                                            fallback=f"知识库不存在: {kb_id or ''}",
                                        )
                                    )
                        elif action in _SPACE_READ_ACTIONS:
                            # 租户管理员（tenant:write）豁免：非成员也能读（成员路径短路，零额外查询）
                            if not await has_space_role(
                                request.tenant_id, space_id, actor,
                                SPACE_MANAGER, SPACE_MEMBER,
                            ) and not await is_tenant_admin(request.tenant_id, actor):
                                # KB 授权兜底：editor/viewer 覆盖「读」
                                granted = (
                                    await get_kb_grant_role(request.tenant_id, kb_id, actor)
                                    if kb_id else None
                                )
                                # KB 授权兜底：任一有效授权都覆盖「读」
                                # [jonex] B3（D1）：取值由三级收为 kb_manager/member
                                if granted in (KB_MANAGER, KB_MEMBER):
                                    data["_kb_granted"] = True
                                else:
                                    await require_space_role(
                                        request.tenant_id, space_id, actor,
                                        SPACE_MANAGER, SPACE_MEMBER,
                                    )


            # [jonex] B6 访问侧分级保护（§5.1）：空间权限之后、dispatch 之前做
            # 文档级/KB 级密级守卫。
            # ⚠ MCP 链路本来就跳过空间判定（机器身份），但**不能**连带跳过密级守卫：
            # 否则 MCP 可读 KB 浏览/文档/原文等出口。MCP 按最低启用等级处理
            # （fail closed，与多 KB 检索过滤同口径）；其余无 user/"anonymous"
            # 的非 MCP 链路保持原口径。
            if not skip_space_check or is_mcp:
                from .services.space_permission_service import is_tenant_admin
                from .services.classification_access_guard import guard_capability_request
                data = await guard_capability_request(
                    request.tenant_id, actor, action, data,
                    is_tenant_admin=await is_tenant_admin(request.tenant_id, actor),
                )

            # [jonex] 双向门禁（方案 §15）：本体 schema/抽取类 action 拒绝 llm-wiki KB——
            # llm-wiki 不跑本体，调了会存没人读的 ontology_compiled_schemas 行。
            # kb_type 已由上方 _resolve_space_context 单查询带回（kb 类 action）；
            # 未命中时保持原有自查（单条兜底，不叠加 action 分组条件——
            # _ONTOLOGY_GATED_ACTIONS 与 _SEARCH_ACTIONS/_SPACE_EXEMPT_ACTIONS 本无交集）
            if action in self._ONTOLOGY_GATED_ACTIONS:
                # [jonex] 导入必须无条件执行：kb_type 非 None（正常登录用户路径，由
                # _resolve_space_context 带回）时下方分支不进入，若 import 留在分支内
                # 则该名字未绑定 → 1337 行 UnboundLocalError（b3dce757 引入，2026-09-21 修复）
                from .services.kb_type_service import LLM_WIKI_TYPES, get_kb_type

                if kb_type is None:
                    fallback_kb_id = (data or {}).get("knowledge_base_id", "")
                    if fallback_kb_id:
                        kb_type = await get_kb_type(request.tenant_id, fallback_kb_id)
                if kb_type in LLM_WIKI_TYPES:
                    return CapabilityResponse.error(
                        request.request_id, 400,
                        "该知识库为 llm-wiki 类型，不支持本体 schema/抽取操作",
                        details={"error_code": "ONTOLOGY_ACTION_KB_TYPE_MISMATCH",
                                 "kb_type": kb_type},
                    )
            # [jonex] 数据运营二期：检索入口埋点（在 handler 之前落库，
            # 使「已接收但失败」的请求也有请求事件）
            await self._record_search_request(request, action, data)
            # 演化台闸门：放在鉴权之后、handler 之前，且**同样在 `skip_space_check`
            # 块之外** —— 无身份调用方（伪造/残缺 Authorization）也一律拒绝。
            if action in _EVOLUTION_ACTIONS:
                await self._require_evolution_admin(request, action, data)
            # 结构化数据支持闸门：放在鉴权之后、handler 之前。
            # 放在 `skip_space_check` 块之外 —— 内部链路/无 actor 调用同样要拦。
            if action in _STRUCTURED_ACTIONS:
                await self._require_structured_enabled(request.tenant_id, data)
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


def _decode_lineage_content(data: dict[str, Any]) -> bytes:
    """`import_lineage_file` 的 `content_b64` → bytes；缺失/非法一律 1001，不是 500。

    原先 dispatch 里直接 `base64.b64decode(d["content_b64"])`：缺键是 `KeyError`、
    非法 base64 是 `binascii.Error`（padding/长度错），两者都会被 `execute` 的
    兜底 `except Exception` 翻成 500「执行失败」—— 但这是**调用方参数问题**
    （1001/400），成因必须说对。`validate=True` 让非法字符也当场报错，而不是被
    静默丢弃后解出一段错误字节。
    """
    raw = data.get("content_b64")
    if not isinstance(raw, str) or not raw:
        raise InvalidParameterError(message="缺少 content_b64（文件内容）")
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidParameterError(message="content_b64 不是合法的 base64") from exc


async def _with_lineage_service(fn):
    """按动作开一个会话，把 `LineageEdgeService(session)` 交给 `fn`。

    为什么是**模块级**函数而不是 `self` 的属性/方法：`KnowledgeBaseCapability` 没有
    `self.session`（见 `_build_dispatch` 里血缘那段的注解），而血缘服务注入会话。
    它也用不到 `self` —— 写成方法只会让调用处变成 `self._with_lineage_service(self, …)`
    这种别扭形状。

    ⚠️ import 放在函数体内（延迟）：本模块在能力注册期就会被导入，而血缘服务的
    依赖（SQLAlchemy 模型等）不必参与那次导入。
    """
    from .services.lineage_edge_service import LineageEdgeService

    async with get_db_session() as session:
        return await fn(LineageEdgeService(session))


async def _with_lineage_forbidden_pair_service(fn):
    """按动作开一个会话，把 `LineageForbiddenPairService(session)` 交给 `fn`。"""
    from .services.lineage_forbidden_pair_service import LineageForbiddenPairService

    async with get_db_session() as session:
        return await fn(LineageForbiddenPairService(session))


async def _governance_sensitive_scan(request: CapabilityRequest,
                                      data: dict[str, Any]) -> dict:
    """W2.7a：只读扫描（编译 schema 已存证据；不查源库、不写行）。"""
    from .services.governance import sensitive_scan

    async with get_db_session() as session:
        return await sensitive_scan.scan(
            request.tenant_id, data["knowledge_base_id"], session=session,
            data_source_ids=data.get("data_source_ids"),
            min_confidence=str(data.get("min_confidence") or "medium"),
        )


async def _governance_adopt_sensitive_scan(request: CapabilityRequest,
                                           data: dict[str, Any]) -> dict:
    """W2.7a：人工采纳 → 写 governance_labels（origin=manual）+ 传播 + 一条审计。"""
    from .services.governance import sensitive_scan

    async with get_db_session() as session:
        return await sensitive_scan.adopt(
            request.tenant_id, data["knowledge_base_id"], data["items"],
            session=session, user_id=getattr(request, "user_id", None),
            request_id=getattr(request, "request_id", None),
        )


async def _with_gov_label_service(fn):
    from .services.governance.label_service import GovernanceLabelService
    async with get_db_session() as session:
        return await fn(GovernanceLabelService(session))


async def _with_gov_tag_service(fn):
    from .services.governance.tag_service import GovernanceTagService
    async with get_db_session() as session:
        return await fn(GovernanceTagService(session))


async def _with_gov_clearance_service(fn):
    from .services.governance.clearance_service import GovernanceClearanceService
    async with get_db_session() as session:
        return await fn(GovernanceClearanceService(session))


async def _with_gov_policy_service(fn):
    from .services.governance.policy_service import GovernancePolicyService
    async with get_db_session() as session:
        return await fn(GovernancePolicyService(session))


async def _with_gov_audit_service(fn):
    from .services.governance.audit_service import GovernanceAuditService
    async with get_db_session() as session:
        return await fn(GovernanceAuditService(session))


def _require_nonblank(data: dict[str, Any], *fields: str) -> None:
    """空串/纯空白**不算「已给」**，按缺参拒 —— `InvalidParameterError`（1001/400）。

    写法照本文件既有同类校验（`_resolve_service_id_to_kb_ids` 的「缺少 service_id 或
    knowledge_base_ids 参数」/「knowledge_base_ids 不能全为空值」）。

    ⚠️ 为什么能力层要单独拦（I-1，2026-10-17 审查）：本文件对空串有**两个相反**的判据 ——
    `validate_input` 用 `is not None`（`""` 算「已给」⇒ 放行），`_resolve_space_context`
    用 `if not kb_id`（`""` 算「没给」⇒ 返回 None ⇒ 空间/授权闸门**整段跳过**）。
    空串正好从缝里穿过去；服务侧也收了口（`knowledge_base_id is not None` ⇒ 撞
    `_ensure_kb("")` ⇒ 1002），这里是**更靠前的一层**：「没给」报 1001，比服务层
    「这个 KB 不存在」的 1002 更贴近真实成因。
    """
    blank = [f for f in fields if not str(data.get(f) or "").strip()]
    if blank:
        raise InvalidParameterError(message="缺少必填参数：" + "、".join(blank))


async def _update_lineage_edge(request: CapabilityRequest, data: dict[str, Any]) -> dict:
    """`update_lineage_edge` 的 handler：空串先按缺参拒（I-1 第一层），再进服务。

    原先是个哑 lambda，参数校验完全指望 `validate_input` —— 而它对空串是放行的
    （见 `_require_nonblank` 的注解）。
    """
    _require_nonblank(data, "edge_id", "knowledge_base_id")
    return await _with_lineage_service(
        lambda s: s.update(request.tenant_id, data["edge_id"], data,
                           knowledge_base_id=data["knowledge_base_id"],
                           user_id=getattr(request, "user_id", None)))


async def _delete_lineage_edge(request: CapabilityRequest, data: dict[str, Any]) -> dict:
    """`delete_lineage_edge` 的 handler：同 update —— 空串先拒，再进服务。

    `_deleted(...)` 不是可有可无的包装：服务返回 bool，而同层所有 delete
    （delete_tag / delete_synonym / delete_folder …）都包成 `{"deleted": …}`
    —— `CapabilityResponse.data` 的 dict 形状靠它们守住。
    """
    _require_nonblank(data, "edge_id", "knowledge_base_id")
    return await _deleted(_with_lineage_service(
        lambda s: s.delete(request.tenant_id, data["edge_id"],
                           knowledge_base_id=data["knowledge_base_id"])))


async def _update_lineage_forbidden_pair(request: CapabilityRequest,
                                         data: dict[str, Any]) -> dict:
    """禁止对更新：空串先按缺参拒（I-1 口径），再进服务。"""
    _require_nonblank(data, "pair_id", "knowledge_base_id")
    return await _with_lineage_forbidden_pair_service(
        lambda s: s.update(request.tenant_id, data["pair_id"], data,
                           knowledge_base_id=data["knowledge_base_id"]))


async def _delete_lineage_forbidden_pair(request: CapabilityRequest,
                                         data: dict[str, Any]) -> dict:
    """禁止对删除：空串先拒，再进服务；沿用 `_deleted` 的 dict 形状。"""
    _require_nonblank(data, "pair_id", "knowledge_base_id")
    return await _deleted(_with_lineage_forbidden_pair_service(
        lambda s: s.delete(request.tenant_id, data["pair_id"],
                           knowledge_base_id=data["knowledge_base_id"])))


async def _update_governance_label(request: CapabilityRequest,
                                   data: dict[str, Any]) -> dict:
    _require_nonblank(data, "label_id", "knowledge_base_id")
    return await _with_gov_label_service(lambda s: s.update(
        request.tenant_id, data["label_id"], data,
        knowledge_base_id=data["knowledge_base_id"]))


async def _delete_governance_label(request: CapabilityRequest,
                                   data: dict[str, Any]) -> dict:
    _require_nonblank(data, "label_id", "knowledge_base_id")
    return await _deleted(_with_gov_label_service(lambda s: s.delete(
        request.tenant_id, data["label_id"],
        knowledge_base_id=data["knowledge_base_id"])))


async def _update_governance_tag(request: CapabilityRequest,
                                 data: dict[str, Any]) -> dict:
    _require_nonblank(data, "tag_id", "knowledge_base_id")
    return await _with_gov_tag_service(lambda s: s.update(
        request.tenant_id, data["tag_id"], data,
        knowledge_base_id=data["knowledge_base_id"]))


async def _delete_governance_tag(request: CapabilityRequest,
                                 data: dict[str, Any]) -> dict:
    _require_nonblank(data, "tag_id", "knowledge_base_id")
    return await _deleted(_with_gov_tag_service(lambda s: s.delete(
        request.tenant_id, data["tag_id"],
        knowledge_base_id=data["knowledge_base_id"])))


async def _update_governance_policy(request: CapabilityRequest,
                                    data: dict[str, Any]) -> dict:
    _require_nonblank(data, "policy_id", "knowledge_base_id")
    return await _with_gov_policy_service(lambda s: s.update(
        request.tenant_id, data["policy_id"], data,
        knowledge_base_id=data["knowledge_base_id"]))


async def _delete_governance_policy(request: CapabilityRequest,
                                    data: dict[str, Any]) -> dict:
    _require_nonblank(data, "policy_id", "knowledge_base_id")
    return await _deleted(_with_gov_policy_service(lambda s: s.delete(
        request.tenant_id, data["policy_id"],
        knowledge_base_id=data["knowledge_base_id"])))


async def _evaluate_governance(request: CapabilityRequest,
                               data: dict[str, Any]) -> dict:
    """PDP 试算：给治理 UI 预览一次判定的结果（不执行 SQL）。"""
    _require_nonblank(data, "knowledge_base_id", "data_source_id", "table")
    from .services.nl2sql.service import (
        _governance_decide, _governance_level, _load_governance,
    )
    gov = await _load_governance(
        request.tenant_id, data["knowledge_base_id"], data["data_source_id"],
        getattr(request, "user_id", None))
    table = data["table"]
    column = data.get("column")
    decision = _governance_decide(gov, table, column,
                                  data.get("action") or "select")
    return {
        "effect": decision.effect, "policy_id": decision.policy_id,
        "reason": decision.reason, "mask_type": decision.mask_type,
        "row_filters": list(decision.row_filters),
        "level": _governance_level(gov, table, column),
        "clearance": gov.get("clearance"),
    }


async def _deleted(coro) -> dict:
    result = await coro
    return {"deleted": result}


async def _internal_only(request, call: Callable[[], Awaitable[Any]]) -> Any:
    """只允许平台内部服务调用（经 Sidecar、不带用户身份）。

    Gateway 没有这类 action 的路由；带用户身份的请求说明它来自用户侧链路，一律拒绝 ——
    否则用户可以绕过执行器的契约判定（权限要求 / 前置条件确认）直接调工具。
    """
    actor = getattr(request, "user_id", None)
    if actor and actor != "anonymous":
        raise PermissionDeniedError(message=translate(
            "err.kb_tool.internal_only", fallback="该操作只供平台内部执行器调用",
        ))
    return await call()


async def _user_only(request, call: Callable[[], Awaitable[Any]]) -> Any:
    """只允许代真实用户的调用（与 `_internal_only` 相反）。

    execute() 的空间 / 知识库判权对「无用户身份」与 MCP Key 调用整段跳过；这类 action 的
    权限只认调用人的知识库写权限，没有用户就无从判起，必须拒绝而不是放行。
    """
    actor = getattr(request, "user_id", None)
    is_mcp = (getattr(request, "context", None) or {}).get("mcp_key_id")
    if not actor or actor == "anonymous" or is_mcp:
        raise PermissionDeniedError(message=translate(
            "err.ontology_action.user_required",
            fallback="执行本体动作需要登录用户身份，内部调用或测试 token 不能执行",
        ))
    return await call()


async def _updated(coro) -> dict:
    result = await coro
    return {"updated": result}


async def _kb_type_options(request, data) -> dict:
    """[jonex] `GET /knowledge-base/kb-types` 的 handler：本部署可创建的 KB 类型。

    ⚠ **必须是 `async def`**：`execute()` 是**无条件** `await handler(request, data)`。
    写成一个直接返回 dict 的 lambda 会抛
    `TypeError: object dict can't be used in 'await' expression` —— 而且它只在
    **真被调用**时才炸（不在启动时、不在 import 时），正是本次踩过的坑。

    `kb_type_options()` 本身是**纯同步**函数（无 IO、会被单测直接调用），所以就地包一层；
    **别**为了省这一层把它改成 `async def` —— 那会把纯函数拖成协程，连累所有调用点。
    """
    return kb_type_options()


async def _list_result(coro) -> dict:
    """包装 list 返回值，使 CapabilityResponse.data 保持 dict 类型"""
    result = await coro
    return {"permissions": result} if isinstance(result, list) else result


async def _candidates_result(coro) -> dict:
    """包装成员候选列表，使 CapabilityResponse.data 保持 dict 类型"""
    result = await coro
    return {"candidates": result}


async def _url_result(coro) -> dict:
    """包装 get_raw_url 的 str 返回值，使 CapabilityResponse.data 保持 dict 类型"""
    url = await coro
    return {"url": url}


# ════════════════════════════════════════════════════════════════
# 演化台服务化（②a）：runs / ledgers / corpus
#
# 所有 handler 都是模块级、函数内 import：`KnowledgeBaseCapability` 没有
# `self.session`（见 `_build_dispatch` 血缘那段注解），演化服务注入会话。
# `RunStore` / `EvolutionRunner` 必须在**调用时**从源模块取（不放进模块顶部），
# 一是延迟导入 SQLAlchemy 模型，二是测试可以替换源模块属性。
# ════════════════════════════════════════════════════════════════

#: 后台演化任务的强引用。`asyncio` 只持弱引用 —— 不存住的话任务可能被 GC
#: 回收在 `await` 中间（Python 文档明说的坑），run 会永久停在 `queued`。
_EVOLUTION_TASKS: set[asyncio.Task] = set()

#: `request` 列只持久化 pipeline 认识的字段（见 engine.run_pipeline 的 req 取用）。
#: `entries` 是 relation 的人工条目；缺省时 builder 走自动发现（P1-3 Task 2）。
_EVOLUTION_REQUEST_FIELDS = (
    "repeat", "picks", "target_cases", "dry_run", "skip_probe",
    "window_days", "margin", "entries",
    # relation 自动发现的预算旋钮（Task 2）：不透传的话 API 传了也不生效。
    "sample_n", "max_candidates", "max_queries", "timeout_seconds",
)

#: 请求字段的类型契约：错类型不是「pipeline 自己会兜」——`int("abc")` 会在
#: 后台任务里抛、运行记成 failed，前端只看到一次莫名失败；`dry_run="false"`
#: 是**真值**、被当成「只生成补丁」，用户以为真的没应用。所以入口就 1001。
_EVOLUTION_INT_FIELDS = ("repeat", "window_days", "margin",
                         "sample_n", "max_candidates", "max_queries")
#: 可接受小数的数字字段（墙钟秒数用 float 更自然；bool 仍要显式排除）。
_EVOLUTION_NUMBER_FIELDS = ("timeout_seconds",)
#: relation 自动发现的预算上限（区间外一律 1001）：`sample_n` 每侧采样行数、
#: `max_queries` 总查询数、`max_candidates` 候选数。天花板既是保护（别让一个
#: 请求把只读账号拖死），也是让「配错一位数」在受理阶段就暴露。
_EVOLUTION_INT_RANGES = {
    "sample_n": (1, 10000),
    "max_queries": (1, 500),
    "max_candidates": (1, 200),
}
_EVOLUTION_BOOL_FIELDS = ("dry_run", "skip_probe")
_EVOLUTION_STR_LIST_FIELDS = ("picks", "target_cases")


def _validate_evolution_request(data: dict[str, Any]) -> None:
    """`evolution_start_run` 的请求参数类型校验（非法 → 1001）。

    `bool` 是 `int` 的子类，必须显式排除 —— `repeat=True` 传给 `int()` 是 1，
    看起来能跑，语义却完全不对。
    """
    for key in _EVOLUTION_INT_FIELDS:
        if key in data and (isinstance(data[key], bool)
                            or not isinstance(data[key], int)):
            raise InvalidParameterError(message=f"{key} 必须是整数")
    for key in _EVOLUTION_NUMBER_FIELDS:
        if key not in data:
            continue
        value = data[key]
        # bool 是 int 的子类；NaN/Infinity 能过 isinstance 却会让超时逻辑失效。
        if (isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value)) or value <= 0):
            raise InvalidParameterError(
                message=f"{key} 必须是有限正数")
    for key, (low, high) in _EVOLUTION_INT_RANGES.items():
        if key in data and not (low <= data[key] <= high):
            raise InvalidParameterError(
                message=f"{key} 必须在 {low}..{high} 之间")
    for key in _EVOLUTION_BOOL_FIELDS:
        if key in data and not isinstance(data[key], bool):
            raise InvalidParameterError(message=f"{key} 必须是布尔值")
    for key in _EVOLUTION_STR_LIST_FIELDS:
        if key not in data:
            continue
        value = data[key]
        if (not isinstance(value, list)
                or any(not isinstance(item, str) for item in value)):
            raise InvalidParameterError(message=f"{key} 必须是字符串数组")
    kind = str(data.get("kind") or "").strip()
    if "entries" in data:
        entries = data["entries"]
        if (not isinstance(entries, list)
                or any(not isinstance(item, dict) for item in entries)):
            raise InvalidParameterError(message="entries 必须是对象数组")
        if kind == "relation" and entries:
            from .services.evolution.engine import (
                RELATION_MAX_ENTRIES,
                RELATION_OPS,
            )
            if len(entries) > RELATION_MAX_ENTRIES:
                raise InvalidParameterError(
                    message=f"relation 单批条目上限 {RELATION_MAX_ENTRIES} 条")
            # P1 收尾：op 白名单 + 同批同 op + remove 必须有 name（全 1001）。
            # 混批是结构性错误：add 与 remove 的端点校验/回滚记账无法共用。
            ops: set[str] = set()
            for index, item in enumerate(entries):
                op = str(item.get("op") or "").strip() or "add"
                if op not in RELATION_OPS:
                    raise InvalidParameterError(
                        message=f"entries[{index}].op 必须是 add/remove")
                ops.add(op)
                if op == "remove":
                    name = item.get("name")
                    if not isinstance(name, str) or not name.strip():
                        raise InvalidParameterError(
                            message=f"entries[{index}].name 必须是非空字符串")
            if len(ops) > 1:
                raise InvalidParameterError(
                    message="relation 同一批次只能使用一种 op（add 或 remove）")
    # `relation` 缺 `entries`（或空数组）= 自动发现模式（P1-3 Task 2）：builder
    # 会跑 join 候选发现器，零产出收口 empty-draft。因此这里不再拒绝缺键。
    if kind == "constraint":
        # 约束编辑**没有**自动来源：缺 entries / 空数组不是「跑发现器」，而是
        # 一次没有意图的请求 → 受理阶段就 1001（不建注定 empty-draft 的 run）。
        from .services.evolution.engine import (
            CONSTRAINT_MAX_ENTRIES,
            CONSTRAINT_OPS,
        )

        entries = data.get("entries")
        if not entries:
            raise InvalidParameterError(
                message="constraint 演化需要显式 entries"
                        "（add/update/disable/enable）")
        if len(entries) > CONSTRAINT_MAX_ENTRIES:
            raise InvalidParameterError(
                message=f"constraint 单批条目上限 {CONSTRAINT_MAX_ENTRIES} 条")
        for index, item in enumerate(entries):
            op = str(item.get("op") or "").strip()
            if op not in CONSTRAINT_OPS:
                raise InvalidParameterError(
                    message=f"entries[{index}].op 必须是 "
                            "add/update/disable/enable")
            if op == "add":
                # R2：add 只生成 query_rule；unique_attribute 只能编辑已有行。
                ctype = str(item.get("constraint_type") or "").strip()
                if ctype not in ("", "query_rule"):
                    raise InvalidParameterError(
                        message=f"entries[{index}]：constraint add 只支持 "
                                "constraint_type=query_rule")
            else:
                name = item.get("name")
                if not isinstance(name, str) or not name.strip():
                    raise InvalidParameterError(
                        message=f"entries[{index}].name 必须是非空字符串")
            if "expression" in item and not isinstance(item["expression"], str):
                raise InvalidParameterError(
                    message=f"entries[{index}].expression 必须是字符串")
    if kind == "mapping":
        # mapping 编辑的是抽象 schema 的落点（KB 级 abstract 链）：没有自动来源，
        # 缺 entries / 空数组一次没有意图的请求 → 受理阶段 1001。
        # 同批只能一个 `data_source_id`：语料/基准源由条目推导（`bench_ds`），
        # 混批多源会让评测落在哪个源上成为不确定。
        from .services.evolution.engine import MAPPING_MAX_ENTRIES, MAPPING_OPS

        entries = data.get("entries")
        if not entries:
            raise InvalidParameterError(
                message="mapping 演化需要显式 entries"
                        "（add/update/disable/enable）")
        if len(entries) > MAPPING_MAX_ENTRIES:
            raise InvalidParameterError(
                message=f"mapping 单批条目上限 {MAPPING_MAX_ENTRIES} 条")
        sources: set[str] = set()
        for index, item in enumerate(entries):
            op = str(item.get("op") or "").strip()
            if op not in MAPPING_OPS:
                raise InvalidParameterError(
                    message=f"entries[{index}].op 必须是 "
                            "add/update/disable/enable")
            target_type = str(item.get("target_type") or "").strip()
            if target_type and target_type != "attribute":
                raise InvalidParameterError(
                    message=f"entries[{index}].target_type 只支持 attribute")
            data_source_id = item.get("data_source_id")
            if (not isinstance(data_source_id, str)
                    or not data_source_id.strip()):
                raise InvalidParameterError(
                    message=f"entries[{index}].data_source_id 必须是非空字符串")
            target_code = item.get("target_code")
            if not isinstance(target_code, str) or not target_code.strip():
                raise InvalidParameterError(
                    message=f"entries[{index}].target_code 必须是非空字符串")
            sources.add(data_source_id.strip())
        if len(sources) > 1:
            raise InvalidParameterError(
                message="mapping 同批条目必须属于同一个 data_source_id")
    if kind == "tool_schema":
        # 工具口径（Tool-Schema 线）：只改抽象 schema 的 tools 覆盖块，三 op、
        # 显式 entries、update 必须带非空 description。工具 name 的**存在性**
        # 由 builder / engine 对照 `TOOL_SCHEMAS` 判（未知 name → skipped），
        # 受理层只挡结构性坏形状（1001）。
        from .services.evolution.engine import (
            TOOL_SCHEMA_MAX_ENTRIES,
            TOOL_SCHEMA_OPS,
        )

        entries = data.get("entries")
        if not entries:
            raise InvalidParameterError(
                message="tool_schema 演化需要显式 entries"
                        "（update/disable/enable）")
        if len(entries) > TOOL_SCHEMA_MAX_ENTRIES:
            raise InvalidParameterError(
                message=f"tool_schema 单批条目上限 {TOOL_SCHEMA_MAX_ENTRIES} 条")
        for index, item in enumerate(entries):
            if not isinstance(item, dict):
                raise InvalidParameterError(
                    message=f"entries[{index}] 必须是对象")
            op = str(item.get("op") or "").strip()
            if op not in TOOL_SCHEMA_OPS:
                raise InvalidParameterError(
                    message=f"entries[{index}].op 必须是 update/disable/enable")
            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                raise InvalidParameterError(
                    message=f"entries[{index}].name 必须是非空字符串")
            if op == "update":
                description = item.get("description")
                if (not isinstance(description, str)
                        or not description.strip()):
                    raise InvalidParameterError(
                        message=f"entries[{index}].description 必须是非空字符串")


def _schedule_evolution_run(tenant_id: str, run_id: str) -> None:
    """create 成功后调度后台执行，不阻塞请求。

    新开一个 `get_db_session()` 的协程：runner 的整个生命周期（含逐阶段写库、
    日志心跳）都要有会话；动作自己那个会话在 `await` 时已经退出了。
    """
    async def _run() -> None:
        from .services.evolution.run_store import RunStore
        from .services.evolution.runner import EvolutionRunner

        try:
            async with get_db_session() as session:
                await EvolutionRunner(RunStore(session)).execute(tenant_id, run_id)
        except Exception as exc:  # noqa: BLE001 —— 后台任务不许留未观测异常
            # runner 自身已把业务失败收口成 failed；能走到这里的是造会话/抢占
            # 这类基础错误 —— 记日志，至少不在事件循环里变成「Task exception was
            # never retrieved」。
            logger.warning("演化运行后台执行失败 run=%s: %s", run_id, exc)

    task = asyncio.create_task(_run())
    _EVOLUTION_TASKS.add(task)
    task.add_done_callback(_EVOLUTION_TASKS.discard)


async def _mark_adopt_failed(tenant_id: str, run_id: str, error: str) -> None:
    """后台采纳最后一道兜底：只在**确定还没 apply** 时把 `adopting` 标 `failed`。

    `AdoptService.execute()` 自己会把业务失败收口成终态；能走到这里的只有
    「造会话 / 执行器异常」这类**没有收口**的错误。因此**另开一个独立会话**
    （第一个会话可能已中毒/关闭）。

    关键安全边界：`bench_status` 一旦离开 `None` / `pending`，说明执行已经
    进入父本臂/候选臂甚至 apply 阶段 —— 此时直接终态 `failed`，可能把一个
    未获批准、但已写进 schema 的补丁永久留在线上（双故障窗口）。这种情况
    **不写终态**，只落一行 `adopt_fallback` 旁路日志，把 run 留给
    `recover_stale`：Task 8 的恢复矩阵会按 smoke/版本事实决定「补完激活」
    还是「恢复快照后失败」。
    """
    from .services.evolution.run_store import RunStore

    try:
        async with get_db_session() as session:
            store = RunStore(session)
            run = await store.get(tenant_id, run_id)
            if run.get("status") != "adopting":
                return
            bench_status = run.get("bench_status")
            if bench_status not in (None, "pending"):
                # 可能已经 apply 过内容：不制造「终态 failed + 线上仍是未批准
                # 补丁」的窗口，交给 recover_stale 按矩阵收口。
                await store.log(
                    tenant_id, run_id,
                    json.dumps({
                        "stage": "adopt_fallback",
                        "reason": "may_have_applied",
                        "bench_status": bench_status,
                    }, ensure_ascii=False, separators=(",", ":")))
                return
            await store.update(
                run_id, tenant_id=tenant_id, status="failed",
                expect_statuses={"adopting"},
                error=f"后台采纳执行失败：{error}"[:2000])
    except Exception as exc:  # noqa: BLE001 —— 兜底失败只记日志，不制造第二个未观测异常
        logger.warning("演化采纳失败兜底写入失败 run=%s: %s", run_id, exc)


def _adopt_execute_kwargs(options: dict[str, Any]) -> dict[str, Any]:
    """`options` → `AdoptService.execute()` 的关键字参数（单 run 与批量共用）。

    两处调度（单 run 后台 / 批量串行）必须逐键一致，否则同一个 run 走哪条入口
    行为不同；抽成一个函数是唯一防漂移的办法。
    """
    return {
        "picks": options.get("picks"),
        "note": options.get("note") or "",
        "user_id": options.get("user_id"),
        "request_id": options.get("request_id"),
        "override_reason": options.get("override_reason"),
        "adhoc_truths": options.get("adhoc_truths"),
        "adhoc_verdicts": options.get("adhoc_verdicts"),
    }


def _schedule_evolution_adopt(tenant_id: str, run_id: str,
                              options: dict[str, Any]) -> None:
    """受理成功后调度后台执行（202 语义），不阻塞请求。

    `accept()` 已经把 run CAS 到 `adopting` 并写好了幂等键/门禁事实；真正的
    apply / 冒烟 / 完整语料基准由这里另开一个会话跑完。options 原样转交
    `execute()`（picks / note / user_id / request_id / override_reason /
    adhoc_truths / adhoc_verdicts），与同步 `adopt()` 内联调用同一套参数。

    任务加入模块级 `_EVOLUTION_TASKS` 并用 `add_done_callback(discard)` 防 GC
    （与 `_schedule_evolution_run` 同款）。
    """
    async def _run() -> None:
        from .services.evolution.adopt_service import AdoptService

        try:
            async with get_db_session() as session:
                await AdoptService(session).execute(
                    tenant_id, run_id, **_adopt_execute_kwargs(options))
        except Exception as exc:  # noqa: BLE001 —— 后台任务不许留未观测异常
            logger.warning("演化采纳后台执行失败 run=%s: %s", run_id, exc)
            await _mark_adopt_failed(tenant_id, run_id, str(exc) or repr(exc))

    task = asyncio.create_task(_run())
    _EVOLUTION_TASKS.add(task)
    task.add_done_callback(_EVOLUTION_TASKS.discard)


#: 串行批量里**尚未开始执行**的 run 的心跳间隔（秒）。`recover_stale` 的阈值
#: 是 30 分钟：`accept()` 之后第 2..N 条一直停在 `adopting` 且 `updated_at`
#: 冻结，串行队列排久一点就会被并发的 sweep 当成僵尸回收成 `failed`（链是空的，
#: `try_chain_lock` 会成功），随后 execute 只拿到只读回执 —— 已受理的 run 从
#: 批次里静默消失。5 分钟一次足够把 30 分钟窗口推出很远。
BATCH_HEARTBEAT_SECONDS = 300
#: 心跳任务名（测试据此断言结束后无残留任务）。
_BATCH_HEARTBEAT_TASK_NAME = "evolution-batch-adopt-heartbeat"


def _schedule_evolution_batch_adopt(tenant_id: str,
                                    items: list[tuple[str, dict[str, Any]]]) -> None:
    """批量采纳的**一个**后台任务：按输入顺序**串行** execute 每条 run。

    为什么不能每条各开一个任务：`execute()` 的父本臂 ruler 在窗口内要保证
    「应用前 = 应用后」（`_ruler_hashes` 覆盖 abstract + compiled(bench_ds)，
    无 facet 排除）。同批的 alias/mapping run 会写 abstract 载体，兄弟 run 并发
    跑完整语料基准时就会看到被改过的 ruler → 眼见的「未参与本批的 facet 变了」
    → 回退 `evaluated` + `executor_error`，而 HTTP 早已回了 accepted。

    串行不会互锁：`validate_batch` 已保证同一批里每链至多一个 run，前一条的
    链锁在它收口后释放，后一条才进 execute。

    **待跑项心跳**：串行意味着第 2..N 条在轮到它之前一直是 `adopting` 且
    `updated_at` 不前进；累计超过 `recover_stale` 的 30 分钟就会被 sweep 回收成
    `failed`（它们的链还是空的，链锁能拿到）。`_run()` 期间起一个心跳循环，
    每 `BATCH_HEARTBEAT_SECONDS` 对**尚未开始执行**的 run 各 `RunStore.update`
    一次（只刷 `updated_at`）；主循环结束（含异常路径）在 finally 里 cancel
    并 await 吞掉 `CancelledError`。

    单条异常沿用单 run 的兜底（`_mark_adopt_failed`）并**继续**下一条 ——
    与受理阶段「单条失败不中断」同口径。任务同样进 `_EVOLUTION_TASKS` 防 GC。
    """
    async def _heartbeat(pending: set[str]) -> None:
        from .services.evolution.run_store import RunStore

        while True:
            await asyncio.sleep(BATCH_HEARTBEAT_SECONDS)
            if not pending:
                return
            try:
                async with get_db_session() as session:
                    store = RunStore(session)
                    for run_id in list(pending):
                        # 只刷 updated_at（不传 status/其它字段），不动状态机事实。
                        await store.update(run_id, tenant_id=tenant_id)
            except Exception as exc:  # noqa: BLE001 —— 心跳失败不能拖垮批次
                logger.warning(
                    "演化批量采纳心跳刷新失败（待跑 %d 条）: %s",
                    len(pending), exc)

    async def _run() -> None:
        from .services.evolution.adopt_service import AdoptService

        pending = {run_id for run_id, _ in items}
        heartbeat = asyncio.create_task(
            _heartbeat(pending), name=_BATCH_HEARTBEAT_TASK_NAME)
        try:
            for run_id, options in items:
                # 轮到它了：从「待跑」集合摘掉（正在执行的 run 自带更新）。
                pending.discard(run_id)
                try:
                    async with get_db_session() as session:
                        await AdoptService(session).execute(
                            tenant_id, run_id,
                            **_adopt_execute_kwargs(options))
                except Exception as exc:  # noqa: BLE001 —— 不许留未观测异常
                    logger.warning(
                        "演化批量采纳后台执行失败 run=%s: %s", run_id, exc)
                    await _mark_adopt_failed(
                        tenant_id, run_id, str(exc) or repr(exc))
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass

    task = asyncio.create_task(_run())
    _EVOLUTION_TASKS.add(task)
    task.add_done_callback(_EVOLUTION_TASKS.discard)


#: 合并分页的 offset 上限。版本列表走单条 UNION 后仍保留这道闸：超大 offset
#: 对用户的检索价值为零，却会让数据库扫描大量行。
_EVOLUTION_MAX_OFFSET = 10000


def _evolution_int_field(data: dict[str, Any], key: str, default: int) -> int:
    """分页参数统一口径：拒绝 bool（`int(True)=1` 是静默陷阱），
    接受整数与数字字符串，其余 → 1001。
    """
    raw = data.get(key, default)
    if isinstance(raw, bool):
        raise InvalidParameterError(message=f"{key} 必须是整数")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError as exc:
            raise InvalidParameterError(message=f"{key} 必须是整数") from exc
    raise InvalidParameterError(message=f"{key} 必须是整数")


def _evolution_limit(data: dict[str, Any]) -> int:
    """每页条数：默认 50，夹紧 1..200。"""
    return max(1, min(_evolution_int_field(data, "limit", 50), 200))


def _evolution_offset(data: dict[str, Any]) -> int:
    """分页偏移：默认 0；负数 / 非整数 / 超过 10000 → 1001。

    负数不是「从头开始」，超大 offset 也没有检索价值 —— 两者都显式拒绝，
    不做静默修正。
    """
    offset = _evolution_int_field(data, "offset", 0)
    if offset < 0:
        raise InvalidParameterError(message="offset 不能为负数")
    if offset > _EVOLUTION_MAX_OFFSET:
        raise InvalidParameterError(
            message=f"offset 不能超过 {_EVOLUTION_MAX_OFFSET}")
    return offset


def _evolution_bundle(run: dict, *, candidates: list | None = None,
                      versions: list | None = None) -> dict:
    """`RunBundle`：与旧桥接 `load_bundle` 同形（前端零改动）。

    - `gate` / `patch` 给**完整**对象（列表页才做三字段投影）；
    - `eval` 从 run.report 拆出（Task 7 pipeline 写 `{parent, candidate}`，
      每臂是 run_eval 的 `{summary, results}`）；report 为空 → `eval=None`；
    - 信任闭环 P0 增补：`candidates` / `versions` / `smoke` /
      `gate_effective` / `gate_adhoc` / `override`。`candidates` / `versions`
      由调用方注入（本函数不查库，保持纯函数、测试可独立钉形状）；
      未注入时给空列表，不伪造数据。
    - `report_md` / `probe` / `rounds` / `artifacts` 在服务化一期没有载体，
      保持空值（不伪造）。
    """
    report = run.get("report") if isinstance(run.get("report"), dict) else {}

    def _arm(key: str) -> dict | None:
        arm = report.get(key)
        if not isinstance(arm, dict):
            return None
        return {"summary": arm.get("summary"),
                "cases": arm.get("results") or []}

    parent, candidate = _arm("parent"), _arm("candidate")
    patch_doc = run.get("patch") if isinstance(run.get("patch"), dict) else None
    patch = None
    if patch_doc:
        patch = {
            "patch_id": patch_doc.get("patch_id"),
            "kind": patch_doc.get("kind"),
            "kb": patch_doc.get("kb"),
            "ds": patch_doc.get("ds"),
            "entries": patch_doc.get("entries") or [],
            "skipped": patch_doc.get("skipped") or [],
            "target_cases": patch_doc.get("target_cases") or [],
            "rollback_added": (patch_doc.get("rollback") or {}).get("added") or [],
        }
    return {
        "run": run,
        "stuck": bool(run.get("stuck")),
        "stale": bool(run.get("stale")),
        "report_md": None,
        "gate": run.get("gate") or None,
        "patch": patch,
        "eval": ({"parent": parent, "candidate": candidate}
                 if (parent is not None or candidate is not None) else None),
        "probe": None,
        "smoke": run.get("smoke"),
        "gate_effective": run.get("gate_effective"),
        "gate_adhoc": run.get("gate_adhoc"),
        "override": {
            "used": bool(run.get("gate_override")),
            "by": run.get("override_by"),
            "reason": run.get("override_reason"),
        },
        "candidates": list(candidates or []),
        "versions": list(versions or []),
        # 信任闭环 Task 9：完整语料基准的顶层事实（前端 `bundle.bench` 读这里；
        # `run` 里同名字段给详情页复用）。旧 run / 未跑基准时为 None，不伪造。
        "bench": run.get("bench"),
        "bench_status": run.get("bench_status"),
        "bench_mode": run.get("bench_mode"),
        "rounds": [],
        "artifacts": [],
    }


#: RunBundle 版本列表一次读多少条链版本（远大于单次 run 产生的版本数）。
_EVOLUTION_RUN_VERSIONS_LIMIT = 200


async def _evolution_run_versions(tenant_id: str, run: dict) -> list[dict]:
    """该 run 的相关版本（摘要）：来源 run ∪ 采纳版本 ∪ 父版本，倒序去重。

    **含归档**：`archive_old` 可能把父版本/历史版本搬进归档表，只查主表会让
    RunDetail 的版本页签凭空少行。这里复用 `VersionStore.list(include_archived
    =True)` 读该 run 所在的**同一条链**（scope 由 kind 推导），再按
    `source_run_id` / 显式 id 过滤；每项带 `archived` 标记。

    查不到 / 表未就绪由调用方 fail-open 到空列表。
    """
    from .services.evolution.locking import scope_for_kind
    from .services.evolution.version_store import VersionStore

    run_id = run.get("run_id")
    kb = run.get("kb")
    kind = run.get("kind")
    if not run_id or not kb or not kind:
        return []
    try:
        scope = scope_for_kind(kind)
    except ValueError:
        return []
    ds = run.get("ds") if scope == "compiled" else None
    extra_ids = [vid for vid in (run.get("adopted_version_id"),
                                 run.get("parent_version_id")) if vid]
    wanted = set(extra_ids)
    async with get_db_session() as session:
        result = await VersionStore(session).list(
            tenant_id, kb, scope, ds, include_archived=True,
            limit=_EVOLUTION_RUN_VERSIONS_LIMIT, offset=0)
    items = [v for v in result.get("items") or []
             if v.get("source_run_id") == run_id
             or v.get("version_id") in wanted]
    # 仓储 ORDER BY 已倒序；这里再兜一次（fake / 多来源合并）。
    items.sort(key=lambda v: (v.get("created_at") or "",
                              v.get("version_id") or ""), reverse=True)
    seen: set[str] = set()
    unique: list[dict] = []
    for item in items:
        version_id = item.get("version_id")
        if version_id in seen:
            continue
        seen.add(version_id)
        unique.append(item)
    return unique


async def _evolution_status(request: CapabilityRequest,
                            data: dict[str, Any]) -> dict:
    """服务化状态：`repo_root` / `runs_dir` 明示数据在库里（不再是工作区目录）。

    **读路径自愈**：先 best-effort 跑一次 `recover_stale`，用户打开页面就能
    收回上个进程留下的僵死 `adopting`（不必等下一次写操作）。恢复必须走
    **独立 session** —— 它内部 commit，批量 UPDATE 失败时事务已中止，与读取
    共会话会让后面的 `list` 连锁 500，恰好是这里要杜绝的失败。
    """
    from .services.evolution.run_store import RunStore

    try:
        async with get_db_session() as session:
            await RunStore(session).recover_stale(request.tenant_id)
    except Exception as exc:  # noqa: BLE001 —— 自愈失败只告警，绝不拖垮读
        logger.warning("演化运行恢复失败 tenant=%s: %s", request.tenant_id, exc)

    async with get_db_session() as session:
        runs = await RunStore(session).list(request.tenant_id)
    active = next(
        (r for r in runs
         if r.get("status") == "running" and not r.get("stale")),
        None,
    )
    return {
        "ok": True,
        "repo_root": "db",
        "runs_dir": "db://evolution_runs",
        "active_run": ({"run_id": active.get("run_id"), "stage": active.get("stage")}
                       if active else None),
        "note": "服务化演化台",
    }


async def _evolution_list_runs(request: CapabilityRequest,
                               data: dict[str, Any]) -> dict:
    """运行列表：按调用方授权集合过滤（`None` = 租户管理员，不过滤）。

    显式 `kb` 过滤仍生效（闸门已拦授权外的 kb）；SQL 里它与授权集合是 AND
    关系，万一两条判断口径分叉也只会收窄、不会放开。
    """
    from .services.evolution.run_store import RunStore

    kb_id = data.get("kb") or data.get("knowledge_base_id") or None
    allowed = await _evolution_allowed_kb_ids(request)
    async with get_db_session() as session:
        runs = await RunStore(session).list(
            request.tenant_id, kb_id=kb_id, limit=_evolution_limit(data),
            kb_ids=allowed)
    return {"runs": runs}


async def _evolution_get_run(request: CapabilityRequest,
                             data: dict[str, Any]) -> dict:
    """运行详情（RunBundle）：补候选、相关版本与信任闭环字段。

    候选/版本是**附加信息**：读取失败（表未就绪、历史数据 shape 异常）只记
    日志并给空列表 —— 运行时详情本身不该因为审计面失败而 500（控制器 A4）。

    读前先 best-effort `recover_stale`（同 `_evolution_status`）：打开运行详情
    即可收回僵死 `adopting`。恢复用**独立 session**，失败只告警 —— 批量
    UPDATE 失败会中止事务，共会话时会让随后的 `get` 连锁 500。
    """
    from .services.evolution.candidate_service import CandidateService
    from .services.evolution.run_store import RunStore

    _require_nonblank(data, "run_id")
    run_id = data["run_id"]

    try:
        async with get_db_session() as session:
            await RunStore(session).recover_stale(request.tenant_id)
    except Exception as exc:  # noqa: BLE001 —— 自愈失败只告警，绝不拖垮读
        logger.warning("演化运行恢复失败 tenant=%s: %s", request.tenant_id, exc)

    async with get_db_session() as session:
        run = await RunStore(session).get(request.tenant_id, run_id)

    candidates: list[dict] = []
    try:
        async with get_db_session() as session:
            candidates = await CandidateService(session).list_for_run(
                request.tenant_id, run_id)
    except Exception as exc:  # noqa: BLE001 —— 附加信息失败不得拖垮详情
        logger.warning("演化运行候选读取失败 run=%s: %s", run_id, exc)

    versions: list[dict] = []
    try:
        versions = await _evolution_run_versions(request.tenant_id, run)
    except Exception as exc:  # noqa: BLE001 —— 同上
        logger.warning("演化运行版本读取失败 run=%s: %s", run_id, exc)

    return _evolution_bundle(run, candidates=candidates, versions=versions)


async def _evolution_list_versions(request: CapabilityRequest,
                                   data: dict[str, Any]) -> dict:
    """版本列表（控制器 A1）。

    - `scope` 省略：该 KB 三条链（abstract / synonym / compiled）**单条 UNION
      合并后统一分页**（`VersionStore.list_merged`，库内一次
      `ORDER BY + LIMIT/OFFSET`）；此时 `ds` **只作用于 compiled 链**，
      abstract / synonym 链恒为 KB 级、不报错；
    - `scope=abstract` / `scope=synonym` 且带 `ds` → 1001（与
      `VersionStore._validate_scope_ds` 同口径，不静默忽略）；
    - `scope=compiled` 且 `ds` 省略：返回该 KB 所有 DS 的编译版本（`all_ds`）；
    - 列表是摘要，默认不含完整 `snapshot`；
    - `offset` 上限 10000（超过 → 1001）。
    """
    from .services.evolution.version_store import VersionStore

    _require_nonblank(data, "kb")
    kb = str(data["kb"]).strip()

    scope = data.get("scope")
    if scope is not None and scope not in ("abstract", "synonym", "compiled"):
        raise InvalidParameterError(
            message="scope 必须是 abstract、synonym 或 compiled")

    raw_ds = data.get("ds")
    if raw_ds is not None and not isinstance(raw_ds, str):
        raise InvalidParameterError(message="ds 必须是字符串")
    ds = (raw_ds or "").strip() or None
    if scope in ("abstract", "synonym") and ds is not None:
        raise InvalidParameterError(
            message=f"{scope} 版本不接受 ds（KB 级单链）")

    include_archived = data.get("include_archived", False)
    if not isinstance(include_archived, bool):
        raise InvalidParameterError(message="include_archived 必须是布尔值")

    limit = _evolution_limit(data)
    offset = _evolution_offset(data)

    async with get_db_session() as session:
        store = VersionStore(session)
        if scope:
            result = await store.list(
                request.tenant_id, kb, scope, ds,
                include_archived=include_archived,
                limit=limit, offset=offset,
                all_ds=(scope == "compiled" and ds is None),
            )
            result["scope"] = scope
            result["ds"] = ds
            return result

        # scope 省略：三条链在库内一次 UNION 分页（offset 再大也不做线性多次取）。
        result = await store.list_merged(
            request.tenant_id, kb, ds=ds,
            include_archived=include_archived,
            limit=limit, offset=offset)

    result["scope"] = None
    result["ds"] = ds
    return result


async def _evolution_get_version(request: CapabilityRequest,
                                 data: dict[str, Any]) -> dict:
    """版本详情（控制器 A2 + 轮 1 I1）：默认不取 `snapshot`。

    - `include_snapshot=true` 才返回完整快照，否则只给 `snapshot_hash` 与摘要
      （默认路径连 SQL 都不 SELECT 该列，不是查出来再丢）；
    - `include_archived=true` 才允许读归档表（spec §3.1：归档版本可查询）。
      默认 false 时归档版本与不存在同口径 → 1002。

    跨租户 / 不存在同口径 → `ResourceNotFoundError`（1002），由仓储承担。
    """
    from .services.evolution.version_store import VersionStore

    _require_nonblank(data, "version_id")
    include_snapshot = data.get("include_snapshot", False)
    if not isinstance(include_snapshot, bool):
        raise InvalidParameterError(message="include_snapshot 必须是布尔值")
    include_archived = data.get("include_archived", False)
    if not isinstance(include_archived, bool):
        raise InvalidParameterError(message="include_archived 必须是布尔值")

    async with get_db_session() as session:
        # 默认不 SELECT snapshot 列（不是查出来再丢）；include_snapshot 才取。
        version = await VersionStore(session).get(
            request.tenant_id, data["version_id"],
            include_archived=include_archived,
            with_snapshot=include_snapshot)
    return version


async def _evolution_run_log(request: CapabilityRequest,
                             data: dict[str, Any]) -> dict:
    from .services.evolution.run_store import RunStore

    _require_nonblank(data, "run_id")
    async with get_db_session() as session:
        lines = await RunStore(session).log_tail(
            request.tenant_id, data["run_id"])
    return {"lines": lines, "exists": bool(lines)}


async def _evolution_start_run(request: CapabilityRequest,
                               data: dict[str, Any]) -> dict:
    """发起一次演化运行（幂等）。

    顺序是硬约束：`recover_stale` **必须**在 create 之前 —— 上个进程留下的
    孤儿 `queued`/`running` 行占着 `(tenant_id) WHERE status IN ('queued','running')`
    的部分唯一索引，不先恢复，同租户**永远**建不出新运行（create 恒 409），
    而恢复调用又排在 create 之后永远走不到。

    父版本在 **create 时**解析并写入（C1）：`adopt` 的 stale 守卫依赖 run 的
    `parent_version_id`；留到采纳时才补，会让所有新 run 走 legacy 分支、完全
    跳过「父版本仍是 active」的校验。链上还没有 active（首次采纳）才留 None。
    """
    from .services.evolution.locking import scope_for_kind
    from .services.evolution.run_store import RunStore
    from .services.evolution.version_store import VersionStore

    kind = str(data.get("kind") or "").strip()
    if kind not in ("alias", "query_rule", "relation", "constraint",
                    "mapping", "tool_schema", "synonym"):
        raise InvalidParameterError(
            message="kind 必须是 alias、query_rule、relation、constraint、"
                    "mapping、tool_schema 或 synonym")
    _require_nonblank(data, "kb")
    kb_id = str(data["kb"]).strip()
    # compiled 家族（query_rule / relation / constraint）的候选都挂在某个数据源
    # 的编译行上，没有数据源就没有「当前行」可读写。
    if kind in ("query_rule", "relation", "constraint"):
        _require_nonblank(data, "ds")
        ds_id = str(data.get("ds") or "").strip() or None
    else:
        # alias / mapping / tool_schema / synonym 都是 KB 级链（ds 恒为 None；
        # 前三个走 abstract 链、synonym 是独立链）：带 ds 是坏参数，不能静默
        # 忽略 —— 否则会写出一条与链键不符的 run，采纳时按 ds 取 active 恒为空。
        if str(data.get("ds") or "").strip():
            raise InvalidParameterError(
                message=f"{kind} 演化是 KB 级版本链，不接受 ds")
        ds_id = None

    request_id = data.get("request_id")
    if request_id is not None:
        # 空串/纯空白不是「没给幂等键」，而是一次坏参数 —— 放过去等于悄悄丢掉
        # 幂等保护（前端会以为重试是安全的）。
        if not isinstance(request_id, str) or not request_id.strip():
            raise InvalidParameterError(message="request_id 必须是非空字符串")
        request_id = request_id.strip()

    _validate_evolution_request(data)

    run_request = {key: data[key] for key in _EVOLUTION_REQUEST_FIELDS
                   if key in data}
    if kind == "mapping":
        # 基准源由条目推导（服务端写入，**不进** `_EVOLUTION_REQUEST_FIELDS`：
        # 它不是客户端可传的参数，只在 run.request 里给评测链路定位语料源）。
        run_request["bench_ds"] = str(
            data["entries"][0]["data_source_id"]).strip()
        # 映射写的是抽象 schema 的落点：抽象 schema 非 active 时映射不会生效。
        # 在开库**之前**拒绝（读行用函数内 import，测试可 monkeypatch）。
        from .services.evolution import schema_io

        row = await schema_io.get_row(
            request.tenant_id, "mapping", kb_id, None) or {}
        if row.get("status") != "active":
            raise InvalidParameterError(
                message="mapping 演化需要抽象 schema 处于 active"
                        "（当前未激活），映射不会生效")
    elif kind == "tool_schema":
        # 工具覆盖块写在抽象 schema 上，只有 active 行会进入多轮消费
        # （`extract_tool_overrides` 只认 active）。在开库前拒绝，避免跑完一整轮
        # 基准却测不到任何覆盖、采纳时再 409。
        from .services.evolution import schema_io

        row = await schema_io.get_row(
            request.tenant_id, "tool_schema", kb_id, None) or {}
        if row.get("status") != "active":
            raise InvalidParameterError(
                message="tool_schema 演化需要抽象 schema 处于 active"
                        "（当前未激活），工具覆盖不会生效")

    async with get_db_session() as session:
        if kind in ("query_rule", "relation", "constraint"):
            # DS 归属：闸门只校验过 KB，`ds` 是 body 里自由填的。不校验就能把
            # run 挂到**别的 KB 的**数据源上（采纳时读写的是那条源的编译行）。
            # 放在任何运行存储操作之前：坏参数不触发 recover / 幂等查询。
            await _assert_evolution_ds_in_kb(
                session, request.tenant_id, kb_id, ds_id or "")
        store = RunStore(session)
        await store.recover_stale(request.tenant_id)
        if request_id:
            existing = await store.get_by_request_id(
                request.tenant_id, request_id)
            if existing:
                return {"run_id": existing["run_id"], "reused": True}
        # 同链快速冲突：同一版本链已有采纳在飞时不建新运行。不能指望 create
        # 的租户级活跃唯一索引 —— 完整语料基准可能跑几十分钟，而且那条索引
        # 是「租户级」而非「链级」，冲突要等到创建时才暴露。
        other = await store.chain_adopting(
            request.tenant_id, kb_id, kind, ds_id)
        if other:
            raise ResourceConflictError(
                message=f"同一版本链已有采纳正在进行（{other}），稍后再试")
        # 父版本 = 该版本链当前 active；链上还没有 active → None（legacy 采纳）。
        scope = scope_for_kind(kind)
        active = await VersionStore(session).active_for(
            request.tenant_id, kb_id, scope, ds_id)
        parent_version_id = (active or {}).get("version_id")
        try:
            created = await store.create(
                request.tenant_id, kb_id, ds_id, kind, run_request,
                request_id=request_id,
                created_by=getattr(request, "user_id", None),
                parent_version_id=parent_version_id,
            )
        except ResourceConflictError:
            # 带 request_id 时，「撞唯一索引」只可能有一个成因：上一次其实
            # 已经建成了、只是回执没回来。重查并复用，不把 409 丢给前端重试。
            if request_id:
                existing = await store.get_by_request_id(
                    request.tenant_id, request_id)
                if existing:
                    return {"run_id": existing["run_id"], "reused": True}
            raise

    run_id = created["run_id"]
    _schedule_evolution_run(request.tenant_id, run_id)
    return {"run_id": run_id, "reused": False}


async def _evolution_list_ledgers(request: CapabilityRequest,
                                  data: dict[str, Any]) -> dict:
    from .services.evolution.ledger_service import LedgerService

    async with get_db_session() as session:
        return await LedgerService(session).list(request.tenant_id)


async def _evolution_list_corpus_cases(request: CapabilityRequest,
                                       data: dict[str, Any]) -> dict:
    """语料列表：`{target, cases}`（旧桥接形状，前端 `listCorpusCases` 只读 cases）。

    按调用方授权集合过滤（`None` = 租户管理员，不过滤）。
    """
    from .services.evolution.corpus_service import CorpusService

    allowed = await _evolution_allowed_kb_ids(request)
    async with get_db_session() as session:
        cases = await CorpusService(session).list(
            request.tenant_id, kb_ids=allowed)
    return {"target": {}, "cases": cases}


async def _evolution_create_corpus_case(request: CapabilityRequest,
                                        data: dict[str, Any]) -> dict:
    """新建语料条目。`ds_id` 必须属于 `kb_id`（否则 1001，见 DS 归属校验）。"""
    from .services.evolution.corpus_service import CorpusService

    async with get_db_session() as session:
        await _assert_evolution_ds_in_kb(
            session, request.tenant_id,
            str(data.get("kb_id") or "").strip(),
            str(data.get("ds_id") or "").strip())
        return await CorpusService(session).create(request.tenant_id, data)


async def _evolution_update_corpus_case(request: CapabilityRequest,
                                        data: dict[str, Any]) -> dict:
    from .services.evolution.corpus_service import CorpusService

    _require_nonblank(data, "case_id")
    async with get_db_session() as session:
        return await CorpusService(session).update(
            request.tenant_id, data["case_id"], data)


async def _evolution_delete_corpus_case(request: CapabilityRequest,
                                        data: dict[str, Any]) -> dict:
    from .services.evolution.corpus_service import CorpusService

    _require_nonblank(data, "case_id")
    async with get_db_session() as session:
        return await CorpusService(session).delete(
            request.tenant_id, data["case_id"])


async def _evolution_promote_adhoc(request: CapabilityRequest,
                                   data: dict[str, Any]) -> dict:
    """把 run 里已验证的 ad-hoc case 提升为语料（P1-2 Task 1）。

    `case_ids` 缺省 = 全部 verified；显式给出时服务层校验（不存在/未验证
    → 1001）。租户隔离由 `RunStore.get` 的 `tenant_id` 条件兜住（跨租户
    一律 1002），管理员闸门在 `execute()` 对 `_EVOLUTION_ACTIONS` 统一判。
    """
    from .services.evolution.corpus_service import CorpusService
    from .services.evolution.run_store import RunStore

    _require_nonblank(data, "run_id")
    async with get_db_session() as session:
        # run 的 (kb, ds) 也必须成立：语料行会绑到这个 DS 上，闸门只校验过
        # run 所属 KB（KB 管理员），DS 却来自 run 行、可能历史上写脏。
        # ds 为空（alias run）跳过 —— 后续由 load_corpus 解析或 1001。
        run = await RunStore(session).get(request.tenant_id, data["run_id"])
        run_ds = str(run.get("ds") or "").strip()
        if run_ds:
            await _assert_evolution_ds_in_kb(
                session, request.tenant_id,
                str(run.get("kb") or "").strip(), run_ds)
        return await CorpusService(session).promote_from_run(
            request.tenant_id, data["run_id"],
            case_ids=data.get("case_ids"),
            user_id=getattr(request, "user_id", None))


def _evolution_picks(data: dict[str, Any]) -> list[str] | None:
    """`picks` 校验：缺省/空数组 = 全取（与旧 CLI `--pick` 缺省同口径）。

    非数组或含非字符串条目是**坏参数**（1001），不是「没给」—— 静默忽略等于
    按「全取」执行，会采纳用户明确排除的条目。
    **全空白数组同样是 1001**：`[""]` 不能被吞成 `None`（那就退化成「全取」，
    恰好是用户想排除全部时的反义）。
    """
    picks = data.get("picks")
    if picks is None:
        return None
    if not isinstance(picks, list) or any(not isinstance(p, str) for p in picks):
        raise InvalidParameterError(message="picks 必须是字符串数组")
    if not picks:
        return None
    if not any(p.strip() for p in picks):
        raise InvalidParameterError(message="picks 不能全是空白")
    return picks


async def _evolution_adhoc_evidence(request: CapabilityRequest,
                                    data: dict[str, Any]) -> dict:
    """人工 ad-hoc 验真证据：临时 apply 候选 → 跑一次问句 → 恢复快照。

    只读辅助（不改 run 状态、不写台账）；`evidence_id` 绑定本次 picks 与
    case，采纳时由服务层校验「证据仍对应当前候选」。`picks` 缺省 = 全取。
    """
    from .services.evolution.adopt_service import AdoptService

    _require_nonblank(data, "run_id", "case_id")
    picks = _evolution_picks(data)
    async with get_db_session() as session:
        return await AdoptService(session).adhoc_evidence(
            request.tenant_id, data["run_id"], data["case_id"], picks)


async def _evolution_adopt_run(request: CapabilityRequest,
                               data: dict[str, Any]) -> dict:
    """`confirm=false`（缺省）→ 只读预览；`confirm=true` → 202 受理并后台执行。

    Task 9 起采纳是**异步**的：`accept()` 只做全部前置校验 + CAS
    `evaluated → adopting` 后立刻返回；apply / 冒烟 / 完整语料基准由
    `_schedule_evolution_adopt` 在独立会话里跑（网关据此回 202）。

    幂等重放（同 `request_id` 命中 `adopting`）**不再排一次** —— 后台已经在
    跑，重复调度只会多一个抢同一把链锁的任务。
    """
    from .services.evolution.adopt_service import (
        AdoptService,
        normalise_request_id,
        normalize_adhoc_truths,
        normalize_adhoc_verdicts,
    )

    _require_nonblank(data, "run_id")
    confirm = data.get("confirm")
    if confirm is not None and not isinstance(confirm, bool):
        # "false" 是真值，会真写库 —— 必须当场拒绝。
        raise InvalidParameterError(message="confirm 必须是布尔")
    picks = _evolution_picks(data)
    # 身份与执行参数在这里一次捕获：调度发生在 `async with` 退出之后，
    # 后台任务必须用**自己的**会话，不能复用/持有请求会话。
    user_id = getattr(request, "user_id", None)
    adopt_options: dict[str, Any] = {}
    # `confirm=false` 在上面的 `async with` 里直接 return；落到这里时 `accept`
    # 必然已经赋值（否则已抛异常）。用空 dict 初始化只是让「后面的 .get 一定
    # 有 dict 兜底」在静态检查下显然成立，不引入 None 分支。
    accepted: dict[str, Any] = {}
    async with get_db_session() as session:
        service = AdoptService(session)
        if not confirm:
            return await service.preview(
                request.tenant_id, data["run_id"], picks)
        # 采纳参数：形状非法一律 1001（在打开任何写路径之前）。
        request_id = normalise_request_id(data.get("request_id"))
        raw_override = data.get("override_reason")
        if raw_override is None:
            override_reason = None
        elif not isinstance(raw_override, str):
            raise InvalidParameterError(message="override_reason 必须是字符串")
        else:
            override_reason = raw_override.strip() or None
        adhoc_truths = normalize_adhoc_truths(data.get("adhoc_truths"))
        adhoc_verdicts = normalize_adhoc_verdicts(data.get("adhoc_verdicts"))
        adopt_options = {
            "picks": picks,
            "note": str(data.get("note") or ""),
            "user_id": user_id,
            "request_id": request_id,
            "override_reason": override_reason,
            "adhoc_truths": adhoc_truths,
            "adhoc_verdicts": adhoc_verdicts,
        }
        accepted = await service.accept(
            request.tenant_id, data["run_id"], picks,
            note=adopt_options["note"],
            user_id=user_id,
            request_id=request_id,
            override_reason=override_reason,
            adhoc_truths=adhoc_truths,
            adhoc_verdicts=adhoc_verdicts,
        )

    if accepted.get("accepted") is True and not accepted.get("idempotent_replay"):
        _schedule_evolution_adopt(
            request.tenant_id, data["run_id"], adopt_options)
    return accepted


async def _evolution_adopt_runs(request: CapabilityRequest,
                                data: dict[str, Any]) -> dict:
    """批量采纳（P1）：同 KB、每链至多一个 run、1..10 条，**确认采纳**语义。

    没有 `confirm` 参数、也不做批量预览 —— 调用即受理。传入 `confirm`（哪怕
    `confirm=false`）一律 1001：单 run 端点里 `confirm=false` 是**预览**，批量
    静默忽略它却真写库就是危险歧义。结构校验（形状/去重/同 KB/同链/上限）由
    服务层 `validate_batch` 在**受理前**整体完成；这里只做入口参数（list 类型 /
    override_reason 形状）的快速拒绝。

    所有 accepted 且非幂等重放的 run 交给**一个**后台任务串行执行
    （`_schedule_evolution_batch_adopt`，顺序 = 输入顺序）：参数逐字对齐
    `_evolution_adopt_run`（picks=None / note="" / 派生 request_id /
    override_reason / adhoc_truths），但不能各开一个任务 —— 兄弟 run 并发会让
    父本臂 ruler 互踩（见 helper docstring）。
    """
    from .services.evolution.adopt_service import normalize_adhoc_truths
    from .services.evolution.batch_adopt import batch_adopt, batch_request_id

    if "confirm" in data:
        raise InvalidParameterError(
            message="批量采纳不支持 confirm（批量即确认）")
    run_ids = data.get("run_ids")
    if not isinstance(run_ids, (list, tuple)):
        raise InvalidParameterError(message="run_ids 必须是数组")
    raw_override = data.get("override_reason")
    if raw_override is None:
        override_reason = None
    elif not isinstance(raw_override, str):
        raise InvalidParameterError(message="override_reason 必须是字符串")
    else:
        override_reason = raw_override.strip() or None
    adhoc_truths = normalize_adhoc_truths(data.get("adhoc_truths"))
    user_id = getattr(request, "user_id", None)

    result = await batch_adopt(
        request.tenant_id, list(run_ids),
        override_reason=override_reason,
        adhoc_truths=adhoc_truths,
        user_id=user_id,
    )

    batch_id = str(result.get("batch_id") or "")
    items: list[tuple[str, dict[str, Any]]] = []
    for item in result.get("results") or []:
        if item.get("accepted") is not True or item.get("idempotent_replay"):
            # 幂等重放不重排（后台已经在跑）：与单 run 受理同口径。
            continue
        items.append((item["run_id"], {
            "picks": None,
            "note": "",
            "user_id": user_id,
            "request_id": batch_request_id(batch_id, item["run_id"]),
            "override_reason": override_reason,
            "adhoc_truths": adhoc_truths,
        }))
    if items:
        # 一个任务、按输入顺序串行（不是每条一个任务）：见 helper docstring。
        _schedule_evolution_batch_adopt(request.tenant_id, items)
    return result


async def _evolution_reject_run(request: CapabilityRequest,
                                data: dict[str, Any]) -> dict:
    from .services.evolution.adopt_service import AdoptService

    _require_nonblank(data, "run_id")
    picks = _evolution_picks(data)
    async with get_db_session() as session:
        return await AdoptService(session).reject(
            request.tenant_id, data["run_id"], picks,
            note=str(data.get("note") or ""))


async def _evolution_rollback_run(request: CapabilityRequest,
                                  data: dict[str, Any]) -> dict:
    """回滚：`to_version_id` 省略时回滚到 run.parent_version_id（控制器 V4）。

    `to_version_id` 非空字符串校验在服务入口（`normalise_version_id`）与这里
    各做一次：这里是更靠前的一层，非法请求在打开 DB 会话之前就拒。
    """
    from .services.evolution.adopt_service import (
        AdoptService,
        normalise_version_id,
    )

    _require_nonblank(data, "run_id")
    to_version_id = normalise_version_id(data.get("to_version_id"))
    raw_reason = data.get("reason")
    if raw_reason is None:
        reason = ""
    elif not isinstance(raw_reason, str):
        raise InvalidParameterError(message="reason 必须是字符串")
    else:
        reason = raw_reason
    async with get_db_session() as session:
        return await AdoptService(session).rollback(
            request.tenant_id, data["run_id"], to_version_id, reason,
            getattr(request, "user_id", None))


async def _evolution_adjust_entry(request: CapabilityRequest,
                                  data: dict[str, Any]) -> dict:
    from .services.evolution.adopt_service import AdoptService

    _require_nonblank(data, "run_id", "entry_key")
    async with get_db_session() as session:
        return await AdoptService(session).adjust(
            request.tenant_id, data["run_id"], data["entry_key"],
            data.get("messages"))


async def _evolution_edit_entry(request: CapabilityRequest,
                                data: dict[str, Any]) -> dict:
    from .services.evolution.adopt_service import AdoptService

    _require_nonblank(data, "run_id", "entry_key")
    alias, expression = data.get("alias"), data.get("expression")
    for name, value in (("alias", alias), ("expression", expression)):
        if value is not None and not isinstance(value, str):
            raise InvalidParameterError(message=f"{name} 必须是字符串")
    async with get_db_session() as session:
        return await AdoptService(session).edit(
            request.tenant_id, data["run_id"], data["entry_key"],
            set_alias=alias, set_expression=expression)


# ── ②b 诊断（2026-10-18）────────────────────────────────────────────

def _evolution_days(data: dict[str, Any]) -> int:
    """诊断窗口天数：默认 7，收口到 1..365。

    类型错（字符串 / 布尔 / 浮点）是 **1001** 而不是「帮它转」——`"false"` 是真值、
    `True` 是 1，静默转换会把窗口悄悄改成别的值。越界（0 / 负数 / 999）夹紧：
    越界不影响语义正确性，只影响范围（与旧桥接 `max(1, int(days))` 同姿态）。

    过渡期兼容 `window_days`：Task 4 之前前端 `runDiagnosis` 发的仍是旧桥接的键。
    """
    raw = data.get("days")
    if raw is None:
        raw = data.get("window_days")
    if raw is None:
        return 7
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise InvalidParameterError(message="days 必须是整数")
    return max(1, min(raw, 365))


def _diagnosis_window(data: dict[str, Any]) -> tuple[Any, Any]:
    """`(since, until)`：until = 现在（naive 本地），since = until - days 天。

    观测 `created_at` 是容器墙钟（TZ=Asia/Shanghai），本机宿主须同帧；
    `diagnose._created_at` 对 aware 值会丢 tz，这里给的 until 恒为 naive。
    """
    until = datetime.datetime.now()
    since = until - datetime.timedelta(days=_evolution_days(data))
    return since, until


async def _evolution_list_failures(request: CapabilityRequest,
                                   data: dict[str, Any]) -> dict:
    """失败观测清单（桥接 `/failures` 形状：rows / since / until / total）。"""
    from .services.evolution.diagnosis_service import list_failures

    since, until = _diagnosis_window(data)
    async with get_db_session() as session:
        rows = await list_failures(session, request.tenant_id, since, until)
    return {"rows": rows,
            "since": since.isoformat(timespec="seconds"),
            "until": until.isoformat(timespec="seconds"),
            "total": len(rows)}


async def _evolution_run_diagnosis(request: CapabilityRequest,
                                   data: dict[str, Any]) -> dict:
    """跑一次诊断并落库；`ok`/`rc` 保留桥接期前端判据（`res.ok && res.report`）。

    操作人记 `request.user_id`（网关未带身份 → None，列可空）；窗口 `since`/`until`
    由服务返回值顶层带回（与桥接 `/diagnosis` 信封同形），handler 不再自己拼。
    """
    from .services.evolution.diagnosis_service import run_diagnosis

    since, until = _diagnosis_window(data)
    async with get_db_session() as session:
        result = await run_diagnosis(session, request.tenant_id, since, until,
                                     created_by=request.user_id)
    return {**result, "ok": True, "rc": 0}


async def _evolution_latest_diagnosis(request: CapabilityRequest,
                                      data: dict[str, Any]) -> dict:
    from .services.evolution.diagnosis_service import latest_report

    async with get_db_session() as session:
        return await latest_report(session, request.tenant_id)


# ── ②b 点踩诊断 / 抽象层选项 / 采纳（Task 3，2026-10-18）──────────────

def _evolution_dislike_days(data: dict[str, Any]) -> int:
    """点踩列表窗口天数：默认 30，收口到 1..365（桥接 `/dislikes` 同默认）。

    类型错是 1001（`"false"` / `True` / 浮点静默转换会悄悄改窗口）；越界夹紧 ——
    只影响范围、不影响语义（同 `_evolution_days` 姿态）。
    """
    raw = data.get("days")
    if raw is None:
        return 30
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise InvalidParameterError(message="days 必须是整数")
    return max(1, min(raw, 365))


def _dislike_window(data: dict[str, Any]) -> tuple[Any, Any]:
    """`(since, until)`：until = 现在（naive 本地），since = until - days 天。"""
    until = datetime.datetime.now()
    since = until - datetime.timedelta(days=_evolution_dislike_days(data))
    return since, until


async def _evolution_list_dislikes(request: CapabilityRequest,
                                   data: dict[str, Any]) -> dict:
    """点踩列表（桥接 `/dislikes` 形状：rows / since / until / total）。"""
    from .services.evolution.dislike_service import MAX_DISLIKES, list_dislikes

    since, until = _dislike_window(data)
    async with get_db_session() as session:
        rows = await list_dislikes(session, request.tenant_id, since, until,
                                   limit=MAX_DISLIKES)
    return {"rows": rows,
            "since": since.isoformat(timespec="seconds"),
            "until": until.isoformat(timespec="seconds"),
            "total": len(rows)}


async def _evolution_dislike_context(request: CapabilityRequest,
                                     data: dict[str, Any]) -> dict:
    from .services.evolution.dislike_service import context

    _require_nonblank(data, "feedback_id")
    async with get_db_session() as session:
        return await context(session, request.tenant_id, data["feedback_id"])


async def _evolution_replay_dislike(request: CapabilityRequest,
                                    data: dict[str, Any]) -> dict:
    """试跑（经 router 重走检索）：身份取 `request.user_id`（网关 invoke 报文透传）。

    服务层现场签发短时用户 JWT POST router 的 `search/mix`（观测由 router 拦下的
    ask 链路写入）；不可达 → 5xxx，不回退进程内检索。试跑可能调模型（最长几分钟），
    由网关路由的 timeout 放宽承接。
    """
    from .services.evolution.dislike_service import replay

    _require_nonblank(data, "feedback_id")
    async with get_db_session() as session:
        return await replay(session, request.tenant_id, request.user_id,
                            data["feedback_id"])


async def _evolution_adopt_dislike_alias(request: CapabilityRequest,
                                         data: dict[str, Any]) -> dict:
    from .services.evolution.dislike_service import adopt_alias

    _require_nonblank(data, "feedback_id", "concept", "alias")
    async with get_db_session() as session:
        return await adopt_alias(
            session, request.tenant_id, request.user_id, data["feedback_id"],
            data["concept"], data["alias"], note=str(data.get("note") or ""))


async def _evolution_metric_options(request: CapabilityRequest,
                                    data: dict[str, Any]) -> dict:
    from .services.evolution.dislike_service import metric_options

    _require_nonblank(data, "kb")
    return await metric_options(request.tenant_id, data["kb"])


async def _evolution_concept_options(request: CapabilityRequest,
                                     data: dict[str, Any]) -> dict:
    from .services.evolution.dislike_service import concept_options

    _require_nonblank(data, "kb")
    return await concept_options(request.tenant_id, data["kb"])


async def _evolution_tool_options(request: CapabilityRequest,
                                  data: dict[str, Any]) -> dict:
    """多轮工具名单：`TOOL_SCHEMAS` ⊕ active 抽象行的 `tools` 覆盖（只读）。"""
    from .services.evolution.dislike_service import tool_options

    _require_nonblank(data, "kb")
    return await tool_options(request.tenant_id, data["kb"])


async def _evolution_adopt_metric(request: CapabilityRequest,
                                  data: dict[str, Any]) -> dict:
    from .services.evolution.dislike_service import adopt_metric

    _require_nonblank(data, "id", "metric", "alias", "kb")
    async with get_db_session() as session:
        return await adopt_metric(
            session, request.tenant_id, request.user_id, data["id"],
            data["metric"], data["alias"], data["kb"],
            note=str(data.get("note") or ""))


# ── 信任闭环 Task 9：采纳/基准指标 ──────────────────────────────────

#: 指标窗口上限：只服务「最近一段」的运维/复盘面板；更大窗口对漏斗没有额外价值，
#: 却会让审计表扫描变大。越界夹紧（同 `_evolution_days` 姿态）。
_EVOLUTION_METRICS_MAX_DAYS = 90


async def _evolution_metrics(request: CapabilityRequest,
                             data: dict[str, Any]) -> dict:
    """`GET /evolution/metrics`：窗口 `days`（默认 7，1..90）内的采纳/基准指标。"""
    from .services.evolution.metrics_service import collect

    days = max(1, min(
        _evolution_int_field(data, "days", 7), _EVOLUTION_METRICS_MAX_DAYS))
    async with get_db_session() as session:
        return await collect(session, request.tenant_id, days=days)


# ── 语料治理 Task 5：覆盖率（只读） ──────────────────────────────────

async def _evolution_corpus_coverage(request: CapabilityRequest,
                                     data: dict[str, Any]) -> dict:
    """`GET /evolution/corpus-coverage`：按 (kb, ds) 的语料覆盖率与版本新鲜度。

    按调用方授权集合过滤（`None` = 租户管理员，不过滤）。
    """
    from .services.evolution.corpus_versions import coverage

    allowed = await _evolution_allowed_kb_ids(request)
    async with get_db_session() as session:
        return await coverage(session, request.tenant_id, kb_ids=allowed)


# ── 语料治理 Task 6：自动播种 ────────────────────────────────────────

async def _evolution_seed_corpus(request: CapabilityRequest,
                                 data: dict[str, Any]) -> dict:
    """`POST /evolution/corpus-seed`：从 schema / cases 播种语料。

    `mode` 只认 `schema` / `cases`（其他值 1001）；`cases` **仅在 cases 模式
    读取**（schema 模式忽略它，避免把一条写错的载荷静默混进播种）。
    `dry_run=true` 只算不写：不建行、不审计、不记版本。
    """
    from .services.evolution.corpus_seed import seed

    _require_nonblank(data, "kb", "ds", "mode")
    mode = str(data["mode"]).strip()
    if mode not in {"schema", "cases"}:
        raise InvalidParameterError(message="mode 必须是 schema 或 cases")
    cases = data.get("cases") if mode == "cases" else None
    # `dry_run` 只认真实 bool 或 "true"/"false"（大小写无关）：`bool("false")`
    # 会得到 True，把一次真跑静默变成演练（或反过来）。
    dry_run_raw = data.get("dry_run", False)
    if isinstance(dry_run_raw, bool):
        dry_run = dry_run_raw
    elif isinstance(dry_run_raw, str):
        lowered = dry_run_raw.strip().lower()
        if lowered not in {"true", "false"}:
            raise InvalidParameterError(message="dry_run 必须是布尔值")
        dry_run = lowered == "true"
    else:
        raise InvalidParameterError(message="dry_run 必须是布尔值")
    async with get_db_session() as session:
        # 同一个归属校验：播种也不能把语料挂到别的 KB 的数据源上（dry_run 同样拦）。
        await _assert_evolution_ds_in_kb(
            session, request.tenant_id,
            str(data["kb"]).strip(), str(data["ds"]).strip())
        return await seed(
            session, request.tenant_id, data["kb"], data["ds"], mode=mode,
            cases=cases, user_id=getattr(request, "user_id", None),
            dry_run=dry_run)
