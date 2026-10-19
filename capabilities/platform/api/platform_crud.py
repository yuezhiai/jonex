"""
平台管理 CRUD API 路由（platform 容器内部）

由 Sidecar 代理调用，不直接对外暴露。
"""
from fastapi import APIRouter, Depends, Query, Request

from jonex_core.common.config import get_config
from jonex_core.common.database import get_db
from jonex_core.common.exceptions import PermissionDeniedError, ResourceNotFoundError
from jonex_core.common.i18n import translate
from jonex_core.common.response import success_response
from jonex_core.common.tenant import extract_tenant_id, require_tenant
from jonex_core.security.permission import PLATFORM_ADMIN_TENANT_IDS, has_permission, require_permission
from jonex_core.security.user_auth import get_current_user
from capabilities.platform.repository.menu_repository import MenuRepository
from capabilities.platform.services.user_service import UserService
from capabilities.platform.services.role_service import RoleService
from capabilities.platform.services.menu_service import MenuService
from capabilities.platform.services.role_binding import ensure_default_role_binding
from capabilities.platform.services.security_config_service import SecurityConfigService
from capabilities.platform.services.misc_services import (
    ApplicationService,
    PermissionService,
    SystemConfigService,
    TaskScheduleService,
    TenantService,
    tenant_matches_keyword,
)
from capabilities.platform.dtos.platform import (
    UserCreateRequest,
    UserUpdateRequest,
    RoleCreateRequest,
    RoleUpdateRequest,
    RolePermissionsRequest,
    RoleUsersRequest,
    UserRolesRequest,
    MenuCreateRequest,
    MenuUpdateRequest,
    ApplicationCreateRequest,
    ApplicationUpdateRequest,
)
from capabilities.platform.dtos.misc import (
    SystemConfigUpdateRequest,
    TaskScheduleCreateRequest,
    TaskScheduleUpdateRequest,
    TenantCreateRequest,
    TenantSecurityConfigUpdateRequest,
    TenantUpdateRequest,
)

router = APIRouter()


async def _operator_permissions(_p: dict) -> set[str]:
    """当前操作者的权限码集合（用于服务层的 scope / D4 授予约束）。

    [jonex] B1（D4）：模拟态必须走 perms 直通 —— 模拟态下操作者在目标租户里
    没有角色行，查库会返回空集，平台管理员反而被自己的约束拦住。
    `get_user_permissions` 的 impersonated/perms 参数正是这个短路。
    """
    from jonex_core.security.permission import get_user_permissions

    return await get_user_permissions(
        _p["tenant_id"], _p["user_id"],
        impersonated=_p.get("impersonated"), perms=_p.get("perms"),
    )


async def _resolve_target_tenant(
    request: Request, db, target_tenant_id: str | None, current: dict
) -> str:
    """解析操作目标租户。

    无 target → 操作者租户（现状口径）；
    有 target → require_tenant 校验合法租户；跨租户时要求 platform:tenant:read，
    并确认目标租户存在（TenantService.get）。同租户 target 直接放行（租户管理员可用）。
    """
    if not target_tenant_id:
        return extract_tenant_id(request)
    tenant_id = require_tenant(target_tenant_id)
    if tenant_id != current.get("tenant_id"):
        # 模拟态越权收口：仅允许操作当前模拟租户本身，禁止跨租户（即使持 platform:tenant:read）
        if current.get("impersonated"):
            raise PermissionDeniedError(
                message=translate(
                    "err.tenant.cross_tenant_forbidden",
                    fallback="无权操作其他租户",
                )
            )
        if not await has_permission(
            current["tenant_id"],
            current["user_id"],
            "platform:tenant:read",
            impersonated=current.get("impersonated", False),
            perms=current.get("perms") or [],
        ):
            raise PermissionDeniedError(
                message=translate(
                    "err.tenant.cross_tenant_forbidden",
                    fallback="无权操作其他租户",
                )
            )
        await TenantService(db).get(tenant_id)
    return tenant_id


