-- ============================================================
-- 悦溪平台数据库初始化 - 表注释
-- 版本: 007
-- ============================================================

COMMENT ON SCHEMA platform IS '平台核心数据';
COMMENT ON SCHEMA knowledge_base IS '知识库业务层数据（CRUD + 状态机 + 本体绑定/编译 + 数据源，与 LightRAG 内部存储分离）';
COMMENT ON SCHEMA business_domain IS '业务领域 + 生态管理数据（领域空间/服务/引擎/适配器/技能/模板）';
COMMENT ON SCHEMA metering IS '计量 schema：存储 LLM/Embedding 出口计量数据';

-- metering
COMMENT ON TABLE metering.llm_usage_log IS 'LLM 网关调用日志：记录每次 LLM/Embedding 调用的 token 用量';
COMMENT ON COLUMN metering.llm_usage_log.request_id IS '计量幂等键：每次逻辑 LLM 调用唯一、重试稳定；调用方透传 X-Jonex-Request-Id 或网关按 trace_id + 请求体哈希派生';
COMMENT ON COLUMN metering.llm_usage_log.trace_id IS '链路追踪 ID：一次用户业务请求一个，用于把该请求下的多次 LLM 调用归组统计（来源 X-Jonex-Trace-Id）';
COMMENT ON COLUMN metering.llm_usage_log.scene IS '调用场景：ontology_extract/ontology_qa/lightrag_extract/lightrag_query/lightrag_embed/lightrag_embed_query 等';
COMMENT ON COLUMN metering.llm_usage_log.is_estimated IS 'usage 是否为估算（true=上游未返回 usage，走兜底估算）';
COMMENT ON COLUMN metering.llm_usage_log.latency_ms IS '上游响应延迟（毫秒）';

-- platform
COMMENT ON TABLE platform.tenants IS '租户信息表';
COMMENT ON TABLE platform.api_keys IS 'API Key 管理表';
COMMENT ON TABLE platform.users IS '用户表';
COMMENT ON TABLE platform.login_tickets IS '一次性登录票据表（跨域 ticket/code 交换，只存 hash 不存明文）';
COMMENT ON TABLE platform.roles IS 'RBAC 角色表';
COMMENT ON TABLE platform.permissions IS 'RBAC 权限表';
COMMENT ON TABLE platform.role_permissions IS '角色-权限关联表';
COMMENT ON TABLE platform.user_roles IS '用户-角色关联表';
COMMENT ON TABLE platform.menus IS '菜单表';
COMMENT ON TABLE platform.applications IS '应用注册表';
COMMENT ON TABLE platform.application_routes IS '应用路由表';
COMMENT ON TABLE platform.system_configs IS '系统配置表';
COMMENT ON TABLE platform.audit_logs IS '审计日志表';
COMMENT ON TABLE platform.task_schedules IS '任务调度表';

