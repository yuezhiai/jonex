"""
DTOs for ontology binding, compiled schema query, and KB-side schema editing.
"""
from datetime import datetime
from typing import Any, Optional

try:
    from pydantic.v1 import BaseModel, Field, ValidationError, validator
except ImportError:
    from pydantic import BaseModel, Field, ValidationError, validator

import re

from jonex_core.common.catalog_skill_spec import is_catalog_skill, parse_catalog_item

_SKILL_REF_RE = re.compile(r"^(builtin|catalog):[A-Za-z0-9_-]+$")


class BindTemplateRequest(BaseModel):
    template_domain_id: Optional[str] = Field(default=None, max_length=64)
    template_scenario_id: Optional[str] = Field(default=None, max_length=64)
    source_type: str = Field(default="business_template", max_length=32)


class BindingResponse(BaseModel):
    id: int
    tenant_id: str
    knowledge_base_id: str
    template_domain_id: Optional[str] = None
    template_scenario_id: Optional[str] = None
    source_type: str = "business_template"
    status: str = "active"
    created_at: Optional[datetime | str] = None
    updated_at: Optional[datetime | str] = None


class CompileRequest(BaseModel):
    knowledge_base_id: str = Field(..., min_length=1, max_length=128)
    force: bool = Field(default=False)


class RecompileRequest(BaseModel):
    knowledge_base_id: str = Field(..., min_length=1, max_length=128)


class CompiledSchemaResponse(BaseModel):
    id: Optional[int] = None
    tenant_id: Optional[str] = None
    knowledge_base_id: Optional[str] = None
    template_domain_id: Optional[str] = None
    template_scenario_id: Optional[str] = None
    source_type: Optional[str] = "business_template"
    source_version: int = 1
    source_hash: Optional[str] = None
    schema_version: int = 1
    entity_types: list[dict] = Field(default_factory=list)
    relation_types: list[dict] = Field(default_factory=list)
    constraints: list[dict] = Field(default_factory=list)
    disambiguation: dict = Field(default_factory=lambda: {"case_insensitive": True, "alias_merge": True})
    prompt_schema: dict = Field(default_factory=dict)
    status: str = "active"
    schema_mode: str = "template_seeded"
    sync_status: str = "synced"
    edited_at: Optional[datetime | str] = None
    edited_by: Optional[str] = None
    compiled_at: Optional[datetime | str] = None

    class Config:
        extra = "allow"


