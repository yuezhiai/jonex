"""quality_rules 仓储（W2.5）：列表筛选、乐观锁更新、软删、合同导入去重。

列表与 count 共用同一份 WHERE；空值参数显式 CAST（PostgreSQL 无法从
`$n IS NULL OR col=$n` 推断类型，W2.2/W2.3 真机踩过）。
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text

_COLUMNS = (
    "id, tenant_id, name, asset_type, asset_id, data_source_id, kb_id, "
    "dimension, severity, check_type, definition, threshold, enabled, "
    "contract_id, created_by, updated_by, revision, is_deleted, "
    "created_at, updated_at"
)

_LIST_WHERE = """
    WHERE tenant_id = :tenant_id AND is_deleted = 0
      AND (CAST(:asset_type AS VARCHAR) IS NULL
           OR asset_type = CAST(:asset_type AS VARCHAR))
      AND (CAST(:dimension AS VARCHAR) IS NULL
           OR dimension = CAST(:dimension AS VARCHAR))
      AND (CAST(:severity AS VARCHAR) IS NULL
           OR severity = CAST(:severity AS VARCHAR))
      AND (CAST(:check_type AS VARCHAR) IS NULL
           OR check_type = CAST(:check_type AS VARCHAR))
      AND (CAST(:data_source_id AS VARCHAR) IS NULL
           OR data_source_id = CAST(:data_source_id AS VARCHAR))
      AND (CAST(:asset_id AS VARCHAR) IS NULL
           OR asset_id = CAST(:asset_id AS VARCHAR))
      AND (CAST(:contract_id AS VARCHAR) IS NULL
           OR contract_id = CAST(:contract_id AS VARCHAR))
      AND (CAST(:enabled AS INTEGER) IS NULL
           OR enabled = CAST(:enabled AS INTEGER))
      AND (CAST(:keyword AS VARCHAR) IS NULL
           OR name ILIKE CAST(:keyword AS VARCHAR) ESCAPE '\\'
           OR asset_id ILIKE CAST(:keyword AS VARCHAR) ESCAPE '\\')
"""

_LIST_SQL = (
    f"SELECT {_COLUMNS} FROM data_governance.quality_rules"
    + _LIST_WHERE
    + " ORDER BY updated_at DESC, id LIMIT :limit OFFSET :offset"
)
_COUNT_SQL = f"SELECT count(*) FROM data_governance.quality_rules{_LIST_WHERE}"

_GET_SQL = (
    f"SELECT {_COLUMNS} FROM data_governance.quality_rules "
    "WHERE tenant_id=:tenant_id AND id=:rule_id AND is_deleted=0"
)

_INSERT_SQL = """
    INSERT INTO data_governance.quality_rules (
        id, tenant_id, name, asset_type, asset_id, data_source_id, kb_id,
        dimension, severity, check_type, definition, threshold, enabled,
        contract_id, created_by, updated_by, revision, is_deleted
    ) VALUES (
        :id, :tenant_id, :name, :asset_type, :asset_id, :data_source_id, :kb_id,
        :dimension, :severity, :check_type, CAST(:definition AS JSONB),
        CAST(:threshold AS JSONB), :enabled, :contract_id, :created_by,
        :updated_by, 1, 0
    )
    RETURNING """ + _COLUMNS

_DELETE_SQL = """
    UPDATE data_governance.quality_rules
       SET is_deleted = 1, revision = revision + 1, updated_at = now(),
           updated_by = :user_id
     WHERE tenant_id=:tenant_id AND id=:rule_id AND is_deleted=0
"""

_EXISTS_IMPORTED_SQL = """
    SELECT 1 FROM data_governance.quality_rules
     WHERE tenant_id=:tenant_id AND contract_id=:contract_id
       AND name=:name AND asset_id=:asset_id AND is_deleted=0
     LIMIT 1
