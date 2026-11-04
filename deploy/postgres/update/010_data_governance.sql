-- ============================================================
-- 010 数据治理控制面 DDL（2026-11-04 由 010 / 011 / 013 / 014 / 018 归并）
--
-- 覆盖：data_governance schema + asset_metadata / retention_policy /
--       classification_level·category·default / user_clearance /
--       asset_metadata 类型白名单
--
-- 应用：空卷随 initdb 自动执行；存量库由 update/010_data_governance.sql
--       （逐字节镜像）经 `make db-migrate` 补齐。
-- 幂等：全部 CREATE/ALTER ... IF [NOT] EXISTS，重复执行安全。
-- ============================================================

-- ------------------------------------------------------------------
-- 来源：migrations/010_data_governance.sql
-- ------------------------------------------------------------------

-- ============================================================
-- Jonex 平台数据库初始化 - 数据治理 (data_governance schema)
-- 版本: 010
-- ============================================================
-- 数据治理第 2 期：资产目录元数据侧表（文档 / 数据源 / 表 / 列 / 指标）。
-- 幂等：CREATE SCHEMA/TABLE/INDEX 全部 IF NOT EXISTS，可重复执行。
-- ============================================================
CREATE SCHEMA IF NOT EXISTS data_governance;

CREATE TABLE IF NOT EXISTS data_governance.asset_metadata (
    id           VARCHAR(64) PRIMARY KEY,
    tenant_id    VARCHAR(64)  NOT NULL,
    asset_type   VARCHAR(32)  NOT NULL,
    asset_id     VARCHAR(255) NOT NULL,
    owner        VARCHAR(128),
    domain       VARCHAR(64),
    sensitivity  VARCHAR(32),
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_asset_metadata UNIQUE (tenant_id, asset_type, asset_id),
    CONSTRAINT ck_asset_metadata_type CHECK (
        asset_type IN ('document','data_source','table','column','metric')
    ),
    CONSTRAINT ck_asset_metadata_sensitivity CHECK (
        sensitivity IS NULL OR sensitivity IN ('public', 'internal', 'confidential')
    )
);

CREATE INDEX IF NOT EXISTS ix_asset_metadata_owner
    ON data_governance.asset_metadata (tenant_id, owner);
CREATE INDEX IF NOT EXISTS ix_asset_metadata_domain
    ON data_governance.asset_metadata (tenant_id, domain);
CREATE INDEX IF NOT EXISTS ix_asset_metadata_sensitivity
    ON data_governance.asset_metadata (tenant_id, sensitivity);

-- ------------------------------------------------------------------
-- 来源：migrations/011_lifecycle_retention.sql
-- ------------------------------------------------------------------

-- ============================================================
-- Jonex 平台数据库初始化 - 生命周期与保留策略
-- 版本: 011
-- ============================================================
-- 数据治理第 3 期：三级保留策略表 + 文档归档时间列。
-- 幂等：CREATE/ALTER 全部 IF NOT EXISTS，可重复执行。
CREATE SCHEMA IF NOT EXISTS data_governance;

CREATE TABLE IF NOT EXISTS data_governance.retention_policy (
    id             VARCHAR(64) PRIMARY KEY,
    tenant_id      VARCHAR(64) NOT NULL,
    scope_type     VARCHAR(16) NOT NULL,
    scope_id       VARCHAR(64) NOT NULL DEFAULT '',
    retention_days INTEGER,
    grace_days     INTEGER,
    updated_by     VARCHAR(128),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_retention_scope UNIQUE (tenant_id, scope_type, scope_id),
    CONSTRAINT ck_retention_scope_type CHECK (scope_type IN ('tenant', 'kb', 'document')),
    -- tenant 作用域必须 scope_id=''；kb/document 必须非空。
    CONSTRAINT ck_retention_scope_pair CHECK ((scope_type = 'tenant') = (scope_id = '')),
    CONSTRAINT ck_retention_days CHECK (retention_days IS NULL OR retention_days >= 0),
    CONSTRAINT ck_retention_grace CHECK (grace_days IS NULL OR grace_days >= 0)
);

COMMENT ON TABLE data_governance.retention_policy
    IS '数据治理三级保留策略（tenant/kb/document）；NULL 表示继承上级策略。';
COMMENT ON COLUMN data_governance.retention_policy.retention_days
    IS '保留天数：NULL=继承上级；0=不自动到期；>0=按天数到期。';
COMMENT ON COLUMN data_governance.retention_policy.grace_days
    IS '宽限天数：NULL=继承上级；0=无宽限；>0=宽限天数。';

ALTER TABLE knowledge_base.knowledge_documents
    ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP;

COMMENT ON COLUMN knowledge_base.knowledge_documents.archived_at
    IS '归档时间（naive TIMESTAMP，与本表 created_at/updated_at 同时区口径）；NULL=未归档。';

CREATE INDEX IF NOT EXISTS idx_kb_doc_retention_scan
    ON knowledge_base.knowledge_documents (tenant_id, status, created_at)
    WHERE is_deleted = 0;

CREATE INDEX IF NOT EXISTS idx_kb_doc_archive_scan
    ON knowledge_base.knowledge_documents (tenant_id, status, archived_at)
    WHERE is_deleted = 0 AND archived_at IS NOT NULL;

-- 保留扫描的窗口排序键：候选集按 tenant 分区、每租户按 created_at 排序
-- （`row_number() OVER (PARTITION BY tenant_id ORDER BY created_at)`），
-- 所以列顺序是 (tenant_id, created_at)。这条与 062 同步。
CREATE INDEX IF NOT EXISTS idx_kb_doc_created_at_scan
    ON knowledge_base.knowledge_documents (tenant_id, created_at)
    WHERE is_deleted = 0;

