"""知识写入 Key 仓储（v1.4 Phase 17 WRITE-01）。"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from capabilities.platform.models.mcp_write_key import McpWriteKey
from capabilities.platform.repository.base import BaseRepository
from jonex_core.common.tenant import require_tenant


class McpWriteKeyRepository(BaseRepository[McpWriteKey]):
    """知识写入 Key 数据访问。

    create / get_by_id / get_required / update / delete_soft 继承 BaseRepository。
    """

    model = McpWriteKey

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def list_by_tenant(self, tenant_id: str) -> list[McpWriteKey]:
        """按租户查询全部写 Key（过滤 is_deleted），按创建时间降序。

        不用 list_all 默认 limit=20（会静默截断），无 limit/offset。
        含已撤销/停用 key。
        """
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(
            select(McpWriteKey)
            .where(
                McpWriteKey.tenant_id == tenant_id,
                McpWriteKey.is_deleted == 0,
            )
            .order_by(McpWriteKey.created_at.desc())
        )
        return list(result.scalars().all())

    async def revoke(
        self, key_id: str, tenant_id: str, revoked_by: str | None = None
    ) -> bool:
        """设置 revoked_at + revoked_by（审计归属）（Python datetime，禁 func.now()）。

        返回 True 表示更新成功，False 表示 Key 不存在或不属于该租户。
        幂等——对已撤销 Key 再次调用，SET 同值无副作用，rowcount 仍 > 0。
        """
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(
            update(McpWriteKey)
            .where(
                McpWriteKey.id == key_id,
                McpWriteKey.tenant_id == tenant_id,
            )
            .values(
                revoked_at=datetime.now(timezone.utc),
                revoked_by=revoked_by,
            )
        )
        await self.session.flush()
        return result.rowcount > 0

    async def get_by_id_include_deleted(
        self, key_id: str, tenant_id: str
    ) -> Optional[McpWriteKey]:
        """按 id + tenant_id 查询，不过滤 is_deleted（供 revoke 存在性检查）。"""
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(
            select(McpWriteKey).where(
                McpWriteKey.id == key_id,
                McpWriteKey.tenant_id == tenant_id,
            )
        )
        return result.scalar_one_or_none()
