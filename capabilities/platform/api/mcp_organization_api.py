"""
MCP 组织管理 API 端点（platform 容器内部）

由 Sidecar 代理调用，不直接对外暴露。
"""
from fastapi import APIRouter, Body, Depends, Path, Request
from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.database import get_db
from jonex_core.common.response import success_response
from jonex_core.common.tenant import extract_tenant_id
from jonex_core.common import require_admin
from capabilities.platform.dtos.mcp_key_dto import (
    McpOrganizationCreateRequest,
    McpOrganizationListResponse,
    McpOrganizationUpdateRequest,
)
from capabilities.platform.services.mcp_organization_service import McpOrganizationService

router = APIRouter()


# ==================== MCP 组织 CRUD ====================


@router.post("/mcp-organizations")
async def create_organization(
    request: Request,
    req: McpOrganizationCreateRequest,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_admin),
):
    """创建 MCP 组织。

    name 自动 strip，空字符串被拒绝。同租户下 name 唯一。
    """
    tenant_id = extract_tenant_id(request)
    authorization_header = request.headers.get("Authorization")
    svc = McpOrganizationService(db)
    result = await svc.create(tenant_id, req, authorization_header)
    return success_response(
        data=result.dict(),
        message="组织已创建",
    )


@router.get("/mcp-organizations")
async def list_organizations(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_admin),
):
    """获取租户下所有 MCP 组织，按创建时间降序排列。

    含 total 计数。
    """
    tenant_id = extract_tenant_id(request)
    svc = McpOrganizationService(db)
    orgs = await svc.list_all(tenant_id)
    items = [o.dict() for o in orgs]
    return success_response(
        data=McpOrganizationListResponse(items=items, total=len(items)).dict(),
        message="查询成功",
    )


@router.put("/mcp-organizations/{org_id}")
async def update_organization(
    org_id: str = Path(..., description="组织 ID"),
    req: McpOrganizationUpdateRequest = Body(..., description="编辑字段——所有字段可选"),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_admin),
):
    """编辑 MCP 组织名称或描述。

    传入字段覆盖原值，未传入字段保持原值。
    """
    tenant_id = extract_tenant_id(request)
    authorization_header = request.headers.get("Authorization")
    svc = McpOrganizationService(db)
    result = await svc.update(tenant_id, org_id, req, authorization_header)
    return success_response(
        data=result.dict(),
        message="组织已更新",
    )


@router.delete("/mcp-organizations/{org_id}")
async def delete_organization(
    org_id: str = Path(..., description="组织 ID"),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_admin),
):
    """软删除 MCP 组织（设置 is_deleted=1）。

    幂等——对已删除组织再次调用不报错。
    """
    tenant_id = extract_tenant_id(request)
    authorization_header = request.headers.get("Authorization")
    svc = McpOrganizationService(db)
    await svc.delete(tenant_id, org_id, authorization_header)
    return success_response(message="组织已删除")
