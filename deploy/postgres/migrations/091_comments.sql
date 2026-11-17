-- ============================================================================
-- 全部 COMMENT ON（表/列注释）
-- ============================================================================

COMMENT ON TABLE business_domain.adapters IS '生态适配器配置表';

COMMENT ON TABLE business_domain.data_access_methods IS '数据接入方式配置表';

COMMENT ON TABLE business_domain.model_providers IS '模型提供商配置表';

COMMENT ON TABLE business_domain.parser_configs IS '解析器配置表';

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

COMMENT ON TABLE business_domain.skill_catalog IS '平台共享 AI Skill 目录表（无租户，系统预置）';

COMMENT ON TABLE business_domain.template_attributes IS '模板属性表（对象下的属性字段定义）';

COMMENT ON COLUMN business_domain.template_attributes.ontology_code IS '稳定的 ontology 属性编码';

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

COMMENT ON TABLE business_domain.template_domains IS '模板领域定义（含版本和发布信息）';

COMMENT ON TABLE business_domain.template_objects IS '模板对象表（场景下的实体对象定义，含属性和本体编码）';

COMMENT ON COLUMN business_domain.template_objects.ontology_code IS '稳定的 ontology 实体编码，用于 compiled schema 和 Neo4j 标签';

COMMENT ON COLUMN business_domain.template_objects.aliases IS '实体别名列表，用于 LLM 抽取时的名称匹配';

COMMENT ON TABLE business_domain.template_relations IS '模板关系表（场景下对象间的关系定义）';

COMMENT ON COLUMN business_domain.template_relations.ontology_code IS '稳定的 ontology 关系编码';

COMMENT ON COLUMN business_domain.template_relations.aliases IS '关系别名列表，用于 LLM 抽取时的名称匹配';

COMMENT ON TABLE business_domain.template_scenarios IS '模板场景定义（含版本和发布信息）';

COMMENT ON TABLE business_domain.tenant_skills IS '租户技能启用状态表（有租户，记录租户对 Skill 的启用/停用）';

COMMENT ON TABLE data_governance.retention_policy IS '数据治理三级保留策略（tenant/kb/document）；NULL 表示继承上级策略。';

COMMENT ON COLUMN data_governance.retention_policy.retention_days IS '保留天数：NULL=继承上级；0=不自动到期；>0=按天数到期。';

COMMENT ON COLUMN data_governance.retention_policy.grace_days IS '宽限天数：NULL=继承上级；0=无宽限；>0=宽限天数。';

COMMENT ON COLUMN knowledge_base.data_source_connections.space_id IS '所属领域空间；连接只在同领域内可见、可被引用（036/089）';

COMMENT ON TABLE knowledge_base.document_tags IS '文档与标签的多对多关联，级联删除。';

COMMENT ON TABLE knowledge_base.evolution_candidates IS '演化候选（含证据与门禁结论；按 run 级联）。';

COMMENT ON TABLE knowledge_base.evolution_corpus_cases IS '演化评测语料（由 tests/eval/nl2sql/corpus.yaml 一次性生成，之后以 DB 为准）。';

COMMENT ON TABLE knowledge_base.evolution_diagnosis_reports IS '演化台诊断报告（一次诊断 run 一份，纯追加）。见 2026-10-18 演化台服务化设计。';

COMMENT ON TABLE knowledge_base.evolution_ledgers IS '演化台账（采纳 / 拒绝记录，人工可审计）。';

COMMENT ON TABLE knowledge_base.evolution_run_logs IS '演化 run 的日志行（按 run_id 级联，无 tenant_id）。';

COMMENT ON TABLE knowledge_base.evolution_runs IS '演化台运行（一次 alias / query_rule 演化）。见 2026-10-18 演化台服务化设计。';

COMMENT ON TABLE knowledge_base.folders IS 'KB 级一层文件夹，用户可按文件夹组织文档。预设文件夹（is_preset=TRUE）按中文拼音排序，用户新建文件夹按创建时间降序排在预设之后。';

COMMENT ON TABLE knowledge_base.kb_tools IS '知识库工具：本库登记的可执行 API 工具，可绑定到本体 Skill/Action';

COMMENT ON COLUMN knowledge_base.kb_tools.tool_name IS '库内唯一的工具名；创建后不可改（ontology_tool_bindings 按它引用）';

COMMENT ON COLUMN knowledge_base.kb_tools.runtime IS '运行时配置（echo / api），格式见 jonex_core/common/tool_runtime.py；NULL = 没有可调入口';

COMMENT ON COLUMN knowledge_base.kb_tools.secret_encrypted IS '鉴权凭据密文（Fernet，DATA_SOURCE_SECRET_KEY）；接口只回 has_secret，不回明文或密文';

COMMENT ON COLUMN knowledge_base.kb_tools.read_only IS '只读声明（登记人显式勾选，默认 false = 写操作）；只有只读工具才可能被知识库问答自动调用';

COMMENT ON TABLE knowledge_base.knowledge_answer_feedback IS '检索问答用户反馈主记录：Web 按 history_id 唯一、MCP 按 request_id 唯一，保存最新反馈状态与上下文快照';

COMMENT ON COLUMN knowledge_base.knowledge_answer_feedback.adopted IS '是否已被管理员采纳';

COMMENT ON COLUMN knowledge_base.knowledge_answer_feedback.request_id IS 'MCP 检索请求 id（= search_request_event.request_id，能力信封 request_id）；Web 反馈为 NULL';

COMMENT ON COLUMN knowledge_base.knowledge_answer_feedback.mcp_key_id IS '提交反馈的 MCP Key id 快照；Web 反馈为 NULL（情况追踪据此区分来源）';

COMMENT ON COLUMN knowledge_base.knowledge_answer_feedback.mcp_key_name IS '提交时的 MCP Key 名称快照（跨 schema 只读取 platform.mcp_keys.name）；Key 改名不影响历史记录';

