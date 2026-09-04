"""审计日志枚举与动作常量

集中定义，避免散落的魔法字符串。

注意：跨能力共享的 ResourceType 枚举定义在 jonex_core/common/audit_enums.py，
本文件从该模块导入并 re-export，同时保留 platform 专属的 AuditAction / LogType 等。
"""

# 注意：新增 dispatch action 后请同步更新 _LABEL_ZH/_LABEL_EN

from enum import Enum

# 从 jonex_core 导入跨能力共享的审计枚举
from jonex_core.common.audit_enums import (
    HTTP_METHOD_TEMPLATE,
    ResourceType,
    _ACTION_TO_RESOURCE,
    _RESOURCE_LABEL_EN,
    _RESOURCE_LABEL_ZH,
)


class LogType(str, Enum):
    """日志大类"""
    LOGIN = "LOGIN"
    OPERATION = "OPERATION"
    SYSTEM = "SYSTEM"
    TASK = "TASK"


class Outcome(str, Enum):
    """执行结果"""
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class LogLevel(str, Enum):
    """日志级别（默认按 outcome 推导）"""
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"


# ============ 中文标签映射 ============

_LABEL_ZH: dict[str, str] = {
    # ---- 认证 ----
    "auth.login": "登录",
    "auth.exchange_ticket": "票据交换",
    "auth.logout": "登出",
    # ---- 文档 ----
    "document.upload": "上传文档",
    "document.parse": "解析文档",
    "document.parse_done": "文档解析完成",
    "document.parse_failed": "文档解析失败",
    "document.parse_recover": "文档解析恢复",
    "document.reparse": "重新解析文档",
    "document.delete": "删除文档",
    # ---- HTTP 方法 ----
    "http.get": "GET",
    "http.post": "POST",
    "http.put": "PUT",
    "http.delete": "DELETE",
    "http.patch": "PATCH",
    # ---- 提示词模板 ----
    "create_prompt_template": "创建提示词模板",
    "update_prompt_template": "更新提示词模板",
    "delete_prompt_template": "删除提示词模板",
    "copy_prompt_template": "复制提示词模板",
    "get_prompt_template": "查看提示词模板",
    "rollback_prompt_template": "回滚提示词模板",
    "list_prompt_templates": "提示词模板列表",
    "list_prompt_template_versions": "提示词模板版本列表",
    # ---- 适配器 ----
    "create_adapter": "创建适配器",
    "update_adapter": "更新适配器",
    "connect_adapter": "连接适配器",
    "disconnect_adapter": "断开适配器",
    "list_adapters": "适配器列表",
    # ---- 解析器 ----
    "create_parser": "创建解析器",
    "update_parser": "更新解析器",
    "list_parsers": "解析器列表",
    # ---- 数据接入 ----
    "create_access_method": "创建数据接入",
    "update_access_method": "更新数据接入",
    "list_access_methods": "数据接入列表",
    # ---- 模板约束/领域/对象/关系/场景 ----
    "create_template_constraint": "创建模板约束",
    "delete_template_constraint": "删除模板约束",
    "update_template_constraint": "更新模板约束",
    "list_template_constraints": "模板约束列表",
    "create_template_domain": "创建模板领域",
    "delete_template_domain": "删除模板领域",
    "update_template_domain": "更新模板领域",
    "get_template_domain": "查看模板领域",
    "list_template_domains": "模板领域列表",
    "create_template_object": "创建模板对象",
    "delete_template_object": "删除模板对象",
    "update_template_object": "更新模板对象",
    "list_template_objects": "模板对象列表",
    "create_template_relation": "创建模板关系",
    "delete_template_relation": "删除模板关系",
    "update_template_relation": "更新模板关系",
    "list_template_relations": "模板关系列表",
    "create_template_scenario": "创建模板场景",
    "delete_template_scenario": "删除模板场景",
    "update_template_scenario": "更新模板场景",
    "get_template_scenario": "查看模板场景",
    "list_template_scenarios": "模板场景列表",
    # ---- 提供商/技能 ----
    "create_provider": "创建提供商",
    "update_provider": "更新提供商",
    "test_provider": "测试提供商",
    "list_providers": "提供商列表",
    "enable_skill": "启用技能",
    "disable_skill": "禁用技能",
    "get_skill": "查看技能",
    "list_skills": "技能列表",
    "list_enabled_mcp_tools": "MCP 工具列表",

    # ---- 知识库 ----
    "add_document_tag": "添加文档标签",
    "cancel_search_feedback": "取消搜索反馈",
    "clear_search_history": "清空搜索历史",
    "create_data_source": "创建数据源",
    "create_folder": "创建文件夹",
    "create_knowledge_info": "创建知识信息",
    "create_ontology_instance": "创建本体实例",
    "create_ontology_relation": "创建本体关系",
    "create_parser_setting": "创建解析器设置",
    "create_service": "创建服务",
    "create_service_api_key": "创建服务 API 密钥",
    "create_space": "创建空间",
    "create_synonym": "创建同义词",
    "create_tag": "创建标签",
    "delete_data_source": "删除数据源",
    "delete_document": "删除文档",
    "delete_folder": "删除文件夹",
    "delete_knowledge_info": "删除知识信息",
    "delete_ontology_instance": "删除本体实例",
    "delete_ontology_relation": "删除本体关系",
    "delete_parser_setting": "删除解析器设置",
    "delete_search_history": "删除搜索历史",
    "delete_service": "删除服务",
    "delete_service_api_key": "删除服务 API 密钥",
    "delete_space": "删除空间",
    "delete_synonym": "删除同义词",
    "delete_tag": "删除标签",
    "documents_stats": "文档统计",
    "expand_ontology_neighbors": "展开本体邻居",
    "generate_upload_url": "生成上传 URL",
    "get_chunk": "查看切片",
    "get_compiled_schema": "查看编译模式",
    "get_data_source": "查看数据源",
    "get_document": "查看文档",
    "get_document_chunks": "查看文档切片",
    "get_document_parse_result": "查看文档解析结果",
    "get_document_tags": "查看文档标签",
    "get_editor_state": "查看编辑器状态",
    "get_knowledge_info": "查看知识信息",
    "get_ontology_graph": "查看本体图",
    "get_ontology_statistics": "查看本体统计",
    "get_parse_result_documents": "查看解析结果文档",
    "get_parse_result_entities": "查看解析结果实体",
    "get_parse_result_graph": "查看解析结果图",
    "get_parse_result_graph_summary": "查看解析结果图摘要",
    "get_parse_result_relationships": "查看解析结果关系",
    "get_parse_result_summary": "查看解析结果摘要",
    "get_raw_content": "查看原始内容",
    "get_raw_location": "查看原始位置",
    "get_raw_url": "查看原始 URL",
    "get_search_feedback_stats": "查看搜索反馈统计",
    "get_search_overview": "查看搜索概览",
    "get_service": "查看服务",
    "get_service_configs": "查看服务配置",
    "get_space": "查看空间",
    "get_space_permissions": "查看空间权限",
    "import_synonyms": "导入同义词",
    "ingest_push": "入站推送",
    "list_data_sources": "数据源列表",
    "list_documents": "文档列表",
    "list_folders": "文件夹列表",
    "list_knowledge_info": "知识信息列表",
    "list_ontology_entity_types": "本体实体类型列表",
    "list_ontology_instances": "本体实例列表",
    "list_ontology_relation_types": "本体关系类型列表",
    "list_ontology_relations": "本体关系列表",
    "list_parser_settings": "解析器设置列表",
    "list_search_feedback": "搜索反馈列表",
    "list_search_history": "搜索历史列表",
    "list_service_api_keys": "服务 API 密钥列表",
    "list_services": "服务列表",
    "list_spaces": "空间列表",
    "list_synonyms": "同义词列表",
    "list_tags": "标签列表",
    "query_with_ontology": "本体查询",
    "recompile_schema": "重新编译模式",
    "reconcile_documents": "文档对账",
    "reconcile_ontology": "本体对账",
    "reextract_kb_documents": "重新抽取知识库文档",
    "remove_document_tag": "移除文档标签",
    "rename_folder": "重命名文件夹",
    "reparse_document": "重新解析文档",
    "reseed_compiled_schema": "重新播种编译模式",
    "reset_ingest_key": "重置接入密钥",
    "resolve_references": "解析引用",
    "retry_ontology_extract": "重试本体抽取",
    "rotate_service_api_key": "轮换服务 API 密钥",
    "save_compiled_schema": "保存编译模式",
    "save_search_history": "保存搜索历史",
    "search": "搜索",
    "search_enhanced": "增强搜索",
    "search_ontology_entities": "搜索本体实体",
    "search_service": "搜索服务",
    "batch_set_document_folder": "批量移动文档",
    "set_document_folder": "设置文档文件夹",
    "set_document_tags": "设置文档标签",
    "set_space_permissions": "设置空间权限",
    "submit_search_feedback": "提交搜索反馈",
    "sync_data_source": "同步数据源",
    "test_data_source": "测试数据源",
    "toggle_search_feedback_adopted": "切换反馈采纳",
    "update_data_source": "更新数据源",
    "update_knowledge_info": "更新知识信息",
    "update_ontology_instance": "更新本体实例",
    "update_ontology_relation": "更新本体关系",
    "update_parser_setting": "更新解析器设置",
    "update_service": "更新服务",
    "update_service_configs": "更新服务配置",
    "update_space": "更新空间",
    "update_synonym": "更新同义词",
    "update_tag": "更新标签",
    "upload_document": "上传文档",
    # ---- 原子能力 ----
    "create_prompt": "创建提示词",
    "update_prompt": "更新提示词",
    "delete_prompt": "删除提示词",
    "get_prompt": "查看提示词",
    "retry": "重试",
    # ---- MCP Key ----
    "mcp_key.create": "创建 MCP Key",
    "mcp_key.revoke": "撤销 MCP Key",
    "mcp_key.enable": "启用 MCP Key",
    "mcp_key.disable": "停用 MCP Key",
    "mcp_key.recreate": "重新生成 MCP Key",
    "mcp_key.delete": "删除 MCP Key",
    "mcp_key.update": "更新 MCP Key",
    # ---- 租户模拟 ----
    "impersonate_start": "开始租户模拟",
    "impersonate_end": "结束租户模拟",
    # ---- 服务启停 ----
    "enable_service": "启用服务",
    "disable_service": "停用服务",
    # ---- LLM Wiki ----
    "apply_llm_wiki_schema": "应用 LLM Wiki 模式",
    "get_llm_wiki_schema": "查看 LLM Wiki 模式",
    "save_llm_wiki_schema": "保存 LLM Wiki 模式",
    "export_llm_wiki_schema_yaml": "导出 LLM Wiki 模式",
    "import_llm_wiki_schema_yaml": "导入 LLM Wiki 模式",
    "recompile_llm_wiki_schema_outdated_documents": "重编译 LLM Wiki 过期文档",
    "list_wiki_contents": "查看 Wiki 内容",
    "get_wiki_page": "查看 Wiki 页面",
    # ---- 检索 ----
    "deep_query": "深度查询",
    "search_mix": "混合搜索",
    "search_llmwiki": "LLM Wiki 搜索",
    # ---- 编译模式 ----
    "export_compiled_schema_yaml": "导出编译模式",
    "import_compiled_schema_yaml": "导入编译模式",
    "preview_compiled_schema": "预览编译模式",
    # ---- 本体模板 ----
    "export_template_ontology_yaml": "导出模板本体",
    "import_template_ontology_yaml": "导入模板本体",
    # ---- 答案反馈 ----
    "get_answer_feedback": "查看答案反馈",
    "get_answer_feedback_stats": "查看答案反馈统计",
    "list_answer_feedback": "答案反馈列表",
    "delete_answer_feedback": "删除答案反馈",
    "submit_answer_feedback": "提交答案反馈",
    "toggle_answer_feedback_adopted": "切换反馈采纳",
    # ---- 知识库权限 ----
    "get_kb_permissions": "查看知识库权限",
    "set_kb_permissions": "设置知识库权限",
    # ---- 文档状态 / 资产 / 配额 ----
    "get_document_status": "查看文档状态",
    "get_asset_raw_location": "查看资产原始位置",
    "get_quota_view": "查看配额",
    # ---- 分块维护 ----
    "scan_stale_chunks": "扫描过期分块",
    "purge_stale_chunks": "清除过期分块",
    # ---- 提示词模板 / 场景 ----
    "check_prompt_template_name": "校验提示词模板名称",
    "publish_template_scenario": "发布模板场景",
    "list_impacted_knowledge_bases": "受影响知识库列表",
}

