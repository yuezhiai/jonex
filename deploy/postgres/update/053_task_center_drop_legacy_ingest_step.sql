-- ============================================================
-- 053_task_center_drop_legacy_ingest_step.sql
-- 存量库的 rag 链退役遗留的 ingest 步（三步编排 → 两步）
--
-- 为什么必须修
--   2026-10 的改版把 rag 链从三步改成了两步：`parse` 步的执行档位由
--   `prepare_only`（只解析）换成 `full`（**解析 + 推切片一趟干完**），而 `ingest` 步
--   （`push_only`）从此只服务「改版前遗留的在途任务」——
--   `capabilities/knowledge_base/services/task_runner_service.py:227-241` 的
--   `_INSERT_MODES` 是唯一事实来源：(parse, lightrag) → full、(ingest, lightrag) → push_only。
--
--   但预置数据的更新只做了一半：
--     · `migrations/008_task_center.sql:487`   全新库 —— `ingest` 行**被注释掉**（两步）
--     · `update/036_task_center.sql:457`       存量库 —— 建了 `ingest` 行（三步）
--     · `update/052_task_stopped_status.sql:99-112` 开 v2 时用 `jsonb_set` 从 v1
--       **原样派生 steps**，把三步结构原封不动搬进了当前版本
--
--   后果（2026-10-15 生产实测）：走过 update 链的库，当前版本仍是
--   `parse → ingest → compile`，于是
--     ① 第一步按新档位 `full` 跑完 —— 切片**其实已经入库**，但它**不写**
--        `content_list.json`（那是 `prepare_only` 的产物，见
--        `Reference/Rag-anything/raganything/service/task_manager.py:3348-3404`；
--        `full` 只顺带写 `content.md` 给 office 预览）；
--     ② 第二步 `push_only` 回读不到产物，直接判失败
--        （`task_manager.py:3460-3469`，报「解析产物缺失或损坏（读不到 content_list.json）」）；
--     ③ 第三步 `compile`（抽本体）被**原地 hold**（§6.3）—— 本体抽取**永不执行**，
--        表现为巡检反复打 `Ontology retry limit reached (3/3)` + 本体图只剩 stub 实体。
--   净效果：每份文档都「切片已入库、任务却判失败」，而且本体侧永远等不到数据。
--
-- 修法（为什么是开新版本，而不是改 v1/v2）
--   `036_task_center.sql:550` 写死了 published 版本「发布后不可改、不可删、不可逆回 draft」，
--   且任务建单时把 `config` 整份抄进 `tasks.definition_config`
--   （`036:567`：推进任务读快照、**不反查版本**）。所以只能**发一个新版本**（版本号 +1）。
--   ⚠️ **已存在的在途任务保持各自快照不变，仍会失败** —— 它们要**重新提交**
--      （重新解析 / 重新上传）才会走到新版本。
--
-- 覆盖范围与幂等
--   · 只处理「当前已发布版本里还带着 `step_code='ingest'` 节点」的定义：把它剔除，并把
--     两张图**一起**改（`execution_repository.py:1080-1082`：`depends_on` 管调度顺序、
--     `next_steps` 管下游入参重算，只改一张会静默出错），同时改写 `bind_inputs` 里
--     指向被剔除节点的引用。改完与 `migrations/008` 种下的两步形态完全一致。
--   · 全新库（008 种的就是两步）在这里是**空操作**，`current_version_no` 不动。
--     因此修过的存量库是 v3、全新库是 v2，**两者编排一致（都是 parse→compile）**，
--     仅版本号不同 —— 这是有意的：数据修复只在有问题的地方落笔。
--   · 幂等：目标版本号已存在则跳过（`NOT EXISTS`），可重复执行。
--   · `task_catalog_steps` 里那行 `step-file-processing-ontology-ingest` **有意保留**：
--     改版前遗留的在途任务仍要它的步骤元数据（timeout / runner_code）。
--
-- ⚠️ 本文件末尾用 `DO` 块做断言（纯 SQL 无法 `RAISE`）。`apply.sh` 以
--    `psql -v ON_ERROR_STOP=1` 执行且本文件自带 `BEGIN/COMMIT`：断言失败即整份回滚、
--    且不会被登记进 `public.schema_migrations`（修复后重跑即可）。
--    断言范围 = **四条预置编排 ∪ 本次真的改过的定义**（见下方 `tc053_scope`）：四条预置
--    编排无条件纳入，所以「形状不符合预期（比如缺 parse 节点）导致本次啥也没改」不会被
--    静默放过 —— 那种情况下断言 A 会报错并整份回滚。
-- ============================================================

