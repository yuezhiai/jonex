-- ============================================================
-- MCP 服务目录：服务级「最近调用时间」列（存量库增量 DDL，幂等可重跑）
-- 版本: 077
-- 对应全量 DDL: deploy/postgres/migrations/002_platform.sql
--
-- last_call_at 由 MCP Server 在工具调用时按 UNIQUE(tenant_id, service_id)
-- UPSERT 写入（见 mcp_server/auth.py:touch_service_call）。
-- 历史回填口径：检索类调用可从 search_request_event.entry_service_id 派生
-- （MCP 检索与 API Key 检索都会写该列）；知识写入类调用没有对应事件源，不回填。
-- ============================================================

ALTER TABLE platform.mcp_service_publish
    ADD COLUMN IF NOT EXISTS last_call_at TIMESTAMPTZ;

UPDATE platform.mcp_service_publish p
   SET last_call_at = agg.max_at
  FROM (
        SELECT tenant_id,
               entry_service_id,
               MAX(received_at) AS max_at
          FROM knowledge_base.search_request_event
         WHERE entry_service_id IS NOT NULL
           AND is_deleted = 0
         GROUP BY tenant_id, entry_service_id
       ) agg
 WHERE p.tenant_id = agg.tenant_id
   AND p.service_id = agg.entry_service_id
   AND (p.last_call_at IS NULL OR p.last_call_at < agg.max_at);
