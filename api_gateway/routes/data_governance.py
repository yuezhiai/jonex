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
    BindingCreateRequest,
    BindingUpdateRequest,
    ClassificationBatchRequest,
    ClassificationCategoryUpdateRequest,
    ClassificationDefaultUpdateRequest,
    ClassificationLevelUpdateRequest,
    DataContractDraftRequest,
    DataContractImportRequest,
    DataContractPublishRequest,
    DataContractScaffoldRequest,
    DataContractUpdateRequest,
    RetentionPolicyUpdateRequest,
    StandardAssetDraftRequest,
    StandardAssetPublishRequest,
    StandardAssetUpdateRequest,
    QualityContractImportRequest,
    QualityIssueAssignRequest,
    QualityIssueCommentRequest,
    QualityIssueStatusRequest,
    QualityIssueVerifyRequest,
    QualityPreviewRequest,
    QualityRuleCreateRequest,
    QualityRuleUpdateRequest,
    QualityRunCreateRequest,
    QualityRunFailRequest,
    QualityRunSubmitRequest,
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
    asset_type: str = Query("all", description="all/document/data_source/table/column/metric"),
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
    "/assets/{asset_type}/{asset_id}/lineage",
    summary="资产血缘（文档级 / 派生资产一跳）",
)
async def get_asset_lineage(
    request: Request,
    asset_type: str,
    asset_id: str,
    direction: str = Query("both", description="up/down/both"),
    depth: int = Query(1, ge=1, le=2, description="展开深度（派生资产仅支持 1）"),
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


# ---------------------------------------------------------------------------
# 标准资产（W2.2）— 术语 / 数据元 / 指标口径卡
# ---------------------------------------------------------------------------
# 注册顺序：静态路径（列表 / versions / drafts）必须排在 `/{asset_id}` 之前，
# 否则 `GET /standard-assets/versions` 会被详情路由吞掉。


@router.get("/standard-assets", summary="标准资产列表")
async def list_standard_assets(
    request: Request,
    kind: Optional[str] = Query(None, description="term/data_element/metric_card"),
    status: Optional[str] = Query(None, description="draft/published/retired"),
    keyword: Optional[str] = Query(None, description="名称/编码关键字"),
    include_drafts: bool = Query(False, description="仅管理员有效"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
):
    payload = {
        "kind": kind,
        "status": status,
        "keyword": keyword,
        "include_drafts": include_drafts,
        "page": page,
        "page_size": page_size,
    }
    result = await _invoke_sidecar_capability(
        request, "list_standard_assets", payload,
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.get("/standard-assets/versions", summary="标准资产版本历史")
async def list_standard_asset_versions(
    request: Request,
    kind: str = Query(..., description="term/data_element/metric_card"),
    code: str = Query(..., description="资产编码"),
):
    result = await _invoke_sidecar_capability(
        request, "list_standard_asset_versions",
        {"kind": kind, "code": code},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.post("/standard-assets/drafts", summary="新建标准资产草稿")
async def create_standard_draft(
    request: Request, body: StandardAssetDraftRequest,
):
    result = await _invoke_sidecar_capability(
        request, "create_standard_draft", body.dict(),
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.get("/standard-assets/{asset_id}", summary="标准资产详情")
async def get_standard_asset(request: Request, asset_id: str):
    result = await _invoke_sidecar_capability(
        request, "get_standard_asset", {"asset_id": asset_id},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.put("/standard-assets/{asset_id}", summary="更新标准资产草稿")
async def update_standard_draft(
    request: Request, asset_id: str, body: StandardAssetUpdateRequest,
):
    # `exclude_unset`：未传字段不能被序列化成 null 覆盖已有内容。
    result = await _invoke_sidecar_capability(
        request, "update_standard_draft",
        {"asset_id": asset_id, **body.dict(exclude_unset=True)},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.post("/standard-assets/{asset_id}/publish", summary="发布标准资产")
async def publish_standard_asset(
    request: Request, asset_id: str, body: StandardAssetPublishRequest,
):
    result = await _invoke_sidecar_capability(
        request, "publish_standard_asset",
        {"asset_id": asset_id, "expected_revision": body.expected_revision},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.post("/standard-assets/{asset_id}/retire", summary="退役标准资产")
async def retire_standard_asset(request: Request, asset_id: str):
    result = await _invoke_sidecar_capability(
        request, "retire_standard_asset", {"asset_id": asset_id},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.delete("/standard-assets/{asset_id}", summary="删除标准资产草稿")
async def delete_standard_draft(request: Request, asset_id: str):
    result = await _invoke_sidecar_capability(
        request, "delete_standard_draft", {"asset_id": asset_id},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


# ---------------------------------------------------------------------------
# 数据合同（W2.3）— 数据源级、版本化的 ODCS v3.x 合同
# ---------------------------------------------------------------------------
# 注册顺序：字面量路径（versions / scaffold-preview / drafts / import）必须排在
# `/{contract_id}` 之前，否则 `GET /data-contracts/versions` 会被详情路由吞掉。


@router.get("/data-contracts", summary="数据合同列表")
async def list_data_contracts(
    request: Request,
    keyword: Optional[str] = Query(None, description="名称/数据源关键字"),
    status: Optional[str] = Query(None, description="draft/published/retired"),
    include_drafts: bool = Query(False, description="仅管理员有效"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
):
    payload = {
        "keyword": keyword,
        "status": status,
        "include_drafts": include_drafts,
        "page": page,
        "page_size": page_size,
    }
    result = await _invoke_sidecar_capability(
        request, "list_data_contracts", payload,
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.get("/data-contracts/versions", summary="数据合同版本历史")
async def list_data_contract_versions(
    request: Request,
    data_source_id: str = Query(..., description="数据源 ID"),
):
    result = await _invoke_sidecar_capability(
        request, "list_data_contract_versions",
        {"data_source_id": data_source_id},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.post("/data-contracts/scaffold-preview", summary="预览数据合同脚手架")
async def preview_data_contract_scaffold(
    request: Request, body: DataContractScaffoldRequest,
):
    result = await _invoke_sidecar_capability(
        request, "preview_data_contract_scaffold", body.dict(),
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.post("/data-contracts/drafts", summary="新建数据合同草稿")
async def create_data_contract_draft(
    request: Request, body: DataContractDraftRequest,
):
    # `exclude_unset`：未传的 payload/name/base_version 不序列化成 null。
    result = await _invoke_sidecar_capability(
        request, "create_data_contract_draft", body.dict(exclude_unset=True),
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.post("/data-contracts/import", summary="导入 ODCS 数据合同")
async def import_data_contract(
    request: Request, body: DataContractImportRequest,
):
    result = await _invoke_sidecar_capability(
        request, "import_data_contract", body.dict(exclude_unset=True),
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.get("/data-contracts/{contract_id}/export", summary="导出 ODCS 数据合同")
async def export_data_contract(
    request: Request,
    contract_id: str,
    format: Optional[str] = Query(None, description="json/yaml"),
):
    result = await _invoke_sidecar_capability(
        request, "export_data_contract",
        {"contract_id": contract_id, "format": format},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.get("/data-contracts/{contract_id}", summary="数据合同详情")
async def get_data_contract(request: Request, contract_id: str):
    result = await _invoke_sidecar_capability(
        request, "get_data_contract", {"contract_id": contract_id},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.put("/data-contracts/{contract_id}", summary="更新数据合同草稿")
async def update_data_contract_draft(
    request: Request,
    contract_id: str,
    body: DataContractUpdateRequest,
):
    # `exclude_unset`：未传字段不能被序列化成 null 覆盖已有内容。
    result = await _invoke_sidecar_capability(
        request, "update_data_contract_draft",
        {"contract_id": contract_id, **body.dict(exclude_unset=True)},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.post("/data-contracts/{contract_id}/publish", summary="发布数据合同")
async def publish_data_contract(
    request: Request,
    contract_id: str,
    body: DataContractPublishRequest,
):
    result = await _invoke_sidecar_capability(
        request, "publish_data_contract",
        {"contract_id": contract_id, "expected_revision": body.expected_revision},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.post("/data-contracts/{contract_id}/retire", summary="退役数据合同")
async def retire_data_contract(request: Request, contract_id: str):
    result = await _invoke_sidecar_capability(
        request, "retire_data_contract", {"contract_id": contract_id},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.delete("/data-contracts/{contract_id}", summary="删除数据合同草稿")
async def delete_data_contract_draft(request: Request, contract_id: str):
    result = await _invoke_sidecar_capability(
        request, "delete_data_contract_draft", {"contract_id": contract_id},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


# ════════════════════════════════════════════════════════════════
# W2.5 数据质量闭环 /quality/**
# 字面量路由必须排在动态 `/{id}` 之前。
# ════════════════════════════════════════════════════════════════


@router.get("/quality/rules", summary="质量规则列表")
async def list_quality_rules(
    request: Request,
    data_source_id: Optional[str] = Query(None),
    asset_type: Optional[str] = Query(None, description="table/column"),
    asset_id: Optional[str] = Query(None),
    dimension: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
    check_type: Optional[str] = Query(None),
    enabled: Optional[int] = Query(None, ge=0, le=1),
    keyword: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    result = await _invoke_sidecar_capability(
        request, "list_quality_rules",
        {
            "data_source_id": data_source_id, "asset_type": asset_type,
            "asset_id": asset_id, "dimension": dimension, "severity": severity,
            "check_type": check_type, "enabled": enabled, "keyword": keyword,
            "page": page, "page_size": page_size,
        },
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.post("/quality/rules", summary="创建质量规则")
async def create_quality_rule(request: Request, body: QualityRuleCreateRequest):
    result = await _invoke_sidecar_capability(
        request, "create_quality_rule", body.dict(exclude_unset=True),
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.post("/quality/rules/import", summary="从数据合同导入质量规则")
async def import_quality_rules_from_contract(
    request: Request, body: QualityContractImportRequest,
):
    result = await _invoke_sidecar_capability(
        request, "import_quality_rules_from_contract", body.dict(),
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.get("/quality/rules/{rule_id}", summary="质量规则详情")
async def get_quality_rule(request: Request, rule_id: str):
    result = await _invoke_sidecar_capability(
        request, "get_quality_rule", {"rule_id": rule_id},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.put("/quality/rules/{rule_id}", summary="更新质量规则")
async def update_quality_rule(
    request: Request, rule_id: str, body: QualityRuleUpdateRequest,
):
    result = await _invoke_sidecar_capability(
        request, "update_quality_rule",
        {"rule_id": rule_id, **body.dict(exclude_unset=True)},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.delete("/quality/rules/{rule_id}", summary="删除质量规则")
async def delete_quality_rule(request: Request, rule_id: str):
    result = await _invoke_sidecar_capability(
        request, "delete_quality_rule", {"rule_id": rule_id},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.post("/quality/preview-sql", summary="预览质量检查 SQL")
async def preview_quality_check_sql(
    request: Request, body: QualityPreviewRequest,
):
    result = await _invoke_sidecar_capability(
        request, "preview_quality_check_sql", body.dict(exclude_unset=True),
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.get("/quality/runs", summary="巡检运行列表")
async def list_quality_runs(
    request: Request,
    data_source_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    result = await _invoke_sidecar_capability(
        request, "list_quality_runs",
        {
            "data_source_id": data_source_id, "status": status,
            "page": page, "page_size": page_size,
        },
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.post("/quality/runs", summary="发起巡检运行")
async def create_quality_run(request: Request, body: QualityRunCreateRequest):
    result = await _invoke_sidecar_capability(
        request, "create_quality_run", body.dict(exclude_unset=True),
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.get("/quality/runs/{run_id}", summary="巡检运行详情")
async def get_quality_run(request: Request, run_id: str):
    result = await _invoke_sidecar_capability(
        request, "get_quality_run", {"run_id": run_id},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.post("/quality/runs/{run_id}/results", summary="回传巡检结果")
async def submit_quality_run_results(
    request: Request, run_id: str, body: QualityRunSubmitRequest,
):
    result = await _invoke_sidecar_capability(
        request, "submit_quality_run_results",
        {"run_id": run_id, **body.dict(exclude_unset=True)},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.post("/quality/runs/{run_id}/fail", summary="巡检失败收口")
async def fail_quality_run(
    request: Request, run_id: str, body: QualityRunFailRequest,
):
    result = await _invoke_sidecar_capability(
        request, "fail_quality_run",
        {"run_id": run_id, **body.dict()},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.get("/quality/issues", summary="质量问题列表")
async def list_quality_issues(
    request: Request,
    status: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
    assignee_id: Optional[str] = Query(None),
    data_source_id: Optional[str] = Query(None),
    asset_type: Optional[str] = Query(None),
    asset_id: Optional[str] = Query(None),
    keyword: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    result = await _invoke_sidecar_capability(
        request, "list_quality_issues",
        {
            "status": status, "severity": severity, "assignee_id": assignee_id,
            "data_source_id": data_source_id, "asset_type": asset_type,
            "asset_id": asset_id, "keyword": keyword,
            "page": page, "page_size": page_size,
        },
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.get("/quality/issues/{issue_id}", summary="质量问题详情（含事件）")
async def get_quality_issue(request: Request, issue_id: str):
    result = await _invoke_sidecar_capability(
        request, "get_quality_issue", {"issue_id": issue_id},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.post("/quality/issues/{issue_id}/assign", summary="指派质量问题")
async def assign_quality_issue(
    request: Request, issue_id: str, body: QualityIssueAssignRequest,
):
    result = await _invoke_sidecar_capability(
        request, "assign_quality_issue",
        {"issue_id": issue_id, **body.dict()},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.post("/quality/issues/{issue_id}/status", summary="流转质量问题状态")
async def update_quality_issue_status(
    request: Request, issue_id: str, body: QualityIssueStatusRequest,
):
    result = await _invoke_sidecar_capability(
        request, "update_quality_issue_status",
        {"issue_id": issue_id, **body.dict(exclude_unset=True)},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.post("/quality/issues/{issue_id}/verify", summary="复验质量问题")
async def verify_quality_issue(
    request: Request, issue_id: str, body: QualityIssueVerifyRequest,
):
    result = await _invoke_sidecar_capability(
        request, "verify_quality_issue",
        {"issue_id": issue_id, **body.dict(exclude_unset=True)},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.post("/quality/issues/{issue_id}/comments", summary="评论质量问题")
async def comment_quality_issue(
    request: Request, issue_id: str, body: QualityIssueCommentRequest,
):
    result = await _invoke_sidecar_capability(
        request, "comment_quality_issue",
        {"issue_id": issue_id, **body.dict()},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.get("/quality/summary", summary="质量摘要")
async def get_quality_summary(
    request: Request,
    data_source_id: Optional[str] = Query(None),
):
    result = await _invoke_sidecar_capability(
        request, "get_quality_summary",
        {"data_source_id": data_source_id},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


# ════════════════════════════════════════════════════════════════
# W2.6 资源视图 /resource-views/**
# 字面量路由（coverage / suggest）必须排在动态 `/{binding_id}` 之前。
# ════════════════════════════════════════════════════════════════


@router.get("/resource-views/domains", summary="业务域视图")
async def get_resource_domain_view(
    request: Request,
    space_id: Optional[str] = Query(None, description="领域空间 ID"),
    domain: Optional[str] = Query(None, description="业务域"),
):
    result = await _invoke_sidecar_capability(
        request, "get_resource_domain_view",
        {"space_id": space_id, "domain": domain},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.get("/resource-views/responsibilities", summary="责任视图")
async def get_responsibility_view(
    request: Request,
    group_by: str = Query("owner", description="owner/domain"),
    space_id: Optional[str] = Query(None, description="领域空间 ID"),
):
    result = await _invoke_sidecar_capability(
        request, "get_responsibility_view",
        {"group_by": group_by, "space_id": space_id},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.get("/resource-views/standard-bindings", summary="落标绑定列表")
async def list_standard_bindings(
    request: Request,
    standard_kind: Optional[str] = Query(None, description="term/data_element/metric_card"),
    standard_code: Optional[str] = Query(None, description="标准编码"),
    asset_type: Optional[str] = Query(None, description="table/column/metric"),
    asset_id: Optional[str] = Query(None, description="W2.1 资产 ID"),
    status: Optional[str] = Query(None, description="active/disabled"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
):
    result = await _invoke_sidecar_capability(
        request, "list_standard_bindings",
        {
            "standard_kind": standard_kind, "standard_code": standard_code,
            "asset_type": asset_type, "asset_id": asset_id,
            "status": status, "page": page, "page_size": page_size,
        },
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.get(
    "/resource-views/standard-bindings/coverage",
    summary="落标覆盖率视图",
)
async def get_standard_binding_view(
    request: Request,
    kind: Optional[str] = Query(None, description="term/data_element/metric_card"),
    domain: Optional[str] = Query(None, description="业务域"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
):
    result = await _invoke_sidecar_capability(
        request, "get_standard_binding_view",
        {"kind": kind, "domain": domain, "page": page, "page_size": page_size},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.post(
    "/resource-views/standard-bindings/suggest",
    summary="落标建议（只读）",
)
async def suggest_standard_bindings(
    request: Request,
    standard_kind: Optional[str] = Query(None, description="term/data_element/metric_card"),
    standard_code: Optional[str] = Query(None, description="标准编码"),
    asset_type: Optional[str] = Query(None, description="table/column/metric"),
    asset_id: Optional[str] = Query(None, description="W2.1 资产 ID"),
    limit: int = Query(20, ge=1, le=100, description="返回条数上限"),
):
    result = await _invoke_sidecar_capability(
        request, "suggest_standard_bindings",
        {
            "standard_kind": standard_kind, "standard_code": standard_code,
            "asset_type": asset_type, "asset_id": asset_id, "limit": limit,
        },
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.post("/resource-views/standard-bindings", summary="创建落标绑定")
async def create_standard_binding(
    request: Request, body: BindingCreateRequest,
):
    # `exclude_unset`：未传的 note/source 不序列化成 null。
    result = await _invoke_sidecar_capability(
        request, "create_standard_binding", body.dict(exclude_unset=True),
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.put(
    "/resource-views/standard-bindings/{binding_id}",
    summary="更新/停用落标绑定",
)
async def update_standard_binding(
    request: Request, binding_id: str, body: BindingUpdateRequest,
):
    result = await _invoke_sidecar_capability(
        request, "update_standard_binding",
        {"binding_id": binding_id, **body.dict(exclude_unset=True)},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.delete(
    "/resource-views/standard-bindings/{binding_id}",
    summary="删除落标绑定（软删）",
)
async def delete_standard_binding(request: Request, binding_id: str):
    result = await _invoke_sidecar_capability(
        request, "delete_standard_binding", {"binding_id": binding_id},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)


@router.get("/resource-views/source-anchors", summary="资产原文锚点")
async def get_asset_source_anchors(
    request: Request,
    asset_type: str = Query(..., description="table/column"),
    asset_id: str = Query(..., description="W2.1 资产 ID"),
):
    result = await _invoke_sidecar_capability(
        request, "get_asset_source_anchors",
        {"asset_type": asset_type, "asset_id": asset_id},
        capability_id=_CAPABILITY_ID,
    )
    return success_response(data=result)