COMMENT ON TABLE knowledge_base.knowledge_answer_feedback_event IS '检索问答用户反馈变更事件：只增不改，operation_id 幂等，保留每次变更';

COMMENT ON COLUMN knowledge_base.knowledge_answer_feedback_event.request_id IS 'MCP 检索请求 id（与主表同源）；Web 反馈为 NULL';

COMMENT ON COLUMN knowledge_base.knowledge_answer_feedback_event.mcp_key_id IS '提交反馈的 MCP Key id 快照；Web 反馈为 NULL';

COMMENT ON TABLE knowledge_base.knowledge_data_sources IS 'KB 数据源实例表（access_type: api 出站拉取 / api_push 入站推送 / storage 外部对象存储 / file 上传）';

COMMENT ON COLUMN knowledge_base.knowledge_data_sources.connection_id IS '指向 data_source_connections；非空 = 这个知识库引用的是池子里的连接，config 从那边读';

COMMENT ON TABLE knowledge_base.knowledge_documents IS '知识库文档元数据表（业务层独有，含状态机和 RAG 对账字段）';

COMMENT ON COLUMN knowledge_base.knowledge_documents.id IS '知识文档 ID';

COMMENT ON COLUMN knowledge_base.knowledge_documents.tenant_id IS '租户 ID，所有普通请求必须按租户隔离';

COMMENT ON COLUMN knowledge_base.knowledge_documents.file_name IS '原始文件名';

COMMENT ON COLUMN knowledge_base.knowledge_documents.file_path IS '上传文件在共享输入目录中的路径';

COMMENT ON COLUMN knowledge_base.knowledge_documents.file_size IS '文件大小，单位字节';

COMMENT ON COLUMN knowledge_base.knowledge_documents.mime_type IS '上传文件 MIME 类型';

COMMENT ON COLUMN knowledge_base.knowledge_documents.storage_backend IS '对象存储后端：local / cos';

COMMENT ON COLUMN knowledge_base.knowledge_documents.storage_key IS '对象存储 key（local 后端等同 file_path）';

COMMENT ON COLUMN knowledge_base.knowledge_documents.knowledge_base_id IS '知识库业务 ID，用于同租户下划分知识库；普通文档请求必须明确指定';

COMMENT ON COLUMN knowledge_base.knowledge_documents.status IS '文档解析状态：pending/parsing/ready/failed/deleting/deleted';

COMMENT ON COLUMN knowledge_base.knowledge_documents.rag_task_id IS 'RAG 原子能力解析任务 ID';

COMMENT ON COLUMN knowledge_base.knowledge_documents.rag_doc_ids IS 'RAG 入库后返回的文档 ID 列表';

COMMENT ON COLUMN knowledge_base.knowledge_documents.error_message IS '文档解析失败原因';

COMMENT ON COLUMN knowledge_base.knowledge_documents.ontology_status IS '本体抽取状态：pending/extracting/ready/failed';

COMMENT ON COLUMN knowledge_base.knowledge_documents.ontology_error IS '本体抽取失败原因';

COMMENT ON COLUMN knowledge_base.knowledge_documents.ontology_retry_count IS '本体抽取重试次数';

COMMENT ON COLUMN knowledge_base.knowledge_documents.extra_metadata IS '业务扩展元数据，不承载租户或知识库隔离字段';

COMMENT ON COLUMN knowledge_base.knowledge_documents.data_source_type IS '文档来源类型：api/api_push/storage/file';

COMMENT ON COLUMN knowledge_base.knowledge_documents.llm_wiki_compile_status IS 'LLM-Wiki 编译状态：NULL/compiling/compiled/stale/failed';

COMMENT ON COLUMN knowledge_base.knowledge_documents.llm_wiki_compile_error IS 'LLM-Wiki 编译失败原因';

COMMENT ON COLUMN knowledge_base.knowledge_documents.llm_wiki_compile_warnings IS 'LLM-Wiki 编译警告列表（JSONB 数组）';

COMMENT ON COLUMN knowledge_base.knowledge_documents.llm_wiki_compile_requested_at IS 'LLM-Wiki 编译请求时刻（patrol 超时基准，UTC）';

COMMENT ON COLUMN knowledge_base.knowledge_documents.llm_wiki_compiled_at IS 'LLM-Wiki 编译完成时刻';

COMMENT ON COLUMN knowledge_base.knowledge_documents.llm_wiki_task_id IS 'OpenKB 容器编译任务 ID（对齐 rag_task_id），供对账巡检轮询';

COMMENT ON COLUMN knowledge_base.knowledge_documents.is_deleted IS '软删除标记：0 正常，1 删除';

COMMENT ON COLUMN knowledge_base.knowledge_documents.archived_at IS '归档时间（naive TIMESTAMP，与本表 created_at/updated_at 同时区口径）；NULL=未归档。';

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

COMMENT ON COLUMN knowledge_base.knowledge_info.ingest_backend IS '[jonex] 库级入库派单：rag；NULL=跟随服务端开关 KB_INGEST_BACKEND（2026-10-09）';

COMMENT ON COLUMN knowledge_base.knowledge_info.ontology_compile_backend IS 'compile 步后端派单值：rag（atomic-rag）/ ontology_compile（新服务）/ NULL=跟随 env';

COMMENT ON COLUMN knowledge_base.knowledge_info.procedural_extract IS '库级 procedural（Skill/Action 规程）抽取开关：TRUE=参与 / NULL·FALSE=不参与；与总闸 ONTOLOGY_PROCEDURAL_EXTRACT_ENABLED AND 生效';

COMMENT ON COLUMN knowledge_base.knowledge_info.extract_tool_calls IS '抽取期调用工具（catalog: 抽取技能）库级开关：TRUE = 写图前调用登记的只读工具补充 / 校验；NULL / FALSE = 不调用';

