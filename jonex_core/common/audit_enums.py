"""审计日志枚举与动作常量

跨能力共享的审计枚举类型。所有 capability 容器都包含 jonex_core，
因此此处定义的枚举可在任意能力服务中安全导入，无需 try-except 保护。

包含:
    - ResourceType: 资源类型枚举
    - _ACTION_TO_RESOURCE: action → resource_type 推导映射
    - HTTP_METHOD_TEMPLATE: HTTP 方法模板常量
"""

from enum import Enum


class ResourceType(str, Enum):
    """资源类型枚举，与 action 的分类对应。

    枚举值即 DB 中 resource 列的原始值，_RESOURCE_LABEL_ZH/_RESOURCE_LABEL_EN
    提供中英文显示名。

    使用:
        ResourceType.DOCUMENT.value        # "document"
        ResourceType.DOCUMENT.label_zh     # "文档"
        ResourceType.DOCUMENT.label_en     # "Document"
        ResourceType.HTTP.label_zh         # "HTTP 请求"
    """

    # ---- 文档 ----
    DOCUMENT = "document"
    DATA_SOURCE = "data_source"
    FOLDER = "folder"
    SPACE = "space"
    TAG = "tag"
    SYNONYM = "synonym"

    # ---- 本体 ----
    ONTOLOGY_INSTANCE = "ontology_instance"
    ONTOLOGY_RELATION = "ontology_relation"
    ONTOLOGY_GRAPH = "ontology_graph"
    ONTOLOGY_STATISTICS = "ontology_statistics"
    ONTOLOGY_QUERY = "ontology_query"

    # ---- 解析/编译 ----
    PARSER_SETTING = "parser_setting"
    CHUNK = "chunk"
    COMPILED_SCHEMA = "compiled_schema"
    RAW_CONTENT = "raw_content"

    # ---- 服务 ----
    SERVICE = "service"
    SERVICE_API_KEY = "service_api_key"
    KNOWLEDGE_INFO = "knowledge_info"

    # ---- 业务域 ----
    PROMPT_TEMPLATE = "prompt_template"
    ADAPTER = "adapter"
    PARSER = "parser"
    ACCESS_METHOD = "access_method"
    TEMPLATE_CONSTRAINT = "template_constraint"
    TEMPLATE_DOMAIN = "template_domain"
    TEMPLATE_OBJECT = "template_object"
    TEMPLATE_RELATION = "template_relation"
    TEMPLATE_SCENARIO = "template_scenario"
    PROVIDER = "provider"
    SKILL = "skill"
    PROMPT = "prompt"

    # ---- 权限 ----
    USER = "user"
    ROLE = "role"
    PERMISSION = "permission"
    MENU = "menu"
    APPLICATION = "application"
    SYSTEM_CONFIG = "system_config"
    TASK_SCHEDULE = "task_schedule"

    # ---- 认证/搜索 ----
    AUTH = "auth"
    SEARCH = "search"
    SEARCH_FEEDBACK = "search_feedback"
    SEARCH_HISTORY = "search_history"

    # ---- 编辑器 ----
    EDITOR_STATE = "editor_state"

    # ---- 通用 ----
    HTTP = "http"

    @property
    def label_zh(self) -> str:
        """中文标签，未知值返回原值"""
        return _RESOURCE_LABEL_ZH.get(self.value, self.value)

    @property
    def label_en(self) -> str:
        """英文标签，未知值返回原值"""
        return _RESOURCE_LABEL_EN.get(self.value, self.value)

    @classmethod
    def get_labels(cls, resource: str) -> dict[str, str]:
        """返回指定 resource_type 的中英文标签。

        用于 /audit-logs/resource-types API 将原始 resource 值映射为结构化标签。
        未知值返回原值作为 label_zh 和 label_en。
        """
        return {
            "label_zh": _RESOURCE_LABEL_ZH.get(resource, resource),
            "label_en": _RESOURCE_LABEL_EN.get(resource, resource),
        }

    @classmethod
    def resolve(cls, action: str) -> str | None:
        """从 action 推导对应的 resource_type。

        用于 collect() 和 @audit_action 装饰器自动填充 resource 字段。
        未知 action 返回 None。
        """
        if not action:
            return None
        return _ACTION_TO_RESOURCE.get(action)


