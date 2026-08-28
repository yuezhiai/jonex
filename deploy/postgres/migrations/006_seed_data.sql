-- ============================================================
-- 悦溪平台数据库初始化 - 种子数据
-- 版本: 006
-- 包含：开发租户、管理员用户、基础角色/权限、基础菜单、基础应用
-- ============================================================

-- 开发租户
INSERT INTO platform.tenants (id, name, description, plan_type)
VALUES ('tenant_jonex_demo', '悦溪默认租户', '默认租户', 'free')
ON CONFLICT (id) DO NOTHING;

-- 多租户登录测试租户
INSERT INTO platform.tenants (id, name, description, plan_type)
VALUES
    ('tenant_jonex_alpha', '悦溪 Alpha 租户', '用于多租户登录选择流程', 'free'),
    ('tenant_jonex_beta', '悦溪 Beta 租户', '用于多租户登录选择流程', 'free')
ON CONFLICT (id) DO NOTHING;

-- 测试 API Key
INSERT INTO platform.api_keys (tenant_id, api_key, name, rate_limit)
VALUES ('tenant_jonex_demo', 'jonex_test_key', '示例 API Key', 1000)
ON CONFLICT (api_key) DO NOTHING;

-- 管理员用户 (password: admin123)
INSERT INTO platform.users (tenant_id, username, password_hash, display_name, role)
VALUES ('tenant_jonex_demo', 'admin',
        '$2b$12$IRcfNr1RSXcVINY.tBvnGefCYSiMdQLI/BaUk/ARNpVFzr0BVQhCG',
        '平台管理员', 'admin');


-- 多租户登录测试用户
-- password: admin123
-- multi_same_pass/admin123：同名用户在多个租户密码均匹配，预期返回 tenant_selection_required。
INSERT INTO platform.users (tenant_id, username, password_hash, display_name, role)
VALUES
    ('tenant_jonex_demo', 'multi_same_pass',
     '$2b$12$IRcfNr1RSXcVINY.tBvnGefCYSiMdQLI/BaUk/ARNpVFzr0BVQhCG',
     '同名同密用户 - 租户', 'admin'),
    ('tenant_jonex_alpha', 'multi_same_pass',
     '$2b$12$IRcfNr1RSXcVINY.tBvnGefCYSiMdQLI/BaUk/ARNpVFzr0BVQhCG',
     '同名同密用户 - Alpha 租户', 'admin');


-- multi_one_match/admin123：同名用户存在于多个租户，但只有演示租户密码匹配，预期直接登录演示租户。
-- multi_one_match/other123：只有 Alpha 租户密码匹配，预期直接登录 Alpha 租户。
INSERT INTO platform.users (tenant_id, username, password_hash, display_name, role)
VALUES
    ('tenant_jonex_demo', 'multi_one_match',
     '$2b$12$IRcfNr1RSXcVINY.tBvnGefCYSiMdQLI/BaUk/ARNpVFzr0BVQhCG',
     '单租户密码匹配用户 - 租户', 'admin'),
    ('tenant_jonex_alpha', 'multi_one_match',
     '$2b$12$A9CcGiSS0l31Ejy8CJNCyeyiIigzyr3hZjzuhpp3PBA9EkbrZyw6O',
     '单租户密码匹配用户 - Alpha 租户', 'admin');


-- tenant_header_user/admin123：用于测试携带 X-Tenant-ID 的指定租户登录。
INSERT INTO platform.users (tenant_id, username, password_hash, display_name, role)
VALUES
    ('tenant_jonex_beta', 'tenant_header_user',
     '$2b$12$IRcfNr1RSXcVINY.tBvnGefCYSiMdQLI/BaUk/ARNpVFzr0BVQhCG',
     '指定租户登录用户 - Beta 租户', 'admin');


-- 空间权限演示账号（设计 2026-08-19-space-permission-design：默认空间成员演示）
-- password: admin123
INSERT INTO platform.users (tenant_id, username, password_hash, display_name, role)
VALUES
    ('tenant_jonex_demo', 'editor_demo',
     '$2b$12$IRcfNr1RSXcVINY.tBvnGefCYSiMdQLI/BaUk/ARNpVFzr0BVQhCG',
     '演示知识编辑者', 'user'),
    ('tenant_jonex_demo', 'viewer_demo',
     '$2b$12$IRcfNr1RSXcVINY.tBvnGefCYSiMdQLI/BaUk/ARNpVFzr0BVQhCG',
     '演示观察者', 'user')
ON CONFLICT DO NOTHING;

-- 旧 platform:* 权限种子已废弃移除（RBAC 系统前遗留；无 id 分配曾占用序列 id 1-13，
-- 导致 RBAC 显式 id 1-14 基础码块被 ON CONFLICT 跳过；无端点消费、无映射引用）。
-- 权限码统一由下方 RBAC 三批种子提供。

-- RBAC 权限（带显式 id，供角色-权限映射引用）
INSERT INTO platform.permissions (id, code, name, resource, action, description) VALUES
    (1, 'tenant:read', '查看租户', 'tenant', 'read', '查看租户列表和详情'),
    (2, 'tenant:write', '管理租户', 'tenant', 'write', '创建、编辑、删除租户'),
    (3, 'user:read', '查看用户', 'user', 'read', '查看用户列表和详情'),
    (4, 'user:write', '管理用户', 'user', 'write', '创建、编辑、删除用户'),
    (5, 'role:read', '查看角色', 'role', 'read', '查看角色和权限配置'),
    (6, 'role:write', '管理角色', 'role', 'write', '创建、编辑、删除角色，分配权限'),
    (7, 'knowledge:read', '查看知识', 'knowledge', 'read', '检索和查看知识库内容'),
    (8, 'knowledge:write', '编辑知识', 'knowledge', 'write', '编辑、上传、维护知识文档'),
    (9, 'service:read', '查看服务', 'service', 'read', '查看领域服务和配置'),
    (10, 'service:write', '管理服务', 'service', 'write', '创建和管理领域服务、知识库、数据源'),
    (11, 'model:read', '查看模型', 'model', 'read', '查看模型配置和状态'),
    (12, 'model:write', '管理模型', 'model', 'write', '配置和管理模型适配'),
    (13, 'system:read', '查看系统配置', 'system', 'read', '查看系统配置项'),
    (14, 'system:write', '管理系统配置', 'system', 'write', '修改系统配置')
ON CONFLICT (id) DO NOTHING;

-- 显式 id 插入不推进序列：立即对齐，防后续无 id 的 INSERT 撞主键
SELECT setval('platform.permissions_id_seq', (SELECT MAX(id) FROM platform.permissions), true);

-- 预设角色（语义：平台管理员 = 平台级最高权限仅 admin 绑定；租户管理员 = 租户内管理员）
INSERT INTO platform.roles (id, tenant_id, name, description, is_system) VALUES
    (1, 'tenant_jonex_demo', '平台管理员', '平台级最高权限：全部权限码（含平台面跨租户码），仅 demo 租户 admin 账号绑定', 1),
    (2, 'tenant_jonex_demo', '租户管理员', '租户内管理员：全部租户业务码（不含平台面码）', 0),
    (3, 'tenant_jonex_demo', '领域服务管理员', '管理领域服务、知识库、数据源等服务相关配置；领域空间由平台/租户管理员创建后授权管理（空间权限收紧，2026-08-19）', 0),
    (4, 'tenant_jonex_demo', '知识编辑者', '负责知识的编辑、上传和维护，可管理知识库中的文档和数据', 0),
    (5, 'tenant_jonex_demo', '观察者', '仅可检索和查看知识，不具备编辑和管理权限，适用于只读访问场景', 0)
ON CONFLICT (id) DO NOTHING;

-- 显式 id 插入不推进序列：立即对齐，防后续无 id 的角色播种撞主键
SELECT setval('platform.roles_id_seq', (SELECT MAX(id) FROM platform.roles), true);

-- RBAC 权限系统：平台面权限码（scope='platform'；其中 7 个 code 与上方旧 platform:* 种子重名，
-- 依赖 ON CONFLICT DO UPDATE 把 scope 收敛为 platform）
INSERT INTO platform.permissions (code, name, resource, action, description, scope) VALUES
    ('platform:admin', '平台管理员', 'platform', 'admin', '平台管理员标识码（is_platform_admin 判定）', 'platform'),
    ('platform:tenant:read', '查看全部租户', 'tenant', 'read', '平台级：租户列表/详情/计数', 'platform'),
    ('platform:tenant:write', '管理租户', 'tenant', 'write', '平台级：创建/编辑/删除租户', 'platform'),
    ('platform:user:all', '跨租户查看全部用户', 'user', 'all', '平台级：/users/all', 'platform'),
    ('platform:menu:read', '查看菜单', 'menu', 'read', '平台级：全量菜单树', 'platform'),
    ('platform:menu:write', '管理菜单', 'menu', 'write', '平台级：菜单写', 'platform'),
    ('platform:application:read', '查看应用', 'application', 'read', '平台级：应用注册管理', 'platform'),
    ('platform:application:write', '管理应用', 'application', 'write', '平台级：应用写', 'platform'),
    ('platform:config:read', '查看配置', 'system_config', 'read', '平台级：系统配置', 'platform'),
    ('platform:config:write', '管理配置', 'system_config', 'write', '平台级：配置写', 'platform'),
    ('platform:audit:read', '查看审计日志', 'audit_log', 'read', '平台级：审计日志', 'platform'),
    ('platform:task:read', '查看任务', 'task_schedule', 'read', '平台级：任务调度', 'platform'),
    ('platform:task:write', '管理任务', 'task_schedule', 'write', '平台级：任务写', 'platform')
ON CONFLICT (code) DO UPDATE SET scope = EXCLUDED.scope;

-- 兜底：旧种子 6 条废弃 platform:* 码（platform:user/role/app:*）无同名新码覆盖，
-- scope 会停在 DEFAULT 'tenant'——显式收敛，防非特权租户枚举/绑定
UPDATE platform.permissions SET scope = 'platform'
WHERE code LIKE 'platform:%' AND scope <> 'platform';

-- RBAC 权限系统：租户业务权限码（scope='tenant'，新增 10 个；user/role/knowledge/service/model 已在 id 1-14）
INSERT INTO platform.permissions (code, name, resource, action, description, scope) VALUES
    ('engine:read', '查看引擎目录', 'engine', 'read', '接入方式/解析器/模型目录查看', 'tenant'),
    ('engine:write', '管理引擎目录', 'engine', 'write', '接入方式/解析器/模型目录管理', 'tenant'),
    ('adapter:read', '查看适配器', 'adapter', 'read', '生态适配器查看', 'tenant'),
    ('adapter:write', '管理适配器', 'adapter', 'write', '生态适配器管理', 'tenant'),
    ('skill:read', '查看技能', 'skill', 'read', '技能查看', 'tenant'),
    ('skill:write', '管理技能', 'skill', 'write', '技能启用/停用', 'tenant'),
    ('template:read', '查看模板', 'template', 'read', '模板系统查看', 'tenant'),
    ('template:write', '管理模板', 'template', 'write', '模板系统编辑', 'tenant'),
    ('prompt:read', '查看提示词模板', 'prompt', 'read', '提示词模板查看', 'tenant'),
    ('prompt:write', '管理提示词模板', 'prompt', 'write', '提示词模板编辑', 'tenant'),
    ('mcp:service:view', '查看 MCP 服务', 'service', 'view', 'MCP 服务目录与详情查看', 'tenant'),
    ('mcp:service:manage', '管理 MCP 服务', 'service', 'manage', 'MCP 服务发布/取消发布/停用/启用', 'tenant'),
    ('mcp:key:view', '查看 MCP Key', 'key', 'view', '本租户 MCP Key 脱敏信息与授权查看', 'tenant'),
    ('mcp:key:manage', '管理 MCP Key', 'key', 'manage', '本租户 MCP Key 创建/停用/启用/撤销/重新创建/删除', 'tenant')
ON CONFLICT (code) DO NOTHING;

-- 角色-权限关联（基础矩阵：平台管理员/租户管理员全 14 码；领域服务管理员/知识编辑者/观察者按矩阵）
INSERT INTO platform.role_permissions (tenant_id, role_id, permission_id) VALUES
    ('tenant_jonex_demo', 1, 1), ('tenant_jonex_demo', 1, 2), ('tenant_jonex_demo', 1, 3),
    ('tenant_jonex_demo', 1, 4), ('tenant_jonex_demo', 1, 5), ('tenant_jonex_demo', 1, 6),
    ('tenant_jonex_demo', 1, 7), ('tenant_jonex_demo', 1, 8), ('tenant_jonex_demo', 1, 9),
    ('tenant_jonex_demo', 1, 10), ('tenant_jonex_demo', 1, 11), ('tenant_jonex_demo', 1, 12),
    ('tenant_jonex_demo', 1, 13), ('tenant_jonex_demo', 1, 14),
    ('tenant_jonex_demo', 2, 1), ('tenant_jonex_demo', 2, 2), ('tenant_jonex_demo', 2, 3),
    ('tenant_jonex_demo', 2, 4), ('tenant_jonex_demo', 2, 5), ('tenant_jonex_demo', 2, 6),
    ('tenant_jonex_demo', 2, 7), ('tenant_jonex_demo', 2, 8), ('tenant_jonex_demo', 2, 9),
    ('tenant_jonex_demo', 2, 10), ('tenant_jonex_demo', 2, 11), ('tenant_jonex_demo', 2, 12),
    ('tenant_jonex_demo', 2, 13), ('tenant_jonex_demo', 2, 14),
    ('tenant_jonex_demo', 3, 3), ('tenant_jonex_demo', 3, 7),
    ('tenant_jonex_demo', 3, 9), ('tenant_jonex_demo', 3, 10),
    ('tenant_jonex_demo', 4, 7), ('tenant_jonex_demo', 4, 8), ('tenant_jonex_demo', 4, 9),
    ('tenant_jonex_demo', 5, 7), ('tenant_jonex_demo', 5, 9)
ON CONFLICT DO NOTHING;

-- demo 预设角色 × 新增业务码矩阵（平台管理员/租户管理员/领域服务管理员全 rw；知识编辑者/观察者 read）
INSERT INTO platform.role_permissions (tenant_id, role_id, permission_id)
SELECT 'tenant_jonex_demo', r.id, p.id
FROM platform.roles r
JOIN platform.permissions p ON p.code IN (
    'engine:read','engine:write','adapter:read','adapter:write','skill:read','skill:write',
    'template:read','template:write','prompt:read','prompt:write','mcp:service:view','mcp:service:manage','mcp:key:view','mcp:key:manage'
)
WHERE r.tenant_id = 'tenant_jonex_demo' AND r.is_deleted = 0
  AND r.name IN ('平台管理员', '租户管理员', '领域服务管理员')
ON CONFLICT DO NOTHING;

-- demo 平台管理员补全部平台面码（平台码只给平台管理员角色）
INSERT INTO platform.role_permissions (tenant_id, role_id, permission_id)
SELECT r.tenant_id, r.id, p.id
FROM platform.roles r
JOIN platform.permissions p ON p.scope = 'platform'
WHERE r.tenant_id = 'tenant_jonex_demo' AND r.name = '平台管理员' AND r.is_deleted = 0
ON CONFLICT DO NOTHING;

-- 平台管理员 + 租户管理员补全部租户业务码（兜底幂等）
INSERT INTO platform.role_permissions (tenant_id, role_id, permission_id)
SELECT r.tenant_id, r.id, p.id
FROM platform.roles r
JOIN platform.permissions p ON p.scope = 'tenant'
WHERE r.tenant_id = 'tenant_jonex_demo' AND r.name IN ('平台管理员', '租户管理员') AND r.is_deleted = 0
ON CONFLICT DO NOTHING;

-- 每租户预设角色播种（demo 已显式插入 id 1-4；其他租户按 demo 模板复制，is_system=0）
INSERT INTO platform.roles (tenant_id, name, description, is_system)
SELECT t.id, r.name, r.description, 0
FROM platform.tenants t
CROSS JOIN (SELECT name, description FROM platform.roles
            WHERE tenant_id = 'tenant_jonex_demo' AND is_deleted = 0
              AND name IN ('租户管理员','领域服务管理员','知识编辑者','观察者')) r
WHERE t.id <> 'tenant_jonex_demo' AND t.is_deleted = 0
ON CONFLICT DO NOTHING;

-- 每租户角色-权限映射播种（按角色 name 对齐 demo 模板；仅 scope='tenant' 码）
INSERT INTO platform.role_permissions (tenant_id, role_id, permission_id)
SELECT t.id, nr.id, rp.permission_id
FROM platform.roles nr
JOIN platform.tenants t ON nr.tenant_id = t.id AND t.is_deleted = 0
JOIN platform.roles dr ON dr.tenant_id = 'tenant_jonex_demo' AND dr.name = nr.name AND dr.is_deleted = 0
JOIN platform.role_permissions rp ON rp.tenant_id = 'tenant_jonex_demo' AND rp.role_id = dr.id
JOIN platform.permissions p ON p.id = rp.permission_id
WHERE t.id <> 'tenant_jonex_demo' AND p.scope = 'tenant'
ON CONFLICT DO NOTHING;

-- 用户-角色绑定种子（语义：平台级仅 demo 租户的 admin 账号；其余 role='admin' 为租户内管理员）
-- 6a. 平台级：仅 demo 租户 username='admin' → 「平台管理员」
INSERT INTO platform.user_roles (tenant_id, user_id, role_id)
SELECT u.tenant_id, u.id, r.id
FROM platform.users u
JOIN platform.roles r ON r.tenant_id = u.tenant_id AND r.is_deleted = 0
WHERE u.tenant_id = 'tenant_jonex_demo' AND u.username = 'admin' AND u.is_deleted = 0
  AND r.name = '平台管理员'
ON CONFLICT DO NOTHING;

-- 6b. 租户内管理员：其余 role='admin' 用户 → 各自租户「租户管理员」（无平台码）
INSERT INTO platform.user_roles (tenant_id, user_id, role_id)
SELECT u.tenant_id, u.id, r.id
FROM platform.users u
JOIN platform.roles r ON r.tenant_id = u.tenant_id AND r.is_deleted = 0
WHERE u.role = 'admin' AND u.is_deleted = 0 AND r.name = '租户管理员'
  AND NOT (u.tenant_id = 'tenant_jonex_demo' AND u.username = 'admin')
ON CONFLICT DO NOTHING;

-- 6c. role='user' → 「观察者」
INSERT INTO platform.user_roles (tenant_id, user_id, role_id)
SELECT u.tenant_id, u.id, r.id
FROM platform.users u
JOIN platform.roles r ON r.tenant_id = u.tenant_id AND r.is_deleted = 0
WHERE u.role = 'user' AND u.is_deleted = 0 AND r.name = '观察者'
ON CONFLICT DO NOTHING;

-- 6d. 空间权限演示：editor_demo → 「知识编辑者」（叠加 6c 的「观察者」，权限码取并集）
INSERT INTO platform.user_roles (tenant_id, user_id, role_id)
SELECT u.tenant_id, u.id, r.id
FROM platform.users u
JOIN platform.roles r ON r.tenant_id = u.tenant_id AND r.is_deleted = 0
WHERE u.tenant_id = 'tenant_jonex_demo' AND u.username = 'editor_demo' AND u.is_deleted = 0
  AND r.name = '知识编辑者'
ON CONFLICT DO NOTHING;

-- 默认菜单（四大板块：领域本体/数据接入/集成扩展/平台管理；path 为前端 hosted 路由；分组 permission_code=NULL 靠子项裁剪）
INSERT INTO platform.menus (id, parent_id, name, path, icon, app_id, sort_order, permission_code) VALUES
    -- 领域本体分组（直接项，无中间组）
    (1, 0, 'navigation.coreBusiness', NULL, 'HomeOutlined', NULL, 1, NULL),
    (2, 1, 'navigation.knowledgeSearch', '/apps/core-business/knowledge-search', 'SearchOutlined', NULL, 1, 'knowledge:read'),
    (3, 1, 'navigation.domainKnowledge', '/apps/core-business/domain-knowledge', 'DatabaseOutlined', NULL, 2, 'knowledge:read'),
    (4, 1, 'navigation.domainManagement', '/apps/core-business/domain-management', 'ClusterOutlined', NULL, 3, 'service:read'),
    (20, 1, 'navigation.templateDomains', '/apps/ecosystem-management/template-domains', 'CopyOutlined', NULL, 4, 'template:read'),
    -- 数据接入分组 → 数据源管理、解析器管理（直接项）
    (21, 0, 'navigation.dataAccessGroup', NULL, 'CloudServerOutlined', NULL, 2, NULL),
    (7, 21, 'navigation.dataAccess', '/apps/platform-management/data-access', 'CloudServerOutlined', NULL, 1, 'engine:read'),
    (8, 21, 'navigation.parserManagement', '/apps/platform-management/parser-management', 'CodeOutlined', NULL, 2, 'engine:read'),
    -- 集成扩展分组 → Mcp生态（直接项；适配器目录已注释隐藏）
    (22, 0, 'navigation.integrationExtension', NULL, 'GlobalOutlined', NULL, 3, NULL),
    -- (18, 22, 'navigation.adapterList', '/apps/ecosystem-management/adapter-management', 'BlockOutlined', NULL, 1, 'adapter:read'), -- 适配器目录：已注释隐藏，恢复时放开本行
    (19, 22, 'navigation.mcpServiceDirectory', '/apps/ecosystem-management/mcp-service-directory', 'ClusterOutlined', NULL, 2, 'mcp:service:view'),
    -- 平台管理分组 → 账号与权限组（可折叠）
    (5, 0, 'navigation.platformManagement', NULL, 'SettingOutlined', NULL, 4, NULL),
    (25, 5, 'navigation.accountPermission', NULL, 'TeamOutlined', NULL, 1, NULL),
    (11, 25, 'navigation.tenantManagement', '/apps/platform-management/tenant-management', 'TeamOutlined', NULL, 1, 'platform:tenant:read'),
    (12, 25, 'navigation.userManagement', '/apps/platform-management/user-management', 'UserOutlined', NULL, 2, 'user:read'),
    (13, 25, 'navigation.rolePermission', '/apps/platform-management/role-permission', 'SafetyOutlined', NULL, 3, 'role:read'),
    -- 平台管理分组 → 提示词与模板（直接项）
    (9, 5, 'navigation.promptTemplates', '/apps/ecosystem-management/prompt-templates', 'FileTextOutlined', NULL, 2, 'prompt:read'),
    -- 平台管理分组 → 系统运维组（可折叠）
    (26, 5, 'navigation.systemOperations', NULL, 'SettingOutlined', NULL, 3, NULL),
    (14, 26, 'navigation.systemConfig', '/apps/platform-management/system-config', 'SettingOutlined', NULL, 1, 'platform:config:read'),
    (15, 26, 'navigation.operationLog', '/apps/platform-management/operation-log', 'FileTextOutlined', NULL, 2, 'platform:audit:read')
ON CONFLICT DO NOTHING;

-- 默认应用注册
INSERT INTO platform.applications (app_code, name, entry_path, description, sort_order) VALUES
    ('shell', '悦溪 Shell', '/', '统一登录入口与导航壳', 1),
    ('core-business', '核心业务', '/apps/core-business', '核心业务管理', 2),
    ('platform-management', '平台管理', '/apps/platform-management', '平台管理', 3),
    ('ecosystem-management', '生态管理', '/apps/ecosystem-management', '生态管理', 4)
ON CONFLICT (app_code) DO NOTHING;

-- ============================================================
-- AI Skill 种子数据（skill_catalog + tenant_skills）
-- ============================================================

INSERT INTO business_domain.skill_catalog (
    id, name, description, category, icon, status,
    tool_name, instruction, input_schema_json, output_schema_json,
    artifact_bucket, artifact_object_key, artifact_checksum, artifact_size, artifact_content_type,
    tags_json, capability_json
) VALUES (
    'skill_image_recognition',
    '图像识别与分析',
    '对图片内容进行智能识别，提取物体、文字、场景等多模态信息，支持OCR文字识别',
    'image', 'FileImage', 'published',
    'image_recognition',
    '当用户需要识别图片中的物体、文字或场景时调用该工具。传入图片URL，返回识别结果和置信度。',
    '{"type":"object","properties":{"file_url":{"type":"string","description":"可访问的图片地址"},"tasks":{"type":"array","items":{"type":"string","enum":["ocr","object_detection","scene_classification"]},"description":"识别任务列表"}},"required":["file_url"]}'::jsonb,
    '{"type":"object","properties":{"text":{"type":"string"},"objects":{"type":"array"},"scene":{"type":"string"},"confidence":{"type":"number"}}}'::jsonb,
    'jonex-skills',
    'mcp-tools/image-recognition/latest/image-recognition.zip',
    'sha256:replace_with_real_checksum',
    0, 'application/zip',
    '["图像","OCR","识别"]'::jsonb,
    '{"requires_file":true,"supports_batch":true}'::jsonb
) ON CONFLICT (id) DO NOTHING;

INSERT INTO business_domain.skill_catalog (
    id, name, description, category, icon, status,
    tool_name, instruction, input_schema_json, output_schema_json,
    artifact_bucket, artifact_object_key, artifact_checksum, artifact_size, artifact_content_type,
    tags_json, capability_json
) VALUES (
    'skill_speech_to_text',
    '语音转文本',
    '将音频文件中的语音内容自动转录为结构化文本，支持多语种和说话人分离',
    'voice', 'Audio', 'published',
    'speech_to_text',
    '当用户需要将音频、录音转换为文字时调用该工具。支持多语种识别和说话人分离。',
    '{"type":"object","properties":{"file_url":{"type":"string","description":"可访问的音频文件地址"},"language":{"type":"string","description":"音频语种，如 zh-CN"},"speaker_diarization":{"type":"boolean","description":"是否启用说话人分离"}},"required":["file_url"]}'::jsonb,
    '{"type":"object","properties":{"text":{"type":"string"},"segments":{"type":"array"},"speakers":{"type":"array"}}}'::jsonb,
    'jonex-skills',
    'mcp-tools/speech-to-text/latest/speech-to-text.zip',
    'sha256:replace_with_real_checksum',
    0, 'application/zip',
    '["语音","转录","ASR"]'::jsonb,
    '{"requires_file":true,"supports_batch":true}'::jsonb
) ON CONFLICT (id) DO NOTHING;

INSERT INTO business_domain.skill_catalog (
    id, name, description, category, icon, status,
    tool_name, instruction, input_schema_json, output_schema_json,
    artifact_bucket, artifact_object_key, artifact_checksum, artifact_size, artifact_content_type,
    tags_json, capability_json
) VALUES (
    'skill_document_layout',
    '文档版面分析',
    '分析PDF、图片等文档的版面结构，识别段落、表格、图表、页眉页脚等元素',
    'document', 'FileText', 'published',
    'document_layout_analysis',
    '当用户需要分析PDF或图片文档的版面结构时调用该工具。可识别段落、表格、图表、页眉页脚等版面元素。',
    '{"type":"object","properties":{"file_url":{"type":"string","description":"可访问的文档地址（PDF或图片）"},"output_format":{"type":"string","enum":["json","markdown"],"description":"输出格式"}},"required":["file_url"]}'::jsonb,
    '{"type":"object","properties":{"pages":{"type":"array"},"elements":{"type":"array"},"structure":{"type":"object"}}}'::jsonb,
    'jonex-skills',
    'mcp-tools/document-layout/latest/document-layout.zip',
    'sha256:replace_with_real_checksum',
    0, 'application/zip',
    '["文档","版面","结构化"]'::jsonb,
    '{"requires_file":true,"supports_batch":false}'::jsonb
) ON CONFLICT (id) DO NOTHING;

INSERT INTO business_domain.skill_catalog (
    id, name, description, category, icon, status,
    tool_name, instruction, input_schema_json, output_schema_json,
    artifact_bucket, artifact_object_key, artifact_checksum, artifact_size, artifact_content_type,
    tags_json, capability_json
) VALUES (
    'skill_video_understanding',
    '视频内容理解',
    '对视频内容进行抽帧分析，识别场景、动作、人物及事件时间线',
    'video', 'VideoCamera', 'published',
    'video_understanding',
    '当用户需要理解视频内容时调用该工具。可进行抽帧分析、场景识别、动作检测和时间线提取。',
    '{"type":"object","properties":{"file_url":{"type":"string","description":"可访问的视频文件地址"},"sample_rate":{"type":"integer","description":"抽帧间隔（秒）","default":5}},"required":["file_url"]}'::jsonb,
    '{"type":"object","properties":{"scenes":{"type":"array"},"objects":{"type":"array"},"timeline":{"type":"array"},"summary":{"type":"string"}}}'::jsonb,
    'jonex-skills',
    'mcp-tools/video-understanding/latest/video-understanding.zip',
    'sha256:replace_with_real_checksum',
    0, 'application/zip',
    '["视频","抽帧","分析"]'::jsonb,
    '{"requires_file":true,"supports_batch":false}'::jsonb
) ON CONFLICT (id) DO NOTHING;

INSERT INTO business_domain.skill_catalog (
    id, name, description, category, icon, status,
    tool_name, instruction, input_schema_json, output_schema_json,
    artifact_bucket, artifact_object_key, artifact_checksum, artifact_size, artifact_content_type,
    tags_json, capability_json
) VALUES (
    'skill_multimodal_search',
    '多模态融合检索',
    '跨文本、图像、语音等多模态数据的统一语义检索与相似度匹配',
    'fusion', 'Search', 'published',
    'multimodal_search',
    '当用户需要跨文本、图像、语音等多模态数据进行检索时调用该工具。支持语义理解和相似度匹配。',
    '{"type":"object","properties":{"query":{"type":"string","description":"检索查询文本"},"modalities":{"type":"array","items":{"type":"string","enum":["text","image","audio"]},"description":"检索模态范围"},"top_k":{"type":"integer","description":"返回结果数","default":10}},"required":["query"]}'::jsonb,
    '{"type":"object","properties":{"results":{"type":"array"},"total":{"type":"integer"},"scores":{"type":"array"}}}'::jsonb,
    'jonex-skills',
    'mcp-tools/multimodal-search/latest/multimodal-search.zip',
    'sha256:replace_with_real_checksum',
    0, 'application/zip',
    '["检索","融合","语义"]'::jsonb,
    '{"requires_file":false,"supports_batch":true}'::jsonb
) ON CONFLICT (id) DO NOTHING;

INSERT INTO business_domain.skill_catalog (
    id, name, description, category, icon, status,
    tool_name, instruction, input_schema_json, output_schema_json,
    artifact_bucket, artifact_object_key, artifact_checksum, artifact_size, artifact_content_type,
    tags_json, capability_json
) VALUES (
    'skill_data_extraction',
    '智能数据提取',
    '从非结构化文档中自动提取关键字段和结构化数据，支持表格、表单、发票等场景',
    'custom', 'Database', 'published',
    'data_extraction',
    '当用户需要从非结构化文档中提取结构化数据时调用该工具。支持表格、表单、发票等常见文档类型的关键字段提取。',
    '{"type":"object","properties":{"file_url":{"type":"string","description":"可访问的文档地址"},"schema":{"type":"object","description":"期望提取的字段定义"},"doc_type":{"type":"string","enum":["invoice","form","contract","table","general"],"description":"文档类型"}},"required":["file_url"]}'::jsonb,
    '{"type":"object","properties":{"extracted_data":{"type":"object"},"confidence":{"type":"number"},"fields_found":{"type":"array"}}}'::jsonb,
    'jonex-skills',
    'mcp-tools/data-extraction/latest/data-extraction.zip',
    'sha256:replace_with_real_checksum',
    0, 'application/zip',
    '["提取","结构化","文档"]'::jsonb,
    '{"requires_file":true,"supports_batch":true}'::jsonb
) ON CONFLICT (id) DO NOTHING;

-- 演示租户启用示例 Skill
INSERT INTO business_domain.tenant_skills (id, tenant_id, skill_id, status)
VALUES ('tenant_skill_demo_image', 'tenant_jonex_demo', 'skill_image_recognition', 'enabled')
ON CONFLICT (tenant_id, skill_id) DO UPDATE
SET status = EXCLUDED.status, updated_at = CURRENT_TIMESTAMP, is_deleted = 0;

INSERT INTO business_domain.tenant_skills (id, tenant_id, skill_id, status)
VALUES ('tenant_skill_demo_speech', 'tenant_jonex_demo', 'skill_speech_to_text', 'enabled')
ON CONFLICT (tenant_id, skill_id) DO UPDATE
SET status = EXCLUDED.status, updated_at = CURRENT_TIMESTAMP, is_deleted = 0;

INSERT INTO business_domain.tenant_skills (id, tenant_id, skill_id, status)
VALUES ('tenant_skill_demo_layout', 'tenant_jonex_demo', 'skill_document_layout', 'enabled')
ON CONFLICT (tenant_id, skill_id) DO UPDATE
SET status = EXCLUDED.status, updated_at = CURRENT_TIMESTAMP, is_deleted = 0;

-- ============================================================
-- 生态适配器种子数据
-- ============================================================

INSERT INTO business_domain.adapters (id, tenant_id, name, adapter_type, config_json, status)
VALUES
    ('adapter_demo_dingtalk', 'tenant_jonex_demo', 'ADP 适配器', 'dingtalk',
     '{"description":"ADP 人力资源数据接入"}'::jsonb, 'connected'),
    ('adapter_demo_wechat_agent', 'tenant_jonex_demo', 'HiAgent 适配器', 'wechat_work',
     '{"description":"HiAgent 智能体平台集成"}'::jsonb, 'disconnected'),
    ('adapter_demo_feishu_analytics', 'tenant_jonex_demo', 'AWS QuickSight', 'feishu',
     '{"description":"AWS 数据分析平台集成"}'::jsonb, 'disconnected'),
    ('adapter_demo_dingtalk_ai', 'tenant_jonex_demo', 'Gemini 适配器', 'dingtalk',
     '{"description":"Google Gemini 模型接入"}'::jsonb, 'disconnected'),
    ('adapter_demo_wechat_workbench', 'tenant_jonex_demo', 'WorkBuddy', 'wechat_work',
     '{"description":"WorkBuddy 工作流集成"}'::jsonb, 'disconnected'),
    ('adapter_demo_feishu_crawler', 'tenant_jonex_demo', 'Claw 适配器', 'feishu',
     '{"description":"Claw 数据抓取平台集成"}'::jsonb, 'disconnected')
ON CONFLICT (id) DO NOTHING;

-- ============================================================
-- 模型供应商种子数据
-- ============================================================

INSERT INTO business_domain.model_providers (id, name, provider_type, model_type, model_name, latency_ms, token_limit, vector_dimension, call_count, success_rate, status, config_json)
VALUES
    ('provider_demo_gpt4o', 'GPT-4o', 'llm', '对话模型', 'gpt-4o', 1200, 128000, NULL, 12458, 99, 'active', '{"vendor":"OpenAI"}'::jsonb),
    ('provider_demo_claude', 'Claude Opus 4', 'llm', '对话模型', 'claude-opus-4', 1800, 200000, NULL, 8234, 99, 'active', '{"vendor":"Anthropic"}'::jsonb),
    ('provider_demo_text2vec', 'text2vec-large', 'embedding', '向量模型', 'text2vec-large-chinese', 300, NULL, 768, 56892, 100, 'active', '{"vendor":"本地部署"}'::jsonb),
    ('provider_demo_reranker', 'bge-reranker', 'reranker', '重排序模型', 'bge-reranker-v2-m3', 500, NULL, NULL, 23456, 99, 'active', '{"vendor":"本地部署","batch_size":64}'::jsonb)
ON CONFLICT (id) DO NOTHING;

-- ============================================================
-- 解析器种子数据
-- ============================================================

