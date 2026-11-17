#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""指标提案 ORM。

对应表：`knowledge_base.metric_proposals`（增量迁移 `update/20261114210000`）。

为什么提案要落表：指标 agent 的产出**不是指标本身**，是一份等人确认的提案。
确认/驳回要留痕（谁、何时、什么理由），没被确认的提案不能影响任何检索或问数。
把它存在 JSONL 里既没有租户边界也没有事务，只适合做原型。

状态机：`pending → confirmed | rejected`，一次性；改主意请重新生成一份。
"""
from sqlalchemy import Column, DateTime, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB

from jonex_core.common.database import Base
from jonex_core.common.entity import SoftDeleteMixin, TenantMixin, TimestampMixin

PROPOSAL_PENDING = "pending"
PROPOSAL_CONFIRMED = "confirmed"
PROPOSAL_REJECTED = "rejected"
PROPOSAL_STATUSES = (PROPOSAL_PENDING, PROPOSAL_CONFIRMED, PROPOSAL_REJECTED)


class MetricProposal(TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    """一条指标提案：公式、粒度、过滤、依据，以及人工决定。"""

    __tablename__ = "metric_proposals"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(String(64), primary_key=True)
    knowledge_base_id = Column(String(128), nullable=False)
    #: 提案默认落在这一个数据源上；跨源指标一期不做（与编译器的约束一致）。
    data_source_id = Column(String(128), nullable=False, default="",
                            server_default=text("''"))
    name = Column(String(256), nullable=False)
    status = Column(String(32), nullable=False, default=PROPOSAL_PENDING,
                    server_default=text("'pending'"))
    #: 完整提案（`formula_sql` / `grain` / `filters` / `rationale` /
    #: `confidence` / `usage` / `steps`）。整体存 JSONB：字段还会长，
    #: 每加一个就改表不划算；需要索引的只有 name/status，已单列。
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
            "name": self.name,
            "status": self.status,
            "created_by": self.created_by or "",
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "decided_by": self.decided_by or "",
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
            "decision_note": self.decision_note or "",
            **payload,
        }


__all__ = [
    "MetricProposal", "PROPOSAL_CONFIRMED", "PROPOSAL_PENDING",
    "PROPOSAL_REJECTED", "PROPOSAL_STATUSES",
]
