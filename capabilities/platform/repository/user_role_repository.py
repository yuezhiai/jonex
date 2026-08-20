from typing import Sequence

from sqlalchemy import select, delete

from jonex_core.common.tenant import require_tenant

from capabilities.platform.models.user_role import UserRole
from capabilities.platform.repository.base import BaseRepository


class UserRoleRepository(BaseRepository[UserRole]):
    model = UserRole

    async def get_role_ids(self, tenant_id: str, user_id: int) -> Sequence[int]:
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(
            select(UserRole.role_id).where(
                UserRole.tenant_id == tenant_id,
                UserRole.user_id == user_id,
            )
        )
        return result.scalars().all()

    async def set_roles(self, tenant_id: str, user_id: int, role_ids: list[int]) -> None:
        tenant_id = require_tenant(tenant_id)
        await self.session.execute(
            delete(UserRole).where(
                UserRole.tenant_id == tenant_id,
                UserRole.user_id == user_id,
            )
        )
        for rid in role_ids:
            self.session.add(UserRole(tenant_id=tenant_id, user_id=user_id, role_id=rid))
        await self.session.flush()

    async def get_users_for_role(self, tenant_id: str, role_id: int) -> Sequence[int]:
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(
            select(UserRole.user_id).where(
                UserRole.tenant_id == tenant_id,
                UserRole.role_id == role_id,
            )
        )
        return result.scalars().all()

    async def set_users_for_role(self, tenant_id: str, role_id: int, user_ids: list[int]) -> None:
        tenant_id = require_tenant(tenant_id)
        await self.session.execute(
            delete(UserRole).where(UserRole.tenant_id == tenant_id, UserRole.role_id == role_id)
        )
        for uid in user_ids:
            self.session.add(UserRole(tenant_id=tenant_id, role_id=role_id, user_id=uid))
        await self.session.flush()