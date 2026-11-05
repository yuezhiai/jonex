-- ============================================================
-- 009 结构化数据 DDL（2026-11-04 归并：原 009–017、024–033 共 19 份脚本）
--
-- 覆盖：
--   · NL2SQL 运行时观测
--   · 血缘边与新鲜度 / 跨模态对齐 / 禁止同查
--   · 数据治理控制面 P1（标签·策略·用户密级·决策审计）
--   · KB 结构化开关
--   · 演化台（运行 / 日志 / 台账 / 语料 / 诊断报告）
--   · 演化信任闭环（语义版本 / 归档 / 候选 / bench / corpus versions）
--   · run kind 扩展（relation / constraint / mapping / tool_schema）
--   · 抽象 schema 保存历史
--
-- 应用：空卷随 initdb 自动执行；存量库由 update/009_structured_data.sql
--       （逐字节镜像）经 `make db-migrate` 补齐 —— 编号刻意早于所有
--       依赖 evolution_runs 等表的后续脚本。
-- 幂等：全部 CREATE/ALTER ... IF [NOT] EXISTS / ON CONFLICT，可重复执行。
-- ⚠ 若某存量库的 public.schema_migrations 已按旧文件名登记过
--   （009_structured_data / 016_evolution_console / 072–085 …），
--   迁移器会按文件名跳过本文件 —— 需手工 `psql -f` 一次（幂等）。
-- ============================================================

-- ------------------------------------------------------------------
-- 来源：migrations/009_nl2sql_observations.sql
-- ------------------------------------------------------------------

-- ============================================================
-- 009 ② NL→SQL 运行时观测（runtime observation）
-- 设计：docs/superpowers/specs/2026-10-12-nl2sql-runtime-observation-design.md（工作区文档，不入库）
-- 性质：纯追加观测表（无 is_deleted/updated_at，不参与业务 CRUD）
-- 应用：空卷随迁移自动执行；存量库手工 psql -f（同 008 任务中心先例）
-- ============================================================

CREATE TABLE IF NOT EXISTS knowledge_base.nl2sql_observations (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    knowledge_base_id VARCHAR(64) NOT NULL,
    data_source_id VARCHAR(64) NOT NULL DEFAULT '',
    question VARCHAR(500) NOT NULL,
    outcome VARCHAR(16) NOT NULL,
    reason_code VARCHAR(64),
    reason VARCHAR(500),
    entity_name VARCHAR(255),
    sql TEXT,
    rejected_sql TEXT,
    row_count INTEGER,
    elapsed_ms INTEGER,
    scene JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_kb_n2s_obs_outcome CHECK (outcome IN ('success', 'failed'))
);
CREATE INDEX IF NOT EXISTS idx_kb_n2s_obs_window
    ON knowledge_base.nl2sql_observations(outcome, created_at);
CREATE INDEX IF NOT EXISTS idx_kb_n2s_obs_ds
    ON knowledge_base.nl2sql_observations(data_source_id, created_at);

COMMENT ON TABLE knowledge_base.nl2sql_observations IS
    '② NL→SQL 运行时观测（诊断/演化输入；审计不动）。见 2026-10-12 运行时观测设计。';

-- ------------------------------------------------------------------
-- 来源：migrations/010_lineage_edges.sql
-- ------------------------------------------------------------------

-- ============================================================
-- 010 血缘边（lineage edges）—— KB 级资产
-- 设计：docs/superpowers/specs/2026-10-17-lineage-ui-and-etl-roadmap.md（工作区文档，不入库）
-- 性质：逐条 CRUD（无文档级版本号 ⇒ 不需要乐观锁，也不产生 409）
-- 应用：空卷随迁移自动执行；存量库手工 psql -f（同 008 任务中心 / 009 观测表先例）
-- ============================================================

CREATE TABLE IF NOT EXISTS knowledge_base.lineage_edges (
    id                VARCHAR(64)  PRIMARY KEY,
    tenant_id         VARCHAR(64)  NOT NULL,
    knowledge_base_id VARCHAR(128) NOT NULL,
    data_source_id    VARCHAR(64)  NOT NULL DEFAULT '',
    from_table        VARCHAR(255) NOT NULL,
    from_column       VARCHAR(255) NOT NULL,
    to_table          VARCHAR(255) NOT NULL,
    to_column         VARCHAR(255) NOT NULL,
    origin            VARCHAR(32)  NOT NULL DEFAULT 'manual',
    note              VARCHAR(500),
    created_by        VARCHAR(64),
    is_deleted        SMALLINT     NOT NULL DEFAULT 0,
    created_at        TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at        TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    -- 列级强制：表级血缘进不了路径搜索（换乘靠列映射翻译），别让它落库
    -- data_source_id 非空也在这里卡：表名是物理表名，缺了源就没有解释（服务层还有一道，双保险）
    CONSTRAINT ck_lg_edge_columns_nonempty
        CHECK (from_column <> '' AND to_column <> ''
               AND data_source_id <> ''),
    -- 档位白名单：与 paths.ORIGIN_CONFIDENCE 的键一一对应
    CONSTRAINT ck_lg_edge_origin
        CHECK (origin IN ('declared','manual','comment','platform','value','derived'))
);

-- 同一 KB 同一源内，同一个列四元组只许有一条活边
CREATE UNIQUE INDEX IF NOT EXISTS uq_lg_edge_quadruple
    ON knowledge_base.lineage_edges
       (tenant_id, knowledge_base_id, data_source_id,
        from_table, from_column, to_table, to_column)
    WHERE is_deleted = 0;

-- 运行时热路径：② 每次提问按 (租户, KB, 源) 全取
CREATE INDEX IF NOT EXISTS idx_lg_edge_scope
    ON knowledge_base.lineage_edges (tenant_id, knowledge_base_id, data_source_id)
    WHERE is_deleted = 0;

-- ------------------------------------------------------------------
-- 来源：migrations/011_ontology_data_alignment.sql
-- ------------------------------------------------------------------

-- ============================================================
-- 011 跨模态对齐：文档实体类型 ↔ 数据源实体（人审）
-- 设计：docs/superpowers/specs/2026-10-17-structured-data-into-ontology-design.md §四
-- 性质：逐条人审记录（proposed/active/rejected），active 才投影 MAPS_TO
-- 应用：空卷随迁移自动执行；存量库手工 psql -f（同 008 / 009 / 010 先例）
-- ============================================================

