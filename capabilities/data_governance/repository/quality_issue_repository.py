"""quality_issues / quality_issue_events 仓储（W2.5）。

- 未关闭问题由部分唯一索引 ``uq_quality_issue_open`` 兜底；
  ``find_open_for_update`` 在结果回传时锁行，避免并发开出两条。
- 所有状态写走 ``revision`` 乐观锁；事件表只追加。
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text

_COLUMNS = (
    "id, tenant_id, rule_id, asset_type, asset_id, data_source_id, severity, "
    "status, assignee_id, title, detail, first_seen_at, last_seen_at, "
    "resolved_at, revision, created_by, updated_by, is_deleted, "
    "created_at, updated_at"
)

_LIST_WHERE = """
    WHERE tenant_id = :tenant_id AND is_deleted = 0
      AND (CAST(:status AS VARCHAR) IS NULL
           OR status = CAST(:status AS VARCHAR))
      AND (CAST(:severity AS VARCHAR) IS NULL
           OR severity = CAST(:severity AS VARCHAR))
      AND (CAST(:assignee_id AS VARCHAR) IS NULL
           OR assignee_id = CAST(:assignee_id AS VARCHAR))
      AND (CAST(:data_source_id AS VARCHAR) IS NULL
           OR data_source_id = CAST(:data_source_id AS VARCHAR))
      AND (CAST(:asset_type AS VARCHAR) IS NULL
           OR asset_type = CAST(:asset_type AS VARCHAR))
      AND (CAST(:asset_id AS VARCHAR) IS NULL
           OR asset_id = CAST(:asset_id AS VARCHAR))
      AND (CAST(:keyword AS VARCHAR) IS NULL
           OR title ILIKE CAST(:keyword AS VARCHAR) ESCAPE '\\'
           OR asset_id ILIKE CAST(:keyword AS VARCHAR) ESCAPE '\\')
"""

_LIST_SQL = (
    f"SELECT {_COLUMNS} FROM data_governance.quality_issues"
    + _LIST_WHERE
    + " ORDER BY updated_at DESC, id LIMIT :limit OFFSET :offset"
)
_COUNT_SQL = f"SELECT count(*) FROM data_governance.quality_issues{_LIST_WHERE}"

_GET_SQL = (
    f"SELECT {_COLUMNS} FROM data_governance.quality_issues "
    "WHERE tenant_id=:tenant_id AND id=:issue_id AND is_deleted=0"
)

_FIND_OPEN_SQL = (
    f"SELECT {_COLUMNS} FROM data_governance.quality_issues "
    "WHERE tenant_id=:tenant_id AND rule_id=:rule_id AND asset_id=:asset_id "
    "AND status <> 'closed' AND is_deleted=0 FOR UPDATE"
)

_INSERT_SQL = """
    INSERT INTO data_governance.quality_issues (
        id, tenant_id, rule_id, asset_type, asset_id, data_source_id, severity,
        status, assignee_id, title, detail, revision, created_by, updated_by,
        is_deleted
    ) VALUES (
        :id, :tenant_id, :rule_id, :asset_type, :asset_id, :data_source_id,
        :severity, :status, :assignee_id, :title, :detail, 1, :created_by,
        :updated_by, 0
    )
    RETURNING """ + _COLUMNS

_EVENT_COLUMNS = (
    "id, tenant_id, issue_id, event_type, from_status, to_status, actor_id, "
    "note, created_at"
)
_INSERT_EVENT_SQL = """
    INSERT INTO data_governance.quality_issue_events (
        id, tenant_id, issue_id, event_type, from_status, to_status, actor_id, note
    ) VALUES (
        :id, :tenant_id, :issue_id, :event_type, :from_status, :to_status,
        :actor_id, :note
    )
    RETURNING """ + _EVENT_COLUMNS
_LIST_EVENTS_SQL = (
    f"SELECT {_EVENT_COLUMNS} FROM data_governance.quality_issue_events "
    "WHERE tenant_id=:tenant_id AND issue_id=:issue_id "
    "ORDER BY created_at, id"
)

#: 摘要：每个数据源一行；规则计数、最近一次运行、未关闭问题按严重度。
_SUMMARY_SQL = """
    SELECT ds.id AS data_source_id, ds.name,
           COALESCE(r.total_rules, 0) AS total_rules,
           COALESCE(r.enabled_rules, 0) AS enabled_rules,
           lr.status AS latest_run_status, lr.finished_at AS latest_run_at,
           lr.passed AS passed, lr.failed AS failed, lr.error AS error,
           COALESCE(i.critical, 0) AS critical,
           COALESCE(i.error, 0) AS error_count,
           COALESCE(i.warning, 0) AS warning,
           COALESCE(i.info, 0) AS info
    FROM knowledge_base.knowledge_data_sources ds
    LEFT JOIN (
        SELECT data_source_id,
               count(*) AS total_rules,
               count(*) FILTER (WHERE enabled = 1) AS enabled_rules
        FROM data_governance.quality_rules
        WHERE tenant_id = :tenant_id AND is_deleted = 0
        GROUP BY data_source_id
    ) r ON r.data_source_id = ds.id
    LEFT JOIN LATERAL (
        SELECT status, finished_at, passed, failed, error
        FROM data_governance.quality_runs
        WHERE tenant_id = :tenant_id AND data_source_id = ds.id
        ORDER BY created_at DESC
        LIMIT 1
    ) lr ON TRUE
    LEFT JOIN (
        SELECT data_source_id,
               count(*) FILTER (WHERE severity = 'critical') AS critical,
               count(*) FILTER (WHERE severity = 'error') AS error,
               count(*) FILTER (WHERE severity = 'warning') AS warning,
               count(*) FILTER (WHERE severity = 'info') AS info
        FROM data_governance.quality_issues
        WHERE tenant_id = :tenant_id AND is_deleted = 0 AND status <> 'closed'
        GROUP BY data_source_id
    ) i ON i.data_source_id = ds.id
    WHERE ds.tenant_id = :tenant_id AND ds.is_deleted = 0
      AND (CAST(:data_source_id AS VARCHAR) IS NULL
           OR ds.id = CAST(:data_source_id AS VARCHAR))
    ORDER BY ds.name, ds.id
