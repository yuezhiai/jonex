-- 标准侧审批流：把「本体模板场景」暴露为菜单入口（原仅能从领域模板跳转）
BEGIN;

INSERT INTO platform.menus
    (id, parent_id, name, path, icon, app_id, sort_order, visible, status, permission_code)
SELECT COALESCE(MAX(id), 0) + 1,
       1,
       'navigation.templateScenarios',
       '/apps/ecosystem-management/template-scenarios',
       'CopyOutlined',
       NULL,
       5,
       1,
       1,
       'template:read'
  FROM platform.menus
 WHERE NOT EXISTS (
       SELECT 1 FROM platform.menus
        WHERE path = '/apps/ecosystem-management/template-scenarios'
          AND is_deleted = 0
 );

COMMIT;
