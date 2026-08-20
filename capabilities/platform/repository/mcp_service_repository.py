"""
MCP 服务目录 Repository —— 跨 schema 查询聚合层。

本 Repository 跨越 platform 和 knowledge_base 两个 schema：
- platform.mcp_service_publish（ORM — 本 capability 内）
- knowledge_base.services / knowledge_base.service_knowledge_bases /
  knowledge_base.knowledge_info / knowledge_base.spaces（sa.text 原始 SQL）

⚠️ Docker 隔离：platform 容器不含 capabilities/knowledge_base/ 代码，
因此禁止 import knowledge_base ORM 模型。跨 schema 只读查询必须使用
sqlalchemy.text() 原始 SQL。
"""

import uuid as _uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select, text as sa_text
from sqlalchemy import and_
from sqlalchemy.ext.asyncio import AsyncSession

from capabilities.platform.models.mcp_key import McpKey, McpKeyServiceMapping
from capabilities.platform.models.mcp_service import McpServicePublish
from jonex_core.common.tenant import require_tenant


# 系统内置写服务条目（WRITE-03 / DIR-02 / K7-B3）——系统服务条目的唯一事实源，
# service 层 import 这些常量构造目录条目与不可编辑守卫。
SYSTEM_SERVICE_ID = "system.knowledge_document_write"
SYSTEM_SERVICE_TOOL = "knowledge_document_write"
SYSTEM_SERVICE_NAME = "知识写入"
SYSTEM_SERVICE_DESCRIPTION = "向授权知识库写入文档"


# 跨 schema 基础查询 SQL —— 列表与详情共用。
# ⚠️ platform 容器不含 knowledge_base 代码，跨 schema 只读必须使用 sa.text() 原始 SQL。
_SERVICE_BASE_SQL = """
    SELECT
        s.id,
        s.name,
        s.description,
        s.domain_type,
        s.space_id,
        s.status,
        s.enabled,
        s.created_at,
        s.updated_at,
        COALESCE(kb_counts.cnt, 0) AS kb_count,
        COALESCE(ARRAY_TO_STRING(kb_counts.kb_names, ','), '') AS kb_names_raw,
        COALESCE(ARRAY_TO_STRING(kb_counts.kb_ids, ','), '') AS kb_ids_raw,
        COALESCE(p.is_published, 0) AS is_published,
        p.published_at,
        p.published_by,
        p.tool,
        p.tool_description,
        p.service_type,
        sp.name AS space_name
    FROM knowledge_base.services s
    LEFT JOIN LATERAL (
        SELECT
            skb.service_id,
            COUNT(skb.kb_id)::int AS cnt,
            ARRAY_AGG(ki.name) AS kb_names,
            ARRAY_AGG(skb.kb_id) AS kb_ids
        FROM knowledge_base.service_knowledge_bases skb
        JOIN knowledge_base.knowledge_info ki
            ON skb.kb_id = ki.id AND ki.is_deleted = 0
        WHERE skb.service_id = s.id
          AND skb.tenant_id = :tenant_id
          AND skb.is_deleted = 0
        GROUP BY skb.service_id
    ) kb_counts ON true
    LEFT JOIN platform.mcp_service_publish p
        ON s.id = p.service_id AND p.tenant_id = :tenant_id
    LEFT JOIN knowledge_base.spaces sp
        ON s.space_id = sp.id AND sp.tenant_id = :tenant_id AND sp.is_deleted = 0
    WHERE s.tenant_id = :tenant_id
      AND s.is_deleted = 0
"""


