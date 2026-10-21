-- 检索索引清理标记：删库批量软删的文档由后台补偿器清理索引
BEGIN;

ALTER TABLE knowledge_base.knowledge_documents
    ADD COLUMN IF NOT EXISTS indexes_cleaned_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS ix_kb_doc_index_cleanup_pending
    ON knowledge_base.knowledge_documents (updated_at)
    WHERE is_deleted = 1 AND indexes_cleaned_at IS NULL;

COMMIT;
