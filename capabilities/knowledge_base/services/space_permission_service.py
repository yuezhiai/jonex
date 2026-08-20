"""空间权限判定服务 — 角色查询、空间读写/管理校验、service:write 判定。

职责边界：本模块只做判定（查询 + 抛异常），不修改任何数据。
`get_user_role` 单查询合并 spaces LEFT JOIN space_permissions（一次往返，设计 §3 性能要求）。
owner 只认 spaces.owner_id（不落 space_permissions）；多行取最高仅限 manager/viewer 之间。
"""
from __future__ import annotations

from sqlalchemy import and_, or_, select

from jonex_core.common import get_db_session
from jonex_core.common.exceptions import PermissionDeniedError, ResourceNotFoundError
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant
from jonex_core.security.permission import has_permission

from ..models.space import Space, SpacePermission

_TENANT_WRITE = "tenant:write"


def _coerce_user_id(user_id: str | None) -> int | None:
    """invoke 链路 user_id 是 str（''/'anonymous' 表示无用户）；权限码查询需要 int。"""
    if not user_id or user_id == "anonymous":
        return None
    try:
        return int(user_id)
    except (TypeError, ValueError):
        return None


async def is_tenant_admin(tenant_id: str, user_id: str | None) -> bool:
    """租户管理员（平台/系统管理员）：持有 tenant:write 权限码（30s 缓存）。

    tenant:write 是系统管理员（role 2）独有码——领域服务管理员（role 3）不含。
    空间权限的唯一特权判据：全空间可见 + 空间级管理 + 创建空间。
    service:write 与空间权限完全解耦（2026-08-19 收紧终版）。
    """
    uid = _coerce_user_id(user_id)
    if uid is None:
        return False
    return await has_permission(tenant_id, uid, _TENANT_WRITE)


async def get_user_role(tenant_id: str, space_id: str, user_id: str | None) -> str | None:
    """'owner'|'manager'|'viewer'|None——单查询：spaces LEFT JOIN space_permissions。"""
    tenant_id = require_tenant(tenant_id)
    if not user_id or user_id == "anonymous":
        return None
    async with get_db_session() as session:
        result = await session.execute(
            select(Space.owner_id, SpacePermission.role)
            .select_from(Space)
            .outerjoin(
                SpacePermission,
                and_(
                    SpacePermission.space_id == Space.id,
                    SpacePermission.tenant_id == Space.tenant_id,
                    SpacePermission.user_id == user_id,
                    SpacePermission.is_deleted == 0,
                ),
            )
            .where(Space.id == space_id, Space.tenant_id == tenant_id, Space.is_deleted == 0)
        )
        rows = result.all()
    if not rows:
        return None
    if rows[0][0] == user_id:
        return "owner"
    roles = {r[1] for r in rows if r[1] in ("manager", "viewer")}
    if not roles:
        return None
    return "manager" if "manager" in roles else "viewer"


async def has_space_role(tenant_id: str, space_id: str, user_id: str | None, *roles: str) -> bool:
    """布尔判定（不抛异常）——KB 授权兜底的前置入口。

    匿名/无 user 恒 False（bypass 由 execute 的 skip_space_check 统一处理）。
    """
    actual = await get_user_role(tenant_id, space_id, user_id)
    return actual in roles


async def require_space_role(tenant_id: str, space_id: str, user_id: str | None, *roles: str) -> None:
    """不满足任一角色抛 403（PermissionDeniedError）。"""
    if not await has_space_role(tenant_id, space_id, user_id, *roles):
        raise PermissionDeniedError(
            message=translate(
                "err.space.insufficient_role",
                params={"space_id": space_id},
                fallback=f"无权操作空间: {space_id}",
            )
        )


