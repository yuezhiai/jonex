"""
MCP 服务目录 API 端点（platform 容器内部）

由 Sidecar 代理调用，不直接对外暴露。

6 个端点覆盖 E6-E11 需求：
- E6: GET /mcp-services — 领域服务列表（含 MCP 发布状态、关联 KB 数量）
- E7: POST /mcp-services/{id}/publish — 发布领域服务
- E8: POST /mcp-services/{id}/unpublish — 取消发布领域服务
- E9: POST /mcp-services/{id}/test-call — 测试调用（模拟 RAG 回答）
- E10: GET /mcp-services/{id}/authorized-keys — 查看已授权 Key 列表
- E11: POST /mcp-services/sync — 手动同步知识库服务

补充：
- GET /mcp-services/{id} — 查询单个领域服务详情（含发布状态、启用状态、关联 KB）
"""
from fastapi import APIRouter, Body, Depends, Path, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.database import get_db
from jonex_core.common.response import success_response
from jonex_core.common.tenant import extract_tenant_id
from jonex_core.common import require_admin
from capabilities.platform.services.mcp_service import McpServiceService
from capabilities.platform.dtos.mcp_service_dto import (
    AuthorizedKeyListResponse,
    AuthorizedKeyResponse,
    McpServiceListRequest,
    McpServiceListResponse,
    McpServiceResponse,
    PublishRequest,
    ServiceKeyAddRequest,
    ServiceKeyCreateRequest,
    ServiceKeyPermissionUpdateRequest,
    TestCallRequest,
    TestCallResponse,
)

router = APIRouter()

# IMPORTANT: POST /mcp-services/sync 必须在 POST /mcp-services/{service_id}/publish
# 之前声明，以避免 FastAPI 将 "sync" 捕获为 {service_id} 路径参数。


# ==================== MCP 服务目录 ====================


@router.get("/mcp-services")
async def list_mcp_services(
    request: Request,
    search: str | None = Query(default=None, max_length=255),
    space_id: str | None = Query(default=None, max_length=64),
    status: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_admin),
):
    """E6: 领域服务列表（含 MCP 发布状态、关联 KB 数量）。

    支持 search / space_id / status 筛选。
    """
    tenant_id = extract_tenant_id(request)
    req = McpServiceListRequest(search=search, space_id=space_id, status=status)
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
    _admin: dict = Depends(require_admin),
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


@router.post("/mcp-services/sync")
async def sync_mcp_services(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_admin),
):
    """E11: 手动从 knowledge_base 同步领域服务列表到 platform 发布表。

    为缺失的服务创建初始发布记录（is_published=0）。
    """
    tenant_id = extract_tenant_id(request)
    authorization_header = request.headers.get("Authorization")
    svc = McpServiceService(db)
    result = await svc.sync(tenant_id, authorization_header)
    return success_response(
        data=result,
        message=f"同步完成：{result['synced_count']} 条记录",
    )


# ==================== MCP 服务配置（从领域服务视角管理 Key） ====================


@router.post("/mcp-services/{service_id}/keys")
async def add_key_to_service(
    service_id: str = Path(..., description="领域服务 ID"),
    req: ServiceKeyAddRequest = Body(..., description="Key ID 和权限级别"),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_admin),
):
    """新增: 将已有 MCP Key 添加到领域服务授权列表"""
    tenant_id = extract_tenant_id(request)
    svc = McpServiceService(db)
    result = await svc.add_key_to_service(tenant_id, service_id, req)
    return success_response(data=result, message="已添加授权")


@router.post("/mcp-services/{service_id}/keys/new")
async def create_key_for_service(
    service_id: str = Path(..., description="领域服务 ID"),
    req: ServiceKeyCreateRequest = Body(..., description="新 Key 参数"),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_admin),
):
    """新增: 创建新 MCP Key 并自动关联到领域服务"""
    tenant_id = extract_tenant_id(request)
    authorization_header = request.headers.get("Authorization")
    svc = McpServiceService(db)
    result = await svc.create_key_for_service(
        tenant_id, service_id, req, authorization_header,
    )
    return success_response(data=result, message="MCP Key 创建成功")


@router.patch("/mcp-services/{service_id}/keys/{key_id}")
async def update_key_permission(
    service_id: str = Path(..., description="领域服务 ID"),
    key_id: str = Path(..., description="MCP Key ID"),
    req: ServiceKeyPermissionUpdateRequest = Body(...),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_admin),
):
    """新增: 切换 Key 对领域服务的权限级别"""
    tenant_id = extract_tenant_id(request)
    svc = McpServiceService(db)
    result = await svc.update_key_permission(
        tenant_id, service_id, key_id, req.permission_level,
    )
    return success_response(data=result, message="权限已更新")


@router.delete("/mcp-services/{service_id}/keys/{key_id}")
async def remove_key_from_service(
    service_id: str = Path(..., description="领域服务 ID"),
    key_id: str = Path(..., description="MCP Key ID"),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_admin),
):
    """新增: 移除 Key 对领域服务的授权"""
    tenant_id = extract_tenant_id(request)
    svc = McpServiceService(db)
    result = await svc.remove_key_from_service(tenant_id, service_id, key_id)
    return success_response(data=result, message="已移除授权")


@router.post("/mcp-services/{service_id}/publish")
async def publish_service(
    service_id: str = Path(..., description="领域服务 ID (knowledge_base.services.id)"),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_admin),
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
    _admin: dict = Depends(require_admin),
):
    """E8: 取消发布领域服务。

    验证 service 在 knowledge_base.services 中存在且属于当前租户。
    """
    tenant_id = extract_tenant_id(request)
    authorization_header = request.headers.get("Authorization")
    svc = McpServiceService(db)
    result = await svc.unpublish(tenant_id, service_id, authorization_header)
    return success_response(data=result, message="已取消发布")


@router.post("/mcp-services/{service_id}/test-call")
async def test_call_service(
    service_id: str = Path(..., description="领域服务 ID"),
    req: TestCallRequest = Body(..., description="测试调用参数"),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_admin),
):
    """E9: 测试调用领域服务（一期模拟 RAG 回答）。

    不进行真实的 Sidecar invoke 调用，直接返回模拟回答。
    """
    tenant_id = extract_tenant_id(request)
    svc = McpServiceService(db)
    result = await svc.test_call(tenant_id, service_id, req)
    return success_response(
        data=result.dict(),
        message="测试调用完成（模拟）",
    )


@router.get("/mcp-services/{service_id}/authorized-keys")
async def get_authorized_keys(
    service_id: str = Path(..., description="领域服务 ID"),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_admin),
):
    """E10: 查看某服务的已授权 MCP Key 列表。

    返回脱敏后的 Key 数据，仅显示 key_prefix，不暴露 key_hash 或明文。
    """
    tenant_id = extract_tenant_id(request)
    svc = McpServiceService(db)
    result = await svc.get_authorized_keys(tenant_id, service_id)
    return success_response(
        data=result.dict(),
        message="查询成功",
    )
