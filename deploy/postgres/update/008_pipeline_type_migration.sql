-- ── [jonex] kb_type 从 service_knowledge_bases 迁移到 knowledge_info ──
-- 原名 pipeline_type，迁移时重命名为 kb_type（知识库自身的一级属性）。

BEGIN;

-- ① 在 knowledge_info 新增列（幂等）
ALTER TABLE knowledge_base.knowledge_info
    ADD COLUMN IF NOT EXISTS kb_type VARCHAR(16) NOT NULL DEFAULT 'lightrag';

-- ② 从 service_knowledge_bases 回填数据
--    一 KB 多服务时可能有冲突：按 DISTINCT ON (kb_id) 去重，
--    优先取 openkb（显式 CASE，不依赖字母序）。
UPDATE knowledge_base.knowledge_info ki
SET kb_type = sub.pipeline_type
FROM (
    SELECT DISTINCT ON (kb_id)
        kb_id,
        pipeline_type
    FROM knowledge_base.service_knowledge_bases
    WHERE is_deleted = 0
    ORDER BY kb_id,
             CASE pipeline_type WHEN 'openkb' THEN 0 ELSE 1 END,
             pipeline_type
) sub
WHERE ki.id = sub.kb_id
  AND ki.kb_type <> sub.pipeline_type;  -- 幂等：已一致的行不重复写

-- ③ 文档 extra_metadata 快照：新增 kb_type key，保留 pipeline_type key
--    旧代码读旧 key、新代码读新 key，互不干扰。回滚也安全。
--    此步不可省：省了会导致对账逐条回落查 DB。
UPDATE knowledge_base.knowledge_documents
SET extra_metadata = jsonb_set(
        extra_metadata,
        '{kb_type}',
        extra_metadata -> 'pipeline_type',
        true
    )
WHERE extra_metadata ? 'pipeline_type'
  AND NOT (extra_metadata ? 'kb_type');  -- 幂等：已有新 key 的行跳过

COMMIT;

-- ④ 删除 service_knowledge_bases 旧列（单独执行，观察 1 周后确认无误再跑）
-- ALTER TABLE knowledge_base.service_knowledge_bases DROP COLUMN IF EXISTS pipeline_type;
