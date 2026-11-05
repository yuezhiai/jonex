"""治理 P1：标签沿血缘传播（上游→下游、列映射、源内、人工钉住）。

纯函数 `compute_derived` 不做 IO；服务层 `propagate_for_source` 负责读写
`governance_labels` 的 `origin='derived'` 行（只升不降、人工钉住）。
"""
from __future__ import annotations

import logging
import uuid
from collections import defaultdict

from sqlalchemy import select

from ...models.governance import GovernanceLabel
from .levels import level_rank

logger = logging.getLogger(__name__)


def _key(table: str, column: str | None) -> tuple[str, str | None]:
    return (str(table or "").strip(), str(column).strip() if column else None)


def _lvl_table(label: dict) -> str:
    return str(label.get("table") or label.get("table_name") or "").strip()


def _lvl_col(label: dict) -> str | None:
    raw = label.get("column", label.get("column_name"))
    return str(raw).strip() if raw else None


def compute_derived(labels: list[dict], edges: list[dict]) -> list[dict]:
    """输入全部标签与血缘边，输出应存在的 `derived` 标签行。

    - 列级：沿 `(from_table,from_column) -> (to_table,to_column)` 做 max 松弛；
      **manual 列标签钉住**，不输出 derived 行。
    - 表级：= max(表级标签, 所有列级)；沿 `from_table -> to_table` 传播；
      **manual 表标签钉住**。
    - 环安全：等级只有四档，松弛到稳定即止（迭代上限 node×4+8）。
    """
    col_levels: dict[tuple[str, str | None], str] = {}
    col_pinned: set[tuple[str, str | None]] = set()
    table_levels: dict[str, str] = {}
    table_pinned: set[str] = set()

    def raise_to(store: dict, key, level: str) -> None:
        old = store.get(key)
        if old is None or level_rank(level) > level_rank(old):
            store[key] = level

    for row in labels or ():
        table = _lvl_table(row)
        if not table:
            continue
        col = _lvl_col(row)
        origin = str(row.get("origin") or "").strip().lower()
        if origin == "derived":
            # 派生行是**输出**，不是输入：把它们当输入会让清理后自持
            # （删掉全部 manual 标签后 derived 仍互相抬高、永远删不掉）。
            continue
        level = str(row.get("level") or "public").strip().lower()
        if col is None:
            if origin == "manual":
                table_pinned.add(table)
            raise_to(table_levels, table, level)
        else:
            key = _key(table, col)
            if origin == "manual":
                col_pinned.add(key)
            raise_to(col_levels, key, level)

    col_adj: dict[tuple[str, str | None], set[tuple[str, str | None]]] = defaultdict(set)
    table_adj: dict[str, set[str]] = defaultdict(set)
    for e in edges or ():
        ft = _lvl_table({"table": e.get("from_table")})
        tt = _lvl_table({"table": e.get("to_table")})
        fc = str(e.get("from_column") or "").strip()
        tc = str(e.get("to_column") or "").strip()
        if not ft or not tt:
            continue
        if fc and tc:
            col_adj[_key(ft, fc)].add(_key(tt, tc))
        table_adj[ft].add(tt)

    # 列级 fixpoint
    rounds = 0
    changed = True
    while changed and rounds < len(col_levels) * 4 + 8:
        changed = False
        rounds += 1
        for src, dsts in col_adj.items():
            level = col_levels.get(src)
            if level is None:
                continue
            for dst in dsts:
                if dst in col_pinned:
                    continue
                old = col_levels.get(dst)
                if old is None or level_rank(level) > level_rank(old):
                    col_levels[dst] = level
                    changed = True

    # 表级 = max(表标签, 所有列级)
    for (table, _col), level in col_levels.items():
        raise_to(table_levels, table, level)

    # 表级沿表边 fixpoint（manual 表钉住）
    rounds = 0
    changed = True
    while changed and rounds < len(table_levels) * 4 + 8:
        changed = False
        rounds += 1
        for src, dsts in table_adj.items():
            level = table_levels.get(src)
            if level is None:
                continue
            for dst in dsts:
                if dst in table_pinned:
                    continue
                old = table_levels.get(dst)
                if old is None or level_rank(level) > level_rank(old):
                    table_levels[dst] = level
                    changed = True

    out: list[dict] = []
    for (table, column), level in sorted(col_levels.items()):
        if (table, column) in col_pinned:
            continue
        out.append({"table": table, "column": column,
                    "level": level, "origin": "derived"})
    for table, level in sorted(table_levels.items()):
        if table in table_pinned:
            continue
        out.append({"table": table, "column": None,
                    "level": level, "origin": "derived"})
    return out


