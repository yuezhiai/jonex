"""数据治理目录/元数据 DTO（Pydantic v1）。"""
from typing import Any, List, Optional

from pydantic.v1 import BaseModel, Field, StrictBool, confloat, conint


class AssetMetadataUpdateRequest(BaseModel):
    """元数据更新请求。

    `extra="forbid"` 把拼错的字段名挡在网关，而不是静默丢字段后返回成功。
    路由层必须用 `dict(exclude_unset=True)` 组装 payload —— 否则未传字段
    会被序列化成 `null` 并清空已有值。
    """

    owner: Optional[str] = Field(None, max_length=128)
    domain: Optional[str] = Field(None, max_length=64)
    sensitivity: Optional[str] = None
    # 显式 `None` 表示清除该资产的分级分类覆盖（`exclude_unset` 保留）。
    categories: Optional[List[str]] = None

    class Config:
        extra = "forbid"


class RetentionPolicyUpdateRequest(BaseModel):
    """保留策略写入请求。

    - `scope_type` 必填；`scope_id` 对 kb/document 必填（tenant 恒为空）。
    - 未传的字段继承上级；显式 `null`（`exclude_unset` 保留）表示**清除该级覆盖**。
    - **不要在这里加 `ge/le`**：FastAPI 会把它变成 422，而 spec §10 要求非法值
      走能力层 `normalize_policy_fields` 的统一 400。
    """

    scope_type: str = Field(..., min_length=1, max_length=16)
    scope_id: Optional[str] = Field(None, max_length=64)
    # strict：拒 bool/float/string（否则 `true` 会被静默当成 1）；
    # 不加 ge/le —— 负值/超上限由 service 抛 400，而不是 FastAPI 的 422。
    retention_days: Optional[conint(strict=True)] = None
    grace_days: Optional[conint(strict=True)] = None

    class Config:
        extra = "forbid"


class ClassificationLevelUpdateRequest(BaseModel):
    """分级等级创建/更新（`code` 走路径参数）。"""

    name: Optional[str] = Field(None, max_length=64)
    # strict：拒 bool/float/string，值域由 service 抛 400（与保留策略同口径）。
    rank: Optional[conint(strict=True)] = None
    is_active: Optional[bool] = None

    class Config:
        extra = "forbid"


class ClassificationCategoryUpdateRequest(BaseModel):
    """分类创建/更新（`code` 走路径参数）。"""

    name: Optional[str] = Field(None, max_length=64)
    is_active: Optional[bool] = None

    class Config:
        extra = "forbid"


class ClassificationDefaultUpdateRequest(BaseModel):
    """分级分类默认值写入请求。

    `level_code=None` / `categories=None` 表示清除该级覆盖；
    `scope_id` 对 kb 必填，tenant 忽略（由能力层归一为空串）。
    """

    scope_type: str = Field(..., min_length=1, max_length=16)
    scope_id: Optional[str] = Field(None, max_length=64)
    level_code: Optional[str] = None
    categories: Optional[List[str]] = None

    class Config:
        extra = "forbid"


class ClassificationBatchRequest(BaseModel):
    """批量设置资产分级分类。

    `assets` 元素为 `{asset_type, asset_id}`；逐项校验与部分失败语义由
    `CatalogService.batch_update_classification` 承载。
    """

    assets: List[dict]
    sensitivity: Optional[str] = None
    categories: Optional[List[str]] = None

    class Config:
        extra = "forbid"


class StandardAssetDraftRequest(BaseModel):
    """新建标准资产草稿。

    `payload` 默认空对象是设计：克隆（`base_version` 非空）时 payload 可为空，
    name 仍必填，但 service 会忽略调用方传入的 name/payload 并用基准行覆盖。
    """

    kind: str = Field(..., min_length=1, max_length=32)
    code: str = Field(..., min_length=1, max_length=128)
    name: str = Field(..., min_length=1, max_length=255)
    payload: dict = Field(default_factory=dict)
    # strict：拒 bool/float/string（否则 `true` 会被静默当成 1）。
    base_version: Optional[conint(strict=True)] = None

    class Config:
        extra = "forbid"


