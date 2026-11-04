-- ============================================================
-- 调用监控：MCP 工具名列（存量库增量 DDL，幂等可重跑）
-- 版本: 079
-- 对应全量 DDL: deploy/postgres/migrations/002_platform.sql
--
-- protocol='mcp' 时由 Sidecar 写入工具名，其余链路恒 NULL。
-- 读侧「对外对象标识」取 COALESCE(mcp_tool, target)：保留 target 的能力标识语义，
-- 同时让开放能力列表按工具名聚合。历史记录不回填。
--
-- 发布顺序：本迁移必须先于转发层 / 采集器发布会话执行；旧库缺列会导致
-- 采集器整批 INSERT 失败并被逐行丢弃。
-- ============================================================

ALTER TABLE monitor.call_log
    ADD COLUMN IF NOT EXISTS mcp_tool VARCHAR(128) NULL;

COMMENT ON COLUMN monitor.call_log.mcp_tool IS
    'MCP 工具名（仅 protocol=mcp 时有值）；读侧对象标识 = COALESCE(mcp_tool, target)';
