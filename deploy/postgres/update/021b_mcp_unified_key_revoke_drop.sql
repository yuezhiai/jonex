-- ══════════════════════════════════════════════════════════════
-- 021b_mcp_unified_key_revoke_drop.sql：MCP 统一 Key（查询/写入合并）—— 撤销存量旧 Key + DROP 旧表/列（破坏性）
-- ══════════════════════════════════════════════════════════════
-- 位置：deploy/postgres/update/（非 migrations/ 初始化目录——只对存量库执行）
-- 全新库无需执行：migrations/002_platform.sql 同步后已无 mcp_write_keys 表、permissions 列。
-- 背景：承接 021a 加列完成之后，本文件是「撤销 + DROP」阶段（021b），破坏性、不可逆。
--
-- ⛔ 发布闸门（四个前提必须已部署，任一未满足即执行本文件会导致运行期错误）：
--   前提 A：mcp_server 已切单查 mcp_keys（不再 find_write_key_by_hash 查 mcp_write_keys），
--           否则 DROP 写表后旧写 Key 查询路径抛 UndefinedTableError → 写路径 500/503。
--   前提 B：platform 已移除 mcp_keys.permissions 列的模型/服务/DTO 读写，
--           否则 DROP 列后旧代码 SELECT permissions 抛 UndefinedColumnError → 读路径 500。
--   前提 C：撤销前已生成 mcp_keys_pre_unified_revoke 快照，且停机窗口内无新 Key 创建，
--           否则窗口外新建的统一 Key 会被一并撤销、且撤销无恢复快照。
--   前提 D：mcp_write_keys 数据已归档到 mcp_write_keys_archive，
--           否则 DROP 写表后旧写 Key 数据不可逆丢失。
--   发布顺序：021a → 前提 A（mcp_server 代码）→ 前提 B（platform 代码）
--           → 前提 C（撤销前快照）→ 前提 D（写表归档）→ 021b（先快照/归档 → 再撤销/DROP）。
--   全程一次性停机窗口，platform 与 mcp_server 锁步同步发布，不做兼容双查、不保留回滚期。
--
-- 内容（全部幂等，重复执行安全；前置快照/归档与破坏性语句同文件，先兜底后破坏）：
--   ① 存量旧查询 Key 统一撤销（旧 Key 直接废弃，用户经统一流程重建）
--   ② DROP 旧写 Key 表 mcp_write_keys（数据需先归档）
--   ③ DROP mcp_keys.permissions 列（服务级权限唯一真源改为 mcp_key_service_mappings.permission_level）
--   注：mcp_service_api_keys 表及整套栈删除推迟到 Phase 19 SVC-05，本文件有意不含其 DROP
--   （PRD §3.2 与 §3.3 / ROADMAP DATA-04 冲突，留 Phase 19 拍板）。

-- ── 前提 C 兜底：撤销前快照 ──
-- 撤销前先把「即将被撤销的 Key」快照归档，撤销可恢复（幂等，重复执行跳过）
CREATE TABLE IF NOT EXISTS platform.mcp_keys_pre_unified_revoke
    AS SELECT * FROM platform.mcp_keys WHERE revoked_at IS NULL;

-- ── ① 存量旧查询 Key 统一撤销 ──
-- 注意：WHERE revoked_at IS NULL 会一并撤销 disabled（停用）与软删除（is_deleted）的 Key，
--   与「旧 Key 直接废弃」策略一致，属有意行为而非误伤；撤销不可逆，仅保留记录可审计。
UPDATE platform.mcp_keys
    SET revoked_at = NOW(), revoked_by = 'migration:unified-key'
    WHERE revoked_at IS NULL;

-- ── 前提 D 兜底：DROP 前归档 ──
-- DROP 前先把旧写 Key 数据归档，避免不可逆丢失（幂等，重复执行跳过；
-- 全新库无 mcp_write_keys 表则跳过归档）。
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'platform' AND table_name = 'mcp_write_keys'
    ) THEN
        EXECUTE 'CREATE TABLE IF NOT EXISTS platform.mcp_write_keys_archive AS SELECT * FROM platform.mcp_write_keys';
    END IF;
END $$;

-- ── ② 废弃旧写 Key 表（数据归档后 DROP）──
DROP TABLE IF EXISTS platform.mcp_write_keys;

-- ── ③ permissions 列作废 ──
-- 服务级权限唯一真源改为 mcp_key_service_mappings.permission_level；
-- 现状创建 Key 时已把全局权限推导值物化进每条 mapping，无「无显式值」记录，直接 DROP 无需交接。
ALTER TABLE platform.mcp_keys
    DROP COLUMN IF EXISTS permissions;