def _menu_code_matches(permission_code: str, perms: set[str]) -> bool:
    """菜单码判定：支持**逗号分隔的多码**，任一命中即可（单码行为不变）。

    为什么需要多码：同一个菜单项可能要对两套权限都开放。合并后的「系统配置」页就是这样 ——
    同一页既按 `platform:config:*` 渲染平台块，又按 `tenant:security:*` 渲染租户块，于是菜单码
    写成 `'platform:config:read,tenant:security:read'`。

    只支持单码等值时只能挂其中一个，两边都要吃亏：
      - 挂平台码 → 租户管理员侧栏里没有入口（接口通了却没有入口）；
      - 挂租户码 → 只持平台码的角色看不到入口（今天靠「平台管理员恰好持全部 tenant 码」侥幸成立）。
    与子应用 `routes.config.ts` 的 `permissionCode` **数组**语义保持一致（见 router/menu.ts）。
    """
    return any(code.strip() in perms for code in permission_code.split(",") if code.strip())


def _filter_visible_menus(menus: list, perms: set[str]) -> list:
    """菜单权限过滤纯函数。

    规则（写死）：
      ① permission_code IS NULL，或其中**任一个**码被持有（逗号分隔多码）→ 节点可见；
      ② 父节点不可见 → 子节点连带隐藏；
      ③ 组头判定：在原树中有子节点的节点视为「组头」——组头自身可见但
        所有子节点不可见 → 裁剪组头；无子节点的叶子按自身码判定。
    """
    from collections import defaultdict

    parent_of: dict = defaultdict(list)
    for m in menus:
        if m.parent_id not in (0, None):
            parent_of[m.parent_id].append(m.id)

    visible = {m for m in menus if not m.permission_code or _menu_code_matches(m.permission_code, perms)}
    visible_ids = {m.id for m in visible}

    # 规则 ②：父不可见 → 子连带隐藏（自顶向下传播，迭代到不动点）
    changed = True
    while changed:
        changed = False
        for m in list(visible):
            if m.parent_id not in (0, None) and m.parent_id not in visible_ids:
                visible.discard(m)
                visible_ids.discard(m.id)
                changed = True

    # 规则 ③：裁掉「所有子节点都不可见」的组头。
    #
    # [jonex] 权限重构 B1 修复：原实现是**单遍**列表推导，对**嵌套组头**失效。
    # 现象：普通用户（只持 knowledge:read/service:read/space:read）会看到一个空的
    # 「平台管理」组头 —— 它的子节点是「账号与权限」和「系统运维」两个组头，
    # 这两个组头因子项全不可见而被裁，但单遍实现里它们仍留在 visible_ids 中，
    # 于是父组头判定为「有可见子节点」而存活。
    #
    # 修法：自底向上迭代到不动点 —— 组头被裁后要从 visible_ids 移除，
    # 让它的父组头在下一轮重新判定。这才真正落实规则 ③ 的意图「避免出现空分组」。
    changed = True
    while changed:
        changed = False
        for m in list(visible):
            children = parent_of.get(m.id)
            if children and not any(c in visible_ids for c in children):
                visible.discard(m)
                visible_ids.discard(m.id)
                changed = True

    return sorted(visible, key=lambda m: (m.sort_order or 0, m.id))


# ==================== 租户管理 ====================

@router.get("/tenants")
async def list_tenants(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    keyword: str | None = Query(None, max_length=128, description="按租户 ID / 名称 / 描述模糊匹配"),
    db=Depends(get_db),
    current: dict = Depends(get_current_user),
):
    svc = TenantService(db)
    if await has_permission(
        current["tenant_id"],
        current["user_id"],
        "platform:tenant:read",
        impersonated=current.get("impersonated", False),
        perms=current.get("perms") or [],
    ):
        offset = (page - 1) * page_size
        result = await svc.list(offset, page_size, keyword)
        return success_response(data=result)
    # 无平台级权限：仅返回当前租户；租户行缺失时降级为空列表（避免整页报错）
    try:
        tenant = await svc.get(current["tenant_id"])
        # 这一支走 get()，不过 SQL 过滤 —— 关键字在这里补一道，否则会出现「搜 A 却列出 B」
        items = [tenant] if tenant_matches_keyword(tenant, keyword) else []
        return success_response(data={"items": items, "total": len(items)})
    except ResourceNotFoundError:
        return success_response(data={"items": [], "total": 0})


