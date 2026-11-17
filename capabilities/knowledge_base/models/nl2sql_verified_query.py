#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""② NL→SQL — 已验证查询（verified query，N1-1）。

对应表：`knowledge_base.nl2sql_verified_queries`（迁移 106 / 全量种子 038）。

一条记录 = 一个「高频、人工确认过」的问句集合 + 一份带参 SQL 模板。命中后走快路径
（渲染 → guard → 治理 → 执行），不调 LLM。绑定单个数据源：表名是物理表名，
跨源没有意义（同 `LineageEdge` 的口径）。

状态机：`draft → active → retired`；`retired` 可再激活（`retire` 不删数据）。
`version` 是乐观锁：每次写 +1，冲突由仓储映射成 1003。
"""
from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB

from jonex_core.common.database import Base
from jonex_core.common.entity import SoftDeleteMixin, TenantMixin, TimestampMixin

STATUS_DRAFT = "draft"
STATUS_ACTIVE = "active"
STATUS_RETIRED = "retired"
STATUSES = (STATUS_DRAFT, STATUS_ACTIVE, STATUS_RETIRED)


class Nl2SqlVerifiedQuery(TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    """一条带参 SQL 模板 + 它覆盖的问句集合。"""

    __tablename__ = "nl2sql_verified_queries"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(String(64), primary_key=True)
    knowledge_base_id = Column(String(128), nullable=False, index=True)
    data_source_id = Column(String(64), nullable=False, default="")
    name = Column(String(128), nullable=False)
    description = Column(String(500), nullable=False, default="")
    questions = Column(JSONB, nullable=False, default=list)
    sql_template = Column(Text, nullable=False, default="")
    params = Column(JSONB, nullable=False, default=list)
    status = Column(String(16), nullable=False, default=STATUS_DRAFT)
    version = Column(Integer, nullable=False, default=1)
    created_by = Column(String(64), nullable=True)
    updated_by = Column(String(64), nullable=True)
    activated_by = Column(String(64), nullable=True)
    activated_at = Column(DateTime(timezone=True), nullable=True)

    def to_dict(self, *, include_sql: bool = True) -> dict:
        """对外形状；不泄漏 `tenant_id`（同其它 KB 资源口径）。"""
        out = {
            "id": self.id,
            "knowledge_base_id": self.knowledge_base_id,
            "data_source_id": self.data_source_id or "",
            "name": self.name,
            "description": self.description or "",
            "questions": list(self.questions or []),
            "params": list(self.params or []),
            "status": self.status,
            "version": self.version,
            "created_by": self.created_by,
            "updated_by": self.updated_by,
            "activated_by": self.activated_by,
            "activated_at": (
                self.activated_at.isoformat() if self.activated_at else None),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_sql:
            out["sql_template"] = self.sql_template or ""
        return out


__all__ = [
    "Nl2SqlVerifiedQuery",
    "STATUS_DRAFT",
    "STATUS_ACTIVE",
    "STATUS_RETIRED",
    "STATUSES",
]
