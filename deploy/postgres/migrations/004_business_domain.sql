-- ============================================================================
-- 业务域库：领域空间、服务、引擎、适配器、技能、模板
-- ============================================================================

CREATE TABLE IF NOT EXISTS business_domain.adapters (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    name character varying(255) NOT NULL,
    adapter_type character varying(32) NOT NULL,
    config_json jsonb DEFAULT '{}'::jsonb,
    status character varying(32) DEFAULT 'disconnected'::character varying,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    is_deleted smallint DEFAULT 0
);

CREATE TABLE IF NOT EXISTS business_domain.data_access_methods (
    id character varying(64) NOT NULL,
    name character varying(255) NOT NULL,
    access_type character varying(32) NOT NULL,
    config_json jsonb DEFAULT '{}'::jsonb,
    status character varying(32) DEFAULT 'active'::character varying,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    is_deleted smallint DEFAULT 0
);

CREATE TABLE IF NOT EXISTS business_domain.model_providers (
    id character varying(64) NOT NULL,
    name character varying(255) NOT NULL,
    provider_type character varying(32) NOT NULL,
    model_type character varying(32),
    endpoint character varying(512),
    api_key_encrypted character varying(512),
    model_name character varying(255),
    vector_dimension integer,
    token_limit integer,
    latency_ms integer,
    call_count integer DEFAULT 0,
    success_rate integer DEFAULT 0,
    status character varying(32) DEFAULT 'active'::character varying,
    config_json jsonb DEFAULT '{}'::jsonb,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    is_deleted smallint DEFAULT 0
);

CREATE TABLE IF NOT EXISTS business_domain.parser_configs (
    id character varying(64) NOT NULL,
    name character varying(255) NOT NULL,
    parser_type character varying(32) NOT NULL,
    file_types jsonb DEFAULT '[]'::jsonb,
    config_json jsonb DEFAULT '{}'::jsonb,
    status character varying(32) DEFAULT 'active'::character varying,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    is_deleted smallint DEFAULT 0
);

CREATE TABLE IF NOT EXISTS business_domain.prompt_templates (
    id character varying(64) NOT NULL,
    tenant_id character varying(64),
    space_id character varying(64),
    name character varying(255) NOT NULL,
    category character varying(64) NOT NULL,
    scope character varying(16) DEFAULT 'domain'::character varying NOT NULL,
    description text,
    status character varying(16) DEFAULT '启用'::character varying NOT NULL,
    current_version character varying(32) DEFAULT '1'::character varying NOT NULL,
    versions_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    created_by character varying(128),
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    is_deleted smallint DEFAULT 0 NOT NULL
);

CREATE TABLE IF NOT EXISTS business_domain.skill_catalog (
    id character varying(64) NOT NULL,
    name character varying(255) NOT NULL,
    description text,
    category character varying(64) DEFAULT 'custom'::character varying NOT NULL,
    icon character varying(64),
    status character varying(32) DEFAULT 'published'::character varying NOT NULL,
    tool_name character varying(128) NOT NULL,
    instruction text NOT NULL,
    input_schema_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    output_schema_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    artifact_bucket character varying(128) NOT NULL,
    artifact_object_key character varying(1024) NOT NULL,
    artifact_checksum character varying(128),
    artifact_size bigint,
    artifact_content_type character varying(128) DEFAULT 'application/zip'::character varying,
    tags_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    capability_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    is_deleted smallint DEFAULT 0
);

CREATE TABLE IF NOT EXISTS business_domain.template_attributes (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    template_object_id character varying(64) NOT NULL,
    attr_name character varying(255) NOT NULL,
    description text,
    attr_type character varying(64) DEFAULT 'string'::character varying,
    is_primary_key smallint DEFAULT 0,
    constraints_json jsonb DEFAULT '{}'::jsonb,
    sort_order integer DEFAULT 0,
    ontology_code character varying(128),
    is_required smallint DEFAULT 0,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    is_deleted smallint DEFAULT 0
);

