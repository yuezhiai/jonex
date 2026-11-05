-- =============================================================================
-- 本体编译 schema：从「一知识库一份」改为「按数据源」
--
-- 背景：编译配置现在挂在整个知识库上（UNIQUE (tenant_id, knowledge_base_id)），
--   而需求是「每个数据源有一份对应的 schema」——一个知识库可以挂多个不同数据库
--   的数据源，各自有独立的库表结构。
--
-- 归属约定：**data_source_id 为 NULL = 知识库级**（不绑定具体数据源）。
--   之所以不把既有行指派给某个数据源：kb_demo_* 那批的 schema 来自种子模板，
--   它们的数据源是 file/api，根本没有数据库数据源可指——硬指就成了孤儿。
--   保持 NULL 既零数据搬动，又不排除以后把某份 KB 级 schema 指派给数据源。
--
-- 幂等：全部 IF [NOT] EXISTS，可重复执行。
-- =============================================================================

ALTER TABLE knowledge_base.ontology_compiled_schemas
    ADD COLUMN IF NOT EXISTS data_source_id VARCHAR(64);

COMMENT ON COLUMN knowledge_base.ontology_compiled_schemas.data_source_id IS
    '所属数据源；NULL = 知识库级（不绑定具体数据源）';

-- 旧唯一约束（一知识库一份）必须放开，否则同一 KB 下挂第二份 schema 会被拒
ALTER TABLE knowledge_base.ontology_compiled_schemas
    DROP CONSTRAINT IF EXISTS ontology_compiled_schemas_tenant_id_knowledge_base_id_key;

-- ⚠️ PostgreSQL 里 UNIQUE 索引**不约束 NULL**：多行 (tenant, kb, NULL) 彼此不算冲突。
-- 所以「一个知识库只能有一份知识库级 schema」不能靠普通唯一索引保证，
-- 必须拆成两个**部分唯一索引**：
--   ① data_source_id IS NULL      → 一 KB 至多一份知识库级
--   ② data_source_id IS NOT NULL  → 一数据源至多一份
CREATE UNIQUE INDEX IF NOT EXISTS uq_ocs_kb_level
    ON knowledge_base.ontology_compiled_schemas (tenant_id, knowledge_base_id)
    WHERE data_source_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_ocs_per_data_source
    ON knowledge_base.ontology_compiled_schemas (tenant_id, knowledge_base_id, data_source_id)
    WHERE data_source_id IS NOT NULL;

-- 按数据源查 schema 是主路径，给个索引
CREATE INDEX IF NOT EXISTS idx_ocs_data_source
    ON knowledge_base.ontology_compiled_schemas (tenant_id, knowledge_base_id, data_source_id)
    WHERE data_source_id IS NOT NULL;
