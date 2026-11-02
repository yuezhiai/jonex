-- ============================================================
-- Jonex 平台数据库初始化 - 任务中心 (task_center schema)
-- 版本: 008（2026-10-08 由 `090` **改回** `008` —— 回到最初的编号。同号**无害**：
--   `make db-migrate` 按**完整文件名**记版本（不是按编号），Docker 首启按文件名字母序执行，
--   本文件排在另一个 `008_*`（`008_search_events`）之后。
--   沿革（**四次改名都不动内容**，脚本整体幂等，已应用过的库再跑一遍是 no-op）：
--   `008_task_center.sql` →(2026-09-29，避开当时另两个 008)→ `010_task_center.sql`
--   →(2026-09-30，让开编号)→ `090_task_center.sql` →(2026-10-08)→ **`008_task_center.sql`**）
-- 设计出处: docs/superpowers/specs/2026-09-09-task-center-design.md §三
--
-- 建表顺序按业务层级自顶向下：
--   内置清单(任务/步骤) -> 运行方清单 -> 定义 -> 版本 -> 触发 ->
--   任务 -> 任务尝试 -> 子任务 -> 子任务尝试 -> 子任务上报
--
-- 约定（§3.1）：
--   * 主键统一 VARCHAR(64)，应用层生成 UUID 字符，不用自增；
--   * 定义与版本分三级：scope=system（无租户无空间）/ tenant（有租户无空间）/ space（都有）；
--   * 删除分两类：配置类表用假删 is_deleted（查询一律滤掉），状态机表用状态位，历史表不设；
--   * 不写数据库外键（FOREIGN KEY），表间关系由应用层保证；
--   * JSONB 存编排/入参/产出/依赖等半结构化数据；
--   * 枚举字段用字符串 + CHECK 限定取值；
--   * `created_at` / `updated_at` 的**可空性不是随手写的**，两类表不一样：
--     - 走 `TimestampMixin` 的四张表（`task_definitions` / `task_triggers` / `tasks` /
--       `subtasks`）由**应用显式写入**，两列一律 `NOT NULL`（与 ORM 的 `nullable=False` 一致）；
--     - 其余七张表由数据库 `DEFAULT CURRENT_TIMESTAMP` 兜底、**应用不写**（见
--       `capabilities/task_center/models/catalog.py` 的说明），故可空。
--     ⚠️ 别为了「看起来整齐」把这两类拉平 —— 拉平哪一边都会跟 ORM 对不上。
-- 全部语句幂等（IF NOT EXISTS / ON CONFLICT DO NOTHING），可重复执行。
--
-- 本文件是 `task_center` schema 的**唯一全量脚本**：建表 + 索引 + 全部预置数据
-- （四条编排链的内置清单 / 平台级定义 / 版本）都在这一份里，**并且已经把后来那些
-- 增量内容内联进来**（`subtask_reports.task_id` / `dead_lettered` 就是 037 补的两列）。
--
-- ⚠️ **本文件的定位在 2026-09-29 变过一次**（用户决定，别当成笔误）：
--   此前 = 「发布即冻结，结构变更一律走 `update/` 增量」；
--   现在 = 「**全量权威**」，存量库 / 线上按**删表重建**对齐 ——
--          `DROP TABLE ... CASCADE` 掉 `task_center` 下的表，再重跑本文件 + `042`（知识库侧两张表）。
--   为什么敢这么改：任务中心**还没有真正的业务数据**（线上零任务，库里全是测试记录），
--   而「增量叠加」让「文件里的形状」与「库里的形状」长期分叉 —— 每次读代码都要在脑子里
--   做三次合并，代价已经大于它防的风险。
--   ⚠️ 重建时**别连知识库那两张表一起删**（`knowledge_base.task_center_documents` /
--   `task_execution_registry`，在 `042` 里）：它们记的是「哪份文档被接管」，属于知识库侧
--   的业务状态，跟 `task_center` 的重建是两件事。
--
-- 仍然保留在 `update/` 的**两份**（都是 `task_center` 之外的、或非结构的）：
--   * `042_knowledge_task_center.sql`（2026-09-29 由 `038_ontology_task_center.sql` 改名；2026-09-30 再由 `037` 改为 `041`（避开与 dev 的 `037_roles_tenant_name_unique.sql` 撞号）；2026-10-08 经 `043` 定为 `042`）——
--     **知识库侧那两张协调表**（`knowledge_base.task_center_documents` /
--     `task_execution_registry`，不在 `task_center` schema 里）；它的预置数据段已并回本文件
--     （见下面的「内置清单预置数据」段）。
--   * `043_task_center_permission.sql` —— **`platform` 侧的两个权限码**
--     （`task_center:read` / `task_center:write`）与对「平台管理员」角色的授权。
--     ⚠️ **它不在 `task_center` schema 里，本文件也不含它** —— 全新的库建完本文件之后
--     **还要跑一次 `make db-migrate`**（它会 apply `update/*.sql`），否则那四个写接口的
--     权限码不在库里、谁调都回 3006。
--   * （原 `037_task_center_subtask_reports_task_id.sql`（`subtask_reports` 补两列）**已删除** ——
--     内容内联进本文件的建表体，重建库即得；不再需要增量兜底。）
--
-- 本文件全部语句幂等（`IF NOT EXISTS` / `ON CONFLICT DO NOTHING`），可重复执行；
-- 但**重复执行不会更新已存在的行**（预置数据段没有 DELETE/upsert）—— 要让库里那几行
-- 变成文件里的样子，走「删表重建」，别指望重跑。
-- ============================================================

-- 建 schema 兜底：正常由 001_schemas.sql 建好，这里重复一次是为了本文件能单独执行
-- （手工 psql 补建 / 只跑这一份的场景），IF NOT EXISTS 不会影响已建好的库。
CREATE SCHEMA IF NOT EXISTS task_center;

