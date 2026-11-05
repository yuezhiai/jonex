#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""MetricCallApproval 仓储（W4.8）。"""
from sqlalchemy import select

from jonex_core.common.repository import BaseRepository

from ..models.metric_call_approval import MetricCallApproval


class MetricCallApprovalRepository(BaseRepository[MetricCallApproval]):
    model = MetricCallApproval

    async def get_for_metric(self, tenant_id: str, kb_id: str,
                             metric_name: str):
        stmt = select(MetricCallApproval).where(
            MetricCallApproval.tenant_id == tenant_id,
            MetricCallApproval.knowledge_base_id == kb_id,
            MetricCallApproval.metric_name == metric_name,
            MetricCallApproval.is_deleted == 0,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_for_kb(self, tenant_id: str, kb_id: str,
                          *, status: str | None = None):
        conditions = [
            MetricCallApproval.tenant_id == tenant_id,
            MetricCallApproval.knowledge_base_id == kb_id,
            MetricCallApproval.is_deleted == 0,
        ]
        if status:
            conditions.append(MetricCallApproval.status == status)
        stmt = (select(MetricCallApproval).where(*conditions)
                .order_by(MetricCallApproval.metric_name))
        result = await self.session.execute(stmt)
        return list(result.scalars().all())


__all__ = ["MetricCallApprovalRepository"]
