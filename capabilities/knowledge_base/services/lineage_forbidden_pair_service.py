"""血缘「禁止同查」表对的 KB 级 CRUD + 1/2 跳建议。

**状态语义**：`proposed`（建议，不生效）/ `active`（生效）/ `disabled`（停用，不生效且不再建议）。
**逐条 CRUD**：没有 `schema_version`、不产生 409；重复对 → 1001/400。
**软删与 disabled 都算「用户表过态」**：建议不会把它们翻回 proposed。
"""
from __future__ import annotations

import uuid
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from jonex_core.common.exceptions import InvalidParameterError, ResourceNotFoundError
from jonex_core.common.tenant import require_tenant

from ..models.lineage_edge import LineageEdge
from ..models.lineage_forbidden_pair import LineageForbiddenPair
from .nl2sql.forbidden import pair_key, stored_order
from .nl2sql.table_ref import is_same_table, refs_same, table_ref

MAX_TABLE_LEN = 255
MAX_NOTE_LEN = 500
#: 中继表连接数超过它就不当桥（公共维表会把所有事实表连成一团）。
HUB_DEGREE_LIMIT = 10
#: 单次建议最多产出多少条（超出置 `truncated`，前端提示分批看）。
MAX_SUGGESTIONS = 200
_STATUSES = frozenset({"proposed", "active", "disabled"})


