#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Knowledge Base — 本体实例 → 平台可执行工具的绑定。

对应表：knowledge_base.ontology_tool_bindings（deploy/postgres/update/084_ontology_skill_action_tools.sql
建表，090_ontology_tool_read_only_qa_callable.sql 加 qa_callable）
"""
from sqlalchemy import Boolean, Column, String
from sqlalchemy.dialects.postgresql import JSONB

from jonex_core.common.database import Base
from jonex_core.common.entity import SoftDeleteMixin, TenantMixin, TimestampMixin

# 表上 CHECK 约束允许的实例类型（存量的 Skill 绑定行仍合法，可停用、改映射、删除）
BINDABLE_ENTITY_TYPES = ("Skill", "Action")
# 新建绑定只接受 Action：执行器只执行单个动作；Skill 是多步规程，要执行得另做编排执行器
EXECUTABLE_ENTITY_TYPES = ("Action",)
# 工具来源（与表上的 CHECK 约束一致）：平台工具目录 / 本库工具
TOOL_SOURCE_PLATFORM = "platform"
TOOL_SOURCE_KB = "kb"
TOOL_SOURCES = (TOOL_SOURCE_PLATFORM, TOOL_SOURCE_KB)


class OntologyToolBinding(TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    """一个本体 Skill / Action 实例绑定一个工具（平台工具或本库工具，人工配置）。"""

    __tablename__ = "ontology_tool_bindings"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(String(64), primary_key=True)
    knowledge_base_id = Column(String(128), nullable=False, index=True)
    entity_type = Column(String(32), nullable=False)
    canonical_name = Column(String(512), nullable=False)
    tool_source = Column(String(16), nullable=False, default=TOOL_SOURCE_PLATFORM)
    tool_name = Column(String(128), nullable=False)
    param_mapping = Column(JSONB, nullable=False, default=dict)
    enabled = Column(Boolean, nullable=False, default=True)
    # 允许知识库问答自动调用（默认关；另需总闸开启、判定 allow、工具声明只读）
    qa_callable = Column(Boolean, nullable=False, default=False)
    created_by = Column(String(64), nullable=True)
    updated_by = Column(String(64), nullable=True)

    def to_dict(self) -> dict:
        # 对外不泄漏 tenant_id
        return {
            "id": self.id,
            "knowledge_base_id": self.knowledge_base_id,
            "entity_type": self.entity_type,
            "canonical_name": self.canonical_name,
            "tool_source": self.tool_source or TOOL_SOURCE_PLATFORM,
            "tool_name": self.tool_name,
            "param_mapping": self.param_mapping or {},
            "enabled": bool(self.enabled),
            "qa_callable": bool(self.qa_callable),
            # 只有 Action 绑定参与执行；存量 Skill 绑定是历史数据
            "executable": self.entity_type in EXECUTABLE_ENTITY_TYPES,
            "created_by": self.created_by,
            "updated_by": self.updated_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


__all__ = [
    "BINDABLE_ENTITY_TYPES",
    "EXECUTABLE_ENTITY_TYPES",
    "OntologyToolBinding",
    "TOOL_SOURCES",
    "TOOL_SOURCE_KB",
    "TOOL_SOURCE_PLATFORM",
]
