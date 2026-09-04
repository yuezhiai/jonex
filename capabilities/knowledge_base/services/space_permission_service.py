"""空间权限判定服务 — 角色查询、空间读写/管理校验、service:write 判定。

职责边界：本模块只做判定（查询 + 抛异常），不修改任何数据。

[jonex] 权限重构 B2（D2：取消 owner 概念）：
空间资源身份收为两级 `space_manager` / `member`，且 **`spaces.owner_id` 不再参与任何判定**
—— 它退化为「创建人」展示字段。创建人的管理权来自 `space_permissions` 里那条
`space_manager` 记录（建空间时同事务写入，存量由 029 回填），而不是 spaces 表的一个字段。

由此 `get_user_role` 变成**纯单表查询**（不再 spaces LEFT JOIN space_permissions），
性能反而更好。方案 docs/permissions/PERMISSIONS_REDESIGN.md §3.3 / §7.1
"""
from __future__ import annotations

from sqlalchemy import and_, select

from jonex_core.common import get_db_session
from jonex_core.common.exceptions import PermissionDeniedError, ResourceNotFoundError
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant
from jonex_core.security.permission import has_permission

from ..models.space import Space, SpacePermission

# [jonex] B2：空间资源身份取值与优先级（唯一定义点）。
# 用 rank 而非三元表达式取最高角色 —— 将来加层级只改这一处。
# 管理者带资源前缀（space_manager），成员统一 member：判定代码自解释，
# 日志排障时也不会出现「manager 到底是哪一层的」歧义。
SPACE_MANAGER = "space_manager"
SPACE_MEMBER = "member"
_SPACE_ROLE_RANK: dict[str, int] = {SPACE_MEMBER: 0, SPACE_MANAGER: 1}

# [jonex] 权限重构 B1：租户管理员标识码。
# 原先是 _TENANT_WRITE = "tenant:write" —— tenant:write 语义过载（既是租户 CRUD
# 又是"全租户知识库超级开关"），且与前端 auth_service 看 user:write 的口径不一致。
# 现在统一为标识码 tenant:admin，platform:admin 为其上位（方案 §3.4 判定链 ①）。
# 方案：docs/permissions/PERMISSIONS_REDESIGN.md §3.2-A / §11.3
_TENANT_ADMIN = "tenant:admin"
_PLATFORM_ADMIN = "platform:admin"


def _coerce_user_id(user_id: str | None) -> int | None:
    """invoke 链路 user_id 是 str（''/'anonymous' 表示无用户）；权限码查询需要 int。"""
    if not user_id or user_id == "anonymous":
        return None
    try:
        return int(user_id)
    except (TypeError, ValueError):
        return None


async def is_tenant_admin(tenant_id: str, user_id: str | None) -> bool:
    """租户管理员（平台/租户管理员）：持有 tenant:admin 或 platform:admin（30s 缓存）。

    判定链 ①（方案 §3.4）：platform:admin 全平台放行；tenant:admin 本租户全业务放行。
    两者都是**纯标识码**，不参与任何具体资源的准入 —— 具体准入走资源身份。

    空间权限的唯一特权判据：全空间可见 + 空间级管理 + 创建空间。
    service:write 与空间权限完全解耦（2026-08-19 收紧终版）。

    [jonex] B1 变更：判据从 tenant:write 换为 tenant:admin。
    tenant:write 回归字面语义（租户 CRUD），不再兼任管理员标识 —— 它与
    前端 auth_service 曾用的 user:write 都**不再**使 is_tenant_admin 为真。
    改这里等于改了全部空间/KB 判定的租户管理员豁免口径，勿轻易加回旧码。
    """
    uid = _coerce_user_id(user_id)
    if uid is None:
        return False
    if await has_permission(tenant_id, uid, _TENANT_ADMIN):
        return True
    # 上位兜底：platform:admin 角色理应同时持有全部 tenant 码（含 tenant:admin），
    # 这里显式再判一次，避免角色矩阵播种漏配时平台管理员被挡在租户业务之外。
    return await has_permission(tenant_id, uid, _PLATFORM_ADMIN)


async def get_user_role(tenant_id: str, space_id: str, user_id: str | None) -> str | None:
    """`'space_manager'` | `'member'` | None —— 纯单表查询 space_permissions。

    [jonex] B2（D2）：删除了原先的 owner 分支。原实现先查 `spaces.owner_id`，
    命中即返回 `'owner'` —— 也就是「只要是创建人，即使 space_permissions 里没有记录
    也拥有最高权限」。D2 取消这条通路，所以不再需要 join spaces。

    ⚠️ 未识别的取值（残留的 manager/viewer/owner）一律视为**无角色**返回 None，
    不做静默兼容。宁可判 403/404 也不要把 viewer 当 member —— 静默兼容会让
    「迁移到底跑过没有」无法从行为上验证，出问题时也定位不到是数据还是代码。
    """
    tenant_id = require_tenant(tenant_id)
    if not user_id or user_id == "anonymous":
        return None
    async with get_db_session() as session:
        result = await session.execute(
            select(SpacePermission.role).where(
                SpacePermission.space_id == space_id,
                SpacePermission.tenant_id == tenant_id,
                SpacePermission.user_id == user_id,
                SpacePermission.is_deleted == 0,
            )
        )
        roles = [r[0] for r in result.all() if r[0] in _SPACE_ROLE_RANK]
    if not roles:
        return None
    return max(roles, key=lambda r: _SPACE_ROLE_RANK[r])


