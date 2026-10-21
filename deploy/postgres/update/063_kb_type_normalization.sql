-- ============================================================
-- kb_type 归一与取值域约束
--
-- 背景：`knowledge_info.kb_type` 没有 CHECK 约束，历史/手工写入可能出现
--   `OpenKB`、` openkb ` 这类大小写/空白脏值。读取侧（`get_kb_type` 等）
--   现在会 strip+lower 归一，写入侧 `resolve_creatable_kb_type` 同样归一；
--   这里把存量数据收口，并用 CHECK 约束挡住未来绕过 service 的直接写库。
--
-- 只做「大小写 + 首尾空白」归一，**不猜别名**（如 `jonex-wiki` 不会被
--   悄悄改成 `jonex_wiki`）；若存在无法归一的未知值，约束会失败并中止迁移，
--   需要人工确认后再处理。
--
-- 幂等：UPDATE 只改脏行；约束存在则跳过。
-- ============================================================
BEGIN;

-- 1) knowledge_info.kb_type：strip + lower
UPDATE knowledge_base.knowledge_info
   SET kb_type = lower(btrim(kb_type)),
       updated_at = NOW()
 WHERE kb_type IS DISTINCT FROM lower(btrim(kb_type));

-- 2) 文档快照 extra_metadata.kb_type：与库表同一口径
UPDATE knowledge_base.knowledge_documents
   SET extra_metadata = jsonb_set(
           extra_metadata,
           '{kb_type}',
           to_jsonb(lower(btrim(extra_metadata->>'kb_type'))),
           false
       ),
       updated_at = NOW()
 WHERE extra_metadata ? 'kb_type'
   AND extra_metadata->>'kb_type' IS DISTINCT FROM lower(btrim(extra_metadata->>'kb_type'));

-- 3) 取值域约束（全新库对应 migrations/012_kb_type_check.sql）
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_knowledge_info_kb_type'
    ) THEN
        ALTER TABLE knowledge_base.knowledge_info
            ADD CONSTRAINT ck_knowledge_info_kb_type
            CHECK (kb_type IN ('lightrag', 'openkb', 'jonex_wiki'));
    END IF;
END $$;

COMMIT;
