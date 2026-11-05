-- 042: 抽象 schema 从「概念簇」升级为完整 schema（entities + relations）。
-- 只改列名与注释；内容形状由 .superpowers/sdd/migrate_abstract_v2.py 迁移。
-- 幂等：按 information_schema 判列名，重复执行无副作用。

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'knowledge_base'
          AND table_name = 'ontology_abstract_schemas'
          AND column_name = 'concepts'
    ) THEN
        ALTER TABLE knowledge_base.ontology_abstract_schemas
            RENAME COLUMN concepts TO schema;
    END IF;
END $$;

-- 040 建的列默认值是 v1 形状的 '[]'::jsonb；v2 的空形状是对象。
-- 它跟着 RENAME 一起留在列上，不显式改的话「不带值 INSERT」会写进一个数组。
ALTER TABLE knowledge_base.ontology_abstract_schemas
    ALTER COLUMN schema SET DEFAULT '{}'::jsonb;

COMMENT ON COLUMN knowledge_base.ontology_abstract_schemas.schema IS
    'v2 完整抽象 schema：{entities:[{name,display_name,description,aliases,sources[{data_source_id,entity_name,included}],attributes[…]},…], relations:[{name,source,target,cardinality,description,backings[{data_source_id,relation_name,source_column,target_column,origin}]},…]}（v1 时代存的是概念数组；迁移后一律 v2）';
