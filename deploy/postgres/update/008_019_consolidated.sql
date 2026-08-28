-- [jonex] 存量库增量 DDL 合并脚本（008-019 合并版）
-- 由以下原脚本按序合并（内容原样保留，分节标记）：
--   008_pipeline_type_migration  010_llm_wiki_compile  011_document_content_hash
--   012_search_history_snapshot  013_llm_wiki_schema_columns  014_engine_catalog_shared
--   015_rbac_activation          016_mcp_key_lifecycle  017_mcp_service_publish
--   018_permission_convergence   019_kb_documents_schema_drift
-- 全部 IF [NOT] EXISTS 幂等，可重复执行；全新库亦安全（migrations/ 已建齐）。
-- 注意：015 段为清空重种语义（RBAC 五表每次执行都会删除重建，用户已决策接受）。
-- 手工执行：docker exec -i jonex-postgres psql -U jonex -d jonex \
--             < deploy/postgres/update/008_019_consolidated.sql
-- 或经执行器：bash deploy/postgres/update/apply.sh

-- ══════════════════════════════════════════════════════════════
-- 分节：原 008_pipeline_type_migration.sql
-- ══════════════════════════════════════════════════════════════

-- ── [jonex] kb_type 从 service_knowledge_bases 迁移到 knowledge_info ──
-- 原名 pipeline_type，迁移时重命名为 kb_type（知识库自身的一级属性）。
-- 幂等注意：pipeline_type 旧列首次执行后已 DROP，重跑时回填/DROP 依赖列存在性判断。

-- ① 在 knowledge_info 新增列（幂等）
ALTER TABLE knowledge_base.knowledge_info
    ADD COLUMN IF NOT EXISTS kb_type VARCHAR(16) NOT NULL DEFAULT 'lightrag';

-- ②③④ 仅当 pipeline_type 旧列仍存在时执行（列已 DROP 的库直接跳过，保证幂等）
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'knowledge_base'
          AND table_name = 'service_knowledge_bases'
          AND column_name = 'pipeline_type'
    ) THEN
        -- ② 从 service_knowledge_bases 回填数据（一 KB 多服务时 DISTINCT ON 去重，优先 openkb）
        UPDATE knowledge_base.knowledge_info ki
        SET kb_type = sub.pipeline_type
        FROM (
            SELECT DISTINCT ON (kb_id)
                kb_id,
                pipeline_type
            FROM knowledge_base.service_knowledge_bases
            WHERE is_deleted = 0
            ORDER BY kb_id,
                     CASE pipeline_type WHEN 'openkb' THEN 0 ELSE 1 END,
                     pipeline_type
        ) sub
        WHERE ki.id = sub.kb_id
          AND ki.kb_type <> sub.pipeline_type;

        -- ③ 文档 extra_metadata 快照：新增 kb_type key，保留 pipeline_type key
        UPDATE knowledge_base.knowledge_documents
        SET extra_metadata = jsonb_set(
                extra_metadata,
                '{kb_type}',
                extra_metadata -> 'pipeline_type',
                true
            )
        WHERE extra_metadata ? 'pipeline_type'
          AND NOT (extra_metadata ? 'kb_type');

        -- ④ 删除旧列
        ALTER TABLE knowledge_base.service_knowledge_bases DROP COLUMN IF EXISTS pipeline_type;
    END IF;
END $$;


-- ══════════════════════════════════════════════════════════════
-- 分节：原 010_llm_wiki_compile.sql
-- ══════════════════════════════════════════════════════════════

-- 010_llm_wiki_compile.sql：LLM-Wiki 编译状态列化 + 任务 ID（存量库增量更新）
-- 方案：docs/openkb/llm-wiki-compile-async-plan.md
-- 位置：deploy/postgres/update/（非 migrations/ 初始化目录——ALTER 只对存量库执行）
-- 手工执行：docker cp 进 postgres 容器后 psql -f（/docker-entrypoint-initdb.d 只在空库执行）

ALTER TABLE knowledge_base.knowledge_documents
  ADD COLUMN IF NOT EXISTS llm_wiki_compile_status       VARCHAR(32)  NULL,  -- NULL/compiling/compiled/stale/failed
  ADD COLUMN IF NOT EXISTS llm_wiki_compile_error        TEXT         NULL,
  ADD COLUMN IF NOT EXISTS llm_wiki_compile_warnings     JSONB        NULL,  -- 编译警告列表（对齐原 openkb_compile_warnings）
  ADD COLUMN IF NOT EXISTS llm_wiki_compile_requested_at TIMESTAMP    NULL,  -- patrol 超时基准
  ADD COLUMN IF NOT EXISTS llm_wiki_compiled_at          TIMESTAMP    NULL,
  ADD COLUMN IF NOT EXISTS llm_wiki_task_id              VARCHAR(128) NULL;  -- 对齐 rag_task_id

-- patrol 高频查询索引（部分索引，只覆盖 compiling；命名与 004 建表索引统一）
CREATE INDEX IF NOT EXISTS idx_kb_doc_llm_wiki_compiling
  ON knowledge_base.knowledge_documents (tenant_id, llm_wiki_compile_status)
  WHERE llm_wiki_compile_status = 'compiling';

-- 存量回填：extra_metadata 的键 → 新列
-- ⚠️ 现 JSONB 键名是 openkb_recompile_requested_at（非 compile_requested_at），
--    patrol 判超时的基准字段就是它，必须搬
UPDATE knowledge_base.knowledge_documents
   SET llm_wiki_compile_status = extra_metadata->>'openkb_compile_status',
       llm_wiki_compile_error  = extra_metadata->>'openkb_compile_error',
       llm_wiki_compile_warnings = (extra_metadata->'openkb_compile_warnings')::jsonb,
       llm_wiki_compile_requested_at =
         NULLIF(extra_metadata->>'openkb_recompile_requested_at', '')::timestamp
 WHERE extra_metadata ? 'openkb_compile_status';

-- 按 007_comments.sql 惯例补列注释
COMMENT ON COLUMN knowledge_base.knowledge_documents.llm_wiki_compile_status
  IS 'LLM-Wiki 编译状态：NULL/compiling/compiled/stale/failed';
COMMENT ON COLUMN knowledge_base.knowledge_documents.llm_wiki_compile_error
  IS 'LLM-Wiki 编译失败原因';
