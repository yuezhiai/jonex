"""
MCP 服务目录 API 端点（platform 容器内部）

由 Sidecar 代理调用，不直接对外暴露。

8 个端点：
- GET /mcp-services — 领域服务列表（含 MCP 发布状态、关联 KB 数量）
- GET /mcp-services/{service_id} — 查询单个领域服务详情（含发布状态、启用状态、关联 KB）
- GET /mcp-services/{service_id}/authorized-keys — 查询领域服务已授权 Key 列表
- PUT /mcp-services/{service_id}/tool — 保存领域服务的 MCP Tool 配置
- POST /mcp-services/{service_id}/publish — 发布领域服务
- POST /mcp-services/{service_id}/unpublish — 取消发布领域服务
- POST /mcp-services/{service_id}/stop — 停用已发布服务
- POST /mcp-services/{service_id}/start — 启用已停用服务
"""
from fastapi import APIRouter, Body, Depends, Path, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.database import get_db
from jonex_core.common.response import success_response
from jonex_core.common.tenant import extract_tenant_id
from jonex_core.security.permission import require_permission
from capabilities.platform.services.mcp_service import McpServiceService
from capabilities.platform.dtos.mcp_service_dto import (
    McpServiceListRequest,
    McpServiceListResponse,
    McpServiceResponse,
    PublishRequest,
    ToolConfigRequest,
)

router = APIRouter()


# ==================== MCP 服务目录 ====================


@router.get("/mcp-services")
async def list_mcp_services(
    request: Request,
    search: str | None = Query(default=None, max_length=255),
    space_id: str | None = Query(default=None, max_length=64),
    capability_type: str | None = Query(default=None),
    source: str | None = Query(default=None),
    status: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_permission("mcp:service:view")),
):
    """E6: 领域服务列表（含 MCP 发布状态、关联 KB 数量）。

    支持 search / capability_type / source / status / space_id 筛选。
    """
    tenant_id = extract_tenant_id(request)
    req = McpServiceListRequest(
        search=search,
        space_id=space_id,
        capability_type=capability_type,
        source=source,
        status=status,
    )
    svc = McpServiceService(db)
    result = await svc.list_services(tenant_id, req)
    return success_response(
        data=result.dict(),
        message="查询成功",
    )


@router.get("/mcp-services/{service_id}")
async def get_mcp_service_detail(
    service_id: str = Path(..., description="领域服务 ID (knowledge_base.services.id)"),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_permission("mcp:service:view")),
):
    """查询单个领域服务详情。

    复用列表的跨 schema 聚合：服务基础信息 + 发布状态 + 启用状态 + 关联 KB。
    """
    tenant_id = extract_tenant_id(request)
    svc = McpServiceService(db)
    result = await svc.get_service_detail(tenant_id, service_id)
    return success_response(
        data=result.dict(),
        message="查询成功",
    )


@router.get("/mcp-services/{service_id}/authorized-keys")
async def get_authorized_keys(
    service_id: str = Path(..., description="领域服务 ID (knowledge_base.services.id)"),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_permission("mcp:service:view")),
):
    """查询领域服务已授权的 MCP Key 列表（含派生状态与有效期）。"""
    tenant_id = extract_tenant_id(request)
    svc = McpServiceService(db)
    result = await svc.get_authorized_keys(tenant_id, service_id)
    return success_response(
        data=result.dict(),
        message="查询成功",
    )


@router.put("/mcp-services/{service_id}/tool")
async def save_tool_config(
    service_id: str = Path(..., description="领域服务 ID (knowledge_base.services.id)"),
    req: ToolConfigRequest = Body(..., description="Tool 配置（tool 名称 + 描述）"),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_permission("mcp:service:manage")),
):
    """DS-03: 保存领域服务的 MCP Tool 配置。

    tool 命名仅支持英文字母、数字、下划线（DTO 层正则校验），
    同租户内唯一（Service 层校验）。
    """
    tenant_id = extract_tenant_id(request)
    svc = McpServiceService(db)
    result = await svc.save_tool_config(tenant_id, service_id, req)
    return success_response(data=result, message="Tool 配置已保存")


@router.post("/mcp-services/{service_id}/publish")
async def publish_service(
    service_id: str = Path(..., description="领域服务 ID (knowledge_base.services.id)"),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_permission("mcp:service:manage")),
):
    """E7: 发布领域服务，标记为 MCP 可见。

    验证 service 在 knowledge_base.services 中存在且属于当前租户。
    """
    tenant_id = extract_tenant_id(request)
    authorization_header = request.headers.get("Authorization")
    svc = McpServiceService(db)
    result = await svc.publish(tenant_id, service_id, authorization_header)
    return success_response(data=result, message="服务已发布")


@router.post("/mcp-services/{service_id}/unpublish")
async def unpublish_service(
    service_id: str = Path(..., description="领域服务 ID"),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_permission("mcp:service:manage")),
):
    """E8: 取消发布领域服务。

    验证 service 在 knowledge_base.services 中存在且属于当前租户。
    """
    tenant_id = extract_tenant_id(request)
    authorization_header = request.headers.get("Authorization")
    svc = McpServiceService(db)
    result = await svc.unpublish(tenant_id, service_id, authorization_header)
    return success_response(data=result, message="已取消发布")


@router.post("/mcp-services/{service_id}/stop")
async def stop_service(
    service_id: str = Path(..., description="领域服务 ID"),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_permission("mcp:service:manage")),
):
    """停用已发布服务（仅 published 态可用，未发布 → 409）。"""
    tenant_id = extract_tenant_id(request)
    authorization_header = request.headers.get("Authorization")
    svc = McpServiceService(db)
    result = await svc.stop(tenant_id, service_id, authorization_header)
    return success_response(data=result, message="服务已停用")


@router.post("/mcp-services/{service_id}/start")
async def start_service(
    service_id: str = Path(..., description="领域服务 ID"),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_permission("mcp:service:manage")),
):
    """启用已停用服务（仅 stopped 态可用，未停用 → 409）。"""
    tenant_id = extract_tenant_id(request)
    authorization_header = request.headers.get("Authorization")
    svc = McpServiceService(db)
    result = await svc.start(tenant_id, service_id, authorization_header)
    return success_response(data=result, message="服务已启用")
