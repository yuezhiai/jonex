-- ============================================================================
-- 平台库：登录、RBAC、菜单、应用注册、审计、MCP 密钥
-- ============================================================================

CREATE TABLE IF NOT EXISTS platform.api_keys (
    id bigint NOT NULL,
    tenant_id character varying(64) NOT NULL,
    api_key character varying(128) NOT NULL,
    name character varying(255),
    description text,
    status smallint DEFAULT 1,
    rate_limit integer DEFAULT 100,
    expire_time timestamp without time zone,
    last_used_at timestamp without time zone,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    is_deleted smallint DEFAULT 0
);

CREATE SEQUENCE IF NOT EXISTS platform.api_keys_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE platform.api_keys_id_seq OWNED BY platform.api_keys.id;

CREATE TABLE IF NOT EXISTS platform.application_routes (
    id bigint NOT NULL,
    app_id bigint NOT NULL,
    route_path character varying(256) NOT NULL,
    title character varying(128),
    permission_code character varying(128),
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE IF NOT EXISTS platform.application_routes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE platform.application_routes_id_seq OWNED BY platform.application_routes.id;

CREATE TABLE IF NOT EXISTS platform.applications (
    id bigint NOT NULL,
    app_code character varying(64) NOT NULL,
    name character varying(128) NOT NULL,
    entry_path character varying(256),
    icon character varying(128),
    description character varying(512),
    status smallint DEFAULT 1,
    sort_order smallint DEFAULT 0,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    is_deleted smallint DEFAULT 0
);

CREATE SEQUENCE IF NOT EXISTS platform.applications_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE platform.applications_id_seq OWNED BY platform.applications.id;

CREATE TABLE IF NOT EXISTS platform.audit_logs (
    id bigint NOT NULL,
    tenant_id character varying(64),
    user_id bigint,
    username character varying(128),
    ip character varying(64),
    action character varying(64) NOT NULL,
    resource character varying(128),
    resource_id character varying(255),
    status_code smallint,
    duration_ms bigint,
    request_params jsonb,
    response_body jsonb,
    error_stack text,
    trace_id character varying(128),
    log_type character varying(32),
    service_name character varying(64),
    outcome character varying(16),
    log_level character varying(16),
    error_message text,
    method character varying(8),
    path character varying(512),
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE IF NOT EXISTS platform.audit_logs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE platform.audit_logs_id_seq OWNED BY platform.audit_logs.id;

CREATE TABLE IF NOT EXISTS platform.identity_integration_config (
    id bigint NOT NULL,
    tenant_id character varying(64) NOT NULL,
    sso_enabled boolean DEFAULT false NOT NULL,
    login_policy character varying(32) DEFAULT 'auto_create'::character varying NOT NULL,
    login_field_mapping jsonb,
    sso_connection_verified_at timestamp without time zone,
    datasource_name character varying(255),
    datasource_url character varying(512),
    auth_type character varying(32),
    token_ciphertext character varying(1024),
    auto_sync_enabled boolean DEFAULT false NOT NULL,
    sync_interval integer,
    datasource_connection_verified_at timestamp without time zone,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    idp_hint_alias character varying(64),
    last_sync_at timestamp without time zone,
    next_sync_at timestamp without time zone
);

CREATE SEQUENCE IF NOT EXISTS platform.identity_integration_config_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE platform.identity_integration_config_id_seq OWNED BY platform.identity_integration_config.id;

CREATE TABLE IF NOT EXISTS platform.identity_sync_failures (
    id bigint NOT NULL,
    sync_task_id bigint NOT NULL,
    entity_type character varying(32) DEFAULT 'user'::character varying NOT NULL,
    external_entity_id character varying(128),
    failure_reason character varying(512),
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE IF NOT EXISTS platform.identity_sync_failures_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE platform.identity_sync_failures_id_seq OWNED BY platform.identity_sync_failures.id;

CREATE TABLE IF NOT EXISTS platform.identity_sync_tasks (
    id bigint NOT NULL,
    tenant_id character varying(64) NOT NULL,
    status character varying(32) DEFAULT 'pending'::character varying NOT NULL,
    sync_type character varying(32) DEFAULT 'incremental'::character varying NOT NULL,
    trigger character varying(32) DEFAULT 'manual'::character varying NOT NULL,
    user_added integer DEFAULT 0 NOT NULL,
    user_updated integer DEFAULT 0 NOT NULL,
    user_disabled integer DEFAULT 0 NOT NULL,
    failed_count integer DEFAULT 0 NOT NULL,
    started_at timestamp without time zone,
    finished_at timestamp without time zone,
    cursor character varying(1024),
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    is_deleted smallint DEFAULT 0,
    user_enabled integer DEFAULT 0 NOT NULL,
    user_deleted integer DEFAULT 0 NOT NULL
);

CREATE SEQUENCE IF NOT EXISTS platform.identity_sync_tasks_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE platform.identity_sync_tasks_id_seq OWNED BY platform.identity_sync_tasks.id;

CREATE TABLE IF NOT EXISTS platform.login_tickets (
    id bigint NOT NULL,
    tenant_id character varying(64) NOT NULL,
    ticket_hash character varying(128) NOT NULL,
    user_id bigint NOT NULL,
    app_id character varying(64) NOT NULL,
    redirect_uri character varying(1024) NOT NULL,
    state character varying(256),
    expires_at timestamp without time zone NOT NULL,
    used_at timestamp without time zone,
    client_ip character varying(64),
    user_agent text,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE IF NOT EXISTS platform.login_tickets_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE platform.login_tickets_id_seq OWNED BY platform.login_tickets.id;

CREATE TABLE IF NOT EXISTS platform.mcp_key_service_mappings (
    mcp_key_id character varying(64) NOT NULL,
    service_id character varying(64) NOT NULL,
    permission_level character varying(16) DEFAULT 'call'::character varying NOT NULL
);

CREATE TABLE IF NOT EXISTS platform.mcp_keys (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    name character varying(255) DEFAULT ''::character varying NOT NULL,
    note character varying(512),
    key_prefix character varying(32) DEFAULT ''::character varying NOT NULL,
    key_hash character varying(64) NOT NULL,
    allowed_kb_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    space_id character varying(64),
    created_by character varying(128),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone,
    disabled_at timestamp with time zone,
    revoked_at timestamp with time zone,
    last_used_at timestamp with time zone,
    last_used_ip character varying(64),
    is_deleted integer DEFAULT 0 NOT NULL,
    revoked_by character varying(128),
    write_grants jsonb,
    client_request_id character varying(64)
);

CREATE TABLE IF NOT EXISTS platform.mcp_service_api_keys (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    service_id character varying(64) NOT NULL,
    name character varying(255) DEFAULT ''::character varying NOT NULL,
    key_prefix character varying(32) DEFAULT ''::character varying NOT NULL,
    key_hash character varying(64) NOT NULL,
    expires_at timestamp with time zone,
    revoked_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    is_deleted integer DEFAULT 0 NOT NULL
);

CREATE TABLE IF NOT EXISTS platform.mcp_service_publish (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    service_id character varying(64) NOT NULL,
    is_published integer DEFAULT 0 NOT NULL,
    published_at timestamp with time zone,
    published_by character varying(64),
    tool character varying(128),
    tool_description text,
    service_type character varying(32) DEFAULT 'domain'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone,
    stopped_at timestamp with time zone,
    stopped_by character varying(64),
    last_call_at timestamp with time zone
);

CREATE TABLE IF NOT EXISTS platform.menus (
    id bigint NOT NULL,
    parent_id bigint DEFAULT 0,
    name character varying(128) NOT NULL,
    path character varying(256),
    icon character varying(128),
    app_id bigint,
    sort_order smallint DEFAULT 0,
    visible smallint DEFAULT 1,
    status smallint DEFAULT 1,
    permission_code character varying(128),
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    is_deleted smallint DEFAULT 0
);

CREATE SEQUENCE IF NOT EXISTS platform.menus_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE platform.menus_id_seq OWNED BY platform.menus.id;

CREATE TABLE IF NOT EXISTS platform.permissions (
    id bigint NOT NULL,
    code character varying(128) NOT NULL,
    name character varying(255) NOT NULL,
    resource character varying(128) NOT NULL,
    action character varying(64) NOT NULL,
    description character varying(512),
    scope character varying(16) DEFAULT 'tenant'::character varying NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE IF NOT EXISTS platform.permissions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE platform.permissions_id_seq OWNED BY platform.permissions.id;

CREATE TABLE IF NOT EXISTS platform.role_permissions (
    id bigint NOT NULL,
    tenant_id character varying(64) NOT NULL,
    role_id bigint NOT NULL,
    permission_id bigint NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE IF NOT EXISTS platform.role_permissions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE platform.role_permissions_id_seq OWNED BY platform.role_permissions.id;

CREATE TABLE IF NOT EXISTS platform.roles (
    id bigint NOT NULL,
    tenant_id character varying(64) NOT NULL,
    name character varying(128) NOT NULL,
    description character varying(512),
    is_system smallint DEFAULT 0,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    is_deleted smallint DEFAULT 0
);

CREATE SEQUENCE IF NOT EXISTS platform.roles_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE platform.roles_id_seq OWNED BY platform.roles.id;

CREATE TABLE IF NOT EXISTS platform.system_configs (
    id bigint NOT NULL,
    config_group character varying(64) NOT NULL,
    config_key character varying(128) NOT NULL,
    config_value text,
    value_type character varying(32) DEFAULT 'string'::character varying,
    description character varying(512),
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE IF NOT EXISTS platform.system_configs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE platform.system_configs_id_seq OWNED BY platform.system_configs.id;

CREATE TABLE IF NOT EXISTS platform.task_schedules (
    id bigint NOT NULL,
    tenant_id character varying(64) NOT NULL,
    name character varying(255) NOT NULL,
    task_type character varying(64) NOT NULL,
    cron_expr character varying(128),
    status smallint DEFAULT 1,
    last_run_at timestamp without time zone,
    next_run_at timestamp without time zone,
    config_json jsonb,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    is_deleted smallint DEFAULT 0
);

CREATE SEQUENCE IF NOT EXISTS platform.task_schedules_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE platform.task_schedules_id_seq OWNED BY platform.task_schedules.id;

CREATE TABLE IF NOT EXISTS platform.tenant_realm_map (
    id bigint NOT NULL,
    tenant_id character varying(64) NOT NULL,
    realm character varying(128) NOT NULL,
    issuer_url character varying(512) NOT NULL,
    client_id character varying(255) NOT NULL,
    status smallint DEFAULT 1,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE IF NOT EXISTS platform.tenant_realm_map_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE platform.tenant_realm_map_id_seq OWNED BY platform.tenant_realm_map.id;

CREATE TABLE IF NOT EXISTS platform.tenant_security_configs (
    id bigint NOT NULL,
    tenant_id character varying(64) NOT NULL,
    config_key character varying(128) NOT NULL,
    config_value text,
    value_type character varying(32) DEFAULT 'int'::character varying,
    description character varying(512),
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE IF NOT EXISTS platform.tenant_security_configs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE platform.tenant_security_configs_id_seq OWNED BY platform.tenant_security_configs.id;

CREATE TABLE IF NOT EXISTS platform.tenants (
    id character varying(64) NOT NULL,
    name character varying(255) NOT NULL,
    description text,
    status smallint DEFAULT 1,
    plan_type character varying(32) DEFAULT 'free'::character varying,
    expire_time timestamp without time zone,
    quota_config jsonb,
    extra_config jsonb,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    is_deleted smallint DEFAULT 0
);

CREATE TABLE IF NOT EXISTS platform.user_roles (
    id bigint NOT NULL,
    tenant_id character varying(64) NOT NULL,
    user_id bigint NOT NULL,
    role_id bigint NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE IF NOT EXISTS platform.user_roles_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE platform.user_roles_id_seq OWNED BY platform.user_roles.id;

CREATE TABLE IF NOT EXISTS platform.users (
    id bigint NOT NULL,
    tenant_id character varying(64) NOT NULL,
    username character varying(128) NOT NULL,
    password_hash character varying(256),
    display_name character varying(255),
    email character varying(255),
    role character varying(32) DEFAULT 'user'::character varying NOT NULL,
    status smallint DEFAULT 1,
    last_login_at timestamp without time zone,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    is_deleted smallint DEFAULT 0,
    external_id character varying(64),
    upstream_subject character varying(64)
);

CREATE SEQUENCE IF NOT EXISTS platform.users_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE platform.users_id_seq OWNED BY platform.users.id;

ALTER TABLE ONLY platform.api_keys ALTER COLUMN id SET DEFAULT nextval('platform.api_keys_id_seq'::regclass);

ALTER TABLE ONLY platform.application_routes ALTER COLUMN id SET DEFAULT nextval('platform.application_routes_id_seq'::regclass);

ALTER TABLE ONLY platform.applications ALTER COLUMN id SET DEFAULT nextval('platform.applications_id_seq'::regclass);

ALTER TABLE ONLY platform.audit_logs ALTER COLUMN id SET DEFAULT nextval('platform.audit_logs_id_seq'::regclass);

ALTER TABLE ONLY platform.identity_integration_config ALTER COLUMN id SET DEFAULT nextval('platform.identity_integration_config_id_seq'::regclass);

ALTER TABLE ONLY platform.identity_sync_failures ALTER COLUMN id SET DEFAULT nextval('platform.identity_sync_failures_id_seq'::regclass);

ALTER TABLE ONLY platform.identity_sync_tasks ALTER COLUMN id SET DEFAULT nextval('platform.identity_sync_tasks_id_seq'::regclass);

ALTER TABLE ONLY platform.login_tickets ALTER COLUMN id SET DEFAULT nextval('platform.login_tickets_id_seq'::regclass);

ALTER TABLE ONLY platform.menus ALTER COLUMN id SET DEFAULT nextval('platform.menus_id_seq'::regclass);

ALTER TABLE ONLY platform.permissions ALTER COLUMN id SET DEFAULT nextval('platform.permissions_id_seq'::regclass);

ALTER TABLE ONLY platform.role_permissions ALTER COLUMN id SET DEFAULT nextval('platform.role_permissions_id_seq'::regclass);

ALTER TABLE ONLY platform.roles ALTER COLUMN id SET DEFAULT nextval('platform.roles_id_seq'::regclass);

ALTER TABLE ONLY platform.system_configs ALTER COLUMN id SET DEFAULT nextval('platform.system_configs_id_seq'::regclass);

ALTER TABLE ONLY platform.task_schedules ALTER COLUMN id SET DEFAULT nextval('platform.task_schedules_id_seq'::regclass);

ALTER TABLE ONLY platform.tenant_realm_map ALTER COLUMN id SET DEFAULT nextval('platform.tenant_realm_map_id_seq'::regclass);

ALTER TABLE ONLY platform.tenant_security_configs ALTER COLUMN id SET DEFAULT nextval('platform.tenant_security_configs_id_seq'::regclass);

ALTER TABLE ONLY platform.user_roles ALTER COLUMN id SET DEFAULT nextval('platform.user_roles_id_seq'::regclass);

ALTER TABLE ONLY platform.users ALTER COLUMN id SET DEFAULT nextval('platform.users_id_seq'::regclass);

ALTER TABLE ONLY platform.api_keys
    ADD CONSTRAINT api_keys_api_key_key UNIQUE (api_key);

ALTER TABLE ONLY platform.api_keys
    ADD CONSTRAINT api_keys_pkey PRIMARY KEY (id);

ALTER TABLE ONLY platform.application_routes
    ADD CONSTRAINT application_routes_pkey PRIMARY KEY (id);

ALTER TABLE ONLY platform.applications
    ADD CONSTRAINT applications_app_code_key UNIQUE (app_code);

ALTER TABLE ONLY platform.applications
    ADD CONSTRAINT applications_pkey PRIMARY KEY (id);

ALTER TABLE ONLY platform.audit_logs
    ADD CONSTRAINT audit_logs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY platform.identity_integration_config
    ADD CONSTRAINT identity_integration_config_pkey PRIMARY KEY (id);

ALTER TABLE ONLY platform.identity_sync_failures
    ADD CONSTRAINT identity_sync_failures_pkey PRIMARY KEY (id);

ALTER TABLE ONLY platform.identity_sync_tasks
    ADD CONSTRAINT identity_sync_tasks_pkey PRIMARY KEY (id);

ALTER TABLE ONLY platform.login_tickets
    ADD CONSTRAINT login_tickets_pkey PRIMARY KEY (id);

ALTER TABLE ONLY platform.login_tickets
    ADD CONSTRAINT login_tickets_ticket_hash_key UNIQUE (ticket_hash);

ALTER TABLE ONLY platform.mcp_key_service_mappings
    ADD CONSTRAINT mcp_key_service_mappings_pkey PRIMARY KEY (mcp_key_id, service_id);

ALTER TABLE ONLY platform.mcp_keys
    ADD CONSTRAINT mcp_keys_key_hash_key UNIQUE (key_hash);

ALTER TABLE ONLY platform.mcp_keys
    ADD CONSTRAINT mcp_keys_pkey PRIMARY KEY (id);

ALTER TABLE ONLY platform.mcp_service_api_keys
    ADD CONSTRAINT mcp_service_api_keys_key_hash_key UNIQUE (key_hash);

ALTER TABLE ONLY platform.mcp_service_api_keys
    ADD CONSTRAINT mcp_service_api_keys_pkey PRIMARY KEY (id);

ALTER TABLE ONLY platform.mcp_service_publish
    ADD CONSTRAINT mcp_service_publish_pkey PRIMARY KEY (id);

ALTER TABLE ONLY platform.mcp_service_publish
    ADD CONSTRAINT mcp_service_publish_tenant_id_service_id_key UNIQUE (tenant_id, service_id);

ALTER TABLE ONLY platform.menus
    ADD CONSTRAINT menus_pkey PRIMARY KEY (id);

ALTER TABLE ONLY platform.permissions
    ADD CONSTRAINT permissions_code_key UNIQUE (code);

ALTER TABLE ONLY platform.permissions
    ADD CONSTRAINT permissions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY platform.role_permissions
    ADD CONSTRAINT role_permissions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY platform.roles
    ADD CONSTRAINT roles_pkey PRIMARY KEY (id);

ALTER TABLE ONLY platform.system_configs
    ADD CONSTRAINT system_configs_config_key_key UNIQUE (config_key);

ALTER TABLE ONLY platform.system_configs
    ADD CONSTRAINT system_configs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY platform.task_schedules
    ADD CONSTRAINT task_schedules_pkey PRIMARY KEY (id);

ALTER TABLE ONLY platform.tenant_realm_map
    ADD CONSTRAINT tenant_realm_map_pkey PRIMARY KEY (id);

ALTER TABLE ONLY platform.tenant_realm_map
    ADD CONSTRAINT tenant_realm_map_realm_key UNIQUE (realm);

ALTER TABLE ONLY platform.tenant_security_configs
    ADD CONSTRAINT tenant_security_configs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY platform.tenants
    ADD CONSTRAINT tenants_pkey PRIMARY KEY (id);

ALTER TABLE ONLY platform.tenant_security_configs
    ADD CONSTRAINT uq_tenant_security_configs_tenant_key UNIQUE (tenant_id, config_key);

ALTER TABLE ONLY platform.user_roles
    ADD CONSTRAINT user_roles_pkey PRIMARY KEY (id);

ALTER TABLE ONLY platform.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);

CREATE INDEX IF NOT EXISTS idx_api_keys_key ON platform.api_keys USING btree (api_key);

CREATE INDEX IF NOT EXISTS idx_api_keys_tenant ON platform.api_keys USING btree (tenant_id);

CREATE INDEX IF NOT EXISTS idx_ar_app ON platform.application_routes USING btree (app_id);

CREATE INDEX IF NOT EXISTS idx_audit_action ON platform.audit_logs USING btree (action);

CREATE INDEX IF NOT EXISTS idx_audit_log_type ON platform.audit_logs USING btree (log_type);

CREATE INDEX IF NOT EXISTS idx_audit_outcome ON platform.audit_logs USING btree (outcome);

CREATE INDEX IF NOT EXISTS idx_audit_service ON platform.audit_logs USING btree (service_name);

CREATE INDEX IF NOT EXISTS idx_audit_tenant ON platform.audit_logs USING btree (tenant_id);

CREATE INDEX IF NOT EXISTS idx_audit_tenant_type_time ON platform.audit_logs USING btree (tenant_id, log_type, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_audit_time ON platform.audit_logs USING btree (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_audit_user ON platform.audit_logs USING btree (user_id);

CREATE INDEX IF NOT EXISTS idx_identity_integration_config_auto_sync ON platform.identity_integration_config USING btree (next_sync_at NULLS FIRST) WHERE (auto_sync_enabled = true);

CREATE INDEX IF NOT EXISTS idx_identity_sync_failures_task ON platform.identity_sync_failures USING btree (sync_task_id);

CREATE INDEX IF NOT EXISTS idx_identity_sync_tasks_tenant ON platform.identity_sync_tasks USING btree (tenant_id);

CREATE INDEX IF NOT EXISTS idx_login_tickets_app_redirect ON platform.login_tickets USING btree (app_id, redirect_uri);

CREATE INDEX IF NOT EXISTS idx_login_tickets_expires ON platform.login_tickets USING btree (expires_at);

CREATE INDEX IF NOT EXISTS idx_login_tickets_hash ON platform.login_tickets USING btree (ticket_hash);

CREATE INDEX IF NOT EXISTS idx_login_tickets_tenant ON platform.login_tickets USING btree (tenant_id);

CREATE INDEX IF NOT EXISTS idx_mcp_keys_space ON platform.mcp_keys USING btree (space_id);

CREATE INDEX IF NOT EXISTS idx_mcp_keys_tenant_active ON platform.mcp_keys USING btree (tenant_id) WHERE (revoked_at IS NULL);

CREATE INDEX IF NOT EXISTS idx_mcp_keys_write_grants ON platform.mcp_keys USING btree (tenant_id) WHERE (write_grants IS NOT NULL);

CREATE INDEX IF NOT EXISTS idx_mcp_svc_api_keys_service ON platform.mcp_service_api_keys USING btree (tenant_id, service_id);

CREATE INDEX IF NOT EXISTS idx_mcp_svc_api_keys_tenant ON platform.mcp_service_api_keys USING btree (tenant_id);

CREATE INDEX IF NOT EXISTS idx_mcp_svc_pub_service ON platform.mcp_service_publish USING btree (service_id);

CREATE INDEX IF NOT EXISTS idx_mcp_svc_pub_tenant ON platform.mcp_service_publish USING btree (tenant_id);

CREATE INDEX IF NOT EXISTS idx_mcp_svc_pub_tenant_published ON platform.mcp_service_publish USING btree (tenant_id) WHERE (is_published = 1);

CREATE INDEX IF NOT EXISTS idx_mcp_svc_pub_tool ON platform.mcp_service_publish USING btree (tenant_id, tool) WHERE (tool IS NOT NULL);

CREATE INDEX IF NOT EXISTS idx_mk_sv_mapping_key ON platform.mcp_key_service_mappings USING btree (mcp_key_id);

CREATE INDEX IF NOT EXISTS idx_mk_sv_mapping_service ON platform.mcp_key_service_mappings USING btree (service_id);

CREATE INDEX IF NOT EXISTS idx_roles_tenant ON platform.roles USING btree (tenant_id);

CREATE INDEX IF NOT EXISTS idx_rp_perm ON platform.role_permissions USING btree (permission_id);

CREATE INDEX IF NOT EXISTS idx_rp_role ON platform.role_permissions USING btree (role_id);

CREATE INDEX IF NOT EXISTS idx_rp_tenant ON platform.role_permissions USING btree (tenant_id);

CREATE INDEX IF NOT EXISTS idx_task_tenant ON platform.task_schedules USING btree (tenant_id);

CREATE INDEX IF NOT EXISTS idx_task_type ON platform.task_schedules USING btree (task_type);

CREATE INDEX IF NOT EXISTS idx_tenant_realm_map_tenant ON platform.tenant_realm_map USING btree (tenant_id);

CREATE INDEX IF NOT EXISTS idx_tenant_security_configs_tenant ON platform.tenant_security_configs USING btree (tenant_id);

CREATE INDEX IF NOT EXISTS idx_ur_role ON platform.user_roles USING btree (role_id);

CREATE INDEX IF NOT EXISTS idx_ur_tenant ON platform.user_roles USING btree (tenant_id);

CREATE INDEX IF NOT EXISTS idx_ur_user ON platform.user_roles USING btree (user_id);

CREATE INDEX IF NOT EXISTS idx_users_tenant ON platform.users USING btree (tenant_id);

CREATE INDEX IF NOT EXISTS idx_users_username ON platform.users USING btree (tenant_id, username);

CREATE UNIQUE INDEX IF NOT EXISTS roles_tenant_id_name_key ON platform.roles USING btree (tenant_id, name) WHERE (is_deleted = 0);

CREATE UNIQUE INDEX IF NOT EXISTS uq_identity_integration_config_tenant ON platform.identity_integration_config USING btree (tenant_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_identity_sync_tasks_running ON platform.identity_sync_tasks USING btree (tenant_id) WHERE ((status)::text = 'running'::text);

CREATE UNIQUE INDEX IF NOT EXISTS uq_mcp_keys_tenant_reqid ON platform.mcp_keys USING btree (tenant_id, client_request_id) WHERE (client_request_id IS NOT NULL);

CREATE UNIQUE INDEX IF NOT EXISTS uq_role_permissions ON platform.role_permissions USING btree (tenant_id, role_id, permission_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_user_roles ON platform.user_roles USING btree (tenant_id, user_id, role_id);

CREATE UNIQUE INDEX IF NOT EXISTS users_tenant_id_external_id_key ON platform.users USING btree (tenant_id, external_id) WHERE (is_deleted = 0);

CREATE UNIQUE INDEX IF NOT EXISTS users_tenant_id_upstream_subject_key ON platform.users USING btree (tenant_id, upstream_subject) WHERE ((is_deleted = 0) AND (upstream_subject IS NOT NULL));

CREATE UNIQUE INDEX IF NOT EXISTS users_tenant_id_username_key ON platform.users USING btree (tenant_id, username) WHERE (is_deleted = 0);
