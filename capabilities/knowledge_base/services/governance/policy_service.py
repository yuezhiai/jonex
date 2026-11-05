"""治理 P1：策略 CRUD（allow/deny/mask/row_filter）。"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select

from jonex_core.common.exceptions import InvalidParameterError, ResourceNotFoundError
from jonex_core.common.tenant import require_tenant

from ...models.governance import GovernancePolicy
from .common import ensure_kb
from .levels import LEVELS

_EFFECTS = frozenset({"allow", "deny", "mask", "row_filter"})
_RESOURCE_TYPES = frozenset({"table", "column", "tag"})
_MASK_TYPES = frozenset({"null", "redact"})


def _dt(raw) -> datetime | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError as exc:
        raise InvalidParameterError(f"非法时间：{raw}") from exc


def normalize_policy_payload(data: dict) -> dict:
    d = data or {}
    name = str(d.get("name") or "").strip()
    if not name:
        raise InvalidParameterError("策略名不能为空")
    if len(name) > 128:
        raise InvalidParameterError("策略名超长（最多 128 字符）")
    effect = str(d.get("effect") or "").strip().lower()
    if effect not in _EFFECTS:
        raise InvalidParameterError(f"非法 effect：{effect}")
    rtype = str(d.get("resource_type") or "table").strip().lower()
    if rtype not in _RESOURCE_TYPES:
        raise InvalidParameterError(f"非法 resource_type：{rtype}")
    min_clearance = str(d.get("min_clearance") or "public").strip().lower()
    if min_clearance not in LEVELS:
        raise InvalidParameterError(f"非法 min_clearance：{min_clearance}")
    mask_type = str(d.get("mask_type") or "").strip().lower() or None
    if mask_type is not None and mask_type not in _MASK_TYPES:
        raise InvalidParameterError(f"非法 mask_type：{mask_type}")
    row_filter = str(d.get("row_filter") or "").strip() or None
    if row_filter is not None and len(row_filter) > 1000:
        raise InvalidParameterError("row_filter 超长（最多 1000 字符）")
    if effect == "row_filter" and not row_filter:
        raise InvalidParameterError("row_filter 策略必须填写过滤表达式")
    table_pattern = str(d.get("table_pattern") or "").strip() or None
    column_pattern = str(d.get("column_pattern") or "").strip() or None
    tag_name = str(d.get("tag_name") or "").strip() or None
    if rtype in ("table", "column") and not table_pattern:
        raise InvalidParameterError("table/column 策略必须填写 table_pattern")
    if rtype == "column" and not column_pattern:
        raise InvalidParameterError("column 策略必须填写 column_pattern")
    if rtype == "tag" and not tag_name:
        raise InvalidParameterError("tag 策略必须填写 tag_name")
    try:
        priority = int(d.get("priority", 100))
    except (TypeError, ValueError) as exc:
        raise InvalidParameterError("priority 必须是整数") from exc
    return {
        "name": name, "description": str(d.get("description") or "").strip() or None,
        "effect": effect, "priority": priority, "resource_type": rtype,
        "table_pattern": table_pattern, "column_pattern": column_pattern,
        "tag_name": tag_name, "min_clearance": min_clearance,
        "mask_type": mask_type, "row_filter": row_filter,
        "valid_from": _dt(d.get("valid_from")), "valid_to": _dt(d.get("valid_to")),
        "enabled": 1 if d.get("enabled", True) else 0,
        "note": str(d.get("note") or "").strip() or None,
    }


class GovernancePolicyService:
    def __init__(self, session) -> None:
        self.session = session

    async def _ensure_kb(self, kb_id: str, tenant_id: str) -> None:
        await ensure_kb(self.session, kb_id, tenant_id)

    async def list(self, tenant_id: str, kb_id: str) -> dict:
        tenant_id = require_tenant(tenant_id)
        await self._ensure_kb(kb_id, tenant_id)
        stmt = select(GovernancePolicy).where(
            GovernancePolicy.tenant_id == tenant_id,
            GovernancePolicy.knowledge_base_id == kb_id,
            GovernancePolicy.is_deleted == 0,
        ).order_by(GovernancePolicy.priority.desc(), GovernancePolicy.id)
        rows = list((await self.session.execute(stmt)).scalars().all())
        return {"items": [r.to_dict() for r in rows], "total": len(rows)}

    async def create(self, tenant_id: str, data: dict, *,
                     user_id: str | None = None) -> dict:
        tenant_id = require_tenant(tenant_id)
        kb_id = str((data or {}).get("knowledge_base_id") or "")
        await self._ensure_kb(kb_id, tenant_id)
        got = normalize_policy_payload(data)
        row = GovernancePolicy(
            id=str(uuid.uuid4()), tenant_id=tenant_id, knowledge_base_id=kb_id,
            created_by=user_id, **got,
        )
        self.session.add(row)
        await self.session.commit()
        await self.session.refresh(row)
        return row.to_dict()

    async def _load(self, tenant_id: str, policy_id: str,
                    kb_id: str | None) -> GovernancePolicy:
        stmt = select(GovernancePolicy).where(
            GovernancePolicy.tenant_id == tenant_id,
            GovernancePolicy.id == policy_id,
            GovernancePolicy.is_deleted == 0,
        )
        if kb_id is not None:
            stmt = stmt.where(GovernancePolicy.knowledge_base_id == kb_id)
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise ResourceNotFoundError("策略不存在")
        return row

    async def update(self, tenant_id: str, policy_id: str, data: dict, *,
                     knowledge_base_id: str | None = None) -> dict:
        tenant_id = require_tenant(tenant_id)
        if knowledge_base_id is not None:
            await self._ensure_kb(knowledge_base_id, tenant_id)
        row = await self._load(tenant_id, policy_id, knowledge_base_id)
        merged = {**row.to_dict(), **(data or {})}
        got = normalize_policy_payload(merged)
        for key, value in got.items():
            setattr(row, key, value)
        await self.session.commit()
        await self.session.refresh(row)
        return row.to_dict()

    async def delete(self, tenant_id: str, policy_id: str, *,
                     knowledge_base_id: str | None = None) -> bool:
        tenant_id = require_tenant(tenant_id)
        if knowledge_base_id is not None:
            await self._ensure_kb(knowledge_base_id, tenant_id)
        row = await self._load(tenant_id, policy_id, knowledge_base_id)
        row.is_deleted = 1
        await self.session.commit()
        return True