COMMENT ON COLUMN knowledge_base.knowledge_documents.llm_wiki_compile_warnings
  IS 'LLM-Wiki 编译警告列表（JSONB 数组）';
COMMENT ON COLUMN knowledge_base.knowledge_documents.llm_wiki_compile_requested_at
  IS 'LLM-Wiki 编译请求时刻（patrol 超时基准，UTC）';
COMMENT ON COLUMN knowledge_base.knowledge_documents.llm_wiki_compiled_at
  IS 'LLM-Wiki 编译完成时刻';
COMMENT ON COLUMN knowledge_base.knowledge_documents.llm_wiki_task_id
  IS 'OpenKB 容器编译任务 ID（对齐 rag_task_id），供对账巡检轮询';


-- ══════════════════════════════════════════════════════════════
-- 分节：原 011_document_content_hash.sql
-- ══════════════════════════════════════════════════════════════

-- 011_document_content_hash.sql：知识文档源文件内容 hash（上传去重 + reparse 跳过）（存量库增量更新）
-- 方案：知识库上传内容去重校验（同 KB 内相同内容文件拒绝重复上传）
-- 位置：deploy/postgres/update/（非 migrations/ 初始化目录——ALTER 只对存量库执行）
-- 手工执行：docker cp 进 postgres 容器后 psql -f（/docker-entrypoint-initdb.d 只在空库执行）
-- 全新库无需执行：migrations/004_knowledge_base.sql 建表已含同款列与索引
-- content_hash：源文件内容 md5（hex 32），上传时持久化，用于同一知识库内内容去重，
-- 并作为 reparse 跳过「源文件未变化」的单一事实来源。md5 仅用于去重/比对，非安全用途。

ALTER TABLE knowledge_base.knowledge_documents
    ADD COLUMN IF NOT EXISTS content_hash VARCHAR(32);

-- 加速去重查询：同 KB 内按 hash 找活跃文档（未删除且非 failed）
CREATE INDEX IF NOT EXISTS idx_kb_doc_content_hash
    ON knowledge_base.knowledge_documents (tenant_id, knowledge_base_id, content_hash)
    WHERE is_deleted = 0 AND status <> 'failed';


-- ══════════════════════════════════════════════════════════════
-- 分节：原 012_search_history_snapshot.sql
-- ══════════════════════════════════════════════════════════════

-- [jonex] 检索历史增量迁移（快照列 + 追加策略，原 012/013 合并）
-- 存量库增量迁移（update/ 目录，手工执行；全部 IF EXISTS 幂等，重复执行安全）：
--   1) 快照列：点击历史直接展示历史结果，不重新检索
--      answer      完整答案原始文本（含 <think> 标记，展示时前端 parseThink/parseReferences 解析）
--      references  引用快照（JSONB，落库前剥离过期的 raw_url 预签名 URL，展示时经 resolve 端点重新富化）
--      reasoning   推理链快照（ReasoningTrace: steps/final_source/total_ms），点击历史时直接展示推理过程
--   2) 追加策略：移除 (tenant_id,user_id,query_hash,knowledge_base_id) 唯一约束
--      需求：再次搜索基于当前知识重新执行检索，新结果形成新的历史记录，不覆盖原记录。
--      同一问题可保留多条历史，超出 200 条上限由 save 链路的 trim_for_user 自动清理最早记录。
-- 全新库：004 建表已含快照三列且无同款唯一索引（追加策略），本文件仅面向存量库手工执行。
ALTER TABLE knowledge_base.knowledge_search_history
  ADD COLUMN IF NOT EXISTS answer TEXT,
  ADD COLUMN IF NOT EXISTS "references" JSONB NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS reasoning JSONB;

DROP INDEX IF EXISTS knowledge_base.uq_knowledge_search_history_query_kb;


-- ══════════════════════════════════════════════════════════════
-- 分节：原 013_llm_wiki_schema_columns.sql
-- ══════════════════════════════════════════════════════════════

-- 013_llm_wiki_schema_columns.sql：LLM-Wiki Schema 编译设置（存量库增量更新）
-- 方案：docs/llmwiki-schema-settings-execution-plan.md
-- 位置：deploy/postgres/update/（非 migrations/ 初始化目录——只对存量库手工执行）
-- 手工执行：docker cp 进 postgres 容器后 psql -f（/docker-entrypoint-initdb.d 只在空库执行）
-- 全新库无需执行：migrations/004_knowledge_base.sql 已含同款建表/列/索引，
--               migrations/007_comments.sql 已含同款注释
--
-- 内容三块：
--   ① 新表 llm_wiki_schemas（建表 + 索引——004 整合后存量库没有这张表）
--   ② knowledge_documents 版本 fencing 两列（llm_wiki_target/applied_schema_version）
--   ③ 表结构修订列（agents_md_extra / concept_types——2026-08-14 修订；
--      若 013 已执行过旧版（无建表），本脚本幂等可重复执行）