-- knowledge_base
COMMENT ON TABLE knowledge_base.knowledge_documents IS '知识库文档元数据表（业务层独有，含状态机和 RAG 对账字段）';
COMMENT ON TABLE knowledge_base.knowledge_search_history IS '知识检索历史表（按 tenant+user+query+knowledge_base 去重，软删除）';
COMMENT ON COLUMN knowledge_base.knowledge_documents.id IS '知识文档 ID';
COMMENT ON COLUMN knowledge_base.knowledge_documents.tenant_id IS '租户 ID，所有普通请求必须按租户隔离';
COMMENT ON COLUMN knowledge_base.knowledge_documents.file_name IS '原始文件名';
COMMENT ON COLUMN knowledge_base.knowledge_documents.file_path IS '上传文件在共享输入目录中的路径';
COMMENT ON COLUMN knowledge_base.knowledge_documents.file_size IS '文件大小，单位字节';
COMMENT ON COLUMN knowledge_base.knowledge_documents.mime_type IS '上传文件 MIME 类型';
COMMENT ON COLUMN knowledge_base.knowledge_documents.knowledge_base_id IS '知识库业务 ID，用于同租户下划分知识库；普通文档请求必须明确指定';
COMMENT ON COLUMN knowledge_base.knowledge_documents.status IS '文档解析状态：pending/parsing/ready/failed/deleting/deleted';
COMMENT ON COLUMN knowledge_base.knowledge_documents.rag_task_id IS 'RAG 原子能力解析任务 ID';
COMMENT ON COLUMN knowledge_base.knowledge_documents.rag_doc_ids IS 'RAG 入库后返回的文档 ID 列表';
COMMENT ON COLUMN knowledge_base.knowledge_documents.error_message IS '文档解析失败原因';
COMMENT ON COLUMN knowledge_base.knowledge_documents.ontology_status IS '本体抽取状态：pending/extracting/ready/failed';
COMMENT ON COLUMN knowledge_base.knowledge_documents.ontology_error IS '本体抽取失败原因';
COMMENT ON COLUMN knowledge_base.knowledge_documents.ontology_retry_count IS '本体抽取重试次数';
COMMENT ON COLUMN knowledge_base.knowledge_documents.extra_metadata IS '业务扩展元数据，不承载租户或知识库隔离字段';
COMMENT ON COLUMN knowledge_base.knowledge_documents.storage_backend IS '对象存储后端：local / cos';
COMMENT ON COLUMN knowledge_base.knowledge_documents.storage_key IS '对象存储 key（local 后端等同 file_path）';
COMMENT ON COLUMN knowledge_base.knowledge_documents.is_deleted IS '软删除标记：0 正常，1 删除';
COMMENT ON COLUMN knowledge_base.knowledge_documents.llm_wiki_compile_status IS 'LLM-Wiki 编译状态：NULL/compiling/compiled/stale/failed';
COMMENT ON COLUMN knowledge_base.knowledge_documents.llm_wiki_compile_error IS 'LLM-Wiki 编译失败原因';
COMMENT ON COLUMN knowledge_base.knowledge_documents.llm_wiki_compile_warnings IS 'LLM-Wiki 编译警告列表（JSONB 数组）';
COMMENT ON COLUMN knowledge_base.knowledge_documents.llm_wiki_compile_requested_at IS 'LLM-Wiki 编译请求时刻（patrol 超时基准，UTC）';
COMMENT ON COLUMN knowledge_base.knowledge_documents.llm_wiki_compiled_at IS 'LLM-Wiki 编译完成时刻';
COMMENT ON COLUMN knowledge_base.knowledge_documents.llm_wiki_task_id IS 'OpenKB 容器编译任务 ID（对齐 rag_task_id），供对账巡检轮询';
COMMENT ON COLUMN knowledge_base.knowledge_search_history.id IS '检索历史 ID';
COMMENT ON COLUMN knowledge_base.knowledge_search_history.tenant_id IS '租户 ID，所有普通请求必须按租户隔离';
COMMENT ON COLUMN knowledge_base.knowledge_search_history.user_id IS '发起检索的用户 ID';
COMMENT ON COLUMN knowledge_base.knowledge_search_history.query IS '原始检索问题';
COMMENT ON COLUMN knowledge_base.knowledge_search_history.query_hash IS '规范化 query 的 SHA-256，用于去重';
COMMENT ON COLUMN knowledge_base.knowledge_search_history.knowledge_base_id IS '检索限定的知识库业务 ID，检索历史必须归属明确知识库';
COMMENT ON COLUMN knowledge_base.knowledge_search_history.mode IS '检索模式，例如 hybrid/vector/keyword/graph';
COMMENT ON COLUMN knowledge_base.knowledge_search_history.top_k IS '检索召回数量';
COMMENT ON COLUMN knowledge_base.knowledge_search_history.status IS '检索状态';
COMMENT ON COLUMN knowledge_base.knowledge_search_history.answer_preview IS '答案预览，不存完整大文本';
COMMENT ON COLUMN knowledge_base.knowledge_search_history.reference_count IS '引用来源数量';
COMMENT ON COLUMN knowledge_base.knowledge_search_history.result_count IS '检索结果数量';
COMMENT ON COLUMN knowledge_base.knowledge_search_history.duration_ms IS '检索耗时，单位毫秒';
COMMENT ON COLUMN knowledge_base.knowledge_search_history.extra_metadata IS '检索扩展元数据，不承载租户或知识库隔离字段';
COMMENT ON COLUMN knowledge_base.knowledge_search_history.searched_at IS '检索发生时间';
COMMENT ON COLUMN knowledge_base.knowledge_search_history.is_deleted IS '软删除标记：0 正常，1 删除';
COMMENT ON TABLE knowledge_base.spaces IS '领域空间表';
COMMENT ON TABLE knowledge_base.space_permissions IS '领域空间权限表';
COMMENT ON TABLE knowledge_base.services IS '领域服务表';
COMMENT ON TABLE knowledge_base.service_knowledge_bases IS '领域服务-知识库关联表';
COMMENT ON TABLE knowledge_base.service_configs IS '领域服务配置表';
COMMENT ON TABLE knowledge_base.service_permissions IS '领域服务权限表';
COMMENT ON TABLE knowledge_base.service_api_keys IS '领域服务 API Key 管理表';

