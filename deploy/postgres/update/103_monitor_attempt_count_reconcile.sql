-- ============================================================
-- 103_monitor_attempt_count_reconcile.sql
-- 平台监控：回填历史 attempt_count 失配（一次性数据修复，幂等可重跑）
--
-- 为什么需要
--   monitor.call_log（逻辑调用主记录，含 attempt_count）与
--   monitor.call_attempt（物理尝试记录）由不同进程写入。跨进程写事务重叠时，
--   双方都看不到对方未提交的行，可能出现 call_attempt 已落 N 行而
--   call_log.attempt_count 仍停在 0（测试服 7 天实测 1,246 行）。新版本已有
--   后台周期对账，但只覆盖最近 10 分钟；本脚本负责把历史失配一次性补齐。
--
-- 只升不降（GREATEST）的原因
--   attempt_count 是逻辑调用的物理尝试次数，语义上单调不减：
--     1) call_attempt 行可能因保留策略（072_platform_monitor_retention.sql）
--        先被清理，此时实际尝试行数会变小，不能反向调低 call_log 的计数；
--     2) 与周期对账 / 并发写入共存时，避免两方在旧快照之间来回抖动；
--     3) 重复执行本脚本天然是 no-op（已一致的行不满足 < 条件）。
--
-- 幂等性
--   WHERE cl.attempt_count < sub.cnt 只会命中仍然偏低的行，重复执行安全；
--   全文件包在 BEGIN/COMMIT，失败整体回滚。apply.sh 用版本表登记文件名，
--   正常只会执行一次；需要补跑（如执行窗口内又有新失配）可手工重跑本文件。
--
-- 执行时机
--   在升级监控采集端 / platform 服务**之前或之后**均可 —— 本脚本只做数据
--   UPDATE，不依赖新代码、不改表结构、不影响服务启动。执行时只锁定/更新
--   实际失配的 call_log 行，一致行不会被触碰。
--
-- 范围
--   只上调 monitor.call_log.attempt_count，不新增/删除任何业务数据，不改表结构。
-- ============================================================

BEGIN;

UPDATE monitor.call_log AS cl
SET attempt_count = GREATEST(cl.attempt_count, sub.cnt),
    updated_at = NOW()
FROM (
    SELECT request_id, COUNT(*) AS cnt
    FROM monitor.call_attempt
    GROUP BY request_id
) AS sub
WHERE cl.request_id = sub.request_id
  AND cl.attempt_count < sub.cnt;

COMMIT;

-- 执行后核对：仍不一致的 call_log 行数（attempt_count <> call_attempt 实际行数）。
-- 正常应为 0；非 0 表示执行窗口内又产生了新的失配，可重跑本文件补齐
-- （或等待新版本的后台周期对账处理最近 10 分钟的数据）。
SELECT COUNT(*) AS inconsistent_call_log_rows
FROM monitor.call_log AS cl
LEFT JOIN (
    SELECT request_id, COUNT(*) AS cnt
    FROM monitor.call_attempt
    GROUP BY request_id
) AS sub
    ON cl.request_id = sub.request_id
WHERE cl.attempt_count <> COALESCE(sub.cnt, 0);
