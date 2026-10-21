-- ============================================================
-- 052_task_stopped_status.sql
-- 「已停止」成为任务 / 运行 / 子任务 / 子任务历史行的独立终态（存量库）
-- ＋ 编排的下游联动开关落到预置定义（开 v2 版本）
--
-- ⚠️ **只放宽四条 CHECK、不动任何列**；后半段是**预置数据**的版本升级
--    （用户 2026-10-15 定：这两个字段要「补进库里，但走新版本号」）。
--
-- 为什么要有这个状态
--   在此之前，「用户点了停止」是**用 `failed` 表达的**：任务中心把还没开始的子任务
--   逐个判死成 `failed`，任务快照与本次运行也一起收成 `failed`，只在 `error_code` 里
--   留一句 `err.task_center.task.stopped_by_user`。于是控制台上点了「停止」，
--   状态标签显示的却是「已失败」—— 看的人分不清「这条是跑挂了」还是「是我自己停的」。
--   新增 `stopped` 之后两者分开：`failed` 是跑挂了，`stopped` 是被人停的。
--
--   ⚠️ **语义边界（别读成「运行方已经停了」）**：平台只保证「自己这边不再往下推进」。
--      停止那一刻正在运行方机器上跑的那一步**杀不掉**（运行方没有「停止」这个动作），
--      它会继续跑完、照常上报，届时只更新它自己那一行 —— 任务快照已经是终态，不再变。
--      所以界面上出现「任务已停止、但某一步还在处理中」是**预期表现**，不是漏判。
--
-- 四条 CHECK 一起改的理由
--   `subtasks` 与 `subtask_attempts` 是「子任务那一行 + 它本次尝试的历史行」，
--   收口时同一时刻写同一个终态（§7.2），两边取值必须齐步；`task_attempts`（本次运行）
--   与 `tasks`（任务快照）同理 —— `_settle_stopped` 一次把两者都收成 `stopped`。
--   只改其中几条，就会出现「任务说已停止、它的运行记录说已失败」这种自相矛盾的行，
--   而排障时最怕的正是这种「库里两份记录各说各话」。
--
-- ⚠️ **不加 `DEFAULT`、不改列宽**：新状态是**写出来的**，不是默认值。
--    `status` 的默认值仍然是 `pending`（见 008 的建表段）。
--
-- ⚠️ **不刷存量数据**：库里已有的「用户手动停止」记录仍是 `failed`（它们的
--    `error_code` 是 `err.task_center.task.stopped_by_user`，可据此识别）。
--    这是有意的 —— 历史就是历史，新语义只作用于新操作。日后若要统一，
--    一条 `UPDATE ... WHERE error_code = 'err.task_center.task.stopped_by_user'` 即可。
--
-- 幂等: 四条都是 `DROP CONSTRAINT IF EXISTS` + `ADD CONSTRAINT` 的固定重建，
--       重跑无害（重建同一份约束）。
--
-- ── 后半段：把两个下游联动开关补进预置编排（**开新版本号**）──
--
-- 为什么要有这一段
--   本轮给编排的**每个节点**加了 `downstream_on_retry` / `downstream_on_stop`
--   （缺省 `true` = 从前的口径：重跑某步带上下游、停某步带上下游）。
--   **代码按「没写 = true」读**（`TaskService._downstream_chain`），所以库里不写这两个
--   字段，行为与从前**逐字相同** —— 也就是说这一段**不是功能需要**，是让库里
--   一眼看得见"这版编排的联动是怎么配的"（用户 2026-10-15 定要补）。
--
-- 为什么开新版本而不是改 v1
--   设计铁律：**已发布的版本永久固化、不可改**（§6.6，`status` 只有 draft → published
--   单向）。所以补字段只有两条路：直接改 JSONB（破纪律，且重跑 008 会被覆盖回去）
--   或**开一个 v2 再发布**。这里走后者。
--   ⚠️ `task_definitions.current_version_no` 跟着推到 2 —— 之后新建的任务用 v2；
--      **已经在跑的任务不受影响**（编排快照钉在任务行 `definition_config` 上）。
--   ⚠️ 触发器一条都没钉 `fixed_version`（本地库实测全为空），所以没有谁被落在 v1 上。
--
-- v2 的内容**从 v1 派生**（不是在这里再抄一遍长 JSON）：只给每个 step 补两个字段，
-- 其余一字不动 —— 抄一遍的话，哪天 008 的预置改了，这里就与它分叉了。
--
-- ⚠️ **新库要按顺序跑**：008（seed v1）→ 本脚本（补 v2）。008 已冻结，不改它。
--
-- 幂等: 插 v2 带 `NOT EXISTS`（重跑不会插第二条）；推版本号带 `EXISTS`（只有 v2 真在
--       才推）。整段重跑无害。
-- ============================================================

