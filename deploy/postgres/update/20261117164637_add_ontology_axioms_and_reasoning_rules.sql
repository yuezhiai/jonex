-- ============================================================
-- 本体公理表 + 推理规则表 + KB 级本体/推理开关（增量，2026-11-17）
--
-- 与 `migrations/003_knowledge_base.sql` 末尾/ knowledge_info 定义保持一致。
-- 幂等：ADD COLUMN IF NOT EXISTS / CREATE TABLE/INDEX IF NOT EXISTS。
-- 部署顺序：先执行本文件，再启动新版 KB 服务，否则 ORM 读新列会 500。
-- ============================================================
BEGIN;

ALTER TABLE knowledge_base.knowledge_info
    ADD COLUMN IF NOT EXISTS ontology_enabled boolean NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS reasoning_enabled boolean NOT NULL DEFAULT false;

-- ============================================================
-- 本体公理（D8）：KB 级唯一权威；抽象 schema 的 axioms 只作 Studio 往返镜像。
-- 表只存声明，不存闭包/继承等派生内容。
-- ============================================================

CREATE TABLE IF NOT EXISTS knowledge_base.ontology_axioms (
    id varchar(64) PRIMARY KEY,
    tenant_id varchar(64) NOT NULL,
    knowledge_base_id varchar(128) NOT NULL,
    axiom_key varchar(64) NOT NULL,
    kind varchar(32) NOT NULL,
    subject jsonb NOT NULL DEFAULT '{}'::jsonb,
    object jsonb,
    chain jsonb NOT NULL DEFAULT '[]'::jsonb,
    facets jsonb NOT NULL DEFAULT '[]'::jsonb,
    status varchar(16) NOT NULL DEFAULT 'active',
    source varchar(16) NOT NULL DEFAULT 'imported',
    origin varchar(16) NOT NULL DEFAULT 'imported',
    module varchar(64),
    content_revision integer NOT NULL DEFAULT 1,
    content_hash varchar(64),
    description text,
    created_by varchar(64),
    updated_by varchar(64),
    is_deleted smallint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_ontology_axioms_status
        CHECK (status IN ('proposed', 'active', 'rejected')),
    CONSTRAINT ck_ontology_axioms_source
        CHECK (source IN ('manual', 'imported', 'extracted')),
    CONSTRAINT ck_ontology_axioms_origin
        CHECK (origin IN ('studio', 'platform', 'imported'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ontology_axioms_key
    ON knowledge_base.ontology_axioms (tenant_id, knowledge_base_id, axiom_key)
    WHERE is_deleted = 0;

CREATE INDEX IF NOT EXISTS idx_ontology_axioms_kb
    ON knowledge_base.ontology_axioms (tenant_id, knowledge_base_id, status);

-- ============================================================
-- 推理规则：AST 为权威；content_revision 只随内容变化，启停单独留痕。
-- ============================================================

CREATE TABLE IF NOT EXISTS knowledge_base.ontology_reasoning_rules (
    id varchar(64) PRIMARY KEY,
    tenant_id varchar(64) NOT NULL,
    knowledge_base_id varchar(128) NOT NULL,
    rule_key varchar(64) NOT NULL,
    name varchar(128) NOT NULL,
    text text NOT NULL,
    premises jsonb NOT NULL DEFAULT '[]'::jsonb,
    conclusion jsonb NOT NULL DEFAULT '{}'::jsonb,
    status varchar(16) NOT NULL DEFAULT 'active',
    enabled boolean NOT NULL DEFAULT true,
    enabled_changed_at timestamptz,
    enabled_changed_by varchar(128),
    origin varchar(16) NOT NULL DEFAULT 'imported',
    module varchar(64),
    content_revision integer NOT NULL DEFAULT 1,
    content_hash varchar(64),
    description text,
    created_by varchar(64),
    updated_by varchar(64),
    is_deleted smallint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_ontology_reasoning_rules_status
        CHECK (status IN ('proposed', 'active', 'rejected')),
    CONSTRAINT ck_ontology_reasoning_rules_origin
        CHECK (origin IN ('studio', 'platform', 'imported'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ontology_reasoning_rules_key
    ON knowledge_base.ontology_reasoning_rules (tenant_id, knowledge_base_id, rule_key)
    WHERE is_deleted = 0;

CREATE UNIQUE INDEX IF NOT EXISTS uq_ontology_reasoning_rules_name
    ON knowledge_base.ontology_reasoning_rules (tenant_id, knowledge_base_id, name)
    WHERE is_deleted = 0;

CREATE INDEX IF NOT EXISTS idx_ontology_reasoning_rules_kb
    ON knowledge_base.ontology_reasoning_rules
       (tenant_id, knowledge_base_id, enabled, status);

COMMIT;