-- ── ① 新表：LLM-Wiki Schema（与 migrations/004 同款；留档模型 + partial unique index）──
CREATE TABLE IF NOT EXISTS knowledge_base.llm_wiki_schemas (
    id BIGSERIAL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    knowledge_base_id VARCHAR(128) NOT NULL,

    schema_version INTEGER NOT NULL DEFAULT 1,
    status VARCHAR(32) NOT NULL DEFAULT 'active',   -- active / archived
    sync_status VARCHAR(32) NOT NULL DEFAULT 'synced',  -- synced / apply_failed

    schema_name VARCHAR(128) NOT NULL DEFAULT 'default',
    language VARCHAR(32) NOT NULL DEFAULT 'zh-CN',
    model VARCHAR(128),

    entity_types JSONB NOT NULL DEFAULT '[]'::jsonb,
    concept_types JSONB NOT NULL DEFAULT '[]'::jsonb,  -- 概念类型词表（AGENTS.md 引导，无硬校验）
    agents_md_extra TEXT NOT NULL DEFAULT '',

    agents_md TEXT NOT NULL,
    config_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,

    edited_by VARCHAR(128),
    edited_at TIMESTAMP,
    applied_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 每个 KB 最多一条 active（部分唯一索引）；历史版本留档为 archived 不受限
CREATE UNIQUE INDEX IF NOT EXISTS uq_llm_wiki_schema_active
    ON knowledge_base.llm_wiki_schemas (tenant_id, knowledge_base_id)
    WHERE status = 'active';

-- active 查询加速（编译触发点/保存 CAS 主路径）
CREATE INDEX IF NOT EXISTS idx_llm_wiki_schema_kb
    ON knowledge_base.llm_wiki_schemas (tenant_id, knowledge_base_id, schema_version);

-- ── ② 文档表版本 fencing 两列 ──
ALTER TABLE knowledge_base.knowledge_documents
    ADD COLUMN IF NOT EXISTS llm_wiki_target_schema_version INTEGER,
    ADD COLUMN IF NOT EXISTS llm_wiki_applied_schema_version INTEGER;

-- ── ③ 表结构修订列（幂等；2026-08-14 由结构化 compile_rules/page_types 改为
--    自定义块 + 概念词表后新增）──
ALTER TABLE knowledge_base.llm_wiki_schemas
    ADD COLUMN IF NOT EXISTS agents_md_extra TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS concept_types JSONB NOT NULL DEFAULT '[]'::jsonb;

-- patrol/重编扫描加速：applied < target 的过期文档（recompile-outdated 主路径）
CREATE INDEX IF NOT EXISTS idx_kb_doc_llm_wiki_schema_outdated
    ON knowledge_base.knowledge_documents (tenant_id, knowledge_base_id)
    WHERE is_deleted = 0
      AND llm_wiki_applied_schema_version IS DISTINCT FROM llm_wiki_target_schema_version;


-- ══════════════════════════════════════════════════════════════
-- 分节：原 014_engine_catalog_shared.sql
-- ══════════════════════════════════════════════════════════════

-- [jonex] 引擎目录平台共享化增量迁移（014）——data_access_methods / parser_configs / model_providers
-- 存量库增量迁移（update/ 目录，手工执行；全部 IF EXISTS 幂等，重复执行安全）：
--   1) 多租户重复 id 去重（保留 ctid 最小的一条）
--   2) 去租户列与索引
--   3) 补齐平台级种子（id 与 migrations/006 一致，ON CONFLICT 跳过已存在）
-- 全新库无需执行：migrations/005 建表已无 tenant_id、006 种子已平台化。
-- 执行顺序：先执行本脚本，再部署新代码（新代码 INSERT 不再携带 tenant_id）。
-- 手工执行：docker exec -i jonex-postgres psql -U jonex -d jonex < deploy/postgres/update/014_engine_catalog_shared.sql

-- ── data_access_methods ──
DELETE FROM business_domain.data_access_methods a
USING business_domain.data_access_methods b
WHERE a.id = b.id AND a.ctid > b.ctid;

DROP INDEX IF EXISTS business_domain.idx_dam_tenant;
ALTER TABLE business_domain.data_access_methods DROP COLUMN IF EXISTS tenant_id;

INSERT INTO business_domain.data_access_methods (id, name, access_type, config_json, status) VALUES
    ('dam_demo_api', 'API 接入（拉取）', 'api', '{"description":"通过 REST/gRPC 接口接入数据"}'::jsonb, 'active'),
    ('dam_api_push_demo', 'API 开放（推送）', 'api_push', '{"description":"外部系统通过 OpenAPI 推送文档入库"}'::jsonb, 'active'),
    ('dam_demo_storage', '文件存储直连', 'storage', '{"description":"NAS/S3/MinIO/OSS 等"}'::jsonb, 'active'),
    ('dam_demo_file', '文件上传', 'file', '{"description":"PDF/DOCX/CSV/JSON 等"}'::jsonb, 'active'),
    ('dam_demo_mqtt', 'MQTT 接入', 'mqtt', '{"description":"物联网消息队列接入"}'::jsonb, 'inactive')
ON CONFLICT (id) DO NOTHING;

-- ── parser_configs ──
DELETE FROM business_domain.parser_configs a
USING business_domain.parser_configs b
WHERE a.id = b.id AND a.ctid > b.ctid;

DROP INDEX IF EXISTS business_domain.idx_pc_tenant;
ALTER TABLE business_domain.parser_configs DROP COLUMN IF EXISTS tenant_id;

INSERT INTO business_domain.parser_configs (id, name, parser_type, file_types, config_json, status)
VALUES
    ('video_full_pipeline', '视频解析器-VLM', 'video',
     '["MP4","AVI","MOV","MKV","FLV","WMV","WEBM","M4V","MPG","MPEG","3GP"]'::jsonb,
     '{"version":"v2.3.0","process_count":1245,"display_fields":[{"label":"关键帧提取","value":"智能模式"},{"label":"分辨率限制","value":"1080p"}]}'::jsonb, 'active'),
    ('video_mps', '视频解析器-MPS', 'video',
     '["MP4","AVI","MOV","MKV","FLV","WMV","WEBM","M4V","MPG","MPEG","3GP"]'::jsonb,
     '{"version":"v2.3.0","process_count":1245,"display_fields":[{"label":"关键帧提取","value":"智能模式"},{"label":"分辨率限制","value":"1080p"}]}'::jsonb, 'active'),
    ('audio_transcribe', '音频解析器', 'audio',
     '["MP3","WAV","FLAC","AAC","M4A","OGG","WMA","OPUS","AMR"]'::jsonb,
     '{"version":"v2.1.2","process_count":3678,"display_fields":[{"label":"转写模型","value":"通用转写模型"},{"label":"输出格式","value":"SRT"}]}'::jsonb, 'active'),
    ('image_parse', '图像解析器', 'image',
     '["JPG","JPEG","PNG","GIF","BMP","TIFF","TIF","WEBP"]'::jsonb,
     '{"version":"v1.9.5","process_count":5432,"display_fields":[{"label":"OCR 引擎","value":"内置 OCR"},{"label":"图像压缩","value":"高质量"}]}'::jsonb, 'active'),
    ('document_parse', '文档解析器', 'document',
     '["PDF","DOC","DOCX","PPT","PPTX","XLS","XLSX"]'::jsonb,
     '{"version":"v3.0.1","process_count":12890,"display_fields":[{"label":"排版保留","value":"启用"},{"label":"表格提取","value":"智能提取"}]}'::jsonb, 'active'),
    ('text_parse', '文本解析器', 'txt',
     '["TXT","MD"]'::jsonb,
     '{"version":"v3.0.1","process_count":12890,"display_fields":[{"label":"排版保留","value":"启用"},{"label":"表格提取","value":"智能提取"}]}'::jsonb, 'active'),
    ('parser_demo_web', '网页解析器', 'web',
     '["HTML","HTM","XHTML"]'::jsonb,
     '{"version":"--","process_count":0,"display_fields":[{"label":"渲染模式","value":"静态渲染"},{"label":"抓取深度","value":"--"}]}'::jsonb, 'inactive'),
    ('parser_demo_cad', 'CAD 解析器', 'cad',
     '["DWG","DXF","STEP"]'::jsonb,
     '{"version":"--","process_count":0,"display_fields":[{"label":"精度等级","value":"标准"},{"label":"图层提取","value":"全部"}]}'::jsonb, 'inactive')