-- ------------------------------------------------------------------
-- 来源：migrations/013_classification.sql
-- ------------------------------------------------------------------

-- ============================================================
-- 数据治理第 4 期：分级分类 MVP — 词表与默认值（全量库目标态）
-- 对应增量迁移 update/064_classification.sql。
-- 全量路径在 010_data_governance.sql 建好 asset_metadata 后执行。
-- ============================================================

CREATE TABLE IF NOT EXISTS data_governance.classification_level (
    id          VARCHAR(64) PRIMARY KEY,
    tenant_id   VARCHAR(64) NOT NULL,
    code        VARCHAR(32) NOT NULL,
    name        VARCHAR(64) NOT NULL,
    rank        INTEGER NOT NULL,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_classification_level_code UNIQUE (tenant_id, code),
    CONSTRAINT ck_classification_level_code CHECK (code ~ '^[a-z][a-z0-9_]{1,31}$'),
    CONSTRAINT ck_classification_level_rank CHECK (rank >= 1 AND rank <= 1000),
    CONSTRAINT ck_classification_level_name CHECK (length(btrim(name)) BETWEEN 1 AND 64)
);

CREATE INDEX IF NOT EXISTS ix_classification_level_tenant
    ON data_governance.classification_level (tenant_id, rank);

CREATE TABLE IF NOT EXISTS data_governance.classification_category (
    id          VARCHAR(64) PRIMARY KEY,
    tenant_id   VARCHAR(64) NOT NULL,
    code        VARCHAR(32) NOT NULL,
    name        VARCHAR(64) NOT NULL,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_classification_category_code UNIQUE (tenant_id, code),
    CONSTRAINT ck_classification_category_code CHECK (code ~ '^[a-z][a-z0-9_]{1,31}$'),
    CONSTRAINT ck_classification_category_name CHECK (length(btrim(name)) BETWEEN 1 AND 64)
);

CREATE INDEX IF NOT EXISTS ix_classification_category_tenant
    ON data_governance.classification_category (tenant_id);

CREATE TABLE IF NOT EXISTS data_governance.classification_default (
    id          VARCHAR(64) PRIMARY KEY,
    tenant_id   VARCHAR(64) NOT NULL,
    scope_type  VARCHAR(16) NOT NULL,
    scope_id    VARCHAR(64) NOT NULL DEFAULT '',
    level_code  VARCHAR(32),
    categories  JSONB,
    updated_by  VARCHAR(128),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_classification_default_scope UNIQUE (tenant_id, scope_type, scope_id),
    CONSTRAINT ck_classification_default_scope_type CHECK (scope_type IN ('tenant', 'kb')),
    CONSTRAINT ck_classification_default_tenant_scope
        CHECK (scope_type <> 'tenant' OR scope_id = ''),
    CONSTRAINT ck_classification_default_categories
        CHECK (categories IS NULL OR jsonb_typeof(categories) = 'array')
);

ALTER TABLE data_governance.asset_metadata
    DROP CONSTRAINT IF EXISTS ck_asset_metadata_sensitivity;

ALTER TABLE data_governance.asset_metadata
    ADD COLUMN IF NOT EXISTS classification_categories JSONB;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_asset_metadata_classification_categories'
    ) THEN
        ALTER TABLE data_governance.asset_metadata
            ADD CONSTRAINT ck_asset_metadata_classification_categories
            CHECK (classification_categories IS NULL
                   OR jsonb_typeof(classification_categories) = 'array');
    END IF;
END $$;

-- ------------------------------------------------------------------
-- 来源：migrations/014_user_clearance.sql
-- ------------------------------------------------------------------

-- B6 访问侧分级保护：用户可读最高等级
CREATE TABLE IF NOT EXISTS data_governance.user_clearance (
    id             VARCHAR(64) PRIMARY KEY,
    tenant_id      VARCHAR(64) NOT NULL,
    user_id        VARCHAR(64) NOT NULL,
    max_level_code VARCHAR(32) NOT NULL,
    updated_by     VARCHAR(128),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_user_clearance_user UNIQUE (tenant_id, user_id),
    CONSTRAINT ck_user_clearance_level_code
        CHECK (max_level_code ~ '^[a-z][a-z0-9_]{1,31}$')
);
CREATE INDEX IF NOT EXISTS ix_user_clearance_tenant
    ON data_governance.user_clearance (tenant_id);

-- ------------------------------------------------------------------
-- 来源：migrations/018_asset_metadata_types.sql
-- ------------------------------------------------------------------

-- 资产元数据类型白名单扩展：table / column / metric（W2.1）。
-- 幂等：DROP IF EXISTS + ADD，可重复执行；BEGIN/COMMIT 原子化，防 ADD 失败后约束缺失。
BEGIN;
ALTER TABLE data_governance.asset_metadata
  DROP CONSTRAINT IF EXISTS ck_asset_metadata_type;
ALTER TABLE data_governance.asset_metadata
  ADD CONSTRAINT ck_asset_metadata_type
  CHECK (asset_type IN ('document','data_source','table','column','metric'));
-- 派生资产 id 为 `{data_source_id}:{table}.{column}`，64 装不下。
ALTER TABLE data_governance.asset_metadata
  ALTER COLUMN asset_id TYPE VARCHAR(255);
COMMIT;
