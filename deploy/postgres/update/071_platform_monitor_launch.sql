-- ============================================================
-- 071_platform_monitor_launch.sql
-- 平台监控上线前置：恢复「系统监控」菜单 + 补权限码 + 绑码模板
--
-- 为什么需要
--   监控 18 条读接口已交付，但少了这一步「入口与授权」：
--   1) 006_seed_data.sql 把 navigation.systemMonitor 软删（is_deleted=1），
--      全 deploy/ 无恢复脚本 ⇒ 接口通、前端侧栏没有入口。
--      存量库还可能**整行缺失**：早期种子版本未收录该菜单，而 migrations/
--      只在数据卷首次初始化时执行，006 不会在存量库重跑；此时仅 UPDATE
--      影响 0 行，必须补插。
--   2) platform:monitor:read 的授予链按现行口径是「运维按租户绑码」；
--      034_repair_tenant_role_seed.sql 只给 demo 的「平台管理员」绑过
--      ⇒ 非 demo 租户做对了闸门也一律 403。
--
-- 本脚本做什么
--   1. 补插或恢复菜单（幂等：整行缺失则补插，已恢复的库重跑无影响）。
--   2. 幂等补 platform:monitor:read 码行（新库由 006 建；存量库由 034 补，这里再兜一次）。
--   3. demo 的平台管理员绑码（与 006 / 034 目标态一致）。
--   4. 非 demo 租户的绑码**模板**（默认注释，由运维按租户执行，见文末说明）。
--
-- 为什么非 demo 绑码不自动执行
--   现行口径：可见性只由 platform:monitor:read 决定，**不做租户隔离**。
--   谁拿到码，谁就能看全平台。自动给所有租户管理员绑码等于替运维扩大了可见范围；
--   该动作必须由运维在知晓影响面的前提下逐租户执行。
--
-- 边界
--   * 不改 006（新库目标态改在下一次种子修订中同步）。
--   * 不新增/删除任何业务数据。
--   * 全部语句可重复执行（apply.sh 重复执行安全）。
--
-- 回滚（仅撤回本脚本的直接效果；已按模板授权给其它租户的绑码需另行清理）
--   -- 菜单恢复原状（006 种子里的初始态就是软删）
--   UPDATE platform.menus SET is_deleted = 1, updated_at = NOW()
--   WHERE name = 'navigation.systemMonitor';
--   -- 撤回 demo 平台管理员的绑码
--   DELETE FROM platform.role_permissions rp
--   USING platform.roles r, platform.permissions p
--   WHERE rp.role_id = r.id AND rp.permission_id = p.id
--     AND r.tenant_id = 'tenant_jonex_demo' AND r.name = '平台管理员'
--     AND p.code = 'platform:monitor:read';
--   -- 权限码行可能由 006 / 034 建立：仅当确认本脚本是其唯一来源时才删
--   -- DELETE FROM platform.permissions WHERE code = 'platform:monitor:read';
-- ============================================================

BEGIN;

-- ------------------------------------------------------------
-- 1. 补插或恢复「系统监控」菜单
--    menus.name 没有唯一约束，用 NOT EXISTS 判重，避免重跑插出重复行。
--    父菜单取「系统运维」组的实际 id（当前库为 26）；取不到则退回顶层。
-- ------------------------------------------------------------

-- 存量库常见：种子用固定 id 插入，menus_id_seq 没有随之推进，
-- 直接 nextval 会撞 menus_pkey。先把序列校准到当前最大 id。
SELECT setval(
    'platform.menus_id_seq',
    (SELECT COALESCE(MAX(id), 1) FROM platform.menus),
    true
);

INSERT INTO platform.menus
    (parent_id, name, path, icon, sort_order, permission_code, visible, is_deleted)
SELECT
    COALESCE((
        SELECT id FROM platform.menus
        WHERE name = 'navigation.systemOperations' AND is_deleted = 0
        LIMIT 1
    ), 0),
    'navigation.systemMonitor',
    '/apps/platform-management/system-monitor',
    'DashboardOutlined',
    3,
    'platform:monitor:read',
    1,
    0
WHERE NOT EXISTS (
    SELECT 1 FROM platform.menus WHERE name = 'navigation.systemMonitor'
);

UPDATE platform.menus
SET is_deleted = 0,
    visible = 1,
    updated_at = NOW()
WHERE name = 'navigation.systemMonitor'
  AND (is_deleted <> 0 OR visible <> 1);

-- ------------------------------------------------------------
-- 2. 幂等补监控权限码（scope=platform：全平台运行状态，不做租户隔离）
-- ------------------------------------------------------------
INSERT INTO platform.permissions (code, name, resource, action, description, scope) VALUES
    ('platform:monitor:read', '查看系统监控', 'monitor', 'read',
     '平台级：全平台服务/任务/组件运行状态', 'platform')
ON CONFLICT (code) DO NOTHING;

-- ------------------------------------------------------------
-- 3. demo 平台管理员绑码（与 006 / 034 的目标态一致）
-- ------------------------------------------------------------
INSERT INTO platform.role_permissions (tenant_id, role_id, permission_id)
SELECT r.tenant_id, r.id, p.id
FROM platform.roles r
JOIN platform.permissions p ON p.code = 'platform:monitor:read'
WHERE r.tenant_id = 'tenant_jonex_demo'
  AND r.name = '平台管理员'
  AND r.is_deleted = 0
ON CONFLICT DO NOTHING;

-- ------------------------------------------------------------
-- 4. 非 demo 租户绑码模板（默认不执行，运维按需逐租户放开）
--
--    ⚠️ 执行前确认：拿到该码的用户能看**全平台**的调用/任务/Token 数据，
--       这是「权限码决定可见性、不做租户隔离」的直接后果，不是越权缺陷。
--
--    用法：把下面的 :tenant_id 换成目标租户，去掉注释后单独执行。
--
--    INSERT INTO platform.role_permissions (tenant_id, role_id, permission_id)
--    SELECT r.tenant_id, r.id, p.id
--    FROM platform.roles r
--    JOIN platform.permissions p ON p.code = 'platform:monitor:read'
--    WHERE r.tenant_id = :tenant_id
--      AND r.name = '租户管理员'      -- 或该租户自行指定的角色名
--      AND r.is_deleted = 0
--    ON CONFLICT DO NOTHING;
-- ------------------------------------------------------------

COMMIT;
