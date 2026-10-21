-- ============================================================
-- 074_unify_monitor_menu_name.sql
-- 统一监控菜单命名为「平台监控」
--
-- 背景
--   原「系统监控」是平台监控上线前的占位入口，本身没有独立功能；上线后沿用
--   旧菜单名，导致侧边栏显示「系统监控」、页面标题显示「平台监控」、角色权限
--   显示「查看系统监控」。产品决策统一为「平台监控」，旧入口废弃。
--
-- 做什么
--   1. 存量库把 navigation.systemMonitor 改名/改路径为 navigation.platformMonitor；
--      两者并存时删除旧行；整行缺失时补插一条。
--   2. 权限显示名与描述统一为「查看平台监控」。
--
-- 边界
--   * 不改变权限码、角色绑定与菜单可见性。
--   * 全部语句可重复执行（apply.sh 重复执行安全）。
--
-- 回滚
--   UPDATE platform.menus
--   SET name = 'navigation.systemMonitor',
--       path = '/apps/platform-management/system-monitor',
--       icon = 'DashboardOutlined',
--       updated_at = NOW()
--   WHERE name = 'navigation.platformMonitor';
--   UPDATE platform.permissions
--   SET name = '查看系统监控', updated_at = NOW()
--   WHERE code = 'platform:monitor:read';
-- ============================================================

BEGIN;

-- 存量库常见：种子用固定 id 插入，menus_id_seq 没有随之推进；
-- 下面补插菜单时若直接 nextval 会撞 menus_pkey。先校准序列（与 071 同因）。
SELECT setval(
    'platform.menus_id_seq',
    (SELECT COALESCE(MAX(id), 1) FROM platform.menus),
    true
);

-- 1. 新菜单已存在时，旧入口直接废弃，避免侧栏出现两个入口
DELETE FROM platform.menus
WHERE name = 'navigation.systemMonitor'
  AND EXISTS (
      SELECT 1 FROM platform.menus WHERE name = 'navigation.platformMonitor'
  );

-- 2. 旧行改名（同名历史重复行只保留最早一条，其余在下一步删除）
WITH keep AS (
    SELECT MIN(id) AS id
    FROM platform.menus
    WHERE name = 'navigation.systemMonitor'
)
UPDATE platform.menus AS m
SET name = 'navigation.platformMonitor',
    path = '/apps/platform-management/platform-monitor',
    icon = 'MonitorOutlined',
    is_deleted = 0,
    visible = 1,
    updated_at = NOW()
FROM keep
WHERE m.id = keep.id;

DELETE FROM platform.menus
WHERE name = 'navigation.systemMonitor';

-- 3. 整行缺失时补插（早期种子未收录该菜单的存量库）
INSERT INTO platform.menus
    (parent_id, name, path, icon, sort_order, permission_code, visible, is_deleted)
SELECT
    COALESCE((
        SELECT id FROM platform.menus
        WHERE name = 'navigation.systemOperations' AND is_deleted = 0
        LIMIT 1
    ), 0),
    'navigation.platformMonitor',
    '/apps/platform-management/platform-monitor',
    'MonitorOutlined',
    3,
    'platform:monitor:read',
    1,
    0
WHERE NOT EXISTS (
    SELECT 1 FROM platform.menus WHERE name = 'navigation.platformMonitor'
);

-- 4. 权限显示名与描述统一
INSERT INTO platform.permissions (code, name, resource, action, description, scope)
VALUES (
    'platform:monitor:read',
    '查看平台监控',
    'monitor',
    'read',
    '平台级：全平台服务/任务/组件运行状态',
    'platform'
)
ON CONFLICT (code) DO UPDATE
SET name = EXCLUDED.name,
    description = EXCLUDED.description,
    updated_at = NOW();

COMMIT;
