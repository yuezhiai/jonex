-- ============================================================
-- 082_ontology_skill_action_tools.sql
-- Skill / Action 动态本体：库级抽取开关 + 本体实例→工具绑定表 + 知识库工具表 + 导航「工具目录」入口
--
-- 各段互不依赖，按段落各自幂等；任一段已存在都会被跳过，可以对已执行过其中部分内容的库重跑。
--
-- 为什么只写 update/、不并回全量脚本
--   004_knowledge_base.sql 已线上发布，与 057 同例只碰增量脚本；全新的库照常跑一次
--   db-migrate（deploy/README.md 的既有条款）。006 的菜单种子另有同一行，见第 3 段说明。
--
-- 发版次序：**先 db-migrate，再换 knowledge-base / business-domain 新镜像** ——
--   反序时新代码读 knowledge_info.procedural_extract 或查绑定表会当场 UndefinedColumn / UndefinedTable。
--
-- 回滚（按段）
--   1) UPDATE knowledge_base.knowledge_info SET procedural_extract = NULL;
--      （派单时读，即刻生效）；或关总闸 ONTOLOGY_PROCEDURAL_EXTRACT_ENABLED=false。
--   2) DROP TABLE IF EXISTS knowledge_base.ontology_tool_bindings;
--      （无其它表引用；执行器在表空时按「无绑定 → 转人工」处理）。
--   2b) DROP TABLE IF EXISTS knowledge_base.kb_tools;
--      （绑定到本库工具的行随之悬空，列表标「工具不可用」，执行器按不可用拒绝）。
--   3) UPDATE platform.menus SET is_deleted = 1 WHERE path = '/apps/ecosystem-management/skills';
-- ============================================================

BEGIN;

-- ------------------------------------------------------------
-- 1. 库级 procedural（Skill/Action 规程）抽取开关
--
-- 为什么要库级开关
--   procedural 子管线原先的每文档触发条件是「compiled schema 含 Action 类型」。
--   内置 4 份 TBox（default / technology / nanfang_publishing / sasseur_fashion）
--   都已声明 Action，凡用它们的 KB 都满足条件 ⇒ 打开进程级总闸
--   ONTOLOGY_PROCEDURAL_EXTRACT_ENABLED 就等于对所有这类 KB 全量打开，
--   每份文档额外最多 ONTOLOGY_PROCEDURAL_CHUNK_MAX 次 LLM 调用，无法按库灰度。
--
-- 语义（代码侧：capabilities/knowledge_base/services/kb_type_service.py
--       get_kb_procedural_extract；jonex_core/capability/atomic/rag/native_ontology_extractor.py
--       的 procedural gate）
--   TRUE  = 该库参与 procedural 抽取（仍需总闸开、schema 含 Action）；
--   NULL / FALSE = 不参与（存量行全 NULL ⇒ 行为与从前一致：总闸关时本来就不跑）。
--   派单时读（提交那一刻求值），随 schema 下发给执行方；改列只影响下一次派单，
--   在途任务按派单时的值执行，无需重启任何服务。界面上在知识库「编译」页设置。
-- ------------------------------------------------------------
ALTER TABLE knowledge_base.knowledge_info
    ADD COLUMN IF NOT EXISTS procedural_extract BOOLEAN;
COMMENT ON COLUMN knowledge_base.knowledge_info.procedural_extract IS
    '库级 procedural（Skill/Action 规程）抽取开关：TRUE=参与 / NULL·FALSE=不参与；与总闸 ONTOLOGY_PROCEDURAL_EXTRACT_ENABLED AND 生效';