class StandardAssetUpdateRequest(BaseModel):
    """更新草稿请求体。

    `expected_revision` 必填（乐观锁），`name`/`payload` 未传即不动。
    """

    # strict + ge：拒 bool/float/string，同时保留 `0`/负数 422。
    expected_revision: conint(strict=True, ge=1)
    name: Optional[str] = Field(None, max_length=255)
    payload: Optional[dict] = None

    class Config:
        extra = "forbid"


class StandardAssetPublishRequest(BaseModel):
    """发布请求体：只接受乐观锁版本号。

    发布是状态迁移而非内容更新；误传 `name`/`payload` 必须是 422，不能被
    静默忽略（与更新请求分型，避免前端复用表单时发错内容）。
    """

    expected_revision: conint(strict=True, ge=1)

    class Config:
        extra = "forbid"


# ---------------------------------------------------------------------------
# 数据合同（W2.3）
# ---------------------------------------------------------------------------


class DataContractDraftRequest(BaseModel):
    """新建/克隆数据合同草稿。

    `payload` 为 None 表示「用脚手架生成」（也可显式传对象覆盖）；`base_version`
    非空表示克隆该历史版本，此时 name/payload 由 service 用基准行覆盖。
    """

    data_source_id: str = Field(..., min_length=1)
    payload: Optional[dict] = None
    name: Optional[str] = None
    # strict：拒 bool/float/string（否则 `true` 会被静默当成 1）。
    base_version: Optional[conint(strict=True)] = None

    class Config:
        extra = "forbid"


class DataContractUpdateRequest(BaseModel):
    """更新草稿：`expected_revision` 必填（乐观锁），其余未传即不动。"""

    # strict + ge：拒 bool/float/string，同时保留 `0`/负数 422。
    expected_revision: conint(strict=True, ge=1)
    payload: Optional[dict] = None
    name: Optional[str] = None

    class Config:
        extra = "forbid"


class DataContractPublishRequest(BaseModel):
    """发布请求体：只接受乐观锁版本号（发布是状态迁移，不是内容更新）。"""

    expected_revision: conint(strict=True, ge=1)

    class Config:
        extra = "forbid"


class DataContractImportRequest(BaseModel):
    """导入 ODCS 合同：`format` 缺省按内容首个非空白字符判定。"""

    data_source_id: str = Field(..., min_length=1)
    content: str = Field(..., min_length=1)
    format: Optional[str] = None

    class Config:
        extra = "forbid"


class DataContractScaffoldRequest(BaseModel):
    """脚手架预览：只读，不落库。"""

    data_source_id: str = Field(..., min_length=1)

    class Config:
        extra = "forbid"


# ── W2.5 数据质量闭环 ────────────────────────────────────────────────


class QualityRuleCreateRequest(BaseModel):
    """创建质量规则。`enabled` 只收 0/1（拒绝 bool/字符串）。"""

    name: str = Field(..., min_length=1, max_length=255)
    asset_type: str
    asset_id: str
    dimension: str
    severity: str
    check_type: str
    definition: dict
    threshold: dict
    enabled: Optional[conint(strict=True, ge=0, le=1)] = None
    kb_id: Optional[str] = None

    class Config:
        extra = "forbid"


class QualityRuleUpdateRequest(BaseModel):
    """更新规则：`expected_revision` 必填，其余未传即不动。"""

    expected_revision: conint(strict=True, ge=1)
    name: Optional[str] = None
    asset_type: Optional[str] = None
    asset_id: Optional[str] = None
    dimension: Optional[str] = None
    severity: Optional[str] = None
    check_type: Optional[str] = None
    definition: Optional[dict] = None
    threshold: Optional[dict] = None
    enabled: Optional[conint(strict=True, ge=0, le=1)] = None
    kb_id: Optional[str] = None

    class Config:
        extra = "forbid"