# ============ 资源类型中文标签映射 ============

_RESOURCE_LABEL_ZH: dict[str, str] = {
    # ---- 文档 ----
    "document": "文档",
    "data_source": "数据源",
    "folder": "文件夹",
    "space": "空间",
    "tag": "标签",
    "synonym": "同义词",
    # ---- 本体 ----
    "ontology_instance": "本体实例",
    "ontology_relation": "本体关系",
    "ontology_graph": "本体图",
    "ontology_statistics": "本体统计",
    "ontology_query": "本体查询",
    # ---- 解析/编译 ----
    "parser_setting": "解析器设置",
    "chunk": "切片",
    "compiled_schema": "编译模式",
    "raw_content": "原始内容",
    # ---- 服务 ----
    "service": "服务",
    "service_api_key": "服务 API 密钥",
    "knowledge_info": "知识信息",
    # ---- 业务域 ----
    "prompt_template": "提示词模板",
    "adapter": "适配器",
    "parser": "解析器",
    "access_method": "数据接入",
    "template_constraint": "模板约束",
    "template_domain": "模板领域",
    "template_object": "模板对象",
    "template_relation": "模板关系",
    "template_scenario": "模板场景",
    "provider": "提供商",
    "skill": "技能",
    "prompt": "提示词",
    # ---- 权限 ----
    "user": "用户",
    "role": "角色",
    "permission": "权限",
    "menu": "菜单",
    "application": "应用",
    "system_config": "系统配置",
    "task_schedule": "任务调度",
    # ---- 认证/搜索 ----
    "auth": "认证",
    "search": "搜索",
    "search_feedback": "搜索反馈",
    "search_history": "搜索历史",
    # ---- 编辑器 ----
    "editor_state": "编辑器状态",
    # ---- 通用 ----
    "http": "HTTP 请求",
}

_RESOURCE_LABEL_EN: dict[str, str] = {
    # ---- 文档 ----
    "document": "Document",
    "data_source": "Data Source",
    "folder": "Folder",
    "space": "Space",
    "tag": "Tag",
    "synonym": "Synonym",
    # ---- 本体 ----
    "ontology_instance": "Ontology Instance",
    "ontology_relation": "Ontology Relation",
    "ontology_graph": "Ontology Graph",
    "ontology_statistics": "Ontology Statistics",
    "ontology_query": "Ontology Query",
    # ---- 解析/编译 ----
    "parser_setting": "Parser Setting",
    "chunk": "Chunk",
    "compiled_schema": "Compiled Schema",
    "raw_content": "Raw Content",
    # ---- 服务 ----
    "service": "Service",
    "service_api_key": "Service API Key",
    "knowledge_info": "Knowledge Info",
    # ---- 业务域 ----
    "prompt_template": "Prompt Template",
    "adapter": "Adapter",
    "parser": "Parser",
    "access_method": "Access Method",
    "template_constraint": "Template Constraint",
    "template_domain": "Template Domain",
    "template_object": "Template Object",
    "template_relation": "Template Relation",
    "template_scenario": "Template Scenario",
    "provider": "Provider",
    "skill": "Skill",
    "prompt": "Prompt",
    # ---- 权限 ----
    "user": "User",
    "role": "Role",
    "permission": "Permission",
    "menu": "Menu",
    "application": "Application",
    "system_config": "System Config",
    "task_schedule": "Task Schedule",
    # ---- 认证/搜索 ----
    "auth": "Authentication",
    "search": "Search",
    "search_feedback": "Search Feedback",
    "search_history": "Search History",
    # ---- 编辑器 ----
    "editor_state": "Editor State",
    # ---- 通用 ----
    "http": "HTTP Request",
}

