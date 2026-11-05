-- =============================================================================
-- 039 · 数据源改为「平台级连接池 + 知识库引用」
--
-- 背景：上一版把「建数据源」做成了**在某个知识库下创建**。实际要的是两层：
--   ① 平台级连接池 —— 数据库连接是可复用资源，创建时不绑知识库；
--   ② 知识库引用 —— 知识库设置页从已建好的连接里选一个。
--
-- 凭证**只存在连接池这一张表**（config_json.password_ref）。这是刻意的：
--   同一份连接被 N 个知识库引用，密码改了就该全部生效；
--   每个引用再存一份副本的话，改密码要改 N 处，漏一个就是一个
--   「昨天还好好的」的故障。
--
-- knowledge_data_sources.connection_id：
--   非空 = 这个知识库引用的是池子里的连接，配置从那边读；
--   NULL = 存量内联数据源（config_json 自带完整配置），**本次不迁移**——
--   迁移会动到已经生成好的 schema 归属，风险大于收益。
--
-- 对齐 `_db_catalog_source` 的两分支读路径（data_source_service.py）。
-- 幂等：全部 IF [NOT] EXISTS，apply.sh 可重复跑。
-- =============================================================================

CREATE TABLE IF NOT EXISTS knowledge_base.data_source_connections (
    id          VARCHAR(64)  PRIMARY KEY,
    tenant_id   VARCHAR(64)  NOT NULL,
    name        VARCHAR(255) NOT NULL,
    access_type VARCHAR(32)  NOT NULL,                            -- 目前只有 'database'
    config_json JSONB        NOT NULL DEFAULT '{}'::jsonb,        -- engine/host/port/database/user/password_ref/schema
    status      VARCHAR(32)  NOT NULL DEFAULT 'active',           -- active / paused
    is_deleted  SMALLINT     NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_dsc_tenant
    ON knowledge_base.data_source_connections (tenant_id, is_deleted);

ALTER TABLE knowledge_base.knowledge_data_sources
    ADD COLUMN IF NOT EXISTS connection_id VARCHAR(64);

COMMENT ON COLUMN knowledge_base.knowledge_data_sources.connection_id IS
    '指向 data_source_connections；非空 = 这个知识库引用的是池子里的连接，config 从那边读';

-- 删连接前要按 connection_id 反查引用它的知识库（见 delete_connection），
-- 这是个按连接聚合的小查询，给个部分索引
CREATE INDEX IF NOT EXISTS idx_kds_connection
    ON knowledge_base.knowledge_data_sources (tenant_id, connection_id)
    WHERE connection_id IS NOT NULL;
