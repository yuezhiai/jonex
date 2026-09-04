"""
[jonex] 权限重构 B1（D4）：「租户管理员只能由平台管理员指定」的共享判据。

方案 docs/permissions/PERMISSIONS_REDESIGN.md §7-D4 / §11.3

D4 有三条入口，都必须守住，否则约束形同虚设：

  1. `RoleService.set_permissions`  —— 给角色配 `tenant:admin` 码（角色 → 码）
  2. `UserService.set_roles`        —— 给用户绑带 `tenant:admin` 的角色（用户 → 角色）
  3. `RoleService.set_users`        —— 往带 `tenant:admin` 的角色里塞用户（角色 → 用户）

只堵 (1) 的话，租户管理员依然能把别人绑到**既有的**「租户管理员」角色上
（029 迁移后该角色天然持有 `tenant:admin`），等于直接绕过 D4。本模块提供 (2)(3) 的判据。

判据一律看**权限码**而不是角色名：角色名 `is_system=0` 时租户管理员可自行改名，
`tenant:admin` 码则受 (1) 保护改不了，是唯一稳定的锚点。
"""
import logging
from typing import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.exceptions import PermissionDeniedError
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant

from capabilities.platform.models.permission import Permission
from capabilities.platform.models.role_permission import RolePermission

logger = logging.getLogger(__name__)

PLATFORM_ADMIN_CODE = "platform:admin"
TENANT_ADMIN_CODE = "tenant:admin"


async def find_admin_role_ids(
    session: AsyncSession,
    tenant_id: str,
    role_ids: Iterable[int],
) -> list[int]:
    """在给定角色里挑出持有 `tenant:admin` 码的那些。

    单条 SQL，空入参直接短路（避免 `IN ()` 这种在部分方言里语法非法的形式）。
    """
    tenant_id = require_tenant(tenant_id)
    ids = [rid for rid in dict.fromkeys(role_ids)]
    if not ids:
        return []

    result = await session.execute(
        select(RolePermission.role_id)
        .join(Permission, Permission.id == RolePermission.permission_id)
        .where(
            RolePermission.tenant_id == tenant_id,
            RolePermission.role_id.in_(ids),
            Permission.code == TENANT_ADMIN_CODE,
        )
        .distinct()
    )
    return list(result.scalars().all())


async def assert_can_assign_roles(
    session: AsyncSession,
    tenant_id: str,
    role_ids: Sequence[int],
    operator_permissions: set[str] | None,
) -> None:
    """D4 闸门：目标角色集合里只要有一个带 `tenant:admin`，操作者就必须是平台管理员。

    校验**目标集合**而非增量，与 `RoleService.set_permissions` 同口径。
    副作用是：租户管理员对一个「已经是租户管理员」的用户点保存也会被拒 —— 这是
    有意为之，`PUT /users/{id}/roles` 是整集合覆盖语义，放过「保持原样」需要先
    读旧集合做 diff，而 diff 相等的判定本身会被角色顺序/重复项等细节绊倒。
    撤销侧不设保护（L6）：租户管理员可以把别人的租户管理员角色摘掉，与既有
    「能删用户」的风险同级，且平台管理员可恢复。
    """
    operator_codes = operator_permissions or set()
    if PLATFORM_ADMIN_CODE in operator_codes:
        return

    admin_role_ids = await find_admin_role_ids(session, tenant_id, role_ids)
    if not admin_role_ids:
        return

    logger.warning(
        f"D4 拦截：非平台管理员尝试分配租户管理员角色 tenant={tenant_id} roles={admin_role_ids}"
    )
    raise PermissionDeniedError(
        message=translate(
            "err.permission.tenant_admin_grant_forbidden",
            fallback="租户管理员标识权限仅平台管理员可授予",
        )
    )
