-- ============================================================================
-- 计量库：调用明细与用量视图
-- ============================================================================

CREATE TABLE IF NOT EXISTS metering.llm_usage_daily (
    day_local date NOT NULL,
    tenant_id character varying(64) NOT NULL,
    scene character varying(64) NOT NULL,
    model character varying(128) NOT NULL,
    call_count bigint DEFAULT 0 NOT NULL,
    prompt_tokens bigint DEFAULT 0 NOT NULL,
    completion_tokens bigint DEFAULT 0 NOT NULL,
    total_tokens bigint DEFAULT 0 NOT NULL,
    avg_latency_ms integer
);

CREATE TABLE IF NOT EXISTS metering.llm_usage_log (
    id bigint NOT NULL,
    request_id character varying(64),
    trace_id character varying(64),
    tenant_id character varying(64) NOT NULL,
    user_id character varying(64),
    scene character varying(64) NOT NULL,
    model character varying(128) NOT NULL,
    kb_id character varying(64),
    doc_id character varying(64),
    prompt_tokens integer DEFAULT 0,
    completion_tokens integer DEFAULT 0,
    total_tokens integer DEFAULT 0,
    latency_ms integer,
    is_stream boolean DEFAULT false,
    is_estimated boolean DEFAULT false,
    call_count integer DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);

CREATE SEQUENCE IF NOT EXISTS metering.llm_usage_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE metering.llm_usage_log_id_seq OWNED BY metering.llm_usage_log.id;

DROP VIEW IF EXISTS metering.v_llm_usage_by_doc;

CREATE VIEW metering.v_llm_usage_by_doc AS
 SELECT u.tenant_id,
    ki.name AS kb_name,
    kd.file_name AS doc_name,
    u.scene,
    u.model,
    sum(u.call_count) AS call_count,
    sum(u.prompt_tokens) AS prompt_tokens,
    sum(u.completion_tokens) AS completion_tokens,
    sum(u.total_tokens) AS total_tokens,
    round(avg(u.latency_ms)) AS avg_latency_ms,
    min((u.created_at AT TIME ZONE 'Asia/Shanghai'::text)) AS first_local,
    max((u.created_at AT TIME ZONE 'Asia/Shanghai'::text)) AS last_local
   FROM ((metering.llm_usage_log u
     LEFT JOIN knowledge_base.knowledge_info ki ON (((ki.id)::text = (u.kb_id)::text)))
     LEFT JOIN knowledge_base.knowledge_documents kd ON (((kd.id)::text = (u.doc_id)::text)))
  GROUP BY u.tenant_id, ki.name, kd.file_name, u.scene, u.model;

DROP VIEW IF EXISTS metering.v_llm_usage_by_trace;

CREATE VIEW metering.v_llm_usage_by_trace AS
 SELECT u.tenant_id,
    u.trace_id,
    u.scene,
    u.model,
    sum(u.call_count) AS call_count,
    sum(u.total_tokens) AS total_tokens,
    round(avg(u.latency_ms)) AS avg_latency_ms,
    min((u.created_at AT TIME ZONE 'Asia/Shanghai'::text)) AS first_local,
    max((u.created_at AT TIME ZONE 'Asia/Shanghai'::text)) AS last_local
   FROM metering.llm_usage_log u
  GROUP BY u.tenant_id, u.trace_id, u.scene, u.model;

DROP VIEW IF EXISTS metering.v_llm_usage_daily;

CREATE VIEW metering.v_llm_usage_daily AS
 SELECT ((u.created_at AT TIME ZONE 'Asia/Shanghai'::text))::date AS day_local,
    u.tenant_id,
    u.scene,
    u.model,
    sum(u.call_count) AS call_count,
    sum(u.prompt_tokens) AS prompt_tokens,
    sum(u.completion_tokens) AS completion_tokens,
    sum(u.total_tokens) AS total_tokens,
    round(avg(u.latency_ms)) AS avg_latency_ms
   FROM metering.llm_usage_log u
  GROUP BY (((u.created_at AT TIME ZONE 'Asia/Shanghai'::text))::date), u.tenant_id, u.scene, u.model;

DROP VIEW IF EXISTS metering.v_llm_usage_detail;

CREATE VIEW metering.v_llm_usage_detail AS
 SELECT u.id,
    (u.created_at AT TIME ZONE 'Asia/Shanghai'::text) AS created_local,
    u.tenant_id,
    u.scene,
    u.model,
    ki.name AS kb_name,
    kd.file_name AS doc_name,
    u.prompt_tokens,
    u.completion_tokens,
    u.total_tokens,
    u.latency_ms,
    u.is_stream,
    u.is_estimated,
    u.trace_id,
    u.request_id,
    u.kb_id,
    u.doc_id
   FROM ((metering.llm_usage_log u
     LEFT JOIN knowledge_base.knowledge_info ki ON (((ki.id)::text = (u.kb_id)::text)))
     LEFT JOIN knowledge_base.knowledge_documents kd ON (((kd.id)::text = (u.doc_id)::text)));

ALTER TABLE ONLY metering.llm_usage_log ALTER COLUMN id SET DEFAULT nextval('metering.llm_usage_log_id_seq'::regclass);

ALTER TABLE ONLY metering.llm_usage_daily
    ADD CONSTRAINT llm_usage_daily_pkey PRIMARY KEY (day_local, tenant_id, scene, model);

ALTER TABLE ONLY metering.llm_usage_log
    ADD CONSTRAINT llm_usage_log_pkey PRIMARY KEY (id);

ALTER TABLE ONLY metering.llm_usage_log
    ADD CONSTRAINT llm_usage_log_request_id_key UNIQUE (request_id);

CREATE INDEX IF NOT EXISTS idx_llm_usage_created ON metering.llm_usage_log USING btree (created_at);

CREATE INDEX IF NOT EXISTS idx_llm_usage_daily_tenant ON metering.llm_usage_daily USING btree (tenant_id, day_local);

CREATE INDEX IF NOT EXISTS idx_llm_usage_scene ON metering.llm_usage_log USING btree (scene);

CREATE INDEX IF NOT EXISTS idx_llm_usage_tenant_time ON metering.llm_usage_log USING btree (tenant_id, created_at);

CREATE INDEX IF NOT EXISTS idx_llm_usage_trace ON metering.llm_usage_log USING btree (trace_id);