COMMENT ON TABLE knowledge_base.knowledge_model_settings IS '知识库级模型覆盖；没有行=继承引擎默认；清空即删行';

COMMENT ON COLUMN knowledge_base.knowledge_model_settings.scope IS 'parse_multimodal | compile_ontology | compile_wiki';

COMMENT ON TABLE knowledge_base.knowledge_parser_settings IS 'KB 级解析引擎设置表：每个知识库按解析器类目(parser_type)绑定一个 parser_config，并保存提示词与模板来源快照';

COMMENT ON COLUMN knowledge_base.knowledge_parser_settings.parser_type IS '解析器类目（= business_domain.parser_configs.parser_type，如 document/video/audio/image/txt/web/cad），KB 内每类唯一，作为上传路由键';

COMMENT ON COLUMN knowledge_base.knowledge_parser_settings.parser_config_id IS '引用 business_domain.parser_configs.id，表示该类目下选中的解析器定义；上传时按文件后缀匹配其 file_types 命中';

COMMENT ON COLUMN knowledge_base.knowledge_parser_settings.prompt_template_id IS '主解析提示词来源模板 ID，仅用于追溯；实际运行使用 prompt_text 快照';

COMMENT ON COLUMN knowledge_base.knowledge_parser_settings.summary_template_id IS '自动摘要提示词来源模板 ID，仅用于追溯；实际运行使用 summary_prompt_text 快照';

COMMENT ON COLUMN knowledge_base.knowledge_parser_settings.tag_template_id IS '自动标签提示词来源模板 ID，仅用于追溯；实际运行使用 tag_prompt_text 快照';

COMMENT ON TABLE knowledge_base.knowledge_query_images IS '以图问知识的查询图片：按 tenant+user 隔离；仅保存历史时落盘；无未删除历史引用时删除对象并软删';

COMMENT ON TABLE knowledge_base.knowledge_search_history IS '知识检索历史表（按 tenant+user+query+knowledge_base 去重，软删除）';

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

COMMENT ON TABLE knowledge_base.knowledge_search_history_images IS '检索历史与查询图片的关联（多对多），引用是否有效以历史行 is_deleted 为准';

COMMENT ON COLUMN knowledge_base.lineage_edges.last_seen_at IS '该边最后一次被来源确认的时间（手工编辑/文件导入/OpenLineage 摄入均刷新）；读取时据此标 stale';

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

COMMENT ON TABLE knowledge_base.metric_call_approvals IS 'W4.8 指标级调用审批（pending/approved/suspended；expires_at 为批准有效期，NULL=不过期）。';

COMMENT ON COLUMN knowledge_base.metric_call_approvals.expires_at IS '批准的有效期；只对 approved 有意义，到期后读时返回 expired（不写回 status）。';

COMMENT ON TABLE knowledge_base.nl2sql_metric_shadow IS 'W3.3 M4 指标编译影子对比（只记录不替换；不存原始数据值）。';

COMMENT ON TABLE knowledge_base.nl2sql_observations IS '② NL→SQL 运行时观测（诊断/演化输入；审计不动）。见 2026-10-12 运行时观测设计。';

COMMENT ON TABLE knowledge_base.ontology_abstract_schemas IS '跨数据源的抽象 schema（概念对齐层），一知识库一份；concepts 里的 sources 自带源映射';

COMMENT ON COLUMN knowledge_base.ontology_abstract_schemas.schema_version IS '每保存一次 +1（生成候选、改名、合并、确认都算），供界面做乐观锁';

COMMENT ON COLUMN knowledge_base.ontology_abstract_schemas.status IS 'proposed = LLM 提的候选（不参与检索）/ active = 人工确认后生效';

COMMENT ON COLUMN knowledge_base.ontology_abstract_schemas.schema IS 'v2 完整抽象 schema：{entities:[{name,display_name,description,aliases,sources[{data_source_id,entity_name,included}],attributes[…]},…], relations:[{name,source,target,cardinality,description,backings[{data_source_id,relation_name,source_column,target_column,origin}]},…]}（v1 时代存的是概念数组；迁移后一律 v2）';

COMMENT ON COLUMN knowledge_base.ontology_abstract_schemas.source_hash IS '生成时各源 (data_source_id, entity_name) 的指纹，供界面判断要不要重新生成';

COMMENT ON COLUMN knowledge_base.ontology_abstract_schemas.edited_by IS '最后人工确认/编辑人的 user_id；NULL = 从未人工编辑过（纯 LLM 候选）';

COMMENT ON COLUMN knowledge_base.ontology_abstract_schemas.is_deleted IS '软删除标记：0-正常，1-删除';

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

COMMENT ON COLUMN knowledge_base.ontology_compiled_schemas.data_source_id IS '所属数据源；NULL = 知识库级（不绑定具体数据源）';

COMMENT ON TABLE knowledge_base.ontology_synonyms IS '知识库级同义词组，等价词统一映射为标准实体（本期仅存储管理）';

COMMENT ON COLUMN knowledge_base.ontology_synonyms.tenant_id IS '租户 ID';

COMMENT ON COLUMN knowledge_base.ontology_synonyms.knowledge_base_id IS '知识库业务 ID';

COMMENT ON COLUMN knowledge_base.ontology_synonyms.terms IS '同义词列表 string[]（归一化去重后至少 2 个）';

COMMENT ON COLUMN knowledge_base.ontology_synonyms.canonical IS '标准词，必须属于 terms，缺省取 terms[0]';

COMMENT ON COLUMN knowledge_base.ontology_synonyms.is_deleted IS '软删除标记：0-正常，1-删除';

COMMENT ON TABLE knowledge_base.ontology_template_bindings IS '知识库-模板绑定表，记录每个 KB 绑定的模板领域和场景';

