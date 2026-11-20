-- ============================================================================
-- 任务中心库：定义/版本/触发、任务与子任务调度
-- ============================================================================

CREATE TABLE IF NOT EXISTS task_center.runner_definitions (
    id character varying(64) NOT NULL,
    code character varying(64) NOT NULL,
    name character varying(128) NOT NULL,
    capability_id character varying(128) NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS task_center.subtask_attempts (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    space_id character varying(64) NOT NULL,
    task_id character varying(64) NOT NULL,
    subtask_id character varying(64) NOT NULL,
    attempt integer NOT NULL,
    execution_id character varying(128),
    status character varying(16) DEFAULT 'pending'::character varying NOT NULL,
    error_message text,
    outputs jsonb,
    started_at timestamp without time zone,
    finished_at timestamp without time zone,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    error_code character varying(128),
    error_params jsonb,
    input_refs jsonb,
    inputs jsonb,
    output_refs jsonb,
    CONSTRAINT ck_tc_sa_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'running'::character varying, 'success'::character varying, 'failed'::character varying, 'timeout'::character varying, 'stopped'::character varying, 'skipped'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS task_center.subtask_reports (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    space_id character varying(64) NOT NULL,
    execution_id character varying(128) NOT NULL,
    task_id character varying(64) NOT NULL,
    status character varying(16) NOT NULL,
    outputs jsonb,
    output_refs jsonb,
    error_message text,
    ts bigint,
    reported_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    last_error text,
    processed_at timestamp without time zone,
    dead_lettered smallint DEFAULT 0 NOT NULL,
    is_deleted smallint DEFAULT 0 NOT NULL,
    error_code character varying(128),
    error_params jsonb,
    input_refs jsonb,
    CONSTRAINT ck_tc_report_status CHECK (((status)::text = ANY ((ARRAY['success'::character varying, 'failed'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS task_center.subtasks (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    space_id character varying(64) NOT NULL,
    task_id character varying(64) NOT NULL,
    task_attempt_id character varying(64) NOT NULL,
    node_id character varying(64) NOT NULL,
    step_code character varying(64) NOT NULL,
    subtask_name character varying(128),
    status character varying(16) DEFAULT 'pending'::character varying NOT NULL,
    depends_on jsonb,
    next_steps jsonb,
    sort integer,
    timeout_seconds integer DEFAULT 300 NOT NULL,
    max_retries integer DEFAULT 0 NOT NULL,
    auto_retries_used integer DEFAULT 0 NOT NULL,
    on_retry_exhausted character varying(16) DEFAULT 'terminate'::character varying NOT NULL,
    inputs jsonb,
    outputs jsonb,
    output_refs jsonb,
    current_attempt integer DEFAULT 1 NOT NULL,
    execution_id character varying(128),
    state_version integer DEFAULT 0 NOT NULL,
    error_message text,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    started_at timestamp without time zone,
    finished_at timestamp without time zone,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    is_deleted smallint DEFAULT 0 NOT NULL,
    last_queried_at timestamp without time zone,
    error_code character varying(128),
    error_params jsonb,
    input_refs jsonb,
    CONSTRAINT ck_tc_sub_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'running'::character varying, 'success'::character varying, 'failed'::character varying, 'timeout'::character varying, 'stopped'::character varying, 'skipped'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS task_center.task_attempts (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    space_id character varying(64) NOT NULL,
    task_id character varying(64) NOT NULL,
    attempt integer NOT NULL,
    status character varying(16) DEFAULT 'pending'::character varying NOT NULL,
    error_message text,
    started_at timestamp without time zone,
    finished_at timestamp without time zone,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    error_code character varying(128),
    error_params jsonb,
    CONSTRAINT ck_tc_ta_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'running'::character varying, 'completed'::character varying, 'failed'::character varying, 'stopped'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS task_center.task_catalog_steps (
    id character varying(64) NOT NULL,
    step_code character varying(64) NOT NULL,
    name character varying(128) NOT NULL,
    runner_code character varying(64) NOT NULL,
    max_concurrency integer,
    timeout_seconds integer DEFAULT 300 NOT NULL,
    input_schema jsonb,
    output_schema jsonb,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS task_center.task_definition_versions (
    id character varying(64) NOT NULL,
    scope character varying(16) NOT NULL,
    tenant_id character varying(64),
    definition_id character varying(64) NOT NULL,
    space_id character varying(64),
    version_no integer NOT NULL,
    status character varying(16) DEFAULT 'draft'::character varying NOT NULL,
    config jsonb NOT NULL,
    published_at timestamp without time zone,
    is_deleted smallint DEFAULT 0 NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_tc_ver_scope CHECK (((scope)::text = ANY ((ARRAY['system'::character varying, 'space'::character varying])::text[]))),
    CONSTRAINT ck_tc_ver_scope_fields CHECK (((((scope)::text = 'system'::text) AND (tenant_id IS NULL) AND (space_id IS NULL)) OR (((scope)::text = 'space'::text) AND (tenant_id IS NOT NULL) AND (space_id IS NOT NULL)))),
    CONSTRAINT ck_tc_ver_status CHECK (((status)::text = ANY ((ARRAY['draft'::character varying, 'published'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS task_center.task_definitions (
    id character varying(64) NOT NULL,
    scope character varying(16) NOT NULL,
    tenant_id character varying(64),
    space_id character varying(64),
    name character varying(128) NOT NULL,
    description character varying(512),
    status character varying(16) DEFAULT 'draft'::character varying NOT NULL,
    current_version_no integer DEFAULT 0 NOT NULL,
    created_by character varying(64),
    updated_by character varying(64),
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    is_deleted smallint DEFAULT 0 NOT NULL,
    CONSTRAINT ck_tc_def_scope CHECK (((scope)::text = ANY ((ARRAY['system'::character varying, 'space'::character varying])::text[]))),
    CONSTRAINT ck_tc_def_scope_fields CHECK (((((scope)::text = 'system'::text) AND (tenant_id IS NULL) AND (space_id IS NULL)) OR (((scope)::text = 'space'::text) AND (tenant_id IS NOT NULL) AND (space_id IS NOT NULL)))),
    CONSTRAINT ck_tc_def_status CHECK (((status)::text = ANY ((ARRAY['draft'::character varying, 'published'::character varying, 'disabled'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS task_center.task_triggers (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    space_id character varying(64) NOT NULL,
    definition_id character varying(64) NOT NULL,
    trigger_key character varying(64) NOT NULL,
    name character varying(128) NOT NULL,
    status character varying(16) DEFAULT 'active'::character varying NOT NULL,
    cron_expr character varying(128) NOT NULL,
    next_fire_at timestamp without time zone NOT NULL,
    misfire_policy character varying(16) NOT NULL,
    fixed_version integer,
    inputs jsonb,
    last_fired_at timestamp without time zone,
    state_version integer DEFAULT 0 NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_tc_trig_misfire CHECK (((misfire_policy)::text = ANY ((ARRAY['skip'::character varying, 'catch_up'::character varying])::text[]))),
    CONSTRAINT ck_tc_trig_status CHECK (((status)::text = ANY ((ARRAY['active'::character varying, 'paused'::character varying, 'deleted'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS task_center.tasks (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    definition_id character varying(64) NOT NULL,
    definition_version_no integer NOT NULL,
    definition_config jsonb NOT NULL,
    space_id character varying(64) NOT NULL,
    task_name character varying(256),
    trigger_id character varying(64),
    inputs jsonb,
    runner_capability_ids jsonb,
    current_attempt integer DEFAULT 1 NOT NULL,
    status character varying(16) DEFAULT 'pending'::character varying NOT NULL,
    error_message text,
    state_version integer DEFAULT 0 NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    started_at timestamp without time zone,
    finished_at timestamp without time zone,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    created_by character varying(64),
    updated_by character varying(64),
    is_deleted smallint DEFAULT 0 NOT NULL,
    error_code character varying(128),
    error_params jsonb,
    task_source character varying(64),
    CONSTRAINT ck_tc_task_source CHECK (((task_source)::text = ANY ((ARRAY['knowledge_base.file.manual_upload'::character varying, 'knowledge_base.file.api_pull'::character varying, 'knowledge_base.file.api_push'::character varying, 'knowledge_base.file.storage_upload'::character varying])::text[]))),
    CONSTRAINT ck_tc_task_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'running'::character varying, 'completed'::character varying, 'failed'::character varying, 'stopped'::character varying])::text[])))
);

ALTER TABLE ONLY task_center.runner_definitions
    ADD CONSTRAINT runner_definitions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY task_center.subtask_attempts
    ADD CONSTRAINT subtask_attempts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY task_center.subtask_reports
    ADD CONSTRAINT subtask_reports_pkey PRIMARY KEY (id);

ALTER TABLE ONLY task_center.subtasks
    ADD CONSTRAINT subtasks_pkey PRIMARY KEY (id);

ALTER TABLE ONLY task_center.task_attempts
    ADD CONSTRAINT task_attempts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY task_center.task_catalog_steps
    ADD CONSTRAINT task_catalog_steps_pkey PRIMARY KEY (id);

ALTER TABLE ONLY task_center.task_definition_versions
    ADD CONSTRAINT task_definition_versions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY task_center.task_definitions
    ADD CONSTRAINT task_definitions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY task_center.task_triggers
    ADD CONSTRAINT task_triggers_pkey PRIMARY KEY (id);

ALTER TABLE ONLY task_center.tasks
    ADD CONSTRAINT tasks_pkey PRIMARY KEY (id);

CREATE INDEX IF NOT EXISTS idx_tc_def_tenant ON task_center.task_definitions USING btree (tenant_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_tc_def_space_name ON task_center.task_definitions USING btree (tenant_id, space_id, name) WHERE ((is_deleted = 0) AND ((scope)::text = 'space'::text));

CREATE INDEX IF NOT EXISTS idx_tc_report_exec ON task_center.subtask_reports USING btree (tenant_id, space_id, execution_id, is_deleted);

CREATE INDEX IF NOT EXISTS idx_tc_report_pending ON task_center.subtask_reports USING btree (reported_at) WHERE (is_deleted = 0);

CREATE INDEX IF NOT EXISTS idx_tc_sa_created ON task_center.subtask_attempts USING btree (created_at);

CREATE INDEX IF NOT EXISTS idx_tc_sa_task ON task_center.subtask_attempts USING btree (task_id);

CREATE INDEX IF NOT EXISTS idx_tc_subtask_status ON task_center.subtasks USING btree (status);

CREATE INDEX IF NOT EXISTS idx_tc_subtask_task ON task_center.subtasks USING btree (task_id);

CREATE INDEX IF NOT EXISTS idx_tc_subtask_tenant ON task_center.subtasks USING btree (tenant_id);

CREATE INDEX IF NOT EXISTS idx_tc_ta_created ON task_center.task_attempts USING btree (created_at);

CREATE INDEX IF NOT EXISTS idx_tc_ta_task ON task_center.task_attempts USING btree (task_id);

CREATE INDEX IF NOT EXISTS idx_tc_ta_tenant ON task_center.task_attempts USING btree (tenant_id);

CREATE INDEX IF NOT EXISTS idx_tc_task_def ON task_center.tasks USING btree (definition_id);

CREATE INDEX IF NOT EXISTS idx_tc_task_tenant_status ON task_center.tasks USING btree (tenant_id, status);

CREATE INDEX IF NOT EXISTS idx_tc_task_trigger ON task_center.tasks USING btree (trigger_id);

CREATE INDEX IF NOT EXISTS idx_tc_triggers_due ON task_center.task_triggers USING btree (next_fire_at) WHERE ((status)::text = 'active'::text);

CREATE INDEX IF NOT EXISTS idx_tc_ver_tenant ON task_center.task_definition_versions USING btree (tenant_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_tc_cs_step_code ON task_center.task_catalog_steps USING btree (step_code);

CREATE UNIQUE INDEX IF NOT EXISTS uq_tc_runner_code ON task_center.runner_definitions USING btree (code);

CREATE UNIQUE INDEX IF NOT EXISTS uq_tc_sa_attempt ON task_center.subtask_attempts USING btree (subtask_id, attempt);

CREATE UNIQUE INDEX IF NOT EXISTS uq_tc_sa_exec ON task_center.subtask_attempts USING btree (tenant_id, space_id, execution_id) WHERE (execution_id IS NOT NULL);

CREATE UNIQUE INDEX IF NOT EXISTS uq_tc_subtask_exec ON task_center.subtasks USING btree (tenant_id, space_id, execution_id) WHERE (execution_id IS NOT NULL);

CREATE UNIQUE INDEX IF NOT EXISTS uq_tc_subtask_node ON task_center.subtasks USING btree (task_attempt_id, node_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_tc_ta_attempt ON task_center.task_attempts USING btree (task_id, attempt);

CREATE UNIQUE INDEX IF NOT EXISTS uq_tc_trigger_key ON task_center.task_triggers USING btree (tenant_id, space_id, trigger_key) WHERE ((status)::text <> 'deleted'::text);

CREATE UNIQUE INDEX IF NOT EXISTS uq_tc_ver_draft ON task_center.task_definition_versions USING btree (definition_id) WHERE ((status)::text = 'draft'::text);

CREATE UNIQUE INDEX IF NOT EXISTS uq_tc_ver_no ON task_center.task_definition_versions USING btree (definition_id, version_no);
