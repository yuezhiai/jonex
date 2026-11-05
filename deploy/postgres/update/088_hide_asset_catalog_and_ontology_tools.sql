-- ============================================================
-- 088 隐藏「资产目录」与「ontology-tools」菜单
-- 需求（2026-11-02）：两个入口不再对用户展示；保留数据与路由，仅软删菜单。
-- 幂等：重复执行为空操作。
-- ============================================================
BEGIN;

UPDATE platform.menus
   SET is_deleted = 1, updated_at = NOW()
 WHERE is_deleted = 0
   AND (
        path = '/apps/platform-management/data-governance/assets'
        OR path = '/apps/platform-management/ontology-tools'
        OR (id = 31 AND name = 'navigation.dataGovernance')
        OR (id = 33 AND name = 'navigation.ontologyTools')
   );

COMMIT;