-- knowledge_base: 数据源 + 本体模板绑定/编译
COMMENT ON TABLE knowledge_base.knowledge_data_sources IS 'KB 数据源实例表（access_type: api 出站拉取 / api_push 入站推送 / storage 外部对象存储 / file 上传）';

COMMENT ON TABLE knowledge_base.ontology_template_bindings IS '知识库-模板绑定表，记录每个 KB 绑定的模板领域和场景';
COMMENT ON COLUMN knowledge_base.ontology_template_bindings.tenant_id IS '租户 ID';
COMMENT ON COLUMN knowledge_base.ontology_template_bindings.knowledge_base_id IS '知识库业务 ID';
COMMENT ON COLUMN knowledge_base.ontology_template_bindings.template_domain_id IS '绑定的模板领域 ID（可选）';
COMMENT ON COLUMN knowledge_base.ontology_template_bindings.template_scenario_id IS '绑定的模板场景 ID（可选）';
COMMENT ON COLUMN knowledge_base.ontology_template_bindings.source_type IS '模板来源类型：business_template / yaml_default';
COMMENT ON COLUMN knowledge_base.ontology_template_bindings.status IS '绑定状态：active / deprecated';

COMMENT ON TABLE knowledge_base.ontology_compiled_schemas IS '知识库级本体编译快照，缓存从业务模板编译出的 ontology schema';
COMMENT ON COLUMN knowledge_base.ontology_compiled_schemas.tenant_id IS '租户 ID';
COMMENT ON COLUMN knowledge_base.ontology_compiled_schemas.knowledge_base_id IS '知识库业务 ID';
COMMENT ON COLUMN knowledge_base.ontology_compiled_schemas.template_domain_id IS '来源模板领域 ID';
COMMENT ON COLUMN knowledge_base.ontology_compiled_schemas.template_scenario_id IS '来源模板场景 ID';
COMMENT ON COLUMN knowledge_base.ontology_compiled_schemas.source_type IS '来源类型：business_template / yaml_default';
COMMENT ON COLUMN knowledge_base.ontology_compiled_schemas.source_version IS '来源模板版本号';
COMMENT ON COLUMN knowledge_base.ontology_compiled_schemas.source_hash IS '来源模板结构哈希，用于过期判断';
COMMENT ON COLUMN knowledge_base.ontology_compiled_schemas.schema_version IS '编译版本号';
COMMENT ON COLUMN knowledge_base.ontology_compiled_schemas.entity_types IS '编译后的实体类型定义列表（JSON）';
COMMENT ON COLUMN knowledge_base.ontology_compiled_schemas.relation_types IS '编译后的关系类型定义列表（JSON）';
COMMENT ON COLUMN knowledge_base.ontology_compiled_schemas.constraints IS '约束定义列表（JSON）';
COMMENT ON COLUMN knowledge_base.ontology_compiled_schemas.disambiguation IS '消歧配置（JSON）';
COMMENT ON COLUMN knowledge_base.ontology_compiled_schemas.prompt_schema IS 'LLM prompt 拼接用的完整 schema（JSON）';
COMMENT ON COLUMN knowledge_base.ontology_compiled_schemas.schema_mode IS 'schema 来源模式：template_seeded / manual_edited';
COMMENT ON COLUMN knowledge_base.ontology_compiled_schemas.sync_status IS '与模板的同步状态：synced / outdated';
COMMENT ON COLUMN knowledge_base.ontology_compiled_schemas.edited_at IS '最近一次知识库侧人工编辑时间';
COMMENT ON COLUMN knowledge_base.ontology_compiled_schemas.edited_by IS '最近一次知识库侧人工编辑人';
COMMENT ON COLUMN knowledge_base.ontology_compiled_schemas.status IS '编译快照状态：active / deprecated / schema_outdated';
COMMENT ON COLUMN knowledge_base.ontology_compiled_schemas.compiled_at IS '编译完成时间';

