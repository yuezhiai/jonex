-- ============================================================
-- 演化台值同义词线：kind `synonym` + 版本链 scope `synonym`
--
-- 变更：
--   · evolution_runs.ck_evo_run_kind 加 `synonym`
--   · semantic_versions.ck_evo_version_scope / ck_evo_version_shape
--     放行 `synonym`（KB 级、`data_source_id IS NULL`；与 abstract 同形状，
--     但是**独立版本链 / 独立锁**）
--   · 归档表 semantic_versions_archive 的同口径约束同步（归档表约束名带
--     `archive` 中缀：ck_evo_version_archive_scope / _shape）
--
-- 幂等：改约束一律 DROP CONSTRAINT IF EXISTS 再 ADD，可重复执行。
-- 应用：空卷随 initdb 自动执行；存量库手工 `psql -f`（或 make db-migrate）。
-- 与 deploy/postgres/update/086_evolution_synonym_kind.sql 逐字节相同。
-- ============================================================
BEGIN;

ALTER TABLE knowledge_base.evolution_runs
    DROP CONSTRAINT IF EXISTS ck_evo_run_kind;
ALTER TABLE knowledge_base.evolution_runs
    ADD CONSTRAINT ck_evo_run_kind
    CHECK (kind IN ('alias','query_rule','relation','constraint','mapping','tool_schema','synonym'));

ALTER TABLE knowledge_base.semantic_versions
    DROP CONSTRAINT IF EXISTS ck_evo_version_scope;
ALTER TABLE knowledge_base.semantic_versions
    ADD CONSTRAINT ck_evo_version_scope
    CHECK (scope IN ('abstract','synonym','compiled'));

ALTER TABLE knowledge_base.semantic_versions
    DROP CONSTRAINT IF EXISTS ck_evo_version_shape;
ALTER TABLE knowledge_base.semantic_versions
    ADD CONSTRAINT ck_evo_version_shape
    CHECK ((scope IN ('abstract','synonym') AND data_source_id IS NULL) OR
           (scope='compiled' AND data_source_id IS NOT NULL));

ALTER TABLE knowledge_base.semantic_versions_archive
    DROP CONSTRAINT IF EXISTS ck_evo_version_archive_scope;
ALTER TABLE knowledge_base.semantic_versions_archive
    ADD CONSTRAINT ck_evo_version_archive_scope
    CHECK (scope IN ('abstract','synonym','compiled'));

ALTER TABLE knowledge_base.semantic_versions_archive
    DROP CONSTRAINT IF EXISTS ck_evo_version_archive_shape;
ALTER TABLE knowledge_base.semantic_versions_archive
    ADD CONSTRAINT ck_evo_version_archive_shape
    CHECK ((scope IN ('abstract','synonym') AND data_source_id IS NULL) OR
           (scope='compiled' AND data_source_id IS NOT NULL));

COMMIT;
