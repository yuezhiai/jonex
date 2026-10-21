-- ============================================================
-- 051_task_center_concurrency_scope.sql
-- `max_concurrency` 的口径由「全局按 step_code」改为「按目录 + 步骤」（存量库）
--
-- ⚠️ **只改注释，不动任何结构**（用户 2026-10-14 定：并发粒度取
--    `task_catalog_code + step_code`，每个目录各自保留 10 的额度）。
--
-- 为什么改注释也要单独发一个增量脚本
--   纪律同 046 / 048 / 050：`migrations/008_task_center.sql` 已发布上线，
--   结构一律只走 update 增量。这一列的 `COMMENT` 就在那份全量 DDL 里
--   （`008` :586、`036` :533），而它写的还是老口径：
--   「派活时按它数全局在跑数、到上限就跳过」——**已经和代码对不上了**。
--   注释是给下一个读库的人看的，留着错的口径比没有更坏：会让人按「全平台一份额度」
--   去估算容量，而实际是「每个目录各一份」。
--
-- 口径变化（2026-10-14）是什么
--   改之前：`count_running_by_step_code` 只按 `step_code` 数，
--   **全平台所有目录、所有租户共用一份名额**。实测后果：
--   `file_processing_jonex_wiki` 的 10 个 `compile` 卡住不结束，
--   **另一个目录 `file_processing_ontology` 的 `compile` 一个都派不出去** ——
--   两者互不相干，却排在同一份额度后面，队头一堵全体饿死。
--   改之后：`count_running_by_catalog_step(step_code, task_catalog_code)`，
--   按「所属任务的 `tasks.task_catalog_code` + 本行的 `step_code`」数 ——
--   **每个目录的每个步骤各有一份名额**，上限数值取该目录那一行配置的 `max_concurrency`。
--
--   ⚠️ **总量会翻**：全平台最大并发从「N」变成「Σ(每个目录各 N)」。这是明确接受的
--      代价（上限数值仍按目录各配 10）。要收紧就得逐目录调那个数。
--   ⚠️ **目录内不变**：同一个目录内、同一个步骤挂在多个节点上（`parse_1` / `parse_2`）
--      **仍然共享**这一份 —— 变的只是「跨目录不再互相占位」。
--
-- 幂等: `COMMENT ON COLUMN` 本来就是覆盖写，重跑无害。
-- ============================================================

BEGIN;

COMMENT ON COLUMN task_center.task_catalog_steps.max_concurrency IS
  '这步最多同时跑几个（运行方对此类活的并发上限）；空=不限；派活时按「所属任务的 task_catalog_code + 本行的 step_code」数在跑数、到上限就跳过 —— 每个目录的每个步骤各有一份名额，跨目录不互相占位（2026-10-14 起，此前是全平台按 step_code 共用一份）';

COMMIT;
