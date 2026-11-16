-- ============================================================================
-- 监控库：调用链与尝试记录
-- ============================================================================

CREATE TABLE IF NOT EXISTS monitor.call_attempt (
    id bigint NOT NULL,
    attempt_id character varying(64) NOT NULL,
    request_id character varying(64) NOT NULL,
    attempt_no integer NOT NULL,
    tenant_id character varying(64) NOT NULL,
    attempt_started_at timestamp with time zone,
    attempt_ended_at timestamp with time zone,
    status character varying(16) NOT NULL,
    error_code character varying(64),
    error_message text,
    is_final boolean DEFAULT false NOT NULL,
    wait_before_ms integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE SEQUENCE IF NOT EXISTS monitor.call_attempt_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE monitor.call_attempt_id_seq OWNED BY monitor.call_attempt.id;

CREATE TABLE IF NOT EXISTS monitor.call_log (
    id bigint NOT NULL,
    request_id character varying(64) NOT NULL,
    trace_id character varying(64),
    tenant_id character varying(64) NOT NULL,
    direction character varying(16) NOT NULL,
    source character varying(128),
    target character varying(128),
    protocol character varying(32),
    trigger_type character varying(16) DEFAULT 'user'::character varying NOT NULL,
    logical_started_at timestamp with time zone,
    final_ended_at timestamp with time zone,
    final_status character varying(16) NOT NULL,
    error_code character varying(64),
    error_message text,
    attempt_count integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    mcp_tool character varying(128),
    call_scope character varying(16),
    CONSTRAINT ck_monitor_call_log_direction CHECK (((direction)::text = ANY ((ARRAY['outbound'::character varying, 'inbound'::character varying])::text[]))),
    CONSTRAINT ck_monitor_call_log_final_status CHECK (((final_status)::text = ANY ((ARRAY['success'::character varying, 'failed'::character varying, 'cancelled'::character varying, 'running'::character varying, 'unknown'::character varying])::text[]))),
    CONSTRAINT ck_monitor_call_log_scope CHECK (((call_scope IS NULL) OR (((direction)::text = 'inbound'::text) AND ((call_scope)::text = 'open'::text)) OR (((direction)::text = 'outbound'::text) AND ((call_scope)::text = ANY ((ARRAY['external'::character varying, 'internal'::character varying])::text[]))))),
    CONSTRAINT ck_monitor_call_log_trigger CHECK (((trigger_type)::text = ANY ((ARRAY['user'::character varying, 'system'::character varying])::text[])))
);

CREATE SEQUENCE IF NOT EXISTS monitor.call_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE monitor.call_log_id_seq OWNED BY monitor.call_log.id;

ALTER TABLE ONLY monitor.call_attempt ALTER COLUMN id SET DEFAULT nextval('monitor.call_attempt_id_seq'::regclass);

ALTER TABLE ONLY monitor.call_log ALTER COLUMN id SET DEFAULT nextval('monitor.call_log_id_seq'::regclass);

ALTER TABLE ONLY monitor.call_attempt
    ADD CONSTRAINT call_attempt_pkey PRIMARY KEY (id);

ALTER TABLE ONLY monitor.call_log
    ADD CONSTRAINT call_log_pkey PRIMARY KEY (id);

ALTER TABLE ONLY monitor.call_attempt
    ADD CONSTRAINT uq_monitor_call_attempt_id UNIQUE (attempt_id);

ALTER TABLE ONLY monitor.call_attempt
    ADD CONSTRAINT uq_monitor_call_attempt_req_no UNIQUE (request_id, attempt_no);

ALTER TABLE ONLY monitor.call_log
    ADD CONSTRAINT uq_monitor_call_log_request_id UNIQUE (request_id);

CREATE INDEX IF NOT EXISTS idx_monitor_call_attempt_created ON monitor.call_attempt USING btree (created_at, id);

CREATE INDEX IF NOT EXISTS idx_monitor_call_attempt_request ON monitor.call_attempt USING btree (request_id);

CREATE INDEX IF NOT EXISTS idx_monitor_call_attempt_started ON monitor.call_attempt USING btree (attempt_started_at DESC);

CREATE INDEX IF NOT EXISTS idx_monitor_call_log_created ON monitor.call_log USING btree (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_monitor_call_log_dir_status_started ON monitor.call_log USING btree (direction, final_status, logical_started_at DESC);

CREATE INDEX IF NOT EXISTS idx_monitor_call_log_logical_started ON monitor.call_log USING btree (logical_started_at, id);

CREATE INDEX IF NOT EXISTS idx_monitor_call_log_scope_started ON monitor.call_log USING btree (call_scope, logical_started_at DESC);

CREATE INDEX IF NOT EXISTS idx_monitor_call_log_target_started ON monitor.call_log USING btree (target, logical_started_at DESC);

CREATE INDEX IF NOT EXISTS idx_monitor_call_log_tenant_started ON monitor.call_log USING btree (tenant_id, logical_started_at DESC);

CREATE INDEX IF NOT EXISTS idx_monitor_call_log_trace ON monitor.call_log USING btree (trace_id);

CREATE INDEX IF NOT EXISTS idx_monitor_call_log_trigger_started ON monitor.call_log USING btree (trigger_type, logical_started_at DESC);