-- 解析器种子数据（文件扩展名基于 Rag-anything Parser 基类定义）
-- 参考: Reference/Rag-anything/raganything/parser.py
--   OFFICE_FORMATS = .doc .docx .ppt .pptx .xls .xlsx
--   IMAGE_FORMATS  = .png .jpeg .jpg .bmp .tiff .tif .gif .webp
--   TEXT_FORMATS   = .txt .md
--   AUDIO_FORMATS  = .mp3 .wav .m4a .ogg .flac .wma .aac .opus .amr
--   VIDEO_FORMATS  = .mp4 .avi .mov .mkv .flv .wmv .webm .m4v .mpg .mpeg .3gp

INSERT INTO business_domain.parser_configs (id, name, parser_type, file_types, config_json, status)
VALUES
    ('video_full_pipeline', '视频解析器-VLM', 'video',
     '["MP4","AVI","MOV","MKV","FLV","WMV","WEBM","M4V","MPG","MPEG","3GP"]'::jsonb,
     '{"version":"v2.3.0","process_count":1245,"display_fields":[{"label":"关键帧提取","value":"智能模式"},{"label":"分辨率限制","value":"1080p"}]}'::jsonb, 'active'),
    ('video_mps', '视频解析器-MPS', 'video',
     '["MP4","AVI","MOV","MKV","FLV","WMV","WEBM","M4V","MPG","MPEG","3GP"]'::jsonb,
     '{"version":"v2.3.0","process_count":1245,"display_fields":[{"label":"关键帧提取","value":"智能模式"},{"label":"分辨率限制","value":"1080p"}]}'::jsonb, 'active'),
    ('audio_transcribe', '音频解析器', 'audio',
     '["MP3","WAV","FLAC","AAC","M4A","OGG","WMA","OPUS","AMR"]'::jsonb,
     '{"version":"v2.1.2","process_count":3678,"display_fields":[{"label":"转写模型","value":"通用转写模型"},{"label":"输出格式","value":"SRT"}]}'::jsonb, 'active'),
    ('image_parse', '图像解析器', 'image',
     '["JPG","JPEG","PNG","GIF","BMP","TIFF","TIF","WEBP"]'::jsonb,
     '{"version":"v1.9.5","process_count":5432,"display_fields":[{"label":"OCR 引擎","value":"内置 OCR"},{"label":"图像压缩","value":"高质量"}]}'::jsonb, 'active'),
    ('document_parse', '文档解析器', 'document',
     '["PDF","DOC","DOCX","PPT","PPTX","XLS","XLSX"]'::jsonb,
     '{"version":"v3.0.1","process_count":12890,"display_fields":[{"label":"排版保留","value":"启用"},{"label":"表格提取","value":"智能提取"}]}'::jsonb, 'active'),
    ('text_parse', '文本解析器', 'txt',
     '["TXT","MD"]'::jsonb,
     '{"version":"v3.0.1","process_count":12890,"display_fields":[{"label":"排版保留","value":"启用"},{"label":"表格提取","value":"智能提取"}]}'::jsonb, 'active'),
    ('parser_demo_web', '网页解析器', 'web',
     '["HTML","HTM","XHTML"]'::jsonb,
     '{"version":"--","process_count":0,"display_fields":[{"label":"渲染模式","value":"静态渲染"},{"label":"抓取深度","value":"--"}]}'::jsonb, 'inactive'),
    ('parser_demo_cad', 'CAD 解析器', 'cad',
     '["DWG","DXF","STEP"]'::jsonb,
     '{"version":"--","process_count":0,"display_fields":[{"label":"精度等级","value":"标准"},{"label":"图层提取","value":"全部"}]}'::jsonb, 'inactive')
ON CONFLICT (id) DO NOTHING;

-- ============================================================
-- 数据接入方式种子数据（原型 4 个 + API 开放推送）
-- ============================================================

INSERT INTO business_domain.data_access_methods (id, name, access_type, config_json, status) VALUES
    ('dam_demo_api', 'API 接入（拉取）', 'api', '{"description":"通过 REST/gRPC 接口接入数据"}'::jsonb, 'active'),
    ('dam_api_push_demo', 'API 开放（推送）', 'api_push', '{"description":"外部系统通过 OpenAPI 推送文档入库"}'::jsonb, 'active'),
    ('dam_demo_storage', '文件存储直连', 'storage', '{"description":"NAS/S3/MinIO/OSS 等"}'::jsonb, 'active'),
    ('dam_demo_file', '文件上传', 'file', '{"description":"PDF/DOCX/CSV/JSON 等"}'::jsonb, 'active'),
    ('dam_demo_mqtt', 'MQTT 接入', 'mqtt', '{"description":"物联网消息队列接入"}'::jsonb, 'inactive')
ON CONFLICT (id) DO NOTHING;

-- ============================================================
-- 模板领域场景联调数据，互联网领域
-- ============================================================

-- 1. 模板领域
INSERT INTO business_domain.template_domains (id, tenant_id, name, name_en, description, status, version, published_at, structure_hash)
VALUES ('tpl_domain_internet', 'tenant_jonex_demo', '互联网', 'Internet Technology', '互联网科技公司/产品/技术情报模板领域', 'active', 1,
        '2026-06-01T00:00:00+00'::timestamptz,
        'b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0abc1de')
ON CONFLICT (id) DO NOTHING;

-- 2. 模板场景
INSERT INTO business_domain.template_scenarios (id, tenant_id, domain_id, name, description, config_json, version, published_at, structure_hash)
VALUES ('tpl_scenario_internet_general', 'tenant_jonex_demo', 'tpl_domain_internet', '通用', '互联网通用实体/关系抽取场景', '{}'::jsonb, 1,
        '2026-06-01T00:00:00+00'::timestamptz,
        'b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0abc1de')
ON CONFLICT (id) DO NOTHING;

-- 3. 模板对象（8 个互联网核心本体实体）
INSERT INTO business_domain.template_objects (id, tenant_id, domain_id, scenario_id, name, description, status, ontology_code, aliases)
VALUES
 ('tpl_obj_inet_company','tenant_jonex_demo','tpl_domain_internet','tpl_scenario_internet_general','公司','互联网公司/平台方','active','Company','["公司","企业","厂商","平台方","机构","Organization"]'::jsonb),
 ('tpl_obj_inet_product','tenant_jonex_demo','tpl_domain_internet','tpl_scenario_internet_general','产品','App/应用/平台','active','Product','["产品","App","应用","平台","Product"]'::jsonb),
 ('tpl_obj_inet_tech','tenant_jonex_demo','tpl_domain_internet','tpl_scenario_internet_general','技术','框架/协议/算法','active','Technology','["技术","框架","协议","算法","方法","Method"]'::jsonb),
 ('tpl_obj_inet_feature','tenant_jonex_demo','tpl_domain_internet','tpl_scenario_internet_general','功能特性','产品功能/模块','active','Feature','["功能","特性","模块","概念","Concept"]'::jsonb),
 ('tpl_obj_inet_person','tenant_jonex_demo','tpl_domain_internet','tpl_scenario_internet_general','人员','创始人/高管/工程师','active','Person','["人员","创始人","高管","工程师","人","Person"]'::jsonb),
 ('tpl_obj_inet_event','tenant_jonex_demo','tpl_domain_internet','tpl_scenario_internet_general','事件','发布会/融资/活动','active','Event','["事件","发布会","融资","活动","Event"]'::jsonb),
 ('tpl_obj_inet_market','tenant_jonex_demo','tpl_domain_internet','tpl_scenario_internet_general','市场','用户群体/赛道','active','Market','["市场","用户群体","赛道","领域"]'::jsonb),
 ('tpl_obj_inet_investor','tenant_jonex_demo','tpl_domain_internet','tpl_scenario_internet_general','投资机构','VC/资本/投资方','active','Investor','["投资机构","资本","VC","投资方"]'::jsonb)
ON CONFLICT (id) DO NOTHING;

-- 4. 模板属性（每个对象至少一个主键属性）
INSERT INTO business_domain.template_attributes
    (id, tenant_id, template_object_id, attr_name, description, attr_type, is_primary_key, constraints_json, sort_order, ontology_code, is_required)
VALUES
    -- 公司 Company
    ('tpl_attr_inet_co_code','tenant_jonex_demo','tpl_obj_inet_company','统一社会信用代码','企业唯一身份识别代码','字符串',1,'{}'::jsonb,1,'unified_social_credit_code',1),
    ('tpl_attr_inet_co_name','tenant_jonex_demo','tpl_obj_inet_company','公司名称','工商登记企业全称','字符串',0,'{}'::jsonb,2,'company_name',1),
    ('tpl_attr_inet_co_industry','tenant_jonex_demo','tpl_obj_inet_company','所属行业','行业分类，如电商、社交、金融科技等','枚举',0,'{}'::jsonb,3,'industry',0),
    ('tpl_attr_inet_co_founded','tenant_jonex_demo','tpl_obj_inet_company','成立时间','公司成立日期','日期',0,'{}'::jsonb,4,'founded',0),
    ('tpl_attr_inet_co_capital','tenant_jonex_demo','tpl_obj_inet_company','注册资本','注册资本金额（万元）','数值',0,'{}'::jsonb,5,'registered_capital',0),
    ('tpl_attr_inet_co_summary','tenant_jonex_demo','tpl_obj_inet_company','公司简介','公司主营业务与核心竞争力概述','文本',0,'{}'::jsonb,6,'company_summary',0),
    -- 产品 Product
    ('tpl_attr_inet_pd_name','tenant_jonex_demo','tpl_obj_inet_product','产品名称','产品/应用/平台名称','字符串',1,'{}'::jsonb,1,'product_name',1),
    ('tpl_attr_inet_pd_category','tenant_jonex_demo','tpl_obj_inet_product','类别','产品类别，如 App、SaaS、平台等','枚举',0,'{}'::jsonb,2,'category',0),
    ('tpl_attr_inet_pd_platform','tenant_jonex_demo','tpl_obj_inet_product','平台','所属平台，如 iOS、Android、Web 等','枚举',0,'{}'::jsonb,3,'platform',0),
    ('tpl_attr_inet_pd_users','tenant_jonex_demo','tpl_obj_inet_product','用户规模','月活用户数或注册用户数','数值',0,'{}'::jsonb,4,'user_scale',0),
    ('tpl_attr_inet_pd_desc','tenant_jonex_demo','tpl_obj_inet_product','产品描述','产品功能与核心价值描述','文本',0,'{}'::jsonb,5,'product_description',0),
    -- 技术 Technology
    ('tpl_attr_inet_tech_name','tenant_jonex_demo','tpl_obj_inet_tech','技术名称','技术/框架/协议名称','字符串',1,'{}'::jsonb,1,'tech_name',1),
    ('tpl_attr_inet_tech_category','tenant_jonex_demo','tpl_obj_inet_tech','技术分类','如前端框架、后端框架、数据库、AI算法等','枚举',0,'{}'::jsonb,2,'tech_category',0),
    ('tpl_attr_inet_tech_desc','tenant_jonex_demo','tpl_obj_inet_tech','技术描述','技术原理与应用场景说明','文本',0,'{}'::jsonb,3,'tech_description',0),
    -- 功能特性 Feature
    ('tpl_attr_inet_feat_name','tenant_jonex_demo','tpl_obj_inet_feature','特性名称','功能/模块名称','字符串',1,'{}'::jsonb,1,'feature_name',1),
    ('tpl_attr_inet_feat_desc','tenant_jonex_demo','tpl_obj_inet_feature','特性描述','功能特性说明','文本',0,'{}'::jsonb,2,'feature_description',0),
    -- 人员 Person
    ('tpl_attr_inet_per_name','tenant_jonex_demo','tpl_obj_inet_person','姓名','人员姓名','字符串',1,'{}'::jsonb,1,'person_name',1),
    ('tpl_attr_inet_per_title','tenant_jonex_demo','tpl_obj_inet_person','职位','职级/头衔','字符串',0,'{}'::jsonb,2,'title',0),
    ('tpl_attr_inet_per_company','tenant_jonex_demo','tpl_obj_inet_person','所属公司','任职公司/机构','字符串',0,'{}'::jsonb,3,'affiliated_company',0),
    ('tpl_attr_inet_per_bio','tenant_jonex_demo','tpl_obj_inet_person','简介','个人履历与成就摘要','文本',0,'{}'::jsonb,4,'biography',0),
    -- 事件 Event
    ('tpl_attr_inet_ev_name','tenant_jonex_demo','tpl_obj_inet_event','事件名称','事件标题','字符串',1,'{}'::jsonb,1,'event_name',1),
    ('tpl_attr_inet_ev_type','tenant_jonex_demo','tpl_obj_inet_event','事件类型','如发布会、融资、收购、合作等','枚举',0,'{}'::jsonb,2,'event_type',0),
    ('tpl_attr_inet_ev_date','tenant_jonex_demo','tpl_obj_inet_event','日期','事件发生日期','日期',0,'{}'::jsonb,3,'event_date',0),
    ('tpl_attr_inet_ev_desc','tenant_jonex_demo','tpl_obj_inet_event','事件描述','事件详情摘要','文本',0,'{}'::jsonb,4,'event_description',0),
    -- 市场 Market
    ('tpl_attr_inet_mkt_name','tenant_jonex_demo','tpl_obj_inet_market','市场名称','目标市场/赛道名称','字符串',1,'{}'::jsonb,1,'market_name',1),
    ('tpl_attr_inet_mkt_size','tenant_jonex_demo','tpl_obj_inet_market','市场规模','市场规模估值（亿元）','数值',0,'{}'::jsonb,2,'market_size',0),
    ('tpl_attr_inet_mkt_desc','tenant_jonex_demo','tpl_obj_inet_market','市场描述','目标市场特征与趋势描述','文本',0,'{}'::jsonb,3,'market_description',0),
    -- 投资机构 Investor
    ('tpl_attr_inet_inv_name','tenant_jonex_demo','tpl_obj_inet_investor','机构名称','投资机构全称','字符串',1,'{}'::jsonb,1,'investor_name',1),
    ('tpl_attr_inet_inv_type','tenant_jonex_demo','tpl_obj_inet_investor','机构类型','如 VC、PE、CVC、天使投资等','枚举',0,'{}'::jsonb,2,'investor_type',0),
    ('tpl_attr_inet_inv_aum','tenant_jonex_demo','tpl_obj_inet_investor','管理规模','资产管理规模（亿元）','数值',0,'{}'::jsonb,3,'aum',0),
    ('tpl_attr_inet_inv_desc','tenant_jonex_demo','tpl_obj_inet_investor','机构简介','投资风格、赛道偏好与代表案例','文本',0,'{}'::jsonb,4,'investor_description',0)
ON CONFLICT (id) DO NOTHING;

-- 5. 模板关系（9 条核心关系）
INSERT INTO business_domain.template_relations
    (id, tenant_id, domain_id, scenario_id, name, description, source_object_id, target_object_id, relation_type, status, ontology_code, aliases)
VALUES
 ('tpl_rel_inet_develops','tenant_jonex_demo','tpl_domain_internet','tpl_scenario_internet_general','开发','公司开发/推出产品','tpl_obj_inet_company','tpl_obj_inet_product','一对多','active','DEVELOPS','["开发","推出"]'::jsonb),
 ('tpl_rel_inet_usestech','tenant_jonex_demo','tpl_domain_internet','tpl_scenario_internet_general','采用技术','产品采用的技术栈/框架','tpl_obj_inet_product','tpl_obj_inet_tech','多对多','active','USES_TECH','["采用","使用技术"]'::jsonb),
 ('tpl_rel_inet_hasfeature','tenant_jonex_demo','tpl_domain_internet','tpl_scenario_internet_general','具备功能','产品具备的功能特性','tpl_obj_inet_product','tpl_obj_inet_feature','一对多','active','HAS_FEATURE','["具备功能","包含"]'::jsonb),
 ('tpl_rel_inet_foundedby','tenant_jonex_demo','tpl_domain_internet','tpl_scenario_internet_general','创立','公司由人员创立','tpl_obj_inet_company','tpl_obj_inet_person','多对多','active','FOUNDED_BY','["创立","创办"]'::jsonb),
 ('tpl_rel_inet_worksat','tenant_jonex_demo','tpl_domain_internet','tpl_scenario_internet_general','任职','人员在某公司任职','tpl_obj_inet_person','tpl_obj_inet_company','多对一','active','WORKS_AT','["任职","就职"]'::jsonb),
 ('tpl_rel_inet_invests','tenant_jonex_demo','tpl_domain_internet','tpl_scenario_internet_general','投资','投资机构投资某公司','tpl_obj_inet_investor','tpl_obj_inet_company','多对多','active','INVESTS_IN','["投资","注资"]'::jsonb),
 ('tpl_rel_inet_competes','tenant_jonex_demo','tpl_domain_internet','tpl_scenario_internet_general','竞争','产品之间的竞争关系','tpl_obj_inet_product','tpl_obj_inet_product','多对多','active','COMPETES_WITH','["竞争","对标"]'::jsonb),
 ('tpl_rel_inet_targets','tenant_jonex_demo','tpl_domain_internet','tpl_scenario_internet_general','面向','产品面向的目标市场','tpl_obj_inet_product','tpl_obj_inet_market','多对一','active','TARGETS','["面向","定位"]'::jsonb),
 ('tpl_rel_inet_participates','tenant_jonex_demo','tpl_domain_internet','tpl_scenario_internet_general','参与','公司参与/组织事件','tpl_obj_inet_company','tpl_obj_inet_event','多对多','active','PARTICIPATES','["参与","出席"]'::jsonb)
ON CONFLICT (id) DO NOTHING;

-- ============================================================
-- 模板领域场景联调数据，金融行业
-- ============================================================

-- 1. 模板领域
INSERT INTO business_domain.template_domains (id, tenant_id, name, name_en, description, status, version, published_at, structure_hash)
VALUES ('tpl_domain_finance', 'tenant_jonex_demo', '金融行业', 'Financial Services', '金融机构风控、投顾与客户经营模板领域', 'active', 1,
        '2026-06-01T00:00:00+00'::timestamptz,
        '0476f5786a7e292d7a2aaaed06a06b6787b96a746da8818e3869cf2cd71f9777')
ON CONFLICT (id) DO NOTHING;

-- 2. 模板场景
INSERT INTO business_domain.template_scenarios (id, tenant_id, domain_id, name, description, config_json, version, published_at, structure_hash)
VALUES
    ('tpl_scenario_credit_risk', 'tenant_jonex_demo', 'tpl_domain_finance', '信贷风控', '基于企业财务数据的信贷风险评估场景', '{}'::jsonb, 1,
     '2026-06-01T00:00:00+00'::timestamptz, '0476f5786a7e292d7a2aaaed06a06b6787b96a746da8818e3869cf2cd71f9777'),
    ('tpl_scenario_robo_advisor', 'tenant_jonex_demo', 'tpl_domain_finance', '智能投顾', '基于市场行情的智能投资顾问场景', '{}'::jsonb, 1,
     '2026-06-01T00:00:00+00'::timestamptz, '6140449c1d747a2622f6c4d8dbc1542c07c26a250a7b9735c670dbbfd0ca62c1')
ON CONFLICT (id) DO NOTHING;

-- 3. 模板对象
INSERT INTO business_domain.template_objects (id, tenant_id, domain_id, scenario_id, name, description, status, ontology_code, aliases)
VALUES
    ('tpl_object_enterprise_customer', 'tenant_jonex_demo', 'tpl_domain_finance', 'tpl_scenario_credit_risk', '企业客户', '贷款申请企业主体信息', 'active', 'enterprise_customer', '["贷款申请企业","客户主体"]'::jsonb),
    ('tpl_object_financial_statement', 'tenant_jonex_demo', 'tpl_domain_finance', 'tpl_scenario_credit_risk', '财务报表', '企业财务报表数据', 'active', 'financial_statement', '["财报","财务报告"]'::jsonb),
    ('tpl_object_guarantee_company', 'tenant_jonex_demo', 'tpl_domain_finance', 'tpl_scenario_credit_risk', '担保企业', '为贷款申请提供担保的企业主体', 'active', 'guarantee_company', '["担保方","保证企业"]'::jsonb)
ON CONFLICT (id) DO NOTHING;

-- 4. 模板属性
INSERT INTO business_domain.template_attributes
    (id, tenant_id, template_object_id, attr_name, description, attr_type, is_primary_key, constraints_json, sort_order, ontology_code, is_required)
VALUES
    -- 企业客户
    ('tpl_attr_enterprise_code', 'tenant_jonex_demo', 'tpl_object_enterprise_customer', '统一社会信用代码', '企业唯一身份识别代码', '字符串', 1, '{}'::jsonb, 1, 'unified_social_credit_code', 1),
    ('tpl_attr_enterprise_name', 'tenant_jonex_demo', 'tpl_object_enterprise_customer', '企业名称', '工商登记企业名称', '字符串', 0, '{}'::jsonb, 2, 'enterprise_name', 1),
    ('tpl_attr_enterprise_capital', 'tenant_jonex_demo', 'tpl_object_enterprise_customer', '注册资本', '企业注册资本金额', '数值', 0, '{}'::jsonb, 3, 'registered_capital', 0),
    ('tpl_attr_enterprise_date', 'tenant_jonex_demo', 'tpl_object_enterprise_customer', '成立日期', '企业成立日期', '日期', 0, '{}'::jsonb, 4, 'establishment_date', 0),
    -- 财务报表
    ('tpl_attr_statement_id', 'tenant_jonex_demo', 'tpl_object_financial_statement', '报表ID', '财务报表唯一编号', '字符串', 1, '{}'::jsonb, 1, 'statement_id', 1),
    ('tpl_attr_statement_type', 'tenant_jonex_demo', 'tpl_object_financial_statement', '报表类型', '资产负债表、利润表等', '枚举', 0, '{}'::jsonb, 2, 'statement_type', 0),
    ('tpl_attr_statement_period', 'tenant_jonex_demo', 'tpl_object_financial_statement', '报告期', '财务报告所属期间', '日期', 0, '{}'::jsonb, 3, 'reporting_period', 0),
    ('tpl_attr_statement_revenue', 'tenant_jonex_demo', 'tpl_object_financial_statement', '营业收入', '报告期内营业收入', '数值', 0, '{}'::jsonb, 4, 'operating_revenue', 0),
    ('tpl_attr_statement_profit', 'tenant_jonex_demo', 'tpl_object_financial_statement', '净利润', '报告期内净利润', '数值', 0, '{}'::jsonb, 5, 'net_profit', 0),
    -- 担保企业
    ('tpl_attr_guarantee_code', 'tenant_jonex_demo', 'tpl_object_guarantee_company', '统一社会信用代码', '担保企业唯一身份识别代码', '字符串', 1, '{}'::jsonb, 1, 'guarantee_credit_code', 1),
    ('tpl_attr_guarantee_name', 'tenant_jonex_demo', 'tpl_object_guarantee_company', '企业名称', '担保企业工商登记名称', '字符串', 0, '{}'::jsonb, 2, 'guarantee_name', 1),
    ('tpl_attr_guarantee_amount', 'tenant_jonex_demo', 'tpl_object_guarantee_company', '担保金额', '为本笔贷款提供的担保金额（万元）', '数值', 0, '{}'::jsonb, 3, 'guarantee_amount', 0)
ON CONFLICT (id) DO NOTHING;

-- 5. 模板关系
INSERT INTO business_domain.template_relations
    (id, tenant_id, domain_id, scenario_id, name, description, source_object_id, target_object_id, relation_type, status, ontology_code, aliases)
VALUES
    ('tpl_relation_customer_statement', 'tenant_jonex_demo', 'tpl_domain_finance', 'tpl_scenario_credit_risk', '持有', '企业客户持有财务报表的关系', 'tpl_object_enterprise_customer', 'tpl_object_financial_statement', '一对多', 'active', 'owns_financial_statement', '["持有","拥有"]'::jsonb),
    ('tpl_relation_customer_guarantee', 'tenant_jonex_demo', 'tpl_domain_finance', 'tpl_scenario_credit_risk', '担保', '企业间担保关系', 'tpl_object_enterprise_customer', 'tpl_object_guarantee_company', '多对多', 'active', 'guaranteed_by', '["担保","保证"]'::jsonb)
ON CONFLICT (id) DO NOTHING;

-- ============================================================
-- 模板领域场景联调数据，医疗健康
-- ============================================================

-- 1. 模板领域
INSERT INTO business_domain.template_domains (id, tenant_id, name, name_en, description, status, version, published_at, structure_hash)
VALUES ('tpl_domain_medical', 'tenant_jonex_demo', '医疗健康', 'Healthcare', '医疗数据解析与健康管理模板领域', 'active', 1,
        '2026-06-01T00:00:00+00'::timestamptz,
        '6b518c74048be883c991406f509f03b1921f062a5ed0a7c1167e85e1886f38ba')
ON CONFLICT (id) DO NOTHING;

-- 2. 模板场景
INSERT INTO business_domain.template_scenarios (id, tenant_id, domain_id, name, description, config_json, version, published_at, structure_hash)
VALUES ('tpl_scenario_medical_record', 'tenant_jonex_demo', 'tpl_domain_medical', '病历智能解析', '电子病历文本的结构化解析场景', '{}'::jsonb, 1,
        '2026-06-01T00:00:00+00'::timestamptz,
        '6b518c74048be883c991406f509f03b1921f062a5ed0a7c1167e85e1886f38ba')
ON CONFLICT (id) DO NOTHING;

-- 3. 模板对象
INSERT INTO business_domain.template_objects (id, tenant_id, domain_id, scenario_id, name, description, status, ontology_code, aliases)
VALUES
    ('tpl_object_patient_record', 'tenant_jonex_demo', 'tpl_domain_medical', 'tpl_scenario_medical_record', '患者病历', '患者电子病历信息', 'active', 'patient_record', '["病历","患者档案"]'::jsonb),
    ('tpl_object_visit_record', 'tenant_jonex_demo', 'tpl_domain_medical', 'tpl_scenario_medical_record', '就诊记录', '患者单次就诊记录', 'active', 'visit_record', '["就诊","门诊记录"]'::jsonb),
    ('tpl_object_diagnosis_result', 'tenant_jonex_demo', 'tpl_domain_medical', 'tpl_scenario_medical_record', '诊断结果', '医生给出的诊断结论', 'active', 'diagnosis_result', '["诊断","诊断结论"]'::jsonb)
ON CONFLICT (id) DO NOTHING;

-- 4. 模板属性
INSERT INTO business_domain.template_attributes
    (id, tenant_id, template_object_id, attr_name, description, attr_type, is_primary_key, constraints_json, sort_order, ontology_code, is_required)
VALUES
    -- 患者病历
    ('tpl_attr_record_no', 'tenant_jonex_demo', 'tpl_object_patient_record', '病历号', '病历唯一编号', '字符串', 1, '{}'::jsonb, 1, 'record_no', 1),
    ('tpl_attr_patient_name', 'tenant_jonex_demo', 'tpl_object_patient_record', '患者姓名', '患者姓名', '字符串', 0, '{}'::jsonb, 2, 'patient_name', 1),
    ('tpl_attr_patient_gender', 'tenant_jonex_demo', 'tpl_object_patient_record', '性别', '患者性别', '枚举', 0, '{}'::jsonb, 3, 'gender', 1),
    ('tpl_attr_patient_age', 'tenant_jonex_demo', 'tpl_object_patient_record', '年龄', '患者年龄', '数值', 0, '{}'::jsonb, 4, 'age', 0),
    ('tpl_attr_diagnosis_text', 'tenant_jonex_demo', 'tpl_object_patient_record', '诊断结果', '医生给出的诊断结论', '文本', 0, '{}'::jsonb, 5, 'diagnosis_text', 0),
    ('tpl_attr_visit_date', 'tenant_jonex_demo', 'tpl_object_patient_record', '就诊日期', '本次就诊日期', '日期', 0, '{}'::jsonb, 6, 'visit_date', 0),
    -- 就诊记录
    ('tpl_attr_visit_no', 'tenant_jonex_demo', 'tpl_object_visit_record', '就诊编号', '单次就诊唯一编号', '字符串', 1, '{}'::jsonb, 1, 'visit_no', 1),
    ('tpl_attr_visit_dept', 'tenant_jonex_demo', 'tpl_object_visit_record', '就诊科室', '就诊科室名称', '字符串', 0, '{}'::jsonb, 2, 'department', 0),
    ('tpl_attr_visit_symptom', 'tenant_jonex_demo', 'tpl_object_visit_record', '主诉症状', '患者主诉症状描述', '文本', 0, '{}'::jsonb, 3, 'symptoms', 0),
    -- 诊断结果
    ('tpl_attr_diag_code', 'tenant_jonex_demo', 'tpl_object_diagnosis_result', '诊断编码', 'ICD 疾病编码', '字符串', 1, '{}'::jsonb, 1, 'diagnosis_code', 1),
    ('tpl_attr_diag_name', 'tenant_jonex_demo', 'tpl_object_diagnosis_result', '诊断名称', '疾病诊断名称', '字符串', 0, '{}'::jsonb, 2, 'diagnosis_name', 1),
    ('tpl_attr_diag_conclusion', 'tenant_jonex_demo', 'tpl_object_diagnosis_result', '诊断结论', '医生最终诊断意见和建议', '文本', 0, '{}'::jsonb, 3, 'diagnosis_conclusion', 0)
ON CONFLICT (id) DO NOTHING;

-- 5. 模板关系
INSERT INTO business_domain.template_relations
    (id, tenant_id, domain_id, scenario_id, name, description, source_object_id, target_object_id, relation_type, status, ontology_code, aliases)
VALUES
    ('tpl_relation_record_visit', 'tenant_jonex_demo', 'tpl_domain_medical', 'tpl_scenario_medical_record', '就诊', '患者病历与就诊记录的关系', 'tpl_object_patient_record', 'tpl_object_visit_record', '一对多', 'active', 'has_visit_record', '["就诊","就医"]'::jsonb),
    ('tpl_relation_visit_diagnosis', 'tenant_jonex_demo', 'tpl_domain_medical', 'tpl_scenario_medical_record', '诊断', '就诊记录与诊断结果的关联', 'tpl_object_visit_record', 'tpl_object_diagnosis_result', '一对一', 'active', 'resulted_in_diagnosis', '["诊断","得出诊断"]'::jsonb)
ON CONFLICT (id) DO NOTHING;

-- ============================================================
-- 模板领域场景联调数据，制造业
-- ============================================================

-- 1. 模板领域
INSERT INTO business_domain.template_domains (id, tenant_id, name, name_en, description, status, version, published_at, structure_hash)
VALUES ('tpl_domain_manufacturing', 'tenant_jonex_demo', '制造业', 'Manufacturing', '生产制造、质检与设备运维模板领域', 'active', 1,
        '2026-06-01T00:00:00+00'::timestamptz,
        'cb568b75836751b97660df5f6ff1f07c50ea57dcc428f12ef438ffcba8e02456')
ON CONFLICT (id) DO NOTHING;

-- 2. 模板场景
INSERT INTO business_domain.template_scenarios (id, tenant_id, domain_id, name, description, config_json, version, published_at, structure_hash)
VALUES ('tpl_scenario_quality_inspection', 'tenant_jonex_demo', 'tpl_domain_manufacturing', '生产质检分析', '生产环节质量检测数据分析场景', '{}'::jsonb, 1,
        '2026-06-01T00:00:00+00'::timestamptz,
        'cb568b75836751b97660df5f6ff1f07c50ea57dcc428f12ef438ffcba8e02456')
ON CONFLICT (id) DO NOTHING;

-- 3. 模板对象
INSERT INTO business_domain.template_objects (id, tenant_id, domain_id, scenario_id, name, description, status, ontology_code, aliases)
VALUES
    ('tpl_obj_mfg_product', 'tenant_jonex_demo', 'tpl_domain_manufacturing', 'tpl_scenario_quality_inspection', '产品', '生产制造的产品/零部件', 'active', 'ManufacturedProduct', '["产品","零部件","制成品"]'::jsonb),
    ('tpl_obj_mfg_line', 'tenant_jonex_demo', 'tpl_domain_manufacturing', 'tpl_scenario_quality_inspection', '生产线', '产品生产流水线', 'active', 'ProductionLine', '["生产线","流水线","产线"]'::jsonb),
    ('tpl_obj_mfg_defect', 'tenant_jonex_demo', 'tpl_domain_manufacturing', 'tpl_scenario_quality_inspection', '质量缺陷', '质检过程中发现的缺陷记录', 'active', 'QualityDefect', '["缺陷","质量问题","不合格项"]'::jsonb),
    ('tpl_obj_mfg_inspection', 'tenant_jonex_demo', 'tpl_domain_manufacturing', 'tpl_scenario_quality_inspection', '质检报告', '产品质量检测报告', 'active', 'InspectionReport', '["质检报告","检测报告","检验单"]'::jsonb)
ON CONFLICT (id) DO NOTHING;

-- 4. 模板属性
INSERT INTO business_domain.template_attributes
    (id, tenant_id, template_object_id, attr_name, description, attr_type, is_primary_key, constraints_json, sort_order, ontology_code, is_required)
VALUES
    -- 产品
    ('tpl_attr_mfg_prod_code', 'tenant_jonex_demo', 'tpl_obj_mfg_product', '产品编码', '产品唯一编码/SKU', '字符串', 1, '{}'::jsonb, 1, 'product_code', 1),
    ('tpl_attr_mfg_prod_name', 'tenant_jonex_demo', 'tpl_obj_mfg_product', '产品名称', '产品名称/型号', '字符串', 0, '{}'::jsonb, 2, 'product_name', 1),
    ('tpl_attr_mfg_prod_category', 'tenant_jonex_demo', 'tpl_obj_mfg_product', '产品类别', '产品分类', '枚举', 0, '{}'::jsonb, 3, 'product_category', 0),
    ('tpl_attr_mfg_prod_spec', 'tenant_jonex_demo', 'tpl_obj_mfg_product', '规格型号', '产品规格和技术参数', '文本', 0, '{}'::jsonb, 4, 'specification', 0),
    -- 生产线
    ('tpl_attr_mfg_line_code', 'tenant_jonex_demo', 'tpl_obj_mfg_line', '产线编码', '生产线唯一编号', '字符串', 1, '{}'::jsonb, 1, 'line_code', 1),
    ('tpl_attr_mfg_line_name', 'tenant_jonex_demo', 'tpl_obj_mfg_line', '产线名称', '生产线名称', '字符串', 0, '{}'::jsonb, 2, 'line_name', 1),
    ('tpl_attr_mfg_line_status', 'tenant_jonex_demo', 'tpl_obj_mfg_line', '运行状态', '产线当前运行状态', '枚举', 0, '{}'::jsonb, 3, 'line_status', 0),
    -- 质量缺陷
    ('tpl_attr_mfg_defect_code', 'tenant_jonex_demo', 'tpl_obj_mfg_defect', '缺陷编号', '缺陷记录唯一编号', '字符串', 1, '{}'::jsonb, 1, 'defect_code', 1),
    ('tpl_attr_mfg_defect_type', 'tenant_jonex_demo', 'tpl_obj_mfg_defect', '缺陷类型', '缺陷分类', '枚举', 0, '{}'::jsonb, 2, 'defect_type', 0),
    ('tpl_attr_mfg_defect_severity', 'tenant_jonex_demo', 'tpl_obj_mfg_defect', '严重等级', '缺陷严重程度', '枚举', 0, '{}'::jsonb, 3, 'severity_level', 0),
    ('tpl_attr_mfg_defect_desc', 'tenant_jonex_demo', 'tpl_obj_mfg_defect', '缺陷描述', '缺陷现象和原因描述', '文本', 0, '{}'::jsonb, 4, 'defect_description', 0),
    -- 质检报告
    ('tpl_attr_mfg_insp_no', 'tenant_jonex_demo', 'tpl_obj_mfg_inspection', '报告编号', '质检报告唯一编号', '字符串', 1, '{}'::jsonb, 1, 'report_no', 1),
    ('tpl_attr_mfg_insp_date', 'tenant_jonex_demo', 'tpl_obj_mfg_inspection', '检验日期', '质量检测日期', '日期', 0, '{}'::jsonb, 2, 'inspection_date', 0),
    ('tpl_attr_mfg_insp_result', 'tenant_jonex_demo', 'tpl_obj_mfg_inspection', '检验结果', '合格/不合格/让步接收', '枚举', 0, '{}'::jsonb, 3, 'inspection_result', 0),
    ('tpl_attr_mfg_insp_conclusion', 'tenant_jonex_demo', 'tpl_obj_mfg_inspection', '检验结论', '质检综合结论和建议', '文本', 0, '{}'::jsonb, 4, 'inspection_conclusion', 0)
