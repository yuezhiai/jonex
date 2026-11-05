#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Knowledge Base — 血缘「禁止同查」表对。

对应表：knowledge_base.lineage_forbidden_pairs（迁移 013）。

不是数据流边：没有列、没有方向，也不进路径图。`status=active` 才生效；
`proposed` 是 1 跳血缘生成的建议，`disabled` 是人工停用/忽略。
"""
from sqlalchemy import Column, String

from jonex_core.common.database import Base
from jonex_core.common.entity import SoftDeleteMixin, TenantMixin, TimestampMixin


class LineageForbiddenPair(TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    """一对禁止在同一查询中同时出现的物理表。"""

    __tablename__ = "lineage_forbidden_pairs"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(String(64), primary_key=True)
    knowledge_base_id = Column(String(128), nullable=False, index=True)
    data_source_id = Column(String(64), nullable=False)
    table_a = Column(String(255), nullable=False)
    table_b = Column(String(255), nullable=False)
    origin = Column(String(32), nullable=False, default="manual")
    status = Column(String(16), nullable=False, default="active")
    note = Column(String(500), nullable=True)
    created_by = Column(String(64), nullable=True)

    def to_dict(self) -> dict:
        # 对外不泄漏 tenant_id（同 LineageEdge.to_dict 口径）。
        return {
            "id": self.id,
            "knowledge_base_id": self.knowledge_base_id,
            "data_source_id": self.data_source_id or "",
            "table_a": self.table_a,
            "table_b": self.table_b,
            "origin": self.origin or "manual",
            "status": self.status or "active",
            "note": self.note,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


__all__ = ["LineageForbiddenPair"]
