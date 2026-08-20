"""
业务领域 — 引擎管理模型（数据接入/解析器/模型供应商）
"""
import uuid

from sqlalchemy import Column, Integer, String
from sqlalchemy.dialects.postgresql import JSONB

from jonex_core.common.database import Base
from jonex_core.common.entity import SoftDeleteMixin, TimestampMixin


class DataAccessMethod(TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "data_access_methods"
    __table_args__ = {"schema": "business_domain"}

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    name = Column(String(255), nullable=False)
    access_type = Column(String(32), nullable=False)
    config_json = Column(JSONB, default=dict)
    status = Column(String(32), default="active")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name, "access_type": self.access_type,
            "description": (self.config_json or {}).get("description", ""),
            "config_json": self.config_json or {},
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ParserConfig(TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "parser_configs"
    __table_args__ = {"schema": "business_domain"}

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    name = Column(String(255), nullable=False)
    parser_type = Column(String(32), nullable=False)
    file_types = Column(JSONB, default=list)
    config_json = Column(JSONB, default=dict)
    status = Column(String(32), default="active")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name, "parser_type": self.parser_type,
            "file_types": self.file_types or [],
            "config_json": self.config_json or {},
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ModelProvider(TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "model_providers"
    __table_args__ = {"schema": "business_domain"}

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    name = Column(String(255), nullable=False)
    provider_type = Column(String(32), nullable=False)
    model_type = Column(String(32))
    endpoint = Column(String(512))
    api_key_encrypted = Column(String(512))
    model_name = Column(String(255))
    vector_dimension = Column(Integer)
    token_limit = Column(Integer)
    latency_ms = Column(Integer)
    call_count = Column(Integer, default=0)
    success_rate = Column(Integer, default=0)  # 0-100
    status = Column(String(32), default="active")
    config_json = Column(JSONB, default=dict)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name, "provider_type": self.provider_type,
            "model_type": self.model_type,
            "endpoint": self.endpoint,
            "model_name": self.model_name,
            "vector_dimension": self.vector_dimension,
            "token_limit": self.token_limit,
            "latency_ms": self.latency_ms,
            "call_count": self.call_count,
            "success_rate": self.success_rate,
            "status": self.status,
            "config_json": self.config_json or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
