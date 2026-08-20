-- [jonex] 空间权限：非法 role 收敛 + 重复行去重 + partial unique index + owner 回填
-- 设计：docs/superpowers/specs/2026-08-19-space-permission-design.md §1
--
-- 依赖：必须在 008_019_consolidated.sql 之后执行（apply.sh 按文件名序，020 > 008 自动保证）。
--   ④ owner 回填 join platform.users——users 不在 015 段清空重种的 RBAC 五表内，无冲突。
-- 幂等：全部语句可重复执行。
--
-- 顺序约束（重要）：① 收敛非法 role 必须先于 ③ 建索引——
--   历史数据可能存在同 (tenant_id, space_id, user_id) 多行且 role 互异（如 owner+viewer），
--   若先建唯一索引会直接失败，ON_ERROR_STOP=1 会让整个脚本挂掉。

-- ① 收敛非法 role：owner → manager（owner 不落 space_permissions）；其他 → viewer
UPDATE knowledge_base.space_permissions
   SET role = CASE
       WHEN role = 'owner' THEN 'manager'
       WHEN role NOT IN ('manager', 'viewer') THEN 'viewer'
       ELSE role END
 WHERE is_deleted = 0;

-- ② 去重：同 (tenant_id, space_id, user_id) 保留一行（manager 优先；同 role 保留 ctid 最小）
DELETE FROM knowledge_base.space_permissions a
 USING knowledge_base.space_permissions b
 WHERE a.tenant_id = b.tenant_id
   AND a.space_id = b.space_id
   AND a.user_id = b.user_id
   AND a.is_deleted = 0
   AND b.is_deleted = 0
   AND (CASE b.role WHEN 'manager' THEN 2 ELSE 1 END, b.ctid)
     > (CASE a.role WHEN 'manager' THEN 2 ELSE 1 END, a.ctid);

-- ③ partial unique index（is_deleted 为遗留列：两处清理均为硬删、本设计约定不使用软删；
--    WHERE is_deleted = 0 保证将来误改软删也不会撞索引）
CREATE UNIQUE INDEX IF NOT EXISTS idx_kb_spp_unique_member
    ON knowledge_base.space_permissions (tenant_id, space_id, user_id)
    WHERE is_deleted = 0;

-- ④ owner 回填：owner_id IS NULL 的空间 → 该租户第一个 admin 用户
--   （username='admin' 优先 → role='admin' → 任一活跃用户，保证每个空间必有 owner）
UPDATE knowledge_base.spaces s
   SET owner_id = sub.owner_id
  FROM (
    SELECT DISTINCT ON (u.tenant_id)
           u.tenant_id,
           CAST(u.id AS VARCHAR) AS owner_id
      FROM platform.users u
     WHERE u.is_deleted = 0
       AND u.status = 1
     ORDER BY u.tenant_id,
              (CASE WHEN u.username = 'admin' THEN 0
                    WHEN u.role = 'admin' THEN 1
                    ELSE 2 END),
              u.id
  ) sub
 WHERE s.tenant_id = sub.tenant_id
   AND s.owner_id IS NULL;

-- ⑤ 空间权限演示数据（与 006 全新库种子保持同步）：演示用户 + 角色绑定 + 默认空间成员
-- 依赖：015 段（008_019）每次执行会清空重建 RBAC 五表——本段位于 020（文件名序在其后），
--   每次 apply.sh 重跑时先被 015 清、再被本段重插，幂等。
-- 注意：users 表不在 RBAC 五表内，不被 015 清空；user_roles 绑定依赖 015 重建的 roles 数据。

-- ⑤a. 演示用户（password: admin123；role='user' 语义由 006 的 6c 规则绑定「观察者」，
--      此处存量库需自行补 6c 语义与 editor 的「知识编辑者」绑定）
INSERT INTO platform.users (tenant_id, username, password_hash, display_name, role)
VALUES
    ('tenant_jonex_demo', 'editor_demo',
     '$2b$12$IRcfNr1RSXcVINY.tBvnGefCYSiMdQLI/BaUk/ARNpVFzr0BVQhCG',
     '演示知识编辑者', 'user'),
    ('tenant_jonex_demo', 'viewer_demo',
     '$2b$12$IRcfNr1RSXcVINY.tBvnGefCYSiMdQLI/BaUk/ARNpVFzr0BVQhCG',
     '演示观察者', 'user')
ON CONFLICT DO NOTHING;

-- ⑤b. 角色绑定（幂等；015 清空重种后由本段重新插入）
INSERT INTO platform.user_roles (tenant_id, user_id, role_id)
SELECT u.tenant_id, u.id, r.id
FROM platform.users u
JOIN platform.roles r ON r.tenant_id = u.tenant_id AND r.is_deleted = 0
WHERE u.tenant_id = 'tenant_jonex_demo' AND u.is_deleted = 0
  AND ((u.username IN ('viewer_demo', 'editor_demo') AND r.name = '观察者')
       OR (u.username = 'editor_demo' AND r.name = '知识编辑者'))
ON CONFLICT DO NOTHING;

-- ⑤c. 默认空间成员（owner=admin 不落表；editor=manager、viewer=viewer）
INSERT INTO knowledge_base.space_permissions (id, tenant_id, space_id, user_id, role, created_at, updated_at)
SELECT 'spp_demo_editor', u.tenant_id, 'space_demo_test', CAST(u.id AS VARCHAR), 'manager', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
  FROM platform.users u
 WHERE u.tenant_id = 'tenant_jonex_demo' AND u.username = 'editor_demo' AND u.is_deleted = 0
UNION ALL
SELECT 'spp_demo_viewer', u.tenant_id, 'space_demo_test', CAST(u.id AS VARCHAR), 'viewer', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
  FROM platform.users u
 WHERE u.tenant_id = 'tenant_jonex_demo' AND u.username = 'viewer_demo' AND u.is_deleted = 0
ON CONFLICT DO NOTHING;

-- ⑥ 空间权限收紧（2026-08-19）：领域服务管理员角色描述同步——空间由平台/系统管理员
--    统一创建后授权管理；service:write 与空间权限完全解耦（不再含「创建和管理领域空间」）。
UPDATE platform.roles
   SET description = '管理领域服务、知识库、数据源等服务相关配置；领域空间由平台/系统管理员创建后授权管理（空间权限收紧，2026-08-19）'
 WHERE tenant_id = 'tenant_jonex_demo' AND name = '领域服务管理员'
   AND description LIKE '%可创建和管理领域空间%';

-- ⑦ 角色管理菜单权限码收敛为 role:read（2026-08-19 终版）：
--   收紧点不在菜单码而在角色矩阵——领域服务管理员不再持有 role:read（见 ⑧ 段），
--   菜单按 role:read 控制即可（仅有 role:read 的平台/系统管理员可见）。
--   无条件 SET 保证幂等（覆盖曾误收紧为 role:write 的存量库）。
UPDATE platform.menus
   SET permission_code = 'role:read'
 WHERE path = '/apps/platform-management/role-permission';

-- ⑧ 领域服务管理员移除 role:read（2026-08-19）：角色管理功能已收紧为 role:write
--    （仅平台/系统管理员），role:read 对领域服务管理员无任何可用入口（角色页读接口
--    已全部挂 role:write；用户编辑角色绑定挂 role:write）。与 006 全新库矩阵同步。
DELETE FROM platform.role_permissions rp
 WHERE rp.tenant_id = 'tenant_jonex_demo'
   AND rp.role_id = 3
   AND rp.permission_id = 5;