ON CONFLICT (id) DO NOTHING;

-- ── model_providers ──
DELETE FROM business_domain.model_providers a
USING business_domain.model_providers b
WHERE a.id = b.id AND a.ctid > b.ctid;

DROP INDEX IF EXISTS business_domain.idx_mp_tenant;
ALTER TABLE business_domain.model_providers DROP COLUMN IF EXISTS tenant_id;

INSERT INTO business_domain.model_providers (id, name, provider_type, model_type, model_name, latency_ms, token_limit, vector_dimension, call_count, success_rate, status, config_json)
VALUES
    ('provider_demo_gpt4o', 'GPT-4o', 'llm', '对话模型', 'gpt-4o', 1200, 128000, NULL, 12458, 99, 'active', '{"vendor":"OpenAI"}'::jsonb),
    ('provider_demo_claude', 'Claude Opus 4', 'llm', '对话模型', 'claude-opus-4', 1800, 200000, NULL, 8234, 99, 'active', '{"vendor":"Anthropic"}'::jsonb),
    ('provider_demo_text2vec', 'text2vec-large', 'embedding', '向量模型', 'text2vec-large-chinese', 300, NULL, 768, 56892, 100, 'active', '{"vendor":"本地部署"}'::jsonb),
    ('provider_demo_reranker', 'bge-reranker', 'reranker', '重排序模型', 'bge-reranker-v2-m3', 500, NULL, NULL, 23456, 99, 'active', '{"vendor":"本地部署","batch_size":64}'::jsonb)
ON CONFLICT (id) DO NOTHING;


-- ══════════════════════════════════════════════════════════════
-- 分节：原 015_rbac_activation.sql
-- ══════════════════════════════════════════════════════════════

-- [jonex] RBAC 权限系统激活（015）
-- 基于已有库的更新脚本（update/ 目录，手工执行）。
-- 语义（用户决策）：**不兼容旧数据**——清空 RBAC 相关表后按全新库种子重种。
-- 即：已有库的 roles / role_permissions / user_roles / permissions / menus
-- 五张表数据会被删除重建（用户在角色/权限/菜单上的自定义数据会丢失）。
-- 执行顺序：先执行本脚本，再部署新代码。
-- 手工执行：docker exec -i jonex-postgres psql -U jonex -d jonex < deploy/postgres/update/015_rbac_activation.sql
--
-- 回滚口径：旧代码不读 scope / user_roles / menus.permission_code，清空重种
-- 对旧代码无副作用——先跑脚本不部署代码是安全的；回滚直接回滚代码即可。

-- 1) 清空 RBAC 相关表（先引用方后被引用方）
DELETE FROM platform.user_roles;
DELETE FROM platform.role_permissions;
DELETE FROM platform.roles;
DELETE FROM platform.menus;
DELETE FROM platform.permissions;

-- 2) 结构列（幂等，防老库缺列）
ALTER TABLE platform.permissions ADD COLUMN IF NOT EXISTS scope VARCHAR(16) NOT NULL DEFAULT 'tenant';
ALTER TABLE platform.menus ADD COLUMN IF NOT EXISTS permission_code VARCHAR(128);
CREATE UNIQUE INDEX IF NOT EXISTS uq_role_permissions ON platform.role_permissions(tenant_id, role_id, permission_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_user_roles ON platform.user_roles(tenant_id, user_id, role_id);

-- 3) 权限码种子（与 006 同款：14 基础业务 + 13 平台 + 10 新业务）
INSERT INTO platform.permissions (id, code, name, resource, action, description, scope) VALUES
    (1, 'tenant:read', '查看租户', 'tenant', 'read', '查看租户列表和详情', 'tenant'),
    (2, 'tenant:write', '管理租户', 'tenant', 'write', '创建、编辑、删除租户', 'tenant'),
    (3, 'user:read', '查看用户', 'user', 'read', '查看用户列表和详情', 'tenant'),
    (4, 'user:write', '管理用户', 'user', 'write', '创建、编辑、删除用户', 'tenant'),
    (5, 'role:read', '查看角色', 'role', 'read', '查看角色和权限配置', 'tenant'),
    (6, 'role:write', '管理角色', 'role', 'write', '创建、编辑、删除角色，分配权限', 'tenant'),
    (7, 'knowledge:read', '查看知识', 'knowledge', 'read', '检索和查看知识库内容', 'tenant'),
    (8, 'knowledge:write', '编辑知识', 'knowledge', 'write', '编辑、上传、维护知识文档', 'tenant'),
    (9, 'service:read', '查看服务', 'service', 'read', '查看领域服务和配置', 'tenant'),
    (10, 'service:write', '管理服务', 'service', 'write', '创建和管理领域服务、知识库、数据源', 'tenant'),
    (11, 'model:read', '查看模型', 'model', 'read', '查看模型配置和状态', 'tenant'),
    (12, 'model:write', '管理模型', 'model', 'write', '配置和管理模型适配', 'tenant'),
    (13, 'system:read', '查看系统配置', 'system', 'read', '查看系统配置项', 'tenant'),
    (14, 'system:write', '管理系统配置', 'system', 'write', '修改系统配置', 'tenant')
ON CONFLICT (code) DO NOTHING;

-- 显式 id 插入不推进序列：立即对齐（清空重种时序列可能停在旧值，防后续无 id INSERT 撞主键）
SELECT setval('platform.permissions_id_seq', (SELECT MAX(id) FROM platform.permissions), true);