COMMENT ON TABLE knowledge_base.ontology_synonyms IS '知识库级同义词组，等价词统一映射为标准实体（本期仅存储管理）';
COMMENT ON COLUMN knowledge_base.ontology_synonyms.tenant_id IS '租户 ID';
COMMENT ON COLUMN knowledge_base.ontology_synonyms.knowledge_base_id IS '知识库业务 ID';
COMMENT ON COLUMN knowledge_base.ontology_synonyms.terms IS '同义词列表 string[]（归一化去重后至少 2 个）';
COMMENT ON COLUMN knowledge_base.ontology_synonyms.canonical IS '标准词，必须属于 terms，缺省取 terms[0]';
COMMENT ON COLUMN knowledge_base.ontology_synonyms.is_deleted IS '软删除标记：0-正常，1-删除';

-- business_domain
COMMENT ON TABLE business_domain.data_access_methods IS '数据接入方式配置表';
COMMENT ON TABLE business_domain.parser_configs IS '解析器配置表';
COMMENT ON TABLE business_domain.model_providers IS '模型提供商配置表';
COMMENT ON TABLE business_domain.adapters IS '生态适配器配置表';
COMMENT ON TABLE business_domain.skill_catalog IS '平台共享 AI Skill 目录表（无租户，系统预置）';
COMMENT ON TABLE business_domain.tenant_skills IS '租户技能启用状态表（有租户，记录租户对 Skill 的启用/停用）';

COMMENT ON TABLE business_domain.template_domains IS '模板领域定义（含版本和发布信息）';
COMMENT ON TABLE business_domain.template_scenarios IS '模板场景定义（含版本和发布信息）';
COMMENT ON COLUMN business_domain.template_objects.ontology_code IS '稳定的 ontology 实体编码，用于 compiled schema 和 Neo4j 标签';
COMMENT ON COLUMN business_domain.template_objects.aliases IS '实体别名列表，用于 LLM 抽取时的名称匹配';
COMMENT ON COLUMN business_domain.template_attributes.ontology_code IS '稳定的 ontology 属性编码';
COMMENT ON COLUMN business_domain.template_relations.ontology_code IS '稳定的 ontology 关系编码';
COMMENT ON COLUMN business_domain.template_relations.aliases IS '关系别名列表，用于 LLM 抽取时的名称匹配';

-- ============================================================
-- 补齐：knowledge_base 新增表/字段
-- ============================================================

