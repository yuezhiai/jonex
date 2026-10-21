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