COMMENT ON COLUMN knowledge_base.ontology_template_bindings.tenant_id IS '租户 ID';

COMMENT ON COLUMN knowledge_base.ontology_template_bindings.knowledge_base_id IS '知识库业务 ID';

COMMENT ON COLUMN knowledge_base.ontology_template_bindings.template_domain_id IS '绑定的模板领域 ID（可选）';

COMMENT ON COLUMN knowledge_base.ontology_template_bindings.template_scenario_id IS '绑定的模板场景 ID（可选）';

COMMENT ON COLUMN knowledge_base.ontology_template_bindings.source_type IS '模板来源类型：business_template / yaml_default';

COMMENT ON COLUMN knowledge_base.ontology_template_bindings.status IS '绑定状态：active / deprecated';

COMMENT ON TABLE knowledge_base.ontology_tool_bindings IS '本体 Skill/Action 实例到平台可执行工具（business_domain.skill_catalog.tool_name）的人工绑定';

COMMENT ON COLUMN knowledge_base.ontology_tool_bindings.entity_type IS '绑定的本体实例类型：Skill / Action';

COMMENT ON COLUMN knowledge_base.ontology_tool_bindings.canonical_name IS '绑定的本体实例名（canonical_name）；重抽改名后绑定悬空，不自动迁移';

COMMENT ON COLUMN knowledge_base.ontology_tool_bindings.tool_source IS '工具来源：platform = 平台工具目录（business_domain.skill_catalog）/ kb = 本库工具（knowledge_base.kb_tools）';

COMMENT ON COLUMN knowledge_base.ontology_tool_bindings.tool_name IS '按 tool_source 指向 skill_catalog.tool_name 或同库 kb_tools.tool_name；可用性在读取时实时校验';

COMMENT ON COLUMN knowledge_base.ontology_tool_bindings.param_mapping IS '工具入参 → 取值来源：$input.<key> / $attr.<属性> / $entity.name / 字面量';

COMMENT ON COLUMN knowledge_base.ontology_tool_bindings.enabled IS '绑定启停；停用后执行器按「无绑定」处理';

COMMENT ON COLUMN knowledge_base.ontology_tool_bindings.qa_callable IS '允许知识库问答自动调用该绑定的工具（默认 false）；另需总闸开启、判定 allow、工具只读';

COMMENT ON COLUMN knowledge_base.search_adoption_event.target_type IS '采用目标类型：document（RAG 召回文档）/ ontology_instance（本体实例）。宽度须容纳最长的 17 字符值，不得收窄';

COMMENT ON COLUMN knowledge_base.search_hit_event.target_type IS '命中目标类型：document（RAG 召回文档）/ ontology_instance（本体实例）。宽度须容纳最长的 17 字符值，不得收窄';

COMMENT ON COLUMN knowledge_base.search_request_event.knowledge_base_ids IS '本次检索实际覆盖的知识库 id 列表（来自 capability 权限过滤后的 data，不是客户端上报值）；领域服务形态（只有 service_id）入口处为空数组，聚合期按 entry_service_id 反查 service_knowledge_bases 补齐';

COMMENT ON COLUMN knowledge_base.search_request_event.mcp_key_id IS '产生该请求的 MCP Key id；Web/其他链路为 NULL。MCP 行 query/query_hash 恒为空（数据最小化）';

COMMENT ON TABLE knowledge_base.semantic_versions IS '语义版本（不可变快照）。见 2026-10-23 演化台信任闭环设计 §3.1。';

COMMENT ON TABLE knowledge_base.semantic_versions_archive IS '语义版本归档（与主表同构 + archived_at；无 active 唯一索引）。';

COMMENT ON TABLE knowledge_base.service_api_keys IS '领域服务 API Key 管理表';

COMMENT ON TABLE knowledge_base.service_configs IS '领域服务配置表';

COMMENT ON TABLE knowledge_base.service_knowledge_bases IS '领域服务-知识库关联表';

COMMENT ON TABLE knowledge_base.services IS '领域服务表';

COMMENT ON TABLE knowledge_base.space_permissions IS '领域空间权限表';

COMMENT ON TABLE knowledge_base.spaces IS '领域空间表';

COMMENT ON TABLE knowledge_base.tags IS 'KB 级标签，支持文档分类与筛选。同知识库内标签名称唯一。';

COMMENT ON TABLE knowledge_base.task_center_documents IS 'Ontology 库文档的任务中心接管行：一份文档交给了任务中心就有一行，记录走到哪一步。与 knowledge_documents 的写入必须在同一事务内（见 capabilities/knowledge_base/models/task_center.py）';

COMMENT ON COLUMN knowledge_base.task_center_documents.document_id IS '被接管的文档 id，即 knowledge_base.knowledge_documents.id。主键——一份文档在链路上只可能有一个位置；代价是二次进链会覆盖旧行';

COMMENT ON COLUMN knowledge_base.task_center_documents.space_id IS '提交给任务中心时用的空间（init_task 会经 knowledge_base.spaces 校验它属于本租户）。存下来是因为「后台重新提交」与「看门狗判死」两条路都不在请求上下文里，拿不到知识库的 space_id';

COMMENT ON COLUMN knowledge_base.task_center_documents.task_id IS '任务中心 tasks.id（由**本侧**提前 uuid4 生成后自带提交，见 SDK 的提交重试约定——不带 task_id 服务端不做幂等去重，重发就是新建第二条任务）。唯一索引供 remove_task 反查';

COMMENT ON COLUMN knowledge_base.task_center_documents.operator_id IS '发起上传的 user_id（落 task_center.tasks.created_by 的语义）。**可空是合法状态**：内部链路 / 拿不到身份时为空，看门狗重新提交时原样带上';

