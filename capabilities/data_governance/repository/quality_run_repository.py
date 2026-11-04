"""quality_runs / quality_results 仓储（W2.5）。"""
from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from jonex_core.common.exceptions import ResourceConflictError

_RUN_COLUMNS = (
    "id, tenant_id, data_source_id, trigger, status, rule_ids, executor, "
    "dialect, total, passed, failed, error, message, started_at, finished_at, "
    "created_by, created_at, updated_at"
)
# 列表只回轻量列；详情（get_run）额外带规则快照。
_RUN_DETAIL_COLUMNS = f"{_RUN_COLUMNS}, rule_snapshot"
_RESULT_COLUMNS = (
    "id, tenant_id, run_id, rule_id, asset_type, asset_id, status, "
    "metric_value, message, sample, checked_at"
)

_LIST_WHERE = """
    WHERE tenant_id = :tenant_id
      AND (CAST(:data_source_id AS VARCHAR) IS NULL
           OR data_source_id = CAST(:data_source_id AS VARCHAR))
      AND (CAST(:status AS VARCHAR) IS NULL
           OR status = CAST(:status AS VARCHAR))
"""
_LIST_SQL = (
    f"SELECT {_RUN_COLUMNS} FROM data_governance.quality_runs"
    + _LIST_WHERE
    + " ORDER BY created_at DESC, id LIMIT :limit OFFSET :offset"
)
_COUNT_SQL = f"SELECT count(*) FROM data_governance.quality_runs{_LIST_WHERE}"

_GET_RUN_SQL = (
    f"SELECT {_RUN_DETAIL_COLUMNS} FROM data_governance.quality_runs "
    "WHERE tenant_id=:tenant_id AND id=:run_id"
)

_INSERT_RUN_SQL = """
    INSERT INTO data_governance.quality_runs (
        id, tenant_id, data_source_id, trigger, status, rule_ids,
        rule_snapshot, executor, dialect, total, passed, failed, error,
        started_at, created_by
    ) VALUES (
        :id, :tenant_id, :data_source_id, :trigger, 'running',
        CAST(:rule_ids AS JSONB), CAST(:rule_snapshot AS JSONB), :executor,
        :dialect, 0, 0, 0, 0, now(), :created_by
    )
    RETURNING """ + _RUN_DETAIL_COLUMNS

_INSERT_RESULT_SQL = """
    INSERT INTO data_governance.quality_results (
        id, tenant_id, run_id, rule_id, asset_type, asset_id, status,
        metric_value, message, sample
    ) VALUES (
        :id, :tenant_id, :run_id, :rule_id, :asset_type, :asset_id, :status,
        CAST(:metric_value AS JSONB), :message, CAST(:sample AS JSONB)
    )
"""

_LIST_RESULTS_SQL = (
    f"SELECT {_RESULT_COLUMNS} FROM data_governance.quality_results "
    "WHERE tenant_id=:tenant_id AND run_id=:run_id "
    "ORDER BY checked_at, id"
)

_COMPLETE_SQL = """
    UPDATE data_governance.quality_runs
       SET status='completed', total=:total, passed=:passed, failed=:failed,
           error=:error, message=:message,
           executor=COALESCE(:executor, executor),
           finished_at=now(), updated_at=now()
     WHERE tenant_id=:tenant_id AND id=:run_id AND status='running'
"""
_FAIL_SQL = """
    UPDATE data_governance.quality_runs
       SET status='failed', message=:message, finished_at=now(), updated_at=now()
     WHERE tenant_id=:tenant_id AND id=:run_id AND status='running'
"""


