"""数据治理 API 路由（薄 REST 适配层）。

对外前缀 `/api/v1/data-governance`，能力 ID `business.data_governance.v1`。
租户身份来自当前 Authorization token，经 Sidecar `/invoke` 透传；请求体从不
接收 `tenant_id`。
"""
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request

from api_gateway.deps import require_auth_header
from api_gateway.routes.knowledge_base import _invoke_sidecar_capability
from jonex_core.common import success_response
from jonex_core.common.exceptions import InvalidParameterError

from capabilities.data_governance.dtos.catalog import (
    AssetMetadataUpdateRequest,
    ClassificationBatchRequest,
    ClassificationCategoryUpdateRequest,
    ClassificationDefaultUpdateRequest,
    ClassificationLevelUpdateRequest,
    RetentionPolicyUpdateRequest,
    UserClearanceUpdateRequest,
)

_CAPABILITY_ID = "business.data_governance.v1"

router = APIRouter(
    prefix="/data-governance",
    tags=["数据治理"],
    dependencies=[Depends(require_auth_header)],
)


@router.get("/assets", summary="资产目录列表")
async def list_assets(
    request: Request,
    asset_type: str = Query("all", description="all/document/data_source"),
    domain_space_id: Optional[str] = Query(None, description="领域空间 ID"),
    kb_id: Optional[str] = Query(None, description="知识库 ID"),
    domain: Optional[str] = Query(None, description="业务域"),
    owner: Optional[str] = Query(None, description="负责人"),
    sensitivity: Optional[str] = Query(None, description="密级"),
    category: Optional[str] = Query(None, description="分类 code"),
    status: Optional[str] = Query(None, description="状态"),
    keyword: Optional[str] = Query(None, description="名称关键字"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
):
    payload = {
        "asset_type": asset_type,
        "domain_space_id": domain_space_id,
        "kb_id": kb_id,
        "domain": domain,
        "owner": owner,
        "sensitivity": sensitivity,
        "category": category,
        "status": status,
        "keyword": keyword,
        "page": page,
        "page_size": page_size,
    }
    result = await _invoke_sidecar_capability(
        request, "list_assets", payload,
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.post("/assets/classification/batch", summary="批量设置资产分级")
async def batch_update_classification(
    request: Request, body: ClassificationBatchRequest,
):
    # 注册顺序保持在动态 `/assets/{asset_type}/{asset_id}` 之前：
    # 未来给详情路径加 POST 时，静态 batch 路径仍优先匹配。
    result = await _invoke_sidecar_capability(
        request, "batch_update_classification", body.dict(exclude_unset=True),
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.get("/assets/{asset_type}/{asset_id}", summary="资产详情")
async def get_asset(request: Request, asset_type: str, asset_id: str):
    result = await _invoke_sidecar_capability(
        request, "get_asset",
        {"asset_type": asset_type, "asset_id": asset_id},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.put("/assets/{asset_type}/{asset_id}/metadata", summary="更新资产元数据")
async def update_asset_metadata(
    request: Request,
    asset_type: str,
    asset_id: str,
    body: AssetMetadataUpdateRequest,
):
    # `exclude_unset` 是关键：未传字段不能被序列化成 null 清空已有值。
    payload = {
        "asset_type": asset_type,
        "asset_id": asset_id,
        **body.dict(exclude_unset=True),
    }
    result = await _invoke_sidecar_capability(
        request, "update_asset_metadata", payload,
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.get("/retention-policy", summary="查询保留策略（生效值）")
async def get_retention_policy(
    request: Request,
    scope_type: str = Query(..., description="tenant/kb/document"),
    scope_id: Optional[str] = Query(None, description="kb/document 必填"),
):
    result = await _invoke_sidecar_capability(
        request, "get_retention_policy",
        {"scope_type": scope_type, "scope_id": scope_id},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.put("/retention-policy", summary="设置保留策略")
async def set_retention_policy(
    request: Request,
    body: RetentionPolicyUpdateRequest,
):
    # `exclude_unset`：未传字段保持继承；显式 null 才是「清除该级覆盖」。
    payload = body.dict(exclude_unset=True)
    result = await _invoke_sidecar_capability(
        request, "set_retention_policy", payload,
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.get(
    "/assets/{asset_type}/{asset_id}/lineage", summary="资产血缘（文档级）",
)
async def get_asset_lineage(
    request: Request,
    asset_type: str,
    asset_id: str,
    direction: str = Query("both", description="up/down/both"),
    depth: int = Query(1, ge=1, le=2, description="展开深度"),
):
    result = await _invoke_sidecar_capability(
        request, "get_asset_lineage",
        {
            "asset_type": asset_type,
            "asset_id": asset_id,
            "direction": direction,
            "depth": depth,
        },
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


# ---------------------------------------------------------------------------
# 分级分类（第 4 期）
# ---------------------------------------------------------------------------


@router.get("/classification-config", summary="分级词表")
async def list_classification_config(request: Request):
    result = await _invoke_sidecar_capability(
        request, "list_classification_config", {},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.put("/classification-levels/{code}", summary="创建/更新分级等级")
async def upsert_classification_level(
    request: Request, code: str, body: ClassificationLevelUpdateRequest,
):
    result = await _invoke_sidecar_capability(
        request, "upsert_classification_level",
        {"code": code, **body.dict(exclude_unset=True)},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.put("/classification-categories/{code}", summary="创建/更新分类")
async def upsert_classification_category(
    request: Request, code: str, body: ClassificationCategoryUpdateRequest,
):
    result = await _invoke_sidecar_capability(
        request, "upsert_classification_category",
        {"code": code, **body.dict(exclude_unset=True)},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.get("/classification-default", summary="查询分级默认值")
async def get_classification_default(
    request: Request,
    scope_type: str = Query(..., description="tenant/kb"),
    scope_id: Optional[str] = Query(None, description="kb 必填"),
):
    result = await _invoke_sidecar_capability(
        request, "get_classification_default",
        {"scope_type": scope_type, "scope_id": scope_id},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.put("/classification-default", summary="设置分级默认值")
async def set_classification_default(
    request: Request, body: ClassificationDefaultUpdateRequest,
):
    # `exclude_unset`：未传字段不动；显式 null 才是「清除该级覆盖」。
    result = await _invoke_sidecar_capability(
        request, "set_classification_default", body.dict(exclude_unset=True),
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


# ---------------------------------------------------------------------------
# 用户密级配置
# ---------------------------------------------------------------------------


@router.get("/classification-users", summary="用户密级列表")
async def list_user_clearances(request: Request):
    result = await _invoke_sidecar_capability(
        request, "list_user_clearances", {},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.put("/classification-users/{user_id}", summary="设置/清除用户密级")
async def set_user_clearance(
    request: Request, user_id: str, body: UserClearanceUpdateRequest,
):
    # 键必须存在：显式 null 是「清除」，缺字段是 400。
    fields = body.dict(exclude_unset=True)
    if "max_level_code" not in fields:
        raise InvalidParameterError(message="max_level_code 字段必须存在")
    result = await _invoke_sidecar_capability(
        request, "set_user_clearance",
        {"user_id": user_id, "max_level_code": fields["max_level_code"]},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


# ---------------------------------------------------------------------------
# 标准目录（第 5 期）— 只读
# ---------------------------------------------------------------------------


@router.get("/standards/domains", summary="标准领域列表")
async def list_standard_domains(request: Request):
    result = await _invoke_sidecar_capability(
        request, "list_standard_domains", {},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.get("/standards/domains/{domain_id}", summary="标准领域详情")
async def get_standard_domain(request: Request, domain_id: str):
    result = await _invoke_sidecar_capability(
        request, "get_standard_domain", {"domain_id": domain_id},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.get("/standards/scenarios/{scenario_id}", summary="标准场景详情")
async def get_standard_scenario(request: Request, scenario_id: str):
    result = await _invoke_sidecar_capability(
        request, "get_standard_scenario", {"scenario_id": scenario_id},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.get(
    "/standards/scenarios/{scenario_id}/versions",
    summary="模板场景发布版本列表",
)
async def list_standard_scenario_versions(request: Request, scenario_id: str):
    result = await _invoke_sidecar_capability(
        request, "list_standard_scenario_versions",
        {"scenario_id": scenario_id},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.get(
    "/standards/scenarios/{scenario_id}/versions/{version}",
    summary="模板场景指定发布版本（含快照）",
)
async def get_standard_scenario_version(
    request: Request, scenario_id: str, version: int,
):
    result = await _invoke_sidecar_capability(
        request, "get_standard_scenario_version",
        {"scenario_id": scenario_id, "version": version},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.get(
    "/standards/scenarios/{scenario_id}/versions-diff",
    summary="模板场景两个发布版本的差异",
)
async def diff_standard_scenario_versions(
    request: Request,
    scenario_id: str,
    from_version: int = Query(..., ge=1, description="基线版本"),
    to_version: int = Query(..., ge=1, description="目标版本"),
):
    result = await _invoke_sidecar_capability(
        request, "diff_standard_scenario_versions",
        {
            "scenario_id": scenario_id,
            "from_version": from_version,
            "to_version": to_version,
        },
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.get(
    "/standards/scenarios/{scenario_id}/compliance",
    summary="落标校验：KB compiled schema 对照最新发布标准",
)
async def get_standard_compliance(request: Request, scenario_id: str):
    result = await _invoke_sidecar_capability(
        request, "get_standard_compliance",
        {"scenario_id": scenario_id},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.get("/standards/kb-schemas", summary="知识库 compiled schema 列表")
async def list_kb_schemas(
    request: Request,
    kb_id: Optional[str] = Query(None, description="知识库 ID"),
    keyword: Optional[str] = Query(None, description="关键字"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
):
    result = await _invoke_sidecar_capability(
        request, "list_kb_schemas",
        {"kb_id": kb_id, "keyword": keyword, "page": page, "page_size": page_size},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.get("/standards/references", summary="标准引用/影响分析")
async def get_standard_references(
    request: Request,
    scope_type: str = Query(..., description="scenario/object/attribute"),
    scope_id: str = Query(..., description="作用域对象 ID"),
):
    result = await _invoke_sidecar_capability(
        request, "get_standard_references",
        {"scope_type": scope_type, "scope_id": scope_id},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)
