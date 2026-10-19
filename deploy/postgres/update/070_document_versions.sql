-- 文档版本链：同一 doc_id 的源文件版本历史（新版生效，旧版仅存档）
BEGIN;

CREATE TABLE IF NOT EXISTS knowledge_base.knowledge_document_versions (
    id              VARCHAR(64) PRIMARY KEY,
    tenant_id       VARCHAR(64) NOT NULL,
    document_id     VARCHAR(64) NOT NULL,
    version_no      INTEGER NOT NULL,
    file_name       VARCHAR(512) NOT NULL,
    file_size       BIGINT NOT NULL DEFAULT 0,
    mime_type       VARCHAR(128),
    storage_backend VARCHAR(16) NOT NULL DEFAULT 'local',
    storage_key     VARCHAR(1024) NOT NULL,
    content_hash    VARCHAR(32),
    source          VARCHAR(32) NOT NULL DEFAULT 'upload',
    created_by      VARCHAR(128),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_kb_doc_version UNIQUE (tenant_id, document_id, version_no)
);
CREATE INDEX IF NOT EXISTS ix_kb_doc_version_doc
    ON knowledge_base.knowledge_document_versions
        (tenant_id, document_id, version_no DESC);

-- 存量文档回填为 v1（幂等）
INSERT INTO knowledge_base.knowledge_document_versions
    (id, tenant_id, document_id, version_no, file_name, file_size, mime_type,
     storage_backend, storage_key, content_hash, source, created_at)
SELECT md5(random()::text || clock_timestamp()::text || d.id),
       d.tenant_id, d.id, 1, d.file_name, d.file_size, d.mime_type,
       coalesce(d.storage_backend, 'local'), d.storage_key, d.content_hash,
       'backfill', d.created_at
  FROM knowledge_base.knowledge_documents d
 WHERE d.is_deleted = 0
   AND NOT EXISTS (
       SELECT 1 FROM knowledge_base.knowledge_document_versions v
        WHERE v.tenant_id = d.tenant_id AND v.document_id = d.id
   );

COMMIT;