CREATE TABLE IF NOT EXISTS business_domain.template_constraints (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    domain_id character varying(64) NOT NULL,
    scenario_id character varying(64) NOT NULL,
    name character varying(255) NOT NULL,
    target_type character varying(32) NOT NULL,
    target_id character varying(64) NOT NULL,
    target_label character varying(255) NOT NULL,
    constraint_type character varying(32) NOT NULL,
    expression text,
    suggestion text,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    is_deleted smallint DEFAULT 0
);

CREATE TABLE IF NOT EXISTS business_domain.template_domains (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    name character varying(255) NOT NULL,
    description text,
    status character varying(32) DEFAULT 'inactive'::character varying,
    version integer DEFAULT 1,
    published_at timestamp with time zone,
    structure_hash character varying(64),
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    is_deleted smallint DEFAULT 0,
    name_en character varying(255)
);

CREATE TABLE IF NOT EXISTS business_domain.template_objects (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    domain_id character varying(64) NOT NULL,
    scenario_id character varying(64) NOT NULL,
    name character varying(255) NOT NULL,
    description text,
    status character varying(32) DEFAULT 'draft'::character varying,
    ontology_code character varying(128),
    aliases jsonb DEFAULT '[]'::jsonb NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    is_deleted smallint DEFAULT 0
);

