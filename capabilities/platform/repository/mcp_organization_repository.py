"""
MCP 组织管理 Repository。
"""
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from capabilities.platform.models.mcp_key import McpOrganization
from capabilities.platform.repository.base import BaseRepository
from jonex_core.common.tenant import require_tenant


class McpOrganizationRepository(BaseRepository[McpOrganization]):
    """MCP 组织仓储——继承 BaseRepository 的 get_by_id / update / delete_soft 能力。

    新增方法：
    - list_by_tenant: 全量返回租户下所有组织，按创建时间降序
    - get_by_id_include_deleted: 查询组织（含已软删除），用于 delete 幂等检查
    """

    model = McpOrganization

    async def list_by_tenant(self, tenant_id: str) -> list[McpOrganization]:
        """全量返回租户下所有组织（不过滤 is_deleted），按创建时间降序。

        每租户组织数至多几十，不做分页。
        """
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(
            select(McpOrganization)
            .where(McpOrganization.tenant_id == tenant_id)
            .order_by(McpOrganization.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_by_id_include_deleted(
        self, org_id: str, tenant_id: str
    ) -> Optional[McpOrganization]:
        """查询组织（含已软删除记录），用于 delete 幂等检查。

        不过滤 is_deleted —— 用于 delete() 区分"从未存在"和"已删除"两种场景。
        """
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(
            select(McpOrganization).where(
                McpOrganization.id == org_id,
                McpOrganization.tenant_id == tenant_id,
            )
        )
        return result.scalar_one_or_none()