INSERT INTO platform.permissions (code, name, resource, action, description, scope) VALUES
    ('platform:admin', '平台管理员', 'platform', 'admin', '平台管理员标识码（is_platform_admin 判定）', 'platform'),
    ('platform:tenant:read', '查看全部租户', 'tenant', 'read', '平台级：租户列表/详情/计数', 'platform'),
    ('platform:tenant:write', '管理租户', 'tenant', 'write', '平台级：创建/编辑/删除租户', 'platform'),
    ('platform:user:all', '跨租户查看全部用户', 'user', 'all', '平台级：/users/all', 'platform'),
    ('platform:menu:read', '查看菜单', 'menu', 'read', '平台级：全量菜单树', 'platform'),
    ('platform:menu:write', '管理菜单', 'menu', 'write', '平台级：菜单写', 'platform'),
    ('platform:application:read', '查看应用', 'application', 'read', '平台级：应用注册管理', 'platform'),
    ('platform:application:write', '管理应用', 'application', 'write', '平台级：应用写', 'platform'),
    ('platform:config:read', '查看配置', 'system_config', 'read', '平台级：系统配置', 'platform'),
    ('platform:config:write', '管理配置', 'system_config', 'write', '平台级：配置写', 'platform'),
    ('platform:audit:read', '查看审计日志', 'audit_log', 'read', '平台级：审计日志', 'platform'),
    ('platform:task:read', '查看任务', 'task_schedule', 'read', '平台级：任务调度', 'platform'),
    ('platform:task:write', '管理任务', 'task_schedule', 'write', '平台级：任务写', 'platform')
ON CONFLICT (code) DO NOTHING;

INSERT INTO platform.permissions (code, name, resource, action, description, scope) VALUES
    ('engine:read', '查看引擎目录', 'engine', 'read', '接入方式/解析器/模型目录查看', 'tenant'),
    ('engine:write', '管理引擎目录', 'engine', 'write', '接入方式/解析器/模型目录管理', 'tenant'),
    ('adapter:read', '查看适配器', 'adapter', 'read', '生态适配器查看', 'tenant'),
    ('adapter:write', '管理适配器', 'adapter', 'write', '生态适配器管理', 'tenant'),
    ('skill:read', '查看技能', 'skill', 'read', '技能查看', 'tenant'),
    ('skill:write', '管理技能', 'skill', 'write', '技能启用/停用', 'tenant'),
    ('template:read', '查看模板', 'template', 'read', '模板系统查看', 'tenant'),
    ('template:write', '管理模板', 'template', 'write', '模板系统编辑', 'tenant'),
    ('prompt:read', '查看提示词模板', 'prompt', 'read', '提示词模板查看', 'tenant'),
    ('prompt:write', '管理提示词模板', 'prompt', 'write', '提示词模板编辑', 'tenant'),
    ('mcp:service:view', '查看 MCP 服务', 'service', 'view', 'MCP 服务目录与详情查看', 'tenant'),
    ('mcp:service:manage', '管理 MCP 服务', 'service', 'manage', 'MCP 服务发布/取消发布/停用/启用', 'tenant'),
    ('mcp:key:view', '查看 MCP Key', 'key', 'view', '本租户 MCP Key 脱敏信息与授权查看', 'tenant'),
    ('mcp:key:manage', '管理 MCP Key', 'key', 'manage', '本租户 MCP Key 创建/停用/启用/撤销/重新创建/删除', 'tenant')
ON CONFLICT (code) DO NOTHING;

-- 4) 角色种子：demo 5 角色（平台管理员 is_system=1；其余 0）
--    语义（用户决策）：平台管理员 = 平台级最高权限（全部码含平台面），仅 admin 账号绑定；
--    租户管理员 = 租户内管理员（全部租户业务码、无平台码）
INSERT INTO platform.roles (id, tenant_id, name, description, is_system) VALUES
    (1, 'tenant_jonex_demo', '平台管理员', '平台级最高权限：全部权限码（含平台面跨租户码），仅 demo 租户 admin 账号绑定', 1),
    (2, 'tenant_jonex_demo', '租户管理员', '租户内管理员：全部租户业务码（不含平台面码）', 0),
    (3, 'tenant_jonex_demo', '领域服务管理员', '管理领域服务、知识库、数据源等服务相关配置，可创建和管理领域空间', 0),
    (4, 'tenant_jonex_demo', '知识编辑者', '负责知识的编辑、上传和维护，可管理知识库中的文档和数据', 0),
    (5, 'tenant_jonex_demo', '观察者', '仅可检索和查看知识，不具备编辑和管理权限，适用于只读访问场景', 0)
ON CONFLICT (id) DO NOTHING;

-- 显式 id 插入不推进序列：立即对齐
SELECT setval('platform.roles_id_seq', (SELECT MAX(id) FROM platform.roles), true);

-- 4b) 每租户预设角色播种（其他租户按 demo 模板复制，is_system=0；排除平台管理员——demo 独有）
INSERT INTO platform.roles (tenant_id, name, description, is_system)
SELECT t.id, r.name, r.description, 0
FROM platform.tenants t
CROSS JOIN (SELECT name, description FROM platform.roles
            WHERE tenant_id = 'tenant_jonex_demo' AND is_deleted = 0
              AND name IN ('租户管理员','领域服务管理员','知识编辑者','观察者')) r
WHERE t.id <> 'tenant_jonex_demo' AND t.is_deleted = 0
ON CONFLICT DO NOTHING;

