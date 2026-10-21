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
