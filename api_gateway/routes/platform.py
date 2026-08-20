"""
平台管理路由 — 纯反代到 Sidecar，零业务逻辑。
"""
import json
import httpx
from fastapi import APIRouter, Query, Request

from jonex_core.common.config import get_config
from jonex_core.common import transmit_locale_header
from api_gateway.deps import raise_from_capability_result

router = APIRouter()


async def _proxy_platform(request: Request, path: str):
    """转发平台管理请求到 Sidecar"""
    config = get_config()
    sidecar_url = config.SIDECAR_URL

    body = None
    if request.method in ("POST", "PUT", "PATCH"):
        raw = await request.body()
        if raw:
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                raise_from_capability_result(
                    {"code": 422, "message": "请求体不是有效的 JSON 格式"}
                )

    headers = {
        "X-API-Key": config.GATEWAY_API_KEY,
        "X-Request-ID": getattr(request.state, "request_id", ""),
        "X-Forwarded-For": request.client.host if request.client else "",
    }
    auth_header = request.headers.get("Authorization")
    if auth_header:
        headers["Authorization"] = auth_header
    tenant_header = request.headers.get("X-Tenant-ID")
    if tenant_header:
        headers["X-Tenant-ID"] = tenant_header

    transmit_locale_header(headers)

    # 拼接 query string
    target = f"{sidecar_url}/platform/{path}"
    if request.query_params:
        target += f"?{request.query_params}"

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            if request.method in ("GET", "DELETE"):
                resp = await client.request(request.method, target, headers=headers)
            else:
                resp = await client.request(request.method, target, json=body, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            try:
                detail = e.response.json()
            except Exception:
                detail = {"message": e.response.text}
            if isinstance(detail, dict) and "success" in detail:
                raise_from_capability_result(detail)
            raise_from_capability_result(
                {"code": e.response.status_code,
                 "message": detail.get("message", f"平台服务错误: HTTP {e.response.status_code}")}
            )


# ==================== 租户管理 ====================

@router.get("/tenants", summary="获取租户列表")
async def list_tenants(
    request: Request,
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
):
    """获取平台租户分页列表"""
    return await _proxy_platform(request, "tenants")

@router.post("/tenants", summary="创建租户")
async def create_tenant(request: Request):
    """创建新的平台租户"""
    return await _proxy_platform(request, "tenants")

@router.get("/tenants/user-counts", summary="获取租户用户数统计")
async def get_tenant_user_counts(request: Request):
    """获取各租户的用户数量统计"""
    return await _proxy_platform(request, "tenants/user-counts")


@router.get("/tenants/{tenant_id}", summary="获取租户详情")
async def get_tenant(tenant_id: str, request: Request):
    """获取指定租户的详细信息"""
    return await _proxy_platform(request, f"tenants/{tenant_id}")

@router.patch("/tenants/{tenant_id}", summary="更新租户")
async def update_tenant(tenant_id: str, request: Request):
    """更新指定租户的配置"""
    return await _proxy_platform(request, f"tenants/{tenant_id}")

@router.delete("/tenants/{tenant_id}", summary="删除租户")
async def delete_tenant(tenant_id: str, request: Request):
    """删除指定租户"""
    return await _proxy_platform(request, f"tenants/{tenant_id}")


# ==================== 用户管理 ====================

@router.get("/users/all", summary="获取全量用户列表")
async def list_all_users(request: Request):
    """获取平台所有用户（不分页）"""
    return await _proxy_platform(request, "users/all")


@router.get("/users", summary="获取用户列表")
async def list_users(
    request: Request,
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=500, description="每页条数"),
):
    """获取平台用户分页列表"""
    return await _proxy_platform(request, "users")


@router.post("/users", summary="创建用户")
async def create_user(request: Request):
    """创建新的平台用户"""
    return await _proxy_platform(request, "users")


@router.get("/users/{user_id}", summary="获取用户详情")
async def get_user(user_id: int, request: Request):
    """获取指定用户的详细信息"""
    return await _proxy_platform(request, f"users/{user_id}")


@router.patch("/users/{user_id}", summary="更新用户")
async def update_user(user_id: int, request: Request):
    """更新指定用户的信息"""
    return await _proxy_platform(request, f"users/{user_id}")