-- 5) 角色-权限映射种子：demo 预设角色基础矩阵（id 1-14 码）
INSERT INTO platform.role_permissions (tenant_id, role_id, permission_id) VALUES
    ('tenant_jonex_demo', 1, 1), ('tenant_jonex_demo', 1, 2), ('tenant_jonex_demo', 1, 3),
    ('tenant_jonex_demo', 1, 4), ('tenant_jonex_demo', 1, 5), ('tenant_jonex_demo', 1, 6),
    ('tenant_jonex_demo', 1, 7), ('tenant_jonex_demo', 1, 8), ('tenant_jonex_demo', 1, 9),
    ('tenant_jonex_demo', 1, 10), ('tenant_jonex_demo', 1, 11), ('tenant_jonex_demo', 1, 12),
    ('tenant_jonex_demo', 1, 13), ('tenant_jonex_demo', 1, 14),
    ('tenant_jonex_demo', 2, 1), ('tenant_jonex_demo', 2, 2), ('tenant_jonex_demo', 2, 3),
    ('tenant_jonex_demo', 2, 4), ('tenant_jonex_demo', 2, 5), ('tenant_jonex_demo', 2, 6),
    ('tenant_jonex_demo', 2, 7), ('tenant_jonex_demo', 2, 8), ('tenant_jonex_demo', 2, 9),
    ('tenant_jonex_demo', 2, 10), ('tenant_jonex_demo', 2, 11), ('tenant_jonex_demo', 2, 12),
    ('tenant_jonex_demo', 2, 13), ('tenant_jonex_demo', 2, 14),
    ('tenant_jonex_demo', 3, 3), ('tenant_jonex_demo', 3, 5), ('tenant_jonex_demo', 3, 7),
    ('tenant_jonex_demo', 3, 9), ('tenant_jonex_demo', 3, 10),
    ('tenant_jonex_demo', 4, 7), ('tenant_jonex_demo', 4, 8), ('tenant_jonex_demo', 4, 9),
    ('tenant_jonex_demo', 5, 7), ('tenant_jonex_demo', 5, 9)
ON CONFLICT DO NOTHING;

-- 5b) demo 预设角色 × 新业务码矩阵（平台管理员/租户管理员/领域服务管理员全 rw；知识编辑者/观察者 read）
INSERT INTO platform.role_permissions (tenant_id, role_id, permission_id)
SELECT 'tenant_jonex_demo', r.id, p.id
FROM platform.roles r
JOIN platform.permissions p ON p.code IN (
    'engine:read','engine:write','adapter:read','adapter:write','skill:read','skill:write',
    'template:read','template:write','prompt:read','prompt:write','mcp:service:view','mcp:service:manage','mcp:key:view','mcp:key:manage'
)
WHERE r.tenant_id = 'tenant_jonex_demo' AND r.is_deleted = 0
  AND r.name IN ('平台管理员', '租户管理员', '领域服务管理员')
ON CONFLICT DO NOTHING;

-- 5c) demo 平台管理员补全部平台面码（平台码只给平台管理员角色）
INSERT INTO platform.role_permissions (tenant_id, role_id, permission_id)
SELECT r.tenant_id, r.id, p.id
FROM platform.roles r
JOIN platform.permissions p ON p.scope = 'platform'
WHERE r.tenant_id = 'tenant_jonex_demo' AND r.name = '平台管理员' AND r.is_deleted = 0
ON CONFLICT DO NOTHING;

-- 5d) 平台管理员 + 租户管理员补全部租户业务码（兜底幂等）
INSERT INTO platform.role_permissions (tenant_id, role_id, permission_id)
SELECT r.tenant_id, r.id, p.id
FROM platform.roles r
JOIN platform.permissions p ON p.scope = 'tenant'
WHERE r.tenant_id = 'tenant_jonex_demo' AND r.name IN ('平台管理员', '租户管理员') AND r.is_deleted = 0
ON CONFLICT DO NOTHING;

-- 5e) 每租户角色-权限映射播种（按角色 name 对齐 demo 模板；仅 scope='tenant' 码）
INSERT INTO platform.role_permissions (tenant_id, role_id, permission_id)
SELECT t.id, nr.id, rp.permission_id
FROM platform.roles nr
JOIN platform.tenants t ON nr.tenant_id = t.id AND t.is_deleted = 0
JOIN platform.roles dr ON dr.tenant_id = 'tenant_jonex_demo' AND dr.name = nr.name AND dr.is_deleted = 0
JOIN platform.role_permissions rp ON rp.tenant_id = 'tenant_jonex_demo' AND rp.role_id = dr.id
JOIN platform.permissions p ON p.id = rp.permission_id
WHERE t.id <> 'tenant_jonex_demo' AND p.scope = 'tenant'
ON CONFLICT DO NOTHING;

-- 6) 用户-角色绑定（语义：平台级仅 demo 租户的 admin 账号；其余 role='admin' 为租户内管理员）
-- 6a. 平台级：仅 demo 租户 username='admin' → 「平台管理员」
INSERT INTO platform.user_roles (tenant_id, user_id, role_id)
SELECT u.tenant_id, u.id, r.id
FROM platform.users u
JOIN platform.roles r ON r.tenant_id = u.tenant_id AND r.is_deleted = 0
WHERE u.tenant_id = 'tenant_jonex_demo' AND u.username = 'admin' AND u.is_deleted = 0
  AND r.name = '平台管理员'
ON CONFLICT DO NOTHING;

-- 6b. 租户内管理员：其余 role='admin' 用户 → 各自租户「租户管理员」（无平台码）
INSERT INTO platform.user_roles (tenant_id, user_id, role_id)
SELECT u.tenant_id, u.id, r.id
FROM platform.users u
JOIN platform.roles r ON r.tenant_id = u.tenant_id AND r.is_deleted = 0
WHERE u.role = 'admin' AND u.is_deleted = 0 AND r.name = '租户管理员'
  AND NOT (u.tenant_id = 'tenant_jonex_demo' AND u.username = 'admin')
ON CONFLICT DO NOTHING;

-- 6c. role='user' → 「观察者」
INSERT INTO platform.user_roles (tenant_id, user_id, role_id)
SELECT u.tenant_id, u.id, r.id
FROM platform.users u
JOIN platform.roles r ON r.tenant_id = u.tenant_id AND r.is_deleted = 0
WHERE u.role = 'user' AND u.is_deleted = 0 AND r.name = '观察者'
ON CONFLICT DO NOTHING;

