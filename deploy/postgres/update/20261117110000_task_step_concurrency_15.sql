-- Raise the former shipped default; preserve explicit non-default limits.
-- These global steps are shared by all knowledge-base types.
BEGIN;

UPDATE task_center.task_catalog_steps
   SET max_concurrency = 15, updated_at = CURRENT_TIMESTAMP
 WHERE step_code IN ('parse', 'compile') AND max_concurrency = 10;

COMMIT;
