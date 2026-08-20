"""
MCP Key 管理 API 端点（platform 容器内部）

由 Sidecar 代理调用，不直接对外暴露。
"""
import re

from fastapi import APIRouter, Body, Depends, Path, Request
from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.database import get_db
from jonex_core.common.exceptions import InvalidParameterError
from jonex_core.common.i18n import translate
from jonex_core.common.response import success_response
from jonex_core.common.tenant import extract_tenant_id
from jonex_core.security.permission import require_permission
from capabilities.platform.dtos.mcp_key_dto import (
    McpKeyCreateRequest,
    McpKeyCreateResponse,
    McpKeyListResponse,
    McpKeyRecreateRequest,
    McpKeyResetRequest,
    McpKeyUpdateRequest,
)
from capabilities.platform.services.mcp_key_service import McpKeyService

router = APIRouter()

KEY_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def _validate_key_id(key_id: str) -> str:
    """校验 key_id 为 32 字符 hex 字符串（UUID4 hex 格式）。"""
    if not KEY_ID_PATTERN.match(key_id):
        raise InvalidParameterError(
            message=translate(
                "err.invalid_parameter",
                fallback=f"无效的 key_id 格式: {key_id}，需为 32 字符 hex 字符串",
            ),
            details={"key_id": key_id},
        )
    return key_id


# ==================== MCP Key CRUD ====================


@router.post("/mcp-keys")
async def create_mcp_key(
    request: Request,
    req: McpKeyCreateRequest,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_permission("mcp:write")),
):
    """创建 MCP Key，一次性返回明文 yxm_... Key。

    明文 Key 仅在此响应中出现一次，后续无法通过任何 API 再次获取。
    """
    tenant_id = extract_tenant_id(request)
    authorization_header = request.headers.get("Authorization")
    svc = McpKeyService(db)
    result = await svc.create(tenant_id, req, authorization_header)
    return success_response(
        data=McpKeyCreateResponse(**result).dict(),
        message="MCP Key 已创建",
    )


@router.get("/mcp-keys")
async def list_mcp_keys(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_permission("mcp:read")),
):
    """获取租户下所有 MCP Key 元数据（含已撤销）。

    响应不含 key_hash 和明文 Key。
    """
    tenant_id = extract_tenant_id(request)
    svc = McpKeyService(db)
    keys = await svc.list_all(tenant_id)
    items = [key.dict() for key in keys]
    return success_response(
        data=McpKeyListResponse(items=items, total=len(items)).dict(),
        message="查询成功",
    )


@router.get("/mcp-keys/{key_id}")
async def get_mcp_key(
    key_id: str = Path(..., description="MCP Key ID (32 字符 hex)"),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_permission("mcp:read")),
):
    """获取单个 MCP Key 的详细信息。

    已撤销 Key 仍可查看元数据（含 revoked_at 字段）。
    """
    _validate_key_id(key_id)
    tenant_id = extract_tenant_id(request)
    svc = McpKeyService(db)
    key_dto = await svc.get_by_id(tenant_id, key_id)
    return success_response(
        data=key_dto.dict(),
        message="查询成功",
    )


@router.put("/mcp-keys/{key_id}")
async def update_mcp_key(
    key_id: str = Path(..., description="MCP Key ID (32 字符 hex)"),
    req: McpKeyUpdateRequest = Body(..., description="编辑字段——所有字段可选"),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_permission("mcp:write")),
):
    """编辑 MCP Key 元数据（不重置 Key，不生成新明文）。

    传入字段覆盖原值，未传入字段保持原值。
    支持更新 name、permissions、allowed_kb_ids、service_ids。
    """
    _validate_key_id(key_id)
    tenant_id = extract_tenant_id(request)
    authorization_header = request.headers.get("Authorization")
    svc = McpKeyService(db)
    result = await svc.update(tenant_id, key_id, req, authorization_header)
    return success_response(
        data=result.dict(),
        message="MCP Key 已更新",
    )


@router.post("/mcp-keys/{key_id}/revoke")
async def revoke_mcp_key(
    key_id: str = Path(..., description="MCP Key ID (32 字符 hex)"),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_permission("mcp:write")),
):
    """撤销 MCP Key（设置 revoked_at）。

    撤销后 Phase 2 MCP Server 中间件将拒绝该 Key。
    """
    _validate_key_id(key_id)
    tenant_id = extract_tenant_id(request)
    authorization_header = request.headers.get("Authorization")
    svc = McpKeyService(db)
    await svc.revoke(tenant_id, key_id, authorization_header)
    return success_response(message="MCP Key 已撤销")