BEGIN;

-- 任务快照：`pending / running / completed / failed` → 加 `stopped`
ALTER TABLE task_center.tasks DROP CONSTRAINT IF EXISTS ck_tc_task_status;
ALTER TABLE task_center.tasks ADD CONSTRAINT ck_tc_task_status
  CHECK (status IN ('pending','running','completed','failed','stopped'));

-- 任务尝试（本次运行）：同上
ALTER TABLE task_center.task_attempts DROP CONSTRAINT IF EXISTS ck_tc_ta_status;
ALTER TABLE task_center.task_attempts ADD CONSTRAINT ck_tc_ta_status
  CHECK (status IN ('pending','running','completed','failed','stopped'));

-- 子任务：加 `stopped`（与 `failed` / `timeout` 并列的终态）
ALTER TABLE task_center.subtasks DROP CONSTRAINT IF EXISTS ck_tc_sub_status;
ALTER TABLE task_center.subtasks ADD CONSTRAINT ck_tc_sub_status
  CHECK (status IN ('pending','running','success','failed','timeout','stopped'));

-- 子任务单次尝试的历史行：与子任务那一行逐字对齐
ALTER TABLE task_center.subtask_attempts DROP CONSTRAINT IF EXISTS ck_tc_sa_status;
ALTER TABLE task_center.subtask_attempts ADD CONSTRAINT ck_tc_sa_status
  CHECK (status IN ('pending','running','success','failed','timeout','stopped'));

-- ── 预置编排：开 v2，把两个下游联动开关显式写进每个节点 ──
--
-- 内容从 v1 **派生**（`jsonb_set` 只替换 `steps`，每个 step 用 `||` 补两个键，
-- 其余一字不动）；新 id 由 v1 的 id 换尾号得来。`ORDER BY ord` 保住步骤顺序 ——
-- 顺序在这里是有意义的（同层 `sort` 与拓扑序都靠它）。
INSERT INTO task_center.task_definition_versions
  (id, scope, tenant_id, definition_id, space_id, version_no, status, config, published_at)
SELECT
  replace(v.id, '-v1', '-v2'),
  v.scope, v.tenant_id, v.definition_id, v.space_id,
  2,
  'published',
  jsonb_set(
    v.config,
    '{steps}',
    coalesce(
      (
        SELECT jsonb_agg(
                 step || '{"downstream_on_retry": true, "downstream_on_stop": true}'::jsonb
                 ORDER BY ord
               )
        FROM jsonb_array_elements(v.config -> 'steps') WITH ORDINALITY AS t(step, ord)
      ),
      '[]'::jsonb
    )
  ),
  CURRENT_TIMESTAMP
FROM task_center.task_definition_versions v
WHERE v.version_no = 1
  AND v.status = 'published'
  AND v.definition_id IN (
    'definition-file-processing-ontology',
    'definition-file-processing-ontology-wiki',
    'definition-file-processing-jonex-wiki'
  )
  AND NOT EXISTS (
    SELECT 1 FROM task_center.task_definition_versions x
    WHERE x.definition_id = v.definition_id AND x.version_no = 2
  );

-- 定义指向新版本（`EXISTS` 保证只有 v2 真落库了才推）
UPDATE task_center.task_definitions d
SET current_version_no = 2, updated_at = CURRENT_TIMESTAMP
WHERE d.id IN (
    'definition-file-processing-ontology',
    'definition-file-processing-ontology-wiki',
    'definition-file-processing-jonex-wiki'
  )
  AND d.current_version_no < 2
  AND EXISTS (
    SELECT 1 FROM task_center.task_definition_versions x
    WHERE x.definition_id = d.id AND x.version_no = 2 AND x.status = 'published'
  );

COMMIT;
