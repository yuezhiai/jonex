"""
应用管理 + 权限管理 + 系统配置 + 审计日志 + 任务调度服务。
"""
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.exceptions import (
    InternalError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant
from capabilities.platform.models.application import Application
from capabilities.platform.models.permission import Permission
from capabilities.platform.models.system_config import SystemConfig
from capabilities.platform.models.task_schedule import TaskSchedule
from capabilities.platform.repository.application_repository import ApplicationRepository
from capabilities.platform.repository.permission_repository import PermissionRepository
from capabilities.platform.repository.system_config_repository import SystemConfigRepository
from capabilities.platform.repository.task_schedule_repository import TaskScheduleRepository
from capabilities.platform.services.role_binding import DEFAULT_USER_ROLE_NAME
from capabilities.platform.dtos.platform import (
    ApplicationCreateRequest,
    ApplicationUpdateRequest,
    ApplicationResponse,
    ApplicationListResponse,
    FrontendManifestEntry,
    FrontendManifestFallback,
    FrontendManifestHealth,
    FrontendManifestPermissions,
    FrontendManifestRemote,
    FrontendManifestResponse,
    FrontendManifestRoutes,
    FrontendManifestVersion,
    PermissionResponse,
    PermissionListResponse,
)
from capabilities.platform.dtos.misc import (
    SystemConfigUpdateRequest,
    SystemConfigResponse,
    SystemConfigListResponse,
    TaskScheduleCreateRequest,
    TaskScheduleUpdateRequest,
    TaskScheduleResponse,
    TaskScheduleListResponse,
)

logger = logging.getLogger(__name__)


FRONTEND_SCOPE_BY_APP_CODE = {
    "core-business": "coreBusiness",
    "platform-management": "platformManagement",
    "ecosystem-management": "ecosystemManagement",
}

FRONTEND_CATEGORY_BY_APP_CODE = {
    "core-business": "core-business",
    "platform-management": "platform-management",
    "ecosystem-management": "ecosystem-management",
}

FRONTEND_ICON_BY_APP_CODE = {
    "core-business": "SearchOutlined",
    "platform-management": "SettingOutlined",
    "ecosystem-management": "AppstoreOutlined",
}

# 应用可见性（RBAC 语义）：所有应用对所有登录用户可见（roles 为空 = 不限制）。
# 页面/功能级权限统一由权限码控制（后端 require_permission 403 + 菜单过滤
# + 前端 is_platform_admin 布尔——后端按 platform:admin 码计算）。
# 不用角色名做判定：角色名是租户可变数据，不属于判定契约。
FRONTEND_ROLES_BY_APP_CODE: dict[str, list[str]] = {}


# ============ 应用管理 ============

class ApplicationService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.repo = ApplicationRepository(session)

    async def create(self, req: ApplicationCreateRequest) -> ApplicationResponse:
        existing = await self.repo.get_by_code(req.app_code)
        if existing:
            raise ResourceConflictError(
            message=translate("err.app.code_exists", params={"app_code": req.app_code}, fallback=f"应用编码已存在: {req.app_code}")
        )  # 原消息: 应用编码已存在: {app_code}
        app = Application(
            app_code=req.app_code,
            name=req.name,
            entry_path=req.entry_path,
            icon=req.icon,
            description=req.description,
            sort_order=req.sort_order,
        )
        self.session.add(app)
        await self.session.flush()
        return ApplicationResponse.from_orm(app)

    async def get(self, app_id: int) -> ApplicationResponse:
        app = await self.repo.get_by_id_shared(app_id)
        if not app or app.is_deleted:
            raise ResourceNotFoundError(
            message=translate("err.app.not_found", params={"app_id": str(app_id)}, fallback=f"应用不存在: {app_id}")
        )  # 原消息: 应用不存在: {app_id}
        return ApplicationResponse.from_orm(app)

    async def list_apps(self) -> ApplicationListResponse:
        items = await self.repo.list_active()
        total = len(items)
        return ApplicationListResponse(
            total=total,
            items=[ApplicationResponse.from_orm(a) for a in items],
        )

    async def get_frontend_manifest(self) -> FrontendManifestResponse:
        items = await self.repo.list_active()
        apps = [
            self._to_frontend_manifest_entry(app)
            for app in items
            if app.app_code != "shell"
        ]
        return FrontendManifestResponse(
            updatedAt=datetime.now(timezone.utc).isoformat(),
            apps=apps,
        )

    async def update(self, app_id: int, req: ApplicationUpdateRequest) -> ApplicationResponse:
        app = await self.repo.get_by_id_shared(app_id)
        if not app or app.is_deleted:
            raise ResourceNotFoundError(
            message=translate("err.app.not_found", params={"app_id": str(app_id)}, fallback=f"应用不存在: {app_id}")
        )  # 原消息: 应用不存在: {app_id}
        update_data = req.dict(exclude_unset=True)
        for key, val in update_data.items():
            setattr(app, key, val)
        await self.session.flush()
        return ApplicationResponse.from_orm(app)

    async def delete(self, app_id: int) -> None:
        app = await self.repo.get_by_id_shared(app_id)
        if not app or app.is_deleted:
            raise ResourceNotFoundError(
            message=translate("err.app.not_found", params={"app_id": str(app_id)}, fallback=f"应用不存在: {app_id}")
        )  # 原消息: 应用不存在: {app_id}
        await self.repo.delete_soft_shared(app)

    def _to_frontend_manifest_entry(self, app: Application) -> FrontendManifestEntry:
        code = app.app_code
        base_path = app.entry_path if app.entry_path and app.entry_path.startswith("/apps/") else f"/apps/{code}"
        standalone_base = f"/{code}"
        standalone_url = f"{standalone_base}/"
        remote_entry = f"/remotes/{code}/assets/remoteEntry.js"
        version_url = f"/remotes/{code}/version.json"
        roles = FRONTEND_ROLES_BY_APP_CODE.get(code, [])
        scope = FRONTEND_SCOPE_BY_APP_CODE.get(code, self._to_camel_case(code))

        return FrontendManifestEntry(
            id=code,
            name=app.name,
            description=app.description,
            enabled=app.status == 1,
            order=app.sort_order,
            category=FRONTEND_CATEGORY_BY_APP_CODE.get(code, "business"),
            icon=app.icon or FRONTEND_ICON_BY_APP_CODE.get(code),
            routes=FrontendManifestRoutes(
                hostedBase=base_path,
                standaloneBase=standalone_base,
            ),
            remote=FrontendManifestRemote(
                scope=scope,
                module="./Mount",
                entry=remote_entry,
            ),
            standaloneUrl=standalone_url,
            basePath=base_path,
            entry=remote_entry,
            scope=scope,
            module="./Mount",
            apiPrefix=f"/api/v1/{code}",
            roles=roles,
            health=FrontendManifestHealth(url=version_url),
            permissions=FrontendManifestPermissions(visibleRoles=roles),
            fallback=FrontendManifestFallback(url=standalone_url),
            version=FrontendManifestVersion(source=version_url),
        )

    @staticmethod
    def _to_camel_case(value: str) -> str:
        parts = [p for p in value.split("-") if p]
        if not parts:
            return value
        return parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:])