ON CONFLICT (id) DO NOTHING;

-- 5. 模板关系
INSERT INTO business_domain.template_relations
    (id, tenant_id, domain_id, scenario_id, name, description, source_object_id, target_object_id, relation_type, status, ontology_code, aliases)
VALUES
    ('tpl_rel_mfg_prod_line', 'tenant_jonex_demo', 'tpl_domain_manufacturing', 'tpl_scenario_quality_inspection', '生产于', '产品在某生产线生产', 'tpl_obj_mfg_product', 'tpl_obj_mfg_line', '多对一', 'active', 'PRODUCED_ON', '["生产于","产自"]'::jsonb),
    ('tpl_rel_mfg_prod_defect', 'tenant_jonex_demo', 'tpl_domain_manufacturing', 'tpl_scenario_quality_inspection', '存在缺陷', '产品存在的质量缺陷', 'tpl_obj_mfg_product', 'tpl_obj_mfg_defect', '一对多', 'active', 'HAS_DEFECT', '["存在缺陷","发现"]'::jsonb),
    ('tpl_rel_mfg_insp_prod', 'tenant_jonex_demo', 'tpl_domain_manufacturing', 'tpl_scenario_quality_inspection', '检测', '质检报告对产品的检测', 'tpl_obj_mfg_inspection', 'tpl_obj_mfg_product', '多对一', 'active', 'INSPECTS', '["检测","检验"]'::jsonb),
    ('tpl_rel_mfg_insp_defect', 'tenant_jonex_demo', 'tpl_domain_manufacturing', 'tpl_scenario_quality_inspection', '记录缺陷', '质检报告中记录的缺陷', 'tpl_obj_mfg_inspection', 'tpl_obj_mfg_defect', '一对多', 'active', 'RECORDS_DEFECT', '["记录缺陷","发现"]'::jsonb)
ON CONFLICT (id) DO NOTHING;

-- ============================================================
-- 领域空间种子数据
-- ============================================================
INSERT INTO knowledge_base.spaces (id, tenant_id, name, description, status, knowledge_base_count, service_count, owner_id) VALUES
    ('space_demo_test', 'tenant_jonex_demo', '默认空间', '默认空间', 'active', 0, 0, '1')
ON CONFLICT (id) DO NOTHING;

-- 默认空间成员种子（演示空间权限：owner 不落 space_permissions，见设计 §2）
INSERT INTO knowledge_base.space_permissions (id, tenant_id, space_id, user_id, role, created_at, updated_at)
SELECT 'spp_demo_editor', u.tenant_id, 'space_demo_test', CAST(u.id AS VARCHAR), 'manager', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
  FROM platform.users u
 WHERE u.tenant_id = 'tenant_jonex_demo' AND u.username = 'editor_demo' AND u.is_deleted = 0
UNION ALL
SELECT 'spp_demo_viewer', u.tenant_id, 'space_demo_test', CAST(u.id AS VARCHAR), 'viewer', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
  FROM platform.users u
 WHERE u.tenant_id = 'tenant_jonex_demo' AND u.username = 'viewer_demo' AND u.is_deleted = 0
ON CONFLICT DO NOTHING;

-- ============================================================
-- 知识库种子数据
-- ============================================================
INSERT INTO knowledge_base.knowledge_info (id, tenant_id, space_id, name, description, data_source_types, document_count, status, owner_id) VALUES
    ('kb_demo_internet', 'tenant_jonex_demo', 'space_demo_test', '互联网知识库', '互联网本体抽取知识库', '["file"]'::jsonb, 0, 'synced', '1'),
    ('kb_demo_credit_risk', 'tenant_jonex_demo', 'space_demo_test', '信贷风控知识库', '金融行业信贷风控本体抽取', '["file"]'::jsonb, 0, 'synced', '1'),
    ('kb_demo_medical', 'tenant_jonex_demo', 'space_demo_test', '医疗知识库', '医疗病历智能解析本体抽取', '["file"]'::jsonb, 0, 'synced', '1')
ON CONFLICT (id) DO NOTHING;

-- 内置「文件上传」数据源（每个知识库默认拥有一条 file 类型数据源实例，
-- 与 knowledge_info.data_source_types 的 "file" 标签保持一致）
INSERT INTO knowledge_base.knowledge_data_sources
    (id, tenant_id, knowledge_base_id, access_method_id,access_type, name, config_json, sync_mode, status)
VALUES
    ('ds_demo_internet_file', 'tenant_jonex_demo', 'kb_demo_internet', 'dam_demo_file', 'file', '文件上传', '{}'::jsonb, 'manual', 'active'),
    ('ds_demo_credit_file', 'tenant_jonex_demo', 'kb_demo_credit_risk', 'dam_demo_file', 'file', '文件上传', '{}'::jsonb, 'manual', 'active'),
    ('ds_demo_medical_file', 'tenant_jonex_demo', 'kb_demo_medical', 'dam_demo_file', 'file', '文件上传', '{}'::jsonb, 'manual', 'active')
ON CONFLICT (id) DO NOTHING; 

-- ============================================================
-- 领域服务种子数据
-- ============================================================

INSERT INTO knowledge_base.services (id, tenant_id, space_id, name, description, domain_type, status, api_key_encrypted)
VALUES
    ('svc_demo_internet', 'tenant_jonex_demo', 'space_demo_test', '互联网领域服务', '互联网领域服务', '', 'active', 'sk-baseline-0123456789abcdef0123456789abcdef'),
    ('svc_demo_credit', 'tenant_jonex_demo', 'space_demo_test', '信贷风控领域服务', '信贷风控领域服务', '金融', 'active', 'sk-credit-0123456789abcdef0123456789abcdef'),
    ('svc_demo_medical', 'tenant_jonex_demo', 'space_demo_test', '医疗领域服务', '医疗病历解析领域服务', '医疗', 'active', 'sk-medical-0123456789abcdef0123456789abcdef')
ON CONFLICT (id) DO NOTHING;

-- 领域服务和知识库关联关系
INSERT INTO knowledge_base.service_knowledge_bases (id, tenant_id, service_id, kb_id)
VALUES
    ('skb_demo_internet', 'tenant_jonex_demo', 'svc_demo_internet', 'kb_demo_internet'),
    ('skb_demo_credit', 'tenant_jonex_demo', 'svc_demo_credit', 'kb_demo_credit_risk'),
    ('skb_demo_medical', 'tenant_jonex_demo', 'svc_demo_medical', 'kb_demo_medical')
ON CONFLICT (id) DO NOTHING;

-- [jonex] OpenKB 知识库种子数据
INSERT INTO knowledge_base.knowledge_info (id, tenant_id, space_id, name, description, data_source_types, document_count, status, owner_id, kb_type)
VALUES
    ('kb_demo_openkb', 'tenant_jonex_demo', 'space_demo_test', 'llm-wiki 知识库',
     '基于 llm-wiki 编译的知识库',
     '["file"]'::jsonb, 0, 'synced', 'user_demo_admin', 'openkb')
ON CONFLICT (id) DO NOTHING;

INSERT INTO knowledge_base.knowledge_data_sources
    (id, tenant_id, knowledge_base_id, access_method_id, access_type, name, config_json, sync_mode, status)
VALUES
    ('ds_openkb_default', 'tenant_jonex_demo', 'kb_demo_openkb', NULL, 'file',
     '默认文件上传',
     '{}'::jsonb, 'manual', 'active')
ON CONFLICT (id) DO NOTHING;

INSERT INTO knowledge_base.services (id, tenant_id, space_id, name, description, domain_type, status, api_key_encrypted)
VALUES
    ('svc_demo_openkb', 'tenant_jonex_demo', 'space_demo_test', 'llm-wiki 编译服务',
     'llm-wiki 知识库',
     'knowledge_compiler', 'active', '')
ON CONFLICT (id) DO NOTHING;

INSERT INTO knowledge_base.service_knowledge_bases (id, tenant_id, service_id, kb_id)
VALUES
    ('skb_demo_openkb', 'tenant_jonex_demo', 'svc_demo_openkb', 'kb_demo_openkb')
ON CONFLICT (id) DO NOTHING;

INSERT INTO knowledge_base.service_api_keys (id, tenant_id, service_id, key_prefix, key_encrypted, expires_at, is_active)
VALUES
    ('sak_openkb_main', 'tenant_jonex_demo', 'svc_demo_openkb', 'sk', 'sk-openkb-0123456789abcdef0123456789abcdef', '2027-12-31'::timestamp, 1)
ON CONFLICT (id) DO NOTHING;

-- 测试用 API Key
INSERT INTO knowledge_base.service_api_keys (id, tenant_id, service_id, key_prefix, key_encrypted, expires_at, is_active)
VALUES
    -- 互联网
    ('sak_internet_main', 'tenant_jonex_demo', 'svc_demo_internet', 'sk', 'sk-baseline-0123456789abcdef0123456789abcdef', '2027-12-31'::timestamp, 1),
    ('sak_internet_readonly', 'tenant_jonex_demo', 'svc_demo_internet', 'sk', 'sk-ro-a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6', '2026-12-31'::timestamp, 1),
    ('sak_internet_expired', 'tenant_jonex_demo', 'svc_demo_internet', 'sk', 'sk-expired-00000000000000000000000000000000', '2026-01-01'::timestamp, 0),
    -- 信贷风控
    ('sak_credit_main', 'tenant_jonex_demo', 'svc_demo_credit', 'sk', 'sk-credit-0123456789abcdef0123456789abcdef', '2027-12-31'::timestamp, 1),
    ('sak_credit_readonly', 'tenant_jonex_demo', 'svc_demo_credit', 'sk', 'sk-ro-credit-a1b2c3d4e5f6a7b8c9d0e1f2a3b4', '2026-12-31'::timestamp, 1),
    ('sak_credit_expired', 'tenant_jonex_demo', 'svc_demo_credit', 'sk', 'sk-expired-credit-0000000000000000000000', '2026-01-01'::timestamp, 0),
    -- 医疗
    ('sak_medical_main', 'tenant_jonex_demo', 'svc_demo_medical', 'sk', 'sk-medical-0123456789abcdef0123456789abcdef', '2027-12-31'::timestamp, 1),
    ('sak_medical_readonly', 'tenant_jonex_demo', 'svc_demo_medical', 'sk', 'sk-ro-medical-a1b2c3d4e5f6a7b8c9d0e1f2a', '2026-12-31'::timestamp, 1),
    ('sak_medical_expired', 'tenant_jonex_demo', 'svc_demo_medical', 'sk', 'sk-expired-medical-000000000000000000000', '2026-01-01'::timestamp, 0)
ON CONFLICT (id) DO NOTHING;


-- ============================================================
-- 本体模板编译 — 种子数据（发布模板、KB 绑定、预编译 schema）
-- ============================================================

-- 本体模板绑定（KB -> 模板场景）
INSERT INTO knowledge_base.ontology_template_bindings
    (tenant_id, knowledge_base_id, template_domain_id, template_scenario_id, source_type, status)
VALUES
    ('tenant_jonex_demo','kb_demo_internet','tpl_domain_internet','tpl_scenario_internet_general','business_template','active'),
    ('tenant_jonex_demo','kb_demo_credit_risk','tpl_domain_finance','tpl_scenario_credit_risk','business_template','active'),
    ('tenant_jonex_demo','kb_demo_medical','tpl_domain_medical','tpl_scenario_medical_record','business_template','active')
ON CONFLICT (tenant_id, knowledge_base_id) DO NOTHING;

-- 7. 预编译本体 schema（互联网通用场景）
INSERT INTO knowledge_base.ontology_compiled_schemas
    (tenant_id, knowledge_base_id, template_domain_id, template_scenario_id,
     source_type, source_version, source_hash, schema_version,
     entity_types, relation_types, constraints, disambiguation, prompt_schema,
     status, compiled_at)
VALUES (
    'tenant_jonex_demo', 'kb_demo_internet',
    'tpl_domain_internet', 'tpl_scenario_internet_general',
    'business_template', 1, 'b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0abc1de', 1,
    '[
        {"name":"Company","display_name":"公司","aliases":["企业","厂商","平台方","机构","Organization"],"source_object_id":"tpl_obj_inet_company","attributes":[
            {"name":"unified_social_credit_code","display_name":"统一社会信用代码","type":"string","required":true,"source_attribute_id":"tpl_attr_inet_co_code"},
            {"name":"company_name","display_name":"公司名称","type":"string","required":true,"source_attribute_id":"tpl_attr_inet_co_name"},
            {"name":"industry","display_name":"所属行业","type":"enum","required":false,"source_attribute_id":"tpl_attr_inet_co_industry"},
            {"name":"founded","display_name":"成立时间","type":"date","required":false,"source_attribute_id":"tpl_attr_inet_co_founded"},
            {"name":"registered_capital","display_name":"注册资本","type":"number","required":false,"source_attribute_id":"tpl_attr_inet_co_capital"},
            {"name":"company_summary","display_name":"公司简介","type":"text","required":false,"source_attribute_id":"tpl_attr_inet_co_summary"}
        ]},
        {"name":"Product","display_name":"产品","aliases":["App","应用","平台","Product"],"source_object_id":"tpl_obj_inet_product","attributes":[
            {"name":"product_name","display_name":"产品名称","type":"string","required":true,"source_attribute_id":"tpl_attr_inet_pd_name"},
            {"name":"category","display_name":"类别","type":"enum","required":false,"source_attribute_id":"tpl_attr_inet_pd_category"},
            {"name":"platform","display_name":"平台","type":"enum","required":false,"source_attribute_id":"tpl_attr_inet_pd_platform"},
            {"name":"user_scale","display_name":"用户规模","type":"number","required":false,"source_attribute_id":"tpl_attr_inet_pd_users"},
            {"name":"product_description","display_name":"产品描述","type":"text","required":false,"source_attribute_id":"tpl_attr_inet_pd_desc"}
        ]},
        {"name":"Technology","display_name":"技术","aliases":["框架","协议","算法","方法","Method"],"source_object_id":"tpl_obj_inet_tech","attributes":[
            {"name":"tech_name","display_name":"技术名称","type":"string","required":true,"source_attribute_id":"tpl_attr_inet_tech_name"},
            {"name":"tech_category","display_name":"技术分类","type":"enum","required":false,"source_attribute_id":"tpl_attr_inet_tech_category"},
            {"name":"tech_description","display_name":"技术描述","type":"text","required":false,"source_attribute_id":"tpl_attr_inet_tech_desc"}
        ]},
        {"name":"Feature","display_name":"功能特性","aliases":["功能","特性","模块","概念","Concept"],"source_object_id":"tpl_obj_inet_feature","attributes":[
            {"name":"feature_name","display_name":"特性名称","type":"string","required":true,"source_attribute_id":"tpl_attr_inet_feat_name"},
            {"name":"feature_description","display_name":"特性描述","type":"text","required":false,"source_attribute_id":"tpl_attr_inet_feat_desc"}
        ]},
        {"name":"Person","display_name":"人员","aliases":["创始人","高管","工程师","人","Person"],"source_object_id":"tpl_obj_inet_person","attributes":[
            {"name":"person_name","display_name":"姓名","type":"string","required":true,"source_attribute_id":"tpl_attr_inet_per_name"},
            {"name":"title","display_name":"职位","type":"string","required":false,"source_attribute_id":"tpl_attr_inet_per_title"},
            {"name":"affiliated_company","display_name":"所属公司","type":"string","required":false,"source_attribute_id":"tpl_attr_inet_per_company"},
            {"name":"biography","display_name":"简介","type":"text","required":false,"source_attribute_id":"tpl_attr_inet_per_bio"}
        ]},
        {"name":"Event","display_name":"事件","aliases":["发布会","融资","活动","Event"],"source_object_id":"tpl_obj_inet_event","attributes":[
            {"name":"event_name","display_name":"事件名称","type":"string","required":true,"source_attribute_id":"tpl_attr_inet_ev_name"},
            {"name":"event_type","display_name":"事件类型","type":"enum","required":false,"source_attribute_id":"tpl_attr_inet_ev_type"},
            {"name":"event_date","display_name":"日期","type":"date","required":false,"source_attribute_id":"tpl_attr_inet_ev_date"},
            {"name":"event_description","display_name":"事件描述","type":"text","required":false,"source_attribute_id":"tpl_attr_inet_ev_desc"}
        ]},
        {"name":"Market","display_name":"市场","aliases":["用户群体","赛道","领域"],"source_object_id":"tpl_obj_inet_market","attributes":[
            {"name":"market_name","display_name":"市场名称","type":"string","required":true,"source_attribute_id":"tpl_attr_inet_mkt_name"},
            {"name":"market_size","display_name":"市场规模","type":"number","required":false,"source_attribute_id":"tpl_attr_inet_mkt_size"},
            {"name":"market_description","display_name":"市场描述","type":"text","required":false,"source_attribute_id":"tpl_attr_inet_mkt_desc"}
        ]},
        {"name":"Investor","display_name":"投资机构","aliases":["资本","VC","投资方"],"source_object_id":"tpl_obj_inet_investor","attributes":[
            {"name":"investor_name","display_name":"机构名称","type":"string","required":true,"source_attribute_id":"tpl_attr_inet_inv_name"},
            {"name":"investor_type","display_name":"机构类型","type":"enum","required":false,"source_attribute_id":"tpl_attr_inet_inv_type"},
            {"name":"aum","display_name":"管理规模","type":"number","required":false,"source_attribute_id":"tpl_attr_inet_inv_aum"},
            {"name":"investor_description","display_name":"机构简介","type":"text","required":false,"source_attribute_id":"tpl_attr_inet_inv_desc"}
        ]}
    ]'::jsonb,
    '[
        {"name":"DEVELOPS","display_name":"开发","aliases":["开发","推出"],"source":"Company","target":"Product","source_relation_id":"tpl_rel_inet_develops","cardinality":"one_to_many"},
        {"name":"USES_TECH","display_name":"采用技术","aliases":["采用","使用技术"],"source":"Product","target":"Technology","source_relation_id":"tpl_rel_inet_usestech","cardinality":"many_to_many"},
        {"name":"HAS_FEATURE","display_name":"具备功能","aliases":["具备功能","包含"],"source":"Product","target":"Feature","source_relation_id":"tpl_rel_inet_hasfeature","cardinality":"one_to_many"},
        {"name":"FOUNDED_BY","display_name":"创立","aliases":["创立","创办"],"source":"Company","target":"Person","source_relation_id":"tpl_rel_inet_foundedby","cardinality":"many_to_many"},
        {"name":"WORKS_AT","display_name":"任职","aliases":["任职","就职"],"source":"Person","target":"Company","source_relation_id":"tpl_rel_inet_worksat","cardinality":"many_to_one"},
        {"name":"INVESTS_IN","display_name":"投资","aliases":["投资","注资"],"source":"Investor","target":"Company","source_relation_id":"tpl_rel_inet_invests","cardinality":"many_to_many"},
        {"name":"COMPETES_WITH","display_name":"竞争","aliases":["竞争","对标"],"source":"Product","target":"Product","source_relation_id":"tpl_rel_inet_competes","cardinality":"many_to_many"},
        {"name":"TARGETS","display_name":"面向","aliases":["面向","定位"],"source":"Product","target":"Market","source_relation_id":"tpl_rel_inet_targets","cardinality":"many_to_one"},
        {"name":"PARTICIPATES","display_name":"参与","aliases":["参与","出席"],"source":"Company","target":"Event","source_relation_id":"tpl_rel_inet_participates","cardinality":"many_to_many"}
    ]'::jsonb,
    '[{"type":"entity","severity":"warning"}]'::jsonb,
    '{"case_insensitive":true,"alias_merge":true}'::jsonb,
    '{
        "entity_types":[
            {"name":"Company","aliases":["企业","厂商","平台方","机构"],"attributes":[
                {"name":"unified_social_credit_code","type":"string","required":true},
                {"name":"company_name","type":"string","required":true},
                {"name":"industry","type":"enum","required":false},
                {"name":"founded","type":"date","required":false},
                {"name":"registered_capital","type":"number","required":false},
                {"name":"company_summary","type":"text","required":false}
            ]},
            {"name":"Product","aliases":["App","应用","平台"],"attributes":[
                {"name":"product_name","type":"string","required":true},
                {"name":"category","type":"enum","required":false},
                {"name":"platform","type":"enum","required":false},
                {"name":"user_scale","type":"number","required":false},
                {"name":"product_description","type":"text","required":false}
            ]},
            {"name":"Technology","aliases":["框架","协议","算法","方法"],"attributes":[
                {"name":"tech_name","type":"string","required":true},
                {"name":"tech_category","type":"enum","required":false},
                {"name":"tech_description","type":"text","required":false}
            ]},
            {"name":"Feature","aliases":["功能","特性","模块","概念"],"attributes":[
                {"name":"feature_name","type":"string","required":true},
                {"name":"feature_description","type":"text","required":false}
            ]},
            {"name":"Person","aliases":["创始人","高管","工程师","人"],"attributes":[
                {"name":"person_name","type":"string","required":true},
                {"name":"title","type":"string","required":false},
                {"name":"affiliated_company","type":"string","required":false},
                {"name":"biography","type":"text","required":false}
            ]},
            {"name":"Event","aliases":["发布会","融资","活动"],"attributes":[
                {"name":"event_name","type":"string","required":true},
                {"name":"event_type","type":"enum","required":false},
                {"name":"event_date","type":"date","required":false},
                {"name":"event_description","type":"text","required":false}
            ]},
            {"name":"Market","aliases":["用户群体","赛道","领域"],"attributes":[
                {"name":"market_name","type":"string","required":true},
                {"name":"market_size","type":"number","required":false},
                {"name":"market_description","type":"text","required":false}
            ]},
            {"name":"Investor","aliases":["资本","VC","投资方"],"attributes":[
                {"name":"investor_name","type":"string","required":true},
                {"name":"investor_type","type":"enum","required":false},
                {"name":"aum","type":"number","required":false},
                {"name":"investor_description","type":"text","required":false}
            ]}
        ],
        "relation_types":[
            {"name":"DEVELOPS","source":"Company","target":"Product"},
            {"name":"USES_TECH","source":"Product","target":"Technology"},
            {"name":"HAS_FEATURE","source":"Product","target":"Feature"},
            {"name":"FOUNDED_BY","source":"Company","target":"Person"},
            {"name":"WORKS_AT","source":"Person","target":"Company"},
            {"name":"INVESTS_IN","source":"Investor","target":"Company"},
            {"name":"COMPETES_WITH","source":"Product","target":"Product"},
            {"name":"TARGETS","source":"Product","target":"Market"},
            {"name":"PARTICIPATES","source":"Company","target":"Event"}
        ]
    }'::jsonb,
    'active', '2026-06-09T00:00:00+00'::timestamptz
) ON CONFLICT (tenant_id, knowledge_base_id) DO NOTHING;

-- 8. 预编译本体 schema（信贷风控场景）
INSERT INTO knowledge_base.ontology_compiled_schemas
    (tenant_id, knowledge_base_id, template_domain_id, template_scenario_id,
     source_type, source_version, source_hash, schema_version,
     entity_types, relation_types, constraints, disambiguation, prompt_schema,
     status, compiled_at)
VALUES (
    'tenant_jonex_demo', 'kb_demo_credit_risk',
    'tpl_domain_finance', 'tpl_scenario_credit_risk',
    'business_template', 1, '0476f5786a7e292d7a2aaaed06a06b6787b96a746da8818e3869cf2cd71f9777', 1,
    '[
        {"name":"enterprise_customer","display_name":"企业客户","aliases":["贷款申请企业","客户主体"],"source_object_id":"tpl_object_enterprise_customer","attributes":[
            {"name":"unified_social_credit_code","display_name":"统一社会信用代码","type":"string","required":true,"source_attribute_id":"tpl_attr_enterprise_code"},
            {"name":"enterprise_name","display_name":"企业名称","type":"string","required":true,"source_attribute_id":"tpl_attr_enterprise_name"},
            {"name":"registered_capital","display_name":"注册资本","type":"number","required":false,"source_attribute_id":"tpl_attr_enterprise_capital"},
            {"name":"establishment_date","display_name":"成立日期","type":"date","required":false,"source_attribute_id":"tpl_attr_enterprise_date"}
        ]},
        {"name":"financial_statement","display_name":"财务报表","aliases":["财报","财务报告"],"source_object_id":"tpl_object_financial_statement","attributes":[
            {"name":"statement_id","display_name":"报表ID","type":"string","required":true,"source_attribute_id":"tpl_attr_statement_id"},
            {"name":"statement_type","display_name":"报表类型","type":"enum","required":false,"source_attribute_id":"tpl_attr_statement_type"},
            {"name":"reporting_period","display_name":"报告期","type":"date","required":false,"source_attribute_id":"tpl_attr_statement_period"},
            {"name":"operating_revenue","display_name":"营业收入","type":"number","required":false,"source_attribute_id":"tpl_attr_statement_revenue"},
            {"name":"net_profit","display_name":"净利润","type":"number","required":false,"source_attribute_id":"tpl_attr_statement_profit"}
        ]},
        {"name":"guarantee_company","display_name":"担保企业","aliases":["担保方","保证企业"],"source_object_id":"tpl_object_guarantee_company","attributes":[
            {"name":"guarantee_credit_code","display_name":"统一社会信用代码","type":"string","required":true,"source_attribute_id":"tpl_attr_guarantee_code"},
            {"name":"guarantee_name","display_name":"企业名称","type":"string","required":true,"source_attribute_id":"tpl_attr_guarantee_name"},
            {"name":"guarantee_amount","display_name":"担保金额","type":"number","required":false,"source_attribute_id":"tpl_attr_guarantee_amount"}
        ]}
    ]'::jsonb,
    '[
        {"name":"owns_financial_statement","display_name":"持有","aliases":["持有","拥有"],"source":"enterprise_customer","target":"financial_statement","source_relation_id":"tpl_relation_customer_statement","cardinality":"one_to_many"},
        {"name":"guaranteed_by","display_name":"担保","aliases":["担保","保证"],"source":"enterprise_customer","target":"guarantee_company","source_relation_id":"tpl_relation_customer_guarantee","cardinality":"many_to_many"}
    ]'::jsonb,
    '[{"type":"entity","severity":"warning"}]'::jsonb,
    '{"case_insensitive":true,"alias_merge":true}'::jsonb,
    '{
        "entity_types":[
            {"name":"enterprise_customer","aliases":["贷款申请企业","客户主体"],"attributes":[
                {"name":"unified_social_credit_code","type":"string","required":true},
                {"name":"enterprise_name","type":"string","required":true},
                {"name":"registered_capital","type":"number","required":false},
                {"name":"establishment_date","type":"date","required":false}
            ]},
            {"name":"financial_statement","aliases":["财报","财务报告"],"attributes":[
                {"name":"statement_id","type":"string","required":true},
                {"name":"statement_type","type":"enum","required":false},
                {"name":"reporting_period","type":"date","required":false},
                {"name":"operating_revenue","type":"number","required":false},
                {"name":"net_profit","type":"number","required":false}
            ]},
            {"name":"guarantee_company","aliases":["担保方","保证企业"],"attributes":[
                {"name":"guarantee_credit_code","type":"string","required":true},
                {"name":"guarantee_name","type":"string","required":true},
                {"name":"guarantee_amount","type":"number","required":false}
            ]}
        ],
        "relation_types":[
            {"name":"owns_financial_statement","source":"enterprise_customer","target":"financial_statement"},
            {"name":"guaranteed_by","source":"enterprise_customer","target":"guarantee_company"}
        ]
    }'::jsonb,
    'active', '2026-06-09T00:00:00+00'::timestamptz
) ON CONFLICT (tenant_id, knowledge_base_id) DO NOTHING;

-- 9. 预编译本体 schema（病历智能解析场景）
INSERT INTO knowledge_base.ontology_compiled_schemas
    (tenant_id, knowledge_base_id, template_domain_id, template_scenario_id,
     source_type, source_version, source_hash, schema_version,
     entity_types, relation_types, constraints, disambiguation, prompt_schema,
     status, compiled_at)
VALUES (
    'tenant_jonex_demo', 'kb_demo_medical',
    'tpl_domain_medical', 'tpl_scenario_medical_record',
    'business_template', 1, '6b518c74048be883c991406f509f03b1921f062a5ed0a7c1167e85e1886f38ba', 1,
    '[
        {"name":"patient_record","display_name":"患者病历","aliases":["病历","患者档案"],"source_object_id":"tpl_object_patient_record","attributes":[
            {"name":"record_no","display_name":"病历号","type":"string","required":true,"source_attribute_id":"tpl_attr_record_no"},
            {"name":"patient_name","display_name":"患者姓名","type":"string","required":true,"source_attribute_id":"tpl_attr_patient_name"},
            {"name":"gender","display_name":"性别","type":"enum","required":true,"source_attribute_id":"tpl_attr_patient_gender"},
            {"name":"age","display_name":"年龄","type":"number","required":false,"source_attribute_id":"tpl_attr_patient_age"},
            {"name":"diagnosis_text","display_name":"诊断结果","type":"text","required":false,"source_attribute_id":"tpl_attr_diagnosis_text"},
            {"name":"visit_date","display_name":"就诊日期","type":"date","required":false,"source_attribute_id":"tpl_attr_visit_date"}
        ]},
        {"name":"visit_record","display_name":"就诊记录","aliases":["就诊","门诊记录"],"source_object_id":"tpl_object_visit_record","attributes":[
            {"name":"visit_no","display_name":"就诊编号","type":"string","required":true,"source_attribute_id":"tpl_attr_visit_no"},
            {"name":"department","display_name":"就诊科室","type":"string","required":false,"source_attribute_id":"tpl_attr_visit_dept"},
            {"name":"symptoms","display_name":"主诉症状","type":"text","required":false,"source_attribute_id":"tpl_attr_visit_symptom"}
        ]},
        {"name":"diagnosis_result","display_name":"诊断结果","aliases":["诊断","诊断结论"],"source_object_id":"tpl_object_diagnosis_result","attributes":[
            {"name":"diagnosis_code","display_name":"诊断编码","type":"string","required":true,"source_attribute_id":"tpl_attr_diag_code"},
            {"name":"diagnosis_name","display_name":"诊断名称","type":"string","required":true,"source_attribute_id":"tpl_attr_diag_name"},
            {"name":"diagnosis_conclusion","display_name":"诊断结论","type":"text","required":false,"source_attribute_id":"tpl_attr_diag_conclusion"}
        ]}
    ]'::jsonb,
    '[
        {"name":"has_visit_record","display_name":"就诊","aliases":["就诊","就医"],"source":"patient_record","target":"visit_record","source_relation_id":"tpl_relation_record_visit","cardinality":"one_to_many"},
        {"name":"resulted_in_diagnosis","display_name":"诊断","aliases":["诊断","得出诊断"],"source":"visit_record","target":"diagnosis_result","source_relation_id":"tpl_relation_visit_diagnosis","cardinality":"one_to_one"}
    ]'::jsonb,
    '[{"type":"entity","severity":"warning"}]'::jsonb,
    '{"case_insensitive":true,"alias_merge":true}'::jsonb,
    '{
        "entity_types":[
            {"name":"patient_record","aliases":["病历","患者档案"],"attributes":[
                {"name":"record_no","type":"string","required":true},
                {"name":"patient_name","type":"string","required":true},
                {"name":"gender","type":"enum","required":true},
                {"name":"age","type":"number","required":false},
                {"name":"diagnosis_text","type":"text","required":false},
                {"name":"visit_date","type":"date","required":false}
            ]},
            {"name":"visit_record","aliases":["就诊","门诊记录"],"attributes":[
                {"name":"visit_no","type":"string","required":true},
                {"name":"department","type":"string","required":false},
                {"name":"symptoms","type":"text","required":false}
            ]},
            {"name":"diagnosis_result","aliases":["诊断","诊断结论"],"attributes":[
                {"name":"diagnosis_code","type":"string","required":true},
                {"name":"diagnosis_name","type":"string","required":true},
                {"name":"diagnosis_conclusion","type":"text","required":false}
            ]}
        ],
        "relation_types":[
            {"name":"has_visit_record","source":"patient_record","target":"visit_record"},
            {"name":"resulted_in_diagnosis","source":"visit_record","target":"diagnosis_result"}
        ]
    }'::jsonb,
    'active', '2026-06-09T00:00:00+00'::timestamptz
) ON CONFLICT (tenant_id, knowledge_base_id) DO NOTHING;

-- ============================================================
-- 模板领域场景联调数据，硬件互联网（财报分析场景）
-- 基于小米集团 2025 年度报告（股份代号：1810/81810）逆向定义，
-- 覆盖上市公司、财务报告、业务分部、产品线、产品、财务/运营/研发
-- 指标、成本费用、股东回报、ESG 指标、关键人员、业务事件等核心本体。
-- ============================================================

-- 1. 模板领域
INSERT INTO business_domain.template_domains (id, tenant_id, name, name_en, description, status, version, published_at, structure_hash)
VALUES ('tpl_domain_hardware_internet', 'tenant_jonex_demo', '硬件互联网', 'Hardware & Internet', '硬件互联网上市公司财报分析与业务情报模板领域', 'active', 3,
        '2026-06-24T00:00:00+00'::timestamptz,
        'd5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6')
ON CONFLICT (id) DO NOTHING;

-- 2. 模板场景
INSERT INTO business_domain.template_scenarios (id, tenant_id, domain_id, name, description, config_json, version, published_at, structure_hash)
VALUES ('tpl_scenario_hw_inet_finance', 'tenant_jonex_demo', 'tpl_domain_hardware_internet', '财报分析', '硬件互联网上市公司年度/季度财报结构化抽取场景（基于小米2025年报）', '{}'::jsonb, 3,
        '2026-06-24T00:00:00+00'::timestamptz,
        'd5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6')
ON CONFLICT (id) DO NOTHING;