COMMENT ON COLUMN knowledge_base.task_center_documents.state IS 'submitting（已提交给任务中心但还没确认建没建成，看门狗会补试）/ managed（这条链在跑、或在等下一步派活）/ released（交接完成：任务中心不再管这份文档，接管行交还给既有对账）。只有三态——用户中途接管是「拦住用户、让他等」，没有任何动作会产生 revoked';

COMMENT ON COLUMN knowledge_base.task_center_documents.current_step IS '当前在跑哪一步：parse / compile（现役两条编排只用这两个；改版前遗留的三步任务还会有 ingest —— 运行方代码里三个都认）';

COMMENT ON COLUMN knowledge_base.task_center_documents.execution_id IS '本步的受理凭证（任务中心生成），上报终态时原样带回';

COMMENT ON COLUMN knowledge_base.task_center_documents.handled_execution_id IS '最后一次上报过终态的那次执行编号（值来自同一句 UPDATE 里那个被清空的 execution_id，所以本步报完之后这一对恒为 (NULL, 刚报的那次)）。用途：① 审计——回答「现在这个文档状态是哪一次执行做出来的」；② mark_step_handled 的 WHERE 条件 handled_execution_id IS DISTINCT FROM :execution_id 兜住「上报与清空之间进程崩掉、下一轮又拿同一行去报」的残余窗口（第二次 rowcount=0）。看门狗自己不读这一列，也没有「handled == execution」这种分档';

COMMENT ON COLUMN knowledge_base.task_center_documents.rag_task_id IS '本步发给下游的任务号：`rag` 链两步都是 atomic-rag 的任务号。**每步完成后清空**——让下一轮的「有 execution_id、无 rag_task_id」天然只衡量任务中心的派活延迟，而不是下游的执行时长（两个完全不同的东西不能共用一个阈值）';

COMMENT ON COLUMN knowledge_base.task_center_documents.step_started_at IS '看门狗的计时基准。**不是 created_at**——那是「接管开始」的时刻，而每步的活是从「本步开始」算的，共用一个基准会把前一步的耗时算到后一步头上';

COMMENT ON COLUMN knowledge_base.task_center_documents.submit_attempts IS '重新提交的次数（state=submitting 卡住时看门狗会补试，用同一个 task_id，撞 1006 即 REUSED）';

COMMENT ON COLUMN knowledge_base.task_center_documents.error_message IS '判死原因 / 最后一次提交失败的原因，只做排障留痕';

COMMENT ON TABLE knowledge_base.task_execution_registry IS '任务执行登记（ExecutionRegistry 端口的持久实现）：这个 execution_id 接没接过。认不出重派就会重复干活，而默认的内存实现重启与多副本都失效';

COMMENT ON COLUMN knowledge_base.task_execution_registry.released_at IS 'NULL = 当前被持有；非空 = 已释放、可以被重新领取。已释放的可再占是重发安全性的前提';

COMMENT ON COLUMN knowledge_base.task_execution_registry.claim_count IS '领过几次（含重派重新领的）。只做观测，不参与任何判定';

COMMENT ON TABLE metering.llm_usage_log IS 'LLM 网关调用日志：记录每次 LLM/Embedding 调用的 token 用量';

COMMENT ON COLUMN metering.llm_usage_log.request_id IS '计量幂等键：每次逻辑 LLM 调用唯一、重试稳定；调用方透传 X-Jonex-Request-Id 或网关按 trace_id + 请求体哈希派生';

COMMENT ON COLUMN metering.llm_usage_log.trace_id IS '链路追踪 ID：一次用户业务请求一个，用于把该请求下的多次 LLM 调用归组统计（来源 X-Jonex-Trace-Id）';

COMMENT ON COLUMN metering.llm_usage_log.scene IS '调用场景：ontology_extract/ontology_qa/lightrag_extract/lightrag_query/lightrag_embed/lightrag_embed_query 等';

COMMENT ON COLUMN metering.llm_usage_log.latency_ms IS '上游响应延迟（毫秒）';

COMMENT ON COLUMN metering.llm_usage_log.is_estimated IS 'usage 是否为估算（true=上游未返回 usage，走兜底估算）';

COMMENT ON VIEW metering.v_llm_usage_by_doc IS 'LLM 计量按文档汇总（call_count/tokens/avg_latency）';

COMMENT ON VIEW metering.v_llm_usage_by_trace IS 'LLM 计量按链路 trace 汇总';

COMMENT ON VIEW metering.v_llm_usage_daily IS 'LLM 计量按天汇总（趋势/对账）';

COMMENT ON VIEW metering.v_llm_usage_detail IS 'LLM 计量明细可读视图（关联 kb/doc 名称 + 本地时区）';

COMMENT ON COLUMN monitor.call_log.mcp_tool IS 'MCP 工具名（仅 protocol=mcp 时有值）；读侧对象标识 = COALESCE(mcp_tool, target)';

COMMENT ON COLUMN monitor.call_log.call_scope IS '调用边界：external(外部依赖)/internal(内部互调)/open(开放能力)；与 direction 正交';

COMMENT ON COLUMN ontology_compile.compile_tasks.content_generation IS 'compile 提交时的文档代次（fencing）：KB 对账代次栅栏与 doc.content_generation 比较；0=未提供';

COMMENT ON TABLE platform.api_keys IS 'API Key 管理表';

COMMENT ON TABLE platform.application_routes IS '应用路由表';

COMMENT ON TABLE platform.applications IS '应用注册表';

COMMENT ON TABLE platform.audit_logs IS '审计日志表';

COMMENT ON COLUMN platform.identity_integration_config.idp_hint_alias IS '租户级 kc_idp_hint alias 覆盖值（Keycloak IdP alias，如 casdoor）；NULL = 用平台默认值 KEYCLOAK_IDP_HINT_DEFAULT_ALIAS，无覆盖值时不落库、无需逐租户配置；空串由写入路径归一为 NULL';

