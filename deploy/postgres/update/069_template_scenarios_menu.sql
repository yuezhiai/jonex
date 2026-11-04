-- 标准侧审批流：把「本体模板场景」暴露为菜单入口（原仅能从领域模板跳转）
--
-- 幂等性（2026-10-22 修复）：原写法
--   INSERT ... SELECT COALESCE(MAX(id), 0) + 1, ... FROM platform.menus
--    WHERE NOT EXISTS (菜单已存在)
-- 有**聚合无 GROUP BY** 的陷阱：WHERE 把全部行过滤掉时，聚合查询仍会返回**一行**，
-- MAX(id) 为 NULL ⇒ COALESCE 取 0 ⇒ 插 id=1，撞 menus_pkey。
-- 全新初始化的库（migrations/ 全量 DDL 已含本菜单，id=32）一定会踩到。
-- 改为先在派生表里算新 id，再用 WHERE NOT EXISTS 过滤整行：菜单在 ⇒ 零行插入。
BEGIN;

INSERT INTO platform.menus
    (id, parent_id, name, path, icon, app_id, sort_order, visible, status, permission_code)
SELECT v.new_id,
       1,
       'navigation.templateScenarios',
       '/apps/ecosystem-management/template-scenarios',
       'CopyOutlined',
       NULL,
       5,
       1,
       1,
       'template:read'
  FROM (SELECT COALESCE(MAX(id), 0) + 1 AS new_id FROM platform.menus) AS v
 WHERE NOT EXISTS (
       SELECT 1 FROM platform.menus
        WHERE path = '/apps/ecosystem-management/template-scenarios'
          AND is_deleted = 0
 );

COMMIT;
