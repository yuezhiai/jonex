"""
角色管理服务。
"""
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.exceptions import ResourceNotFoundError, ResourceConflictError, PermissionDeniedError
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant
from jonex_core.security.permission import invalidate_users_permissions
from capabilities.platform.models.role import Role
from capabilities.platform.repository.permission_repository import PermissionRepository
from capabilities.platform.repository.role_repository import RoleRepository
from capabilities.platform.repository.role_permission_repository import RolePermissionRepository
from capabilities.platform.repository.user_role_repository import UserRoleRepository
from capabilities.platform.services.admin_role_guard import (
    PLATFORM_ADMIN_CODE,
    TENANT_ADMIN_CODE,
    assert_can_assign_roles,
)
from capabilities.platform.dtos.platform import (
    RoleCreateRequest,
    RoleUpdateRequest,
    RoleResponse,
    RoleListResponse,
)

logger = logging.getLogger(__name__)

# [jonex] 权限重构 B1：管理员标识码（方案 §3.2-A）。
# 授予这两个码受额外约束：platform:* 靠 scope 拦，tenant:admin 靠 D4 单独拦。
# 单一定义点在 admin_role_guard，这里只做别名，避免同一个字面量在多处各写一份。
_PLATFORM_ADMIN = PLATFORM_ADMIN_CODE
_TENANT_ADMIN = TENANT_ADMIN_CODE


