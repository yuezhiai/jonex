-- ============================================================
-- 081_task_center_step_chunk_refs.sql
-- 步骤 schema 增加 chunk 引用，并给四个预置定义各发一版绑定它的编排
--
-- 背景
--   解析产物实际上不止一个文档 id —— 还有切片（chunk）。但切片正文量大、
--   且口径归 RAG 引擎所有，塞进任务上下文既浪费又不稳。产品要的是「编排里
--   能看到并能引用 chunks」，所以这里用**引用**表达：
--
--     {"kind":"knowledge_base.chunks","params":{"document_id":...,"knowledge_base_id":...}}
--
--   前端按 kind 展示「切片」，运行方仍只用 params.document_id 去查引擎，
--   不读正文。`ref_kind` 是结构化标记，校验器允许 schema 带额外键。
--
-- 本次改动
--   1. parse.output_schema 增加 chunks（必填，解析成功必有切片引用）；
--      compile.input_schema 增加 chunks（选填，运行方当前不消费它）；
--      compile.output_schema 增加 ontology / wiki 两个**可选**字段 —— 编译产物
--      在 lightrag 库是本体、在 wiki 类库是页面树，全局只有一条 compile 步骤，
--      所以两种都声明、运行方按库类型只填其中一个。
--   2. 四个预置定义各发一版新编排：compile_1.bind_inputs 增加
--      chunks ← @parse_1.outputs.chunks，current_version_no 向前推。
--
-- 兼容
--   - 历史版本 / 历史任务快照一行不改：老任务照样按旧编排跑，重试行为不变。
--   - 老编排不带 chunks 绑定，运行期 compile.inputs.chunks 归一为 {}，
--     运行方忽略该字段，不影响执行。
--
-- 幂等
--   两条 UPDATE 只在顶层还没有 chunks 键时写入；发版以「该定义已有任一新版
--   绑定了 chunks」为判据，重复执行不会重复发版。
--
-- 执行前提
--   `apply.sh` 以 `psql -v ON_ERROR_STOP=1` 执行，本文件自带 BEGIN/COMMIT：
--   断言失败即整份回滚、且不会被登记进 public.schema_migrations。
--   本文件按文件名排在 079（删内置任务层）之后，依赖它先把步骤清单收成两条。
-- ============================================================

BEGIN;

-- ------------------------------------------------------------
-- 1. 步骤 schema：加 chunks 引用
-- ------------------------------------------------------------
UPDATE task_center.task_catalog_steps
   SET output_schema = COALESCE(output_schema, '{}'::jsonb) || '{
         "chunks": {
           "type": "object",
           "required": true,
           "ref_kind": "knowledge_base.chunks",
           "description": "切片引用（不含正文；按 params.document_id 去查）",
           "properties": {
             "kind": {
               "type": "string",
               "required": true,
               "enum": ["knowledge_base.chunks"]
             },
             "params": {
               "type": "object",
               "required": true,
               "properties": {
                 "document_id": { "type": "string", "required": true },
                 "knowledge_base_id": { "type": "string", "required": true }
               }
             }
           }
         }
       }'::jsonb,
       updated_at = CURRENT_TIMESTAMP
 WHERE step_code = 'parse'
   AND NOT (COALESCE(output_schema, '{}'::jsonb) ? 'chunks');

UPDATE task_center.task_catalog_steps
   SET input_schema = COALESCE(input_schema, '{}'::jsonb) || '{
         "chunks": {
           "type": "object",
           "required": false,
           "ref_kind": "knowledge_base.chunks",
           "description": "切片引用（选填；运行方按 document_id 查切片，不读此字段）",
           "properties": {
             "kind": {
               "type": "string",
               "required": true,
               "enum": ["knowledge_base.chunks"]
             },
             "params": {
               "type": "object",
               "required": true,
               "properties": {
                 "document_id": { "type": "string", "required": true },
                 "knowledge_base_id": { "type": "string", "required": true }
               }
             }
           }
         }
       }'::jsonb,
       updated_at = CURRENT_TIMESTAMP
 WHERE step_code = 'compile'
   AND NOT (COALESCE(input_schema, '{}'::jsonb) ? 'chunks');

