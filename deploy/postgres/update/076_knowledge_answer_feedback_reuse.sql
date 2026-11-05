-- ============================================================
-- 076_knowledge_answer_feedback_reuse.sql
-- MCP 检索反馈入口：回答反馈表复用为「按 request_id 锚定」，三表变更
--
-- 为什么需要
--   MCP 链路固定关闭检索历史，`knowledge_search_history` 不再生成行，
--   现有反馈主表的唯一锚点 `history_id` 随之消失。MCP 反馈改为以
--   `search_request_event.request_id`（能力信封 request_id）为锚点：
--     1) 主表 `knowledge_answer_feedback` 放开 user_id / history_id / query，
--        新增 request_id / mcp_key_id / mcp_key_name；
--     2) 事件表 `knowledge_answer_feedback_event` 同步放开 user_id / history_id，
--        新增 request_id / mcp_key_id；
--     3) 请求事件表 `search_request_event` 落 mcp_key_id，供数据最小化与
--        「情况追踪」按 Key 筛选。
--   契约（2026-10-21 冻结）见 docs/ai-mcp/mcp-feedback-and-history-contract.md。
--
-- 语义要点
--   * `uq_kb_answer_feedback_history (history_id)` 保留：PostgreSQL 唯一索引
--     允许多个 NULL，MCP 行 history_id 全为 NULL，不互相冲突，也不影响 Web 行。
--   * 新的幂等冲突键是 `request_id`，但**只对非 NULL 行唯一**（部分唯一索引）：
--       - Web 反馈（request_id IS NULL）继续靠 history_id 唯一；
--       - MCP 反馈（request_id 非空）同一 request_id 只允许一行主记录，
--         多条 history_id 为 NULL 的行可共存。
--   * 不迁移/清理存量数据；存量行 request_id / mcp_key_id / mcp_key_name 全为 NULL，
--     天然表现为「Web 来源」。
--
-- 幂等
--   ALTER COLUMN ... DROP NOT NULL 与 COMMENT 本身幂等；
--   ADD COLUMN / CREATE INDEX 均带 IF NOT EXISTS，重复执行不报错。
--   本文件只由 `deploy/postgres/update/apply.sh` 人工触发（make db-migrate），
--   不在共享库或生产库自动执行。
--
-- 回滚（结构回退；仅当确认无 MCP 反馈行、即三张表新增列全为 NULL 时可执行）
--   DROP INDEX IF EXISTS knowledge_base.uq_kb_answer_feedback_request;
--   DROP INDEX IF EXISTS knowledge_base.idx_kb_answer_feedback_mcp_key;
--   DROP INDEX IF EXISTS knowledge_base.idx_kb_answer_feedback_event_request;
--   DROP INDEX IF EXISTS knowledge_base.idx_sre_mcp_key;
--   ALTER TABLE knowledge_base.knowledge_answer_feedback
--       DROP COLUMN IF EXISTS request_id,
--       DROP COLUMN IF EXISTS mcp_key_id,
--       DROP COLUMN IF EXISTS mcp_key_name;
--   ALTER TABLE knowledge_base.knowledge_answer_feedback_event
--       DROP COLUMN IF EXISTS request_id,
--       DROP COLUMN IF EXISTS mcp_key_id;
--   ALTER TABLE knowledge_base.search_request_event
--       DROP COLUMN IF EXISTS mcp_key_id;
--   -- 可空性回退需先清理 MCP 行，否则 UPDATE 会失败：
--   -- ALTER TABLE ... ALTER COLUMN history_id SET NOT NULL;（等，按需人工执行）
-- ============================================================

BEGIN;

-- ------------------------------------------------------------
-- 1. 回答反馈主表
-- ------------------------------------------------------------
ALTER TABLE knowledge_base.knowledge_answer_feedback
    ALTER COLUMN history_id DROP NOT NULL,
    ALTER COLUMN user_id DROP NOT NULL,
    ALTER COLUMN query DROP NOT NULL;

ALTER TABLE knowledge_base.knowledge_answer_feedback
    ADD COLUMN IF NOT EXISTS request_id VARCHAR(64),
    ADD COLUMN IF NOT EXISTS mcp_key_id VARCHAR(64),
    ADD COLUMN IF NOT EXISTS mcp_key_name VARCHAR(255);

COMMENT ON COLUMN knowledge_base.knowledge_answer_feedback.request_id IS
    'MCP 检索请求 id（= search_request_event.request_id，能力信封 request_id）；Web 反馈为 NULL';
COMMENT ON COLUMN knowledge_base.knowledge_answer_feedback.mcp_key_id IS
    '提交反馈的 MCP Key id 快照；Web 反馈为 NULL（情况追踪据此区分来源）';
COMMENT ON COLUMN knowledge_base.knowledge_answer_feedback.mcp_key_name IS
    '提交时的 MCP Key 名称快照（跨 schema 只读取 platform.mcp_keys.name）；Key 改名不影响历史记录';

-- 幂等冲突键：同一 request_id 只允许一行主记录（NULL 行不参与）
CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_answer_feedback_request
    ON knowledge_base.knowledge_answer_feedback (request_id)
    WHERE request_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_kb_answer_feedback_mcp_key
    ON knowledge_base.knowledge_answer_feedback (mcp_key_id);

-- ------------------------------------------------------------
-- 2. 回答反馈事件表
-- ------------------------------------------------------------
ALTER TABLE knowledge_base.knowledge_answer_feedback_event
    ALTER COLUMN history_id DROP NOT NULL,
    ALTER COLUMN user_id DROP NOT NULL;

ALTER TABLE knowledge_base.knowledge_answer_feedback_event
    ADD COLUMN IF NOT EXISTS request_id VARCHAR(64),
    ADD COLUMN IF NOT EXISTS mcp_key_id VARCHAR(64);

COMMENT ON COLUMN knowledge_base.knowledge_answer_feedback_event.request_id IS
    'MCP 检索请求 id（与主表同源）；Web 反馈为 NULL';
COMMENT ON COLUMN knowledge_base.knowledge_answer_feedback_event.mcp_key_id IS
    '提交反馈的 MCP Key id 快照；Web 反馈为 NULL';

CREATE INDEX IF NOT EXISTS idx_kb_answer_feedback_event_request
    ON knowledge_base.knowledge_answer_feedback_event (request_id);

-- ------------------------------------------------------------
-- 3. 检索请求事件（保留行、清空 query/query_hash，落 mcp_key_id）
-- ------------------------------------------------------------
ALTER TABLE knowledge_base.search_request_event
    ADD COLUMN IF NOT EXISTS mcp_key_id VARCHAR(64);

COMMENT ON COLUMN knowledge_base.search_request_event.mcp_key_id IS
    '产生该请求的 MCP Key id；Web/其他链路为 NULL。MCP 行 query/query_hash 恒为空（数据最小化）';

CREATE INDEX IF NOT EXISTS idx_sre_mcp_key
    ON knowledge_base.search_request_event (mcp_key_id);

COMMIT;