class GovernancePropagationService:
    def __init__(self, session) -> None:
        self.session = session

    async def _load_labels(self, tenant_id: str, kb_id: str, ds_id: str) -> list[dict]:
        stmt = select(GovernanceLabel).where(
            GovernanceLabel.tenant_id == tenant_id,
            GovernanceLabel.knowledge_base_id == kb_id,
            GovernanceLabel.data_source_id == ds_id,
            GovernanceLabel.is_deleted == 0,
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        return [{"table": r.table_name, "column": r.column_name,
                 "level": r.level, "origin": r.origin} for r in rows]

    async def _load_edges(self, tenant_id: str, kb_id: str, ds_id: str) -> list[dict]:
        from ...models.lineage_edge import LineageEdge
        stmt = select(LineageEdge).where(
            LineageEdge.tenant_id == tenant_id,
            LineageEdge.knowledge_base_id == kb_id,
            LineageEdge.data_source_id == ds_id,
            LineageEdge.is_deleted == 0,
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        return [{"from_table": r.from_table, "from_column": r.from_column,
                 "to_table": r.to_table, "to_column": r.to_column} for r in rows]

    async def _apply(self, tenant_id: str, kb_id: str, ds_id: str,
                     desired: list[dict]) -> dict:
        stmt = select(GovernanceLabel).where(
            GovernanceLabel.tenant_id == tenant_id,
            GovernanceLabel.knowledge_base_id == kb_id,
            GovernanceLabel.data_source_id == ds_id,
            GovernanceLabel.origin == "derived",
            GovernanceLabel.is_deleted == 0,
        )
        existing = {(r.table_name, r.column_name): r
                    for r in (await self.session.execute(stmt)).scalars().all()}
        desired_map = {(d["table"], d["column"]): d for d in desired}
        upserted = deleted = 0
        for key, d in desired_map.items():
            row = existing.get(key)
            if row is None:
                self.session.add(GovernanceLabel(
                    id=uuid.uuid4().hex,
                    tenant_id=tenant_id, knowledge_base_id=kb_id,
                    data_source_id=ds_id, table_name=d["table"],
                    column_name=d["column"], level=d["level"],
                    tags=[], origin="derived", created_by=None,
                ))
                upserted += 1
            elif level_rank(d["level"]) > level_rank(row.level):
                row.level = d["level"]
                upserted += 1
        for key, row in existing.items():
            if key not in desired_map:
                row.is_deleted = 1
                deleted += 1
        await self.session.commit()
        return {"upserted": upserted, "deleted": deleted}

    async def propagate(self, tenant_id: str, kb_id: str, ds_id: str) -> dict:
        labels = await self._load_labels(tenant_id, kb_id, ds_id)
        edges = await self._load_edges(tenant_id, kb_id, ds_id)
        desired = compute_derived(labels, edges)
        return await self._apply(tenant_id, kb_id, ds_id, desired)


async def propagate_for_source(session, tenant_id: str, kb_id: str,
                               ds_id: str) -> dict:
    """绑定会话的入口；调用方负责 best-effort 包裹。"""
    return await GovernancePropagationService(session).propagate(
        tenant_id, kb_id, ds_id)