@router.delete("/users/{user_id}", summary="删除用户")
async def delete_user(user_id: int, request: Request):
    """删除指定用户"""
    return await _proxy_platform(request, f"users/{user_id}")


@router.get("/users/{user_id}/roles", summary="获取用户角色")
async def get_user_roles(user_id: int, request: Request):
    """获取指定用户已分配的角色"""
    return await _proxy_platform(request, f"users/{user_id}/roles")


@router.put("/users/{user_id}/roles", summary="设置用户角色")
async def set_user_roles(user_id: int, request: Request):
    """设置指定用户的角色分配"""
    return await _proxy_platform(request, f"users/{user_id}/roles")


# ==================== 角色管理 ====================

@router.get("/roles", summary="获取角色列表")
async def list_roles(
    request: Request,
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
):
    """获取平台角色分页列表"""
    return await _proxy_platform(request, "roles")


@router.post("/roles", summary="创建角色")
async def create_role(request: Request):
    """创建新的平台角色"""
    return await _proxy_platform(request, "roles")


@router.get("/roles/{role_id}", summary="获取角色详情")
async def get_role(role_id: int, request: Request):
    """获取指定角色的详细信息"""
    return await _proxy_platform(request, f"roles/{role_id}")


@router.patch("/roles/{role_id}", summary="更新角色")
async def update_role(role_id: int, request: Request):
    """更新指定角色的配置"""
    return await _proxy_platform(request, f"roles/{role_id}")


@router.delete("/roles/{role_id}", summary="删除角色")
async def delete_role(role_id: int, request: Request):
    """删除指定角色"""
    return await _proxy_platform(request, f"roles/{role_id}")


@router.get("/roles/{role_id}/permissions", summary="获取角色权限")
async def get_role_permissions(role_id: int, request: Request):
    """获取指定角色已分配的权限"""
    return await _proxy_platform(request, f"roles/{role_id}/permissions")


@router.put("/roles/{role_id}/permissions", summary="设置角色权限")
async def set_role_permissions(role_id: int, request: Request):
    """设置指定角色的权限分配"""
    return await _proxy_platform(request, f"roles/{role_id}/permissions")


@router.get("/roles/{role_id}/users", summary="获取角色用户")
async def get_role_users(role_id: int, request: Request):
    """获取指定角色已分配的用户"""
    return await _proxy_platform(request, f"roles/{role_id}/users")


# ==================== 权限管理 ====================

@router.get("/permissions", summary="获取权限列表")
async def list_permissions(
    request: Request,
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(100, ge=1, le=500, description="每页条数"),
):
    """获取平台权限分页列表"""
    return await _proxy_platform(request, "permissions")


# ==================== 菜单管理 ====================

@router.get("/menus", summary="获取菜单树")
async def get_menus(request: Request):
    """获取平台菜单树结构"""
    return await _proxy_platform(request, "menus")


@router.get("/menus/my", summary="获取当前用户可见菜单树")
async def get_my_menus(request: Request):
    """按当前用户权限码过滤的菜单树（登录可见）"""
    return await _proxy_platform(request, "menus/my")


@router.post("/menus", summary="创建菜单")
async def create_menu(request: Request):
    """创建新的平台菜单项"""
    return await _proxy_platform(request, "menus")


@router.put("/menus/{menu_id}", summary="更新菜单")
async def update_menu(menu_id: int, request: Request):
    """更新指定菜单项的配置"""
    return await _proxy_platform(request, f"menus/{menu_id}")


@router.delete("/menus/{menu_id}", summary="删除菜单")
async def delete_menu(menu_id: int, request: Request):
    """删除指定菜单项"""
    return await _proxy_platform(request, f"menus/{menu_id}")


# ==================== 应用管理 ====================

@router.get("/frontend/apps", summary="获取前端应用清单")
async def get_frontend_manifest(request: Request):
    """获取前端子应用注册清单"""
    return await _proxy_platform(request, "frontend/apps")


@router.get("/applications", summary="获取应用列表")
async def list_applications(request: Request):
    """获取已注册的应用列表"""
    return await _proxy_platform(request, "applications")


