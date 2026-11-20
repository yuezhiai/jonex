-- ============================================================================
-- 任务定义管理改造（2026-11-18）
--
-- 覆盖：
--   1. scope 收窄为 system/space（删 tenant）
--   2. task_definitions 加 status / created_by / updated_by + 名称唯一索引
--   3. subtasks 加自动重试三列；subtasks / subtask_attempts 的 status 加 skipped
--   4. task_catalog_steps 的 parse / compile 换成新契约
--   5. 系统级定义 4 -> 1（改名 + 主键 UPDATE + 其余软删），并发布 v3
--
-- ⚠️ 执行顺序：停任务中心/知识库服务 → 执行本脚本 → 部署新代码 → 起服务。
--    原因：定义主键改名后旧代码的常量会找不到定义；catalog 换 schema 也会影响
--    在途任务的下一轮收口，必须先排空在途任务。
--
-- 幂等：约束用 DROP IF EXISTS + ADD；列用 ADD COLUMN IF NOT EXISTS；
--       数据用 WHERE 条件；v3 用 ON CONFLICT DO NOTHING。
-- ============================================================================

BEGIN;

-- ── 0. 前置断言 + 清理无主残留 ────────────────────────────────
-- 只允许 system / space 两种 scope 存活。已知库里**没有活跃的** tenant 级定义；
-- 但可能留着**已软删**的 tenant 行（历史测试/旧功能遗留），而 CHECK 对软删行同样
-- 生效 —— 这类行已经无主（定义软删、版本随定义软删），物理删掉即可。
DO $$
DECLARE
    v_tenant_alive integer;
BEGIN
    SELECT count(*) INTO v_tenant_alive
      FROM task_center.task_definitions
     WHERE scope = 'tenant' AND is_deleted = 0;
    IF v_tenant_alive > 0 THEN
        RAISE EXCEPTION
            '存在 % 条活跃的 tenant 级任务定义，本脚本不处理这类存量，请人工迁移后再执行',
            v_tenant_alive;
    END IF;
END $$;

-- 软删的 tenant 行连同其版本一并物理删除（它们已经读不到、跑不了）。
DELETE FROM task_center.task_definition_versions
 WHERE definition_id IN (
     SELECT id FROM task_center.task_definitions
      WHERE scope = 'tenant' AND is_deleted = 1
 );

DELETE FROM task_center.task_definitions
 WHERE scope = 'tenant' AND is_deleted = 1;

-- ── 1. scope 收窄 ─────────────────────────────────────────────
ALTER TABLE task_center.task_definitions
    DROP CONSTRAINT IF EXISTS ck_tc_def_scope;
ALTER TABLE task_center.task_definitions
    ADD CONSTRAINT ck_tc_def_scope CHECK (scope IN ('system', 'space'));

ALTER TABLE task_center.task_definitions
    DROP CONSTRAINT IF EXISTS ck_tc_def_scope_fields;
ALTER TABLE task_center.task_definitions
    ADD CONSTRAINT ck_tc_def_scope_fields CHECK (
        (scope = 'system' AND tenant_id IS NULL     AND space_id IS NULL)
        OR (scope = 'space' AND tenant_id IS NOT NULL AND space_id IS NOT NULL)
    );

ALTER TABLE task_center.task_definition_versions
    DROP CONSTRAINT IF EXISTS ck_tc_ver_scope;
ALTER TABLE task_center.task_definition_versions
    ADD CONSTRAINT ck_tc_ver_scope CHECK (scope IN ('system', 'space'));

ALTER TABLE task_center.task_definition_versions
    DROP CONSTRAINT IF EXISTS ck_tc_ver_scope_fields;
ALTER TABLE task_center.task_definition_versions
    ADD CONSTRAINT ck_tc_ver_scope_fields CHECK (
        (scope = 'system' AND tenant_id IS NULL     AND space_id IS NULL)
        OR (scope = 'space' AND tenant_id IS NOT NULL AND space_id IS NOT NULL)
    );

