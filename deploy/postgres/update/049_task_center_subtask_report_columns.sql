-- task_center 补齐 3 个缺列（2026-10-13）：`subtasks.output_refs` /
-- `subtask_reports.task_id` / `subtask_reports.output_refs`
--
-- 为什么存量库会缺：这 3 列只写在**基础迁移** `migrations/008_task_center.sql` 里，
-- `update/` 增量链从没覆盖过 ——
--   · `subtasks.output_refs`：8a131934（2026-10-08「feat(task_center): 上报支持 output_refs
--     产出引用」）加进基础迁移，当时**没有**配套增量文件；
--   · `subtask_reports.task_id`：原由 `037_task_center_subtask_reports_task_id.sql` 提供
--     （见 `036_task_center.sql` 顶部注释），后内联进基础迁移并**把那个增量文件删了**。
-- 于是只跑增量链的库永远拿不到它们（`make db-migrate` 报「无待应用」而列就是不存在），
-- 只有 initdb 全新初始化的库才有 ⇒ 任务中心后台调度循环每轮
-- `ProgrammingError: column subtasks.output_refs does not exist`，子任务永不派发、任务
-- 永远 `pending`。2026-10-13 本地实测：循环每 ~3 秒挂一次（1873+ 次）、当天 13 个任务
-- 全 pending、「重新解析」一律回 1003。
--
-- ⚠️ **一句一个 `ADD COLUMN`** —— 仓库那条「列对齐守卫」只解析单句（同 048 的告诫），
--    写成逗号并列会被当成没加列。
-- ⚠️ 三条都带 `IF NOT EXISTS`（幂等）：在全新库上重跑是空操作（`already exists, skipping`）。

-- ① 产出「去哪儿看」的引用（JSONB 数组，展示用、**不参与下游取参**；`outputs` 才是下游取参来源）。
ALTER TABLE task_center.subtasks ADD COLUMN IF NOT EXISTS output_refs JSONB;

-- ② 上报收件箱侧的 `task_id`（跟着报文走；ORM 里声明为 NOT NULL）。
ALTER TABLE task_center.subtask_reports ADD COLUMN IF NOT EXISTS task_id VARCHAR(64);

-- ③ 上报收件箱侧的产出引用（消化时原样转抄到 `subtasks.output_refs`）。
ALTER TABLE task_center.subtask_reports ADD COLUMN IF NOT EXISTS output_refs JSONB;

-- ④ `task_id` 收紧为 NOT NULL（与 ORM 对齐）：先按 `execution_id` 回填存量行，对不上
--    （任务已删 / 凭证已被重试清掉 —— 收件箱本来就允许这类报文落库）的置空串兜底，
--    最后才 `SET NOT NULL`。重复执行安全：回填是空操作、`SET NOT NULL` 幂等。
UPDATE task_center.subtask_reports r
   SET task_id = s.task_id
  FROM task_center.subtasks s
 WHERE r.task_id IS NULL AND s.execution_id IS NOT NULL AND r.execution_id = s.execution_id;

UPDATE task_center.subtask_reports SET task_id = '' WHERE task_id IS NULL;

ALTER TABLE task_center.subtask_reports ALTER COLUMN task_id SET NOT NULL;

-- ⑤ 列注释与全新初始化的库对齐（基础迁移里的 COMMENT 只在新库 initdb 时执行过）。
COMMENT ON COLUMN task_center.subtasks.output_refs IS '产出「去哪儿看」的引用（展示用，**不参与下游取参**）：运行方终态上报时带的数组，控制台读接口原样回给前端，前端据此调运行方自己的接口看详情（权限归运行方那侧判）';

COMMENT ON COLUMN task_center.subtask_reports.output_refs IS '产出「去哪儿看」的引用（随行记录）：消化时原样转抄到 `subtasks.output_refs`，本列不参与裁决';
