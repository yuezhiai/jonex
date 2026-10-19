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
from jonex_core.common.config import get_config
from jonex_core.common.exceptions import (
    InternalError,
    InvalidParameterError,
    JonexException,
    PermissionDeniedError,
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
# [jonex] KB_ENABLED_TYPES：本部署允许创建哪些 KB 类型（部署级白名单，随 env 走）
from .services.kb_type_service import enabled_kb_types, kb_type_options

logger = logging.getLogger(__name__)

ActionHandler = Callable[[CapabilityRequest, dict[str, Any]], Awaitable[dict]]

#: 「这一趟对账没跑成」的哨兵（`_reconcile_pass` 的返回值）。
#: 必须与「跑成了、只是结果是空的」区分开 —— 后者是正常回执（如 `{}`、`None`、
#: `{"timed_out": 0}`），拿假值判失败会把每一轮都记成失败、退化成一味退避。
_PASS_FAILED = object()

# ---------------------------------------------------------------------------
# 模块级常量：每个 action 的必填字段（纯静态数据，避免每次调用重建 ~90 键 dict）
# 与 _build_dispatch() 的 key 集合保持一致，由 test_capability_dispatch_consistency 验证。
# ---------------------------------------------------------------------------
_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "get_document": ("document_id", "knowledge_base_id"),
    "list_document_versions": ("document_id",),
    "upload_document_version": ("document_id", "knowledge_base_id", "storage_key"),
    "get_document_version_url": ("document_id", "version_no"),
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
    # ── 回答级反馈（history_id 锚点）──
    "submit_answer_feedback": ("history_id", "operation_id", "version", "feedback_type"),
    "get_answer_feedback": ("history_id",),
    "list_answer_feedback": ("knowledge_base_id",),
    "get_answer_feedback_stats": ("knowledge_base_id",),
    "toggle_answer_feedback_adopted": ("feedback_id",),
    "delete_answer_feedback": ("feedback_id",),
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
    "ingest_push",
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
})

