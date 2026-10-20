-- ============================================================
-- 数据运营事件表：新增 6 张 knowledge_base 表（存量库增量 DDL，幂等可重跑）
-- 版本: 032
-- 对应全量 DDL: deploy/postgres/migrations/004_knowledge_base.sql（数据运营事件表分节）
--              原为 008_search_events.sql，已并入 004
-- 六张检索/处理链路的埋点事件表。
-- ============================================================

-- 1. 检索请求接收事件
CREATE TABLE IF NOT EXISTS knowledge_base.search_request_event (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    user_id VARCHAR(128),
    request_id VARCHAR(64) NOT NULL,
    entry_service_id VARCHAR(64),
    domain_space_id VARCHAR(64),
    query TEXT,
    query_hash VARCHAR(64),
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sre_tenant_time ON knowledge_base.search_request_event (tenant_id, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_sre_request ON knowledge_base.search_request_event (request_id);

-- 2. 检索步骤事件
CREATE TABLE IF NOT EXISTS knowledge_base.search_step_event (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    request_id VARCHAR(64) NOT NULL,
    stage VARCHAR(32) NOT NULL,
    title VARCHAR(128),
    status VARCHAR(16) NOT NULL,
    summary TEXT,
    detail JSONB,
    duration_ms INTEGER,
    knowledge_base_ids JSONB NOT NULL DEFAULT '[]',
    service_id VARCHAR(64),
    searched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sse_tenant_time ON knowledge_base.search_step_event (tenant_id, searched_at DESC);
CREATE INDEX IF NOT EXISTS idx_sse_request ON knowledge_base.search_step_event (request_id);

-- 3. 检索命中事件
CREATE TABLE IF NOT EXISTS knowledge_base.search_hit_event (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    request_id VARCHAR(64) NOT NULL,
    -- 32 而非 16：本体命中事件写 'ontology_instance'（17 字符），
    -- 16 会报 value too long 并连带回滚整批埋点。与 004 全量 DDL 对齐，见 049 增量。
    target_type VARCHAR(32) NOT NULL,
    target_id VARCHAR(128) NOT NULL,
    entity_type VARCHAR(64),
    canonical_name VARCHAR(256),
    kb_id VARCHAR(128),
    hit_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_she_tenant_time ON knowledge_base.search_hit_event (tenant_id, hit_at DESC);
CREATE INDEX IF NOT EXISTS idx_she_request ON knowledge_base.search_hit_event (request_id);
CREATE INDEX IF NOT EXISTS idx_she_target ON knowledge_base.search_hit_event (target_type, target_id);

-- 4. 回答采用事件
CREATE TABLE IF NOT EXISTS knowledge_base.search_adoption_event (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    answer_id VARCHAR(64) NOT NULL,
    request_id VARCHAR(64),
    -- 同 search_hit_event.target_type：32 而非 16
    target_type VARCHAR(32) NOT NULL,
    target_id VARCHAR(128) NOT NULL,
    entity_type VARCHAR(64),
    canonical_name VARCHAR(256),
    kb_id VARCHAR(128),
    adopted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sae_tenant_time ON knowledge_base.search_adoption_event (tenant_id, adopted_at DESC);
CREATE INDEX IF NOT EXISTS idx_sae_answer ON knowledge_base.search_adoption_event (answer_id);
CREATE INDEX IF NOT EXISTS idx_sae_target ON knowledge_base.search_adoption_event (target_type, target_id);

-- 5. 资产状态事件
CREATE TABLE IF NOT EXISTS knowledge_base.asset_status_event (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    asset_id VARCHAR(64) NOT NULL,
    kb_id VARCHAR(128),
    status VARCHAR(32) NOT NULL,
    previous_status VARCHAR(32),
    reason TEXT,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ase_tenant_time ON knowledge_base.asset_status_event (tenant_id, changed_at DESC);
CREATE INDEX IF NOT EXISTS idx_ase_asset ON knowledge_base.asset_status_event (asset_id);

-- 6. 处理任务事件
CREATE TABLE IF NOT EXISTS knowledge_base.processing_task_event (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    processing_task_id VARCHAR(128) NOT NULL,
    asset_id VARCHAR(64) NOT NULL,
    kb_id VARCHAR(128),
    task_type VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL,
    error TEXT,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT NOT NULL DEFAULT 0,
    CONSTRAINT uq_pte_task UNIQUE (tenant_id, processing_task_id)
);
CREATE INDEX IF NOT EXISTS idx_pte_tenant_time ON knowledge_base.processing_task_event (tenant_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_pte_asset ON knowledge_base.processing_task_event (asset_id);
