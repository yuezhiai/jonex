"""
知识写入 Key 管理 API 端点（platform 容器内部）

由 Sidecar 代理调用（REST 透传），不直接对外暴露。
最终路径 /api/v1/platform/mcp-write-keys（capability.py 前缀 /api/v1）。

6 个端点（WRITE-01/02）：创建（一次性返回明文，HTTP 201）、列表、详情、
编辑、停用/启用（可逆）、撤销（不可逆）。
"""
import re

from fastapi import APIRouter, Body, Depends, Path, Request
from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.security.permission import require_permission
from jonex_core.common.database import get_db
from jonex_core.common.exceptions import InvalidParameterError
from jonex_core.common.i18n import translate
from jonex_core.common.response import success_response
from jonex_core.common.tenant import extract_tenant_id

from capabilities.platform.dtos.mcp_write_key_dto import (
    WriteKeyCreateRequest,
    WriteKeyUpdateRequest,
)
from capabilities.platform.services.mcp_write_key_service import McpWriteKeyService

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


# ==================== 知识写入 Key CRUD ====================


@router.post("/mcp-write-keys")
async def create_mcp_write_key(
    request: Request,
    req: WriteKeyCreateRequest = Body(...),
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_permission("mcp:write")),
):
    """创建知识写入 Key，一次性返回明文 mcpw_... Key（HTTP 201）。

    明文 Key 仅在此响应中出现一次，后续无法通过任何 API 再次获取。
    """
    tenant_id = extract_tenant_id(request)
    authorization_header = request.headers.get("Authorization")
    svc = McpWriteKeyService(db)
    result = await svc.create(tenant_id, req, authorization_header)
    return success_response(
        data=result.dict(),
        message="知识写入 Key 已创建",
        status_code=201,
    )


@router.get("/mcp-write-keys")
async def list_mcp_write_keys(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_permission("mcp:read")),
):
    """获取租户下所有知识写入 Key 元数据（含已撤销，脱敏）。"""
    tenant_id = extract_tenant_id(request)
    svc = McpWriteKeyService(db)
    items = await svc.list_all(tenant_id)
    return success_response(
        data={"items": [i.dict() for i in items], "total": len(items)},
        message="查询成功",
    )


@router.get("/mcp-write-keys/{key_id}")
async def get_mcp_write_key(
    request: Request,
    key_id: str = Path(..., description="知识写入 Key ID (32 字符 hex)"),
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_permission("mcp:read")),
):
    """获取单个知识写入 Key 详细信息（脱敏）。"""
    _validate_key_id(key_id)
    tenant_id = extract_tenant_id(request)
    svc = McpWriteKeyService(db)
    result = await svc.get_by_id(tenant_id, key_id)
    return success_response(data=result.dict(), message="查询成功")


@router.put("/mcp-write-keys/{key_id}")
async def update_mcp_write_key(
    request: Request,
    key_id: str = Path(..., description="知识写入 Key ID (32 字符 hex)"),
    req: WriteKeyUpdateRequest = Body(..., description="编辑字段——所有字段可选"),
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_permission("mcp:write")),
):
    """编辑知识写入 Key 元数据（不重置 Key，不生成新明文）。

    传入字段覆盖原值，未传入字段保持原值。
    """
    _validate_key_id(key_id)
    tenant_id = extract_tenant_id(request)
    authorization_header = request.headers.get("Authorization")
    svc = McpWriteKeyService(db)
    result = await svc.update(tenant_id, key_id, req, authorization_header)
    return success_response(data=result.dict(), message="知识写入 Key 已更新")


@router.post("/mcp-write-keys/{key_id}/toggle")
async def toggle_mcp_write_key(
    request: Request,
    key_id: str = Path(..., description="知识写入 Key ID (32 字符 hex)"),
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_permission("mcp:write")),
):
    """停用/启用知识写入 Key（可逆，key 不变）。

    已撤销 Key 调 toggle → 409 ResourceConflictError。
    """
    _validate_key_id(key_id)
    tenant_id = extract_tenant_id(request)
    authorization_header = request.headers.get("Authorization")
    svc = McpWriteKeyService(db)
    result = await svc.toggle(tenant_id, key_id, authorization_header)
    return success_response(data=result.dict(), message="知识写入 Key 状态已切换")


@router.post("/mcp-write-keys/{key_id}/revoke")
async def revoke_mcp_write_key(
    request: Request,
    key_id: str = Path(..., description="知识写入 Key ID (32 字符 hex)"),
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_permission("mcp:write")),
):
    """撤销知识写入 Key（设置 revoked_at，不可逆）。"""
    _validate_key_id(key_id)
    tenant_id = extract_tenant_id(request)
    authorization_header = request.headers.get("Authorization")
    svc = McpWriteKeyService(db)
    await svc.revoke(tenant_id, key_id, authorization_header)
    return success_response(message="知识写入 Key 已撤销")