# ============ 权限管理 ============

class PermissionService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.repo = PermissionRepository(session)

    async def list_permissions(self, offset: int = 0, limit: int = 100, scope: str | None = None) -> PermissionListResponse:
        items = await self.repo.list_all_sorted(offset, limit, scope=scope)
        total = await self.repo.count_all(scope=scope)
        return PermissionListResponse(
            total=total,
            items=[PermissionResponse.from_orm(p) for p in items],
        )


# ============ 系统配置 ============

class SystemConfigService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.repo = SystemConfigRepository(session)

    async def list_configs(self) -> SystemConfigListResponse:
        items = await self.repo.list_all_shared(limit=1000)
        return SystemConfigListResponse(
            items=[SystemConfigResponse.from_orm(c) for c in items],
        )

    async def update_config(self, config_key: str, req: SystemConfigUpdateRequest) -> SystemConfigResponse:
        cfg = await self.repo.get_by_key(config_key)
        if not cfg:
            # 配置项尚未种子时按 upsert 创建（配置页保存新键不再 404）
            cfg = SystemConfig(
                config_group="platform",
                config_key=config_key,
                config_value=req.config_value,
                value_type="string",
                description=req.description,
            )
            self.session.add(cfg)
        else:
            if req.config_value is not None:
                cfg.config_value = req.config_value
            if req.description is not None:
                cfg.description = req.description
        await self.session.flush()
        return SystemConfigResponse.from_orm(cfg)


