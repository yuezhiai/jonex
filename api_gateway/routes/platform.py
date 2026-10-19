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
        # 原样透传 ``Bearer   ``（尾部空白）会让 httpx 抛 LocalProtocolError
        # （Illegal header value），表现为畸形头 500 而不是 401。
        headers["Authorization"] = auth_header.strip()
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
                # 平台 REST 透传响应走 StandardResponse 序列化，结构化详情键名是
                # error_details；而 raise_from_capability_result 读的是 invoke 契约的
                # details。此处归一，否则详情在 Gateway 静默丢失（前端拿不到失败原因）。
                raise_from_capability_result(
                    {**detail, "details": detail.get("error_details")}
                )
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


@router.put("/roles/{role_id}/users", summary="设置角色用户")
async def set_role_users(role_id: int, request: Request):
    """设置指定角色的用户分配（body: {user_ids: [...]}）"""
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


# ==================== 前端外壳 UI 开关 ====================

@router.get("/frontend/ui-config", summary="前端外壳 UI 开关")
async def get_frontend_ui_config(request: Request):
    """外壳 UI 开关（顶栏「任务中心」按钮显隐；值来自部署级 env `HIDE_TASK_CENTER`）。

    ⚠ 网关对 platform 是**逐条登记**的（不像 sidecar 有 `/platform/{path:path}` 兜底），
    所以 platform 侧每加一个对外口，**这里必须同步加一条** —— 漏了就是 404，而前端
    `fetchUiConfig` 会静默回落成「不隐藏」，表现成「配了开关却还看得见」这种最难查的形态。
    `tests/unit/test_hide_task_center.py` 里有一条**通用**对照测试钉这件事
    （platform 全部路由 − 网关两个模块登记 = 一份写明原因的白名单）。
    """
    return await _proxy_platform(request, "frontend/ui-config")


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


# ==================== 租户级安全配置（租户管理员自助）====================
#
# ⚠ 网关对 platform 是**逐条登记**的（不像 sidecar 有 /platform/{path:path} 兜底），
# 所以 platform 侧每加一个对外口，这里必须同步加一条 —— 漏了就是 404。
# tests/unit/test_hide_task_center.py 里有一条通用对照测试盯着这件事
# （platform 全部路由 − 网关两个模块登记 = 一份写明原因的白名单）。
#
# 与上面「系统配置」的区别：那是平台共享的全局值（只有平台管理员能改），
# 这里改的是**本租户的覆盖值**（租户管理员即可），读取链三层回落。

@router.get("/tenant-security-config", summary="获取本租户安全配置")
async def list_tenant_security_config(request: Request):
    """本租户的安全配置（生效值 + 平台默认 + 是否被本租户覆盖）"""
    return await _proxy_platform(request, "tenant-security-config")


@router.put("/tenant-security-config/{config_key}", summary="设置本租户安全配置")
async def update_tenant_security_config(config_key: str, request: Request):
    """设置本租户对某个安全配置的覆盖值"""
    return await _proxy_platform(request, f"tenant-security-config/{config_key}")


@router.delete("/tenant-security-config/{config_key}", summary="恢复本租户安全配置默认值")
async def reset_tenant_security_config(config_key: str, request: Request):
    """删除本租户的覆盖值 = 恢复平台默认"""
    return await _proxy_platform(request, f"tenant-security-config/{config_key}")


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


@router.post("/mcp-keys/{key_id}/toggle", summary="停用/启用 MCP Key")
async def toggle_mcp_key(key_id: str, request: Request):
    """停用/启用 MCP Key（可逆，恢复原 Key）"""
    return await _proxy_platform(request, f"mcp-keys/{key_id}/toggle")


@router.post("/mcp-keys/{key_id}/recreate", summary="重新创建 MCP Key")
async def recreate_mcp_key(key_id: str, request: Request):
    """重新创建 MCP Key（仅过期态可用，继承授权 + 生成新明文）"""
    return await _proxy_platform(request, f"mcp-keys/{key_id}/recreate")


@router.put("/mcp-keys/{key_id}", summary="编辑 MCP Key")
async def update_mcp_key(key_id: str, request: Request):
    """编辑 MCP Key 元数据（不重置 Key，不生成新明文）"""
    return await _proxy_platform(request, f"mcp-keys/{key_id}")


