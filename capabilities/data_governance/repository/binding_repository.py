"""W2.6 落标绑定仓储：CRUD、按标准批量读取与覆盖率聚合。

- 绑定以 ``(standard_kind, standard_code)`` 表达标准身份，不绑单版；
- 同一 ``(tenant, kind, code, asset_type, asset_id)`` 至多一条 active，
  由部分唯一索引 ``uq_sab_active`` 兜底（冲突由服务层映射 1003）；
- 覆盖率以 ``standard_assets`` 的 published 行（每个 ``(kind, code)`` 一条）
  为左表，只左连 active 绑定；草稿/退役不参与覆盖率；
- 所有 SQL 都带 ``tenant_id``，空值过滤显式 CAST（PostgreSQL 无法从
  ``$n IS NULL OR col = $n`` 推断参数类型，W2.2/W2.5 真机踩过）。
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text

_BINDING_COLUMNS = (
    "id, tenant_id, standard_kind, standard_code, standard_asset_id, "
    "asset_type, asset_id, source, confidence, note, status, "
    "created_by, updated_by, revision, is_deleted, created_at, updated_at"
)

#: 列表与 count 共用的 WHERE（count 必须与列表逐字一致）。
_LIST_WHERE = """
    WHERE tenant_id = :tenant_id AND is_deleted = 0
      AND (CAST(:standard_kind AS VARCHAR) IS NULL
           OR standard_kind = CAST(:standard_kind AS VARCHAR))
      AND (CAST(:standard_code AS VARCHAR) IS NULL
           OR standard_code = CAST(:standard_code AS VARCHAR))
      AND (CAST(:asset_type AS VARCHAR) IS NULL
           OR asset_type = CAST(:asset_type AS VARCHAR))
      AND (CAST(:asset_id AS VARCHAR) IS NULL
           OR asset_id = CAST(:asset_id AS VARCHAR))
      AND (CAST(:status AS VARCHAR) IS NULL
           OR status = CAST(:status AS VARCHAR))
"""

_LIST_SQL = (
    f"SELECT {_BINDING_COLUMNS} FROM data_governance.standard_asset_bindings"
    + _LIST_WHERE
    + " ORDER BY updated_at DESC, id LIMIT :limit OFFSET :offset"
)
_COUNT_SQL = (
    f"SELECT count(*) FROM data_governance.standard_asset_bindings{_LIST_WHERE}"
)

_GET_SQL = (
    f"SELECT {_BINDING_COLUMNS} FROM data_governance.standard_asset_bindings "
    "WHERE tenant_id=:tenant_id AND id=:binding_id AND is_deleted=0"
)

_INSERT_SQL = """
    INSERT INTO data_governance.standard_asset_bindings (
        id, tenant_id, standard_kind, standard_code, standard_asset_id,
        asset_type, asset_id, source, confidence, note, status,
        created_by, updated_by, revision, is_deleted
    ) VALUES (
        :id, :tenant_id, :standard_kind, :standard_code, :standard_asset_id,
        :asset_type, :asset_id, :source, :confidence, :note, 'active',
        :created_by, :updated_by, 1, 0
    )
    RETURNING """ + _BINDING_COLUMNS

_DELETE_SQL = """
    UPDATE data_governance.standard_asset_bindings
       SET is_deleted = 1, revision = revision + 1, updated_at = now(),
           updated_by = :user_id
     WHERE tenant_id=:tenant_id AND id=:binding_id AND is_deleted=0
"""

#: 采信标准 code 当前代表行的读法：published 优先，其次 draft，最后 retired；
#: 同状态取最大版本。create 只要求 code 存在（任意未删版本）。
_STANDARD_ROW_SQL = """
    SELECT id, kind, code, name, status, version
    FROM data_governance.standard_assets
    WHERE tenant_id=:tenant_id AND kind=:kind AND code=:code AND is_deleted=0
    ORDER BY CASE status
                 WHEN 'published' THEN 0
                 WHEN 'draft' THEN 1
                 ELSE 2
             END,
             version DESC
    LIMIT 1
