-- ============================================================================
-- 知识库库：知识库、文档状态机、结构化/本体、检索事件
-- ============================================================================

CREATE TABLE IF NOT EXISTS knowledge_base.abstract_schema_save_history (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    knowledge_base_id character varying(128) NOT NULL,
    schema_version integer NOT NULL,
    status character varying(32) NOT NULL,
    schema jsonb NOT NULL,
    saved_by character varying(128),
    source character varying(16) DEFAULT 'manual'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_base.asset_status_event (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    asset_id character varying(64) NOT NULL,
    kb_id character varying(128),
    status character varying(32) NOT NULL,
    previous_status character varying(32),
    reason text,
    changed_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    is_deleted smallint DEFAULT 0 NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_base.data_source_connections (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    name character varying(255) NOT NULL,
    access_type character varying(32) NOT NULL,
    config_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    status character varying(32) DEFAULT 'active'::character varying NOT NULL,
    is_deleted smallint DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    space_id character varying(64)
);

CREATE TABLE IF NOT EXISTS knowledge_base.document_tags (
    document_id character varying(64) NOT NULL,
    tag_id character varying(64) NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_base.evolution_candidates (
    id character varying(64) NOT NULL,
    run_id character varying(64) NOT NULL,
    candidate_no integer NOT NULL,
    parent_version_id character varying(64),
    layer character varying(16) NOT NULL,
    edit_type character varying(32) NOT NULL,
    target jsonb DEFAULT '[]'::jsonb NOT NULL,
    before jsonb DEFAULT '{}'::jsonb NOT NULL,
    after jsonb DEFAULT '{}'::jsonb NOT NULL,
    expected_changes jsonb DEFAULT '[]'::jsonb NOT NULL,
    rationale text,
    evidence jsonb DEFAULT '{}'::jsonb NOT NULL,
    gate_decision character varying(16),
    gate_reasons jsonb,
    status character varying(16) DEFAULT 'draft'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_evo_candidate_layer CHECK (((layer)::text = ANY ((ARRAY['content'::character varying, 'tool'::character varying, 'schema'::character varying])::text[]))),
    CONSTRAINT ck_evo_candidate_status CHECK (((status)::text = ANY ((ARRAY['draft'::character varying, 'evaluated'::character varying, 'adopted'::character varying, 'rejected'::character varying, 'superseded'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS knowledge_base.evolution_corpus_cases (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    name character varying(128) NOT NULL,
    question text NOT NULL,
    tier character varying(32) NOT NULL,
    expect character varying(16) NOT NULL,
    control jsonb,
    kb_id character varying(128) NOT NULL,
    ds_id character varying(64) NOT NULL,
    enabled smallint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_evo_case_expect CHECK (((expect)::text = ANY ((ARRAY['answer'::character varying, 'refuse'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS knowledge_base.evolution_corpus_versions (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    knowledge_base_id character varying(128) NOT NULL,
    data_source_id character varying(64) NOT NULL,
    snapshot jsonb NOT NULL,
    snapshot_hash character(64) NOT NULL,
    status character varying(16) DEFAULT 'active'::character varying NOT NULL,
    source character varying(32) DEFAULT 'manual'::character varying NOT NULL,
    note text,
    created_by character varying(64),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    activated_at timestamp with time zone
);

CREATE TABLE IF NOT EXISTS knowledge_base.evolution_diagnosis_reports (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    params jsonb DEFAULT '{}'::jsonb NOT NULL,
    report jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_by character varying(64),
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_base.evolution_ledgers (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    ledger_type character varying(16) NOT NULL,
    entry jsonb DEFAULT '{}'::jsonb NOT NULL,
    run_id character varying(64),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_evo_ledger_type CHECK (((ledger_type)::text = ANY ((ARRAY['alias'::character varying, 'query_rule'::character varying, 'reject'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS knowledge_base.evolution_run_logs (
    run_id character varying(64) NOT NULL,
    seq integer NOT NULL,
    line text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_base.evolution_runs (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    knowledge_base_id character varying(128) NOT NULL,
    data_source_id character varying(64),
    kind character varying(16) NOT NULL,
    status character varying(16) DEFAULT 'queued'::character varying NOT NULL,
    stage character varying(32) DEFAULT 'created'::character varying NOT NULL,
    request jsonb DEFAULT '{}'::jsonb NOT NULL,
    parent jsonb DEFAULT '{}'::jsonb NOT NULL,
    report jsonb DEFAULT '{}'::jsonb NOT NULL,
    gate jsonb DEFAULT '{}'::jsonb NOT NULL,
    patch jsonb DEFAULT '{}'::jsonb NOT NULL,
    backup jsonb DEFAULT '{}'::jsonb NOT NULL,
    error text,
    request_id character varying(64),
    created_by character varying(64),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    parent_version_id character varying(64),
    gate_decision character varying(16),
    gate_adhoc jsonb,
    gate_effective character varying(16),
    adopted_version_id character varying(64),
    adopted_by character varying(64),
    adopted_at timestamp with time zone,
    adopt_request_id character varying(64),
    gate_override smallint DEFAULT 0 NOT NULL,
    override_by character varying(64),
    override_reason text,
    smoke_status character varying(16),
    smoke_mode character varying(16),
    smoke jsonb,
    rolled_back_at timestamp with time zone,
    rolled_back_by character varying(64),
    rollback_reason text,
    legacy_adopt smallint DEFAULT 0 NOT NULL,
    rollback_target_version_id character varying(64),
    rollback_from_version_id character varying(64),
    rollback_started_at timestamp with time zone,
    bench jsonb,
    bench_status character varying(16),
    bench_mode character varying(16),
    bench_started_at timestamp with time zone,
    bench_ended_at timestamp with time zone,
    corpus_version_id character varying(64),
    corpus_hash character(64),
    CONSTRAINT ck_evo_bench_status CHECK (((bench_status IS NULL) OR ((bench_status)::text = ANY ((ARRAY['pending'::character varying, 'parent_ran'::character varying, 'passed'::character varying, 'failed'::character varying, 'warn'::character varying, 'skipped'::character varying])::text[])))),
    CONSTRAINT ck_evo_run_kind CHECK (((kind)::text = ANY ((ARRAY['alias'::character varying, 'query_rule'::character varying, 'relation'::character varying, 'constraint'::character varying, 'mapping'::character varying, 'tool_schema'::character varying, 'synonym'::character varying, 'metric'::character varying])::text[]))),
    CONSTRAINT ck_evo_run_stage CHECK (((stage)::text = ANY ((ARRAY['created'::character varying, 'drafted'::character varying, 'probed'::character varying, 'parent-ran'::character varying, 'applied'::character varying, 'candidate-ran'::character varying, 'rolled-back'::character varying, 'gated'::character varying])::text[]))),
    CONSTRAINT ck_evo_run_status CHECK (((status)::text = ANY ((ARRAY['queued'::character varying, 'running'::character varying, 'evaluated'::character varying, 'adopting'::character varying, 'adopted'::character varying, 'rejected'::character varying, 'rolled-back'::character varying, 'smoke-failed'::character varying, 'failed'::character varying, 'dry-run'::character varying, 'empty-draft'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS knowledge_base.folders (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    knowledge_base_id character varying(128) NOT NULL,
    name character varying(255) NOT NULL,
    is_preset boolean DEFAULT false NOT NULL,
    sort_order integer DEFAULT 0,
    created_by character varying(64),
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    is_deleted smallint DEFAULT 0 NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_base.governance_decision_audit (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    request_id character varying(64),
    user_id character varying(64),
    knowledge_base_id character varying(128) NOT NULL,
    data_source_id character varying(64),
    action character varying(16) NOT NULL,
    resource_table character varying(255),
    resource_column character varying(255),
    level character varying(16),
    decision character varying(16) NOT NULL,
    policy_id character varying(64),
    reason character varying(500),
    sql_hash character varying(64),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_gov_audit_action CHECK (((action)::text = ANY ((ARRAY['select'::character varying, 'predicate'::character varying, 'aggregate'::character varying])::text[]))),
    CONSTRAINT ck_gov_audit_decision CHECK (((decision)::text = ANY ((ARRAY['allow'::character varying, 'deny'::character varying, 'mask'::character varying, 'row_filter'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS knowledge_base.governance_labels (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    knowledge_base_id character varying(128) NOT NULL,
    data_source_id character varying(64) NOT NULL,
    table_name character varying(255) NOT NULL,
    column_name character varying(255),
    level character varying(16) NOT NULL,
    tags jsonb DEFAULT '[]'::jsonb NOT NULL,
    origin character varying(16) DEFAULT 'manual'::character varying NOT NULL,
    note character varying(500),
    created_by character varying(64),
    is_deleted smallint DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_gov_label_level CHECK (((level)::text = ANY ((ARRAY['public'::character varying, 'internal'::character varying, 'confidential'::character varying, 'secret'::character varying])::text[]))),
    CONSTRAINT ck_gov_label_nonblank CHECK (((btrim((data_source_id)::text) <> ''::text) AND (btrim((table_name)::text) <> ''::text))),
    CONSTRAINT ck_gov_label_origin CHECK (((origin)::text = ANY ((ARRAY['manual'::character varying, 'imported'::character varying, 'derived'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS knowledge_base.governance_policies (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    knowledge_base_id character varying(128) NOT NULL,
    name character varying(128) NOT NULL,
    description character varying(500),
    effect character varying(16) NOT NULL,
    priority integer DEFAULT 100 NOT NULL,
    resource_type character varying(16) NOT NULL,
    table_pattern character varying(255),
    column_pattern character varying(255),
    tag_name character varying(128),
    min_clearance character varying(16) DEFAULT 'public'::character varying NOT NULL,
    mask_type character varying(16),
    row_filter character varying(1000),
    valid_from timestamp with time zone,
    valid_to timestamp with time zone,
    enabled smallint DEFAULT 1 NOT NULL,
    created_by character varying(64),
    note character varying(500),
    is_deleted smallint DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_gov_policy_clearance CHECK (((min_clearance)::text = ANY ((ARRAY['public'::character varying, 'internal'::character varying, 'confidential'::character varying, 'secret'::character varying])::text[]))),
    CONSTRAINT ck_gov_policy_effect CHECK (((effect)::text = ANY ((ARRAY['allow'::character varying, 'deny'::character varying, 'mask'::character varying, 'row_filter'::character varying])::text[]))),
    CONSTRAINT ck_gov_policy_mask CHECK (((mask_type IS NULL) OR ((mask_type)::text = ANY ((ARRAY['null'::character varying, 'redact'::character varying])::text[])))),
    CONSTRAINT ck_gov_policy_resource CHECK (((resource_type)::text = ANY ((ARRAY['table'::character varying, 'column'::character varying, 'tag'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS knowledge_base.governance_tag_defs (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    knowledge_base_id character varying(128) NOT NULL,
    name character varying(128) NOT NULL,
    color character varying(32),
    description character varying(500),
    created_by character varying(64),
    is_deleted smallint DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_gov_tag_name CHECK ((btrim((name)::text) <> ''::text))
);

CREATE TABLE IF NOT EXISTS knowledge_base.governance_user_clearance (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    user_id character varying(64) NOT NULL,
    level character varying(16) DEFAULT 'public'::character varying NOT NULL,
    granted_by character varying(64),
    note character varying(500),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_gov_clearance_level CHECK (((level)::text = ANY ((ARRAY['public'::character varying, 'internal'::character varying, 'confidential'::character varying, 'secret'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS knowledge_base.kb_permissions (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    kb_id character varying(64) NOT NULL,
    user_id character varying(64) NOT NULL,
    role character varying(32) DEFAULT 'member'::character varying NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    is_deleted smallint DEFAULT 0,
    CONSTRAINT ck_kb_kbp_role CHECK (((role)::text = ANY ((ARRAY['kb_manager'::character varying, 'member'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS knowledge_base.kb_tools (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    knowledge_base_id character varying(128) NOT NULL,
    tool_name character varying(128) NOT NULL,
    name character varying(255) NOT NULL,
    description text,
    input_schema jsonb DEFAULT '{}'::jsonb NOT NULL,
    output_schema jsonb DEFAULT '{}'::jsonb NOT NULL,
    runtime jsonb,
    secret_encrypted text,
    enabled boolean DEFAULT true NOT NULL,
    created_by character varying(64),
    updated_by character varying(64),
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL,
    is_deleted smallint DEFAULT 0 NOT NULL,
    read_only boolean DEFAULT false NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_base.knowledge_answer_feedback (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    user_id character varying(128),
    history_id character varying(64),
    query text,
    answer text,
    feedback_type character varying(16) NOT NULL,
    feedback_reason character varying(32),
    feedback_comment character varying(300),
    domain_space_id character varying(64),
    knowledge_base_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    source character varying(64),
    mode character varying(32),
    source_missing_reason character varying(64),
    version integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    is_deleted smallint DEFAULT 0 NOT NULL,
    adopted boolean DEFAULT false NOT NULL,
    request_id character varying(64),
    mcp_key_id character varying(64),
    mcp_key_name character varying(255)
);

CREATE TABLE IF NOT EXISTS knowledge_base.knowledge_answer_feedback_event (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    history_id character varying(64),
    feedback_id character varying(64),
    user_id character varying(128),
    operation_id character varying(64) NOT NULL,
    version integer NOT NULL,
    feedback_type character varying(16),
    feedback_reason character varying(32),
    feedback_comment character varying(300),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    request_id character varying(64),
    mcp_key_id character varying(64)
);

CREATE TABLE IF NOT EXISTS knowledge_base.knowledge_data_sources (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    knowledge_base_id character varying(128) NOT NULL,
    access_method_id character varying(64),
    access_type character varying(32) NOT NULL,
    name character varying(255) NOT NULL,
    config_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    sync_mode character varying(16) DEFAULT 'manual'::character varying NOT NULL,
    cron_expr character varying(128),
    schedule_task_id integer,
    status character varying(32) DEFAULT 'active'::character varying NOT NULL,
    last_sync_at timestamp with time zone,
    last_sync_status character varying(32),
    last_sync_message text,
    document_count integer DEFAULT 0 NOT NULL,
    is_deleted smallint DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    connection_id character varying(64)
);

CREATE TABLE IF NOT EXISTS knowledge_base.knowledge_document_signatures (
    document_id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    knowledge_base_id character varying(128) NOT NULL,
    simhash bigint,
    text_chars integer DEFAULT 0 NOT NULL,
    text_source character varying(32) NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_kb_doc_sig_simhash CHECK (((simhash IS NULL) OR (simhash >= 0)))
);

CREATE TABLE IF NOT EXISTS knowledge_base.knowledge_document_similarity_suggestions (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    knowledge_base_id character varying(128) NOT NULL,
    source_document_id character varying(64) NOT NULL,
    target_document_id character varying(64) NOT NULL,
    similarity numeric(5,4) NOT NULL,
    method character varying(32) DEFAULT 'text_lines'::character varying NOT NULL,
    status character varying(16) DEFAULT 'pending'::character varying NOT NULL,
    dismissed_by character varying(64),
    dismissed_at timestamp without time zone,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_kb_doc_sim_distinct CHECK (((source_document_id)::text <> (target_document_id)::text)),
    CONSTRAINT ck_kb_doc_sim_similarity CHECK (((similarity >= (0)::numeric) AND (similarity <= (1)::numeric)))
);

CREATE TABLE IF NOT EXISTS knowledge_base.knowledge_document_versions (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    document_id character varying(64) NOT NULL,
    version_no integer NOT NULL,
    file_name character varying(512) NOT NULL,
    file_size bigint DEFAULT 0 NOT NULL,
    mime_type character varying(128),
    storage_backend character varying(16) DEFAULT 'local'::character varying NOT NULL,
    storage_key character varying(1024) NOT NULL,
    content_hash character varying(32),
    source character varying(32) DEFAULT 'upload'::character varying NOT NULL,
    created_by character varying(128),
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_base.knowledge_documents (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    file_name character varying(512) NOT NULL,
    file_path character varying(1024) NOT NULL,
    file_size bigint DEFAULT 0 NOT NULL,
    mime_type character varying(128),
    storage_backend character varying(16) DEFAULT 'local'::character varying NOT NULL,
    storage_key character varying(1024) NOT NULL,
    knowledge_base_id character varying(128) NOT NULL,
    status character varying(32) DEFAULT 'pending'::character varying NOT NULL,
    rag_task_id character varying(128),
    rag_doc_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    error_message text,
    ontology_status character varying(32) DEFAULT 'pending'::character varying NOT NULL,
    ontology_error text,
    ontology_retry_count integer DEFAULT 0 NOT NULL,
    content_generation integer DEFAULT 0 NOT NULL,
    ontology_target_schema_version integer,
    ontology_applied_schema_version integer,
    ontology_applied_schema_hash character varying(32),
    extra_metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    data_source_type character varying(32),
    folder_id character varying(64),
    llm_wiki_compile_status character varying(32),
    llm_wiki_compile_error text,
    llm_wiki_compile_warnings jsonb,
    llm_wiki_compile_requested_at timestamp without time zone,
    llm_wiki_compiled_at timestamp without time zone,
    llm_wiki_task_id character varying(128),
    llm_wiki_target_schema_version integer,
    llm_wiki_applied_schema_version integer,
    content_hash character varying(32),
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    is_deleted smallint DEFAULT 0 NOT NULL,
    archived_at timestamp without time zone,
    indexes_cleaned_at timestamp with time zone
);

CREATE TABLE IF NOT EXISTS knowledge_base.knowledge_info (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    space_id character varying(64) NOT NULL,
    name character varying(255) NOT NULL,
    description text,
    data_source_types jsonb DEFAULT '[]'::jsonb,
    document_count integer DEFAULT 0,
    status character varying(32) DEFAULT 'synced'::character varying,
    owner_id character varying(64),
    kb_type character varying(16) DEFAULT 'lightrag'::character varying NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    is_deleted smallint DEFAULT 0,
    ingest_backend character varying(32),
    ontology_compile_backend character varying(32),
    similarity_check_enabled boolean DEFAULT false NOT NULL,
    similarity_check_threshold numeric(4,3),
    procedural_extract boolean,
    structured_enabled boolean DEFAULT false NOT NULL,
    extract_tool_calls boolean,
    CONSTRAINT ck_kb_similarity_threshold CHECK (((similarity_check_threshold IS NULL) OR ((similarity_check_threshold >= 0.500) AND (similarity_check_threshold <= 0.999)))),
    CONSTRAINT ck_knowledge_info_kb_type CHECK (((kb_type)::text = ANY ((ARRAY['lightrag'::character varying, 'openkb'::character varying, 'jonex_wiki'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS knowledge_base.knowledge_model_settings (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    knowledge_base_id character varying(128) NOT NULL,
    scope character varying(32) NOT NULL,
    model_name character varying(255) NOT NULL,
    created_by character varying(64),
    updated_by character varying(64),
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_base.knowledge_parser_settings (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    knowledge_base_id character varying(128) NOT NULL,
    parser_type character varying(64) NOT NULL,
    parser_config_id character varying(64),
    prompt_config_id character varying(64),
    preprocessing_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    postprocessing_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    prompt_text text,
    prompt_template_id character varying(64),
    prompt_template_version character varying(32),
    summary_prompt_text text,
    summary_template_id character varying(64),
    summary_template_version character varying(32),
    tag_prompt_text text,
    tag_template_id character varying(64),
    tag_template_version character varying(32),
    status character varying(32) DEFAULT 'active'::character varying NOT NULL,
    is_deleted smallint DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_base.knowledge_query_images (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    user_id character varying(128) NOT NULL,
    storage_backend character varying(16) NOT NULL,
    storage_key character varying(512) NOT NULL,
    mime_type character varying(64) NOT NULL,
    file_size integer NOT NULL,
    sha256 character varying(64) NOT NULL,
    recognized_query text,
    description text,
    ocr_text text,
    entities jsonb DEFAULT '[]'::jsonb NOT NULL,
    prompt_version character varying(16),
    vlm_model character varying(128),
    purged_at timestamp without time zone,
    object_deleted smallint DEFAULT 0 NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    is_deleted smallint DEFAULT 0 NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_base.knowledge_search_feedback (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    user_id character varying(128) NOT NULL,
    session_id character varying(128) NOT NULL,
    query text NOT NULL,
    answer_preview text,
    knowledge_base_id character varying(128) NOT NULL,
    knowledge_base_name character varying(256),
    feedback_type character varying(16) NOT NULL,
    adopted boolean DEFAULT false NOT NULL,
    searched_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    is_deleted integer DEFAULT 0 NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_base.knowledge_search_history (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    user_id character varying(128) NOT NULL,
    query text NOT NULL,
    query_hash character varying(64) NOT NULL,
    knowledge_base_id character varying(128) NOT NULL,
    domain_space_id character varying(64),
    mode character varying(32) DEFAULT 'hybrid'::character varying NOT NULL,
    top_k integer DEFAULT 5 NOT NULL,
    status character varying(32) DEFAULT 'done'::character varying NOT NULL,
    answer_preview text,
    reference_count integer DEFAULT 0 NOT NULL,
    result_count integer DEFAULT 0 NOT NULL,
    duration_ms integer,
    extra_metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    answer text,
    "references" jsonb DEFAULT '[]'::jsonb NOT NULL,
    reasoning jsonb,
    searched_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    is_deleted smallint DEFAULT 0 NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_base.knowledge_search_history_images (
    history_id character varying(64) NOT NULL,
    image_id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    user_id character varying(128) NOT NULL,
    sort_order smallint DEFAULT 0 NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_base.lineage_edges (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    knowledge_base_id character varying(128) NOT NULL,
    data_source_id character varying(64) DEFAULT ''::character varying NOT NULL,
    from_table character varying(255) NOT NULL,
    from_column character varying(255) NOT NULL,
    to_table character varying(255) NOT NULL,
    to_column character varying(255) NOT NULL,
    origin character varying(32) DEFAULT 'manual'::character varying NOT NULL,
    note character varying(500),
    created_by character varying(64),
    is_deleted smallint DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seen_at timestamp with time zone,
    CONSTRAINT ck_lg_edge_columns_nonempty CHECK ((((from_column)::text <> ''::text) AND ((to_column)::text <> ''::text) AND ((data_source_id)::text <> ''::text))),
    CONSTRAINT ck_lg_edge_origin CHECK (((origin)::text = ANY ((ARRAY['declared'::character varying, 'manual'::character varying, 'comment'::character varying, 'platform'::character varying, 'value'::character varying, 'derived'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS knowledge_base.lineage_forbidden_pairs (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    knowledge_base_id character varying(128) NOT NULL,
    data_source_id character varying(64) NOT NULL,
    table_a character varying(255) NOT NULL,
    table_b character varying(255) NOT NULL,
    origin character varying(32) DEFAULT 'manual'::character varying NOT NULL,
    status character varying(16) DEFAULT 'active'::character varying NOT NULL,
    note character varying(500),
    created_by character varying(64),
    is_deleted smallint DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_lfp_distinct CHECK ((lower(btrim((table_a)::text)) <> lower(btrim((table_b)::text)))),
    CONSTRAINT ck_lfp_nonblank CHECK (((btrim((data_source_id)::text) <> ''::text) AND (btrim((table_a)::text) <> ''::text) AND (btrim((table_b)::text) <> ''::text))),
    CONSTRAINT ck_lfp_order CHECK ((lower(btrim((table_a)::text)) <= lower(btrim((table_b)::text)))),
    CONSTRAINT ck_lfp_origin CHECK (((origin)::text = ANY ((ARRAY['manual'::character varying, 'derived'::character varying])::text[]))),
    CONSTRAINT ck_lfp_status CHECK (((status)::text = ANY ((ARRAY['proposed'::character varying, 'active'::character varying, 'disabled'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS knowledge_base.llm_wiki_schemas (
    id bigint NOT NULL,
    tenant_id character varying(64) NOT NULL,
    knowledge_base_id character varying(128) NOT NULL,
    schema_version integer DEFAULT 1 NOT NULL,
    status character varying(32) DEFAULT 'active'::character varying NOT NULL,
    sync_status character varying(32) DEFAULT 'synced'::character varying NOT NULL,
    schema_name character varying(128) DEFAULT 'default'::character varying NOT NULL,
    language character varying(32) DEFAULT 'zh-CN'::character varying NOT NULL,
    model character varying(128),
    entity_types jsonb DEFAULT '[]'::jsonb NOT NULL,
    concept_types jsonb DEFAULT '[]'::jsonb NOT NULL,
    agents_md_extra text DEFAULT ''::text NOT NULL,
    agents_md text NOT NULL,
    config_snapshot jsonb DEFAULT '{}'::jsonb NOT NULL,
    edited_by character varying(128),
    edited_at timestamp without time zone,
    applied_at timestamp without time zone,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE SEQUENCE IF NOT EXISTS knowledge_base.llm_wiki_schemas_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE knowledge_base.llm_wiki_schemas_id_seq OWNED BY knowledge_base.llm_wiki_schemas.id;

CREATE TABLE IF NOT EXISTS knowledge_base.metric_call_approvals (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    knowledge_base_id character varying(128) NOT NULL,
    metric_name character varying(255) NOT NULL,
    status character varying(16) DEFAULT 'pending'::character varying NOT NULL,
    requested_by character varying(128),
    decided_by character varying(128),
    reason character varying(500),
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    is_deleted smallint DEFAULT 0 NOT NULL,
    expires_at timestamp with time zone,
    CONSTRAINT ck_kb_metric_approval_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'approved'::character varying, 'suspended'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS knowledge_base.nl2sql_metric_shadow (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    knowledge_base_id character varying(64) NOT NULL,
    data_source_id character varying(64) DEFAULT ''::character varying NOT NULL,
    question character varying(500) NOT NULL,
    entity_name character varying(255),
    main_outcome character varying(16) NOT NULL,
    main_reason_code character varying(64),
    main_row_count integer,
    main_elapsed_ms integer,
    status character varying(32) NOT NULL,
    metric_name character varying(255),
    metric_type character varying(16),
    match_kind character varying(16),
    matched_term character varying(255),
    compiled_sql text,
    shadow_row_count integer,
    shadow_elapsed_ms integer,
    agreement character varying(32) NOT NULL,
    scene jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_kb_n2s_shadow_agree CHECK (((agreement)::text = ANY ((ARRAY['main_failed_shadow_ok'::character varying, 'both_failed'::character varying, 'both_empty'::character varying, 'row_count_mismatch'::character varying, 'equal_scalar'::character varying, 'value_mismatch'::character varying, 'unknown'::character varying, 'not_comparable'::character varying])::text[]))),
    CONSTRAINT ck_kb_n2s_shadow_main CHECK (((main_outcome)::text = ANY ((ARRAY['success'::character varying, 'failed'::character varying])::text[]))),
    CONSTRAINT ck_kb_n2s_shadow_status CHECK (((status)::text = ANY ((ARRAY['no_metric'::character varying, 'not_executable'::character varying, 'compile_error'::character varying, 'exec_failed'::character varying, 'exec_ok'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS knowledge_base.nl2sql_observations (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    knowledge_base_id character varying(64) NOT NULL,
    data_source_id character varying(64) DEFAULT ''::character varying NOT NULL,
    question character varying(500) NOT NULL,
    outcome character varying(16) NOT NULL,
    reason_code character varying(64),
    reason character varying(500),
    entity_name character varying(255),
    sql text,
    rejected_sql text,
    row_count integer,
    elapsed_ms integer,
    scene jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    total_ms integer,
    precompile_violation character varying(32),
    precompile_retried boolean DEFAULT false NOT NULL,
    CONSTRAINT ck_kb_n2s_obs_outcome CHECK (((outcome)::text = ANY ((ARRAY['success'::character varying, 'failed'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS knowledge_base.nl2sql_verified_queries (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    knowledge_base_id character varying(128) NOT NULL,
    data_source_id character varying(64) DEFAULT ''::character varying NOT NULL,
    name character varying(128) NOT NULL,
    description character varying(500) DEFAULT ''::character varying NOT NULL,
    questions jsonb DEFAULT '[]'::jsonb NOT NULL,
    sql_template text DEFAULT ''::text NOT NULL,
    params jsonb DEFAULT '[]'::jsonb NOT NULL,
    status character varying(16) DEFAULT 'draft'::character varying NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    created_by character varying(64),
    updated_by character varying(64),
    activated_by character varying(64),
    activated_at timestamp with time zone,
    is_deleted smallint DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_n2s_vq_params CHECK ((jsonb_typeof(params) = 'array'::text)),
    CONSTRAINT ck_n2s_vq_questions CHECK (((jsonb_typeof(questions) = 'array'::text) AND (jsonb_array_length(questions) >= 1))),
    CONSTRAINT ck_n2s_vq_status CHECK (((status)::text = ANY ((ARRAY['draft'::character varying, 'active'::character varying, 'retired'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS knowledge_base.ontology_abstract_schemas (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    knowledge_base_id character varying(128) NOT NULL,
    schema_version integer DEFAULT 1 NOT NULL,
    status character varying(32) DEFAULT 'proposed'::character varying NOT NULL,
    schema jsonb DEFAULT '{}'::jsonb NOT NULL,
    source_hash character varying(64),
    edited_at timestamp with time zone,
    edited_by character varying(128),
    is_deleted smallint DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_base.ontology_compiled_schemas (
    id bigint NOT NULL,
    tenant_id character varying(64) NOT NULL,
    knowledge_base_id character varying(128) NOT NULL,
    template_domain_id character varying(64),
    template_scenario_id character varying(64),
    source_type character varying(32) DEFAULT 'business_template'::character varying NOT NULL,
    source_version integer DEFAULT 1 NOT NULL,
    source_hash character varying(64),
    schema_version integer DEFAULT 1 NOT NULL,
    entity_types jsonb DEFAULT '[]'::jsonb NOT NULL,
    relation_types jsonb DEFAULT '[]'::jsonb NOT NULL,
    constraints jsonb DEFAULT '[]'::jsonb NOT NULL,
    disambiguation jsonb DEFAULT '{"alias_merge": true, "case_insensitive": true}'::jsonb NOT NULL,
    prompt_schema jsonb DEFAULT '{}'::jsonb NOT NULL,
    schema_mode character varying(32) DEFAULT 'template_seeded'::character varying NOT NULL,
    sync_status character varying(32) DEFAULT 'synced'::character varying NOT NULL,
    edited_at timestamp with time zone,
    edited_by character varying(128),
    status character varying(32) DEFAULT 'active'::character varying NOT NULL,
    compiled_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    data_source_id character varying(64),
    evidence jsonb DEFAULT '{}'::jsonb NOT NULL
);

CREATE SEQUENCE IF NOT EXISTS knowledge_base.ontology_compiled_schemas_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE knowledge_base.ontology_compiled_schemas_id_seq OWNED BY knowledge_base.ontology_compiled_schemas.id;

CREATE TABLE IF NOT EXISTS knowledge_base.ontology_data_alignment (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    knowledge_base_id character varying(128) NOT NULL,
    doc_type character varying(255) NOT NULL,
    data_source_id character varying(64) NOT NULL,
    entity_name character varying(255) NOT NULL,
    status character varying(16) DEFAULT 'proposed'::character varying NOT NULL,
    created_by character varying(64),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_oda_nonblank CHECK (((btrim((doc_type)::text) <> ''::text) AND (btrim((data_source_id)::text) <> ''::text) AND (btrim((entity_name)::text) <> ''::text))),
    CONSTRAINT ck_oda_status CHECK (((status)::text = ANY ((ARRAY['proposed'::character varying, 'active'::character varying, 'rejected'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS knowledge_base.ontology_synonyms (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    knowledge_base_id character varying(128) NOT NULL,
    terms jsonb DEFAULT '[]'::jsonb NOT NULL,
    canonical character varying(255),
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL,
    is_deleted smallint DEFAULT 0 NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_base.ontology_template_bindings (
    id bigint NOT NULL,
    tenant_id character varying(64) NOT NULL,
    knowledge_base_id character varying(128) NOT NULL,
    template_domain_id character varying(64),
    template_scenario_id character varying(64),
    source_type character varying(32) DEFAULT 'business_template'::character varying NOT NULL,
    status character varying(32) DEFAULT 'active'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE SEQUENCE IF NOT EXISTS knowledge_base.ontology_template_bindings_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE knowledge_base.ontology_template_bindings_id_seq OWNED BY knowledge_base.ontology_template_bindings.id;

CREATE TABLE IF NOT EXISTS knowledge_base.ontology_tool_bindings (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    knowledge_base_id character varying(128) NOT NULL,
    entity_type character varying(32) NOT NULL,
    canonical_name character varying(512) NOT NULL,
    tool_source character varying(16) DEFAULT 'platform'::character varying NOT NULL,
    tool_name character varying(128) NOT NULL,
    param_mapping jsonb DEFAULT '{}'::jsonb NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    created_by character varying(64),
    updated_by character varying(64),
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL,
    is_deleted smallint DEFAULT 0 NOT NULL,
    qa_callable boolean DEFAULT false NOT NULL,
    CONSTRAINT ck_ontology_tool_bindings_entity_type CHECK (((entity_type)::text = ANY ((ARRAY['Skill'::character varying, 'Action'::character varying])::text[]))),
    CONSTRAINT ck_ontology_tool_bindings_tool_source CHECK (((tool_source)::text = ANY ((ARRAY['platform'::character varying, 'kb'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS knowledge_base.processing_task_event (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    processing_task_id character varying(128) NOT NULL,
    asset_id character varying(64) NOT NULL,
    kb_id character varying(128),
    task_type character varying(32) NOT NULL,
    status character varying(32) NOT NULL,
    error text,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    is_deleted smallint DEFAULT 0 NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_base.search_adoption_event (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    answer_id character varying(64) NOT NULL,
    request_id character varying(64),
    target_type character varying(32) NOT NULL,
    target_id character varying(128) NOT NULL,
    entity_type character varying(64),
    canonical_name character varying(256),
    kb_id character varying(128),
    adopted_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    is_deleted smallint DEFAULT 0 NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_base.search_hit_event (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    request_id character varying(64) NOT NULL,
    target_type character varying(32) NOT NULL,
    target_id character varying(128) NOT NULL,
    entity_type character varying(64),
    canonical_name character varying(256),
    kb_id character varying(128),
    hit_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    is_deleted smallint DEFAULT 0 NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_base.search_request_event (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    user_id character varying(128),
    request_id character varying(64) NOT NULL,
    entry_service_id character varying(64),
    domain_space_id character varying(64),
    query text,
    query_hash character varying(64),
    received_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    is_deleted smallint DEFAULT 0 NOT NULL,
    knowledge_base_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    mcp_key_id character varying(64)
);

CREATE TABLE IF NOT EXISTS knowledge_base.search_step_event (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    request_id character varying(64) NOT NULL,
    stage character varying(32) NOT NULL,
    title character varying(128),
    status character varying(16) NOT NULL,
    summary text,
    detail jsonb,
    duration_ms integer,
    knowledge_base_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    service_id character varying(64),
    searched_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    is_deleted smallint DEFAULT 0 NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_base.semantic_versions (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    knowledge_base_id character varying(128) NOT NULL,
    scope character varying(16) NOT NULL,
    data_source_id character varying(64),
    parent_version_id character varying(64),
    source_run_id character varying(64),
    source_version_id character varying(64),
    snapshot jsonb NOT NULL,
    snapshot_hash character(64) NOT NULL,
    status character varying(16) NOT NULL,
    created_by character varying(64),
    legacy smallint DEFAULT 0 NOT NULL,
    note text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    activated_at timestamp with time zone,
    CONSTRAINT ck_evo_version_scope CHECK (((scope)::text = ANY ((ARRAY['abstract'::character varying, 'synonym'::character varying, 'compiled'::character varying])::text[]))),
    CONSTRAINT ck_evo_version_shape CHECK (((((scope)::text = ANY ((ARRAY['abstract'::character varying, 'synonym'::character varying])::text[])) AND (data_source_id IS NULL)) OR (((scope)::text = 'compiled'::text) AND (data_source_id IS NOT NULL)))),
    CONSTRAINT ck_evo_version_status CHECK (((status)::text = ANY ((ARRAY['candidate'::character varying, 'active'::character varying, 'superseded'::character varying, 'rejected'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS knowledge_base.semantic_versions_archive (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    knowledge_base_id character varying(128) NOT NULL,
    scope character varying(16) NOT NULL,
    data_source_id character varying(64),
    parent_version_id character varying(64),
    source_run_id character varying(64),
    source_version_id character varying(64),
    snapshot jsonb NOT NULL,
    snapshot_hash character(64) NOT NULL,
    status character varying(16) NOT NULL,
    created_by character varying(64),
    legacy smallint DEFAULT 0 NOT NULL,
    note text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    activated_at timestamp with time zone,
    archived_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_evo_version_archive_scope CHECK (((scope)::text = ANY ((ARRAY['abstract'::character varying, 'synonym'::character varying, 'compiled'::character varying])::text[]))),
    CONSTRAINT ck_evo_version_archive_shape CHECK (((((scope)::text = ANY ((ARRAY['abstract'::character varying, 'synonym'::character varying])::text[])) AND (data_source_id IS NULL)) OR (((scope)::text = 'compiled'::text) AND (data_source_id IS NOT NULL)))),
    CONSTRAINT ck_evo_version_archive_status CHECK (((status)::text = ANY ((ARRAY['candidate'::character varying, 'active'::character varying, 'superseded'::character varying, 'rejected'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS knowledge_base.service_api_keys (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    service_id character varying(64) NOT NULL,
    key_prefix character varying(16) DEFAULT 'sk'::character varying NOT NULL,
    key_encrypted character varying(512) NOT NULL,
    expires_at timestamp without time zone,
    is_active smallint DEFAULT 1,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    is_deleted smallint DEFAULT 0
);

CREATE TABLE IF NOT EXISTS knowledge_base.service_configs (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    service_id character varying(64) NOT NULL,
    config_key character varying(128) NOT NULL,
    config_value text,
    description character varying(512),
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    is_deleted smallint DEFAULT 0
);

CREATE TABLE IF NOT EXISTS knowledge_base.service_knowledge_bases (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    service_id character varying(64) NOT NULL,
    kb_id character varying(64) NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    is_deleted smallint DEFAULT 0
);

CREATE TABLE IF NOT EXISTS knowledge_base.services (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    space_id character varying(64) NOT NULL,
    name character varying(255) NOT NULL,
    description text,
    domain_type character varying(64),
    status character varying(32) DEFAULT 'active'::character varying,
    api_key_encrypted character varying(512),
    enabled smallint DEFAULT 1,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    is_deleted smallint DEFAULT 0
);

CREATE TABLE IF NOT EXISTS knowledge_base.space_permissions (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    space_id character varying(64) NOT NULL,
    user_id character varying(64) NOT NULL,
    role character varying(32) DEFAULT 'member'::character varying NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    is_deleted smallint DEFAULT 0,
    CONSTRAINT ck_kb_spp_role CHECK (((role)::text = ANY ((ARRAY['space_manager'::character varying, 'member'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS knowledge_base.spaces (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    name character varying(255) NOT NULL,
    description text,
    owner_id character varying(64),
    status character varying(32) DEFAULT 'active'::character varying,
    knowledge_base_count integer DEFAULT 0,
    service_count integer DEFAULT 0,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    is_deleted smallint DEFAULT 0
);

CREATE TABLE IF NOT EXISTS knowledge_base.tags (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    knowledge_base_id character varying(128) NOT NULL,
    name character varying(255) NOT NULL,
    color character varying(32),
    created_by character varying(64),
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    is_deleted smallint DEFAULT 0 NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_base.task_center_documents (
    document_id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    knowledge_base_id character varying(128) NOT NULL,
    space_id character varying(64) NOT NULL,
    task_id character varying(64) NOT NULL,
    operator_id character varying(64),
    state character varying(16) DEFAULT 'submitting'::character varying NOT NULL,
    current_step character varying(32),
    execution_id character varying(64),
    handled_execution_id character varying(64),
    rag_task_id character varying(128),
    step_started_at timestamp without time zone,
    submit_attempts integer DEFAULT 0 NOT NULL,
    error_message text,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    last_report jsonb
);

CREATE TABLE IF NOT EXISTS knowledge_base.task_execution_registry (
    execution_id character varying(64) NOT NULL,
    tenant_id character varying(64),
    space_id character varying(64),
    task_id character varying(64),
    step_code character varying(32),
    claimed_at timestamp without time zone,
    released_at timestamp without time zone,
    claim_count integer DEFAULT 1 NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

ALTER TABLE ONLY knowledge_base.llm_wiki_schemas ALTER COLUMN id SET DEFAULT nextval('knowledge_base.llm_wiki_schemas_id_seq'::regclass);

ALTER TABLE ONLY knowledge_base.ontology_compiled_schemas ALTER COLUMN id SET DEFAULT nextval('knowledge_base.ontology_compiled_schemas_id_seq'::regclass);

ALTER TABLE ONLY knowledge_base.ontology_template_bindings ALTER COLUMN id SET DEFAULT nextval('knowledge_base.ontology_template_bindings_id_seq'::regclass);

ALTER TABLE ONLY knowledge_base.abstract_schema_save_history
    ADD CONSTRAINT abstract_schema_save_history_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.asset_status_event
    ADD CONSTRAINT asset_status_event_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.data_source_connections
    ADD CONSTRAINT data_source_connections_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.document_tags
    ADD CONSTRAINT document_tags_pkey PRIMARY KEY (document_id, tag_id);

ALTER TABLE ONLY knowledge_base.evolution_candidates
    ADD CONSTRAINT evolution_candidates_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.evolution_corpus_cases
    ADD CONSTRAINT evolution_corpus_cases_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.evolution_corpus_versions
    ADD CONSTRAINT evolution_corpus_versions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.evolution_diagnosis_reports
    ADD CONSTRAINT evolution_diagnosis_reports_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.evolution_ledgers
    ADD CONSTRAINT evolution_ledgers_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.evolution_run_logs
    ADD CONSTRAINT evolution_run_logs_pkey PRIMARY KEY (run_id, seq);

ALTER TABLE ONLY knowledge_base.evolution_runs
    ADD CONSTRAINT evolution_runs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.folders
    ADD CONSTRAINT folders_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.governance_decision_audit
    ADD CONSTRAINT governance_decision_audit_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.governance_labels
    ADD CONSTRAINT governance_labels_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.governance_policies
    ADD CONSTRAINT governance_policies_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.governance_tag_defs
    ADD CONSTRAINT governance_tag_defs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.governance_user_clearance
    ADD CONSTRAINT governance_user_clearance_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.kb_permissions
    ADD CONSTRAINT kb_permissions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.kb_tools
    ADD CONSTRAINT kb_tools_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.knowledge_answer_feedback_event
    ADD CONSTRAINT knowledge_answer_feedback_event_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.knowledge_answer_feedback
    ADD CONSTRAINT knowledge_answer_feedback_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.knowledge_data_sources
    ADD CONSTRAINT knowledge_data_sources_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.knowledge_document_signatures
    ADD CONSTRAINT knowledge_document_signatures_pkey PRIMARY KEY (document_id);

ALTER TABLE ONLY knowledge_base.knowledge_document_similarity_suggestions
    ADD CONSTRAINT knowledge_document_similarity_suggestions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.knowledge_document_versions
    ADD CONSTRAINT knowledge_document_versions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.knowledge_documents
    ADD CONSTRAINT knowledge_documents_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.knowledge_info
    ADD CONSTRAINT knowledge_info_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.knowledge_model_settings
    ADD CONSTRAINT knowledge_model_settings_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.knowledge_parser_settings
    ADD CONSTRAINT knowledge_parser_settings_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.knowledge_query_images
    ADD CONSTRAINT knowledge_query_images_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.knowledge_search_feedback
    ADD CONSTRAINT knowledge_search_feedback_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.knowledge_search_history_images
    ADD CONSTRAINT knowledge_search_history_images_pkey PRIMARY KEY (history_id, image_id);

ALTER TABLE ONLY knowledge_base.knowledge_search_history
    ADD CONSTRAINT knowledge_search_history_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.lineage_edges
    ADD CONSTRAINT lineage_edges_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.lineage_forbidden_pairs
    ADD CONSTRAINT lineage_forbidden_pairs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.llm_wiki_schemas
    ADD CONSTRAINT llm_wiki_schemas_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.metric_call_approvals
    ADD CONSTRAINT metric_call_approvals_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.nl2sql_metric_shadow
    ADD CONSTRAINT nl2sql_metric_shadow_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.nl2sql_observations
    ADD CONSTRAINT nl2sql_observations_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.nl2sql_verified_queries
    ADD CONSTRAINT nl2sql_verified_queries_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.ontology_abstract_schemas
    ADD CONSTRAINT ontology_abstract_schemas_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.ontology_compiled_schemas
    ADD CONSTRAINT ontology_compiled_schemas_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.ontology_data_alignment
    ADD CONSTRAINT ontology_data_alignment_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.ontology_synonyms
    ADD CONSTRAINT ontology_synonyms_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.ontology_template_bindings
    ADD CONSTRAINT ontology_template_bindings_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.ontology_template_bindings
    ADD CONSTRAINT ontology_template_bindings_tenant_id_knowledge_base_id_key UNIQUE (tenant_id, knowledge_base_id);

ALTER TABLE ONLY knowledge_base.ontology_tool_bindings
    ADD CONSTRAINT ontology_tool_bindings_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.task_center_documents
    ADD CONSTRAINT pk_kb_tc_documents PRIMARY KEY (document_id);

ALTER TABLE ONLY knowledge_base.task_execution_registry
    ADD CONSTRAINT pk_kb_tc_execution_registry PRIMARY KEY (execution_id);

ALTER TABLE ONLY knowledge_base.processing_task_event
    ADD CONSTRAINT processing_task_event_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.search_adoption_event
    ADD CONSTRAINT search_adoption_event_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.search_hit_event
    ADD CONSTRAINT search_hit_event_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.search_request_event
    ADD CONSTRAINT search_request_event_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.search_step_event
    ADD CONSTRAINT search_step_event_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.semantic_versions_archive
    ADD CONSTRAINT semantic_versions_archive_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.semantic_versions
    ADD CONSTRAINT semantic_versions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.service_api_keys
    ADD CONSTRAINT service_api_keys_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.service_configs
    ADD CONSTRAINT service_configs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.service_knowledge_bases
    ADD CONSTRAINT service_knowledge_bases_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.services
    ADD CONSTRAINT services_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.space_permissions
    ADD CONSTRAINT space_permissions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.spaces
    ADD CONSTRAINT spaces_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.tags
    ADD CONSTRAINT tags_pkey PRIMARY KEY (id);

ALTER TABLE ONLY knowledge_base.evolution_candidates
    ADD CONSTRAINT uq_evo_candidate_no UNIQUE (run_id, candidate_no);

ALTER TABLE ONLY knowledge_base.knowledge_document_similarity_suggestions
    ADD CONSTRAINT uq_kb_doc_sim_pair UNIQUE (tenant_id, source_document_id, target_document_id);

ALTER TABLE ONLY knowledge_base.knowledge_document_versions
    ADD CONSTRAINT uq_kb_doc_version UNIQUE (tenant_id, document_id, version_no);

ALTER TABLE ONLY knowledge_base.processing_task_event
    ADD CONSTRAINT uq_pte_task UNIQUE (tenant_id, processing_task_id);

CREATE INDEX IF NOT EXISTS idx_ase_asset ON knowledge_base.asset_status_event USING btree (asset_id);

CREATE INDEX IF NOT EXISTS idx_ase_tenant_time ON knowledge_base.asset_status_event USING btree (tenant_id, changed_at DESC);

CREATE INDEX IF NOT EXISTS idx_dsc_space ON knowledge_base.data_source_connections USING btree (tenant_id, space_id, is_deleted);

CREATE INDEX IF NOT EXISTS idx_dsc_tenant ON knowledge_base.data_source_connections USING btree (tenant_id, is_deleted);

CREATE INDEX IF NOT EXISTS idx_evo_candidate_run ON knowledge_base.evolution_candidates USING btree (run_id, candidate_no);

CREATE INDEX IF NOT EXISTS idx_evo_diag_latest ON knowledge_base.evolution_diagnosis_reports USING btree (tenant_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_evo_ledger_list ON knowledge_base.evolution_ledgers USING btree (tenant_id, ledger_type, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_evo_run_list ON knowledge_base.evolution_runs USING btree (tenant_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_evo_version_archive_list ON knowledge_base.semantic_versions_archive USING btree (tenant_id, knowledge_base_id, scope, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_evo_version_list ON knowledge_base.semantic_versions USING btree (tenant_id, knowledge_base_id, scope, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_gov_audit_scope ON knowledge_base.governance_decision_audit USING btree (tenant_id, knowledge_base_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_gov_label_scope ON knowledge_base.governance_labels USING btree (tenant_id, knowledge_base_id, data_source_id, table_name) WHERE (is_deleted = 0);

CREATE INDEX IF NOT EXISTS idx_gov_policy_scope ON knowledge_base.governance_policies USING btree (tenant_id, knowledge_base_id, enabled) WHERE (is_deleted = 0);

CREATE INDEX IF NOT EXISTS idx_kb_answer_feedback_event_history ON knowledge_base.knowledge_answer_feedback_event USING btree (history_id, version DESC);

CREATE INDEX IF NOT EXISTS idx_kb_answer_feedback_event_request ON knowledge_base.knowledge_answer_feedback_event USING btree (request_id);

CREATE INDEX IF NOT EXISTS idx_kb_answer_feedback_kb_ids ON knowledge_base.knowledge_answer_feedback USING gin (knowledge_base_ids);

CREATE INDEX IF NOT EXISTS idx_kb_answer_feedback_mcp_key ON knowledge_base.knowledge_answer_feedback USING btree (mcp_key_id);

CREATE INDEX IF NOT EXISTS idx_kb_answer_feedback_tenant_time ON knowledge_base.knowledge_answer_feedback USING btree (tenant_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_kb_answer_feedback_tenant_user ON knowledge_base.knowledge_answer_feedback USING btree (tenant_id, user_id);

CREATE INDEX IF NOT EXISTS idx_kb_data_sources_kb ON knowledge_base.knowledge_data_sources USING btree (tenant_id, knowledge_base_id, is_deleted);

CREATE INDEX IF NOT EXISTS idx_kb_doc_archive_scan ON knowledge_base.knowledge_documents USING btree (tenant_id, status, archived_at) WHERE ((is_deleted = 0) AND (archived_at IS NOT NULL));

CREATE INDEX IF NOT EXISTS idx_kb_doc_content_hash ON knowledge_base.knowledge_documents USING btree (tenant_id, knowledge_base_id, content_hash) WHERE ((is_deleted = 0) AND ((status)::text <> 'failed'::text));

CREATE INDEX IF NOT EXISTS idx_kb_doc_created_at_scan ON knowledge_base.knowledge_documents USING btree (tenant_id, created_at) WHERE (is_deleted = 0);

CREATE INDEX IF NOT EXISTS idx_kb_doc_folder ON knowledge_base.knowledge_documents USING btree (tenant_id, knowledge_base_id, folder_id) WHERE ((is_deleted = 0) AND (folder_id IS NOT NULL));

CREATE INDEX IF NOT EXISTS idx_kb_doc_llm_wiki_compiling ON knowledge_base.knowledge_documents USING btree (tenant_id, llm_wiki_compile_status) WHERE ((llm_wiki_compile_status)::text = 'compiling'::text);

CREATE INDEX IF NOT EXISTS idx_kb_doc_llm_wiki_schema_outdated ON knowledge_base.knowledge_documents USING btree (tenant_id, knowledge_base_id) WHERE ((is_deleted = 0) AND (llm_wiki_applied_schema_version IS DISTINCT FROM llm_wiki_target_schema_version));

CREATE INDEX IF NOT EXISTS idx_kb_doc_ontology_outdated ON knowledge_base.knowledge_documents USING btree (tenant_id, knowledge_base_id, ontology_status, ontology_applied_schema_version) WHERE (is_deleted = 0);

CREATE INDEX IF NOT EXISTS idx_kb_doc_rag_task ON knowledge_base.knowledge_documents USING btree (rag_task_id);

CREATE INDEX IF NOT EXISTS idx_kb_doc_retention_scan ON knowledge_base.knowledge_documents USING btree (tenant_id, status, created_at) WHERE (is_deleted = 0);

CREATE INDEX IF NOT EXISTS idx_kb_doc_tags_tag ON knowledge_base.document_tags USING btree (tag_id);

CREATE INDEX IF NOT EXISTS idx_kb_doc_tenant_created ON knowledge_base.knowledge_documents USING btree (tenant_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_kb_doc_tenant_deleted ON knowledge_base.knowledge_documents USING btree (tenant_id, is_deleted);

CREATE INDEX IF NOT EXISTS idx_kb_doc_tenant_kb ON knowledge_base.knowledge_documents USING btree (tenant_id, knowledge_base_id);

CREATE INDEX IF NOT EXISTS idx_kb_doc_tenant_kb_type ON knowledge_base.knowledge_documents USING btree (tenant_id, knowledge_base_id, data_source_type) WHERE (is_deleted = 0);

CREATE INDEX IF NOT EXISTS idx_kb_doc_tenant_ontology_status ON knowledge_base.knowledge_documents USING btree (tenant_id, ontology_status);

CREATE INDEX IF NOT EXISTS idx_kb_doc_tenant_status ON knowledge_base.knowledge_documents USING btree (tenant_id, status);

CREATE INDEX IF NOT EXISTS idx_kb_feedback_session ON knowledge_base.knowledge_search_feedback USING btree (session_id);

CREATE INDEX IF NOT EXISTS idx_kb_feedback_tenant_kb_time ON knowledge_base.knowledge_search_feedback USING btree (tenant_id, knowledge_base_id, searched_at);

CREATE INDEX IF NOT EXISTS idx_kb_feedback_tenant_user ON knowledge_base.knowledge_search_feedback USING btree (tenant_id, user_id);

CREATE INDEX IF NOT EXISTS idx_kb_feedback_user_id ON knowledge_base.knowledge_search_feedback USING btree (user_id);

CREATE INDEX IF NOT EXISTS idx_kb_folders_tenant_kb ON knowledge_base.folders USING btree (tenant_id, knowledge_base_id, is_deleted);

CREATE INDEX IF NOT EXISTS idx_kb_hist_domain_space_id ON knowledge_base.knowledge_search_history USING btree (domain_space_id) WHERE (domain_space_id IS NOT NULL);

CREATE INDEX IF NOT EXISTS idx_kb_hist_img_history ON knowledge_base.knowledge_search_history_images USING btree (history_id);

CREATE INDEX IF NOT EXISTS idx_kb_hist_img_image ON knowledge_base.knowledge_search_history_images USING btree (image_id);

CREATE INDEX IF NOT EXISTS idx_kb_hist_tenant_user_deleted ON knowledge_base.knowledge_search_history USING btree (tenant_id, user_id, is_deleted);

CREATE INDEX IF NOT EXISTS idx_kb_hist_tenant_user_kb_time ON knowledge_base.knowledge_search_history USING btree (tenant_id, user_id, knowledge_base_id, searched_at DESC);

CREATE INDEX IF NOT EXISTS idx_kb_hist_tenant_user_time ON knowledge_base.knowledge_search_history USING btree (tenant_id, user_id, searched_at DESC);

CREATE INDEX IF NOT EXISTS idx_kb_is_deleted ON knowledge_base.knowledge_info USING btree (is_deleted);

CREATE INDEX IF NOT EXISTS idx_kb_metric_approval_status ON knowledge_base.metric_call_approvals USING btree (tenant_id, knowledge_base_id, status);

CREATE INDEX IF NOT EXISTS idx_kb_n2s_obs_ds ON knowledge_base.nl2sql_observations USING btree (data_source_id, created_at);

CREATE INDEX IF NOT EXISTS idx_kb_n2s_obs_window ON knowledge_base.nl2sql_observations USING btree (outcome, created_at);

CREATE INDEX IF NOT EXISTS idx_kb_n2s_shadow_status ON knowledge_base.nl2sql_metric_shadow USING btree (tenant_id, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_kb_n2s_shadow_window ON knowledge_base.nl2sql_metric_shadow USING btree (tenant_id, knowledge_base_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_kb_perms_is_deleted ON knowledge_base.kb_permissions USING btree (is_deleted);

CREATE INDEX IF NOT EXISTS idx_kb_perms_kb ON knowledge_base.kb_permissions USING btree (kb_id);

CREATE INDEX IF NOT EXISTS idx_kb_perms_tenant ON knowledge_base.kb_permissions USING btree (tenant_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_kb_perms_unique_member ON knowledge_base.kb_permissions USING btree (tenant_id, kb_id, user_id) WHERE (is_deleted = 0);

CREATE INDEX IF NOT EXISTS idx_kb_perms_user ON knowledge_base.kb_permissions USING btree (user_id);

CREATE INDEX IF NOT EXISTS idx_kb_qimg_pending_object_delete ON knowledge_base.knowledge_query_images USING btree (purged_at) WHERE ((is_deleted = 1) AND (object_deleted = 0));

CREATE INDEX IF NOT EXISTS idx_kb_qimg_sweep ON knowledge_base.knowledge_query_images USING btree (updated_at) WHERE (is_deleted = 0);

CREATE INDEX IF NOT EXISTS idx_kb_sak_is_deleted ON knowledge_base.service_api_keys USING btree (is_deleted);

CREATE INDEX IF NOT EXISTS idx_kb_sak_service ON knowledge_base.service_api_keys USING btree (service_id);

CREATE INDEX IF NOT EXISTS idx_kb_sak_tenant ON knowledge_base.service_api_keys USING btree (tenant_id);

CREATE INDEX IF NOT EXISTS idx_kb_sc_is_deleted ON knowledge_base.service_configs USING btree (is_deleted);

CREATE INDEX IF NOT EXISTS idx_kb_sc_service ON knowledge_base.service_configs USING btree (service_id);

CREATE INDEX IF NOT EXISTS idx_kb_sc_tenant ON knowledge_base.service_configs USING btree (tenant_id);

CREATE INDEX IF NOT EXISTS idx_kb_skb_is_deleted ON knowledge_base.service_knowledge_bases USING btree (is_deleted);

CREATE INDEX IF NOT EXISTS idx_kb_skb_service ON knowledge_base.service_knowledge_bases USING btree (service_id);

CREATE INDEX IF NOT EXISTS idx_kb_skb_tenant ON knowledge_base.service_knowledge_bases USING btree (tenant_id);

CREATE INDEX IF NOT EXISTS idx_kb_sp_is_deleted ON knowledge_base.spaces USING btree (is_deleted);

CREATE INDEX IF NOT EXISTS idx_kb_sp_tenant ON knowledge_base.spaces USING btree (tenant_id);

CREATE INDEX IF NOT EXISTS idx_kb_space ON knowledge_base.knowledge_info USING btree (space_id);

CREATE INDEX IF NOT EXISTS idx_kb_spp_is_deleted ON knowledge_base.space_permissions USING btree (is_deleted);

CREATE INDEX IF NOT EXISTS idx_kb_spp_space ON knowledge_base.space_permissions USING btree (space_id);

CREATE INDEX IF NOT EXISTS idx_kb_spp_tenant ON knowledge_base.space_permissions USING btree (tenant_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_kb_spp_unique_member ON knowledge_base.space_permissions USING btree (tenant_id, space_id, user_id) WHERE (is_deleted = 0);

CREATE INDEX IF NOT EXISTS idx_kb_spp_user ON knowledge_base.space_permissions USING btree (user_id);

CREATE INDEX IF NOT EXISTS idx_kb_svc_is_deleted ON knowledge_base.services USING btree (is_deleted);

CREATE INDEX IF NOT EXISTS idx_kb_svc_space ON knowledge_base.services USING btree (space_id);

CREATE INDEX IF NOT EXISTS idx_kb_svc_tenant ON knowledge_base.services USING btree (tenant_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_kb_svc_unique_name ON knowledge_base.services USING btree (tenant_id, space_id, name) WHERE (is_deleted = 0);

CREATE INDEX IF NOT EXISTS idx_kb_synonyms_tenant_kb ON knowledge_base.ontology_synonyms USING btree (tenant_id, knowledge_base_id, is_deleted);

CREATE INDEX IF NOT EXISTS idx_kb_tags_tenant_kb ON knowledge_base.tags USING btree (tenant_id, knowledge_base_id, is_deleted);

CREATE INDEX IF NOT EXISTS idx_kb_tc_doc_tenant ON knowledge_base.task_center_documents USING btree (tenant_id);

CREATE INDEX IF NOT EXISTS idx_kb_tenant ON knowledge_base.knowledge_info USING btree (tenant_id);

CREATE INDEX IF NOT EXISTS idx_kb_tools_tenant_kb ON knowledge_base.kb_tools USING btree (tenant_id, knowledge_base_id, is_deleted);

CREATE INDEX IF NOT EXISTS idx_kds_connection ON knowledge_base.knowledge_data_sources USING btree (tenant_id, connection_id) WHERE (connection_id IS NOT NULL);

CREATE INDEX IF NOT EXISTS idx_kps_kb ON knowledge_base.knowledge_parser_settings USING btree (tenant_id, knowledge_base_id, is_deleted);

CREATE INDEX IF NOT EXISTS idx_kps_parser ON knowledge_base.knowledge_parser_settings USING btree (tenant_id, parser_config_id) WHERE ((parser_config_id IS NOT NULL) AND (is_deleted = 0));

CREATE INDEX IF NOT EXISTS idx_lfp_scope ON knowledge_base.lineage_forbidden_pairs USING btree (tenant_id, knowledge_base_id, data_source_id, status) WHERE (is_deleted = 0);

CREATE INDEX IF NOT EXISTS idx_lg_edge_scope ON knowledge_base.lineage_edges USING btree (tenant_id, knowledge_base_id, data_source_id) WHERE (is_deleted = 0);

CREATE INDEX IF NOT EXISTS idx_llm_wiki_schema_kb ON knowledge_base.llm_wiki_schemas USING btree (tenant_id, knowledge_base_id, schema_version);

CREATE INDEX IF NOT EXISTS idx_n2s_obs_tenant_kb_time ON knowledge_base.nl2sql_observations USING btree (tenant_id, knowledge_base_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_n2s_obs_tenant_time ON knowledge_base.nl2sql_observations USING btree (tenant_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_n2s_vq_lookup ON knowledge_base.nl2sql_verified_queries USING btree (tenant_id, knowledge_base_id, data_source_id, status);

CREATE INDEX IF NOT EXISTS idx_oas_history_kb ON knowledge_base.abstract_schema_save_history USING btree (tenant_id, knowledge_base_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_ocs_data_source ON knowledge_base.ontology_compiled_schemas USING btree (tenant_id, knowledge_base_id, data_source_id) WHERE (data_source_id IS NOT NULL);

CREATE INDEX IF NOT EXISTS idx_ocs_source ON knowledge_base.ontology_compiled_schemas USING btree (tenant_id, template_domain_id, template_scenario_id, source_hash);

CREATE INDEX IF NOT EXISTS idx_ocs_tenant_kb ON knowledge_base.ontology_compiled_schemas USING btree (tenant_id, knowledge_base_id);

CREATE INDEX IF NOT EXISTS idx_oda_scope ON knowledge_base.ontology_data_alignment USING btree (tenant_id, knowledge_base_id, status);

CREATE INDEX IF NOT EXISTS idx_ont_tool_bindings_tenant_kb ON knowledge_base.ontology_tool_bindings USING btree (tenant_id, knowledge_base_id, is_deleted);

CREATE INDEX IF NOT EXISTS idx_otb_tenant_template ON knowledge_base.ontology_template_bindings USING btree (tenant_id, template_domain_id, template_scenario_id);

CREATE INDEX IF NOT EXISTS idx_pte_asset ON knowledge_base.processing_task_event USING btree (asset_id);

CREATE INDEX IF NOT EXISTS idx_pte_asset_type ON knowledge_base.processing_task_event USING btree (asset_id, task_type);

CREATE INDEX IF NOT EXISTS idx_pte_tenant_kb_started ON knowledge_base.processing_task_event USING btree (tenant_id, kb_id, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_pte_tenant_task_status ON knowledge_base.processing_task_event USING btree (tenant_id, task_type, status);

CREATE INDEX IF NOT EXISTS idx_pte_tenant_time ON knowledge_base.processing_task_event USING btree (tenant_id, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_sae_answer ON knowledge_base.search_adoption_event USING btree (answer_id);

CREATE INDEX IF NOT EXISTS idx_sae_request ON knowledge_base.search_adoption_event USING btree (request_id);

CREATE INDEX IF NOT EXISTS idx_sae_target ON knowledge_base.search_adoption_event USING btree (target_type, target_id);

CREATE INDEX IF NOT EXISTS idx_sae_tenant_time ON knowledge_base.search_adoption_event USING btree (tenant_id, adopted_at DESC);

CREATE INDEX IF NOT EXISTS idx_she_request ON knowledge_base.search_hit_event USING btree (request_id);

CREATE INDEX IF NOT EXISTS idx_she_request_type ON knowledge_base.search_hit_event USING btree (request_id, target_type);

CREATE INDEX IF NOT EXISTS idx_she_target ON knowledge_base.search_hit_event USING btree (target_type, target_id);

CREATE INDEX IF NOT EXISTS idx_she_tenant_time ON knowledge_base.search_hit_event USING btree (tenant_id, hit_at DESC);

CREATE INDEX IF NOT EXISTS idx_sre_kbs ON knowledge_base.search_request_event USING gin (knowledge_base_ids);

CREATE INDEX IF NOT EXISTS idx_sre_mcp_key ON knowledge_base.search_request_event USING btree (mcp_key_id);

CREATE INDEX IF NOT EXISTS idx_sre_request ON knowledge_base.search_request_event USING btree (request_id);

CREATE INDEX IF NOT EXISTS idx_sre_tenant_time ON knowledge_base.search_request_event USING btree (tenant_id, received_at DESC);

CREATE INDEX IF NOT EXISTS idx_sse_request ON knowledge_base.search_step_event USING btree (request_id);

CREATE INDEX IF NOT EXISTS idx_sse_request_stage ON knowledge_base.search_step_event USING btree (request_id, stage);

CREATE INDEX IF NOT EXISTS idx_sse_tenant_time ON knowledge_base.search_step_event USING btree (tenant_id, searched_at DESC);

CREATE INDEX IF NOT EXISTS ix_evo_corpus_version_chain ON knowledge_base.evolution_corpus_versions USING btree (tenant_id, knowledge_base_id, data_source_id, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_kb_doc_index_cleanup_pending ON knowledge_base.knowledge_documents USING btree (updated_at) WHERE ((is_deleted = 1) AND (indexes_cleaned_at IS NULL));

CREATE INDEX IF NOT EXISTS ix_kb_doc_sig_scope ON knowledge_base.knowledge_document_signatures USING btree (tenant_id, knowledge_base_id);

CREATE INDEX IF NOT EXISTS ix_kb_doc_sig_unavailable ON knowledge_base.knowledge_document_signatures USING btree (tenant_id, knowledge_base_id, updated_at) WHERE (simhash IS NULL);

CREATE INDEX IF NOT EXISTS ix_kb_doc_sim_kb ON knowledge_base.knowledge_document_similarity_suggestions USING btree (tenant_id, knowledge_base_id, status);

CREATE INDEX IF NOT EXISTS ix_kb_doc_sim_source ON knowledge_base.knowledge_document_similarity_suggestions USING btree (tenant_id, source_document_id, status);

CREATE INDEX IF NOT EXISTS ix_kb_doc_sim_target ON knowledge_base.knowledge_document_similarity_suggestions USING btree (tenant_id, target_document_id, status);

CREATE INDEX IF NOT EXISTS ix_kb_doc_version_doc ON knowledge_base.knowledge_document_versions USING btree (tenant_id, document_id, version_no DESC);

CREATE INDEX IF NOT EXISTS ix_kb_tc_doc_kb ON knowledge_base.task_center_documents USING btree (knowledge_base_id);

CREATE INDEX IF NOT EXISTS ix_kb_tc_doc_state ON knowledge_base.task_center_documents USING btree (state);

CREATE UNIQUE INDEX IF NOT EXISTS uq_evo_adopt_request ON knowledge_base.evolution_runs USING btree (tenant_id, adopt_request_id) WHERE (adopt_request_id IS NOT NULL);

CREATE UNIQUE INDEX IF NOT EXISTS uq_evo_case_name ON knowledge_base.evolution_corpus_cases USING btree (tenant_id, name);

CREATE UNIQUE INDEX IF NOT EXISTS uq_evo_corpus_version_active ON knowledge_base.evolution_corpus_versions USING btree (tenant_id, knowledge_base_id, data_source_id) WHERE ((status)::text = 'active'::text);

CREATE UNIQUE INDEX IF NOT EXISTS uq_evo_run_active ON knowledge_base.evolution_runs USING btree (tenant_id) WHERE ((status)::text = ANY ((ARRAY['queued'::character varying, 'running'::character varying])::text[]));

CREATE UNIQUE INDEX IF NOT EXISTS uq_evo_run_request ON knowledge_base.evolution_runs USING btree (tenant_id, request_id) WHERE (request_id IS NOT NULL);

CREATE UNIQUE INDEX IF NOT EXISTS uq_evo_version_active ON knowledge_base.semantic_versions USING btree (tenant_id, knowledge_base_id, scope, COALESCE(data_source_id, ''::character varying)) WHERE ((status)::text = 'active'::text);

CREATE UNIQUE INDEX IF NOT EXISTS uq_gov_clearance_user ON knowledge_base.governance_user_clearance USING btree (tenant_id, user_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_gov_label_scope ON knowledge_base.governance_labels USING btree (tenant_id, knowledge_base_id, data_source_id, lower(btrim((table_name)::text)), lower(btrim((COALESCE(column_name, ''::character varying))::text)), origin) WHERE (is_deleted = 0);

CREATE UNIQUE INDEX IF NOT EXISTS uq_gov_tag_name ON knowledge_base.governance_tag_defs USING btree (tenant_id, knowledge_base_id, lower(btrim((name)::text))) WHERE (is_deleted = 0);

CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_answer_feedback_event_operation ON knowledge_base.knowledge_answer_feedback_event USING btree (operation_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_answer_feedback_history ON knowledge_base.knowledge_answer_feedback USING btree (history_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_answer_feedback_request ON knowledge_base.knowledge_answer_feedback USING btree (request_id) WHERE (request_id IS NOT NULL);

CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_ds_file_per_kb ON knowledge_base.knowledge_data_sources USING btree (tenant_id, knowledge_base_id) WHERE (((access_type)::text = 'file'::text) AND (is_deleted = 0));

CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_folders_name_per_kb ON knowledge_base.folders USING btree (tenant_id, knowledge_base_id, name) WHERE (is_deleted = 0);

CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_info_name_per_space ON knowledge_base.knowledge_info USING btree (tenant_id, space_id, name) WHERE (is_deleted = 0);

CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_metric_approval ON knowledge_base.metric_call_approvals USING btree (tenant_id, knowledge_base_id, metric_name) WHERE (is_deleted = 0);

CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_model_settings_scope ON knowledge_base.knowledge_model_settings USING btree (tenant_id, knowledge_base_id, scope);

CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_qimg_tenant_user_sha ON knowledge_base.knowledge_query_images USING btree (tenant_id, user_id, sha256) WHERE (is_deleted = 0);

CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_tags_name_per_kb ON knowledge_base.tags USING btree (tenant_id, knowledge_base_id, name) WHERE (is_deleted = 0);

CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_tc_doc_task ON knowledge_base.task_center_documents USING btree (task_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_tools_name_active ON knowledge_base.kb_tools USING btree (tenant_id, knowledge_base_id, tool_name) WHERE (is_deleted = 0);

CREATE UNIQUE INDEX IF NOT EXISTS uq_kps_parser_type_per_kb ON knowledge_base.knowledge_parser_settings USING btree (tenant_id, knowledge_base_id, parser_type) WHERE (is_deleted = 0);

CREATE UNIQUE INDEX IF NOT EXISTS uq_lfp_pair ON knowledge_base.lineage_forbidden_pairs USING btree (tenant_id, knowledge_base_id, data_source_id, lower(btrim((table_a)::text)), lower(btrim((table_b)::text))) WHERE (is_deleted = 0);

CREATE UNIQUE INDEX IF NOT EXISTS uq_lg_edge_quadruple ON knowledge_base.lineage_edges USING btree (tenant_id, knowledge_base_id, data_source_id, from_table, from_column, to_table, to_column) WHERE (is_deleted = 0);

CREATE UNIQUE INDEX IF NOT EXISTS uq_llm_wiki_schema_active ON knowledge_base.llm_wiki_schemas USING btree (tenant_id, knowledge_base_id) WHERE ((status)::text = 'active'::text);

CREATE UNIQUE INDEX IF NOT EXISTS uq_n2s_vq_name ON knowledge_base.nl2sql_verified_queries USING btree (tenant_id, knowledge_base_id, data_source_id, name) WHERE (is_deleted = 0);

CREATE UNIQUE INDEX IF NOT EXISTS uq_oas_per_kb ON knowledge_base.ontology_abstract_schemas USING btree (tenant_id, knowledge_base_id) WHERE (is_deleted = 0);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ocs_kb_level ON knowledge_base.ontology_compiled_schemas USING btree (tenant_id, knowledge_base_id) WHERE (data_source_id IS NULL);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ocs_per_data_source ON knowledge_base.ontology_compiled_schemas USING btree (tenant_id, knowledge_base_id, data_source_id) WHERE (data_source_id IS NOT NULL);

CREATE UNIQUE INDEX IF NOT EXISTS uq_oda_triple ON knowledge_base.ontology_data_alignment USING btree (tenant_id, knowledge_base_id, doc_type, data_source_id, entity_name);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ont_tool_bindings_entity_active ON knowledge_base.ontology_tool_bindings USING btree (tenant_id, knowledge_base_id, entity_type, canonical_name) WHERE (is_deleted = 0);

-- 外键约束（dump 把外键统一放在末尾，这里保持同样的做法，避免建表顺序问题）

ALTER TABLE ONLY knowledge_base.document_tags
    ADD CONSTRAINT document_tags_document_id_fkey FOREIGN KEY (document_id) REFERENCES knowledge_base.knowledge_documents(id) ON DELETE CASCADE;

ALTER TABLE ONLY knowledge_base.document_tags
    ADD CONSTRAINT document_tags_tag_id_fkey FOREIGN KEY (tag_id) REFERENCES knowledge_base.tags(id) ON DELETE CASCADE;

ALTER TABLE ONLY knowledge_base.evolution_candidates
    ADD CONSTRAINT evolution_candidates_run_id_fkey FOREIGN KEY (run_id) REFERENCES knowledge_base.evolution_runs(id) ON DELETE CASCADE;

ALTER TABLE ONLY knowledge_base.evolution_run_logs
    ADD CONSTRAINT evolution_run_logs_run_id_fkey FOREIGN KEY (run_id) REFERENCES knowledge_base.evolution_runs(id) ON DELETE CASCADE;

-- ============================================================
-- 落点 agent 运行记录（2026-11-14）
-- 存量库升级见 update/20261114190000_landing_agent_runs.sql（建表 + 列）
-- 与 update/20261114190100_landing_agent_run_params.sql（仅存量库补列，本文件
-- 的建表已含这两列，故那条 ALTER 在全新库上是 no-op）。
-- ============================================================

CREATE TABLE IF NOT EXISTS knowledge_base.abstract_landing_runs (
    id varchar(64) PRIMARY KEY,
    tenant_id varchar(64) NOT NULL,
    knowledge_base_id varchar(128) NOT NULL,
    run_mode varchar(16) NOT NULL DEFAULT 'landings',
    force boolean NOT NULL DEFAULT false,
    alias_hints text NOT NULL DEFAULT '',
    status varchar(32) NOT NULL DEFAULT 'queued',
    phase varchar(64) NOT NULL DEFAULT '',
    progress jsonb NOT NULL DEFAULT '{}'::jsonb,
    summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    error text,
    created_by varchar(128),
    started_at timestamptz,
    finished_at timestamptz,
    is_deleted smallint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_alr_kb
    ON knowledge_base.abstract_landing_runs (tenant_id, knowledge_base_id, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS uq_abstract_landing_runs_active
    ON knowledge_base.abstract_landing_runs (tenant_id, knowledge_base_id)
    WHERE status IN ('queued', 'running') AND is_deleted = 0;

-- ============================================================
-- 指标提案（metric agent 线的产出口，2026-11-14）
--   · 一条记录 = 一份「等人确认」的指标提案（公式/粒度/过滤/依据）。
--   · 状态机 pending → confirmed | rejected，一次性；改主意请重新生成。
--   · 提案不直接改抽象 schema，也不影响任何检索/问数。
--   · is_deleted 是 smallint（SoftDeleteMixin 口径），**不是 boolean**。
-- ============================================================

CREATE TABLE IF NOT EXISTS knowledge_base.metric_proposals (
    id varchar(64) PRIMARY KEY,
    tenant_id varchar(64) NOT NULL,
    knowledge_base_id varchar(128) NOT NULL,
    data_source_id varchar(128) NOT NULL DEFAULT '',
    name varchar(256) NOT NULL,
    status varchar(32) NOT NULL DEFAULT 'pending',
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_by varchar(128) NOT NULL DEFAULT '',
    decided_by varchar(128),
    decided_at timestamptz,
    decision_note text NOT NULL DEFAULT '',
    is_deleted smallint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_metric_proposals_kb
    ON knowledge_base.metric_proposals (tenant_id, knowledge_base_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_metric_proposals_status
    ON knowledge_base.metric_proposals (tenant_id, knowledge_base_id, status);

-- ============================================================
-- join 边提案（join-agent 线的产出口，2026-11-15）
--   · 一条记录 = 一条「等人确认」的 join 边（源列 → 目标列）。
--   · 状态机 pending → confirmed | rejected，一次性。
--   · 提案不写关系边；确认后由人工/后续流程落血缘。
--   · is_deleted 是 smallint（SoftDeleteMixin 口径），不是 boolean。
-- ============================================================

CREATE TABLE IF NOT EXISTS knowledge_base.join_proposals (
    id varchar(64) PRIMARY KEY,
    tenant_id varchar(64) NOT NULL,
    knowledge_base_id varchar(128) NOT NULL,
    data_source_id varchar(128) NOT NULL DEFAULT '',
    source_entity varchar(256) NOT NULL,
    source_column varchar(256) NOT NULL,
    target_entity varchar(256) NOT NULL,
    target_column varchar(256) NOT NULL,
    status varchar(32) NOT NULL DEFAULT 'pending',
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_by varchar(128) NOT NULL DEFAULT '',
    decided_by varchar(128),
    decided_at timestamptz,
    decision_note text NOT NULL DEFAULT '',
    is_deleted smallint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_join_proposals_kb
    ON knowledge_base.join_proposals (tenant_id, knowledge_base_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_join_proposals_status
    ON knowledge_base.join_proposals (tenant_id, knowledge_base_id, status);