def _json_obj(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value
    return value


def to_run(row) -> dict:
    data = dict(row)
    data["rule_ids"] = _json_obj(data.get("rule_ids")) or []
    if "rule_snapshot" in data:
        data["rule_snapshot"] = _json_obj(data.get("rule_snapshot")) or []
    return data


def to_result(row) -> dict:
    data = dict(row)
    for field in ("metric_value", "sample"):
        data[field] = _json_obj(data.get(field))
    return data


def _list_params(tenant_id: str, filters: dict | None, **extra) -> dict:
    filters = filters or {}
    return {
        "tenant_id": tenant_id,
        "data_source_id": filters.get("data_source_id") or None,
        "status": filters.get("status") or None,
        **extra,
    }


async def create_run(session, *, tenant_id: str, **values) -> dict:
    params = {
        "id": uuid.uuid4().hex,
        "tenant_id": tenant_id,
        "data_source_id": values["data_source_id"],
        "trigger": values.get("trigger") or "manual",
        "rule_ids": json.dumps(values.get("rule_ids") or [], ensure_ascii=False),
        "rule_snapshot": json.dumps(
            values.get("rule_snapshot") or [], ensure_ascii=False,
        ),
        "executor": values.get("executor"),
        "dialect": values.get("dialect") or "tsql",
        "created_by": values.get("created_by"),
    }
    result = await session.execute(text(_INSERT_RUN_SQL), params)
    return to_run(result.mappings().first())


async def get_run(
    session, *, tenant_id: str, run_id: str, for_update: bool = False,
) -> dict | None:
    sql = _GET_RUN_SQL + (" FOR UPDATE" if for_update else "")
    result = await session.execute(
        text(sql), {"tenant_id": tenant_id, "run_id": run_id},
    )
    row = result.mappings().first()
    return to_run(row) if row else None


async def list_runs(
    session, *, tenant_id: str, page: int, page_size: int,
    filters: dict | None = None,
) -> list[dict]:
    result = await session.execute(
        text(_LIST_SQL),
        _list_params(
            tenant_id, filters, limit=page_size, offset=(page - 1) * page_size,
        ),
    )
    return [to_run(row) for row in result.mappings().all()]


async def count_runs(session, *, tenant_id: str, filters: dict | None = None) -> int:
    result = await session.execute(
        text(_COUNT_SQL), _list_params(tenant_id, filters),
    )
    return int(result.scalar() or 0)


async def insert_results(
    session, *, tenant_id: str, run_id: str, results: list[dict],
) -> None:
    for item in results:
        try:
            await session.execute(
                text(_INSERT_RESULT_SQL),
                {
                    "id": uuid.uuid4().hex,
                    "tenant_id": tenant_id,
                    "run_id": run_id,
                    "rule_id": item["rule_id"],
                    "asset_type": item["asset_type"],
                    "asset_id": item["asset_id"],
                    "status": item["status"],
                    "metric_value": json.dumps(
                        item.get("metric_value"), ensure_ascii=False,
                    ),
                    "message": item.get("message"),
                    "sample": json.dumps(item.get("sample"), ensure_ascii=False),
                },
            )
        except IntegrityError as exc:
            # uq_qres_run_rule：同一运行同一规则只允许一条结果。
            await session.rollback()
            raise ResourceConflictError(
                message="已有结果，不能重复回传",
                details={"run_id": run_id, "rule_id": item["rule_id"]},
            ) from exc


async def list_results(session, *, tenant_id: str, run_id: str) -> list[dict]:
    result = await session.execute(
        text(_LIST_RESULTS_SQL), {"tenant_id": tenant_id, "run_id": run_id},
    )
    return [to_result(row) for row in result.mappings().all()]


async def mark_completed(
    session, *, tenant_id: str, run_id: str, counts: dict, message=None,
    executor=None,
) -> int:
    result = await session.execute(
        text(_COMPLETE_SQL),
        {
            "tenant_id": tenant_id, "run_id": run_id,
            "total": counts.get("total", 0),
            "passed": counts.get("passed", 0),
            "failed": counts.get("failed", 0),
            "error": counts.get("error", 0),
            "message": message,
            "executor": executor,
        },
    )
    return int(result.rowcount or 0)


async def mark_failed(session, *, tenant_id: str, run_id: str, message=None) -> int:
    result = await session.execute(
        text(_FAIL_SQL),
        {"tenant_id": tenant_id, "run_id": run_id, "message": message},
    )
    return int(result.rowcount or 0)
