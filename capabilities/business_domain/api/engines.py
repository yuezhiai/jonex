"""
业务领域 — 引擎管理 API 路由
"""
from fastapi import APIRouter, Query, Request

from jonex_core.common.response import success_response, error_response
from jonex_core.common.exceptions import JonexException, PermissionDeniedError
from jonex_core.common.i18n import translate
from jonex_core.security.permission import has_permission
from jonex_core.security.user_auth import get_current_user

from ..services import EngineService

router = APIRouter()
_service = EngineService()


async def _require_engine_write(request: Request) -> None:
    """引擎目录写守卫：engine:write 权限（含测试 token 兜底，与 require_permission 口径一致）。"""
    current = await get_current_user(authorization=request.headers.get("Authorization", ""))
    if current.get("user_id") == 0 and current.get("role") == "admin":
        return  # 测试 token 兜底
    if not await has_permission(current["tenant_id"], current["user_id"], "engine:write"):
        raise PermissionDeniedError(
            message=translate("err.auth.insufficient_permission", params={"required": "engine:write"}, fallback="权限不足：需要权限 engine:write")
        )


# ── 数据接入方式（平台共享：全体租户可读，写需 engine:write 权限） ──

@router.get("/access-methods")
async def list_access_methods(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    try:
        result = await _service.list_access_methods(offset, limit)
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.post("/access-methods")
async def create_access_method(request: Request):
    body = await request.json()
    try:
        await _require_engine_write(request)
        result = await _service.create_access_method(body)
        return success_response(data=result, message="数据接入方式已创建")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.patch("/access-methods/{method_id}")
async def update_access_method(method_id: str, request: Request):
    body = await request.json()
    try:
        await _require_engine_write(request)
        result = await _service.update_access_method(method_id, body)
        return success_response(data=result, message="数据接入方式已更新")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


# ── 解析器配置（平台共享：全体租户可读，写需 engine:write 权限） ──

@router.get("/parsers")
async def list_parsers(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    try:
        result = await _service.list_parsers(offset, limit)
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.post("/parsers")
async def create_parser(request: Request):
    body = await request.json()
    try:
        await _require_engine_write(request)
        result = await _service.create_parser(body)
        return success_response(data=result, message="解析器已创建")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.patch("/parsers/{parser_id}")
async def update_parser(parser_id: str, request: Request):
    body = await request.json()
    try:
        await _require_engine_write(request)
        result = await _service.update_parser(parser_id, body)
        return success_response(data=result, message="解析器已更新")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


# ── 模型供应商（平台共享：全体租户可读，写需 engine:write 权限） ──

@router.get("/providers")
async def list_providers(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    try:
        result = await _service.list_providers(offset, limit)
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.post("/providers")
async def create_provider(request: Request):
    body = await request.json()
    try:
        await _require_engine_write(request)
        result = await _service.create_provider(body)
        return success_response(data=result, message="模型供应商已创建")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.patch("/providers/{provider_id}")
async def update_provider(provider_id: str, request: Request):
    body = await request.json()
    try:
        await _require_engine_write(request)
        result = await _service.update_provider(provider_id, body)
        return success_response(data=result, message="模型供应商已更新")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.post("/providers/{provider_id}/test")
async def test_provider(provider_id: str, request: Request):
    try:
        await _require_engine_write(request)
        result = await _service.test_provider(provider_id)
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)
