"""
用户管理服务。
"""
import logging
from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.exceptions import ResourceNotFoundError, ResourceConflictError
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant
from capabilities.platform.models.user import User
from capabilities.platform.repository.role_repository import RoleRepository
from capabilities.platform.repository.user_repository import UserRepository
from capabilities.platform.repository.user_role_repository import UserRoleRepository
from capabilities.platform.services.admin_role_guard import assert_can_assign_roles
from jonex_core.security.user_auth import get_user_auth
from capabilities.platform.dtos.platform import (
    UserCreateRequest,
    UserUpdateRequest,
    UserResponse,
    UserListResponse,
)

logger = logging.getLogger(__name__)


class UserService:
    """用户管理服务"""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.repo = UserRepository(session)
        self.role_repo = RoleRepository(session)
        self.user_role_repo = UserRoleRepository(session)
        self.user_auth = get_user_auth()

    async def create(self, tenant_id: str, req: UserCreateRequest) -> UserResponse:
        tenant_id = require_tenant(tenant_id)
        existing = await self.repo.get_by_username(tenant_id, req.username)
        if existing:
            raise ResourceConflictError(
            message=translate("err.user.name_exists", params={"username": req.username}, fallback=f"用户名已存在: {req.username}")
        )  # 原消息: 用户名已存在: {username}

        user = User(
            tenant_id=tenant_id,
            username=req.username,
            password_hash=self.user_auth.hash_password(req.password),
            display_name=req.display_name,
            email=req.email,
            role=req.role,
        )
        self.session.add(user)
        await self.session.flush()
        logger.info(f"创建用户: {user.username} (id={user.id})")
        return UserResponse.from_orm(user)

    async def get(self, tenant_id: str, user_id: int) -> UserResponse:
        tenant_id = require_tenant(tenant_id)
        user = await self.repo.get_by_id(user_id, tenant_id)
        if not user or user.is_deleted:
            raise ResourceNotFoundError(
            message=translate("err.user.not_found", params={"user_id": str(user_id)}, fallback=f"用户不存在: {user_id}")
        )  # 原消息: 用户不存在: {user_id}
        return UserResponse.from_orm(user)

    async def list_users(
        self, tenant_id: str, offset: int = 0, limit: int = 20
    ) -> UserListResponse:
        tenant_id = require_tenant(tenant_id)
        items = await self.repo.list_by_tenant(tenant_id, offset, limit)
        total = await self.repo.count_by_tenant(tenant_id)
        # 批量查 RBAC 绑定角色 id+名称（单 SQL，避免 N+1）——列表角色列与编辑弹窗（get_roles）同源
        role_id_map: dict[int, list[int]] = {u.id: [] for u in items}
        role_name_map: dict[int, list[str]] = {u.id: [] for u in items}
        if items:
            from sqlalchemy import select

            from capabilities.platform.models.role import Role
            from capabilities.platform.models.user_role import UserRole

            rows = await self.session.execute(
                select(UserRole.user_id, Role.id, Role.name)
                .join(Role, Role.id == UserRole.role_id)
                .where(
                    UserRole.tenant_id == tenant_id,
                    UserRole.user_id.in_([u.id for u in items]),
                    Role.is_deleted == 0,
                )
                .order_by(Role.id)
            )
            for user_id, role_id, role_name in rows.all():
                role_id_map.setdefault(user_id, []).append(role_id)
                role_name_map.setdefault(user_id, []).append(role_name)
        responses = [UserResponse.from_orm(u) for u in items]
        for r in responses:
            r.role_ids = role_id_map.get(r.id, [])
            r.role_names = role_name_map.get(r.id, [])
        return UserListResponse(total=total, items=responses)

    async def update(self, tenant_id: str, user_id: int, req: UserUpdateRequest) -> UserResponse:
        tenant_id = require_tenant(tenant_id)
        user = await self.repo.get_by_id(user_id, tenant_id)
        if not user or user.is_deleted:
            raise ResourceNotFoundError(
            message=translate("err.user.not_found", params={"user_id": str(user_id)}, fallback=f"用户不存在: {user_id}")
        )  # 原消息: 用户不存在: {user_id}

        update_data = req.dict(exclude_unset=True)
        new_password = update_data.pop("new_password", None)
        if new_password:
            user.password_hash = self.user_auth.hash_password(new_password)
        for key, val in update_data.items():
            setattr(user, key, val)
        await self.session.flush()
        logger.info(f"更新用户: {user.username} (id={user.id})")
        return UserResponse.from_orm(user)

    async def delete(self, tenant_id: str, user_id: int) -> None:
        tenant_id = require_tenant(tenant_id)
        user = await self.repo.get_by_id(user_id, tenant_id)
        if not user or user.is_deleted:
            raise ResourceNotFoundError(
            message=translate("err.user.not_found", params={"user_id": str(user_id)}, fallback=f"用户不存在: {user_id}")
        )  # 原消息: 用户不存在: {user_id}
        await self.repo.delete_soft(user, tenant_id)
        logger.info(f"删除用户: {user.username} (id={user.id})")

    async def get_roles(self, tenant_id: str, user_id: int) -> list[int]:
        tenant_id = require_tenant(tenant_id)
        user = await self.repo.get_by_id(user_id, tenant_id)
        if not user or user.is_deleted:
            raise ResourceNotFoundError(
            message=translate("err.user.not_found", params={"user_id": str(user_id)}, fallback=f"用户不存在: {user_id}")
        )  # 原消息: 用户不存在: {user_id}
        return list(await self.user_role_repo.get_role_ids(tenant_id, user_id))

    async def get_role_names(self, tenant_id: str, user_id: int) -> list[str]:
        """用户角色名列表（登录/JWT 用）。"""
        tenant_id = require_tenant(tenant_id)
        role_ids = list(await self.user_role_repo.get_role_ids(tenant_id, user_id))
        if not role_ids:
            return []
        from sqlalchemy import select

        from capabilities.platform.models.role import Role

        result = await self.session.execute(
            select(Role.name).where(
                Role.id.in_(role_ids),
                Role.tenant_id == tenant_id,
                Role.is_deleted == 0,
            )
        )
        return list(result.scalars().all())

    async def set_roles(
        self,
        tenant_id: str,
        user_id: int,
        role_ids: list[int],
        operator_permissions: set[str] | None = None,
    ) -> None:
        tenant_id = require_tenant(tenant_id)
        user = await self.repo.get_by_id(user_id, tenant_id)
        if not user or user.is_deleted:
            raise ResourceNotFoundError(
            message=translate("err.user.not_found", params={"user_id": str(user_id)}, fallback=f"用户不存在: {user_id}")
        )  # 原消息: 用户不存在: {user_id}

        normalized_role_ids = list(dict.fromkeys(role_ids))
        for role_id in normalized_role_ids:
            role = await self.role_repo.get_by_id(role_id, tenant_id)
            if not role or role.is_deleted:
                raise ResourceNotFoundError(
                    message=translate("err.role.not_found", params={"role_id": str(role_id)}, fallback=f"角色不存在: {role_id}")
                )  # 原消息: 角色不存在: {role_id}

        # [jonex] 权限重构 B1（D4）：「租户管理员只能由平台管理员指定」。
        # 只在 RoleService.set_permissions 拦「配码」是不够的 —— 029 迁移后
        # 「租户管理员」角色天然持有 tenant:admin，租户管理员把别人绑到这个**既有**
        # 角色上即可绕过。这里补上「用户 → 角色」方向的闸门。
        # operator_permissions=None 表示内部调用（无 HTTP 操作者上下文），此时不拦，
        # 避免种子/迁移脚本类调用被误伤；所有 API 入口都必须显式透传。
        if operator_permissions is not None:
            await assert_can_assign_roles(
                self.session, tenant_id, normalized_role_ids, operator_permissions
            )

        await self.user_role_repo.set_roles(tenant_id, user_id, normalized_role_ids)
        logger.info(f"设置用户角色: user_id={user_id}, roles={normalized_role_ids}")

        from jonex_core.security.permission import invalidate_user_permissions

        await invalidate_user_permissions(tenant_id, user_id)

    async def list_all_users(self) -> list:
        """跨租户查询所有用户（管理员视角）。返回 UserResponse 列表。"""
        users = await self.repo.list_all_shared(0, 10000)
        active = [u for u in users if not u.is_deleted]
        # 批量补 RBAC 绑定角色名（与 list_users 同口径：跨租户按 (tenant_id, user_id) 对单 SQL）
        role_name_map: dict[tuple[str, int], list[str]] = {}
        if active:
            from sqlalchemy import select, tuple_

            from capabilities.platform.models.role import Role
            from capabilities.platform.models.user_role import UserRole

            pairs = [(u.tenant_id, u.id) for u in active]
            rows = await self.session.execute(
                select(UserRole.tenant_id, UserRole.user_id, Role.name)
                .join(Role, Role.id == UserRole.role_id)
                .where(
                    tuple_(UserRole.tenant_id, UserRole.user_id).in_(pairs),
                    Role.is_deleted == 0,
                )
                .order_by(Role.id)
            )
            for t, uid, name in rows.all():
                role_name_map.setdefault((t, uid), []).append(name)
        responses = [self._to_response(u) for u in active]
        for r in responses:
            r.role_names = role_name_map.get((r.tenant_id, r.id), [])
        return responses

    def _to_response(self, user) -> dict:
        from capabilities.platform.dtos.platform import UserResponse
        return UserResponse.from_orm(user)

    async def get_user_counts(self) -> dict[str, int]:
        """获取所有租户的用户数（跨租户统计）"""
        users = await self.repo.list_all_shared(0, 10000)
        counts: dict[str, int] = {}
        for u in users:
            if not u.is_deleted:
                counts[u.tenant_id] = counts.get(u.tenant_id, 0) + 1
        return counts