"""

#: 建议候选的标准侧：只取 published（草稿/退役不可落标）。
_PUBLISHED_STANDARD_SQL = """
    SELECT DISTINCT ON (kind, code)
           id, kind, code, name, payload
    FROM data_governance.standard_assets
    WHERE tenant_id=:tenant_id AND status='published' AND is_deleted=0
      AND (CAST(:kind AS VARCHAR) IS NULL OR kind = CAST(:kind AS VARCHAR))
      AND (CAST(:code AS VARCHAR) IS NULL OR code = CAST(:code AS VARCHAR))
    ORDER BY kind, code, version DESC
"""

#: 覆盖率基准：每个 (kind, code) 取当前 published 行（部分唯一索引保证至多一条）。
_COVERAGE_PUBLISHED = """
    WITH published AS (
        SELECT DISTINCT ON (kind, code)
               id, kind, code, name, version, payload
        FROM data_governance.standard_assets
        WHERE tenant_id = :tenant_id AND status = 'published' AND is_deleted = 0
          AND (CAST(:kind AS VARCHAR) IS NULL OR kind = CAST(:kind AS VARCHAR))
          AND (CAST(:code AS VARCHAR) IS NULL OR code = CAST(:code AS VARCHAR))
          AND (CAST(:domain AS VARCHAR) IS NULL
               OR payload->>'domain' = CAST(:domain AS VARCHAR))
        ORDER BY kind, code, version DESC
    )
"""

#: 每行绑定数 + 前 20 个绑定资产。两个 LATERAL 分开：count 统计全量，
#: jsonb_agg 只聚合最近 20 条（前 20 是展示口径，不改变 bindings_count）。
_COVERAGE_ROWS_SQL = _COVERAGE_PUBLISHED + """
    SELECT p.id AS standard_asset_id,
           p.kind AS standard_kind,
           p.code AS standard_code,
           p.name,
           p.version,
           p.payload->>'domain' AS domain,
           'published' AS status,
           COALESCE(binding_count.count, 0) AS bindings_count,
           COALESCE(bound_assets.items, '[]'::jsonb) AS bound_assets
    FROM published p
    LEFT JOIN LATERAL (
        SELECT count(*) AS count
        FROM data_governance.standard_asset_bindings b
        WHERE b.tenant_id = :tenant_id
          AND b.standard_kind = p.kind AND b.standard_code = p.code
          AND b.status = 'active' AND b.is_deleted = 0
    ) binding_count ON TRUE
    LEFT JOIN LATERAL (
        SELECT jsonb_agg(jsonb_build_object(
                   'binding_id', recent.id,
                   'asset_type', recent.asset_type,
                   'asset_id', recent.asset_id,
                   'source', recent.source,
                   'confidence', recent.confidence
               ) ORDER BY recent.created_at DESC, recent.id) AS items
        FROM (
            SELECT id, asset_type, asset_id, source, confidence, created_at
            FROM data_governance.standard_asset_bindings b
            WHERE b.tenant_id = :tenant_id
              AND b.standard_kind = p.kind AND b.standard_code = p.code
              AND b.status = 'active' AND b.is_deleted = 0
            ORDER BY b.created_at DESC, b.id
            LIMIT 20
        ) recent
    ) bound_assets ON TRUE
    ORDER BY p.kind, p.code
    LIMIT :limit OFFSET :offset
"""

_COVERAGE_COUNT_SQL = _COVERAGE_PUBLISHED + "SELECT count(*) FROM published"

_COVERAGE_SUMMARY_SQL = _COVERAGE_PUBLISHED + """
    SELECT count(*) AS total_standards,
           count(*) FILTER (
               WHERE COALESCE(binding_count.count, 0) > 0
           ) AS bound_standards,
           COALESCE(sum(COALESCE(binding_count.count, 0)), 0) AS binding_count
    FROM published p
    LEFT JOIN LATERAL (
        SELECT count(*) AS count
        FROM data_governance.standard_asset_bindings b
        WHERE b.tenant_id = :tenant_id
          AND b.standard_kind = p.kind AND b.standard_code = p.code
          AND b.status = 'active' AND b.is_deleted = 0
    ) binding_count ON TRUE
