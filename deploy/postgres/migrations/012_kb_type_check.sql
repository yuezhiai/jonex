-- ============================================================
-- kb_type 取值域约束（全量库目标态）
-- 对应增量迁移 update/063_kb_type_normalization.sql。
-- 全量路径先跑 006 种子（取值均为 canonical），这里直接加约束。
-- ============================================================
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
