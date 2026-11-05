-- ============================================================
-- 035 W4.8 指标级调用审批（metric call approval）
-- 设计：docs/superpowers/specs/2026-11-01-w4.8-metric-call-approval-design.md（工作区文档，不入库）
-- 性质：KB 级业务表（带 is_deleted / updated_at）；整体幂等
-- 应用：存量库手工 psql -f，再启动新版服务（开启 METRIC_CALL_APPROVAL_ENABLED 前必须落表）
-- ============================================================

CREATE TABLE IF NOT EXISTS knowledge_base.metric_call_approvals (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    knowledge_base_id VARCHAR(128) NOT NULL,
    metric_name VARCHAR(255) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    requested_by VARCHAR(128),
    decided_by VARCHAR(128),
    reason VARCHAR(500),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT NOT NULL DEFAULT 0,
    CONSTRAINT ck_kb_metric_approval_status CHECK (
        status IN ('pending', 'approved', 'suspended')
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_metric_approval
    ON knowledge_base.metric_call_approvals(tenant_id, knowledge_base_id, metric_name)
    WHERE is_deleted = 0;
CREATE INDEX IF NOT EXISTS idx_kb_metric_approval_status
    ON knowledge_base.metric_call_approvals(tenant_id, knowledge_base_id, status);

COMMENT ON TABLE knowledge_base.metric_call_approvals IS
    'W4.8 指标级调用审批（pending/approved/suspended）。见 metric-call-approval 设计。';