-- 3. 模板对象（19 个财报核心本体实体）
INSERT INTO business_domain.template_objects (id, tenant_id, domain_id, scenario_id, name, description, status, ontology_code, aliases)
VALUES
 ('tpl_obj_hwfin_company','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','上市公司','财报披露主体（硬件互联网上市公司）','active','listed_company','["上市公司","公司","集团","发行人","Issuer"]'::jsonb),
 ('tpl_obj_hwfin_report','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','财务报告','年度/季度/中期财务报告','active','financial_report','["财务报告","财报","年报","年度报告","季报","中期报告","Annual Report"]'::jsonb),
 ('tpl_obj_hwfin_segment','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','业务分部','公司业务分部（如手机×AIoT、智能电动汽车及AI等创新业务）','active','business_segment','["业务分部","分部","板块","业务线","Segment"]'::jsonb),
 ('tpl_obj_hwfin_line','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','产品线','分部下的产品线（如智能手机、IoT与生活消费产品、互联网服务、智能电动汽车）','active','product_line','["产品线","业务类别","Product Line"]'::jsonb),
 ('tpl_obj_hwfin_product','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','产品','具体产品/机型/应用/服务/车型','active','product','["产品","机型","应用","服务","车型","Product"]'::jsonb),
 ('tpl_obj_hwfin_fin_metric','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','财务指标','营收/利润/毛利等财务指标','active','financial_metric','["财务指标","财务数据","Financial Metric"]'::jsonb),
 ('tpl_obj_hwfin_op_metric','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','运营指标','出货量/月活/市占率/连接设备数等运营指标','active','operational_metric','["运营指标","经营数据","Operational Metric"]'::jsonb),
 ('tpl_obj_hwfin_rnd_metric','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','研发指标','研发投入/研发人员等研发指标','active','rnd_metric','["研发指标","研发投入","RnD Metric"]'::jsonb),
 ('tpl_obj_hwfin_cost','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','成本费用','销售成本/销售推广/行政/研发/所得税等成本费用','active','cost_expense','["成本费用","费用","支出","Cost Expense"]'::jsonb),
 ('tpl_obj_hwfin_shareholder','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','股东回报','股息/配售/回购等股东回报事项','active','shareholder_return','["股东回报","股息","分红","配售","回购","Shareholder Return"]'::jsonb),
 ('tpl_obj_hwfin_esg','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','ESG指标','环境/社会/管治相关指标','active','esg_metric','["ESG指标","ESG","环境社会管治","ESG Metric"]'::jsonb),
 ('tpl_obj_hwfin_person','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','关键人员','高管/创始人/核心管理人员/董事会成员','active','key_person','["关键人员","高管","管理层","董事","Key Person"]'::jsonb),
 ('tpl_obj_hwfin_event','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','业务事件','财报披露的业务事件/里程碑','active','business_event','["业务事件","事件","里程碑","Business Event"]'::jsonb),
 ('tpl_obj_hwfin_market','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','地区市场','收入/门店/用户分地区披露的市场（如中国大陆、境外、欧洲、印度）','active','geographic_market','["地区市场","市场","区域","地区","Region","Market"]'::jsonb),
 ('tpl_obj_hwfin_channel','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','销售渠道','线上/线下销售与服务渠道（如小米之家、直营店、授权店、经销商、小米商城）','active','sales_channel','["销售渠道","渠道","门店","零售网络","Channel"]'::jsonb),
 ('tpl_obj_hwfin_subsidiary','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','子公司','附属公司/集团实体（如小米印度、境外附属公司）','active','subsidiary','["子公司","附属公司","集团实体","Subsidiary"]'::jsonb),
 ('tpl_obj_hwfin_risk','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','风险因素','年报披露的风险因素（如竞争、舆情、地缘政治、气候、外汇风险）','active','risk_factor','["风险因素","风险","Risk Factor"]'::jsonb),
 ('tpl_obj_hwfin_legal','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','法律诉讼','诉讼/监管调查/或然负债事项（如小米印度调查与资产冻结）','active','legal_proceeding','["法律诉讼","诉讼","监管调查","或然负债","Legal Proceeding"]'::jsonb),
 ('tpl_obj_hwfin_esginit','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','可持续举措','ESG/可持续发展举措与行动（如物流低碳、绿电采购、以旧换新、碳减排）','active','sustainability_initiative','["可持续举措","ESG举措","可持续发展","Sustainability Initiative"]'::jsonb)
ON CONFLICT (id) DO NOTHING;

-- 4. 模板属性
INSERT INTO business_domain.template_attributes
    (id, tenant_id, template_object_id, attr_name, description, attr_type, is_primary_key, constraints_json, sort_order, ontology_code, is_required)
VALUES
    -- 上市公司 listed_company
    ('tpl_attr_hwfin_co_stock','tenant_jonex_demo','tpl_obj_hwfin_company','股票代码','交易所股票代码（如 HK01810）','字符串',1,'{}'::jsonb,1,'stock_code',1),
    ('tpl_attr_hwfin_co_name','tenant_jonex_demo','tpl_obj_hwfin_company','公司名称','公司全称','字符串',0,'{}'::jsonb,2,'company_name',1),
    ('tpl_attr_hwfin_co_exchange','tenant_jonex_demo','tpl_obj_hwfin_company','上市交易所','如港交所、上交所、深交所、纳斯达克','枚举',0,'{}'::jsonb,3,'exchange',0),
    ('tpl_attr_hwfin_co_industry','tenant_jonex_demo','tpl_obj_hwfin_company','所属行业','如硬件互联网、消费电子、智能出行','枚举',0,'{}'::jsonb,4,'industry',0),
    ('tpl_attr_hwfin_co_founded','tenant_jonex_demo','tpl_obj_hwfin_company','成立日期','公司成立日期','日期',0,'{}'::jsonb,5,'founded_date',0),
    ('tpl_attr_hwfin_co_chairman','tenant_jonex_demo','tpl_obj_hwfin_company','董事长','董事长姓名','字符串',0,'{}'::jsonb,6,'chairman',0),
    ('tpl_attr_hwfin_co_ceo','tenant_jonex_demo','tpl_obj_hwfin_company','首席执行官','CEO 姓名','字符串',0,'{}'::jsonb,7,'ceo',0),
    ('tpl_attr_hwfin_co_hq','tenant_jonex_demo','tpl_obj_hwfin_company','总部','总部所在地','字符串',0,'{}'::jsonb,8,'headquarters',0),
    ('tpl_attr_hwfin_co_desc','tenant_jonex_demo','tpl_obj_hwfin_company','公司简介','主营业务与核心竞争力概述','文本',0,'{}'::jsonb,9,'company_description',0),
    -- 财务报告 financial_report
    ('tpl_attr_hwfin_rpt_id','tenant_jonex_demo','tpl_obj_hwfin_report','报告ID','报告唯一标识','字符串',1,'{}'::jsonb,1,'report_id',1),
    ('tpl_attr_hwfin_rpt_type','tenant_jonex_demo','tpl_obj_hwfin_report','报告类型','annual/quarterly/interim','枚举',0,'{}'::jsonb,2,'report_type',0),
    ('tpl_attr_hwfin_rpt_year','tenant_jonex_demo','tpl_obj_hwfin_report','财年','财报所属财年','数值',0,'{}'::jsonb,3,'fiscal_year',1),
    ('tpl_attr_hwfin_rpt_period','tenant_jonex_demo','tpl_obj_hwfin_report','报告期','如 2025FY / 2025Q4','字符串',0,'{}'::jsonb,4,'reporting_period',0),
    ('tpl_attr_hwfin_rpt_release','tenant_jonex_demo','tpl_obj_hwfin_report','发布日期','财报发布日期','日期',0,'{}'::jsonb,5,'release_date',0),
    ('tpl_attr_hwfin_rpt_currency','tenant_jonex_demo','tpl_obj_hwfin_report','币种','如人民币、美元','字符串',0,'{}'::jsonb,6,'currency',0),
    ('tpl_attr_hwfin_rpt_revenue','tenant_jonex_demo','tpl_obj_hwfin_report','总营收','报告期总营收（万元）','数值',0,'{}'::jsonb,7,'total_revenue',0),
    ('tpl_attr_hwfin_rpt_rev_yoy','tenant_jonex_demo','tpl_obj_hwfin_report','营收同比','总营收同比增长率（%）','数值',0,'{}'::jsonb,8,'revenue_yoy',0),
    ('tpl_attr_hwfin_rpt_cogs','tenant_jonex_demo','tpl_obj_hwfin_report','销售成本','报告期销售成本（万元）','数值',0,'{}'::jsonb,9,'cost_of_sales',0),
    ('tpl_attr_hwfin_rpt_gp','tenant_jonex_demo','tpl_obj_hwfin_report','毛利','报告期毛利（万元）','数值',0,'{}'::jsonb,10,'gross_profit',0),
    ('tpl_attr_hwfin_rpt_gm','tenant_jonex_demo','tpl_obj_hwfin_report','毛利率','毛利率（%）','数值',0,'{}'::jsonb,11,'gross_margin',0),
    ('tpl_attr_hwfin_rpt_selling','tenant_jonex_demo','tpl_obj_hwfin_report','销售及推广开支','报告期销售及推广开支（万元）','数值',0,'{}'::jsonb,12,'selling_expense',0),
    ('tpl_attr_hwfin_rpt_admin','tenant_jonex_demo','tpl_obj_hwfin_report','行政开支','报告期行政开支（万元）','数值',0,'{}'::jsonb,13,'admin_expense',0),
    ('tpl_attr_hwfin_rpt_rnd','tenant_jonex_demo','tpl_obj_hwfin_report','研发开支','报告期研发开支（万元）','数值',0,'{}'::jsonb,14,'rnd_expense',0),
    ('tpl_attr_hwfin_rpt_op','tenant_jonex_demo','tpl_obj_hwfin_report','经营利润','报告期经营利润（万元）','数值',0,'{}'::jsonb,15,'operating_profit',0),
    ('tpl_attr_hwfin_rpt_fincome','tenant_jonex_demo','tpl_obj_hwfin_report','财务收入净额','报告期财务收入净额（万元）','数值',0,'{}'::jsonb,16,'finance_income_net',0),
    ('tpl_attr_hwfin_rpt_pbt','tenant_jonex_demo','tpl_obj_hwfin_report','除所得税前利润','报告期除所得税前利润（万元）','数值',0,'{}'::jsonb,17,'profit_before_tax',0),
    ('tpl_attr_hwfin_rpt_tax','tenant_jonex_demo','tpl_obj_hwfin_report','所得税费用','报告期所得税费用（万元）','数值',0,'{}'::jsonb,18,'income_tax_expense',0),
    ('tpl_attr_hwfin_rpt_np','tenant_jonex_demo','tpl_obj_hwfin_report','年度利润','报告期年度利润（万元）','数值',0,'{}'::jsonb,19,'net_profit',0),
    ('tpl_attr_hwfin_rpt_anp','tenant_jonex_demo','tpl_obj_hwfin_report','经调整净利润','经调整净利润（万元）','数值',0,'{}'::jsonb,20,'adjusted_net_profit',0),
    ('tpl_attr_hwfin_rpt_anp_yoy','tenant_jonex_demo','tpl_obj_hwfin_report','经调整净利润同比','经调整净利润同比增长率（%）','数值',0,'{}'::jsonb,21,'adjusted_net_profit_yoy',0),
    -- 业务分部 business_segment
    ('tpl_attr_hwfin_seg_code','tenant_jonex_demo','tpl_obj_hwfin_segment','分部代码','分部唯一标识','字符串',1,'{}'::jsonb,1,'segment_code',1),
    ('tpl_attr_hwfin_seg_name','tenant_jonex_demo','tpl_obj_hwfin_segment','分部名称','如手机×AIoT、智能电动汽车及AI等创新业务','字符串',0,'{}'::jsonb,2,'segment_name',1),
    ('tpl_attr_hwfin_seg_type','tenant_jonex_demo','tpl_obj_hwfin_segment','分部类型','如核心业务、创新业务','枚举',0,'{}'::jsonb,3,'segment_type',0),
    ('tpl_attr_hwfin_seg_rev','tenant_jonex_demo','tpl_obj_hwfin_segment','分部营收','分部营收（万元）','数值',0,'{}'::jsonb,4,'revenue',0),
    ('tpl_attr_hwfin_seg_rev_yoy','tenant_jonex_demo','tpl_obj_hwfin_segment','营收同比','分部营收同比增长率（%）','数值',0,'{}'::jsonb,5,'revenue_yoy',0),
    ('tpl_attr_hwfin_seg_ratio','tenant_jonex_demo','tpl_obj_hwfin_segment','营收占比','分部营收占总营收比例（%）','数值',0,'{}'::jsonb,6,'revenue_ratio',0),
    ('tpl_attr_hwfin_seg_cogs','tenant_jonex_demo','tpl_obj_hwfin_segment','分部销售成本','分部销售成本（万元）','数值',0,'{}'::jsonb,7,'cost_of_sales',0),
    ('tpl_attr_hwfin_seg_gp','tenant_jonex_demo','tpl_obj_hwfin_segment','分部毛利','分部毛利（万元）','数值',0,'{}'::jsonb,8,'gross_profit',0),
    ('tpl_attr_hwfin_seg_gm','tenant_jonex_demo','tpl_obj_hwfin_segment','分部毛利率','分部毛利率（%）','数值',0,'{}'::jsonb,9,'gross_margin',0),
    ('tpl_attr_hwfin_seg_op','tenant_jonex_demo','tpl_obj_hwfin_segment','分部经营收益','分部经营收益/(亏损)（万元）','数值',0,'{}'::jsonb,10,'operating_result',0),
    ('tpl_attr_hwfin_seg_anp','tenant_jonex_demo','tpl_obj_hwfin_segment','经调整净利润','分部经调整净利润（万元）','数值',0,'{}'::jsonb,11,'adjusted_net_profit',0),
    ('tpl_attr_hwfin_seg_anl','tenant_jonex_demo','tpl_obj_hwfin_segment','经调整净亏损','分部经调整净亏损（万元）','数值',0,'{}'::jsonb,12,'adjusted_net_loss',0),
    ('tpl_attr_hwfin_seg_desc','tenant_jonex_demo','tpl_obj_hwfin_segment','分部描述','分部业务范围与表现说明','文本',0,'{}'::jsonb,13,'segment_description',0),
    -- 产品线 product_line
    ('tpl_attr_hwfin_ln_code','tenant_jonex_demo','tpl_obj_hwfin_line','产品线代码','产品线唯一标识','字符串',1,'{}'::jsonb,1,'line_code',1),
    ('tpl_attr_hwfin_ln_name','tenant_jonex_demo','tpl_obj_hwfin_line','产品线名称','如智能手机、IoT与生活消费产品、互联网服务、智能电动汽车','字符串',0,'{}'::jsonb,2,'line_name',1),
    ('tpl_attr_hwfin_ln_cat','tenant_jonex_demo','tpl_obj_hwfin_line','类别','产品线类别','枚举',0,'{}'::jsonb,3,'category',0),
    ('tpl_attr_hwfin_ln_rev','tenant_jonex_demo','tpl_obj_hwfin_line','产品线营收','产品线营收（万元）','数值',0,'{}'::jsonb,4,'revenue',0),
    ('tpl_attr_hwfin_ln_rev_yoy','tenant_jonex_demo','tpl_obj_hwfin_line','营收同比','产品线营收同比增长率（%）','数值',0,'{}'::jsonb,5,'revenue_yoy',0),
    ('tpl_attr_hwfin_ln_ratio','tenant_jonex_demo','tpl_obj_hwfin_line','营收占比','产品线营收占总营收比例（%）','数值',0,'{}'::jsonb,6,'revenue_ratio',0),
    ('tpl_attr_hwfin_ln_cogs','tenant_jonex_demo','tpl_obj_hwfin_line','产品线销售成本','产品线销售成本（万元）','数值',0,'{}'::jsonb,7,'cost_of_sales',0),
    ('tpl_attr_hwfin_ln_gp','tenant_jonex_demo','tpl_obj_hwfin_line','产品线毛利','产品线毛利（万元）','数值',0,'{}'::jsonb,8,'gross_profit',0),
    ('tpl_attr_hwfin_ln_gm','tenant_jonex_demo','tpl_obj_hwfin_line','产品线毛利率','产品线毛利率（%）','数值',0,'{}'::jsonb,9,'gross_margin',0),
    ('tpl_attr_hwfin_ln_desc','tenant_jonex_demo','tpl_obj_hwfin_line','描述','产品线业务说明','文本',0,'{}'::jsonb,10,'description',0),
    -- 产品 product
    ('tpl_attr_hwfin_pd_code','tenant_jonex_demo','tpl_obj_hwfin_product','产品代码','产品唯一标识','字符串',1,'{}'::jsonb,1,'product_code',1),
    ('tpl_attr_hwfin_pd_name','tenant_jonex_demo','tpl_obj_hwfin_product','产品名称','如小米SU7、小米SU7 Ultra、小米YU7、米家APP','字符串',0,'{}'::jsonb,2,'product_name',1),
    ('tpl_attr_hwfin_pd_cat','tenant_jonex_demo','tpl_obj_hwfin_product','产品类别','如智能手机、电动汽车、家电、应用','枚举',0,'{}'::jsonb,3,'product_category',0),
    ('tpl_attr_hwfin_pd_launch','tenant_jonex_demo','tpl_obj_hwfin_product','上市日期','产品上市/发布日期','日期',0,'{}'::jsonb,4,'launch_date',0),
    ('tpl_attr_hwfin_pd_ship','tenant_jonex_demo','tpl_obj_hwfin_product','出货量','产品出货量/交付量','数值',0,'{}'::jsonb,5,'shipment_volume',0),
    ('tpl_attr_hwfin_pd_ship_yoy','tenant_jonex_demo','tpl_obj_hwfin_product','出货量同比','出货量同比增长率（%）','数值',0,'{}'::jsonb,6,'shipment_yoy',0),
    ('tpl_attr_hwfin_pd_asp','tenant_jonex_demo','tpl_obj_hwfin_product','平均售价','产品平均售价（元）','数值',0,'{}'::jsonb,7,'asp',0),
    ('tpl_attr_hwfin_pd_asp_yoy','tenant_jonex_demo','tpl_obj_hwfin_product','ASP同比','平均售价同比增长率（%）','数值',0,'{}'::jsonb,8,'asp_yoy',0),
    ('tpl_attr_hwfin_pd_gm','tenant_jonex_demo','tpl_obj_hwfin_product','毛利率','产品毛利率（%）','数值',0,'{}'::jsonb,9,'gross_margin',0),
    ('tpl_attr_hwfin_pd_share','tenant_jonex_demo','tpl_obj_hwfin_product','市占率','产品市场份额（%）','数值',0,'{}'::jsonb,10,'market_share',0),
    ('tpl_attr_hwfin_pd_desc','tenant_jonex_demo','tpl_obj_hwfin_product','产品描述','产品定位与核心卖点','文本',0,'{}'::jsonb,11,'product_description',0),
    -- 财务指标 financial_metric
    ('tpl_attr_hwfin_fm_code','tenant_jonex_demo','tpl_obj_hwfin_fin_metric','指标代码','指标唯一标识','字符串',1,'{}'::jsonb,1,'metric_code',1),
    ('tpl_attr_hwfin_fm_name','tenant_jonex_demo','tpl_obj_hwfin_fin_metric','指标名称','如总营收、毛利、经营利润','字符串',0,'{}'::jsonb,2,'metric_name',1),
    ('tpl_attr_hwfin_fm_val','tenant_jonex_demo','tpl_obj_hwfin_fin_metric','指标值','指标数值','数值',0,'{}'::jsonb,3,'metric_value',0),
    ('tpl_attr_hwfin_fm_unit','tenant_jonex_demo','tpl_obj_hwfin_fin_metric','单位','如万元、亿元、%','字符串',0,'{}'::jsonb,4,'metric_unit',0),
    ('tpl_attr_hwfin_fm_yoy','tenant_jonex_demo','tpl_obj_hwfin_fin_metric','同比','指标同比增长率（%）','数值',0,'{}'::jsonb,5,'metric_yoy',0),
    ('tpl_attr_hwfin_fm_period','tenant_jonex_demo','tpl_obj_hwfin_fin_metric','指标期间','如 2025FY / 2025Q4','字符串',0,'{}'::jsonb,6,'metric_period',0),
    ('tpl_attr_hwfin_fm_cat','tenant_jonex_demo','tpl_obj_hwfin_fin_metric','指标分类','如营收、利润、成本','枚举',0,'{}'::jsonb,7,'metric_category',0),
    -- 运营指标 operational_metric
    ('tpl_attr_hwfin_om_code','tenant_jonex_demo','tpl_obj_hwfin_op_metric','指标代码','指标唯一标识','字符串',1,'{}'::jsonb,1,'metric_code',1),
    ('tpl_attr_hwfin_om_name','tenant_jonex_demo','tpl_obj_hwfin_op_metric','指标名称','如月活、出货量、连接设备数','字符串',0,'{}'::jsonb,2,'metric_name',1),
    ('tpl_attr_hwfin_om_val','tenant_jonex_demo','tpl_obj_hwfin_op_metric','指标值','指标数值','数值',0,'{}'::jsonb,3,'metric_value',0),
    ('tpl_attr_hwfin_om_unit','tenant_jonex_demo','tpl_obj_hwfin_op_metric','单位','如万台、百万、亿、%','字符串',0,'{}'::jsonb,4,'metric_unit',0),
    ('tpl_attr_hwfin_om_yoy','tenant_jonex_demo','tpl_obj_hwfin_op_metric','同比','指标同比增长率（%）','数值',0,'{}'::jsonb,5,'metric_yoy',0),
    ('tpl_attr_hwfin_om_cat','tenant_jonex_demo','tpl_obj_hwfin_op_metric','指标分类','如 user/shipment/market_share/iot_device','枚举',0,'{}'::jsonb,6,'metric_category',0),
    ('tpl_attr_hwfin_om_region','tenant_jonex_demo','tpl_obj_hwfin_op_metric','统计区域','如全球、中国大陆、拉美、东南亚','字符串',0,'{}'::jsonb,7,'region',0),
    ('tpl_attr_hwfin_om_desc','tenant_jonex_demo','tpl_obj_hwfin_op_metric','指标描述','指标口径与统计说明','文本',0,'{}'::jsonb,8,'metric_description',0),
    -- 研发指标 rnd_metric
    ('tpl_attr_hwfin_rm_code','tenant_jonex_demo','tpl_obj_hwfin_rnd_metric','指标代码','指标唯一标识','字符串',1,'{}'::jsonb,1,'metric_code',1),
    ('tpl_attr_hwfin_rm_name','tenant_jonex_demo','tpl_obj_hwfin_rnd_metric','指标名称','如研发投入、研发人员数、累计研发','字符串',0,'{}'::jsonb,2,'metric_name',1),
    ('tpl_attr_hwfin_rm_val','tenant_jonex_demo','tpl_obj_hwfin_rnd_metric','指标值','指标数值','数值',0,'{}'::jsonb,3,'metric_value',0),
    ('tpl_attr_hwfin_rm_unit','tenant_jonex_demo','tpl_obj_hwfin_rnd_metric','单位','如万元、人','字符串',0,'{}'::jsonb,4,'metric_unit',0),
    ('tpl_attr_hwfin_rm_yoy','tenant_jonex_demo','tpl_obj_hwfin_rnd_metric','同比','指标同比增长率（%）','数值',0,'{}'::jsonb,5,'metric_yoy',0),
    ('tpl_attr_hwfin_rm_head','tenant_jonex_demo','tpl_obj_hwfin_rnd_metric','研发人员数','研发人员数量','数值',0,'{}'::jsonb,6,'rnd_headcount',0),
    ('tpl_attr_hwfin_rm_cum','tenant_jonex_demo','tpl_obj_hwfin_rnd_metric','累计研发投入','过去若干年累计研发投入（万元）','数值',0,'{}'::jsonb,7,'cumulative_rnd',0),
    -- 成本费用 cost_expense
    ('tpl_attr_hwfin_ce_code','tenant_jonex_demo','tpl_obj_hwfin_cost','费用代码','费用项唯一标识','字符串',1,'{}'::jsonb,1,'expense_code',1),
    ('tpl_attr_hwfin_ce_name','tenant_jonex_demo','tpl_obj_hwfin_cost','费用名称','如销售成本、销售推广、行政、研发、所得税','字符串',0,'{}'::jsonb,2,'expense_name',1),
    ('tpl_attr_hwfin_ce_cat','tenant_jonex_demo','tpl_obj_hwfin_cost','费用分类','如 cost/selling/admin/rnd/tax','枚举',0,'{}'::jsonb,3,'expense_category',0),
    ('tpl_attr_hwfin_ce_val','tenant_jonex_demo','tpl_obj_hwfin_cost','费用金额','费用金额（万元）','数值',0,'{}'::jsonb,4,'expense_value',0),
    ('tpl_attr_hwfin_ce_yoy','tenant_jonex_demo','tpl_obj_hwfin_cost','同比','费用同比增长率（%）','数值',0,'{}'::jsonb,5,'expense_yoy',0),
    ('tpl_attr_hwfin_ce_ratio','tenant_jonex_demo','tpl_obj_hwfin_cost','占营收比','费用占总营收比例（%）','数值',0,'{}'::jsonb,6,'expense_ratio',0),
    ('tpl_attr_hwfin_ce_period','tenant_jonex_demo','tpl_obj_hwfin_cost','费用期间','如 2025FY / 2025Q4','字符串',0,'{}'::jsonb,7,'expense_period',0),
    -- 股东回报 shareholder_return
    ('tpl_attr_hwfin_sr_code','tenant_jonex_demo','tpl_obj_hwfin_shareholder','事项代码','事项唯一标识','字符串',1,'{}'::jsonb,1,'return_code',1),
    ('tpl_attr_hwfin_sr_type','tenant_jonex_demo','tpl_obj_hwfin_shareholder','事项类型','如 dividend/placement/buyback','枚举',0,'{}'::jsonb,2,'return_type',1),
    ('tpl_attr_hwfin_sr_desc','tenant_jonex_demo','tpl_obj_hwfin_shareholder','事项描述','如末期股息、2025年配售及认购','字符串',0,'{}'::jsonb,3,'return_description',0),
    ('tpl_attr_hwfin_sr_amount','tenant_jonex_demo','tpl_obj_hwfin_shareholder','金额/规模','事项金额或规模（万元/万股）','数值',0,'{}'::jsonb,4,'return_amount',0),
    ('tpl_attr_hwfin_sr_price','tenant_jonex_demo','tpl_obj_hwfin_shareholder','每股价格','每股价格（港元/元）','数值',0,'{}'::jsonb,5,'per_share_price',0),
    ('tpl_attr_hwfin_sr_date','tenant_jonex_demo','tpl_obj_hwfin_shareholder','事项日期','事项发生/完成日期','日期',0,'{}'::jsonb,6,'return_date',0),
    ('tpl_attr_hwfin_sr_status','tenant_jonex_demo','tpl_obj_hwfin_shareholder','事项状态','如已宣派/不宣派/已完成','枚举',0,'{}'::jsonb,7,'return_status',0),
    -- ESG指标 esg_metric
    ('tpl_attr_hwfin_eg_code','tenant_jonex_demo','tpl_obj_hwfin_esg','指标代码','指标唯一标识','字符串',1,'{}'::jsonb,1,'metric_code',1),
    ('tpl_attr_hwfin_eg_name','tenant_jonex_demo','tpl_obj_hwfin_esg','指标名称','如CDP评级、碳排放、光伏用电','字符串',0,'{}'::jsonb,2,'metric_name',1),
    ('tpl_attr_hwfin_eg_cat','tenant_jonex_demo','tpl_obj_hwfin_esg','指标分类','如 environment/social/governance','枚举',0,'{}'::jsonb,3,'metric_category',0),
    ('tpl_attr_hwfin_eg_val','tenant_jonex_demo','tpl_obj_hwfin_esg','指标值','指标数值','数值',0,'{}'::jsonb,4,'metric_value',0),
    ('tpl_attr_hwfin_eg_unit','tenant_jonex_demo','tpl_obj_hwfin_esg','单位','如吨CO2e、万度、级','字符串',0,'{}'::jsonb,5,'metric_unit',0),
    ('tpl_attr_hwfin_eg_yoy','tenant_jonex_demo','tpl_obj_hwfin_esg','同比','指标同比变化（%）','数值',0,'{}'::jsonb,6,'metric_yoy',0),
    ('tpl_attr_hwfin_eg_desc','tenant_jonex_demo','tpl_obj_hwfin_esg','指标描述','指标口径与说明','文本',0,'{}'::jsonb,7,'metric_description',0),
    -- 关键人员 key_person
    ('tpl_attr_hwfin_kp_name','tenant_jonex_demo','tpl_obj_hwfin_person','姓名','人员姓名','字符串',1,'{}'::jsonb,1,'person_name',1),
    ('tpl_attr_hwfin_kp_title','tenant_jonex_demo','tpl_obj_hwfin_person','职位','职级/头衔','字符串',0,'{}'::jsonb,2,'title',1),
    ('tpl_attr_hwfin_kp_role','tenant_jonex_demo','tpl_obj_hwfin_person','角色','如董事长、副董事长、CEO、总裁、独立非执行董事','枚举',0,'{}'::jsonb,3,'role',0),
    ('tpl_attr_hwfin_kp_type','tenant_jonex_demo','tpl_obj_hwfin_person','董事类型','如执行董事、非执行董事、独立非执行董事','枚举',0,'{}'::jsonb,4,'director_type',0),
    ('tpl_attr_hwfin_kp_co','tenant_jonex_demo','tpl_obj_hwfin_person','所属公司','任职公司','字符串',0,'{}'::jsonb,5,'affiliated_company',0),
    -- 业务事件 business_event
    ('tpl_attr_hwfin_ev_name','tenant_jonex_demo','tpl_obj_hwfin_event','事件名称','事件标题','字符串',1,'{}'::jsonb,1,'event_name',1),
    ('tpl_attr_hwfin_ev_type','tenant_jonex_demo','tpl_obj_hwfin_event','事件类型','如财报发布、产品交付、战略发布、配售','枚举',0,'{}'::jsonb,2,'event_type',0),
    ('tpl_attr_hwfin_ev_date','tenant_jonex_demo','tpl_obj_hwfin_event','事件日期','事件发生日期','日期',0,'{}'::jsonb,3,'event_date',0),
    ('tpl_attr_hwfin_ev_desc','tenant_jonex_demo','tpl_obj_hwfin_event','事件描述','事件详情摘要','文本',0,'{}'::jsonb,4,'event_description',0),
    -- 地区市场 geographic_market
    ('tpl_attr_hwfin_mk_name','tenant_jonex_demo','tpl_obj_hwfin_market','市场名称','地区/市场名称（如中国大陆、境外、欧洲、印度）','字符串',1,'{}'::jsonb,1,'market_name',1),
    ('tpl_attr_hwfin_mk_type','tenant_jonex_demo','tpl_obj_hwfin_market','区域类型','中国大陆/境外/区域/国家','枚举',0,'{}'::jsonb,2,'region_type',0),
    ('tpl_attr_hwfin_mk_rev','tenant_jonex_demo','tpl_obj_hwfin_market','地区营收','该地区营收（万元）','数值',0,'{}'::jsonb,3,'revenue',0),
    ('tpl_attr_hwfin_mk_ratio','tenant_jonex_demo','tpl_obj_hwfin_market','营收占比','该地区营收占总营收比例（%）','数值',0,'{}'::jsonb,4,'revenue_ratio',0),
    ('tpl_attr_hwfin_mk_store','tenant_jonex_demo','tpl_obj_hwfin_market','门店数','该地区门店/服务点数量','数值',0,'{}'::jsonb,5,'store_count',0),
    ('tpl_attr_hwfin_mk_desc','tenant_jonex_demo','tpl_obj_hwfin_market','市场描述','地区市场表现与布局说明','文本',0,'{}'::jsonb,6,'market_description',0),
    -- 销售渠道 sales_channel
    ('tpl_attr_hwfin_ch_name','tenant_jonex_demo','tpl_obj_hwfin_channel','渠道名称','渠道名称（如小米之家、直营店、授权店、经销商、小米商城）','字符串',1,'{}'::jsonb,1,'channel_name',1),
    ('tpl_attr_hwfin_ch_type','tenant_jonex_demo','tpl_obj_hwfin_channel','渠道类型','线上/线下/直营/授权/经销','枚举',0,'{}'::jsonb,2,'channel_type',0),
    ('tpl_attr_hwfin_ch_store','tenant_jonex_demo','tpl_obj_hwfin_channel','门店数','渠道门店/网点数量','数值',0,'{}'::jsonb,3,'store_count',0),
    ('tpl_attr_hwfin_ch_region','tenant_jonex_demo','tpl_obj_hwfin_channel','覆盖地区','渠道覆盖地区','字符串',0,'{}'::jsonb,4,'region',0),
    ('tpl_attr_hwfin_ch_desc','tenant_jonex_demo','tpl_obj_hwfin_channel','渠道描述','渠道运营与布局说明','文本',0,'{}'::jsonb,5,'channel_description',0),
    -- 子公司 subsidiary
    ('tpl_attr_hwfin_sub_name','tenant_jonex_demo','tpl_obj_hwfin_subsidiary','子公司名称','附属公司/集团实体名称（如小米印度）','字符串',1,'{}'::jsonb,1,'subsidiary_name',1),
    ('tpl_attr_hwfin_sub_loc','tenant_jonex_demo','tpl_obj_hwfin_subsidiary','所在地','子公司所在国家/地区','字符串',0,'{}'::jsonb,2,'location',0),
    ('tpl_attr_hwfin_sub_own','tenant_jonex_demo','tpl_obj_hwfin_subsidiary','持股比例','母公司持股比例（%）','数值',0,'{}'::jsonb,3,'ownership',0),
    ('tpl_attr_hwfin_sub_scope','tenant_jonex_demo','tpl_obj_hwfin_subsidiary','经营范围','子公司主营业务范围','字符串',0,'{}'::jsonb,4,'business_scope',0),
    ('tpl_attr_hwfin_sub_desc','tenant_jonex_demo','tpl_obj_hwfin_subsidiary','子公司描述','子公司情况说明','文本',0,'{}'::jsonb,5,'subsidiary_description',0),
    -- 风险因素 risk_factor
    ('tpl_attr_hwfin_rk_name','tenant_jonex_demo','tpl_obj_hwfin_risk','风险名称','风险因素名称（如竞争风险、地缘政治风险）','字符串',1,'{}'::jsonb,1,'risk_name',1),
    ('tpl_attr_hwfin_rk_cat','tenant_jonex_demo','tpl_obj_hwfin_risk','风险类别','竞争/市场/政治/气候/财务/运营/合规','枚举',0,'{}'::jsonb,2,'risk_category',0),
    ('tpl_attr_hwfin_rk_desc','tenant_jonex_demo','tpl_obj_hwfin_risk','风险描述','风险成因与潜在影响说明','文本',0,'{}'::jsonb,3,'risk_description',0),
    ('tpl_attr_hwfin_rk_mit','tenant_jonex_demo','tpl_obj_hwfin_risk','应对措施','风险缓释/应对措施','文本',0,'{}'::jsonb,4,'mitigation',0),
    -- 法律诉讼 legal_proceeding
    ('tpl_attr_hwfin_lg_name','tenant_jonex_demo','tpl_obj_hwfin_legal','案件名称','诉讼/监管事项名称','字符串',1,'{}'::jsonb,1,'case_name',1),
    ('tpl_attr_hwfin_lg_juris','tenant_jonex_demo','tpl_obj_hwfin_legal','司法管辖','涉及国家/地区/监管机构（如印度税务局）','字符串',0,'{}'::jsonb,2,'jurisdiction',0),
    ('tpl_attr_hwfin_lg_status','tenant_jonex_demo','tpl_obj_hwfin_legal','案件状态','调查中/聽證/已结案等','枚举',0,'{}'::jsonb,3,'status',0),
    ('tpl_attr_hwfin_lg_amount','tenant_jonex_demo','tpl_obj_hwfin_legal','涉及金额','涉案/冻结/撥備金额（万元）','数值',0,'{}'::jsonb,4,'amount_involved',0),
    ('tpl_attr_hwfin_lg_desc','tenant_jonex_demo','tpl_obj_hwfin_legal','案件描述','事项详情与进展说明','文本',0,'{}'::jsonb,5,'case_description',0),
    -- 可持续举措 sustainability_initiative
    ('tpl_attr_hwfin_si_name','tenant_jonex_demo','tpl_obj_hwfin_esginit','举措名称','可持续/ESG 举措名称（如物流低碳、以旧换新）','字符串',1,'{}'::jsonb,1,'initiative_name',1),
    ('tpl_attr_hwfin_si_pillar','tenant_jonex_demo','tpl_obj_hwfin_esginit','ESG支柱','环境/社会/管治','枚举',0,'{}'::jsonb,2,'esg_pillar',0),
    ('tpl_attr_hwfin_si_target','tenant_jonex_demo','tpl_obj_hwfin_esginit','目标','举措目标（如碳减排目标）','字符串',0,'{}'::jsonb,3,'target',0),
    ('tpl_attr_hwfin_si_progress','tenant_jonex_demo','tpl_obj_hwfin_esginit','进展','举措进展/成效（如减少约2,471吨CO2e）','字符串',0,'{}'::jsonb,4,'progress',0),
    ('tpl_attr_hwfin_si_desc','tenant_jonex_demo','tpl_obj_hwfin_esginit','举措描述','举措内容与方法说明','文本',0,'{}'::jsonb,5,'initiative_description',0)