# ============ Action → ResourceType 映射 ============

_ACTION_TO_RESOURCE: dict[str, str] = {
    # ---- 认证 → auth ----
    "auth.login": "auth",
    "auth.exchange_ticket": "auth",
    "auth.logout": "auth",
    # ---- 文档 → document ----
    "document.upload": "document",
    "document.parse": "document",
    "document.parse_done": "document",
    "document.parse_failed": "document",
    "document.parse_recover": "document",
    "document.reparse": "document",
    "document.delete": "document",
    # ---- HTTP → http ----
    "http.get": "http",
    "http.post": "http",
    "http.put": "http",
    "http.delete": "http",
    "http.patch": "http",
    # ---- 提示词模板 → prompt_template ----
    "create_prompt_template": "prompt_template",
    "update_prompt_template": "prompt_template",
    "delete_prompt_template": "prompt_template",
    "copy_prompt_template": "prompt_template",
    "get_prompt_template": "prompt_template",
    "rollback_prompt_template": "prompt_template",
    "list_prompt_templates": "prompt_template",
    "list_prompt_template_versions": "prompt_template",
    # ---- 适配器 → adapter ----
    "create_adapter": "adapter",
    "update_adapter": "adapter",
    "connect_adapter": "adapter",
    "disconnect_adapter": "adapter",
    "list_adapters": "adapter",
    # ---- 解析器 → parser ----
    "create_parser": "parser",
    "update_parser": "parser",
    "list_parsers": "parser",
    # ---- 数据接入 → access_method ----
    "create_access_method": "access_method",
    "update_access_method": "access_method",
    "list_access_methods": "access_method",
    # ---- 模板约束 → template_constraint ----
    "create_template_constraint": "template_constraint",
    "delete_template_constraint": "template_constraint",
    "update_template_constraint": "template_constraint",
    "list_template_constraints": "template_constraint",
    # ---- 模板领域 → template_domain ----
    "create_template_domain": "template_domain",
    "delete_template_domain": "template_domain",
    "update_template_domain": "template_domain",
    "get_template_domain": "template_domain",
    "list_template_domains": "template_domain",
    # ---- 模板对象 → template_object ----
    "create_template_object": "template_object",
    "delete_template_object": "template_object",
    "update_template_object": "template_object",
    "list_template_objects": "template_object",
    # ---- 模板关系 → template_relation ----
    "create_template_relation": "template_relation",
    "delete_template_relation": "template_relation",
    "update_template_relation": "template_relation",
    "list_template_relations": "template_relation",
    # ---- 模板场景 → template_scenario ----
    "create_template_scenario": "template_scenario",
    "delete_template_scenario": "template_scenario",
    "update_template_scenario": "template_scenario",
    "get_template_scenario": "template_scenario",
    "list_template_scenarios": "template_scenario",
    # ---- 提供商 → provider ----
    "create_provider": "provider",
    "update_provider": "provider",
    "test_provider": "provider",
    "list_providers": "provider",
    # ---- 技能 → skill ----
    "enable_skill": "skill",
    "disable_skill": "skill",
    "get_skill": "skill",
    "list_skills": "skill",
    "list_enabled_mcp_tools": "skill",
    # ---- 知识库 → knowledge_info / document / folder / data_source / tag / synonym / service / space / chunk / parser_setting / compiled_schema / raw_content / editor_state / ontology_* ----
    "add_document_tag": "document",
    "cancel_search_feedback": "search_feedback",
    "clear_search_history": "search_history",
    "create_data_source": "data_source",
    "create_folder": "folder",
    "create_knowledge_info": "knowledge_info",
    "create_ontology_instance": "ontology_instance",
    "create_ontology_relation": "ontology_relation",
    "create_parser_setting": "parser_setting",
    "create_service": "service",
    "create_service_api_key": "service_api_key",
    "create_space": "space",
    "create_synonym": "synonym",
    "create_tag": "tag",
    "delete_data_source": "data_source",
    "delete_document": "document",
    "delete_folder": "folder",
    "delete_knowledge_info": "knowledge_info",
    "delete_ontology_instance": "ontology_instance",
    "delete_ontology_relation": "ontology_relation",
    "delete_parser_setting": "parser_setting",
    "delete_search_history": "search_history",
    "delete_service": "service",
    "delete_service_api_key": "service_api_key",
    "delete_space": "space",
    "delete_synonym": "synonym",
    "delete_tag": "tag",
    "documents_stats": "document",
    "expand_ontology_neighbors": "ontology_instance",
    "generate_upload_url": "document",
    "get_chunk": "chunk",
    "get_compiled_schema": "compiled_schema",
    "get_data_source": "data_source",
    "get_document": "document",
    "get_document_chunks": "document",
    "get_document_parse_result": "document",
    "get_document_tags": "document",
    "get_editor_state": "editor_state",
    "get_knowledge_info": "knowledge_info",
    "get_ontology_graph": "ontology_graph",
    "get_ontology_statistics": "ontology_statistics",
    "get_parse_result_documents": "document",
    "get_parse_result_entities": "document",
    "get_parse_result_graph": "document",
    "get_parse_result_graph_summary": "document",
    "get_parse_result_relationships": "document",
    "get_parse_result_summary": "document",
    "get_raw_content": "raw_content",
    "get_raw_location": "document",
    "get_raw_url": "document",
    "get_search_feedback_stats": "search_feedback",
    "get_search_overview": "search",
    "get_service": "service",
    "get_service_configs": "service",
    "get_service_permissions": "service",
    "get_space": "space",
    "get_space_permissions": "space",
    "import_synonyms": "synonym",
    "ingest_push": "data_source",
    "list_data_sources": "data_source",
    "list_documents": "document",
    "list_folders": "folder",
    "list_knowledge_info": "knowledge_info",
    "list_ontology_entity_types": "ontology_instance",
    "list_ontology_instances": "ontology_instance",
    "list_ontology_relation_types": "ontology_relation",
    "list_ontology_relations": "ontology_relation",
    "list_parser_settings": "parser_setting",
    "list_search_feedback": "search_feedback",
    "list_search_history": "search_history",
    "list_service_api_keys": "service_api_key",
    "list_services": "service",
    "list_spaces": "space",
    "list_synonyms": "synonym",
    "list_tags": "tag",
    "query_with_ontology": "ontology_query",
    "recompile_schema": "compiled_schema",
    "reconcile_documents": "document",
    "reconcile_ontology": "ontology_instance",
    "reextract_kb_documents": "document",
    "remove_document_tag": "document",
    "rename_folder": "folder",
    "reparse_document": "document",
    "reseed_compiled_schema": "compiled_schema",
    "reset_ingest_key": "data_source",
    "resolve_references": "document",
    "retry_ontology_extract": "ontology_instance",
    "rotate_service_api_key": "service_api_key",
    "save_compiled_schema": "compiled_schema",
    "save_search_history": "search_history",
    "search": "search",
    "search_enhanced": "search",
    "search_ontology_entities": "search",
    "search_service": "service",
    "set_document_folder": "document",
    "set_document_tags": "document",
    "set_service_permissions": "service",
    "set_space_permissions": "space",
    "submit_search_feedback": "search_feedback",
    "sync_data_source": "data_source",
    "test_data_source": "data_source",
    "toggle_search_feedback_adopted": "search_feedback",
    "update_data_source": "data_source",
    "update_knowledge_info": "knowledge_info",
    "update_ontology_instance": "ontology_instance",
    "update_ontology_relation": "ontology_relation",
    "update_parser_setting": "parser_setting",
    "update_service": "service",
    "update_service_configs": "service",
    "update_space": "space",
    "update_synonym": "synonym",
    "update_tag": "tag",
    "upload_document": "document",
    # ---- 原子能力 → prompt ----
    "create_prompt": "prompt",
    "update_prompt": "prompt",
    "delete_prompt": "prompt",
    "get_prompt": "prompt",
    "retry": "prompt",
}

