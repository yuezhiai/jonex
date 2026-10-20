-- ============================================================
-- Jonex 平台数据库初始化 - 平台层 (platform schema) + 计量层 (metering schema)
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
    external_id VARCHAR(64),             -- Keycloak sub(UUID) 字符串，映射本地 int user_id（D7 映射桥梁）
    upstream_subject VARCHAR(64),        -- 上游客户系统的稳定主体标识（本地 = Casdoor id）；跨 realm 稳定，认人主键
    password_hash VARCHAR(256),          -- 可空：密码校验上移 Keycloak，影子用户首登无本地密码（D7/BC2）
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
CREATE UNIQUE INDEX IF NOT EXISTS users_tenant_id_external_id_key ON platform.users(tenant_id, external_id) WHERE is_deleted = 0;
CREATE UNIQUE INDEX IF NOT EXISTS users_tenant_id_upstream_subject_key ON platform.users(tenant_id, upstream_subject) WHERE is_deleted = 0 AND upstream_subject IS NOT NULL;

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

-- 租户 ↔ Realm 映射表（Keycloak SSO，PRD §3.2 / T5 拍板独立表）
-- realm = 'jonex-' + tenant_id，全局唯一；供 pre-provision Realm、登录反查 tenant、SLO 回调定位 tenant
CREATE TABLE IF NOT EXISTS platform.tenant_realm_map (
    id BIGSERIAL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    realm VARCHAR(128) NOT NULL UNIQUE,
    issuer_url VARCHAR(512) NOT NULL,
    client_id VARCHAR(255) NOT NULL,
    status SMALLINT DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_tenant_realm_map_tenant ON platform.tenant_realm_map(tenant_id);

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

-- 租户级安全配置表（只存**覆盖值**；某租户没配过的键在这里没有行）
-- 读取链：租户覆盖 → 平台默认(system_configs) → 代码默认(SECURITY_CONFIG_DEFAULTS)
-- 为什么不给上面那张表加 tenant_id：它是平台共享元数据（无 tenant_id、config_key 全局唯一、
-- 后台用 list_all_shared() 读），混进租户运行数据会同时违反项目规范并让既有读法语义变味。
-- 唯一键必须是 (tenant_id, config_key)：只按 config_key 唯一会让第一个租户设过之后别人再也设不了。
CREATE TABLE IF NOT EXISTS platform.tenant_security_configs (
    id BIGSERIAL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    config_key VARCHAR(128) NOT NULL,
    config_value TEXT,
    value_type VARCHAR(32) DEFAULT 'int',
    description VARCHAR(512),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_tenant_security_configs_tenant_key UNIQUE (tenant_id, config_key)
);

CREATE INDEX IF NOT EXISTS idx_tenant_security_configs_tenant
    ON platform.tenant_security_configs(tenant_id);

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

-- 企业身份集成配置（配置聚合根，一租户一份）
-- SSO 子配置：Realm/issuer_url/client_id 归 tenant_realm_map，此处不复制
CREATE TABLE IF NOT EXISTS platform.identity_integration_config (
    id BIGSERIAL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    sso_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    login_policy VARCHAR(32) NOT NULL DEFAULT 'auto_create',
    login_field_mapping JSONB,
    sso_connection_verified_at TIMESTAMP,
    -- 租户级 kc_idp_hint alias 覆盖值；NULL = 用平台默认值
    -- KEYCLOAK_IDP_HINT_DEFAULT_ALIAS（无覆盖值零人工配置）
    idp_hint_alias VARCHAR(64),
    -- 主数据源子配置
    datasource_name VARCHAR(255),
    datasource_url VARCHAR(512),
    auth_type VARCHAR(32),
    token_ciphertext VARCHAR(1024),
    auto_sync_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    sync_interval INTEGER,
    datasource_connection_verified_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 一租户一份配置
CREATE UNIQUE INDEX IF NOT EXISTS uq_identity_integration_config_tenant
    ON platform.identity_integration_config(tenant_id);

-- 同步任务（同步任务聚合根，同步头）
CREATE TABLE IF NOT EXISTS platform.identity_sync_tasks (
    id BIGSERIAL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    sync_type VARCHAR(32) NOT NULL DEFAULT 'incremental',
    trigger VARCHAR(32) NOT NULL DEFAULT 'manual',
    user_added INTEGER NOT NULL DEFAULT 0,
    user_updated INTEGER NOT NULL DEFAULT 0,
    user_disabled INTEGER NOT NULL DEFAULT 0,
    user_enabled INTEGER NOT NULL DEFAULT 0,
    user_deleted INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    cursor VARCHAR(1024),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted SMALLINT DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_identity_sync_tasks_tenant
    ON platform.identity_sync_tasks(tenant_id);

-- 单租户最多一条 running（防并发重复 running，状态机 + 部分唯一索引双兜底）
CREATE UNIQUE INDEX IF NOT EXISTS uq_identity_sync_tasks_running
    ON platform.identity_sync_tasks(tenant_id)
    WHERE status = 'running';

-- 同步失败明细（弱实体，归属 sync_task 聚合，不单独带 tenant_id）
CREATE TABLE IF NOT EXISTS platform.identity_sync_failures (
    id BIGSERIAL PRIMARY KEY,
    sync_task_id BIGINT NOT NULL,
    entity_type VARCHAR(32) NOT NULL DEFAULT 'user',
    external_entity_id VARCHAR(128),
    failure_reason VARCHAR(512),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_identity_sync_failures_task
    ON platform.identity_sync_failures(sync_task_id);

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
-- ✅ 2026-10-12 拍板：K1–K4 只按时间扫，补独立时间索引（存量库见 update/052）
CREATE INDEX IF NOT EXISTS idx_llm_usage_created
    ON metering.llm_usage_log (created_at);

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
-- v1.5 统一 Key：新增 write_grants / client_request_id（纯加列）；本表无 permissions 列、无 mcp_write_keys 表
CREATE TABLE IF NOT EXISTS platform.mcp_keys (
    id              VARCHAR(64) PRIMARY KEY,
    tenant_id       VARCHAR(64) NOT NULL,
    name            VARCHAR(255) NOT NULL DEFAULT '',
    note            VARCHAR(512),
    key_prefix      VARCHAR(32) NOT NULL DEFAULT '',
    key_hash        VARCHAR(64) NOT NULL,
    allowed_kb_ids  JSONB NOT NULL DEFAULT '[]'::jsonb,
    space_id        VARCHAR(64),
    write_grants    JSONB,
    client_request_id  VARCHAR(64),
    created_by      VARCHAR(128),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ,
    disabled_at     TIMESTAMPTZ,
    revoked_at      TIMESTAMPTZ,
    revoked_by      VARCHAR(128),
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

CREATE UNIQUE INDEX IF NOT EXISTS uq_mcp_keys_tenant_reqid
    ON platform.mcp_keys (tenant_id, client_request_id)
    WHERE client_request_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_mcp_keys_write_grants
    ON platform.mcp_keys (tenant_id)
    WHERE write_grants IS NOT NULL;

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
    stopped_at      TIMESTAMPTZ,
    stopped_by      VARCHAR(64),
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
-- ============================================================
-- 平台监控 · 外部调用明细（monitor schema，2026-10-12 新增）
-- 归属：capabilities/platform（monitor 模块），走 Gateway _proxy_platform REST 透传
-- 增量：deploy/postgres/update/050_platform_monitor.sql（存量库，同逻辑）
-- N3（Token 幂等）本期不做：metering.llm_usage_log 保持原样
-- 两表均不建 space_id 列（采 01-SDD §3.4 口径）；保留期 / 分区本期不含
-- ============================================================

CREATE SCHEMA IF NOT EXISTS monitor;

-- N1：逻辑调用表（一行 = 一次逻辑调用；request_id 在本表内唯一）
CREATE TABLE IF NOT EXISTS monitor.call_log (
    id                  BIGSERIAL     PRIMARY KEY,
    request_id          VARCHAR(64)   NOT NULL,
    trace_id            VARCHAR(64)   NULL,
    tenant_id           VARCHAR(64)   NOT NULL,
    direction           VARCHAR(16)   NOT NULL,
    source              VARCHAR(128)  NULL,
    target              VARCHAR(128)  NULL,
    protocol            VARCHAR(32)   NULL,
    trigger_type        VARCHAR(16)   NOT NULL DEFAULT 'user',
    logical_started_at  TIMESTAMPTZ   NULL,
    final_ended_at      TIMESTAMPTZ   NULL,
    final_status        VARCHAR(16)   NOT NULL,
    error_code          VARCHAR(64)   NULL,
    error_message       TEXT          NULL,
    attempt_count       INTEGER       NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_monitor_call_log_request_id UNIQUE (request_id),
    -- 枚举取值集未定（direction / final_status）
    CONSTRAINT ck_monitor_call_log_direction  CHECK (direction IN ('outbound','inbound')),
    CONSTRAINT ck_monitor_call_log_final_status
        CHECK (final_status IN ('success','failed','cancelled','running','unknown')),
    -- 触发方取值集已定（user 用户直接发起 / system 系统后台批量）
    CONSTRAINT ck_monitor_call_log_trigger CHECK (trigger_type IN ('user','system'))
);

CREATE INDEX IF NOT EXISTS idx_monitor_call_log_tenant_started
    ON monitor.call_log (tenant_id, logical_started_at DESC);
CREATE INDEX IF NOT EXISTS idx_monitor_call_log_trace
    ON monitor.call_log (trace_id);
CREATE INDEX IF NOT EXISTS idx_monitor_call_log_dir_status_started
    ON monitor.call_log (direction, final_status, logical_started_at DESC);
CREATE INDEX IF NOT EXISTS idx_monitor_call_log_target_started
    ON monitor.call_log (target, logical_started_at DESC);
CREATE INDEX IF NOT EXISTS idx_monitor_call_log_created
    ON monitor.call_log (created_at DESC);
-- I9：按触发方（用户/系统后台）筛选 + 时间倒序（调用列表的筛选维度）
CREATE INDEX IF NOT EXISTS idx_monitor_call_log_trigger_started
    ON monitor.call_log (trigger_type, logical_started_at DESC);
-- I10：只按时间的范围扫（C5 不传 direction / C6 / 降级判定）；
--      既有索引时间列均非前导列，无该索引时只能顺扫 90 天明细。
--      增量：deploy/postgres/update/073_platform_monitor_time_index.sql
CREATE INDEX IF NOT EXISTS idx_monitor_call_log_logical_started
    ON monitor.call_log (logical_started_at, id);

-- N2：物理 Attempt 表（request_id 不唯一 —— 降级模式取数表）
CREATE TABLE IF NOT EXISTS monitor.call_attempt (
    id                  BIGSERIAL     PRIMARY KEY,
    attempt_id          VARCHAR(64)   NOT NULL,
    request_id          VARCHAR(64)   NOT NULL,
    attempt_no          INTEGER       NOT NULL,
    tenant_id           VARCHAR(64)   NOT NULL,
    attempt_started_at  TIMESTAMPTZ   NULL,
    attempt_ended_at    TIMESTAMPTZ   NULL,
    status              VARCHAR(16)   NOT NULL,
    error_code          VARCHAR(64)   NULL,
    error_message       TEXT          NULL,
    is_final            BOOLEAN       NOT NULL DEFAULT FALSE,
    wait_before_ms      INTEGER       NULL,
    created_at          TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_monitor_call_attempt_id UNIQUE (attempt_id),
    CONSTRAINT uq_monitor_call_attempt_req_no UNIQUE (request_id, attempt_no)
);

CREATE INDEX IF NOT EXISTS idx_monitor_call_attempt_request
    ON monitor.call_attempt (request_id);
CREATE INDEX IF NOT EXISTS idx_monitor_call_attempt_started
    ON monitor.call_attempt (attempt_started_at DESC);
-- I11：保留期清理按 created_at 分批删除（ORDER BY created_at, id LIMIT :batch）；
--      attempt_started_at 不是删除锚点，无该索引时 DELETE 会全表扫。
--      增量：deploy/postgres/update/072_platform_monitor_retention.sql
CREATE INDEX IF NOT EXISTS idx_monitor_call_attempt_created
    ON monitor.call_attempt (created_at, id);
