-- ============================================================
-- 046_task_center_error_reason.sql
-- 失败原因的「代号 + 参数」两列（存量库）
--
-- ⚠️ **`migrations/008_task_center.sql` 有意没动**（用户定：那份已经发布上线，
--   结构改动一律只走 update 增量脚本）。代价要说清楚：任务中心的存量库是按
--   「**删表重建**」对齐的（重跑 `008`）—— 重建完**必须再跑一次本脚本**，
--   否则新库里没有这两列，代码一读到就报「列不存在」。
--   这与 `042_knowledge_task_center.sql` 是同一个处境（重建库也要补跑它）。
--
-- 为什么加
--   失败原因原来是**写入时**就 `translate()` 成一句话存进 `error_message` 的，而写它的
--   多半是**后台循环**（超时巡检 / 防僵尸 / 派活判死 / 停止任务）—— 那些帧里没有请求
--   上下文，`translate()` 只能拿到 DEFAULT_LOCALE，于是**语言在写的那一刻就被冻住**
--   （实测库里 34 条英文、12 条硬编码中文，读的人换语言也变不了）。
--   改成「存代号 + 参数，**读的时候**按请求的 `X-Lang` 现场渲染」。
--
--   `error_message` **不删不改** —— 它继续承担三件事：
--     ① 引擎 / 下下游的**自由文本**（`LibreOffice conversion failed for …`
--        这类永远没有代号）；② 老数据兜底（存量行没有代号）；③ 排障快照。
--   也就是说：可翻译的那半搬到新两列，不可翻译的那半仍只有它。
--
-- 五张表都加（列名 / 类型 / 上限与协议层 `check_error_reason` 一致）
--   `error_code`   消息键，如 `err.task_center.subtask.timed_out`（128 = 协议层上限）
--   `error_params` 渲染占位符的实参（JSONB；任务级那条装的是「零件数组」）
--
-- 幂等: `ADD COLUMN IF NOT EXISTS`。重复执行是空操作。
-- ============================================================

BEGIN;

-- ⚠️ **一句只加一列**（别写成 `ALTER TABLE t ADD COLUMN a ..., ADD COLUMN b ...`）：
--   仓库里那条列对齐守卫（`tests/unit/task_center/test_task_center_schema_parity.py`）
--   逐条解析本目录的 DDL，逗号并列的第二个 `ADD COLUMN` 它读不到 —— 会误报「ORM 有、DDL 没有」。
ALTER TABLE task_center.tasks ADD COLUMN IF NOT EXISTS error_code VARCHAR(128);
ALTER TABLE task_center.tasks ADD COLUMN IF NOT EXISTS error_params JSONB;

ALTER TABLE task_center.task_attempts ADD COLUMN IF NOT EXISTS error_code VARCHAR(128);
ALTER TABLE task_center.task_attempts ADD COLUMN IF NOT EXISTS error_params JSONB;

ALTER TABLE task_center.subtasks ADD COLUMN IF NOT EXISTS error_code VARCHAR(128);
ALTER TABLE task_center.subtasks ADD COLUMN IF NOT EXISTS error_params JSONB;

ALTER TABLE task_center.subtask_attempts ADD COLUMN IF NOT EXISTS error_code VARCHAR(128);
ALTER TABLE task_center.subtask_attempts ADD COLUMN IF NOT EXISTS error_params JSONB;

ALTER TABLE task_center.subtask_reports ADD COLUMN IF NOT EXISTS error_code VARCHAR(128);
ALTER TABLE task_center.subtask_reports ADD COLUMN IF NOT EXISTS error_params JSONB;

COMMENT ON COLUMN task_center.subtasks.error_code IS
  '失败原因的消息键（可翻译的那半）；读接口按请求 X-Lang 渲染。error_message 仍是原文槽 + 排障快照';
COMMENT ON COLUMN task_center.subtasks.error_params IS
  'error_code 的渲染实参（JSONB）。任务级那条装的是「零件数组」：每个子任务一条 {node_id, code, params, message}';

COMMIT;
