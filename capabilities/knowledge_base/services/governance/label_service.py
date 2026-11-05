"""治理 P1：资源标签 CRUD（manual；imported/derived 由传播/适配器写）。"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from jonex_core.common.exceptions import InvalidParameterError, ResourceNotFoundError
from jonex_core.common.tenant import require_tenant

from ...models.governance import GovernanceLabel
from .common import ensure_kb, schema_tables
from .levels import LEVELS, level_rank

MAX_NAME = 255
MAX_NOTE = 500

logger = logging.getLogger(__name__)


def normalize_label_payload(data: dict) -> dict:
    got = {
        "data_source_id": str((data or {}).get("data_source_id") or "").strip(),
        "table_name": str((data or {}).get("table_name") or "").strip(),
        "column_name": str((data or {}).get("column_name") or "").strip() or None,
        "level": str((data or {}).get("level") or "public").strip().lower(),
        "tags": [],
        "note": str((data or {}).get("note") or "").strip() or None,
    }
    if not got["data_source_id"] or not got["table_name"]:
        raise InvalidParameterError("data_source_id 与 table_name 不能为空")
    for field in ("table_name", "column_name"):
        val = got.get(field)
        if val is not None and len(val) > MAX_NAME:
            raise InvalidParameterError(f"{field} 超长（最多 {MAX_NAME} 字符）")
    if got["level"] not in LEVELS:
        raise InvalidParameterError(f"非法 level：{got['level']}")
    raw_tags = (data or {}).get("tags") or []
    if not isinstance(raw_tags, list):
        raise InvalidParameterError("tags 必须是字符串数组")
    seen: list[str] = []
    for t in raw_tags:
        name = str(t or "").strip()
        if name and name not in seen:
            seen.append(name)
    got["tags"] = seen
    if got["note"] is not None and len(got["note"]) > MAX_NOTE:
        raise InvalidParameterError(f"note 超长（最多 {MAX_NOTE} 字符）")
    got["origin"] = "manual"
    return got


class GovernanceLabelService:
    def __init__(self, session) -> None:
        self.session = session

    async def _ensure_kb(self, kb_id: str, tenant_id: str) -> None:
        await ensure_kb(self.session, kb_id, tenant_id)

    async def _schema_tables(self, tenant_id: str, kb_id: str,
                             data_source_id: str) -> dict[str, set[str]]:
        return await schema_tables(self.session, tenant_id, kb_id, data_source_id)

    async def _assert_in_schema(self, tenant_id: str, kb_id: str, got: dict) -> None:
        tables = await self._schema_tables(tenant_id, kb_id, got["data_source_id"])
        cols = tables.get(got["table_name"])
        if cols is None:
            raise InvalidParameterError(f"编译 schema 里没有表 {got['table_name']}")
        if got["column_name"] is not None and got["column_name"] not in cols:
            raise InvalidParameterError(
                f"表 {got['table_name']} 上没有列 {got['column_name']}")

    async def _existing_row(self, tenant_id: str, kb_id: str, got: dict,
                            *, exclude_id: str | None = None) -> GovernanceLabel | None:
        stmt = select(GovernanceLabel).where(
            GovernanceLabel.tenant_id == tenant_id,
            GovernanceLabel.knowledge_base_id == kb_id,
            GovernanceLabel.data_source_id == got["data_source_id"],
            GovernanceLabel.table_name == got["table_name"],
            GovernanceLabel.column_name == got["column_name"]
            if got["column_name"] is not None else GovernanceLabel.column_name.is_(None),
            GovernanceLabel.origin == got["origin"],
            GovernanceLabel.is_deleted == 0,
        )
        if exclude_id:
            stmt = stmt.where(GovernanceLabel.id != exclude_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list(self, tenant_id: str, kb_id: str,
                   data_source_id: str | None = None,
                   table_name: str | None = None) -> dict:
        tenant_id = require_tenant(tenant_id)
        await self._ensure_kb(kb_id, tenant_id)
        stmt = select(GovernanceLabel).where(
            GovernanceLabel.tenant_id == tenant_id,
            GovernanceLabel.knowledge_base_id == kb_id,
            GovernanceLabel.is_deleted == 0,
        )
        if data_source_id:
            stmt = stmt.where(GovernanceLabel.data_source_id == data_source_id)
        if table_name:
            stmt = stmt.where(GovernanceLabel.table_name == table_name)
        stmt = stmt.order_by(GovernanceLabel.table_name, GovernanceLabel.column_name,
                             GovernanceLabel.origin, GovernanceLabel.id)
        rows = list((await self.session.execute(stmt)).scalars().all())
        return {"items": [r.to_dict() for r in rows], "total": len(rows)}

    async def create(self, tenant_id: str, data: dict, *,
                     user_id: str | None = None) -> dict:
        tenant_id = require_tenant(tenant_id)
        kb_id = str((data or {}).get("knowledge_base_id") or "")
        await self._ensure_kb(kb_id, tenant_id)
        got = normalize_label_payload(data)
        await self._assert_in_schema(tenant_id, kb_id, got)
        if await self._existing_row(tenant_id, kb_id, got):
            raise InvalidParameterError("该资源已有手工标签（请编辑）")
        row = GovernanceLabel(
            id=str(uuid.uuid4()), tenant_id=tenant_id, knowledge_base_id=kb_id,
            data_source_id=got["data_source_id"], table_name=got["table_name"],
            column_name=got["column_name"], level=got["level"], tags=got["tags"],
            origin="manual", note=got["note"], created_by=user_id,
        )
        self.session.add(row)
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise InvalidParameterError("该资源已有手工标签（请编辑）") from exc
        await self.session.refresh(row)
        await self._propagate(tenant_id, kb_id, got["data_source_id"])
        return row.to_dict()

    async def _propagate(self, tenant_id: str, kb_id: str, ds_id: str) -> None:
        from .propagation import propagate_for_source
        try:
            await propagate_for_source(self.session, tenant_id, kb_id, ds_id)
        except Exception as exc:  # noqa: BLE001 —— 派生物，best-effort
            logger.warning("治理标签传播失败（best-effort）：%s", exc)

    async def update(self, tenant_id: str, label_id: str, data: dict, *,
                     knowledge_base_id: str | None = None) -> dict:
        tenant_id = require_tenant(tenant_id)
        if knowledge_base_id is not None:
            await self._ensure_kb(knowledge_base_id, tenant_id)
        stmt = select(GovernanceLabel).where(
            GovernanceLabel.tenant_id == tenant_id,
            GovernanceLabel.id == label_id,
            GovernanceLabel.origin == "manual",
            GovernanceLabel.is_deleted == 0,
        )
        if knowledge_base_id is not None:
            stmt = stmt.where(GovernanceLabel.knowledge_base_id == knowledge_base_id)
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise ResourceNotFoundError("标签不存在")
        got = data or {}
        if got.get("level") is not None:
            new_level = str(got["level"]).strip().lower()
            if new_level not in LEVELS:
                raise InvalidParameterError(f"非法 level：{new_level}")
            note = str(got.get("note") or row.note or "").strip()
            if level_rank(new_level) < level_rank(row.level) and not note:
                raise InvalidParameterError("人工降级必须填写理由（note）")
            row.level = new_level
        if got.get("tags") is not None:
            tags = got["tags"]
            if not isinstance(tags, list):
                raise InvalidParameterError("tags 必须是字符串数组")
            row.tags = list(dict.fromkeys(str(t).strip() for t in tags if str(t).strip()))
        if got.get("note") is not None:
            note = str(got["note"]).strip() or None
            if note is not None and len(note) > MAX_NOTE:
                raise InvalidParameterError(f"note 超长（最多 {MAX_NOTE} 字符）")
            row.note = note
        await self.session.commit()
        await self.session.refresh(row)
        await self._propagate(tenant_id, row.knowledge_base_id, row.data_source_id)
        return row.to_dict()

    async def delete(self, tenant_id: str, label_id: str, *,
                     knowledge_base_id: str | None = None) -> bool:
        tenant_id = require_tenant(tenant_id)
        if knowledge_base_id is not None:
            await self._ensure_kb(knowledge_base_id, tenant_id)
        stmt = select(GovernanceLabel).where(
            GovernanceLabel.tenant_id == tenant_id,
            GovernanceLabel.id == label_id,
            GovernanceLabel.is_deleted == 0,
        )
        if knowledge_base_id is not None:
            stmt = stmt.where(GovernanceLabel.knowledge_base_id == knowledge_base_id)
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise ResourceNotFoundError("标签不存在")
        kb_id = row.knowledge_base_id
        ds_id = row.data_source_id
        row.is_deleted = 1
        await self.session.commit()
        await self._propagate(tenant_id, kb_id, ds_id)
        return True