-- 7) 菜单种子（四大板块：领域本体/数据接入/集成扩展/平台管理；path 为前端 hosted 路由；分组 permission_code=NULL 靠子项裁剪）
INSERT INTO platform.menus (id, parent_id, name, path, icon, app_id, sort_order, permission_code) VALUES
    -- 领域本体分组（直接项，无中间组）
    (1, 0, 'navigation.coreBusiness', NULL, 'HomeOutlined', NULL, 1, NULL),
    (2, 1, 'navigation.knowledgeSearch', '/apps/core-business/knowledge-search', 'SearchOutlined', NULL, 1, 'knowledge:read'),
    (3, 1, 'navigation.domainKnowledge', '/apps/core-business/domain-knowledge', 'DatabaseOutlined', NULL, 2, 'knowledge:read'),
    (4, 1, 'navigation.domainManagement', '/apps/core-business/domain-management', 'ClusterOutlined', NULL, 3, 'service:read'),
    (20, 1, 'navigation.templateDomains', '/apps/ecosystem-management/template-domains', 'CopyOutlined', NULL, 4, 'template:read'),
    -- 数据接入分组 → 数据源管理、解析器管理（直接项）
    (21, 0, 'navigation.dataAccessGroup', NULL, 'CloudServerOutlined', NULL, 2, NULL),
    (7, 21, 'navigation.dataAccess', '/apps/platform-management/data-access', 'CloudServerOutlined', NULL, 1, 'engine:read'),
    (8, 21, 'navigation.parserManagement', '/apps/platform-management/parser-management', 'CodeOutlined', NULL, 2, 'engine:read'),
    -- 集成扩展分组 → Mcp生态（直接项；适配器目录已注释隐藏）
    (22, 0, 'navigation.integrationExtension', NULL, 'GlobalOutlined', NULL, 3, NULL),
    -- (18, 22, 'navigation.adapterList', '/apps/ecosystem-management/adapter-management', 'BlockOutlined', NULL, 1, 'adapter:read'), -- 适配器目录：已注释隐藏，恢复时放开本行
    (19, 22, 'navigation.mcpServiceDirectory', '/apps/ecosystem-management/mcp-service-directory', 'ClusterOutlined', NULL, 2, 'mcp:service:view'),
    -- 平台管理分组 → 账号与权限组（可折叠）
    (5, 0, 'navigation.platformManagement', NULL, 'SettingOutlined', NULL, 4, NULL),
    (25, 5, 'navigation.accountPermission', NULL, 'TeamOutlined', NULL, 1, NULL),
    (11, 25, 'navigation.tenantManagement', '/apps/platform-management/tenant-management', 'TeamOutlined', NULL, 1, 'platform:tenant:read'),
    (12, 25, 'navigation.userManagement', '/apps/platform-management/user-management', 'UserOutlined', NULL, 2, 'user:read'),
    (13, 25, 'navigation.rolePermission', '/apps/platform-management/role-permission', 'SafetyOutlined', NULL, 3, 'role:read'),
    -- 平台管理分组 → 提示词与模板（直接项）
    (9, 5, 'navigation.promptTemplates', '/apps/ecosystem-management/prompt-templates', 'FileTextOutlined', NULL, 2, 'prompt:read'),
    -- 平台管理分组 → 系统运维组（可折叠）
    (26, 5, 'navigation.systemOperations', NULL, 'SettingOutlined', NULL, 3, NULL),
    (14, 26, 'navigation.systemConfig', '/apps/platform-management/system-config', 'SettingOutlined', NULL, 1, 'platform:config:read'),
    (15, 26, 'navigation.operationLog', '/apps/platform-management/operation-log', 'FileTextOutlined', NULL, 2, 'platform:audit:read')
ON CONFLICT DO NOTHING;

-- 8) 序列兜底
SELECT setval('platform.permissions_id_seq', (SELECT MAX(id) FROM platform.permissions), true);
SELECT setval('platform.roles_id_seq', (SELECT MAX(id) FROM platform.roles), true);
SELECT setval('platform.menus_id_seq', (SELECT MAX(id) FROM platform.menus), true);


-- ══════════════════════════════════════════════════════════════
-- 分节：原 016_mcp_key_lifecycle.sql
-- ══════════════════════════════════════════════════════════════

-- 016_mcp_key_lifecycle.sql：MCP Key 生命周期相关表/列（存量库增量更新）
-- 位置：deploy/postgres/update/（非 migrations/ 初始化目录——只对存量库执行）
-- 全新库无需执行：migrations/002_platform.sql 已含同款表/列/索引。
-- 内容（全部 IF [NOT] EXISTS 幂等，重复执行安全）：
--   ① mcp_keys 生命周期列：is_deleted / expires_at / note / disabled_at / space_id
--   ② mcp_key_service_mappings 中间映射表（含 permission_level）
--   ③ 下线组织维度：DROP org_id 列 + mcp_organizations 表

-- ── ① mcp_keys 生命周期列 ──
ALTER TABLE platform.mcp_keys
    ADD COLUMN IF NOT EXISTS is_deleted INT NOT NULL DEFAULT 0;

ALTER TABLE platform.mcp_keys
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

ALTER TABLE platform.mcp_keys
    ADD COLUMN IF NOT EXISTS note        VARCHAR(512),
    ADD COLUMN IF NOT EXISTS disabled_at TIMESTAMPTZ;

-- MCP 空间隔离：space_id + 加速索引
ALTER TABLE platform.mcp_keys
    ADD COLUMN IF NOT EXISTS space_id VARCHAR(64);
CREATE INDEX IF NOT EXISTS idx_mcp_keys_space
    ON platform.mcp_keys (space_id);

-- MCP Key 撤销审计归属：revoked_by（统一 Key 审计归属列）
ALTER TABLE platform.mcp_keys
    ADD COLUMN IF NOT EXISTS revoked_by VARCHAR(128);

-- ── ② mcp_key_service_mappings（极早期数据库可能整表缺失）──
CREATE TABLE IF NOT EXISTS platform.mcp_key_service_mappings (
    mcp_key_id       VARCHAR(64) NOT NULL,
    service_id       VARCHAR(64) NOT NULL,
    permission_level VARCHAR(16) NOT NULL DEFAULT 'call',
    PRIMARY KEY (mcp_key_id, service_id)
);
CREATE INDEX IF NOT EXISTS idx_mk_sv_mapping_key
    ON platform.mcp_key_service_mappings (mcp_key_id);
CREATE INDEX IF NOT EXISTS idx_mk_sv_mapping_service
    ON platform.mcp_key_service_mappings (service_id);

-- 极早期库若无 permission_level 列则补齐
ALTER TABLE platform.mcp_key_service_mappings
    ADD COLUMN IF NOT EXISTS permission_level VARCHAR(16) NOT NULL DEFAULT 'call';

-- ── ③ 下线组织维度 ──
ALTER TABLE platform.mcp_keys DROP COLUMN IF EXISTS org_id;
DROP TABLE IF EXISTS platform.mcp_organizations;


-- ══════════════════════════════════════════════════════════════
-- 分节：原 017_mcp_service_publish.sql
-- ══════════════════════════════════════════════════════════════

