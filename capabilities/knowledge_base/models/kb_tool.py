#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Knowledge Base — 知识库工具（本库登记的可执行 API 工具）。

对应表：knowledge_base.kb_tools（deploy/postgres/update/082_ontology_skill_action_tools.sql）
"""
from sqlalchemy import Boolean, Column, String, Text
from sqlalchemy.dialects.postgresql import JSONB

from jonex_core.common.database import Base
from jonex_core.common.entity import SoftDeleteMixin, TenantMixin, TimestampMixin
from jonex_core.common.tool_runtime import runtime_brief


class KbTool(TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    """一个知识库内的可执行工具；运行时与平台工具同格式，凭据加密存储。"""

    __tablename__ = "kb_tools"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(String(64), primary_key=True)
    knowledge_base_id = Column(String(128), nullable=False, index=True)
    tool_name = Column(String(128), nullable=False)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    input_schema = Column(JSONB, nullable=False, default=dict)
    output_schema = Column(JSONB, nullable=False, default=dict)
    runtime = Column(JSONB, nullable=True)
    secret_encrypted = Column(Text, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    # 登记人显式声明「调用不改变对方系统数据」；默认 false（按写操作对待）
    read_only = Column(Boolean, nullable=False, default=False)
    created_by = Column(String(64), nullable=True)
    updated_by = Column(String(64), nullable=True)

    def to_dict(self) -> dict:
        # 对外不泄漏 tenant_id 与凭据（明文、密文都不回）
        return {
            "id": self.id,
            "knowledge_base_id": self.knowledge_base_id,
            "tool_name": self.tool_name,
            "name": self.name,
            "description": self.description or "",
            "input_schema": self.input_schema or {},
            "output_schema": self.output_schema or {},
            "runtime": self.runtime,
            "runtime_brief": runtime_brief(self.runtime),
            "has_secret": bool(self.secret_encrypted),
            "enabled": bool(self.enabled),
            "read_only": bool(self.read_only),
            "created_by": self.created_by,
            "updated_by": self.updated_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


__all__ = ["KbTool"]
