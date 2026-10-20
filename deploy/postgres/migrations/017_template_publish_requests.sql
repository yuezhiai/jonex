-- 标准侧审批流：模板场景发布申请（强制审批）
CREATE TABLE IF NOT EXISTS business_domain.template_publish_requests (
    id                VARCHAR(64) PRIMARY KEY,
    tenant_id         VARCHAR(64) NOT NULL,
    domain_id         VARCHAR(64) NOT NULL,
    scenario_id       VARCHAR(64) NOT NULL,
    status            VARCHAR(16) NOT NULL DEFAULT 'pending',
    structure_hash    VARCHAR(64) NOT NULL,
    snapshot          JSONB NOT NULL DEFAULT '{}'::jsonb,
    submitted_by      VARCHAR(128),
    submitted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_by        VARCHAR(128),
    decided_at        TIMESTAMPTZ,
    comment           TEXT,
    published_version INTEGER,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_tpr_status
        CHECK (status IN ('pending', 'approved', 'rejected', 'cancelled'))
);
-- 同一场景同时只允许一个待审申请（避免审批队列出现重复项）
CREATE UNIQUE INDEX IF NOT EXISTS uq_tpr_pending_scenario
    ON business_domain.template_publish_requests (tenant_id, scenario_id)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS ix_tpr_tenant_scenario
    ON business_domain.template_publish_requests
        (tenant_id, scenario_id, submitted_at DESC);