"""

_UPDATABLE = frozenset({
    "severity", "status", "assignee_id", "title", "detail",
    "last_seen_at", "resolved_at", "updated_by",
})


def to_dict(row) -> dict:
    return dict(row)


def _params(tenant_id: str, filters: dict | None, **extra) -> dict:
    filters = filters or {}
    params = {
        "tenant_id": tenant_id,
        "status": filters.get("status") or None,
        "severity": filters.get("severity") or None,
        "assignee_id": filters.get("assignee_id") or None,
        "data_source_id": filters.get("data_source_id") or None,
        "asset_type": filters.get("asset_type") or None,
        "asset_id": filters.get("asset_id") or None,
        "keyword": f"%{filters['keyword']}%" if filters.get("keyword") else None,
    }
    params.update(extra)
    return params


async def list_page(
    session, *, tenant_id: str, page: int, page_size: int,
    filters: dict | None = None,
) -> list[dict]:
    result = await session.execute(
        text(_LIST_SQL),
        _params(tenant_id, filters, limit=page_size, offset=(page - 1) * page_size),
    )
    return [to_dict(row) for row in result.mappings().all()]


async def count(session, *, tenant_id: str, filters: dict | None = None) -> int:
    result = await session.execute(text(_COUNT_SQL), _params(tenant_id, filters))
    return int(result.scalar() or 0)


async def get_by_id(session, *, tenant_id: str, issue_id: str) -> dict | None:
    result = await session.execute(
        text(_GET_SQL), {"tenant_id": tenant_id, "issue_id": issue_id},
    )
    row = result.mappings().first()
    return to_dict(row) if row else None


async def find_open_for_update(
    session, *, tenant_id: str, rule_id: str, asset_id: str,
) -> dict | None:
    result = await session.execute(
        text(_FIND_OPEN_SQL),
        {"tenant_id": tenant_id, "rule_id": rule_id, "asset_id": asset_id},
    )
    row = result.mappings().first()
    return to_dict(row) if row else None


async def create_issue(session, *, tenant_id: str, **values) -> dict:
    params = {
        "id": uuid.uuid4().hex,
        "tenant_id": tenant_id,
        "rule_id": values["rule_id"],
        "asset_type": values["asset_type"],
        "asset_id": values["asset_id"],
        "data_source_id": values["data_source_id"],
        "severity": values.get("severity") or "warning",
        "status": values.get("status") or "open",
        "assignee_id": values.get("assignee_id"),
        "title": values["title"],
        "detail": values.get("detail"),
        "created_by": values.get("created_by"),
        "updated_by": values.get("updated_by"),
    }
    result = await session.execute(text(_INSERT_SQL), params)
    return to_dict(result.mappings().first())


async def update_issue(
    session, *, tenant_id: str, issue_id: str, expected_revision: int,
    fields: dict,
) -> int:
    assignments: list[str] = []
    params: dict[str, Any] = {
        "tenant_id": tenant_id, "issue_id": issue_id,
        "revision": expected_revision,
    }
    for key, value in fields.items():
        if key not in _UPDATABLE:
            continue
        assignments.append(f"{key} = :{key}")
        params[key] = value
    if not assignments:
        return 0
    sql = (
        "UPDATE data_governance.quality_issues SET "
        + ", ".join(assignments)
        + ", revision = revision + 1, updated_at = now() "
        "WHERE tenant_id=:tenant_id AND id=:issue_id AND is_deleted=0 "
        "AND revision=:revision"
    )
    result = await session.execute(text(sql), params)
    return int(result.rowcount or 0)


async def append_event(
    session, *, tenant_id: str, issue_id: str, event_type: str,
    from_status: str | None = None, to_status: str | None = None,
    actor_id=None, note: str | None = None,
) -> dict:
    result = await session.execute(
        text(_INSERT_EVENT_SQL),
        {
            "id": uuid.uuid4().hex,
            "tenant_id": tenant_id,
            "issue_id": issue_id,
            "event_type": event_type,
            "from_status": from_status,
            "to_status": to_status,
            "actor_id": actor_id,
            "note": note,
        },
    )
    return to_dict(result.mappings().first())


async def list_events(session, *, tenant_id: str, issue_id: str) -> list[dict]:
    result = await session.execute(
        text(_LIST_EVENTS_SQL),
        {"tenant_id": tenant_id, "issue_id": issue_id},
    )
    return [to_dict(row) for row in result.mappings().all()]


async def summary_rows(
    session, *, tenant_id: str, data_source_id: str | None = None,
) -> list[dict]:
    result = await session.execute(
        text(_SUMMARY_SQL),
        {"tenant_id": tenant_id, "data_source_id": data_source_id},
    )
    return [dict(row) for row in result.mappings().all()]
