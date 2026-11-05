-- =============================================================================
-- 036 · 平台级数据源连接绑定领域空间
--
-- 背景（2026-11-02）：连接池原先是「租户级、不绑知识库」，于是切换领域后
-- 仍能看到并引用别的领域建好的连接。现在每条连接必须归属一个领域空间：
--   · 列表/详情/改/删/测/读目录都按 space_id 过滤；
--   · 知识库建数据源时，引用的连接必须与知识库同领域。
--
-- 存量数据回填（尽力而为，不丢连接）：
--   ① 被知识库引用的连接 → 取其引用知识库的 space_id；
--   ② 仍为空的（没有任何知识库引用）→ 归到该租户最早创建的那个空间。
-- 两种回填都只写 space_id IS NULL 的行，可重复执行。
-- =============================================================================

-- 新库：这张表原本只在 update/039 建；这里补齐（存量库已存在则跳过）。
CREATE TABLE IF NOT EXISTS knowledge_base.data_source_connections (
    id          VARCHAR(64)  PRIMARY KEY,
    tenant_id   VARCHAR(64)  NOT NULL,
    name        VARCHAR(255) NOT NULL,
    access_type VARCHAR(32)  NOT NULL,
    config_json JSONB        NOT NULL DEFAULT '{}'::jsonb,
    status      VARCHAR(32)  NOT NULL DEFAULT 'active',
    space_id    VARCHAR(64),
    is_deleted  SMALLINT     NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

ALTER TABLE knowledge_base.data_source_connections
    ADD COLUMN IF NOT EXISTS space_id VARCHAR(64);

CREATE INDEX IF NOT EXISTS idx_dsc_space
    ON knowledge_base.data_source_connections (tenant_id, space_id, is_deleted);

ALTER TABLE knowledge_base.knowledge_data_sources
    ADD COLUMN IF NOT EXISTS connection_id VARCHAR(64);

COMMENT ON COLUMN knowledge_base.data_source_connections.space_id IS
    '所属领域空间；连接只在同领域内可见、可被引用（036/089）';

CREATE INDEX IF NOT EXISTS idx_kds_connection
    ON knowledge_base.knowledge_data_sources (tenant_id, connection_id)
    WHERE connection_id IS NOT NULL;

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
