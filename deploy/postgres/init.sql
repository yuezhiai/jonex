-- ============================================================
-- Jonex 平台数据库初始化脚本（入口文件）
-- ============================================================
-- 用法（手动 psql，在 deploy/postgres 目录下执行）：
--   psql -U jonex -d jonex -f init.sql
--
-- Docker 自动初始化：
--   PostgreSQL 镜像自动按字母序执行 migrations/ 下所有 .sql 文件
--   本文件仅用于手动 psql 场景
--
-- 2026-11-12 起 migrations/ 按「一个库一个文件」组织：
--   001–010 库/子系统（DDL），090 种子数据（含种子后的数据修正），091 注释。
--   011–089 留给以后的库；顺序即语义，种子必须晚于全部 DDL。
-- ============================================================

\ir migrations/001_schemas.sql
\ir migrations/002_platform.sql
\ir migrations/003_knowledge_base.sql
\ir migrations/004_business_domain.sql
\ir migrations/005_task_center.sql
\ir migrations/006_metering.sql
\ir migrations/007_monitor.sql
\ir migrations/008_data_governance.sql
\ir migrations/010_ontology_compile.sql
\ir migrations/090_seed_data.sql
\ir migrations/091_comments.sql
