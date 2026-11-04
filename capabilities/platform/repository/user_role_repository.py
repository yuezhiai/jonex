from typing import Sequence

from sqlalchemy import select, delete, and_

from jonex_core.common.tenant import require_tenant

from capabilities.platform.models.role import Role
from capabilities.platform.models.user import User
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

    async def get_active_role_ids(self, tenant_id: str, user_id: int) -> Sequence[int]:
        """用户的**未软删**角色 id（展示/编辑场景使用）。

        角色删除只置 ``roles.is_deleted=1``，``user_roles`` 绑定保留。不过滤软删时，
        用户编辑弹窗会拿到一个角色列表里不存在的 id：界面上显示成裸数字，保存时
        又因 ``set_roles`` 校验「角色不存在」而整次失败。
        """
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(
            select(UserRole.role_id)
            .join(
                Role,
                and_(
                    Role.id == UserRole.role_id,
                    Role.tenant_id == UserRole.tenant_id,
                    Role.is_deleted == 0,
                ),
            )
            .where(
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

    async def get_active_users_for_role(self, tenant_id: str, role_id: int) -> Sequence[int]:
        """角色下的**未软删**用户 id（供「分配用户」弹窗等展示场景使用）。

        与 ``get_users_for_role`` 的差别只在 join ``platform.users`` 并加
        ``is_deleted = 0``。软删用户**必须保留** ``user_roles`` 绑定——身份同步的
        软删支持原地复活，复活后角色/数据要随时间恢复，所以不能靠删绑定来「隐藏」，
        只能在查询侧过滤。

        ⚠️ 权限缓存失效（``RoleService.delete`` / ``set_permissions``）仍要用不过滤的
        ``get_users_for_role``：软删行也要清缓存，否则复活后可能读到旧权限。
        """
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(
            select(UserRole.user_id)
            .join(
                User,
                and_(
                    User.id == UserRole.user_id,
                    User.tenant_id == UserRole.tenant_id,
                ),
            )
            .where(
                UserRole.tenant_id == tenant_id,
                UserRole.role_id == role_id,
                User.is_deleted == 0,
            )
        )
        return result.scalars().all()

    async def get_deleted_users_for_role(self, tenant_id: str, role_id: int) -> Sequence[int]:
        """角色下**已被软删**的用户 id。

        用于保存时保留这些用户的绑定：分配用户弹窗只展示活跃用户，且保存是
        delete-then-insert 全量替换；若不做保留，「打开弹窗→直接保存」会把软删
        用户的绑定物理删除，破坏「复活后角色恢复」的既有语义。
        """
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(
            select(UserRole.user_id)
            .join(
                User,
                and_(
                    User.id == UserRole.user_id,
                    User.tenant_id == UserRole.tenant_id,
                ),
            )
            .where(
                UserRole.tenant_id == tenant_id,
                UserRole.role_id == role_id,
                User.is_deleted != 0,
            )
        )
        return result.scalars().all()

    async def set_users_for_role(
        self, tenant_id: str, role_id: int, user_ids: list[int]
    ) -> set[int]:
        """全量替换角色的**活跃用户**集合（delete-then-insert）；返回被移出的 user_id 集合。

        ⚠️ 「全量」只针对活跃用户：``user_ids`` 是管理端可见名单，而软删用户的绑定
        必须保留（身份同步支持原地复活，复活后角色要随时间恢复）。因此写回时把
        ``get_deleted_users_for_role`` 拿到的软删用户一并插回——否则「打开分配弹窗
        直接保存」就会把他们的绑定物理删除。

        [jonex] 权限重构 B1（N5①）：返回值是给服务层失效缓存用的。
        原实现返回 None，调用侧只能失效新名单，被移出的人在 Redis 里最多再持有
        已撤销的权限 30s（PERM_CACHE_TTL）。旧名单必须在 DELETE **之前**查。
        """
        tenant_id = require_tenant(tenant_id)
        previous = set(
            (
                await self.session.execute(
                    select(UserRole.user_id).where(
                        UserRole.tenant_id == tenant_id,
                        UserRole.role_id == role_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        # 软删用户不在入参里（弹窗看不到），必须原样保留其绑定。
        preserved = set(await self.get_deleted_users_for_role(tenant_id, role_id))
        await self.session.execute(
            delete(UserRole).where(UserRole.tenant_id == tenant_id, UserRole.role_id == role_id)
        )
        for uid in set(user_ids) | preserved:
            self.session.add(UserRole(tenant_id=tenant_id, role_id=role_id, user_id=uid))
        await self.session.flush()
        return previous - set(user_ids)