COMMENT ON COLUMN platform.identity_integration_config.last_sync_at IS '最近一次自动同步的**发起**时刻（调度器 CAS 抢占时写入，与本行 next_sync_at 同一次 UPDATE）；手动「立即同步」不写本列。NULL = 从未自动同步过。朴素时间（TIMESTAMP 无时区），与同表 datasource_connection_verified_at 同档';

COMMENT ON COLUMN platform.identity_integration_config.next_sync_at IS '下一次该自动同步的时刻 = 上次发起时刻 + max(sync_interval, 1) 小时（未配 sync_interval 时用 IDENTITY_SYNC_DEFAULT_INTERVAL_HOURS）。多副本靠本列做 CAS 抢占（IS NOT DISTINCT FROM，不是 =）。NULL = **立即到期**（首次打开自动同步 / 本列刚落地），不是「未排期」';

COMMENT ON COLUMN platform.identity_sync_tasks.user_enabled IS '本次同步启用（重新启用）的用户数（与 user_disabled 对称）';

COMMENT ON COLUMN platform.identity_sync_tasks.user_deleted IS '本次全量同步经复核确认上游已删、因而被平台软删的用户数（复活走 user_enabled）';

COMMENT ON TABLE platform.login_tickets IS '一次性登录票据表（跨域 ticket/code 交换，只存 hash 不存明文）';

COMMENT ON TABLE platform.menus IS '菜单表';

COMMENT ON TABLE platform.permissions IS 'RBAC 权限表';

COMMENT ON TABLE platform.role_permissions IS '角色-权限关联表';

COMMENT ON TABLE platform.roles IS 'RBAC 角色表';

COMMENT ON TABLE platform.system_configs IS '系统配置表';

COMMENT ON TABLE platform.task_schedules IS '任务调度表';

COMMENT ON TABLE platform.tenants IS '租户信息表';

COMMENT ON TABLE platform.user_roles IS '用户-角色关联表';

COMMENT ON TABLE platform.users IS '用户表';

COMMENT ON COLUMN platform.users.upstream_subject IS '上游客户系统的稳定主体标识（本地场景 = Casdoor id）。跨 realm 稳定，是认人主匹配键；external_id 为 Keycloak 本地 user_id、跨 realm 漂移。两者语义不得混用。';

COMMENT ON TABLE task_center.runner_definitions IS '运行方清单：平台内置（平台预置，不通过接口写；不带租户）';

COMMENT ON COLUMN task_center.runner_definitions.capability_id IS '运行方对外能力 ID（对应管理方那边的 client）';

COMMENT ON TABLE task_center.subtask_attempts IS '子任务尝试历史：某个子任务每一次尝试（含首次和每次重试）的一条记录（只追加不覆盖）';

COMMENT ON COLUMN task_center.subtask_attempts.execution_id IS '这次尝试的受理凭证（管理方生成），在同一租户+同一空间内唯一；report_status 不靠它定位（定位只认 subtasks.execution_id），只有查历史/对账读它';

COMMENT ON COLUMN task_center.subtask_attempts.outputs IS '这次尝试的产出快照（结构同 subtasks.outputs）';

COMMENT ON TABLE task_center.subtask_reports IS '上报收件箱：运行方上报先入队、后台循环再逐条消化（is_deleted=1 表示「已处理完」——正常消化或判死信，看 dead_lettered；不是「被删」）';

COMMENT ON COLUMN task_center.subtask_reports.execution_id IS '受理凭证：消化时按 tenant_id + space_id + 它定位 subtasks（同 subtasks.execution_id）';

COMMENT ON COLUMN task_center.subtask_reports.output_refs IS '产出「去哪儿看」的引用（随行记录）：消化时原样转抄到 `subtasks.output_refs`，本列不参与裁决';

COMMENT ON COLUMN task_center.subtask_reports.ts IS '运行方侧事件时间；只落库留痕、不参与任何裁决（管理方侧不打日志，唯一的落点就是本列）';

COMMENT ON COLUMN task_center.subtask_reports.reported_at IS '管理方收到上报的时刻；超时巡检「让路」判据读它（跟 started_at 同一个钟：管理方本机时间）';

COMMENT ON COLUMN task_center.subtask_reports.attempts IS '消化尝试次数；不仅是观测——达 TASK_CENTER_REPORT_MAX_ATTEMPTS 就判死信（默认 5）';

COMMENT ON COLUMN task_center.subtask_reports.last_error IS '上次消化失败的原因（不截断，存「异常类名: 异常消息」）';

COMMENT ON COLUMN task_center.subtask_reports.processed_at IS '处理完的时刻（正常消化 / 判死信都会写）';

COMMENT ON COLUMN task_center.subtask_reports.dead_lettered IS '1 = 已判死信：消化连续失败达上限，放弃自动重试（按「上报行」算，不是按子任务）。死信行 is_deleted 同时为 1（出队）；要放它重来，把这一行的 dead_lettered 与 is_deleted 都重置回 0、attempts 归 0';

COMMENT ON COLUMN task_center.subtask_reports.is_deleted IS '1 = 已处理完（正常消化或判死信，看 dead_lettered；语义特殊：不是「被删掉」，记录永远留着供对账）';

COMMENT ON TABLE task_center.subtasks IS '子任务实例：挂在某一次运行（task_attempt_id）下的一步；一行存该子任务最新一次尝试';

COMMENT ON COLUMN task_center.subtasks.node_id IS '是编排里的哪个节点（对应 config.steps[].node_id）；同一次运行里不重复';

COMMENT ON COLUMN task_center.subtasks.step_code IS '这个节点用的是哪个内置步骤（如 parse / ingest / compile）；管理方用来判并发上限、发版校验、查历史，并填进 execute 派活报文供运行方分流';

