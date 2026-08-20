"""
应用管理 + 权限管理 + 系统配置 + 审计日志 + 任务调度服务。
"""
import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.exceptions import ResourceNotFoundError, ResourceConflictError
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
            raise ResourceNotFoundError(
            message=translate("err.config.not_found", params={"config_key": config_key}, fallback=f"配置不存在: {config_key}")
        )  # 原消息: 配置不存在: {config_key}
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

async def seed_tenant_roles(session, tenant_id: str) -> None:
    """新租户预设角色播种：4 角色 + scope='tenant' 权限映射（与 demo 模板同构，不含平台码）。

    与 015 第 4/5 步逻辑同构；由 TenantService.create 复用。
    **is_system 一律置 0**（已决策）：仅 demo 租户的系统管理员受系统角色保护；
    租户自己的「系统管理员」是普通角色，可改权限/可删。
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
                Role.name.in_(["系统管理员", "领域服务管理员", "知识编辑者", "观察者"]),
            )
        )
    ).all()

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

    async def list(self, offset: int = 0, limit: int = 20) -> dict:
        items = await self.repo.list_all_shared(offset, limit)
        total = await self.repo.count_shared()
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
