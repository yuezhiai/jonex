-- ============================================================
-- 080_task_center_drop_catalog.sql
-- 删除任务中心的「内置任务」这一层，让编排不再受任务类型约束
--
-- 背景
--   以前任务中心的定义必须先选一个内置任务（task_catalog），节点清单按
--   task_catalog_steps.task_catalog_code 挂在各自任务下。产品要的是「可自由编排」：
--   节点由全局步骤清单定义，编排时直接选，任务类型这一层反而是限制。
--
-- 本次改动
--   1. 未终态、且编排里还带 ingest 节点的任务 → 任务 / 运行 / 子任务 / 子任务尝试
--      四表一起收成 failed（发布时 SQL 一并执行，不留悬空任务）。
--      ⚠️ 运行方那边可能还在跑 ingest，迟到的上报会被任务中心的「已是终态」闸丢弃。
--   2. task_catalog_steps 去掉 task_catalog_code，唯一键改为 step_code；
--      只保留 parse / compile 两条（ingest 不再预置）。
--   3. 四个预置定义各发一版新编排（两步 + output_schema: {}），并把
--      current_version_no 向前推。历史已发布版本不删不改；历史在途任务按旧快照
--      自己失败（与 053 的口径一致）。
--   4. 删除 task_catalog 表，以及 task_definitions / tasks 上的 task_catalog_code。
--
-- 幂等
--   以「task_catalog_steps.task_catalog_code 列是否还在」作为「是否已迁移」的判据，
--   整段结构迁移包在一个 DO 块里；重复执行只跳过，不会误删后来新增的步骤行。
--
-- 执行前提
--   `apply.sh` 以 `psql -v ON_ERROR_STOP=1` 执行，本文件自带 BEGIN/COMMIT：
--   断言失败即整份回滚、且不会被登记进 public.schema_migrations（修复后重跑即可）。
-- ============================================================

BEGIN;

DO $tc080$
DECLARE
    v_new_version RECORD;
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM information_schema.columns
         WHERE table_schema = 'task_center'
           AND table_name   = 'task_catalog_steps'
           AND column_name  = 'task_catalog_code'
    ) THEN
        RAISE NOTICE '080：task_catalog_code 已不存在，视为已迁移，跳过';
        RETURN;
    END IF;

    -- --------------------------------------------------------
    -- 1. 受影响任务：未终态 + 编排里含 ingest 节点
    -- --------------------------------------------------------
    CREATE TEMP TABLE tc080_affected (
        task_id    VARCHAR(64) PRIMARY KEY,
        attempt_id VARCHAR(64) NOT NULL
    ) ON COMMIT DROP;

    INSERT INTO tc080_affected (task_id, attempt_id)
    SELECT t.id, ta.id
      FROM task_center.tasks t
      JOIN task_center.task_attempts ta
        ON ta.task_id = t.id
       AND ta.attempt = t.current_attempt
     WHERE t.is_deleted = 0
       AND t.status IN ('pending', 'running')
       AND EXISTS (
           SELECT 1
             FROM jsonb_array_elements(t.definition_config -> 'steps') s
            WHERE s ->> 'step_code' = 'ingest'
               OR s ->> 'node_id'   = 'ingest_1'
       );

    RAISE NOTICE '080：受影响任务 % 条，将直接判失败', (SELECT count(*) FROM tc080_affected);

    UPDATE task_center.subtask_attempts sa
       SET status = 'failed',
           finished_at = CURRENT_TIMESTAMP
      FROM task_center.subtasks s
      JOIN tc080_affected a
        ON a.task_id = s.task_id
       AND a.attempt_id = s.task_attempt_id
     WHERE sa.subtask_id = s.id
       AND sa.status IN ('pending', 'running');

    UPDATE task_center.subtasks s
       SET status = 'failed',
           finished_at = CURRENT_TIMESTAMP,
           updated_at = CURRENT_TIMESTAMP
      FROM tc080_affected a
     WHERE s.task_id = a.task_id
       AND s.task_attempt_id = a.attempt_id
       AND s.status IN ('pending', 'running')
       AND s.is_deleted = 0;

    UPDATE task_center.task_attempts ta
       SET status = 'failed',
           finished_at = CURRENT_TIMESTAMP,
           error_code = 'err.task_center.task.catalog_removed',
           error_params = '{}'::jsonb,
           error_message = '内置任务清单已下线，运行中的任务被终止，请重新提交'
      FROM tc080_affected a
     WHERE ta.id = a.attempt_id
       AND ta.status IN ('pending', 'running');

    UPDATE task_center.tasks t
       SET status = 'failed',
           finished_at = CURRENT_TIMESTAMP,
           error_code = 'err.task_center.task.catalog_removed',
           error_params = '{}'::jsonb,
           error_message = '内置任务清单已下线，运行中的任务被终止，请重新提交',
           state_version = t.state_version + 1,
           updated_at = CURRENT_TIMESTAMP
      FROM tc080_affected a
     WHERE t.id = a.task_id;

    -- --------------------------------------------------------
    -- 2. 步骤清单全局化：只保留 parse / compile 两条
    -- --------------------------------------------------------
    DELETE FROM task_center.task_catalog_steps;

    INSERT INTO task_center.task_catalog_steps
      (id, task_catalog_code, step_code, name, runner_code, max_concurrency,
       timeout_seconds, input_schema, output_schema)
    VALUES
      ('step-parse', '__global__', 'parse', '文件解析', 'knowledge_base', 10, 25200,
       '{"document_id":{"type":"string","required":true},"knowledge_base_id":{"type":"string","required":true}}',
       '{"document_id":{"type":"string","required":true}}'),
      ('step-compile', '__global__', 'compile', '知识编译', 'knowledge_base', 10, 25200,
       '{"document_id":{"type":"string","required":true},"knowledge_base_id":{"type":"string","required":true}}',
       '{}')
    ON CONFLICT DO NOTHING;

    -- --------------------------------------------------------
    -- 3. 四个预置定义各发一版：两步 + output_schema
    --    版本 id 沿用旧数据命名：version-{去掉 definition- 前缀}-v{N}
    -- --------------------------------------------------------
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
               AND NOT EXISTS (
                       SELECT 1
                         FROM task_center.task_definition_versions v
                        WHERE v.definition_id = d.id
                          AND v.config ? 'output_schema'
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
            '{"input_schema":{"document_id":{"type":"string","required":true},"knowledge_base_id":{"type":"string","required":true}},"output_schema":{},"entry":"parse_1","steps":[{"node_id":"parse_1","step_code":"parse","depends_on":[],"next_steps":["compile_1"],"sort":1,"timeout_seconds":25200,"bind_inputs":{"document_id":"@task.inputs.document_id","knowledge_base_id":"@task.inputs.knowledge_base_id"}},{"node_id":"compile_1","step_code":"compile","depends_on":["parse_1"],"next_steps":[],"sort":2,"timeout_seconds":25200,"bind_inputs":{"document_id":"@parse_1.outputs.document_id","knowledge_base_id":"@task.inputs.knowledge_base_id"}}]}'::jsonb,
            CURRENT_TIMESTAMP
        )
        ON CONFLICT DO NOTHING;
    END LOOP;

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
             AND config ? 'output_schema'
           ORDER BY definition_id, version_no DESC
      ) v
     WHERE d.id = v.definition_id
       AND d.current_version_no < v.version_no;