CREATE TABLE IF NOT EXISTS business_domain.template_publish_requests (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    domain_id character varying(64) NOT NULL,
    scenario_id character varying(64) NOT NULL,
    status character varying(16) DEFAULT 'pending'::character varying NOT NULL,
    structure_hash character varying(64) NOT NULL,
    snapshot jsonb DEFAULT '{}'::jsonb NOT NULL,
    submitted_by character varying(128),
    submitted_at timestamp with time zone DEFAULT now() NOT NULL,
    decided_by character varying(128),
    decided_at timestamp with time zone,
    comment text,
    published_version integer,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_tpr_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'approved'::character varying, 'rejected'::character varying, 'cancelled'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS business_domain.template_relations (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    domain_id character varying(64) NOT NULL,
    scenario_id character varying(64) NOT NULL,
    name character varying(255) NOT NULL,
    description text,
    source_object_id character varying(64),
    target_object_id character varying(64),
    relation_type character varying(32) DEFAULT 'custom'::character varying,
    status character varying(32) DEFAULT 'draft'::character varying,
    ontology_code character varying(128),
    aliases jsonb DEFAULT '[]'::jsonb NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    is_deleted smallint DEFAULT 0
);

CREATE TABLE IF NOT EXISTS business_domain.template_scenario_versions (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    scenario_id character varying(64) NOT NULL,
    domain_id character varying(64) NOT NULL,
    version integer NOT NULL,
    structure_hash character varying(64) NOT NULL,
    snapshot jsonb DEFAULT '{}'::jsonb NOT NULL,
    published_by character varying(128),
    published_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE IF NOT EXISTS business_domain.template_scenarios (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    domain_id character varying(64) NOT NULL,
    name character varying(255) NOT NULL,
    description text,
    config_json jsonb DEFAULT '{}'::jsonb,
    version integer DEFAULT 1,
    published_at timestamp with time zone,
    structure_hash character varying(64),
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    is_deleted smallint DEFAULT 0
);

CREATE TABLE IF NOT EXISTS business_domain.tenant_skills (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    skill_id character varying(64) NOT NULL,
    status character varying(32) DEFAULT 'disabled'::character varying NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    is_deleted smallint DEFAULT 0
);

ALTER TABLE ONLY business_domain.adapters
    ADD CONSTRAINT adapters_pkey PRIMARY KEY (id);

ALTER TABLE ONLY business_domain.data_access_methods
    ADD CONSTRAINT data_access_methods_pkey PRIMARY KEY (id);

ALTER TABLE ONLY business_domain.model_providers
    ADD CONSTRAINT model_providers_pkey PRIMARY KEY (id);

ALTER TABLE ONLY business_domain.parser_configs
    ADD CONSTRAINT parser_configs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY business_domain.prompt_templates
    ADD CONSTRAINT prompt_templates_pkey PRIMARY KEY (id);

ALTER TABLE ONLY business_domain.skill_catalog
    ADD CONSTRAINT skill_catalog_pkey PRIMARY KEY (id);

ALTER TABLE ONLY business_domain.skill_catalog
    ADD CONSTRAINT skill_catalog_tool_name_key UNIQUE (tool_name);

ALTER TABLE ONLY business_domain.template_attributes
    ADD CONSTRAINT template_attributes_pkey PRIMARY KEY (id);

ALTER TABLE ONLY business_domain.template_constraints
    ADD CONSTRAINT template_constraints_pkey PRIMARY KEY (id);

ALTER TABLE ONLY business_domain.template_domains
    ADD CONSTRAINT template_domains_pkey PRIMARY KEY (id);

ALTER TABLE ONLY business_domain.template_objects
    ADD CONSTRAINT template_objects_pkey PRIMARY KEY (id);

ALTER TABLE ONLY business_domain.template_publish_requests
    ADD CONSTRAINT template_publish_requests_pkey PRIMARY KEY (id);

ALTER TABLE ONLY business_domain.template_relations
    ADD CONSTRAINT template_relations_pkey PRIMARY KEY (id);

ALTER TABLE ONLY business_domain.template_scenario_versions
    ADD CONSTRAINT template_scenario_versions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY business_domain.template_scenarios
    ADD CONSTRAINT template_scenarios_pkey PRIMARY KEY (id);

ALTER TABLE ONLY business_domain.tenant_skills
    ADD CONSTRAINT tenant_skills_pkey PRIMARY KEY (id);

ALTER TABLE ONLY business_domain.tenant_skills
    ADD CONSTRAINT tenant_skills_tenant_id_skill_id_key UNIQUE (tenant_id, skill_id);

ALTER TABLE ONLY business_domain.template_scenario_versions
    ADD CONSTRAINT uq_tsv_scenario_version UNIQUE (tenant_id, scenario_id, version);

CREATE INDEX IF NOT EXISTS idx_adp_is_deleted ON business_domain.adapters USING btree (is_deleted);

CREATE INDEX IF NOT EXISTS idx_adp_tenant ON business_domain.adapters USING btree (tenant_id);

CREATE INDEX IF NOT EXISTS idx_dam_is_deleted ON business_domain.data_access_methods USING btree (is_deleted);

CREATE INDEX IF NOT EXISTS idx_mp_is_deleted ON business_domain.model_providers USING btree (is_deleted);

CREATE INDEX IF NOT EXISTS idx_pc_is_deleted ON business_domain.parser_configs USING btree (is_deleted);

CREATE INDEX IF NOT EXISTS idx_pt_scope_system ON business_domain.prompt_templates USING btree (scope, is_deleted) WHERE ((tenant_id IS NULL) AND ((scope)::text = 'system'::text));

CREATE INDEX IF NOT EXISTS idx_pt_space_id ON business_domain.prompt_templates USING btree (space_id) WHERE (space_id IS NOT NULL);

CREATE INDEX IF NOT EXISTS idx_pt_tenant_deleted ON business_domain.prompt_templates USING btree (tenant_id, is_deleted) WHERE (tenant_id IS NOT NULL);

CREATE INDEX IF NOT EXISTS idx_pt_tenant_scope ON business_domain.prompt_templates USING btree (tenant_id, scope) WHERE (tenant_id IS NOT NULL);

CREATE INDEX IF NOT EXISTS idx_skill_catalog_category ON business_domain.skill_catalog USING btree (category);

CREATE INDEX IF NOT EXISTS idx_skill_catalog_is_deleted ON business_domain.skill_catalog USING btree (is_deleted);

CREATE INDEX IF NOT EXISTS idx_skill_catalog_status ON business_domain.skill_catalog USING btree (status);

CREATE INDEX IF NOT EXISTS idx_ta_is_deleted ON business_domain.template_attributes USING btree (is_deleted);

CREATE INDEX IF NOT EXISTS idx_ta_object ON business_domain.template_attributes USING btree (template_object_id);

CREATE INDEX IF NOT EXISTS idx_ta_tenant ON business_domain.template_attributes USING btree (tenant_id);

CREATE INDEX IF NOT EXISTS idx_tc_domain ON business_domain.template_constraints USING btree (domain_id);

CREATE INDEX IF NOT EXISTS idx_tc_is_deleted ON business_domain.template_constraints USING btree (is_deleted);

CREATE INDEX IF NOT EXISTS idx_tc_scenario ON business_domain.template_constraints USING btree (scenario_id);

CREATE INDEX IF NOT EXISTS idx_tc_target ON business_domain.template_constraints USING btree (target_id);

CREATE INDEX IF NOT EXISTS idx_tc_tenant ON business_domain.template_constraints USING btree (tenant_id);

CREATE INDEX IF NOT EXISTS idx_td_is_deleted ON business_domain.template_domains USING btree (is_deleted);

CREATE INDEX IF NOT EXISTS idx_td_tenant ON business_domain.template_domains USING btree (tenant_id);

CREATE INDEX IF NOT EXISTS idx_tenant_skills_is_deleted ON business_domain.tenant_skills USING btree (is_deleted);

CREATE INDEX IF NOT EXISTS idx_tenant_skills_skill ON business_domain.tenant_skills USING btree (skill_id);

CREATE INDEX IF NOT EXISTS idx_tenant_skills_status ON business_domain.tenant_skills USING btree (status);

CREATE INDEX IF NOT EXISTS idx_tenant_skills_tenant ON business_domain.tenant_skills USING btree (tenant_id);

CREATE INDEX IF NOT EXISTS idx_tobj_domain ON business_domain.template_objects USING btree (domain_id);

CREATE INDEX IF NOT EXISTS idx_tobj_is_deleted ON business_domain.template_objects USING btree (is_deleted);

CREATE INDEX IF NOT EXISTS idx_tobj_scenario ON business_domain.template_objects USING btree (scenario_id);

CREATE INDEX IF NOT EXISTS idx_tobj_tenant ON business_domain.template_objects USING btree (tenant_id);

CREATE INDEX IF NOT EXISTS idx_tr_domain ON business_domain.template_relations USING btree (domain_id);

CREATE INDEX IF NOT EXISTS idx_tr_is_deleted ON business_domain.template_relations USING btree (is_deleted);

CREATE INDEX IF NOT EXISTS idx_tr_scenario ON business_domain.template_relations USING btree (scenario_id);

CREATE INDEX IF NOT EXISTS idx_tr_tenant ON business_domain.template_relations USING btree (tenant_id);

CREATE INDEX IF NOT EXISTS idx_ts_domain ON business_domain.template_scenarios USING btree (domain_id);

CREATE INDEX IF NOT EXISTS idx_ts_is_deleted ON business_domain.template_scenarios USING btree (is_deleted);

CREATE INDEX IF NOT EXISTS idx_ts_tenant ON business_domain.template_scenarios USING btree (tenant_id);

CREATE INDEX IF NOT EXISTS ix_tpr_tenant_scenario ON business_domain.template_publish_requests USING btree (tenant_id, scenario_id, submitted_at DESC);

CREATE INDEX IF NOT EXISTS ix_tsv_tenant_domain ON business_domain.template_scenario_versions USING btree (tenant_id, domain_id);

CREATE INDEX IF NOT EXISTS ix_tsv_tenant_scenario ON business_domain.template_scenario_versions USING btree (tenant_id, scenario_id, version DESC);

CREATE UNIQUE INDEX IF NOT EXISTS uq_tpr_pending_scenario ON business_domain.template_publish_requests USING btree (tenant_id, scenario_id) WHERE ((status)::text = 'pending'::text);

-- 外键约束（dump 把外键统一放在末尾，这里保持同样的做法，避免建表顺序问题）

ALTER TABLE ONLY business_domain.tenant_skills
    ADD CONSTRAINT tenant_skills_skill_id_fkey FOREIGN KEY (skill_id) REFERENCES business_domain.skill_catalog(id);
