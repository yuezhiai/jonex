-- ============================================================
-- 悦溪平台数据库初始化 - 平台层 (platform schema) + 计量层 (metering schema)
-- 版本: 002
-- 包含：租户、API Key、用户、登录票据
--       RBAC (角色/权限/角色-权限/用户-角色)
--       菜单、应用注册、应用路由、系统配置、审计日志、任务调度
--       LLM 网关计量明细表 metering.llm_usage_log
-- ============================================================

-- 租户表
CREATE TABLE IF NOT EXISTS platform.tenants (
    id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    status SMALLINT DEFAULT 1,
    plan_type VARCHAR(32) DEFAULT 'free',
    expire_time TIMESTAMP,
    quota_config JSONB,
    extra_config JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT DEFAULT 0
);

-- API Key 表
CREATE TABLE IF NOT EXISTS platform.api_keys (
    id BIGSERIAL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    api_key VARCHAR(128) NOT NULL UNIQUE,
    name VARCHAR(255),
    description TEXT,
    status SMALLINT DEFAULT 1,
    rate_limit INTEGER DEFAULT 100,
    expire_time TIMESTAMP,
    last_used_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_api_keys_tenant ON platform.api_keys(tenant_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_key ON platform.api_keys(api_key);

-- 用户表
CREATE TABLE IF NOT EXISTS platform.users (
    id BIGSERIAL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    username VARCHAR(128) NOT NULL,
    password_hash VARCHAR(256) NOT NULL,
    display_name VARCHAR(255),
    email VARCHAR(255),
    role VARCHAR(32) NOT NULL DEFAULT 'user',
    status SMALLINT DEFAULT 1,
    last_login_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS users_tenant_id_username_key ON platform.users(tenant_id, username) WHERE is_deleted = 0;

CREATE INDEX IF NOT EXISTS idx_users_tenant ON platform.users(tenant_id);
CREATE INDEX IF NOT EXISTS idx_users_username ON platform.users(tenant_id, username);

-- 一次性登录票据表（跨域登录 ticket/code 交换）
CREATE TABLE IF NOT EXISTS platform.login_tickets (
    id BIGSERIAL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    ticket_hash VARCHAR(128) NOT NULL UNIQUE,
    user_id BIGINT NOT NULL,
    app_id VARCHAR(64) NOT NULL,
    redirect_uri VARCHAR(1024) NOT NULL,
    state VARCHAR(256),
    expires_at TIMESTAMP NOT NULL,
    used_at TIMESTAMP,
    client_ip VARCHAR(64),
    user_agent TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_login_tickets_tenant ON platform.login_tickets(tenant_id);
CREATE INDEX IF NOT EXISTS idx_login_tickets_hash ON platform.login_tickets(ticket_hash);
CREATE INDEX IF NOT EXISTS idx_login_tickets_expires ON platform.login_tickets(expires_at);
CREATE INDEX IF NOT EXISTS idx_login_tickets_app_redirect ON platform.login_tickets(app_id, redirect_uri);

-- 调用计量表已下线：LLM/embedding 出口计量统一由 llm-gateway 写入 metering.llm_usage_log
-- （表定义见本文件末尾「计量层」段；决策见 docs/llm-gateway-token-metering-execution-plan.md G2c-A）

-- RBAC 角色表
CREATE TABLE IF NOT EXISTS platform.roles (
    id BIGSERIAL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    name VARCHAR(128) NOT NULL,
    description VARCHAR(512),
    is_system SMALLINT DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_roles_tenant ON platform.roles(tenant_id);

-- 权限表
CREATE TABLE IF NOT EXISTS platform.permissions (
    id BIGSERIAL PRIMARY KEY,
    code VARCHAR(128) NOT NULL UNIQUE,
    name VARCHAR(255) NOT NULL,
    resource VARCHAR(128) NOT NULL,
    action VARCHAR(64) NOT NULL,
    description VARCHAR(512),
    scope VARCHAR(16) NOT NULL DEFAULT 'tenant',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 角色-权限关联表
CREATE TABLE IF NOT EXISTS platform.role_permissions (
    id BIGSERIAL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    role_id BIGINT NOT NULL,
    permission_id BIGINT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_rp_tenant ON platform.role_permissions(tenant_id);
CREATE INDEX IF NOT EXISTS idx_rp_role ON platform.role_permissions(role_id);
CREATE INDEX IF NOT EXISTS idx_rp_perm ON platform.role_permissions(permission_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_role_permissions ON platform.role_permissions(tenant_id, role_id, permission_id);

-- 用户-角色关联表
CREATE TABLE IF NOT EXISTS platform.user_roles (
    id BIGSERIAL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    user_id BIGINT NOT NULL,
    role_id BIGINT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ur_tenant ON platform.user_roles(tenant_id);
CREATE INDEX IF NOT EXISTS idx_ur_user ON platform.user_roles(user_id);
CREATE INDEX IF NOT EXISTS idx_ur_role ON platform.user_roles(role_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_user_roles ON platform.user_roles(tenant_id, user_id, role_id);

-- 菜单表
CREATE TABLE IF NOT EXISTS platform.menus (
    id BIGSERIAL PRIMARY KEY,
    parent_id BIGINT DEFAULT 0,
    name VARCHAR(128) NOT NULL,
    path VARCHAR(256),
    icon VARCHAR(128),
    app_id BIGINT,
    sort_order SMALLINT DEFAULT 0,
    visible SMALLINT DEFAULT 1,
    status SMALLINT DEFAULT 1,
    permission_code VARCHAR(128),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT DEFAULT 0
);

-- 应用注册表
CREATE TABLE IF NOT EXISTS platform.applications (
    id BIGSERIAL PRIMARY KEY,
    app_code VARCHAR(64) NOT NULL UNIQUE,
    name VARCHAR(128) NOT NULL,
    entry_path VARCHAR(256),
    icon VARCHAR(128),
    description VARCHAR(512),
    status SMALLINT DEFAULT 1,
    sort_order SMALLINT DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT DEFAULT 0
);

-- 应用路由表
CREATE TABLE IF NOT EXISTS platform.application_routes (
    id BIGSERIAL PRIMARY KEY,
    app_id BIGINT NOT NULL,
    route_path VARCHAR(256) NOT NULL,
    title VARCHAR(128),
    permission_code VARCHAR(128),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ar_app ON platform.application_routes(app_id);

-- 系统配置表
CREATE TABLE IF NOT EXISTS platform.system_configs (
    id BIGSERIAL PRIMARY KEY,
    config_group VARCHAR(64) NOT NULL,
    config_key VARCHAR(128) NOT NULL UNIQUE,
    config_value TEXT,
    value_type VARCHAR(32) DEFAULT 'string',
    description VARCHAR(512),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 审计日志表
CREATE TABLE IF NOT EXISTS platform.audit_logs (
    id BIGSERIAL PRIMARY KEY,
    tenant_id VARCHAR(64),                 -- 可空：支持无租户的系统级事件
    user_id BIGINT,
    username VARCHAR(128),
    ip VARCHAR(64),
    action VARCHAR(64) NOT NULL,
    resource VARCHAR(128),
    resource_id VARCHAR(64),
    status_code SMALLINT,
    duration_ms BIGINT,
    request_params JSONB,
    response_body JSONB,
    error_stack TEXT,
    trace_id VARCHAR(128),
    -- 区分维度字段
    log_type VARCHAR(32),                  -- OPERATION / TASK / SECURITY 等
    service_name VARCHAR(64),              -- 来源服务
    outcome VARCHAR(16),                   -- SUCCESS / FAILED
    log_level VARCHAR(16),                 -- INFO / WARN / ERROR
    error_message TEXT,
    method VARCHAR(8),                     -- HTTP 方法
    path VARCHAR(512),                     -- 请求路径
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_audit_tenant ON platform.audit_logs(tenant_id);
CREATE INDEX IF NOT EXISTS idx_audit_user ON platform.audit_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON platform.audit_logs(action);
CREATE INDEX IF NOT EXISTS idx_audit_time ON platform.audit_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_type ON platform.audit_logs(log_type);
CREATE INDEX IF NOT EXISTS idx_audit_service ON platform.audit_logs(service_name);
CREATE INDEX IF NOT EXISTS idx_audit_outcome ON platform.audit_logs(outcome);
-- 控制台最常见查询：某租户下某类日志按时间倒序
CREATE INDEX IF NOT EXISTS idx_audit_tenant_type_time
    ON platform.audit_logs(tenant_id, log_type, created_at DESC);

-- 任务调度表
CREATE TABLE IF NOT EXISTS platform.task_schedules (
    id BIGSERIAL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    name VARCHAR(255) NOT NULL,
    task_type VARCHAR(64) NOT NULL,
    cron_expr VARCHAR(128),
    status SMALLINT DEFAULT 1,
    last_run_at TIMESTAMP,
    next_run_at TIMESTAMP,
    config_json JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_task_tenant ON platform.task_schedules(tenant_id);
CREATE INDEX IF NOT EXISTS idx_task_type ON platform.task_schedules(task_type);

-- ============================================================
-- 计量层 (metering schema)
-- LLM 网关计量明细表：记录所有经 llm-gateway 的 LLM/Embedding 调用 token 用量
-- 按 tenant/scene/model/kb/doc 粒度统计
-- ============================================================

CREATE TABLE IF NOT EXISTS metering.llm_usage_log (
    id               BIGSERIAL PRIMARY KEY,
    request_id       VARCHAR(64) UNIQUE,          -- 计量幂等键：每次逻辑 LLM 调用唯一、重试稳定
    trace_id         VARCHAR(64),                 -- 链路追踪 ID：一次用户业务请求一个，用于多次调用归组
    tenant_id        VARCHAR(64) NOT NULL,
    user_id          VARCHAR(64),
    scene            VARCHAR(64) NOT NULL,        -- ontology_extract / ontology_qa / lightrag_extract / lightrag_embed / ...
    model            VARCHAR(128) NOT NULL,
    kb_id            VARCHAR(64),
    doc_id           VARCHAR(64),
    prompt_tokens    INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens     INTEGER DEFAULT 0,
    latency_ms       INTEGER,                     -- 上游响应延迟
    is_stream        BOOLEAN DEFAULT false,
    is_estimated     BOOLEAN DEFAULT false,       -- usage 是否为估算
    call_count       INTEGER NOT NULL DEFAULT 1,  -- 明细行=1；embedding 聚合行累加为 N（Phase 2A，见 docs/llm-usage-log-optimization-plan.md §4.3）
    created_at       TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_llm_usage_tenant_time
    ON metering.llm_usage_log (tenant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_llm_usage_scene
    ON metering.llm_usage_log (scene);
CREATE INDEX IF NOT EXISTS idx_llm_usage_trace
    ON metering.llm_usage_log (trace_id);

-- 日汇总表（Phase 3）：明细清理后仍可查长期趋势/对账。由手动 `make metering-rollup`
-- 用 INSERT...SELECT...ON CONFLICT DO UPDATE（整天重聚合、replace 语义、幂等可重跑）刷新。
-- 见 docs/llm-usage-log-optimization-plan.md §4.4。
CREATE TABLE IF NOT EXISTS metering.llm_usage_daily (
    day_local          DATE         NOT NULL,
    tenant_id          VARCHAR(64)  NOT NULL,
    scene              VARCHAR(64)  NOT NULL,
    model              VARCHAR(128) NOT NULL,
    call_count         BIGINT       NOT NULL DEFAULT 0,
    prompt_tokens      BIGINT       NOT NULL DEFAULT 0,
    completion_tokens  BIGINT       NOT NULL DEFAULT 0,
    total_tokens       BIGINT       NOT NULL DEFAULT 0,
    avg_latency_ms     INTEGER,
    PRIMARY KEY (day_local, tenant_id, scene, model)
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_daily_tenant
    ON metering.llm_usage_daily (tenant_id, day_local);

-- MCP Key 鉴权表（v1.2 MCP Server）
-- v1.3 Phase 07: 新增 expires_at（有效期）
-- v1.4 Phase 16: 新增 note（用途描述）、disabled_at（停用态）；下线组织维度（D8）
CREATE TABLE IF NOT EXISTS platform.mcp_keys (
    id              VARCHAR(64) PRIMARY KEY,
    tenant_id       VARCHAR(64) NOT NULL,
    name            VARCHAR(255) NOT NULL DEFAULT '',
    note            VARCHAR(512),
    key_prefix      VARCHAR(32) NOT NULL DEFAULT '',
    key_hash        VARCHAR(64) NOT NULL,
    permissions     VARCHAR(512) NOT NULL DEFAULT 'view',
    allowed_kb_ids  JSONB NOT NULL DEFAULT '[]'::jsonb,
    space_id        VARCHAR(64),
    created_by      VARCHAR(128),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ,
    disabled_at     TIMESTAMPTZ,
    revoked_at      TIMESTAMPTZ,
    last_used_at    TIMESTAMPTZ,
    last_used_ip    VARCHAR(64),
    is_deleted      INT NOT NULL DEFAULT 0,
    UNIQUE(key_hash)
);

CREATE INDEX idx_mcp_keys_tenant_active
    ON platform.mcp_keys(tenant_id)
    WHERE revoked_at IS NULL;

CREATE INDEX idx_mcp_keys_space
    ON platform.mcp_keys(space_id);

-- MCP Key ↔ 领域服务映射中间表（v1.3 Phase 2 B2 前置）
-- v1.3 Phase 07: 新增 permission_level（权限级别）
CREATE TABLE IF NOT EXISTS platform.mcp_key_service_mappings (
    mcp_key_id        VARCHAR(64) NOT NULL,
    service_id        VARCHAR(64) NOT NULL,
    permission_level  VARCHAR(16) NOT NULL DEFAULT 'call',
    PRIMARY KEY(mcp_key_id, service_id)
);

CREATE INDEX IF NOT EXISTS idx_mk_sv_mapping_key
    ON platform.mcp_key_service_mappings(mcp_key_id);

CREATE INDEX IF NOT EXISTS idx_mk_sv_mapping_service
    ON platform.mcp_key_service_mappings(service_id);

-- MCP 服务发布状态表
-- 独立存储 MCP 发布状态，避免跨 capability 修改 knowledge_base schema
CREATE TABLE IF NOT EXISTS platform.mcp_service_publish (
    id              VARCHAR(64) PRIMARY KEY,
    tenant_id       VARCHAR(64) NOT NULL,
    service_id      VARCHAR(64) NOT NULL,
    is_published    INT NOT NULL DEFAULT 0,
    published_at    TIMESTAMPTZ,
    published_by    VARCHAR(64),
    tool            VARCHAR(128),
    tool_description TEXT,
    service_type    VARCHAR(32) NOT NULL DEFAULT 'domain',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ,
    UNIQUE(tenant_id, service_id)
);

CREATE INDEX IF NOT EXISTS idx_mcp_svc_pub_tenant
    ON platform.mcp_service_publish(tenant_id);

CREATE INDEX IF NOT EXISTS idx_mcp_svc_pub_service
    ON platform.mcp_service_publish(service_id);

CREATE INDEX IF NOT EXISTS idx_mcp_svc_pub_tenant_published
    ON platform.mcp_service_publish(tenant_id)
    WHERE is_published = 1;

CREATE INDEX IF NOT EXISTS idx_mcp_svc_pub_tool
    ON platform.mcp_service_publish(tenant_id, tool)
    WHERE tool IS NOT NULL;

-- 领域服务 API Key 表（v1.4 Phase 15 DS-04）
-- 明文 Key 仅在创建响应一次性返回，绝不落库（无明文列，仅存 key_hash）
-- service_id 对应 knowledge_base.services.id；expires_at = NULL 表示永久有效
CREATE TABLE IF NOT EXISTS platform.mcp_service_api_keys (
    id          VARCHAR(64) PRIMARY KEY,
    tenant_id   VARCHAR(64) NOT NULL,
    service_id  VARCHAR(64) NOT NULL,
    name        VARCHAR(255) NOT NULL DEFAULT '',
    key_prefix  VARCHAR(32)  NOT NULL DEFAULT '',
    key_hash    VARCHAR(64)  NOT NULL,
    expires_at  TIMESTAMPTZ,
    revoked_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_deleted  INT NOT NULL DEFAULT 0,
    UNIQUE(key_hash)
);

CREATE INDEX IF NOT EXISTS idx_mcp_svc_api_keys_tenant
    ON platform.mcp_service_api_keys(tenant_id);

CREATE INDEX IF NOT EXISTS idx_mcp_svc_api_keys_service
    ON platform.mcp_service_api_keys(tenant_id, service_id);

-- 知识写入 Key 表（v1.4 Phase 17 WRITE-01）
-- 明文 Key 仅在创建响应一次性返回，绝不落库（无明文列，仅存 key_hash）
-- grants JSONB 存写入范围 [{kb, mode(all|specified), directories[]}]；
-- space_id 由 grants[0].kb 反推、kb_id = grants[0].kb 冗余索引（17-02 计算）
CREATE TABLE IF NOT EXISTS platform.mcp_write_keys (
    id              VARCHAR(64) PRIMARY KEY,
    tenant_id       VARCHAR(64) NOT NULL,
    name            VARCHAR(255) NOT NULL DEFAULT '',
    key_prefix      VARCHAR(32) NOT NULL DEFAULT '',
    key_hash        VARCHAR(64) NOT NULL,
    grants          JSONB NOT NULL DEFAULT '[]'::jsonb,
    space_id        VARCHAR(64),
    kb_id           VARCHAR(64),
    disabled_at     TIMESTAMPTZ,
    revoked_at      TIMESTAMPTZ,
    revoked_by      VARCHAR(128),
    expires_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by      VARCHAR(128),
    updated_at      TIMESTAMPTZ,
    is_deleted      INT NOT NULL DEFAULT 0,
    UNIQUE(key_hash)
);

CREATE INDEX IF NOT EXISTS idx_mcp_write_keys_tenant
    ON platform.mcp_write_keys(tenant_id);

CREATE INDEX IF NOT EXISTS idx_mcp_write_keys_kb
    ON platform.mcp_write_keys(tenant_id, kb_id);