_LABEL_EN: dict[str, str] = {
    # ---- 认证 ----
    "auth.login": "Login",
    "auth.exchange_ticket": "Exchange Ticket",
    "auth.logout": "Logout",
    # ---- 文档 ----
    "document.upload": "Upload Document",
    "document.parse": "Parse Document",
    "document.parse_done": "Document Parse Done",
    "document.parse_failed": "Document Parse Failed",
    "document.parse_recover": "Document Parse Recover",
    "document.reparse": "Reparse Document",
    "document.delete": "Delete Document",
    # ---- HTTP 方法 ----
    "http.get": "GET",
    "http.post": "POST",
    "http.put": "PUT",
    "http.delete": "DELETE",
    "http.patch": "PATCH",
    # ---- 提示词模板 ----
    "create_prompt_template": "Create Prompt Template",
    "update_prompt_template": "Update Prompt Template",
    "delete_prompt_template": "Delete Prompt Template",
    "copy_prompt_template": "Copy Prompt Template",
    "get_prompt_template": "View Prompt Template",
    "rollback_prompt_template": "Rollback Prompt Template",
    "list_prompt_templates": "List Prompt Templates",
    "list_prompt_template_versions": "List Prompt Template Versions",
    # ---- 适配器 ----
    "create_adapter": "Create Adapter",
    "update_adapter": "Update Adapter",
    "connect_adapter": "Connect Adapter",
    "disconnect_adapter": "Disconnect Adapter",
    "list_adapters": "List Adapters",
    # ---- 解析器 ----
    "create_parser": "Create Parser",
    "update_parser": "Update Parser",
    "list_parsers": "List Parsers",
    # ---- 数据接入 ----
    "create_access_method": "Create Access Method",
    "update_access_method": "Update Access Method",
    "list_access_methods": "List Access Methods",
    # ---- 模板约束/领域/对象/关系/场景 ----
    "create_template_constraint": "Create Template Constraint",
    "delete_template_constraint": "Delete Template Constraint",
    "update_template_constraint": "Update Template Constraint",
    "list_template_constraints": "List Template Constraints",
    "create_template_domain": "Create Template Domain",
    "delete_template_domain": "Delete Template Domain",
    "update_template_domain": "Update Template Domain",
    "get_template_domain": "View Template Domain",
    "list_template_domains": "List Template Domains",
    "create_template_object": "Create Template Object",
    "delete_template_object": "Delete Template Object",
    "update_template_object": "Update Template Object",
    "list_template_objects": "List Template Objects",
    "create_template_relation": "Create Template Relation",
    "delete_template_relation": "Delete Template Relation",
    "update_template_relation": "Update Template Relation",
    "list_template_relations": "List Template Relations",
    "create_template_scenario": "Create Template Scenario",
    "delete_template_scenario": "Delete Template Scenario",
    "update_template_scenario": "Update Template Scenario",
    "get_template_scenario": "View Template Scenario",
    "list_template_scenarios": "List Template Scenarios",
    # ---- 提供商/技能 ----
    "create_provider": "Create Provider",
    "update_provider": "Update Provider",
    "test_provider": "Test Provider",
    "list_providers": "List Providers",
    "enable_skill": "Enable Skill",
    "disable_skill": "Disable Skill",
    "get_skill": "View Skill",
    "list_skills": "List Skills",
    "list_enabled_mcp_tools": "List Enabled MCP Tools",

    # ---- 知识库 ----
    "add_document_tag": "Add Document Tag",
    "cancel_search_feedback": "Cancel Search Feedback",
    "clear_search_history": "Clear Search History",
    "create_data_source": "Create Data Source",
    "create_folder": "Create Folder",
    "create_knowledge_info": "Create Knowledge Info",
    "create_ontology_instance": "Create Ontology Instance",
    "create_ontology_relation": "Create Ontology Relation",
    "create_parser_setting": "Create Parser Setting",
    "create_service": "Create Service",
    "create_service_api_key": "Create Service API Key",
    "create_space": "Create Space",
    "create_synonym": "Create Synonym",
    "create_tag": "Create Tag",
    "delete_data_source": "Delete Data Source",
    "delete_document": "Delete Document",
    "delete_folder": "Delete Folder",
    "delete_knowledge_info": "Delete Knowledge Info",
    "delete_ontology_instance": "Delete Ontology Instance",
    "delete_ontology_relation": "Delete Ontology Relation",
    "delete_parser_setting": "Delete Parser Setting",
    "delete_search_history": "Delete Search History",
    "delete_service": "Delete Service",
    "delete_service_api_key": "Delete Service API Key",
    "delete_space": "Delete Space",
    "delete_synonym": "Delete Synonym",
    "delete_tag": "Delete Tag",
    "documents_stats": "Documents Stats",
    "expand_ontology_neighbors": "Expand Ontology Neighbors",
    "generate_upload_url": "Generate Upload URL",
    "get_chunk": "View Chunk",
    "get_compiled_schema": "View Compiled Schema",
    "get_data_source": "View Data Source",
    "get_document": "View Document",
    "get_document_chunks": "View Document Chunks",
    "get_document_parse_result": "View Document Parse Result",
    "get_document_tags": "View Document Tags",
    "get_editor_state": "View Editor State",
    "get_knowledge_info": "View Knowledge Info",
    "get_ontology_graph": "View Ontology Graph",
    "get_ontology_statistics": "View Ontology Statistics",
    "get_parse_result_documents": "View Parse Result Documents",
    "get_parse_result_entities": "View Parse Result Entities",
    "get_parse_result_graph": "View Parse Result Graph",
    "get_parse_result_graph_summary": "View Parse Result Graph Summary",
    "get_parse_result_relationships": "View Parse Result Relationships",
    "get_parse_result_summary": "View Parse Result Summary",
    "get_raw_content": "View Raw Content",
    "get_raw_location": "View Raw Location",
    "get_raw_url": "View Raw URL",
    "get_search_feedback_stats": "View Search Feedback Stats",
    "get_search_overview": "View Search Overview",
    "get_service": "View Service",
    "get_service_configs": "View Service Configs",
    "get_space": "View Space",
    "get_space_permissions": "View Space Permissions",
    "import_synonyms": "Import Synonyms",
    "ingest_push": "Ingest Push",
    "list_data_sources": "List Data Sources",
    "list_documents": "List Documents",
    "list_folders": "List Folders",
    "list_knowledge_info": "List Knowledge Info",
    "list_ontology_entity_types": "List Ontology Entity Types",
    "list_ontology_instances": "List Ontology Instances",
    "list_ontology_relation_types": "List Ontology Relation Types",
    "list_ontology_relations": "List Ontology Relations",
    "list_parser_settings": "List Parser Settings",
    "list_search_feedback": "List Search Feedback",
    "list_search_history": "List Search History",
    "list_service_api_keys": "List Service API Keys",
    "list_services": "List Services",
    "list_spaces": "List Spaces",
    "list_synonyms": "List Synonyms",
    "list_tags": "List Tags",
    "query_with_ontology": "Query with Ontology",
    "recompile_schema": "Recompile Schema",
    "reconcile_documents": "Reconcile Documents",
    "reconcile_ontology": "Reconcile Ontology",
    "reextract_kb_documents": "Re-extract KB Documents",
    "remove_document_tag": "Remove Document Tag",
    "rename_folder": "Rename Folder",
    "reparse_document": "Reparse Document",
    "reseed_compiled_schema": "Reseed Compiled Schema",
    "reset_ingest_key": "Reset Ingest Key",
    "resolve_references": "Resolve References",
    "retry_ontology_extract": "Retry Ontology Extract",
    "rotate_service_api_key": "Rotate Service API Key",
    "save_compiled_schema": "Save Compiled Schema",
    "save_search_history": "Save Search History",
    "search": "Search",
    "search_enhanced": "Search Enhanced",
    "search_ontology_entities": "Search Ontology Entities",
    "search_service": "Search Service",
    "batch_set_document_folder": "Move Documents",
    "set_document_folder": "Set Document Folder",
    "set_document_tags": "Set Document Tags",
    "set_space_permissions": "Set Space Permissions",
    "submit_search_feedback": "Submit Search Feedback",
    "sync_data_source": "Sync Data Source",
    "test_data_source": "Test Data Source",
    "toggle_search_feedback_adopted": "Toggle Search Feedback Adopted",
    "update_data_source": "Update Data Source",
    "update_knowledge_info": "Update Knowledge Info",
    "update_ontology_instance": "Update Ontology Instance",
    "update_ontology_relation": "Update Ontology Relation",
    "update_parser_setting": "Update Parser Setting",
    "update_service": "Update Service",
    "update_service_configs": "Update Service Configs",
    "update_space": "Update Space",
    "update_synonym": "Update Synonym",
    "update_tag": "Update Tag",
    "upload_document": "Upload Document",
    # ---- 原子能力 ----
    "create_prompt": "Create Prompt",
    "update_prompt": "Update Prompt",
    "delete_prompt": "Delete Prompt",
    "get_prompt": "View Prompt",
    "retry": "Retry",
    # ---- MCP Key ----
    "mcp_key.create": "Create MCP Key",
    "mcp_key.revoke": "Revoke MCP Key",
    "mcp_key.enable": "Enable MCP Key",
    "mcp_key.disable": "Disable MCP Key",
    "mcp_key.recreate": "Recreate MCP Key",
    "mcp_key.delete": "Delete MCP Key",
    "mcp_key.update": "Update MCP Key",
    # ---- 租户模拟 ----
    "impersonate_start": "Start Tenant Impersonation",
    "impersonate_end": "End Tenant Impersonation",
    # ---- 服务启停 ----
    "enable_service": "Enable Service",
    "disable_service": "Disable Service",
    # ---- LLM Wiki ----
    "apply_llm_wiki_schema": "Apply LLM Wiki Schema",
    "get_llm_wiki_schema": "View LLM Wiki Schema",
    "save_llm_wiki_schema": "Save LLM Wiki Schema",
    "export_llm_wiki_schema_yaml": "Export LLM Wiki Schema",
    "import_llm_wiki_schema_yaml": "Import LLM Wiki Schema",
    "recompile_llm_wiki_schema_outdated_documents": "Recompile LLM Wiki Outdated Documents",
    "list_wiki_contents": "List Wiki Contents",
    "get_wiki_page": "View Wiki Page",
    # ---- 检索 ----
    "deep_query": "Deep Query",
    "search_mix": "Mixed Search",
    "search_llmwiki": "LLM Wiki Search",
    # ---- 编译模式 ----
    "export_compiled_schema_yaml": "Export Compiled Schema",
    "import_compiled_schema_yaml": "Import Compiled Schema",
    "preview_compiled_schema": "Preview Compiled Schema",
    # ---- 本体模板 ----
    "export_template_ontology_yaml": "Export Template Ontology",
    "import_template_ontology_yaml": "Import Template Ontology",
    # ---- 答案反馈 ----
    "get_answer_feedback": "View Answer Feedback",
    "get_answer_feedback_stats": "View Answer Feedback Stats",
    "list_answer_feedback": "List Answer Feedback",
    "delete_answer_feedback": "Delete Answer Feedback",
    "submit_answer_feedback": "Submit Answer Feedback",
    "toggle_answer_feedback_adopted": "Toggle Answer Feedback Adopted",
    # ---- 知识库权限 ----
    "get_kb_permissions": "View KB Permissions",
    "set_kb_permissions": "Set KB Permissions",
    # ---- 文档状态 / 资产 / 配额 ----
    "get_document_status": "View Document Status",
    "get_asset_raw_location": "View Asset Raw Location",
    "get_quota_view": "View Quota",
    # ---- 分块维护 ----
    "scan_stale_chunks": "Scan Stale Chunks",
    "purge_stale_chunks": "Purge Stale Chunks",
    # ---- 提示词模板 / 场景 ----
    "check_prompt_template_name": "Check Prompt Template Name",
    "publish_template_scenario": "Publish Template Scenario",
    "list_impacted_knowledge_bases": "List Impacted Knowledge Bases",
}



