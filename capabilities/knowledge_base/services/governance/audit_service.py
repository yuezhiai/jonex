"""治理 P1：判定审计写入与查询。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from jonex_core.common.tenant import require_tenant

from ...models.governance import GovernanceDecisionAudit


class GovernanceAuditService:
    def __init__(self, session) -> None:
        self.session = session

    async def record(self, tenant_id: str, *, knowledge_base_id: str,
                     action: str, decision: str,
                     request_id: str | None = None, user_id: str | None = None,
                     data_source_id: str | None = None,
                     resource_table: str | None = None,
                     resource_column: str | None = None,
                     level: str | None = None, policy_id: str | None = None,
                     reason: str | None = None, sql_hash: str | None = None) -> str:
        tenant_id = require_tenant(tenant_id)
        audit_id = str(uuid.uuid4())
        self.session.add(GovernanceDecisionAudit(
            id=audit_id, tenant_id=tenant_id, request_id=request_id,
            user_id=user_id, knowledge_base_id=knowledge_base_id,
            data_source_id=data_source_id, action=action,
            resource_table=resource_table, resource_column=resource_column,
            level=level, decision=decision, policy_id=policy_id,
            reason=reason, sql_hash=sql_hash,
            created_at=datetime.now(timezone.utc),
        ))
        await self.session.commit()
        return audit_id

    async def record_many(self, tenant_id: str, knowledge_base_id: str,
                          decisions: list[dict], *, request_id: str | None = None,
                          user_id: str | None = None,
                          data_source_id: str | None = None,
                          sql_hash: str | None = None) -> int:
        """批量写判定；`row_filters` 非空的 allow 额外记一条 row_filter。"""
        tenant_id = require_tenant(tenant_id)
        now = datetime.now(timezone.utc)
        count = 0
        for d in decisions or ():
            effect = str(d.get("effect") or "allow")
            base = dict(
                tenant_id=tenant_id, request_id=request_id, user_id=user_id,
                knowledge_base_id=knowledge_base_id, data_source_id=data_source_id,
                action=str(d.get("action") or "select"),
                resource_table=d.get("table"), resource_column=d.get("column"),
                level=d.get("level"), policy_id=d.get("policy_id"),
                reason=(str(d.get("reason") or "")[:500] or None),
                sql_hash=sql_hash, created_at=now,
            )
            self.session.add(GovernanceDecisionAudit(
                id=str(uuid.uuid4()), decision=effect, **base))
            count += 1
            if effect == "allow" and d.get("row_filters"):
                # base 里已经有 reason；row_filter 这条要换成自己的文案，
                # 不能 `reason=..., **base`（会 duplicate keyword：真机每次带行过滤
                # 的查询都写审计失败，日志刷 ERROR 但查询照常）。
                row_base = {k: v for k, v in base.items() if k != "reason"}
                self.session.add(GovernanceDecisionAudit(
                    id=str(uuid.uuid4()), decision="row_filter",
                    reason=(str(d.get("reason") or "行过滤")[:500]), **row_base))
                count += 1
        await self.session.commit()
        return count

    async def list(self, tenant_id: str, kb_id: str, *,
                   user_id: str | None = None, decision: str | None = None,
                   action: str | None = None, resource_table: str | None = None,
                   offset: int = 0, limit: int = 50) -> dict:
        tenant_id = require_tenant(tenant_id)
        stmt = select(GovernanceDecisionAudit).where(
            GovernanceDecisionAudit.tenant_id == tenant_id,
            GovernanceDecisionAudit.knowledge_base_id == kb_id,
        )
        if user_id:
            stmt = stmt.where(GovernanceDecisionAudit.user_id == user_id)
        if decision:
            stmt = stmt.where(GovernanceDecisionAudit.decision == decision)
        if action:
            stmt = stmt.where(GovernanceDecisionAudit.action == action)
        if resource_table:
            stmt = stmt.where(GovernanceDecisionAudit.resource_table == resource_table)
        total = len(list((await self.session.execute(stmt)).scalars().all()))
        stmt = stmt.order_by(GovernanceDecisionAudit.created_at.desc()).offset(
            max(int(offset), 0)).limit(min(max(int(limit), 1), 200))
        rows = list((await self.session.execute(stmt)).scalars().all())
        return {"items": [r.to_dict() for r in rows], "total": total}
