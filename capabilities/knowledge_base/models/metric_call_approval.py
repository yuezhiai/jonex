#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""指标级调用审批行 — MetricCallApproval（W4.8）。

迁移：`deploy/postgres/migrations/035_metric_call_approvals.sql`（列与本模型一一对应）。
设计：docs/superpowers/specs/2026-11-01-w4.8-metric-call-approval-design.md（工作区文档，不入库）。
"""
import uuid

from sqlalchemy import Column, String

from jonex_core.common.database import Base
from jonex_core.common.entity import SoftDeleteMixin, TenantMixin, TimestampMixin


class MetricCallApproval(TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    """一个指标在某个 KB 下的调用审批状态（pending / approved / suspended）。"""

    __tablename__ = "metric_call_approvals"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    knowledge_base_id = Column(String(128), nullable=False, index=True)
    metric_name = Column(String(255), nullable=False)
    status = Column(String(16), nullable=False, default="pending")
    requested_by = Column(String(128), nullable=True)
    decided_by = Column(String(128), nullable=True)
    reason = Column(String(500), nullable=True)


__all__ = ["MetricCallApproval"]