# ============ 任务调度 ============

class TaskScheduleService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.repo = TaskScheduleRepository(session)

    async def create(self, tenant_id: str, req: TaskScheduleCreateRequest) -> TaskScheduleResponse:
        tenant_id = require_tenant(tenant_id)
        task = TaskSchedule(
            tenant_id=tenant_id,
            name=req.name,
            task_type=req.task_type,
            cron_expr=req.cron_expr,
            config_json=req.config_json,
        )
        self.session.add(task)
        await self.session.flush()
        return TaskScheduleResponse.from_orm(task)

    async def get(self, tenant_id: str, task_id: int) -> TaskScheduleResponse:
        tenant_id = require_tenant(tenant_id)
        task = await self.repo.get_by_id(task_id, tenant_id)
        if not task or task.is_deleted:
            raise ResourceNotFoundError(
            message=translate("err.task.not_found", params={"task_id": str(task_id)}, fallback=f"任务不存在: {task_id}")
        )  # 原消息: 任务不存在: {task_id}
        return TaskScheduleResponse.from_orm(task)

    async def list_tasks(
        self, tenant_id: str, offset: int = 0, limit: int = 20
    ) -> TaskScheduleListResponse:
        tenant_id = require_tenant(tenant_id)
        items = await self.repo.list_by_tenant(tenant_id, offset, limit)
        total = await self.repo.count_by_tenant(tenant_id)
        return TaskScheduleListResponse(
            total=total,
            items=[TaskScheduleResponse.from_orm(t) for t in items],
        )

    async def update(
        self, tenant_id: str, task_id: int, req: TaskScheduleUpdateRequest
    ) -> TaskScheduleResponse:
        tenant_id = require_tenant(tenant_id)
        task = await self.repo.get_by_id(task_id, tenant_id)
        if not task or task.is_deleted:
            raise ResourceNotFoundError(
            message=translate("err.task.not_found", params={"task_id": str(task_id)}, fallback=f"任务不存在: {task_id}")
        )  # 原消息: 任务不存在: {task_id}
        update_data = req.dict(exclude_unset=True)
        for key, val in update_data.items():
            setattr(task, key, val)
        await self.session.flush()
        return TaskScheduleResponse.from_orm(task)

    async def delete(self, tenant_id: str, task_id: int) -> None:
        tenant_id = require_tenant(tenant_id)
        task = await self.repo.get_by_id(task_id, tenant_id)
        if not task or task.is_deleted:
            raise ResourceNotFoundError(
            message=translate("err.task.not_found", params={"task_id": str(task_id)}, fallback=f"任务不存在: {task_id}")
        )  # 原消息: 任务不存在: {task_id}
        await self.repo.delete_soft(task, tenant_id)


# ============ 租户管理 ============

