-- ============================================================
-- Jonex 平台数据库初始化 - 独立数据库 + Schema 创建
-- 版本: 001（initdb.d 按字母序最先执行，仅新数据卷首次初始化生效）
-- ============================================================

-- ------------------------------------------------------------
-- 2. 业务库 Schema 创建
-- ------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS platform;
CREATE SCHEMA IF NOT EXISTS knowledge_base;
CREATE SCHEMA IF NOT EXISTS business_domain;
CREATE SCHEMA IF NOT EXISTS metering;
CREATE SCHEMA IF NOT EXISTS task_center;
CREATE SCHEMA IF NOT EXISTS monitor;
