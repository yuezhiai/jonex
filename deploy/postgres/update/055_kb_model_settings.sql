-- ============================================================
-- 055_kb_model_settings.sql
-- 知识库模型配置（KB 级）：解析 / 编译阶段按 KB 覆盖模型名
--
-- 设计：docs/knowledge-base/kb-model-settings-design.md
-- 范围：parse_multimodal / compile_ontology / compile_wiki 三个 scope。
--       - 向量化（embedding）不支持 KB 级：LightRAG 的 embedding 在容器内
--         进程级定形，API 无 embedding 入参，故不落行、接口层拒绝写入。
--       - ASR 本期不做（不经网关、无计量，无法验收）。
--       - openkb 的 Wiki 编译不做（模型名需 litellm 前缀，且已有 config.model 通道）。
--
-- 语义：只存覆盖值 —— 没有行 = 继承引擎默认；清空即**硬删**（无 is_deleted），
--       与 platform.tenant_security_configs 的硬删语义一致。
--       不播种平台默认值：平台层写不进去，且会造出双事实源。
--
-- 幂等：CREATE TABLE/INDEX IF NOT EXISTS。重复执行是空操作。
--
-- 说明：只放 update/（沿用 042/045 的先例），未同步
--       migrations/004（schema）与 007（comments）；如需全量 DDL 同步另补。
-- ============================================================

BEGIN;

CREATE TABLE IF NOT EXISTS knowledge_base.knowledge_model_settings (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    knowledge_base_id VARCHAR(128) NOT NULL,
    scope VARCHAR(32) NOT NULL,
    model_name VARCHAR(255) NOT NULL,
    created_by VARCHAR(64),
    updated_by VARCHAR(64),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 同一 KB 同一 scope 只允许一行；唯一索引已含 (tenant_id, knowledge_base_id) 前缀，
-- 覆盖了按 KB 查询的场景，不再单建 kb 索引。
CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_model_settings_scope
    ON knowledge_base.knowledge_model_settings (tenant_id, knowledge_base_id, scope);

COMMENT ON TABLE knowledge_base.knowledge_model_settings IS
    '知识库级模型覆盖；没有行=继承引擎默认；清空即删行';
COMMENT ON COLUMN knowledge_base.knowledge_model_settings.scope IS
    'parse_multimodal | compile_ontology | compile_wiki';

COMMIT;