def build_suggestions(
    edges: Iterable[tuple[str, str]],
    *,
    limit: int | None = MAX_SUGGESTIONS,
) -> tuple[list[tuple[str, str, str]], bool]:
    """血缘边 → 2 跳候选 `[(a, c, via)]`，以及是否因 `limit` 被截断。

    - 中继表连接数 > `HUB_DEGREE_LIMIT` 时跳过（公共维表不当桥）；
    - 1 跳对由调用方单独产出，这里不重复；
    - 同一对只留第一条，结果按两端显示名排序；中继按名字排序（`via` 不抖动）；
    - 同一张表的两种写法（`orders` / `dbo.orders`）不产候选对；
    - `limit=None` 表示**不截断**（服务层按「新增条数」预算自己扫，不能在这里
      先砍掉候选，否则后面的候选永远轮不到）。
    """
    adjacency: dict[str, set[str]] = {}
    display: dict[str, str] = {}
    first_hop: set[tuple] = set()

    def key_of(name: str) -> str:
        return ".".join(table_ref(name))

    for from_table, to_table in edges or ():
        a, b = key_of(str(from_table)), key_of(str(to_table))
        if not a or not b or a == b:
            continue
        display.setdefault(a, str(from_table).strip())
        display.setdefault(b, str(to_table).strip())
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)
        first_hop.add(pair_key(a, b))

    out: dict[tuple, tuple[str, str, str]] = {}
    # 中继按名字排序：同一对可由多个桥接时，`via` 不随边的输入顺序抖动。
    for hub in sorted(adjacency):
        neighbors = adjacency[hub]
        if len(neighbors) > HUB_DEGREE_LIMIT:
            continue
        ordered = sorted(neighbors)
        for index, left in enumerate(ordered):
            for right in ordered[index + 1:]:
                # 同一张表的两种写法（`orders` / `dbo.orders`）不能组成一对。
                if is_same_table(display[left], display[right]):
                    continue
                pair = pair_key(left, right)
                if pair in first_hop or pair in out:
                    continue
                a, b = stored_order(display[left], display[right])
                out[pair] = (a, b, display[hub])

    items = sorted(out.values(), key=lambda item: (
        item[0].lower(), item[1].lower(), item[2].lower()))
    if limit is not None and len(items) > limit:
        return items[:limit], True
    return items, False


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
    if is_same_table(a, b):
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

    async def _lineage_edges(self, tenant_id: str, kb_id: str,
                             data_source_id: str) -> list[tuple[str, str]]:
        """该源活血缘边的原始方向表对（2 跳建议的邻接图来源）。"""
        stmt = select(LineageEdge.from_table, LineageEdge.to_table).where(
            LineageEdge.tenant_id == tenant_id,
            LineageEdge.knowledge_base_id == kb_id,
            LineageEdge.data_source_id == data_source_id,
            LineageEdge.is_deleted == 0,
        )
        return [(str(a), str(b)) for a, b in (await self.session.execute(stmt)).all()]

    async def _existing_pairs(self, tenant_id: str, kb_id: str,
                              data_source_id: str) -> list[tuple[str, str]]:
        """已有行（**含软删与任何状态**）的两端表名 —— 建议不翻回用户表过态的行。

        返回**原始表名**而非 `pair_key`：判重走 `is_same_table`，`orders` 与
        `dbo.orders` 视为同一张表（跨写法重复建议不得再落一行）。
        """
        stmt = select(LineageForbiddenPair.table_a, LineageForbiddenPair.table_b).where(
            LineageForbiddenPair.tenant_id == tenant_id,
            LineageForbiddenPair.knowledge_base_id == kb_id,
            LineageForbiddenPair.data_source_id == data_source_id,
        )
        return [(str(a), str(b))
                for a, b in (await self.session.execute(stmt)).all()]

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
            # 激活 = 真的生效：历史遗留的同表对（`orders` / `dbo.orders`）不能从
            # 状态流转复活 —— 否则单表查询会被判「同现」。
            if status == "active" and is_same_table(row.table_a, row.table_b):
                raise InvalidParameterError("禁止对的两端是同一张表")
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
        """按「新增条数」预算生成建议。

        返回字段：
        - `created`：本次真正落库的**新增**条数（不超过 `MAX_SUGGESTIONS`）；
        - `skipped`：本次扫到、但已存在（任意状态/任意写法）而跳过的条数；
        - `total`：本次**扫过**的候选数，恒等于 `created + skipped`（不是候选全集）；
        - `truncated`：新增数达到上限时后面还有没扫的候选。
        """
        tenant_id = require_tenant(tenant_id)
        await self._ensure_kb(kb_id, tenant_id)
        ds_id = str(data_source_id or "").strip()
        if not ds_id:
            raise InvalidParameterError("缺少 data_source_id")
        edges = await self._lineage_edges(tenant_id, kb_id, ds_id)
        one_hop = await self._lineage_table_pairs(tenant_id, kb_id, ds_id)
        # **不截断**：预算按「新增条数」算，候选必须完整交给下面的扫描循环。
        # 先按上限切片再过滤存量行的话，第二次调用仍只扫同一批已存在候选，
        # 第 201 条起永远轮不到。
        two_hop, _ = build_suggestions(edges, limit=None)
        existing = await self._existing_pairs(tenant_id, kb_id, ds_id)
        # 存量行只解析一次；判重仍是无序双向后缀匹配（`orders` ≡ `dbo.orders`）。
        existing_refs = [(table_ref(a), table_ref(b)) for a, b in existing]

        def is_existing(a: str, b: str) -> bool:
            left, right = table_ref(a), table_ref(b)
            return any(
                (refs_same(left, x) and refs_same(right, y))
                or (refs_same(left, y) and refs_same(right, x))
                for x, y in existing_refs)

        # 1 跳与 2 跳统一过滤同表对：1 跳来自 `_lineage_table_pairs`，血缘边本身
        # 可能带 `orders → dbo.orders` 这类同表不同写法的噪声。
        plan: dict[tuple, tuple[str, str, str]] = {}
        for key, (a, b) in one_hop.items():
            if is_same_table(a, b):
                continue
            plan[key] = (a, b, "")
        for a, b, via in two_hop:
            if is_same_table(a, b):
                continue
            plan.setdefault(pair_key(a, b), (a, b, via))
        # 1 跳与 2 跳一起排序：保证每次调用按同一顺序推进，预算才可续。
        items = sorted(plan.values(), key=lambda item: (
            item[0].lower(), item[1].lower(), item[2].lower()))
        created = 0
        skipped = 0
        truncated = False
        for index, (a, b, via) in enumerate(items):
            if is_existing(a, b):
                skipped += 1
                continue
            self.session.add(LineageForbiddenPair(
                id=str(uuid.uuid4()), tenant_id=tenant_id, knowledge_base_id=kb_id,
                data_source_id=ds_id, table_a=a, table_b=b,
                origin="derived", status="proposed", created_by=user_id,
                note=f"via {via}" if via else None,
            ))
            created += 1
            if created >= MAX_SUGGESTIONS:
                truncated = index < len(items) - 1
                break
        total = created + skipped
        try:
            await self.session.commit()
        except IntegrityError:
            # 并发建议：唯一索引挡住重复。回滚后按「全部跳过」回报（不重试，
            # 建议是可重复触发的幂等动作）。
            await self.session.rollback()
            return {"created": 0, "skipped": total, "total": total,
                    "truncated": truncated}
        return {"created": created, "skipped": skipped,
                "total": total, "truncated": truncated}
