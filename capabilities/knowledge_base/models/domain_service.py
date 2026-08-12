"""
知识库能力 — 领域服务模型
"""
import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, SmallInteger, String, Text

from jonex_core.common.database import Base
from jonex_core.common.entity import SoftDeleteMixin, TenantMixin, TimestampMixin


class DomainService(TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "services"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    space_id = Column(String(64), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text)
    domain_type = Column(String(64))
    status = Column(String(32), default="active")
    api_key_encrypted = Column(String(512))
    enabled = Column(SmallInteger, default=1)

    def to_dict(self, include_kb_ids: list[str] | None = None, space_name: str = "", kb_names: dict[str, str] | None = None):
        kb_ids = include_kb_ids or []
        result = {
            "id": self.id, "tenant_id": self.tenant_id,
            "space_id": self.space_id,
            "name": self.name, "description": self.description,
            "domain_type": self.domain_type,
            "status": self.status,
            "api_key_encrypted": self.api_key_encrypted,
            "enabled": self.enabled,
            "kb_ids": kb_ids,
            "space_name": space_name,
            "kb_names": [kb_names.get(kid, kid) for kid in kb_ids] if kb_names else kb_ids,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        return result


class ServiceKnowledgeBase(TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "service_knowledge_bases"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    service_id = Column(String(64), nullable=False, index=True)
    kb_id = Column(String(64), nullable=False)
    pipeline_type = Column(String(16), nullable=False, default="lightrag")  # [jonex] lightrag / openkb


class ServiceApiKey(TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "service_api_keys"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    service_id = Column(String(64), nullable=False, index=True)
    key_prefix = Column(String(16), nullable=False, default="sk")
    key_encrypted = Column(String(512), nullable=False)
    expires_at = Column(DateTime, nullable=True)
    is_active = Column(SmallInteger, default=1)

    def to_dict(self):
        return {
            "id": self.id,
            "service_id": self.service_id,
            "key_prefix": self.key_prefix,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "is_active": self.is_active,
            "key_encrypted": self.key_encrypted,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class ServiceConfig(TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "service_configs"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    service_id = Column(String(64), nullable=False, index=True)
    config_key = Column(String(128), nullable=False)
    config_value = Column(Text)
    description = Column(String(512))


class ServicePermission(TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "service_permissions"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    service_id = Column(String(64), nullable=False, index=True)
    user_id = Column(String(64), nullable=False, index=True)
    role = Column(String(32), nullable=False, default="viewer")
