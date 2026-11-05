-- 038 · 补「数据库接入」这一种接入方式
--
-- 为什么要单独补一行：`business_domain.data_access_methods` 是接入方式的**唯一来源**，
-- 而 `engine_service.list_access_methods` 只返回 status='active' 的行——**种子里没有，
-- 这个类型在 UI 上就根本不存在**，前端的「添加数据源」下拉里选不到「数据库」。
--
-- 为什么用 update/ 而不是改 migrations/006_seed_data.sql：
-- migrations 只在**卷首次初始化**时经 /docker-entrypoint-initdb.d 执行，对已存在的卷一律跳过
-- （RUNBOOK §一.3 记过这个坑，platform.users.external_id 缺失就是这么来的）。
-- 已经跑起来的库只能靠 update/ 增量补。
--
-- access_type 用 `database`（不是 engine/db）：与后端 `_VALID_ACCESS_TYPES`
-- （data_source_service.py:43）和 037 的 `data_source_id` 粒度对齐。
--
-- 幂等：ON CONFLICT DO NOTHING，apply.sh 可重复跑。

INSERT INTO business_domain.data_access_methods (id, name, access_type, config_json, status) VALUES
    ('dam_database', '数据库接入', 'database',
     '{"description":"直连 PostgreSQL / MySQL / SQL Server 读取库表结构（只读 information_schema，不取业务数据）"}'::jsonb,
     'active')
ON CONFLICT (id) DO NOTHING;
