-- 知识库存量库 schema 对齐 + 任务中心清理补偿 outbox（2026-11-17）
--
-- 合并自两份增量：
--   1) 任务中心清理补偿 outbox —— 删文档 / 删库时那次出站清任务失败，
--      task_id 必须落一张可扫的表，由 knowledge_base 的 30 秒循环补删，
--      否则孤儿任务的 running 子任务会继续吃 compile 并发名额。
--   2) 存量库缺列 —— 本地 / 早期初始化的库缺少代码已在读的列：
--      · 上传 1000：`_require_file_data_source_id` 查 connection_id 失败；
--      · 相似度回填每 10 分钟报错：knowledge_info.procedural_extract 不存在；
--      · compile 报「No compiled schema available」：ontology_compiled_schemas
--        缺 data_source_id，查询里 `data_source_id IS NULL` 直接抛异常被吞掉。
--
-- 全部 IF NOT EXISTS，可重复执行。

-- ============================================================
-- 一、任务中心清理补偿 outbox
-- ============================================================

CREATE TABLE IF NOT EXISTS knowledge_base.task_center_cleanup_outbox (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       varchar(64)  NOT NULL,
    task_id         varchar(64)  NOT NULL,
    attempts        integer      NOT NULL DEFAULT 0,
    next_attempt_at timestamptz  NOT NULL DEFAULT now(),
    last_error      text,
    created_at      timestamptz  NOT NULL DEFAULT now(),
    updated_at      timestamptz  NOT NULL DEFAULT now()
);

-- 同一个任务只排一次：重发/重复删库不会把待清理队列撑大。
CREATE UNIQUE INDEX IF NOT EXISTS uq_tc_cleanup_outbox_tenant_task
    ON knowledge_base.task_center_cleanup_outbox (tenant_id, task_id);

-- 补偿扫描的驱动索引。
CREATE INDEX IF NOT EXISTS ix_tc_cleanup_outbox_due
    ON knowledge_base.task_center_cleanup_outbox (next_attempt_at);

-- ============================================================
-- 二、knowledge_base 存量库缺列
-- ============================================================

ALTER TABLE knowledge_base.knowledge_info
    ADD COLUMN IF NOT EXISTS procedural_extract boolean,
    ADD COLUMN IF NOT EXISTS structured_enabled boolean DEFAULT false NOT NULL,
    ADD COLUMN IF NOT EXISTS extract_tool_calls boolean;

ALTER TABLE knowledge_base.knowledge_data_sources
    ADD COLUMN IF NOT EXISTS connection_id character varying(64);

CREATE INDEX IF NOT EXISTS idx_kds_connection
    ON knowledge_base.knowledge_data_sources USING btree (tenant_id, connection_id)
    WHERE (connection_id IS NOT NULL);

-- 本体 schema 的数据源维度（ORM 已读，存量表缺失；缺它时 compile 步报
-- 「No compiled schema available」——查询里 `data_source_id IS NULL` 直接炸）。
ALTER TABLE knowledge_base.ontology_compiled_schemas
    ADD COLUMN IF NOT EXISTS data_source_id character varying(64);

CREATE INDEX IF NOT EXISTS ix_ontology_compiled_schemas_data_source_id
    ON knowledge_base.ontology_compiled_schemas (data_source_id);
