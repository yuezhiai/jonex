-- ============================================================
-- 073_platform_monitor_time_index.sql
-- 平台监控 · 调用明细「只按时间」的范围扫索引（存量库）
--   monitor.call_log (logical_started_at, id)
--
-- 背景
--   监控调用列表 / 降级判定按 logical_started_at 做时间范围过滤与倒序：
--     * C5：不传 direction 的列表查询
--     * C6：传 direction 的列表查询（conditions 里同样先卡时间窗）
--     * 明细列表排序：ORDER BY logical_started_at DESC
--   现有索引（050 建，I2/I3/I4/I5/I8/I9）都以 tenant_id / direction /
--   final_status / trigger_type / target 开头，时间列不是前导列；查询不带这些
--   维度时 PostgreSQL 15 无 index skip scan，只能顺扫 90 天明细。
--   本脚本补一枚以时间列开头的索引，时间窗查询可走 index range scan。
--
--   二级列 id 的作用：同毫秒内多行时给倒序分页一个稳定的 tie-breaker 走索引，
--   避免同一 logical_started_at 下的行序抖动（读接口当前只按时间排序，未显式
--   加 id DESC；本脚本只加索引，不改读接口代码）。
--
-- 边界
--   * 不改表结构 / 约束，不动既有索引。
--   * 幂等：IF NOT EXISTS，可重复执行（apply.sh 重复执行安全）。
--   * 写放大：call_log 是 append-only 明细表，每行多维护一枚 b-tree；
--     生产大表建索引会持写锁（阻塞 INSERT），建议低峰执行。
--   * 大表若需要 CONCURRENTLY：CREATE INDEX CONCURRENTLY 不能放在显式事务块中，
--     而 apply.sh 会原样执行本文件的 BEGIN/COMMIT ⇒ 大表请绕开 apply.sh 手工执行
--     CREATE INDEX CONCURRENTLY IF NOT EXISTS ...（失败会留下 INVALID 索引，需先
--     DROP 再重跑），执行成功后再手工登记 schema_migrations。
--
-- 回滚
--   -- 回滚：DROP INDEX IF EXISTS monitor.idx_monitor_call_log_logical_started;
--
-- 对应全量基线
--   deploy/postgres/migrations/002_platform.sql → monitor.call_log 建表与索引段
--   （已在 I9 之后同步本索引，新库无需再跑本脚本）。
-- ============================================================

BEGIN;

CREATE INDEX IF NOT EXISTS idx_monitor_call_log_logical_started
    ON monitor.call_log (logical_started_at, id);

COMMIT;
