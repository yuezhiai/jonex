-- ============================================================
-- 检索埋点：命中事件 / 采用事件的 target_type 列宽 16 → 32
-- 版本: 049
-- 对应全量 DDL: deploy/postgres/migrations/004_knowledge_base.sql
--              （原 008_search_events.sql 已并入 004，见 004 内注释）
-- 依据: 数据运营「被引用数据资产数 / 高频引用资产 Top10 不更新」故障定位
--       （E:\ZYwork\修复\数据运营-被引用数据资产数不更新-埋点列宽截断-修复方案.md）
--
-- 为什么需要本脚本：
--   写入器对本体命中事件写 target_type = 'ontology_instance'（**17 字符**，且不走 _clip），
--   见 capabilities/knowledge_base/services/search_event_writer.py:79 与 _extract_hits()。
--   而这两张表的 target_type 声明为 VARCHAR(16)，于是任何「产出本体命中」的检索
--   （混合链路 /search/mix 必产）INSERT 直接报：
--       value too long for type character varying(16)
--   而 write_trace_events 是**一个事务写「步骤 + 命中 + 采用」三类事件**，
--   一条超长即整批回滚、异常又被裸 except 吞掉（只留一条 warning），检索照常返回。
--   采用事件是「被引用数据资产数」「高频引用资产 Top10」**唯一**的数据源，
--   命中事件是「本体检索 / RAG 检索参与率」的数据源 —— 于是这些指标长期不动或恒为 0；
--   而请求事件写在 handler 之前、单独事务提交，所以「检索请求数」照常在涨，
--   现场看起来就是「请求数在涨、引用数不涨」。
--
-- 只放宽长度：不改数据、不改语义、不改接口形状。
--   'document' = 8 字符，'ontology_instance' = 17 字符，均在新宽度内。
--   放宽 varchar 上限不重写数据、不加长锁事务。
-- 幂等：重复执行无副作用。
-- 回滚提示：本变更视为**单向** —— 一旦表中存在 17 字符的值，
--   改回 VARCHAR(16) 会因超长失败，需要先删掉那些行。
-- ============================================================

ALTER TABLE knowledge_base.search_hit_event
  ALTER COLUMN target_type TYPE VARCHAR(32);

ALTER TABLE knowledge_base.search_adoption_event
  ALTER COLUMN target_type TYPE VARCHAR(32);

COMMENT ON COLUMN knowledge_base.search_hit_event.target_type IS
  '命中目标类型：document（RAG 召回文档）/ ontology_instance（本体实例）。宽度须容纳最长的 17 字符值，不得收窄';

COMMENT ON COLUMN knowledge_base.search_adoption_event.target_type IS
  '采用目标类型：document（RAG 召回文档）/ ontology_instance（本体实例）。宽度须容纳最长的 17 字符值，不得收窄';