COMMENT ON COLUMN task_center.subtasks.subtask_name IS '给人看的名字：诞生时取内置步骤的 name（按 step_code 查 task_catalog_steps.step_code）';

COMMENT ON COLUMN task_center.subtasks.depends_on IS '它依赖哪些节点（一堆 node_id），诞生时从 config 固化';

COMMENT ON COLUMN task_center.subtasks.next_steps IS '我成功后激活哪些节点（一堆 node_id），诞生时从 config 固化；不参与调度判定，只供对偶校验与排查';

COMMENT ON COLUMN task_center.subtasks.timeout_seconds IS '这步的超时时间（秒），诞生时从编排固化，超时巡检读这一列';

COMMENT ON COLUMN task_center.subtasks.inputs IS '填好后的真实入参（bind_inputs 解析填值后的结果）';

COMMENT ON COLUMN task_center.subtasks.outputs IS '产出（成功后回填）';

COMMENT ON COLUMN task_center.subtasks.output_refs IS '产出「去哪儿看」的引用（展示用，**不参与下游取参**）：运行方终态上报时带的数组，控制台读接口原样回给前端，前端据此调运行方自己的接口看详情（权限归运行方那侧判）';

COMMENT ON COLUMN task_center.subtasks.current_attempt IS '当前第几次尝试（指针，指向 subtask_attempts.attempt）；首次=1，retry_subtask 时 +1';

COMMENT ON COLUMN task_center.subtasks.execution_id IS '管理方生成的受理凭证（派活前生成、随 execute 传给运行方；重试时重新生成）；在同一租户+同一空间内唯一';

COMMENT ON COLUMN task_center.subtasks.state_version IS '每行版本号，用于防重复改（乐观锁）';

COMMENT ON COLUMN task_center.subtasks.updated_at IS '每次对该行的写都显式刷新（诞生/上游填值/状态变更/retry 重置）；防僵尸拿它当「变成就绪的时刻」';

COMMENT ON COLUMN task_center.subtasks.is_deleted IS '假删除标记：删任务时它的子任务一并置 1（任务删了，子任务必定也全删）';

COMMENT ON COLUMN task_center.subtasks.error_code IS '失败原因的消息键（可翻译的那半）；读接口按请求 X-Lang 渲染。error_message 仍是原文槽 + 排障快照';

COMMENT ON COLUMN task_center.subtasks.error_params IS 'error_code 的渲染实参（JSONB）。任务级那条装的是「零件数组」：每个子任务一条 {node_id, code, params, message}';

COMMENT ON TABLE task_center.task_attempts IS '任务尝试：某任务每一次运行（含首次和每次重跑）的一条记录（只追加不覆盖）';

COMMENT ON COLUMN task_center.task_attempts.attempt IS '第几次运行（首次=1，重跑 +1）';

COMMENT ON TABLE task_center.task_catalog_steps IS '内置步骤清单：每个任务下有哪些步骤可选（步骤交给谁跑 runner_code、输入输出 schema、默认超时 timeout_seconds、并发上限 max_concurrency 都在这）';

COMMENT ON COLUMN task_center.task_catalog_steps.step_code IS '步骤名，如 parse / ingest / compile（编排里 steps[].step_code 引用它 —— 两边**同名**，就是这一列的值；运行方按这个值分流到不同执行模式）';

COMMENT ON COLUMN task_center.task_catalog_steps.runner_code IS '这步交给哪个运行方（runner_definitions.code），一对一绑定';

COMMENT ON COLUMN task_center.task_catalog_steps.max_concurrency IS '这步最多同时跑几个（运行方对此类活的并发上限）；空=不限；派活时按「所属任务的 task_catalog_code + 本行的 step_code」数在跑数、到上限就跳过 —— 每个目录的每个步骤各有一份名额，跨目录不互相占位（2026-10-14 起，此前是全平台按 step_code 共用一份）';

COMMENT ON COLUMN task_center.task_catalog_steps.timeout_seconds IS '这步的默认超时时间（秒）；建编排时预填到 config.steps[].timeout_seconds（必填、可改）';

COMMENT ON COLUMN task_center.task_catalog_steps.input_schema IS '这步接受什么输入：{ 字段名: FieldSpec }';

COMMENT ON COLUMN task_center.task_catalog_steps.output_schema IS '这步产出什么：{ 字段名: FieldSpec }，字段名供下游 @step.outputs.x 引用';

COMMENT ON TABLE task_center.task_definition_versions IS '任务定义版本：一版编排（步骤顺序 / 依赖关系 / 数据传递 / 超时）；published 后永久固化不可改（**没有失败策略这个配置** —— 失败怎么处置下游是平台固定规则，见 config 列注释）';

COMMENT ON COLUMN task_center.task_definition_versions.scope IS '跟所属定义一致：system / tenant / space';

COMMENT ON COLUMN task_center.task_definition_versions.space_id IS '空间 ID：scope=space 时必有；任务执行哪一版就用哪一版的 space_id';

COMMENT ON COLUMN task_center.task_definition_versions.status IS 'draft(草稿)/published(已发布，终点)；发布后不可改、不可删、不可逆回 draft';

COMMENT ON COLUMN task_center.task_definition_versions.config IS '编排内容：{ input_schema, entry, steps[] }（entry 是单个 node_id；没有 fail_policy —— 失败一律 hold）';

COMMENT ON COLUMN task_center.task_definition_versions.is_deleted IS '假删除标记：只由「删定义」带出来（定义删了，版本一并假删），版本本身不能被单独删';

COMMENT ON TABLE task_center.task_definitions IS '任务定义：一类任务的「身份证」，按 scope 分系统级/租户级/空间级';

COMMENT ON COLUMN task_center.task_definitions.scope IS 'system(系统级，无租户无空间)/tenant(租户级，有租户无空间)/space(空间级，租户+空间都有)';