"""

_UPDATABLE = frozenset({
    "name", "asset_type", "asset_id", "data_source_id", "kb_id", "dimension",
    "severity", "check_type", "definition", "threshold", "enabled",
})
_JSON_FIELDS = frozenset({"definition", "threshold"})


def _json_obj(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if value is not None else {}


def to_dict(row) -> dict:
    """Mapping / dict → 行字典；JSONB 列为字符串时解析。"""
    data = dict(row)
    for field in _JSON_FIELDS:
        data[field] = _json_obj(data.get(field))
    data["enabled"] = int(data.get("enabled") or 0)
    return data


def _params(tenant_id: str, filters: dict | None, **extra) -> dict:
    filters = filters or {}
    params = {
        "tenant_id": tenant_id,
        "asset_type": filters.get("asset_type") or None,
        "dimension": filters.get("dimension") or None,
        "severity": filters.get("severity") or None,
        "check_type": filters.get("check_type") or None,
        "data_source_id": filters.get("data_source_id") or None,
        "asset_id": filters.get("asset_id") or None,
        "contract_id": filters.get("contract_id") or None,
        "enabled": filters.get("enabled"),
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


async def get_by_id(session, *, tenant_id: str, rule_id: str) -> dict | None:
    result = await session.execute(
        text(_GET_SQL), {"tenant_id": tenant_id, "rule_id": rule_id},
    )
    row = result.mappings().first()
    return to_dict(row) if row else None


async def create_rule(session, *, tenant_id: str, **values) -> dict:
    params = {
        "id": uuid.uuid4().hex,
        "tenant_id": tenant_id,
        "name": values["name"],
        "asset_type": values["asset_type"],
        "asset_id": values["asset_id"],
        "data_source_id": values["data_source_id"],
        "kb_id": values.get("kb_id"),
        "dimension": values["dimension"],
        "severity": values["severity"],
        "check_type": values["check_type"],
        "definition": json.dumps(values.get("definition") or {}, ensure_ascii=False),
        "threshold": json.dumps(values.get("threshold") or {}, ensure_ascii=False),
        "enabled": int(values.get("enabled") or 0),
        "contract_id": values.get("contract_id"),
        "created_by": values.get("created_by"),
        "updated_by": values.get("updated_by"),
    }
    result = await session.execute(text(_INSERT_SQL), params)
    return to_dict(result.mappings().first())


async def update_rule(
    session, *, tenant_id: str, rule_id: str, expected_revision: int,
    user_id, fields: dict,
) -> int:
    """乐观锁更新；返回 rowcount（0 = 不存在或 revision 失配）。"""
    assignments: list[str] = []
    params: dict[str, Any] = {
        "tenant_id": tenant_id,
        "rule_id": rule_id,
        "revision": expected_revision,
        "updated_by": user_id,
    }
    for key, value in fields.items():
        if key not in _UPDATABLE:
            continue
        if key in _JSON_FIELDS:
            assignments.append(f"{key} = CAST(:{key} AS JSONB)")
            params[key] = json.dumps(value or {}, ensure_ascii=False)
        elif key == "enabled":
            assignments.append("enabled = :enabled")
            params[key] = 1 if value else 0
        else:
            assignments.append(f"{key} = :{key}")
            params[key] = value
    if not assignments:
        return 0
    sql = (
        "UPDATE data_governance.quality_rules SET "
        + ", ".join(assignments)
        + ", revision = revision + 1, updated_at = now(), updated_by = :updated_by "
        "WHERE tenant_id=:tenant_id AND id=:rule_id AND is_deleted=0 "
        "AND revision=:revision"
    )
    result = await session.execute(text(sql), params)
    return int(result.rowcount or 0)


async def soft_delete(session, *, tenant_id: str, rule_id: str, user_id) -> int:
    result = await session.execute(
        text(_DELETE_SQL),
        {"tenant_id": tenant_id, "rule_id": rule_id, "user_id": user_id},
    )
    return int(result.rowcount or 0)


async def list_for_run(
    session, *, tenant_id: str, data_source_id: str,
    rule_ids: list[str] | None = None,
) -> list[dict]:
    """巡检用规则集：指定 id（必须属于该源）或缺省取全部 enabled。"""
    params: dict[str, Any] = {
        "tenant_id": tenant_id, "data_source_id": data_source_id,
    }
    if rule_ids is None:
        condition = "enabled = 1"
    else:
        if not rule_ids:
            return []
        placeholders = ", ".join(f":r{index}" for index in range(len(rule_ids)))
        params.update({f"r{index}": value for index, value in enumerate(rule_ids)})
        condition = f"id IN ({placeholders})"
    sql = (
        f"SELECT {_COLUMNS} FROM data_governance.quality_rules "
        "WHERE tenant_id=:tenant_id AND data_source_id=:data_source_id "
        f"AND is_deleted=0 AND {condition} ORDER BY name, id"
    )
    result = await session.execute(text(sql), params)
    return [to_dict(row) for row in result.mappings().all()]


async def exists_imported(
    session, *, tenant_id: str, contract_id: str, name: str, asset_id: str,
) -> bool:
    result = await session.execute(
        text(_EXISTS_IMPORTED_SQL),
        {
            "tenant_id": tenant_id, "contract_id": contract_id,
            "name": name, "asset_id": asset_id,
        },
    )
    return result.scalar() is not None
