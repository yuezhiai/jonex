"""目录实时读模型：文档 / 数据源 / 表 / 列 / 指标 UNION，左连 asset_metadata。

无投影、无缓存：每次请求都从业务表实时读。所有用户值只进绑定参数，
SQL 字面量里不出现调用方输入（`keyword` 的 `%` 包裹在 Python 侧完成）。
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_BASE = """
WITH assets AS (
    SELECT 'document' AS asset_type, d.id AS asset_id, d.file_name AS name,
           d.knowledge_base_id AS kb_id, d.status AS status, d.updated_at,
           NULL::text AS source_id, NULL::text AS parent_id,
           NULL::int AS schema_version
    FROM knowledge_base.knowledge_documents d
    JOIN knowledge_base.knowledge_info k
      ON k.id = d.knowledge_base_id AND k.tenant_id = d.tenant_id AND k.is_deleted = 0
    WHERE d.tenant_id = :tenant_id AND d.is_deleted = 0
      {doc_filters}
    UNION ALL
    SELECT 'data_source' AS asset_type, s.id AS asset_id, s.name AS name,
           s.knowledge_base_id AS kb_id, s.status AS status, s.updated_at,
           NULL::text AS source_id, NULL::text AS parent_id,
           NULL::int AS schema_version
    FROM knowledge_base.knowledge_data_sources s
    JOIN knowledge_base.knowledge_info k
      ON k.id = s.knowledge_base_id AND k.tenant_id = s.tenant_id AND k.is_deleted = 0
    WHERE s.tenant_id = :tenant_id AND s.is_deleted = 0
      {source_filters}
    UNION ALL
    SELECT DISTINCT 'table' AS asset_type,
           cs.data_source_id || ':' || (e->>'source_object_id') AS asset_id,
           e->>'source_object_id' AS name,
           cs.knowledge_base_id AS kb_id,
           cs.sync_status AS status,
           cs.updated_at,
           cs.data_source_id AS source_id,
           cs.data_source_id AS parent_id,
           cs.schema_version
    FROM knowledge_base.ontology_compiled_schemas cs
    JOIN knowledge_base.knowledge_info k
      ON k.id = cs.knowledge_base_id AND k.tenant_id = cs.tenant_id AND k.is_deleted = 0
    CROSS JOIN LATERAL jsonb_array_elements(coalesce(cs.entity_types, '[]'::jsonb)) e
    WHERE cs.tenant_id = :tenant_id AND cs.status = 'active'
      AND cs.data_source_id IS NOT NULL
      AND btrim(coalesce(e->>'source_object_id', '')) <> ''
      {table_filters}
    UNION ALL
    SELECT DISTINCT 'column' AS asset_type,
           cs.data_source_id || ':' || (e->>'source_object_id') || '.' ||
             coalesce(nullif(a->>'source_attribute_id',''), a->>'name') AS asset_id,
           coalesce(nullif(a->>'source_attribute_id',''), a->>'name') AS name,
           cs.knowledge_base_id AS kb_id,
           cs.sync_status AS status,
           cs.updated_at,
           cs.data_source_id AS source_id,
           cs.data_source_id || ':' || (e->>'source_object_id') AS parent_id,
           cs.schema_version
    FROM knowledge_base.ontology_compiled_schemas cs
    JOIN knowledge_base.knowledge_info k
      ON k.id = cs.knowledge_base_id AND k.tenant_id = cs.tenant_id AND k.is_deleted = 0
    CROSS JOIN LATERAL jsonb_array_elements(coalesce(cs.entity_types, '[]'::jsonb)) e
    CROSS JOIN LATERAL jsonb_array_elements(coalesce(e->'attributes', '[]'::jsonb)) a
    WHERE cs.tenant_id = :tenant_id AND cs.status = 'active'
      AND cs.data_source_id IS NOT NULL
      AND btrim(coalesce(e->>'source_object_id', '')) <> ''
      AND btrim(coalesce(nullif(a->>'source_attribute_id',''), a->>'name', '')) <> ''
      {column_filters}
    UNION ALL
    SELECT DISTINCT 'metric' AS asset_type,
           s.knowledge_base_id || ':' || (m->>'name') AS asset_id,
           m->>'name' AS name,
           s.knowledge_base_id AS kb_id,
           'active' AS status,
           s.updated_at,
           s.knowledge_base_id AS source_id,
           s.knowledge_base_id AS parent_id,
           s.schema_version
    FROM knowledge_base.ontology_abstract_schemas s
    JOIN knowledge_base.knowledge_info k
      ON k.id = s.knowledge_base_id AND k.tenant_id = s.tenant_id AND k.is_deleted = 0
    CROSS JOIN LATERAL jsonb_array_elements(coalesce(s.schema->'metrics', '[]'::jsonb)) m
    WHERE s.tenant_id = :tenant_id AND s.status = 'active' AND s.is_deleted = 0
      AND btrim(coalesce(m->>'name', '')) <> ''
      {metric_filters}
),
rows AS (
    SELECT a.asset_type, a.asset_id, a.name, a.kb_id, a.status, a.updated_at,
           a.source_id, a.parent_id, a.schema_version,
           k.name AS kb_name, k.space_id,
           m.owner, m.domain,
           m.sensitivity AS asset_level,
           m.classification_categories AS asset_categories,
           kbd.level_code AS kb_level,
           kbd.categories AS kb_categories,
           td.level_code AS tenant_level,
           td.categories AS tenant_categories,
           CASE WHEN a.asset_type IN ('table','column') THEN src.name
                WHEN a.asset_type = 'metric' THEN k.name
                ELSE NULL END AS source_label,
           tm.sensitivity AS table_level,
           tm.classification_categories AS table_categories,
           COALESCE(m.sensitivity,
                    CASE WHEN a.asset_type = 'column' THEN tm.sensitivity END,
                    kbd.level_code, td.level_code) AS effective_level,
           COALESCE(m.classification_categories,
                    CASE WHEN a.asset_type = 'column'
                         THEN tm.classification_categories END,
                    kbd.categories, td.categories,
                    '[]'::jsonb) AS effective_categories,
           CASE
               WHEN COALESCE(m.sensitivity, kbd.level_code, td.level_code) IS NULL
                   THEN 0
               ELSE COALESCE(
                   lvl.rank,
                   CASE COALESCE(m.sensitivity, kbd.level_code, td.level_code)
                       WHEN 'public' THEN 1
                       WHEN 'internal' THEN 2
                       WHEN 'confidential' THEN 3
                       ELSE 1000000
                   END
               )
           END AS effective_rank
    FROM assets a
    JOIN knowledge_base.knowledge_info k
      ON k.id = a.kb_id AND k.tenant_id = :tenant_id
    LEFT JOIN data_governance.asset_metadata m
      ON m.tenant_id = :tenant_id AND m.asset_type = a.asset_type
     AND m.asset_id = a.asset_id
    LEFT JOIN knowledge_base.knowledge_data_sources src
      ON src.id = a.source_id AND src.tenant_id = :tenant_id AND src.is_deleted = 0
    LEFT JOIN data_governance.asset_metadata tm
      ON tm.tenant_id = :tenant_id AND tm.asset_type = 'table'
     AND a.asset_type = 'column'
     AND tm.asset_id = a.parent_id
    LEFT JOIN data_governance.classification_default kbd
      ON kbd.tenant_id = :tenant_id AND kbd.scope_type = 'kb'
     AND kbd.scope_id = a.kb_id
    LEFT JOIN data_governance.classification_default td
      ON td.tenant_id = :tenant_id AND td.scope_type = 'tenant'
     AND td.scope_id = ''
    LEFT JOIN data_governance.classification_level lvl
      ON lvl.tenant_id = :tenant_id
     AND lvl.code = COALESCE(m.sensitivity, kbd.level_code, td.level_code)
    WHERE a.asset_type NOT IN ('table','column') OR src.id IS NOT NULL
)
SELECT * FROM rows
WHERE 1=1 {meta_filters}
{order_limit}
"""

# 外层查询没有 `a` 别名：排序/分页只能引用 `rows` 的列。
_ORDER_LIMIT = "ORDER BY updated_at DESC, asset_id\nLIMIT :limit OFFSET :offset"

_DETAIL_SQL = {
    "document": """
        SELECT file_size, mime_type, data_source_type, folder_id,
               content_generation, ontology_status, archived_at, created_at
        FROM knowledge_base.knowledge_documents
        WHERE id = :asset_id AND tenant_id = :tenant_id AND is_deleted = 0
    """,
    "data_source": """
        SELECT access_type, sync_mode, last_sync_at, last_sync_status,
               document_count, NULL AS archived_at, created_at
        FROM knowledge_base.knowledge_data_sources
        WHERE id = :asset_id AND tenant_id = :tenant_id AND is_deleted = 0
    """,
    # 表的 detail 只取 compiled schema 元信息；列清单与 `column_count` 由
    # `fetch_table_columns` 的同源结果给出（零列时详情仍返回一行）。
    "table": """
        SELECT cs.data_source_id, cs.schema_version, cs.sync_status,
               cs.source_type, cs.compiled_at,
               e->>'source_object_id' AS table
        FROM knowledge_base.ontology_compiled_schemas cs
        CROSS JOIN LATERAL jsonb_array_elements(
            coalesce(cs.entity_types, '[]'::jsonb)
        ) e
        WHERE cs.tenant_id = :tenant_id AND cs.status = 'active'
          AND cs.data_source_id IS NOT NULL
          AND cs.data_source_id || ':' || (e->>'source_object_id') = :asset_id
        LIMIT 1
    """,
    # compiled schema 的属性键是 `is_primary_key`（ontology_compiler 归一）；
    # 旧数据可能残留 `primary_key`，按前者优先、后者兜底。
    "column": """
        SELECT cs.data_source_id,
               e->>'source_object_id' AS table,
               coalesce(nullif(a->>'source_attribute_id',''), a->>'name') AS name,
               a->>'type' AS column_type,
               coalesce(
                   (a->>'is_primary_key')::boolean,
                   (a->>'primary_key')::boolean,
                   false
               ) AS primary_key,
               a->>'display_name' AS display_name
        FROM knowledge_base.ontology_compiled_schemas cs
        CROSS JOIN LATERAL jsonb_array_elements(
            coalesce(cs.entity_types, '[]'::jsonb)
        ) e
        CROSS JOIN LATERAL jsonb_array_elements(
            coalesce(e->'attributes', '[]'::jsonb)
        ) a
        WHERE cs.tenant_id = :tenant_id AND cs.status = 'active'
          AND cs.data_source_id IS NOT NULL
          AND cs.data_source_id || ':' || (e->>'source_object_id') || '.' ||
              coalesce(nullif(a->>'source_attribute_id',''), a->>'name') = :asset_id
        LIMIT 1
    """,
    "metric": """
        SELECT s.schema_version, m->>'description' AS description,
               m->>'additivity' AS additivity,
               coalesce(m->'aliases', '[]'::jsonb) AS aliases,
               coalesce(m->'landings', '[]'::jsonb) AS landings
        FROM knowledge_base.ontology_abstract_schemas s
        CROSS JOIN LATERAL jsonb_array_elements(
            coalesce(s.schema->'metrics', '[]'::jsonb)
        ) m
        WHERE s.tenant_id = :tenant_id AND s.status = 'active'
          AND s.is_deleted = 0
          AND s.knowledge_base_id || ':' || (m->>'name') = :asset_id
        LIMIT 1
    """,
}


def _escape_like(value: str) -> str:
    """转义 ILIKE 字面量里的通配符（反斜杠必须先转，否则会二次转义）。"""
    return (
        value.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def build_catalog_sql(
    *,
    tenant_id: str,
    asset_type: str | None = None,
    kb_ids: list[str] | None = None,
    space_id: str | None = None,
    kb_id: str | None = None,
    keyword: str | None = None,
    domain: str | None = None,
    owner: str | None = None,
    sensitivity: str | None = None,
    category: str | None = None,
    status: str | None = None,
    asset_id: str | None = None,
    include_archived: bool = False,
    max_rank: int | None = None,
    limit: int = 20,
    offset: int = 0,
    count_only: bool = False,
) -> tuple[str, dict]:
    """构造目录查询 SQL 与绑定参数。

    `count_only=True` 时**不追加** `ORDER BY / LIMIT / OFFSET`，供
    `count()` 把整段 body 包进 `SELECT count(*) FROM (...) AS c` 使用
    （不靠字符串切割，避免排序子句变化时静默截错）。

    `include_archived=False`（默认）时文档分支排除 `status='archived'`
    （spec §8/§9.3）；详情/显式筛选归档时由 service 传 `True` 保留。
    """
    params: dict = {"tenant_id": tenant_id}
    if not count_only:
        params["limit"] = limit
        params["offset"] = offset

    # 五个 CTE 分支共用的筛选片段：各占位符由分支实参填空。
    common: list[str] = []
    if space_id:
        # 每个分支各有自己的 k 别名（所属 KB），同一条件对所有分支生效。
        common.append("AND k.space_id = :space_id")
        params["space_id"] = space_id
    if kb_id:
        common.append("AND {kb_expr} = :kb_id")
        params["kb_id"] = kb_id
    if kb_ids is not None:
        common.append("AND {kb_expr} = ANY(:kb_ids)")
        params["kb_ids"] = list(kb_ids)
    if keyword:
        # ESCAPE '\' 让用户输入的 % / _ 按字面量匹配，而不是通配符。
        common.append("AND {name_col} ILIKE :keyword ESCAPE '\\'")
        params["keyword"] = f"%{_escape_like(str(keyword))}%"
    if status:
        common.append("AND {status_expr} = :status")
        params["status"] = status
    if asset_id:
        common.append("AND {id_expr} = :asset_id")
        params["asset_id"] = asset_id

    def _branch_filters(
        alias: str, name_expr: str, id_expr: str, kb_expr: str,
        status_expr: str, branch_type: str, *, common: list[str],
    ) -> str:
        parts = [
            fragment.format(
                alias=alias, name_col=name_expr,
                id_expr=id_expr, kb_expr=kb_expr,
                status_expr=status_expr,
            )
            for fragment in common
        ]
        # 只作用于文档分支：数据源没有归档状态。
        if branch_type == "document" and not include_archived:
            parts.append(f"AND {alias}.status <> 'archived'")
        if asset_type and asset_type != branch_type:
            parts.append("AND FALSE")
        return "\n      ".join(parts)

    # 生效值筛选：全部作用在外层 `rows` 的列上（继承关系已在 CTE 内解析）。
    meta: list[str] = []
    if domain:
        meta.append("AND domain = :domain")
        params["domain"] = domain
    if owner:
        meta.append("AND owner = :owner")
        params["owner"] = owner
    if sensitivity == "unclassified":
        meta.append("AND effective_level IS NULL")
    elif sensitivity:
        meta.append("AND effective_level = :sensitivity")
        params["sensitivity"] = sensitivity
    if category:
        # JSONB 数组包含：生效分类已合并资产/KB/租户覆盖。
        meta.append("AND jsonb_exists(effective_categories, :category)")
        params["category"] = category
    if max_rank is not None:
        # 用户密级上限：列表与计数共用（count 走同一构造器）。
        meta.append("AND effective_rank <= :max_rank")
        params["max_rank"] = max_rank

    column_id_expr = (
        "(cs.data_source_id || ':' || (e->>'source_object_id') || '.' || "
        "coalesce(nullif(a->>'source_attribute_id',''), a->>'name'))"
    )
    sql = _BASE.format(
        doc_filters=_branch_filters(
            "d", "d.file_name", "d.id", "d.knowledge_base_id", "d.status",
            "document", common=common,
        ),
        source_filters=_branch_filters(
            "s", "s.name", "s.id", "s.knowledge_base_id", "s.status",
            "data_source", common=common,
        ),
        table_filters=_branch_filters(
            "cs", "(e->>'source_object_id')",
            "(cs.data_source_id || ':' || (e->>'source_object_id'))",
            "cs.knowledge_base_id", "cs.sync_status", "table", common=common,
        ),
        column_filters=_branch_filters(
            "cs",
            "(coalesce(nullif(a->>'source_attribute_id',''), a->>'name'))",
            column_id_expr, "cs.knowledge_base_id", "cs.sync_status",
            "column", common=common,
        ),
        metric_filters=_branch_filters(
            "s", "(m->>'name')",
            "(s.knowledge_base_id || ':' || (m->>'name'))",
            "s.knowledge_base_id", "'active'", "metric", common=common,
        ),
        meta_filters="\n  ".join(meta),
        order_limit="" if count_only else _ORDER_LIMIT,
    )
    return sql, params


async def fetch_page(
    session: AsyncSession, sql: str, params: dict,
) -> list[dict]:
    rows = (await session.execute(text(sql), params)).mappings().all()
    return [dict(row) for row in rows]


async def count(session: AsyncSession, **filters) -> int:
    """与列表**同一段 WHERE** 的计数（同一构造器，不切割字符串）。"""
    sql, params = build_catalog_sql(count_only=True, **filters)
    total_sql = f"SELECT count(*) FROM (\n{sql}\n) AS c"
    return int((await session.execute(text(total_sql), params)).scalar() or 0)


async def fetch_detail_fields(
    session: AsyncSession, *, tenant_id: str, asset_type: str, asset_id: str,
) -> dict:
    """详情特有字段（列表不取）；未知类型返回空 dict。

    SQL 收在仓储层：service 不持有裸 SQL，也便于与列表共用同一 session。
    """
    sql = _DETAIL_SQL.get(asset_type)
    if sql is None:
        return {}
    row = (
        await session.execute(
            text(sql), {"asset_id": asset_id, "tenant_id": tenant_id},
        )
    ).mappings().first()
    return dict(row) if row else {}


async def fetch_table_columns(
    session: AsyncSession, *, tenant_id: str, data_source_id: str, table: str,
) -> list[dict]:
    """表的列清单（来自 active per-source compiled schema）。

    与列表/详情同一优先级：列名取 `source_attribute_id`，缺省回落到 `name`；
    主键标记优先读 `is_primary_key`（编译器归一后的键），旧数据回落
    `primary_key`。`DISTINCT` + 按 name 去重，保证详情 `column_count`
    与清单长度同源一致。
    """
    sql = """
        SELECT DISTINCT
               coalesce(nullif(a->>'source_attribute_id',''), a->>'name') AS name,
               a->>'type' AS type,
               coalesce(
                   (a->>'is_primary_key')::boolean,
                   (a->>'primary_key')::boolean,
                   false
               ) AS primary_key,
               a->>'display_name' AS display_name
        FROM knowledge_base.ontology_compiled_schemas cs
        CROSS JOIN LATERAL jsonb_array_elements(
            coalesce(cs.entity_types, '[]'::jsonb)
        ) e
        CROSS JOIN LATERAL jsonb_array_elements(
            coalesce(e->'attributes', '[]'::jsonb)
        ) a
        WHERE cs.tenant_id = :tenant_id AND cs.status = 'active'
          AND cs.data_source_id = :data_source_id
          AND e->>'source_object_id' = :table
        ORDER BY name
    """
    rows = (
        await session.execute(text(sql), {
            "tenant_id": tenant_id,
            "data_source_id": data_source_id,
            "table": table,
        })
    ).mappings().all()
    columns: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        name = row.get("name")
        if not name or name in seen:
            continue
        seen.add(name)
        columns.append(dict(row))
    return columns


def normalize_metric_landings(raw) -> list[dict]:
    """指标落点统一形状（详情与血缘共用，任务 5 复用本函数）。

    固定四键：`data_source_id/entity_name/di_metric/table`；缺失通道补空串
    （前端列可直接渲染），`table` 缺省 `None`，非对象项静默丢弃。
    """
    out: list[dict] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        out.append({
            "data_source_id": str(item.get("data_source_id") or "").strip(),
            "entity_name": str(item.get("entity_name") or "").strip(),
            "di_metric": str(item.get("di_metric") or "").strip(),
            "table": item.get("table") or None,
        })
    return out


async def resolve_landing_tables(
    session: AsyncSession, *, tenant_id: str, landings: list[dict],
) -> list[dict]:
    """metric landings → 物理表（`data_source_id + entity_name` 查 active
    per-source compiled schema；找不到时 `table=None`）。"""
    out: list[dict] = []
    for landing in landings or []:
        data_source_id = str(landing.get("data_source_id") or "")
        entity_name = str(landing.get("entity_name") or "")
        table = None
        if data_source_id and entity_name:
            row = (
                await session.execute(text("""
                    SELECT e->>'source_object_id' AS table
                    FROM knowledge_base.ontology_compiled_schemas cs
                    CROSS JOIN LATERAL jsonb_array_elements(
                        coalesce(cs.entity_types, '[]'::jsonb)
                    ) e
                    WHERE cs.tenant_id = :tenant_id AND cs.status = 'active'
                      AND cs.data_source_id = :data_source_id
                      AND e->>'name' = :entity_name
                    LIMIT 1
                """), {
                    "tenant_id": tenant_id,
                    "data_source_id": data_source_id,
                    "entity_name": entity_name,
                })
            ).mappings().first()
            raw_table = row["table"] if row else None
            # 空白表名等同未解析（前端据此显示「未解析到物理表」）。
            table = str(raw_table).strip() if raw_table is not None else ""
            table = table or None
        out.append({**landing, "table": table})
    return out
