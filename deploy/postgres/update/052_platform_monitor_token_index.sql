-- ============================================================
-- 052_platform_monitor_token_index.sql
-- 平台监控 · Token 读接口补「只按时间」的索引（存量库）：metering.llm_usage_log(created_at)
--
-- 为什么需要
--   Token 监控读接口 K1–K4（/tokens/summary、/tokens/trend、/tokens/models/top、
--   /tokens/usage）**不做租户过滤** —— 可见性只凭 platform:monitor:read（见 00-契约冻结清单 /
--   2026-10-10 拍板「不做租户/空间隔离」），四个接口一律只按 `created_at` 做时间范围扫。
--   而本表现有与时间相关的索引只有复合索引 idx_llm_usage_tenant_time (tenant_id, created_at)：
--   查询不带 tenant_id 时**复合索引的第一列用不上**，PostgreSQL 15 又**无 index skip scan**
--   ⇒ 规划器只能退化为**全表顺序扫描**，数据量增长后 K1–K4 会明显变慢。
--   故需求方本轮拍板：顺手补一个**只按时间**的索引。
--
-- 影响哪些接口
--   K1 GET /api/v1/platform/monitor/tokens/summary
--   K2 GET /api/v1/platform/monitor/tokens/trend
--   K3 GET /api/v1/platform/monitor/tokens/models/top
--   K4 GET /api/v1/platform/monitor/tokens/usage
--   （四个接口全部走 `WHERE created_at >= :start AND created_at < :end` 的时间范围扫）
--
-- 对应全量
--   deploy/postgres/migrations/002_platform.sql → metering.llm_usage_log 建表 + 既有三索引
--     （idx_llm_usage_tenant_time / idx_llm_usage_scene / idx_llm_usage_trace）
--   ⚠️ 已知缺口：migrations/002 的全量 DDL **尚未**同步本索引 ⇒ 本脚本落地后新建的全量库
--   仍缺少本索引；由本脚本只服务**已经跑过 002 的存量库**。全量侧补录为 follow-up（不在本脚本范围）。
--
-- 命名与类型
--   * 命名沿用本表既有风格 idx_llm_usage_<维度>（参考 idx_llm_usage_tenant_time）。
--   * 列上不写 DESC：K1–K4 是范围扫（>= / <），b-tree 普通索引对双向范围等价可用；
--     且与既有 idx_llm_usage_tenant_time (tenant_id, created_at) 的列排序风格保持一致。
--
-- 如何回滚
--   DROP INDEX IF EXISTS metering.idx_llm_usage_created;
--
-- 边界（有意不做的事）
--   * 不动机器既有索引（不改 idx_llm_usage_tenant_time 等三枚）
--   * 不新增任何列 / 不改表结构（N3「Token 幂等」本期仍不做）
--   * 全部语句 IF NOT EXISTS，可重复执行（apply.sh 重复执行安全）
--   * 登记由 apply.sh 自动写入 public.schema_migrations（按文件名），本脚本内**不需要**手工登记
-- ============================================================

BEGIN;

-- I-K：Token 读接口 K1–K4 的「只按时间」范围扫索引（不做租户过滤）
CREATE INDEX IF NOT EXISTS idx_llm_usage_created
    ON metering.llm_usage_log (created_at);

COMMIT;
