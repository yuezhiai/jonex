-- =============================================================================
-- 089 · 平台级数据源连接绑定领域空间（存量库增量）
--
-- 表由 update/039 创建。本脚本只加列、加索引并回填；全部幂等，
-- apply.sh 可重复执行。回填口径见 migrations/036 同名脚本的注释。
-- =============================================================================

ALTER TABLE knowledge_base.data_source_connections
    ADD COLUMN IF NOT EXISTS space_id VARCHAR(64);

CREATE INDEX IF NOT EXISTS idx_dsc_space
    ON knowledge_base.data_source_connections (tenant_id, space_id, is_deleted);

COMMENT ON COLUMN knowledge_base.data_source_connections.space_id IS
    '所属领域空间；连接只在同领域内可见、可被引用（036/089）';

-- ① 按引用它的知识库回填
UPDATE knowledge_base.data_source_connections c
SET space_id = ref.space_id
FROM (
    SELECT DISTINCT ON (kds.connection_id)
           kds.connection_id, ki.space_id
    FROM knowledge_base.knowledge_data_sources kds
    JOIN knowledge_base.knowledge_info ki
      ON ki.id = kds.knowledge_base_id
     AND ki.tenant_id = kds.tenant_id
    WHERE kds.connection_id IS NOT NULL
      AND kds.is_deleted = 0
      AND ki.is_deleted = 0
      AND ki.space_id IS NOT NULL
      AND ki.space_id <> ''
    ORDER BY kds.connection_id, kds.created_at
) ref
WHERE c.id = ref.connection_id
  AND c.space_id IS NULL;

-- ② 孤连接归到该租户最早的空间（没有空间的租户保持 NULL，由前端提示先建空间）
UPDATE knowledge_base.data_source_connections c
SET space_id = first_space.space_id
FROM (
    SELECT DISTINCT ON (tenant_id)
           tenant_id, id AS space_id
    FROM knowledge_base.spaces
    WHERE is_deleted = 0
      AND status = 'active'
    ORDER BY tenant_id, created_at
) first_space
WHERE c.space_id IS NULL
  AND c.tenant_id = first_space.tenant_id;