class RoleService:
    """角色管理服务"""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.repo = RoleRepository(session)
        self.permission_repo = PermissionRepository(session)
        self.role_perm_repo = RolePermissionRepository(session)
        self.user_role_repo = UserRoleRepository(session)

    async def create(self, tenant_id: str, req: RoleCreateRequest) -> RoleResponse:
        tenant_id = require_tenant(tenant_id)
        existing = await self.repo.get_by_name(tenant_id, req.name)
        if existing:
            raise ResourceConflictError(
            message=translate("err.role.name_exists", params={"name": req.name}, fallback=f"角色名已存在: {req.name}")
        )  # 原消息: 角色名已存在: {name}

        role = Role(
            tenant_id=tenant_id,
            name=req.name,
            description=req.description,
        )
        self.session.add(role)
        await self.session.flush()
        logger.info(f"创建角色: {role.name} (id={role.id})")
        return RoleResponse.from_orm(role)

    async def get(self, tenant_id: str, role_id: int) -> RoleResponse:
        tenant_id = require_tenant(tenant_id)
        role = await self.repo.get_by_id(role_id, tenant_id)
        if not role or role.is_deleted:
            raise ResourceNotFoundError(
            message=translate("err.role.not_found", params={"role_id": str(role_id)}, fallback=f"角色不存在: {role_id}")
        )  # 原消息: 角色不存在: {role_id}
        return RoleResponse.from_orm(role)

    async def list_roles(
        self, tenant_id: str, offset: int = 0, limit: int = 20
    ) -> RoleListResponse:
        tenant_id = require_tenant(tenant_id)
        items = await self.repo.list_by_tenant(tenant_id, offset, limit)
        total = await self.repo.count_by_tenant(tenant_id)
        return RoleListResponse(
            total=total,
            items=[RoleResponse.from_orm(r) for r in items],
        )

    async def update(self, tenant_id: str, role_id: int, req: RoleUpdateRequest) -> RoleResponse:
        tenant_id = require_tenant(tenant_id)
        role = await self.repo.get_by_id(role_id, tenant_id)
        if not role or role.is_deleted:
            raise ResourceNotFoundError(
            message=translate("err.role.not_found", params={"role_id": str(role_id)}, fallback=f"角色不存在: {role_id}")
        )  # 原消息: 角色不存在: {role_id}

        update_data = req.dict(exclude_unset=True)
        if "name" in update_data and update_data["name"] != role.name:
            existing = await self.repo.get_by_name(tenant_id, update_data["name"])
            if existing and existing.id != role.id:
                raise ResourceConflictError(
                message=translate("err.role.name_exists", params={"name": update_data['name']}, fallback=f"角色名已存在: {update_data['name']}")
            )  # 原消息: 角色名已存在: {name}
        for key, val in update_data.items():
            setattr(role, key, val)
        await self.session.flush()
        return RoleResponse.from_orm(role)

    async def delete(self, tenant_id: str, role_id: int) -> None:
        tenant_id = require_tenant(tenant_id)
        role = await self.repo.get_by_id(role_id, tenant_id)
        if not role or role.is_deleted:
            raise ResourceNotFoundError(
            message=translate("err.role.not_found", params={"role_id": str(role_id)}, fallback=f"角色不存在: {role_id}")
        )  # 原消息: 角色不存在: {role_id}
        if role.is_system:
            raise ResourceConflictError(
            message=translate("err.role.system_undeletable", fallback="系统角色不可删除")
        )  # 原消息: 系统角色不可删除
        # [jonex] B1（N5②）：名单必须在软删**之前**取。
        # delete_soft 之后 user_roles 的行虽然还在（软删只动 roles 表），但
        # get_user_permissions 的 SQL 是 user_roles ⨝ role_permissions ⨝ permissions，
        # 不看 roles.is_deleted —— 也就是说软删角色后权限码**不会自动消失**，
        # 只有失效缓存 + 下次查 DB 才生效。所以这里的失效不是优化，是撤权的必要一步。
        affected_user_ids = list(await self.user_role_repo.get_users_for_role(tenant_id, role_id))
        await self.repo.delete_soft(role, tenant_id)
        await invalidate_users_permissions(tenant_id, affected_user_ids)
        logger.info(f"删除角色: {role.name} (id={role.id}), 已失效 {len(affected_user_ids)} 个用户的权限缓存")

    async def get_permissions(self, tenant_id: str, role_id: int) -> list[int]:
        tenant_id = require_tenant(tenant_id)
        role = await self.repo.get_by_id(role_id, tenant_id)
        if not role or role.is_deleted:
            raise ResourceNotFoundError(
            message=translate("err.role.not_found", params={"role_id": str(role_id)}, fallback=f"角色不存在: {role_id}")
        )  # 原消息: 角色不存在: {role_id}
        return list(await self.role_perm_repo.get_permission_ids(tenant_id, role_id))

    async def set_permissions(
        self,
        tenant_id: str,
        role_id: int,
        permission_ids: list[int],
        operator_permissions: set[str] | None = None,
    ) -> None:
        tenant_id = require_tenant(tenant_id)
        role = await self.repo.get_by_id(role_id, tenant_id)
        if not role or role.is_deleted:
            raise ResourceNotFoundError(
            message=translate("err.role.not_found", params={"role_id": str(role_id)}, fallback=f"角色不存在: {role_id}")
        )  # 原消息: 角色不存在: {role_id}

        if role.is_system == 1:
            raise PermissionDeniedError(
                message=translate("err.role.system_role_immutable", fallback="系统角色不可修改权限")
            )

        normalized_permission_ids = list(dict.fromkeys(permission_ids))
        for permission_id in normalized_permission_ids:
            permission = await self.permission_repo.get_by_id_shared(permission_id)
            if not permission:
                raise ResourceNotFoundError(
                message=translate("err.permission.not_found", params={"permission_id": str(permission_id)}, fallback=f"权限不存在: {permission_id}")
            )  # 原消息: 权限不存在: {permission_id}
            operator_codes = operator_permissions or set()
            # 权限码驱动：仅当前操作者持有 platform:admin 时可授予平台级权限码
            if getattr(permission, "scope", "tenant") == "platform" and _PLATFORM_ADMIN not in operator_codes:
                raise PermissionDeniedError(
                    message=translate("err.permission.platform_scope_forbidden", fallback="平台级权限仅平台管理员可授予")
                )
            # [jonex] D4：「租户管理员只能由平台管理员指定」。
            # tenant:admin 是 scope='tenant' 的码，落不到上面那条 platform scope 约束里，
            # 需要单独一条。判据是**码**而非角色名 —— 角色名可被租户管理员改，码不能。
            # 效果：租户管理员能建自定义角色、能配普通租户码，但配不出第二个租户管理员。
            # 方案 docs/permissions/PERMISSIONS_REDESIGN.md §7-D4 / §11.3
            if getattr(permission, "code", None) == _TENANT_ADMIN and _PLATFORM_ADMIN not in operator_codes:
                raise PermissionDeniedError(
                    message=translate(
                        "err.permission.tenant_admin_grant_forbidden",
                        fallback="租户管理员标识权限仅平台管理员可授予",
                    )
                )

        await self.role_perm_repo.set_permissions(tenant_id, role_id, normalized_permission_ids)
        # 角色维度失效：服务层用自身 session 取该角色用户 ids（勿在内核另开连接）
        user_ids = list(await self.user_role_repo.get_users_for_role(tenant_id, role_id))
        await invalidate_users_permissions(tenant_id, user_ids)
        logger.info(f"设置角色权限: role_id={role_id}, perms={normalized_permission_ids}")

    async def set_users(
        self,
        tenant_id: str,
        role_id: int,
        user_ids: list[int],
        operator_permissions: set[str] | None = None,
    ) -> None:
        """分配角色的用户集合（delete-then-insert）。"""
        tenant_id = require_tenant(tenant_id)
        role = await self.repo.get_by_id(role_id, tenant_id)
        if not role or role.is_deleted:
            raise ResourceNotFoundError(
            message=translate("err.role.not_found", params={"role_id": str(role_id)}, fallback=f"角色不存在: {role_id}")
        )  # 原消息: 角色不存在: {role_id}

        # [jonex] 权限重构 B1（D4）：这是「角色 → 用户」方向，与 set_roles 的
        # 「用户 → 角色」是同一件事的两个入口，漏一个 D4 就不成立。
        # 空 user_ids 也照拦：清空「租户管理员」名单等于把租户交出去，同样只该平台管理员做。
        if operator_permissions is not None:
            await assert_can_assign_roles(
                self.session, tenant_id, [role_id], operator_permissions
            )

        normalized_user_ids = list(dict.fromkeys(user_ids))
        # [jonex] B1（N5①）：set_users_for_role 是 delete-then-insert，
        # 原实现只失效新名单 → **被移出角色的人**缓存不动，最多 30s 内仍持已撤销的权限。
        # repo 现在返回被移出的 id 集合，两边一起失效。
        removed_user_ids = await self.user_role_repo.set_users_for_role(
            tenant_id, role_id, normalized_user_ids
        )
        await invalidate_users_permissions(
            tenant_id, list(set(normalized_user_ids) | set(removed_user_ids or ()))
        )
        logger.info(
            f"设置角色用户: role_id={role_id}, users={normalized_user_ids}, "
            f"removed={sorted(removed_user_ids or ())}"
        )

    async def get_users(self, tenant_id: str, role_id: int) -> list[int]:
        tenant_id = require_tenant(tenant_id)
        role = await self.repo.get_by_id(role_id, tenant_id)
        if not role or role.is_deleted:
            raise ResourceNotFoundError(
            message=translate("err.role.not_found", params={"role_id": str(role_id)}, fallback=f"角色不存在: {role_id}")
        )  # 原消息: 角色不存在: {role_id}
        return list(await self.user_role_repo.get_users_for_role(tenant_id, role_id))
