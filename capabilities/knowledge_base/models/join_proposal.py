#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""join 边提案 ORM。

对应表：`knowledge_base.join_proposals`（增量迁移 `update/20261115100000`）。

为什么单独一张表而不是复用 `metric_proposals`：两者的业务对象、确认后的
落地动作完全不同（指标进抽象 schema 的 metrics[]，join 边进血缘/关系边），
混在一张表里只会让「payload 长什么样」靠 kind 猜。

状态机：`pending → confirmed | rejected`，一次性；改主意请重新生成。
"""
from sqlalchemy import Column, DateTime, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB

from jonex_core.common.database import Base
from jonex_core.common.entity import SoftDeleteMixin, TenantMixin, TimestampMixin

JOIN_PROPOSAL_PENDING = "pending"
JOIN_PROPOSAL_CONFIRMED = "confirmed"
JOIN_PROPOSAL_REJECTED = "rejected"
JOIN_PROPOSAL_STATUSES = (JOIN_PROPOSAL_PENDING, JOIN_PROPOSAL_CONFIRMED,
                          JOIN_PROPOSAL_REJECTED)


class JoinProposal(TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    """一条 join 边提案：源列 → 目标列，含实测重叠率与依据。"""

    __tablename__ = "join_proposals"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(String(64), primary_key=True)
    knowledge_base_id = Column(String(128), nullable=False)
    data_source_id = Column(String(128), nullable=False, default="",
                            server_default=text("''"))
    source_entity = Column(String(256), nullable=False)
    source_column = Column(String(256), nullable=False)
    target_entity = Column(String(256), nullable=False)
    target_column = Column(String(256), nullable=False)
    status = Column(String(32), nullable=False, default=JOIN_PROPOSAL_PENDING,
                    server_default=text("'pending'"))
    #: 完整候选证据：discovery 判词 / 实测 ratio / 样本 / rationale / confidence。
    payload = Column(JSONB, nullable=False, default=dict,
                     server_default=text("'{}'::jsonb"))
    created_by = Column(String(128), nullable=False, default="",
                        server_default=text("''"))
    decided_by = Column(String(128), nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    decision_note = Column(Text, nullable=False, default="",
                           server_default=text("''"))

    def to_dict(self) -> dict:
        payload = self.payload if isinstance(self.payload, dict) else {}
        return {
            "proposal_id": self.id,
            "knowledge_base_id": self.knowledge_base_id,
            "data_source_id": self.data_source_id,
            "source_entity": self.source_entity,
            "source_column": self.source_column,
            "target_entity": self.target_entity,
            "target_column": self.target_column,
            "status": self.status,
            "created_by": self.created_by or "",
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "decided_by": self.decided_by or "",
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
            "decision_note": self.decision_note or "",
            **payload,
        }


__all__ = [
    "JoinProposal", "JOIN_PROPOSAL_CONFIRMED", "JOIN_PROPOSAL_PENDING",
    "JOIN_PROPOSAL_REJECTED", "JOIN_PROPOSAL_STATUSES",
]
