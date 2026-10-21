-- ============================================================
-- 051_monitor_call_trigger_type.sql
-- 平台监控 · 调用明细补「触发方」列（存量库）：monitor.call_log.trigger_type
--
-- 为什么需要
--   「调用记录」列表要能筛「用户直接发起的」与「系统后台发起的」两类调用
--   （两类都记，靠前端筛选组件区分）。区分必须有稳定列承载，
--   不能靠 source / scene 白名单在展示层猜。
--
-- 对应全量：
--   deploy/postgres/migrations/002_platform.sql → monitor.call_log 建表已含本列
--   新库由上面那处直接建立；本脚本只服务**已经跑过 050**的存量库。
--
-- 为什么单开一个脚本而不是改 050
--   050 里的建表是 CREATE TABLE IF NOT EXISTS —— 表已存在的库重跑时整段跳过，
--   不会补列。所以 050 之后的表结构演进必须走**新的 ALTER 脚本**。
--
-- 边界（有意不做的事）
--   * 不加 space_id（沿用 050 口径）
--   * 不改 call_attempt（Attempt 级别不区分触发方，继承其 N1）
--   * 全部语句幂等，可重复执行（apply.sh 重复执行安全）
-- ============================================================

BEGIN;

ALTER TABLE monitor.call_log
    ADD COLUMN IF NOT EXISTS trigger_type VARCHAR(16) NOT NULL DEFAULT 'user';

-- CHECK 约束不能 IF NOT EXISTS，先 DROP 再 ADD 保证可重复执行
ALTER TABLE monitor.call_log
    DROP CONSTRAINT IF EXISTS ck_monitor_call_log_trigger;
ALTER TABLE monitor.call_log
    ADD CONSTRAINT ck_monitor_call_log_trigger
    CHECK (trigger_type IN ('user','system'));

-- I9：按触发方（用户/系统后台）筛选 + 时间倒序
CREATE INDEX IF NOT EXISTS idx_monitor_call_log_trigger_started
    ON monitor.call_log (trigger_type, logical_started_at DESC);

COMMIT;
