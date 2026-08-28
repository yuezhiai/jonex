-- 022_answer_feedback.sql
-- 检索问答用户反馈（回答级）：主记录表 + 变更事件表
-- 锚点 history_id = knowledge_base.knowledge_search_history.id
-- 主表按问答记录唯一保存最新反馈；事件表只增不改，operation_id 幂等。

CREATE TABLE IF NOT EXISTS knowledge_base.knowledge_answer_feedback (
    id              VARCHAR(64) PRIMARY KEY,
    tenant_id       VARCHAR(64) NOT NULL,
    user_id         VARCHAR(128) NOT NULL,
    history_id      VARCHAR(64) NOT NULL,
    query           TEXT NOT NULL,
    answer          TEXT,
    feedback_type   VARCHAR(16) NOT NULL,
    feedback_reason VARCHAR(32),
    feedback_comment VARCHAR(300),
    domain_space_id VARCHAR(64),
    knowledge_base_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    source          VARCHAR(64),
    mode            VARCHAR(32),
    source_missing_reason VARCHAR(64),
    version         INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_deleted      SMALLINT NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_answer_feedback_history
    ON knowledge_base.knowledge_answer_feedback (history_id);
CREATE INDEX IF NOT EXISTS idx_kb_answer_feedback_tenant_user
    ON knowledge_base.knowledge_answer_feedback (tenant_id, user_id);
CREATE INDEX IF NOT EXISTS idx_kb_answer_feedback_tenant_time
    ON knowledge_base.knowledge_answer_feedback (tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS knowledge_base.knowledge_answer_feedback_event (
    id              VARCHAR(64) PRIMARY KEY,
    tenant_id       VARCHAR(64) NOT NULL,
    history_id      VARCHAR(64) NOT NULL,
    feedback_id     VARCHAR(64),
    user_id         VARCHAR(128) NOT NULL,
    operation_id    VARCHAR(64) NOT NULL,
    version         INTEGER NOT NULL,
    feedback_type   VARCHAR(16),
    feedback_reason VARCHAR(32),
    feedback_comment VARCHAR(300),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_answer_feedback_event_operation
    ON knowledge_base.knowledge_answer_feedback_event (operation_id);
CREATE INDEX IF NOT EXISTS idx_kb_answer_feedback_event_history
    ON knowledge_base.knowledge_answer_feedback_event (history_id, version DESC);

COMMENT ON TABLE knowledge_base.knowledge_answer_feedback IS '检索问答用户反馈主记录：按问答记录 history_id 唯一，保存最新反馈状态与上下文快照';
COMMENT ON TABLE knowledge_base.knowledge_answer_feedback_event IS '检索问答用户反馈变更事件：只增不改，operation_id 幂等，保留每次变更';
