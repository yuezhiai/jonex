-- ============================================================
-- 悦溪平台数据库初始化 - 知识库 (knowledge_base schema)
-- 版本: 004
-- 建表顺序按业务层级自顶向下：
--   领域空间 -> 知识库 -> 领域服务及其关联 ->
--   知识文档 -> 检索历史 -> 数据源实例 -> 本体模板绑定/编译快照
-- 说明：knowledge_base 业务层与 LightRAG 内部存储分离，
--       通过 rag_task_id / rag_doc_ids 做映射。
-- ============================================================

-- ------------------------------------------------------------
-- 领域空间
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_base.spaces (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    owner_id VARCHAR(64),
    status VARCHAR(32) DEFAULT 'active',
    knowledge_base_count INTEGER DEFAULT 0,
    service_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_kb_sp_tenant ON knowledge_base.spaces(tenant_id);
CREATE INDEX IF NOT EXISTS idx_kb_sp_is_deleted ON knowledge_base.spaces(is_deleted);

-- 领域空间权限
CREATE TABLE IF NOT EXISTS knowledge_base.space_permissions (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    space_id VARCHAR(64) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    role VARCHAR(32) NOT NULL DEFAULT 'viewer',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_kb_spp_tenant ON knowledge_base.space_permissions(tenant_id);
CREATE INDEX IF NOT EXISTS idx_kb_spp_is_deleted ON knowledge_base.space_permissions(is_deleted);
CREATE INDEX IF NOT EXISTS idx_kb_spp_space ON knowledge_base.space_permissions(space_id);
CREATE INDEX IF NOT EXISTS idx_kb_spp_user ON knowledge_base.space_permissions(user_id);

-- ------------------------------------------------------------
-- 知识库信息管理
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_base.knowledge_info (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    space_id VARCHAR(64) NOT NULL,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    data_source_types JSONB DEFAULT '[]'::jsonb,
    document_count INTEGER DEFAULT 0,
    status VARCHAR(32) DEFAULT 'synced',
    owner_id VARCHAR(64),
    kb_type VARCHAR(16) NOT NULL DEFAULT 'lightrag',  -- [jonex] 知识库类型 lightrag / openkb
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_kb_tenant ON knowledge_base.knowledge_info(tenant_id);
CREATE INDEX IF NOT EXISTS idx_kb_is_deleted ON knowledge_base.knowledge_info(is_deleted);
CREATE INDEX IF NOT EXISTS idx_kb_space ON knowledge_base.knowledge_info(space_id);

-- ------------------------------------------------------------
-- 领域服务及其关联
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_base.services (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    space_id VARCHAR(64) NOT NULL,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    domain_type VARCHAR(64),
    status VARCHAR(32) DEFAULT 'active',
    api_key_encrypted VARCHAR(512),
    enabled SMALLINT DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_kb_svc_tenant ON knowledge_base.services(tenant_id);
CREATE INDEX IF NOT EXISTS idx_kb_svc_is_deleted ON knowledge_base.services(is_deleted);
CREATE INDEX IF NOT EXISTS idx_kb_svc_space ON knowledge_base.services(space_id);

-- 领域服务-知识库关联
CREATE TABLE IF NOT EXISTS knowledge_base.service_knowledge_bases (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    service_id VARCHAR(64) NOT NULL,
    kb_id VARCHAR(64) NOT NULL,
    pipeline_type VARCHAR(16) NOT NULL DEFAULT 'lightrag',  -- [jonex] lightrag / openkb；存量 KB 默认 lightrag
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_kb_skb_tenant ON knowledge_base.service_knowledge_bases(tenant_id);
CREATE INDEX IF NOT EXISTS idx_kb_skb_is_deleted ON knowledge_base.service_knowledge_bases(is_deleted);
CREATE INDEX IF NOT EXISTS idx_kb_skb_service ON knowledge_base.service_knowledge_bases(service_id);

-- 领域服务配置
CREATE TABLE IF NOT EXISTS knowledge_base.service_configs (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    service_id VARCHAR(64) NOT NULL,
    config_key VARCHAR(128) NOT NULL,
    config_value TEXT,
    description VARCHAR(512),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_kb_sc_tenant ON knowledge_base.service_configs(tenant_id);
CREATE INDEX IF NOT EXISTS idx_kb_sc_is_deleted ON knowledge_base.service_configs(is_deleted);
CREATE INDEX IF NOT EXISTS idx_kb_sc_service ON knowledge_base.service_configs(service_id);

-- 领域服务权限
CREATE TABLE IF NOT EXISTS knowledge_base.service_permissions (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    service_id VARCHAR(64) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    role VARCHAR(32) NOT NULL DEFAULT 'viewer',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_kb_svcp_tenant ON knowledge_base.service_permissions(tenant_id);
CREATE INDEX IF NOT EXISTS idx_kb_svcp_is_deleted ON knowledge_base.service_permissions(is_deleted);
CREATE INDEX IF NOT EXISTS idx_kb_svcp_service ON knowledge_base.service_permissions(service_id);

-- 领域服务 API Key 管理
CREATE TABLE IF NOT EXISTS knowledge_base.service_api_keys (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    service_id VARCHAR(64) NOT NULL,
    key_prefix VARCHAR(16) NOT NULL DEFAULT 'sk',
    key_encrypted VARCHAR(512) NOT NULL,
    expires_at TIMESTAMP,
    is_active SMALLINT DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_kb_sak_tenant ON knowledge_base.service_api_keys(tenant_id);
CREATE INDEX IF NOT EXISTS idx_kb_sak_is_deleted ON knowledge_base.service_api_keys(is_deleted);
CREATE INDEX IF NOT EXISTS idx_kb_sak_service ON knowledge_base.service_api_keys(service_id);

-- ------------------------------------------------------------
-- 知识库文档表（业务层独有，与 LightRAG 内部存储分离）
-- 状态机：pending → parsing → ready / failed
--        ready → deleting → deleted
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_base.knowledge_documents (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    file_name VARCHAR(512) NOT NULL,
    file_path VARCHAR(1024) NOT NULL,
    file_size BIGINT NOT NULL DEFAULT 0,
    mime_type VARCHAR(128),
    storage_backend VARCHAR(16) NOT NULL DEFAULT 'local',
    storage_key VARCHAR(1024) NOT NULL,
    knowledge_base_id VARCHAR(128) NOT NULL,
    status VARCHAR(32) DEFAULT 'pending' NOT NULL,
    rag_task_id VARCHAR(128),
    rag_doc_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    error_message TEXT,
    ontology_status VARCHAR(32) DEFAULT 'pending' NOT NULL,
    ontology_error TEXT,
    ontology_retry_count INTEGER NOT NULL DEFAULT 0,
    content_generation INTEGER NOT NULL DEFAULT 0,          -- reparse 代次：每次 reparse 原子递增，旧代次任务结果作废（P0-I fencing）
    ontology_target_schema_version INTEGER,                 -- 文档应归类到的目标 compiled schema 版本（新上传/reparse 必写）
    ontology_applied_schema_version INTEGER,                -- 已写入 Neo4j 的 compiled schema 版本；NULL=未知，only_outdated 视为过期
    ontology_applied_schema_hash VARCHAR(32),               -- 已应用 schema 的兜底 hash（版本号相同但内容变更时区分，可选）
    extra_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    data_source_type VARCHAR(32),          -- 文档来源方式：api / api_push / storage / file（统计按此列分组）
    folder_id VARCHAR(64),
    -- [jonex] LLM-Wiki 编译状态列化（update/010_llm_wiki_compile.sql 同款；全新库直接建列）
    -- 时区口径：llm_wiki_compile_requested_at 用 UTC（与 created_at 的本地时间口径
    -- 不同是有意的——编译超时判定按 UTC 单调时间）
    llm_wiki_compile_status VARCHAR(32),          -- NULL/compiling/compiled/stale/failed
    llm_wiki_compile_error TEXT,
    llm_wiki_compile_warnings JSONB,              -- 编译警告列表
    llm_wiki_compile_requested_at TIMESTAMP,      -- patrol 超时基准（UTC）
    llm_wiki_compiled_at TIMESTAMP,
    llm_wiki_task_id VARCHAR(128),                -- OpenKB 容器编译任务 ID（对齐 rag_task_id）
    -- [jonex] 源文件内容 hash（update/011_document_content_hash.sql 同款；全新库直接建列）
    content_hash VARCHAR(32),                     -- 源文件内容 md5（上传去重 + reparse 跳过，md5 仅比对非安全用途）
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_kb_doc_tenant_deleted
    ON knowledge_base.knowledge_documents(tenant_id, is_deleted);
CREATE INDEX IF NOT EXISTS idx_kb_doc_tenant_status
    ON knowledge_base.knowledge_documents(tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_kb_doc_tenant_ontology_status
    ON knowledge_base.knowledge_documents(tenant_id, ontology_status);
CREATE INDEX IF NOT EXISTS idx_kb_doc_tenant_created
    ON knowledge_base.knowledge_documents(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_kb_doc_tenant_kb
    ON knowledge_base.knowledge_documents(tenant_id, knowledge_base_id);
CREATE INDEX IF NOT EXISTS idx_kb_doc_rag_task
    ON knowledge_base.knowledge_documents(rag_task_id);
-- 按 KB + 来源方式 分组统计 document_count 的加速索引
CREATE INDEX IF NOT EXISTS idx_kb_doc_tenant_kb_type
    ON knowledge_base.knowledge_documents(tenant_id, knowledge_base_id, data_source_type)
    WHERE is_deleted = 0;
-- only_outdated 扫描加速：按 KB + 本体状态 + 已应用 schema 版本筛选（reparse/recompile 批量重抽）
CREATE INDEX IF NOT EXISTS idx_kb_doc_ontology_outdated
    ON knowledge_base.knowledge_documents(tenant_id, knowledge_base_id, ontology_status, ontology_applied_schema_version)
    WHERE is_deleted = 0;
-- 同 KB 内容去重加速（update/011_document_content_hash.sql 同款；全新库直接建索引）
CREATE INDEX IF NOT EXISTS idx_kb_doc_content_hash
    ON knowledge_base.knowledge_documents (tenant_id, knowledge_base_id, content_hash)
    WHERE is_deleted = 0 AND status <> 'failed';
-- LLM-Wiki 编译巡检（patrol_openkb_compile）高频查询索引：只覆盖 compiling
CREATE INDEX IF NOT EXISTS idx_kb_doc_llm_wiki_compiling
    ON knowledge_base.knowledge_documents(tenant_id, llm_wiki_compile_status)
    WHERE llm_wiki_compile_status = 'compiling';
COMMENT ON COLUMN knowledge_base.knowledge_documents.llm_wiki_compile_status
  IS 'LLM-Wiki 编译状态：NULL/compiling/compiled/stale/failed';
COMMENT ON COLUMN knowledge_base.knowledge_documents.llm_wiki_compile_error
  IS 'LLM-Wiki 编译失败原因';
COMMENT ON COLUMN knowledge_base.knowledge_documents.llm_wiki_compile_warnings
  IS 'LLM-Wiki 编译警告列表（JSONB 数组）';
COMMENT ON COLUMN knowledge_base.knowledge_documents.llm_wiki_compile_requested_at
  IS 'LLM-Wiki 编译请求时刻（patrol 超时基准，UTC）';
COMMENT ON COLUMN knowledge_base.knowledge_documents.llm_wiki_compiled_at
  IS 'LLM-Wiki 编译完成时刻';
COMMENT ON COLUMN knowledge_base.knowledge_documents.llm_wiki_task_id
  IS 'OpenKB 容器编译任务 ID（对齐 rag_task_id），供对账巡检轮询';

-- ------------------------------------------------------------
-- 检索历史表（按 tenant+user+query+knowledge_base 去重，软删除）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_base.knowledge_search_history (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    user_id VARCHAR(128) NOT NULL,
    query TEXT NOT NULL,
    query_hash VARCHAR(64) NOT NULL,
    knowledge_base_id VARCHAR(128) NOT NULL,
    domain_space_id VARCHAR(64),
    mode VARCHAR(32) NOT NULL DEFAULT 'hybrid',
    top_k INTEGER NOT NULL DEFAULT 5,
    status VARCHAR(32) NOT NULL DEFAULT 'done',
    answer_preview TEXT,
    reference_count INTEGER NOT NULL DEFAULT 0,
    result_count INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER,
    extra_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    searched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_search_history_query_kb
    ON knowledge_base.knowledge_search_history(
        tenant_id,
        user_id,
        query_hash,
        knowledge_base_id
    );
CREATE INDEX IF NOT EXISTS idx_kb_hist_tenant_user_time
    ON knowledge_base.knowledge_search_history(tenant_id, user_id, searched_at DESC);
CREATE INDEX IF NOT EXISTS idx_kb_hist_tenant_user_deleted
    ON knowledge_base.knowledge_search_history(tenant_id, user_id, is_deleted);
CREATE INDEX IF NOT EXISTS idx_kb_hist_tenant_user_kb_time
    ON knowledge_base.knowledge_search_history(tenant_id, user_id, knowledge_base_id, searched_at DESC);
CREATE INDEX IF NOT EXISTS idx_kb_hist_domain_space_id
    ON knowledge_base.knowledge_search_history(domain_space_id) WHERE domain_space_id IS NOT NULL;

-- ------------------------------------------------------------
-- KB 数据源实例表（access_type: api / api_push / storage / file）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_base.knowledge_data_sources (
    id                VARCHAR(64) PRIMARY KEY,
    tenant_id         VARCHAR(64)  NOT NULL,
    knowledge_base_id VARCHAR(128) NOT NULL,
    access_method_id  VARCHAR(64),                       -- 引用 business_domain.data_access_methods.id（只读引用）
    access_type       VARCHAR(32)  NOT NULL,             -- api / api_push / storage / file
    name              VARCHAR(255) NOT NULL,
    config_json       JSONB        NOT NULL DEFAULT '{}', -- 连接配置；凭据字段存密文
    sync_mode         VARCHAR(16)  NOT NULL DEFAULT 'manual',  -- manual / scheduled（一期仅 manual）
    cron_expr         VARCHAR(128),
    schedule_task_id  INTEGER,
    status            VARCHAR(32)  NOT NULL DEFAULT 'active',   -- active / paused / error
    last_sync_at      TIMESTAMPTZ,
    last_sync_status  VARCHAR(32),                       -- success / failed / running
    last_sync_message TEXT,
    document_count    INTEGER      NOT NULL DEFAULT 0,
    is_deleted        SMALLINT     NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_kb_data_sources_kb
    ON knowledge_base.knowledge_data_sources (tenant_id, knowledge_base_id, is_deleted);
-- 每个 KB 至多一个有效的 file（文件上传）数据源：归属与统计无歧义、并发兜底
CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_ds_file_per_kb
    ON knowledge_base.knowledge_data_sources (tenant_id, knowledge_base_id)
    WHERE access_type = 'file' AND is_deleted = 0;

-- ------------------------------------------------------------
-- 知识库-模板绑定表：记录每个 KB 绑定的模板领域和场景
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_base.ontology_template_bindings (
    id                   BIGSERIAL       PRIMARY KEY,
    tenant_id            VARCHAR(64)     NOT NULL,
    knowledge_base_id    VARCHAR(128)    NOT NULL,
    template_domain_id   VARCHAR(64),
    template_scenario_id VARCHAR(64),
    source_type          VARCHAR(32)     NOT NULL DEFAULT 'business_template',
    status               VARCHAR(32)     NOT NULL DEFAULT 'active',
    created_at           TIMESTAMPTZ     NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ     NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, knowledge_base_id)
);
CREATE INDEX IF NOT EXISTS idx_otb_tenant_template
    ON knowledge_base.ontology_template_bindings (tenant_id, template_domain_id, template_scenario_id);

-- ------------------------------------------------------------
-- 知识库级本体编译快照：缓存从业务模板编译出的 ontology schema
-- （含 KB 侧可编辑状态字段 schema_mode / sync_status / edited_*）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_base.ontology_compiled_schemas (
    id                    BIGSERIAL       PRIMARY KEY,
    tenant_id             VARCHAR(64)     NOT NULL,
    knowledge_base_id     VARCHAR(128)    NOT NULL,
    template_domain_id    VARCHAR(64),
    template_scenario_id  VARCHAR(64),
    source_type           VARCHAR(32)     NOT NULL DEFAULT 'business_template',
    source_version        INTEGER         NOT NULL DEFAULT 1,
    source_hash           VARCHAR(64),
    schema_version        INTEGER         NOT NULL DEFAULT 1,
    entity_types          JSONB           NOT NULL DEFAULT '[]'::jsonb,
    relation_types        JSONB           NOT NULL DEFAULT '[]'::jsonb,
    constraints           JSONB           NOT NULL DEFAULT '[]'::jsonb,
    disambiguation        JSONB           NOT NULL DEFAULT '{"case_insensitive": true, "alias_merge": true}'::jsonb,
    prompt_schema         JSONB           NOT NULL DEFAULT '{}'::jsonb,
    schema_mode           VARCHAR(32)     NOT NULL DEFAULT 'template_seeded',  -- template_seeded / manual_edited
    sync_status           VARCHAR(32)     NOT NULL DEFAULT 'synced',           -- synced / outdated
    edited_at             TIMESTAMPTZ,
    edited_by             VARCHAR(128),
    status                VARCHAR(32)     NOT NULL DEFAULT 'active',
    compiled_at           TIMESTAMPTZ     NOT NULL DEFAULT now(),
    created_at            TIMESTAMPTZ     NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ     NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, knowledge_base_id)
);
CREATE INDEX IF NOT EXISTS idx_ocs_tenant_kb
    ON knowledge_base.ontology_compiled_schemas (tenant_id, knowledge_base_id);
CREATE INDEX IF NOT EXISTS idx_ocs_source
    ON knowledge_base.ontology_compiled_schemas (tenant_id, template_domain_id, template_scenario_id, source_hash);

-- ------------------------------------------------------------
-- 知识库级同义词组（KB 级、独立于具体实体的等价词组）
-- 用途：知识编译与查询时把同义词统一映射为标准实体（本期仅存储 + 管理）
-- 时间列用不带时区的 timestamp，与 ORM TimestampMixin(DateTime + datetime.utcnow) 一致
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_base.ontology_synonyms (
    id                VARCHAR(64) PRIMARY KEY,
    tenant_id         VARCHAR(64) NOT NULL,
    knowledge_base_id VARCHAR(128) NOT NULL,
    terms             JSONB NOT NULL DEFAULT '[]'::jsonb,   -- string[]，同义词列表
    canonical         VARCHAR(255),                          -- 标准词，默认取 terms[0]
    created_at        TIMESTAMP NOT NULL DEFAULT now(),
    updated_at        TIMESTAMP NOT NULL DEFAULT now(),
    is_deleted        SMALLINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_kb_synonyms_tenant_kb
    ON knowledge_base.ontology_synonyms (tenant_id, knowledge_base_id, is_deleted);

-- ============================================================
-- 悦溪平台数据库迁移 - 知识库解析引擎设置
-- 版本: 008
-- 包含: knowledge_base.knowledge_parser_settings 表
-- 说明: KB 级文件类解析配置，引用 business_domain.parser_configs
-- ============================================================

CREATE TABLE IF NOT EXISTS knowledge_base.knowledge_parser_settings (
    id                         VARCHAR(64) PRIMARY KEY,
    tenant_id                  VARCHAR(64)  NOT NULL,
    knowledge_base_id          VARCHAR(128) NOT NULL,
    parser_type                VARCHAR(64)  NOT NULL,
    parser_config_id           VARCHAR(64),
    prompt_config_id           VARCHAR(64),                       -- 主解析提示词在 atomic-rag 的 prompt 配置 id（下发 prompt_ids 用；空=未关联）
    preprocessing_json         JSONB        NOT NULL DEFAULT '[]'::jsonb,
    postprocessing_json        JSONB        NOT NULL DEFAULT '[]'::jsonb,
    prompt_text                TEXT,
    prompt_template_id         VARCHAR(64),
    prompt_template_version    VARCHAR(32),
    summary_prompt_text        TEXT,
    summary_template_id        VARCHAR(64),
    summary_template_version   VARCHAR(32),
    tag_prompt_text            TEXT,
    tag_template_id            VARCHAR(64),
    tag_template_version       VARCHAR(32),
    status                     VARCHAR(32)  NOT NULL DEFAULT 'active',
    is_deleted                 SMALLINT     NOT NULL DEFAULT 0,
    created_at                 TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at                 TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_kps_kb
    ON knowledge_base.knowledge_parser_settings (tenant_id, knowledge_base_id, is_deleted);

CREATE INDEX IF NOT EXISTS idx_kps_parser
    ON knowledge_base.knowledge_parser_settings (tenant_id, parser_config_id)
    WHERE parser_config_id IS NOT NULL AND is_deleted = 0;

CREATE UNIQUE INDEX IF NOT EXISTS uq_kps_parser_type_per_kb
    ON knowledge_base.knowledge_parser_settings (tenant_id, knowledge_base_id, parser_type)
    WHERE is_deleted = 0;

COMMENT ON TABLE knowledge_base.knowledge_parser_settings IS
'KB 级解析引擎设置表：每个知识库按解析器类目(parser_type)绑定一个 parser_config，并保存提示词与模板来源快照';
COMMENT ON COLUMN knowledge_base.knowledge_parser_settings.parser_type IS
'解析器类目（= business_domain.parser_configs.parser_type，如 document/video/audio/image/txt/web/cad），KB 内每类唯一，作为上传路由键';
COMMENT ON COLUMN knowledge_base.knowledge_parser_settings.parser_config_id IS
'引用 business_domain.parser_configs.id，表示该类目下选中的解析器定义；上传时按文件后缀匹配其 file_types 命中';
COMMENT ON COLUMN knowledge_base.knowledge_parser_settings.prompt_template_id IS
'主解析提示词来源模板 ID，仅用于追溯；实际运行使用 prompt_text 快照';
COMMENT ON COLUMN knowledge_base.knowledge_parser_settings.summary_template_id IS
'自动摘要提示词来源模板 ID，仅用于追溯；实际运行使用 summary_prompt_text 快照';
COMMENT ON COLUMN knowledge_base.knowledge_parser_settings.tag_template_id IS
'自动标签提示词来源模板 ID，仅用于追溯；实际运行使用 tag_prompt_text 快照';

-- ------------------------------------------------------------
-- 文件夹（一层层级，无嵌套）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_base.folders (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    knowledge_base_id VARCHAR(128) NOT NULL,
    name VARCHAR(255) NOT NULL,
    is_preset BOOLEAN NOT NULL DEFAULT FALSE,
    sort_order INTEGER DEFAULT 0,
    created_by VARCHAR(64),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_kb_folders_tenant_kb
    ON knowledge_base.folders (tenant_id, knowledge_base_id, is_deleted);
CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_folders_name_per_kb
    ON knowledge_base.folders (tenant_id, knowledge_base_id, name)
    WHERE is_deleted = 0;

-- knowledge_documents.folder_id 查询索引（列已于建表时创建）
CREATE INDEX IF NOT EXISTS idx_kb_doc_folder
    ON knowledge_base.knowledge_documents (tenant_id, knowledge_base_id, folder_id)
    WHERE is_deleted = 0 AND folder_id IS NOT NULL;
COMMENT ON TABLE knowledge_base.folders IS 'KB 级一层文件夹，用户可按文件夹组织文档。预设文件夹（is_preset=TRUE）按中文拼音排序，用户新建文件夹按创建时间降序排在预设之后。';

CREATE TABLE IF NOT EXISTS knowledge_base.knowledge_search_feedback (
    id              VARCHAR(64) PRIMARY KEY,
    tenant_id       VARCHAR(64) NOT NULL,
    user_id         VARCHAR(128) NOT NULL,
    session_id      VARCHAR(128) NOT NULL,
    query           TEXT NOT NULL,
    answer_preview  TEXT,
    knowledge_base_id       VARCHAR(128) NOT NULL,
    knowledge_base_name     VARCHAR(256),
    feedback_type   VARCHAR(16) NOT NULL,
    adopted         BOOLEAN NOT NULL DEFAULT FALSE,
    searched_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_deleted      INTEGER NOT NULL DEFAULT 0
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_kb_feedback_tenant_kb_time
    ON knowledge_base.knowledge_search_feedback (tenant_id, knowledge_base_id, searched_at);

CREATE INDEX IF NOT EXISTS idx_kb_feedback_tenant_user
    ON knowledge_base.knowledge_search_feedback (tenant_id, user_id);

CREATE INDEX IF NOT EXISTS idx_kb_feedback_session
    ON knowledge_base.knowledge_search_feedback (session_id);

CREATE INDEX IF NOT EXISTS idx_kb_feedback_user_id
    ON knowledge_base.knowledge_search_feedback (user_id);

-- ------------------------------------------------------------
-- 标签（KB 级，同 KB 内名称唯一）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_base.tags (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    knowledge_base_id VARCHAR(128) NOT NULL,
    name VARCHAR(255) NOT NULL,
    color VARCHAR(32),
    created_by VARCHAR(64),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_kb_tags_tenant_kb
    ON knowledge_base.tags (tenant_id, knowledge_base_id, is_deleted);
CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_tags_name_per_kb
    ON knowledge_base.tags (tenant_id, knowledge_base_id, name)
    WHERE is_deleted = 0;
COMMENT ON TABLE knowledge_base.tags IS 'KB 级标签，支持文档分类与筛选。同知识库内标签名称唯一。';

-- ------------------------------------------------------------
-- 文档-标签多对多关联
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_base.document_tags (
    document_id VARCHAR(64) NOT NULL REFERENCES knowledge_base.knowledge_documents(id) ON DELETE CASCADE,
    tag_id VARCHAR(64) NOT NULL REFERENCES knowledge_base.tags(id) ON DELETE CASCADE,
    PRIMARY KEY (document_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_kb_doc_tags_tag
    ON knowledge_base.document_tags (tag_id);
COMMENT ON TABLE knowledge_base.document_tags IS '文档与标签的多对多关联，级联删除。';
