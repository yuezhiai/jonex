-- ============================================================================
-- knowledge_base.task_center_documents.last_step 改为三态（2026-11-20）
--
-- 背景：该列是「本步是不是本次编排的最后一步」（任务中心派活报文的
-- `is_last_step`）。原来 NOT NULL DEFAULT false，无法区分
--   · 「明确不是最后一步」（多 compile 节点的 compile_1）
--   · 「老行 / 老运行方没带这个字段」
-- 看门狗对后者必须回落「compile 才算最后一步」，对前者必须尊重 false ——
-- 二态下这两件事被混成同一种，多 compile 节点时 compile_1 会被提前收口。
--
-- 改动：允许 NULL（NULL = 未知）。新代码起，运行方与看门狗都按三态处理。
-- 幂等：重复执行安全。
-- ============================================================================
BEGIN;

ALTER TABLE knowledge_base.task_center_documents
    ALTER COLUMN last_step DROP NOT NULL;

ALTER TABLE knowledge_base.task_center_documents
    ALTER COLUMN last_step SET DEFAULT NULL;

COMMIT;
