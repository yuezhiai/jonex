#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Knowledge Base — 血缘边（列级）。

对应表：knowledge_base.lineage_edges（迁移 010）。

**逐条 CRUD**，不是 KB 级单文档：所以没有 `schema_version`、没有乐观锁、也不产生
409 冲突信号（对比 `ontology_abstract_schema` 那种「一库一份」的形态）。
每一行就是一条边：`from_table.from_column → to_table.to_column`。
`data_source_id` 必填：表名是**物理表名**，跨源没有意义（② 的边池按单源构建）。
"""
from sqlalchemy import Column, DateTime, String

from jonex_core.common.database import Base
from jonex_core.common.entity import SoftDeleteMixin, TenantMixin, TimestampMixin


class LineageEdge(TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    """一条列级血缘边。"""

    __tablename__ = "lineage_edges"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(String(64), primary_key=True)
    knowledge_base_id = Column(String(128), nullable=False, index=True)
    data_source_id = Column(String(64), nullable=False, default="")
    from_table = Column(String(255), nullable=False)
    from_column = Column(String(255), nullable=False)
    to_table = Column(String(255), nullable=False)
    to_column = Column(String(255), nullable=False)
    origin = Column(String(32), nullable=False, default="manual")
    note = Column(String(500), nullable=True)
    created_by = Column(String(64), nullable=True)
    #: 来源最后一次确认该边的时间（迁移 012）。读取时对 platform 来源按滚动窗口标 stale。
    last_seen_at = Column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict:
        # 对外不泄漏 tenant_id（同 OntologySynonym.to_dict 口径）。
        return {
            "id": self.id,
            "knowledge_base_id": self.knowledge_base_id,
            "data_source_id": self.data_source_id or "",
            "from_table": self.from_table,
            "from_column": self.from_column,
            "to_table": self.to_table,
            "to_column": self.to_column,
            "origin": self.origin or "manual",
            "note": self.note,
            "created_by": self.created_by,
            "last_seen_at": (
                self.last_seen_at.isoformat() if self.last_seen_at else None),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


__all__ = ["LineageEdge"]
