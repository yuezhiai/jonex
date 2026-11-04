"""表 / 列 / 指标的一跳血缘装配。

- 只读 PG `knowledge_base.lineage_edges`，**不读 Neo4j、不写图、不触发投影**；
- `depth` 只支持 1（其他值 1001），`direction` 为 `up/down/both`；
- 表名两端按「最后一段」归一（与 ② 安全闸同口径：`dbo.jobs` / `[dbo].[jobs]` / `jobs` 等价）；
- column 资产同时返回列级边（`meta.level='column'`）与所属表的表级边（`meta.level='table'`）；
- metric 先出虚拟边 `metric → landing 表`（`type='lands_on'`，只在下游方向），再叠加这些表的一跳血缘；
- DB/存储失败 fail-open：`degraded=true`、节点保留、边为空，不 500。

返回形状对齐文档级血缘契约：
`{asset, nodes:[{id,type,label,meta}], edges:[{source,target,type,meta}],
  counts:{nodes,edges}, truncated, degraded, unresolved}`。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from jonex_core.common.config import config
from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import InvalidParameterError
from jonex_core.common.tenant import require_tenant

from ..asset_identity import split_asset_id

logger = logging.getLogger(__name__)

DIRECTIONS = frozenset({"up", "down", "both"})

#: `origin` 阶梯（与血缘边/治理同口径）；其余来源不臆造置信度。
CONFIDENCE_BY_ORIGIN = {
    "declared": 1.0,
    "manual": 1.0,
    "comment": 0.8,
    "platform": 0.6,
    "value": 0.4,
    "derived": 0.2,
}

#: `dbo.Jobs` / `[dbo].[jobs]` / `` `jobs` `` → `jobs`（小写、只取最后一段）。
_TABLE_KEY_SQL = (
    "lower(btrim(regexp_replace(regexp_replace({expr}, '^.*\\.', ''), "
    "'[\\[\\]`\"]', '', 'g')))"
)

_SELECT_EDGE = """
    SELECT from_table, from_column, to_table, to_column, origin, note,
           last_seen_at, data_source_id, knowledge_base_id
    FROM knowledge_base.lineage_edges
    WHERE tenant_id = :tenant_id AND data_source_id = :ds_id AND is_deleted = 0
      AND ({clauses})