async def has_space_role(tenant_id: str, space_id: str, user_id: str | None, *roles: str) -> bool:
    """布尔判定（不抛异常）——KB 授权兜底的前置入口。

    租户管理员（is_tenant_admin）对租户内所有空间全读写——与 require_space_visible /
    require_space_manage 的「租户管理员全可见 / 全可管理」口径一致。
    匿名/无 user 恒 False（bypass 由 execute 的 skip_space_check 统一处理）。
    """
    if await is_tenant_admin(tenant_id, user_id):
        return True
    actual = await get_user_role(tenant_id, space_id, user_id)
    return actual in roles


async def require_space_role(tenant_id: str, space_id: str, user_id: str | None, *roles: str) -> None:
    """不满足任一角色抛 403（PermissionDeniedError）。

    租户管理员（is_tenant_admin）豁免：与 require_space_manage / require_space_visible
    口径一致——租户管理员即使不是空间成员也对空间内资源具备读写角色。
    """
    if await is_tenant_admin(tenant_id, user_id):
        return
    if not await has_space_role(tenant_id, space_id, user_id, *roles):
        raise PermissionDeniedError(
            message=translate(
                "err.space.insufficient_role",
                params={"space_id": space_id},
                fallback=f"无权操作空间: {space_id}",
            )
        )


async def require_space_manage(tenant_id: str, space_id: str, user_id: str | None) -> None:
    """空间管理（update/delete/set_permissions）：空间管理者或租户管理员。

    [jonex] B2（D2）：判据由 `role != "owner"` 改为 `role != SPACE_MANAGER`。
    创建人不再自动拥有管理权 —— 他的管理权来自 space_permissions 里那条
    space_manager 记录（建空间时同事务写入，存量由 029 回填）。
    i18n key 也从 err.space.owner_required 改为 err.space.manager_required。

    先查租户管理员（30s 缓存，最常见调用者），不命中才查空间角色。
    """
    tenant_id = require_tenant(tenant_id)
    if await is_tenant_admin(tenant_id, user_id):
        return
    role = await get_user_role(tenant_id, space_id, user_id)
    if role != SPACE_MANAGER:
        raise PermissionDeniedError(
            message=translate(
                "err.space.manager_required",
                params={"space_id": space_id},
                fallback=f"仅空间管理者或租户管理员可执行此操作: {space_id}",
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

    None 场景：租户管理员（is_tenant_admin，全可见）、无 user_id/`"anonymous"`（内部链路口径）。
    供 KB/服务列表接口做 SQL 层空间过滤（`space_id.in_(ids)`）与检索类 action 批量过滤，
    避免逐 KB 查角色的 N+1。

    ⚠️ **这是「检索/服务可见」口径，与 KB 列表口径故意不同**（方案 §3.4 / 执行文档 §5.2）：
    空间成员（member）在这里全量命中该空间，所以他能检索到空间下**全部** KB 的内容；
    但 KB 列表只显示他是成员的 KB，那边走 `get_write_space_ids ∪ get_granted_kb_ids`。
    **两个口径不要合并成一个函数** —— 合并即回归。

    [jonex] B2（D2）：去掉了 `Space.owner_id == user_id` 条件。
    """
    tenant_id = require_tenant(tenant_id)
    if not user_id or user_id == "anonymous":
        return None
    if await is_tenant_admin(tenant_id, user_id):
        return None
    async with get_db_session() as session:
        result = await session.execute(
            select(Space.id)
            .join(
                SpacePermission,
                and_(
                    SpacePermission.space_id == Space.id,
                    SpacePermission.tenant_id == Space.tenant_id,
                    SpacePermission.user_id == user_id,
                    SpacePermission.role.in_(tuple(_SPACE_ROLE_RANK)),
                    SpacePermission.is_deleted == 0,
                ),
            )
            .where(
                Space.tenant_id == tenant_id,
                Space.is_deleted == 0,
            )
        )
        return [row[0] for row in result.all()]


async def get_write_space_ids(tenant_id: str, user_id: str | None) -> set[str] | None:
    """可管空间 id 集合（`space_manager` 成员）；None 表示全可写。

    None 场景：租户管理员（is_tenant_admin）、无 user_id/`"anonymous"`（内部链路口径）。
    空间写口径（KB/文档/服务）：`space_manager`；`member` 只读。

    两个用途：
    - 空间列表响应的 `can_write_space` / `can_manage_permissions` 批量计算（单 SQL，避免 N+1）；
    - **KB 列表可见范围的一半**（另一半是 `get_granted_kb_ids`）—— 见 B4 与 §5.2，
      这与 `get_visible_space_ids` 的检索口径**故意不同**，勿合并。

    [jonex] B2（D2）：去掉了 `Space.owner_id == user_id`，且 `role == "manager"`
    改为 `SPACE_MANAGER`。原实现里 owner 无论有没有成员记录都算可写。
    """
    tenant_id = require_tenant(tenant_id)
    if not user_id or user_id == "anonymous":
        return None
    if await is_tenant_admin(tenant_id, user_id):
        return None
    async with get_db_session() as session:
        result = await session.execute(
            select(Space.id)
            .join(
                SpacePermission,
                and_(
                    SpacePermission.space_id == Space.id,
                    SpacePermission.tenant_id == Space.tenant_id,
                    SpacePermission.user_id == user_id,
                    SpacePermission.role == SPACE_MANAGER,
                    SpacePermission.is_deleted == 0,
                ),
            )
            .where(
                Space.tenant_id == tenant_id,
                Space.is_deleted == 0,
            )
        )
        return {row[0] for row in result.all()}