async def require_space_manage(tenant_id: str, space_id: str, user_id: str | None) -> None:
    """空间管理（update/delete/set_permissions）：owner 或租户管理员（tenant:write）。

    收紧口径（2026-08-19）：service:write 不再隔空生效——领域服务管理员
    仅在其 owner 的空间可管理；平台/系统管理员（tenant:write）全空间可管理。
    先查租户管理员（30s 缓存，最常见调用者），不命中才查空间角色。
    """
    tenant_id = require_tenant(tenant_id)
    if await is_tenant_admin(tenant_id, user_id):
        return
    role = await get_user_role(tenant_id, space_id, user_id)
    if role != "owner":
        raise PermissionDeniedError(
            message=translate(
                "err.space.owner_required",
                params={"space_id": space_id},
                fallback=f"仅空间 owner 或管理员可执行此操作: {space_id}",
            )
        )


async def require_space_visible(tenant_id: str, space_id: str, user_id: str | None) -> None:
    """空间可见性：租户管理员全可见；成员可见；非成员 404（防探测）。

    收紧口径（2026-08-19）：service:write（如领域服务管理员）不再全可见，
    按成员关系过滤。
    """
    tenant_id = require_tenant(tenant_id)
    if await is_tenant_admin(tenant_id, user_id):
        return
    role = await get_user_role(tenant_id, space_id, user_id)
    if role is None:
        raise ResourceNotFoundError(
            message=translate(
                "err.space.not_found",
                params={"space_id": space_id},
                fallback=f"领域空间 {space_id} 不存在",
            )
        )


async def get_visible_space_ids(tenant_id: str, user_id: str | None) -> list[str] | None:
    """租户内可见空间 id 列表（单 SQL）；None 表示不过滤。

    None 场景：租户管理员（tenant:write，全可见）、无 user_id/`"anonymous"`（内部链路口径）。
    供 KB/服务列表接口做 SQL 层空间过滤（`space_id.in_(ids)`）与检索类 action 批量过滤，
    避免逐 KB 查角色的 N+1。
    """
    tenant_id = require_tenant(tenant_id)
    if not user_id or user_id == "anonymous":
        return None
    if await is_tenant_admin(tenant_id, user_id):
        return None
    async with get_db_session() as session:
        result = await session.execute(
            select(Space.id)
            .outerjoin(
                SpacePermission,
                and_(
                    SpacePermission.space_id == Space.id,
                    SpacePermission.tenant_id == Space.tenant_id,
                    SpacePermission.user_id == user_id,
                    SpacePermission.is_deleted == 0,
                ),
            )
            .where(
                Space.tenant_id == tenant_id,
                Space.is_deleted == 0,
                or_(Space.owner_id == user_id, SpacePermission.user_id == user_id),
            )
        )
        return [row[0] for row in result.all()]


async def get_write_space_ids(tenant_id: str, user_id: str | None) -> set[str] | None:
    """可写空间 id 集合（owner 或 manager 成员）；None 表示全可写。

    None 场景：租户管理员（tenant:write）、无 user_id/`"anonymous"`（内部链路口径）。
    空间写口径（KB/文档/服务）：owner / manager；viewer 只读。
    供空间列表响应的 `can_write_space` 批量计算（单 SQL，避免逐空间查角色）。
    """
    tenant_id = require_tenant(tenant_id)
    if not user_id or user_id == "anonymous":
        return None
    if await is_tenant_admin(tenant_id, user_id):
        return None
    async with get_db_session() as session:
        result = await session.execute(
            select(Space.id)
            .outerjoin(
                SpacePermission,
                and_(
                    SpacePermission.space_id == Space.id,
                    SpacePermission.tenant_id == Space.tenant_id,
                    SpacePermission.user_id == user_id,
                    SpacePermission.role == "manager",
                    SpacePermission.is_deleted == 0,
                ),
            )
            .where(
                Space.tenant_id == tenant_id,
                Space.is_deleted == 0,
                or_(
                    Space.owner_id == user_id,
                    SpacePermission.role == "manager",
                ),
            )
        )
        return {row[0] for row in result.all()}
