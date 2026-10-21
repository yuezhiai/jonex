-- 标准侧版本治理：模板场景发布快照
BEGIN;

CREATE TABLE IF NOT EXISTS business_domain.template_scenario_versions (
    id             VARCHAR(64) PRIMARY KEY,
    tenant_id      VARCHAR(64) NOT NULL,
    scenario_id    VARCHAR(64) NOT NULL,
    domain_id      VARCHAR(64) NOT NULL,
    version        INTEGER NOT NULL,
    structure_hash VARCHAR(64) NOT NULL,
    snapshot       JSONB NOT NULL DEFAULT '{}'::jsonb,
    published_by   VARCHAR(128),
    published_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_tsv_scenario_version UNIQUE (tenant_id, scenario_id, version)
);
CREATE INDEX IF NOT EXISTS ix_tsv_tenant_scenario
    ON business_domain.template_scenario_versions (tenant_id, scenario_id, version DESC);
CREATE INDEX IF NOT EXISTS ix_tsv_tenant_domain
    ON business_domain.template_scenario_versions (tenant_id, domain_id);

COMMIT;
