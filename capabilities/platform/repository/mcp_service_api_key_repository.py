"""领域服务 API Key 仓储（v1.4 Phase 15 DS-04）。"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from capabilities.platform.models.mcp_service_api_key import McpServiceApiKey
from capabilities.platform.repository.base import BaseRepository
from jonex_core.common.tenant import require_tenant


class McpServiceApiKeyRepository(BaseRepository[McpServiceApiKey]):
    """领域服务 API Key 数据访问。

    create 继承 BaseRepository.create（session.add + flush）。
    """

    model = McpServiceApiKey

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def list_by_service(
        self, tenant_id: str, service_id: str
    ) -> list[McpServiceApiKey]:
        """按 service 查询 API Key（过滤 is_deleted），按创建时间降序。"""
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(
            select(McpServiceApiKey)
            .where(
                McpServiceApiKey.tenant_id == tenant_id,
                McpServiceApiKey.service_id == service_id,
                McpServiceApiKey.is_deleted == 0,
            )
            .order_by(McpServiceApiKey.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_by_id_include_deleted(
        self, key_id: str, tenant_id: str
    ) -> Optional[McpServiceApiKey]:
        """按 id + tenant_id 查询，不过滤 is_deleted（供 revoke 存在性检查）。"""
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(
            select(McpServiceApiKey).where(
                McpServiceApiKey.id == key_id,
                McpServiceApiKey.tenant_id == tenant_id,
            )
        )
        return result.scalar_one_or_none()

    async def revoke(self, key_id: str, tenant_id: str, service_id: str) -> bool:
        """设置 revoked_at = 当前时间（Python datetime，禁 func.now()）。

        返回 True 表示更新成功，False 表示 Key 不存在或不属于该租户/服务。
        幂等——对已撤销 Key 再次调用，SET 同值无副作用，rowcount 仍 > 0。
        """
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(
            update(McpServiceApiKey)
            .where(
                McpServiceApiKey.id == key_id,
                McpServiceApiKey.tenant_id == tenant_id,
                McpServiceApiKey.service_id == service_id,
            )
            .values(revoked_at=datetime.now(timezone.utc))
        )
        await self.session.flush()
        return result.rowcount > 0
