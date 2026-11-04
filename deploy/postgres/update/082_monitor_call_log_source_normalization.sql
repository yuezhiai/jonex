-- ============================================================
-- 082_monitor_call_log_source_normalization.sql
-- 监控调用记录 source 归一化：裸数字用户 ID → users.username（存量库 · 过渡脚本）
--
-- ⚠️ 适用范围（执行前务必确认）
--   本迁移只适用于「采集端尚未升级、仍会把裸数字用户 ID 写进 source」的环境，
--   它把裸数字回刷成用户名，是**过渡形态**。采集端的终态口径是只写稳定标识
--   `user:<本地用户主键>`（用户名由读侧按主键解析，改名不复发）。
--   · 若该环境已经（或即将）完成 `user:<id>` 改造 —— 请直接执行
--     083_monitor_call_log_source_to_user_identity.sql，本文件不必再跑；
--     083 完全吸收本文件的目标。
--   · 本文件刻意**不匹配** `user:<id>` 形态：那是规范终态，改写它会把标识
--     退回用户名，方向相反。
--
-- 背景
--   monitor.call_log.source 由多个采集点写入，历史形态混杂：
--     · Sidecar 入站：用户名为准（旧版）
--     · llm-gateway 出站（旧版）：用户主键原值（纯数字）
--     · 系统标识：apikey / platform / anonymous / unknown 等
--   读侧会把同一个人的 "1" 与 "admin" 显示成两个同名筛选项，列表也无法区分。
--
-- 执行前提（⚠️ 顺序不能反）
--   1) 本环境所有写 source 的采集端已升级，不再写裸数字；
--   2) 观察至少一个业务周期，确认不再新增裸数字：
--        SELECT date_trunc('day', created_at) d, source, count(*)
--        FROM monitor.call_log
--        WHERE source ~ '^[0-9]{1,18}$'
--        GROUP BY 1, 2 ORDER BY 1;
--      最后一整天无新行后再执行。先回刷后升级 = 旧采集端会立刻把裸数字写回来。
--
-- 安全边界
--   · 严格按行自身 tenant_id 匹配 platform.users，禁止跨租户串号；
--   · 只回刷 is_deleted = 0 的活跃用户；解析不到的行（含租户内无此人、
--     用户已软删、纯数字但不是有效主键）保持原值，留给 083 或人工处理；
--   · 幂等：只处理纯数字形态，回刷后不再命中，可安全重跑；
--   · 事务包裹：UPDATE 与备份同事务，失败即整体回滚。
--
-- 回滚（需在应用后手动执行；备份表保留期建议 ≥ 30 天）
--   BEGIN;
--   UPDATE monitor.call_log c SET source = b.source
--   FROM monitor.call_log_source_bak_082 b WHERE c.id = b.id;
--   COMMIT;
--
-- 已知副作用
--   · 存量筛选链接若带 source=<数字>，回刷后查不到数据。
--   · 回刷后 source 是用户名，用户改名会与后续的 `user:<id>` 形态再次分叉；
--     根治靠采集端只写稳定标识（另行升级），本脚本只是清存量。
-- ============================================================

BEGIN;

-- 备份受影响行（回滚唯一依据；固定表名 + IF NOT EXISTS 保证可重复执行）
CREATE TABLE IF NOT EXISTS monitor.call_log_source_bak_082 AS
SELECT id, tenant_id, source
FROM monitor.call_log
WHERE source ~ '^[0-9]{1,18}$';

-- 回刷：按行自身租户 + 活跃用户主键匹配。
-- 数字 → bigint 的转换放在 CASE 里，避免执行计划把转换提前到正则过滤之前
-- 而对非数字文本抛 invalid input syntax for type bigint。
UPDATE monitor.call_log c
SET source = u.username
FROM platform.users u
WHERE u.tenant_id = c.tenant_id
  AND u.is_deleted = 0
  -- 显式限定形态：CASE 已能兜住非数字行，这里再写一遍是为了少扫无关行
  AND c.source ~ '^[0-9]{1,18}$'
  AND u.id = (CASE WHEN c.source ~ '^[0-9]{1,18}$' THEN c.source::bigint END);

COMMIT;
