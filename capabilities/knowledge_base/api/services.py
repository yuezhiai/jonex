"""
知识库能力 — 领域服务 API 路由
"""
from fastapi import APIRouter, Query, Request

from jonex_core.common.response import success_response, error_response
from jonex_core.common.exceptions import JonexException
from jonex_core.common.tenant import extract_tenant_id

from ..services import DomainServiceService

router = APIRouter()
_service = DomainServiceService()


@router.get("/domain-services")
async def list_services(
    request: Request,
    space_id: str = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.list(tenant_id, space_id, offset, limit)
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.post("/domain-services")
async def create_service(request: Request):
    body = await request.json()
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.create(tenant_id, body)
        return success_response(data=result, message="领域服务已创建")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.get("/domain-services/{service_id}")
async def get_service(service_id: str, request: Request):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.get(service_id, tenant_id)
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.patch("/domain-services/{service_id}")
async def update_service(service_id: str, request: Request):
    body = await request.json()
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.update(service_id, tenant_id, body)
        return success_response(data=result, message="领域服务已更新")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.delete("/domain-services/{service_id}")
async def delete_service(service_id: str, request: Request):
    tenant_id = extract_tenant_id(request)
    try:
        await _service.delete(service_id, tenant_id)
        return success_response(message="领域服务已删除")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.post("/domain-services/{service_id}/enable")
async def enable_service(service_id: str, request: Request):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.enable(service_id, tenant_id)
        return success_response(data=result, message="领域服务已启用")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.post("/domain-services/{service_id}/disable")
async def disable_service(service_id: str, request: Request):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.disable(service_id, tenant_id)
        return success_response(data=result, message="领域服务已停用")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.post("/domain-services/{service_id}/rotate-api-key")
async def rotate_service_api_key(service_id: str, request: Request):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.rotate_api_key(service_id, tenant_id)
        return success_response(data=result, message="API Key 已轮换")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.get("/domain-services/{service_id}/configs")
async def get_service_configs(service_id: str, request: Request):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.get_configs(service_id, tenant_id)
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.put("/domain-services/{service_id}/configs")
async def update_service_configs(service_id: str, request: Request):
    body = await request.json()
    tenant_id = extract_tenant_id(request)
    try:
        await _service.update_configs(service_id, tenant_id, body.get("configs", {}))
        return success_response(message="服务配置已更新")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.get("/domain-services/{service_id}/api-keys", summary="获取 API Key 列表")
async def list_service_api_keys(service_id: str, request: Request):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.list_api_keys(service_id, tenant_id)
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.post("/domain-services/{service_id}/api-keys", summary="创建 API Key")
async def create_service_api_key(service_id: str, request: Request):
    body = await request.json()
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.create_api_key(service_id, tenant_id, body)
        return success_response(data=result, message="API Key 已创建")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.delete("/domain-services/{service_id}/api-keys/{key_id}", summary="删除 API Key")
async def delete_service_api_key(service_id: str, key_id: str, request: Request):
    tenant_id = extract_tenant_id(request)
    try:
        await _service.delete_api_key(key_id, service_id, tenant_id)
        return success_response(message="API Key 已删除")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.get("/domain-services/{service_id}/search", summary="搜索领域服务")
async def search_service(
    request: Request,
    service_id: str,
    query: str = Query(..., description="搜索关键词"),
):
    """在指定领域服务中搜索"""
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.search(service_id, tenant_id, query)
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)