"""


def _table_key(name) -> str:
    """把表名归一成最后一段的小写（`dbo.jobs` / `[dbo].[jobs]` → `jobs`）。"""
    cleaned = str(name or "").strip()
    for ch in "[]`\"":
        cleaned = cleaned.replace(ch, "")
    if cleaned.endswith("."):
        return ""
    parts = [part for part in cleaned.split(".") if part]
    return (parts[-1] if parts else "").strip().lower()


def _column_key(name) -> str:
    return str(name or "").strip().lower()


def _confidence(origin) -> float | None:
    return CONFIDENCE_BY_ORIGIN.get(str(origin or "").strip().lower())


def _node(node_id: str, node_type: str, label, **meta) -> dict:
    return {
        "id": node_id,
        "type": node_type,
        "label": str(label or node_id),
        "meta": meta,
    }


def _table_node(ds_id: str, table) -> dict:
    key = _table_key(table)
    return {
        "id": f"{ds_id}:{key}",
        "type": "table",
        "label": str(table or key),
        "meta": {"name": key, "table": key},
    }


def _column_node(ds_id: str, table, column) -> dict:
    table_key = _table_key(table)
    column_key = _column_key(column)
    return {
        "id": f"{ds_id}:{table_key}.{column_key}",
        "type": "column",
        "label": str(column or column_key),
        "meta": {"name": str(column or ""), "table": table_key, "column": str(column or "")},
    }


def _root_node_id(asset_type: str, scope_id: str, table: str, column: str) -> str:
    """根节点与边端点同口径：表名最后一段 + 列名小写。"""
    table_key = _table_key(table)
    if asset_type == "column":
        return f"{scope_id}:{table_key}.{_column_key(column)}"
    return f"{scope_id}:{table_key}"


def _is_stale(row: dict) -> bool:
    """读时标 `stale`：只对机器来源（origin=platform）按滚动窗口判定。"""
    days = int(getattr(config, "LINEAGE_STALE_AFTER_DAYS", 30) or 0)
    if days <= 0 or str(row.get("origin") or "") != "platform":
        return False
    raw = row.get("last_seen_at")
    if not raw:
        return False
    try:
        seen = datetime.fromisoformat(str(raw))
    except ValueError:
        return False
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return seen < datetime.now(timezone.utc) - timedelta(days=days)


def _graph(
    asset: dict, nodes: list[dict], edges: list[dict], *,
    degraded: bool = False, unresolved: bool = False,
) -> dict:
    return {
        "asset": asset,
        "nodes": nodes,
        "edges": edges,
        "counts": {"nodes": len(nodes), "edges": len(edges)},
        "truncated": False,
        "degraded": bool(degraded),
        "unresolved": bool(unresolved),
    }


async def _metric_landings(tenant_id: str, kb_id: str, name: str) -> list[dict]:
    """从 active 抽象 schema 取指标落点，并解析成物理表。

    详情路径（`get_asset`）已经解析过落点，正常请求不会走到这里；本函数是
    直接调用装配层时的兜底，解析复用 `resolve_landing_tables`。
    """
    from ..repository.catalog_read_repository import (
        normalize_metric_landings,
        resolve_landing_tables,
    )

    async with get_db_session() as session:
        row = (
            await session.execute(text("""
                SELECT coalesce(m->'landings', '[]'::jsonb) AS landings
                FROM knowledge_base.ontology_abstract_schemas s
                CROSS JOIN LATERAL jsonb_array_elements(
                    coalesce(s.schema->'metrics', '[]'::jsonb)
                ) m
                WHERE s.tenant_id = :tenant_id AND s.status = 'active'
                  AND s.is_deleted = 0
                  AND s.knowledge_base_id = :kb_id AND m->>'name' = :name
                LIMIT 1
            """), {
                "tenant_id": tenant_id, "kb_id": kb_id, "name": name,
            })
        ).mappings().first()
        if not row:
            return []
        return await resolve_landing_tables(
            session,
            tenant_id=tenant_id,
            landings=normalize_metric_landings(row.get("landings")),
        )


async def _edges(tenant_id: str, ds_id: str, table: str, direction: str) -> list[dict]:
    clauses = []
    if direction in ("down", "both"):
        clauses.append(
            "(from_table = :table_key OR "
            f"{_TABLE_KEY_SQL.format(expr='from_table')} = :table_key)"
        )
    if direction in ("up", "both"):
        clauses.append(
            "(to_table = :table_key OR "
            f"{_TABLE_KEY_SQL.format(expr='to_table')} = :table_key)"
        )
    sql = _SELECT_EDGE.format(clauses=" OR ".join(clauses))
    async with get_db_session() as session:
        rows = (
            await session.execute(text(sql), {
                "tenant_id": tenant_id,
                "ds_id": ds_id,
                "table_key": _table_key(table),
            })
        ).mappings().all()
    return [dict(row) for row in rows]


def _match_level(
    row: dict, *, asset_type: str, table_key: str, column: str, direction: str,
) -> str | None:
    """判断边与资产的匹配层级；SQL 只做候选筛选，这里再按端点和列精确判定。"""
    from_key = _table_key(row.get("from_table"))
    to_key = _table_key(row.get("to_table"))
    column_key = _column_key(column)
    down = direction in ("down", "both")
    up = direction in ("up", "both")
    if asset_type == "column":
        if (
            down and from_key == table_key
            and _column_key(row.get("from_column")) == column_key
        ):
            return "column"
        if (
            up and to_key == table_key
            and _column_key(row.get("to_column")) == column_key
        ):
            return "column"
        # 列级未命中时回落表级：所属表的任意入/出边。
        if down and from_key == table_key:
            return "table"
        if up and to_key == table_key:
            return "table"
        return None
    if down and from_key == table_key:
        return "table"
    if up and to_key == table_key:
        return "table"
    return None


def _lineage_edge(ds_id: str, row: dict, level: str) -> dict | None:
    from_key = _table_key(row.get("from_table"))
    to_key = _table_key(row.get("to_table"))
    if not from_key or not to_key:
        return None
    origin = row.get("origin")
    raw_seen = row.get("last_seen_at")
    return {
        "source": f"{ds_id}:{from_key}",
        "target": f"{ds_id}:{to_key}",
        "type": "lineage",
        "meta": {
            "kind": "lineage",
            "level": level,
            "origin": origin,
            "confidence": _confidence(origin),
            "stale": _is_stale(row),
            "note": row.get("note"),
            "last_seen_at": (
                raw_seen.isoformat() if hasattr(raw_seen, "isoformat")
                else str(raw_seen)
            ) if raw_seen else None,
            "from": {
                "data_source_id": ds_id,
                "table": row.get("from_table"),
                "column": row.get("from_column"),
            },
            "to": {
                "data_source_id": ds_id,
                "table": row.get("to_table"),
                "column": row.get("to_column"),
            },
        },
    }


def _edge_key(edge: dict) -> tuple:
    meta = edge["meta"]
    return (
        edge["source"],
        edge["target"],
        edge["type"],
        _column_key((meta.get("from") or {}).get("column")),
        _column_key((meta.get("to") or {}).get("column")),
        str(meta.get("origin") or ""),
    )


def _append_node(nodes: list[dict], seen: set[str], node: dict) -> None:
    if node["id"] not in seen:
        seen.add(node["id"])
        nodes.append(node)


def _append_table_node(
    nodes: list[dict], seen: set[str], ds_id: str, table,
) -> None:
    """按「最后一段表名」归一后去重（`[dbo].[jobs]` 与 `jobs` 是同一个节点）。"""
    if not _table_key(table):
        return
    _append_node(nodes, seen, _table_node(ds_id, table))


def _append_endpoint_node(
    nodes: list[dict], seen: set[str], ds_id: str, table, column,
) -> None:
    """列资产的边端点：有列名落列节点，否则保持表节点。"""
    if _column_key(column):
        _append_node(nodes, seen, _column_node(ds_id, table, column))
    else:
        _append_table_node(nodes, seen, ds_id, table)


def _column_endpoints(
    ds_id: str, row: dict, *, root_table: str, root_column: str,
) -> tuple[str, str]:
    """列资产的边端点 → 列节点 id，保证根节点与边相连。

    - 根列所属表上的端点：列名与根列一致（或该端无列）时用根列 id；
      同表其他列保留自己的 id，避免把 `jobs.title` 的边错锚到 `jobs.id`。
    - 其他表端点：有列名用列节点，否则保持表节点（表级血缘）。
    """

    def endpoint(table, column) -> str:
        table_key = _table_key(table)
        column_key = _column_key(column)
        if table_key == root_table:
            if not column_key or column_key == root_column:
                return f"{ds_id}:{root_table}.{root_column}"
            return f"{ds_id}:{root_table}.{column_key}"
        if column_key:
            return f"{ds_id}:{table_key}.{column_key}"
        return f"{ds_id}:{table_key}"

    return (
        endpoint(row.get("from_table"), row.get("from_column")),
        endpoint(row.get("to_table"), row.get("to_column")),
    )


async def _metric_graph(
    tenant_id: str, asset: dict, *, asset_id: str, kb_id: str, name: str,
    direction: str,
) -> dict:
    landings = asset.get("landings")
    degraded = False
    if not isinstance(landings, list):
        try:
            landings = await _metric_landings(tenant_id, kb_id, name)
        except (SQLAlchemyError, OSError):
            logger.warning("metric landings degraded for %s", asset_id, exc_info=True)
            landings = []
            degraded = True

    valid = [item for item in landings or [] if isinstance(item, dict)]
    label = asset.get("name") or name
    nodes = [_node(asset_id, "metric", label, name=name)]
    seen = {asset_id}
    edges: list[dict] = []
    edge_keys: set[tuple] = set()
    landing_tables: list[tuple[str, str]] = []
    landing_seen: set[tuple[str, str]] = set()
    add_lands_on = direction in ("down", "both")

    for landing in valid:
        ds_id = str(landing.get("data_source_id") or "")
        table = landing.get("table")
        if not ds_id or not table:
            continue
        dedupe_key = (ds_id, _table_key(table))
        if dedupe_key in landing_seen:
            continue
        landing_seen.add(dedupe_key)
        _append_table_node(nodes, seen, ds_id, table)
        landing_tables.append((ds_id, table))
        if add_lands_on:
            edge = {
                "source": asset_id,
                "target": f"{ds_id}:{_table_key(table)}",
                "type": "lands_on",
                "meta": {
                    "kind": "lands_on",
                    "origin": "declared",
                    "confidence": CONFIDENCE_BY_ORIGIN["declared"],
                    "from": {"metric": asset_id},
                    "to": {
                        "data_source_id": ds_id,
                        "table": table,
                        "column": "",
                    },
                },
            }
            key = _edge_key(edge)
            if key not in edge_keys:
                edge_keys.add(key)
                edges.append(edge)

    # 落点表的一跳血缘（best-effort：单表查询失败只丢该表这段）。
    for ds_id, table in landing_tables:
        try:
            rows = await _edges(tenant_id, ds_id, table, direction)
        except (SQLAlchemyError, OSError):
            logger.warning(
                "metric landing lineage degraded for %s/%s", ds_id, table,
                exc_info=True,
            )
            degraded = True
            continue
        table_key = _table_key(table)
        for row in rows:
            level = _match_level(
                row, asset_type="table", table_key=table_key,
                column="", direction=direction,
            )
            if level is None:
                continue
            edge = _lineage_edge(ds_id, row, level)
            if edge is None:
                continue
            key = _edge_key(edge)
            if key in edge_keys:
                continue
            edge_keys.add(key)
            edges.append(edge)
            _append_table_node(nodes, seen, ds_id, row.get("from_table"))
            _append_table_node(nodes, seen, ds_id, row.get("to_table"))

    unresolved = False
    if not degraded:
        unresolved = not valid or all(
            not item.get("table") for item in valid
        )
    return _graph(asset, nodes, edges, degraded=degraded, unresolved=unresolved)


async def build_data_asset_graph(
    tenant_id: str, asset: dict, *, direction: str = "both", depth: int = 1,
) -> dict:
    """派生资产（table / column / metric）的一跳血缘图。"""
    tenant_id = require_tenant(tenant_id)
    if depth is None:
        depth = 1
    if depth != 1:
        raise InvalidParameterError(message="depth 只支持 1", details={"depth": depth})
    if direction is None:
        direction = "both"
    if not isinstance(direction, str) or direction.strip().lower() not in DIRECTIONS:
        raise InvalidParameterError(
            message="direction 必须是 up/down/both", details={"direction": direction},
        )
    direction = direction.strip().lower()

    asset_type = asset.get("asset_type")
    asset_id = str(asset.get("asset_id") or "")
    scope_id, table, column = split_asset_id(asset_type, asset_id)

    if asset_type == "metric":
        return await _metric_graph(
            tenant_id, asset, asset_id=asset_id, kb_id=scope_id, name=table,
            direction=direction,
        )

    root_id = _root_node_id(asset_type, scope_id, table, column)
    root_label = asset.get("name") or (column if asset_type == "column" else table)
    root_meta = {"name": table, "table": _table_key(table)}
    if asset_type == "column":
        root_meta["column"] = column
    nodes = [_node(root_id, asset_type, root_label, **root_meta)]
    try:
        rows = await _edges(tenant_id, scope_id, table, direction)
    except (SQLAlchemyError, OSError):
        logger.warning("asset lineage degraded for %s", asset_id, exc_info=True)
        return _graph(asset, nodes, [], degraded=True)

    seen = {root_id}
    edges: list[dict] = []
    edge_keys: set[tuple] = set()
    table_key = _table_key(table)
    for row in rows:
        level = _match_level(
            row, asset_type=asset_type, table_key=table_key,
            column=column, direction=direction,
        )
        if level is None:
            continue
        edge = _lineage_edge(scope_id, row, level)
        if edge is None:
            continue
        if asset_type == "column":
            # 列资产的边端点落到列节点：根列 id 至少出现在一条边的
            # source/target 上，详情画布的中心节点才不会悬空。
            edge["source"], edge["target"] = _column_endpoints(
                scope_id, row,
                root_table=table_key, root_column=_column_key(column),
            )
        key = _edge_key(edge)
        if key in edge_keys:
            continue
        edge_keys.add(key)
        edges.append(edge)
        if asset_type == "column":
            _append_endpoint_node(
                nodes, seen, scope_id, row.get("from_table"), row.get("from_column"),
            )
            _append_endpoint_node(
                nodes, seen, scope_id, row.get("to_table"), row.get("to_column"),
            )
        else:
            _append_table_node(nodes, seen, scope_id, row.get("from_table"))
            _append_table_node(nodes, seen, scope_id, row.get("to_table"))
    return _graph(asset, nodes, edges)
