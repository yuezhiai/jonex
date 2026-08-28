-- ============================================================
-- 悦溪平台数据库初始化 - 业务领域 (business_domain schema)
-- 版本: 005
-- 包含：引擎管理（数据接入/解析器/模型）、领域空间、领域服务
--       领域服务 API Key 管理、生态适配器、技能管理、
--       模板系统（领域/场景/对象/属性/关系）
-- ============================================================

-- 引擎管理 - 数据接入方式
CREATE TABLE IF NOT EXISTS business_domain.data_access_methods (
    id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    access_type VARCHAR(32) NOT NULL,
    config_json JSONB DEFAULT '{}'::jsonb,
    status VARCHAR(32) DEFAULT 'active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_dam_is_deleted ON business_domain.data_access_methods(is_deleted);

-- 引擎管理 - 解析器配置
CREATE TABLE IF NOT EXISTS business_domain.parser_configs (
    id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    parser_type VARCHAR(32) NOT NULL,
    file_types JSONB DEFAULT '[]'::jsonb,
    config_json JSONB DEFAULT '{}'::jsonb,
    status VARCHAR(32) DEFAULT 'active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pc_is_deleted ON business_domain.parser_configs(is_deleted);

-- 引擎管理 - 模型提供商
CREATE TABLE IF NOT EXISTS business_domain.model_providers (
    id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    provider_type VARCHAR(32) NOT NULL,
    model_type VARCHAR(32),
    endpoint VARCHAR(512),
    api_key_encrypted VARCHAR(512),
    model_name VARCHAR(255),
    vector_dimension INTEGER,
    token_limit INTEGER,
    latency_ms INTEGER,
    call_count INTEGER DEFAULT 0,
    success_rate INTEGER DEFAULT 0,
    status VARCHAR(32) DEFAULT 'active',
    config_json JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_mp_is_deleted ON business_domain.model_providers(is_deleted);

-- 生态适配器
CREATE TABLE IF NOT EXISTS business_domain.adapters (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    name VARCHAR(255) NOT NULL,
    adapter_type VARCHAR(32) NOT NULL,
    config_json JSONB DEFAULT '{}'::jsonb,
    status VARCHAR(32) DEFAULT 'disconnected',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_adp_tenant ON business_domain.adapters(tenant_id);
CREATE INDEX IF NOT EXISTS idx_adp_is_deleted ON business_domain.adapters(is_deleted);

-- 技能管理 — skill_catalog 平台共享目录（无租户）
CREATE TABLE IF NOT EXISTS business_domain.skill_catalog (
    id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    category VARCHAR(64) NOT NULL DEFAULT 'custom',
    icon VARCHAR(64),
    status VARCHAR(32) NOT NULL DEFAULT 'published',

    tool_name VARCHAR(128) NOT NULL UNIQUE,
    instruction TEXT NOT NULL,
    input_schema_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    output_schema_json JSONB NOT NULL DEFAULT '{}'::jsonb,

    artifact_bucket VARCHAR(128) NOT NULL,
    artifact_object_key VARCHAR(1024) NOT NULL,
    artifact_checksum VARCHAR(128),
    artifact_size BIGINT,
    artifact_content_type VARCHAR(128) DEFAULT 'application/zip',

    tags_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    capability_json JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_skill_catalog_status ON business_domain.skill_catalog(status);
CREATE INDEX IF NOT EXISTS idx_skill_catalog_category ON business_domain.skill_catalog(category);
CREATE INDEX IF NOT EXISTS idx_skill_catalog_is_deleted ON business_domain.skill_catalog(is_deleted);

-- 技能管理 — tenant_skills 租户启用状态（有租户）
CREATE TABLE IF NOT EXISTS business_domain.tenant_skills (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    skill_id VARCHAR(64) NOT NULL REFERENCES business_domain.skill_catalog(id),
    status VARCHAR(32) NOT NULL DEFAULT 'disabled',

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT DEFAULT 0,

    UNIQUE (tenant_id, skill_id)
);
CREATE INDEX IF NOT EXISTS idx_tenant_skills_tenant ON business_domain.tenant_skills(tenant_id);
CREATE INDEX IF NOT EXISTS idx_tenant_skills_skill ON business_domain.tenant_skills(skill_id);
CREATE INDEX IF NOT EXISTS idx_tenant_skills_status ON business_domain.tenant_skills(status);
CREATE INDEX IF NOT EXISTS idx_tenant_skills_is_deleted ON business_domain.tenant_skills(is_deleted);

-- 模板领域
CREATE TABLE IF NOT EXISTS business_domain.template_domains (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    name VARCHAR(255) NOT NULL,
    name_en VARCHAR(255),
    description TEXT,
    status VARCHAR(32) DEFAULT 'inactive',
    version INTEGER DEFAULT 1,
    published_at TIMESTAMPTZ,
    structure_hash VARCHAR(64),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_td_tenant ON business_domain.template_domains(tenant_id);
CREATE INDEX IF NOT EXISTS idx_td_is_deleted ON business_domain.template_domains(is_deleted);

-- 模板场景
CREATE TABLE IF NOT EXISTS business_domain.template_scenarios (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    domain_id VARCHAR(64) NOT NULL,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    config_json JSONB DEFAULT '{}'::jsonb,
    version INTEGER DEFAULT 1,
    published_at TIMESTAMPTZ,
    structure_hash VARCHAR(64),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ts_tenant ON business_domain.template_scenarios(tenant_id);
CREATE INDEX IF NOT EXISTS idx_ts_is_deleted ON business_domain.template_scenarios(is_deleted);
CREATE INDEX IF NOT EXISTS idx_ts_domain ON business_domain.template_scenarios(domain_id);

-- 模板对象
CREATE TABLE IF NOT EXISTS business_domain.template_objects (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    domain_id VARCHAR(64) NOT NULL,
    scenario_id VARCHAR(64) NOT NULL,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    status VARCHAR(32) DEFAULT 'draft',
    ontology_code VARCHAR(128),
    aliases JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tobj_tenant ON business_domain.template_objects(tenant_id);
CREATE INDEX IF NOT EXISTS idx_tobj_is_deleted ON business_domain.template_objects(is_deleted);
CREATE INDEX IF NOT EXISTS idx_tobj_domain ON business_domain.template_objects(domain_id);
CREATE INDEX IF NOT EXISTS idx_tobj_scenario ON business_domain.template_objects(scenario_id);

-- 模板属性
CREATE TABLE IF NOT EXISTS business_domain.template_attributes (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    template_object_id VARCHAR(64) NOT NULL,
    attr_name VARCHAR(255) NOT NULL,
    description TEXT,
    attr_type VARCHAR(64) DEFAULT 'string',
    is_primary_key SMALLINT DEFAULT 0,
    constraints_json JSONB DEFAULT '{}'::jsonb,
    sort_order INTEGER DEFAULT 0,
    ontology_code VARCHAR(128),
    is_required SMALLINT DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ta_tenant ON business_domain.template_attributes(tenant_id);
CREATE INDEX IF NOT EXISTS idx_ta_is_deleted ON business_domain.template_attributes(is_deleted);
CREATE INDEX IF NOT EXISTS idx_ta_object ON business_domain.template_attributes(template_object_id);

-- 模板关系
CREATE TABLE IF NOT EXISTS business_domain.template_relations (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    domain_id VARCHAR(64) NOT NULL,
    scenario_id VARCHAR(64) NOT NULL,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    source_object_id VARCHAR(64),
    target_object_id VARCHAR(64),
    relation_type VARCHAR(32) DEFAULT 'custom',
    status VARCHAR(32) DEFAULT 'draft',
    ontology_code VARCHAR(128),
    aliases JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tr_tenant ON business_domain.template_relations(tenant_id);
CREATE INDEX IF NOT EXISTS idx_tr_is_deleted ON business_domain.template_relations(is_deleted);
CREATE INDEX IF NOT EXISTS idx_tr_domain ON business_domain.template_relations(domain_id);
CREATE INDEX IF NOT EXISTS idx_tr_scenario ON business_domain.template_relations(scenario_id);

-- ============================================================
-- 008: 模板约束表 (template constraints)
-- 本体约束模板 CRUD，支持对象/属性/关系三级目标绑定
-- ============================================================

CREATE TABLE IF NOT EXISTS business_domain.template_constraints (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    domain_id VARCHAR(64) NOT NULL,
    scenario_id VARCHAR(64) NOT NULL,
    name VARCHAR(255) NOT NULL,
    target_type VARCHAR(32) NOT NULL,   -- object / attribute / relation
    target_id VARCHAR(64) NOT NULL,
    target_label VARCHAR(255) NOT NULL,
    constraint_type VARCHAR(32) NOT NULL,  -- unique / exists / conditional / range
    expression TEXT,
    suggestion TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_tc_tenant ON business_domain.template_constraints(tenant_id);
CREATE INDEX IF NOT EXISTS idx_tc_is_deleted ON business_domain.template_constraints(is_deleted);
CREATE INDEX IF NOT EXISTS idx_tc_domain ON business_domain.template_constraints(domain_id);
CREATE INDEX IF NOT EXISTS idx_tc_scenario ON business_domain.template_constraints(scenario_id);
CREATE INDEX IF NOT EXISTS idx_tc_target ON business_domain.template_constraints(target_id);

-- ============================================================
-- 悦溪平台数据库迁移 - 提示词模板
-- 版本: 008
-- 包含: business_domain.prompt_templates 表
-- 说明: tenant_id 为 NULL 时表示系统全局模板（跨租户可见，只读）
--       tenant_id 为非 NULL 时表示领域空间模板（租户隔离，完整 CRUD）
-- ============================================================

CREATE TABLE IF NOT EXISTS business_domain.prompt_templates (
    id              VARCHAR(64) PRIMARY KEY,
    tenant_id       VARCHAR(64),                    -- NULL=系统全局, 非NULL=领域租户
    space_id        VARCHAR(64),                    -- NULL=无空间归属, 非NULL=领域空间隔离
    name            VARCHAR(255) NOT NULL,
    category        VARCHAR(64) NOT NULL,           -- 通用问答/文档处理/金融分析/合同审查/数据分析/其他
    scope           VARCHAR(16) NOT NULL DEFAULT 'domain', -- system | domain
    description     TEXT,
    status          VARCHAR(16) NOT NULL DEFAULT '启用',   -- 启用 | 停用
    current_version VARCHAR(32) NOT NULL DEFAULT '1',
    versions_json   JSONB NOT NULL DEFAULT '[]'::jsonb,   -- [{version, content, updated_by, updated_at, remark}]
    created_by      VARCHAR(128),
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    is_deleted      SMALLINT NOT NULL DEFAULT 0
);

-- 领域模板：按租户查询
CREATE INDEX IF NOT EXISTS idx_pt_tenant_scope
    ON business_domain.prompt_templates(tenant_id, scope)
    WHERE tenant_id IS NOT NULL;

-- 领域模板：软删除过滤
CREATE INDEX IF NOT EXISTS idx_pt_tenant_deleted
    ON business_domain.prompt_templates(tenant_id, is_deleted)
    WHERE tenant_id IS NOT NULL;

-- 系统模板：按 scope 查询（无 tenant 过滤）
CREATE INDEX IF NOT EXISTS idx_pt_scope_system
    ON business_domain.prompt_templates(scope, is_deleted)
    WHERE tenant_id IS NULL AND scope = 'system';

COMMENT ON TABLE business_domain.prompt_templates IS
'提示词模板 — system scope 由种子 SQL 管理(tenant_id=NULL 只读)，domain scope 由 API 管理';

-- 领域模板空间隔离索引
CREATE INDEX IF NOT EXISTS idx_pt_space_id
    ON business_domain.prompt_templates(space_id)
    WHERE space_id IS NOT NULL;
