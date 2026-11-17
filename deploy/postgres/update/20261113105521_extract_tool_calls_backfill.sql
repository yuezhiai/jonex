-- ============================================================
-- 20261113105521_extract_tool_calls_backfill.sql
-- 补齐 knowledge_base.knowledge_info.extract_tool_calls（抽取期调用工具库级开关）
--
-- 背景：这一列最初是追加进 090_ontology_tool_read_only_qa_callable.sql 的，但
-- apply.sh 只按**文件名**登记已应用脚本：早期跑过 090 的库不会再执行 090，
-- 也就永远补不上这一列（新版 knowledge-base 读它直接 UndefinedColumn）。
--
-- 因此单独建一个幂等脚本。全新库先跑 090 再跑 091，第二步是 no-op；
-- 存量库 `make db-migrate` 会执行 091 把列补上，不再需要手工 SQL。
--
-- 发版次序：先 db-migrate（090 → 091），再换 knowledge-base 新镜像。
--
-- 回滚
--   ALTER TABLE knowledge_base.knowledge_info DROP COLUMN IF EXISTS extract_tool_calls;
-- ============================================================

BEGIN;

ALTER TABLE knowledge_base.knowledge_info
    ADD COLUMN IF NOT EXISTS extract_tool_calls BOOLEAN;

COMMENT ON COLUMN knowledge_base.knowledge_info.extract_tool_calls IS
    '抽取期调用工具（catalog: 抽取技能）库级开关：TRUE = 写图前调用登记的只读工具补充 / 校验；NULL / FALSE = 不调用';

COMMIT;
