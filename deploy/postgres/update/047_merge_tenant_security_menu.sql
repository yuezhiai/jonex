-- ============================================================
-- 047_merge_tenant_security_menu.sql
-- 合并「租户安全设置」入口（存量库）：
--   1) 删掉 046 插的那条独立菜单 navigation.tenantSecurity
--   2) 把「系统配置」菜单码改成**租户级**码 tenant:security:read
--
-- 对应全量:
--   deploy/postgres/migrations/006_seed_data.sql → 已无那条独立菜单；系统配置菜单码已改
-- 新库从一开始就是合并后的样子；本脚本只服务**已经跑过 046** 的存量库。
--
-- 为什么要这么改
--   前端把「租户安全设置」页并入了 /apps/platform-management/system-config：同一个路由按权限
--   分段渲染「平台信息 + 平台安全设置」(platform:config:*) 与「租户安全设置」(tenant:security:*),
--   /tenant-security 这个路由**已不存在**。
--
--   ① 删独立菜单：侧栏菜单是**后端下发**的（shell 渲染 GET /api/v1/platform/menus/my），
--      留着那行 = 侧栏挂一个点进去 404 的死入口。
--   ② 改系统配置菜单码为**两个码**（逗号分隔）：合并后这一行同时承载平台块与租户块。
--      `_filter_visible_menus` → `_menu_code_matches`（capabilities/platform/api/platform_crud.py）
--      支持逗号分隔、任一命中即可（与子应用 routes.config.ts 的 permissionCode 数组语义一致）。
--      只挂 platform:config:read → 租户管理员侧栏里没有入口（接口通了却没入口）；
--      只挂 tenant:security:read → 只持平台码的角色看不到（今天靠「平台管理员恰好持全部
--      tenant 码」侥幸成立，不该依赖它）。
--
-- 有意不做的事
--   1) **不删权限码** tenant:security:read/write：接口 /platform/tenant-security-config*
--      仍按它们鉴权（读 read / 写 write）。删了码，合并后的页面那块会既看不到也改不了。
--   2) **不动** role_permissions 里已授的这两个码：合并后页面与菜单可见性都依赖它们。
--   3) 硬删而非 is_deleted=1：该菜单是**废弃**而不是暂时隐藏，硬删后与「新库从来没有它」
--      的终态一致（两条升级路径收敛到同一结果）。platform.menus 没有被任何外键引用。
--
-- 幂等：DELETE 按 path/name 删除；UPDATE 带 `IS DISTINCT FROM` 条件 —— 重复执行都是 0 行。
-- 人工触发：make db-migrate-dry 预览 → make db-migrate 执行（登记进 public.schema_migrations）。

BEGIN;

-- 1) 删掉已废弃的独立菜单（按 path 精确匹配，兼容按 name 匹配的历史写法）
DELETE FROM platform.menus
WHERE path = '/apps/platform-management/tenant-security'
   OR name = 'navigation.tenantSecurity';

-- 2) 保留下来的唯一入口：菜单码改**两个码**（逗号分隔、任一命中即可）
UPDATE platform.menus
SET permission_code = 'platform:config:read,tenant:security:read',
    updated_at = CURRENT_TIMESTAMP
WHERE path = '/apps/platform-management/system-config'
  AND permission_code IS DISTINCT FROM 'platform:config:read,tenant:security:read';

-- 3) 自检：不变量被破坏就吼一声（静默失败会让「没入口」这种问题查起来很费劲）
DO $$
DECLARE
    remaining int;
    codes int;
    sysconfig_code text;
BEGIN
    SELECT COUNT(*) INTO remaining
    FROM platform.menus
    WHERE path = '/apps/platform-management/tenant-security'
       OR name = 'navigation.tenantSecurity';
    IF remaining <> 0 THEN
        RAISE WARNING '047: 仍残留 % 条 tenant-security 菜单行 —— 侧栏会出现指向已删路由的死入口', remaining;
    END IF;

    -- 权限码必须还在（接口鉴权在用）：缺了就是 046 没跑或被人误删
    SELECT COUNT(*) INTO codes
    FROM platform.permissions
    WHERE code IN ('tenant:security:read', 'tenant:security:write');
    IF codes <> 2 THEN
        RAISE WARNING '047: tenant:security:read/write 只找到 % 个（应为 2）—— 接口鉴权会失败，请确认 046 已应用', codes;
    END IF;

    SELECT permission_code INTO sysconfig_code
    FROM platform.menus
    WHERE path = '/apps/platform-management/system-config' AND is_deleted = 0
    LIMIT 1;
    IF sysconfig_code IS NULL
       OR sysconfig_code NOT LIKE '%platform:config:read%'
       OR sysconfig_code NOT LIKE '%tenant:security:read%' THEN
        RAISE WARNING '047: 系统配置菜单码是 %（应同时含 platform:config:read 与 tenant:security:read）—— 会有一侧角色看不到入口',
            COALESCE(sysconfig_code, '<无此菜单行>');
    END IF;

    RAISE NOTICE '047: 独立菜单已删、系统配置菜单码已挂双码；合并后的页面位于 /apps/platform-management/system-config';
END $$;

COMMIT;
