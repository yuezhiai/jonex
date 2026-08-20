from datetime import datetime
from typing import Optional

try:
    from pydantic.v1 import BaseModel, Field
except ImportError:
    from pydantic import BaseModel, Field


# ============ 用户 ============

class UserCreateRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=128)
    password: str = Field(..., min_length=6, max_length=128)
    display_name: Optional[str] = None
    email: Optional[str] = None
    role: str = "user"
    # 跨租户创建用户时指定目标租户（端点有 user:write 权限码守卫）
    target_tenant_id: Optional[str] = None
    # RBAC 角色绑定：创建后绑定的角色 id（目标租户内的角色；为空则不绑定，users.role 保持默认 'user'）
    role_id: Optional[int] = None


class UserUpdateRequest(BaseModel):
    display_name: Optional[str] = None
    email: Optional[str] = None
    role: Optional[str] = None
    status: Optional[int] = None
    # [jonex] 跨租户编辑目标租户（路由层解析，service 不落实体）
    target_tenant_id: Optional[str] = None


class UserResponse(BaseModel):
    id: int
    tenant_id: str
    username: str
    display_name: Optional[str] = None
    email: Optional[str] = None
    role: str
    status: int
    last_login_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    # RBAC 绑定角色名（user_roles join roles；与编辑弹窗 get_roles 同源）。
    # users.role 是历史列（admin/user），仅作兜底展示。
    role_names: list[str] = []

    class Config:
        orm_mode = True


class UserListResponse(BaseModel):
    total: int
    items: list[UserResponse]


# ============ 角色 ============

class RoleCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    description: Optional[str] = None


class RoleUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class RoleResponse(BaseModel):
    id: int
    tenant_id: str
    name: str
    description: Optional[str] = None
    is_system: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        orm_mode = True


class RoleListResponse(BaseModel):
    total: int
    items: list[RoleResponse]


class RolePermissionsRequest(BaseModel):
    permission_ids: list[int]


class RoleUsersRequest(BaseModel):
    user_ids: list[int] = Field(default_factory=list)


class UserRolesRequest(BaseModel):
    role_ids: list[int]
    # [jonex] 跨租户绑定目标租户（路由层解析）
    target_tenant_id: Optional[str] = None


# ============ 权限 ============

class PermissionResponse(BaseModel):
    id: int
    code: str
    name: str
    resource: str
    action: str
    description: Optional[str] = None

    class Config:
        orm_mode = True


class PermissionListResponse(BaseModel):
    total: int
    items: list[PermissionResponse]


# ============ 菜单 ============

class MenuCreateRequest(BaseModel):
    parent_id: int = 0
    name: str = Field(..., max_length=128)
    path: Optional[str] = None
    icon: Optional[str] = None
    app_id: Optional[int] = None
    sort_order: int = 0
    visible: int = 1
    permission_code: Optional[str] = None


class MenuUpdateRequest(BaseModel):
    parent_id: Optional[int] = None
    name: Optional[str] = None
    path: Optional[str] = None
    icon: Optional[str] = None
    sort_order: Optional[int] = None
    visible: Optional[int] = None
    status: Optional[int] = None
    permission_code: Optional[str] = None


class MenuResponse(BaseModel):
    id: int
    parent_id: int
    name: str
    path: Optional[str] = None
    icon: Optional[str] = None
    app_id: Optional[int] = None
    sort_order: int
    visible: int
    status: int
    permission_code: Optional[str] = None
    children: list["MenuResponse"] = []

    class Config:
        orm_mode = True


class MenuListResponse(BaseModel):
    items: list[MenuResponse]


# ============ 应用注册 ============

class ApplicationCreateRequest(BaseModel):
    app_code: str = Field(..., max_length=64)
    name: str = Field(..., max_length=128)
    entry_path: Optional[str] = None
    icon: Optional[str] = None
    description: Optional[str] = None
    sort_order: int = 0


class ApplicationUpdateRequest(BaseModel):
    name: Optional[str] = None
    entry_path: Optional[str] = None
    icon: Optional[str] = None
    description: Optional[str] = None
    status: Optional[int] = None
    sort_order: Optional[int] = None


class ApplicationResponse(BaseModel):
    id: int
    app_code: str
    name: str
    entry_path: Optional[str] = None
    icon: Optional[str] = None
    description: Optional[str] = None
    status: int
    sort_order: int

    class Config:
        orm_mode = True


class ApplicationListResponse(BaseModel):
    total: int
    items: list[ApplicationResponse]


# ============ 前端应用清单 ============

class FrontendManifestRoutes(BaseModel):
    hostedBase: str
    standaloneBase: str


class FrontendManifestRemote(BaseModel):
    type: str = "module-federation"
    scope: str
    module: str
    entry: str


class FrontendManifestHealth(BaseModel):
    url: str
    timeoutMs: int = 3000


class FrontendManifestPermissions(BaseModel):
    visibleRoles: list[str]
    requiredFeatures: Optional[list[str]] = None


class FrontendManifestFallback(BaseModel):
    mode: str = "standalone"
    url: Optional[str] = None


class FrontendManifestVersion(BaseModel):
    expected: str = "1.x"
    source: str


class FrontendManifestEntry(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    enabled: bool = True
    order: int = 0
    category: Optional[str] = None
    icon: Optional[str] = None
    routes: FrontendManifestRoutes
    remote: FrontendManifestRemote
    standaloneUrl: str
    basePath: str
    entry: str
    scope: str
    module: str = "./Mount"
    apiPrefix: str
    roles: list[str]
    health: FrontendManifestHealth
    permissions: FrontendManifestPermissions
    fallback: FrontendManifestFallback
    version: FrontendManifestVersion


class FrontendManifestResponse(BaseModel):
    schemaVersion: int = 2
    updatedAt: str
    apps: list[FrontendManifestEntry]
