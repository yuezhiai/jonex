-- ============================================================
-- 090_ontology_tool_read_only_qa_callable.sql
-- 本体工具调用：知识库工具「只读声明」+ 工具绑定「允许问答调用」+ 库级「抽取期调用工具」开关
--
-- 表由 update/084 创建；本脚本只加列，全部幂等，apply.sh 可重复执行。
--
-- 1) knowledge_base.kb_tools.read_only
--    工具登记人显式声明「这个接口只读、不改对方数据」。默认 false = 写操作。
--    请求方式是 GET 不等于只读，不做推断。平台工具的同一声明存在
--    business_domain.skill_catalog.capability_json.read_only（JSONB 键，无需 DDL）。
--
-- 2) knowledge_base.ontology_tool_bindings.qa_callable
--    绑定级开关：允许知识库问答在回答时自动调用这个工具。默认 false。
--    问答实际调用还要同时满足：总闸 ONTOLOGY_TOOL_EXECUTION_ENABLED 打开、绑定启用且工具可用、
--    执行判定为 allow、工具声明了只读；工具输出会作为上下文发给上游大模型。
--
-- 3) knowledge_base.knowledge_info.extract_tool_calls
--    库级开关：写图前按 TBox 里的 `catalog:` 抽取技能声明调用登记的只读工具，
--    补充 / 校验抽取结果（每篇文档会多出外部调用）。只认 TRUE；NULL / FALSE = 不调用。
--    与进程总闸 ONTOLOGY_EXTRACT_TOOL_ENABLED AND 生效，写法同 procedural_extract。
--
-- 发版次序：**先 db-migrate，再换 knowledge-base 新镜像** ——
--   反序时新代码读这三列会当场 UndefinedColumn。
--
-- 回滚
--   ALTER TABLE knowledge_base.knowledge_info DROP COLUMN IF EXISTS extract_tool_calls;
--   ALTER TABLE knowledge_base.ontology_tool_bindings DROP COLUMN IF EXISTS qa_callable;
--   ALTER TABLE knowledge_base.kb_tools DROP COLUMN IF EXISTS read_only;
--   （或只关总闸 ONTOLOGY_TOOL_EXECUTION_ENABLED / ONTOLOGY_EXTRACT_TOOL_ENABLED）
-- ============================================================

BEGIN;

ALTER TABLE knowledge_base.kb_tools
    ADD COLUMN IF NOT EXISTS read_only BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN knowledge_base.kb_tools.read_only IS
    '只读声明（登记人显式勾选，默认 false = 写操作）；只有只读工具才可能被知识库问答自动调用';

ALTER TABLE knowledge_base.ontology_tool_bindings
    ADD COLUMN IF NOT EXISTS qa_callable BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN knowledge_base.ontology_tool_bindings.qa_callable IS
    '允许知识库问答自动调用该绑定的工具（默认 false）；另需总闸开启、判定 allow、工具只读';

ALTER TABLE knowledge_base.knowledge_info
    ADD COLUMN IF NOT EXISTS extract_tool_calls BOOLEAN;

COMMENT ON COLUMN knowledge_base.knowledge_info.extract_tool_calls IS
    '抽取期调用工具（catalog: 抽取技能）库级开关：TRUE = 写图前调用登记的只读工具补充 / 校验；NULL / FALSE = 不调用';

COMMIT;