ON CONFLICT (id) DO NOTHING;

-- 5. 模板关系（26 条核心关系）
INSERT INTO business_domain.template_relations
    (id, tenant_id, domain_id, scenario_id, name, description, source_object_id, target_object_id, relation_type, status, ontology_code, aliases)
VALUES
 ('tpl_rel_hwfin_issues','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','发布报告','上市公司发布财务报告','tpl_obj_hwfin_company','tpl_obj_hwfin_report','一对多','active','ISSUES_REPORT','["发布报告","披露","出具"]'::jsonb),
 ('tpl_rel_hwfin_has_seg','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','包含分部','财务报告包含的业务分部','tpl_obj_hwfin_report','tpl_obj_hwfin_segment','一对多','active','HAS_SEGMENT','["包含分部","涵盖分部"]'::jsonb),
 ('tpl_rel_hwfin_seg_line','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','分部含产品线','业务分部下的产品线','tpl_obj_hwfin_segment','tpl_obj_hwfin_line','一对多','active','SEGMENT_INCLUDES_LINE','["包含产品线","下设产品线"]'::jsonb),
 ('tpl_rel_hwfin_line_prod','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','产品线产出产品','产品线产出的具体产品','tpl_obj_hwfin_line','tpl_obj_hwfin_product','一对多','active','LINE_PRODUCES_PRODUCT','["产出","生产","包含产品"]'::jsonb),
 ('tpl_rel_hwfin_rpt_fm','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','报告财务指标','财务报告披露的财务指标','tpl_obj_hwfin_report','tpl_obj_hwfin_fin_metric','一对多','active','REPORT_HAS_FINANCIAL_METRIC','["披露指标","含指标"]'::jsonb),
 ('tpl_rel_hwfin_seg_fm','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','分部财务指标','业务分部披露的财务指标','tpl_obj_hwfin_segment','tpl_obj_hwfin_fin_metric','一对多','active','SEGMENT_HAS_METRIC','["分部指标"]'::jsonb),
 ('tpl_rel_hwfin_pd_om','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','产品运营指标','产品对应的运营指标','tpl_obj_hwfin_product','tpl_obj_hwfin_op_metric','一对多','active','PRODUCT_HAS_OPERATIONAL_METRIC','["产品指标"]'::jsonb),
 ('tpl_rel_hwfin_co_kp','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','关键人员','上市公司关键管理人员','tpl_obj_hwfin_company','tpl_obj_hwfin_person','一对多','active','COMPANY_HAS_KEYPERSON','["高管","管理层"]'::jsonb),
 ('tpl_rel_hwfin_co_ev','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','参与事件','上市公司参与/披露的业务事件','tpl_obj_hwfin_company','tpl_obj_hwfin_event','多对多','active','PARTICIPATES_EVENT','["参与","披露事件"]'::jsonb),
 ('tpl_rel_hwfin_co_rm','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','研发指标','上市公司研发指标','tpl_obj_hwfin_company','tpl_obj_hwfin_rnd_metric','一对多','active','COMPANY_HAS_RND_METRIC','["研发投入"]'::jsonb),
 ('tpl_rel_hwfin_co_om','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','公司运营指标','上市公司整体运营指标','tpl_obj_hwfin_company','tpl_obj_hwfin_op_metric','一对多','active','COMPANY_HAS_OPERATIONAL_METRIC','["运营指标"]'::jsonb),
 ('tpl_rel_hwfin_ln_fm','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','产品线财务指标','产品线披露的财务指标','tpl_obj_hwfin_line','tpl_obj_hwfin_fin_metric','一对多','active','LINE_HAS_METRIC','["产品线指标"]'::jsonb),
 ('tpl_rel_hwfin_rpt_ce','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','报告成本费用','财务报告披露的成本费用','tpl_obj_hwfin_report','tpl_obj_hwfin_cost','一对多','active','REPORT_HAS_COST_EXPENSE','["含费用","披露费用"]'::jsonb),
 ('tpl_rel_hwfin_seg_ce','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','分部成本费用','业务分部披露的成本费用','tpl_obj_hwfin_segment','tpl_obj_hwfin_cost','一对多','active','SEGMENT_HAS_COST_EXPENSE','["分部费用"]'::jsonb),
 ('tpl_rel_hwfin_co_sr','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','股东回报','上市公司股东回报事项','tpl_obj_hwfin_company','tpl_obj_hwfin_shareholder','一对多','active','COMPANY_HAS_SHAREHOLDER_RETURN','["股息","配售","回购"]'::jsonb),
 ('tpl_rel_hwfin_co_eg','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','ESG指标','上市公司ESG相关指标','tpl_obj_hwfin_company','tpl_obj_hwfin_esg','一对多','active','COMPANY_HAS_ESG_METRIC','["ESG","环境社会管治"]'::jsonb),
 ('tpl_rel_hwfin_co_mkt','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','地区经营','上市公司在各地区市场运营','tpl_obj_hwfin_company','tpl_obj_hwfin_market','一对多','active','COMPANY_OPERATES_IN_MARKET','["地区经营","分地区","市场布局"]'::jsonb),
 ('tpl_rel_hwfin_seg_mkt','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','分地区营收','业务分部在各地区的营收分布','tpl_obj_hwfin_segment','tpl_obj_hwfin_market','多对多','active','SEGMENT_SELLS_IN_MARKET','["分地区营收","地区收入"]'::jsonb),
 ('tpl_rel_hwfin_co_ch','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','销售渠道','上市公司通过渠道销售','tpl_obj_hwfin_company','tpl_obj_hwfin_channel','一对多','active','COMPANY_USES_CHANNEL','["销售渠道","渠道布局"]'::jsonb),
 ('tpl_rel_hwfin_ch_mkt','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','渠道覆盖','销售渠道覆盖的地区市场','tpl_obj_hwfin_channel','tpl_obj_hwfin_market','多对多','active','CHANNEL_COVERS_MARKET','["渠道覆盖","渠道分布"]'::jsonb),
 ('tpl_rel_hwfin_co_sub','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','子公司','上市公司拥有的子公司','tpl_obj_hwfin_company','tpl_obj_hwfin_subsidiary','一对多','active','COMPANY_HAS_SUBSIDIARY','["子公司","附属公司"]'::jsonb),
 ('tpl_rel_hwfin_sub_mkt','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','子公司所在地','子公司所在的地区市场','tpl_obj_hwfin_subsidiary','tpl_obj_hwfin_market','多对一','active','SUBSIDIARY_LOCATED_IN_MARKET','["所在地","注册地"]'::jsonb),
 ('tpl_rel_hwfin_co_risk','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','面临风险','上市公司面临的风险因素','tpl_obj_hwfin_company','tpl_obj_hwfin_risk','一对多','active','COMPANY_FACES_RISK','["面临风险","风险披露"]'::jsonb),
 ('tpl_rel_hwfin_sub_lg','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','涉及诉讼','子公司涉及的法律诉讼/监管事项','tpl_obj_hwfin_subsidiary','tpl_obj_hwfin_legal','一对多','active','SUBSIDIARY_INVOLVED_IN_PROCEEDING','["涉及诉讼","涉诉"]'::jsonb),
 ('tpl_rel_hwfin_co_si','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','开展举措','上市公司开展的可持续举措','tpl_obj_hwfin_company','tpl_obj_hwfin_esginit','一对多','active','COMPANY_UNDERTAKES_INITIATIVE','["开展举措","可持续行动"]'::jsonb),
 ('tpl_rel_hwfin_si_esg','tenant_jonex_demo','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','改善指标','可持续举措改善的ESG指标','tpl_obj_hwfin_esginit','tpl_obj_hwfin_esg','多对多','active','INITIATIVE_IMPROVES_ESG','["改善指标","支撑ESG"]'::jsonb)
ON CONFLICT (id) DO NOTHING;

-- ============================================================
-- 硬件互联网财报知识库种子数据
-- ============================================================
INSERT INTO knowledge_base.knowledge_info (id, tenant_id, space_id, name, description, data_source_types, document_count, status, owner_id) VALUES
    ('kb_demo_hw_inet_finance', 'tenant_jonex_demo', 'space_demo_test', '硬件互联网财报知识库', '硬件互联网上市公司财报结构化抽（基于小米集团2025年度报告）', '["file"]'::jsonb, 0, 'synced', '1')
ON CONFLICT (id) DO NOTHING;

-- 内置「文件上传」数据源
INSERT INTO knowledge_base.knowledge_data_sources
    (id, tenant_id, knowledge_base_id, access_method_id,access_type, name, config_json, sync_mode, status)
VALUES
    ('ds_demo_hwfin_file', 'tenant_jonex_demo', 'kb_demo_hw_inet_finance', 'dam_demo_file', 'file', '文件上传', '{}'::jsonb, 'manual', 'active')
ON CONFLICT (id) DO NOTHING;

-- 领域服务
INSERT INTO knowledge_base.services (id, tenant_id, space_id, name, description, domain_type, status, api_key_encrypted)
VALUES
    ('svc_demo_hw_inet_finance', 'tenant_jonex_demo', 'space_demo_test', '硬件互联网财报领域服务', '硬件互联网财报解析领域服务', '硬件互联网', 'active', 'sk-hwfin-0123456789abcdef0123456789abcdef')
ON CONFLICT (id) DO NOTHING;

-- 领域服务和知识库关联关系
INSERT INTO knowledge_base.service_knowledge_bases (id, tenant_id, service_id, kb_id)
VALUES
    ('skb_demo_hwfin', 'tenant_jonex_demo', 'svc_demo_hw_inet_finance', 'kb_demo_hw_inet_finance')
ON CONFLICT (id) DO NOTHING;

-- 测试用 API Key
INSERT INTO knowledge_base.service_api_keys (id, tenant_id, service_id, key_prefix, key_encrypted, expires_at, is_active)
VALUES
    ('sak_hwfin_main', 'tenant_jonex_demo', 'svc_demo_hw_inet_finance', 'sk', 'sk-hwfin-0123456789abcdef0123456789abcdef', '2027-12-31'::timestamp, 1),
    ('sak_hwfin_readonly', 'tenant_jonex_demo', 'svc_demo_hw_inet_finance', 'sk', 'sk-ro-hwfin-a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6', '2026-12-31'::timestamp, 1),
    ('sak_hwfin_expired', 'tenant_jonex_demo', 'svc_demo_hw_inet_finance', 'sk', 'sk-expired-hwfin-00000000000000000000000000', '2026-01-01'::timestamp, 0)
ON CONFLICT (id) DO NOTHING;

-- 本体模板绑定（KB -> 模板场景）
INSERT INTO knowledge_base.ontology_template_bindings
    (tenant_id, knowledge_base_id, template_domain_id, template_scenario_id, source_type, status)
VALUES
    ('tenant_jonex_demo','kb_demo_hw_inet_finance','tpl_domain_hardware_internet','tpl_scenario_hw_inet_finance','business_template','active')
ON CONFLICT (tenant_id, knowledge_base_id) DO NOTHING;

-- 10. 预编译本体 schema（硬件互联网财报分析场景）
-- 基于小米集团 2025 年度报告（股份代号：1810/81810）逆向定义，v3 在原 13 类实体/16 类关系
-- 基础上补充 地区市场/销售渠道/子公司/风险因素/法律诉讼/可持续举措 6 类实体与 10 类关系，
-- 共覆盖 19 类实体与 26 类关系。
INSERT INTO knowledge_base.ontology_compiled_schemas
    (tenant_id, knowledge_base_id, template_domain_id, template_scenario_id,
     source_type, source_version, source_hash, schema_version,
     entity_types, relation_types, constraints, disambiguation, prompt_schema,
     status, compiled_at)
VALUES (
    'tenant_jonex_demo', 'kb_demo_hw_inet_finance',
    'tpl_domain_hardware_internet', 'tpl_scenario_hw_inet_finance',
    'business_template', 3, 'd5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6', 3,
    '[
        {"name":"listed_company","display_name":"上市公司","aliases":["上市公司","公司","集团","发行人","Issuer"],"source_object_id":"tpl_obj_hwfin_company","attributes":[
            {"name":"stock_code","display_name":"股票代码","type":"string","required":true,"source_attribute_id":"tpl_attr_hwfin_co_stock"},
            {"name":"company_name","display_name":"公司名称","type":"string","required":true,"source_attribute_id":"tpl_attr_hwfin_co_name"},
            {"name":"exchange","display_name":"上市交易所","type":"enum","required":false,"source_attribute_id":"tpl_attr_hwfin_co_exchange"},
            {"name":"industry","display_name":"所属行业","type":"enum","required":false,"source_attribute_id":"tpl_attr_hwfin_co_industry"},
            {"name":"founded_date","display_name":"成立日期","type":"date","required":false,"source_attribute_id":"tpl_attr_hwfin_co_founded"},
            {"name":"chairman","display_name":"董事长","type":"string","required":false,"source_attribute_id":"tpl_attr_hwfin_co_chairman"},
            {"name":"ceo","display_name":"首席执行官","type":"string","required":false,"source_attribute_id":"tpl_attr_hwfin_co_ceo"},
            {"name":"headquarters","display_name":"总部","type":"string","required":false,"source_attribute_id":"tpl_attr_hwfin_co_hq"},
            {"name":"company_description","display_name":"公司简介","type":"text","required":false,"source_attribute_id":"tpl_attr_hwfin_co_desc"}
        ]},
        {"name":"financial_report","display_name":"财务报告","aliases":["财务报告","财报","年报","年度报告","季报","中期报告","Annual Report"],"source_object_id":"tpl_obj_hwfin_report","attributes":[
            {"name":"report_id","display_name":"报告ID","type":"string","required":true,"source_attribute_id":"tpl_attr_hwfin_rpt_id"},
            {"name":"report_type","display_name":"报告类型","type":"enum","required":false,"source_attribute_id":"tpl_attr_hwfin_rpt_type"},
            {"name":"fiscal_year","display_name":"财年","type":"number","required":true,"source_attribute_id":"tpl_attr_hwfin_rpt_year"},
            {"name":"reporting_period","display_name":"报告期","type":"string","required":false,"source_attribute_id":"tpl_attr_hwfin_rpt_period"},
            {"name":"release_date","display_name":"发布日期","type":"date","required":false,"source_attribute_id":"tpl_attr_hwfin_rpt_release"},
            {"name":"currency","display_name":"币种","type":"string","required":false,"source_attribute_id":"tpl_attr_hwfin_rpt_currency"},
            {"name":"total_revenue","display_name":"总营收","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_rpt_revenue"},
            {"name":"revenue_yoy","display_name":"营收同比","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_rpt_rev_yoy"},
            {"name":"cost_of_sales","display_name":"销售成本","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_rpt_cogs"},
            {"name":"gross_profit","display_name":"毛利","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_rpt_gp"},
            {"name":"gross_margin","display_name":"毛利率","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_rpt_gm"},
            {"name":"selling_expense","display_name":"销售及推广开支","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_rpt_selling"},
            {"name":"admin_expense","display_name":"行政开支","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_rpt_admin"},
            {"name":"rnd_expense","display_name":"研发开支","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_rpt_rnd"},
            {"name":"operating_profit","display_name":"经营利润","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_rpt_op"},
            {"name":"finance_income_net","display_name":"财务收入净额","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_rpt_fincome"},
            {"name":"profit_before_tax","display_name":"除所得税前利润","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_rpt_pbt"},
            {"name":"income_tax_expense","display_name":"所得税费用","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_rpt_tax"},
            {"name":"net_profit","display_name":"年度利润","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_rpt_np"},
            {"name":"adjusted_net_profit","display_name":"经调整净利润","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_rpt_anp"},
            {"name":"adjusted_net_profit_yoy","display_name":"经调整净利润同比","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_rpt_anp_yoy"}
        ]},
        {"name":"business_segment","display_name":"业务分部","aliases":["业务分部","分部","板块","业务线","Segment"],"source_object_id":"tpl_obj_hwfin_segment","attributes":[
            {"name":"segment_code","display_name":"分部代码","type":"string","required":true,"source_attribute_id":"tpl_attr_hwfin_seg_code"},
            {"name":"segment_name","display_name":"分部名称","type":"string","required":true,"source_attribute_id":"tpl_attr_hwfin_seg_name"},
            {"name":"segment_type","display_name":"分部类型","type":"enum","required":false,"source_attribute_id":"tpl_attr_hwfin_seg_type"},
            {"name":"revenue","display_name":"分部营收","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_seg_rev"},
            {"name":"revenue_yoy","display_name":"营收同比","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_seg_rev_yoy"},
            {"name":"revenue_ratio","display_name":"营收占比","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_seg_ratio"},
            {"name":"cost_of_sales","display_name":"分部销售成本","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_seg_cogs"},
            {"name":"gross_profit","display_name":"分部毛利","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_seg_gp"},
            {"name":"gross_margin","display_name":"分部毛利率","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_seg_gm"},
            {"name":"operating_result","display_name":"分部经营收益","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_seg_op"},
            {"name":"adjusted_net_profit","display_name":"经调整净利润","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_seg_anp"},
            {"name":"adjusted_net_loss","display_name":"经调整净亏损","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_seg_anl"},
            {"name":"segment_description","display_name":"分部描述","type":"text","required":false,"source_attribute_id":"tpl_attr_hwfin_seg_desc"}
        ]},
        {"name":"product_line","display_name":"产品线","aliases":["产品线","业务类别","Product Line"],"source_object_id":"tpl_obj_hwfin_line","attributes":[
            {"name":"line_code","display_name":"产品线代码","type":"string","required":true,"source_attribute_id":"tpl_attr_hwfin_ln_code"},
            {"name":"line_name","display_name":"产品线名称","type":"string","required":true,"source_attribute_id":"tpl_attr_hwfin_ln_name"},
            {"name":"category","display_name":"类别","type":"enum","required":false,"source_attribute_id":"tpl_attr_hwfin_ln_cat"},
            {"name":"revenue","display_name":"产品线营收","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_ln_rev"},
            {"name":"revenue_yoy","display_name":"营收同比","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_ln_rev_yoy"},
            {"name":"revenue_ratio","display_name":"营收占比","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_ln_ratio"},
            {"name":"cost_of_sales","display_name":"产品线销售成本","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_ln_cogs"},
            {"name":"gross_profit","display_name":"产品线毛利","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_ln_gp"},
            {"name":"gross_margin","display_name":"产品线毛利率","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_ln_gm"},
            {"name":"description","display_name":"描述","type":"text","required":false,"source_attribute_id":"tpl_attr_hwfin_ln_desc"}
        ]},
        {"name":"product","display_name":"产品","aliases":["产品","机型","应用","服务","车型","Product"],"source_object_id":"tpl_obj_hwfin_product","attributes":[
            {"name":"product_code","display_name":"产品代码","type":"string","required":true,"source_attribute_id":"tpl_attr_hwfin_pd_code"},
            {"name":"product_name","display_name":"产品名称","type":"string","required":true,"source_attribute_id":"tpl_attr_hwfin_pd_name"},
            {"name":"product_category","display_name":"产品类别","type":"enum","required":false,"source_attribute_id":"tpl_attr_hwfin_pd_cat"},
            {"name":"launch_date","display_name":"上市日期","type":"date","required":false,"source_attribute_id":"tpl_attr_hwfin_pd_launch"},
            {"name":"shipment_volume","display_name":"出货量","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_pd_ship"},
            {"name":"shipment_yoy","display_name":"出货量同比","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_pd_ship_yoy"},
            {"name":"asp","display_name":"平均售价","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_pd_asp"},
            {"name":"asp_yoy","display_name":"ASP同比","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_pd_asp_yoy"},
            {"name":"gross_margin","display_name":"毛利率","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_pd_gm"},
            {"name":"market_share","display_name":"市占率","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_pd_share"},
            {"name":"product_description","display_name":"产品描述","type":"text","required":false,"source_attribute_id":"tpl_attr_hwfin_pd_desc"}
        ]},
        {"name":"financial_metric","display_name":"财务指标","aliases":["财务指标","财务数据","Financial Metric"],"source_object_id":"tpl_obj_hwfin_fin_metric","attributes":[
            {"name":"metric_code","display_name":"指标代码","type":"string","required":true,"source_attribute_id":"tpl_attr_hwfin_fm_code"},
            {"name":"metric_name","display_name":"指标名称","type":"string","required":true,"source_attribute_id":"tpl_attr_hwfin_fm_name"},
            {"name":"metric_value","display_name":"指标值","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_fm_val"},
            {"name":"metric_unit","display_name":"单位","type":"string","required":false,"source_attribute_id":"tpl_attr_hwfin_fm_unit"},
            {"name":"metric_yoy","display_name":"同比","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_fm_yoy"},
            {"name":"metric_period","display_name":"指标期间","type":"string","required":false,"source_attribute_id":"tpl_attr_hwfin_fm_period"},
            {"name":"metric_category","display_name":"指标分类","type":"enum","required":false,"source_attribute_id":"tpl_attr_hwfin_fm_cat"}
        ]},
        {"name":"operational_metric","display_name":"运营指标","aliases":["运营指标","经营数据","Operational Metric"],"source_object_id":"tpl_obj_hwfin_op_metric","attributes":[
            {"name":"metric_code","display_name":"指标代码","type":"string","required":true,"source_attribute_id":"tpl_attr_hwfin_om_code"},
            {"name":"metric_name","display_name":"指标名称","type":"string","required":true,"source_attribute_id":"tpl_attr_hwfin_om_name"},
            {"name":"metric_value","display_name":"指标值","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_om_val"},
            {"name":"metric_unit","display_name":"单位","type":"string","required":false,"source_attribute_id":"tpl_attr_hwfin_om_unit"},
            {"name":"metric_yoy","display_name":"同比","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_om_yoy"},
            {"name":"metric_category","display_name":"指标分类","type":"enum","required":false,"source_attribute_id":"tpl_attr_hwfin_om_cat"},
            {"name":"region","display_name":"统计区域","type":"string","required":false,"source_attribute_id":"tpl_attr_hwfin_om_region"},
            {"name":"metric_description","display_name":"指标描述","type":"text","required":false,"source_attribute_id":"tpl_attr_hwfin_om_desc"}
        ]},
        {"name":"rnd_metric","display_name":"研发指标","aliases":["研发指标","研发投入","RnD Metric"],"source_object_id":"tpl_obj_hwfin_rnd_metric","attributes":[
            {"name":"metric_code","display_name":"指标代码","type":"string","required":true,"source_attribute_id":"tpl_attr_hwfin_rm_code"},
            {"name":"metric_name","display_name":"指标名称","type":"string","required":true,"source_attribute_id":"tpl_attr_hwfin_rm_name"},
            {"name":"metric_value","display_name":"指标值","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_rm_val"},
            {"name":"metric_unit","display_name":"单位","type":"string","required":false,"source_attribute_id":"tpl_attr_hwfin_rm_unit"},
            {"name":"metric_yoy","display_name":"同比","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_rm_yoy"},
            {"name":"rnd_headcount","display_name":"研发人员数","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_rm_head"},
            {"name":"cumulative_rnd","display_name":"累计研发投入","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_rm_cum"}
        ]},
        {"name":"cost_expense","display_name":"成本费用","aliases":["成本费用","费用","支出","Cost Expense"],"source_object_id":"tpl_obj_hwfin_cost","attributes":[
            {"name":"expense_code","display_name":"费用代码","type":"string","required":true,"source_attribute_id":"tpl_attr_hwfin_ce_code"},
            {"name":"expense_name","display_name":"费用名称","type":"string","required":true,"source_attribute_id":"tpl_attr_hwfin_ce_name"},
            {"name":"expense_category","display_name":"费用分类","type":"enum","required":false,"source_attribute_id":"tpl_attr_hwfin_ce_cat"},
            {"name":"expense_value","display_name":"费用金额","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_ce_val"},
            {"name":"expense_yoy","display_name":"同比","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_ce_yoy"},
            {"name":"expense_ratio","display_name":"占营收比","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_ce_ratio"},
            {"name":"expense_period","display_name":"费用期间","type":"string","required":false,"source_attribute_id":"tpl_attr_hwfin_ce_period"}
        ]},
        {"name":"shareholder_return","display_name":"股东回报","aliases":["股东回报","股息","分红","配售","回购","Shareholder Return"],"source_object_id":"tpl_obj_hwfin_shareholder","attributes":[
            {"name":"return_code","display_name":"事项代码","type":"string","required":true,"source_attribute_id":"tpl_attr_hwfin_sr_code"},
            {"name":"return_type","display_name":"事项类型","type":"enum","required":true,"source_attribute_id":"tpl_attr_hwfin_sr_type"},
            {"name":"return_description","display_name":"事项描述","type":"string","required":false,"source_attribute_id":"tpl_attr_hwfin_sr_desc"},
            {"name":"return_amount","display_name":"金额/规模","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_sr_amount"},
            {"name":"per_share_price","display_name":"每股价格","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_sr_price"},
            {"name":"return_date","display_name":"事项日期","type":"date","required":false,"source_attribute_id":"tpl_attr_hwfin_sr_date"},
            {"name":"return_status","display_name":"事项状态","type":"enum","required":false,"source_attribute_id":"tpl_attr_hwfin_sr_status"}
        ]},
        {"name":"esg_metric","display_name":"ESG指标","aliases":["ESG指标","ESG","环境社会管治","ESG Metric"],"source_object_id":"tpl_obj_hwfin_esg","attributes":[
            {"name":"metric_code","display_name":"指标代码","type":"string","required":true,"source_attribute_id":"tpl_attr_hwfin_eg_code"},
            {"name":"metric_name","display_name":"指标名称","type":"string","required":true,"source_attribute_id":"tpl_attr_hwfin_eg_name"},
            {"name":"metric_category","display_name":"指标分类","type":"enum","required":false,"source_attribute_id":"tpl_attr_hwfin_eg_cat"},
            {"name":"metric_value","display_name":"指标值","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_eg_val"},
            {"name":"metric_unit","display_name":"单位","type":"string","required":false,"source_attribute_id":"tpl_attr_hwfin_eg_unit"},
            {"name":"metric_yoy","display_name":"同比","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_eg_yoy"},
            {"name":"metric_description","display_name":"指标描述","type":"text","required":false,"source_attribute_id":"tpl_attr_hwfin_eg_desc"}
        ]},
        {"name":"key_person","display_name":"关键人员","aliases":["关键人员","高管","管理层","董事","Key Person"],"source_object_id":"tpl_obj_hwfin_person","attributes":[
            {"name":"person_name","display_name":"姓名","type":"string","required":true,"source_attribute_id":"tpl_attr_hwfin_kp_name"},
            {"name":"title","display_name":"职位","type":"string","required":true,"source_attribute_id":"tpl_attr_hwfin_kp_title"},
            {"name":"role","display_name":"角色","type":"enum","required":false,"source_attribute_id":"tpl_attr_hwfin_kp_role"},
            {"name":"director_type","display_name":"董事类型","type":"enum","required":false,"source_attribute_id":"tpl_attr_hwfin_kp_type"},
            {"name":"affiliated_company","display_name":"所属公司","type":"string","required":false,"source_attribute_id":"tpl_attr_hwfin_kp_co"}
        ]},
        {"name":"business_event","display_name":"业务事件","aliases":["业务事件","事件","里程碑","Business Event"],"source_object_id":"tpl_obj_hwfin_event","attributes":[
            {"name":"event_name","display_name":"事件名称","type":"string","required":true,"source_attribute_id":"tpl_attr_hwfin_ev_name"},
            {"name":"event_type","display_name":"事件类型","type":"enum","required":false,"source_attribute_id":"tpl_attr_hwfin_ev_type"},
            {"name":"event_date","display_name":"事件日期","type":"date","required":false,"source_attribute_id":"tpl_attr_hwfin_ev_date"},
            {"name":"event_description","display_name":"事件描述","type":"text","required":false,"source_attribute_id":"tpl_attr_hwfin_ev_desc"}
        ]},
        {"name":"geographic_market","display_name":"地区市场","aliases":["地区市场","市场","区域","地区","Region","Market"],"source_object_id":"tpl_obj_hwfin_market","attributes":[
            {"name":"market_name","display_name":"市场名称","type":"string","required":true,"source_attribute_id":"tpl_attr_hwfin_mk_name"},
            {"name":"region_type","display_name":"区域类型","type":"enum","required":false,"source_attribute_id":"tpl_attr_hwfin_mk_type"},
            {"name":"revenue","display_name":"地区营收","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_mk_rev"},
            {"name":"revenue_ratio","display_name":"营收占比","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_mk_ratio"},
            {"name":"store_count","display_name":"门店数","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_mk_store"},
            {"name":"market_description","display_name":"市场描述","type":"text","required":false,"source_attribute_id":"tpl_attr_hwfin_mk_desc"}
        ]},
        {"name":"sales_channel","display_name":"销售渠道","aliases":["销售渠道","渠道","门店","零售网络","Channel"],"source_object_id":"tpl_obj_hwfin_channel","attributes":[
            {"name":"channel_name","display_name":"渠道名称","type":"string","required":true,"source_attribute_id":"tpl_attr_hwfin_ch_name"},
            {"name":"channel_type","display_name":"渠道类型","type":"enum","required":false,"source_attribute_id":"tpl_attr_hwfin_ch_type"},
            {"name":"store_count","display_name":"门店数","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_ch_store"},
            {"name":"region","display_name":"覆盖地区","type":"string","required":false,"source_attribute_id":"tpl_attr_hwfin_ch_region"},
            {"name":"channel_description","display_name":"渠道描述","type":"text","required":false,"source_attribute_id":"tpl_attr_hwfin_ch_desc"}
        ]},
        {"name":"subsidiary","display_name":"子公司","aliases":["子公司","附属公司","集团实体","Subsidiary"],"source_object_id":"tpl_obj_hwfin_subsidiary","attributes":[
            {"name":"subsidiary_name","display_name":"子公司名称","type":"string","required":true,"source_attribute_id":"tpl_attr_hwfin_sub_name"},
            {"name":"location","display_name":"所在地","type":"string","required":false,"source_attribute_id":"tpl_attr_hwfin_sub_loc"},
            {"name":"ownership","display_name":"持股比例","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_sub_own"},
            {"name":"business_scope","display_name":"经营范围","type":"string","required":false,"source_attribute_id":"tpl_attr_hwfin_sub_scope"},
            {"name":"subsidiary_description","display_name":"子公司描述","type":"text","required":false,"source_attribute_id":"tpl_attr_hwfin_sub_desc"}
        ]},
        {"name":"risk_factor","display_name":"风险因素","aliases":["风险因素","风险","Risk Factor"],"source_object_id":"tpl_obj_hwfin_risk","attributes":[
            {"name":"risk_name","display_name":"风险名称","type":"string","required":true,"source_attribute_id":"tpl_attr_hwfin_rk_name"},
            {"name":"risk_category","display_name":"风险类别","type":"enum","required":false,"source_attribute_id":"tpl_attr_hwfin_rk_cat"},
            {"name":"risk_description","display_name":"风险描述","type":"text","required":false,"source_attribute_id":"tpl_attr_hwfin_rk_desc"},
            {"name":"mitigation","display_name":"应对措施","type":"text","required":false,"source_attribute_id":"tpl_attr_hwfin_rk_mit"}
        ]},
        {"name":"legal_proceeding","display_name":"法律诉讼","aliases":["法律诉讼","诉讼","监管调查","或然负债","Legal Proceeding"],"source_object_id":"tpl_obj_hwfin_legal","attributes":[
            {"name":"case_name","display_name":"案件名称","type":"string","required":true,"source_attribute_id":"tpl_attr_hwfin_lg_name"},
            {"name":"jurisdiction","display_name":"司法管辖","type":"string","required":false,"source_attribute_id":"tpl_attr_hwfin_lg_juris"},
            {"name":"status","display_name":"案件状态","type":"enum","required":false,"source_attribute_id":"tpl_attr_hwfin_lg_status"},
            {"name":"amount_involved","display_name":"涉及金额","type":"number","required":false,"source_attribute_id":"tpl_attr_hwfin_lg_amount"},
            {"name":"case_description","display_name":"案件描述","type":"text","required":false,"source_attribute_id":"tpl_attr_hwfin_lg_desc"}
        ]},
        {"name":"sustainability_initiative","display_name":"可持续举措","aliases":["可持续举措","ESG举措","可持续发展","Sustainability Initiative"],"source_object_id":"tpl_obj_hwfin_esginit","attributes":[
            {"name":"initiative_name","display_name":"举措名称","type":"string","required":true,"source_attribute_id":"tpl_attr_hwfin_si_name"},
            {"name":"esg_pillar","display_name":"ESG支柱","type":"enum","required":false,"source_attribute_id":"tpl_attr_hwfin_si_pillar"},
            {"name":"target","display_name":"目标","type":"string","required":false,"source_attribute_id":"tpl_attr_hwfin_si_target"},
            {"name":"progress","display_name":"进展","type":"string","required":false,"source_attribute_id":"tpl_attr_hwfin_si_progress"},
            {"name":"initiative_description","display_name":"举措描述","type":"text","required":false,"source_attribute_id":"tpl_attr_hwfin_si_desc"}
        ]}
    ]'::jsonb,
    '[
        {"name":"ISSUES_REPORT","display_name":"发布报告","aliases":["发布报告","披露","出具"],"source":"listed_company","target":"financial_report","source_relation_id":"tpl_rel_hwfin_issues","cardinality":"one_to_many"},
        {"name":"HAS_SEGMENT","display_name":"包含分部","aliases":["包含分部","涵盖分部"],"source":"financial_report","target":"business_segment","source_relation_id":"tpl_rel_hwfin_has_seg","cardinality":"one_to_many"},
        {"name":"SEGMENT_INCLUDES_LINE","display_name":"分部含产品线","aliases":["包含产品线","下设产品线"],"source":"business_segment","target":"product_line","source_relation_id":"tpl_rel_hwfin_seg_line","cardinality":"one_to_many"},
        {"name":"LINE_PRODUCES_PRODUCT","display_name":"产品线产出产品","aliases":["产出","生产","包含产品"],"source":"product_line","target":"product","source_relation_id":"tpl_rel_hwfin_line_prod","cardinality":"one_to_many"},
        {"name":"REPORT_HAS_FINANCIAL_METRIC","display_name":"报告财务指标","aliases":["披露指标","含指标"],"source":"financial_report","target":"financial_metric","source_relation_id":"tpl_rel_hwfin_rpt_fm","cardinality":"one_to_many"},
        {"name":"SEGMENT_HAS_METRIC","display_name":"分部财务指标","aliases":["分部指标"],"source":"business_segment","target":"financial_metric","source_relation_id":"tpl_rel_hwfin_seg_fm","cardinality":"one_to_many"},
        {"name":"PRODUCT_HAS_OPERATIONAL_METRIC","display_name":"产品运营指标","aliases":["产品指标"],"source":"product","target":"operational_metric","source_relation_id":"tpl_rel_hwfin_pd_om","cardinality":"one_to_many"},
        {"name":"COMPANY_HAS_KEYPERSON","display_name":"关键人员","aliases":["高管","管理层"],"source":"listed_company","target":"key_person","source_relation_id":"tpl_rel_hwfin_co_kp","cardinality":"one_to_many"},
        {"name":"PARTICIPATES_EVENT","display_name":"参与事件","aliases":["参与","披露事件"],"source":"listed_company","target":"business_event","source_relation_id":"tpl_rel_hwfin_co_ev","cardinality":"many_to_many"},
        {"name":"COMPANY_HAS_RND_METRIC","display_name":"研发指标","aliases":["研发投入"],"source":"listed_company","target":"rnd_metric","source_relation_id":"tpl_rel_hwfin_co_rm","cardinality":"one_to_many"},
        {"name":"COMPANY_HAS_OPERATIONAL_METRIC","display_name":"公司运营指标","aliases":["运营指标"],"source":"listed_company","target":"operational_metric","source_relation_id":"tpl_rel_hwfin_co_om","cardinality":"one_to_many"},
        {"name":"LINE_HAS_METRIC","display_name":"产品线财务指标","aliases":["产品线指标"],"source":"product_line","target":"financial_metric","source_relation_id":"tpl_rel_hwfin_ln_fm","cardinality":"one_to_many"},
        {"name":"REPORT_HAS_COST_EXPENSE","display_name":"报告成本费用","aliases":["含费用","披露费用"],"source":"financial_report","target":"cost_expense","source_relation_id":"tpl_rel_hwfin_rpt_ce","cardinality":"one_to_many"},
        {"name":"SEGMENT_HAS_COST_EXPENSE","display_name":"分部成本费用","aliases":["分部费用"],"source":"business_segment","target":"cost_expense","source_relation_id":"tpl_rel_hwfin_seg_ce","cardinality":"one_to_many"},
        {"name":"COMPANY_HAS_SHAREHOLDER_RETURN","display_name":"股东回报","aliases":["股息","配售","回购"],"source":"listed_company","target":"shareholder_return","source_relation_id":"tpl_rel_hwfin_co_sr","cardinality":"one_to_many"},
        {"name":"COMPANY_HAS_ESG_METRIC","display_name":"ESG指标","aliases":["ESG","环境社会管治"],"source":"listed_company","target":"esg_metric","source_relation_id":"tpl_rel_hwfin_co_eg","cardinality":"one_to_many"},
        {"name":"COMPANY_OPERATES_IN_MARKET","display_name":"地区经营","aliases":["地区经营","分地区","市场布局"],"source":"listed_company","target":"geographic_market","source_relation_id":"tpl_rel_hwfin_co_mkt","cardinality":"one_to_many"},
        {"name":"SEGMENT_SELLS_IN_MARKET","display_name":"分地区营收","aliases":["分地区营收","地区收入"],"source":"business_segment","target":"geographic_market","source_relation_id":"tpl_rel_hwfin_seg_mkt","cardinality":"many_to_many"},
        {"name":"COMPANY_USES_CHANNEL","display_name":"销售渠道","aliases":["销售渠道","渠道布局"],"source":"listed_company","target":"sales_channel","source_relation_id":"tpl_rel_hwfin_co_ch","cardinality":"one_to_many"},
        {"name":"CHANNEL_COVERS_MARKET","display_name":"渠道覆盖","aliases":["渠道覆盖","渠道分布"],"source":"sales_channel","target":"geographic_market","source_relation_id":"tpl_rel_hwfin_ch_mkt","cardinality":"many_to_many"},
        {"name":"COMPANY_HAS_SUBSIDIARY","display_name":"子公司","aliases":["子公司","附属公司"],"source":"listed_company","target":"subsidiary","source_relation_id":"tpl_rel_hwfin_co_sub","cardinality":"one_to_many"},
        {"name":"SUBSIDIARY_LOCATED_IN_MARKET","display_name":"子公司所在地","aliases":["所在地","注册地"],"source":"subsidiary","target":"geographic_market","source_relation_id":"tpl_rel_hwfin_sub_mkt","cardinality":"many_to_one"},
        {"name":"COMPANY_FACES_RISK","display_name":"面临风险","aliases":["面临风险","风险披露"],"source":"listed_company","target":"risk_factor","source_relation_id":"tpl_rel_hwfin_co_risk","cardinality":"one_to_many"},
        {"name":"SUBSIDIARY_INVOLVED_IN_PROCEEDING","display_name":"涉及诉讼","aliases":["涉及诉讼","涉诉"],"source":"subsidiary","target":"legal_proceeding","source_relation_id":"tpl_rel_hwfin_sub_lg","cardinality":"one_to_many"},
        {"name":"COMPANY_UNDERTAKES_INITIATIVE","display_name":"开展举措","aliases":["开展举措","可持续行动"],"source":"listed_company","target":"sustainability_initiative","source_relation_id":"tpl_rel_hwfin_co_si","cardinality":"one_to_many"},
        {"name":"INITIATIVE_IMPROVES_ESG","display_name":"改善指标","aliases":["改善指标","支撑ESG"],"source":"sustainability_initiative","target":"esg_metric","source_relation_id":"tpl_rel_hwfin_si_esg","cardinality":"many_to_many"}
    ]'::jsonb,
    '[
        {"type":"entity","severity":"warning"},
        {"type":"relation","severity":"warning","rule":"relation_source_target_must_exist"},
        {"type":"value","severity":"warning","rule":"currency_unit_consistency","fields":["total_revenue","gross_profit","operating_profit","net_profit","adjusted_net_profit","rnd_expense","cost_of_sales","selling_expense","admin_expense","income_tax_expense"],"expected_unit":"万元"}
    ]'::jsonb,
    '{"case_insensitive":true,"alias_merge":true,"currency_normalization":"CNY","unit_million":true,"segment_aliases":{"手机×AIoT":["手机×AIoT","手机xAIoT","手机 AIoT","手机AIoT"],"智能电动汽车及AI等创新业务":["智能电动汽车及AI等创新业务","智能电动汽车","汽车业务","EV业务"]}}'::jsonb,
    '{
        "entity_types":[
            {"name":"listed_company","aliases":["上市公司","公司","集团","发行人"],"attributes":[
                {"name":"stock_code","type":"string","required":true},
                {"name":"company_name","type":"string","required":true},
                {"name":"exchange","type":"enum","required":false},
                {"name":"industry","type":"enum","required":false},
                {"name":"founded_date","type":"date","required":false},
                {"name":"chairman","type":"string","required":false},
                {"name":"ceo","type":"string","required":false},
                {"name":"headquarters","type":"string","required":false},
                {"name":"company_description","type":"text","required":false}
            ]},
            {"name":"financial_report","aliases":["财务报告","财报","年报","年度报告","季报"],"attributes":[
                {"name":"report_id","type":"string","required":true},
                {"name":"report_type","type":"enum","required":false},
                {"name":"fiscal_year","type":"number","required":true},
                {"name":"reporting_period","type":"string","required":false},
                {"name":"release_date","type":"date","required":false},
                {"name":"currency","type":"string","required":false},
                {"name":"total_revenue","type":"number","required":false},
                {"name":"revenue_yoy","type":"number","required":false},
                {"name":"cost_of_sales","type":"number","required":false},
                {"name":"gross_profit","type":"number","required":false},
                {"name":"gross_margin","type":"number","required":false},
                {"name":"selling_expense","type":"number","required":false},
                {"name":"admin_expense","type":"number","required":false},
                {"name":"rnd_expense","type":"number","required":false},
                {"name":"operating_profit","type":"number","required":false},
                {"name":"finance_income_net","type":"number","required":false},
                {"name":"profit_before_tax","type":"number","required":false},
                {"name":"income_tax_expense","type":"number","required":false},
                {"name":"net_profit","type":"number","required":false},
                {"name":"adjusted_net_profit","type":"number","required":false},
                {"name":"adjusted_net_profit_yoy","type":"number","required":false}
            ]},
            {"name":"business_segment","aliases":["业务分部","分部","板块","业务线"],"attributes":[
                {"name":"segment_code","type":"string","required":true},
                {"name":"segment_name","type":"string","required":true},
                {"name":"segment_type","type":"enum","required":false},
                {"name":"revenue","type":"number","required":false},
                {"name":"revenue_yoy","type":"number","required":false},
                {"name":"revenue_ratio","type":"number","required":false},
                {"name":"cost_of_sales","type":"number","required":false},
                {"name":"gross_profit","type":"number","required":false},
                {"name":"gross_margin","type":"number","required":false},
                {"name":"operating_result","type":"number","required":false},
                {"name":"adjusted_net_profit","type":"number","required":false},
                {"name":"adjusted_net_loss","type":"number","required":false},
                {"name":"segment_description","type":"text","required":false}
            ]},
            {"name":"product_line","aliases":["产品线","业务类别"],"attributes":[
                {"name":"line_code","type":"string","required":true},
                {"name":"line_name","type":"string","required":true},
                {"name":"category","type":"enum","required":false},
                {"name":"revenue","type":"number","required":false},
                {"name":"revenue_yoy","type":"number","required":false},
                {"name":"revenue_ratio","type":"number","required":false},
                {"name":"cost_of_sales","type":"number","required":false},
                {"name":"gross_profit","type":"number","required":false},
                {"name":"gross_margin","type":"number","required":false},
                {"name":"description","type":"text","required":false}
            ]},
            {"name":"product","aliases":["产品","机型","应用","服务","车型"],"attributes":[
                {"name":"product_code","type":"string","required":true},
                {"name":"product_name","type":"string","required":true},
                {"name":"product_category","type":"enum","required":false},
                {"name":"launch_date","type":"date","required":false},
                {"name":"shipment_volume","type":"number","required":false},
                {"name":"shipment_yoy","type":"number","required":false},
                {"name":"asp","type":"number","required":false},
                {"name":"asp_yoy","type":"number","required":false},
                {"name":"gross_margin","type":"number","required":false},
                {"name":"market_share","type":"number","required":false},
                {"name":"product_description","type":"text","required":false}
            ]},
            {"name":"financial_metric","aliases":["财务指标","财务数据"],"attributes":[
                {"name":"metric_code","type":"string","required":true},
                {"name":"metric_name","type":"string","required":true},
                {"name":"metric_value","type":"number","required":false},
                {"name":"metric_unit","type":"string","required":false},
                {"name":"metric_yoy","type":"number","required":false},
                {"name":"metric_period","type":"string","required":false},
                {"name":"metric_category","type":"enum","required":false}
            ]},
            {"name":"operational_metric","aliases":["运营指标","经营数据"],"attributes":[
                {"name":"metric_code","type":"string","required":true},
                {"name":"metric_name","type":"string","required":true},
                {"name":"metric_value","type":"number","required":false},
                {"name":"metric_unit","type":"string","required":false},
                {"name":"metric_yoy","type":"number","required":false},
                {"name":"metric_category","type":"enum","required":false},
                {"name":"region","type":"string","required":false},
                {"name":"metric_description","type":"text","required":false}
            ]},
            {"name":"rnd_metric","aliases":["研发指标","研发投入"],"attributes":[
                {"name":"metric_code","type":"string","required":true},
                {"name":"metric_name","type":"string","required":true},
                {"name":"metric_value","type":"number","required":false},
                {"name":"metric_unit","type":"string","required":false},
                {"name":"metric_yoy","type":"number","required":false},
                {"name":"rnd_headcount","type":"number","required":false},
                {"name":"cumulative_rnd","type":"number","required":false}
            ]},
            {"name":"cost_expense","aliases":["成本费用","费用","支出"],"attributes":[
                {"name":"expense_code","type":"string","required":true},
                {"name":"expense_name","type":"string","required":true},
                {"name":"expense_category","type":"enum","required":false},
                {"name":"expense_value","type":"number","required":false},
                {"name":"expense_yoy","type":"number","required":false},
                {"name":"expense_ratio","type":"number","required":false},
                {"name":"expense_period","type":"string","required":false}
            ]},
            {"name":"shareholder_return","aliases":["股东回报","股息","分红","配售","回购"],"attributes":[
                {"name":"return_code","type":"string","required":true},
                {"name":"return_type","type":"enum","required":true},
                {"name":"return_description","type":"string","required":false},
                {"name":"return_amount","type":"number","required":false},
                {"name":"per_share_price","type":"number","required":false},
                {"name":"return_date","type":"date","required":false},
                {"name":"return_status","type":"enum","required":false}
            ]},
            {"name":"esg_metric","aliases":["ESG指标","ESG","环境社会管治"],"attributes":[
                {"name":"metric_code","type":"string","required":true},
                {"name":"metric_name","type":"string","required":true},
                {"name":"metric_category","type":"enum","required":false},
                {"name":"metric_value","type":"number","required":false},
                {"name":"metric_unit","type":"string","required":false},
                {"name":"metric_yoy","type":"number","required":false},
                {"name":"metric_description","type":"text","required":false}
            ]},
            {"name":"key_person","aliases":["关键人员","高管","管理层","董事"],"attributes":[
                {"name":"person_name","type":"string","required":true},
                {"name":"title","type":"string","required":true},
                {"name":"role","type":"enum","required":false},
                {"name":"director_type","type":"enum","required":false},
                {"name":"affiliated_company","type":"string","required":false}
            ]},
            {"name":"business_event","aliases":["业务事件","事件","里程碑"],"attributes":[
                {"name":"event_name","type":"string","required":true},
                {"name":"event_type","type":"enum","required":false},
                {"name":"event_date","type":"date","required":false},
                {"name":"event_description","type":"text","required":false}
            ]},
            {"name":"geographic_market","aliases":["地区市场","市场","区域","地区"],"attributes":[
                {"name":"market_name","type":"string","required":true},
                {"name":"region_type","type":"enum","required":false},
                {"name":"revenue","type":"number","required":false},
                {"name":"revenue_ratio","type":"number","required":false},
                {"name":"store_count","type":"number","required":false},
                {"name":"market_description","type":"text","required":false}
            ]},
            {"name":"sales_channel","aliases":["销售渠道","渠道","门店","零售网络"],"attributes":[
                {"name":"channel_name","type":"string","required":true},
                {"name":"channel_type","type":"enum","required":false},
                {"name":"store_count","type":"number","required":false},
                {"name":"region","type":"string","required":false},
                {"name":"channel_description","type":"text","required":false}
            ]},
            {"name":"subsidiary","aliases":["子公司","附属公司","集团实体"],"attributes":[
                {"name":"subsidiary_name","type":"string","required":true},
                {"name":"location","type":"string","required":false},
                {"name":"ownership","type":"number","required":false},
                {"name":"business_scope","type":"string","required":false},
                {"name":"subsidiary_description","type":"text","required":false}
            ]},
            {"name":"risk_factor","aliases":["风险因素","风险"],"attributes":[
                {"name":"risk_name","type":"string","required":true},
                {"name":"risk_category","type":"enum","required":false},
                {"name":"risk_description","type":"text","required":false},
                {"name":"mitigation","type":"text","required":false}
            ]},
            {"name":"legal_proceeding","aliases":["法律诉讼","诉讼","监管调查","或然负债"],"attributes":[
                {"name":"case_name","type":"string","required":true},
                {"name":"jurisdiction","type":"string","required":false},
                {"name":"status","type":"enum","required":false},
                {"name":"amount_involved","type":"number","required":false},
                {"name":"case_description","type":"text","required":false}
            ]},
            {"name":"sustainability_initiative","aliases":["可持续举措","ESG举措","可持续发展"],"attributes":[
                {"name":"initiative_name","type":"string","required":true},
                {"name":"esg_pillar","type":"enum","required":false},
                {"name":"target","type":"string","required":false},
                {"name":"progress","type":"string","required":false},
                {"name":"initiative_description","type":"text","required":false}
            ]}
        ],
        "relation_types":[
            {"name":"ISSUES_REPORT","source":"listed_company","target":"financial_report"},
            {"name":"HAS_SEGMENT","source":"financial_report","target":"business_segment"},
            {"name":"SEGMENT_INCLUDES_LINE","source":"business_segment","target":"product_line"},
            {"name":"LINE_PRODUCES_PRODUCT","source":"product_line","target":"product"},
            {"name":"REPORT_HAS_FINANCIAL_METRIC","source":"financial_report","target":"financial_metric"},
            {"name":"SEGMENT_HAS_METRIC","source":"business_segment","target":"financial_metric"},
            {"name":"PRODUCT_HAS_OPERATIONAL_METRIC","source":"product","target":"operational_metric"},
            {"name":"COMPANY_HAS_KEYPERSON","source":"listed_company","target":"key_person"},
            {"name":"PARTICIPATES_EVENT","source":"listed_company","target":"business_event"},
            {"name":"COMPANY_HAS_RND_METRIC","source":"listed_company","target":"rnd_metric"},
            {"name":"COMPANY_HAS_OPERATIONAL_METRIC","source":"listed_company","target":"operational_metric"},
            {"name":"LINE_HAS_METRIC","source":"product_line","target":"financial_metric"},
            {"name":"REPORT_HAS_COST_EXPENSE","source":"financial_report","target":"cost_expense"},
            {"name":"SEGMENT_HAS_COST_EXPENSE","source":"business_segment","target":"cost_expense"},
            {"name":"COMPANY_HAS_SHAREHOLDER_RETURN","source":"listed_company","target":"shareholder_return"},
            {"name":"COMPANY_HAS_ESG_METRIC","source":"listed_company","target":"esg_metric"},
            {"name":"COMPANY_OPERATES_IN_MARKET","source":"listed_company","target":"geographic_market"},
            {"name":"SEGMENT_SELLS_IN_MARKET","source":"business_segment","target":"geographic_market"},
            {"name":"COMPANY_USES_CHANNEL","source":"listed_company","target":"sales_channel"},
            {"name":"CHANNEL_COVERS_MARKET","source":"sales_channel","target":"geographic_market"},
            {"name":"COMPANY_HAS_SUBSIDIARY","source":"listed_company","target":"subsidiary"},
            {"name":"SUBSIDIARY_LOCATED_IN_MARKET","source":"subsidiary","target":"geographic_market"},
            {"name":"COMPANY_FACES_RISK","source":"listed_company","target":"risk_factor"},
            {"name":"SUBSIDIARY_INVOLVED_IN_PROCEEDING","source":"subsidiary","target":"legal_proceeding"},
            {"name":"COMPANY_UNDERTAKES_INITIATIVE","source":"listed_company","target":"sustainability_initiative"},
            {"name":"INITIATIVE_IMPROVES_ESG","source":"sustainability_initiative","target":"esg_metric"}
        ]
    }'::jsonb,
    'active', '2026-06-24T00:00:00+00'::timestamptz
) ON CONFLICT (tenant_id, knowledge_base_id) DO NOTHING;


