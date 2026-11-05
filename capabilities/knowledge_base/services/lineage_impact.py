"""影响分析：在 `lineage_walk.walk` 选出的边集上做分层（纯函数）+ 只读服务。

`walk` 负责「选哪些边」（方向/跳数/列锚点/信任档/截断），本模块只负责
「这些边分别在第几跳」——不重新选边，避免与 walk 的语义分叉。
"""
from __future__ import annotations

from typing import Any

from jonex_core.common.exceptions import InvalidParameterError
from jonex_core.common.tenant import require_tenant

from .lineage_walk import walk
from .nl2sql.paths import Edge, confidence_of

_DIRECTIONS = {"upstream", "downstream", "both"}
MAX_HOPS = 3
MAX_NODES = 200
MAX_EDGES = 400


def clamp_int(value: Any, *, default: int, low: int, high: int, name: str) -> int:
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidParameterError(message=f"{name} 必须是整数") from exc
    return max(low, min(number, high))


def _matches(table: str, column: str, node: tuple[str, str | None], *, first: bool) -> bool:
    t, c = node
    if table != t:
        return False
    return (not c) or (not first) or column == c


def annotate_levels(edges: list[dict], anchor_table: str,
                    anchor_column: str | None = None, *,
                    direction: str = "downstream", hops: int = MAX_HOPS,
                    max_nodes: int = MAX_NODES) -> dict:
    if direction not in _DIRECTIONS:
        raise InvalidParameterError(message=f"非法 direction: {direction}")
    pool = [
        Edge(type="lineage",
             from_table=str(e.get("from_table") or ""),
             from_column=str(e.get("from_column") or ""),
             to_table=str(e.get("to_table") or ""),
             to_column=str(e.get("to_column") or ""),
             origin=str(e.get("origin") or ""))
        for e in (edges or []) if isinstance(e, dict)
    ]
    frontier: set[tuple[str, str | None]] = {(anchor_table, anchor_column)}
    # 节点身份是 (表, 列) 而非表：同一张表的不同列是不同节点（同跳多列、跨跳新列
    # 都必须计入），同一节点只出现在最早可达的那一跳。
    seen_nodes: set[tuple[str, str | None]] = {(anchor_table, anchor_column)}
    seen_edges: set[tuple[str, str, str, str]] = set()
    levels: list[dict] = []
    truncated = False

    for hop in range(1, max(1, int(hops)) + 1):
        nodes: list[dict] = []
        level_edges: list[dict] = []
        next_frontier: set[tuple[str, str | None]] = set()
        for e in pool:
            key = (e.from_table, e.from_column, e.to_table, e.to_column)
            if key in seen_edges:
                continue
            hit = None
            reversed_ = False
            if direction in ("downstream", "both"):
                for node in frontier:
                    if _matches(e.from_table, e.from_column, node, first=(hop == 1)):
                        hit = (e.to_table, e.to_column)
                        break
            if hit is None and direction in ("upstream", "both"):
                for node in frontier:
                    if _matches(e.to_table, e.to_column, node, first=(hop == 1)):
                        hit = (e.from_table, e.from_column)
                        reversed_ = True
                        break
            if hit is None:
                continue
            edge_view = {
                "from_table": e.from_table, "from_column": e.from_column,
                "to_table": e.to_table, "to_column": e.to_column,
                "origin": e.origin, "confidence": confidence_of(e.origin),
                "reversed": reversed_,
            }
            if hit in seen_nodes:
                # 目标节点已在前层（菱形/环）或本层出现：边仍属于本跳，记边不记节点，
                # 也不进 next_frontier（同一节点不重复展开）。
                seen_edges.add(key)
                level_edges.append(edge_view)
                continue
            # seen_nodes 含锚点；上限只约束「受影响节点」，不含锚点本身。
            # 超限时整条边跳过（不记边）。
            if len(seen_nodes) - 1 >= max_nodes:
                truncated = True
                continue
            seen_nodes.add(hit)
            seen_edges.add(key)
            next_frontier.add(hit)
            nodes.append({"table": hit[0], "column": hit[1]})
            level_edges.append(edge_view)
        # 该跳可能只有「目标已见过」的边（无新节点），这些边同样要保留。
        if nodes or level_edges:
            levels.append({"hop": hop, "nodes": nodes, "edges": level_edges})
        if not next_frontier:
            break
        # 后续跳只看表：列锚点只在第一跳生效（与 walk_edges 同口径）
        frontier = {(t, None) for t, _ in next_frontier}

    affected_nodes = [n for lv in levels for n in lv["nodes"]]
    return {
        "levels": levels,
        "affected": {
            "tables": sorted({n["table"] for n in affected_nodes}),
            "columns": sorted(f"{n['table']}.{n['column']}"
                              for n in affected_nodes if n["column"]),
        },
        "total_edges": sum(len(lv["edges"]) for lv in levels),
        "truncated": truncated,
    }


async def analyze_lineage_impact(tenant_id: str, kb_id: str, data_source_id: str,
                                 table: str, *, column: str | None = None,
                                 direction: str = "downstream", hops: int = MAX_HOPS,
                                 min_origin: str | None = None) -> dict:
    tenant_id = require_tenant(tenant_id)
    kb_id = str(kb_id or "").strip()
    ds_id = str(data_source_id or "").strip()
    anchor = str(table or "").strip()
    if not kb_id:
        raise InvalidParameterError(message="缺少 knowledge_base_id")
    if not ds_id:
        raise InvalidParameterError(message="缺少 data_source_id")
    if not anchor:
        raise InvalidParameterError(message="缺少 table")
    if direction not in _DIRECTIONS:
        raise InvalidParameterError(message=f"非法 direction: {direction}")
    hop_count = clamp_int(hops, default=MAX_HOPS, low=1, high=MAX_HOPS, name="hops")
    col = str(column).strip() if column is not None else None
    col = col or None
    origin_floor = str(min_origin).strip() if min_origin is not None else None
    origin_floor = origin_floor or None

    result = await walk(
        tenant_id, kb_id, ds_id, anchor,
        column=col, direction=direction, hops=hop_count,
        min_origin=origin_floor, max_edges=MAX_EDGES,
    )
    anchor_payload = {"table": anchor, "column": col}
    if result.get("unknown"):
        return {
            "anchor": anchor_payload, "direction": direction, "hops": hop_count,
            "levels": [], "affected": {"tables": [], "columns": []},
            "total_edges": 0, "truncated": False, "unknown": True,
        }

    annotated = annotate_levels(
        result.get("edges") or [], anchor, col,
        direction=direction, hops=hop_count, max_nodes=MAX_NODES,
    )
    annotated.update({
        "anchor": anchor_payload, "direction": direction, "hops": hop_count,
        "truncated": bool(result.get("truncated")) or annotated["truncated"],
        "unknown": False,
    })
    return annotated
