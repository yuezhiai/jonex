-- ============================================================
-- Jonex 平台数据库初始化 - 数据治理 (data_governance schema)
-- 版本: 010
-- ============================================================
-- 数据治理第 2 期：资产目录元数据侧表（文档 / 数据源）。
-- 幂等：CREATE SCHEMA/TABLE/INDEX 全部 IF NOT EXISTS，可重复执行。
-- ============================================================
CREATE SCHEMA IF NOT EXISTS data_governance;

CREATE TABLE IF NOT EXISTS data_governance.asset_metadata (
    id           VARCHAR(64) PRIMARY KEY,
    tenant_id    VARCHAR(64)  NOT NULL,
    asset_type   VARCHAR(32)  NOT NULL,
    asset_id     VARCHAR(64)  NOT NULL,
    owner        VARCHAR(128),
    domain       VARCHAR(64),
    sensitivity  VARCHAR(32),
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_asset_metadata UNIQUE (tenant_id, asset_type, asset_id),
    CONSTRAINT ck_asset_metadata_type CHECK (asset_type IN ('document', 'data_source')),
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
