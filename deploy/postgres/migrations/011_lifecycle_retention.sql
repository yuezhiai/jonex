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