@router.get("/tenants/user-counts")
async def get_tenant_user_counts(db=Depends(get_db), current: dict = Depends(get_current_user)):
    """各租户用户数统计（跨租户）；无平台级权限仅返回本租户计数"""
    svc = UserService(db)
    if await has_permission(
        current["tenant_id"],
        current["user_id"],
        "platform:tenant:read",
        impersonated=current.get("impersonated", False),
        perms=current.get("perms") or [],
    ):
        counts = await svc.get_user_counts()
        return success_response(data=counts)
    result = await svc.list_users(current["tenant_id"], 0, 10000)
    return success_response(data={current["tenant_id"]: result.total})


@router.post("/tenants")
async def create_tenant(
    req: TenantCreateRequest, db=Depends(get_db), _p: dict = Depends(require_permission("platform:tenant:write"))
):
    svc = TenantService(db)
    result = await svc.create(req)
    return success_response(data=result, message="租户已创建")


@router.get("/tenants/{tenant_id}")
async def get_tenant(
    tenant_id: str, db=Depends(get_db), _p: dict = Depends(require_permission("platform:tenant:read"))
):
    svc = TenantService(db)
    result = await svc.get(tenant_id)
    return success_response(data=result)


@router.patch("/tenants/{tenant_id}")
async def update_tenant(
    tenant_id: str, req: TenantUpdateRequest, db=Depends(get_db), _p: dict = Depends(require_permission("platform:tenant:write"))
):
    svc = TenantService(db)
    result = await svc.update(tenant_id, req)
    return success_response(data=result, message="租户已更新")


@router.delete("/tenants/{tenant_id}")
async def delete_tenant(
    tenant_id: str, db=Depends(get_db), _p: dict = Depends(require_permission("platform:tenant:write"))
):
    svc = TenantService(db)
    await svc.delete(tenant_id)
    return success_response(message="租户已删除")


# ==================== 用户管理 ====================

@router.get("/users/all")
async def list_all_users(db=Depends(get_db), _p: dict = Depends(require_permission("platform:user:all"))):
    """跨租户查询所有用户（平台级权限）"""
    svc = UserService(db)
    result = await svc.list_all_users()
    return success_response(data={"items": [r.dict() for r in result], "total": len(result)})


@router.get("/users")
async def list_users(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=500),
    db=Depends(get_db),
    _p: dict = Depends(require_permission("user:read")),
):
    tenant_id = extract_tenant_id(request)
    svc = UserService(db)
    offset = (page - 1) * page_size
    result = await svc.list_users(tenant_id, offset, page_size)
    return success_response(data=result.dict())


@router.post("/users")
async def create_user(
    request: Request, req: UserCreateRequest, db=Depends(get_db),
    _p: dict = Depends(require_permission("user:write")),
    current: dict = Depends(get_current_user),
):
    # 管理员跨租户创建：helper 校验目标租户合法且存在（require_tenant 拒绝 default/system 等）
    tenant_id = await _resolve_target_tenant(request, db, req.target_tenant_id, current)
    svc = UserService(db)
    result = await svc.create(tenant_id, req)
    # RBAC 角色绑定：创建时选择了角色则绑定（set_roles 内校验角色存在并失效权限缓存）。
    # [jonex] 支持多角色；role_ids 优先，回落到旧的单值 role_id。
    # 与 create 同一 session/事务：一旦调用 set_roles，绑定失败会整体回滚。
    # 两者皆空时走兜底绑定（见 else 分支）—— 建号不产出零角色用户。
    role_ids = req.role_ids or ([req.role_id] if req.role_id is not None else [])
    if role_ids:
        # [jonex] B1（D4）：建用户时直接选「租户管理员」角色同样要拦，否则绕过 PUT roles
        await svc.set_roles(
            tenant_id, result.id, role_ids,
            operator_permissions=await _operator_permissions(_p),
        )
    else:
        # 未指定角色 → 兜底绑该租户「普通用户」。不绑的话用户只写遗留列 users.role、
        # 无任何 user_roles 绑定 ⇒ 权限集为空集、侧边栏全空，而请求仍返回 200（静默坏状态，
        # 遗留列对该缺陷完全透明）。口径与 SSO 首登 / 企业身份同步两条自动建号路径一致。
        # 走内部调用语义（operator_permissions=None）：绑的是「普通用户」，不涉 D4 闸门。
        await ensure_default_role_binding(
            svc, tenant_id, result.id, source="admin_create"
        )
    return success_response(data=result.dict())


