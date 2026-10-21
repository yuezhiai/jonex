-- ============================================================
-- 050_task_source.sql
-- 任务来源一列（存量库）
--
-- ⚠️ **编号 050 与 `050_platform_monitor.sql` 撞车，是有意保留的**（2026-10-14 定）。
--   本脚本原为 `049_task_source.sql`，因 049 已被 `049_task_center_subtask_report_columns.sql`
--   占用而改到 050，当时未察觉 050 也已占用。
--   为什么不改名：登记表 `public.schema_migrations` 按**完整文件名**做主键（`apply.sh` /
--   `jonex.ps1` 均如此），本脚本**已在库中应用过**（本机库 2026-10-14 10:04 登记）。
--   此时改名 = 换了个主键 ⇒ 下次 `make db-migrate` 会把它**当新迁移重放一遍**。
--   撞号本身无实害：两个 runner 都不按编号前缀取件，两条脚本各自按文件名独立登记。
--   后续脚本请接 **053** 起编号（052 已被 `052_platform_monitor_token_index.sql` 占用）。
--
-- ⚠️ **`migrations/008_task_center.sql` 有意没动**（用户定：那份已发布上线，结构改动
--   一律只走 update 增量脚本 —— 与 046 / 048 同一条纪律）。代价说清楚：存量库若按
--   「删表重建」对齐（重跑 `008`），重建完**必须再跑一次本脚本**，否则新库里没有这一列，
--   代码一读到就报「列不存在」。
--
-- 为什么加
--   `tasks` 原来只有 `trigger_id`（哪个触发器触发的）与 `created_by`（谁点的），
--   **答不出「这条任务是哪个入口来的」**：定时触发与内部调用都留空、分不出彼此；
--   而运行方（知识库）发起的任务里，手动上传 / API 接入 / API 开放（推送）/
--   文件存储直连四种入口在任务表上**完全同形** —— 按来源统计、排障、筛选都做不到。
--
--   取值是**协议层写死的枚举**（`jonex_core/task_protocol/models.py` 的 `TASK_SOURCES`，
--   格式 `{运行方 code}.{对象}.{通道}`）。管理方存**原码**、不存文案 —— 中文名由前端
--   按码映射（与 `knowledge_documents.data_source_type` 那套的做法一致）。
--
-- ⚠️ **可空，而且空是有意义的**（**不要**给它加 NOT NULL）：平台侧自己建的任务
--   （定时触发、人点「立即触发」）不是运行方发起的，本来就没有「来源入口」，留空是对的；
--   这列诞生之前的存量行也没值，回填不了、也不该编一个。「必传」只落在 `init_task`
--   那条路上（应用层判，非法值 1001），不在这一层表达。
--
-- 幂等: 列走 `ADD COLUMN IF NOT EXISTS`；CHECK 走 `DROP ... IF EXISTS` + `ADD`
--   ——`ALTER TABLE ... ADD CONSTRAINT` **不支持 `IF NOT EXISTS`**（见 038 里的同一条
--   说明），而本仓的迁移执行器允许重跑同一个文件（版本表可能对不上），所以显式先删。
-- ============================================================

BEGIN;

-- ⚠️ **一句一个 `ADD COLUMN`**（别写成逗号并列）：仓库那条列对齐守卫
-- （`tests/unit/task_center/test_task_center_schema_parity.py`）逐句解析本目录的 DDL，
-- 逗号并列的第二个 `ADD COLUMN` 它读不到 —— 会误报「ORM 有、DDL 没有」。
ALTER TABLE task_center.tasks ADD COLUMN IF NOT EXISTS task_source VARCHAR(64);

-- 取值与协议层 `TASK_SOURCES` **同源**：那条对账用例按约束名把两边取值逐个对照
-- （`_ENUM_BY_CONSTRAINT` 里 `ck_tc_task_source` → `TASK_SOURCE_VALUES`）。
-- 加一个新来源 = **三处一起改**：协议层枚举 + 这里 + 前端映射。
--
-- ⚠️ 可空列上的 CHECK 遇到 NULL 是「通过」的（SQL 三值逻辑）—— 正是要的语义：
-- 平台侧自己发起的任务留空合法；**填了就必须在册**。
ALTER TABLE task_center.tasks DROP CONSTRAINT IF EXISTS ck_tc_task_source;
ALTER TABLE task_center.tasks ADD CONSTRAINT ck_tc_task_source CHECK (
    task_source IN (
        'knowledge_base.file.manual_upload',
        'knowledge_base.file.api_pull',
        'knowledge_base.file.api_push',
        'knowledge_base.file.storage_upload'
    )
);

COMMENT ON COLUMN task_center.tasks.task_source IS
  '任务来源（协议层 TASK_SOURCES 写死枚举，如 knowledge_base.file.manual）：这条任务是运行方从哪个入口提交的。NULL = 不是运行方提交的（定时触发 / 人工立即触发），或这列诞生之前的存量行。存码不存文案，中文名由前端按码映射';

COMMIT;
