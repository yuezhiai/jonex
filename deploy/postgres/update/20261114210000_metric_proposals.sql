-- ============================================================
-- 指标提案表：knowledge_base.metric_proposals（增量，2026-11-14）
--
-- 与 `migrations/003_knowledge_base.sql` 末尾的那一段逐字节相同 ——
-- 存量库 `psql -f` / `make db-migrate`，全新库由 initdb 按序执行 migrations。
-- 幂等：CREATE TABLE/INDEX IF NOT EXISTS，可重复执行。
-- ============================================================
BEGIN;

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

COMMIT;