BEGIN;

-- ---- 待修对象：当前版本里还带着 ingest 节点、且还没开过下一版的定义 ----
-- 断言范围（`tc053_scope`）= 四条预置编排 ∪ 本次真的改过的定义：
--   · 预置四条**无条件**纳入 —— 它们的当前版本必须两步。这样「形状不符合预期、
--     目标集为空」的情况不会被静默放过：`target` 为空 ⇒ 没有新版本 ⇒ 断言 A 报错回滚。
CREATE TEMP TABLE tc053_scope (
    definition_id   TEXT PRIMARY KEY
) ON COMMIT DROP;

INSERT INTO tc053_scope (definition_id)
SELECT id
  FROM task_center.task_definitions
 WHERE id IN (
       'definition-file-processing-ontology',
       'definition-file-processing-ontology-wiki',
       'definition-file-processing-jonex-wiki'
   )
ON CONFLICT (definition_id) DO NOTHING;

WITH target AS (
    SELECT v.id AS ver_id,
           v.scope, v.tenant_id, v.definition_id, v.space_id,
           v.version_no, v.config,
           ing.step AS ing_step,
           par.step AS par_step,
           ing.step ->> 'node_id' AS ing_node,
           par.step ->> 'node_id' AS par_node
      FROM task_center.task_definition_versions v
      JOIN task_center.task_definitions d
        ON d.id = v.definition_id
       AND d.current_version_no = v.version_no
      -- 必须有 ingest 节点 —— 没有就没得修（全新库在本行被滤掉）
      JOIN LATERAL (
           SELECT s.step
             FROM jsonb_array_elements(v.config -> 'steps') AS s(step)
            WHERE s.step ->> 'step_code' = 'ingest'
            ORDER BY (s.step ->> 'sort')::int
            LIMIT 1
      ) AS ing ON TRUE
      -- 还必须认得出「去程节点」：剔掉 ingest 之后要把它接到 parse 上
      JOIN LATERAL (
           SELECT s.step
             FROM jsonb_array_elements(v.config -> 'steps') AS s(step)
            WHERE s.step ->> 'step_code' = 'parse'
            ORDER BY (s.step ->> 'sort')::int
            LIMIT 1
      ) AS par ON TRUE
     WHERE v.status = 'published'
       AND v.is_deleted = 0
       AND NOT EXISTS (
             SELECT 1
               FROM task_center.task_definition_versions x
              WHERE x.definition_id = v.definition_id
                AND x.version_no = v.version_no + 1
           )
),
rebuilt AS (
    SELECT t.*,
           -- 重建 steps：剔除 ingest 节点，其余节点原样保留，只改三处引用
           (
             SELECT jsonb_agg(
                      (s.step - 'next_steps' - 'depends_on' - 'bind_inputs')
                      -- ① next_steps：把指向 ingest 的**那一项**就地展开成 ingest 的后继
                      --    （同层顺序靠原数组下标 ord 保序，展开项用 sub 排在其后）
                      || jsonb_build_object('next_steps', coalesce((
                             SELECT jsonb_agg(u.item ORDER BY u.ord, u.sub)
                               FROM (
                                     SELECT coalesce(succ.item, e.elem) AS item,
                                            e.ord AS ord,
                                            coalesce(succ.sub, 0) AS sub
                                       FROM jsonb_array_elements_text(
                                              coalesce(s.step -> 'next_steps', '[]'::jsonb)
                                            ) WITH ORDINALITY AS e(elem, ord)
                                       LEFT JOIN LATERAL (
                                            SELECT x.elem AS item, x.ord AS sub
                                              FROM jsonb_array_elements_text(
                                                     coalesce(t.ing_step -> 'next_steps', '[]'::jsonb)
                                                   ) WITH ORDINALITY AS x(elem, ord)
                                             -- 只在「这一项就是 ingest」时展开；否则不产生行
                                             WHERE e.elem = t.ing_node
                                       ) AS succ ON TRUE
                               ) AS u
                         ), '[]'::jsonb))
                      -- ② depends_on：ingest → parse（本编排形状下就是 compile.depends_on）
                      || jsonb_build_object('depends_on', coalesce((
                             SELECT jsonb_agg(
                                      CASE WHEN e.elem = t.ing_node
                                           THEN to_jsonb(t.par_node)
                                           ELSE to_jsonb(e.elem) END
                                      ORDER BY e.ord)
                               FROM jsonb_array_elements_text(
                                      coalesce(s.step -> 'depends_on', '[]'::jsonb)
                                    ) WITH ORDINALITY AS e(elem, ord)
                         ), '[]'::jsonb))
                      -- ③ bind_inputs：'@ingest_1.' → '@parse_1.'（不改就取不到输入）
                      || CASE WHEN s.step ? 'bind_inputs'
                              THEN jsonb_build_object('bind_inputs', coalesce((
                                     SELECT jsonb_object_agg(
                                              b.key,
                                              replace(b.value,
                                                      '@' || t.ing_node || '.',
                                                      '@' || t.par_node || '.')
                                            )
                                       FROM jsonb_each_text(s.step -> 'bind_inputs') AS b(key, value)
                                   ), '{}'::jsonb))
                              ELSE '{}'::jsonb END
                      ORDER BY s.ord
                    )
               FROM jsonb_array_elements(t.config -> 'steps') WITH ORDINALITY AS s(step, ord)
              WHERE s.step ->> 'step_code' <> 'ingest'
           ) AS new_steps
      FROM target t
),
ins AS (
    INSERT INTO task_center.task_definition_versions
      (id, scope, tenant_id, definition_id, space_id, version_no, status, config, published_at)
    SELECT
      -- 版本 id 沿用 052 的命名法（version-...-v2 → version-...-v3）
      regexp_replace(r.ver_id, '-v[0-9]+$', '-v' || (r.version_no + 1)),
      r.scope, r.tenant_id, r.definition_id, r.space_id,
      r.version_no + 1,
      'published',
      jsonb_set(
        -- entry 若指过 ingest，一并挪到 parse（当前预置数据里 entry=parse_1，这里是防御）
        CASE WHEN r.config ->> 'entry' = r.ing_node
             THEN jsonb_set(r.config, '{entry}', to_jsonb(r.par_node))
             ELSE r.config END,
        '{steps}', r.new_steps),
      CURRENT_TIMESTAMP
      FROM rebuilt r
    RETURNING definition_id, version_no
),
mark AS (
    INSERT INTO tc053_scope (definition_id)
    SELECT definition_id FROM ins
    ON CONFLICT (definition_id) DO NOTHING
    RETURNING definition_id
)
-- 定义指向新版本（只有新版本真落了库才推）
UPDATE task_center.task_definitions d
   SET current_version_no = ins.version_no,
       updated_at = CURRENT_TIMESTAMP
  FROM ins
 WHERE d.id = ins.definition_id;

