"""血缘「禁止同查」表对的 KB 级 CRUD + 1 跳建议。

**状态语义**：`proposed`（建议，不生效）/ `active`（生效）/ `disabled`（停用，不生效且不再建议）。
**逐条 CRUD**：没有 `schema_version`、不产生 409；重复对 → 1001/400。
**软删与 disabled 都算「用户表过态」**：建议不会把它们翻回 proposed。
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from jonex_core.common.exceptions import InvalidParameterError, ResourceNotFoundError
from jonex_core.common.tenant import require_tenant

from ..models.lineage_edge import LineageEdge
from ..models.lineage_forbidden_pair import LineageForbiddenPair
from .nl2sql.forbidden import pair_key, stored_order

MAX_TABLE_LEN = 255
MAX_NOTE_LEN = 500
_STATUSES = frozenset({"proposed", "active", "disabled"})


def normalize_pair_payload(data: dict) -> dict:
    """只归一，不连库（存在性校验在 service 里做）。"""
    a = str((data or {}).get("table_a") or "").strip()
    b = str((data or {}).get("table_b") or "").strip()
    if not a or not b:
        raise InvalidParameterError("禁止对的两端表名不能为空")
    for field, value in (("table_a", a), ("table_b", b)):
        if len(value) > MAX_TABLE_LEN:
            raise InvalidParameterError(
                f"{field} 超长（最多 {MAX_TABLE_LEN} 字符，本次 {len(value)}）")
    if a.lower() == b.lower():
        raise InvalidParameterError("禁止对的两端不能是同一张表")
    ds = str((data or {}).get("data_source_id") or "").strip()
    if not ds:
        raise InvalidParameterError("缺少 data_source_id（表名是物理表名，必须指明是哪个源）")
    note = str((data or {}).get("note") or "").strip() or None
    if note is not None and len(note) > MAX_NOTE_LEN:
        raise InvalidParameterError(
            f"note 超长（最多 {MAX_NOTE_LEN} 字符，本次 {len(note)}）")
    a, b = stored_order(a, b)
    return {"table_a": a, "table_b": b, "data_source_id": ds, "note": note}


class LineageForbiddenPairService:
    """KB 级禁止对的增删改查与建议生成。"""

    def __init__(self, session) -> None:
        self.session = session

    async def _ensure_kb(self, kb_id: str, tenant_id: str) -> None:
        if not str(kb_id or "").strip():
            raise ResourceNotFoundError(message="知识库不存在", details={"id": kb_id})
        from ..repository.knowledge_info_repository import KnowledgeInfoRepository
        await KnowledgeInfoRepository(self.session).get_required(kb_id, tenant_id)

    async def _schema_tables(self, tenant_id: str, kb_id: str,
                             data_source_id: str) -> dict[str, set[str]]:
        from ..repository.ontology_schema_repository import OntologySchemaRepository
        rows = await OntologySchemaRepository(self.session).list_compiled_schemas(
            tenant_id, kb_id)
        row = next((r for r in rows if r.data_source_id == data_source_id), None)
        if row is None:
            raise InvalidParameterError(f"该数据源没有可用的编译 schema：{data_source_id}")
        by_table: dict[str, set[str]] = {}
        for e in (row.entity_types or []):
            if isinstance(e, dict) and str(e.get("source_object_id") or "").strip():
                by_table[str(e["source_object_id"]).strip()] = {
                    str(a.get("name") or "") for a in (e.get("attributes") or [])
                    if isinstance(a, dict)
                }
        return by_table

    async def _assert_tables_in_schema(self, tenant_id: str, kb_id: str,
                                       data_source_id: str, got: dict) -> None:
        by_table = await self._schema_tables(tenant_id, kb_id, data_source_id)
        for field in ("table_a", "table_b"):
            table = got[field]
            if table not in by_table:
                raise InvalidParameterError(f"编译 schema 里没有表 {table}（端点 {field}）")

    async def _existing_row(self, tenant_id: str, kb_id: str, data_source_id: str,
                            got: dict) -> LineageForbiddenPair | None:
        """同对是否已有**活行**（重复创建 → 1001）。"""
        stmt = select(LineageForbiddenPair).where(
            LineageForbiddenPair.tenant_id == tenant_id,
            LineageForbiddenPair.knowledge_base_id == kb_id,
            LineageForbiddenPair.data_source_id == data_source_id,
            LineageForbiddenPair.is_deleted == 0,
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        target = pair_key(got["table_a"], got["table_b"])
        return next((r for r in rows
                     if pair_key(r.table_a, r.table_b) == target), None)

    async def _load_row(self, tenant_id: str, pair_id: str,
                        knowledge_base_id: str | None = None,
                        ) -> LineageForbiddenPair | None:
        stmt = select(LineageForbiddenPair).where(
            LineageForbiddenPair.tenant_id == tenant_id,
            LineageForbiddenPair.id == pair_id,
            LineageForbiddenPair.is_deleted == 0,
        )
        if knowledge_base_id is not None:
            stmt = stmt.where(LineageForbiddenPair.knowledge_base_id == knowledge_base_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def _lineage_table_pairs(self, tenant_id: str, kb_id: str,
                                   data_source_id: str) -> dict[tuple, tuple]:
        """该源活血缘边的表对（去方向、去重）。"""
        stmt = select(LineageEdge.from_table, LineageEdge.to_table).where(
            LineageEdge.tenant_id == tenant_id,
            LineageEdge.knowledge_base_id == kb_id,
            LineageEdge.data_source_id == data_source_id,
            LineageEdge.is_deleted == 0,
        )
        out: dict[tuple, tuple] = {}
        for from_table, to_table in (await self.session.execute(stmt)).all():
            a, b = stored_order(str(from_table), str(to_table))
            out[pair_key(a, b)] = (a, b)
        return out

    async def _existing_keys(self, tenant_id: str, kb_id: str,
                             data_source_id: str) -> set[tuple]:
        """已有行（**含软删与任何状态**）的 pair key —— 建议不翻回用户表过态的行。"""
        stmt = select(LineageForbiddenPair.table_a, LineageForbiddenPair.table_b).where(
            LineageForbiddenPair.tenant_id == tenant_id,
            LineageForbiddenPair.knowledge_base_id == kb_id,
            LineageForbiddenPair.data_source_id == data_source_id,
        )
        return {pair_key(a, b) for a, b in (await self.session.execute(stmt)).all()}

    async def list(self, tenant_id: str, kb_id: str,
                   data_source_id: str | None = None,
                   status: str | None = None) -> dict:
        tenant_id = require_tenant(tenant_id)
        await self._ensure_kb(kb_id, tenant_id)
        if status and status not in _STATUSES:
            raise InvalidParameterError(f"非法 status：{status}")
        stmt = select(LineageForbiddenPair).where(
            LineageForbiddenPair.tenant_id == tenant_id,
            LineageForbiddenPair.knowledge_base_id == kb_id,
            LineageForbiddenPair.is_deleted == 0,
        )
        if data_source_id:
            stmt = stmt.where(LineageForbiddenPair.data_source_id == data_source_id)
        if status:
            stmt = stmt.where(LineageForbiddenPair.status == status)
        stmt = stmt.order_by(LineageForbiddenPair.table_a, LineageForbiddenPair.table_b,
                             LineageForbiddenPair.status, LineageForbiddenPair.id)
        rows = list((await self.session.execute(stmt)).scalars().all())
        return {"items": [r.to_dict() for r in rows], "total": len(rows)}

    async def create(self, tenant_id: str, data: dict, *,
                     user_id: str | None = None) -> dict:
        tenant_id = require_tenant(tenant_id)
        kb_id = str((data or {}).get("knowledge_base_id") or "")
        await self._ensure_kb(kb_id, tenant_id)
        got = normalize_pair_payload(data)
        await self._assert_tables_in_schema(tenant_id, kb_id, got["data_source_id"], got)
        if await self._existing_row(tenant_id, kb_id, got["data_source_id"], got):
            raise InvalidParameterError("该禁止对已存在")
        row = LineageForbiddenPair(
            id=str(uuid.uuid4()), tenant_id=tenant_id, knowledge_base_id=kb_id,
            data_source_id=got["data_source_id"],
            table_a=got["table_a"], table_b=got["table_b"],
            origin="manual", status="active", note=got["note"], created_by=user_id,
        )
        self.session.add(row)
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise InvalidParameterError("该禁止对已存在") from exc
        await self.session.refresh(row)
        return row.to_dict()

    async def update(self, tenant_id: str, pair_id: str, data: dict, *,
                     knowledge_base_id: str | None = None) -> dict:
        tenant_id = require_tenant(tenant_id)
        if knowledge_base_id is not None:
            await self._ensure_kb(knowledge_base_id, tenant_id)
        row = await self._load_row(tenant_id, pair_id, knowledge_base_id)
        if row is None:
            raise ResourceNotFoundError("禁止对不存在")
        got = data or {}
        if got.get("status") is not None:
            status = str(got["status"]).strip()
            if status not in _STATUSES:
                raise InvalidParameterError(f"非法 status：{status}")
            row.status = status
        if got.get("note") is not None:
            note = str(got["note"]).strip() or None
            if note is not None and len(note) > MAX_NOTE_LEN:
                raise InvalidParameterError(
                    f"note 超长（最多 {MAX_NOTE_LEN} 字符，本次 {len(note)}）")
            row.note = note
        await self.session.commit()
        await self.session.refresh(row)
        return row.to_dict()

    async def delete(self, tenant_id: str, pair_id: str, *,
                     knowledge_base_id: str | None = None) -> bool:
        tenant_id = require_tenant(tenant_id)
        if knowledge_base_id is not None:
            await self._ensure_kb(knowledge_base_id, tenant_id)
        row = await self._load_row(tenant_id, pair_id, knowledge_base_id)
        if row is None:
            raise ResourceNotFoundError("禁止对不存在")
        row.is_deleted = 1
        await self.session.commit()
        return True

    async def suggest(self, tenant_id: str, kb_id: str, data_source_id: str, *,
                      user_id: str | None = None) -> dict:
        tenant_id = require_tenant(tenant_id)
        await self._ensure_kb(kb_id, tenant_id)
        ds_id = str(data_source_id or "").strip()
        if not ds_id:
            raise InvalidParameterError("缺少 data_source_id")
        pairs = await self._lineage_table_pairs(tenant_id, kb_id, ds_id)
        existing = await self._existing_keys(tenant_id, kb_id, ds_id)
        created = 0
        for key in sorted(pairs):
            if key in existing:
                continue
            a, b = pairs[key]
            self.session.add(LineageForbiddenPair(
                id=str(uuid.uuid4()), tenant_id=tenant_id, knowledge_base_id=kb_id,
                data_source_id=ds_id, table_a=a, table_b=b,
                origin="derived", status="proposed", created_by=user_id,
            ))
            created += 1
        try:
            await self.session.commit()
        except IntegrityError:
            # 并发建议：唯一索引挡住重复。回滚后按「全部跳过」回报（不重试，
            # 建议是可重复触发的幂等动作）。
            await self.session.rollback()
            return {"created": 0, "skipped": len(pairs), "total": len(pairs)}
        return {"created": created, "skipped": len(pairs) - created, "total": len(pairs)}