COMMENT ON TABLE knowledge_base.knowledge_info IS '知识库基本信息表（业务域管理用的 KB 主表，含名称/空间/状态/文档数）';
COMMENT ON COLUMN knowledge_base.knowledge_info.id IS '知识库唯一 ID';
COMMENT ON COLUMN knowledge_base.knowledge_info.tenant_id IS '租户 ID';
COMMENT ON COLUMN knowledge_base.knowledge_info.space_id IS '所属领域空间 ID';
COMMENT ON COLUMN knowledge_base.knowledge_info.name IS '知识库名称';
COMMENT ON COLUMN knowledge_base.knowledge_info.description IS '知识库描述';
COMMENT ON COLUMN knowledge_base.knowledge_info.data_source_types IS '已启用数据源类型列表（JSON 数组）';
COMMENT ON COLUMN knowledge_base.knowledge_info.document_count IS '文档总数（冗余聚合）';
COMMENT ON COLUMN knowledge_base.knowledge_info.status IS '知识库状态：synced/syncing/failed/disabled';
COMMENT ON COLUMN knowledge_base.knowledge_info.owner_id IS '负责人用户 ID';

COMMENT ON COLUMN knowledge_base.knowledge_documents.data_source_type IS '文档来源类型：api/api_push/storage/file';

-- ============================================================
-- 补齐：business_domain 模板表注释
-- ============================================================

COMMENT ON TABLE business_domain.template_objects IS '模板对象表（场景下的实体对象定义，含属性和本体编码）';
COMMENT ON TABLE business_domain.template_attributes IS '模板属性表（对象下的属性字段定义）';
COMMENT ON TABLE business_domain.template_relations IS '模板关系表（场景下对象间的关系定义）';

COMMENT ON TABLE business_domain.prompt_templates IS '提示词模板表（system scope 为系统全局只读种子，domain scope 为租户级 CRUD）';
COMMENT ON COLUMN business_domain.prompt_templates.id IS '模板 ID（system 类用 seed_pt_sys_* 前缀，domain 类用 uuid）';
COMMENT ON COLUMN business_domain.prompt_templates.tenant_id IS '租户 ID（NULL=系统全局模板，非NULL=领域租户模板）';
COMMENT ON COLUMN business_domain.prompt_templates.name IS '模板名称';
COMMENT ON COLUMN business_domain.prompt_templates.category IS '分类：通用问答/文档处理/金融分析/合同审查/数据分析/其他';
COMMENT ON COLUMN business_domain.prompt_templates.scope IS '作用域：system（系统全局，只读）/ domain（领域模板，可 CRUD）';
COMMENT ON COLUMN business_domain.prompt_templates.description IS '模板描述说明';
COMMENT ON COLUMN business_domain.prompt_templates.status IS '模板状态：启用/停用';
COMMENT ON COLUMN business_domain.prompt_templates.current_version IS '当前版本号';
COMMENT ON COLUMN business_domain.prompt_templates.versions_json IS '版本历史（[{version, content, updated_by, updated_at, remark}]）';
COMMENT ON COLUMN business_domain.prompt_templates.created_by IS '创建人';

COMMENT ON TABLE business_domain.template_constraints IS '本体约束模板表（scenario 维度，指向 template_objects / template_attributes / template_relations）';
COMMENT ON COLUMN business_domain.template_constraints.id IS '约束唯一 ID';
COMMENT ON COLUMN business_domain.template_constraints.tenant_id IS '租户 ID';
COMMENT ON COLUMN business_domain.template_constraints.domain_id IS '所属模板领域 ID';
COMMENT ON COLUMN business_domain.template_constraints.scenario_id IS '所属模板场景 ID';
COMMENT ON COLUMN business_domain.template_constraints.name IS '约束名称';
COMMENT ON COLUMN business_domain.template_constraints.target_type IS '约束目标类型：object/attribute/relation';
COMMENT ON COLUMN business_domain.template_constraints.target_id IS '约束目标 ID（指向 template_objects.id / template_attributes.id / template_relations.id）';
COMMENT ON COLUMN business_domain.template_constraints.target_label IS '目标展示名称快照（由后端写入，目标改名时同步刷新）';
COMMENT ON COLUMN business_domain.template_constraints.constraint_type IS '约束类型：unique/exists/conditional/range';
COMMENT ON COLUMN business_domain.template_constraints.expression IS '约束表达式（conditional/range 时必填）';
COMMENT ON COLUMN business_domain.template_constraints.suggestion IS '违反约束时的修正建议';