-- ------------------------------------------------------------
-- 2. 本体 Skill / Action 实例 → 平台可执行工具（business_domain.skill_catalog.tool_name）绑定表
--
-- 为什么需要
--   本体里的 Action 是从制度文档抽取的知识（谁能做、做前查什么、失败怎么办），
--   本身不可执行；平台可执行的是工具目录（SkillCatalog）里的工具。两者之间的对应关系由
--   知识库管理员人工配置（抽取侧不知道平台有哪些工具），存在这张表里。
--   执行器经 knowledge_base 的 `resolve_action_execution` 读取它，再按契约字段判定能否执行。
--
-- 语义（代码侧：capabilities/knowledge_base/services/ontology_tool_binding_service.py）
--   实例身份 = (tenant_id, knowledge_base_id, entity_type, canonical_name)；
--   一个实例同时只能有一条有效绑定（is_deleted = 0），见下面的部分唯一索引。
--   重抽后实例改名或类型变化会让绑定悬空：列表标注「绑定目标不存在」，
--   不自动迁移、不静默删除。
--   tool_name 只存名字，不建跨 schema 外键：工具是否可用（已上架且本租户已启用）
--   在读取时实时校验，工具被下架 / 停用不需要回头改绑定。
--   param_mapping：工具入参 → 取值来源，形如
--     {"card_no": "$input.card_no", "reason": "$attr.definition", "channel": "counter"}
--   （$input.* = 执行时调用方传入；$attr.* = 该 Action 的本体属性；$entity.name = 实例名；
--    其余为字面量）。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_base.ontology_tool_bindings (
    id                 VARCHAR(64)  PRIMARY KEY,
    tenant_id          VARCHAR(64)  NOT NULL,
    knowledge_base_id  VARCHAR(128) NOT NULL,
    entity_type        VARCHAR(32)  NOT NULL,
    canonical_name     VARCHAR(512) NOT NULL,
    tool_source        VARCHAR(16)  NOT NULL DEFAULT 'platform',
    tool_name          VARCHAR(128) NOT NULL,
    param_mapping      JSONB        NOT NULL DEFAULT '{}'::jsonb,
    enabled            BOOLEAN      NOT NULL DEFAULT TRUE,
    created_by         VARCHAR(64),
    updated_by         VARCHAR(64),
    created_at         TIMESTAMP    NOT NULL DEFAULT now(),
    updated_at         TIMESTAMP    NOT NULL DEFAULT now(),
    is_deleted         SMALLINT     NOT NULL DEFAULT 0,
    CONSTRAINT ck_ontology_tool_bindings_entity_type CHECK (entity_type IN ('Skill', 'Action')),
    CONSTRAINT ck_ontology_tool_bindings_tool_source CHECK (tool_source IN ('platform', 'kb'))
);

