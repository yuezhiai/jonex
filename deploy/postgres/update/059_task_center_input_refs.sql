-- task_center 补齐 5 列（2026-10-16）：
--   · `subtasks.input_refs`          —— 输入「从哪儿来」的引用（最新一次尝试的现状）
--   · `subtask_reports.input_refs`   —— 上报收件箱侧的同名一份（跟着报文走）
--   · `subtask_attempts.input_refs`  —— 每次尝试各一份的输入引用
--   · `subtask_attempts.inputs`      —— 每次尝试的入参快照（与已有的 `outputs` 对称）
--   · `subtask_attempts.output_refs` —— 每次尝试的产出引用（与已有的 `outputs` 配套）
--
-- 后三列补的是同一格缺口：`subtask_attempts` 从前只按次存了**产出**（`outputs`），
-- 而「入参」「输入入口」「产出入口」都只挂在 `subtasks` 那行 —— 那一行永远只是**最新一次
-- 尝试**的现状，重试时 `inputs` 还会被 `_fill_inputs` 重算。历史要能复盘，就得按次留。
--
-- 这 5 列**只在增量链里**，基础迁移 `migrations/008_task_center.sql` 不动（用户 2026-10-16 的
-- 口径：008 已冻结，新列一律走 update）。部署链是「先跑完 migrations 下的全部，再跑 update
-- 下的全部」，所以全新初始化的库与存量库都会走到这里 —— 不存在 049 那种
-- 「列只写在基础迁移里、增量链没覆盖」的缺口。
--
-- ⚠️ **一句一个 `ADD COLUMN`** —— 仓库那条「列对齐守卫」只解析单句（同 048 / 049 的告诫），
--    写成逗号并列会被当成没加列。
-- ⚠️ 五条都带 `IF NOT EXISTS`（幂等）：在已有这一列的库上重跑是空操作（`already exists, skipping`）。
-- ⚠️ 五列都可空：老运行方不上报、或将来真没有引用的任务，这一列就是 NULL。

-- ① 输入「从哪儿来」的引用（JSONB 数组，展示用、**不参与下游取参**；`inputs` 才是下游取参来源）。
ALTER TABLE task_center.subtasks ADD COLUMN IF NOT EXISTS input_refs JSONB;

-- ② 上报收件箱侧的输入引用（跟着报文走；消化时原样转抄到 `subtasks.input_refs`）。
ALTER TABLE task_center.subtask_reports ADD COLUMN IF NOT EXISTS input_refs JSONB;

-- ③ 每次尝试那一侧的输入引用（`subtask_attempts`，「尝试历史」要按次看输入）。
--    与 `subtasks.input_refs` **不是同一份**：那边是「最新一次尝试」的现状，会被重试重算；
--    这边是**每一次尝试各存一份**，重试之后仍能查到「第 2 次尝试当时吃的是什么」。
ALTER TABLE task_center.subtask_attempts ADD COLUMN IF NOT EXISTS input_refs JSONB;

-- ④ 每次尝试的**入参快照**（`subtask_attempts.inputs`，2026-10-16 加）。
--    与 `outputs` 对称的那一格缺口：产出按次存着、入参却只在 `subtasks` 上（最新一次尝试的
--    现状，重试会被 `_fill_inputs` 重算）—— 不补这一列，「第 2 次尝试吃的是什么」在重试后
--    就查不到了。
ALTER TABLE task_center.subtask_attempts ADD COLUMN IF NOT EXISTS inputs JSONB;

-- ⑤ 每次尝试的**产出引用**（`subtask_attempts.output_refs`，2026-10-16 加）。
--    与 `outputs`（产出快照）配套：产出正文一直按次存着，而「它的产物在哪儿看」这个入口
--    从前只挂在 `subtasks` 那行（最新一次尝试）—— 同样的理由，按次补一份。
ALTER TABLE task_center.subtask_attempts ADD COLUMN IF NOT EXISTS output_refs JSONB;