-- ============================================================
-- LLM 计量可读性视图（只读，不改 metering.llm_usage_log 表/写入）
-- 归属说明：视图属 metering 计量特性，但因 v_llm_usage_detail / v_llm_usage_by_doc
--   需 JOIN 本文件（004）的 knowledge_base.knowledge_info / knowledge_documents，
--   而 CREATE VIEW 在创建时即校验被引用表存在（Docker 初始化 ON_ERROR_STOP=on），
--   故必须放在 metering 表（002）与本文件 KB 表**都已建好**之后 —— 即本文件末尾，
--   不能放 002_platform.sql（002 早于 004，KB 表尚不存在会导致初始化失败）。
-- 设计见 docs/llm-usage-log-optimization-plan.md §4.1。
-- ============================================================

-- (1) 明细可读视图：关联 kb/doc 名称 + 本地时区，供逐行排障
CREATE OR REPLACE VIEW metering.v_llm_usage_detail AS
SELECT
    u.id,
    (u.created_at AT TIME ZONE 'Asia/Shanghai')          AS created_local,
    u.tenant_id,
    u.scene,
    u.model,
    ki.name                                              AS kb_name,
    kd.file_name                                         AS doc_name,
    u.prompt_tokens, u.completion_tokens, u.total_tokens,
    u.latency_ms, u.is_stream, u.is_estimated,
    u.trace_id, u.request_id, u.kb_id, u.doc_id
FROM metering.llm_usage_log u
LEFT JOIN knowledge_base.knowledge_info      ki ON ki.id = u.kb_id
LEFT JOIN knowledge_base.knowledge_documents kd ON kd.id = u.doc_id;

-- (2) 按文档汇总：一篇文档的总消耗（最常用）
CREATE OR REPLACE VIEW metering.v_llm_usage_by_doc AS
SELECT
    u.tenant_id,
    ki.name                                              AS kb_name,
    kd.file_name                                         AS doc_name,
    u.scene, u.model,
    SUM(u.call_count)                                    AS call_count,
    SUM(u.prompt_tokens)                                 AS prompt_tokens,
    SUM(u.completion_tokens)                             AS completion_tokens,
    SUM(u.total_tokens)                                  AS total_tokens,
    ROUND(AVG(u.latency_ms))                             AS avg_latency_ms,
    MIN(u.created_at AT TIME ZONE 'Asia/Shanghai')       AS first_local,
    MAX(u.created_at AT TIME ZONE 'Asia/Shanghai')       AS last_local
FROM metering.llm_usage_log u
LEFT JOIN knowledge_base.knowledge_info      ki ON ki.id = u.kb_id
LEFT JOIN knowledge_base.knowledge_documents kd ON kd.id = u.doc_id
GROUP BY u.tenant_id, ki.name, kd.file_name, u.scene, u.model;

-- (3) 按链路 trace 汇总：一次业务请求（如一次问答/一篇入库）的总消耗
CREATE OR REPLACE VIEW metering.v_llm_usage_by_trace AS
SELECT
    u.tenant_id, u.trace_id, u.scene, u.model,
    SUM(u.call_count)                                    AS call_count,
    SUM(u.total_tokens)                                  AS total_tokens,
    ROUND(AVG(u.latency_ms))                             AS avg_latency_ms,
    MIN(u.created_at AT TIME ZONE 'Asia/Shanghai')       AS first_local,
    MAX(u.created_at AT TIME ZONE 'Asia/Shanghai')       AS last_local
FROM metering.llm_usage_log u
GROUP BY u.tenant_id, u.trace_id, u.scene, u.model;

