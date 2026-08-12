-- 010_llm_wiki_compile.sql：LLM-Wiki 编译状态列化 + 任务 ID（存量库增量更新）
-- 方案：docs/openkb/llm-wiki-compile-async-plan.md
-- 位置：deploy/postgres/update/（非 migrations/ 初始化目录——ALTER 只对存量库执行）
-- 手工执行：docker cp 进 postgres 容器后 psql -f（/docker-entrypoint-initdb.d 只在空库执行）

ALTER TABLE knowledge_base.knowledge_documents
  ADD COLUMN IF NOT EXISTS llm_wiki_compile_status       VARCHAR(32)  NULL,  -- NULL/compiling/compiled/stale/failed
  ADD COLUMN IF NOT EXISTS llm_wiki_compile_error        TEXT         NULL,
  ADD COLUMN IF NOT EXISTS llm_wiki_compile_warnings     JSONB        NULL,  -- 编译警告列表（对齐原 openkb_compile_warnings）
  ADD COLUMN IF NOT EXISTS llm_wiki_compile_requested_at TIMESTAMP    NULL,  -- patrol 超时基准
  ADD COLUMN IF NOT EXISTS llm_wiki_compiled_at          TIMESTAMP    NULL,
  ADD COLUMN IF NOT EXISTS llm_wiki_task_id              VARCHAR(128) NULL;  -- 对齐 rag_task_id

-- patrol 高频查询索引（部分索引，只覆盖 compiling；命名与 004 建表索引统一）
CREATE INDEX IF NOT EXISTS idx_kb_doc_llm_wiki_compiling
  ON knowledge_base.knowledge_documents (tenant_id, llm_wiki_compile_status)
  WHERE llm_wiki_compile_status = 'compiling';

-- 存量回填：extra_metadata 的键 → 新列
-- ⚠️ 现 JSONB 键名是 openkb_recompile_requested_at（非 compile_requested_at），
--    patrol 判超时的基准字段就是它，必须搬
UPDATE knowledge_base.knowledge_documents
   SET llm_wiki_compile_status = extra_metadata->>'openkb_compile_status',
       llm_wiki_compile_error  = extra_metadata->>'openkb_compile_error',
       llm_wiki_compile_warnings = (extra_metadata->'openkb_compile_warnings')::jsonb,
       llm_wiki_compile_requested_at =
         NULLIF(extra_metadata->>'openkb_recompile_requested_at', '')::timestamp
 WHERE extra_metadata ? 'openkb_compile_status';

-- 按 007_comments.sql 惯例补列注释
COMMENT ON COLUMN knowledge_base.knowledge_documents.llm_wiki_compile_status
  IS 'LLM-Wiki 编译状态：NULL/compiling/compiled/stale/failed';
COMMENT ON COLUMN knowledge_base.knowledge_documents.llm_wiki_compile_error
  IS 'LLM-Wiki 编译失败原因';
COMMENT ON COLUMN knowledge_base.knowledge_documents.llm_wiki_compile_warnings
  IS 'LLM-Wiki 编译警告列表（JSONB 数组）';
COMMENT ON COLUMN knowledge_base.knowledge_documents.llm_wiki_compile_requested_at
  IS 'LLM-Wiki 编译请求时刻（patrol 超时基准，UTC）';
COMMENT ON COLUMN knowledge_base.knowledge_documents.llm_wiki_compiled_at
  IS 'LLM-Wiki 编译完成时刻';
COMMENT ON COLUMN knowledge_base.knowledge_documents.llm_wiki_task_id
  IS 'OpenKB 容器编译任务 ID（对齐 rag_task_id），供对账巡检轮询';
