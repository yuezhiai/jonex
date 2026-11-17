-- ============================================================
-- 落点 agent 运行记录（原 108，2026-11-14 按时间戳命名规范改名）：knowledge_base.abstract_landing_runs
--
-- 一条记录 = 一次「落点 / 构造」后台运行的状态与进度快照。
-- 前端靠轮询 `get_abstract_landing_run` 拿 progress；进程重启不丢进度。
--
-- 关键约束：
--   · 状态机 queued → running → succeeded | failed（v1 无取消，故无 cancelled）
--   · 同一 (tenant_id, knowledge_base_id) **至多一个占用中的 run**：
--     部分唯一索引 uq_abstract_landing_runs_active 兜底（queued/running 才占位，
--     软删行不占位）。多副本并发点「开始」时，只有库上的约束是全局的。
--
-- 列与 ORM 的对应（models/abstract_landing_run.py）：
--   is_deleted 是 smallint（SoftDeleteMixin 的 SmallInteger，0/1），
--   **不是 boolean** —— 本表沿用 update/106 等新表的 smallint 口径，
--   索引谓词也必须是 `is_deleted = 0`（boolean 谓词在 smallint 列上是类型错误）。
--
-- 幂等：CREATE TABLE/INDEX IF NOT EXISTS，可重复执行。
-- 应用：存量库 `psql -f` 或 `make db-migrate`；全新库由 initdb 按序执行
--       `migrations/003_knowledge_base.sql（终态建表）`（与本文件逐字节相同）——
--       两条通道都建同一张表，别只改其中一份。
-- ============================================================
BEGIN;

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

-- 「这个 KB 有没有在跑的 run」与「列出某 KB 的历史 run」两条查询共用此前缀。
CREATE INDEX IF NOT EXISTS idx_alr_kb
    ON knowledge_base.abstract_landing_runs (tenant_id, knowledge_base_id, created_at DESC);

-- 一个 KB 同时只允许一个 queued/running；终态行不占位（历史随便攒）。
CREATE UNIQUE INDEX IF NOT EXISTS uq_abstract_landing_runs_active
    ON knowledge_base.abstract_landing_runs (tenant_id, knowledge_base_id)
    WHERE status IN ('queued', 'running') AND is_deleted = 0;

COMMIT;