@router.delete("/mcp-keys/{key_id}", summary="删除 MCP Key")
async def delete_mcp_key(key_id: str, request: Request):
    """软删除 MCP Key（设置 is_deleted=1）"""
    return await _proxy_platform(request, f"mcp-keys/{key_id}")


# ==================== MCP 服务目录 ====================


@router.get("/mcp-services", summary="获取 MCP 服务列表")
async def list_mcp_services(request: Request):
    """获取领域服务列表（含 MCP 发布状态、关联 KB 数量）"""
    return await _proxy_platform(request, "mcp-services")


@router.get("/mcp-services/{service_id}", summary="获取 MCP 服务详情")
async def get_mcp_service_detail(service_id: str, request: Request):
    """查询单个领域服务详情（含发布状态、启用状态、关联 KB）"""
    return await _proxy_platform(request, f"mcp-services/{service_id}")


@router.get(
    "/mcp-services/{service_id}/authorized-keys", summary="获取 MCP 服务已授权 Key"
)
async def get_mcp_service_authorized_keys(service_id: str, request: Request):
    """查询领域服务已授权的 MCP Key 列表（含派生状态与有效期）"""
    return await _proxy_platform(request, f"mcp-services/{service_id}/authorized-keys")


@router.post("/mcp-services/{service_id}/publish", summary="发布 MCP 服务")
async def publish_service(service_id: str, request: Request):
    """发布领域服务标记为 MCP 可见"""
    return await _proxy_platform(request, f"mcp-services/{service_id}/publish")


@router.post("/mcp-services/{service_id}/unpublish", summary="取消发布 MCP 服务")
async def unpublish_service(service_id: str, request: Request):
    """取消发布领域服务"""
    return await _proxy_platform(request, f"mcp-services/{service_id}/unpublish")


@router.post("/mcp-services/{service_id}/stop", summary="停用 MCP 服务")
async def stop_service(service_id: str, request: Request):
    """停用已发布服务（仅 published 态可用）"""
    return await _proxy_platform(request, f"mcp-services/{service_id}/stop")


@router.post("/mcp-services/{service_id}/start", summary="启用 MCP 服务")
async def start_service(service_id: str, request: Request):
    """启用已停用服务（仅 stopped 态可用）"""
    return await _proxy_platform(request, f"mcp-services/{service_id}/start")


@router.put("/mcp-services/{service_id}/tool", summary="保存领域服务 Tool 配置")
async def save_tool_config(service_id: str, request: Request):
    """保存领域服务的 MCP Tool 配置（tool 名称 + 描述）"""
    return await _proxy_platform(request, f"mcp-services/{service_id}/tool")


# ==================== 企业身份集成 ====================


@router.get("/identity-integration", summary="查询企业身份集成统一展示")
async def get_identity_integration(request: Request):
    """查询统一展示（SSO 状态 + 主数据源 + 最近同步记录摘要）"""
    return await _proxy_platform(request, "identity-integration")


@router.put("/identity-integration", summary="更新企业身份集成配置")
async def update_identity_integration(request: Request):
    """更新配置（SSO 子配置 + 主数据源子配置）"""
    return await _proxy_platform(request, "identity-integration")


@router.post("/identity-integration/test-connection", summary="测试企业身份集成连接")
async def test_identity_integration_connection(request: Request):
    """测试连接（target=sso 或 datasource）"""
    return await _proxy_platform(request, "identity-integration/test-connection")


@router.get("/identity-integration/sso-realm", summary="获取 SSO Realm 只读展示")
async def get_sso_realm(request: Request):
    """tenant_realm_map 只读展示（服务地址/Realm/Client ID/回调地址）。"""
    return await _proxy_platform(request, "identity-integration/sso-realm")


@router.post("/identity-integration/sync", summary="立即同步企业用户")
async def trigger_identity_sync(request: Request):
    """立即同步（mode=incremental/full）"""
    return await _proxy_platform(request, "identity-integration/sync")


@router.get("/identity-integration/sync-records", summary="获取企业身份同步记录列表")
async def list_identity_sync_records(request: Request):
    """同步记录列表（分页）"""
    return await _proxy_platform(request, "identity-integration/sync-records")


@router.get(
    "/identity-integration/sync-records/{record_id}", summary="获取企业身份同步记录详情"
)
async def get_identity_sync_record(record_id: int, request: Request):
    """同步记录详情（含失败明细）"""
    return await _proxy_platform(request, f"identity-integration/sync-records/{record_id}")


