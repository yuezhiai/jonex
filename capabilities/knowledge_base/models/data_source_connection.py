#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""平台级数据源连接池模型 — DataSourceConnection。

与 `KnowledgeDataSource` 的关系：那张表是「知识库引用」，这张是「连接本体」。
一个连接可被多个知识库引用，**凭证只在这一张表里**（见 039 迁移的注释）。
"""
import uuid

from sqlalchemy import Column, String
from sqlalchemy.dialects.postgresql import JSONB

from jonex_core.common.database import Base
from jonex_core.common.entity import SoftDeleteMixin, TenantMixin, TimestampMixin

from .data_source import SECRET_KEYS, mask_secrets


class DataSourceConnection(TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    """领域级连接（目前只有 database；同一领域内可被多个知识库引用）。"""

    __tablename__ = "data_source_connections"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    name = Column(String(255), nullable=False)
    access_type = Column(String(32), nullable=False, default="database")
    config_json = Column(JSONB, nullable=False, default=dict)
    status = Column(String(32), nullable=False, default="active")
    # 所属领域空间：连接只在同一领域内可见、可被引用（2026-11-02）。
    # 历史连接由迁移按「引用它的知识库空间」回填，见 036/089。
    space_id = Column(String(64), nullable=True, index=True)

    _SECRET_KEYS = SECRET_KEYS

    def public_config(self) -> dict:
        """脱敏后的配置。密码密文永远不出现在这里。"""
        return mask_secrets(self.config_json)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "access_type": self.access_type,
            "config_json": self.public_config(),
            "status": self.status,
            "space_id": self.space_id or "",
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


__all__ = ["DataSourceConnection"]
