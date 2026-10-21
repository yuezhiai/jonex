-- ============================================================
-- 072_platform_monitor_retention.sql
-- 平台监控 · 保留期清理所需的索引
--
-- 背景
--   monitor.call_log / monitor.call_attempt 是 append-only 明细表，
--   050 建表时保留了「不含清理语句」的缺口。清理任务按 created_at 分批删除：
--
--     DELETE ... WHERE id IN (SELECT id ... WHERE created_at < :cutoff
--                             ORDER BY created_at, id LIMIT :batch)
--
--   call_log 已有 050_platform_monitor.sql 建的
--   idx_monitor_call_log_created(created_at DESC)；
--   call_attempt 只有 attempt_started_at 索引，按 created_at 删会全表扫。
--   本脚本补 call_attempt 的 created_at 索引。
--
-- 边界
--   * 不改任何表结构 / 约束。
--   * 幂等，可重复执行。
--
-- 回滚
--   -- 回滚：DROP INDEX IF EXISTS monitor.idx_monitor_call_attempt_created;
--   （清理任务会退回按 created_at 全表扫，功能不受影响，只影响清理耗时。）
--
-- 对应全量基线
--   deploy/postgres/migrations/002_platform.sql → monitor.call_attempt 索引段（I11），
--   新库无需再跑本脚本。
-- ============================================================

BEGIN;

CREATE INDEX IF NOT EXISTS idx_monitor_call_attempt_created
    ON monitor.call_attempt (created_at, id);

COMMIT;
