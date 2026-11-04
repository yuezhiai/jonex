"""
业务领域 — AI Skill MCP 化模型

- SkillCatalog: 平台共享 Skill 目录，不带 tenant_id
- TenantSkill: 租户启用状态，带 tenant_id
"""
import uuid

from sqlalchemy import BigInteger, Column, String, Text
from sqlalchemy.dialects.postgresql import JSONB

from jonex_core.common.database import Base
from jonex_core.common.entity import SoftDeleteMixin, TenantMixin, TimestampMixin


class SkillCatalog(TimestampMixin, SoftDeleteMixin, Base):
    """平台共享 Skill 目录 — 不带租户"""

    __tablename__ = "skill_catalog"
    __table_args__ = {"schema": "business_domain"}

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    name = Column(String(255), nullable=False)
    description = Column(Text)
    category = Column(String(64), nullable=False, default="custom")
    icon = Column(String(64))
    status = Column(String(32), nullable=False, default="published")

    tool_name = Column(String(128), nullable=False, unique=True)
    instruction = Column(Text, nullable=False)
    input_schema_json = Column(JSONB, nullable=False, default=dict)
    output_schema_json = Column(JSONB, nullable=False, default=dict)

    artifact_bucket = Column(String(128), nullable=False)
    artifact_object_key = Column(String(1024), nullable=False)
    artifact_checksum = Column(String(128))
    artifact_size = Column(BigInteger)
    artifact_content_type = Column(String(128), default="application/zip")

    tags_json = Column(JSONB, nullable=False, default=list)
    capability_json = Column(JSONB, nullable=False, default=dict)

    def to_skill_item(self, tenant_status: str | None = None) -> dict:
        enabled = tenant_status == "enabled"
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "icon": self.icon,
            "status": self.status,
            "enabled": enabled,
            "tenant_status": tenant_status or "disabled",
            "tool_name": self.tool_name,
            "instruction": self.instruction,
            "input_schema": self.input_schema_json or {},
            "output_schema": self.output_schema_json or {},
            "tags": self.tags_json or [],
            # 租户视图不暴露运行时（调用地址、鉴权方式）与加密凭据：只有平台管理员的目录管理视图能看
            "capability": {
                k: v for k, v in (self.capability_json or {}).items()
                if k not in ("runtime", "runtime_secret")
            },
        }

    def to_mcp_tool_definition(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "tool_name": self.tool_name,
            "instruction": self.instruction,
            "input_schema": self.input_schema_json or {},
            "output_schema": self.output_schema_json or {},
            "artifact": {
                "bucket": self.artifact_bucket,
                "object_key": self.artifact_object_key,
                "checksum": self.artifact_checksum,
                "content_type": self.artifact_content_type,
            },
        }


class TenantSkill(TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    """租户技能启用状态 — 带 tenant_id"""

    __tablename__ = "tenant_skills"
    __table_args__ = {"schema": "business_domain"}

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    skill_id = Column(String(64), nullable=False)
    status = Column(String(32), nullable=False, default="disabled")