-- ============================================================
-- 模板领域场景联调数据，AI大模型技术报告（LLM技术报告场景）
-- 基于小米 MiMo-V2-Flash 技术报告逆向定义，
-- 覆盖 AI模型/模型架构/训练配置/基准测试/评测结果/竞品模型/
-- 后训练技术/RL基础设施/部署配置/采样参数/系统提示词/工具使用实践
-- 12 类核心本体实体与 11 类关系。
-- ============================================================

-- 1. 模板领域
INSERT INTO business_domain.template_domains (id, tenant_id, name, name_en, description, status, version, published_at, structure_hash)
VALUES ('tpl_domain_ai_tech_report', 'tenant_jonex_demo', 'AI大模型技术报告', 'LLM Technical Reports', 'AI大模型技术报告结构化抽取与分析模板领域', 'active', 2,
        '2026-06-24T00:00:00+00'::timestamptz,
        'c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5')
ON CONFLICT (id) DO NOTHING;

-- 2. 模板场景
INSERT INTO business_domain.template_scenarios (id, tenant_id, domain_id, name, description, config_json, version, published_at, structure_hash)
VALUES ('tpl_scenario_llm_tech_report', 'tenant_jonex_demo', 'tpl_domain_ai_tech_report', 'LLM技术报告', '大语言模型技术报告结构化抽取场景（基于小米MiMo-V2-Flash技术报告）', '{}'::jsonb, 2,
        '2026-06-24T00:00:00+00'::timestamptz,
        'c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5')
ON CONFLICT (id) DO NOTHING;

-- 3. 模板对象（18 个 LLM 技术报告核心本体实体）
INSERT INTO business_domain.template_objects (id, tenant_id, domain_id, scenario_id, name, description, status, ontology_code, aliases)
VALUES
 ('tpl_obj_llm_model','tenant_jonex_demo','tpl_domain_ai_tech_report','tpl_scenario_llm_tech_report','AI模型','大语言模型本体（如 MiMo-V2-Flash）','active','ai_model','["AI模型","大模型","语言模型","LLM","Model"]'::jsonb),
 ('tpl_obj_llm_arch','tenant_jonex_demo','tpl_domain_ai_tech_report','tpl_scenario_llm_tech_report','模型架构','模型架构特性（混合注意力、MTP等）','active','model_architecture','["模型架构","架构","Architecture"]'::jsonb),
 ('tpl_obj_llm_train','tenant_jonex_demo','tpl_domain_ai_tech_report','tpl_scenario_llm_tech_report','训练配置','预训练阶段配置（token数、精度、序列长度）','active','training_config','["训练配置","预训练","Training"]'::jsonb),
 ('tpl_obj_llm_bench','tenant_jonex_demo','tpl_domain_ai_tech_report','tpl_scenario_llm_tech_report','基准测试','评测基准定义（如 MMLU-Pro、SWE-Bench）','active','benchmark','["基准测试","评测基准","Benchmark"]'::jsonb),
 ('tpl_obj_llm_result','tenant_jonex_demo','tpl_domain_ai_tech_report','tpl_scenario_llm_tech_report','评测结果','模型在基准上的具体得分','active','benchmark_result','["评测结果","得分","Result","Score"]'::jsonb),
 ('tpl_obj_llm_comp','tenant_jonex_demo','tpl_domain_ai_tech_report','tpl_scenario_llm_tech_report','竞品模型','用于对比的其他模型（如 Kimi-K2、DeepSeek-V3.2）','active','competitor_model','["竞品模型","对比模型","竞品","Competitor"]'::jsonb),
 ('tpl_obj_llm_pt','tenant_jonex_demo','tpl_domain_ai_tech_report','tpl_scenario_llm_tech_report','后训练技术','后训练阶段技术（如 MOPD、Agentic RL）','active','post_training_technique','["后训练技术","训练技术","Post-Training"]'::jsonb),
 ('tpl_obj_llm_infra','tenant_jonex_demo','tpl_domain_ai_tech_report','tpl_scenario_llm_tech_report','RL基础设施','强化学习训练基础设施组件','active','rl_infrastructure','["RL基础设施","训练基础设施","Infrastructure"]'::jsonb),
 ('tpl_obj_llm_deploy','tenant_jonex_demo','tpl_domain_ai_tech_report','tpl_scenario_llm_tech_report','部署配置','推理部署配置（框架、精度、启动命令）','active','deployment_config','["部署配置","部署","Deployment"]'::jsonb),
 ('tpl_obj_llm_sample','tenant_jonex_demo','tpl_domain_ai_tech_report','tpl_scenario_llm_tech_report','采样参数','推荐采样参数（top_p、temperature等）','active','sampling_parameter','["采样参数","参数","Sampling"]'::jsonb),
 ('tpl_obj_llm_prompt','tenant_jonex_demo','tpl_domain_ai_tech_report','tpl_scenario_llm_tech_report','系统提示词','推荐的系统提示词','active','system_prompt','["系统提示词","提示词","System Prompt"]'::jsonb),
 ('tpl_obj_llm_tool','tenant_jonex_demo','tpl_domain_ai_tech_report','tpl_scenario_llm_tech_report','工具使用实践','工具调用注意事项与最佳实践','active','tool_use_practice','["工具使用实践","工具调用","Tool Use"]'::jsonb),
 ('tpl_obj_llm_org','tenant_jonex_demo','tpl_domain_ai_tech_report','tpl_scenario_llm_tech_report','研发机构','模型研发方/团队（如 Xiaomi、LLM-Core Xiaomi）','active','research_org','["研发机构","开发方","厂商","团队","Organization"]'::jsonb),
 ('tpl_obj_llm_report','tenant_jonex_demo','tpl_domain_ai_tech_report','tpl_scenario_llm_tech_report','技术报告','模型技术报告/论文本体（含 arXiv 引用）','active','technical_report','["技术报告","论文","Technical Report","Paper"]'::jsonb),
 ('tpl_obj_llm_scenario','tenant_jonex_demo','tpl_domain_ai_tech_report','tpl_scenario_llm_tech_report','应用场景','模型适用的任务/应用场景（如 数学、写作、Web开发、智能体、工具调用）','active','application_scenario','["应用场景","任务类型","场景","Use Case"]'::jsonb),
 ('tpl_obj_llm_framework','tenant_jonex_demo','tpl_domain_ai_tech_report','tpl_scenario_llm_tech_report','软件框架','推理/训练/解码软件框架（如 SGLang、Megatron-LM、DeepEP、EAGLE）','active','software_framework','["软件框架","框架","引擎","Framework"]'::jsonb),
 ('tpl_obj_llm_perf','tenant_jonex_demo','tpl_domain_ai_tech_report','tpl_scenario_llm_tech_report','性能指标','模型/架构的性能与效率指标（如 KV缓存缩减、推理加速、吞吐）','active','performance_metric','["性能指标","效率指标","Performance Metric"]'::jsonb),
 ('tpl_obj_llm_env','tenant_jonex_demo','tpl_domain_ai_tech_report','tpl_scenario_llm_tech_report','训练环境','RL/智能体训练环境与数据（如 GitHub issue 任务、K8s 集群、多模态验证器）','active','training_environment','["训练环境","训练数据","环境","Environment"]'::jsonb)
ON CONFLICT (id) DO NOTHING;

-- 4. 模板属性
INSERT INTO business_domain.template_attributes
    (id, tenant_id, template_object_id, attr_name, description, attr_type, is_primary_key, constraints_json, sort_order, ontology_code, is_required)
