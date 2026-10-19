-- ============================================================
-- 046_tenant_security_config.sql
-- 租户级安全配置（存量库）：新建表 + 2 个 tenant scope 权限码 + 菜单
--
-- 对应全量:
--   deploy/postgres/migrations/002_platform.sql  → 建 platform.tenant_security_configs
--   deploy/postgres/migrations/006_seed_data.sql → 插两个码 + 菜单行
-- 新库由上面两处直接播种；本脚本只服务**已经跑过它们**的存量库。
--
-- 为什么存量库要单独补
--   1) 建表：migrations/ 只在数据卷**首次初始化**时执行（docker-entrypoint-initdb.d），
--      存量库不会回头建表 —— 不补这里，读配置时会一直报「表不存在」。
--   2) 插码与授权：两个码 scope='tenant'，而 006 里两段授权块是**按 scope 驱动**的
--      （demo 的管理员角色拿全部 tenant 码；其余租户按 demo 模板复制）。
--      新库跑 006 时会被自动覆盖，但存量库当初跑 006 时这两个码还不存在，
--      ON CONFLICT DO NOTHING 也不会回头补 —— 所以这里把「插码」和「授权」一起做一遍。
--   3) 菜单：006 的菜单 INSERT 幂等，但存量库不会重跑 006，这里按同样口径补一行。
--
-- 边界（有意不做的事）
--   本脚本**不动** platform.system_configs 里既有的 session_timeout 等值 ——
--   它们继续作为「平台默认」生效。租户没配覆盖值时行为与升级前**完全一致**，
--   这正是「升级不改变任何现存租户行为」的保证。
--
-- 幂等: CREATE TABLE/INDEX IF NOT EXISTS + ON CONFLICT DO NOTHING + WHERE NOT EXISTS。
--       重复执行是空操作。
-- ============================================================

BEGIN;

-- ------------------------------------------------------------
-- 1. 租户级安全配置表（只存覆盖值；没配过的键这里没有行）
-- ------------------------------------------------------------
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

-- ------------------------------------------------------------
-- 2. 两个权限码（scope='tenant' ⇒ 租户管理员可自助）
--    resource 用 tenant_security，与平台级 system_config 分开：两个不同的页面/语义。
-- ------------------------------------------------------------
INSERT INTO platform.permissions (code, name, resource, action, description, scope) VALUES
    ('tenant:security:read', '查看安全配置', 'tenant_security', 'read', '租户级：本租户安全配置（令牌有效期/密码策略/锁定策略）查看', 'tenant'),
    ('tenant:security:write', '管理安全配置', 'tenant_security', 'write', '租户级：设置本租户安全配置覆盖值、恢复平台默认', 'tenant')
ON CONFLICT (code) DO NOTHING;

-- ------------------------------------------------------------
-- 3. 预置授权（照 043 / 034 / 036 的两段播种写法）
-- ------------------------------------------------------------

-- 3a. demo 的平台管理员 + 租户管理员 ← 全部 scope='tenant' 码
--     （存量库里其余 tenant 码早授过，这里实际只会补上 tenant:security:read/write）
INSERT INTO platform.role_permissions (tenant_id, role_id, permission_id)
SELECT r.tenant_id, r.id, p.id
FROM platform.roles r
JOIN platform.permissions p ON p.scope = 'tenant'
WHERE r.tenant_id = 'tenant_jonex_demo' AND r.name IN ('平台管理员', '租户管理员') AND r.is_deleted = 0
ON CONFLICT DO NOTHING;

-- 3b. 其余租户：按 demo 模板复制（仍只复制 scope='tenant' 码）
--     demo 的「普通用户」没拿到新码，所以这里也不会把它复制出去 —— 正是想要的。
INSERT INTO platform.role_permissions (tenant_id, role_id, permission_id)
SELECT t.id, nr.id, rp.permission_id
FROM platform.roles nr
JOIN platform.tenants t ON nr.tenant_id = t.id AND t.is_deleted = 0
JOIN platform.roles dr ON dr.tenant_id = 'tenant_jonex_demo' AND dr.name = nr.name AND dr.is_deleted = 0
JOIN platform.role_permissions rp ON rp.tenant_id = 'tenant_jonex_demo' AND rp.role_id = dr.id
JOIN platform.permissions p ON p.id = rp.permission_id
WHERE t.id <> 'tenant_jonex_demo' AND p.scope = 'tenant'
ON CONFLICT DO NOTHING;

-- ------------------------------------------------------------
-- 4. 菜单（照 006 的列；租户级码，否则租户管理员被 _filter_visible_menus 裁掉）
-- ------------------------------------------------------------
INSERT INTO platform.menus (id, parent_id, name, path, icon, app_id, sort_order, permission_code)
SELECT 30, 26, 'navigation.tenantSecurity', '/apps/platform-management/tenant-security',
       'SafetyCertificateOutlined', NULL, 4, 'tenant:security:read'
WHERE NOT EXISTS (
    SELECT 1 FROM platform.menus WHERE name = 'navigation.tenantSecurity'
);

-- 显式 id 插入后推进序列，避免后续新增菜单撞主键（006 结尾有同类 setval）
SELECT setval('platform.menus_id_seq', (SELECT COALESCE(MAX(id), 1) FROM platform.menus));

-- ------------------------------------------------------------
-- 5. 自检（只告警、不中断迁移）
-- ------------------------------------------------------------
-- ⚠️ 匹配 0 行也算成功（INSERT … SELECT 的语义），所以必须吼一声：
--    3a/3b 是**按角色名**找角色的。若这个库里没有叫「平台管理员」/「租户管理员」的角色，
--    迁移会照常跑完并登记进版本表，而 tenant:security:read/write **谁都没绑上** ——
--    租户管理员的「租户安全设置」页永远不出现，且没有任何报错线索。
DO $$
BEGIN
  IF NOT EXISTS (
      SELECT 1 FROM platform.role_permissions rp
        JOIN platform.permissions p ON p.id = rp.permission_id
       WHERE p.code = 'tenant:security:read'
  ) THEN
    RAISE WARNING '046: tenant:security:read/write 没有任何角色持有 —— 请核对是否存在名为「平台管理员」/「租户管理员」的角色（口径见本脚本抬头）';
  END IF;

  IF NOT EXISTS (
      SELECT 1 FROM platform.menus WHERE name = 'navigation.tenantSecurity'
  ) THEN
    RAISE WARNING '046: 菜单 navigation.tenantSecurity 未写入 —— 租户管理员会「接口通了但没有入口」';
  END IF;

  IF NOT EXISTS (
      SELECT 1 FROM information_schema.tables
       WHERE table_schema = 'platform' AND table_name = 'tenant_security_configs'
  ) THEN
    RAISE WARNING '046: platform.tenant_security_configs 不存在 —— 读租户安全配置会一直回落平台默认';
  END IF;
END $$;

COMMIT;
