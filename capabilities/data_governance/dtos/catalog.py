"""数据治理目录/元数据 DTO（Pydantic v1）。"""
from typing import List, Optional

from pydantic.v1 import BaseModel, Field, conint


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


class UserClearanceUpdateRequest(BaseModel):
    """用户密级写入请求。

    字段必须存在；显式 `null` 表示清除该用户的密级覆盖。路由层用
    `dict(exclude_unset=True)` 区分「未传」与「显式 null」。
    """

    max_level_code: Optional[str] = None

    class Config:
        extra = "forbid"