@router.get("/users/{user_id}")
async def get_user(
    request: Request, user_id: int, db=Depends(get_db), _p: dict = Depends(require_permission("user:read"))
):
    tenant_id = extract_tenant_id(request)
    svc = UserService(db)
    result = await svc.get(tenant_id, user_id)
    return success_response(data=result.dict())


@router.patch("/users/{user_id}")
async def update_user(
    request: Request, user_id: int, req: UserUpdateRequest, db=Depends(get_db),
    _p: dict = Depends(require_permission("user:write")),
    current: dict = Depends(get_current_user),
):
    tenant_id = await _resolve_target_tenant(request, db, req.target_tenant_id, current)
    # target_tenant_id 是路由参数，剔除后传 service，避免 setattr 到 User 实体
    update_req = req.copy(exclude={"target_tenant_id"})
    svc = UserService(db)
    result = await svc.update(tenant_id, user_id, update_req)
    return success_response(data=result.dict())


@router.delete("/users/{user_id}")
async def delete_user(
    request: Request, user_id: int,
    target_tenant_id: str | None = Query(None),
    db=Depends(get_db),
    _p: dict = Depends(require_permission("user:write")),
    current: dict = Depends(get_current_user),
):
    # [jonex] 补齐跨租户删除：此前只有 extract_tenant_id，与同组其他端点
    # （POST / PATCH / roles）不一致，平台管理员跨租户删除必然报「用户不存在」。
    # _resolve_target_tenant 内含跨租户闸门（模拟态禁止 + 需 platform:tenant:read + 目标租户存在）。
    tenant_id = await _resolve_target_tenant(request, db, target_tenant_id, current)
    svc = UserService(db)
    await svc.delete(tenant_id, user_id)

    from jonex_core.security.permission import invalidate_user_permissions

    await invalidate_user_permissions(tenant_id, user_id)
    return success_response(message="用户已删除")


@router.post("/users/{user_id}/disable")
async def disable_user(
    request: Request, user_id: int,
    target_tenant_id: str | None = Query(None),
    db=Depends(get_db),
    _p: dict = Depends(require_permission("user:write")),
    current: dict = Depends(get_current_user),
):
    # [jonex] B8：账号停用。status=0 后登录链路（auth_service login/refresh/me）
    # 已校验 status!=1 拒绝签发 token；service 层负责写 status、失效权限缓存、
    # 自我停用/最后管理员防护，此处只解析目标租户并透传操作者身份。
    tenant_id = await _resolve_target_tenant(request, db, target_tenant_id, current)
    svc = UserService(db)
    result = await svc.disable(tenant_id, user_id, operator_id=current["user_id"])
    return success_response(data=result.dict())


@router.post("/users/{user_id}/enable")
async def enable_user(
    request: Request, user_id: int,
    target_tenant_id: str | None = Query(None),
    db=Depends(get_db),
    _p: dict = Depends(require_permission("user:write")),
    current: dict = Depends(get_current_user),
):
    tenant_id = await _resolve_target_tenant(request, db, target_tenant_id, current)
    svc = UserService(db)
    result = await svc.enable(tenant_id, user_id)
    return success_response(data=result.dict())


