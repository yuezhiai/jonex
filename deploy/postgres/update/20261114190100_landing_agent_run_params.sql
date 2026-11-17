-- ============================================================
-- 落点 run 的构造参数（原 109，2026-11-14 按时间戳命名规范改名）：force / alias_hints
--
-- 为什么落在 run 行上：`execute` 是**另一个后台任务**，只拿 run_id；`full` 模式
-- 要按「提交时是否确认覆盖」调用 construct(force=...)，并带上业务词表。
-- 调度参数不能当事实（重复投递 / 进程重启后已过时），必须持久化。
--
-- 幂等：ADD COLUMN IF NOT EXISTS。全新库由 migrations/003_knowledge_base.sql 的终态建表已带这两列，
--       本脚本在其上是 no-op；存量库（已跑过旧建表脚本）由这里补列。
-- ============================================================
BEGIN;

ALTER TABLE knowledge_base.abstract_landing_runs
    ADD COLUMN IF NOT EXISTS force boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS alias_hints text NOT NULL DEFAULT '';

COMMIT;
