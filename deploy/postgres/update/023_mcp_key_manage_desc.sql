-- 023_mcp_key_manage_desc.sql
-- 修正 mcp:key:manage 权限描述：补充「删除」能力（软删除接口已上线，描述文案同步）。
-- 种子数据用 ON CONFLICT (code) DO NOTHING，已存在的行不会被重插更新，故存量库需本条 UPDATE。

UPDATE platform.permissions
SET description = '本租户 MCP Key 创建/停用/启用/撤销/重新创建/删除',
    updated_at = CURRENT_TIMESTAMP
WHERE code = 'mcp:key:manage';
