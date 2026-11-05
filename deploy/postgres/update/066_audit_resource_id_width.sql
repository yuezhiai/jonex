-- 审计 resource_id 放宽到 255：派生资产 id（`{data_source_id}:{table}.{column}`）
-- 可达数百字符，VARCHAR(64) 会让 audit ingest 整批丢弃。
-- 幂等：TYPE VARCHAR(255) 可重复执行；BEGIN/COMMIT 原子化。
-- 前置：platform.audit_logs 已存在（新库由 migrations/002_platform.sql 建表）；
-- 存量库按序执行或走 `make db-migrate`，不要跳序单独跑本文件。
BEGIN;
ALTER TABLE platform.audit_logs
  ALTER COLUMN resource_id TYPE VARCHAR(255);
COMMIT;
