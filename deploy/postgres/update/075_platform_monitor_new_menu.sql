-- ============================================================
-- 075_platform_monitor_new_menu.sql
-- 「平台监控」独立成菜单：挂在「平台管理」组下，紧跟数据运营
--
-- 为什么需要
--   产品口径：「平台监控」与旧的「系统监控」**不是同一个内容**，是两个入口。
--     平台监控 = 任务 / 调用 / Token 监控页（/apps/platform-management/platform-monitor）
--     系统监控 = 历史占位入口，留给以后自己的系统级监控页面，现在不上侧栏
--   所以平台监控必须是**新建的菜单行**，不能沿用旧行。
--
--   但 074_unify_monitor_menu_name.sql 把旧 navigation.systemMonitor 行就地改名成了
--   navigation.platformMonitor —— 旧行被消费掉了。本脚本在它之后跑，负责把两件事摆正：
--     1) 让旧「系统监控」行重新存在，但隐藏（不删、不上侧栏，以后恢复即可）；
--     2) 让「平台监控」行落在「平台管理」组 sort_order = 2，紧跟数据运营。
--
--   前端已同步：路由 path 是 platform-monitor（见 frontends/platform-management/src/
--   router/routes.config.ts，standalone 侧栏 menu.order = 4）。hosted 侧栏由
--   platform.menus 驱动，所以位置必须在这里落库，否则两种壳排法不一致。
--   旧 system-monitor 路由已在远端删除（09effbfc），本脚本不再恢复它 —— 旧菜单保持隐藏，
--   不会出现死链入口。
--
-- 幂等
--   * 旧系统监控行已存在且已隐藏时不重复插入。
--   * 平台监控已在位（父组正确且 sort_order = 2）时直接返回，重跑不会二次后移。
--
-- 注意
--   新库的菜单种子（migrations/006_seed_data.sql）本次不改，与 071 / 074 的处理方式一致，
--   留待下一次种子修订时同步目标态。
--
-- 回滚（撤回平台监控菜单，恢复到 074 之后的状态）
--   UPDATE platform.menus SET is_deleted = 1, updated_at = NOW()
--    WHERE name = 'navigation.platformMonitor';
--   UPDATE platform.menus SET sort_order = sort_order - 1, updated_at = NOW()
--    WHERE parent_id = (SELECT id FROM platform.menus
--                        WHERE name = 'navigation.platformManagement' AND is_deleted = 0 LIMIT 1)
--      AND is_deleted = 0
--      AND sort_order > 2;
-- ============================================================

BEGIN;

-- 存量库常见：种子用固定 id 插入，menus_id_seq 没有随之推进，补插时直接 nextval 会撞
-- menus_pkey。先校准（与 071 / 074 同因）。
SELECT setval(
    'platform.menus_id_seq',
    (SELECT COALESCE(MAX(id), 1) FROM platform.menus),
    true
);

DO $$
DECLARE
    v_group   BIGINT;
    v_ops     BIGINT;
    v_plat    BIGINT;
    v_parent  BIGINT;
    v_sort    INTEGER;
    v_deleted INTEGER;
    v_occupy  BIGINT;
BEGIN
    SELECT id INTO v_group
      FROM platform.menus
     WHERE name = 'navigation.platformManagement' AND is_deleted = 0
     LIMIT 1;

    IF v_group IS NULL THEN
        RAISE NOTICE '平台管理分组不存在，跳过平台监控菜单创建';
        RETURN;
    END IF;

    -- ------------------------------------------------------------
    -- 1. 旧「系统监控」：保留行，只从侧栏摘掉
    --    跑过 074 的库里这行已被改名消费，这里补回一条隐藏行，
    --    以后做系统级监控页面时直接改 path / 放开 is_deleted 即可。
    -- ------------------------------------------------------------
    UPDATE platform.menus
       SET visible = 0,
           is_deleted = 1,
           updated_at = NOW()
     WHERE name = 'navigation.systemMonitor'
       AND (is_deleted = 0 OR visible <> 0);

    IF NOT EXISTS (
        SELECT 1 FROM platform.menus WHERE name = 'navigation.systemMonitor'
    ) THEN
        SELECT id INTO v_ops
          FROM platform.menus
         WHERE name = 'navigation.systemOperations' AND is_deleted = 0
         LIMIT 1;

        INSERT INTO platform.menus
            (parent_id, name, path, icon, sort_order, permission_code, visible, is_deleted)
        VALUES
            (COALESCE(v_ops, 0),
             'navigation.systemMonitor',
             '/apps/platform-management/system-monitor',
             'DashboardOutlined',
             3,
             'platform:monitor:read',
             0,
             1);
    END IF;

    -- ------------------------------------------------------------
    -- 2. 新「平台监控」：落到「平台管理」组第 2 位
    -- ------------------------------------------------------------
    SELECT id, parent_id, sort_order, is_deleted
      INTO v_plat, v_parent, v_sort, v_deleted
      FROM platform.menus
     WHERE name = 'navigation.platformMonitor'
     LIMIT 1;

    IF v_plat IS NOT NULL AND v_parent = v_group AND v_sort = 2 AND v_deleted = 0 THEN
        RAISE NOTICE '平台监控菜单已就位，跳过';
        RETURN;
    END IF;

    -- 让出第 2 位：只在该位确实有人、且不是自己时整体后移，避免凭空多出一格
    SELECT id INTO v_occupy
      FROM platform.menus
     WHERE parent_id = v_group
       AND is_deleted = 0
       AND sort_order = 2
       AND id <> COALESCE(v_plat, -1)
     LIMIT 1;

    IF v_occupy IS NOT NULL THEN
        UPDATE platform.menus
           SET sort_order = sort_order + 1,
               updated_at = NOW()
         WHERE parent_id = v_group
           AND is_deleted = 0
           AND sort_order >= 2
           AND id <> COALESCE(v_plat, -1);
    END IF;

    IF v_plat IS NULL THEN
        INSERT INTO platform.menus
            (parent_id, name, path, icon, sort_order, permission_code, visible, is_deleted)
        VALUES
            (v_group,
             'navigation.platformMonitor',
             '/apps/platform-management/platform-monitor',
             'MonitorOutlined',
             2,
             'platform:monitor:read',
             1,
             0);
    ELSE
        UPDATE platform.menus
           SET parent_id = v_group,
               sort_order = 2,
               path = '/apps/platform-management/platform-monitor',
               icon = 'MonitorOutlined',
               visible = 1,
               is_deleted = 0,
               updated_at = NOW()
         WHERE id = v_plat;
    END IF;
END $$;

-- 权限显示名与描述统一为「平台监控」（074 已做，这里兜未跑 074 的库）
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
