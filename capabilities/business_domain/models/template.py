"""
业务领域 — 业务领域模板模型
"""
import uuid

from sqlalchemy import Column, DateTime, Integer, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import JSONB

from jonex_core.common.database import Base
from jonex_core.common.entity import SoftDeleteMixin, TenantMixin, TimestampMixin


class TemplateDomain(TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "template_domains"
    __table_args__ = {"schema": "business_domain"}

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    name = Column(String(255), nullable=False)
    name_en = Column(String(255), nullable=True)
    description = Column(Text)
    status = Column(String(32), default="inactive")
    version = Column(Integer, default=1)
    published_at = Column(DateTime(timezone=True), nullable=True)
    structure_hash = Column(String(64), nullable=True)

    def to_dict(self):
        return {
            "id": self.id, "tenant_id": self.tenant_id, "name": self.name,
            "name_en": self.name_en,
            "description": self.description,
            "status": self.status,
            "version": self.version,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "structure_hash": self.structure_hash,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class TemplateScenario(TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "template_scenarios"
    __table_args__ = {"schema": "business_domain"}

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    domain_id = Column(String(64), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text)
    config_json = Column(JSONB, default=dict)
    version = Column(Integer, default=1)
    published_at = Column(DateTime(timezone=True), nullable=True)
    structure_hash = Column(String(64), nullable=True)

    def to_dict(self):
        return {
            "id": self.id, "tenant_id": self.tenant_id, "domain_id": self.domain_id,
            "name": self.name, "description": self.description,
            "config_json": self.config_json or {},
            "version": self.version,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "structure_hash": self.structure_hash,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class TemplateObject(TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "template_objects"
    __table_args__ = {"schema": "business_domain"}

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    domain_id = Column(String(64), nullable=False, index=True)
    scenario_id = Column(String(64), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text)
    status = Column(String(32), default="draft")
    ontology_code = Column(String(128), nullable=True)
    aliases = Column(JSONB, default=list)

    def to_dict(self):
        return {
            "id": self.id, "tenant_id": self.tenant_id, "domain_id": self.domain_id,
            "scenario_id": self.scenario_id,
            "name": self.name, "description": self.description,
            "status": self.status,
            "ontology_code": self.ontology_code,
            "aliases": self.aliases or [],
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class TemplateAttribute(TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "template_attributes"
    __table_args__ = {"schema": "business_domain"}

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    template_object_id = Column(String(64), nullable=False, index=True)
    attr_name = Column(String(255), nullable=False)
    description = Column(Text)
    attr_type = Column(String(64), default="string")
    is_primary_key = Column(SmallInteger, default=0)
    constraints_json = Column(JSONB, default=dict)
    sort_order = Column(Integer, default=0)
    ontology_code = Column(String(128), nullable=True)
    is_required = Column(SmallInteger, default=0)

    def to_dict(self):
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "template_object_id": self.template_object_id,
            "attr_name": self.attr_name,
            "description": self.description,
            "attr_type": self.attr_type,
            "is_primary_key": self.is_primary_key,
            "constraints_json": self.constraints_json or {},
            "sort_order": self.sort_order,
            "ontology_code": self.ontology_code,
            "is_required": self.is_required,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class TemplateRelation(TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "template_relations"
    __table_args__ = {"schema": "business_domain"}

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    domain_id = Column(String(64), nullable=False, index=True)
    scenario_id = Column(String(64), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text)
    source_object_id = Column(String(64))
    target_object_id = Column(String(64))
    relation_type = Column(String(32), default="custom")
    status = Column(String(32), default="draft")
    ontology_code = Column(String(128), nullable=True)
    aliases = Column(JSONB, default=list)

    def to_dict(self):
        return {
            "id": self.id, "tenant_id": self.tenant_id, "domain_id": self.domain_id,
            "scenario_id": self.scenario_id,
            "name": self.name,
            "description": self.description,
            "source_object_id": self.source_object_id,
            "target_object_id": self.target_object_id,
            "relation_type": self.relation_type,
            "status": self.status,
            "ontology_code": self.ontology_code,
            "aliases": self.aliases or [],
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class TemplateConstraint(TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "template_constraints"
    __table_args__ = {"schema": "business_domain"}

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    domain_id = Column(String(64), nullable=False, index=True)
    scenario_id = Column(String(64), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    target_type = Column(String(32), nullable=False)
    target_id = Column(String(64), nullable=False)
    target_label = Column(String(255), nullable=False)
    constraint_type = Column(String(32), nullable=False)
    expression = Column(Text)
    suggestion = Column(Text)

    def to_dict(self):
        return {
            "id": self.id, "tenant_id": self.tenant_id,
            "domain_id": self.domain_id, "scenario_id": self.scenario_id,
            "name": self.name,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "target_label": self.target_label,
            "constraint_type": self.constraint_type,
            "expression": self.expression,
            "suggestion": self.suggestion,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class TemplateScenarioVersion(TenantMixin, Base):
    """模板场景的发布快照。

    每次 `publish_scenario` 成功时写入一行，`version` 与 `template_scenarios.version`
    对齐；`snapshot` 保存发布当时的完整结构（场景 + 对象/属性 + 关系 + 约束），
    用于历史查看与后续 diff / 落标校验。**只写不改**：不提供更新/删除。
    """

    __tablename__ = "template_scenario_versions"
    __table_args__ = {"schema": "business_domain"}

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    domain_id = Column(String(64), nullable=False, index=True)
    scenario_id = Column(String(64), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    structure_hash = Column(String(64), nullable=False)
    snapshot = Column(JSONB, nullable=False, default=dict)
    published_by = Column(String(128), nullable=True)
    published_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)

    def to_dict(self, *, include_snapshot: bool = False):
        data = {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "domain_id": self.domain_id,
            "scenario_id": self.scenario_id,
            "version": self.version,
            "structure_hash": self.structure_hash,
            "published_by": self.published_by,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
        if include_snapshot:
            data["snapshot"] = self.snapshot or {}
        return data


class TemplatePublishRequest(TenantMixin, Base):
    """模板场景发布申请（强制审批）。

    普通编辑提交申请（同时冻结一份 candidate snapshot）；租户管理员审批时
    与当前内容比对：内容已变 ⇒ 409（防止「审的和发的不一致」）。
    审批通过后由 `TemplatePublishService.publish_scenario` 落版本快照。
    """

    __tablename__ = "template_publish_requests"
    __table_args__ = {"schema": "business_domain"}

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    domain_id = Column(String(64), nullable=False, index=True)
    scenario_id = Column(String(64), nullable=False, index=True)
    status = Column(String(16), nullable=False, default="pending")
    structure_hash = Column(String(64), nullable=False)
    snapshot = Column(JSONB, nullable=False, default=dict)
    submitted_by = Column(String(128), nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=False)
    decided_by = Column(String(128), nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    comment = Column(Text)
    published_version = Column(Integer, nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False)

    def to_dict(self, *, include_snapshot: bool = False):
        data = {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "domain_id": self.domain_id,
            "scenario_id": self.scenario_id,
            "status": self.status,
            "structure_hash": self.structure_hash,
            "submitted_by": self.submitted_by,
            "submitted_at": (
                self.submitted_at.isoformat() if self.submitted_at else None
            ),
            "decided_by": self.decided_by,
            "decided_at": (
                self.decided_at.isoformat() if self.decided_at else None
            ),
            "comment": self.comment,
            "published_version": self.published_version,
            "updated_at": (
                self.updated_at.isoformat() if self.updated_at else None
            ),
        }
        if include_snapshot:
            data["snapshot"] = self.snapshot or {}
        return data