@router.delete("/mcp-keys/{key_id}")
async def delete_mcp_key(
    key_id: str = Path(..., description="MCP Key ID (32 字符 hex)"),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_permission("mcp:write")),
):
    """软删除 MCP Key（设置 is_deleted=1）。

    删除后 Key 不可再用于认证，list/get API 自动不可见。
    对已删除 Key 再次调用不报错（幂等）。
    """
    _validate_key_id(key_id)
    tenant_id = extract_tenant_id(request)
    authorization_header = request.headers.get("Authorization")
    svc = McpKeyService(db)
    await svc.delete(tenant_id, key_id, authorization_header)
    return success_response(message="MCP Key 已删除")


@router.post("/mcp-keys/{key_id}/reset")
async def reset_mcp_key(
    key_id: str = Path(..., description="MCP Key ID (32 字符 hex)"),
    request: Request = None,
    body: McpKeyResetRequest | None = Body(
        None, description="可选：覆盖新 Key 的 name/permissions/allowed_kb_ids"
    ),
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_permission("mcp:write")),
):
    """重置 MCP Key：撤销旧 Key + 生成新 Key，返回新明文。

    不传 body 时继承旧 Key 的 name/permissions/allowed_kb_ids。
    body 中传入的字段覆盖旧 Key 对应字段。
    """
    _validate_key_id(key_id)
    tenant_id = extract_tenant_id(request)
    authorization_header = request.headers.get("Authorization")

    name = body.name if body else None
    space_id = body.space_id if body else None
    permissions = body.permissions if body else None
    allowed_kb_ids = body.allowed_kb_ids if body else None
    service_ids = body.service_ids if body else None
    service_permissions = body.service_permissions if body else None
    expires_at = body.expires_at if body else None

    svc = McpKeyService(db)
    result = await svc.reset(
        tenant_id, key_id, authorization_header,
        name=name,
        space_id=space_id,
        permissions=permissions,
        allowed_kb_ids=allowed_kb_ids,
        service_ids=service_ids,
        service_permissions=service_permissions,
        expires_at=expires_at,
    )
    return success_response(
        data=McpKeyCreateResponse(**result).dict(),
        message="MCP Key 已重置",
    )


@router.post("/mcp-services/{service_id}/keys/{key_id}/toggle")
async def toggle_mcp_key(
    service_id: str = Path(..., description="领域服务 ID（契约兼容，不参与查询）"),
    key_id: str = Path(..., description="MCP Key ID (32 字符 hex)"),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_permission("mcp:write")),
):
    """停用/启用 MCP Key（可逆，恢复原 Key，非 reset 换新）。

    已撤销 Key 调 toggle 返回 409 ResourceConflictError。
    key_id 唯一定位，跨租户由 require_tenant + get_by_id 的 tenant 过滤保证。
    """
    _validate_key_id(key_id)
    tenant_id = extract_tenant_id(request)
    authorization_header = request.headers.get("Authorization")
    svc = McpKeyService(db)
    result = await svc.toggle(tenant_id, key_id, authorization_header)
    return success_response(
        data=result.dict(),
        message="MCP Key 状态已切换",
    )


@router.post("/mcp-services/{service_id}/keys/{key_id}/recreate")
async def recreate_mcp_key(
    service_id: str = Path(..., description="领域服务 ID（契约兼容，不参与查询）"),
    key_id: str = Path(..., description="MCP Key ID (32 字符 hex)"),
    request: Request = None,
    body: McpKeyRecreateRequest | None = Body(
        None, description="可选：覆盖新 Key 的 expires_at"
    ),
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_permission("mcp:write")),
):
    """重新创建 MCP Key：继承授权 + 生成新明文 Key，旧 Key 保留历史（不撤销）。

    区别于 toggle「启用」（恢复原 Key），recreate 生成全新 Key。
    """
    _validate_key_id(key_id)
    tenant_id = extract_tenant_id(request)
    authorization_header = request.headers.get("Authorization")
    expires_at = body.expires_at if body else None
    svc = McpKeyService(db)
    result = await svc.recreate(
        tenant_id, key_id, authorization_header, expires_at=expires_at
    )
    return success_response(
        data=McpKeyCreateResponse(**result).dict(),
        message="MCP Key 已重新创建",
    )
