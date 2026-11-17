#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""指标提案仓储。

写走 `text()` + 具名参数（与落点 run 仓储同一手法）：决定动作是状态机，
`AND status='pending'` 是守卫不是优化 —— 少了它，一条已确认的提案能被再判一次，
「谁确认的」就被最后一次点按钮的人覆盖。

读走 `select()`，直接回 ORM 对象（`to_dict()` 在模型上）。
所有语句都带 `tenant_id` + `is_deleted = 0`。
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Optional

from sqlalchemy import select, text

from jonex_core.common.repository import BaseRepository
from jonex_core.common.tenant import require_tenant

from ..models.metric_proposal import (
    PROPOSAL_PENDING,
    PROPOSAL_STATUSES,
    MetricProposal,
)

logger = logging.getLogger(__name__)

_TABLE = "knowledge_base.metric_proposals"


class MetricProposalRepository(BaseRepository[MetricProposal]):
    """`knowledge_base.metric_proposals` 的读写。"""

    model = MetricProposal

    async def create(self, tenant_id: str, kb_id: str, *,
                     name: str, data_source_id: str = "", payload: dict,
                     created_by: str = "") -> str:
        tenant_id = require_tenant(tenant_id)
        proposal_id = uuid.uuid4().hex
        await self.session.execute(
            text(
                f"INSERT INTO {_TABLE} "
                "(id, tenant_id, knowledge_base_id, data_source_id, name, "
                " status, payload, created_by, is_deleted) "
                "VALUES (:id, :tenant_id, :kb_id, :ds_id, :name, "
                "        :status, CAST(:payload AS jsonb), :created_by, 0)"
            ),
            {
                "id": proposal_id,
                "tenant_id": tenant_id,
                "kb_id": kb_id,
                "ds_id": data_source_id or "",
                "name": name,
                "status": PROPOSAL_PENDING,
                "payload": json.dumps(payload or {}, ensure_ascii=False,
                                      default=str),
                "created_by": created_by or "",
            },
        )
        return proposal_id

    async def get(self, tenant_id: str, proposal_id: str,
                  *, kb_id: str = "") -> Optional[MetricProposal]:
        tenant_id = require_tenant(tenant_id)
        stmt = select(MetricProposal).where(
            MetricProposal.tenant_id == tenant_id,
            MetricProposal.id == proposal_id,
            MetricProposal.is_deleted == 0,
        )
        if kb_id:
            stmt = stmt.where(MetricProposal.knowledge_base_id == kb_id)
        return (await self.session.execute(stmt)).scalars().first()

    async def list_for_kb(self, tenant_id: str, kb_id: str, *,
                          status: str = "", limit: int = 50) -> list[MetricProposal]:
        tenant_id = require_tenant(tenant_id)
        stmt = select(MetricProposal).where(
            MetricProposal.tenant_id == tenant_id,
            MetricProposal.knowledge_base_id == kb_id,
            MetricProposal.is_deleted == 0,
        )
        if status:
            stmt = stmt.where(MetricProposal.status == status)
        stmt = stmt.order_by(MetricProposal.created_at.desc()).limit(
            max(1, min(int(limit), 200)))
        return list((await self.session.execute(stmt)).scalars().all())

    async def decide(self, tenant_id: str, proposal_id: str, *,
                     decision: str, decided_by: str = "",
                     note: str = "") -> bool:
        """`pending → confirmed|rejected`；返回是否真的改了这一行。

        非 pending 的行一律不动（返回 False）：重复点击、两个标签页同时点，
        都只有第一次生效。
        """
        tenant_id = require_tenant(tenant_id)
        if decision not in PROPOSAL_STATUSES or decision == PROPOSAL_PENDING:
            raise ValueError(f"非法决定：{decision}")
        result = await self.session.execute(
            text(
                f"UPDATE {_TABLE} SET status = :status, decided_by = :by, "
                "decided_at = now(), decision_note = :note, updated_at = now() "
                "WHERE tenant_id = :tenant_id AND id = :id "
                "  AND status = 'pending' AND is_deleted = 0"
            ),
            {
                "status": decision,
                "by": decided_by or "",
                "note": note or "",
                "tenant_id": tenant_id,
                "id": proposal_id,
            },
        )
        if result.rowcount == 0:
            logger.warning(
                "[metric-proposal] 决定未生效（不存在或已决定） id=%s tenant=%s",
                proposal_id, tenant_id)
            return False
        return True


__all__ = ["MetricProposalRepository"]