# [jonex] 权限重构 B1（D6）：新租户的预设角色模板由 4 个收为 2 个。
#
# 「平台管理员」不在模板里：D3 已确认平台管理员绑定在运营租户（demo），不复制给业务租户。
# 「领域服务管理员/知识编辑者/观察者」已取消 —— 其能力改由资源身份承担
# （空间 space_manager/member、知识库 kb_manager/member）。
#
# ⚠️ 这个常量必须与 migrations/006_seed_data.sql 的「每租户预设角色播种」块、
#    以及 update/034 的第 3、5 节保持一致。三处描述的是同一件事：一个租户开箱应有哪些账号角色。
#
# 「普通用户」不再写字面量，改引用 role_binding.DEFAULT_USER_ROLE_NAME ——
#    自动建号补默认角色绑定时查的就是这个角色名，两处必须字面一致，否则建号会静默绑不上。
_TENANT_ROLE_TEMPLATE = ("租户管理员", DEFAULT_USER_ROLE_NAME)


async def seed_tenant_roles(session, tenant_id: str) -> None:
    """新租户预设角色播种：2 角色 + scope='tenant' 权限映射（与 demo 模板同构，不含平台码）。

    与 015 第 4/5 步逻辑同构；由 TenantService.create 复用。
    **is_system 一律置 0**（已决策）：仅 demo 租户的租户管理员受系统角色保护；
    租户自己的「租户管理员」是普通角色，可改权限/可删。

    [jonex] B1：模板从 demo 按**角色名**复制，且查询带 `is_deleted == 0` ——
    这意味着 demo 里某个模板角色被软删后，新租户会**静默地少一个角色**。
    所以本函数末尾加了完整性校验：
    模板角色没建齐就抛异常，而不是造出一个「没有普通用户角色」的残缺租户 ——
    那种租户里所有普通用户都绑不到账号角色，登录后菜单全空，且现象与权限缓存问题难以区分。
    """
    from sqlalchemy import select

    from capabilities.platform.models.role import Role
    from capabilities.platform.models.role_permission import RolePermission
    from capabilities.platform.models.permission import Permission

    template = (
        await session.execute(
            select(Role.name, Role.description, Role.is_system).where(
                Role.tenant_id == "tenant_jonex_demo",
                Role.is_deleted == 0,
                Role.name.in_(_TENANT_ROLE_TEMPLATE),
            )
        )
    ).all()

    missing_in_demo = set(_TENANT_ROLE_TEMPLATE) - {name for name, _, _ in template}
    if missing_in_demo:
        raise InternalError(
            message=(
                f"运营租户 tenant_jonex_demo 缺少预设角色模板 {sorted(missing_in_demo)}，"
                f"无法为租户 {tenant_id} 播种角色。请先修复 demo 租户的角色数据"
                f"（参见 deploy/postgres/update/034_repair_tenant_role_seed.sql 第 3 节）"
            )
        )

    for name, description, _is_system in template:
        role = Role(tenant_id=tenant_id, name=name, description=description, is_system=0)
        session.add(role)
        await session.flush()
        # 复制 demo 同名角色的 role_permissions（仅 scope='tenant'）
        demo_role_id = (
            await session.execute(
                select(Role.id).where(
                    Role.tenant_id == "tenant_jonex_demo", Role.name == name, Role.is_deleted == 0
                )
            )
        ).scalar_one()
        rows = (
            await session.execute(
                select(RolePermission.permission_id).where(
                    RolePermission.tenant_id == "tenant_jonex_demo",
                    RolePermission.role_id == demo_role_id,
                )
            )
        ).scalars().all()
        for pid in rows:
            perm_scope = (
                await session.execute(
                    select(Permission.scope).where(Permission.id == pid)
                )
            ).scalar_one_or_none()
            if perm_scope == "platform":
                continue
            session.add(RolePermission(tenant_id=tenant_id, role_id=role.id, permission_id=pid))
    await session.flush()


def normalize_keyword(keyword: Any) -> str:
    """把列表检索的关键字收敛成「干净字符串」，非字符串一律当没传。

    ⚠ 这里必须防一手类型：直接调用路由函数（单测、内部调用）时，没显式传的参数拿到的是
    FastAPI 的 `Query(None)` **对象**而不是 `None` —— 它既不是 str（`.strip()` 会
    AttributeError），又是 truthy（会被误当成"有关键字"，把列表过滤成空）。收敛放在服务层，
    路由层照常声明并透传，调用方不必关心这个坑。
    """
    return keyword.strip() if isinstance(keyword, str) else ""


