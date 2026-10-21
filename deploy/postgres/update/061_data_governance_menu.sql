-- ============================================================
-- 第 2 期数据治理：平台管理 → 资产目录 菜单
-- 复用既有 knowledge:read，不新增权限码；只做加法 + 排序顺延。
-- 幂等：INSERT ON CONFLICT + 条件 UPDATE + COMMIT 前不变量自检，
--       重跑为空操作；全新库（006 已插同 id）同样通过。
--
-- ⚠ id 取 31 而非 30：id 30 曾被 update/046 占用（047 随菜单合并删除）。
--   若这里用 30，全新数据卷路径会 006 先插 30 → apply.sh 再跑 046 撞主键
--   → ON_ERROR_STOP 中止，047–061 全不执行（含 060 建表）。
--   全量种子对应 migrations/006_seed_data.sql（两处 id / 排序必须一致）。
-- ============================================================
BEGIN;

INSERT INTO platform.menus
    (id, parent_id, name, path, icon, app_id, sort_order, permission_code)
VALUES
    (31, 5, 'navigation.dataGovernance',
     '/apps/platform-management/data-governance/assets',
     'DatabaseOutlined', NULL, 2, 'knowledge:read')
ON CONFLICT (id) DO NOTHING;

-- 平台管理分组原有子项顺延一位（只在仍是旧值时改）
UPDATE platform.menus SET sort_order = 3, updated_at = NOW()
WHERE id = 25 AND parent_id = 5 AND sort_order = 2;   -- 账号与权限

UPDATE platform.menus SET sort_order = 4, updated_at = NOW()
WHERE id = 9 AND parent_id = 5 AND sort_order = 3;    -- 提示词与模板

UPDATE platform.menus SET sort_order = 5, updated_at = NOW()
WHERE id = 26 AND parent_id = 5 AND sort_order = 4;   -- 系统运维

SELECT setval('platform.menus_id_seq',
              (SELECT COALESCE(MAX(id), 0) FROM platform.menus));

-- 不变量自检（失败则 ROLLBACK，避免「菜单没插上/排序撞车却登记成功」）
DO $$
DECLARE
    bad_order int;
    missing int;
BEGIN
    SELECT count(*) INTO missing
    FROM platform.menus
    WHERE parent_id = 5 AND name = 'navigation.dataGovernance'
      AND path = '/apps/platform-management/data-governance/assets'
      AND is_deleted = 0;
    IF missing = 0 THEN
        RAISE EXCEPTION '061: dataGovernance menu missing or path mismatch';
    END IF;

    SELECT count(*) INTO bad_order FROM (
        SELECT sort_order FROM platform.menus
        WHERE parent_id = 5 AND is_deleted = 0
        GROUP BY sort_order HAVING count(*) > 1
    ) dup;
    IF bad_order > 0 THEN
        RAISE EXCEPTION '061: duplicate sort_order under platform management';
    END IF;
END $$;

COMMIT;
