-- ============================================================
-- Jonex 平台数据库初始化脚本（入口文件）
-- ============================================================
-- 用法（手动 psql）：
--   psql -U jonex -d jonex -f deploy/postgres/init.sql
--
-- Docker 自动初始化：
--   PostgreSQL 镜像自动按字母序执行 migrations/ 下所有 .sql 文件
--   本文件仅用于手动 psql 场景
-- ============================================================

\ir migrations/001_schemas.sql
\ir migrations/002_platform.sql
\ir migrations/004_knowledge_base.sql
\ir migrations/005_business_domain.sql
\ir migrations/006_seed_data.sql
\ir migrations/007_comments.sql
