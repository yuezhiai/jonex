-- 041: 给编译 schema 加「证据」列（P1-1 生成期有界探测的落点）。
--
-- 为什么**另起一列**而不是写进 entity_types[].attributes：
-- 那一层是白名单式重建的（`_normalize_entity_types` + DTO + 前端画布的 ATTRIBUTE_EXTRAS），
-- 漏任一处的表现都是**静默丢字段**；而证据与语义的生命周期也不同（证据会过期、语义不会）。
-- 独立列不在任何白名单里，且 `upsert_compiled_schema` 的 UPDATE 显式列举列名、不碰它。
ALTER TABLE knowledge_base.ontology_compiled_schemas
    ADD COLUMN IF NOT EXISTS evidence JSONB NOT NULL DEFAULT '{}'::jsonb;
