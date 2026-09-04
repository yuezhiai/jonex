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
