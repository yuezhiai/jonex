-- ============================================================
-- 第 2 期数据治理：平台管理 → 资产目录 菜单
-- 复用既有 knowledge:read，不新增权限码；只做加法 + 排序顺延。
-- 幂等：NOT EXISTS 判存 + 条件 UPDATE + COMMIT 前不变量自检，
--       重跑为空操作；菜单已存在（无论 id 是多少）则不插入。
--
-- ⚠ 不再硬编码 id。原写法固定 id=31，但 migrations/018 用 MAX(id)+1
--   动态分配，全新库上会先占到 31，导致本脚本 ON CONFLICT 跳过、自检失败
--   （2026-11-04 真机踩过）。改为与 update/069 相同的动态分配写法。
-- ============================================================
BEGIN;

INSERT INTO platform.menus
    (id, parent_id, name, path, icon, app_id, sort_order, permission_code)
SELECT v.new_id,
       5,
       'navigation.dataGovernance',
       '/apps/platform-management/data-governance/assets',
       'DatabaseOutlined',
       NULL,
       2,
       'knowledge:read'
  FROM (SELECT COALESCE(MAX(id), 0) + 1 AS new_id FROM platform.menus) AS v
 WHERE NOT EXISTS (
       SELECT 1 FROM platform.menus
        WHERE parent_id = 5
          AND name = 'navigation.dataGovernance'
       -- 不限定 is_deleted：菜单被 088 软删隐藏后，重跑不得复活
       );

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
    -- 不限定 is_deleted：被 088 隐藏后（is_deleted=1）同样视为「菜单存在」
    SELECT count(*) INTO missing
    FROM platform.menus
    WHERE parent_id = 5 AND name = 'navigation.dataGovernance'
      AND path = '/apps/platform-management/data-governance/assets';
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