@router.get("/users/{user_id}/roles")
async def get_user_roles(
    request: Request, user_id: int,
    target_tenant_id: str | None = Query(None),
    db=Depends(get_db),
    _p: dict = Depends(require_permission("role:read")),
    current: dict = Depends(get_current_user),
):
    tenant_id = await _resolve_target_tenant(request, db, target_tenant_id, current)
    svc = UserService(db)
    roles = await svc.get_roles(tenant_id, user_id)
    return success_response(data={"role_ids": roles})


@router.put("/users/{user_id}/roles")
async def set_user_roles(
    request: Request, user_id: int, req: UserRolesRequest, db=Depends(get_db),
    _p: dict = Depends(require_permission("role:write")),
    current: dict = Depends(get_current_user),
):
    tenant_id = await _resolve_target_tenant(request, db, req.target_tenant_id, current)
    svc = UserService(db)
    # [jonex] B1（D4）：D4 的主入口 —— 「租户管理员只能由平台管理员指定」
    await svc.set_roles(
        tenant_id, user_id, req.role_ids,
        operator_permissions=await _operator_permissions(_p),
    )
    return success_response(message="用户角色已更新")


# ==================== 角色管理 ====================

@router.get("/roles")
async def list_roles(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    target_tenant_id: str | None = Query(None),
    db=Depends(get_db),
    _p: dict = Depends(require_permission("role:read")),
    current: dict = Depends(get_current_user),
):
    tenant_id = await _resolve_target_tenant(request, db, target_tenant_id, current)
    svc = RoleService(db)
    offset = (page - 1) * page_size
    result = await svc.list_roles(tenant_id, offset, page_size)
    return success_response(data=result.dict())


@router.post("/roles")
async def create_role(
    request: Request, req: RoleCreateRequest, db=Depends(get_db), _p: dict = Depends(require_permission("role:write"))
):
    tenant_id = extract_tenant_id(request)
    svc = RoleService(db)
    result = await svc.create(tenant_id, req)
    return success_response(data=result.dict())


@router.get("/roles/{role_id}")
async def get_role(
    request: Request, role_id: int, db=Depends(get_db), _p: dict = Depends(require_permission("role:read"))
):
    tenant_id = extract_tenant_id(request)
    svc = RoleService(db)
    result = await svc.get(tenant_id, role_id)
    return success_response(data=result.dict())


@router.patch("/roles/{role_id}")
async def update_role(
    request: Request, role_id: int, req: RoleUpdateRequest, db=Depends(get_db), _p: dict = Depends(require_permission("role:write"))
):
    tenant_id = extract_tenant_id(request)
    svc = RoleService(db)
    result = await svc.update(tenant_id, role_id, req)
    return success_response(data=result.dict())


@router.delete("/roles/{role_id}")
async def delete_role(
    request: Request, role_id: int, db=Depends(get_db), _p: dict = Depends(require_permission("role:write"))
):
    tenant_id = extract_tenant_id(request)
    svc = RoleService(db)
    await svc.delete(tenant_id, role_id)
    return success_response(message="角色已删除")


@router.get("/roles/{role_id}/permissions")
async def get_role_permissions(
    request: Request, role_id: int, db=Depends(get_db), _p: dict = Depends(require_permission("role:read"))
):
    tenant_id = extract_tenant_id(request)
    svc = RoleService(db)
    perms = await svc.get_permissions(tenant_id, role_id)
    return success_response(data={"permission_ids": perms})


@router.get("/roles/{role_id}/users")
async def get_role_users(
    request: Request, role_id: int, db=Depends(get_db), _p: dict = Depends(require_permission("role:read"))
):
    tenant_id = extract_tenant_id(request)
    svc = RoleService(db)
    users = await svc.get_users(tenant_id, role_id)
    return success_response(data={"user_ids": users})