-- compile 的**产出**：本体（lightrag）或 Wiki 页面树（openkb / jonex_wiki）。
-- 全局只有一条 compile 步骤，所以两个字段都声明、只填其中一个；
-- `required: false` 是刻意的 —— 同一条 schema 无法要求两类链都产出两种。
UPDATE task_center.task_catalog_steps
   SET output_schema = COALESCE(output_schema, '{}'::jsonb) || '{
         "ontology": {
           "type": "object",
           "required": false,
           "ref_kind": "knowledge_base.ontology",
           "description": "本体编译结果引用（lightrag 库；不含正文）",
           "properties": {
             "kind": {
               "type": "string",
               "required": true,
               "enum": ["knowledge_base.ontology"]
             },
             "params": {
               "type": "object",
               "required": true,
               "properties": {
                 "document_id": { "type": "string", "required": true },
                 "knowledge_base_id": { "type": "string", "required": true }
               }
             }
           }
         },
         "wiki": {
           "type": "object",
           "required": false,
           "ref_kind": "knowledge_base.wiki",
           "description": "Wiki 页面树引用（openkb / jonex_wiki 库；不含正文）",
           "properties": {
             "kind": {
               "type": "string",
               "required": true,
               "enum": ["knowledge_base.wiki"]
             },
             "params": {
               "type": "object",
               "required": true,
               "properties": {
                 "document_id": { "type": "string", "required": true },
                 "knowledge_base_id": { "type": "string", "required": true }
               }
             }
           }
         }
       }'::jsonb,
       updated_at = CURRENT_TIMESTAMP
 WHERE step_code = 'compile'
   AND NOT (
         COALESCE(output_schema, '{}'::jsonb) ? 'ontology'
     AND COALESCE(output_schema, '{}'::jsonb) ? 'wiki'
   );

-- ------------------------------------------------------------
-- 2. 四个预置定义各发一版：compile_1 绑定 parse 产出的 chunks
--    版本 id 沿用旧数据命名：version-{去掉 definition- 前缀}-v{N}
-- ------------------------------------------------------------
DO $tc081$
DECLARE
    v_new_version RECORD;
BEGIN
    FOR v_new_version IN
        WITH target AS (
            SELECT d.id,
                   d.scope,
                   d.tenant_id,
                   d.space_id,
                   (SELECT COALESCE(MAX(v.version_no), 0) + 1
                      FROM task_center.task_definition_versions v
                     WHERE v.definition_id = d.id) AS new_no
              FROM task_center.task_definitions d
             WHERE d.id IN (
                       'definition-file-processing-ontology',
                       'definition-file-processing-ontology-wiki',
                       'definition-file-processing-jonex-wiki'
                   )
               -- 已有一版绑定了 chunks 就不再发版
               AND NOT EXISTS (
                       SELECT 1
                         FROM task_center.task_definition_versions v
                        WHERE v.definition_id = d.id
                          AND COALESCE(v.config -> 'steps', '[]'::jsonb)
                              @> '[{"bind_inputs": {"chunks": "@parse_1.outputs.chunks"}}]'::jsonb
                   )
        )
        SELECT id, scope, tenant_id, space_id, new_no FROM target
    LOOP
        INSERT INTO task_center.task_definition_versions
          (id, scope, tenant_id, definition_id, space_id, version_no,
           status, config, published_at)
        VALUES (
            'version-' || regexp_replace(v_new_version.id, '^definition-', '')
                || '-v' || v_new_version.new_no,
            v_new_version.scope,
            v_new_version.tenant_id,
            v_new_version.id,
            v_new_version.space_id,
            v_new_version.new_no,
            'published',
            '{"input_schema":{"document_id":{"type":"string","required":true},"knowledge_base_id":{"type":"string","required":true}},"output_schema":{},"entry":"parse_1","steps":[{"node_id":"parse_1","step_code":"parse","depends_on":[],"next_steps":["compile_1"],"sort":1,"timeout_seconds":25200,"bind_inputs":{"document_id":"@task.inputs.document_id","knowledge_base_id":"@task.inputs.knowledge_base_id"}},{"node_id":"compile_1","step_code":"compile","depends_on":["parse_1"],"next_steps":[],"sort":2,"timeout_seconds":25200,"bind_inputs":{"document_id":"@parse_1.outputs.document_id","knowledge_base_id":"@task.inputs.knowledge_base_id","chunks":"@parse_1.outputs.chunks"}}]}'::jsonb,
            CURRENT_TIMESTAMP
        )
        ON CONFLICT DO NOTHING;
    END LOOP;

    -- 只向前推：不回退手工指定的更高版本
    UPDATE task_center.task_definitions d
       SET current_version_no = v.version_no,
           updated_at = CURRENT_TIMESTAMP
      FROM (
          SELECT DISTINCT ON (definition_id) definition_id, version_no
            FROM task_center.task_definition_versions
           WHERE definition_id IN (
                     'definition-file-processing-ontology',
                     'definition-file-processing-ontology-wiki',
                     'definition-file-processing-jonex-wiki'
                 )
             AND status = 'published'
             AND COALESCE(config -> 'steps', '[]'::jsonb)
                 @> '[{"bind_inputs": {"chunks": "@parse_1.outputs.chunks"}}]'::jsonb
           ORDER BY definition_id, version_no DESC
      ) v
     WHERE d.id = v.definition_id
       AND d.current_version_no < v.version_no;