# 空间级管理：仅 owner 或租户管理员（tenant:write）。
# 显式声明，不得落入前缀推导的 write 语义（manager 删空间必须 403）。
_SPACE_LEVEL_ACTIONS = frozenset({
    "update_space", "delete_space", "set_space_permissions",
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
    "create_knowledge_info", "update_knowledge_info", "delete_knowledge_info",
    "upload_document", "delete_document", "reparse_document",
    "upload_document_version",
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
    "create_ontology_instance", "update_ontology_instance", "delete_ontology_instance",
    "create_ontology_relation", "update_ontology_relation", "delete_ontology_relation",
    "create_synonym", "update_synonym", "delete_synonym", "import_synonyms",
    "create_tag", "update_tag", "delete_tag",
    "create_parser_setting", "update_parser_setting", "delete_parser_setting",
    "save_compiled_schema", "import_compiled_schema_yaml", "reseed_compiled_schema",
    "recompile_schema", "reextract_kb_documents", "retry_ontology_extract",
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
# 只收这 5 个「纯浏览知识库」的入口，判据是**每个都能从 knowledge_base_id/kb_id
# 解析出 kb_id**（见 _resolve_space_context），否则 KB 授权兜底无从查起。
_KB_MEMBER_READ_ACTIONS = frozenset({
    "list_documents", "documents_stats", "list_folders",
    "get_document_tags", "get_kb_permissions",
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
    "list_ontology_instances", "list_ontology_relations",
    "list_ontology_entity_types", "list_ontology_relation_types",
    "get_ontology_graph", "get_ontology_statistics",
    "expand_ontology_neighbors", "search_ontology_entities",
    "get_compiled_schema", "export_compiled_schema_yaml", "export_llm_wiki_schema_yaml",
    "get_compiled_schema_for_extraction",  # [jonex] atomic-rag 抽取入口（含同义词注入）
    "get_llm_wiki_schema", "list_synonyms", "list_tags", "list_parser_settings",
    "resolve_wiki_evidence", "get_wiki_page", "list_wiki_contents", "get_wiki_outline", "get_editor_state",
    "get_wiki_compilation_state",
    "get_wiki_backlinks", "get_wiki_claims",
    "get_parse_result_documents", "get_parse_result_entities",
    "get_parse_result_graph", "get_parse_result_graph_summary",
    "get_parse_result_relationships", "get_parse_result_summary",
    "get_space_permissions",
})

# 检索类（过滤无权限 KB，不整体拒绝）
_SEARCH_ACTIONS = frozenset({
    "search", "search_enhanced", "deep_query", "search_mix",
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
})


class KnowledgeBaseCapability(BaseCapability):
    """Knowledge Base capability adapter."""

    _INTERNAL_ACTIONS = {"ingest_push"}

    # [jonex] 双向门禁（方案 §15）：本体 schema/抽取类 action 拒绝 openkb KB。
    # 清单与方案 §15 一致——这些 action 当前都在 dispatch 直接分发、无 kb_type 校验。
    _ONTOLOGY_GATED_ACTIONS = {
        "get_compiled_schema",
        # [jonex] 抽取入口同为「本体 schema 消费方」，openkb KB 一律拒绝（同 get_compiled_schema）
        "get_compiled_schema_for_extraction",
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

        # 调用明细采集写口（monitor.call_attempt，N2）。task_runtime 四个方向的重试埋点
        # （`jonex_core/task_runtime/runtime.py` 的 `attempt_span`）走的是共享内核那个
        # 采集器，而**采集器是每进程一份实例**：本服务不启动它，埋点会静默失效
        # （`put_call_attempt` 见 `_running=False` 直接返回，业务无感、数据全丢）。
        # 同 platform 的理由：采集是旁路，起不来绝不能拖垮知识库本体。
        try:
            from jonex_core.common.monitor_collector import get_monitor_collector
            get_monitor_collector().start()
            logger.info("MonitorCollector 已启动")
        except Exception:
            logger.exception("MonitorCollector 启动失败（不影响知识库功能）")

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
        try:
            from jonex_core.common.monitor_collector import get_monitor_collector
            await get_monitor_collector().stop()
            logger.info("MonitorCollector 已停止")
        except Exception:
            logger.exception("MonitorCollector 停止失败")

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

    def _build_dispatch(self) -> dict[str, ActionHandler]:
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
        kbinfo = self.service.knowledge_infos
        compiler = self.service.compiler
        lws = self.service.llm_wiki_schemas  # [jonex] LLM-Wiki Schema
        wo = self.service.wiki_outlines  # [jonex] Wiki 阅读目录
        folders = self.service.folders
        syn = self.service.synonyms
        oq = self.service.ontology_query
        ds = self.service.data_sources
        ps = self.service.parser_settings
        s = self._space
        sv = self._domain_service
        quota = QuotaService()
        analytics = AnalyticsService()

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
            "generate_upload_url": lambda r, d: docs.generate_upload_url(
                r.tenant_id, d["knowledge_base_id"], d["file_name"], d.get("content_type"),
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
            "get_compiled_schema": lambda r, d: compiler.get_compiled_schema(
                r.tenant_id, d["knowledge_base_id"],
            ),
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

        # [jonex] 任务中心接入：运行方那两个入口（§4.1 ① 派活 `execute` / ② 删除
        # `remove_task`）。**就这一行，别自己往 `dispatch` 里塞方法** —— `bind()` 兜住
        # 两个会静默坏掉的坑（签名必须是两个位置参数 `(request, data)`；回执必须是
        # 裸三层壳 dict）。塞 `runner.execute` 会 `TypeError` → 兜成 500 → 子任务直接
        # 终态 `failed`；回 pydantic 对象会被判成「没送达」→ 传送层重发，同一个
        # `execution_id` 最多被调 3 次（详见 `RunnerBase.bind` 的 docstring）。
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
        await write_request_event(
            tenant_id=request.tenant_id,
            request_id=request.request_id,
            user_id=_user_id(request),
            query=data.get("query"),
            kb_ids=list(kb_ids),
            entry_service_id=data.get("service_id"),
            domain_space_id=data.get("domain_space_id"),
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
                    SPACE_MANAGER,
                    SPACE_MEMBER,
                    has_space_role,
                    is_tenant_admin,
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
