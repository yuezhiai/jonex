from typing import Optional, Sequence

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from capabilities.platform.models.user import User
from capabilities.platform.repository.base import BaseRepository
from jonex_core.common.tenant import require_tenant


class UserRepository(BaseRepository[User]):
    model = User

    async def get_by_username(self, tenant_id: str, username: str) -> Optional[User]:
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(
            select(User).where(
                User.tenant_id == tenant_id,
                User.username == username,
                User.is_deleted == 0,
            )
        )
        return result.scalar_one_or_none()

    async def list_by_tenant(
        self, tenant_id: str, offset: int = 0, limit: int = 20
    ) -> Sequence[User]:
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(
            select(User)
            .where(User.tenant_id == tenant_id, User.is_deleted == 0)
            .order_by(User.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return result.scalars().all()

    async def count_by_tenant(self, tenant_id: str) -> int:
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(
            select(func.count()).select_from(User).where(
                User.tenant_id == tenant_id, User.is_deleted == 0
            )
        )
        return result.scalar() or 0