-- 先于 tool_source 列建表的库（本文件早期版本只建了不带该列的表）：补列与约束
ALTER TABLE knowledge_base.ontology_tool_bindings
    ADD COLUMN IF NOT EXISTS tool_source VARCHAR(16) NOT NULL DEFAULT 'platform';
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_ontology_tool_bindings_tool_source'
    ) THEN
        ALTER TABLE knowledge_base.ontology_tool_bindings
            ADD CONSTRAINT ck_ontology_tool_bindings_tool_source CHECK (tool_source IN ('platform', 'kb'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_ont_tool_bindings_tenant_kb
    ON knowledge_base.ontology_tool_bindings (tenant_id, knowledge_base_id, is_deleted);

-- 一个本体实例同时只能有一条有效绑定（软删的行不占位，可重新绑定）
CREATE UNIQUE INDEX IF NOT EXISTS uq_ont_tool_bindings_entity_active
    ON knowledge_base.ontology_tool_bindings (tenant_id, knowledge_base_id, entity_type, canonical_name)
    WHERE is_deleted = 0;

COMMENT ON TABLE knowledge_base.ontology_tool_bindings IS
    '本体 Skill/Action 实例到平台可执行工具（business_domain.skill_catalog.tool_name）的人工绑定';
COMMENT ON COLUMN knowledge_base.ontology_tool_bindings.entity_type IS '绑定的本体实例类型：Skill / Action';
COMMENT ON COLUMN knowledge_base.ontology_tool_bindings.canonical_name IS '绑定的本体实例名（canonical_name）；重抽改名后绑定悬空，不自动迁移';
COMMENT ON COLUMN knowledge_base.ontology_tool_bindings.tool_source IS '工具来源：platform = 平台工具目录（business_domain.skill_catalog）/ kb = 本库工具（knowledge_base.kb_tools）';
COMMENT ON COLUMN knowledge_base.ontology_tool_bindings.tool_name IS '按 tool_source 指向 skill_catalog.tool_name 或同库 kb_tools.tool_name；可用性在读取时实时校验';
COMMENT ON COLUMN knowledge_base.ontology_tool_bindings.param_mapping IS '工具入参 → 取值来源：$input.<key> / $attr.<属性> / $entity.name / 字面量';
COMMENT ON COLUMN knowledge_base.ontology_tool_bindings.enabled IS '绑定启停；停用后执行器按「无绑定」处理';

-- ------------------------------------------------------------
-- 2b. 知识库工具（KB 级工具目录）
--
-- 为什么需要
--   平台工具目录只有平台管理员能维护，租户只能启用；知识库常要对接本业务自己的 API
--   （查卡状态、提交工单……），这些接口只对这个库有意义。知识库写权限用户在这里登记
--   本库工具，再在「工具绑定」里绑到本体的 Skill / Action 上。
--
-- 语义（代码侧：capabilities/knowledge_base/services/kb_tool_service.py）
--   tool_name 在同一知识库内唯一（软删的行不占名）；创建后不可改（绑定按它引用）。
--   runtime：与平台工具同一份格式（jonex_core/common/tool_runtime.py：echo / api）。
--   secret_encrypted：鉴权凭据（Bearer token / API Key / Basic 密码）经
--   jonex_core/common/crypto.py 加密后的密文，任何接口都不回传，只回 has_secret。
--   执行只发生在 knowledge-base 服务内（内部 action run_kb_tool），凭据不出这个服务。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_base.kb_tools (
    id                 VARCHAR(64)  PRIMARY KEY,
    tenant_id          VARCHAR(64)  NOT NULL,
    knowledge_base_id  VARCHAR(128) NOT NULL,
    tool_name          VARCHAR(128) NOT NULL,
    name               VARCHAR(255) NOT NULL,
    description        TEXT,
    input_schema       JSONB        NOT NULL DEFAULT '{}'::jsonb,
    output_schema      JSONB        NOT NULL DEFAULT '{}'::jsonb,
    runtime            JSONB,
    secret_encrypted   TEXT,
    enabled            BOOLEAN      NOT NULL DEFAULT TRUE,
    created_by         VARCHAR(64),
    updated_by         VARCHAR(64),
    created_at         TIMESTAMP    NOT NULL DEFAULT now(),
    updated_at         TIMESTAMP    NOT NULL DEFAULT now(),
    is_deleted         SMALLINT     NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_kb_tools_tenant_kb
    ON knowledge_base.kb_tools (tenant_id, knowledge_base_id, is_deleted);

-- 同一知识库内工具名唯一（软删的行不占名，可重建同名工具）
CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_tools_name_active
    ON knowledge_base.kb_tools (tenant_id, knowledge_base_id, tool_name)
    WHERE is_deleted = 0;

COMMENT ON TABLE knowledge_base.kb_tools IS '知识库工具：本库登记的可执行 API 工具，可绑定到本体 Skill/Action';
COMMENT ON COLUMN knowledge_base.kb_tools.tool_name IS '库内唯一的工具名；创建后不可改（ontology_tool_bindings 按它引用）';
COMMENT ON COLUMN knowledge_base.kb_tools.runtime IS '运行时配置（echo / api），格式见 jonex_core/common/tool_runtime.py；NULL = 没有可调入口';
COMMENT ON COLUMN knowledge_base.kb_tools.secret_encrypted IS '鉴权凭据密文（Fernet，DATA_SOURCE_SECRET_KEY）；接口只回 has_secret，不回明文或密文';

-- ------------------------------------------------------------
-- 3. 导航「工具目录」入口（/apps/ecosystem-management/skills）
--
-- 为什么需要
--   shell 托管模式的侧栏只认 platform.menus（GET /platform/menus/my），子应用自己路由里的
--   菜单元数据只用于 standalone 侧栏。这个页面（租户启用 / 停用工具，平台管理员另有
--   「目录管理」页签）此前从未进过菜单表 ⇒ 托管模式下没有入口，只能手输地址。
--   菜单键沿用 navigation.skillCatalog（显示文案在共享语言包里是「工具目录」）。
--
-- 挂在哪
--   优先挂「集成扩展」组（navigation.integrationExtension）；旧结构的库没有这个组，
--   回退到「生态管理」组（navigation.ecosystemManagement）；两者都没有时挂顶层。
--   权限码 skill:read（tenant scope）：租户管理员也能看到，与页面路由守卫、后端 list_skills 同一个码。
--
-- 幂等：按 path 判断已存在（含软删）即跳过 —— 运维软删隐藏后重跑本脚本不会把它复活。
--
-- id：先把 menus_id_seq 推到 MAX(id)，再走序列取号。有的存量脚本用 MAX(id)+1 显式插 id、
--   不推序列，序列可能落后于现有最大 id，直接取号会撞主键。
-- ------------------------------------------------------------
SELECT setval('platform.menus_id_seq', GREATEST((SELECT COALESCE(MAX(id), 1) FROM platform.menus), 1));

DO $$
DECLARE
    v_parent BIGINT;
    v_sort   INT;
BEGIN
    IF EXISTS (
        SELECT 1 FROM platform.menus WHERE path = '/apps/ecosystem-management/skills'
    ) THEN
        RAISE NOTICE '工具目录菜单已存在，跳过';
        RETURN;
    END IF;

    SELECT id INTO v_parent
      FROM platform.menus
     WHERE name = 'navigation.integrationExtension' AND is_deleted = 0
     ORDER BY id
     LIMIT 1;

    IF v_parent IS NULL THEN
        SELECT id INTO v_parent
          FROM platform.menus
         WHERE name = 'navigation.ecosystemManagement' AND is_deleted = 0
         ORDER BY id
         LIMIT 1;
    END IF;

    v_parent := COALESCE(v_parent, 0);

    -- 排在同组现有项之后
    SELECT COALESCE(MAX(sort_order), 0) + 1 INTO v_sort
      FROM platform.menus
     WHERE parent_id = v_parent AND is_deleted = 0;

    INSERT INTO platform.menus
        (parent_id, name, path, icon, app_id, sort_order, visible, status, permission_code)
    VALUES
        (v_parent,
         'navigation.skillCatalog',
         '/apps/ecosystem-management/skills',
         'ThunderboltOutlined',
         NULL,
         v_sort,
         1,
         1,
         'skill:read');
END $$;

COMMIT;
