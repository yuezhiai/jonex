-- ============================================================================
-- 本体编译库：编译任务与知识库级编译快照
-- ============================================================================

CREATE TABLE IF NOT EXISTS ontology_compile.compile_tasks (
    task_id text NOT NULL,
    tenant_id text NOT NULL,
    kb_id text DEFAULT ''::text NOT NULL,
    document_id text DEFAULT ''::text NOT NULL,
    name text DEFAULT ''::text NOT NULL,
    file_path text DEFAULT ''::text NOT NULL,
    status text DEFAULT 'created'::text NOT NULL,
    current_step text DEFAULT ''::text NOT NULL,
    progress double precision DEFAULT 0 NOT NULL,
    ontology_status text DEFAULT ''::text NOT NULL,
    ontology_schema jsonb,
    ontology_data jsonb,
    ontology_error text DEFAULT ''::text NOT NULL,
    config_snapshot jsonb DEFAULT '{}'::jsonb NOT NULL,
    timeline jsonb DEFAULT '[]'::jsonb NOT NULL,
    error_code text,
    error_message text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    content_generation integer DEFAULT 0 NOT NULL
);

ALTER TABLE ONLY ontology_compile.compile_tasks
    ADD CONSTRAINT compile_tasks_pkey PRIMARY KEY (task_id);

CREATE INDEX IF NOT EXISTS idx_compile_tasks_scope ON ontology_compile.compile_tasks USING btree (tenant_id, kb_id, created_at DESC);
