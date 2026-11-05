"""治理 P1：用户数据密级（租户级，独立于功能 RBAC）。"""
from __future__ import annotations

import uuid

from sqlalchemy import select, text

from jonex_core.common.exceptions import InvalidParameterError
from jonex_core.common.tenant import require_tenant

from ...models.governance import GovernanceUserClearance
from .levels import LEVELS


def normalize_level(level: str | None) -> str:
    value = str(level or "public").strip().lower()
    if value not in LEVELS:
        raise InvalidParameterError(f"非法 level：{value}")
    return value


class GovernanceClearanceService:
    def __init__(self, session) -> None:
        self.session = session

    async def list(self, tenant_id: str) -> dict:
        tenant_id = require_tenant(tenant_id)
        # 优先 join 平台用户表拿用户名；平台表不可用时退回只有 user_id 的清单。
        try:
            rows = (await self.session.execute(text(
                "SELECT u.id::text AS user_id, u.username, u.display_name, "
                "       COALESCE(c.level, 'public') AS level, c.id AS clearance_id, "
                "       c.granted_by, c.note, c.updated_at "
                "  FROM platform.users u "
                "  LEFT JOIN knowledge_base.governance_user_clearance c "
                "    ON c.tenant_id = u.tenant_id AND c.user_id = u.id::text "
                " WHERE u.tenant_id = :tenant AND u.is_deleted = 0 "
                " ORDER BY u.username"
            ), {"tenant": tenant_id})).mappings().all()
            items = [{
                "user_id": r["user_id"], "username": r["username"],
                "display_name": r["display_name"], "level": r["level"],
                "clearance_id": r["clearance_id"], "granted_by": r["granted_by"],
                "note": r["note"],
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            } for r in rows]
        except Exception:  # noqa: BLE001 —— 平台用户表不可用时退化为现有密级行
            stmt = select(GovernanceUserClearance).where(
                GovernanceUserClearance.tenant_id == tenant_id
            ).order_by(GovernanceUserClearance.user_id)
            rows = list((await self.session.execute(stmt)).scalars().all())
            items = [r.to_dict() for r in rows]
        return {"items": items, "total": len(items)}

    async def get(self, tenant_id: str, user_id: str | None) -> str:
        tenant_id = require_tenant(tenant_id)
        if not str(user_id or "").strip():
            return "public"
        stmt = select(GovernanceUserClearance).where(
            GovernanceUserClearance.tenant_id == tenant_id,
            GovernanceUserClearance.user_id == str(user_id),
        )
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        return str(row.level) if row is not None else "public"

    async def set(self, tenant_id: str, data: dict, *,
                  granted_by: str | None = None) -> dict:
        tenant_id = require_tenant(tenant_id)
        user_id = str((data or {}).get("user_id") or "").strip()
        if not user_id:
            raise InvalidParameterError("缺少 user_id")
        level = normalize_level((data or {}).get("level"))
        note = str((data or {}).get("note") or "").strip() or None
        stmt = select(GovernanceUserClearance).where(
            GovernanceUserClearance.tenant_id == tenant_id,
            GovernanceUserClearance.user_id == user_id,
        )
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            row = GovernanceUserClearance(
                id=str(uuid.uuid4()), tenant_id=tenant_id, user_id=user_id,
                level=level, granted_by=granted_by, note=note,
            )
            self.session.add(row)
        else:
            row.level = level
            row.granted_by = granted_by
            row.note = note
        await self.session.commit()
        await self.session.refresh(row)
        return row.to_dict()
