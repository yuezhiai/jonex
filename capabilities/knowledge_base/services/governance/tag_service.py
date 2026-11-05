"""治理 P1：自由标签字典 CRUD。"""
from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from jonex_core.common.exceptions import InvalidParameterError, ResourceNotFoundError
from jonex_core.common.tenant import require_tenant

from ...models.governance import GovernanceTagDef
from .common import ensure_kb


def normalize_tag_payload(data: dict) -> dict:
    name = str((data or {}).get("name") or "").strip()
    if not name:
        raise InvalidParameterError("标签名不能为空")
    if len(name) > 128:
        raise InvalidParameterError("标签名超长（最多 128 字符）")
    color = str((data or {}).get("color") or "").strip() or None
    if color is not None and len(color) > 32:
        raise InvalidParameterError("color 超长（最多 32 字符）")
    desc = str((data or {}).get("description") or "").strip() or None
    if desc is not None and len(desc) > 500:
        raise InvalidParameterError("description 超长（最多 500 字符）")
    return {"name": name, "color": color, "description": desc}


class GovernanceTagService:
    def __init__(self, session) -> None:
        self.session = session

    async def _ensure_kb(self, kb_id: str, tenant_id: str) -> None:
        await ensure_kb(self.session, kb_id, tenant_id)

    async def list(self, tenant_id: str, kb_id: str) -> dict:
        tenant_id = require_tenant(tenant_id)
        await self._ensure_kb(kb_id, tenant_id)
        stmt = select(GovernanceTagDef).where(
            GovernanceTagDef.tenant_id == tenant_id,
            GovernanceTagDef.knowledge_base_id == kb_id,
            GovernanceTagDef.is_deleted == 0,
        ).order_by(GovernanceTagDef.name, GovernanceTagDef.id)
        rows = list((await self.session.execute(stmt)).scalars().all())
        return {"items": [r.to_dict() for r in rows], "total": len(rows)}

    async def _duplicate(self, tenant_id: str, kb_id: str, name: str,
                         *, exclude_id: str | None = None) -> bool:
        stmt = select(GovernanceTagDef.id).where(
            GovernanceTagDef.tenant_id == tenant_id,
            GovernanceTagDef.knowledge_base_id == kb_id,
            func.lower(GovernanceTagDef.name) == name.lower(),
            GovernanceTagDef.is_deleted == 0,
        )
        if exclude_id:
            stmt = stmt.where(GovernanceTagDef.id != exclude_id)
        return (await self.session.execute(stmt)).scalar_one_or_none() is not None

    async def create(self, tenant_id: str, data: dict, *,
                     user_id: str | None = None) -> dict:
        tenant_id = require_tenant(tenant_id)
        kb_id = str((data or {}).get("knowledge_base_id") or "")
        await self._ensure_kb(kb_id, tenant_id)
        got = normalize_tag_payload(data)
        if await self._duplicate(tenant_id, kb_id, got["name"]):
            raise InvalidParameterError("该标签名已存在")
        row = GovernanceTagDef(
            id=str(uuid.uuid4()), tenant_id=tenant_id, knowledge_base_id=kb_id,
            name=got["name"], color=got["color"], description=got["description"],
            created_by=user_id,
        )
        self.session.add(row)
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise InvalidParameterError("该标签名已存在") from exc
        await self.session.refresh(row)
        return row.to_dict()

    async def _load(self, tenant_id: str, tag_id: str,
                    kb_id: str | None) -> GovernanceTagDef:
        stmt = select(GovernanceTagDef).where(
            GovernanceTagDef.tenant_id == tenant_id,
            GovernanceTagDef.id == tag_id,
            GovernanceTagDef.is_deleted == 0,
        )
        if kb_id is not None:
            stmt = stmt.where(GovernanceTagDef.knowledge_base_id == kb_id)
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise ResourceNotFoundError("标签不存在")
        return row

    async def update(self, tenant_id: str, tag_id: str, data: dict, *,
                     knowledge_base_id: str | None = None) -> dict:
        tenant_id = require_tenant(tenant_id)
        if knowledge_base_id is not None:
            await self._ensure_kb(knowledge_base_id, tenant_id)
        row = await self._load(tenant_id, tag_id, knowledge_base_id)
        got = data or {}
        if got.get("name") is not None:
            name = str(got["name"]).strip()
            if not name:
                raise InvalidParameterError("标签名不能为空")
            if await self._duplicate(tenant_id, row.knowledge_base_id, name,
                                     exclude_id=tag_id):
                raise InvalidParameterError("该标签名已存在")
            row.name = name
        if got.get("color") is not None:
            row.color = str(got["color"]).strip() or None
        if got.get("description") is not None:
            row.description = str(got["description"]).strip() or None
        await self.session.commit()
        await self.session.refresh(row)
        return row.to_dict()

    async def delete(self, tenant_id: str, tag_id: str, *,
                     knowledge_base_id: str | None = None) -> bool:
        tenant_id = require_tenant(tenant_id)
        if knowledge_base_id is not None:
            await self._ensure_kb(knowledge_base_id, tenant_id)
        row = await self._load(tenant_id, tag_id, knowledge_base_id)
        row.is_deleted = 1
        await self.session.commit()
        return True