-- (4) 按天汇总：租户/场景/模型的每日趋势
CREATE OR REPLACE VIEW metering.v_llm_usage_daily AS
SELECT
    (u.created_at AT TIME ZONE 'Asia/Shanghai')::date    AS day_local,
    u.tenant_id, u.scene, u.model,
    SUM(u.call_count)                                    AS call_count,
    SUM(u.prompt_tokens)                                 AS prompt_tokens,
    SUM(u.completion_tokens)                             AS completion_tokens,
    SUM(u.total_tokens)                                  AS total_tokens,
    ROUND(AVG(u.latency_ms))                             AS avg_latency_ms
FROM metering.llm_usage_log u
GROUP BY 1, u.tenant_id, u.scene, u.model;

COMMENT ON VIEW metering.v_llm_usage_detail   IS 'LLM 计量明细可读视图（关联 kb/doc 名称 + 本地时区）';
COMMENT ON VIEW metering.v_llm_usage_by_doc   IS 'LLM 计量按文档汇总（call_count/tokens/avg_latency）';
COMMENT ON VIEW metering.v_llm_usage_by_trace IS 'LLM 计量按链路 trace 汇总';
COMMENT ON VIEW metering.v_llm_usage_daily    IS 'LLM 计量按天汇总（趋势/对账）';
-- LLM-Wiki Schema 编译设置（kb_type=openkb 的编译配置权威源，方案 docs/llmwiki-schema-settings-execution-plan.md）
COMMENT ON TABLE knowledge_base.llm_wiki_schemas IS 'LLM-Wiki Schema 编译设置表（留档模型：active/archived，每 KB 单 active；Jonex DB 权威源，OpenKB 文件系统为运行时投影）';
COMMENT ON COLUMN knowledge_base.llm_wiki_schemas.tenant_id IS '租户 ID';
COMMENT ON COLUMN knowledge_base.llm_wiki_schemas.knowledge_base_id IS '知识库业务 ID（kb_type=openkb）';
COMMENT ON COLUMN knowledge_base.llm_wiki_schemas.schema_version IS 'Schema 业务版本：每次保存递增，留档历史版本';
COMMENT ON COLUMN knowledge_base.llm_wiki_schemas.status IS '留档状态：active（当前生效）/ archived（历史留档）';
COMMENT ON COLUMN knowledge_base.llm_wiki_schemas.sync_status IS 'apply_schema 同步状态：synced / apply_failed（与 status 的留档语义分开）';
COMMENT ON COLUMN knowledge_base.llm_wiki_schemas.schema_name IS 'Schema 名称（默认 default）';
COMMENT ON COLUMN knowledge_base.llm_wiki_schemas.language IS 'KB 语言（默认 zh-CN，投影进 OpenKB config.yaml）';
COMMENT ON COLUMN knowledge_base.llm_wiki_schemas.model IS 'LLM 模型（可选；None = 走 OpenKB 默认/全局继承）';
COMMENT ON COLUMN knowledge_base.llm_wiki_schemas.entity_types IS '实体类型受控词表（JSONB：code/name/description/examples；编译时超出词表的 type 回落 other）';
COMMENT ON COLUMN knowledge_base.llm_wiki_schemas.concept_types IS '概念类型词表（JSONB：code/name/description；无硬校验，仅渲染进 AGENTS.md 作分类引导）';
COMMENT ON COLUMN knowledge_base.llm_wiki_schemas.agents_md_extra IS '用户自定义追加块（多行 markdown，渲染时原样拼到 AGENTS.md 末尾）';
COMMENT ON COLUMN knowledge_base.llm_wiki_schemas.agents_md IS '最终渲染的 AGENTS.md 全文（内置段 + 词表段 + 自定义块，确定性渲染）';
COMMENT ON COLUMN knowledge_base.llm_wiki_schemas.config_snapshot IS '投影给 OpenKB 的完整配置快照（排障/回放用）';
COMMENT ON COLUMN knowledge_base.llm_wiki_schemas.edited_by IS '最近编辑人';
COMMENT ON COLUMN knowledge_base.llm_wiki_schemas.edited_at IS '最近编辑时间';
COMMENT ON COLUMN knowledge_base.llm_wiki_schemas.applied_at IS '最近一次 apply_schema 成功投影时间';