@router.post("/applications", summary="注册应用")
async def create_application(request: Request):
    """注册新的平台应用"""
    return await _proxy_platform(request, "applications")


@router.get("/applications/{app_id}", summary="获取应用详情")
async def get_application(app_id: int, request: Request):
    """获取指定应用的详细信息"""
    return await _proxy_platform(request, f"applications/{app_id}")


@router.patch("/applications/{app_id}", summary="更新应用")
async def update_application(app_id: int, request: Request):
    """更新指定应用的配置"""
    return await _proxy_platform(request, f"applications/{app_id}")


@router.delete("/applications/{app_id}", summary="删除应用")
async def delete_application(app_id: int, request: Request):
    """注销指定应用"""
    return await _proxy_platform(request, f"applications/{app_id}")


# ==================== 系统配置 ====================

@router.get("/system-configs", summary="获取系统配置列表")
async def list_system_configs(request: Request):
    """获取平台系统配置项列表"""
    return await _proxy_platform(request, "system-configs")


@router.put("/system-configs/{config_key}", summary="更新系统配置")
async def update_system_config(config_key: str, request: Request):
    """更新指定系统配置项的值"""
    return await _proxy_platform(request, f"system-configs/{config_key}")


# ==================== 审计日志 ====================

@router.get("/audit-logs", summary="获取审计日志列表")
async def list_audit_logs(
    request: Request,
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
    user_id: int = Query(None, description="用户 ID 过滤"),
    action: str = Query(None, description="操作类型过滤"),
):
    """获取平台审计日志分页列表"""
    return await _proxy_platform(request, "audit-logs")


@router.get("/audit-logs/actions", summary="获取操作类型列表")
async def list_audit_actions(request: Request):
    """获取当前租户审计日志中已使用的操作类型及其中英文标签"""
    return await _proxy_platform(request, "audit-logs/actions")


@router.get("/audit-logs/resource-types", summary="获取资源类型列表")
async def list_audit_resource_types(request: Request):
    """获取当前租户审计日志中已使用的资源类型及其中英文标签"""
    return await _proxy_platform(request, "audit-logs/resource-types")


@router.get("/audit-logs/{log_id}", summary="获取审计日志详情")
async def get_audit_log(log_id: int, request: Request):
    """获取指定审计日志的详细信息"""
    return await _proxy_platform(request, f"audit-logs/{log_id}")


# ==================== 任务调度 ====================

@router.get("/task-schedules", summary="获取任务调度列表")
async def list_task_schedules(
    request: Request,
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
):
    """获取定时任务调度分页列表"""
    return await _proxy_platform(request, "task-schedules")


@router.post("/task-schedules", summary="创建任务调度")
async def create_task_schedule(request: Request):
    """创建新的定时任务调度"""
    return await _proxy_platform(request, "task-schedules")


@router.get("/task-schedules/{task_id}", summary="获取任务调度详情")
async def get_task_schedule(task_id: int, request: Request):
    """获取指定任务调度的详细信息"""
    return await _proxy_platform(request, f"task-schedules/{task_id}")


@router.patch("/task-schedules/{task_id}", summary="更新任务调度")
async def update_task_schedule(task_id: int, request: Request):
    """更新指定任务调度的配置"""
    return await _proxy_platform(request, f"task-schedules/{task_id}")


@router.delete("/task-schedules/{task_id}", summary="删除任务调度")
async def delete_task_schedule(task_id: int, request: Request):
    """删除指定任务调度"""
    return await _proxy_platform(request, f"task-schedules/{task_id}")


@router.post("/task-schedules/{task_id}/trigger", summary="触发任务调度")
async def trigger_task_schedule(task_id: int, request: Request):
    """立即触发指定任务调度执行"""
    return await _proxy_platform(request, f"task-schedules/{task_id}/trigger")


# ==================== MCP Key 管理 ====================

@router.post("/mcp-keys", summary="创建 MCP Key")
async def create_mcp_key(request: Request):
    """创建新的 MCP Key"""
    return await _proxy_platform(request, "mcp-keys")


@router.get("/mcp-keys", summary="获取 MCP Key 列表")
async def list_mcp_keys(request: Request):
    """获取租户下所有 MCP Key（含已撤销）"""
    return await _proxy_platform(request, "mcp-keys")