-- ── 2. 定义状态与操作人 ───────────────────────────────────────
-- ⚠️ 只在**第一次加列**时回填：脚本支持重跑（apply.sh --only），而重跑不能把
-- 用户后来「停用」的定义改回 published（审计发现：那会静默重新启用触发器）。
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = 'task_center'
           AND table_name = 'task_definitions'
           AND column_name = 'status'
    ) THEN
        ALTER TABLE task_center.task_definitions
            ADD COLUMN status character varying(16)
                DEFAULT 'draft'::character varying NOT NULL;
        -- 存量回填：有已发布版本的视为已发布，其余保持草稿
        UPDATE task_center.task_definitions
           SET status = 'published'
         WHERE current_version_no > 0;
    END IF;
END $$;

ALTER TABLE task_center.task_definitions
    ADD COLUMN IF NOT EXISTS created_by character varying(64);
ALTER TABLE task_center.task_definitions
    ADD COLUMN IF NOT EXISTS updated_by character varying(64);

ALTER TABLE task_center.task_definitions
    DROP CONSTRAINT IF EXISTS ck_tc_def_status;
ALTER TABLE task_center.task_definitions
    ADD CONSTRAINT ck_tc_def_status CHECK (status IN ('draft', 'published', 'disabled'));

-- ── 3. 空间级定义名称唯一（先查重名，有冲突就中止并列出） ──────
DO $$
DECLARE
    v_dup record;
BEGIN
    FOR v_dup IN
        SELECT tenant_id, space_id, name, count(*) AS cnt
          FROM task_center.task_definitions
         WHERE is_deleted = 0
           AND scope = 'space'
         GROUP BY tenant_id, space_id, name
        HAVING count(*) > 1
    LOOP
        RAISE EXCEPTION
            '空间级定义名称重复：tenant=% space=% name=% (% 条)，请先人工改名',
            v_dup.tenant_id, v_dup.space_id, v_dup.name, v_dup.cnt;
    END LOOP;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_tc_def_space_name
    ON task_center.task_definitions USING btree (tenant_id, space_id, name)
    WHERE (is_deleted = 0 AND scope = 'space');

-- ── 4. 子任务自动重试列 + skipped 状态 ───────────────────────
ALTER TABLE task_center.subtasks
    ADD COLUMN IF NOT EXISTS max_retries integer DEFAULT 0 NOT NULL;
ALTER TABLE task_center.subtasks
    ADD COLUMN IF NOT EXISTS auto_retries_used integer DEFAULT 0 NOT NULL;
ALTER TABLE task_center.subtasks
    ADD COLUMN IF NOT EXISTS on_retry_exhausted character varying(16)
        DEFAULT 'terminate'::character varying NOT NULL;

ALTER TABLE task_center.subtasks
    DROP CONSTRAINT IF EXISTS ck_tc_sub_status;
ALTER TABLE task_center.subtasks
    ADD CONSTRAINT ck_tc_sub_status CHECK (status IN (
        'pending', 'running', 'success', 'failed', 'timeout', 'stopped', 'skipped'
    ));

ALTER TABLE task_center.subtask_attempts
    DROP CONSTRAINT IF EXISTS ck_tc_sa_status;
ALTER TABLE task_center.subtask_attempts
    ADD CONSTRAINT ck_tc_sa_status CHECK (status IN (
        'pending', 'running', 'success', 'failed', 'timeout', 'stopped', 'skipped'
    ));

-- ── 4b. 接管行加「是否最后一步」（只有解析、没有编译的链要它收口） ──
ALTER TABLE knowledge_base.task_center_documents
    ADD COLUMN IF NOT EXISTS last_step boolean DEFAULT false NOT NULL;

-- ── 5. 内置步骤新契约 ────────────────────────────────────────
UPDATE task_center.task_catalog_steps
   SET input_schema = '{
         "document": {"type": "file", "required": true},
         "knowledge_id": {"type": "string", "required": true}
       }'::jsonb,
       output_schema = '{
         "chunks": {"type": "object", "abstract": true},
         "document_id": {"type": "string", "required": true}
       }'::jsonb,
       updated_at = CURRENT_TIMESTAMP
 WHERE step_code = 'parse';

UPDATE task_center.task_catalog_steps
   SET input_schema = '{
         "chunks": {"type": "object", "abstract": true, "required": true},
         "knowledge_id": {"type": "string", "required": true},
         "document_id": {"type": "string", "required": true}
       }'::jsonb,
       output_schema = '{
         "ontology": {"type": "object", "abstract": true},
         "wiki": {"type": "object", "abstract": true}
       }'::jsonb,
       updated_at = CURRENT_TIMESTAMP
 WHERE step_code = 'compile';