END
$tc081$;

-- ============================================================
-- 收尾断言
-- ============================================================
DO $tc081_assert$
DECLARE
    v_bad TEXT;
BEGIN
    -- 1. parse 出参 / compile 入参都有 chunks
    SELECT string_agg(step_code || '.' || side, ', ') INTO v_bad
      FROM (
              SELECT step_code, 'output_schema' AS side, output_schema AS schema
                FROM task_center.task_catalog_steps WHERE step_code = 'parse'
              UNION ALL
              SELECT step_code, 'input_schema', input_schema
                FROM task_center.task_catalog_steps WHERE step_code = 'compile'
           ) s
     WHERE NOT (COALESCE(s.schema, '{}'::jsonb) ? 'chunks');
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION '081 断言失败：这些步骤 schema 缺 chunks：%', v_bad;
    END IF;

    -- 2. ref_kind 必须是 knowledge_base.chunks（前端靠它识别「切片」）
    SELECT string_agg(step_code || '.' || side, ', ') INTO v_bad
      FROM (
              SELECT step_code, 'output_schema' AS side, output_schema AS schema
                FROM task_center.task_catalog_steps WHERE step_code = 'parse'
              UNION ALL
              SELECT step_code, 'input_schema', input_schema
                FROM task_center.task_catalog_steps WHERE step_code = 'compile'
           ) s
     WHERE s.schema -> 'chunks' ->> 'ref_kind' IS DISTINCT FROM 'knowledge_base.chunks';
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION '081 断言失败：这些步骤的 chunks.ref_kind 不对：%', v_bad;
    END IF;

    -- 1.5 compile 的产出：ontology / wiki 两个字段都在、ref_kind 对得上
    SELECT string_agg(x.field || ':' || COALESCE(x.actual, '缺失'), ', ')
      INTO v_bad
      FROM (
              SELECT 'ontology' AS field,
                     output_schema -> 'ontology' ->> 'ref_kind' AS actual
                FROM task_center.task_catalog_steps WHERE step_code = 'compile'
              UNION ALL
              SELECT 'wiki', output_schema -> 'wiki' ->> 'ref_kind'
                FROM task_center.task_catalog_steps WHERE step_code = 'compile'
           ) x
     WHERE x.actual IS DISTINCT FROM 'knowledge_base.' || x.field;
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION '081 断言失败：compile.output_schema 的产物引用不对：%', v_bad;
    END IF;

    -- 3. 四个预置定义的当前版本都在 compile_1 上绑定了 chunks
    SELECT string_agg(d.id, ', ' ORDER BY d.id) INTO v_bad
      FROM task_center.task_definitions d
      LEFT JOIN task_center.task_definition_versions v
        ON v.definition_id = d.id
       AND v.version_no = d.current_version_no
     WHERE d.id IN (
               'definition-file-processing-ontology',
               'definition-file-processing-ontology-wiki',
               'definition-file-processing-jonex-wiki'
           )
       AND NOT COALESCE(v.config -> 'steps', '[]'::jsonb)
               @> '[{"bind_inputs": {"chunks": "@parse_1.outputs.chunks"}}]'::jsonb;
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION '081 断言失败：这些定义的当前版本没有绑定 chunks：%', v_bad;
    END IF;
END
$tc081_assert$;

COMMIT;
