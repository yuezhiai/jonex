-- ══════════════════════════════════════════════════════════════
-- 021a_mcp_unified_key_add.sql：MCP 统一 Key（查询/写入合并）—— 加列 + 索引（非破坏性增量）
-- ══════════════════════════════════════════════════════════════
-- 位置：deploy/postgres/update/（非 migrations/ 初始化目录——只对存量库执行）
-- 全新库无需执行：后续并入 migrations/002_platform.sql 同款列/索引。
-- 背景：查询/写入双 Key（mcp_keys + mcp_write_keys）合并为一把统一 mcp_keys。
--   本文件是「加列 + 索引」阶段（021a），只做非破坏性增量，可先于代码发布单独落地：
--   新旧代码均忽略新增列，不产生运行期影响。
--   撤销存量旧 Key / DROP 旧写表 / DROP permissions 等破坏性动作在 021b 执行，
--   须在 mcp_server 切单查 mcp_keys、platform 移除 permissions 读写之后落地。
-- 内容（全部 IF [NOT] EXISTS 幂等，重复执行安全）：
--   ① mcp_keys 增加 write_grants（知识写入授权范围；三态 NULL/[]/非空）
--   ② mcp_keys 增加 client_request_id（创建幂等键）+ 唯一索引 uq_mcp_keys_tenant_reqid
--   ③ 写授权按租户查询索引 idx_mcp_keys_write_grants
--   ④ mcp_service_publish 增加 stopped 态列（stopped_at / stopped_by）

-- ── ① 知识写入授权范围（可空；NULL=未开启，非空=[{kb,mode,directories[]}]）──
ALTER TABLE platform.mcp_keys
    ADD COLUMN IF NOT EXISTS write_grants JSONB;

-- ── ② 创建幂等键（防重复提交产生第二把 Key；编号终身占用，撤销/软删除后也不复用）──
ALTER TABLE platform.mcp_keys
    ADD COLUMN IF NOT EXISTS client_request_id VARCHAR(64);
CREATE UNIQUE INDEX IF NOT EXISTS uq_mcp_keys_tenant_reqid
    ON platform.mcp_keys (tenant_id, client_request_id)
    WHERE client_request_id IS NOT NULL;

-- ── ③ 写授权按租户查询索引（供运行时/列表按 write_grants 非空过滤）──
CREATE INDEX IF NOT EXISTS idx_mcp_keys_write_grants
    ON platform.mcp_keys (tenant_id)
    WHERE write_grants IS NOT NULL;

-- ── ④ MCP 服务发布 stopped 态（stopped = 发布后被停用，服务本体仍在运行）──
ALTER TABLE platform.mcp_service_publish
    ADD COLUMN IF NOT EXISTS stopped_at TIMESTAMPTZ;
ALTER TABLE platform.mcp_service_publish
    ADD COLUMN IF NOT EXISTS stopped_by VARCHAR(64);
