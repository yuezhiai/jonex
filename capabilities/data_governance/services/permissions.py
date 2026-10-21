"""数据治理权限：复用 KB 的权限表与 helper，不新建权限体系。

读口径（spec §7）：租户管理员全量；空间成员可见该空间资产；KB 授权成员可见该 KB。
写口径：租户管理员 / 所属空间 `space_manager` / 所属 KB `kb_manager`，其余 403。
"""
from __future__ import annotations

from jonex_core.common.exceptions import PermissionDeniedError
from jonex_core.common.tenant import require_tenant

from capabilities.knowledge_base.services.kb_permission_service import (
    KB_MANAGER,
    get_accessible_kb_ids,
    get_kb_grant_role,
)
from capabilities.knowledge_base.services.space_permission_service import (
    SPACE_MANAGER,
    get_visible_space_ids,
    has_space_role,
    is_tenant_admin,
)


async def visible_kb_ids(tenant_id: str, user_id: str | None) -> list[str] | None:
    """可见 KB id 列表；None = 不过滤（租户管理员），[] = 无可见资产。

    普通成员复用 KB 侧已测试的 helper：可见空间内 KB ∪ 单独授权 KB
    （`get_accessible_kb_ids` 的「检索/服务可见」口径，空间 member 全量命中该空间）。
    """
    tenant_id = require_tenant(tenant_id)
    # 必须最先判：get_visible_space_ids 对 anonymous/无 user 返回 None（不过滤），
    # 若直接复用会变成「匿名全可见」。
    if not user_id or user_id == "anonymous":
        return []
    if await is_tenant_admin(tenant_id, user_id):
        return None
    space_ids = await get_visible_space_ids(tenant_id, user_id)
    kb_ids = await get_accessible_kb_ids(tenant_id, user_id, space_ids)
    return sorted(kb_ids or [])


async def assert_can_write(
    tenant_id: str, user_id: str | None, *, kb_id: str, space_id: str,
) -> None:
    """写权限三档：租户管理员 / 空间管理者 / KB 管理者；否则抛 403。"""
    tenant_id = require_tenant(tenant_id)
    if user_id and await is_tenant_admin(tenant_id, user_id):
        return
    if user_id and space_id and await has_space_role(
        tenant_id, space_id, user_id, SPACE_MANAGER
    ):
        return
    if user_id and kb_id and await get_kb_grant_role(
        tenant_id, kb_id, user_id
    ) == KB_MANAGER:
        return
    raise PermissionDeniedError(
        message="仅租户管理员、空间管理者或知识库管理者可修改资产元数据",
        details={"kb_id": kb_id, "space_id": space_id},
    )