class QualityPreviewRequest(BaseModel):
    """预览检查 SQL（只读）。"""

    data_source_id: str = Field(..., min_length=1)
    rule_ids: Optional[List[str]] = None
    dialect: Optional[str] = None

    class Config:
        extra = "forbid"


class QualityRunCreateRequest(BaseModel):
    """发起一次巡检运行。"""

    data_source_id: str = Field(..., min_length=1)
    rule_ids: Optional[List[str]] = None
    trigger: Optional[str] = None
    executor: Optional[str] = None
    dialect: Optional[str] = None

    class Config:
        extra = "forbid"


class QualityResultItem(BaseModel):
    """单条规则结果。"""

    rule_id: str = Field(..., min_length=1)
    status: str
    metric_value: Optional[Any] = None
    message: Optional[str] = None
    sample: Optional[List[Any]] = None

    class Config:
        extra = "forbid"


class QualityRunSubmitRequest(BaseModel):
    """结果回传（一次 1..500 条）。"""

    results: List[QualityResultItem]
    executor: Optional[str] = None
    message: Optional[str] = None

    class Config:
        extra = "forbid"


class QualityRunFailRequest(BaseModel):
    """巡检失败收口。"""

    message: str = Field(..., min_length=1)

    class Config:
        extra = "forbid"


class QualityIssueAssignRequest(BaseModel):
    """指派工单。"""

    assignee_id: str = Field(..., min_length=1)
    expected_revision: conint(strict=True, ge=1)

    class Config:
        extra = "forbid"


class QualityIssueStatusRequest(BaseModel):
    """工单状态流转。"""

    to_status: str
    expected_revision: conint(strict=True, ge=1)
    note: Optional[str] = None

    class Config:
        extra = "forbid"


class QualityIssueVerifyRequest(BaseModel):
    """复验：P1 一次性通过/驳回。"""

    passed: StrictBool
    note: Optional[str] = None
    expected_revision: conint(strict=True, ge=1)

    class Config:
        extra = "forbid"


class QualityIssueCommentRequest(BaseModel):
    """工单评论。"""

    note: str = Field(..., min_length=1)

    class Config:
        extra = "forbid"


class QualityContractImportRequest(BaseModel):
    """从数据合同导入质量规则。"""

    contract_id: str = Field(..., min_length=1)

    class Config:
        extra = "forbid"


# ── W2.6 资源视图 / 落标绑定 ──────────────────────────────────────


class BindingCreateRequest(BaseModel):
    """创建落标绑定。

    绑定身份是 `(standard_kind, standard_code)` + W2.1 资产 ID；`source`
    仅区分人工/建议来源（人工确认即生效，不做二次审批）。
    """

    standard_kind: str = Field(..., min_length=1, max_length=32)
    standard_code: str = Field(..., min_length=1, max_length=128)
    asset_type: str = Field(..., min_length=1, max_length=16)
    asset_id: str = Field(..., min_length=1, max_length=255)
    note: Optional[str] = Field(None, max_length=512)
    source: Optional[str] = None
    # 采纳建议时由前端回传；人工绑定缺省 1.0（service 侧决定）。
    # 非 strict：同时接受 int 与 float（`confidence=1` 不能被 422 挡掉），
    # 值域 [0, 1] 仍由 DTO 先行 422、service 再兜一次 1001。
    confidence: Optional[confloat(ge=0, le=1)] = None

    class Config:
        extra = "forbid"


class BindingUpdateRequest(BaseModel):
    """更新/停用绑定：`expected_revision` 必填，其余未传即不动。"""

    # strict + ge：拒 bool/float/string，同时保留 `0`/负数 422。
    expected_revision: conint(strict=True, ge=1)
    note: Optional[str] = Field(None, max_length=512)
    status: Optional[str] = None


class UserClearanceUpdateRequest(BaseModel):
    """用户密级写入请求。

    字段必须存在；显式 `null` 表示清除该用户的密级覆盖。路由层用
    `dict(exclude_unset=True)` 区分「未传」与「显式 null」。
    """

    max_level_code: Optional[str] = None

    class Config:
        extra = "forbid"
