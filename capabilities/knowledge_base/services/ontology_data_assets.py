"""结构化数据资产只读查询（P1.5a Task 4）。

读路径：仓储 ``snapshot``（同一会话取节点 + 边）→ 纯函数 ``filter_neighbors``
（方向 / 跳数 / 未知表）→ 返回。图是派生物（权威 = compiled schema + 血缘行），
本模块只读、不触发任何投影。

方向语义与边一致（数据流方向，spec §2.2）：
``downstream`` 沿 from→to（本表 → 下游）、``upstream`` 沿 to→from（本表 ← 上游）、
``both`` 取并集。

``unknown`` 是**显式**的「这张表不在该源的投影里」，与「表在、但没有边」区分开：
前者返回空图 + ``unknown=true``，后者返回锚点节点 + ``unknown=false``。
"""
from __future__ import annotations

import json
from typing import Any, Optional

from jonex_core.common.exceptions import InvalidParameterError
from jonex_core.common.neo4j_client import get_neo4j_driver
from jonex_core.common.tenant import require_tenant

from ..repository.ontology_data_asset_repository import OntologyDataAssetRepository

_DIRECTIONS = ("upstream", "downstream", "both")
_MAX_HOPS = 3

# 画布上限（P2a）：超过即截断，并在返回里用 ``truncated=True`` 显式告知
# 前端「图没画全」——静默丢节点/边会让血缘图看起来自洽但实际残缺。
MAX_GRAPH_NODES = 200
MAX_GRAPH_EDGES = 400


def _parse_json_list(value: Any) -> list:
    """图属性里的 JSON 字符串 → list；缺失/非法值按空列表处理。

    写侧（``merge_node`` / ``merge_edge``）总是落 JSON 字符串，这里只做读侧
    还原。非法 JSON 不让整条查询炸掉：单个属性坏了仍应能返回表/边骨架。
    """
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _apply_graph_caps(nodes: list, edges: list) -> tuple[list, list, bool]:
    """按传入顺序裁剪到画布上限，返回 ``(nodes, edges, truncated)``。

    调用方先排好期望顺序（``table=None`` 是快照顺序，BFS 是发现顺序），
    这里只做「保留前 N 个」。快照顺序由仓储 Cypher 的 ``ORDER BY`` 固定
    （节点按表名；边按起点、终点、``pair_key``），所以「保留前 N 个」在
    跨请求间是确定的 —— 不会因 Neo4j 返回顺序抖动而换掉入选的节点/边。
    只要节点或边任一超限就标 ``truncated=True``（两侧都截到上限）。
    """
    if len(nodes) <= MAX_GRAPH_NODES and len(edges) <= MAX_GRAPH_EDGES:
        return nodes, edges, False
    return nodes[:MAX_GRAPH_NODES], edges[:MAX_GRAPH_EDGES], True


