"""只读血缘 walk（表级为主，列只做首跳锚点过滤）。

**为什么单独一个模块**：NL→SQL 的 `search_paths` 面向「锚点→目标」的路径打分；
本体侧要的是「从这张表出发、上下 N 跳能看到什么」，不关心目标加成与换乘惩罚。
两者都是纯函数，但语义不同，不硬套。

`unknown` 三态：读表失败 ⇒ `unknown=True`（查不到 ≠ 没有）；
读到空 ⇒ `unknown=False` + 空 edges（该表确实没有登记血缘）。
"""
from __future__ import annotations

import logging

from jonex_core.common.exceptions import InvalidParameterError
from jonex_core.common.tenant import require_tenant

from .nl2sql.paths import Edge, confidence_of

logger = logging.getLogger(__name__)

_DIRECTIONS = {"upstream", "downstream", "both"}


def walk_edges(edges: list[Edge], anchor_table: str, *, anchor_column: str | None = None,
               direction: str = "both", hops: int = 2,
               min_origin: str | None = None, max_edges: int = 500) -> dict:
    if direction not in _DIRECTIONS:
        raise InvalidParameterError(f"非法 direction: {direction}")
    min_conf = confidence_of(min_origin) if min_origin else 0.0
    pool = [e for e in edges if confidence_of(e.origin) >= min_conf]

    frontier = {(anchor_table, anchor_column)} if anchor_column else {(anchor_table, None)}
    visited_tables = {anchor_table}
    picked: list[Edge] = []
    truncated = False

    def matches(table: str, column: str, node: tuple[str, str | None], first: bool) -> bool:
        t, c = node
        if table != t:
            return False
        return (not c) or (not first) or column == c

    for hop in range(max(1, int(hops))):
        next_frontier: set[tuple[str, str | None]] = set()
        for e in pool:
            if e in picked:
                continue
            hit = None
            if direction in ("downstream", "both"):
                for node in frontier:
                    if matches(e.from_table, e.from_column, node, hop == 0):
                        hit = (e.to_table, e.to_column); break
            if hit is None and direction in ("upstream", "both"):
                for node in frontier:
                    if matches(e.to_table, e.to_column, node, hop == 0):
                        hit = (e.from_table, e.from_column); break
            if hit is None:
                continue
            if len(picked) >= max_edges:
                truncated = True
                break
            picked.append(e)
            # 列锚点自环：首跳命中锚点表**另一列**（如 a.x→a.y）时，锚点表早已在
            # visited_tables 里。若不再入队，a.y→… 的第 2 跳会被整段丢掉。
            # 仅在首跳把锚点表重新放回 frontier，后续跳仍只看表（不锁列）。
            if hit[0] not in visited_tables or (
                    anchor_column and hop == 0 and hit[0] == anchor_table):
                visited_tables.add(hit[0])
                next_frontier.add((hit[0], None))   # 后续跳只看表，不再锁列
        if truncated or not next_frontier:
            break
        frontier = next_frontier

    return {
        "edges": [{"from_table": e.from_table, "from_column": e.from_column,
                   "to_table": e.to_table, "to_column": e.to_column,
                   "origin": e.origin, "confidence": confidence_of(e.origin)}
                  for e in picked],
        "nodes": [{"table": t, "column": None} for t in sorted(visited_tables)],
        "truncated": truncated,
        "unknown": False,
        "direction": direction,
    }


async def walk(tenant_id: str, kb_id: str, data_source_id: str, table: str,
               *, column: str | None = None, direction: str = "both", hops: int = 2,
               min_origin: str | None = None, max_edges: int = 500, session=None) -> dict:
    """读该 KB + 源的活边后走图；DB 读失败 ⇒ unknown=True + 空图（fail open，不抛）；
    参数错误（tenant / direction）先于读库抛出，不被读失败吞成 unknown。"""
    # 参数校验必须在读库之前：坏 direction 若等到 walk_edges 才炸，
    # 读失败会先一步吞成 unknown=True（1001 被降级成「查不到」）。
    tenant_id = require_tenant(tenant_id)  # 服务层统一租户校验（同 LineageEdgeService）
    if direction not in _DIRECTIONS:
        raise InvalidParameterError(f"非法 direction: {direction}")

    from sqlalchemy import select
    from ..models.lineage_edge import LineageEdge

    async def _load(sess) -> list:
        stmt = select(LineageEdge).where(
            LineageEdge.tenant_id == tenant_id,
            LineageEdge.knowledge_base_id == kb_id,
            LineageEdge.data_source_id == data_source_id,
            LineageEdge.is_deleted == 0,
        )
        return list((await sess.execute(stmt)).scalars().all())

    try:
        if session is not None:
            rows = await _load(session)
        else:
            from jonex_core.common.database import get_db_session
            async with get_db_session() as sess:
                rows = await _load(sess)
    except Exception as exc:  # noqa: BLE001 —— 增能项，坏了一律 unknown
        logger.warning("血缘 walk 读表失败，本次返回 unknown：%s", exc)
        return {"edges": [], "nodes": [], "truncated": False,
                "unknown": True, "direction": direction}

    edges = [Edge(type="lineage", from_table=str(r.from_table),
                  from_column=str(r.from_column), to_table=str(r.to_table),
                  to_column=str(r.to_column), origin=str(r.origin or "")) for r in rows]
    return walk_edges(edges, table, anchor_column=column, direction=direction,
                      hops=hops, min_origin=min_origin, max_edges=max_edges)