CREATE TABLE IF NOT EXISTS knowledge_base.ontology_data_alignment (
    id                VARCHAR(64)  PRIMARY KEY,
    tenant_id         VARCHAR(64)  NOT NULL,
    knowledge_base_id VARCHAR(128) NOT NULL,
    doc_type          VARCHAR(255) NOT NULL,
    data_source_id    VARCHAR(64)  NOT NULL,
    entity_name       VARCHAR(255) NOT NULL,
    status            VARCHAR(16)  NOT NULL DEFAULT 'proposed',
    created_by        VARCHAR(64),
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT ck_oda_status CHECK (status IN ('proposed','active','rejected')),
    CONSTRAINT ck_oda_nonblank CHECK (btrim(doc_type) <> '' AND btrim(data_source_id) <> ''
                                      AND btrim(entity_name) <> '')
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_oda_triple
    ON knowledge_base.ontology_data_alignment
    (tenant_id, knowledge_base_id, doc_type, data_source_id, entity_name);
CREATE INDEX IF NOT EXISTS idx_oda_scope
    ON knowledge_base.ontology_data_alignment (tenant_id, knowledge_base_id, status);

-- ------------------------------------------------------------------
-- 来源：migrations/012_lineage_edge_last_seen.sql
-- ------------------------------------------------------------------

-- ============================================================
-- 012 血缘边新鲜度（roadmap P2 #9：失效标记）
-- 设计：滚动窗口（默认 30 天）。`last_seen_at` 由每次写入/重复摄入刷新；
--       读取时对 `origin=platform` 且超窗的边标 `stale=true`（读时计算，
--       不写回 —— 与抽象 schema 的 stale 同款口径）。
-- 应用：空卷随迁移自动执行；存量库手工 psql -f（同 008/009/010/011 先例）。
-- ============================================================

ALTER TABLE knowledge_base.lineage_edges
    ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ;

-- 存量行以最后一次写库时间为准；再退到创建时间，最后退到当前时间。
UPDATE knowledge_base.lineage_edges
   SET last_seen_at = COALESCE(updated_at, created_at, now())
 WHERE last_seen_at IS NULL;

COMMENT ON COLUMN knowledge_base.lineage_edges.last_seen_at IS
    '该边最后一次被来源确认的时间（手工编辑/文件导入/OpenLineage 摄入均刷新）；读取时据此标 stale';

-- ------------------------------------------------------------------
-- 来源：migrations/013_lineage_forbidden_pairs.sql
-- ------------------------------------------------------------------

-- ============================================================
-- 013 血缘「禁止同查」表对（KB 级，逐数据源）
-- 设计：docs/superpowers/specs/2026-10-17-lineage-forbidden-pairs-design.md（工作区文档，不入库）
-- 性质：逐条记录（proposed/active/disabled）；只有 active 生效
-- 应用：空卷随迁移自动执行；存量库手工 psql -f
-- ============================================================

CREATE TABLE IF NOT EXISTS knowledge_base.lineage_forbidden_pairs (
    id                VARCHAR(64)  PRIMARY KEY,
    tenant_id         VARCHAR(64)  NOT NULL,
    knowledge_base_id VARCHAR(128) NOT NULL,
    data_source_id    VARCHAR(64)  NOT NULL,
    table_a           VARCHAR(255) NOT NULL,
    table_b           VARCHAR(255) NOT NULL,
    origin            VARCHAR(32)  NOT NULL DEFAULT 'manual',
    status            VARCHAR(16)  NOT NULL DEFAULT 'active',
    note              VARCHAR(500),
    created_by        VARCHAR(64),
    is_deleted        SMALLINT     NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT ck_lfp_nonblank CHECK (
        btrim(data_source_id) <> '' AND btrim(table_a) <> '' AND btrim(table_b) <> ''),
    CONSTRAINT ck_lfp_distinct CHECK (lower(btrim(table_a)) <> lower(btrim(table_b))),
    CONSTRAINT ck_lfp_origin CHECK (origin IN ('manual','derived')),
    CONSTRAINT ck_lfp_status CHECK (status IN ('proposed','active','disabled')),
    -- 同一对表只有一种存法：规范化顺序 + 下方唯一索引挡并发重复
    CONSTRAINT ck_lfp_order CHECK (lower(btrim(table_a)) <= lower(btrim(table_b)))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_lfp_pair
    ON knowledge_base.lineage_forbidden_pairs
       (tenant_id, knowledge_base_id, data_source_id, lower(btrim(table_a)), lower(btrim(table_b)))
    WHERE is_deleted = 0;

CREATE INDEX IF NOT EXISTS idx_lfp_scope
    ON knowledge_base.lineage_forbidden_pairs
       (tenant_id, knowledge_base_id, data_source_id, status)
    WHERE is_deleted = 0;

-- ------------------------------------------------------------------
-- 来源：migrations/014_governance.sql
-- ------------------------------------------------------------------

-- ============================================================
-- 014 数据治理控制面 P1（#11，2026-10-18）
-- 设计：docs/superpowers/specs/2026-10-18-governance-control-plane-design.md
-- 性质：标签/策略/用户密级/审计；逐条 CRUD，重复 1001、缺失 1002，不产生 409
-- 应用：空卷随迁移自动执行；存量库手工 psql -f
-- ============================================================

CREATE TABLE IF NOT EXISTS knowledge_base.governance_tag_defs (
    id                VARCHAR(64)  PRIMARY KEY,
    tenant_id         VARCHAR(64)  NOT NULL,
    knowledge_base_id VARCHAR(128) NOT NULL,
    name              VARCHAR(128) NOT NULL,
    color             VARCHAR(32),
    description       VARCHAR(500),
    created_by        VARCHAR(64),
    is_deleted        SMALLINT     NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT ck_gov_tag_name CHECK (btrim(name) <> '')
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_gov_tag_name
    ON knowledge_base.governance_tag_defs
       (tenant_id, knowledge_base_id, lower(btrim(name))) WHERE is_deleted = 0;

CREATE TABLE IF NOT EXISTS knowledge_base.governance_labels (
    id                VARCHAR(64)  PRIMARY KEY,
    tenant_id         VARCHAR(64)  NOT NULL,
    knowledge_base_id VARCHAR(128) NOT NULL,
    data_source_id    VARCHAR(64)  NOT NULL,
    table_name        VARCHAR(255) NOT NULL,
    column_name       VARCHAR(255),
    level             VARCHAR(16)  NOT NULL,
    tags              JSONB        NOT NULL DEFAULT '[]',
    origin            VARCHAR(16)  NOT NULL DEFAULT 'manual',
    note              VARCHAR(500),
    created_by        VARCHAR(64),
    is_deleted        SMALLINT     NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT ck_gov_label_nonblank CHECK (
        btrim(data_source_id) <> '' AND btrim(table_name) <> ''),
    CONSTRAINT ck_gov_label_level CHECK (
        level IN ('public','internal','confidential','secret')),
    CONSTRAINT ck_gov_label_origin CHECK (
        origin IN ('manual','imported','derived'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_gov_label_scope
    ON knowledge_base.governance_labels
       (tenant_id, knowledge_base_id, data_source_id, lower(btrim(table_name)),
        lower(btrim(coalesce(column_name, ''))), origin)
    WHERE is_deleted = 0;
CREATE INDEX IF NOT EXISTS idx_gov_label_scope
    ON knowledge_base.governance_labels
       (tenant_id, knowledge_base_id, data_source_id, table_name) WHERE is_deleted = 0;

CREATE TABLE IF NOT EXISTS knowledge_base.governance_user_clearance (
    id                VARCHAR(64)  PRIMARY KEY,
    tenant_id         VARCHAR(64)  NOT NULL,
    user_id           VARCHAR(64)  NOT NULL,
    level             VARCHAR(16)  NOT NULL DEFAULT 'public',
    granted_by        VARCHAR(64),
    note              VARCHAR(500),
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT ck_gov_clearance_level CHECK (
        level IN ('public','internal','confidential','secret'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_gov_clearance_user
    ON knowledge_base.governance_user_clearance (tenant_id, user_id);

CREATE TABLE IF NOT EXISTS knowledge_base.governance_policies (
    id                VARCHAR(64)  PRIMARY KEY,
    tenant_id         VARCHAR(64)  NOT NULL,
    knowledge_base_id VARCHAR(128) NOT NULL,
    name              VARCHAR(128) NOT NULL,
    description       VARCHAR(500),
    effect            VARCHAR(16)  NOT NULL,
    priority          INTEGER      NOT NULL DEFAULT 100,
    resource_type     VARCHAR(16)  NOT NULL,
    table_pattern     VARCHAR(255),
    column_pattern    VARCHAR(255),
    tag_name          VARCHAR(128),
    min_clearance     VARCHAR(16)  NOT NULL DEFAULT 'public',
    mask_type         VARCHAR(16),
    row_filter        VARCHAR(1000),
    valid_from        TIMESTAMPTZ,
    valid_to          TIMESTAMPTZ,
    enabled           SMALLINT     NOT NULL DEFAULT 1,
    created_by        VARCHAR(64),
    note              VARCHAR(500),
    is_deleted        SMALLINT     NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT ck_gov_policy_effect CHECK (
        effect IN ('allow','deny','mask','row_filter')),
    CONSTRAINT ck_gov_policy_resource CHECK (
        resource_type IN ('table','column','tag')),
    CONSTRAINT ck_gov_policy_clearance CHECK (
        min_clearance IN ('public','internal','confidential','secret')),
    CONSTRAINT ck_gov_policy_mask CHECK (
        mask_type IS NULL OR mask_type IN ('null','redact'))
);
CREATE INDEX IF NOT EXISTS idx_gov_policy_scope
    ON knowledge_base.governance_policies
       (tenant_id, knowledge_base_id, enabled) WHERE is_deleted = 0;

CREATE TABLE IF NOT EXISTS knowledge_base.governance_decision_audit (
    id                VARCHAR(64)  PRIMARY KEY,
    tenant_id         VARCHAR(64)  NOT NULL,
    request_id        VARCHAR(64),
    user_id           VARCHAR(64),
    knowledge_base_id VARCHAR(128) NOT NULL,
    data_source_id    VARCHAR(64),
    action            VARCHAR(16)  NOT NULL,
    resource_table    VARCHAR(255),
    resource_column   VARCHAR(255),
    level             VARCHAR(16),
    decision          VARCHAR(16)  NOT NULL,
    policy_id         VARCHAR(64),
    reason            VARCHAR(500),
    sql_hash          VARCHAR(64),
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT ck_gov_audit_action CHECK (
        action IN ('select','predicate','aggregate')),
    CONSTRAINT ck_gov_audit_decision CHECK (
        decision IN ('allow','deny','mask','row_filter'))
);
CREATE INDEX IF NOT EXISTS idx_gov_audit_scope
    ON knowledge_base.governance_decision_audit
       (tenant_id, knowledge_base_id, created_at DESC);

-- ------------------------------------------------------------------
-- 来源：migrations/015_kb_structured_enabled.sql
-- ------------------------------------------------------------------

-- deploy/postgres/migrations/015_kb_structured_enabled.sql
-- 每 KB 的「结构化数据支持」开关。
-- ⚠️ 幂等语义：只在**首次加列**时把存量 KB 置为开启；重跑不得复活用户已关闭的库。
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'knowledge_base'
      AND table_name = 'knowledge_info'
      AND column_name = 'structured_enabled'
  ) THEN
    ALTER TABLE knowledge_base.knowledge_info
      ADD COLUMN structured_enabled BOOLEAN NOT NULL DEFAULT FALSE;
    UPDATE knowledge_base.knowledge_info SET structured_enabled = TRUE;
  END IF;
END $$;

-- ------------------------------------------------------------------
-- 来源：migrations/016_evolution_console.sql
-- ------------------------------------------------------------------

-- ============================================================
-- 016 演化台服务化：运行 / 日志 / 台账 / 语料
-- 应用：空卷随 initdb 自动执行；存量库手工 `psql -f`（同 008/009/014 先例）。
-- 幂等：全部 CREATE ... IF NOT EXISTS；语料种子 ON CONFLICT DO NOTHING。
-- ============================================================

-- ------------------------------------------------------------
-- 运行（一次演化 run）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_base.evolution_runs (
  id                  VARCHAR(64) PRIMARY KEY,
  tenant_id           VARCHAR(64) NOT NULL,
  knowledge_base_id   VARCHAR(128) NOT NULL,
  data_source_id      VARCHAR(64),
  kind                VARCHAR(16) NOT NULL,
  status              VARCHAR(16) NOT NULL DEFAULT 'queued',
  stage               VARCHAR(32) NOT NULL DEFAULT 'created',
  request             JSONB NOT NULL DEFAULT '{}'::jsonb,
  parent              JSONB NOT NULL DEFAULT '{}'::jsonb,
  report              JSONB NOT NULL DEFAULT '{}'::jsonb,
  gate                JSONB NOT NULL DEFAULT '{}'::jsonb,
  patch               JSONB NOT NULL DEFAULT '{}'::jsonb,
  backup              JSONB NOT NULL DEFAULT '{}'::jsonb,
  error               TEXT,
  request_id          VARCHAR(64),
  created_by          VARCHAR(64),
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_evo_run_kind CHECK (kind IN ('alias','query_rule')),
  CONSTRAINT ck_evo_run_status CHECK (status IN
    ('queued','running','evaluated','adopted','rejected','rolled-back','failed',
     'dry-run','empty-draft')),
  CONSTRAINT ck_evo_run_stage CHECK (stage IN
    ('created','drafted','probed','parent-ran','applied','candidate-ran',
     'rolled-back','gated'))
);

-- 存量库补 stage 白名单（表已建时 CREATE IF NOT EXISTS 不会补列上约束；
-- 重跑本迁移可补上，且不会重复添加）
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'ck_evo_run_stage'
      AND conrelid = 'knowledge_base.evolution_runs'::regclass
  ) THEN
    ALTER TABLE knowledge_base.evolution_runs
      ADD CONSTRAINT ck_evo_run_stage CHECK (stage IN
        ('created','drafted','probed','parent-ran','applied','candidate-ran','rolled-back','gated'));
  END IF;
END $$;

-- 幂等键：同租户下 request_id 唯一（NULL 不参与）
CREATE UNIQUE INDEX IF NOT EXISTS uq_evo_run_request
  ON knowledge_base.evolution_runs (tenant_id, request_id)
  WHERE request_id IS NOT NULL;

-- 同租户同一时刻只允许一个排队/运行中的 run（并发保护）
CREATE UNIQUE INDEX IF NOT EXISTS uq_evo_run_active
  ON knowledge_base.evolution_runs (tenant_id)
  WHERE status IN ('queued','running');

CREATE INDEX IF NOT EXISTS idx_evo_run_list
  ON knowledge_base.evolution_runs (tenant_id, created_at DESC);

-- ------------------------------------------------------------
-- 运行日志（按 run 级联；自身不带 tenant_id）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_base.evolution_run_logs (
  run_id      VARCHAR(64) NOT NULL REFERENCES knowledge_base.evolution_runs(id) ON DELETE CASCADE,
  seq         INTEGER NOT NULL,
  line        TEXT NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, seq)
);

-- ------------------------------------------------------------
-- 台账（采纳 / 拒绝的人工可审计记录）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_base.evolution_ledgers (
  id          VARCHAR(64) PRIMARY KEY,
  tenant_id   VARCHAR(64) NOT NULL,
  ledger_type VARCHAR(16) NOT NULL,
  entry       JSONB NOT NULL DEFAULT '{}'::jsonb,
  run_id      VARCHAR(64),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_evo_ledger_type CHECK (ledger_type IN ('alias','query_rule','reject'))
);
CREATE INDEX IF NOT EXISTS idx_evo_ledger_list
  ON knowledge_base.evolution_ledgers (tenant_id, ledger_type, created_at DESC);

-- ------------------------------------------------------------
-- 语料（DB 化的 nl2sql 评测语料；旧的 tests/eval/nl2sql/corpus.yaml 是迁移来源）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_base.evolution_corpus_cases (
  id          VARCHAR(64) PRIMARY KEY,
  tenant_id   VARCHAR(64) NOT NULL,
  name        VARCHAR(128) NOT NULL,
  question    TEXT NOT NULL,
  tier        VARCHAR(32) NOT NULL,
  expect      VARCHAR(16) NOT NULL,
  control     JSONB,
  kb_id       VARCHAR(128) NOT NULL,
  ds_id       VARCHAR(64) NOT NULL,
  enabled     SMALLINT NOT NULL DEFAULT 1,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_evo_case_expect CHECK (expect IN ('answer','refuse'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_evo_case_name
  ON knowledge_base.evolution_corpus_cases (tenant_id, name);

COMMENT ON TABLE knowledge_base.evolution_runs IS
    '演化台运行（一次 alias / query_rule 演化）。见 2026-10-18 演化台服务化设计。';
COMMENT ON TABLE knowledge_base.evolution_run_logs IS
    '演化 run 的日志行（按 run_id 级联，无 tenant_id）。';
COMMENT ON TABLE knowledge_base.evolution_ledgers IS
    '演化台账（采纳 / 拒绝记录，人工可审计）。';
COMMENT ON TABLE knowledge_base.evolution_corpus_cases IS
    '演化评测语料（由 tests/eval/nl2sql/corpus.yaml 一次性生成，之后以 DB 为准）。';

-- ------------------------------------------------------------
-- 语料种子：仅 demo 租户 + kb_assets_r2 + jinpin 源齐备时插入。
-- 生产环境（无该 KB / 源）不留指向不存在对象的数据。
-- 由 tests/eval/nl2sql/corpus.yaml 生成（2026-10-18 时为 35 条：
-- 31 条 answer 带 control，4 条 refuse 的 control 为 NULL）。
-- ------------------------------------------------------------
INSERT INTO knowledge_base.evolution_corpus_cases
  (id, tenant_id, name, question, tier, expect, control, kb_id, ds_id)
SELECT md5('evo:'||c.name), 'tenant_jonex_demo', c.name, c.question, c.tier, c.expect,
       c.control::jsonb, 'kb_assets_r2', '4cdb29e4-268c-4daf-8abe-7b879fa6abf0'
FROM (VALUES
      ('jobs_total', '一共有多少个职位', 'single_table', 'answer', '["SELECT COUNT(*) AS n FROM dbo.jobs"]'),
      ('company_total', '一共有多少家公司', 'single_table', 'answer', '["SELECT COUNT(*) AS n FROM dbo.company"]'),
      ('city_total', '系统里一共有多少个城市', 'single_table', 'answer', '["SELECT COUNT(*) AS n FROM dbo.citys"]'),
      ('benefit_total', '一共有多少种福利', 'single_table', 'answer', '["SELECT COUNT(*) AS n FROM dbo.benefits"]'),
      ('stock_cash_total', '一共有多少条余额流水', 'single_table', 'answer', '["SELECT COUNT(*) AS n FROM dbo.user_stock_cash"]'),
      ('users_total', '一共有多少个用户', 'single_table', 'answer', '["SELECT COUNT(*) AS n FROM dbo.users"]'),
      ('jobs_not_deleted', '有多少个职位还没被删除', 'filtered', 'answer', '["SELECT COUNT(*) AS n FROM dbo.jobs WHERE is_del = 0"]'),
      ('company_in_shenzhen', '在深圳市的公司有多少家', 'filtered', 'answer', '["SELECT COUNT(*) AS n FROM dbo.company WHERE city_name = N''深圳市''"]'),
      ('jobs_high_salary', '综合薪资上限超过一万的职位有多少个', 'filtered', 'answer', '["SELECT COUNT(*) AS n FROM dbo.jobs WHERE max_comprehensive_salary > 10000"]'),
      ('company_name_like_shenzhen', '公司名称里带“深圳”的有多少家', 'filtered', 'answer', '["SELECT COUNT(*) AS n FROM dbo.company WHERE name LIKE N''%深圳%''"]'),
      ('jobs_recruiting', '有多少个职位正在招聘中', 'filtered', 'answer', '["SELECT COUNT(*) AS n FROM dbo.jobs WHERE status = ''1'' AND is_del = ''False''"]'),
      ('jobs_recruiting_wording', '有多少个职位正在招人', 'filtered', 'answer', '["SELECT COUNT(*) AS n FROM dbo.jobs WHERE status = ''1'' AND is_del = ''False''"]'),
      ('jobs_pay_date_20th', '每月 20 号发工资的职位有多少个', 'filtered', 'answer', '["SELECT COUNT(*) AS n FROM dbo.jobs WHERE pay_money_detail_date = N''每月20号''"]'),
      ('jobs_created_2026', '2026 年发布的职位有多少个', 'time_dim', 'answer', '["SELECT COUNT(*) AS n FROM dbo.jobs WHERE create_time >= ''2026-01-01'' AND create_time < ''2027-01-01''"]'),
      ('jobs_by_year', '按年份统计职位发布数量，从早到晚列出每一年', 'time_dim', 'answer', '["SELECT YEAR(create_time) AS y, COUNT(*) AS n FROM dbo.jobs GROUP BY YEAR(create_time) ORDER BY y"]'),
      ('apply_jobs_2025', '2025 年一共有多少条投递记录', 'time_dim', 'answer', '["SELECT COUNT(*) AS n FROM dbo.apply_jobs WHERE create_time >= ''2025-01-01'' AND create_time < ''2026-01-01''"]'),
      ('jobs_created_2026_06', '2026 年 6 月发布了多少个职位', 'time_dim', 'answer', '["SELECT COUNT(*) AS n FROM dbo.jobs WHERE create_time >= ''2026-06-01'' AND create_time < ''2026-07-01''"]'),
      ('jobs_by_working_life', '按工作年限统计职位数量，只要两列：工作年限的名称，以及该年限下的职位数量（不要额外列出年限 ID）', 'cross_table', 'answer', '["SELECT w.name, COUNT(*) AS n FROM dbo.jobs j JOIN dbo.working_lifes w ON j.working_lifes_id = w.id GROUP BY w.name ORDER BY COUNT(*) DESC", "SELECT w.name, COUNT(*) AS n FROM dbo.jobs j LEFT JOIN dbo.working_lifes w ON j.working_lifes_id = w.id GROUP BY w.name ORDER BY COUNT(*) DESC"]'),
      ('top5_company_by_jobs', '发布职位最多的5家公司分别是哪几家，各发布了多少职位', 'cross_table', 'answer', '["SELECT TOP 5 c.name, COUNT(*) AS n FROM dbo.jobs j JOIN dbo.company c ON j.company_id = c.id GROUP BY c.name ORDER BY COUNT(*) DESC", "SELECT TOP 5 c.name, COUNT(*) AS n FROM dbo.jobs j JOIN dbo.company c ON j.company_id = c.id GROUP BY c.id, c.name ORDER BY COUNT(*) DESC"]'),
      ('company_name_shenzhen_jobs', '公司名字里带“深圳”的公司一共发布了多少个职位', 'cross_table', 'answer', '["SELECT COUNT(*) AS n FROM dbo.jobs j JOIN dbo.company c ON c.id = j.company_id WHERE c.name LIKE N''%深圳%''"]'),
      ('companies_with_intro', '在填了公司简介的公司里，这些公司一共发布了多少个职位（只要一个数字）', 'cross_table', 'answer', '["SELECT COUNT(*) AS n FROM dbo.jobs j JOIN dbo.company c ON c.id = j.company_id WHERE c.intro IS NOT NULL AND c.intro <> ''''"]'),
      ('top3_company_license', '发布职位最多的3家公司，它们的营业执照号分别是什么', 'cross_table', 'answer', '["SELECT TOP 3 c.name, c.business_license_number FROM dbo.jobs j JOIN dbo.company c ON c.id = j.company_id GROUP BY c.id, c.name, c.business_license_number ORDER BY COUNT(*) DESC", "SELECT TOP 3 c.name, c.business_license_number FROM dbo.jobs j JOIN dbo.company c ON c.id = j.company_id GROUP BY c.name, c.business_license_number ORDER BY COUNT(*) DESC"]'),
      ('jobs_within_500km_guangzhou', '从广州到上班地点少于 500 千米的职位有多少个？', 'filtered', 'answer', '["SELECT COUNT(*) AS n FROM dbo.jobs WHERE 111*SQRT(SQUARE(work_address_lat-23.1291)+SQUARE((work_address_lng-113.2644)*COS(RADIANS(23.1291)))) < 500"]'),
      ('jobs_avg_salary_all', '所有职位的平均薪资是多少？', 'filtered', 'answer', '["SELECT ROUND(AVG((min_comprehensive_salary+max_comprehensive_salary)/2.0), 2) AS v FROM dbo.jobs WHERE min_comprehensive_salary > 0 AND max_comprehensive_salary > 0"]'),
      ('jobs_avg_salary_guangzhou', '广州市职位的平均薪资是多少？', 'filtered', 'answer', '["SELECT ROUND(AVG((min_comprehensive_salary+max_comprehensive_salary)/2.0), 2) AS v FROM dbo.jobs WHERE min_comprehensive_salary > 0 AND max_comprehensive_salary > 0 AND work_address_city_name = N''广州市''"]'),
      ('multi_hop_working_life_companies', '按工作年限统计发布过职位的公司数量，只要两列：工作年限名称，以及该年限下发布过职位的公司数量（不要额外列出年限 ID）', 'multi_hop', 'answer', '["SELECT w.name, COUNT(DISTINCT c.id) AS n FROM dbo.working_lifes w JOIN dbo.jobs j ON j.working_lifes_id = w.id JOIN dbo.company c ON c.id = j.company_id GROUP BY w.name ORDER BY n DESC", "SELECT w.name, COUNT(DISTINCT c.name) AS n FROM dbo.working_lifes w JOIN dbo.jobs j ON j.working_lifes_id = w.id JOIN dbo.company c ON c.id = j.company_id GROUP BY w.name ORDER BY n DESC"]'),
      ('multi_hop_browsed_companies', '用户浏览过的职位一共来自多少家不同的公司？', 'multi_hop', 'answer', '["SELECT COUNT(DISTINCT c.id) AS n FROM dbo.browsing_histories b JOIN dbo.jobs j ON j.id = b.job_id JOIN dbo.company c ON c.id = j.company_id", "SELECT COUNT(DISTINCT c.name) AS n FROM dbo.browsing_histories b JOIN dbo.jobs j ON j.id = b.job_id JOIN dbo.company c ON c.id = j.company_id"]'),
      ('multi_hop_applied_companies', '收到过投递记录的职位，一共属于多少家不同的公司？', 'multi_hop', 'answer', '["SELECT COUNT(DISTINCT c.id) AS n FROM dbo.apply_jobs a JOIN dbo.jobs j ON j.id = a.job_id JOIN dbo.company c ON c.id = j.company_id", "SELECT COUNT(DISTINCT c.name) AS n FROM dbo.apply_jobs a JOIN dbo.jobs j ON j.id = a.job_id JOIN dbo.company c ON c.id = j.company_id"]'),
      ('multi_hop_applies_by_experience', '要求 1 年以下工作经验的职位一共收到了多少条投递记录？', 'multi_hop', 'answer', '["SELECT COUNT(*) AS n FROM dbo.working_lifes w JOIN dbo.jobs j ON j.working_lifes_id = w.id JOIN dbo.apply_jobs a ON a.job_id = j.id WHERE w.name = N''1年以下''"]'),
      ('multi_hop_shenzhen_company_browsed', '公司名称里带“深圳”的公司的职位，一共被用户浏览了多少次？', 'multi_hop', 'answer', '["SELECT COUNT(*) AS n FROM dbo.company c JOIN dbo.jobs j ON j.company_id = c.id JOIN dbo.browsing_histories b ON b.job_id = j.id WHERE c.name LIKE N''%深圳%''"]'),
      ('multi_hop_browsed_and_applied_jobs', '既被用户浏览过、又收到过投递的职位一共有多少个？', 'multi_hop', 'answer', '["SELECT COUNT(DISTINCT j.id) AS n FROM dbo.browsing_histories b JOIN dbo.jobs j ON j.id = b.job_id JOIN dbo.apply_jobs a ON a.job_id = j.id"]'),
      ('refuse_gross_margin', '上个月的毛利是多少', 'ambiguous', 'refuse', NULL),
      ('refuse_avg_performance', '员工的平均绩效分是多少', 'ambiguous', 'refuse', NULL),
      ('refuse_stock_price', '哪家公司的股价最高', 'ambiguous', 'refuse', NULL),
      ('refuse_customer_churn', '这个月的客户流失率是多少', 'ambiguous', 'refuse', NULL)
) AS c(name, question, tier, expect, control)
WHERE EXISTS (
    SELECT 1 FROM knowledge_base.knowledge_info
    WHERE id = 'kb_assets_r2'
      AND tenant_id = 'tenant_jonex_demo'
      AND is_deleted = 0
  )
  AND EXISTS (
    SELECT 1 FROM knowledge_base.knowledge_data_sources
    WHERE id = '4cdb29e4-268c-4daf-8abe-7b879fa6abf0'
      AND tenant_id = 'tenant_jonex_demo'
      AND knowledge_base_id = 'kb_assets_r2'
      AND is_deleted = 0
  )
ON CONFLICT DO NOTHING;

-- ------------------------------------------------------------------
-- 来源：migrations/017_evolution_diagnosis_reports.sql
-- ------------------------------------------------------------------

-- ============================================================
-- 017 演化台服务化 ②b：诊断报告表
-- 应用：空卷随 initdb 自动执行；存量库手工 `psql -f`（同 008/009/014/016 先例）。
-- 幂等：全部 CREATE ... IF NOT EXISTS。
-- ============================================================

-- ------------------------------------------------------------
-- 诊断报告（一次诊断 run 一份；纯追加，无 updated_at / is_deleted）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_base.evolution_diagnosis_reports (
  id          VARCHAR(64) PRIMARY KEY,
  tenant_id   VARCHAR(64) NOT NULL,
  params      JSONB NOT NULL DEFAULT '{}'::jsonb,
  report      JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by  VARCHAR(64),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 取最新报告（同租户按时间倒序）
CREATE INDEX IF NOT EXISTS idx_evo_diag_latest
  ON knowledge_base.evolution_diagnosis_reports (tenant_id, created_at DESC);

COMMENT ON TABLE knowledge_base.evolution_diagnosis_reports IS
    '演化台诊断报告（一次诊断 run 一份，纯追加）。见 2026-10-18 演化台服务化设计。';

-- ------------------------------------------------------------------
-- 来源：migrations/024_evolution_trust_loop.sql
-- ------------------------------------------------------------------

-- ============================================================
-- 024 演化台信任闭环（P0）：语义版本 / 归档 / 候选 + run 增列
-- 应用：空卷随 initdb 自动执行；存量库手工 `psql -f`（同 016 / 023 先例）。
-- 幂等：整体包在 BEGIN/COMMIT；建表与增列全部 IF NOT EXISTS，
--       约束先 DROP CONSTRAINT IF EXISTS 再 ADD。
-- 与 deploy/postgres/update/072_evolution_trust_loop.sql 逐字节相同。
-- 设计：docs/superpowers/specs/2026-10-23-evolution-trust-loop-design.md §3。
-- ============================================================
BEGIN;

-- ------------------------------------------------------------
-- evolution_runs 增列（新列全部 nullable 或有默认值，旧 API 字段不变）
-- ------------------------------------------------------------
ALTER TABLE knowledge_base.evolution_runs
  ADD COLUMN IF NOT EXISTS parent_version_id VARCHAR(64),
  ADD COLUMN IF NOT EXISTS gate_decision VARCHAR(16),
  ADD COLUMN IF NOT EXISTS gate_adhoc JSONB,
  ADD COLUMN IF NOT EXISTS gate_effective VARCHAR(16),
  ADD COLUMN IF NOT EXISTS adopted_version_id VARCHAR(64),
  ADD COLUMN IF NOT EXISTS adopted_by VARCHAR(64),
  ADD COLUMN IF NOT EXISTS adopted_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS adopt_request_id VARCHAR(64),
  ADD COLUMN IF NOT EXISTS gate_override SMALLINT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS override_by VARCHAR(64),
  ADD COLUMN IF NOT EXISTS override_reason TEXT,
  ADD COLUMN IF NOT EXISTS smoke_status VARCHAR(16),
  ADD COLUMN IF NOT EXISTS smoke_mode VARCHAR(16),
  ADD COLUMN IF NOT EXISTS smoke JSONB,
  ADD COLUMN IF NOT EXISTS rolled_back_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS rolled_back_by VARCHAR(64),
  ADD COLUMN IF NOT EXISTS rollback_reason TEXT,
  ADD COLUMN IF NOT EXISTS legacy_adopt SMALLINT NOT NULL DEFAULT 0;

-- 重建 status CHECK，加入 adopting / smoke-failed（旧值不受影响）
ALTER TABLE knowledge_base.evolution_runs
  DROP CONSTRAINT IF EXISTS ck_evo_run_status;
ALTER TABLE knowledge_base.evolution_runs
  ADD CONSTRAINT ck_evo_run_status CHECK (status IN
    ('queued','running','evaluated','adopting','adopted','rejected',
     'rolled-back','smoke-failed','failed','dry-run','empty-draft'));

-- 采纳幂等键：同租户下 adopt_request_id 唯一（NULL 不参与）
CREATE UNIQUE INDEX IF NOT EXISTS uq_evo_adopt_request
  ON knowledge_base.evolution_runs (tenant_id, adopt_request_id)
  WHERE adopt_request_id IS NOT NULL;

-- ------------------------------------------------------------
-- 语义版本（不可变快照；只允许改 status）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_base.semantic_versions (
  id                VARCHAR(64) PRIMARY KEY,
  tenant_id         VARCHAR(64) NOT NULL,
  knowledge_base_id VARCHAR(128) NOT NULL,
  scope             VARCHAR(16) NOT NULL,
  data_source_id    VARCHAR(64),
  parent_version_id VARCHAR(64),
  source_run_id     VARCHAR(64),
  source_version_id VARCHAR(64),
  snapshot          JSONB NOT NULL,
  snapshot_hash     CHAR(64) NOT NULL,
  status            VARCHAR(16) NOT NULL,
  created_by        VARCHAR(64),
  legacy            SMALLINT NOT NULL DEFAULT 0,
  note              TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  activated_at      TIMESTAMPTZ,
  CONSTRAINT ck_evo_version_scope CHECK (scope IN ('abstract','compiled')),
  CONSTRAINT ck_evo_version_status CHECK (status IN ('candidate','active','superseded','rejected')),
  CONSTRAINT ck_evo_version_shape CHECK (
    (scope='abstract' AND data_source_id IS NULL) OR
    (scope='compiled' AND data_source_id IS NOT NULL))
);

-- 每 (tenant, kb, scope, coalesce(ds,'')) 至多一条 active
CREATE UNIQUE INDEX IF NOT EXISTS uq_evo_version_active
  ON knowledge_base.semantic_versions
     (tenant_id, knowledge_base_id, scope, coalesce(data_source_id,''))
  WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_evo_version_list
  ON knowledge_base.semantic_versions
     (tenant_id, knowledge_base_id, scope, created_at DESC);

-- ------------------------------------------------------------
-- 归档表：与 semantic_versions 同构 + archived_at；**不加** active 唯一索引
-- （只由归档任务写入；同一 id 不会同时存在于两表）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_base.semantic_versions_archive (
  id                VARCHAR(64) PRIMARY KEY,
  tenant_id         VARCHAR(64) NOT NULL,
  knowledge_base_id VARCHAR(128) NOT NULL,
  scope             VARCHAR(16) NOT NULL,
  data_source_id    VARCHAR(64),
  parent_version_id VARCHAR(64),
  source_run_id     VARCHAR(64),
  source_version_id VARCHAR(64),
  snapshot          JSONB NOT NULL,
  snapshot_hash     CHAR(64) NOT NULL,
  status            VARCHAR(16) NOT NULL,
  created_by        VARCHAR(64),
  legacy            SMALLINT NOT NULL DEFAULT 0,
  note              TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  activated_at      TIMESTAMPTZ,
  archived_at       TIMESTAMPTZ NOT NULL,
  CONSTRAINT ck_evo_version_archive_scope CHECK (scope IN ('abstract','compiled')),
  CONSTRAINT ck_evo_version_archive_status CHECK (status IN ('candidate','active','superseded','rejected')),
  CONSTRAINT ck_evo_version_archive_shape CHECK (
    (scope='abstract' AND data_source_id IS NULL) OR
    (scope='compiled' AND data_source_id IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS idx_evo_version_archive_list
  ON knowledge_base.semantic_versions_archive
     (tenant_id, knowledge_base_id, scope, created_at DESC);

-- ------------------------------------------------------------
-- 候选（从 run.patch 独立出来；按 run 级联删除）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_base.evolution_candidates (
  id                VARCHAR(64) PRIMARY KEY,
  run_id            VARCHAR(64) NOT NULL REFERENCES knowledge_base.evolution_runs(id) ON DELETE CASCADE,
  candidate_no      INTEGER NOT NULL,
  parent_version_id VARCHAR(64),
  layer             VARCHAR(16) NOT NULL,
  edit_type         VARCHAR(32) NOT NULL,
  target            JSONB NOT NULL DEFAULT '[]'::jsonb,
  before            JSONB NOT NULL DEFAULT '{}'::jsonb,
  after             JSONB NOT NULL DEFAULT '{}'::jsonb,
  expected_changes  JSONB NOT NULL DEFAULT '[]'::jsonb,
  rationale         TEXT,
  evidence          JSONB NOT NULL DEFAULT '{}'::jsonb,
  gate_decision     VARCHAR(16),
  gate_reasons      JSONB,
  status            VARCHAR(16) NOT NULL DEFAULT 'draft',
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_evo_candidate_no UNIQUE (run_id, candidate_no),
  CONSTRAINT ck_evo_candidate_layer CHECK (layer IN ('content','tool','schema')),
  CONSTRAINT ck_evo_candidate_status CHECK (status IN ('draft','evaluated','adopted','rejected','superseded'))
);

CREATE INDEX IF NOT EXISTS idx_evo_candidate_run
  ON knowledge_base.evolution_candidates (run_id, candidate_no);

COMMENT ON TABLE knowledge_base.semantic_versions IS
    '语义版本（不可变快照）。见 2026-10-23 演化台信任闭环设计 §3.1。';
COMMENT ON TABLE knowledge_base.semantic_versions_archive IS
    '语义版本归档（与主表同构 + archived_at；无 active 唯一索引）。';
COMMENT ON TABLE knowledge_base.evolution_candidates IS
    '演化候选（含证据与门禁结论；按 run 级联）。';

COMMIT;

-- ------------------------------------------------------------------
-- 来源：migrations/025_evolution_rollback_pending.sql
-- ------------------------------------------------------------------

-- ============================================================
-- 025 演化台回滚崩溃窗口：pending 标记（A3）
-- 应用：空卷随 initdb 自动执行；存量库手工 `psql -f`（同 016 / 024 先例）。
-- 幂等：整体包在 BEGIN/COMMIT；增列全部 IF NOT EXISTS，可反复执行。
-- 与 deploy/postgres/update/073_evolution_rollback_pending.sql 逐字节相同。
--
-- 语义：版本回滚在 `restore_snapshot` **之前**先用 CAS 写入
-- `rollback_target_version_id` / `rollback_from_version_id` / `rollback_started_at`；
-- 成功收口（run → rolled-back）只清 `rollback_started_at`（保留目标与来源供
-- 重放展示），进程内补偿 / 恢复失败时三者全清。服务重启后 `recover_stale`
-- 据此回收超过 30 分钟的 pending 回滚，并用 `rollback_from_version_id` 判断
-- 版本链是否已被别的采纳推进（推进则绝不再恢复/激活）。
-- ============================================================
BEGIN;

ALTER TABLE knowledge_base.evolution_runs
  ADD COLUMN IF NOT EXISTS rollback_target_version_id VARCHAR(64),
  ADD COLUMN IF NOT EXISTS rollback_from_version_id VARCHAR(64),
  ADD COLUMN IF NOT EXISTS rollback_started_at TIMESTAMPTZ;

COMMIT;

-- ------------------------------------------------------------------
-- 来源：migrations/026_evolution_bench.sql
-- ------------------------------------------------------------------

-- 026 演化台完整语料基准（P1）：run 增列
-- 应用：空卷随 initdb 自动执行；存量库手工 `psql -f`。
-- 幂等：整体 BEGIN/COMMIT；增列 IF NOT EXISTS；约束先 DROP 再 ADD。
-- 与 deploy/postgres/update/078_evolution_bench.sql 逐字节相同。
BEGIN;

ALTER TABLE knowledge_base.evolution_runs
  ADD COLUMN IF NOT EXISTS bench JSONB,
  ADD COLUMN IF NOT EXISTS bench_status VARCHAR(16),
  ADD COLUMN IF NOT EXISTS bench_mode VARCHAR(16),
  ADD COLUMN IF NOT EXISTS bench_started_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS bench_ended_at TIMESTAMPTZ;

ALTER TABLE knowledge_base.evolution_runs
  DROP CONSTRAINT IF EXISTS ck_evo_bench_status;
ALTER TABLE knowledge_base.evolution_runs
  ADD CONSTRAINT ck_evo_bench_status CHECK (
    bench_status IS NULL OR bench_status IN
    ('pending','parent_ran','passed','failed','warn','skipped'));

COMMIT;

-- ------------------------------------------------------------------
-- 来源：migrations/027_evolution_relation_kind.sql
-- ------------------------------------------------------------------

-- ============================================================
-- 027 演化台 P1-3：relation 演化 kind
-- 应用：空卷随 initdb 自动执行；存量库手工 `psql -f`。
-- 幂等：整体 BEGIN/COMMIT；约束先 DROP IF EXISTS 再 ADD（同 024 风格）。
-- relation 与 query_rule 共用 compiled 版本链（R1），但 run.kind 必须能落库。
-- 设计：docs/superpowers/plans/2026-10-25-evolution-p1-3-relation-kind-plan.md
-- ============================================================
BEGIN;

ALTER TABLE knowledge_base.evolution_runs
  DROP CONSTRAINT IF EXISTS ck_evo_run_kind;
ALTER TABLE knowledge_base.evolution_runs
  ADD CONSTRAINT ck_evo_run_kind CHECK (
    kind IN ('alias','query_rule','relation'));

COMMIT;

-- ------------------------------------------------------------------
-- 来源：migrations/028_evolution_constraint_kind.sql
-- ------------------------------------------------------------------

-- ============================================================
-- 028 演化台 P1-4：constraint 演化 kind
-- 应用：空卷随 initdb 自动执行；存量库手工 `psql -f`。
-- 幂等：整体 BEGIN/COMMIT；约束先 DROP IF EXISTS 再 ADD（同 024 风格）。
-- constraint 与 query_rule / relation 共用 compiled 版本链（R5），但 run.kind
-- 必须能落库。**先迁移再起服务**。
-- 设计：docs/superpowers/plans/2026-10-25-evolution-p1-4-constraint-editable-plan.md
-- ============================================================
BEGIN;

ALTER TABLE knowledge_base.evolution_runs
  DROP CONSTRAINT IF EXISTS ck_evo_run_kind;
ALTER TABLE knowledge_base.evolution_runs
  ADD CONSTRAINT ck_evo_run_kind CHECK (
    kind IN ('alias','query_rule','relation','constraint'));

COMMIT;

-- ------------------------------------------------------------------
-- 来源：migrations/029_evolution_mapping_kind.sql
-- ------------------------------------------------------------------

-- 演化台 P1-5：kind 白名单加 mapping（abstract 家族，与 alias 同链）
BEGIN;
ALTER TABLE knowledge_base.evolution_runs
    DROP CONSTRAINT IF EXISTS ck_evo_run_kind;
ALTER TABLE knowledge_base.evolution_runs
    ADD CONSTRAINT ck_evo_run_kind
    CHECK (kind IN ('alias', 'query_rule', 'relation', 'constraint', 'mapping'));
COMMIT;

-- ------------------------------------------------------------------
-- 来源：migrations/030_evolution_corpus_versions.sql
-- ------------------------------------------------------------------

-- 演化台语料版本（2026-10-26）：append-only，每 (tenant,kb,ds) 链一条 active。
BEGIN;
CREATE TABLE IF NOT EXISTS knowledge_base.evolution_corpus_versions (
  id varchar(64) PRIMARY KEY,
  tenant_id varchar(64) NOT NULL,
  knowledge_base_id varchar(128) NOT NULL,
  data_source_id varchar(64) NOT NULL,
  snapshot jsonb NOT NULL,
  snapshot_hash char(64) NOT NULL,
  status varchar(16) NOT NULL DEFAULT 'active',
  source varchar(32) NOT NULL DEFAULT 'manual',
  note text,
  created_by varchar(64),
  created_at timestamptz NOT NULL DEFAULT now(),
  activated_at timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_evo_corpus_version_active
  ON knowledge_base.evolution_corpus_versions
     (tenant_id, knowledge_base_id, data_source_id)
  WHERE status = 'active';
CREATE INDEX IF NOT EXISTS ix_evo_corpus_version_chain
  ON knowledge_base.evolution_corpus_versions
     (tenant_id, knowledge_base_id, data_source_id, created_at DESC);
COMMIT;

-- ------------------------------------------------------------------
-- 来源：migrations/031_evolution_run_corpus_provenance.sql
-- ------------------------------------------------------------------

-- 演化台 run 的语料来源（2026-10-26）：本次实际用的语料版本与内容 hash。
BEGIN;
ALTER TABLE knowledge_base.evolution_runs
  ADD COLUMN IF NOT EXISTS corpus_version_id varchar(64);
ALTER TABLE knowledge_base.evolution_runs
  ADD COLUMN IF NOT EXISTS corpus_hash char(64);
COMMIT;

-- ------------------------------------------------------------------
-- 来源：migrations/032_evolution_tool_schema_kind.sql
-- ------------------------------------------------------------------

-- 演化台 Tool-Schema 线：kind 白名单加 tool_schema（abstract 家族，与 alias/mapping 同链）
BEGIN;
ALTER TABLE knowledge_base.evolution_runs
    DROP CONSTRAINT IF EXISTS ck_evo_run_kind;
ALTER TABLE knowledge_base.evolution_runs
    ADD CONSTRAINT ck_evo_run_kind
    CHECK (kind IN ('alias', 'query_rule', 'relation', 'constraint', 'mapping', 'tool_schema'));
COMMIT;

-- ------------------------------------------------------------------
-- 来源：migrations/033_abstract_schema_save_history.sql
-- ------------------------------------------------------------------

-- 抽象 schema 保存历史（2026-10-27）：公开保存/确认/导入/还原每次留一份快照。
-- 整体幂等，可重复执行（存量库先 psql -f 本文件再启动新版 KB 服务）。
BEGIN;
CREATE TABLE IF NOT EXISTS knowledge_base.abstract_schema_save_history (
  id varchar(64) PRIMARY KEY,
  tenant_id varchar(64) NOT NULL,
  knowledge_base_id varchar(128) NOT NULL,
  schema_version integer NOT NULL,
  status varchar(32) NOT NULL,
  schema jsonb NOT NULL,
  saved_by varchar(128),
  source varchar(16) NOT NULL DEFAULT 'manual',   -- manual | restore
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_oas_history_kb
  ON knowledge_base.abstract_schema_save_history (tenant_id, knowledge_base_id, created_at DESC);
COMMIT;
