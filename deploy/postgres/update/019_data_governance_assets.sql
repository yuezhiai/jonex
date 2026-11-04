-- ============================================================
-- 019 数据治理资产 DDL（2026-11-04 由 019 / 020 / 021 / 022 / 023 归并）
--
-- 覆盖：标准资产 / 草稿唯一 / 数据合同 / 质量闭环五表 / 落标绑定
--
-- 应用：空卷随 initdb 自动执行；存量库由 update/019_data_governance_assets.sql
--       （逐字节镜像）经 `make db-migrate` 补齐。依赖 010 建的
--       data_governance schema —— 本文件开头也带 CREATE SCHEMA 兜底。
-- 幂等：全部 CREATE/ALTER ... IF [NOT] EXISTS / ON CONFLICT，重复执行安全。
-- ============================================================

CREATE SCHEMA IF NOT EXISTS data_governance;

-- ------------------------------------------------------------------
-- 来源：migrations/019_standard_assets.sql
-- ------------------------------------------------------------------

-- W2.2 标准资产：术语 / 数据元 / 指标口径卡（每版一行）。
-- 幂等：CREATE IF NOT EXISTS，可重复执行。
BEGIN;

CREATE TABLE IF NOT EXISTS data_governance.standard_assets (
    id           VARCHAR(64) PRIMARY KEY,
    tenant_id    VARCHAR(64) NOT NULL,
    kind         VARCHAR(32) NOT NULL,
    code         VARCHAR(128) NOT NULL,
    version      INTEGER NOT NULL,
    name         VARCHAR(255) NOT NULL,
    payload      JSONB NOT NULL DEFAULT '{}'::jsonb,
    status       VARCHAR(16) NOT NULL DEFAULT 'draft',
    revision     INTEGER NOT NULL DEFAULT 1,
    created_by   VARCHAR(64),
    updated_by   VARCHAR(64),
    published_by VARCHAR(64),
    published_at TIMESTAMPTZ,
    is_deleted   SMALLINT NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_std_asset_kind CHECK (kind IN ('term','data_element','metric_card')),
    CONSTRAINT ck_std_asset_status CHECK (status IN ('draft','published','retired'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_std_asset_version
    ON data_governance.standard_assets (tenant_id, kind, code, version)
    WHERE is_deleted = 0;

CREATE UNIQUE INDEX IF NOT EXISTS uq_std_asset_published
    ON data_governance.standard_assets (tenant_id, kind, code)
    WHERE status = 'published' AND is_deleted = 0;

-- 同一 (tenant_id, kind, code) 至多一条草稿（迁移 020 / update-068）。
-- 存量去重：旧实现允许同 code 多条草稿；只保留最高 version（同 version
-- 取最近更新），其余软删，避免下面的唯一索引创建失败。
WITH ranked_drafts AS (
    SELECT id,
           row_number() OVER (
               PARTITION BY tenant_id, kind, code
               ORDER BY version DESC, updated_at DESC, id
           ) AS rn
    FROM data_governance.standard_assets
    WHERE status = 'draft' AND is_deleted = 0
)
UPDATE data_governance.standard_assets s
SET is_deleted = 1, updated_at = now()
FROM ranked_drafts r
WHERE s.id = r.id AND r.rn > 1;

CREATE UNIQUE INDEX IF NOT EXISTS uq_std_asset_draft
    ON data_governance.standard_assets (tenant_id, kind, code)
    WHERE status = 'draft' AND is_deleted = 0;

CREATE INDEX IF NOT EXISTS ix_std_asset_list
    ON data_governance.standard_assets (tenant_id, kind, status, updated_at DESC);

COMMIT;

-- ------------------------------------------------------------------
-- 来源：migrations/020_standard_asset_draft_unique.sql
-- ------------------------------------------------------------------

-- W2.2 标准资产：同一 (tenant_id, kind, code) 至多一条草稿。
-- 幂等：先软删重复草稿（保留最高 version），再 CREATE UNIQUE INDEX
-- IF NOT EXISTS；可重复执行。
BEGIN;

-- 存量去重：旧实现允许同 code 多条草稿；只保留最高 version（同 version
-- 取最近更新），其余软删，避免下面的唯一索引创建失败。
WITH ranked_drafts AS (
    SELECT id,
           row_number() OVER (
               PARTITION BY tenant_id, kind, code
               ORDER BY version DESC, updated_at DESC, id
           ) AS rn
    FROM data_governance.standard_assets
    WHERE status = 'draft' AND is_deleted = 0
)
UPDATE data_governance.standard_assets s
SET is_deleted = 1, updated_at = now()
FROM ranked_drafts r
WHERE s.id = r.id AND r.rn > 1;

CREATE UNIQUE INDEX IF NOT EXISTS uq_std_asset_draft
    ON data_governance.standard_assets (tenant_id, kind, code)
    WHERE status = 'draft' AND is_deleted = 0;

COMMIT;

-- ------------------------------------------------------------------
-- 来源：migrations/021_data_contracts.sql
-- ------------------------------------------------------------------

-- W2.3 数据合同（每版一行）。幂等：CREATE IF NOT EXISTS / 先软删重复再建唯一索引。
BEGIN;

CREATE TABLE IF NOT EXISTS data_governance.data_contracts (
    id              VARCHAR(64) PRIMARY KEY,
    tenant_id       VARCHAR(64) NOT NULL,
    data_source_id  VARCHAR(64) NOT NULL,
    name            VARCHAR(255) NOT NULL,
    version         INTEGER NOT NULL,
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
    status          VARCHAR(16) NOT NULL DEFAULT 'draft',
    revision        INTEGER NOT NULL DEFAULT 1,
    created_by      VARCHAR(64),
    updated_by      VARCHAR(64),
    published_by    VARCHAR(64),
    published_at    TIMESTAMPTZ,
    is_deleted      SMALLINT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_data_contract_status CHECK (status IN ('draft','published','retired'))
);

-- 存量去重（迁移可重复执行）
WITH ranked AS (
    SELECT id, row_number() OVER (
        PARTITION BY tenant_id, data_source_id, status
        ORDER BY version DESC, updated_at DESC, id
    ) AS rn
    FROM data_governance.data_contracts
    WHERE status IN ('draft','published') AND is_deleted = 0
)
UPDATE data_governance.data_contracts c
SET is_deleted = 1, updated_at = now()
FROM ranked r
WHERE c.id = r.id AND r.rn > 1;

CREATE UNIQUE INDEX IF NOT EXISTS uq_dc_version
    ON data_governance.data_contracts (tenant_id, data_source_id, version)
    WHERE is_deleted = 0;
CREATE UNIQUE INDEX IF NOT EXISTS uq_dc_published
    ON data_governance.data_contracts (tenant_id, data_source_id)
    WHERE status = 'published' AND is_deleted = 0;
CREATE UNIQUE INDEX IF NOT EXISTS uq_dc_draft
    ON data_governance.data_contracts (tenant_id, data_source_id)
    WHERE status = 'draft' AND is_deleted = 0;
CREATE INDEX IF NOT EXISTS ix_dc_list
    ON data_governance.data_contracts (tenant_id, status, updated_at DESC);

COMMIT;

-- ------------------------------------------------------------------
-- 来源：migrations/022_data_quality.sql
-- ------------------------------------------------------------------

-- W2.5 数据质量闭环。幂等：CREATE IF NOT EXISTS + 部分唯一索引。
BEGIN;

CREATE TABLE IF NOT EXISTS data_governance.quality_rules (
    id              VARCHAR(64) PRIMARY KEY,
    tenant_id       VARCHAR(64) NOT NULL,
    name            VARCHAR(255) NOT NULL,
    asset_type      VARCHAR(16) NOT NULL,
    asset_id        VARCHAR(255) NOT NULL,
    data_source_id  VARCHAR(64) NOT NULL,
    kb_id           VARCHAR(128),
    dimension       VARCHAR(16) NOT NULL,
    severity        VARCHAR(16) NOT NULL DEFAULT 'warning',
    check_type      VARCHAR(16) NOT NULL DEFAULT 'library',
    definition      JSONB NOT NULL DEFAULT '{}'::jsonb,
    threshold       JSONB NOT NULL DEFAULT '{}'::jsonb,
    enabled         SMALLINT NOT NULL DEFAULT 0,
    contract_id     VARCHAR(64),
    created_by      VARCHAR(64),
    updated_by      VARCHAR(64),
    revision        INTEGER NOT NULL DEFAULT 1,
    is_deleted      SMALLINT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_qr_asset_type CHECK (asset_type IN ('table','column')),
    CONSTRAINT ck_qr_dimension CHECK (dimension IN
        ('completeness','uniqueness','validity','consistency','timeliness')),
    CONSTRAINT ck_qr_severity CHECK (severity IN ('info','warning','error','critical')),
    CONSTRAINT ck_qr_check_type CHECK (check_type IN ('library','sql','custom'))
);
CREATE INDEX IF NOT EXISTS ix_qr_asset ON data_governance.quality_rules (tenant_id, asset_type, asset_id);
CREATE INDEX IF NOT EXISTS ix_qr_source ON data_governance.quality_rules (tenant_id, data_source_id, enabled);
CREATE INDEX IF NOT EXISTS ix_qr_list ON data_governance.quality_rules (tenant_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS data_governance.quality_runs (
    id              VARCHAR(64) PRIMARY KEY,
    tenant_id       VARCHAR(64) NOT NULL,
    data_source_id  VARCHAR(64) NOT NULL,
    trigger         VARCHAR(16) NOT NULL DEFAULT 'manual',
    status          VARCHAR(16) NOT NULL DEFAULT 'running',
    rule_ids        JSONB NOT NULL DEFAULT '[]'::jsonb,
    rule_snapshot   JSONB NOT NULL DEFAULT '[]'::jsonb,
    executor        VARCHAR(128),
    dialect         VARCHAR(16) NOT NULL DEFAULT 'tsql',
    total           INTEGER NOT NULL DEFAULT 0,
    passed          INTEGER NOT NULL DEFAULT 0,
    failed          INTEGER NOT NULL DEFAULT 0,
    error           INTEGER NOT NULL DEFAULT 0,
    message         TEXT,
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    created_by      VARCHAR(64),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_qrun_status CHECK (status IN ('running','completed','failed'))
);
-- 存量库（本迁移早于 rule_snapshot）补列；幂等可重复执行。
ALTER TABLE data_governance.quality_runs
    ADD COLUMN IF NOT EXISTS rule_snapshot JSONB NOT NULL DEFAULT '[]'::jsonb;
CREATE INDEX IF NOT EXISTS ix_qrun_source ON data_governance.quality_runs (tenant_id, data_source_id, created_at DESC);

CREATE TABLE IF NOT EXISTS data_governance.quality_results (
    id              VARCHAR(64) PRIMARY KEY,
    tenant_id       VARCHAR(64) NOT NULL,
    run_id          VARCHAR(64) NOT NULL,
    rule_id         VARCHAR(64) NOT NULL,
    asset_type      VARCHAR(16) NOT NULL,
    asset_id        VARCHAR(255) NOT NULL,
    status          VARCHAR(16) NOT NULL,
    metric_value    JSONB,
    message         TEXT,
    sample          JSONB,
    checked_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_qres_status CHECK (status IN ('passed','failed','error'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_qres_run_rule ON data_governance.quality_results (tenant_id, run_id, rule_id);
CREATE INDEX IF NOT EXISTS ix_qres_rule ON data_governance.quality_results (tenant_id, rule_id, checked_at DESC);

CREATE TABLE IF NOT EXISTS data_governance.quality_issues (
    id              VARCHAR(64) PRIMARY KEY,
    tenant_id       VARCHAR(64) NOT NULL,
    rule_id         VARCHAR(64) NOT NULL,
    asset_type      VARCHAR(16) NOT NULL,
    asset_id        VARCHAR(255) NOT NULL,
    data_source_id  VARCHAR(64) NOT NULL,
    severity        VARCHAR(16) NOT NULL DEFAULT 'warning',
    status          VARCHAR(16) NOT NULL DEFAULT 'open',
    assignee_id     VARCHAR(64),
    title           VARCHAR(255) NOT NULL,
    detail          TEXT,
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at     TIMESTAMPTZ,
    revision        INTEGER NOT NULL DEFAULT 1,
    created_by      VARCHAR(64),
    updated_by      VARCHAR(64),
    is_deleted      SMALLINT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_qi_status CHECK (status IN
        ('open','assigned','fixing','pending_verify','closed'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_quality_issue_open
    ON data_governance.quality_issues (tenant_id, rule_id, asset_id)
    WHERE status <> 'closed' AND is_deleted = 0;
CREATE INDEX IF NOT EXISTS ix_qi_list ON data_governance.quality_issues (tenant_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS data_governance.quality_issue_events (
    id              VARCHAR(64) PRIMARY KEY,
    tenant_id       VARCHAR(64) NOT NULL,
    issue_id        VARCHAR(64) NOT NULL,
    event_type      VARCHAR(24) NOT NULL,
    from_status     VARCHAR(16),
    to_status       VARCHAR(16),
    actor_id        VARCHAR(64),
    note            TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_qie_type CHECK (event_type IN
        ('created','assigned','status_change','comment','verified','reopened'))
);
CREATE INDEX IF NOT EXISTS ix_qie_issue ON data_governance.quality_issue_events (tenant_id, issue_id, created_at);

COMMIT;

-- ------------------------------------------------------------------
-- 来源：migrations/023_standard_asset_bindings.sql
-- ------------------------------------------------------------------

-- W2.6 标准资产 ↔ 数据资产落标绑定。幂等。
BEGIN;

CREATE TABLE IF NOT EXISTS data_governance.standard_asset_bindings (
    id                VARCHAR(64) PRIMARY KEY,
    tenant_id         VARCHAR(64) NOT NULL,
    standard_kind     VARCHAR(32) NOT NULL,
    standard_code     VARCHAR(128) NOT NULL,
    standard_asset_id VARCHAR(64),
    asset_type        VARCHAR(16) NOT NULL,
    asset_id          VARCHAR(255) NOT NULL,
    source            VARCHAR(16) NOT NULL DEFAULT 'manual',
    confidence        NUMERIC(5,4) NOT NULL DEFAULT 1.0,
    note              VARCHAR(512),
    status            VARCHAR(16) NOT NULL DEFAULT 'active',
    created_by        VARCHAR(64),
    updated_by        VARCHAR(64),
    revision          INTEGER NOT NULL DEFAULT 1,
    is_deleted        SMALLINT NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_sab_kind CHECK (standard_kind IN ('term','data_element','metric_card')),
    CONSTRAINT ck_sab_asset_type CHECK (asset_type IN ('table','column','metric')),
    CONSTRAINT ck_sab_source CHECK (source IN ('manual','suggested')),
    CONSTRAINT ck_sab_status CHECK (status IN ('active','disabled'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_sab_active
    ON data_governance.standard_asset_bindings
    (tenant_id, standard_kind, standard_code, asset_type, asset_id)
    WHERE status = 'active' AND is_deleted = 0;
CREATE INDEX IF NOT EXISTS ix_sab_standard
    ON data_governance.standard_asset_bindings (tenant_id, standard_kind, standard_code, status);
CREATE INDEX IF NOT EXISTS ix_sab_asset
    ON data_governance.standard_asset_bindings (tenant_id, asset_type, asset_id, status);

COMMIT;
