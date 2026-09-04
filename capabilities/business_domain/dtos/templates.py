"""
业务领域模板 DTOs。

租户由认证上下文/Gateway/Sidecar 传递，所有外部请求体都不接收 tenant_id。
"""
from datetime import datetime
from typing import Any, Optional

try:
    from pydantic.v1 import BaseModel, Field
except ImportError:
    from pydantic import BaseModel, Field


class TemplateDomainCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    name_en: Optional[str] = Field(None, max_length=255)
    description: Optional[str] = None
    status: Optional[str] = Field(default="inactive", max_length=32)
    ontology_code: Optional[str] = Field(None, max_length=128)
    aliases: list[str] = Field(default_factory=list)


class TemplateDomainUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    name_en: Optional[str] = Field(None, max_length=255)
    description: Optional[str] = None
    status: Optional[str] = Field(None, max_length=32)
    ontology_code: Optional[str] = Field(None, max_length=128)
    aliases: Optional[list[str]] = None


class TemplateScenarioCreateRequest(BaseModel):
    domain_id: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    config_json: dict[str, Any] = Field(default_factory=dict)


class TemplateScenarioUpdateRequest(BaseModel):
    domain_id: Optional[str] = Field(None, min_length=1, max_length=64)
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    config_json: Optional[dict[str, Any]] = None


class TemplateAttributeCreateRequest(BaseModel):
    attr_name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    attr_type: str = Field(default="string", min_length=1, max_length=64)
    is_primary_key: bool = False
    constraints_json: dict[str, Any] = Field(default_factory=dict)
    sort_order: int = 0
    ontology_code: Optional[str] = Field(None, max_length=128)
    is_required: bool = False


class TemplateObjectCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    status: Optional[str] = Field(default="draft", max_length=32)
    ontology_code: Optional[str] = Field(None, max_length=128)
    aliases: list[str] = Field(default_factory=list)
    attributes: list[TemplateAttributeCreateRequest] = Field(default_factory=list)


class TemplateObjectUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    status: Optional[str] = Field(None, max_length=32)
    ontology_code: Optional[str] = Field(None, max_length=128)
    aliases: Optional[list[str]] = None
    attributes: Optional[list[TemplateAttributeCreateRequest]] = None


class TemplateRelationCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    source_object_id: str = Field(..., min_length=1, max_length=64)
    target_object_id: str = Field(..., min_length=1, max_length=64)
    relation_type: str = Field(..., min_length=1, max_length=32)
    status: Optional[str] = Field(default="draft", max_length=32)
    ontology_code: Optional[str] = Field(None, max_length=128)
    aliases: list[str] = Field(default_factory=list)


class TemplateRelationUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    source_object_id: Optional[str] = Field(None, min_length=1, max_length=64)
    target_object_id: Optional[str] = Field(None, min_length=1, max_length=64)
    relation_type: Optional[str] = Field(None, min_length=1, max_length=32)
    status: Optional[str] = Field(None, max_length=32)
    ontology_code: Optional[str] = Field(None, max_length=128)
    aliases: Optional[list[str]] = None


class TemplateConstraintCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    target_type: str = Field(
        ..., min_length=1, max_length=32,
        description="object / attribute / relation",
    )
    target_id: str = Field(..., min_length=1, max_length=64)
    constraint_type: str = Field(..., min_length=1, max_length=32)
    expression: Optional[str] = None
    suggestion: Optional[str] = None


class TemplateConstraintUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    target_type: Optional[str] = Field(None, min_length=1, max_length=32)
    target_id: Optional[str] = Field(None, min_length=1, max_length=64)
    constraint_type: Optional[str] = Field(None, min_length=1, max_length=32)
    expression: Optional[str] = None
    suggestion: Optional[str] = None


# ── Response ──────────────────────────────────────────────


class TemplateDomainResponse(BaseModel):
    id: str
    tenant_id: str
    name: str
    name_en: Optional[str] = None
    description: Optional[str] = None
    status: str
    version: int = 1
    published_at: Optional[datetime] = None
    structure_hash: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class TemplateScenarioResponse(BaseModel):
    id: str
    tenant_id: str
    domain_id: str
    name: str
    description: Optional[str] = None
    config_json: dict[str, Any] = Field(default_factory=dict)
    version: int = 1
    published_at: Optional[datetime] = None
    structure_hash: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class TemplateAttributeResponse(BaseModel):
    id: str
    tenant_id: str
    template_object_id: str
    attr_name: str
    description: Optional[str] = None
    attr_type: str
    is_primary_key: int
    constraints_json: dict[str, Any] = Field(default_factory=dict)
    sort_order: int
    ontology_code: Optional[str] = None
    is_required: int = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class TemplateObjectResponse(BaseModel):
    id: str
    tenant_id: str
    domain_id: str
    scenario_id: str
    name: str
    description: Optional[str] = None
    status: str
    ontology_code: Optional[str] = None
    aliases: list[str] = Field(default_factory=list)
    attributes: list[TemplateAttributeResponse] = Field(default_factory=list)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class TemplateRelationResponse(BaseModel):
    id: str
    tenant_id: str
    domain_id: str
    scenario_id: str
    name: str
    description: Optional[str] = None
    source_object_id: str
    target_object_id: str
    source_object_name: Optional[str] = None
    target_object_name: Optional[str] = None
    relation_type: str
    status: str
    ontology_code: Optional[str] = None
    aliases: list[str] = Field(default_factory=list)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class TemplateConstraintResponse(BaseModel):
    id: str
    tenant_id: str
    domain_id: str
    scenario_id: str
    name: str
    target_type: str
    target_id: str
    target_label: str
    constraint_type: str
    expression: Optional[str] = None
    suggestion: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


# ── 发布/编译预览/影响范围 ─────────────────────────────────


class PublishScenarioRequest(BaseModel):
    """发布模板场景。触发 version/hash 更新。"""
    pass


class CompilePreviewResponse(BaseModel):
    """编译预览响应。"""
    entity_types: list[dict] = Field(default_factory=list)
    relation_types: list[dict] = Field(default_factory=list)
    source_version: int = 1
    source_hash: Optional[str] = None


class ImpactedKBItem(BaseModel):
    knowledge_base_id: str
    schema_outdated: bool = False


class ImpactedKBResponse(BaseModel):
    items: list[ImpactedKBItem] = Field(default_factory=list)
    total: int = 0