VALUES
    -- AI模型 ai_model
    ('tpl_attr_llm_md_name','tenant_jonex_demo','tpl_obj_llm_model','模型名称','模型名称（如 MiMo-V2-Flash）','字符串',1,'{}'::jsonb,1,'model_name',1),
    ('tpl_attr_llm_md_family','tenant_jonex_demo','tpl_obj_llm_model','模型系列','模型系列（如 MiMo）','字符串',0,'{}'::jsonb,2,'model_family',0),
    ('tpl_attr_llm_md_type','tenant_jonex_demo','tpl_obj_llm_model','模型类型','MoE/Dense','枚举',0,'{}'::jsonb,3,'model_type',0),
    ('tpl_attr_llm_md_total','tenant_jonex_demo','tpl_obj_llm_model','总参数量','模型总参数量（如 309B）','字符串',0,'{}'::jsonb,4,'total_params',0),
    ('tpl_attr_llm_md_active','tenant_jonex_demo','tpl_obj_llm_model','激活参数量','模型激活参数量（如 15B）','字符串',0,'{}'::jsonb,5,'active_params',0),
    ('tpl_attr_llm_md_ctx','tenant_jonex_demo','tpl_obj_llm_model','上下文长度','模型支持的最大上下文长度（如 256k）','字符串',0,'{}'::jsonb,6,'context_length',0),
    ('tpl_attr_llm_md_dev','tenant_jonex_demo','tpl_obj_llm_model','开发方','模型开发方（如 Xiaomi）','字符串',0,'{}'::jsonb,7,'developer',0),
    ('tpl_attr_llm_md_release','tenant_jonex_demo','tpl_obj_llm_model','发布日期','模型发布日期','日期',0,'{}'::jsonb,8,'release_date',0),
    ('tpl_attr_llm_md_hf','tenant_jonex_demo','tpl_obj_llm_model','HuggingFace地址','HuggingFace 模型地址','字符串',0,'{}'::jsonb,9,'huggingface_url',0),
    ('tpl_attr_llm_md_cutoff','tenant_jonex_demo','tpl_obj_llm_model','知识截止日期','模型知识截止日期（如 2024-12）','字符串',0,'{}'::jsonb,10,'knowledge_cutoff',0),
    ('tpl_attr_llm_md_license','tenant_jonex_demo','tpl_obj_llm_model','许可证','模型许可证类型','字符串',0,'{}'::jsonb,11,'license_type',0),
    -- 模型架构 model_architecture
    ('tpl_attr_llm_ar_name','tenant_jonex_demo','tpl_obj_llm_arch','架构名称','架构名称（如 Hybrid Sliding Window Attention）','字符串',1,'{}'::jsonb,1,'architecture_name',1),
    ('tpl_attr_llm_ar_atype','tenant_jonex_demo','tpl_obj_llm_arch','注意力类型','Hybrid/SWA/GA','枚举',0,'{}'::jsonb,2,'attention_type',0),
    ('tpl_attr_llm_ar_ratio','tenant_jonex_demo','tpl_obj_llm_arch','SWA:GA比例','SWA与GA层比例（如 5:1）','字符串',0,'{}'::jsonb,3,'swa_ga_ratio',0),
    ('tpl_attr_llm_ar_win','tenant_jonex_demo','tpl_obj_llm_arch','窗口大小','SWA窗口大小（token数）','数值',0,'{}'::jsonb,4,'window_size',0),
    ('tpl_attr_llm_ar_blocks','tenant_jonex_demo','tpl_obj_llm_arch','混合块数','混合块数量 M（如 8）','数值',0,'{}'::jsonb,5,'num_hybrid_blocks',0),
    ('tpl_attr_llm_ar_swa','tenant_jonex_demo','tpl_obj_llm_arch','每块SWA层数','每个混合块中SWA层数 N（如 5）','数值',0,'{}'::jsonb,6,'swa_layers_per_block',0),
    ('tpl_attr_llm_ar_ga','tenant_jonex_demo','tpl_obj_llm_arch','每块GA层数','每个混合块中GA层数（如 1）','数值',0,'{}'::jsonb,7,'ga_layers_per_block',0),
    ('tpl_attr_llm_ar_sink','tenant_jonex_demo','tpl_obj_llm_arch','启用sink bias','是否启用可学习注意力sink bias','布尔',0,'{}'::jsonb,8,'sink_bias_enabled',0),
    ('tpl_attr_llm_ar_mtp','tenant_jonex_demo','tpl_obj_llm_arch','启用MTP','是否启用多Token预测','布尔',0,'{}'::jsonb,9,'mtp_enabled',0),
    ('tpl_attr_llm_ar_mtp_param','tenant_jonex_demo','tpl_obj_llm_arch','MTP每块参数量','MTP模块每块参数量（如 0.33B）','字符串',0,'{}'::jsonb,10,'mtp_params_per_block',0),
    ('tpl_attr_llm_ar_mtp_struct','tenant_jonex_demo','tpl_obj_llm_arch','MTP结构','MTP模块结构（如 dense FFN + SWA）','字符串',0,'{}'::jsonb,11,'mtp_structure',0),
    ('tpl_attr_llm_ar_kv','tenant_jonex_demo','tpl_obj_llm_arch','KV缓存缩减','KV缓存缩减倍数（如 6x）','字符串',0,'{}'::jsonb,12,'kv_cache_reduction',0),
    -- 训练配置 training_config
    ('tpl_attr_llm_tr_name','tenant_jonex_demo','tpl_obj_llm_train','配置名称','训练配置名称','字符串',1,'{}'::jsonb,1,'config_name',1),
    ('tpl_attr_llm_tr_tokens','tenant_jonex_demo','tpl_obj_llm_train','训练token数','预训练token数（如 27T）','字符串',0,'{}'::jsonb,2,'training_tokens',0),
    ('tpl_attr_llm_tr_prec','tenant_jonex_demo','tpl_obj_llm_train','精度','训练精度（如 FP8 mixed）','字符串',0,'{}'::jsonb,3,'precision',0),
    ('tpl_attr_llm_tr_seq','tenant_jonex_demo','tpl_obj_llm_train','原生序列长度','原生序列长度（如 32000）','数值',0,'{}'::jsonb,4,'native_seq_length',0),
    ('tpl_attr_llm_tr_maxctx','tenant_jonex_demo','tpl_obj_llm_train','最大上下文长度','支持的最大上下文长度（如 262144）','数值',0,'{}'::jsonb,5,'max_context_length',0),
    ('tpl_attr_llm_tr_method','tenant_jonex_demo','tpl_obj_llm_train','训练方法','训练方法说明','文本',0,'{}'::jsonb,6,'training_method',0),
    -- 基准测试 benchmark
    ('tpl_attr_llm_bm_name','tenant_jonex_demo','tpl_obj_llm_bench','基准名称','基准测试名称（如 MMLU-Pro）','字符串',1,'{}'::jsonb,1,'benchmark_name',1),
    ('tpl_attr_llm_bm_cat','tenant_jonex_demo','tpl_obj_llm_bench','类别','基准类别（General/Math/Code/Chinese/Multilingual/Long Context/Reasoning/Writing/Code Agent/General Agent）','枚举',0,'{}'::jsonb,2,'category',0),
    ('tpl_attr_llm_bm_setting','tenant_jonex_demo','tpl_obj_llm_bench','设置','评测设置（如 5-shot）','字符串',0,'{}'::jsonb,3,'setting',0),
    ('tpl_attr_llm_bm_shot','tenant_jonex_demo','tpl_obj_llm_bench','shot数','few-shot 数量','数值',0,'{}'::jsonb,4,'shot_count',0),
    ('tpl_attr_llm_bm_desc','tenant_jonex_demo','tpl_obj_llm_bench','描述','基准测试说明','文本',0,'{}'::jsonb,5,'description',0),
    -- 评测结果 benchmark_result
    ('tpl_attr_llm_rs_id','tenant_jonex_demo','tpl_obj_llm_result','结果ID','评测结果唯一标识','字符串',1,'{}'::jsonb,1,'result_id',1),
    ('tpl_attr_llm_rs_model','tenant_jonex_demo','tpl_obj_llm_result','模型名称','被评测模型名称','字符串',0,'{}'::jsonb,2,'model_name',1),
    ('tpl_attr_llm_rs_bench','tenant_jonex_demo','tpl_obj_llm_result','基准名称','评测基准名称','字符串',0,'{}'::jsonb,3,'benchmark_name',1),
    ('tpl_attr_llm_rs_score','tenant_jonex_demo','tpl_obj_llm_result','得分','评测得分','数值',0,'{}'::jsonb,4,'score',1),
    ('tpl_attr_llm_rs_setting','tenant_jonex_demo','tpl_obj_llm_result','设置','评测设置','字符串',0,'{}'::jsonb,5,'setting',0),
    ('tpl_attr_llm_rs_length','tenant_jonex_demo','tpl_obj_llm_result','长度设置','长上下文评测长度（如 32K/64K/128K/256K）','字符串',0,'{}'::jsonb,6,'length',0),
    ('tpl_attr_llm_rs_phase','tenant_jonex_demo','tpl_obj_llm_result','评测阶段','base/post-training','枚举',0,'{}'::jsonb,7,'eval_phase',0),
    -- 竞品模型 competitor_model
    ('tpl_attr_llm_cp_name','tenant_jonex_demo','tpl_obj_llm_comp','模型名称','竞品模型名称（如 Kimi-K2 Thinking）','字符串',1,'{}'::jsonb,1,'model_name',1),
    ('tpl_attr_llm_cp_dev','tenant_jonex_demo','tpl_obj_llm_comp','开发方','竞品开发方','字符串',0,'{}'::jsonb,2,'developer',0),
    ('tpl_attr_llm_cp_total','tenant_jonex_demo','tpl_obj_llm_comp','总参数量','竞品总参数量','字符串',0,'{}'::jsonb,3,'total_params',0),
    ('tpl_attr_llm_cp_active','tenant_jonex_demo','tpl_obj_llm_comp','激活参数量','竞品激活参数量','字符串',0,'{}'::jsonb,4,'active_params',0),
    -- 后训练技术 post_training_technique
    ('tpl_attr_llm_pt_name','tenant_jonex_demo','tpl_obj_llm_pt','技术名称','后训练技术名称（如 MOPD）','字符串',1,'{}'::jsonb,1,'technique_name',1),
    ('tpl_attr_llm_pt_type','tenant_jonex_demo','tpl_obj_llm_pt','技术类型','distillation/rl/infrastructure','枚举',0,'{}'::jsonb,2,'technique_type',0),
    ('tpl_attr_llm_pt_desc','tenant_jonex_demo','tpl_obj_llm_pt','描述','技术描述','文本',0,'{}'::jsonb,3,'description',0),
    ('tpl_attr_llm_pt_feat','tenant_jonex_demo','tpl_obj_llm_pt','核心特性','技术核心特性说明','文本',0,'{}'::jsonb,4,'key_features',0),
    -- RL基础设施 rl_infrastructure
    ('tpl_attr_llm_if_name','tenant_jonex_demo','tpl_obj_llm_infra','组件名称','基础设施组件名称（如 Rollout Routing Replay）','字符串',1,'{}'::jsonb,1,'component_name',1),
    ('tpl_attr_llm_if_desc','tenant_jonex_demo','tpl_obj_llm_infra','描述','组件描述','文本',0,'{}'::jsonb,2,'description',0),
    ('tpl_attr_llm_if_purpose','tenant_jonex_demo','tpl_obj_llm_infra','用途','组件用途说明','文本',0,'{}'::jsonb,3,'purpose',0),
    -- 部署配置 deployment_config
    ('tpl_attr_llm_dp_name','tenant_jonex_demo','tpl_obj_llm_deploy','配置名称','部署配置名称','字符串',1,'{}'::jsonb,1,'config_name',1),
    ('tpl_attr_llm_dp_fw','tenant_jonex_demo','tpl_obj_llm_deploy','框架','推理框架（如 SGLang）','字符串',0,'{}'::jsonb,2,'framework',0),
    ('tpl_attr_llm_dp_prec','tenant_jonex_demo','tpl_obj_llm_deploy','精度','推理精度（如 FP8）','字符串',0,'{}'::jsonb,3,'precision',0),
    ('tpl_attr_llm_dp_cmd','tenant_jonex_demo','tpl_obj_llm_deploy','服务启动命令','服务启动命令','文本',0,'{}'::jsonb,4,'server_command',0),
    ('tpl_attr_llm_dp_ver','tenant_jonex_demo','tpl_obj_llm_deploy','推荐版本','推荐框架版本','字符串',0,'{}'::jsonb,5,'recommended_version',0),
    -- 采样参数 sampling_parameter
    ('tpl_attr_llm_sp_name','tenant_jonex_demo','tpl_obj_llm_sample','参数名称','采样参数名称（如 top_p、temperature）','字符串',1,'{}'::jsonb,1,'param_name',1),
    ('tpl_attr_llm_sp_val','tenant_jonex_demo','tpl_obj_llm_sample','推荐值','参数推荐值（如 0.95）','字符串',0,'{}'::jsonb,2,'recommended_value',0),
    ('tpl_attr_llm_sp_case','tenant_jonex_demo','tpl_obj_llm_sample','使用场景','参数适用场景（如 math/writing/agentic）','字符串',0,'{}'::jsonb,3,'use_case',0),
    -- 系统提示词 system_prompt
    ('tpl_attr_llm_sy_lang','tenant_jonex_demo','tpl_obj_llm_prompt','语言','提示词语言（en/zh）','枚举',1,'{}'::jsonb,1,'language',1),
    ('tpl_attr_llm_sy_content','tenant_jonex_demo','tpl_obj_llm_prompt','内容','提示词内容','文本',0,'{}'::jsonb,2,'content',1),
    ('tpl_attr_llm_sy_purpose','tenant_jonex_demo','tpl_obj_llm_prompt','用途','提示词用途说明','字符串',0,'{}'::jsonb,3,'purpose',0),
    -- 工具使用实践 tool_use_practice
    ('tpl_attr_llm_tl_name','tenant_jonex_demo','tpl_obj_llm_tool','实践名称','工具使用实践名称','字符串',1,'{}'::jsonb,1,'practice_name',1),
    ('tpl_attr_llm_tl_desc','tenant_jonex_demo','tpl_obj_llm_tool','描述','实践描述','文本',0,'{}'::jsonb,2,'description',0),
    ('tpl_attr_llm_tl_req','tenant_jonex_demo','tpl_obj_llm_tool','要求','实践要求说明','文本',0,'{}'::jsonb,3,'requirement',0),
    -- 研发机构 research_org
    ('tpl_attr_llm_org_name','tenant_jonex_demo','tpl_obj_llm_org','机构名称','研发机构/团队名称（如 Xiaomi、LLM-Core Xiaomi）','字符串',1,'{}'::jsonb,1,'org_name',1),
    ('tpl_attr_llm_org_type','tenant_jonex_demo','tpl_obj_llm_org','机构类型','公司/研究团队/实验室','枚举',0,'{}'::jsonb,2,'org_type',0),
    ('tpl_attr_llm_org_contact','tenant_jonex_demo','tpl_obj_llm_org','联系方式','邮箱/官网等联系方式（如 mimo@xiaomi.com）','字符串',0,'{}'::jsonb,3,'contact',0),
    -- 技术报告 technical_report
    ('tpl_attr_llm_rp_title','tenant_jonex_demo','tpl_obj_llm_report','报告标题','技术报告/论文标题（如 MiMo-V2-Flash Technical Report）','字符串',1,'{}'::jsonb,1,'report_title',1),
    ('tpl_attr_llm_rp_arxiv','tenant_jonex_demo','tpl_obj_llm_report','arXiv编号','arXiv 预印本编号（如 2601.02780）','字符串',0,'{}'::jsonb,2,'arxiv_id',0),
    ('tpl_attr_llm_rp_authors','tenant_jonex_demo','tpl_obj_llm_report','作者','报告作者（如 LLM-Core Xiaomi）','字符串',0,'{}'::jsonb,3,'authors',0),
    ('tpl_attr_llm_rp_year','tenant_jonex_demo','tpl_obj_llm_report','年份','报告发表年份（如 2026）','数值',0,'{}'::jsonb,4,'year',0),
    ('tpl_attr_llm_rp_url','tenant_jonex_demo','tpl_obj_llm_report','链接','报告/论文访问链接','字符串',0,'{}'::jsonb,5,'url',0),
    ('tpl_attr_llm_rp_class','tenant_jonex_demo','tpl_obj_llm_report','主题分类','arXiv 主题分类（如 cs.CL）','字符串',0,'{}'::jsonb,6,'primary_class',0),
    -- 应用场景 application_scenario
    ('tpl_attr_llm_sc_name','tenant_jonex_demo','tpl_obj_llm_scenario','场景名称','应用/任务场景名称（如 数学、写作、Web开发、智能体、工具调用）','字符串',1,'{}'::jsonb,1,'scenario_name',1),
    ('tpl_attr_llm_sc_type','tenant_jonex_demo','tpl_obj_llm_scenario','场景类型','reasoning/coding/writing/agentic/tool_use/math','枚举',0,'{}'::jsonb,2,'scenario_type',0),
    ('tpl_attr_llm_sc_desc','tenant_jonex_demo','tpl_obj_llm_scenario','描述','场景说明','文本',0,'{}'::jsonb,3,'description',0),
    -- 软件框架 software_framework
    ('tpl_attr_llm_fw_name','tenant_jonex_demo','tpl_obj_llm_framework','框架名称','软件框架名称（如 SGLang、Megatron-LM、DeepEP、EAGLE）','字符串',1,'{}'::jsonb,1,'framework_name',1),
    ('tpl_attr_llm_fw_type','tenant_jonex_demo','tpl_obj_llm_framework','框架类型','推理/训练/解码/通信','枚举',0,'{}'::jsonb,2,'framework_type',0),
    ('tpl_attr_llm_fw_version','tenant_jonex_demo','tpl_obj_llm_framework','版本','框架版本（如 sglang==0.5.6.post2...）','字符串',0,'{}'::jsonb,3,'version',0),
    ('tpl_attr_llm_fw_purpose','tenant_jonex_demo','tpl_obj_llm_framework','用途','框架用途说明','文本',0,'{}'::jsonb,4,'purpose',0),
    -- 性能指标 performance_metric
    ('tpl_attr_llm_pf_name','tenant_jonex_demo','tpl_obj_llm_perf','指标名称','性能/效率指标名称（如 KV缓存缩减、推理加速、训练吞吐）','字符串',1,'{}'::jsonb,1,'metric_name',1),
    ('tpl_attr_llm_pf_value','tenant_jonex_demo','tpl_obj_llm_perf','指标值','指标数值/倍数（如 6x、3x、70%）','字符串',0,'{}'::jsonb,2,'metric_value',0),
    ('tpl_attr_llm_pf_baseline','tenant_jonex_demo','tpl_obj_llm_perf','基线/对比','对比基线或对照对象','字符串',0,'{}'::jsonb,3,'baseline',0),
    ('tpl_attr_llm_pf_desc','tenant_jonex_demo','tpl_obj_llm_perf','描述','指标说明','文本',0,'{}'::jsonb,4,'description',0),
    -- 训练环境 training_environment
    ('tpl_attr_llm_ev_name','tenant_jonex_demo','tpl_obj_llm_env','环境名称','训练环境/数据名称（如 Code Agent 环境、WebDev 多模态验证器）','字符串',1,'{}'::jsonb,1,'env_name',1),
    ('tpl_attr_llm_ev_type','tenant_jonex_demo','tpl_obj_llm_env','环境类型','code_agent/webdev/general/dataset','枚举',0,'{}'::jsonb,2,'env_type',0),
    ('tpl_attr_llm_ev_scale','tenant_jonex_demo','tpl_obj_llm_env','规模','环境/数据规模（如 100,000 可验证任务、10,000 并发 pod）','字符串',0,'{}'::jsonb,3,'scale',0),
    ('tpl_attr_llm_ev_desc','tenant_jonex_demo','tpl_obj_llm_env','描述','环境说明','文本',0,'{}'::jsonb,4,'description',0)
ON CONFLICT (id) DO NOTHING;

-- 5. 模板关系（20 条核心关系）
INSERT INTO business_domain.template_relations
    (id, tenant_id, domain_id, scenario_id, name, description, source_object_id, target_object_id, relation_type, status, ontology_code, aliases)
VALUES
 ('tpl_rel_llm_md_arch','tenant_jonex_demo','tpl_domain_ai_tech_report','tpl_scenario_llm_tech_report','采用架构','AI模型采用的模型架构','tpl_obj_llm_model','tpl_obj_llm_arch','一对一','active','MODEL_HAS_ARCHITECTURE','["采用架构","使用架构"]'::jsonb),
 ('tpl_rel_llm_md_train','tenant_jonex_demo','tpl_domain_ai_tech_report','tpl_scenario_llm_tech_report','训练配置','AI模型的预训练配置','tpl_obj_llm_model','tpl_obj_llm_train','一对多','active','MODEL_TRAINED_WITH','["训练配置","预训练配置"]'::jsonb),
 ('tpl_rel_llm_md_result','tenant_jonex_demo','tpl_domain_ai_tech_report','tpl_scenario_llm_tech_report','评测结果','AI模型在基准上的评测结果','tpl_obj_llm_model','tpl_obj_llm_result','一对多','active','MODEL_EVALUATED_ON','["评测结果","得分"]'::jsonb),
 ('tpl_rel_llm_rs_bench','tenant_jonex_demo','tpl_domain_ai_tech_report','tpl_scenario_llm_tech_report','所属基准','评测结果对应的基准测试','tpl_obj_llm_result','tpl_obj_llm_bench','多对一','active','RESULT_MEASURED_BY','["所属基准","对应基准"]'::jsonb),
 ('tpl_rel_llm_rs_comp','tenant_jonex_demo','tpl_domain_ai_tech_report','tpl_scenario_llm_tech_report','对比竞品','评测结果对比的竞品模型','tpl_obj_llm_result','tpl_obj_llm_comp','多对多','active','RESULT_COMPARED_WITH','["对比竞品","竞品对比"]'::jsonb),
 ('tpl_rel_llm_md_pt','tenant_jonex_demo','tpl_domain_ai_tech_report','tpl_scenario_llm_tech_report','后训练技术','AI模型使用的后训练技术','tpl_obj_llm_model','tpl_obj_llm_pt','一对多','active','MODEL_USES_TECHNIQUE','["后训练技术","使用技术"]'::jsonb),
 ('tpl_rel_llm_pt_infra','tenant_jonex_demo','tpl_domain_ai_tech_report','tpl_scenario_llm_tech_report','依赖基础设施','后训练技术依赖的RL基础设施','tpl_obj_llm_pt','tpl_obj_llm_infra','多对多','active','TECHNIQUE_USES_INFRA','["依赖基础设施","使用基础设施"]'::jsonb),
 ('tpl_rel_llm_md_deploy','tenant_jonex_demo','tpl_domain_ai_tech_report','tpl_scenario_llm_tech_report','部署配置','AI模型的推理部署配置','tpl_obj_llm_model','tpl_obj_llm_deploy','一对多','active','MODEL_DEPLOYED_WITH','["部署配置","部署"]'::jsonb),
 ('tpl_rel_llm_dp_sp','tenant_jonex_demo','tpl_domain_ai_tech_report','tpl_scenario_llm_tech_report','采样参数','部署配置推荐的采样参数','tpl_obj_llm_deploy','tpl_obj_llm_sample','一对多','active','DEPLOYMENT_HAS_SAMPLING_PARAM','["采样参数","推荐参数"]'::jsonb),
 ('tpl_rel_llm_dp_sy','tenant_jonex_demo','tpl_domain_ai_tech_report','tpl_scenario_llm_tech_report','系统提示词','部署配置推荐的系统提示词','tpl_obj_llm_deploy','tpl_obj_llm_prompt','一对多','active','DEPLOYMENT_HAS_SYSTEM_PROMPT','["系统提示词","提示词"]'::jsonb),
 ('tpl_rel_llm_dp_tl','tenant_jonex_demo','tpl_domain_ai_tech_report','tpl_scenario_llm_tech_report','工具使用实践','部署配置的工具使用实践','tpl_obj_llm_deploy','tpl_obj_llm_tool','一对多','active','DEPLOYMENT_HAS_TOOL_PRACTICE','["工具使用实践","工具实践"]'::jsonb),
 ('tpl_rel_llm_md_org','tenant_jonex_demo','tpl_domain_ai_tech_report','tpl_scenario_llm_tech_report','研发方','AI模型由研发机构开发','tpl_obj_llm_model','tpl_obj_llm_org','多对一','active','MODEL_DEVELOPED_BY','["研发方","开发方","由...研发"]'::jsonb),
 ('tpl_rel_llm_org_rp','tenant_jonex_demo','tpl_domain_ai_tech_report','tpl_scenario_llm_tech_report','发布报告','研发机构发布技术报告','tpl_obj_llm_org','tpl_obj_llm_report','一对多','active','ORG_PUBLISHES_REPORT','["发布报告","发表"]'::jsonb),
 ('tpl_rel_llm_rp_md','tenant_jonex_demo','tpl_domain_ai_tech_report','tpl_scenario_llm_tech_report','描述模型','技术报告描述的AI模型','tpl_obj_llm_report','tpl_obj_llm_model','一对一','active','REPORT_DESCRIBES_MODEL','["描述模型","介绍模型"]'::jsonb),
 ('tpl_rel_llm_md_sc','tenant_jonex_demo','tpl_domain_ai_tech_report','tpl_scenario_llm_tech_report','适用场景','AI模型适用的应用/任务场景','tpl_obj_llm_model','tpl_obj_llm_scenario','多对多','active','MODEL_TARGETS_SCENARIO','["适用场景","面向场景"]'::jsonb),
 ('tpl_rel_llm_sp_sc','tenant_jonex_demo','tpl_domain_ai_tech_report','tpl_scenario_llm_tech_report','适用场景','采样参数推荐适用的场景','tpl_obj_llm_sample','tpl_obj_llm_scenario','多对多','active','SAMPLING_PARAM_FOR_SCENARIO','["适用场景","推荐场景"]'::jsonb),
 ('tpl_rel_llm_dp_fw','tenant_jonex_demo','tpl_domain_ai_tech_report','tpl_scenario_llm_tech_report','使用框架','部署配置使用的软件框架','tpl_obj_llm_deploy','tpl_obj_llm_framework','多对多','active','DEPLOYMENT_USES_FRAMEWORK','["使用框架","基于框架"]'::jsonb),
 ('tpl_rel_llm_if_fw','tenant_jonex_demo','tpl_domain_ai_tech_report','tpl_scenario_llm_tech_report','基于框架','RL基础设施构建于软件框架之上','tpl_obj_llm_infra','tpl_obj_llm_framework','多对多','active','INFRA_BUILT_ON_FRAMEWORK','["基于框架","构建于"]'::jsonb),
 ('tpl_rel_llm_ar_pf','tenant_jonex_demo','tpl_domain_ai_tech_report','tpl_scenario_llm_tech_report','性能表现','模型架构带来的性能/效率指标','tpl_obj_llm_arch','tpl_obj_llm_perf','一对多','active','ARCHITECTURE_HAS_METRIC','["性能表现","效率指标"]'::jsonb),
 ('tpl_rel_llm_pt_ev','tenant_jonex_demo','tpl_domain_ai_tech_report','tpl_scenario_llm_tech_report','使用环境','后训练技术使用的训练环境/数据','tpl_obj_llm_pt','tpl_obj_llm_env','多对多','active','TECHNIQUE_USES_ENVIRONMENT','["使用环境","依赖环境"]'::jsonb)
ON CONFLICT (id) DO NOTHING;

-- ============================================================
-- AI大模型技术报告知识库种子数据
-- ============================================================
INSERT INTO knowledge_base.knowledge_info (id, tenant_id, space_id, name, description, data_source_types, document_count, status, owner_id) VALUES
    ('kb_demo_llm_tech_report', 'tenant_jonex_demo', 'space_demo_test', 'AI大模型技术报告知识库', 'AI大模型技术报告结构化抽取（基于小米MiMo-V2-Flash技术报告）', '["file"]'::jsonb, 0, 'synced', '1')
ON CONFLICT (id) DO NOTHING;

-- 内置「文件上传」数据源
INSERT INTO knowledge_base.knowledge_data_sources
    (id, tenant_id, knowledge_base_id, access_method_id,access_type, name, config_json, sync_mode, status)
VALUES
    ('ds_demo_llm_file', 'tenant_jonex_demo', 'kb_demo_llm_tech_report', 'dam_demo_file', 'file', '文件上传', '{}'::jsonb, 'manual', 'active')
ON CONFLICT (id) DO NOTHING;

-- 内置「文件存储直连」数据源
INSERT INTO knowledge_base.knowledge_data_sources
    (id, tenant_id, knowledge_base_id, access_method_id, access_type, name, config_json, sync_mode, status)
VALUES
    ('ds_demo_llm_storage', 'tenant_jonex_demo', 'kb_demo_llm_tech_report', 'dam_demo_storage', 'storage', '文件存储直连', '{"bucket": "data-sourse", "prefix": "", "backend": "minio", "endpoint": "http://host.docker.internal:9000", "include_ext": ["pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "txt", "md", "jpg", "jpeg", "png", "gif", "bmp", "tiff", "tif", "webp", "mp3", "wav", "flac", "aac", "m4a", "ogg", "wma", "opus", "amr", "mp4", "avi", "mov", "mkv", "flv", "wmv", "webm", "m4v", "mpg", "mpeg", "3gp"], "credential_ref": "gAAAAABqYcT2YX64kUbkFm9jEv7bAJPsNEqp-E5hPooumz7l9zVxayioOhNIYekHNDCrT-6N1IFMBcOioi3lym9CF_l030gDJB69O2lnt_VwXsNSTsJq5zg="}'::jsonb, 'manual', 'active')
ON CONFLICT (id) DO NOTHING;

-- 内置「API 接入（拉取）」数据源
INSERT INTO knowledge_base.knowledge_data_sources
    (id, tenant_id, knowledge_base_id, access_method_id, access_type, name, config_json, sync_mode, status)
VALUES
    ('ds_demo_llm_api', 'tenant_jonex_demo', 'kb_demo_llm_tech_report', 'dam_demo_api', 'api', 'API 接入（拉取）', '{"auth": {"type": "bearer", "token_ref": "gAAAAABqYb_s0R4-CVHkZ_Lqz_t574xHegIohh5dC5TC4b0EKZ2gylIclpYqIzkIf6NcFRFWWdVnBoHthLrUsdq2nH5tVr_ivnny62zk5HCNISNyD5HupUU="}, "method": "GET", "endpoint": "http://host.docker.internal:8910/api/documents", "list_path": "$.data.items", "file_url_field": "url", "file_name_field": "name"}'::jsonb, 'manual', 'active')
ON CONFLICT (id) DO NOTHING;

-- 内置「API 开放（推送）」数据源
INSERT INTO knowledge_base.knowledge_data_sources
    (id, tenant_id, knowledge_base_id, access_method_id, access_type, name, config_json, sync_mode, status)
VALUES
    ('ds_demo_llm_api_push', 'tenant_jonex_demo', 'kb_demo_llm_tech_report', 'dam_api_push_demo', 'api_push', 'API 开放（推送）', '{"allowed_ext": ["pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "txt", "md", "jpg", "jpeg", "png", "gif", "bmp", "tiff", "tif", "webp", "mp3", "wav", "flac", "aac", "m4a", "ogg", "wma", "opus", "amr", "mp4", "avi", "mov", "mkv", "flv", "wmv", "webm", "m4v", "mpg", "mpeg", "3gp"], "max_file_mb": 50, "ingest_key_hash": "7f2c677abaa4838cbb8ebcf8da0634262c6135c2df4abeabca8be659d24f4c49"}'::jsonb, 'manual', 'active')
ON CONFLICT (id) DO NOTHING;

-- 内置解析器设置（document/txt/image/audio/video 五类，开箱即用；上传按文件后缀路由到对应解析器）
INSERT INTO knowledge_base.knowledge_parser_settings
    (id, tenant_id, knowledge_base_id, parser_type, parser_config_id, prompt_config_id, status, is_deleted)
VALUES
    ('ps_demo_llmtr_document', 'tenant_jonex_demo', 'kb_demo_llm_tech_report', 'document', 'document_parse',      NULL, 'active', 0),
    ('ps_demo_llmtr_txt',     'tenant_jonex_demo', 'kb_demo_llm_tech_report', 'txt',      'text_parse',          NULL, 'active', 0),
    ('ps_demo_llmtr_image',   'tenant_jonex_demo', 'kb_demo_llm_tech_report', 'image',    'image_parse',         NULL, 'active', 0),
    ('ps_demo_llmtr_audio',   'tenant_jonex_demo', 'kb_demo_llm_tech_report', 'audio',    'audio_transcribe',    NULL, 'active', 0),
    ('ps_demo_llmtr_video',   'tenant_jonex_demo', 'kb_demo_llm_tech_report', 'video',    'video_full_pipeline', NULL, 'active', 0)
ON CONFLICT (id) DO NOTHING;

-- 领域服务
INSERT INTO knowledge_base.services (id, tenant_id, space_id, name, description, domain_type, status, api_key_encrypted)
VALUES
    ('svc_demo_llm_tech_report', 'tenant_jonex_demo', 'space_demo_test', 'AI大模型技术报告领域服务', 'AI大模型技术报告解析领域服务', 'AI大模型', 'active', 'sk-llmtr-0123456789abcdef0123456789abcdef')
ON CONFLICT (id) DO NOTHING;

-- 领域服务和知识库关联关系
INSERT INTO knowledge_base.service_knowledge_bases (id, tenant_id, service_id, kb_id)
VALUES
    ('skb_demo_llmtr', 'tenant_jonex_demo', 'svc_demo_llm_tech_report', 'kb_demo_llm_tech_report')
ON CONFLICT (id) DO NOTHING;

-- 测试用 API Key
INSERT INTO knowledge_base.service_api_keys (id, tenant_id, service_id, key_prefix, key_encrypted, expires_at, is_active)
VALUES
    ('sak_llmtr_main', 'tenant_jonex_demo', 'svc_demo_llm_tech_report', 'sk', 'sk-llmtr-0123456789abcdef0123456789abcdef', '2027-12-31'::timestamp, 1),
    ('sak_llmtr_readonly', 'tenant_jonex_demo', 'svc_demo_llm_tech_report', 'sk', 'sk-ro-llmtr-a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6', '2026-12-31'::timestamp, 1),
    ('sak_llmtr_expired', 'tenant_jonex_demo', 'svc_demo_llm_tech_report', 'sk', 'sk-expired-llmtr-000000000000000000000000', '2026-01-01'::timestamp, 0)
ON CONFLICT (id) DO NOTHING;

-- 本体模板绑定（KB -> 模板场景）
INSERT INTO knowledge_base.ontology_template_bindings
    (tenant_id, knowledge_base_id, template_domain_id, template_scenario_id, source_type, status)
VALUES
    ('tenant_jonex_demo','kb_demo_llm_tech_report','tpl_domain_ai_tech_report','tpl_scenario_llm_tech_report','business_template','active')
ON CONFLICT (tenant_id, knowledge_base_id) DO NOTHING;

-- 11. 预编译本体 schema（AI大模型技术报告场景）
-- 基于小米 MiMo-V2-Flash 技术报告逆向定义，v2 在原 12 类实体/11 类关系基础上补充
-- 研发机构/技术报告/应用场景/软件框架/性能指标/训练环境 6 类实体与 9 类关系，
-- 共覆盖 18 类实体与 20 类关系。
INSERT INTO knowledge_base.ontology_compiled_schemas
    (tenant_id, knowledge_base_id, template_domain_id, template_scenario_id,
     source_type, source_version, source_hash, schema_version,
     entity_types, relation_types, constraints, disambiguation, prompt_schema,
     status, compiled_at)