class CompiledAttributeInput(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    display_name: Optional[str] = Field(default=None, max_length=255)
    description: Optional[str] = None
    type: str = Field(default="string", min_length=1, max_length=32)
    required: bool = False
    is_primary_key: bool = False
    source_attribute_id: Optional[str] = Field(default=None, max_length=64)
    # 物理列名（catalog 原名）：属性名可能是归一后的 slug，生成 SQL 用物理名。
    physical_name: Optional[str] = Field(default=None, max_length=255)


class CompiledEntityTypeInput(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    display_name: Optional[str] = Field(default=None, max_length=255)
    description: Optional[str] = None
    requirement: Optional[str] = None
    status: Optional[str] = Field(default="active", max_length=32)
    aliases: list[str] = Field(default_factory=list)
    source_object_id: Optional[str] = Field(default=None, max_length=64)
    attributes: list[CompiledAttributeInput] = Field(default_factory=list)
    # [jonex] P1：TBox 构建技能声明（可选，缺省零行为变化）。
    # 保持 dict 形状（与 JSONB 存储 / YAML / models.py 口径一致）：
    # {"refine": [{skill, description}], "constrain": [{skill, description}]}
    extraction_skills: Optional[dict] = None

    @validator("extraction_skills")
    def _validate_extraction_skills(cls, v):
        if v is None:
            return v
        if not isinstance(v, dict):
            raise ValueError("extraction_skills 必须是对象")
        unknown = set(v.keys()) - {"refine", "constrain"}
        if unknown:
            raise ValueError(
                f"extraction_skills 存在未知分组: {', '.join(sorted(unknown))}"
            )
        for group in ("refine", "constrain"):
            items = v.get(group) or []
            if not isinstance(items, list):
                raise ValueError(f"extraction_skills.{group} 必须是列表")
            for item in items:
                if not isinstance(item, dict) or not item.get("skill"):
                    raise ValueError(f"extraction_skills.{group} 每项必须含 skill 字段")
                skill = str(item["skill"]).strip()
                if not _SKILL_REF_RE.match(skill):
                    raise ValueError(
                        f"extraction_skills 技能名必须以 builtin: 或 catalog: 为前缀: {skill}"
                    )
                if is_catalog_skill(skill):
                    # catalog: 声明由 knowledge-base 写图前执行，入参 / 写入 / 拒绝条件在保存时就要合法
                    _, errs = parse_catalog_item(group, item)
                    if errs:
                        raise ValueError("；".join(errs))
        return v


class CompiledRelationTypeInput(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    display_name: Optional[str] = Field(default=None, max_length=255)
    description: Optional[str] = None
    status: Optional[str] = Field(default="active", max_length=32)
    aliases: list[str] = Field(default_factory=list)
    source: str = Field(..., min_length=1, max_length=128)
    target: str = Field(..., min_length=1, max_length=128)
    source_relation_id: Optional[str] = Field(default=None, max_length=64)
    cardinality: str = Field(default="custom", min_length=1, max_length=32)
    # [jonex] P1：inverse_of 纯声明不建反向边；source_types/target_types 为空回落单值
    inverse_of: Optional[str] = Field(default=None, min_length=1, max_length=128)
    source_types: list[str] = Field(default_factory=list)
    target_types: list[str] = Field(default_factory=list)
    # [jonex] P0-2：join 键。生成器填、② NL→SQL 读；编辑器保存必须原样带上，
    # 否则一次画布保存就把 join 图洗掉了（前端 `RELATION_EXTRAS` 是另一半）。
    source_column: Optional[str] = Field(default=None, max_length=128)
    target_column: Optional[str] = Field(default=None, max_length=128)
    origin: Optional[str] = Field(default=None, max_length=16)


class CompiledConstraintInput(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    target_type: str = Field(..., min_length=1, max_length=16)  # entity|attribute|relation
    target_code: str = Field(..., min_length=1, max_length=257)  # entity=code / attribute=entityCode.attrCode / relation=code
    target_label: Optional[str] = Field(default=None, max_length=255)
    constraint_type: str = Field(default="custom", min_length=1, max_length=32)
    expression: Optional[str] = None
    suggestion: Optional[str] = None


class SaveCompiledSchemaRequest(BaseModel):
    knowledge_base_id: str = Field(..., min_length=1, max_length=128)
    # 要保存哪一份：None = 知识库级；给了 id = 该数据源自己的那份。
    # 乐观锁是**按份**的，两个槽位各有各的 schema_version。
    data_source_id: Optional[str] = Field(default=None, max_length=64)
    entity_types: list[CompiledEntityTypeInput] = Field(default_factory=list)
    relation_types: list[CompiledRelationTypeInput] = Field(default_factory=list)
    # 三态：未传/None=保留现有约束；[]=清空；非空=替换
    constraints: Optional[list[CompiledConstraintInput]] = None
    # 乐观锁：编辑器保存必须携带；service 层 None -> 400
    expected_schema_version: Optional[int] = None


class ReseedCompiledSchemaRequest(BaseModel):
    knowledge_base_id: str = Field(..., min_length=1, max_length=128)
    template_domain_id: Optional[str] = Field(default=None, max_length=64)
    template_scenario_id: str = Field(..., min_length=1, max_length=64)
    source_type: str = Field(default="business_template", max_length=32)


class TemplateSummaryResponse(BaseModel):
    domain_id: Optional[str] = None
    domain_name: Optional[str] = None
    scenario_id: Optional[str] = None
    scenario_name: Optional[str] = None
    source_version: Optional[int] = None
    source_hash: Optional[str] = None


class EditorStateResponse(BaseModel):
    knowledge_base_id: str
    binding: Optional[BindingResponse | dict[str, Any]] = None
    compiled_schema: Optional[CompiledSchemaResponse | dict[str, Any]] = None
    current_template: Optional[TemplateSummaryResponse | dict[str, Any]] = None


class EntityTypeItem(BaseModel):
    name: str
    display_name: Optional[str] = None
    aliases: list[str] = Field(default_factory=list)
    attributes: list[dict] = Field(default_factory=list)


class RelationTypeItem(BaseModel):
    name: str
    display_name: Optional[str] = None
    source: Optional[str] = None
    target: Optional[str] = None
    cardinality: Optional[str] = None
    inverse_of: Optional[str] = None
    source_types: list[str] = Field(default_factory=list)
    target_types: list[str] = Field(default_factory=list)


class NativeOntologyExtractRequest(BaseModel):
    """独立本体抽取 HTTP 接口请求（ontology-native-extraction-execution-plan §5.2）。

    chunks + tbox 直传（不绑定 KB），tenant_id 由 Gateway 从认证上下文注入，
    body 不接受 tenant_id。knowledge_base_id / document_id 仅作 scope 元数据。
    """

    chunks: list[dict] = Field(..., min_items=1)
    tbox: dict = Field(...)  # prompt_schema: entity_types/relation_types/synonyms/disambiguation
    knowledge_base_id: Optional[str] = Field(default=None, max_length=128)
    document_id: Optional[str] = Field(default=None, max_length=128)
    options: Optional[dict] = None


class ImpactedKBItem(BaseModel):
    tenant_id: str
    knowledge_base_id: str
    schema_outdated: bool = False


class ImpactedKBResponse(BaseModel):
    items: list[ImpactedKBItem] = Field(default_factory=list)
    total: int = 0


__all__ = [
    "BindTemplateRequest",
    "BindingResponse",
    "CompileRequest",
    "CompiledAttributeInput",
    "CompiledConstraintInput",
    "CompiledEntityTypeInput",
    "CompiledRelationTypeInput",
    "CompiledSchemaResponse",
    "EditorStateResponse",
    "EntityTypeItem",
    "ImpactedKBItem",
    "ImpactedKBResponse",
    "NativeOntologyExtractRequest",
    "RecompileRequest",
    "RelationTypeItem",
    "ReseedCompiledSchemaRequest",
    "SaveCompiledSchemaRequest",
    "TemplateSummaryResponse",
]
