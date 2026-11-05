-- ============================================================
-- 076_knowledge_answer_feedback_create.sql
-- 回答级反馈两张表的存量库建表（补缺口）
--
-- 为什么需要
--   knowledge_answer_feedback / knowledge_answer_feedback_event 只在全量 DDL
--   deploy/postgres/migrations/004_knowledge_base.sql 中创建，没有对应的存量库
--   增量脚本。靠 update/*.sql 升级的存量库因此没有这两张表，紧随其后的
--   076_knowledge_answer_feedback_reuse.sql 对它们做 ALTER 时直接报
--   relation ... does not exist 并中止整条迁移链。
--
-- 执行顺序
--   文件名按字典序排在 076_knowledge_answer_feedback_reuse.sql 之前
--   （"create" < "reuse"），apply.sh 会先建表、再执行 reuse 的列变更。
--
-- 内容
--   与 004_knowledge_base.sql 的「回答级反馈」段逐字一致（已含 076 reuse 引入的
--   request_id / mcp_key_id / mcp_key_name 列与可空性），两处修改必须同步。
--
-- 幂等
--   全部 CREATE ... IF NOT EXISTS：全新库（004 已建表）与已有表的存量库均为空操作；
--   之后 076_knowledge_answer_feedback_reuse.sql 的 DROP NOT NULL / ADD COLUMN IF NOT
--   EXISTS / CREATE INDEX IF NOT EXISTS 同样为空操作。
--
-- 回滚（仅当两张表为空时可执行）
--   DROP TABLE IF EXISTS knowledge_base.knowledge_answer_feedback_event;
--   DROP TABLE IF EXISTS knowledge_base.knowledge_answer_feedback;
-- ============================================================

BEGIN;

CREATE TABLE IF NOT EXISTS knowledge_base.knowledge_answer_feedback (
    id              VARCHAR(64) PRIMARY KEY,
    tenant_id       VARCHAR(64) NOT NULL,
    user_id         VARCHAR(128),
    history_id      VARCHAR(64),
    request_id      VARCHAR(64),
    query           TEXT,
    answer          TEXT,
    feedback_type   VARCHAR(16) NOT NULL,
    feedback_reason VARCHAR(32),
    feedback_comment VARCHAR(300),
    domain_space_id VARCHAR(64),
    knowledge_base_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    source          VARCHAR(64),
    mode            VARCHAR(32),
    source_missing_reason VARCHAR(64),
    mcp_key_id      VARCHAR(64),
    mcp_key_name    VARCHAR(255),
    version         INTEGER NOT NULL DEFAULT 0,
    adopted         BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_deleted      SMALLINT NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_answer_feedback_history
    ON knowledge_base.knowledge_answer_feedback (history_id);
-- MCP 幂等冲突键：同一 request_id 只允许一行（NULL 行不参与唯一性）
CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_answer_feedback_request
    ON knowledge_base.knowledge_answer_feedback (request_id)
    WHERE request_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_kb_answer_feedback_tenant_user
    ON knowledge_base.knowledge_answer_feedback (tenant_id, user_id);
CREATE INDEX IF NOT EXISTS idx_kb_answer_feedback_tenant_time
    ON knowledge_base.knowledge_answer_feedback (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_kb_answer_feedback_kb_ids
    ON knowledge_base.knowledge_answer_feedback USING GIN (knowledge_base_ids);
CREATE INDEX IF NOT EXISTS idx_kb_answer_feedback_mcp_key
    ON knowledge_base.knowledge_answer_feedback (mcp_key_id);

CREATE TABLE IF NOT EXISTS knowledge_base.knowledge_answer_feedback_event (
    id              VARCHAR(64) PRIMARY KEY,
    tenant_id       VARCHAR(64) NOT NULL,
    history_id      VARCHAR(64),
    request_id      VARCHAR(64),
    feedback_id     VARCHAR(64),
    user_id         VARCHAR(128),
    operation_id    VARCHAR(64) NOT NULL,
    version         INTEGER NOT NULL,
    feedback_type   VARCHAR(16),
    feedback_reason VARCHAR(32),
    feedback_comment VARCHAR(300),
    mcp_key_id      VARCHAR(64),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_answer_feedback_event_operation
    ON knowledge_base.knowledge_answer_feedback_event (operation_id);
CREATE INDEX IF NOT EXISTS idx_kb_answer_feedback_event_history
    ON knowledge_base.knowledge_answer_feedback_event (history_id, version DESC);
CREATE INDEX IF NOT EXISTS idx_kb_answer_feedback_event_request
    ON knowledge_base.knowledge_answer_feedback_event (request_id);

COMMENT ON TABLE knowledge_base.knowledge_answer_feedback IS '检索问答用户反馈主记录：Web 按 history_id 唯一、MCP 按 request_id 唯一，保存最新反馈状态与上下文快照';
COMMENT ON TABLE knowledge_base.knowledge_answer_feedback_event IS '检索问答用户反馈变更事件：只增不改，operation_id 幂等，保留每次变更';

COMMIT;