-- 017_mcp_service_publish.sql：MCP 服务发布状态 + 领域服务 API Key（存量库增量更新）
-- 位置：deploy/postgres/update/（非 migrations/ 初始化目录——只对存量库执行）
-- 全新库无需执行：migrations/002_platform.sql 已含同款表/列/索引。
-- 内容（全部 IF [NOT] EXISTS 幂等，重复执行安全）：
--   ① knowledge_base.services.enabled（服务启用开关）
--   ② platform.mcp_service_publish（MCP 服务发布状态表，含 tool/tool_description/service_type）
--   ③ platform.mcp_service_api_keys（领域服务 API Key 表）

-- ── ① 服务启用开关 ──
ALTER TABLE knowledge_base.services
    ADD COLUMN IF NOT EXISTS enabled SMALLINT NOT NULL DEFAULT 1;

-- ── ② MCP 服务发布状态表 ──
CREATE TABLE IF NOT EXISTS platform.mcp_service_publish (
    id              VARCHAR(64)  PRIMARY KEY,
    tenant_id       VARCHAR(64)  NOT NULL,
    service_id      VARCHAR(64)  NOT NULL,
    is_published    INT          NOT NULL DEFAULT 0,
    published_at    TIMESTAMPTZ,
    published_by    VARCHAR(64),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ,
    UNIQUE (tenant_id, service_id)
);
CREATE INDEX IF NOT EXISTS idx_mcp_svc_pub_tenant
    ON platform.mcp_service_publish (tenant_id);
CREATE INDEX IF NOT EXISTS idx_mcp_svc_pub_service
    ON platform.mcp_service_publish (service_id);
CREATE INDEX IF NOT EXISTS idx_mcp_svc_pub_tenant_published
    ON platform.mcp_service_publish (tenant_id)
    WHERE is_published = 1;

-- 服务目录展示：tool 名 / 描述 / 服务类型
ALTER TABLE platform.mcp_service_publish
    ADD COLUMN IF NOT EXISTS tool             VARCHAR(128),
    ADD COLUMN IF NOT EXISTS tool_description TEXT,
    ADD COLUMN IF NOT EXISTS service_type     VARCHAR(32) NOT NULL DEFAULT 'domain';
CREATE INDEX IF NOT EXISTS idx_mcp_svc_pub_tool
    ON platform.mcp_service_publish (tenant_id, tool)
    WHERE tool IS NOT NULL;

-- ── ③ 领域服务 API Key 表 ──
CREATE TABLE IF NOT EXISTS platform.mcp_service_api_keys (
    id          VARCHAR(64) PRIMARY KEY,
    tenant_id   VARCHAR(64) NOT NULL,
    service_id  VARCHAR(64) NOT NULL,
    name        VARCHAR(255) NOT NULL DEFAULT '',
    key_prefix  VARCHAR(32)  NOT NULL DEFAULT '',
    key_hash    VARCHAR(64)  NOT NULL,
    expires_at  TIMESTAMPTZ,
    revoked_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_deleted  INT NOT NULL DEFAULT 0,
    UNIQUE(key_hash)
);
CREATE INDEX IF NOT EXISTS idx_mcp_svc_api_keys_tenant
    ON platform.mcp_service_api_keys (tenant_id);
CREATE INDEX IF NOT EXISTS idx_mcp_svc_api_keys_service
    ON platform.mcp_service_api_keys (tenant_id, service_id);


-- ══════════════════════════════════════════════════════════════
-- 分节：原 018_permission_convergence.sql
-- ══════════════════════════════════════════════════════════════

-- 018_permission_convergence.sql：MCP 服务级权限值收敛（存量数据迁移，幂等）
-- 位置：deploy/postgres/update/（非 migrations/ 初始化目录——只对存量库执行）
-- 统一 Key 后 mcp_keys.permissions 列已废弃（021b DROP），本段仅保留
-- service 级 mcp_key_service_mappings.permission_level 收敛（write/* → call）。

-- service 级 permission_level：write/* 迁为 call
UPDATE platform.mcp_key_service_mappings
   SET permission_level = 'call'
 WHERE permission_level IN ('write', '*');


-- ══════════════════════════════════════════════════════════════
-- 分节：原 019_kb_documents_schema_drift.sql
-- ══════════════════════════════════════════════════════════════

-- 019_kb_documents_schema_drift.sql：文档表 schema 漂移补齐（存量库增量更新）
-- 位置：deploy/postgres/update/（非 migrations/ 初始化目录——只对存量库执行）
-- 全新库无需执行：migrations/004_knowledge_base.sql 建表已含同款列/索引。
-- 背景：早期存量库缺这些列，list_documents 全列 SELECT 会抛 UndefinedColumnError；
--   幂等补齐。llm_wiki_target/applied_schema_version 两列同款见 update/013。
-- 内容：本体版本记账 / reparse 代次 / 文件夹 / 数据源 列 + 加速索引

ALTER TABLE knowledge_base.knowledge_documents
    ADD COLUMN IF NOT EXISTS content_generation              INTEGER     NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS ontology_target_schema_version  INTEGER,
    ADD COLUMN IF NOT EXISTS ontology_applied_schema_version INTEGER,
    ADD COLUMN IF NOT EXISTS ontology_applied_schema_hash    VARCHAR(32),
    ADD COLUMN IF NOT EXISTS folder_id                       VARCHAR(64),
    ADD COLUMN IF NOT EXISTS data_source_type                VARCHAR(32);

-- 文件夹查询（列已于 013/004 建表时创建，此处兜底）
CREATE INDEX IF NOT EXISTS idx_kb_doc_folder
    ON knowledge_base.knowledge_documents (tenant_id, knowledge_base_id, folder_id)
    WHERE is_deleted = 0 AND folder_id IS NOT NULL;
-- 按 KB + 来源方式分组统计
CREATE INDEX IF NOT EXISTS idx_kb_doc_tenant_kb_type
    ON knowledge_base.knowledge_documents (tenant_id, knowledge_base_id, data_source_type)
    WHERE is_deleted = 0;
-- only_outdated 扫描加速
CREATE INDEX IF NOT EXISTS idx_kb_doc_ontology_outdated
    ON knowledge_base.knowledge_documents (tenant_id, knowledge_base_id, ontology_status, ontology_applied_schema_version)
    WHERE is_deleted = 0;


