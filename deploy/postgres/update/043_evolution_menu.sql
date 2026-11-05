-- ============================================================
-- 043_evolution_menu.sql
-- 演化台：系统运维分组下补菜单项（② 自进化闭环前端入口）
--
-- 对应全量种子: deploy/postgres/migrations/006_seed_data.sql（新库目标态）
--
-- 背景
--   演化台页（/apps/platform-management/evolution，T11–T16 交付）此前只有路由、
--   没有菜单行 → hosted 开发台（8081）侧边栏导航（唯一来源是
--   GET /api/v1/platform/menus/my 映射的 navItems）里没有入口，用户只能手输 URL。
--   （子应用 standalone 模式的侧边栏由 routes.config.ts 元数据驱动，与 hosted 无关。）
--
--   本批只做一件加法：系统运维组（menus.id=26）下新增菜单项「演化台」，
--   排在 systemConfig(1) / operationLog(2) / systemMonitor(3) 之后的第 4 位。
--   权限码用既有的 audit:read（与操作日志同码）——能看审计的人才能看演化台；
--   该码平台管理员已持有，无需新增权限、无需授权段。
--   注意：systemMonitor（id=28）在 006 尾部被有意软删除（占位页研发中），
--   本行排在其后但**不**加入那批隐藏，演化台保持可见。
--
-- 幂等
--   可重复执行：menus 有主键，ON CONFLICT (id) DO NOTHING；
--   重复执行时插入为空操作，setval 结果不变。
--
-- 范围
--   只做加法：一条菜单行 + 序列对齐。不新增权限码，不撤销任何已有授权。
-- ============================================================

BEGIN;

-- ------------------------------------------------------------
-- 1. 菜单项：系统运维分组（id=26）下新增「演化台」
--    id 30 = 现有最大 id(29) 之后的下一个（006 里同一行用同一 id）
-- ------------------------------------------------------------
INSERT INTO platform.menus (id, parent_id, name, path, icon, app_id, sort_order, permission_code) VALUES
    (30, 26, 'navigation.evolution', '/apps/platform-management/evolution',
     'ExperimentOutlined', NULL, 4, 'audit:read')
ON CONFLICT (id) DO NOTHING;

-- ------------------------------------------------------------
-- 2. 菜单显式 id 插入不推进序列 → 立即对齐（同 006 / 036 收尾）
-- ------------------------------------------------------------
SELECT setval('platform.menus_id_seq', (SELECT COALESCE(MAX(id), 0) FROM platform.menus));

COMMIT;

-- ------------------------------------------------------------
-- 自查（可单独执行）
--   SELECT id, parent_id, name, path, icon, sort_order, permission_code, is_deleted, visible, status
--   FROM platform.menus WHERE id = 30;
--   SELECT id, name, sort_order, is_deleted FROM platform.menus
--   WHERE parent_id = 26 ORDER BY sort_order;
-- ------------------------------------------------------------