# ============ ResourceType → ID 字段名映射 ============

_RESOURCE_TO_ID_FIELD: dict[str, str] = {
    "space": "space_id",
    "document": "doc_id",
    "knowledge_info": "knowledge_base_id",
    "data_source": "ds_id",
    "folder": "folder_id",
    "service": "service_id",
    "tag": "tag_id",
    "synonym": "synonym_id",
    "prompt": "prompt_id",
    "provider": "provider_id",
    "adapter": "adapter_id",
    "parser": "parser_id",
    "knowledge_base": "kb_id",
    "ontology_instance": "instance_id",
    "ontology_relation": "relation_id",
    "compiled_schema": "schema_id",
    "parser_setting": "setting_id",
    "prompt_template": "template_id",
    "template_constraint": "constraint_id",
    "template_domain": "domain_id",
    "template_object": "object_id",
    "template_relation": "relation_id",
    "template_scenario": "scenario_id",
    "access_method": "method_id",
    "search_history": "history_id",
    "search_feedback": "feedback_id",
    "service_api_key": "api_key_id",
    "editor_state": "editor_id",
    "chunk": "chunk_id",
    "ontology_graph": "graph_id",
    "ontology_statistics": "stats_id",
    "ontology_query": "query_id",
}

# ============ ResourceType → DB 名称查询映射 ============
# 用于 ResourceNameEnricher 查询资源实例的显示名称。
# 格式：resource_type → (schema, table, id_column, name_column)
# 仅包含有对应 DB 实体表且存在 display-name 列的 resource_type。

