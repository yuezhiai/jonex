-- ============================================================================
-- 数据治理库：资产、标准、质量、保留策略
-- ============================================================================

CREATE TABLE IF NOT EXISTS data_governance.asset_metadata (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    asset_type character varying(32) NOT NULL,
    asset_id character varying(255) NOT NULL,
    owner character varying(128),
    domain character varying(64),
    sensitivity character varying(32),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    classification_categories jsonb,
    CONSTRAINT ck_asset_metadata_classification_categories CHECK (((classification_categories IS NULL) OR (jsonb_typeof(classification_categories) = 'array'::text))),
    CONSTRAINT ck_asset_metadata_type CHECK (((asset_type)::text = ANY ((ARRAY['document'::character varying, 'data_source'::character varying, 'table'::character varying, 'column'::character varying, 'metric'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS data_governance.classification_category (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    code character varying(32) NOT NULL,
    name character varying(64) NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_classification_category_code CHECK (((code)::text ~ '^[a-z][a-z0-9_]{1,31}$'::text)),
    CONSTRAINT ck_classification_category_name CHECK (((length(btrim((name)::text)) >= 1) AND (length(btrim((name)::text)) <= 64)))
);

CREATE TABLE IF NOT EXISTS data_governance.classification_default (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    scope_type character varying(16) NOT NULL,
    scope_id character varying(64) DEFAULT ''::character varying NOT NULL,
    level_code character varying(32),
    categories jsonb,
    updated_by character varying(128),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_classification_default_categories CHECK (((categories IS NULL) OR (jsonb_typeof(categories) = 'array'::text))),
    CONSTRAINT ck_classification_default_scope_type CHECK (((scope_type)::text = ANY ((ARRAY['tenant'::character varying, 'kb'::character varying])::text[]))),
    CONSTRAINT ck_classification_default_tenant_scope CHECK ((((scope_type)::text <> 'tenant'::text) OR ((scope_id)::text = ''::text)))
);

CREATE TABLE IF NOT EXISTS data_governance.classification_level (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    code character varying(32) NOT NULL,
    name character varying(64) NOT NULL,
    rank integer NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_classification_level_code CHECK (((code)::text ~ '^[a-z][a-z0-9_]{1,31}$'::text)),
    CONSTRAINT ck_classification_level_name CHECK (((length(btrim((name)::text)) >= 1) AND (length(btrim((name)::text)) <= 64))),
    CONSTRAINT ck_classification_level_rank CHECK (((rank >= 1) AND (rank <= 1000)))
);

CREATE TABLE IF NOT EXISTS data_governance.data_contracts (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    data_source_id character varying(64) NOT NULL,
    name character varying(255) NOT NULL,
    version integer NOT NULL,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    status character varying(16) DEFAULT 'draft'::character varying NOT NULL,
    revision integer DEFAULT 1 NOT NULL,
    created_by character varying(64),
    updated_by character varying(64),
    published_by character varying(64),
    published_at timestamp with time zone,
    is_deleted smallint DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_data_contract_status CHECK (((status)::text = ANY ((ARRAY['draft'::character varying, 'published'::character varying, 'retired'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS data_governance.quality_issue_events (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    issue_id character varying(64) NOT NULL,
    event_type character varying(24) NOT NULL,
    from_status character varying(16),
    to_status character varying(16),
    actor_id character varying(64),
    note text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_qie_type CHECK (((event_type)::text = ANY ((ARRAY['created'::character varying, 'assigned'::character varying, 'status_change'::character varying, 'comment'::character varying, 'verified'::character varying, 'reopened'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS data_governance.quality_issues (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    rule_id character varying(64) NOT NULL,
    asset_type character varying(16) NOT NULL,
    asset_id character varying(255) NOT NULL,
    data_source_id character varying(64) NOT NULL,
    severity character varying(16) DEFAULT 'warning'::character varying NOT NULL,
    status character varying(16) DEFAULT 'open'::character varying NOT NULL,
    assignee_id character varying(64),
    title character varying(255) NOT NULL,
    detail text,
    first_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    resolved_at timestamp with time zone,
    revision integer DEFAULT 1 NOT NULL,
    created_by character varying(64),
    updated_by character varying(64),
    is_deleted smallint DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_qi_status CHECK (((status)::text = ANY ((ARRAY['open'::character varying, 'assigned'::character varying, 'fixing'::character varying, 'pending_verify'::character varying, 'closed'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS data_governance.quality_results (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    run_id character varying(64) NOT NULL,
    rule_id character varying(64) NOT NULL,
    asset_type character varying(16) NOT NULL,
    asset_id character varying(255) NOT NULL,
    status character varying(16) NOT NULL,
    metric_value jsonb,
    message text,
    sample jsonb,
    checked_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_qres_status CHECK (((status)::text = ANY ((ARRAY['passed'::character varying, 'failed'::character varying, 'error'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS data_governance.quality_rules (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    name character varying(255) NOT NULL,
    asset_type character varying(16) NOT NULL,
    asset_id character varying(255) NOT NULL,
    data_source_id character varying(64) NOT NULL,
    kb_id character varying(128),
    dimension character varying(16) NOT NULL,
    severity character varying(16) DEFAULT 'warning'::character varying NOT NULL,
    check_type character varying(16) DEFAULT 'library'::character varying NOT NULL,
    definition jsonb DEFAULT '{}'::jsonb NOT NULL,
    threshold jsonb DEFAULT '{}'::jsonb NOT NULL,
    enabled smallint DEFAULT 0 NOT NULL,
    contract_id character varying(64),
    created_by character varying(64),
    updated_by character varying(64),
    revision integer DEFAULT 1 NOT NULL,
    is_deleted smallint DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_qr_asset_type CHECK (((asset_type)::text = ANY ((ARRAY['table'::character varying, 'column'::character varying])::text[]))),
    CONSTRAINT ck_qr_check_type CHECK (((check_type)::text = ANY ((ARRAY['library'::character varying, 'sql'::character varying, 'custom'::character varying])::text[]))),
    CONSTRAINT ck_qr_dimension CHECK (((dimension)::text = ANY ((ARRAY['completeness'::character varying, 'uniqueness'::character varying, 'validity'::character varying, 'consistency'::character varying, 'timeliness'::character varying])::text[]))),
    CONSTRAINT ck_qr_severity CHECK (((severity)::text = ANY ((ARRAY['info'::character varying, 'warning'::character varying, 'error'::character varying, 'critical'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS data_governance.quality_runs (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    data_source_id character varying(64) NOT NULL,
    trigger character varying(16) DEFAULT 'manual'::character varying NOT NULL,
    status character varying(16) DEFAULT 'running'::character varying NOT NULL,
    rule_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    rule_snapshot jsonb DEFAULT '[]'::jsonb NOT NULL,
    executor character varying(128),
    dialect character varying(16) DEFAULT 'tsql'::character varying NOT NULL,
    total integer DEFAULT 0 NOT NULL,
    passed integer DEFAULT 0 NOT NULL,
    failed integer DEFAULT 0 NOT NULL,
    error integer DEFAULT 0 NOT NULL,
    message text,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    created_by character varying(64),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_qrun_status CHECK (((status)::text = ANY ((ARRAY['running'::character varying, 'completed'::character varying, 'failed'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS data_governance.retention_policy (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    scope_type character varying(16) NOT NULL,
    scope_id character varying(64) DEFAULT ''::character varying NOT NULL,
    retention_days integer,
    grace_days integer,
    updated_by character varying(128),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_retention_days CHECK (((retention_days IS NULL) OR (retention_days >= 0))),
    CONSTRAINT ck_retention_grace CHECK (((grace_days IS NULL) OR (grace_days >= 0))),
    CONSTRAINT ck_retention_scope_pair CHECK ((((scope_type)::text = 'tenant'::text) = ((scope_id)::text = ''::text))),
    CONSTRAINT ck_retention_scope_type CHECK (((scope_type)::text = ANY ((ARRAY['tenant'::character varying, 'kb'::character varying, 'document'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS data_governance.standard_asset_bindings (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    standard_kind character varying(32) NOT NULL,
    standard_code character varying(128) NOT NULL,
    standard_asset_id character varying(64),
    asset_type character varying(16) NOT NULL,
    asset_id character varying(255) NOT NULL,
    source character varying(16) DEFAULT 'manual'::character varying NOT NULL,
    confidence numeric(5,4) DEFAULT 1.0 NOT NULL,
    note character varying(512),
    status character varying(16) DEFAULT 'active'::character varying NOT NULL,
    created_by character varying(64),
    updated_by character varying(64),
    revision integer DEFAULT 1 NOT NULL,
    is_deleted smallint DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_sab_asset_type CHECK (((asset_type)::text = ANY ((ARRAY['table'::character varying, 'column'::character varying, 'metric'::character varying])::text[]))),
    CONSTRAINT ck_sab_kind CHECK (((standard_kind)::text = ANY ((ARRAY['term'::character varying, 'data_element'::character varying, 'metric_card'::character varying])::text[]))),
    CONSTRAINT ck_sab_source CHECK (((source)::text = ANY ((ARRAY['manual'::character varying, 'suggested'::character varying])::text[]))),
    CONSTRAINT ck_sab_status CHECK (((status)::text = ANY ((ARRAY['active'::character varying, 'disabled'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS data_governance.standard_assets (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    kind character varying(32) NOT NULL,
    code character varying(128) NOT NULL,
    version integer NOT NULL,
    name character varying(255) NOT NULL,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    status character varying(16) DEFAULT 'draft'::character varying NOT NULL,
    revision integer DEFAULT 1 NOT NULL,
    created_by character varying(64),
    updated_by character varying(64),
    published_by character varying(64),
    published_at timestamp with time zone,
    is_deleted smallint DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_std_asset_kind CHECK (((kind)::text = ANY ((ARRAY['term'::character varying, 'data_element'::character varying, 'metric_card'::character varying])::text[]))),
    CONSTRAINT ck_std_asset_status CHECK (((status)::text = ANY ((ARRAY['draft'::character varying, 'published'::character varying, 'retired'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS data_governance.user_clearance (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    user_id character varying(64) NOT NULL,
    max_level_code character varying(32) NOT NULL,
    updated_by character varying(128),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_user_clearance_level_code CHECK (((max_level_code)::text ~ '^[a-z][a-z0-9_]{1,31}$'::text))
);

ALTER TABLE ONLY data_governance.asset_metadata
    ADD CONSTRAINT asset_metadata_pkey PRIMARY KEY (id);

ALTER TABLE ONLY data_governance.classification_category
    ADD CONSTRAINT classification_category_pkey PRIMARY KEY (id);

ALTER TABLE ONLY data_governance.classification_default
    ADD CONSTRAINT classification_default_pkey PRIMARY KEY (id);

ALTER TABLE ONLY data_governance.classification_level
    ADD CONSTRAINT classification_level_pkey PRIMARY KEY (id);

ALTER TABLE ONLY data_governance.data_contracts
    ADD CONSTRAINT data_contracts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY data_governance.quality_issue_events
    ADD CONSTRAINT quality_issue_events_pkey PRIMARY KEY (id);

ALTER TABLE ONLY data_governance.quality_issues
    ADD CONSTRAINT quality_issues_pkey PRIMARY KEY (id);

ALTER TABLE ONLY data_governance.quality_results
    ADD CONSTRAINT quality_results_pkey PRIMARY KEY (id);

ALTER TABLE ONLY data_governance.quality_rules
    ADD CONSTRAINT quality_rules_pkey PRIMARY KEY (id);

ALTER TABLE ONLY data_governance.quality_runs
    ADD CONSTRAINT quality_runs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY data_governance.retention_policy
    ADD CONSTRAINT retention_policy_pkey PRIMARY KEY (id);

ALTER TABLE ONLY data_governance.standard_asset_bindings
    ADD CONSTRAINT standard_asset_bindings_pkey PRIMARY KEY (id);

ALTER TABLE ONLY data_governance.standard_assets
    ADD CONSTRAINT standard_assets_pkey PRIMARY KEY (id);

ALTER TABLE ONLY data_governance.asset_metadata
    ADD CONSTRAINT uq_asset_metadata UNIQUE (tenant_id, asset_type, asset_id);

ALTER TABLE ONLY data_governance.classification_category
    ADD CONSTRAINT uq_classification_category_code UNIQUE (tenant_id, code);

ALTER TABLE ONLY data_governance.classification_default
    ADD CONSTRAINT uq_classification_default_scope UNIQUE (tenant_id, scope_type, scope_id);

ALTER TABLE ONLY data_governance.classification_level
    ADD CONSTRAINT uq_classification_level_code UNIQUE (tenant_id, code);

ALTER TABLE ONLY data_governance.retention_policy
    ADD CONSTRAINT uq_retention_scope UNIQUE (tenant_id, scope_type, scope_id);

ALTER TABLE ONLY data_governance.user_clearance
    ADD CONSTRAINT uq_user_clearance_user UNIQUE (tenant_id, user_id);

ALTER TABLE ONLY data_governance.user_clearance
    ADD CONSTRAINT user_clearance_pkey PRIMARY KEY (id);

CREATE INDEX IF NOT EXISTS ix_asset_metadata_domain ON data_governance.asset_metadata USING btree (tenant_id, domain);

CREATE INDEX IF NOT EXISTS ix_asset_metadata_owner ON data_governance.asset_metadata USING btree (tenant_id, owner);

CREATE INDEX IF NOT EXISTS ix_asset_metadata_sensitivity ON data_governance.asset_metadata USING btree (tenant_id, sensitivity);

CREATE INDEX IF NOT EXISTS ix_classification_category_tenant ON data_governance.classification_category USING btree (tenant_id);

CREATE INDEX IF NOT EXISTS ix_classification_level_tenant ON data_governance.classification_level USING btree (tenant_id, rank);

CREATE INDEX IF NOT EXISTS ix_dc_list ON data_governance.data_contracts USING btree (tenant_id, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS ix_qi_list ON data_governance.quality_issues USING btree (tenant_id, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS ix_qie_issue ON data_governance.quality_issue_events USING btree (tenant_id, issue_id, created_at);

CREATE INDEX IF NOT EXISTS ix_qr_asset ON data_governance.quality_rules USING btree (tenant_id, asset_type, asset_id);

CREATE INDEX IF NOT EXISTS ix_qr_list ON data_governance.quality_rules USING btree (tenant_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS ix_qr_source ON data_governance.quality_rules USING btree (tenant_id, data_source_id, enabled);

CREATE INDEX IF NOT EXISTS ix_qres_rule ON data_governance.quality_results USING btree (tenant_id, rule_id, checked_at DESC);

CREATE INDEX IF NOT EXISTS ix_qrun_source ON data_governance.quality_runs USING btree (tenant_id, data_source_id, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_sab_asset ON data_governance.standard_asset_bindings USING btree (tenant_id, asset_type, asset_id, status);

CREATE INDEX IF NOT EXISTS ix_sab_standard ON data_governance.standard_asset_bindings USING btree (tenant_id, standard_kind, standard_code, status);

CREATE INDEX IF NOT EXISTS ix_std_asset_list ON data_governance.standard_assets USING btree (tenant_id, kind, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS ix_user_clearance_tenant ON data_governance.user_clearance USING btree (tenant_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_dc_draft ON data_governance.data_contracts USING btree (tenant_id, data_source_id) WHERE (((status)::text = 'draft'::text) AND (is_deleted = 0));

CREATE UNIQUE INDEX IF NOT EXISTS uq_dc_published ON data_governance.data_contracts USING btree (tenant_id, data_source_id) WHERE (((status)::text = 'published'::text) AND (is_deleted = 0));

CREATE UNIQUE INDEX IF NOT EXISTS uq_dc_version ON data_governance.data_contracts USING btree (tenant_id, data_source_id, version) WHERE (is_deleted = 0);

CREATE UNIQUE INDEX IF NOT EXISTS uq_qres_run_rule ON data_governance.quality_results USING btree (tenant_id, run_id, rule_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_quality_issue_open ON data_governance.quality_issues USING btree (tenant_id, rule_id, asset_id) WHERE (((status)::text <> 'closed'::text) AND (is_deleted = 0));

CREATE UNIQUE INDEX IF NOT EXISTS uq_sab_active ON data_governance.standard_asset_bindings USING btree (tenant_id, standard_kind, standard_code, asset_type, asset_id) WHERE (((status)::text = 'active'::text) AND (is_deleted = 0));

CREATE UNIQUE INDEX IF NOT EXISTS uq_std_asset_draft ON data_governance.standard_assets USING btree (tenant_id, kind, code) WHERE (((status)::text = 'draft'::text) AND (is_deleted = 0));

CREATE UNIQUE INDEX IF NOT EXISTS uq_std_asset_published ON data_governance.standard_assets USING btree (tenant_id, kind, code) WHERE (((status)::text = 'published'::text) AND (is_deleted = 0));

CREATE UNIQUE INDEX IF NOT EXISTS uq_std_asset_version ON data_governance.standard_assets USING btree (tenant_id, kind, code, version) WHERE (is_deleted = 0);
