-- deploy/postgres/update/056_ontology_compile_tasks.sql
-- P2：ontology-compile 独立服务的任务存储（PG）。
-- 形状对齐 `CompileTask`（task_model.py）；ontology_data 单任务实测 15–20 MB，用 JSONB。
-- 幂等：CREATE SCHEMA/TABLE/INDEX 全部 IF NOT EXISTS。
CREATE SCHEMA IF NOT EXISTS ontology_compile;

CREATE TABLE IF NOT EXISTS ontology_compile.compile_tasks (
    task_id         TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    kb_id           TEXT NOT NULL DEFAULT '',
    document_id     TEXT NOT NULL DEFAULT '',
    name            TEXT NOT NULL DEFAULT '',
    file_path       TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'created',
    current_step    TEXT NOT NULL DEFAULT '',
    progress        DOUBLE PRECISION NOT NULL DEFAULT 0,
    ontology_status TEXT NOT NULL DEFAULT '',
    ontology_schema JSONB,
    ontology_data   JSONB,
    ontology_error  TEXT NOT NULL DEFAULT '',
    config_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    timeline        JSONB NOT NULL DEFAULT '[]'::jsonb,
    error_code      TEXT,
    error_message   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_compile_tasks_scope
    ON ontology_compile.compile_tasks (tenant_id, kb_id, created_at DESC);