COMMENT ON COLUMN task_center.task_definitions.space_id IS '空间 ID：scope=space 时必有；任务执行哪一版就用哪一版的 space_id';

COMMENT ON COLUMN task_center.task_definitions.current_version_no IS '最新已发布版本号；0 = 还没发过版';

COMMENT ON TABLE task_center.task_triggers IS '触发：什么时候、用什么参数把任务跑起来（空间级）；触发器只等于定时，没有类型列';

COMMENT ON COLUMN task_center.task_triggers.space_id IS '这个触发器在哪个空间下引用定义；它触发出来的任务就落这个空间';

COMMENT ON COLUMN task_center.task_triggers.status IS 'active(启用)/paused(已暂停)/deleted(已删)；paused 的不会被调度循环扫到，但仍能手动 trig.fire';

COMMENT ON COLUMN task_center.task_triggers.cron_expr IS '【必填】5 段表达式「分 时 日 月 周」';

COMMENT ON COLUMN task_center.task_triggers.next_fire_at IS '【必填】下次该触发的时间（秒一律为 00）；后台循环按它扫（<= 现在 就是要触发）；算不出来时置 paused 告警，不许写 NULL（列本身 NOT NULL）';

COMMENT ON COLUMN task_center.task_triggers.misfire_policy IS '【必填】错过点的处置：skip=跳过、只推下次 / catch_up=补跑一次（只补一次）';

COMMENT ON COLUMN task_center.task_triggers.fixed_version IS '触发时用哪一版：空=用最新已发布版；给了号=钉住那一版（必须是 >= 1 的号、存在且已发布）。0 既不是「没填」也不是「清空」，是非法版本号（DTO ge=1 当场拒 1001）。trig.update 里「传 null」= 清空、切回最新已发布版，「不传」= 不动';

COMMENT ON COLUMN task_center.task_triggers.inputs IS '触发时带的入参，触发时被当作 fire 的 inputs 传下去';

COMMENT ON COLUMN task_center.task_triggers.last_fired_at IS '上次「到点处理」的时间 —— 不代表真的起过任务：misfire_policy=skip 那次只推 next_fire_at、没起任务，也照样写它；要「上次真正触发任务的时间」看 trig.runs 最后一条的 created_at';

COMMENT ON COLUMN task_center.task_triggers.state_version IS '每行版本号：触发抢占的 CAS 用它（§7.5 ⓪ 防多副本同时触发同一条）';

COMMENT ON TABLE task_center.tasks IS '任务实例：一次触发的产物，是子任务树的根，也是这条任务的稳定身份（重跑不换 id）';

COMMENT ON COLUMN task_center.tasks.definition_version_no IS '用的是哪个版本号（钉死，仅作溯源；编排内容本身见 definition_config）';

COMMENT ON COLUMN task_center.tasks.definition_config IS '编排快照：建任务时从 task_definition_versions.config 抄来的整份编排。推进任务读它、不反查版本——版本会被「删定义」带成假删，反查一断任务就永远收不了尾';

COMMENT ON COLUMN task_center.tasks.space_id IS '空间 ID：这次运行落在哪个空间';

COMMENT ON COLUMN task_center.tasks.inputs IS '这次运行的任务级入参（按 config.input_schema 收口后的实值），供 @task.inputs.x 在运行期求值、retry_task 复用';

COMMENT ON COLUMN task_center.tasks.current_attempt IS '当前跑到第几次运行（指针，指向 task_attempts.attempt）；首次=1，retry_task 时 +1';

COMMENT ON COLUMN task_center.tasks.status IS 'pending(待执行)/running(跑着)/completed(完成)/failed(失败)：最新一次运行的状态快照';

COMMENT ON COLUMN task_center.tasks.state_version IS '每行版本号，用于防重复改（乐观锁）';

COMMENT ON COLUMN task_center.tasks.created_by IS '建这条任务的人（user_id，不是用户名）。空 = 不是用户发起的（触发器自动触发、内部系统调用）；只写一次、之后不再变';

COMMENT ON COLUMN task_center.tasks.updated_by IS '最后一次「用户主动写」这条任务的人（user_id）。跟 created_by 的区别是它会被覆盖：系统自己推进状态（派活抢占 / 消化上报 / 超时巡检）不写这一列，只有人发起的写（如停止任务）才写';

COMMENT ON COLUMN task_center.tasks.task_source IS '任务来源（协议层 TASK_SOURCES 写死枚举，如 knowledge_base.file.manual）：这条任务是运行方从哪个入口提交的。NULL = 不是运行方提交的（定时触发 / 人工立即触发），或这列诞生之前的存量行。存码不存文案，中文名由前端按码映射';

COMMENT ON INDEX platform.idx_identity_integration_config_auto_sync IS '自动同步到期扫描的支撑索引（partial，WHERE auto_sync_enabled = true）：按 next_sync_at ASC NULLS FIRST 取一批到期租户。谓词只含不变列 auto_sync_enabled；datasource_url 非空是易变条件，留在查询 WHERE 里';

COMMENT ON INDEX platform.roles_tenant_id_name_key IS '活跃角色 (tenant_id, name) 唯一（partial，WHERE is_deleted = 0）。支撑 RoleRepository.get_by_name 的 scalar_one_or_none 与 SSO 首登/身份同步的按名绑角色；软删行不参与唯一性。';

COMMENT ON INDEX platform.users_tenant_id_upstream_subject_key IS '活跃且已回填 (tenant_id, upstream_subject) 唯一（partial，WHERE is_deleted = 0 AND upstream_subject IS NOT NULL）。支撑 UserRepository.get_by_upstream_subject 与「同一上游主体不会建重复号」的保证；未回填（NULL）与软删行不参与唯一性。';
