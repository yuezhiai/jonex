-- 上线前提：本文件尚未在任何环境应用；若已有库应用过同名 078/020，需另加 079 做 ALTER COLUMN simhash DROP NOT NULL + 约束/索引补齐。
-- 解析后相似检测：KB 开关 + 文档签名 + 合并建议
BEGIN;

ALTER TABLE knowledge_base.knowledge_info
    ADD COLUMN IF NOT EXISTS similarity_check_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS similarity_check_threshold NUMERIC(4,3);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'ck_kb_similarity_threshold'
           AND conrelid = 'knowledge_base.knowledge_info'::regclass
           AND contype = 'c'
    ) THEN
        ALTER TABLE knowledge_base.knowledge_info
            ADD CONSTRAINT ck_kb_similarity_threshold
            CHECK (similarity_check_threshold IS NULL
                   OR (similarity_check_threshold >= 0.500
                       AND similarity_check_threshold <= 0.999));
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS knowledge_base.knowledge_document_signatures (
    document_id       VARCHAR(64) PRIMARY KEY,
    tenant_id         VARCHAR(64) NOT NULL,
    knowledge_base_id VARCHAR(128) NOT NULL,
    simhash           BIGINT,  -- NULL = 已尝试但无可用文本
    text_chars        INTEGER NOT NULL DEFAULT 0,
    text_source       VARCHAR(32) NOT NULL,
    created_at        TIMESTAMP NOT NULL DEFAULT now(),
    updated_at        TIMESTAMP NOT NULL DEFAULT now(),
    CONSTRAINT ck_kb_doc_sig_simhash
        CHECK (simhash IS NULL OR simhash >= 0)
);
CREATE INDEX IF NOT EXISTS ix_kb_doc_sig_scope
    ON knowledge_base.knowledge_document_signatures (tenant_id, knowledge_base_id);
-- unavailable 哨兵 TTL 重试查询的部分索引（simhash IS NULL 的行）
CREATE INDEX IF NOT EXISTS ix_kb_doc_sig_unavailable
    ON knowledge_base.knowledge_document_signatures (tenant_id, knowledge_base_id, updated_at)
    WHERE simhash IS NULL;

CREATE TABLE IF NOT EXISTS knowledge_base.knowledge_document_similarity_suggestions (
    id                 VARCHAR(64) PRIMARY KEY,
    tenant_id          VARCHAR(64) NOT NULL,
    knowledge_base_id  VARCHAR(128) NOT NULL,
    source_document_id VARCHAR(64) NOT NULL,
    target_document_id VARCHAR(64) NOT NULL,
    similarity         NUMERIC(5,4) NOT NULL,
    method             VARCHAR(32) NOT NULL DEFAULT 'text_lines',
    status             VARCHAR(16) NOT NULL DEFAULT 'pending',
    dismissed_by       VARCHAR(64),
    dismissed_at       TIMESTAMP,
    created_at         TIMESTAMP NOT NULL DEFAULT now(),
    updated_at         TIMESTAMP NOT NULL DEFAULT now(),
    CONSTRAINT uq_kb_doc_sim_pair
        UNIQUE (tenant_id, source_document_id, target_document_id),
    CONSTRAINT ck_kb_doc_sim_similarity
        CHECK (similarity >= 0 AND similarity <= 1),
    CONSTRAINT ck_kb_doc_sim_distinct
        CHECK (source_document_id <> target_document_id)
);
CREATE INDEX IF NOT EXISTS ix_kb_doc_sim_source
    ON knowledge_base.knowledge_document_similarity_suggestions
        (tenant_id, source_document_id, status);
CREATE INDEX IF NOT EXISTS ix_kb_doc_sim_kb
    ON knowledge_base.knowledge_document_similarity_suggestions
        (tenant_id, knowledge_base_id, status);
CREATE INDEX IF NOT EXISTS ix_kb_doc_sim_target
    ON knowledge_base.knowledge_document_similarity_suggestions
        (tenant_id, target_document_id, status);

COMMIT;