class McpServiceRepository:
    """MCP 服务目录数据访问 —— 不继承 BaseRepository（跨 schema 查询不适合通用基类）。"""

    def __init__(self, session: AsyncSession):
        self.session = session

    # ------------------------------------------------------------------
    # 对外公共方法
    # ------------------------------------------------------------------

    async def list_services(
        self,
        tenant_id: str,
        search: Optional[str] = None,
        space_id: Optional[str] = None,
        status_filter: Optional[str] = None,
    ) -> list[dict]:
        """跨 schema 聚合查询 domain services。

        从 knowledge_base.services 读取服务列表，LEFT JOIN：
        - LATERAL 子查询聚合 KB 计数与名称（knowledge_base.service_knowledge_bases + knowledge_base.knowledge_info）
        - platform.mcp_service_publish 获取发布状态
        - knowledge_base.spaces 获取空间名称

        筛选参数：
        - search：s.name ILIKE '%search%'
        - space_id：s.space_id = space_id
        - status_filter：'published' 仅返回已发布，'unpublished' 仅返回未发布
        """
        tenant_id = require_tenant(tenant_id)

        params: dict = {"tenant_id": tenant_id}
        where_clauses: list[str] = []

        if search:
            where_clauses.append("s.name ILIKE :search")
            params["search"] = f"%{search}%"
        if space_id:
            where_clauses.append("s.space_id = :space_id")
            params["space_id"] = space_id
        if status_filter == "published":
            where_clauses.append("p.is_published = 1")
        elif status_filter == "unpublished":
            where_clauses.append("(p.is_published IS NULL OR p.is_published = 0)")

        return await self._query_services(tenant_id, where_clauses, params)

    async def _query_services(
        self,
        tenant_id: str,
        where_clauses: list[str],
        params: dict,
    ) -> list[dict]:
        """执行跨 schema 服务查询，统一解析 kb_names / kb_ids 聚合列。

        供 list_services（列表）与 get_service_detail（详情）共用。
        """
        sql = _SERVICE_BASE_SQL
        for clause in where_clauses:
            sql += f"\n              AND {clause}"
        sql += "\n            ORDER BY s.created_at DESC"

        result = await self.session.execute(sa_text(sql), params)

        output: list[dict] = []
        for row in result.all():
            row_dict = dict(row._mapping)
            # kb_names_raw / kb_ids_raw 是 ARRAY_TO_STRING 产生的逗号分隔串
            raw_names = row_dict.pop("kb_names_raw", "")
            row_dict["kb_names"] = [n for n in raw_names.split(",") if n] if raw_names else []
            raw_ids = row_dict.pop("kb_ids_raw", "")
            row_dict["kb_ids"] = [i for i in raw_ids.split(",") if i] if raw_ids else []
            output.append(row_dict)
        return output

    async def get_service_detail(
        self, tenant_id: str, service_id: str
    ) -> Optional[dict]:
        """查询单个领域服务详情（跨 schema 聚合）。

        服务不存在或不属于该租户时返回 None。
        """
        tenant_id = require_tenant(tenant_id)
        rows = await self._query_services(
            tenant_id,
            ["s.id = :service_id"],
            {"tenant_id": tenant_id, "service_id": service_id},
        )
        return rows[0] if rows else None

    async def get_publish_status(
        self, tenant_id: str, service_id: str
    ) -> Optional[McpServicePublish]:
        """查询指定 service 的发布状态记录。

        返回 McpServicePublish | None。
        """
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(
            select(McpServicePublish).where(
                McpServicePublish.tenant_id == tenant_id,
                McpServicePublish.service_id == service_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_tool(
        self, tenant_id: str, tool: str
    ) -> Optional[McpServicePublish]:
        """按 tool 名查询当前租户的发布记录（用于同租户内 Tool 名唯一性校验）。

        返回 McpServicePublish | None。
        """
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(
            select(McpServicePublish).where(
                McpServicePublish.tenant_id == tenant_id,
                McpServicePublish.tool == tool,
            )
        )
        return result.scalar_one_or_none()

    async def save_tool_config(
        self,
        tenant_id: str,
        service_id: str,
        tool: str,
        tool_description: Optional[str],
    ) -> McpServicePublish:
        """保存服务的 MCP Tool 配置。

        已有发布记录则更新 tool / tool_description；否则创建 is_published=0 桩记录。
        返回更新后的 McpServicePublish。
        """
        tenant_id = require_tenant(tenant_id)
        existing = await self.get_publish_status(tenant_id, service_id)

        if existing:
            existing.tool = tool
            existing.tool_description = tool_description
            await self.session.flush()
            return existing

        record = McpServicePublish(
            id=_uuid.uuid4().hex,
            tenant_id=tenant_id,
            service_id=service_id,
            is_published=0,
            tool=tool,
            tool_description=tool_description,
            created_at=datetime.now(timezone.utc),
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def upsert_publish(
        self,
        tenant_id: str,
        service_id: str,
        published_by: str,
        is_published: int = 1,
    ) -> McpServicePublish:
        """插入或更新发布记录。

        返回更新后的 McpServicePublish 记录。
        """
        tenant_id = require_tenant(tenant_id)
        existing = await self.get_publish_status(tenant_id, service_id)

        if existing:
            existing.is_published = is_published
            existing.published_at = datetime.now(timezone.utc) if is_published else None
            existing.published_by = published_by
            # updated_at is auto-managed by onupdate=func.now() on the column
            await self.session.flush()
            return existing

        record = McpServicePublish(
            id=_uuid.uuid4().hex,
            tenant_id=tenant_id,
            service_id=service_id,
            is_published=is_published,
            published_at=datetime.now(timezone.utc) if is_published else None,
            published_by=published_by,
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def ensure_publish_stubs(
        self, tenant_id: str, service_ids: list[str]
    ) -> int:
        """批量兜底 upsert 未发布服务桩记录（DS-05 / K10）。

        对 knowledge_base 存在但 platform.mcp_service_publish 无记录的服务，
        写 is_published=0 桩记录（published_at/published_by 均为空）。
        一次 select 预查缺桩集合，避免逐条 get_publish_status 的 N+1；
        复用 UNIQUE(tenant_id, service_id) 兜底幂等。

        返回本次新增的桩记录数量。
        """
        tenant_id = require_tenant(tenant_id)
        if not service_ids:
            return 0

        result = await self.session.execute(
            select(McpServicePublish.service_id).where(
                McpServicePublish.tenant_id == tenant_id,
                McpServicePublish.service_id.in_(service_ids),
            )
        )
        existing_ids = set(result.scalars().all())

        missing = [sid for sid in service_ids if sid not in existing_ids]
        for sid in missing:
            self.session.add(
                McpServicePublish(
                    id=_uuid.uuid4().hex,
                    tenant_id=tenant_id,
                    service_id=sid,
                    is_published=0,
                    published_at=None,
                    published_by=None,
                    created_at=datetime.now(timezone.utc),
                )
            )

        if missing:
            await self.session.flush()

        return len(missing)

    async def ensure_system_service(self, tenant_id: str) -> McpServicePublish:
        """幂等 upsert 系统服务条目。service_type='system'、tool 固定、is_published=1。

        复用 get_publish_status 的 UNIQUE(tenant_id, service_id) 语义，
        「先查后插」实现幂等，不引入 ON CONFLICT / raw SQL。
        """
        tenant_id = require_tenant(tenant_id)
        existing = await self.get_publish_status(tenant_id, SYSTEM_SERVICE_ID)
        if existing is not None:
            return existing
        record = McpServicePublish(
            id=_uuid.uuid4().hex,
            tenant_id=tenant_id,
            service_id=SYSTEM_SERVICE_ID,
            is_published=1,
            published_at=None,
            published_by=None,
            tool=SYSTEM_SERVICE_TOOL,
            tool_description=SYSTEM_SERVICE_DESCRIPTION,
            service_type="system",
            created_at=datetime.now(timezone.utc),
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def count_kbs_per_service(
        self, tenant_id: str, service_ids: list[str]
    ) -> dict[str, dict]:
        """批量查询每个 service 的 KB 数量和名称列表。

        跨 schema，使用 sa.text() 原始 SQL。
        返回 {service_id: {"cnt": int, "kb_names": [str]}}。
        """
        tenant_id = require_tenant(tenant_id)
        if not service_ids:
            return {}

        result = await self.session.execute(
            sa_text("""
                SELECT
                    skb.service_id,
                    COUNT(skb.kb_id)::int AS cnt,
                    ARRAY_AGG(ki.name) AS kb_names
                FROM knowledge_base.service_knowledge_bases skb
                JOIN knowledge_base.knowledge_info ki
                    ON skb.kb_id = ki.id AND ki.is_deleted = 0
                WHERE skb.service_id = ANY(:service_ids)
                  AND skb.tenant_id = :tenant_id
                  AND skb.is_deleted = 0
                GROUP BY skb.service_id
            """),
            {"service_ids": service_ids, "tenant_id": tenant_id},
        )

        out: dict[str, dict] = {}
        for row in result.all():
            service_id_val = row.service_id
            out[service_id_val] = {
                "cnt": row.cnt,
                "kb_names": list(row.kb_names) if row.kb_names else [],
            }
        return out

    async def get_authorized_keys(
        self, tenant_id: str, service_id: str
    ) -> list[tuple]:
        """查询已授权给指定 service 的 MCP Key 列表。

        JOIN platform.mcp_key_service_mappings + platform.mcp_keys。
        返回 list of tuples (key_id, key_name, key_prefix, permission_level,
        revoked_at, expires_at)。
        """
        tenant_id = require_tenant(tenant_id)

        stmt = (
            select(
                McpKey.id.label("key_id"),
                McpKey.name.label("key_name"),
                McpKey.key_prefix,
                McpKeyServiceMapping.permission_level,
                McpKey.revoked_at,
                McpKey.expires_at,
            )
            .select_from(McpKeyServiceMapping)
            .join(
                McpKey,
                and_(
                    McpKeyServiceMapping.mcp_key_id == McpKey.id,
                    McpKey.tenant_id == tenant_id,
                    McpKey.is_deleted == 0,
                ),
            )
            .where(
                McpKeyServiceMapping.service_id == service_id,
            )
        )

        result = await self.session.execute(stmt)
        return list(result.all())

    async def get_domain_services(self, tenant_id: str) -> dict[str, str]:
        """读取 knowledge_base.services 获取当前租户所有 domain service。

        跨 schema 只读查询，使用 sa.text()。
        返回 {service_id: name} 字典。
        """
        tenant_id = require_tenant(tenant_id)

        result = await self.session.execute(
            sa_text("""
                SELECT id, name
                FROM knowledge_base.services
                WHERE tenant_id = :tenant_id AND is_deleted = 0
            """),
            {"tenant_id": tenant_id},
        )
        return {row[0]: row[1] for row in result.all()}
