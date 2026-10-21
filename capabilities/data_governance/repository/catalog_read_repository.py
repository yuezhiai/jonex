"""目录实时读模型：文档 + 数据源 UNION，左连 asset_metadata。

无投影、无缓存：每次请求都从业务表实时读。所有用户值只进绑定参数，
SQL 字面量里不出现调用方输入（`keyword` 的 `%` 包裹在 Python 侧完成）。
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_BASE = """
WITH assets AS (
    SELECT 'document' AS asset_type, d.id AS asset_id, d.file_name AS name,
           d.knowledge_base_id AS kb_id, d.status AS status, d.updated_at
    FROM knowledge_base.knowledge_documents d
    JOIN knowledge_base.knowledge_info k
      ON k.id = d.knowledge_base_id AND k.tenant_id = d.tenant_id AND k.is_deleted = 0
    WHERE d.tenant_id = :tenant_id AND d.is_deleted = 0
      {doc_filters}
    UNION ALL
    SELECT 'data_source' AS asset_type, s.id AS asset_id, s.name AS name,
           s.knowledge_base_id AS kb_id, s.status AS status, s.updated_at
    FROM knowledge_base.knowledge_data_sources s
    JOIN knowledge_base.knowledge_info k
      ON k.id = s.knowledge_base_id AND k.tenant_id = s.tenant_id AND k.is_deleted = 0
    WHERE s.tenant_id = :tenant_id AND s.is_deleted = 0
      {source_filters}
),
rows AS (
    SELECT a.asset_type, a.asset_id, a.name, a.kb_id, a.status, a.updated_at,
           k.name AS kb_name, k.space_id,
           m.owner, m.domain,
           m.sensitivity AS asset_level,
           m.classification_categories AS asset_categories,
           kbd.level_code AS kb_level,
           kbd.categories AS kb_categories,
           td.level_code AS tenant_level,
           td.categories AS tenant_categories,
           COALESCE(m.sensitivity, kbd.level_code, td.level_code) AS effective_level,
           COALESCE(m.classification_categories, kbd.categories, td.categories,
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
    LEFT JOIN data_governance.classification_default kbd
      ON kbd.tenant_id = :tenant_id AND kbd.scope_type = 'kb'
     AND kbd.scope_id = a.kb_id
    LEFT JOIN data_governance.classification_default td
      ON td.tenant_id = :tenant_id AND td.scope_type = 'tenant'
     AND td.scope_id = ''
    LEFT JOIN data_governance.classification_level lvl
      ON lvl.tenant_id = :tenant_id
     AND lvl.code = COALESCE(m.sensitivity, kbd.level_code, td.level_code)
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

    # 两个 CTE 分支共用的筛选片段：{alias} / {name_col} 由各分支填空。
    common: list[str] = []
    if space_id:
        # 两个分支各有自己的 k 别名（所属 KB），同一条件对两分支都生效。
        common.append("AND k.space_id = :space_id")
        params["space_id"] = space_id
    if kb_id:
        common.append("AND {alias}.knowledge_base_id = :kb_id")
        params["kb_id"] = kb_id
    if kb_ids is not None:
        common.append("AND {alias}.knowledge_base_id = ANY(:kb_ids)")
        params["kb_ids"] = list(kb_ids)
    if keyword:
        # ESCAPE '\' 让用户输入的 % / _ 按字面量匹配，而不是通配符。
        common.append("AND {alias}.{name_col} ILIKE :keyword ESCAPE '\\'")
        params["keyword"] = f"%{_escape_like(str(keyword))}%"
    if status:
        common.append("AND {alias}.status = :status")
        params["status"] = status
    if asset_id:
        common.append("AND {alias}.id = :asset_id")
        params["asset_id"] = asset_id

    def _branch_filters(alias: str, name_col: str, branch_type: str) -> str:
        parts = [
            fragment.format(alias=alias, name_col=name_col)
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

    sql = _BASE.format(
        doc_filters=_branch_filters("d", "file_name", "document"),
        source_filters=_branch_filters("s", "name", "data_source"),
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
