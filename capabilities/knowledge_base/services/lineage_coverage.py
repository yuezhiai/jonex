"""血缘覆盖率：active per-source compiled schema 作分母，活血缘边作分子。

分母口径与 `LineageEdgeService._schema_tables` 完全一致：
表 = `source_object_id`，列 = `attribute.name`。**只读、读时聚合**。

`unknown` 的语义：读边失败 ⇒ `unknown=True`、比率置 None —— 不能把故障
伪装成 0%（那会被读成「这个源一条血缘都没有」）。
"""
from __future__ import annotations

import logging
from collections import Counter

from jonex_core.common import get_db_session
from jonex_core.common.exceptions import InvalidParameterError
from jonex_core.common.tenant import require_tenant

from ..repository.ontology_schema_repository import OntologySchemaRepository

logger = logging.getLogger(__name__)

MAX_TABLES = 200


def _ratio(covered: int, total: int) -> float | None:
    return round(covered / total, 4) if total else None


def compute_coverage(entity_types: list[dict], edges: list[dict], *,
                     max_tables: int = MAX_TABLES) -> dict:
    tables: dict[str, set[str]] = {}
    entity_names: dict[str, str] = {}
    for entity in entity_types or []:
        if not isinstance(entity, dict):
            continue
        table = str(entity.get("source_object_id") or "").strip()
        if not table:
            continue
        tables[table] = {
            str(attr.get("name") or "")
            for attr in (entity.get("attributes") or []) if isinstance(attr, dict)
        }
        entity_names[table] = str(entity.get("name") or "")

    covered_tables: set[str] = set()
    covered_columns: set[tuple[str, str]] = set()
    by_origin: Counter[str] = Counter()
    orphan_edges = 0
    total_edges = 0
    for edge in edges or []:
        if not isinstance(edge, dict):
            continue
        ft = str(edge.get("from_table") or "")
        fc = str(edge.get("from_column") or "")
        tt = str(edge.get("to_table") or "")
        tc = str(edge.get("to_column") or "")
        if not (ft and fc and tt and tc):
            continue
        total_edges += 1
        by_origin[str(edge.get("origin") or "") or "unknown"] += 1
        orphan = False
        # 表覆盖按 `(table, *)` 命中：只要端点表在 schema 里，该表就算被覆盖，
        # 即使这一侧的列名对不上（列未命中只影响列覆盖，并让整条边记为孤立）。
        for table, column in ((ft, fc), (tt, tc)):
            if table not in tables:
                orphan = True
            else:
                covered_tables.add(table)
                if column in tables[table]:
                    covered_columns.add((table, column))
                else:
                    orphan = True
        if orphan:
            orphan_edges += 1

    total_tables = len(tables)
    total_columns = sum(len(cols) for cols in tables.values())
    per_table = []
    for table in sorted(tables):
        columns = tables[table]
        covered = sum(1 for column in columns if (table, column) in covered_columns)
        per_table.append({
            "table": table,
            "entity_name": entity_names.get(table, ""),
            "columns_total": len(columns),
            "columns_covered": covered,
            "covered": table in covered_tables,
        })
    truncated = len(per_table) > max_tables

    return {
        "tables": {"total": total_tables, "covered": len(covered_tables),
                   "ratio": _ratio(len(covered_tables), total_tables)},
        "columns": {"total": total_columns, "covered": len(covered_columns),
                    "ratio": _ratio(len(covered_columns), total_columns)},
        "by_origin": dict(sorted(by_origin.items())),
        "orphan_edges": orphan_edges,
        "total_edges": total_edges,
        "per_table": per_table[:max_tables],
        "truncated": truncated,
        "unknown": False,
    }


async def _load_schema_row(session, tenant_id: str, kb_id: str, ds_id: str):
    """取该源的 active 编译 schema 行（`list_compiled_schemas` 只回 active）。"""
    rows = await OntologySchemaRepository(session).list_compiled_schemas(tenant_id, kb_id)
    return next((row for row in rows if row.data_source_id == ds_id), None)


async def _load_edge_rows(session, tenant_id: str, kb_id: str, ds_id: str) -> list[dict]:
    """读活血缘边；失败**抛出**（由服务层转 unknown，不得 fail-open 成空图）。"""
    from sqlalchemy import select

    from ..models.lineage_edge import LineageEdge

    stmt = select(LineageEdge).where(
        LineageEdge.tenant_id == tenant_id,
        LineageEdge.knowledge_base_id == kb_id,
        LineageEdge.data_source_id == ds_id,
        LineageEdge.is_deleted == 0,
    )
    rows = list((await session.execute(stmt)).scalars().all())
    return [
        {"from_table": str(r.from_table), "from_column": str(r.from_column),
         "to_table": str(r.to_table), "to_column": str(r.to_column),
         "origin": str(r.origin or "")}
        for r in rows
    ]


async def get_lineage_coverage(tenant_id: str, kb_id: str,
                               data_source_id: str) -> dict:
    tenant_id = require_tenant(tenant_id)
    ds_id = str(data_source_id or "").strip()
    kb_id = str(kb_id or "").strip()
    if not ds_id:
        raise InvalidParameterError(message="缺少 data_source_id")
    if not kb_id:
        raise InvalidParameterError(message="缺少 knowledge_base_id")

    # 第一个会话只读 schema 行：DB 故障原样外抛，不能被当成「没有 schema」。
    async with get_db_session() as session:
        row = await _load_schema_row(session, tenant_id, kb_id, ds_id)
        if row is None:
            raise InvalidParameterError(
                message=f"该数据源没有可用的编译 schema：{ds_id}")
        entity_types = list(row.entity_types or [])

    # 第二个会话只读边。try/except 包在 `async with` **外层**：异常穿过
    # generator 的 `__aexit__`，由 `get_db_session` 走 rollback，而不是把
    # 失败的读事务正常提交。读失败同样必须显式 unknown（不 fail-open 成空图）。
    try:
        async with get_db_session() as session:
            edges = await _load_edge_rows(session, tenant_id, kb_id, ds_id)
    except Exception as exc:  # noqa: BLE001 —— 读失败必须显式 unknown
        logger.warning("血缘覆盖率读边失败，本次返回 unknown：%s", exc)
        out = compute_coverage(entity_types, [])
        out["unknown"] = True
        out["tables"]["ratio"] = None
        out["columns"]["ratio"] = None
        out["data_source_id"] = ds_id
        return out

    out = compute_coverage(entity_types, edges)
    out["data_source_id"] = ds_id
    return out