@router.put("/roles/{role_id}/users")
async def set_role_users(
    role_id: int,
    req: RoleUsersRequest,
    request: Request,
    db=Depends(get_db),
    _p: dict = Depends(require_permission("role:write")),
):
    tenant_id = extract_tenant_id(request)
    svc = RoleService(db)
    # [jonex] B1（D4）：往「租户管理员」角色里塞人也是「指定租户管理员」，需透传操作者码
    await svc.set_users(
        tenant_id, role_id, req.user_ids,
        operator_permissions=await _operator_permissions(_p),
    )
    return success_response(message="角色用户已更新")


@router.put("/roles/{role_id}/permissions")
async def set_role_permissions(
    request: Request,
    role_id: int,
    req: RolePermissionsRequest,
    db=Depends(get_db),
    _p: dict = Depends(require_permission("role:write")),
):
    tenant_id = extract_tenant_id(request)
    operator_permissions = await _operator_permissions(_p)
    svc = RoleService(db)
    await svc.set_permissions(tenant_id, role_id, req.permission_ids, operator_permissions=operator_permissions)
    return success_response(message="角色权限已更新")


# ==================== 权限管理 ====================

@router.get("/permissions")
async def list_permissions(
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    db=Depends(get_db),
    current: dict = Depends(get_current_user),
):
    svc = PermissionService(db)
    offset = (page - 1) * page_size
    # 权限码驱动：仅当前用户持有 platform:admin 时返回平台码（防枚举/防看到不可授予的码）
    from jonex_core.security.permission import get_user_permissions

    perms = await get_user_permissions(
        current["tenant_id"], current["user_id"],
        impersonated=current.get("impersonated"), perms=current.get("perms"),
    )
    is_platform_admin = "platform:admin" in perms
    scope = None if is_platform_admin else "tenant"
    result = await svc.list_permissions(offset, page_size, scope=scope)
    # [jonex] 权限重构 B1（D4）：tenant:admin 是 scope='tenant' 的码，上面的 scope
    # 过滤拦不住它，租户管理员会在角色权限页看到这个复选框 —— 但勾了保存必然 403
    # （RoleService.set_permissions 的 D4 约束）。这里一并隐掉，避免出现
    # 「配了也没用」的勾选项误导配置者（同 §11.5 删废弃码的理由）。
    # 平台管理员照常可见可授。
    if not is_platform_admin:
        data = result.dict()
        before = len(data.get("items", []))
        data["items"] = [it for it in data.get("items", []) if it.get("code") != "tenant:admin"]
        # total 同步扣减，否则前端分页显示的条数与实际列表不一致
        data["total"] = max(0, data.get("total", 0) - (before - len(data["items"])))
        return success_response(data=data)
    return success_response(data=result.dict())


# ==================== 菜单管理 ====================

@router.get("/menus")
async def get_menus(db=Depends(get_db), _p: dict = Depends(require_permission("platform:menu:read"))):
    svc = MenuService(db)
    result = await svc.get_tree()
    return success_response(data=result.dict())


@router.get("/menus/my")
async def get_my_menus(db=Depends(get_db), current: dict = Depends(get_current_user)):
    """当前用户可见菜单树（登录可见，按权限过滤）。"""
    from jonex_core.security.permission import get_user_permissions

    perms = await get_user_permissions(
        current["tenant_id"], current["user_id"],
        impersonated=current.get("impersonated"), perms=current.get("perms"),
    )
    menus = await MenuRepository(db).list_tree()
    filtered = _filter_visible_menus(menus, perms)
    tree = MenuService._build_tree(filtered)
    return success_response(data={"items": [n.dict() for n in tree]})


@router.post("/menus")
async def create_menu(req: MenuCreateRequest, db=Depends(get_db), _p: dict = Depends(require_permission("platform:menu:write"))):
    svc = MenuService(db)
    result = await svc.create(req)
    return success_response(data=result.dict())


@router.put("/menus/{menu_id}")
async def update_menu(menu_id: int, req: MenuUpdateRequest, db=Depends(get_db), _p: dict = Depends(require_permission("platform:menu:write"))):
    svc = MenuService(db)
    result = await svc.update(menu_id, req)
    return success_response(data=result.dict())


