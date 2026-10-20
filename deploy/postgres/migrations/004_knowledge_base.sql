-- ============================================================
-- Jonex 平台数据库初始化 - 知识库 (knowledge_base schema)
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
    role VARCHAR(32) NOT NULL DEFAULT 'member',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT DEFAULT 0,
    CONSTRAINT ck_kb_spp_role CHECK (role IN ('space_manager', 'member'))
);
CREATE INDEX IF NOT EXISTS idx_kb_spp_tenant ON knowledge_base.space_permissions(tenant_id);
CREATE INDEX IF NOT EXISTS idx_kb_spp_is_deleted ON knowledge_base.space_permissions(is_deleted);
CREATE INDEX IF NOT EXISTS idx_kb_spp_space ON knowledge_base.space_permissions(space_id);
CREATE INDEX IF NOT EXISTS idx_kb_spp_user ON knowledge_base.space_permissions(user_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_kb_spp_unique_member ON knowledge_base.space_permissions(tenant_id, space_id, user_id) WHERE is_deleted = 0;

-- 知识库授权成员（KB 级叠加授权：kb_manager/member，设计 2026-08-20-kb-permission-design）
CREATE TABLE IF NOT EXISTS knowledge_base.kb_permissions (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    kb_id VARCHAR(64) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    role VARCHAR(32) NOT NULL DEFAULT 'member',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT DEFAULT 0,
    CONSTRAINT ck_kb_kbp_role CHECK (role IN ('kb_manager', 'member'))
);
CREATE INDEX IF NOT EXISTS idx_kb_perms_tenant ON knowledge_base.kb_permissions(tenant_id);
CREATE INDEX IF NOT EXISTS idx_kb_perms_is_deleted ON knowledge_base.kb_permissions(is_deleted);
CREATE INDEX IF NOT EXISTS idx_kb_perms_kb ON knowledge_base.kb_permissions(kb_id);
CREATE INDEX IF NOT EXISTS idx_kb_perms_user ON knowledge_base.kb_permissions(user_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_kb_perms_unique_member ON knowledge_base.kb_permissions(tenant_id, kb_id, user_id) WHERE is_deleted = 0;

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
    kb_type VARCHAR(16) NOT NULL DEFAULT 'lightrag',  -- [jonex] 知识库类型 lightrag / openkb / jonex_wiki
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_kb_tenant ON knowledge_base.knowledge_info(tenant_id);
CREATE INDEX IF NOT EXISTS idx_kb_is_deleted ON knowledge_base.knowledge_info(is_deleted);
CREATE INDEX IF NOT EXISTS idx_kb_space ON knowledge_base.knowledge_info(space_id);
-- [jonex] 同领域空间内知识库不可重名；软删记录不占名（与 services / folders / tags 同口径）。
-- 应用层同口径校验见 KnowledgeInfoService._ensure_name_unique；存量库由
-- deploy/postgres/update/055_knowledge_info_name_unique.sql 补建同名索引。
CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_info_name_per_space
    ON knowledge_base.knowledge_info (tenant_id, space_id, name)
    WHERE is_deleted = 0;

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
CREATE UNIQUE INDEX IF NOT EXISTS idx_kb_svc_unique_name
    ON knowledge_base.services (tenant_id, space_id, name)
    WHERE is_deleted = 0;

-- 领域服务-知识库关联
CREATE TABLE IF NOT EXISTS knowledge_base.service_knowledge_bases (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    service_id VARCHAR(64) NOT NULL,
    kb_id VARCHAR(64) NOT NULL,
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
    -- [jonex] LLM-Wiki Schema 版本 fencing（update/013_llm_wiki_schema_columns.sql 同款；全新库直接建列）
    llm_wiki_target_schema_version INTEGER,       -- 本次应使用的 LLM-Wiki Schema 版本（claim 时写入）
    llm_wiki_applied_schema_version INTEGER,      -- 实际完成编译的版本（patrol completed 回写）
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
-- LLM-Wiki Schema 过期文档扫描（update/013_llm_wiki_schema_columns.sql 同款；全新库直接建索引）
CREATE INDEX IF NOT EXISTS idx_kb_doc_llm_wiki_schema_outdated
    ON knowledge_base.knowledge_documents (tenant_id, knowledge_base_id)
    WHERE is_deleted = 0
      AND llm_wiki_applied_schema_version IS DISTINCT FROM llm_wiki_target_schema_version;
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
-- 检索历史表（按 tenant+user 隔离，软删除；同 query 可保留多条，超出 200 由 save 链路 trim 清理）
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
    -- [jonex] 检索历史快照列：点击历史直接展示历史结果，不重新检索
    -- answer      完整答案原始文本（含 <think> 标记，展示时前端 parseThink/parseReferences 解析）
    -- references  引用快照（JSONB，落库前剥离过期的 raw_url，展示时经 resolve 端点重新富化）
    -- reasoning   推理链快照（ReasoningTrace: steps/final_source/total_ms）
    answer TEXT,
    "references" JSONB NOT NULL DEFAULT '[]'::jsonb,
    reasoning JSONB,
    searched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT NOT NULL DEFAULT 0
);

-- [jonex] 追加策略：不建 (tenant_id,user_id,query_hash,knowledge_base_id) 唯一索引，
-- 同一问题可保留多条历史（新结果不覆盖旧记录），超 200 条由 save 链路 trim_for_user 清理。
CREATE INDEX IF NOT EXISTS idx_kb_hist_tenant_user_time
    ON knowledge_base.knowledge_search_history(tenant_id, user_id, searched_at DESC);
CREATE INDEX IF NOT EXISTS idx_kb_hist_tenant_user_deleted
    ON knowledge_base.knowledge_search_history(tenant_id, user_id, is_deleted);
CREATE INDEX IF NOT EXISTS idx_kb_hist_tenant_user_kb_time
    ON knowledge_base.knowledge_search_history(tenant_id, user_id, knowledge_base_id, searched_at DESC);
CREATE INDEX IF NOT EXISTS idx_kb_hist_domain_space_id
    ON knowledge_base.knowledge_search_history(domain_space_id) WHERE domain_space_id IS NOT NULL;

-- ------------------------------------------------------------
-- [jonex] 以图问知识：查询图片表 + 历史-图片关联表
-- 设计：docs/knowledge-base/image-query-search-execution-plan.md §4-B11
-- 存量库增量脚本：deploy/postgres/update/045_kb_query_images.sql
-- 历史表本身一列不加；图片的存活判据是「至少还有一条未删除的历史引用它」
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_base.knowledge_query_images (
    id               VARCHAR(64)  PRIMARY KEY,
    tenant_id        VARCHAR(64)  NOT NULL,
    user_id          VARCHAR(128) NOT NULL,
    storage_backend  VARCHAR(16)  NOT NULL,          -- local / cos / s3（落盘时的全局后端）
    storage_key      VARCHAR(512) NOT NULL,
    mime_type        VARCHAR(64)  NOT NULL,          -- magic bytes 判定的真实类型
    file_size        INTEGER      NOT NULL,
    sha256           VARCHAR(64)  NOT NULL,
    recognized_query TEXT,
    description      TEXT,
    ocr_text         TEXT,
    entities         JSONB        NOT NULL DEFAULT '[]'::jsonb,
    prompt_version   VARCHAR(16),
    vlm_model        VARCHAR(128),
    purged_at        TIMESTAMP,                      -- 被判定无引用、软删的时刻
    object_deleted   SMALLINT     NOT NULL DEFAULT 0, -- 软删后对象已删除为 1；删失败保持 0，兜底扫描补删
    created_at       TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,  -- 最后一次被引用
    is_deleted       SMALLINT     NOT NULL DEFAULT 0
);

-- 同用户同图只存一份（并发落盘靠它兜底：插入撞唯一索引 → 改用已有行）
CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_qimg_tenant_user_sha
    ON knowledge_base.knowledge_query_images (tenant_id, user_id, sha256)
    WHERE is_deleted = 0;
CREATE INDEX IF NOT EXISTS idx_kb_qimg_sweep
    ON knowledge_base.knowledge_query_images (updated_at)
    WHERE is_deleted = 0;
CREATE INDEX IF NOT EXISTS idx_kb_qimg_pending_object_delete
    ON knowledge_base.knowledge_query_images (purged_at)
    WHERE is_deleted = 1 AND object_deleted = 0;

-- 检索历史 ⇄ 查询图片 多对多（关联行不做软删：有效性以历史行 is_deleted 为准）
CREATE TABLE IF NOT EXISTS knowledge_base.knowledge_search_history_images (
    history_id  VARCHAR(64)  NOT NULL,
    image_id    VARCHAR(64)  NOT NULL,
    tenant_id   VARCHAR(64)  NOT NULL,
    user_id     VARCHAR(128) NOT NULL,
    sort_order  SMALLINT     NOT NULL DEFAULT 0,     -- 用户附图顺序
    created_at  TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (history_id, image_id)
);
CREATE INDEX IF NOT EXISTS idx_kb_hist_img_image
    ON knowledge_base.knowledge_search_history_images (image_id);
CREATE INDEX IF NOT EXISTS idx_kb_hist_img_history
    ON knowledge_base.knowledge_search_history_images (history_id);

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
-- Jonex 平台数据库迁移 - 知识库解析引擎设置
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
-- 回答级反馈（answer feedback）：按问答记录 history_id 锚定
-- 主表保存最新反馈 + 上下文快照；事件表只增不改（operation_id 幂等）
-- 锚点 history_id = knowledge_base.knowledge_search_history.id
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_base.knowledge_answer_feedback (
    id              VARCHAR(64) PRIMARY KEY,
    tenant_id       VARCHAR(64) NOT NULL,
    user_id         VARCHAR(128) NOT NULL,
    history_id      VARCHAR(64) NOT NULL,
    query           TEXT NOT NULL,
    answer          TEXT,
    feedback_type   VARCHAR(16) NOT NULL,
    feedback_reason VARCHAR(32),
    feedback_comment VARCHAR(300),
    domain_space_id VARCHAR(64),
    knowledge_base_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    source          VARCHAR(64),
    mode            VARCHAR(32),
    source_missing_reason VARCHAR(64),
    version         INTEGER NOT NULL DEFAULT 0,
    adopted         BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_deleted      SMALLINT NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_answer_feedback_history
    ON knowledge_base.knowledge_answer_feedback (history_id);
CREATE INDEX IF NOT EXISTS idx_kb_answer_feedback_tenant_user
    ON knowledge_base.knowledge_answer_feedback (tenant_id, user_id);
CREATE INDEX IF NOT EXISTS idx_kb_answer_feedback_tenant_time
    ON knowledge_base.knowledge_answer_feedback (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_kb_answer_feedback_kb_ids
    ON knowledge_base.knowledge_answer_feedback USING GIN (knowledge_base_ids);

CREATE TABLE IF NOT EXISTS knowledge_base.knowledge_answer_feedback_event (
    id              VARCHAR(64) PRIMARY KEY,
    tenant_id       VARCHAR(64) NOT NULL,
    history_id      VARCHAR(64) NOT NULL,
    feedback_id     VARCHAR(64),
    user_id         VARCHAR(128) NOT NULL,
    operation_id    VARCHAR(64) NOT NULL,
    version         INTEGER NOT NULL,
    feedback_type   VARCHAR(16),
    feedback_reason VARCHAR(32),
    feedback_comment VARCHAR(300),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_answer_feedback_event_operation
    ON knowledge_base.knowledge_answer_feedback_event (operation_id);
CREATE INDEX IF NOT EXISTS idx_kb_answer_feedback_event_history
    ON knowledge_base.knowledge_answer_feedback_event (history_id, version DESC);

COMMENT ON TABLE knowledge_base.knowledge_answer_feedback IS '检索问答用户反馈主记录：按问答记录 history_id 唯一，保存最新反馈状态与上下文快照';
COMMENT ON TABLE knowledge_base.knowledge_answer_feedback_event IS '检索问答用户反馈变更事件：只增不改，operation_id 幂等，保留每次变更';

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

-- ------------------------------------------------------------
-- LLM-Wiki Schema 编译设置表（llm-wiki 类型 kb_type=openkb/jonex_wiki 的编译配置权威源）
-- 方案：docs/llmwiki-schema-settings-execution-plan.md
-- 留档模型：每次保存递增 schema_version，旧 active 行转 archived；
-- partial unique index 只约束 active（每 KB 单 active），archived 留历史。
-- sync_status 与 status 分开：status 是留档语义，sync_status 是 apply_schema
-- 同步状态（synced / apply_failed）。
-- 时间列用 TIMESTAMP（naive），与 knowledge_base 域内现状一致。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_base.llm_wiki_schemas (
    id BIGSERIAL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    knowledge_base_id VARCHAR(128) NOT NULL,

    schema_version INTEGER NOT NULL DEFAULT 1,
    status VARCHAR(32) NOT NULL DEFAULT 'active',   -- active / archived
    sync_status VARCHAR(32) NOT NULL DEFAULT 'synced',  -- synced / apply_failed

    schema_name VARCHAR(128) NOT NULL DEFAULT 'default',
    language VARCHAR(32) NOT NULL DEFAULT 'zh-CN',
    model VARCHAR(128),

    entity_types JSONB NOT NULL DEFAULT '[]'::jsonb,
    concept_types JSONB NOT NULL DEFAULT '[]'::jsonb,  -- 概念类型词表（AGENTS.md 引导，无硬校验）
    -- [jonex] 用户自定义追加块（多行 markdown，渲染时原样拼到 AGENTS.md 末尾）
    -- 页面规则/编译规则等均由该块承载
    agents_md_extra TEXT NOT NULL DEFAULT '',

    agents_md TEXT NOT NULL,
    config_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,

    edited_by VARCHAR(128),
    edited_at TIMESTAMP,
    applied_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 每个 KB 最多一条 active（部分唯一索引）；历史版本留档为 archived 不受限
CREATE UNIQUE INDEX IF NOT EXISTS uq_llm_wiki_schema_active
    ON knowledge_base.llm_wiki_schemas (tenant_id, knowledge_base_id)
    WHERE status = 'active';

-- active 查询加速（编译触发点/保存 CAS 主路径）
CREATE INDEX IF NOT EXISTS idx_llm_wiki_schema_kb
    ON knowledge_base.llm_wiki_schemas (tenant_id, knowledge_base_id, schema_version);

-- ------------------------------------------------------------
-- 数据运营事件表（检索/处理链路的埋点，append-only 事实表）
-- 不承载业务 CRUD：由检索/处理链路落库，由 knowledge-base
-- AnalyticsService 聚合查询。id 沿用知识库服务统一约定：
-- VARCHAR(64) + Python 端 uuid4。
-- 方案：原先独立成 008_search_events.sql，后并入本文件——
--       六张表全在 knowledge_base schema 内，无独立执行时机，
--       且 008 不在 init.sql 的 \ir 清单里（手动初始化会漏建）。
-- 存量库增量脚本：deploy/postgres/update/032_search_events.sql
-- ------------------------------------------------------------

-- 1. 检索请求接收事件（每次检索调用的入口记录，含失败/中断的请求）
CREATE TABLE IF NOT EXISTS knowledge_base.search_request_event (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    user_id VARCHAR(128),
    request_id VARCHAR(64) NOT NULL,
    entry_service_id VARCHAR(64),
    domain_space_id VARCHAR(64),
    query TEXT,
    query_hash VARCHAR(64),
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sre_tenant_time ON knowledge_base.search_request_event (tenant_id, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_sre_request ON knowledge_base.search_request_event (request_id);

-- 2. 检索步骤事件（一次检索内各推理/检索步骤，stage 对齐 ReasoningStep 的 STAGE_*/STATUS_*）
CREATE TABLE IF NOT EXISTS knowledge_base.search_step_event (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    request_id VARCHAR(64) NOT NULL,
    stage VARCHAR(32) NOT NULL,
    title VARCHAR(128),
    status VARCHAR(16) NOT NULL,
    summary TEXT,
    detail JSONB,
    duration_ms INTEGER,
    knowledge_base_ids JSONB NOT NULL DEFAULT '[]',
    service_id VARCHAR(64),
    searched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sse_tenant_time ON knowledge_base.search_step_event (tenant_id, searched_at DESC);
CREATE INDEX IF NOT EXISTS idx_sse_request ON knowledge_base.search_step_event (request_id);

-- 3. 检索命中事件（检索链路实际命中的来源，request_id + target_id 去重）
CREATE TABLE IF NOT EXISTS knowledge_base.search_hit_event (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    request_id VARCHAR(64) NOT NULL,
    -- 32 而非 16：写入器对本体命中事件写 'ontology_instance'（17 字符）；
    -- 16 会让 INSERT 报 value too long，连带回滚同一事务里的步骤/命中/采用事件。
    target_type VARCHAR(32) NOT NULL,
    target_id VARCHAR(128) NOT NULL,
    entity_type VARCHAR(64),
    canonical_name VARCHAR(256),
    kb_id VARCHAR(128),
    hit_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_she_tenant_time ON knowledge_base.search_hit_event (tenant_id, hit_at DESC);
CREATE INDEX IF NOT EXISTS idx_she_request ON knowledge_base.search_hit_event (request_id);
CREATE INDEX IF NOT EXISTS idx_she_target ON knowledge_base.search_hit_event (target_type, target_id);

-- 4. 回答采用事件（最终答案实际引用的来源）
CREATE TABLE IF NOT EXISTS knowledge_base.search_adoption_event (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    answer_id VARCHAR(64) NOT NULL,
    request_id VARCHAR(64),
    -- 同 search_hit_event.target_type：32 而非 16（'ontology_instance' 17 字符）
    target_type VARCHAR(32) NOT NULL,
    target_id VARCHAR(128) NOT NULL,
    entity_type VARCHAR(64),
    canonical_name VARCHAR(256),
    kb_id VARCHAR(128),
    adopted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sae_tenant_time ON knowledge_base.search_adoption_event (tenant_id, adopted_at DESC);
CREATE INDEX IF NOT EXISTS idx_sae_answer ON knowledge_base.search_adoption_event (answer_id);
CREATE INDEX IF NOT EXISTS idx_sae_target ON knowledge_base.search_adoption_event (target_type, target_id);

-- 5. 资产状态事件（文档资产状态流转的变更记录）
CREATE TABLE IF NOT EXISTS knowledge_base.asset_status_event (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    asset_id VARCHAR(64) NOT NULL,
    kb_id VARCHAR(128),
    status VARCHAR(32) NOT NULL,
    previous_status VARCHAR(32),
    reason TEXT,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ase_tenant_time ON knowledge_base.asset_status_event (tenant_id, changed_at DESC);
CREATE INDEX IF NOT EXISTS idx_ase_asset ON knowledge_base.asset_status_event (asset_id);

-- 6. 处理任务事件（解析/本体抽取/编译等处理任务的执行记录）
CREATE TABLE IF NOT EXISTS knowledge_base.processing_task_event (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    processing_task_id VARCHAR(128) NOT NULL,
    asset_id VARCHAR(64) NOT NULL,
    kb_id VARCHAR(128),
    task_type VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL,
    error TEXT,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT NOT NULL DEFAULT 0,
    CONSTRAINT uq_pte_task UNIQUE (tenant_id, processing_task_id)
);
CREATE INDEX IF NOT EXISTS idx_pte_tenant_time ON knowledge_base.processing_task_event (tenant_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_pte_asset ON knowledge_base.processing_task_event (asset_id);