@router.get("/mcp-keys/{key_id}", summary="获取 MCP Key 详情")
async def get_mcp_key(key_id: str, request: Request):
    """获取指定 MCP Key 的详细信息"""
    return await _proxy_platform(request, f"mcp-keys/{key_id}")


@router.post("/mcp-keys/{key_id}/revoke", summary="撤销 MCP Key")
async def revoke_mcp_key(key_id: str, request: Request):
    """撤销指定 MCP Key（设置 revoked_at）"""
    return await _proxy_platform(request, f"mcp-keys/{key_id}/revoke")


@router.put("/mcp-keys/{key_id}", summary="编辑 MCP Key")
async def update_mcp_key(key_id: str, request: Request):
    """编辑 MCP Key 元数据（不重置 Key，不生成新明文）"""
    return await _proxy_platform(request, f"mcp-keys/{key_id}")


@router.delete("/mcp-keys/{key_id}", summary="删除 MCP Key")
async def delete_mcp_key(key_id: str, request: Request):
    """软删除 MCP Key（设置 is_deleted=1）"""
    return await _proxy_platform(request, f"mcp-keys/{key_id}")


@router.post("/mcp-keys/{key_id}/reset", summary="重置 MCP Key")
async def reset_mcp_key(key_id: str, request: Request):
    """重置指定 MCP Key（撤销旧 Key + 生成新 Key）"""
    return await _proxy_platform(request, f"mcp-keys/{key_id}/reset")


# ==================== MCP 知识写入 Key ====================


@router.post("/mcp-write-keys", summary="创建知识写入 Key")
async def create_mcp_write_key(request: Request):
    """创建知识写入 Key（一次性返回明文 mcpw_... Key，HTTP 201）"""
    return await _proxy_platform(request, "mcp-write-keys")


@router.get("/mcp-write-keys", summary="获取知识写入 Key 列表")
async def list_mcp_write_keys(request: Request):
    """获取租户下所有知识写入 Key 元数据（含已撤销，脱敏）"""
    return await _proxy_platform(request, "mcp-write-keys")


@router.get("/mcp-write-keys/{key_id}", summary="获取知识写入 Key 详情")
async def get_mcp_write_key(key_id: str, request: Request):
    """获取单个知识写入 Key 详细信息（脱敏）"""
    return await _proxy_platform(request, f"mcp-write-keys/{key_id}")


@router.put("/mcp-write-keys/{key_id}", summary="编辑知识写入 Key")
async def update_mcp_write_key(key_id: str, request: Request):
    """编辑知识写入 Key 元数据（不重置 Key，不生成新明文）"""
    return await _proxy_platform(request, f"mcp-write-keys/{key_id}")


@router.post("/mcp-write-keys/{key_id}/toggle", summary="停用/启用知识写入 Key")
async def toggle_mcp_write_key(key_id: str, request: Request):
    """停用/启用知识写入 Key（可逆，key 不变）"""
    return await _proxy_platform(request, f"mcp-write-keys/{key_id}/toggle")


@router.post("/mcp-write-keys/{key_id}/revoke", summary="撤销知识写入 Key")
async def revoke_mcp_write_key(key_id: str, request: Request):
    """撤销知识写入 Key（设置 revoked_at，不可逆）"""
    return await _proxy_platform(request, f"mcp-write-keys/{key_id}/revoke")


# ==================== MCP 服务目录 ====================


@router.get("/mcp-services", summary="获取 MCP 服务列表")
async def list_mcp_services(request: Request):
    """E6: 获取领域服务列表（含 MCP 发布状态、关联 KB 数量）"""
    return await _proxy_platform(request, "mcp-services")


@router.get("/mcp-services/{service_id}", summary="获取 MCP 服务详情")
async def get_mcp_service_detail(service_id: str, request: Request):
    """查询单个领域服务详情（含发布状态、启用状态、关联 KB）"""
    return await _proxy_platform(request, f"mcp-services/{service_id}")


