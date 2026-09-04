"""
业务领域 — 业务模板 API 路由
"""
from fastapi import APIRouter, Body, Query, Request

from jonex_core.common.response import success_response, error_response
from jonex_core.common.exceptions import JonexException
from jonex_core.common.tenant import extract_tenant_id

from ..dtos import (
    CompilePreviewResponse,
    ImpactedKBResponse,
    PublishScenarioRequest,
    TemplateConstraintCreateRequest,
    TemplateConstraintUpdateRequest,
    TemplateDomainCreateRequest,
    TemplateDomainUpdateRequest,
    TemplateObjectCreateRequest,
    TemplateObjectUpdateRequest,
    TemplateRelationCreateRequest,
    TemplateRelationUpdateRequest,
    TemplateScenarioCreateRequest,
    TemplateScenarioUpdateRequest,
)
from ..services import TemplateService
from ..services.template_publish_service import TemplatePublishService

router = APIRouter()
_service = TemplateService()
_publish_service = TemplatePublishService()


# ── 模板领域 ──

@router.get("/templates/domains")
async def list_template_domains(
    request: Request,
    status: str | None = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.list_domains(tenant_id, offset, limit, status=status)
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.post("/templates/domains")
async def create_template_domain(request: Request, payload: TemplateDomainCreateRequest):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.create_domain(tenant_id, payload.dict(exclude_none=True))
        return success_response(data=result, message="模板领域已创建")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.get("/templates/domains/{domain_id}")
async def get_template_domain(domain_id: str, request: Request):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.get_domain(domain_id, tenant_id)
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.patch("/templates/domains/{domain_id}")
async def update_template_domain(domain_id: str, request: Request, payload: TemplateDomainUpdateRequest):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.update_domain(domain_id, tenant_id, payload.dict(exclude_unset=True))
        return success_response(data=result, message="模板领域已更新")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.delete("/templates/domains/{domain_id}")
async def delete_template_domain(domain_id: str, request: Request):
    tenant_id = extract_tenant_id(request)
    try:
        await _service.delete_domain(domain_id, tenant_id)
        return success_response(message="模板领域已删除")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


# ── 模板场景 ──

@router.get("/templates/scenarios")
async def list_template_scenarios(
    request: Request,
    domain_id: str | None = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.list_scenarios(tenant_id, domain_id, offset, limit)
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.post("/templates/scenarios")
async def create_template_scenario(request: Request, payload: TemplateScenarioCreateRequest):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.create_scenario(tenant_id, payload.dict(exclude_none=True))
        return success_response(data=result, message="模板场景已创建")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.get("/templates/scenarios/{scenario_id}")
async def get_template_scenario(scenario_id: str, request: Request):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.get_scenario(scenario_id, tenant_id)
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.patch("/templates/scenarios/{scenario_id}")
async def update_template_scenario(scenario_id: str, request: Request, payload: TemplateScenarioUpdateRequest):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.update_scenario(scenario_id, tenant_id, payload.dict(exclude_unset=True))
        return success_response(data=result, message="模板场景已更新")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.delete("/templates/scenarios/{scenario_id}")
async def delete_template_scenario(scenario_id: str, request: Request):
    tenant_id = extract_tenant_id(request)
    try:
        await _service.delete_scenario(scenario_id, tenant_id)
        return success_response(message="模板场景已删除")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


# ── 模板对象 ──

@router.get("/templates/scenarios/{scenario_id}/objects")
async def list_template_objects(
    request: Request,
    scenario_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.list_objects(tenant_id, scenario_id, offset, limit)
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.post("/templates/scenarios/{scenario_id}/objects")
async def create_template_object(scenario_id: str, request: Request, payload: TemplateObjectCreateRequest):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.create_object(tenant_id, scenario_id, payload.dict(exclude_none=True))
        return success_response(data=result, message="模板对象已创建")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.patch("/templates/objects/{object_id}")
async def update_template_object(object_id: str, request: Request, payload: TemplateObjectUpdateRequest):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.update_object(object_id, tenant_id, payload.dict(exclude_unset=True))
        return success_response(data=result, message="模板对象已更新")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.delete("/templates/objects/{object_id}")
async def delete_template_object(object_id: str, request: Request):
    tenant_id = extract_tenant_id(request)
    try:
        await _service.delete_object(object_id, tenant_id)
        return success_response(message="模板对象已删除")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


# ── 模板关系 ──

@router.get("/templates/scenarios/{scenario_id}/relations")
async def list_template_relations(
    request: Request,
    scenario_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.list_relations(tenant_id, scenario_id, offset, limit)
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.post("/templates/scenarios/{scenario_id}/relations")
async def create_template_relation(scenario_id: str, request: Request, payload: TemplateRelationCreateRequest):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.create_relation(tenant_id, scenario_id, payload.dict(exclude_none=True))
        return success_response(data=result, message="模板关系已创建")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.patch("/templates/relations/{relation_id}")
async def update_template_relation(relation_id: str, request: Request, payload: TemplateRelationUpdateRequest):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.update_relation(relation_id, tenant_id, payload.dict(exclude_unset=True))
        return success_response(data=result, message="模板关系已更新")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.delete("/templates/relations/{relation_id}")
async def delete_template_relation(relation_id: str, request: Request):
    tenant_id = extract_tenant_id(request)
    try:
        await _service.delete_relation(relation_id, tenant_id)
        return success_response(message="模板关系已删除")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


# ── 模板约束 ──────────────────────────────────────────


@router.get("/templates/scenarios/{scenario_id}/constraints")
async def list_template_constraints(
    scenario_id: str,
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _service.list_constraints(tenant_id, scenario_id, offset, limit)
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.post("/templates/scenarios/{scenario_id}/constraints")
async def create_template_constraint(
    scenario_id: str,
    request: Request,
    payload: TemplateConstraintCreateRequest = Body(...),
):
    tenant_id = extract_tenant_id(request)
    try:
        data = payload.dict(exclude_none=True)
        result = await _service.create_constraint(tenant_id, scenario_id, data)
        return success_response(data=result, message="约束已创建")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.patch("/templates/constraints/{constraint_id}")
async def update_template_constraint(
    constraint_id: str,
    request: Request,
    payload: TemplateConstraintUpdateRequest = Body(...),
):
    tenant_id = extract_tenant_id(request)
    try:
        data = payload.dict(exclude_none=True)
        result = await _service.update_constraint(constraint_id, tenant_id, data)
        return success_response(data=result, message="约束已更新")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.delete("/templates/constraints/{constraint_id}")
async def delete_template_constraint(
    constraint_id: str,
    request: Request,
):
    tenant_id = extract_tenant_id(request)
    try:
        await _service.delete_constraint(constraint_id, tenant_id)
        return success_response(message="约束已删除")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


# ── 模板发布/编译预览/影响范围 ──────────────────────────


@router.post("/templates/scenarios/{scenario_id}/publish", summary="发布模板场景")
async def publish_template_scenario(
    scenario_id: str,
    request: Request,
    payload: PublishScenarioRequest = Body(...),
):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _publish_service.publish_scenario(tenant_id, scenario_id)
        return success_response(data=result, message="模板场景已发布")
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.get("/templates/scenarios/{scenario_id}/compile-preview", summary="预览 compiled schema")
async def preview_compiled_schema(
    scenario_id: str,
    request: Request,
):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _publish_service.compile_preview(tenant_id, scenario_id)
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)


@router.get("/templates/scenarios/{scenario_id}/impacted-kbs", summary="查询模板变更影响 KB")
async def list_impacted_knowledge_bases(
    scenario_id: str,
    request: Request,
):
    tenant_id = extract_tenant_id(request)
    try:
        result = await _publish_service.list_impacted_kbs(tenant_id, scenario_id)
        return success_response(data=result)
    except JonexException as e:
        return error_response(code=e.code, message=e.message, status_code=e.status_code, details=e.details)
