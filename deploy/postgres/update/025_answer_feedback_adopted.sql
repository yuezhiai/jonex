-- 025_answer_feedback_adopted.sql
-- 回答级反馈主记录补「采纳」字段 + knowledge_base_ids 的 GIN 索引（按知识库聚合查询）

ALTER TABLE knowledge_base.knowledge_answer_feedback
    ADD COLUMN IF NOT EXISTS adopted BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_kb_answer_feedback_kb_ids
    ON knowledge_base.knowledge_answer_feedback USING GIN (knowledge_base_ids);

COMMENT ON COLUMN knowledge_base.knowledge_answer_feedback.adopted IS '是否已被管理员采纳';
