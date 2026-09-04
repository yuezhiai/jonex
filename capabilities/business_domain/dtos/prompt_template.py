"""
提示词模板 DTOs。

租户由认证上下文/Gateway/Sidecar 传递，所有外部请求体都不接收 tenant_id。
system 模板由种子 SQL 管理，API 不暴露 scope 字段 — 创建时固定为 "domain"。
"""
from datetime import datetime
from typing import Any, Optional

try:
    from pydantic.v1 import BaseModel, Field
except ImportError:
    from pydantic import BaseModel, Field


# ── 请求 ────────────────────────────────────────────────────


class CreatePromptTemplateRequest(BaseModel):
    """创建提示词模板（scope 固定 domain，不暴露给请求方）"""
    name: str = Field(..., min_length=1, max_length=255)
    category: str = Field(..., min_length=1, max_length=64)
    description: Optional[str] = Field(None, max_length=512)
    content: str = Field(..., min_length=1, max_length=5000)  # 提示词内容
    status: str = Field(default="启用", max_length=16)
    domain_space_id: Optional[str] = Field(None, description="所属领域空间 ID（不传则归默认空间）")


class UpdatePromptTemplateRequest(BaseModel):
    """更新提示词模板。content 变化时自动生成新版本。"""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    category: Optional[str] = Field(None, min_length=1, max_length=64)
    description: Optional[str] = Field(None, max_length=512)
    content: Optional[str] = Field(None, min_length=1, max_length=5000)
    status: Optional[str] = Field(None, max_length=16)
    version_remark: Optional[str] = Field(None, max_length=512)
    target_version: Optional[str] = Field(None, max_length=32)


class RollbackVersionRequest(BaseModel):
    """回滚到指定版本（以新版本号保存，保留历史）"""
    target_version: str = Field(..., min_length=1, max_length=32)


class ListPromptTemplatesQuery(BaseModel):
    """列表查询参数"""
    scope: Optional[str] = Field(None, max_length=16)  # system | domain
    category: Optional[str] = Field(None, max_length=64)
    keyword: Optional[str] = Field(None, max_length=255)
    domain_space_id: Optional[str] = Field(None, description="领域空间 ID（domain 模板按空间过滤）")
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)


# ── 响应 ────────────────────────────────────────────────────


class VersionItem(BaseModel):
    """版本历史条目"""
    version: str
    content: str
    updated_by: Optional[str] = None
    updated_at: Optional[str] = None
    remark: Optional[str] = None


class PromptTemplateResponse(BaseModel):
    """提示词模板响应"""
    id: str
    tenant_id: Optional[str] = None  # system 模板为 None
    space_id: Optional[str] = Field(None, description="所属领域空间 ID（system 模板为 None）")
    name: str
    category: str
    scope: str
    description: Optional[str] = None
    status: str
    current_version: str
    versions_json: list[dict] = Field(default_factory=list)
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class PromptTemplateDetailResponse(PromptTemplateResponse):
    """详情响应 — 附加展开的版本列表"""
    versions: list[VersionItem] = Field(default_factory=list)
    current_content: Optional[str] = None  # 当前版本内容（便捷字段）


class PromptTemplateListResponse(BaseModel):
    """分页列表响应"""
    items: list[PromptTemplateResponse]
    total: int
    offset: int
    limit: int


class VersionListResponse(BaseModel):
    """版本历史列表"""
    items: list[VersionItem]
    current_version: str
