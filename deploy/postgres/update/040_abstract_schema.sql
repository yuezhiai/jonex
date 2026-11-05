-- =============================================================================
-- 040 · 跨数据源的抽象 schema（概念对齐层）
--
-- 背景：TBox 里的 `entity_types` **从来不参与检索**（只有 relation 的
--   `inverse_of` 参与，见 search_service.py:2192 的 _build_inverse_map），
--   而实体识别（search_service.py:2086 的 _match_ontology）只读 Neo4j。
--   于是「接了数据库数据源的知识库，检索时用不到它那份 schema」——不是接线
--   问题，是这块从来没实现过。
--
-- 本表存「把该库所有数据源的 schema 抽象起来」的那一份对齐结果，形态是
-- **一库一份**（决策 6：作用域是知识库内跨数据源，不是跨知识库）。
--
-- concepts 的形状 —— **源映射随概念走**，这是「分源列举 + 标注来源」能落地的前提：
--
--   [ { "name": "不良",
--       "description": "设备/工序产生的不合格品",
--       "aliases": ["缺陷", "不良品"],
--       "sources": [
--         { "data_source_id": "<引用行 id>", "entity_name": "EmployeeDefectRate" },
--         { "data_source_id": "<引用行 id>", "entity_name": "DefectRecord" }
--       ] } ]
--
--   ⚠️ data_source_id 指向 knowledge_data_sources.id（**知识库引用行**），
--      不是 data_source_connections.id（连接池）—— 与 ontology_compiled_schemas
--      .data_source_id 同一口径（见 037 与本仓 2026-09-24 连接池设计 §二）。
--
-- status：proposed（LLM 提的候选，**未确认不参与检索**，决策 4）
--       / active（人工确认后才生效）。
--
-- 失效语义（决策 7）：源 schema 再生后对齐可能指向不存在的实体 →
--   **标脏 + 界面提示，不自动改**。标脏是**读时算的派生量**，所以本表
--   **没有** stale 列 —— DB 里的 concepts 永远是人确认过的那一份。
--
-- 幂等：全部 IF [NOT] EXISTS，apply.sh 可重复跑。
-- =============================================================================

CREATE TABLE IF NOT EXISTS knowledge_base.ontology_abstract_schemas (
    id                VARCHAR(64)  PRIMARY KEY,
    tenant_id         VARCHAR(64)  NOT NULL,
    knowledge_base_id VARCHAR(128) NOT NULL,
    schema_version    INTEGER      NOT NULL DEFAULT 1,
    status            VARCHAR(32)  NOT NULL DEFAULT 'proposed',
    concepts          JSONB        NOT NULL DEFAULT '[]'::jsonb,
    source_hash       VARCHAR(64),
    edited_at         TIMESTAMPTZ,
    edited_by         VARCHAR(128),
    is_deleted        SMALLINT     NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- 一库一份。这里没有可空列参与键，所以一条普通部分唯一索引就够
-- （对比 037 的两个部分唯一索引：那边 data_source_id 可空，而 PG 的
--  UNIQUE 索引**不约束 NULL**，一行拆成两条才挡得住）。
CREATE UNIQUE INDEX IF NOT EXISTS uq_oas_per_kb
    ON knowledge_base.ontology_abstract_schemas (tenant_id, knowledge_base_id)
    WHERE is_deleted = 0;

COMMENT ON TABLE knowledge_base.ontology_abstract_schemas IS
    '跨数据源的抽象 schema（概念对齐层），一知识库一份；concepts 里的 sources 自带源映射';
COMMENT ON COLUMN knowledge_base.ontology_abstract_schemas.schema_version IS
    '每保存一次 +1（生成候选、改名、合并、确认都算），供界面做乐观锁';
COMMENT ON COLUMN knowledge_base.ontology_abstract_schemas.status IS
    'proposed = LLM 提的候选（不参与检索）/ active = 人工确认后生效';
COMMENT ON COLUMN knowledge_base.ontology_abstract_schemas.concepts IS
    '抽象概念列表 [{name, description, aliases, sources:[{data_source_id, entity_name}]}]';
COMMENT ON COLUMN knowledge_base.ontology_abstract_schemas.source_hash IS
    '生成时各源 (data_source_id, entity_name) 的指纹，供界面判断要不要重新生成';
COMMENT ON COLUMN knowledge_base.ontology_abstract_schemas.edited_by IS
    '最后人工确认/编辑人的 user_id；NULL = 从未人工编辑过（纯 LLM 候选）';
COMMENT ON COLUMN knowledge_base.ontology_abstract_schemas.is_deleted IS
    '软删除标记：0-正常，1-删除';