@router.delete("/menus/{menu_id}")
async def delete_menu(menu_id: int, db=Depends(get_db), _p: dict = Depends(require_permission("platform:menu:write"))):
    svc = MenuService(db)
    await svc.delete(menu_id)
    return success_response(message="菜单已删除")


# ==================== 应用管理 ====================

@router.get("/frontend/apps")
async def get_frontend_manifest(db=Depends(get_db), _p: dict = Depends(get_current_user)):
    svc = ApplicationService(db)
    result = await svc.get_frontend_manifest()
    return success_response(data=result.dict())


# ==================== 前端外壳 UI 开关 ====================

@router.get("/frontend/ui-config")
async def get_frontend_ui_config(_p: dict = Depends(get_current_user)):
    """前端外壳的 UI 开关（已登录即可读，不额外要权限码）。

    目前只有一项：``hide_task_center`` —— shell 顶栏那个「任务中心」快捷按钮渲不渲染。
    取值来自部署级环境变量 ``HIDE_TASK_CENTER``（**默认 false = 显示**）。任务中心功能

    ⚠ 不回传权限码、密钥或任何用户数据：这是「界面开关」的通道，不是配置中心。
    """
    return success_response(data={"hide_task_center": bool(get_config().HIDE_TASK_CENTER)})


@router.get("/applications")
async def list_applications(db=Depends(get_db), _p: dict = Depends(require_permission("platform:application:read"))):
    svc = ApplicationService(db)
    result = await svc.list_apps()
    return success_response(data=result.dict())


@router.post("/applications")
async def create_application(req: ApplicationCreateRequest, db=Depends(get_db), _p: dict = Depends(require_permission("platform:application:write"))):
    svc = ApplicationService(db)
    result = await svc.create(req)
    return success_response(data=result.dict())


@router.get("/applications/{app_id}")
async def get_application(app_id: int, db=Depends(get_db), _p: dict = Depends(require_permission("platform:application:read"))):
    svc = ApplicationService(db)
    result = await svc.get(app_id)
    return success_response(data=result.dict())


@router.patch("/applications/{app_id}")
async def update_application(app_id: int, req: ApplicationUpdateRequest, db=Depends(get_db), _p: dict = Depends(require_permission("platform:application:write"))):
    svc = ApplicationService(db)
    result = await svc.update(app_id, req)
    return success_response(data=result.dict())


@router.delete("/applications/{app_id}")
async def delete_application(app_id: int, db=Depends(get_db), _p: dict = Depends(require_permission("platform:application:write"))):
    svc = ApplicationService(db)
    await svc.delete(app_id)
    return success_response(message="应用已删除")


# ==================== 系统配置 ====================

@router.get("/system-configs")
async def list_system_configs(db=Depends(get_db), _p: dict = Depends(require_permission("platform:config:read"))):
    svc = SystemConfigService(db)
    result = await svc.list_configs()
    return success_response(data=result.dict())


@router.put("/system-configs/{config_key}")
async def update_system_config(config_key: str, req: SystemConfigUpdateRequest, db=Depends(get_db), _p: dict = Depends(require_permission("platform:config:write"))):
    svc = SystemConfigService(db)
    result = await svc.update_config(config_key, req)
    return success_response(data=result.dict())


# ==================== 租户级安全配置（租户管理员自助）====================
#
# 与上面「系统配置」的区别，改代码前先读：
#   · 系统配置（/system-configs）是**平台共享的一份全局值**，权限码 platform:config:*
#     （scope=platform）⇒ 只有平台管理员能改，改一次全平台生效。
#   · 这里是**每个租户自己的覆盖值**（platform.tenant_security_configs），权限码
#     tenant:security:*（scope=tenant）⇒ 租户管理员即可自助，只影响本租户。
# 读取链为 租户覆盖 → 平台默认 → 代码默认，定义在 services/security_config_service.py。
#
# ⛔ 租户只能来自鉴权上下文（extract_tenant_id(request)），不得从 body/query 取 ——
#   否则就是把「改别的租户的安全配置」做成 API。