def tenant_matches_keyword(tenant: dict, keyword: Any) -> bool:
    """Python 侧的关键字匹配（ID / 名称 / 描述，忽略大小写），**口径与仓储里的 SQL 一致**。

    只服务于「无平台权限 → 仅返回本租户」那条降级分支：那条路走 `svc.get()`，不过 SQL 过滤，
    不在这里补一下就会出现「搜了 A 却列出 B」。⚠ 列数多一列少一列都会与 SQL 口径漂移，
    改 `TenantRepository.keyword_conditions` 时必须同步改这里（test_tenant_search.py 有守卫）。
    """
    text = normalize_keyword(keyword).casefold()
    if not text:
        return True
    haystack = f"{tenant['id']} {tenant['name']} {tenant.get('description') or ''}"
    return text in haystack.casefold()


class TenantService:
    def __init__(self, db: AsyncSession):
        from capabilities.platform.repository.tenant_repository import TenantRepository
        from capabilities.platform.models.tenant import Tenant
        self.repo = TenantRepository(db)
        self.Tenant = Tenant

    @staticmethod
    def _to_dict(t) -> dict:
        return {
            "id": t.id,
            "name": t.name,
            "description": t.description,
            "status": t.status,
            "plan_type": t.plan_type,
            "expire_time": t.expire_time.isoformat() if t.expire_time else None,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        }

    async def list(self, offset: int = 0, limit: int = 20, keyword: Any = None) -> dict:
        """租户分页列表；`keyword` 同时匹配 ID / 名称 / 描述（过滤与分页都在库里做）。

        原先没有这个参数，前端只能「拉前 100 条再本地过滤」—— 第 101 个之后的租户搜不到。
        """
        text = normalize_keyword(keyword)
        items = await self.repo.list_by_keyword(offset, limit, text)
        total = await self.repo.count_by_keyword(text)
        return {"items": [self._to_dict(t) for t in items], "total": total}

    async def get(self, tenant_id: str) -> dict:
        t = await self.repo.get_required_shared(tenant_id)
        return self._to_dict(t)

    async def create(self, req) -> dict:
        existing = await self.repo.get_by_id_shared(req.id)
        if existing is not None:
            raise ResourceConflictError(
                message=translate("err.tenant.id_exists", params={"tenant_id": req.id}, fallback=f"租户 ID 已存在: {req.id}")
            )
        # 已软删的同 id 记录：恢复复用（主键 id 仍被占用，直接 insert 会撞 tenants_pkey）
        revived = await self.repo.get_including_deleted_by_id(req.id)
        if revived is not None:
            revived.is_deleted = 0
            revived.name = req.name
            revived.description = req.description
            revived.plan_type = req.plan_type
            revived.expire_time = req.expire_time
            revived.status = 1
            await self.repo.session.flush()
            # 角色随租户软删而保留（delete 不级联），无需重复播种
            return self._to_dict(revived)
        t = self.Tenant(
            id=req.id, name=req.name, description=req.description,
            plan_type=req.plan_type, expire_time=req.expire_time,
        )
        self.repo.session.add(t)
        await self.repo.session.flush()
        # 新租户创建即播种预设角色（与租户行同一事务，commit 时一并落库）
        await seed_tenant_roles(self.repo.session, req.id)
        return self._to_dict(t)

    async def update(self, tenant_id: str, req) -> dict:
        t = await self.repo.get_required_shared(tenant_id)
        updates = req.dict(exclude_unset=True)
        for k, v in updates.items():
            setattr(t, k, v)
        await self.repo.session.flush()
        return self._to_dict(t)

    async def delete(self, tenant_id: str) -> None:
        t = await self.repo.get_required_shared(tenant_id)
        await self.repo.delete_soft_shared(t)