-- ── 6. 系统级定义 4 -> 1 ─────────────────────────────────────
-- 先改子表（版本、任务），再改定义主键；DDL 没有外键，靠脚本保证三处一致。
UPDATE task_center.task_definition_versions
   SET definition_id = 'definition-kb-document-parse-compile'
 WHERE definition_id = 'definition-file-processing-ontology';

UPDATE task_center.tasks
   SET definition_id = 'definition-kb-document-parse-compile'
 WHERE definition_id = 'definition-file-processing-ontology';

UPDATE task_center.task_definitions
   SET id = 'definition-kb-document-parse-compile',
       name = '知识库文档解析与编译',
       description = '知识库上传文件后自动解析，并按库类型完成编译'
 WHERE id = 'definition-file-processing-ontology';

-- **触发器也要跟着改**（审计发现：漏了第四处引用）——
-- 不改的话，指向旧 id 的 cron 触发器会被扫描 SQL 静默跳过（既不触发也不报错）。
UPDATE task_center.task_triggers
   SET definition_id = 'definition-kb-document-parse-compile'
 WHERE definition_id = 'definition-file-processing-ontology';

-- 其余 2 条软删（定义 + 版本）；它们名下的历史任务不改指针，靠快照跑完
UPDATE task_center.task_definition_versions
   SET is_deleted = 1
 WHERE definition_id IN (
       'definition-file-processing-ontology-wiki',
       'definition-file-processing-jonex-wiki'
   );

UPDATE task_center.task_definitions
   SET is_deleted = 1,
       updated_at = CURRENT_TIMESTAMP
 WHERE id IN (
       'definition-file-processing-ontology-wiki',
       'definition-file-processing-jonex-wiki'
   );

-- 被软删定义名下的触发器一并下线（与 def.delete 的级联口径一致）
UPDATE task_center.task_triggers
   SET status = 'deleted'
 WHERE definition_id IN (
       'definition-file-processing-ontology-wiki',
       'definition-file-processing-jonex-wiki'
   );

-- ── 7. 合并后的定义发布 v3（新编排结构 + 新变量契约） ─────────
-- 旧 v1/v2 保持原样（已发布不可改），新代码读时会把旧结构规范化为新结构。
INSERT INTO task_center.task_definition_versions
    (id, scope, tenant_id, definition_id, space_id, version_no, status, config, published_at)
VALUES (
    'version-kb-document-parse-compile-v3',
    'system', NULL, 'definition-kb-document-parse-compile', NULL,
    3,
    'published',
    '{
      "start": {
        "node_id": "start",
        "inputs": {
          "document": {"type": "file", "required": true},
          "knowledge_id": {"type": "string", "required": true}
        }
      },
      "steps": [
        {
          "node_id": "parse_1",
          "step_code": "parse",
          "depends_on": [],
          "next_steps": ["compile_1"],
          "sort": 1,
          "timeout_seconds": 25200,
          "max_retries": 0,
          "on_retry_exhausted": "terminate",
          "bind_inputs": {
            "document": "@task.inputs.document",
            "knowledge_id": "@task.inputs.knowledge_id"
          }
        },
        {
          "node_id": "compile_1",
          "step_code": "compile",
          "depends_on": ["parse_1"],
          "next_steps": [],
          "sort": 2,
          "timeout_seconds": 25200,
          "max_retries": 0,
          "on_retry_exhausted": "terminate",
          "bind_inputs": {
            "chunks": "@parse_1.outputs.chunks",
            "knowledge_id": "@task.inputs.knowledge_id",
            "document_id": "@parse_1.outputs.document_id"
          }
        }
      ],
      "end": {"node_id": "end", "outputs": {}}
    }'::jsonb,
    CURRENT_TIMESTAMP
)
ON CONFLICT DO NOTHING;

-- 只推进版本号；`status` 由第 2 节的首次回填负责（重跑不得重置用户的停用状态）。
UPDATE task_center.task_definitions
   SET current_version_no = 3,
       updated_at = CURRENT_TIMESTAMP
 WHERE id = 'definition-kb-document-parse-compile';

COMMIT;
