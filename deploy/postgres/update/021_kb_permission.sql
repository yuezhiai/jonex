-- [jonex] KB 授权成员表（存量库增量；设计 2026-08-20-kb-permission-design §1）
-- 幂等：全部 IF [NOT] EXISTS 可重复执行。与 004 全新库 DDL 保持一致。
-- 依赖：knowledge_info 表已存在（004/008_019 已建）。

CREATE TABLE IF NOT EXISTS knowledge_base.kb_permissions (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    kb_id VARCHAR(64) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    role VARCHAR(32) NOT NULL DEFAULT 'viewer',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_kb_perms_tenant ON knowledge_base.kb_permissions(tenant_id);
CREATE INDEX IF NOT EXISTS idx_kb_perms_is_deleted ON knowledge_base.kb_permissions(is_deleted);
CREATE INDEX IF NOT EXISTS idx_kb_perms_kb ON knowledge_base.kb_permissions(kb_id);
CREATE INDEX IF NOT EXISTS idx_kb_perms_user ON knowledge_base.kb_permissions(user_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_kb_perms_unique_member ON knowledge_base.kb_permissions(tenant_id, kb_id, user_id) WHERE is_deleted = 0;