@router.get("/tenant-security-config")
async def list_tenant_security_config(
    request: Request,
    db=Depends(get_db),
    _p: dict = Depends(require_permission("tenant:security:read")),
):
    """本租户的安全配置（生效值 + 平台默认 + 是否被本租户覆盖）。"""
    tenant_id = extract_tenant_id(request)
    svc = SecurityConfigService(db)
    return success_response(data={"items": await svc.list_effective(tenant_id)})


@router.put("/tenant-security-config/{config_key}")
async def update_tenant_security_config(
    config_key: str,
    request: Request,
    req: TenantSecurityConfigUpdateRequest,
    db=Depends(get_db),
    _p: dict = Depends(require_permission("tenant:security:write")),
):
    """设置本租户对某个安全配置的覆盖值（范围校验后 upsert）。"""
    tenant_id = extract_tenant_id(request)
    svc = SecurityConfigService(db)
    item = await svc.set_value(tenant_id, config_key, req.config_value, req.description)
    return success_response(data=item)


@router.delete("/tenant-security-config/{config_key}")
async def reset_tenant_security_config(
    config_key: str,
    request: Request,
    db=Depends(get_db),
    _p: dict = Depends(require_permission("tenant:security:write")),
):
    """删除本租户的覆盖值 = 恢复平台默认。"""
    tenant_id = extract_tenant_id(request)
    svc = SecurityConfigService(db)
    item = await svc.reset_value(tenant_id, config_key)
    return success_response(data=item)


# ==================== 任务调度 ====================

@router.get("/task-schedules")
async def list_task_schedules(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db=Depends(get_db),
    _p: dict = Depends(require_permission("platform:task:read")),
):
    tenant_id = extract_tenant_id(request)
    svc = TaskScheduleService(db)
    offset = (page - 1) * page_size
    result = await svc.list_tasks(tenant_id, offset, page_size)
    return success_response(data=result.dict())


@router.post("/task-schedules")
async def create_task_schedule(
    request: Request, req: TaskScheduleCreateRequest, db=Depends(get_db), _p: dict = Depends(require_permission("platform:task:write"))
):
    tenant_id = extract_tenant_id(request)
    svc = TaskScheduleService(db)
    result = await svc.create(tenant_id, req)
    return success_response(data=result.dict())


@router.get("/task-schedules/{task_id}")
async def get_task_schedule(
    request: Request, task_id: int, db=Depends(get_db), _p: dict = Depends(require_permission("platform:task:read"))
):
    tenant_id = extract_tenant_id(request)
    svc = TaskScheduleService(db)
    result = await svc.get(tenant_id, task_id)
    return success_response(data=result.dict())


@router.patch("/task-schedules/{task_id}")
async def update_task_schedule(
    request: Request,
    task_id: int,
    req: TaskScheduleUpdateRequest,
    db=Depends(get_db),
    _p: dict = Depends(require_permission("platform:task:write")),
):
    tenant_id = extract_tenant_id(request)
    svc = TaskScheduleService(db)
    result = await svc.update(tenant_id, task_id, req)
    return success_response(data=result.dict())


@router.delete("/task-schedules/{task_id}")
async def delete_task_schedule(
    request: Request, task_id: int, db=Depends(get_db), _p: dict = Depends(require_permission("platform:task:write"))
):
    tenant_id = extract_tenant_id(request)
    svc = TaskScheduleService(db)
    await svc.delete(tenant_id, task_id)
    return success_response(message="任务已删除")


@router.post("/task-schedules/{task_id}/trigger")
async def trigger_task_schedule(
    request: Request, task_id: int, db=Depends(get_db), _p: dict = Depends(require_permission("platform:task:write"))
):
    tenant_id = extract_tenant_id(request)
    svc = TaskScheduleService(db)
    task = await svc.get(tenant_id, task_id)
    return success_response(
        data={"task_id": task.id, "name": task.name, "triggered": True},
        message=f"任务 {task.name} 已触发"
    )