END
$tc080$;

-- ------------------------------------------------------------
-- 4. 结构收口：删旧索引 / 旧列 / 旧表
--
--    这几条放在 DO 块**外面**、且全部带 IF EXISTS —— 既幂等，也让
--    `test_task_center_schema_parity.py` 的 DDL 解析器看得见（它只解析顶层语句）。
-- ------------------------------------------------------------
DROP INDEX IF EXISTS task_center.uq_tc_cs_code;
ALTER TABLE task_center.task_catalog_steps DROP COLUMN IF EXISTS task_catalog_code;
CREATE UNIQUE INDEX IF NOT EXISTS uq_tc_cs_step_code
    ON task_center.task_catalog_steps(step_code);

DROP TABLE IF EXISTS task_center.task_catalog;
ALTER TABLE task_center.task_definitions DROP COLUMN IF EXISTS task_catalog_code;
ALTER TABLE task_center.tasks DROP COLUMN IF EXISTS task_catalog_code;

-- ============================================================
-- 收尾断言
-- ============================================================
DO $tc080_assert$
DECLARE
    v_bad TEXT;
BEGIN
    -- 步骤清单恰好两条
    SELECT string_agg(step_code, ', ' ORDER BY step_code) INTO v_bad
      FROM task_center.task_catalog_steps;
    IF v_bad IS DISTINCT FROM 'compile, parse' THEN
        RAISE EXCEPTION '080 断言失败：task_catalog_steps 应恰好为 compile/parse，实际 %', v_bad;
    END IF;

    -- 三处列与 task_catalog 表都不在
    SELECT string_agg(x.name, ', ') INTO v_bad
      FROM (VALUES
              ('table:task_catalog'),
              ('col:task_catalog_steps.task_catalog_code'),
              ('col:task_definitions.task_catalog_code'),
              ('col:tasks.task_catalog_code')
           ) AS x(name)
     WHERE (
             x.name = 'table:task_catalog' AND EXISTS (
                 SELECT 1 FROM information_schema.tables
                  WHERE table_schema='task_center' AND table_name='task_catalog'
             )
           )
        OR (
             x.name <> 'table:task_catalog' AND EXISTS (
                 SELECT 1 FROM information_schema.columns
                  WHERE table_schema='task_center'
                    AND table_name = split_part(split_part(x.name, ':', 2), '.', 1)
                    AND column_name = split_part(x.name, '.', 2)
             )
           );
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION '080 断言失败：以下对象仍然存在：%', v_bad;
    END IF;

    -- 没有未终态任务还引用 ingest
    SELECT string_agg(t.id, ', ' ORDER BY t.id) INTO v_bad
      FROM task_center.tasks t
     WHERE t.is_deleted = 0
       AND t.status IN ('pending', 'running')
       AND EXISTS (
           SELECT 1 FROM jsonb_array_elements(t.definition_config -> 'steps') s
            WHERE s ->> 'step_code' = 'ingest'
               OR s ->> 'node_id'   = 'ingest_1'
       );
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION '080 断言失败：仍有未终态任务引用 ingest：%', v_bad;
    END IF;

    -- 四个预置定义的当前版本都带 output_schema
    SELECT string_agg(d.id, ', ' ORDER BY d.id) INTO v_bad
      FROM task_center.task_definitions d
     WHERE d.id IN (
               'definition-file-processing-ontology',
               'definition-file-processing-ontology-wiki',
               'definition-file-processing-jonex-wiki'
           )
       AND NOT EXISTS (
               SELECT 1
                 FROM task_center.task_definition_versions v
                WHERE v.definition_id = d.id
                  AND v.version_no = d.current_version_no
                  AND v.config ? 'output_schema'
           );
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION '080 断言失败：这些定义的当前版本没有 output_schema：%', v_bad;
    END IF;
END
$tc080_assert$;

COMMIT;
