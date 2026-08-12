-- [jonex] services 表新增 enabled 列（origin/dev-llm-wiki 合并带入）
ALTER TABLE knowledge_base.services ADD COLUMN IF NOT EXISTS enabled SMALLINT DEFAULT 1;