@router.post("/mcp-services/sync", summary="同步领域服务")
async def sync_mcp_services(request: Request):
    """E11: 手动从 domain service 同步最新服务列表"""
    return await _proxy_platform(request, "mcp-services/sync")


@router.post("/mcp-services/{service_id}/publish", summary="发布 MCP 服务")
async def publish_service(service_id: str, request: Request):
    """E7: 发布领域服务标记为 MCP 可见"""
    return await _proxy_platform(request, f"mcp-services/{service_id}/publish")


@router.post("/mcp-services/{service_id}/unpublish", summary="取消发布 MCP 服务")
async def unpublish_service(service_id: str, request: Request):
    """E8: 取消发布领域服务"""
    return await _proxy_platform(request, f"mcp-services/{service_id}/unpublish")


@router.post("/mcp-services/{service_id}/test-call", summary="测试调用 MCP 服务")
async def test_call_service(service_id: str, request: Request):
    """E9: 测试调用领域服务（一期模拟 RAG）"""
    return await _proxy_platform(request, f"mcp-services/{service_id}/test-call")


@router.get("/mcp-services/{service_id}/authorized-keys", summary="查看授权 Key")
async def get_authorized_keys(service_id: str, request: Request):
    """E10: 查看某服务的已授权 MCP Key 列表"""
    return await _proxy_platform(request, f"mcp-services/{service_id}/authorized-keys")


@router.put("/mcp-services/{service_id}/tool", summary="保存领域服务 Tool 配置")
async def save_tool_config(service_id: str, request: Request):
    """DS-03: 保存领域服务的 MCP Tool 配置（tool 名称 + 描述）"""
    return await _proxy_platform(request, f"mcp-services/{service_id}/tool")


# ==================== MCP 服务配置 — 从领域服务视角管理 Key ====================


@router.post("/mcp-services/{service_id}/keys")
async def _add_key_to_service(service_id: str, request: Request):
    return await _proxy_platform(request, f"mcp-services/{service_id}/keys")


@router.post("/mcp-services/{service_id}/keys/new")
async def _create_key_for_service(service_id: str, request: Request):
    return await _proxy_platform(request, f"mcp-services/{service_id}/keys/new")


@router.patch("/mcp-services/{service_id}/keys/{key_id}")
async def _update_key_permission(service_id: str, key_id: str, request: Request):
    return await _proxy_platform(request, f"mcp-services/{service_id}/keys/{key_id}")


@router.delete("/mcp-services/{service_id}/keys/{key_id}")
async def _remove_key_from_service(service_id: str, key_id: str, request: Request):
    return await _proxy_platform(request, f"mcp-services/{service_id}/keys/{key_id}")


@router.post("/mcp-services/{service_id}/keys/{key_id}/toggle")
async def _toggle_key_for_service(service_id: str, key_id: str, request: Request):
    return await _proxy_platform(request, f"mcp-services/{service_id}/keys/{key_id}/toggle")


@router.post("/mcp-services/{service_id}/keys/{key_id}/recreate")
async def _recreate_key_for_service(service_id: str, key_id: str, request: Request):
    return await _proxy_platform(request, f"mcp-services/{service_id}/keys/{key_id}/recreate")


# ==================== 领域服务 API Key（DS-04） ====================


@router.post("/mcp-services/{service_id}/api-keys", summary="创建领域服务 API Key")
async def create_service_api_key(service_id: str, request: Request):
    """DS-04: 为领域服务创建 API Key（一次性返回明文 yxm_...）"""
    return await _proxy_platform(request, f"mcp-services/{service_id}/api-keys")


@router.get("/mcp-services/{service_id}/api-keys", summary="查询领域服务 API Key 列表")
async def list_service_api_keys(service_id: str, request: Request):
    """DS-04: 查询某领域服务的 API Key 列表（脱敏）"""
    return await _proxy_platform(request, f"mcp-services/{service_id}/api-keys")


@router.delete("/mcp-services/{service_id}/api-keys/{key_id}", summary="撤销领域服务 API Key")
async def revoke_service_api_key(service_id: str, key_id: str, request: Request):
    """DS-04: 撤销领域服务 API Key（设置 revoked_at）"""
    return await _proxy_platform(request, f"mcp-services/{service_id}/api-keys/{key_id}")