-- ---- 断言：产物必须真的收敛，否则整份回滚 ----
DO $$
DECLARE
    bad RECORD;
BEGIN
    -- 断言 A：修过的定义，当前版本里不能再有 ingest 节点
    FOR bad IN
        SELECT t.definition_id,
               d.current_version_no AS ver,
               (SELECT string_agg(s.step ->> 'step_code', '→' ORDER BY s.ord)
                  FROM jsonb_array_elements(v.config -> 'steps') WITH ORDINALITY AS s(step, ord)
               ) AS chain
          FROM tc053_scope t
          JOIN task_center.task_definitions d
            ON d.id = t.definition_id
          JOIN task_center.task_definition_versions v
            ON v.definition_id = d.id
           AND v.version_no = d.current_version_no
         WHERE EXISTS (
                 SELECT 1
                   FROM jsonb_array_elements(v.config -> 'steps') AS s(step)
                  WHERE s.step ->> 'step_code' = 'ingest'
               )
    LOOP
        RAISE EXCEPTION
            '053 未收敛：% 的当前版本 v% 仍带着 ingest 步（%）。'
            '要么形状不符合预期（缺少 parse 节点 / 已存在下一版），要么本次改写没落上 —— 已整份回滚，请人工核对该定义的编排',
            bad.definition_id, bad.ver, bad.chain;
    END LOOP;

    -- 断言 B：两张图必须互为对偶、且没有悬空引用（发版校验 N4 的那条不变量）
    FOR bad IN
        SELECT t.definition_id,
               d.current_version_no AS ver,
               x.msg
          FROM tc053_scope t
          JOIN task_center.task_definitions d
            ON d.id = t.definition_id
          JOIN task_center.task_definition_versions v
            ON v.definition_id = d.id
           AND v.version_no = d.current_version_no
          CROSS JOIN LATERAL (
               SELECT string_agg(m, '; ') AS msg
                 FROM (
                       -- next_steps 里指到的节点，其 depends_on 必须包含本节点
                       -- ⚠️ `->>` 与 `||` 同优先级左结合：拼接前必须把取值括起来，
                       --    否则会被解析成 `('文本' || step) ->> 'node_id'`（把中文串当 jsonb）
                       SELECT 'next_steps 不对偶/悬空: ' || (s.step ->> 'node_id')
                              || '→' || nb.target AS m
                         FROM jsonb_array_elements(v.config -> 'steps') AS s(step)
                         CROSS JOIN LATERAL jsonb_array_elements_text(
                                       coalesce(s.step -> 'next_steps', '[]'::jsonb)
                                     ) AS nb(target)
                        WHERE NOT EXISTS (
                                SELECT 1
                                  FROM jsonb_array_elements(v.config -> 'steps') AS t2(step)
                                 WHERE t2.step ->> 'node_id' = nb.target
                                   AND coalesce(t2.step -> 'depends_on', '[]'::jsonb)
                                       ? (s.step ->> 'node_id')
                              )
                       UNION ALL
                       -- depends_on 里写到的上游，其 next_steps 必须包含本节点
                       SELECT 'depends_on 不对偶/悬空: ' || (s.step ->> 'node_id')
                              || '←' || ub.up AS m
                         FROM jsonb_array_elements(v.config -> 'steps') AS s(step)
                         CROSS JOIN LATERAL jsonb_array_elements_text(
                                       coalesce(s.step -> 'depends_on', '[]'::jsonb)
                                     ) AS ub(up)
                        WHERE NOT EXISTS (
                                SELECT 1
                                  FROM jsonb_array_elements(v.config -> 'steps') AS t3(step)
                                 WHERE t3.step ->> 'node_id' = ub.up
                                   AND coalesce(t3.step -> 'next_steps', '[]'::jsonb)
                                       ? (s.step ->> 'node_id')
                              )
                 ) AS allmsg
          ) AS x
         WHERE x.msg IS NOT NULL
    LOOP
        RAISE EXCEPTION
            '053 产物不合规（两张图不对偶）：% 的 v% —— %',
            bad.definition_id, bad.ver, bad.msg;
    END LOOP;
END
$$;

COMMIT;