def filter_neighbors(nodes: list[dict], edges: list[dict],
                     table: Optional[str] = None, direction: str = "both",
                     hops: int = 1) -> dict:
    """在已取回的图快照上做方向 + 跳数过滤。纯函数（不碰 IO）。

    - ``table=None``：返回全部节点/边（``unknown=False``），但仍受
      ``MAX_GRAPH_NODES`` / ``MAX_GRAPH_EDGES`` 约束（超过则截断并置
      ``truncated=True``）。
    - 否则以 ``table`` 为锚点做 BFS：``downstream`` 沿 from→to，
      ``upstream`` 沿 to→from，``both`` 并集；``hops`` 收敛到 1..3。
    - 返回的边 = BFS **实际走过**的边；返回的节点 = 锚点 + 到达的表（去重）。
      环安全（已访问表不再入队）。
    - 节点/边超过 ``MAX_GRAPH_NODES`` / ``MAX_GRAPH_EDGES`` 时按发现顺序截断
      （节点：锚点在前 + BFS 发现序；边：快照原序），并置 ``truncated=True``。
    - ``table`` 不在节点集合里 → 空图 + ``unknown=True``、``truncated=False``。
    """
    if direction not in _DIRECTIONS:
        raise InvalidParameterError(
            message="direction 必须是 upstream/downstream/both")
    try:
        hops = int(hops)
    except (TypeError, ValueError):
        raise InvalidParameterError(message="hops 必须是整数")
    hops = max(1, min(hops, _MAX_HOPS))

    nodes = list(nodes or [])
    edges = list(edges or [])
    if table is None:
        out_nodes, out_edges, truncated = _apply_graph_caps(nodes, edges)
        return {
            "nodes": out_nodes, "edges": out_edges,
            "unknown": False, "truncated": truncated,
        }

    node_by_table = {n["table"]: n for n in nodes if n.get("table")}
    if table not in node_by_table:
        # 未知 ≠ 无：表根本没进投影，与「表在但没边」是两种结论。
        return {"nodes": [], "edges": [], "unknown": True, "truncated": False}

    # 邻接表按边索引建（同一行边在 both 下会出现两次，索引去重才可靠）；
    # 端点不在节点集合里的边一律不参与遍历（图里没有节点可落）。
    adjacency: dict[str, list[tuple[str, int]]] = {}
    for idx, edge in enumerate(edges):
        from_table = edge.get("from_table")
        to_table = edge.get("to_table")
        if not from_table or not to_table:
            continue
        if from_table not in node_by_table or to_table not in node_by_table:
            continue
        if direction in ("downstream", "both"):
            adjacency.setdefault(from_table, []).append((to_table, idx))
        if direction in ("upstream", "both"):
            adjacency.setdefault(to_table, []).append((from_table, idx))

    seen: set[str] = {table}
    order: list[str] = [table]
    frontier = [table]
    selected_idx: set[int] = set()
    for _ in range(hops):
        next_frontier: list[str] = []
        for current in frontier:
            for adjacent, idx in adjacency.get(current, ()):
                selected_idx.add(idx)
                if adjacent not in seen:
                    seen.add(adjacent)
                    order.append(adjacent)
                    next_frontier.append(adjacent)
        frontier = next_frontier
        if not frontier:
            break

    out_nodes, out_edges, truncated = _apply_graph_caps(
        # 锚点在前，其后按 BFS 发现顺序；只回投影里真实存在的节点。
        [node_by_table[t] for t in order if t in node_by_table],
        # 保持快照里的原始边序（不按遍历顺序抖动）。
        [edges[i] for i in sorted(selected_idx)],
    )
    return {
        "nodes": out_nodes,
        "edges": out_edges,
        "unknown": False,
        "truncated": truncated,
    }


async def get_data_assets(tenant_id: str, kb_id: str, data_source_id: str,
                          table: Optional[str] = None, direction: str = "both",
                          hops: int = 1) -> dict:
    """该 KB + 数据源作用域内的数据资产子图（只读）。

    ``table`` 缺省 = 全部节点/边；给了就按方向/跳数取锚点子图。
    返回 ``{"nodes": [...], "edges": [...], "unknown": bool, "truncated": bool}``
    （``truncated`` 由 ``filter_neighbors`` 的画布上限决定，原样透传），其中
    ``attributes`` / ``column_map`` 已从 JSON 字符串还原为列表。
    """
    tenant_id = require_tenant(tenant_id)
    if not str(data_source_id or "").strip():
        # 空 ds 会让 Neo4j 作用域查询退化成「同 KB 全部源」——必须挡在入口。
        raise InvalidParameterError(message="缺少 data_source_id")
    if table is not None and not isinstance(table, str):
        # 能力直调绕过网关的 Query 类型校验；非字符串在下面 .strip() 会炸成 500。
        raise InvalidParameterError(message="table 必须是字符串")

    snapshot = await OntologyDataAssetRepository(
        get_neo4j_driver()).snapshot(tenant_id, kb_id, data_source_id)
    nodes = []
    for row in snapshot.get("nodes") or []:
        item = dict(row)
        item["attributes"] = _parse_json_list(item.get("attributes"))
        nodes.append(item)
    edges = []
    for row in snapshot.get("edges") or []:
        item = dict(row)
        item["column_map"] = _parse_json_list(item.get("column_map"))
        edges.append(item)

    return filter_neighbors(
        nodes, edges,
        table=(table or "").strip() or None,
        direction=direction,
        hops=hops,
    )