VALUES (
    'tenant_jonex_demo', 'kb_demo_llm_tech_report',
    'tpl_domain_ai_tech_report', 'tpl_scenario_llm_tech_report',
    'business_template', 2, 'c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5', 2,
    '[
        {"name":"ai_model","display_name":"AI模型","aliases":["AI模型","大模型","语言模型","LLM","Model"],"source_object_id":"tpl_obj_llm_model","attributes":[
            {"name":"model_name","display_name":"模型名称","type":"string","required":true,"source_attribute_id":"tpl_attr_llm_md_name"},
            {"name":"model_family","display_name":"模型系列","type":"string","required":false,"source_attribute_id":"tpl_attr_llm_md_family"},
            {"name":"model_type","display_name":"模型类型","type":"enum","required":false,"source_attribute_id":"tpl_attr_llm_md_type"},
            {"name":"total_params","display_name":"总参数量","type":"string","required":false,"source_attribute_id":"tpl_attr_llm_md_total"},
            {"name":"active_params","display_name":"激活参数量","type":"string","required":false,"source_attribute_id":"tpl_attr_llm_md_active"},
            {"name":"context_length","display_name":"上下文长度","type":"string","required":false,"source_attribute_id":"tpl_attr_llm_md_ctx"},
            {"name":"developer","display_name":"开发方","type":"string","required":false,"source_attribute_id":"tpl_attr_llm_md_dev"},
            {"name":"release_date","display_name":"发布日期","type":"date","required":false,"source_attribute_id":"tpl_attr_llm_md_release"},
            {"name":"huggingface_url","display_name":"HuggingFace地址","type":"string","required":false,"source_attribute_id":"tpl_attr_llm_md_hf"},
            {"name":"knowledge_cutoff","display_name":"知识截止日期","type":"string","required":false,"source_attribute_id":"tpl_attr_llm_md_cutoff"},
            {"name":"license_type","display_name":"许可证","type":"string","required":false,"source_attribute_id":"tpl_attr_llm_md_license"}
        ]},
        {"name":"model_architecture","display_name":"模型架构","aliases":["模型架构","架构","Architecture"],"source_object_id":"tpl_obj_llm_arch","attributes":[
            {"name":"architecture_name","display_name":"架构名称","type":"string","required":true,"source_attribute_id":"tpl_attr_llm_ar_name"},
            {"name":"attention_type","display_name":"注意力类型","type":"enum","required":false,"source_attribute_id":"tpl_attr_llm_ar_atype"},
            {"name":"swa_ga_ratio","display_name":"SWA:GA比例","type":"string","required":false,"source_attribute_id":"tpl_attr_llm_ar_ratio"},
            {"name":"window_size","display_name":"窗口大小","type":"number","required":false,"source_attribute_id":"tpl_attr_llm_ar_win"},
            {"name":"num_hybrid_blocks","display_name":"混合块数","type":"number","required":false,"source_attribute_id":"tpl_attr_llm_ar_blocks"},
            {"name":"swa_layers_per_block","display_name":"每块SWA层数","type":"number","required":false,"source_attribute_id":"tpl_attr_llm_ar_swa"},
            {"name":"ga_layers_per_block","display_name":"每块GA层数","type":"number","required":false,"source_attribute_id":"tpl_attr_llm_ar_ga"},
            {"name":"sink_bias_enabled","display_name":"启用sink bias","type":"boolean","required":false,"source_attribute_id":"tpl_attr_llm_ar_sink"},
            {"name":"mtp_enabled","display_name":"启用MTP","type":"boolean","required":false,"source_attribute_id":"tpl_attr_llm_ar_mtp"},
            {"name":"mtp_params_per_block","display_name":"MTP每块参数量","type":"string","required":false,"source_attribute_id":"tpl_attr_llm_ar_mtp_param"},
            {"name":"mtp_structure","display_name":"MTP结构","type":"string","required":false,"source_attribute_id":"tpl_attr_llm_ar_mtp_struct"},
            {"name":"kv_cache_reduction","display_name":"KV缓存缩减","type":"string","required":false,"source_attribute_id":"tpl_attr_llm_ar_kv"}
        ]},
        {"name":"training_config","display_name":"训练配置","aliases":["训练配置","预训练","Training"],"source_object_id":"tpl_obj_llm_train","attributes":[
            {"name":"config_name","display_name":"配置名称","type":"string","required":true,"source_attribute_id":"tpl_attr_llm_tr_name"},
            {"name":"training_tokens","display_name":"训练token数","type":"string","required":false,"source_attribute_id":"tpl_attr_llm_tr_tokens"},
            {"name":"precision","display_name":"精度","type":"string","required":false,"source_attribute_id":"tpl_attr_llm_tr_prec"},
            {"name":"native_seq_length","display_name":"原生序列长度","type":"number","required":false,"source_attribute_id":"tpl_attr_llm_tr_seq"},
            {"name":"max_context_length","display_name":"最大上下文长度","type":"number","required":false,"source_attribute_id":"tpl_attr_llm_tr_maxctx"},
            {"name":"training_method","display_name":"训练方法","type":"text","required":false,"source_attribute_id":"tpl_attr_llm_tr_method"}
        ]},
        {"name":"benchmark","display_name":"基准测试","aliases":["基准测试","评测基准","Benchmark"],"source_object_id":"tpl_obj_llm_bench","attributes":[
            {"name":"benchmark_name","display_name":"基准名称","type":"string","required":true,"source_attribute_id":"tpl_attr_llm_bm_name"},
            {"name":"category","display_name":"类别","type":"enum","required":false,"source_attribute_id":"tpl_attr_llm_bm_cat"},
            {"name":"setting","display_name":"设置","type":"string","required":false,"source_attribute_id":"tpl_attr_llm_bm_setting"},
            {"name":"shot_count","display_name":"shot数","type":"number","required":false,"source_attribute_id":"tpl_attr_llm_bm_shot"},
            {"name":"description","display_name":"描述","type":"text","required":false,"source_attribute_id":"tpl_attr_llm_bm_desc"}
        ]},
        {"name":"benchmark_result","display_name":"评测结果","aliases":["评测结果","得分","Result","Score"],"source_object_id":"tpl_obj_llm_result","attributes":[
            {"name":"result_id","display_name":"结果ID","type":"string","required":true,"source_attribute_id":"tpl_attr_llm_rs_id"},
            {"name":"model_name","display_name":"模型名称","type":"string","required":true,"source_attribute_id":"tpl_attr_llm_rs_model"},
            {"name":"benchmark_name","display_name":"基准名称","type":"string","required":true,"source_attribute_id":"tpl_attr_llm_rs_bench"},
            {"name":"score","display_name":"得分","type":"number","required":true,"source_attribute_id":"tpl_attr_llm_rs_score"},
            {"name":"setting","display_name":"设置","type":"string","required":false,"source_attribute_id":"tpl_attr_llm_rs_setting"},
            {"name":"length","display_name":"长度设置","type":"string","required":false,"source_attribute_id":"tpl_attr_llm_rs_length"},
            {"name":"eval_phase","display_name":"评测阶段","type":"enum","required":false,"source_attribute_id":"tpl_attr_llm_rs_phase"}
        ]},
        {"name":"competitor_model","display_name":"竞品模型","aliases":["竞品模型","对比模型","竞品","Competitor"],"source_object_id":"tpl_obj_llm_comp","attributes":[
            {"name":"model_name","display_name":"模型名称","type":"string","required":true,"source_attribute_id":"tpl_attr_llm_cp_name"},
            {"name":"developer","display_name":"开发方","type":"string","required":false,"source_attribute_id":"tpl_attr_llm_cp_dev"},
            {"name":"total_params","display_name":"总参数量","type":"string","required":false,"source_attribute_id":"tpl_attr_llm_cp_total"},
            {"name":"active_params","display_name":"激活参数量","type":"string","required":false,"source_attribute_id":"tpl_attr_llm_cp_active"}
        ]},
        {"name":"post_training_technique","display_name":"后训练技术","aliases":["后训练技术","训练技术","Post-Training"],"source_object_id":"tpl_obj_llm_pt","attributes":[
            {"name":"technique_name","display_name":"技术名称","type":"string","required":true,"source_attribute_id":"tpl_attr_llm_pt_name"},
            {"name":"technique_type","display_name":"技术类型","type":"enum","required":false,"source_attribute_id":"tpl_attr_llm_pt_type"},
            {"name":"description","display_name":"描述","type":"text","required":false,"source_attribute_id":"tpl_attr_llm_pt_desc"},
            {"name":"key_features","display_name":"核心特性","type":"text","required":false,"source_attribute_id":"tpl_attr_llm_pt_feat"}
        ]},
        {"name":"rl_infrastructure","display_name":"RL基础设施","aliases":["RL基础设施","训练基础设施","Infrastructure"],"source_object_id":"tpl_obj_llm_infra","attributes":[
            {"name":"component_name","display_name":"组件名称","type":"string","required":true,"source_attribute_id":"tpl_attr_llm_if_name"},
            {"name":"description","display_name":"描述","type":"text","required":false,"source_attribute_id":"tpl_attr_llm_if_desc"},
            {"name":"purpose","display_name":"用途","type":"text","required":false,"source_attribute_id":"tpl_attr_llm_if_purpose"}
        ]},
        {"name":"deployment_config","display_name":"部署配置","aliases":["部署配置","部署","Deployment"],"source_object_id":"tpl_obj_llm_deploy","attributes":[
            {"name":"config_name","display_name":"配置名称","type":"string","required":true,"source_attribute_id":"tpl_attr_llm_dp_name"},
            {"name":"framework","display_name":"框架","type":"string","required":false,"source_attribute_id":"tpl_attr_llm_dp_fw"},
            {"name":"precision","display_name":"精度","type":"string","required":false,"source_attribute_id":"tpl_attr_llm_dp_prec"},
            {"name":"server_command","display_name":"服务启动命令","type":"text","required":false,"source_attribute_id":"tpl_attr_llm_dp_cmd"},
            {"name":"recommended_version","display_name":"推荐版本","type":"string","required":false,"source_attribute_id":"tpl_attr_llm_dp_ver"}
        ]},
        {"name":"sampling_parameter","display_name":"采样参数","aliases":["采样参数","参数","Sampling"],"source_object_id":"tpl_obj_llm_sample","attributes":[
            {"name":"param_name","display_name":"参数名称","type":"string","required":true,"source_attribute_id":"tpl_attr_llm_sp_name"},
            {"name":"recommended_value","display_name":"推荐值","type":"string","required":false,"source_attribute_id":"tpl_attr_llm_sp_val"},
            {"name":"use_case","display_name":"使用场景","type":"string","required":false,"source_attribute_id":"tpl_attr_llm_sp_case"}
        ]},
        {"name":"system_prompt","display_name":"系统提示词","aliases":["系统提示词","提示词","System Prompt"],"source_object_id":"tpl_obj_llm_prompt","attributes":[
            {"name":"language","display_name":"语言","type":"enum","required":true,"source_attribute_id":"tpl_attr_llm_sy_lang"},
            {"name":"content","display_name":"内容","type":"text","required":true,"source_attribute_id":"tpl_attr_llm_sy_content"},
            {"name":"purpose","display_name":"用途","type":"string","required":false,"source_attribute_id":"tpl_attr_llm_sy_purpose"}
        ]},
        {"name":"tool_use_practice","display_name":"工具使用实践","aliases":["工具使用实践","工具调用","Tool Use"],"source_object_id":"tpl_obj_llm_tool","attributes":[
            {"name":"practice_name","display_name":"实践名称","type":"string","required":true,"source_attribute_id":"tpl_attr_llm_tl_name"},
            {"name":"description","display_name":"描述","type":"text","required":false,"source_attribute_id":"tpl_attr_llm_tl_desc"},
            {"name":"requirement","display_name":"要求","type":"text","required":false,"source_attribute_id":"tpl_attr_llm_tl_req"}
        ]},
        {"name":"research_org","display_name":"研发机构","aliases":["研发机构","开发方","厂商","团队","Organization"],"source_object_id":"tpl_obj_llm_org","attributes":[
            {"name":"org_name","display_name":"机构名称","type":"string","required":true,"source_attribute_id":"tpl_attr_llm_org_name"},
            {"name":"org_type","display_name":"机构类型","type":"enum","required":false,"source_attribute_id":"tpl_attr_llm_org_type"},
            {"name":"contact","display_name":"联系方式","type":"string","required":false,"source_attribute_id":"tpl_attr_llm_org_contact"}
        ]},
        {"name":"technical_report","display_name":"技术报告","aliases":["技术报告","论文","Technical Report","Paper"],"source_object_id":"tpl_obj_llm_report","attributes":[
            {"name":"report_title","display_name":"报告标题","type":"string","required":true,"source_attribute_id":"tpl_attr_llm_rp_title"},
            {"name":"arxiv_id","display_name":"arXiv编号","type":"string","required":false,"source_attribute_id":"tpl_attr_llm_rp_arxiv"},
            {"name":"authors","display_name":"作者","type":"string","required":false,"source_attribute_id":"tpl_attr_llm_rp_authors"},
            {"name":"year","display_name":"年份","type":"number","required":false,"source_attribute_id":"tpl_attr_llm_rp_year"},
            {"name":"url","display_name":"链接","type":"string","required":false,"source_attribute_id":"tpl_attr_llm_rp_url"},
            {"name":"primary_class","display_name":"主题分类","type":"string","required":false,"source_attribute_id":"tpl_attr_llm_rp_class"}
        ]},
        {"name":"application_scenario","display_name":"应用场景","aliases":["应用场景","任务类型","场景","Use Case"],"source_object_id":"tpl_obj_llm_scenario","attributes":[
            {"name":"scenario_name","display_name":"场景名称","type":"string","required":true,"source_attribute_id":"tpl_attr_llm_sc_name"},
            {"name":"scenario_type","display_name":"场景类型","type":"enum","required":false,"source_attribute_id":"tpl_attr_llm_sc_type"},
            {"name":"description","display_name":"描述","type":"text","required":false,"source_attribute_id":"tpl_attr_llm_sc_desc"}
        ]},
        {"name":"software_framework","display_name":"软件框架","aliases":["软件框架","框架","引擎","Framework"],"source_object_id":"tpl_obj_llm_framework","attributes":[
            {"name":"framework_name","display_name":"框架名称","type":"string","required":true,"source_attribute_id":"tpl_attr_llm_fw_name"},
            {"name":"framework_type","display_name":"框架类型","type":"enum","required":false,"source_attribute_id":"tpl_attr_llm_fw_type"},
            {"name":"version","display_name":"版本","type":"string","required":false,"source_attribute_id":"tpl_attr_llm_fw_version"},
            {"name":"purpose","display_name":"用途","type":"text","required":false,"source_attribute_id":"tpl_attr_llm_fw_purpose"}
        ]},
        {"name":"performance_metric","display_name":"性能指标","aliases":["性能指标","效率指标","Performance Metric"],"source_object_id":"tpl_obj_llm_perf","attributes":[
            {"name":"metric_name","display_name":"指标名称","type":"string","required":true,"source_attribute_id":"tpl_attr_llm_pf_name"},
            {"name":"metric_value","display_name":"指标值","type":"string","required":false,"source_attribute_id":"tpl_attr_llm_pf_value"},
            {"name":"baseline","display_name":"基线/对比","type":"string","required":false,"source_attribute_id":"tpl_attr_llm_pf_baseline"},
            {"name":"description","display_name":"描述","type":"text","required":false,"source_attribute_id":"tpl_attr_llm_pf_desc"}
        ]},
        {"name":"training_environment","display_name":"训练环境","aliases":["训练环境","训练数据","环境","Environment"],"source_object_id":"tpl_obj_llm_env","attributes":[
            {"name":"env_name","display_name":"环境名称","type":"string","required":true,"source_attribute_id":"tpl_attr_llm_ev_name"},
            {"name":"env_type","display_name":"环境类型","type":"enum","required":false,"source_attribute_id":"tpl_attr_llm_ev_type"},
            {"name":"scale","display_name":"规模","type":"string","required":false,"source_attribute_id":"tpl_attr_llm_ev_scale"},
            {"name":"description","display_name":"描述","type":"text","required":false,"source_attribute_id":"tpl_attr_llm_ev_desc"}
        ]}
    ]'::jsonb,
    '[
        {"name":"MODEL_HAS_ARCHITECTURE","display_name":"采用架构","aliases":["采用架构","使用架构"],"source":"ai_model","target":"model_architecture","source_relation_id":"tpl_rel_llm_md_arch","cardinality":"one_to_one"},
        {"name":"MODEL_TRAINED_WITH","display_name":"训练配置","aliases":["训练配置","预训练配置"],"source":"ai_model","target":"training_config","source_relation_id":"tpl_rel_llm_md_train","cardinality":"one_to_many"},
        {"name":"MODEL_EVALUATED_ON","display_name":"评测结果","aliases":["评测结果","得分"],"source":"ai_model","target":"benchmark_result","source_relation_id":"tpl_rel_llm_md_result","cardinality":"one_to_many"},
        {"name":"RESULT_MEASURED_BY","display_name":"所属基准","aliases":["所属基准","对应基准"],"source":"benchmark_result","target":"benchmark","source_relation_id":"tpl_rel_llm_rs_bench","cardinality":"many_to_one"},
        {"name":"RESULT_COMPARED_WITH","display_name":"对比竞品","aliases":["对比竞品","竞品对比"],"source":"benchmark_result","target":"competitor_model","source_relation_id":"tpl_rel_llm_rs_comp","cardinality":"many_to_many"},
        {"name":"MODEL_USES_TECHNIQUE","display_name":"后训练技术","aliases":["后训练技术","使用技术"],"source":"ai_model","target":"post_training_technique","source_relation_id":"tpl_rel_llm_md_pt","cardinality":"one_to_many"},
        {"name":"TECHNIQUE_USES_INFRA","display_name":"依赖基础设施","aliases":["依赖基础设施","使用基础设施"],"source":"post_training_technique","target":"rl_infrastructure","source_relation_id":"tpl_rel_llm_pt_infra","cardinality":"many_to_many"},
        {"name":"MODEL_DEPLOYED_WITH","display_name":"部署配置","aliases":["部署配置","部署"],"source":"ai_model","target":"deployment_config","source_relation_id":"tpl_rel_llm_md_deploy","cardinality":"one_to_many"},
        {"name":"DEPLOYMENT_HAS_SAMPLING_PARAM","display_name":"采样参数","aliases":["采样参数","推荐参数"],"source":"deployment_config","target":"sampling_parameter","source_relation_id":"tpl_rel_llm_dp_sp","cardinality":"one_to_many"},
        {"name":"DEPLOYMENT_HAS_SYSTEM_PROMPT","display_name":"系统提示词","aliases":["系统提示词","提示词"],"source":"deployment_config","target":"system_prompt","source_relation_id":"tpl_rel_llm_dp_sy","cardinality":"one_to_many"},
        {"name":"DEPLOYMENT_HAS_TOOL_PRACTICE","display_name":"工具使用实践","aliases":["工具使用实践","工具实践"],"source":"deployment_config","target":"tool_use_practice","source_relation_id":"tpl_rel_llm_dp_tl","cardinality":"one_to_many"},
        {"name":"MODEL_DEVELOPED_BY","display_name":"研发方","aliases":["研发方","开发方","由...研发"],"source":"ai_model","target":"research_org","source_relation_id":"tpl_rel_llm_md_org","cardinality":"many_to_one"},
        {"name":"ORG_PUBLISHES_REPORT","display_name":"发布报告","aliases":["发布报告","发表"],"source":"research_org","target":"technical_report","source_relation_id":"tpl_rel_llm_org_rp","cardinality":"one_to_many"},
        {"name":"REPORT_DESCRIBES_MODEL","display_name":"描述模型","aliases":["描述模型","介绍模型"],"source":"technical_report","target":"ai_model","source_relation_id":"tpl_rel_llm_rp_md","cardinality":"one_to_one"},
        {"name":"MODEL_TARGETS_SCENARIO","display_name":"适用场景","aliases":["适用场景","面向场景"],"source":"ai_model","target":"application_scenario","source_relation_id":"tpl_rel_llm_md_sc","cardinality":"many_to_many"},
        {"name":"SAMPLING_PARAM_FOR_SCENARIO","display_name":"适用场景","aliases":["适用场景","推荐场景"],"source":"sampling_parameter","target":"application_scenario","source_relation_id":"tpl_rel_llm_sp_sc","cardinality":"many_to_many"},
        {"name":"DEPLOYMENT_USES_FRAMEWORK","display_name":"使用框架","aliases":["使用框架","基于框架"],"source":"deployment_config","target":"software_framework","source_relation_id":"tpl_rel_llm_dp_fw","cardinality":"many_to_many"},
        {"name":"INFRA_BUILT_ON_FRAMEWORK","display_name":"基于框架","aliases":["基于框架","构建于"],"source":"rl_infrastructure","target":"software_framework","source_relation_id":"tpl_rel_llm_if_fw","cardinality":"many_to_many"},
        {"name":"ARCHITECTURE_HAS_METRIC","display_name":"性能表现","aliases":["性能表现","效率指标"],"source":"model_architecture","target":"performance_metric","source_relation_id":"tpl_rel_llm_ar_pf","cardinality":"one_to_many"},
        {"name":"TECHNIQUE_USES_ENVIRONMENT","display_name":"使用环境","aliases":["使用环境","依赖环境"],"source":"post_training_technique","target":"training_environment","source_relation_id":"tpl_rel_llm_pt_ev","cardinality":"many_to_many"}
    ]'::jsonb,
    '[
        {"type":"entity","severity":"warning"},
        {"type":"relation","severity":"warning","rule":"relation_source_target_must_exist"},
        {"type":"value","severity":"warning","rule":"score_range_consistency","fields":["score"],"expected_range":[0,100]}
    ]'::jsonb,
    '{
        "case_insensitive":true,
        "alias_merge":true,
        "model_name_aliases":{"MiMo-V2-Flash":["MiMo-V2-Flash","MiMo V2 Flash","MiMo-V2","mimo-v2-flash"],"MiMo-V2-Flash-Base":["MiMo-V2-Flash-Base","MiMo-V2-Flash Base","MiMo-V2 Base"]},
        "benchmark_category_aliases":{"Code Agent":["Code Agent","代码智能体"],"General Agent":["General Agent","通用智能体"],"Long Context":["Long Context","长上下文"]}
    }'::jsonb,
    '{
        "entity_types":[
            {"name":"ai_model","aliases":["AI模型","大模型","语言模型","LLM","Model"],"attributes":[
                {"name":"model_name","type":"string","required":true},
                {"name":"model_family","type":"string","required":false},
                {"name":"model_type","type":"enum","required":false},
                {"name":"total_params","type":"string","required":false},
                {"name":"active_params","type":"string","required":false},
                {"name":"context_length","type":"string","required":false},
                {"name":"developer","type":"string","required":false},
                {"name":"release_date","type":"date","required":false},
                {"name":"huggingface_url","type":"string","required":false},
                {"name":"knowledge_cutoff","type":"string","required":false},
                {"name":"license_type","type":"string","required":false}
            ]},
            {"name":"model_architecture","aliases":["模型架构","架构","Architecture"],"attributes":[
                {"name":"architecture_name","type":"string","required":true},
                {"name":"attention_type","type":"enum","required":false},
                {"name":"swa_ga_ratio","type":"string","required":false},
                {"name":"window_size","type":"number","required":false},
                {"name":"num_hybrid_blocks","type":"number","required":false},
                {"name":"swa_layers_per_block","type":"number","required":false},
                {"name":"ga_layers_per_block","type":"number","required":false},
                {"name":"sink_bias_enabled","type":"boolean","required":false},
                {"name":"mtp_enabled","type":"boolean","required":false},
                {"name":"mtp_params_per_block","type":"string","required":false},
                {"name":"mtp_structure","type":"string","required":false},
                {"name":"kv_cache_reduction","type":"string","required":false}
            ]},
            {"name":"training_config","aliases":["训练配置","预训练","Training"],"attributes":[
                {"name":"config_name","type":"string","required":true},
                {"name":"training_tokens","type":"string","required":false},
                {"name":"precision","type":"string","required":false},
                {"name":"native_seq_length","type":"number","required":false},
                {"name":"max_context_length","type":"number","required":false},
                {"name":"training_method","type":"text","required":false}
            ]},
            {"name":"benchmark","aliases":["基准测试","评测基准","Benchmark"],"attributes":[
                {"name":"benchmark_name","type":"string","required":true},
                {"name":"category","type":"enum","required":false},
                {"name":"setting","type":"string","required":false},
                {"name":"shot_count","type":"number","required":false},
                {"name":"description","type":"text","required":false}
            ]},
            {"name":"benchmark_result","aliases":["评测结果","得分","Result","Score"],"attributes":[
                {"name":"result_id","type":"string","required":true},
                {"name":"model_name","type":"string","required":true},
                {"name":"benchmark_name","type":"string","required":true},
                {"name":"score","type":"number","required":true},
                {"name":"setting","type":"string","required":false},
                {"name":"length","type":"string","required":false},
                {"name":"eval_phase","type":"enum","required":false}
            ]},
            {"name":"competitor_model","aliases":["竞品模型","对比模型","竞品","Competitor"],"attributes":[
                {"name":"model_name","type":"string","required":true},
                {"name":"developer","type":"string","required":false},
                {"name":"total_params","type":"string","required":false},
                {"name":"active_params","type":"string","required":false}
            ]},
            {"name":"post_training_technique","aliases":["后训练技术","训练技术","Post-Training"],"attributes":[
                {"name":"technique_name","type":"string","required":true},
                {"name":"technique_type","type":"enum","required":false},
                {"name":"description","type":"text","required":false},
                {"name":"key_features","type":"text","required":false}
            ]},
            {"name":"rl_infrastructure","aliases":["RL基础设施","训练基础设施","Infrastructure"],"attributes":[
                {"name":"component_name","type":"string","required":true},
                {"name":"description","type":"text","required":false},
                {"name":"purpose","type":"text","required":false}
            ]},
            {"name":"deployment_config","aliases":["部署配置","部署","Deployment"],"attributes":[
                {"name":"config_name","type":"string","required":true},
                {"name":"framework","type":"string","required":false},
                {"name":"precision","type":"string","required":false},
                {"name":"server_command","type":"text","required":false},
                {"name":"recommended_version","type":"string","required":false}
            ]},
            {"name":"sampling_parameter","aliases":["采样参数","参数","Sampling"],"attributes":[
                {"name":"param_name","type":"string","required":true},
                {"name":"recommended_value","type":"string","required":false},
                {"name":"use_case","type":"string","required":false}
            ]},
            {"name":"system_prompt","aliases":["系统提示词","提示词","System Prompt"],"attributes":[
                {"name":"language","type":"enum","required":true},
                {"name":"content","type":"text","required":true},
                {"name":"purpose","type":"string","required":false}
            ]},
            {"name":"tool_use_practice","aliases":["工具使用实践","工具调用","Tool Use"],"attributes":[
                {"name":"practice_name","type":"string","required":true},
                {"name":"description","type":"text","required":false},
                {"name":"requirement","type":"text","required":false}
            ]},
            {"name":"research_org","aliases":["研发机构","开发方","厂商","团队","Organization"],"attributes":[
                {"name":"org_name","type":"string","required":true},
                {"name":"org_type","type":"enum","required":false},
                {"name":"contact","type":"string","required":false}
            ]},
            {"name":"technical_report","aliases":["技术报告","论文","Technical Report","Paper"],"attributes":[
                {"name":"report_title","type":"string","required":true},
                {"name":"arxiv_id","type":"string","required":false},
                {"name":"authors","type":"string","required":false},
                {"name":"year","type":"number","required":false},
                {"name":"url","type":"string","required":false},
                {"name":"primary_class","type":"string","required":false}
            ]},
            {"name":"application_scenario","aliases":["应用场景","任务类型","场景","Use Case"],"attributes":[
                {"name":"scenario_name","type":"string","required":true},
                {"name":"scenario_type","type":"enum","required":false},
                {"name":"description","type":"text","required":false}
            ]},
            {"name":"software_framework","aliases":["软件框架","框架","引擎","Framework"],"attributes":[
                {"name":"framework_name","type":"string","required":true},
                {"name":"framework_type","type":"enum","required":false},
                {"name":"version","type":"string","required":false},
                {"name":"purpose","type":"text","required":false}
            ]},
            {"name":"performance_metric","aliases":["性能指标","效率指标","Performance Metric"],"attributes":[
                {"name":"metric_name","type":"string","required":true},
                {"name":"metric_value","type":"string","required":false},
                {"name":"baseline","type":"string","required":false},
                {"name":"description","type":"text","required":false}
            ]},
            {"name":"training_environment","aliases":["训练环境","训练数据","环境","Environment"],"attributes":[
                {"name":"env_name","type":"string","required":true},
                {"name":"env_type","type":"enum","required":false},
                {"name":"scale","type":"string","required":false},
                {"name":"description","type":"text","required":false}
            ]}
        ],
        "relation_types":[
            {"name":"MODEL_HAS_ARCHITECTURE","source":"ai_model","target":"model_architecture"},
            {"name":"MODEL_TRAINED_WITH","source":"ai_model","target":"training_config"},
            {"name":"MODEL_EVALUATED_ON","source":"ai_model","target":"benchmark_result"},
            {"name":"RESULT_MEASURED_BY","source":"benchmark_result","target":"benchmark"},
            {"name":"RESULT_COMPARED_WITH","source":"benchmark_result","target":"competitor_model"},
            {"name":"MODEL_USES_TECHNIQUE","source":"ai_model","target":"post_training_technique"},
            {"name":"TECHNIQUE_USES_INFRA","source":"post_training_technique","target":"rl_infrastructure"},
            {"name":"MODEL_DEPLOYED_WITH","source":"ai_model","target":"deployment_config"},
            {"name":"DEPLOYMENT_HAS_SAMPLING_PARAM","source":"deployment_config","target":"sampling_parameter"},
            {"name":"DEPLOYMENT_HAS_SYSTEM_PROMPT","source":"deployment_config","target":"system_prompt"},
            {"name":"DEPLOYMENT_HAS_TOOL_PRACTICE","source":"deployment_config","target":"tool_use_practice"},
            {"name":"MODEL_DEVELOPED_BY","source":"ai_model","target":"research_org"},
            {"name":"ORG_PUBLISHES_REPORT","source":"research_org","target":"technical_report"},
            {"name":"REPORT_DESCRIBES_MODEL","source":"technical_report","target":"ai_model"},
            {"name":"MODEL_TARGETS_SCENARIO","source":"ai_model","target":"application_scenario"},
            {"name":"SAMPLING_PARAM_FOR_SCENARIO","source":"sampling_parameter","target":"application_scenario"},
            {"name":"DEPLOYMENT_USES_FRAMEWORK","source":"deployment_config","target":"software_framework"},
            {"name":"INFRA_BUILT_ON_FRAMEWORK","source":"rl_infrastructure","target":"software_framework"},
            {"name":"ARCHITECTURE_HAS_METRIC","source":"model_architecture","target":"performance_metric"},
            {"name":"TECHNIQUE_USES_ENVIRONMENT","source":"post_training_technique","target":"training_environment"}
        ]
    }'::jsonb,
    'active', '2026-06-22T00:00:00+00'::timestamptz
) ON CONFLICT (tenant_id, knowledge_base_id) DO NOTHING;

-- ============================================================
-- 悦溪平台 - business_domain.prompt_templates 数据导出
-- 导出时间: 2026-07-07 14:42:02
-- 记录数: 6
-- ============================================================

INSERT INTO business_domain.prompt_templates (id, tenant_id, space_id, name, category, scope, description, status, current_version, versions_json, created_by, created_at, updated_at)
VALUES ('seed_pt_dom_contract', 'tenant_jonex_demo', 'space_demo_test', '合同条款合规审查', '合同审查', 'domain', '识别合同中的风险条款与合规问题，给出修改建议。', '启用', '1', '[{"remark": "初始版本", "content": "请作为法律合规专家审查以下合同条款，输出：\\n- 风险条款清单（标注位置）\\n- 合规问题说明\\n- 修改建议\\n\\n合同文本：{{合同内容}}", "version": "1", "updated_at": "2026-07-06 18:42", "updated_by": "系统用户"}]'::jsonb, '系统用户', '2026-07-06 18:42:58', '2026-07-06 18:42:58')
ON CONFLICT (id) DO NOTHING;

INSERT INTO business_domain.prompt_templates (id, tenant_id, space_id, name, category, scope, description, status, current_version, versions_json, created_by, created_at, updated_at)
VALUES ('seed_pt_dom_report', 'tenant_jonex_demo', 'space_demo_test', '数据报表解读', '文档处理', 'domain', '基于数据报表自动生成业务洞察与趋势解读。', '启用', '1', '[{"remark": "初始版本", "content": "请基于以下报表数据，生成业务解读报告：\\n1. 关键指标同比/环比变化；\\n2. 异常波动归因；\\n3. 趋势预测与建议。\\n\\n报表数据：{{报表数据}}", "version": "1", "updated_at": "2026-07-06 18:42", "updated_by": "系统用户"}]'::jsonb, '系统用户', '2026-07-06 18:42:58', '2026-07-06 18:42:58')
ON CONFLICT (id) DO NOTHING;

INSERT INTO business_domain.prompt_templates (id, tenant_id, name, category, scope, description, status, current_version, versions_json, created_by, created_at, updated_at)
VALUES ('seed_pt_sys_docsum', NULL, '文档摘要生成', '文档处理', 'system', '对长文档进行自动摘要，输出核心观点与关键信息。', '启用', '1', '[{"remark": "初始版本", "content": "请阅读以下文档，生成一段不超过 200 字的摘要，包含：\\n- 核心主题\\n- 关键论点（3 条）\\n- 结论建议\\n\\n文档内容：{{文档内容}}", "version": "1", "updated_at": "2026-07-06 18:42", "updated_by": "系统用户"}]'::jsonb, '系统用户', '2026-07-06 18:42:58', '2026-07-06 18:42:58')
ON CONFLICT (id) DO NOTHING;

INSERT INTO business_domain.prompt_templates (id, tenant_id, name, category, scope, description, status, current_version, versions_json, created_by, created_at, updated_at)
VALUES ('seed_pt_sys_email', NULL, '邮件智能回复', '通用问答', 'system', '根据邮件内容生成专业、得体的回复草稿。', '启用', '1', '[{"remark": "初始版本", "content": "请根据收到的邮件内容，撰写一封得体的回复邮件，要求：\\n- 语气专业、礼貌；\\n- 覆盖对方所有问题；\\n- 200 字以内。\\n\\n收件邮件：{{邮件内容}}", "version": "1", "updated_at": "2026-07-06 18:42", "updated_by": "系统用户"}]'::jsonb, '系统用户', '2026-07-06 18:42:58', '2026-07-06 18:42:58')
ON CONFLICT (id) DO NOTHING;

INSERT INTO business_domain.prompt_templates (id, tenant_id, name, category, scope, description, status, current_version, versions_json, created_by, created_at, updated_at)
VALUES ('seed_pt_sys_qa', NULL, '智能问答助手', '通用问答', 'system', '面向通用知识问答场景，结合上下文给出准确、结构化的回答。', '启用', '1', '[{"remark": "初始版本", "content": "你是一个专业、严谨的知识问答助手。请根据以下上下文回答用户问题，要求：\\n1. 仅依据给定上下文作答，不编造信息；\\n2. 答案结构化、条理清晰；\\n3. 如上下文不足，请如实说明。\\n\\n上下文：{{检索内容}}\\n用户问题：{{用户问题}}", "version": "1", "updated_at": "2026-07-06 18:42", "updated_by": "系统用户"}]'::jsonb, '系统用户', '2026-07-06 18:42:58', '2026-07-06 18:42:58')
ON CONFLICT (id) DO NOTHING;

-- ============================================================
-- 修自增序列：种子数据指定了 ID，须对齐序列避免后续 INSERT 冲突
-- ============================================================
SELECT setval('platform.roles_id_seq', (SELECT COALESCE(MAX(id), 0) FROM platform.roles));
SELECT setval('platform.permissions_id_seq', (SELECT COALESCE(MAX(id), 0) FROM platform.permissions));
SELECT setval('platform.menus_id_seq', (SELECT COALESCE(MAX(id), 0) FROM platform.menus));


INSERT INTO jonex.knowledge_base.knowledge_info (id, tenant_id, space_id, "name", description, data_source_types, document_count, status, owner_id, created_at, updated_at, is_deleted) VALUES('b11dd30de0994178a3a4e388e6cf816e', 'tenant_jonex_demo', 'space_demo_test', '金融开源数据集', NULL, '["file"]', 0, 'synced', NULL, '2026-07-23 05:08:32.539', '2026-07-23 05:08:32.539', 0);

INSERT INTO jonex.knowledge_base.knowledge_data_sources (id, tenant_id, knowledge_base_id, access_method_id, access_type, "name", config_json, sync_mode, cron_expr, schedule_task_id, status, last_sync_at, last_sync_status, last_sync_message, document_count, is_deleted, created_at, updated_at) VALUES('fda83241-9ec4-439d-bf71-b351c3d6db49', 'tenant_jonex_demo', 'b11dd30de0994178a3a4e388e6cf816e', 'dam_demo_file', 'file', '文件上传', '{}', 'manual', NULL, NULL, 'active', NULL, NULL, NULL, 1, 0, '2026-07-23 05:08:49.340', '2026-07-23 05:08:49.340');

INSERT INTO jonex.knowledge_base.services (id, tenant_id, space_id, "name", description, domain_type, status, api_key_encrypted, created_at, updated_at, is_deleted) VALUES('68bb7dd208c54f45ac31b6fbb5d655f5', 'tenant_jonex_demo', 'space_demo_test', '金融开源数据集服务', NULL, NULL, 'active', NULL, '2026-07-23 06:14:29.743', '2026-07-23 06:14:29.743', 0);

INSERT INTO jonex.knowledge_base.service_knowledge_bases (id, tenant_id, service_id, kb_id, created_at, updated_at, is_deleted) VALUES('0fc886a5083646e188e7e1c84a95b7f7', 'tenant_jonex_demo', '68bb7dd208c54f45ac31b6fbb5d655f5', 'b11dd30de0994178a3a4e388e6cf816e', '2026-07-23 06:14:29.748', '2026-07-23 06:14:29.748', 0);


INSERT INTO jonex.knowledge_base.ontology_compiled_schemas (tenant_id, knowledge_base_id, template_domain_id, template_scenario_id, source_type, source_version, source_hash, schema_version, entity_types, relation_types, "constraints", disambiguation, prompt_schema, schema_mode, sync_status, edited_at, edited_by, status, compiled_at, created_at, updated_at) VALUES('tenant_jonex_demo', 'b11dd30de0994178a3a4e388e6cf816e', NULL, NULL, 'yaml_default', 1, NULL, 1, '[{"name": "Organization", "status": "active", "aliases": ["公司", "企业", "机构", "集团", "组织"], "attributes": [{"name": "legal_name", "type": "string", "required": false, "description": "", "display_name": "legal_name", "is_primary_key": false, "source_attribute_id": null}, {"name": "industry", "type": "string", "required": false, "description": "", "display_name": "industry", "is_primary_key": false, "source_attribute_id": null}], "description": "", "requirement": "", "display_name": "Organization", "source_object_id": null}, {"name": "Person", "status": "active", "aliases": ["人", "人员", "个人", "员工"], "attributes": [{"name": "title", "type": "string", "required": false, "description": "", "display_name": "title", "is_primary_key": false, "source_attribute_id": null}], "description": "", "requirement": "", "display_name": "Person", "source_object_id": null}, {"name": "Location", "status": "active", "aliases": ["地点", "位置", "地区", "城市"], "attributes": [{"name": "address", "type": "string", "required": false, "description": "", "display_name": "address", "is_primary_key": false, "source_attribute_id": null}], "description": "", "requirement": "", "display_name": "Location", "source_object_id": null}, {"name": "Product", "status": "active", "aliases": ["产品", "服务", "解决方案"], "attributes": [{"name": "model", "type": "string", "required": false, "description": "", "display_name": "model", "is_primary_key": false, "source_attribute_id": null}], "description": "", "requirement": "", "display_name": "Product", "source_object_id": null}, {"name": "Concept", "status": "active", "aliases": ["概念", "术语", "定义"], "attributes": [], "description": "", "requirement": "", "display_name": "Concept", "source_object_id": null}, {"name": "Method", "status": "active", "aliases": ["方法", "技术", "算法", "方法论"], "attributes": [], "description": "", "requirement": "", "display_name": "Method", "source_object_id": null}, {"name": "Event", "status": "active", "aliases": ["事件", "活动", "会议"], "attributes": [{"name": "date", "type": "string", "required": false, "description": "", "display_name": "date", "is_primary_key": false, "source_attribute_id": null}], "description": "", "requirement": "", "display_name": "Event", "source_object_id": null}]', '[{"name": "BELONGS_TO", "source": "Person", "status": "active", "target": "Organization", "aliases": [], "cardinality": "custom", "description": "", "display_name": "BELONGS_TO", "source_relation_id": null}, {"name": "PRODUCES", "source": "Organization", "status": "active", "target": "Product", "aliases": [], "cardinality": "custom", "description": "", "display_name": "PRODUCES", "source_relation_id": null}, {"name": "LOCATED_AT", "source": "Organization", "status": "active", "target": "Location", "aliases": [], "cardinality": "custom", "description": "", "display_name": "LOCATED_AT", "source_relation_id": null}, {"name": "WORKS_WITH", "source": "Person", "status": "active", "target": "Person", "aliases": [], "cardinality": "custom", "description": "", "display_name": "WORKS_WITH", "source_relation_id": null}, {"name": "USES", "source": "Organization", "status": "active", "target": "Method", "aliases": [], "cardinality": "custom", "description": "", "display_name": "USES", "source_relation_id": null}, {"name": "RELATES_TO", "source": "Concept", "status": "active", "target": "Concept", "aliases": [], "cardinality": "custom", "description": "", "display_name": "RELATES_TO", "source_relation_id": null}, {"name": "PART_OF", "source": "Concept", "status": "active", "target": "Product", "aliases": [], "cardinality": "custom", "description": "", "display_name": "PART_OF", "source_relation_id": null}, {"name": "HAS_FEATURE", "source": "Product", "status": "active", "target": "Concept", "aliases": [], "cardinality": "custom", "description": "", "display_name": "HAS_FEATURE", "source_relation_id": null}]', '[]', '{"alias_merge": true, "case_insensitive": true}', '{"entity_types": [{"name": "Organization", "aliases": ["公司", "企业", "机构", "集团", "组织"], "attributes": [{"name": "legal_name", "type": "string", "required": false}, {"name": "industry", "type": "string", "required": false}]}, {"name": "Person", "aliases": ["人", "人员", "个人", "员工"], "attributes": [{"name": "title", "type": "string", "required": false}]}, {"name": "Location", "aliases": ["地点", "位置", "地区", "城市"], "attributes": [{"name": "address", "type": "string", "required": false}]}, {"name": "Product", "aliases": ["产品", "服务", "解决方案"], "attributes": [{"name": "model", "type": "string", "required": false}]}, {"name": "Concept", "aliases": ["概念", "术语", "定义"], "attributes": []}, {"name": "Method", "aliases": ["方法", "技术", "算法", "方法论"], "attributes": []}, {"name": "Event", "aliases": ["事件", "活动", "会议"], "attributes": [{"name": "date", "type": "string", "required": false}]}], "relation_types": [{"name": "BELONGS_TO", "source": "Person", "target": "Organization"}, {"name": "PRODUCES", "source": "Organization", "target": "Product"}, {"name": "LOCATED_AT", "source": "Organization", "target": "Location"}, {"name": "WORKS_WITH", "source": "Person", "target": "Person"}, {"name": "USES", "source": "Organization", "target": "Method"}, {"name": "RELATES_TO", "source": "Concept", "target": "Concept"}, {"name": "PART_OF", "source": "Concept", "target": "Product"}, {"name": "HAS_FEATURE", "source": "Product", "target": "Concept"}]}', 'template_seeded', 'synced', NULL, NULL, 'active', '2026-07-23 13:09:04.065', '2026-07-23 05:09:04.067', '2026-07-23 05:09:04.067');

INSERT INTO jonex.knowledge_base.knowledge_parser_settings (id, tenant_id, knowledge_base_id, parser_type, parser_config_id, prompt_config_id, preprocessing_json, postprocessing_json, prompt_text, prompt_template_id, prompt_template_version, summary_prompt_text, summary_template_id, summary_template_version, tag_prompt_text, tag_template_id, tag_template_version, status, is_deleted, created_at, updated_at) VALUES('94688b83c58f461cb74af4f5f661bffd', 'tenant_jonex_demo', 'b11dd30de0994178a3a4e388e6cf816e', 'document', 'document_parse', NULL, '[]', '[]', '', NULL, NULL, '', NULL, NULL, '', NULL, NULL, 'active', 0, '2026-07-23 05:09:01.974', '2026-07-23 05:09:01.974');