_RESOURCE_NAME_LOOKUP: dict[str, tuple[str, str, str, str]] = {
    # ---- Platform ----
    "user": ("platform", "users", "id", "display_name"),
    "role": ("platform", "roles", "id", "name"),
    "permission": ("platform", "permissions", "id", "name"),
    "menu": ("platform", "menus", "id", "name"),
    "application": ("platform", "applications", "id", "name"),
    "system_config": ("platform", "system_configs", "id", "config_key"),
    "task_schedule": ("platform", "task_schedules", "id", "name"),
    # ---- Knowledge Base ----
    "space": ("knowledge_base", "spaces", "id", "name"),
    "knowledge_info": ("knowledge_base", "knowledge_info", "id", "name"),
    "document": ("knowledge_base", "knowledge_documents", "id", "file_name"),
    "data_source": ("knowledge_base", "knowledge_data_sources", "id", "name"),
    "folder": ("knowledge_base", "folders", "id", "name"),
    "tag": ("knowledge_base", "tags", "id", "name"),
    "synonym": ("knowledge_base", "ontology_synonyms", "id", "canonical"),
    "service": ("knowledge_base", "services", "id", "name"),
    "service_api_key": ("knowledge_base", "service_api_keys", "id", "key_prefix"),
    "parser_setting": ("knowledge_base", "knowledge_parser_settings", "id", "parser_type"),
    # ---- Business Domain ----
    "prompt_template": ("business_domain", "prompt_templates", "id", "name"),
    "template_domain": ("business_domain", "template_domains", "id", "name"),
    "template_scenario": ("business_domain", "template_scenarios", "id", "name"),
    "template_object": ("business_domain", "template_objects", "id", "name"),
    "template_relation": ("business_domain", "template_relations", "id", "name"),
    "template_constraint": ("business_domain", "template_constraints", "id", "name"),
    "provider": ("business_domain", "model_providers", "id", "name"),
    "adapter": ("business_domain", "adapters", "id", "name"),
    "parser": ("business_domain", "parser_configs", "id", "name"),
    "access_method": ("business_domain", "data_access_methods", "id", "name"),
    "skill": ("business_domain", "skill_catalog", "id", "name"),
}

# ============ HTTP 方法模板常量 ============

# 非枚举成员，仅供动态拼接 HTTP action 时使用
HTTP_METHOD_TEMPLATE = "http.{method}"