"""

#: update 只允许 note / status（身份与租户列不可借动态 SET 改写）。
_UPDATABLE = frozenset({"note", "status"})


def _iso(value):
    if value is None or isinstance(value, str):
        return value
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else value


def _json_list(value) -> list:
    """jsonb 列可能是 str（asyncpg 默认 codec）或 list，统一成 list。"""
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return []
    return value if isinstance(value, list) else []


def _confidence(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_dict(row) -> dict:
    """绑定行：confidence 转 float，时间转 isoformat。"""
    data = dict(row)
    data["confidence"] = _confidence(data.get("confidence"))
    data["created_at"] = _iso(data.get("created_at"))
    data["updated_at"] = _iso(data.get("updated_at"))
    data["revision"] = int(data.get("revision") or 1)
    data["is_deleted"] = int(data.get("is_deleted") or 0)
    return data


def to_coverage_row(row) -> dict:
    """覆盖率行：bound_assets 统一成 list，内部 confidence 转 float。"""
    data = dict(row)
    data["bindings_count"] = int(data.get("bindings_count") or 0)
    assets = []
    for item in _json_list(data.get("bound_assets")):
        if not isinstance(item, dict):
            continue
        assets.append({**item, "confidence": _confidence(item.get("confidence"))})
    data["bound_assets"] = assets
    data["version"] = int(data.get("version") or 0)
    return data


def _filter_params(tenant_id: str, filters: dict | None) -> dict:
    filters = filters or {}
    return {
        "tenant_id": tenant_id,
        "standard_kind": filters.get("standard_kind") or None,
        "standard_code": filters.get("standard_code") or None,
        "asset_type": filters.get("asset_type") or None,
        "asset_id": filters.get("asset_id") or None,
        "status": filters.get("status") or None,
    }


async def list_page(
    session, *, tenant_id: str, page: int, page_size: int,
    filters: dict | None = None,
) -> list[dict]:
    params = _filter_params(tenant_id, filters)
    params.update({"limit": page_size, "offset": (page - 1) * page_size})
    result = await session.execute(text(_LIST_SQL), params)
    return [to_dict(row) for row in result.mappings().all()]


async def count(
    session, *, tenant_id: str, filters: dict | None = None,
) -> int:
    result = await session.execute(text(_COUNT_SQL), _filter_params(tenant_id, filters))
    return int(result.scalar() or 0)


async def get_by_id(session, *, tenant_id: str, binding_id: str) -> dict | None:
    result = await session.execute(
        text(_GET_SQL), {"tenant_id": tenant_id, "binding_id": binding_id},
    )
    row = result.mappings().first()
    return to_dict(row) if row else None


async def create(
    session, *, tenant_id: str, standard_kind: str, standard_code: str,
    standard_asset_id: str | None, asset_type: str, asset_id: str,
    source: str = "manual", confidence: float = 1.0,
    note: str | None = None, created_by=None, updated_by=None,
) -> dict:
    params = {
        "id": uuid.uuid4().hex,
        "tenant_id": tenant_id,
        "standard_kind": standard_kind,
        "standard_code": standard_code,
        "standard_asset_id": standard_asset_id,
        "asset_type": asset_type,
        "asset_id": asset_id,
        "source": source,
        "confidence": confidence,
        "note": note,
        "created_by": created_by,
        "updated_by": updated_by,
    }
    result = await session.execute(text(_INSERT_SQL), params)
    return to_dict(result.mappings().first())


async def update(
    session, *, tenant_id: str, binding_id: str, expected_revision: int,
    user_id, fields: dict,
) -> int:
    """乐观锁更新；返回 rowcount（0 = 不存在 / 已删 / revision 失配）。"""
    assignments: list[str] = []
    params: dict[str, Any] = {
        "tenant_id": tenant_id,
        "binding_id": binding_id,
        "revision": expected_revision,
        "updated_by": user_id,
    }
    for key, value in (fields or {}).items():
        if key not in _UPDATABLE:
            continue
        assignments.append(f"{key} = :{key}")
        params[key] = value
    if not assignments:
        return 0
    sql = (
        "UPDATE data_governance.standard_asset_bindings SET "
        + ", ".join(assignments)
        + ", revision = revision + 1, updated_at = now(), "
        "updated_by = :updated_by "
        "WHERE tenant_id=:tenant_id AND id=:binding_id AND is_deleted=0 "
        "AND revision=:revision"
    )
    result = await session.execute(text(sql), params)
    return int(result.rowcount or 0)


async def soft_delete(
    session, *, tenant_id: str, binding_id: str, user_id,
) -> int:
    result = await session.execute(
        text(_DELETE_SQL),
        {"tenant_id": tenant_id, "binding_id": binding_id, "user_id": user_id},
    )
    return int(result.rowcount or 0)


async def list_for_codes(
    session, *, tenant_id: str, standard_kind: str, codes: list[str],
    status: str = "active",
) -> list[dict]:
    """按标准 code 批量取绑定（覆盖率/前端批量展示用）。空列表不查库。"""
    if not codes:
        return []
    placeholders = ", ".join(f":c{index}" for index in range(len(codes)))
    params: dict[str, Any] = {
        "tenant_id": tenant_id,
        "standard_kind": standard_kind,
        "status": status,
    }
    params.update({f"c{index}": code for index, code in enumerate(codes)})
    sql = (
        f"SELECT {_BINDING_COLUMNS} FROM data_governance.standard_asset_bindings "
        "WHERE tenant_id=:tenant_id AND standard_kind=:standard_kind "
        f"AND standard_code IN ({placeholders}) "
        "AND status=:status AND is_deleted=0 "
        "ORDER BY standard_code, asset_type, asset_id"
    )
    result = await session.execute(text(sql), params)
    return [to_dict(row) for row in result.mappings().all()]


async def standard_row_for_code(
    session, *, tenant_id: str, kind: str, code: str,
) -> dict | None:
    """code 的当前代表行（published 优先）；任一无删版本即视为存在。"""
    result = await session.execute(
        text(_STANDARD_ROW_SQL),
        {"tenant_id": tenant_id, "kind": kind, "code": code},
    )
    row = result.mappings().first()
    return dict(row) if row else None


async def published_standard_rows(
    session, *, tenant_id: str, kind: str | None = None,
    code: str | None = None,
) -> list[dict]:
    """建议候选的标准侧（每 code 一条 published）。"""
    result = await session.execute(
        text(_PUBLISHED_STANDARD_SQL),
        {"tenant_id": tenant_id, "kind": kind, "code": code},
    )
    return [dict(row) for row in result.mappings().all()]


def _coverage_params(
    tenant_id: str, *, kind=None, code=None, domain=None,
) -> dict:
    return {
        "tenant_id": tenant_id,
        "kind": kind or None,
        "code": code or None,
        "domain": domain or None,
    }


async def coverage_rows(
    session, *, tenant_id: str, kind: str | None = None,
    code: str | None = None, domain: str | None = None,
    page: int = 1, page_size: int = 20,
) -> list[dict]:
    result = await session.execute(
        text(_COVERAGE_ROWS_SQL),
        {
            **_coverage_params(tenant_id, kind=kind, code=code, domain=domain),
            "limit": page_size,
            "offset": (page - 1) * page_size,
        },
    )
    return [to_coverage_row(row) for row in result.mappings().all()]


async def coverage_count(
    session, *, tenant_id: str, kind: str | None = None,
    code: str | None = None, domain: str | None = None,
) -> int:
    result = await session.execute(
        text(_COVERAGE_COUNT_SQL),
        _coverage_params(tenant_id, kind=kind, code=code, domain=domain),
    )
    return int(result.scalar() or 0)


async def coverage_summary(
    session, *, tenant_id: str, kind: str | None = None,
    code: str | None = None, domain: str | None = None,
) -> dict:
    result = await session.execute(
        text(_COVERAGE_SUMMARY_SQL),
        _coverage_params(tenant_id, kind=kind, code=code, domain=domain),
    )
    row = result.mappings().first()
    if row is None:
        return {"total_standards": 0, "bound_standards": 0, "binding_count": 0}
    return {
        "total_standards": int(row.get("total_standards") or 0),
        "bound_standards": int(row.get("bound_standards") or 0),
        "binding_count": int(row.get("binding_count") or 0),
    }
