-- 011_document_content_hash.sql：知识文档源文件内容 hash（上传去重 + reparse 跳过）（存量库增量更新）
-- 方案：知识库上传内容去重校验（同 KB 内相同内容文件拒绝重复上传）
-- 位置：deploy/postgres/update/（非 migrations/ 初始化目录——ALTER 只对存量库执行）
-- 手工执行：docker cp 进 postgres 容器后 psql -f（/docker-entrypoint-initdb.d 只在空库执行）
-- 全新库无需执行：migrations/004_knowledge_base.sql 建表已含同款列与索引
-- content_hash：源文件内容 md5（hex 32），上传时持久化，用于同一知识库内内容去重，
-- 并作为 reparse 跳过「源文件未变化」的单一事实来源。md5 仅用于去重/比对，非安全用途。

ALTER TABLE knowledge_base.knowledge_documents
    ADD COLUMN IF NOT EXISTS content_hash VARCHAR(32);

-- 加速去重查询：同 KB 内按 hash 找活跃文档（未删除且非 failed）
CREATE INDEX IF NOT EXISTS idx_kb_doc_content_hash
    ON knowledge_base.knowledge_documents (tenant_id, knowledge_base_id, content_hash)
    WHERE is_deleted = 0 AND status <> 'failed';
