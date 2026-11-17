#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""join 边提案仓储（与 metric 提案同一套纪律：状态机守卫 + 租户谓词）。"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Optional

from sqlalchemy import select, text

from jonex_core.common.repository import BaseRepository
from jonex_core.common.tenant import require_tenant

from ..models.join_proposal import (
    JOIN_PROPOSAL_PENDING,
    JOIN_PROPOSAL_STATUSES,
    JoinProposal,
)

logger = logging.getLogger(__name__)

_TABLE = "knowledge_base.join_proposals"


class JoinProposalRepository(BaseRepository[JoinProposal]):
    """`knowledge_base.join_proposals` 的读写。"""

    model = JoinProposal

    async def create(self, tenant_id: str, kb_id: str, *,
                     source_entity: str, source_column: str,
                     target_entity: str, target_column: str,
                     data_source_id: str = "", payload: dict,
                     created_by: str = "") -> str:
        tenant_id = require_tenant(tenant_id)
        proposal_id = uuid.uuid4().hex
        await self.session.execute(
            text(
                f"INSERT INTO {_TABLE} "
                "(id, tenant_id, knowledge_base_id, data_source_id, "
                " source_entity, source_column, target_entity, target_column, "
                " status, payload, created_by, is_deleted) "
                "VALUES (:id, :tenant_id, :kb_id, :ds_id, :se, :sc, :te, :tc, "
                "        :status, CAST(:payload AS jsonb), :created_by, 0)"
            ),
            {
                "id": proposal_id,
                "tenant_id": tenant_id,
                "kb_id": kb_id,
                "ds_id": data_source_id or "",
                "se": source_entity or "",
                "sc": source_column or "",
                "te": target_entity or "",
                "tc": target_column or "",
                "status": JOIN_PROPOSAL_PENDING,
                "payload": json.dumps(payload or {}, ensure_ascii=False,
                                      default=str),
                "created_by": created_by or "",
            },
        )
        return proposal_id

    async def get(self, tenant_id: str, proposal_id: str,
                  *, kb_id: str = "") -> Optional[JoinProposal]:
        tenant_id = require_tenant(tenant_id)
        stmt = select(JoinProposal).where(
            JoinProposal.tenant_id == tenant_id,
            JoinProposal.id == proposal_id,
            JoinProposal.is_deleted == 0,
        )
        if kb_id:
            stmt = stmt.where(JoinProposal.knowledge_base_id == kb_id)
        return (await self.session.execute(stmt)).scalars().first()

    async def list_for_kb(self, tenant_id: str, kb_id: str, *,
                          status: str = "",
                          limit: int = 100) -> list[JoinProposal]:
        tenant_id = require_tenant(tenant_id)
        stmt = select(JoinProposal).where(
            JoinProposal.tenant_id == tenant_id,
            JoinProposal.knowledge_base_id == kb_id,
            JoinProposal.is_deleted == 0,
        )
        if status:
            stmt = stmt.where(JoinProposal.status == status)
        stmt = stmt.order_by(JoinProposal.created_at.desc()).limit(
            max(1, min(int(limit), 300)))
        return list((await self.session.execute(stmt)).scalars().all())

    async def mark_applied(self, tenant_id: str, proposal_id: str,
                           relation_name: str) -> bool:
        """记录「这条提案已经落成哪条边」，供回滚与排障核对。"""
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(
            text(
                f"UPDATE {_TABLE} "
                "SET payload = jsonb_set(payload, '{applied_relation}', "
                "                        to_jsonb(CAST(:relation AS text))), "
                "    updated_at = now() "
                "WHERE tenant_id = :tenant_id AND id = :id AND is_deleted = 0"
            ),
            {"relation": relation_name or "", "tenant_id": tenant_id,
             "id": proposal_id},
        )
        return result.rowcount > 0

    async def decide(self, tenant_id: str, proposal_id: str, *,
                     decision: str, decided_by: str = "",
                     note: str = "") -> bool:
        """`pending → confirmed|rejected`；返回是否真的改了这一行。"""
        tenant_id = require_tenant(tenant_id)
        if decision not in JOIN_PROPOSAL_STATUSES or decision == JOIN_PROPOSAL_PENDING:
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
                "[join-proposal] 决定未生效（不存在或已决定） id=%s tenant=%s",
                proposal_id, tenant_id)
            return False
        return True


__all__ = ["JoinProposalRepository"]
