"""
知识库 — 领域空间 API 路由（Sidecar 反代直连；网关业务链路走 invoke dispatch）

双入口兜底（设计 §4）：本路由解析用户后传入 service，service 层校验
（owner/service:write/role 白名单）与 dispatch 层判定同源。
"""
from fastapi import APIRouter, Depends, Query, Request

from jonex_core.common.exceptions import JonexException
from jonex_core.common.response import error_response, success_response
from jonex_core.common.tenant import extract_tenant_id
from jonex_core.security.user_auth import get_current_user

from ..services import SpaceService

router = APIRouter()
_service = SpaceService()


def _actor_user_id(current: dict) -> str | None:
    """REST 链路 user_id：JWT 真实 id 转 str；测试 token 特判 user_id=0（get_current_user 已映射）。"""
    uid = current.get("user_id")
    return str(uid) if uid is not None else None


@router.get("/spaces")
async def list_spaces(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    current: dict = Depends(get_current_user),
):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.list(tenant_id, offset, limit, user_id=_actor_user_id(current))
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.post("/spaces")
async def create_space(request: Request, current: dict = Depends(get_current_user)):
    body = await request.json()
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.create(tenant_id, body, owner_id=_actor_user_id(current))
        return success_response(data=result, message="领域空间已创建")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.get("/spaces/{space_id}")
async def get_space(space_id: str, request: Request, current: dict = Depends(get_current_user)):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.get(space_id, tenant_id, user_id=_actor_user_id(current))
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.patch("/spaces/{space_id}")
async def update_space(space_id: str, request: Request, current: dict = Depends(get_current_user)):
    body = await request.json()
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.update(space_id, tenant_id, body, user_id=_actor_user_id(current))
        return success_response(data=result, message="领域空间已更新")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.delete("/spaces/{space_id}")
async def delete_space(space_id: str, request: Request, current: dict = Depends(get_current_user)):
    tenant_id = extract_tenant_id(request)
    try:
        await _service.delete(space_id, tenant_id, user_id=_actor_user_id(current))
        return success_response(message="领域空间已删除")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.get("/spaces/{space_id}/permissions")
async def get_space_permissions(
    space_id: str, request: Request, current: dict = Depends(get_current_user)
):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.get_permissions(space_id, tenant_id, user_id=_actor_user_id(current))
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.put("/spaces/{space_id}/permissions")
async def set_space_permissions(
    space_id: str, request: Request, current: dict = Depends(get_current_user)
):
    body = await request.json()
    tenant_id = extract_tenant_id(request)
    try:
        await _service.set_permissions(
            space_id, tenant_id, body.get("permissions", []), user_id=_actor_user_id(current)
        )
        return success_response(message="权限已更新")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.get("/spaces/{space_id}/permission-candidates", summary="领域空间添加成员候选用户")
async def get_space_permission_candidates(
    space_id: str, request: Request, current: dict = Depends(get_current_user)
):
    """双入口兜底：REST 解析用户后传入 service；invoke 走 dispatch 判定。"""
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.get_permission_candidates(
            space_id, tenant_id, user_id=_actor_user_id(current)
        )
        return success_response(data={"candidates": result})
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)