# ============ AuditAction 枚举 ============

class AuditAction(str, Enum):
    """审计动作枚举，继承 str 使成员可直接当字符串使用。

    使用:
        AuditAction.AUTH_LOGIN               # == "auth.login"
        AuditAction.AUTH_LOGIN.label_zh      # "登录"
        AuditAction.AUTH_LOGIN.label_en      # "Login"
        str(AuditAction.AUTH_LOGIN)          # "auth.login"
        "auth.login" == AuditAction.AUTH_LOGIN  # True (str 比较)
    """

    # ---- 认证 ----
    AUTH_LOGIN = "auth.login"
    AUTH_EXCHANGE_TICKET = "auth.exchange_ticket"
    AUTH_LOGOUT = "auth.logout"

    # ---- 文档 ----
    DOCUMENT_UPLOAD = "document.upload"
    DOCUMENT_PARSE = "document.parse"
    DOCUMENT_PARSE_DONE = "document.parse_done"
    DOCUMENT_PARSE_FAILED = "document.parse_failed"
    DOCUMENT_PARSE_RECOVER = "document.parse_recover"
    DOCUMENT_REPARSE = "document.reparse"
    DOCUMENT_DELETE = "document.delete"

    @property
    def label_zh(self) -> str:
        """中文标签，未知值返回原值"""
        return _LABEL_ZH.get(self.value, self.value)

    @property
    def label_en(self) -> str:
        """英文标签，未知值返回原值"""
        return _LABEL_EN.get(self.value, self.value)

    @classmethod
    def all_values(cls) -> list[str]:
        """返回所有已定义的 action 值（不含 HTTP 模板）。"""
        return [m.value for m in cls]

    @classmethod
    def get_labels(cls, action: str) -> dict[str, str]:
        """返回指定 action 的中英文标签。

        用于 /audit-logs/actions API 将原始 action 值映射为结构化标签。
        未知值返回原值作为 label_zh 和 label_en。
        """
        return {
            "label_zh": _LABEL_ZH.get(action, action),
            "label_en": _LABEL_EN.get(action, action),
        }