# ==================== 平台监控 · 调用监控（C1–C9 + M1）+ Token 监控（K1–K4）====================
#
# ⚠ 网关对 platform 是**逐条登记**的（不像 sidecar 有 /platform/{path:path} 兜底）
# ⇒ platform 侧 14 条监控端点必须在**同一批**登记在这里：只加 platform 不登记
# 网关 = 浏览器侧 404；只登记网关不加 platform = `test_gateway_has_no_stale_routes` 红。
# tests/unit/test_hide_task_center.py 的正反双向断言盯着这件事。


@router.get("/monitor/calls/external/summary", summary="外部依赖统计卡")
async def monitor_external_summary(request: Request):
    return await _proxy_platform(request, "monitor/calls/external/summary")


@router.get("/monitor/calls/external/objects", summary="外部依赖对象列表")
async def monitor_external_objects(request: Request):
    return await _proxy_platform(request, "monitor/calls/external/objects")


@router.get("/monitor/calls/open/summary", summary="开放能力统计卡")
async def monitor_open_summary(request: Request):
    return await _proxy_platform(request, "monitor/calls/open/summary")


@router.get("/monitor/calls/open/objects", summary="开放能力对象列表")
async def monitor_open_objects(request: Request):
    return await _proxy_platform(request, "monitor/calls/open/objects")


@router.get("/monitor/calls/trend", summary="调用趋势")
async def monitor_call_trend(request: Request):
    return await _proxy_platform(request, "monitor/calls/trend")


@router.get("/monitor/calls/records", summary="调用记录列表")
async def monitor_call_records(request: Request):
    return await _proxy_platform(request, "monitor/calls/records")


@router.get("/monitor/calls/objects/{object_id}/detail", summary="对象 / 能力详情")
async def monitor_object_detail(object_id: str, request: Request):
    return await _proxy_platform(request, f"monitor/calls/objects/{object_id}/detail")


@router.get("/monitor/calls/{request_id}/detail", summary="单次调用详情")
async def monitor_call_detail(request_id: str, request: Request):
    return await _proxy_platform(request, f"monitor/calls/{request_id}/detail")


@router.get("/monitor/calls/{request_id}/attempts", summary="Attempt 明细列表")
async def monitor_call_attempts(request_id: str, request: Request):
    return await _proxy_platform(request, f"monitor/calls/{request_id}/attempts")


@router.get("/monitor/meta/filters", summary="筛选元数据")
async def monitor_meta_filters(request: Request):
    return await _proxy_platform(request, "monitor/meta/filters")


# ---------------------------------------------------------------- Token 监控族（K1–K4）
# 上游路径串 = platform 侧路径去掉 `/platform` 前缀，逐字符对齐。


@router.get("/monitor/tokens/summary", summary="Token 用量统计卡")
async def monitor_token_summary(request: Request):
    return await _proxy_platform(request, "monitor/tokens/summary")


@router.get("/monitor/tokens/trend", summary="Token 用量趋势")
async def monitor_token_trend(request: Request):
    return await _proxy_platform(request, "monitor/tokens/trend")


@router.get("/monitor/tokens/models/top", summary="Token 用量模型 TOP N")
async def monitor_token_models_top(request: Request):
    return await _proxy_platform(request, "monitor/tokens/models/top")


@router.get("/monitor/tokens/usage", summary="Token 用量明细列表")
async def monitor_token_usage(request: Request):
    return await _proxy_platform(request, "monitor/tokens/usage")


# ---------------------------------------------------------------- 任务监控族（T1–T4）
# 上游路径串 = platform 侧路径去掉 `/platform` 前缀，逐字符对齐。


@router.get("/monitor/tasks/summary", summary="任务监控统计卡")
async def monitor_task_summary(request: Request):
    return await _proxy_platform(request, "monitor/tasks/summary")


@router.get("/monitor/tasks/trend", summary="任务监控趋势")
async def monitor_task_trend(request: Request):
    return await _proxy_platform(request, "monitor/tasks/trend")


@router.get("/monitor/tasks/failures", summary="失败任务列表")
async def monitor_task_failures(request: Request):
    return await _proxy_platform(request, "monitor/tasks/failures")


@router.get("/monitor/tasks/overview", summary="任务运行概览")
async def monitor_task_overview(request: Request):
    return await _proxy_platform(request, "monitor/tasks/overview")
