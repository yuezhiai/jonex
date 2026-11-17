-- ============================================================
-- join 边提案表：knowledge_base.join_proposals（增量，2026-11-15）
--
-- 与 `migrations/003_knowledge_base.sql` 末尾那一段逐字节相同。
-- 幂等：CREATE TABLE/INDEX IF NOT EXISTS，可重复执行。
-- ============================================================
BEGIN;

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

COMMIT;