-- ------------------------------------------------------------
-- 内置步骤清单：编排里能选哪些节点（平台预置、只读）
-- 步骤的「固有形状」——交给哪个运行方、名字、默认超时、并发上限、输入输出 schema——都在这
--
-- ⚠️ 2026-10-23 起没有「内置任务」这一层：任务中心要支持自由编排，任务类型不再
-- 约束可选节点，步骤清单变成**全局一张表**（`step_code` 唯一）。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS task_center.task_catalog_steps (
    id                VARCHAR(64) PRIMARY KEY,
    step_code         VARCHAR(64) NOT NULL,
    name              VARCHAR(128) NOT NULL,
    runner_code       VARCHAR(64) NOT NULL,
    max_concurrency   INTEGER,
    timeout_seconds   INTEGER NOT NULL DEFAULT 300,
    input_schema      JSONB,
    output_schema     JSONB,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_tc_cs_step_code ON task_center.task_catalog_steps(step_code);

-- ------------------------------------------------------------
-- 运行方清单：平台内置（平台预置，不通过接口写；不带租户）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS task_center.runner_definitions (
    id            VARCHAR(64) PRIMARY KEY,
    code          VARCHAR(64) NOT NULL,
    name          VARCHAR(128) NOT NULL,
    capability_id VARCHAR(128) NOT NULL,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_tc_runner_code ON task_center.runner_definitions(code);

-- ------------------------------------------------------------
-- 任务定义：一类任务的「身份证」，按 scope 分三级（§3.1）
--   scope=system  系统级 —— 平台自己的定义，没有租户、没有空间（对所有租户可见，只有平台能改）
--   scope=tenant  租户级 —— 某租户的定义，只有租户、没有空间
--   scope=space   空间级 —— 某空间下的定义，租户 + 空间都有
-- 自己不存编排，只存 current_version_no 指向最新已发布版本
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS task_center.task_definitions (
    id                  VARCHAR(64) PRIMARY KEY,
    scope               VARCHAR(16) NOT NULL,
    tenant_id           VARCHAR(64),
    space_id            VARCHAR(64),
    name                VARCHAR(128) NOT NULL,
    description         VARCHAR(512),
    current_version_no  INTEGER NOT NULL DEFAULT 0,
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    is_deleted          SMALLINT NOT NULL DEFAULT 0,
    CONSTRAINT ck_tc_def_scope CHECK (scope IN ('system','tenant','space')),
    -- scope 与 tenant_id / space_id 必须配套：系统级两个都空、租户级只有租户、空间级两个都有
    CONSTRAINT ck_tc_def_scope_fields CHECK (
        (scope = 'system' AND tenant_id IS NULL     AND space_id IS NULL)
     OR (scope = 'tenant' AND tenant_id IS NOT NULL AND space_id IS NULL)
     OR (scope = 'space'  AND tenant_id IS NOT NULL AND space_id IS NOT NULL)
    )
);
-- 按租户查定义（def.list）用
CREATE INDEX IF NOT EXISTS idx_tc_def_tenant ON task_center.task_definitions(tenant_id);

-- ------------------------------------------------------------
-- 任务定义版本：真正存编排的地方（draft -> published 单向，发布后固化不可改）
-- scope / tenant_id / space_id 与所属定义一致；任务执行哪一版，就用哪一版的 space_id
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS task_center.task_definition_versions (
    id                VARCHAR(64) PRIMARY KEY,
    scope             VARCHAR(16) NOT NULL,
    tenant_id         VARCHAR(64),
    definition_id     VARCHAR(64) NOT NULL,
    space_id          VARCHAR(64),
    version_no        INTEGER NOT NULL,
    status            VARCHAR(16) NOT NULL DEFAULT 'draft',
    config            JSONB NOT NULL,
    published_at      TIMESTAMP,
    is_deleted        SMALLINT NOT NULL DEFAULT 0,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_tc_ver_status CHECK (status IN ('draft','published')),
    CONSTRAINT ck_tc_ver_scope CHECK (scope IN ('system','tenant','space')),
    -- 跟 task_definitions 一样的配套约束：scope 与 tenant_id / space_id 必须配套（§3.2）
    CONSTRAINT ck_tc_ver_scope_fields CHECK (
        (scope = 'system' AND tenant_id IS NULL     AND space_id IS NULL)
     OR (scope = 'tenant' AND tenant_id IS NOT NULL AND space_id IS NULL)
     OR (scope = 'space'  AND tenant_id IS NOT NULL AND space_id IS NOT NULL)
    )
);
-- 同一个定义的版本号不重复
--
-- 下面两条唯一索引**都没带 `is_deleted = 0`**，跟 §3.1「假删表加唯一约束就用部分索引」
-- 的惯例不同，是刻意的：版本**只能跟着定义整摊假删**（没有单独删某个版本的入口），
-- 定义一旦假删，它的版本就再没有新的写路径（`next_version_no` 也是故意不滤
-- `is_deleted` 的），所以「假删行占着槽位」不会造成任何插入冲突。反过来把假删行排除在
-- 唯一性之外，只会让约束**变松**（同号的两行、其中一行已假删，就漏过去了），一点好处没有。
-- 对照 `subtasks` 的 `uq_tc_subtask_node` —— 那条也不带 `WHERE is_deleted = 0`，理由不同：
-- 它的唯一性由「同一次运行」限定，而假删是整任务一起删、不会再往同一个 attempt 里插同名节点
-- （见下面 subtasks 处的注释）。真正需要部分索引的是带 `execution_id` 的那两条 —— 它们在
-- **未派活时为空**，才必须排除 NULL。
CREATE UNIQUE INDEX IF NOT EXISTS uq_tc_ver_no ON task_center.task_definition_versions(definition_id, version_no);
-- 平台惯例是「带 tenant_id 的表都有一条以它打头的索引」（全库 54 张带 tenant_id 的表里 53 张如此，唯一例外是 knowledge_base.knowledge_answer_feedback_event）
CREATE INDEX IF NOT EXISTS idx_tc_ver_tenant ON task_center.task_definition_versions(tenant_id);
-- 同一个定义同一时间只能有一个草稿（`is_deleted` 的处理同上）
CREATE UNIQUE INDEX IF NOT EXISTS uq_tc_ver_draft ON task_center.task_definition_versions(definition_id) WHERE status = 'draft';

-- ------------------------------------------------------------
-- 触发：什么时候、用什么参数把任务跑起来（空间级：带 tenant_id + space_id）
-- **触发器只等于定时**：没有类型列了，cron 三件套（cron_expr / next_fire_at /
-- misfire_policy）无条件必填。「手动跑一次」走 task.fire，不经触发器。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS task_center.task_triggers (
    id                VARCHAR(64) PRIMARY KEY,
    tenant_id         VARCHAR(64) NOT NULL,
    space_id          VARCHAR(64) NOT NULL,
    definition_id     VARCHAR(64) NOT NULL,
    trigger_key       VARCHAR(64) NOT NULL,
    name              VARCHAR(128) NOT NULL,
    status            VARCHAR(16) NOT NULL DEFAULT 'active',
    cron_expr         VARCHAR(128) NOT NULL,
    next_fire_at      TIMESTAMP NOT NULL,
    misfire_policy    VARCHAR(16) NOT NULL,
    -- 触发时用哪一版：空 = 用最新已发布版；给了号 = 钉住那一版（必须存在且已发布）。
    -- ⚠️ 给了号就只能是 >= 1 —— 0 不是「没填」的意思（那是空）：「填了 0」会被写成「以为钉住了一版、
    --    其实跟着最新版漂」。写入侧 DTO 用 ge=1 当场挡掉（1001），这一列不另设 CHECK（写路径只有 DTO 一条）
    -- 从前还有一列 version_mode（latest / fixed），已删 —— 跟「这一列填没填号」说的是同一件事
    fixed_version     INTEGER,
    inputs            JSONB,
    last_fired_at     TIMESTAMP,
    state_version     INTEGER NOT NULL DEFAULT 0,
    created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_tc_trig_status CHECK (status IN ('active','paused','deleted')),
    CONSTRAINT ck_tc_trig_misfire CHECK (misfire_policy IN ('skip','catch_up'))
);
-- 同一个空间下触发名不重复（已删的除外）
CREATE UNIQUE INDEX IF NOT EXISTS uq_tc_trigger_key ON task_center.task_triggers(tenant_id, space_id, trigger_key) WHERE status <> 'deleted';
-- 后台循环「扫到期触发器」用：只扫 active
-- （谓词里从前还有 trigger_type = 'cron'，那一列已删，剩下这半边才是它真正在管的事）
CREATE INDEX IF NOT EXISTS idx_tc_triggers_due ON task_center.task_triggers(next_fire_at) WHERE status = 'active';

-- ------------------------------------------------------------
-- 任务实例：一次触发的产物，是子任务树的根，也是这条任务的稳定身份
-- 存「稳定身份 + 最新一次运行的状态快照」，重跑不新建行
-- space_id = 这次运行落在哪个空间（空间级版本用版本自带的；system / tenant 级由发起方指定）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS task_center.tasks (
    id                    VARCHAR(64) PRIMARY KEY,
    tenant_id             VARCHAR(64) NOT NULL,
    definition_id         VARCHAR(64) NOT NULL,
    definition_version_no INTEGER NOT NULL,
    -- 编排快照：建任务时把 task_definition_versions.config 整份抄下来（**不反查版本**：
    -- 版本会被「删定义」带成假删，那时任务多半还在跑，反查一断，消化上报 / 停止 / 超时
    -- 巡检三条路全灭，那条 running 子任务永远收不掉、并发名额永久泄漏）。见 models/execution.py
    definition_config     JSONB NOT NULL,
    space_id              VARCHAR(64) NOT NULL,
    task_name             VARCHAR(256),
    trigger_id            VARCHAR(64),
    inputs                JSONB,
    -- 创建时快照的运行方 capability_id 列表（去重）。删任务要按它出站清理运行方残留，
    -- 不能删的时候现推：定义被删掉之后版本一并假删，那时推出来是空的（见 models/execution.py）
    runner_capability_ids JSONB,
    current_attempt       INTEGER NOT NULL DEFAULT 1,
    status                VARCHAR(16) NOT NULL DEFAULT 'pending',
    error_message         TEXT,
    state_version         INTEGER NOT NULL DEFAULT 0,
    created_at            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at            TIMESTAMP,
    finished_at           TIMESTAMP,
    updated_at            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by            VARCHAR(64),
    updated_by            VARCHAR(64),
    is_deleted            SMALLINT NOT NULL DEFAULT 0,
    CONSTRAINT ck_tc_task_status CHECK (status IN ('pending','running','completed','failed'))
);
CREATE INDEX IF NOT EXISTS idx_tc_task_tenant_status ON task_center.tasks(tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_tc_task_def ON task_center.tasks(definition_id);
CREATE INDEX IF NOT EXISTS idx_tc_task_trigger ON task_center.tasks(trigger_id);

-- ------------------------------------------------------------
-- 任务尝试：某任务每一次运行（含首次和每次重跑）的一条记录（只追加不覆盖）
-- 最新一行是 tasks 快照的权威来源
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS task_center.task_attempts (
    id                VARCHAR(64) PRIMARY KEY,
    tenant_id         VARCHAR(64) NOT NULL,
    space_id          VARCHAR(64) NOT NULL,
    task_id           VARCHAR(64) NOT NULL,
    attempt           INTEGER NOT NULL,
    status            VARCHAR(16) NOT NULL DEFAULT 'pending',
    error_message     TEXT,
    started_at        TIMESTAMP,
    finished_at       TIMESTAMP,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_tc_ta_status CHECK (status IN ('pending','running','completed','failed'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_tc_ta_attempt ON task_center.task_attempts(task_id, attempt);
CREATE INDEX IF NOT EXISTS idx_tc_ta_task ON task_center.task_attempts(task_id);
-- 派活排队：主任务按运行创建时间排序用
CREATE INDEX IF NOT EXISTS idx_tc_ta_created ON task_center.task_attempts(created_at);
-- 平台惯例是「带 tenant_id 的表都有一条以它打头的索引」（全库 54 张带 tenant_id 的表里 53 张如此，唯一例外是 knowledge_base.knowledge_answer_feedback_event）
CREATE INDEX IF NOT EXISTS idx_tc_ta_tenant ON task_center.task_attempts(tenant_id);

-- ------------------------------------------------------------
-- 子任务实例：挂在某一次运行（task_attempt_id）下的一步
-- 一行存「该子任务最新一次尝试」；重跑任务会为新一次运行生成一套全新 subtasks
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS task_center.subtasks (
    id                VARCHAR(64) PRIMARY KEY,
    tenant_id         VARCHAR(64) NOT NULL,
    space_id          VARCHAR(64) NOT NULL,
    task_id           VARCHAR(64) NOT NULL,
    task_attempt_id   VARCHAR(64) NOT NULL,
    node_id           VARCHAR(64) NOT NULL,
    step_code         VARCHAR(64) NOT NULL,
    subtask_name      VARCHAR(128),
    status            VARCHAR(16) NOT NULL DEFAULT 'pending',
    depends_on        JSONB,
    next_steps        JSONB,
    sort              INTEGER,
    timeout_seconds   INTEGER NOT NULL DEFAULT 300,
    inputs            JSONB,
    outputs           JSONB,
    -- 产出「去哪儿看」的引用（展示用，**不参与下游取参**；`outputs` 才是下游 `@节点.outputs.x` 的数据源）。
    -- 存一个**数组**（一步可能有多份产物，如切片 + 图片资源），元素形状由运行方定义、未知键忽略，例如：
    --   [{"kind":"chunks","label":"解析切片","doc_id":"…","kb_id":"…",
    --     "href":"/api/v1/knowledge-base/documents/…/chunks?knowledge_base_id=…",
    --     "count":1234,"attempt":1}]
    -- 可空：老运行方不上报、未终态的步骤都是空 → 前端不显示入口。
    output_refs       JSONB,
    current_attempt   INTEGER NOT NULL DEFAULT 1,
    execution_id      VARCHAR(128),
    state_version     INTEGER NOT NULL DEFAULT 0,
    error_message     TEXT,
    created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at        TIMESTAMP,
    finished_at       TIMESTAMP,
    updated_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    is_deleted        SMALLINT NOT NULL DEFAULT 0,
    CONSTRAINT ck_tc_sub_status CHECK (status IN ('pending','running','success','failed','timeout'))
);
-- 同一次运行里，节点 id 不重复
-- （node_id 是编排节点、唯一；step_code 只是「用哪个内置步骤」、可重复，所以不进唯一键）
-- （这张表虽然也有 is_deleted，但唯一索引不带 WHERE is_deleted=0：唯一性已由「同一次运行」限定，
--   且假删是整任务一起删、不会再往同一个 attempt 里插同名节点，所以不需要部分索引）
CREATE UNIQUE INDEX IF NOT EXISTS uq_tc_subtask_node ON task_center.subtasks(task_attempt_id, node_id);
-- 受理凭证在「租户 + 空间」范围内唯一（未派活时为空，所以用部分索引）
CREATE UNIQUE INDEX IF NOT EXISTS uq_tc_subtask_exec ON task_center.subtasks(tenant_id, space_id, execution_id) WHERE execution_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tc_subtask_status ON task_center.subtasks(status);
CREATE INDEX IF NOT EXISTS idx_tc_subtask_tenant ON task_center.subtasks(tenant_id);
CREATE INDEX IF NOT EXISTS idx_tc_subtask_task ON task_center.subtasks(task_id);

-- ------------------------------------------------------------
-- 子任务尝试历史：某个子任务每一次尝试（含首次和每次重试）的一条记录（只追加不覆盖）
-- 子任务诞生时先插一条 attempt=1 的 pending 行，此后每次重试再追加一行
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS task_center.subtask_attempts (
    id                VARCHAR(64) PRIMARY KEY,
    tenant_id         VARCHAR(64) NOT NULL,
    space_id          VARCHAR(64) NOT NULL,
    task_id           VARCHAR(64) NOT NULL,
    subtask_id        VARCHAR(64) NOT NULL,
    attempt           INTEGER NOT NULL,
    execution_id      VARCHAR(128),
    status            VARCHAR(16) NOT NULL DEFAULT 'pending',
    error_message     TEXT,
    outputs           JSONB,
    started_at        TIMESTAMP,
    finished_at       TIMESTAMP,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_tc_sa_status CHECK (status IN ('pending','running','success','failed','timeout'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_tc_sa_attempt ON task_center.subtask_attempts(subtask_id, attempt);
-- 受理凭证在「租户 + 空间」范围内唯一（未派活时为空，所以用部分索引）
CREATE UNIQUE INDEX IF NOT EXISTS uq_tc_sa_exec ON task_center.subtask_attempts(tenant_id, space_id, execution_id) WHERE execution_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tc_sa_task ON task_center.subtask_attempts(task_id);
-- 派活排队：同一任务内的子任务按尝试创建时间排序用
CREATE INDEX IF NOT EXISTS idx_tc_sa_created ON task_center.subtask_attempts(created_at);

-- ------------------------------------------------------------
-- 上报收件箱：运行方上报先入队，后台循环再逐条消化（§4.2 ①、§7.5 ②）
-- 同步阶段做两件事：① 报文级校验（status 取值 + 条件必填）；② 身份一致性核对
--（报文里的任务 / 空间 / 凭证得互相对得上，见 TaskService._require_report_identity）。
-- 过了就落一行、回「受理」；真正的裁决与落库由后台循环那一趟完成。
-- ②要读一眼库，但**不拿锁、不写库**。
-- ⚠️ 假删语义跟别的表不同：这里 is_deleted = 1 表示「这条上报已经处理完了」，
--    不是「被删掉了」—— 处理过的记录永远留着，供事后对账「管理方到底收到没」。
--    「处理完了」分两种，由 dead_lettered 区分：0=正常消化 / 1=判了死信（放弃重试）。
--    **死信也置 is_deleted=1 出队** —— 超时巡检的让路判据只看 is_deleted = 0，
--    死信留在队列里会让那条子任务永远判不了超时。
-- 不加任何唯一约束：同一张凭证允许重复上报（重复那条靠「已是终态」这一闸吸收）。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS task_center.subtask_reports (
    id            VARCHAR(64) PRIMARY KEY,
    tenant_id     VARCHAR(64) NOT NULL,
    space_id      VARCHAR(64) NOT NULL,
    execution_id  VARCHAR(128) NOT NULL,
    -- 报文里那条**任务** id（不是凭证所属子任务的）：运行方上报时，管理方在同步阶段拿它跟
    -- 报文里的 task_id 交叉核对，对不上当场拒（`task_service._require_report_identity`）。
    -- **只做校验与排障**，不参与定位、不建索引 —— 它**不是外键**：「任务已删 / 凭证已被
    -- 重试清掉」的报文照样要落库，所以这里不能引用任何东西。
    -- （列在 2026-09-29 由 update/037 内联进来，文件内声明顺序与 ORM 一致。）
    task_id       VARCHAR(64) NOT NULL,
    status        VARCHAR(16) NOT NULL,
    outputs       JSONB,
    -- 产出「去哪儿看」的引用（运行时上报带上来、消化时原样落到 `subtasks.output_refs`）。
    -- 它**跟着报文走**，所以收件箱也得存一列 —— 否则后台消化那一刻拿不到。
    output_refs   JSONB,
    error_message TEXT,
    ts            BIGINT,
    reported_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    processed_at  TIMESTAMP,
    dead_lettered SMALLINT NOT NULL DEFAULT 0,
    is_deleted    SMALLINT NOT NULL DEFAULT 0,
    CONSTRAINT ck_tc_report_status CHECK (status IN ('success','failed'))
);
-- 后台扫描未消化的：按收到时间升序（先收到的先消化）
CREATE INDEX IF NOT EXISTS idx_tc_report_pending ON task_center.subtask_reports(reported_at) WHERE is_deleted = 0;
-- 超时巡检「让路」判据：按凭证找还没消化的上报
CREATE INDEX IF NOT EXISTS idx_tc_report_exec ON task_center.subtask_reports(tenant_id, space_id, execution_id, is_deleted);

-- ============================================================
-- 内置清单预置数据（§3.4 内置数据示例）
-- 平台预置、只读；ON CONFLICT DO NOTHING 保证可重复执行
-- ============================================================
-- 全局内置步骤清单（2026-10-23 起没有「内置任务」这一层）。
-- 任务中心要支持自由编排：定义不再绑定某种任务类型，一版编排直接从下面这些步骤里选节点。
-- 目前只有两类节点 —— `parse`（文件解析）/ `compile`（知识编译），都交给知识库运行方。
-- 「哪类库」现在由**编排定义名**承载，不再靠目录/步骤名区分。
--
-- 三类知识库（Ontology / Ontology Wiki / Jonex Wiki）的差异
-- 全在运行方内部按 KB 的 `kb_type` 分流（parse 落 content.md 还是 content_list.json、
-- compile 走 recompile_openkb 还是 recompile_jonex_wiki），编排不承载这个差别。

-- 步骤超时必须**大于知识库自己的看门狗阈值**，否则会出现「任务中心先判超时、
-- 知识库后上报却被幂等吸收」——用户看到「任务超时失败」而文档状态是另一回事，
-- 两头说法不一致。两类步骤统一取 25200（7 小时），比知识库那道晚一档。
--
-- 知识库侧对**上传链路建的那些文档**（带任务中心标记的），对账循环那套超时巡检
-- （`patrol_parsing_timeout`，SOFT 3600 只探活 / HARD 21600 才判死）整条跳过 ——
-- 它那套「探活 + 自动重推」跟编排并起来会互相踩（PARSING 分支的 re-insert 等于绕开
-- 编排把一个文档解析两遍）。于是判死改由运行方自己做，读的是同一个阈值
-- `TC_STEP_HARD_TIMEOUT_SEC`（独立的环境变量，默认值与 `RAG_TASK_HARD_TIMEOUT_SEC`
-- 恰好相同、都是 21600 —— 但**两者没有联动**，改一个不会带动另一个）：超了它就上报 failed
-- 并连文档一起落
-- FAILED。25200 = 21600 + 1 小时余量，保证正常情况下知识库先判死、管理方按终态
-- 吸收，永远走不到这里；这里只是「运行方自己也彻底没动静了」的最后一层兜底。
--
-- 两种 llm-wiki 库的编译各另有一道阈值（openkb 是 `OPENKB_COMPILE_TIMEOUT_SEC`，
-- jonex_wiki 是 `JONEX_WIKI_COMPILE_TIMEOUT_SEC`，都回落到 1800，到点**直接判死**，
-- `patrol_openkb_compile` / `patrol_jonex_wiki_compile` 都不探活），同样早于这里的 25200。
--
-- max_concurrency：两类步骤统一取 10。
--
-- 这个上限是**全局按 `runner_code + step_code` 统计**的（全平台共用同一份额度，见
-- 设计文档 §6.3）—— 不是「每个库各 10」。原来的 parse=5 / ingest=2 / compile=2 是按
-- 「单份文档走到这一步有多重」定的（ingest 逐 chunk 抽实体关系 + 写图/向量库，最重所以
-- 最保守），但会让人看见的瓶颈不在单份有多重，而在名额太少让批次走不动：「整库重抽」
-- 这类场景里一批文档会卡在同一个相位上排队，前端上就是十几份一起长时间停在「解析中」/
-- 「知识抽取入库中」/「待编译」，看着像卡死其实只是没轮到。
--
-- 10 是让「一次重抽十几份」这种批次能整体推进的量级。代价是同一时刻最多有 10 份文档
-- 一起压 LLM 与图库；真要回退，这三个数是并发调优的第一顺位。
--
-- 步骤名**全平台只有两个字面量**：文件解析 / 知识编译。同一件事叫不同名字会让用户
-- 对着页面找不到任务；**「哪类库」由任务定义名承载，不由步骤名承载**。
-- 名字是给人看的；运行方认的是 `step_code`（parse / compile），改名不影响派活。
--
-- ⚠️ 2026-10-23 起只预置 **parse + compile**：`ingest`（「知识抽取」，其实是推切片）
-- **不再预置** —— Ontology 链 2026-09-29 由三步改两步，`parse` 自己推切片。
-- 运行方代码里仍留着 `ingest` 的 handler，只服务改版前遗留的在途任务；存量库里的
-- 那些任务由 `update/055` 在迁移时直接判失败。
--
-- output_schema 里声明 document_id，是给下游 `@<节点>.outputs.document_id` 用的
-- （见下方「数据怎么传」）：运行方上报成功时把它带回来。
INSERT INTO task_center.task_catalog_steps
  (id, step_code, name, runner_code, max_concurrency, timeout_seconds, input_schema, output_schema) VALUES
  ('step-parse', 'parse', '文件解析', 'knowledge_base', 10, 25200,
   '{"document_id":{"type":"string","required":true},"knowledge_base_id":{"type":"string","required":true}}',
   '{"document_id":{"type":"string","required":true}}'),
  ('step-compile', 'compile', '知识编译', 'knowledge_base', 10, 25200,
   '{"document_id":{"type":"string","required":true},"knowledge_base_id":{"type":"string","required":true}}',
   '{}')
ON CONFLICT DO NOTHING;

INSERT INTO task_center.runner_definitions (id, code, name, capability_id) VALUES
  ('runner-knowledge-base', 'knowledge_base', '知识库', 'business.knowledge_base.v1')
ON CONFLICT DO NOTHING;

-- ------------------------------------------------------------
-- 平台级任务定义：知识库上传后的处理链路（**三条，都是两步串行**）
--   definition-file-processing-ontology                    file_processing_ontology                    第一步 atomic-rag `full`（上传）/ `reparse_strict`（重新解析）—— 解析 + 推切片一趟
--   definition-file-processing-ontology-wiki               file_processing_ontology_wiki               第一步 atomic-rag `parse_only`（只解析）
--   definition-file-processing-jonex-wiki                  file_processing_jonex_wiki                  第一步 atomic-rag `parse_only`（只解析）
-- 三条链的**第二步都是 `compile`**（atomic-rag `ontology_only`：不读原文件，按 document_id
-- 从 LightRAG 读实体做本体归类、写 Neo4j）。
-- ⚠️ Ontology 那条 2026-09-29 由**三步**（文件解析 → 知识抽取 → 知识编译）改成两步 ——
-- 理由见上一段 `ingest` 那几行；另外两条 wiki 链本来就是两步。
-- 定义名就是任务中心页面上的「这条任务属于哪类库」，所以带上库类型后缀；步骤名不带。
-- 上传时按 kb_type 挑一个（知识库侧用固定 definition_id 映射，见
-- `task_runner_service.definition_for_kb_type`）。
-- scope=system，所以 tenant_id / space_id 都为 NULL（ck_tc_def_scope_fields 强制）
--
-- 数据怎么传：**优先从上游产出取**（`@<节点>.outputs.<字段>`），哪一步都不产出它
-- 才回落到任务入参（`@task.inputs.<字段>`）。于是：
--   · 源头那一步 parse_1 没有上游，两个入参只能取任务入参 —— 文档 id 在上传时就
--     已生成落库（「一个文件一条任务」）；
--   · 下游各步的 document_id 一律取**上一步的产出**；
--   · knowledge_base_id 没有任何一步会产出它（它是任务级入参），所以全程取任务入参。
-- 为什么下游不图省事重复写 @task.inputs.document_id：两者取值相同，但取上游产出能让
-- 编排自己表达出「谁喂谁」，而且一版编排里如果出现多个 parse 节点（解析两个不同文件、
-- 各推各的切片），**只有这种写法能让每份产物喂对下游** —— 都用任务入参的话，两个
-- parse 节点会把同一个文档推两遍。串行顺序另由 depends_on 保证（跟数据传递是两回事）。
-- 代价：运行方上报时必须带回 outputs.document_id；漏报不静默 —— 下游拿到空值就失败。
-- ------------------------------------------------------------
INSERT INTO task_center.task_definitions
  (id, scope, tenant_id, space_id, name, description, current_version_no) VALUES
  ('definition-file-processing-ontology', 'system', NULL, NULL, '文件处理（Ontology）', '上传文件后自动解析并编译本体知识', 1),
  ('definition-file-processing-ontology-wiki', 'system', NULL, NULL, '文件处理（Ontology Wiki）', '上传文件后自动解析并编译 Ontology Wiki', 1),
  ('definition-file-processing-jonex-wiki', 'system', NULL, NULL, '文件处理（Jonex Wiki）', '上传文件后自动解析并编译 Jonex Wiki', 1)
ON CONFLICT DO NOTHING;

INSERT INTO task_center.task_definition_versions
  (id, scope, tenant_id, definition_id, space_id, version_no, status, config, published_at) VALUES
  ('version-file-processing-ontology-v1', 'system', NULL, 'definition-file-processing-ontology', NULL, 1, 'published',
   '{"input_schema":{"document_id":{"type":"string","required":true},"knowledge_base_id":{"type":"string","required":true}},"output_schema":{},"entry":"parse_1","steps":[{"node_id":"parse_1","step_code":"parse","depends_on":[],"next_steps":["compile_1"],"sort":1,"timeout_seconds":25200,"bind_inputs":{"document_id":"@task.inputs.document_id","knowledge_base_id":"@task.inputs.knowledge_base_id"}},{"node_id":"compile_1","step_code":"compile","depends_on":["parse_1"],"next_steps":[],"sort":2,"timeout_seconds":25200,"bind_inputs":{"document_id":"@parse_1.outputs.document_id","knowledge_base_id":"@task.inputs.knowledge_base_id"}}]}',
   CURRENT_TIMESTAMP),
  ('version-file-processing-ontology-wiki-v1', 'system', NULL, 'definition-file-processing-ontology-wiki', NULL, 1, 'published',
   '{"input_schema":{"document_id":{"type":"string","required":true},"knowledge_base_id":{"type":"string","required":true}},"output_schema":{},"entry":"parse_1","steps":[{"node_id":"parse_1","step_code":"parse","depends_on":[],"next_steps":["compile_1"],"sort":1,"timeout_seconds":25200,"bind_inputs":{"document_id":"@task.inputs.document_id","knowledge_base_id":"@task.inputs.knowledge_base_id"}},{"node_id":"compile_1","step_code":"compile","depends_on":["parse_1"],"next_steps":[],"sort":2,"timeout_seconds":25200,"bind_inputs":{"document_id":"@parse_1.outputs.document_id","knowledge_base_id":"@task.inputs.knowledge_base_id"}}]}',
   CURRENT_TIMESTAMP),
  ('version-file-processing-jonex-wiki-v1', 'system', NULL, 'definition-file-processing-jonex-wiki', NULL, 1, 'published',
   '{"input_schema":{"document_id":{"type":"string","required":true},"knowledge_base_id":{"type":"string","required":true}},"output_schema":{},"entry":"parse_1","steps":[{"node_id":"parse_1","step_code":"parse","depends_on":[],"next_steps":["compile_1"],"sort":1,"timeout_seconds":25200,"bind_inputs":{"document_id":"@task.inputs.document_id","knowledge_base_id":"@task.inputs.knowledge_base_id"}},{"node_id":"compile_1","step_code":"compile","depends_on":["parse_1"],"next_steps":[],"sort":2,"timeout_seconds":25200,"bind_inputs":{"document_id":"@parse_1.outputs.document_id","knowledge_base_id":"@task.inputs.knowledge_base_id"}}]}',
   CURRENT_TIMESTAMP),
   CURRENT_TIMESTAMP)
ON CONFLICT DO NOTHING;

-- ============================================================
-- 注释
-- ============================================================
COMMENT ON TABLE task_center.task_catalog_steps IS '内置步骤清单：编排里能选哪些节点（步骤交给谁跑 runner_code、输入输出 schema、默认超时 timeout_seconds、并发上限 max_concurrency 都在这；2026-10-23 起全局一张表，step_code 唯一）';
COMMENT ON COLUMN task_center.task_catalog_steps.step_code IS '步骤名，如 parse / compile（编排里 steps[].step_code 引用它 —— 两边**同名**，就是这一列的值；运行方按这个值分流到不同执行模式）';
COMMENT ON COLUMN task_center.task_catalog_steps.runner_code IS '这步交给哪个运行方（runner_definitions.code），一对一绑定';
COMMENT ON COLUMN task_center.task_catalog_steps.max_concurrency IS '这步最多同时跑几个（运行方对此类活的并发上限）；空=不限；派活时按它数全局在跑数、到上限就跳过';
COMMENT ON COLUMN task_center.task_catalog_steps.timeout_seconds IS '这步的默认超时时间（秒）；建编排时预填到 config.steps[].timeout_seconds（必填、可改）';
COMMENT ON COLUMN task_center.task_catalog_steps.input_schema IS '这步接受什么输入：{ 字段名: FieldSpec }';
COMMENT ON COLUMN task_center.task_catalog_steps.output_schema IS '这步产出什么：{ 字段名: FieldSpec }，字段名供下游 @step.outputs.x 引用';

COMMENT ON TABLE task_center.runner_definitions IS '运行方清单：平台内置（平台预置，不通过接口写；不带租户）';
COMMENT ON COLUMN task_center.runner_definitions.capability_id IS '运行方对外能力 ID（对应管理方那边的 client）';

COMMENT ON TABLE task_center.task_definitions IS '任务定义：一类任务的「身份证」，按 scope 分系统级/租户级/空间级';
COMMENT ON COLUMN task_center.task_definitions.scope IS 'system(系统级，无租户无空间)/tenant(租户级，有租户无空间)/space(空间级，租户+空间都有)';
COMMENT ON COLUMN task_center.task_definitions.space_id IS '空间 ID：scope=space 时必有；任务执行哪一版就用哪一版的 space_id';
COMMENT ON COLUMN task_center.task_definitions.current_version_no IS '最新已发布版本号；0 = 还没发过版';

COMMENT ON TABLE task_center.task_definition_versions IS '任务定义版本：一版编排（步骤顺序 / 依赖关系 / 数据传递 / 超时）；published 后永久固化不可改（**没有失败策略这个配置** —— 失败怎么处置下游是平台固定规则，见 config 列注释）';
COMMENT ON COLUMN task_center.task_definition_versions.scope IS '跟所属定义一致：system / tenant / space';
COMMENT ON COLUMN task_center.task_definition_versions.space_id IS '空间 ID：scope=space 时必有；任务执行哪一版就用哪一版的 space_id';
COMMENT ON COLUMN task_center.task_definition_versions.status IS 'draft(草稿)/published(已发布，终点)；发布后不可改、不可删、不可逆回 draft';
COMMENT ON COLUMN task_center.task_definition_versions.config IS '编排内容：{ input_schema, output_schema, entry, steps[] }（entry 是单个 node_id；input/output 同时承载画布开始/结束节点的参数面板；没有 fail_policy —— 失败一律 hold）';
COMMENT ON COLUMN task_center.task_definition_versions.is_deleted IS '假删除标记：只由「删定义」带出来（定义删了，版本一并假删），版本本身不能被单独删';

COMMENT ON TABLE task_center.task_triggers IS '触发：什么时候、用什么参数把任务跑起来（空间级）；触发器只等于定时，没有类型列';
COMMENT ON COLUMN task_center.task_triggers.space_id IS '这个触发器在哪个空间下引用定义；它触发出来的任务就落这个空间';
COMMENT ON COLUMN task_center.task_triggers.status IS 'active(启用)/paused(已暂停)/deleted(已删)；paused 的不会被调度循环扫到，但仍能手动 trig.fire';
COMMENT ON COLUMN task_center.task_triggers.cron_expr IS '【必填】5 段表达式「分 时 日 月 周」';
COMMENT ON COLUMN task_center.task_triggers.next_fire_at IS '【必填】下次该触发的时间（秒一律为 00）；后台循环按它扫（<= 现在 就是要触发）；算不出来时置 paused 告警，不许写 NULL（列本身 NOT NULL）';
COMMENT ON COLUMN task_center.task_triggers.misfire_policy IS '【必填】错过点的处置：skip=跳过、只推下次 / catch_up=补跑一次（只补一次）';
COMMENT ON COLUMN task_center.task_triggers.fixed_version IS '触发时用哪一版：空=用最新已发布版；给了号=钉住那一版（必须是 >= 1 的号、存在且已发布）。0 既不是「没填」也不是「清空」，是非法版本号（DTO ge=1 当场拒 1001）。trig.update 里「传 null」= 清空、切回最新已发布版，「不传」= 不动';
COMMENT ON COLUMN task_center.task_triggers.inputs IS '触发时带的入参，触发时被当作 fire 的 inputs 传下去';
COMMENT ON COLUMN task_center.task_triggers.last_fired_at IS '上次「到点处理」的时间 —— 不代表真的起过任务：misfire_policy=skip 那次只推 next_fire_at、没起任务，也照样写它；要「上次真正触发任务的时间」看 trig.runs 最后一条的 created_at';
COMMENT ON COLUMN task_center.task_triggers.state_version IS '每行版本号：触发抢占的 CAS 用它（§7.5 ⓪ 防多副本同时触发同一条）';

COMMENT ON TABLE task_center.tasks IS '任务实例：一次触发的产物，是子任务树的根，也是这条任务的稳定身份（重跑不换 id）';
COMMENT ON COLUMN task_center.tasks.definition_version_no IS '用的是哪个版本号（钉死，仅作溯源；编排内容本身见 definition_config）';
COMMENT ON COLUMN task_center.tasks.definition_config IS '编排快照：建任务时从 task_definition_versions.config 抄来的整份编排。推进任务读它、不反查版本——版本会被「删定义」带成假删，反查一断任务就永远收不了尾';
COMMENT ON COLUMN task_center.tasks.space_id IS '空间 ID：这次运行落在哪个空间';
COMMENT ON COLUMN task_center.tasks.inputs IS '这次运行的任务级入参（按 config.input_schema 收口后的实值），供 @task.inputs.x 在运行期求值、retry_task 复用';
COMMENT ON COLUMN task_center.tasks.current_attempt IS '当前跑到第几次运行（指针，指向 task_attempts.attempt）；首次=1，retry_task 时 +1';
COMMENT ON COLUMN task_center.tasks.status IS 'pending(待执行)/running(跑着)/completed(完成)/failed(失败)：最新一次运行的状态快照';
COMMENT ON COLUMN task_center.tasks.state_version IS '每行版本号，用于防重复改（乐观锁）';
COMMENT ON COLUMN task_center.tasks.created_by IS '建这条任务的人（user_id，不是用户名）。空 = 不是用户发起的（触发器自动触发、内部系统调用）；只写一次、之后不再变';
COMMENT ON COLUMN task_center.tasks.updated_by IS '最后一次「用户主动写」这条任务的人（user_id）。跟 created_by 的区别是它会被覆盖：系统自己推进状态（派活抢占 / 消化上报 / 超时巡检）不写这一列，只有人发起的写（如停止任务）才写';

COMMENT ON TABLE task_center.task_attempts IS '任务尝试：某任务每一次运行（含首次和每次重跑）的一条记录（只追加不覆盖）';
COMMENT ON COLUMN task_center.task_attempts.attempt IS '第几次运行（首次=1，重跑 +1）';

COMMENT ON TABLE task_center.subtasks IS '子任务实例：挂在某一次运行（task_attempt_id）下的一步；一行存该子任务最新一次尝试';
COMMENT ON COLUMN task_center.subtasks.node_id IS '是编排里的哪个节点（对应 config.steps[].node_id）；同一次运行里不重复';
COMMENT ON COLUMN task_center.subtasks.step_code IS '这个节点用的是哪个内置步骤（如 parse / compile）；管理方用来判并发上限、发版校验、查历史，并填进 execute 派活报文供运行方分流';
COMMENT ON COLUMN task_center.subtasks.subtask_name IS '给人看的名字：诞生时取内置步骤的 name（按 step_code 查 task_catalog_steps.step_code）';
COMMENT ON COLUMN task_center.subtasks.depends_on IS '它依赖哪些节点（一堆 node_id），诞生时从 config 固化';
COMMENT ON COLUMN task_center.subtasks.next_steps IS '我成功后激活哪些节点（一堆 node_id），诞生时从 config 固化；不参与调度判定，只供对偶校验与排查';
COMMENT ON COLUMN task_center.subtasks.updated_at IS '每次对该行的写都显式刷新（诞生/上游填值/状态变更/retry 重置）；防僵尸拿它当「变成就绪的时刻」';
COMMENT ON COLUMN task_center.subtasks.timeout_seconds IS '这步的超时时间（秒），诞生时从编排固化，超时巡检读这一列';
COMMENT ON COLUMN task_center.subtasks.inputs IS '填好后的真实入参（bind_inputs 解析填值后的结果）';
COMMENT ON COLUMN task_center.subtasks.outputs IS '产出（成功后回填）——**契约字段**：下游 `@<节点>.outputs.x` 与发版校验（N9/N12）都以它为准';
COMMENT ON COLUMN task_center.subtasks.output_refs IS '产出「去哪儿看」的引用（展示用，**不参与下游取参**）：运行方终态上报时带的数组，控制台读接口原样回给前端，前端据此调运行方自己的接口看详情（权限归运行方那侧判）';
COMMENT ON COLUMN task_center.subtasks.current_attempt IS '当前第几次尝试（指针，指向 subtask_attempts.attempt）；首次=1，retry_subtask 时 +1';
COMMENT ON COLUMN task_center.subtasks.execution_id IS '管理方生成的受理凭证（派活前生成、随 execute 传给运行方；重试时重新生成）；在同一租户+同一空间内唯一';
COMMENT ON COLUMN task_center.subtasks.state_version IS '每行版本号，用于防重复改（乐观锁）';
COMMENT ON COLUMN task_center.subtasks.is_deleted IS '假删除标记：删任务时它的子任务一并置 1（任务删了，子任务必定也全删）';

COMMENT ON TABLE task_center.subtask_attempts IS '子任务尝试历史：某个子任务每一次尝试（含首次和每次重试）的一条记录（只追加不覆盖）';
COMMENT ON COLUMN task_center.subtask_attempts.execution_id IS '这次尝试的受理凭证（管理方生成），在同一租户+同一空间内唯一；report_status 不靠它定位（定位只认 subtasks.execution_id），只有查历史/对账读它';
COMMENT ON COLUMN task_center.subtask_attempts.outputs IS '这次尝试的产出快照（结构同 subtasks.outputs）';

COMMENT ON TABLE task_center.subtask_reports IS '上报收件箱：运行方上报先入队、后台循环再逐条消化（is_deleted=1 表示「已处理完」——正常消化或判死信，看 dead_lettered；不是「被删」）';
COMMENT ON COLUMN task_center.subtask_reports.execution_id IS '受理凭证：消化时按 tenant_id + space_id + 它定位 subtasks（同 subtasks.execution_id）';
COMMENT ON COLUMN task_center.subtask_reports.reported_at IS '管理方收到上报的时刻；超时巡检「让路」判据读它（跟 started_at 同一个钟：管理方本机时间）';
COMMENT ON COLUMN task_center.subtask_reports.output_refs IS '产出「去哪儿看」的引用（随行记录）：消化时原样转抄到 `subtasks.output_refs`，本列不参与裁决';
COMMENT ON COLUMN task_center.subtask_reports.ts IS '运行方侧事件时间；只落库留痕、不参与任何裁决（管理方侧不打日志，唯一的落点就是本列）';
COMMENT ON COLUMN task_center.subtask_reports.attempts IS '消化尝试次数；不仅是观测——达 TASK_CENTER_REPORT_MAX_ATTEMPTS 就判死信（默认 5）';
COMMENT ON COLUMN task_center.subtask_reports.last_error IS '上次消化失败的原因（不截断，存「异常类名: 异常消息」）';
COMMENT ON COLUMN task_center.subtask_reports.processed_at IS '处理完的时刻（正常消化 / 判死信都会写）';
COMMENT ON COLUMN task_center.subtask_reports.dead_lettered IS '1 = 已判死信：消化连续失败达上限，放弃自动重试（按「上报行」算，不是按子任务）。死信行 is_deleted 同时为 1（出队）；要放它重来，把这一行的 dead_lettered 与 is_deleted 都重置回 0、attempts 归 0';
COMMENT ON COLUMN task_center.subtask_reports.is_deleted IS '1 = 已处理完（正常消化或判死信，看 dead_lettered；语义特殊：不是「被删掉」，记录永远留着供对账）';
