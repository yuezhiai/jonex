"""W2.5 质量规则服务：CRUD + 合同导入。

- 写操作仅租户管理员；读任意登录用户。
- 规则绑定 W2.1 资产 ID（`{ds}:{table}` / `{ds}:{table}.{col}`）。
- 合同导入只读已发布/草稿合同，规则默认 `enabled=0`，重复导入按
  `(contract_id, name, asset_id)` 跳过。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text

from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import (
    InvalidParameterError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from jonex_core.common.tenant import require_tenant

from ..repository import quality_rule_repository as repo
from .contracts.scaffold import (
    attribute_name,
    entity_name,
    load_active_schema,
)
from .quality.common import (
    audit,
    paging,
    require_admin,
    require_login,
)
from .quality.sqlgen import DIALECTS
from .quality.validation import (
    CHECK_TYPES,
    DIMENSIONS,
    LIBRARY_METRICS,
    SEVERITIES,
    THRESHOLD_OPERATORS,
    parse_asset,
    validate_rule_fields,
)

logger = logging.getLogger(__name__)

_RESOURCE = "quality_rule"
_CONTRACT_STATUSES = frozenset({"published", "draft"})


def _invalid(message: str, **details) -> None:
    raise InvalidParameterError(message=message, details=details or None)


def _require_revision(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _invalid("expected_revision 必须是整数")
    return value


def _optional_enum(data: dict, key: str, allowed) -> str | None:
    value = data.get(key)
    if value in (None, ""):
        return None
    if value not in allowed:
        _invalid(f"{key} 非法", field=key, value=value)
    return value


def _filters(data: dict) -> dict:
    enabled = data.get("enabled")
    if enabled is not None:
        if isinstance(enabled, bool):
            enabled = 1 if enabled else 0
        elif isinstance(enabled, int) and enabled in (0, 1):
            pass
        else:
            _invalid("enabled 必须是 0/1 或布尔值", field="enabled")
    return {
        "asset_type": _optional_enum(data, "asset_type", {"table", "column"}),
        "dimension": _optional_enum(data, "dimension", DIMENSIONS),
        "severity": _optional_enum(data, "severity", SEVERITIES),
        "check_type": _optional_enum(data, "check_type", CHECK_TYPES),
        "data_source_id": data.get("data_source_id") or None,
        "asset_id": data.get("asset_id") or None,
        "contract_id": data.get("contract_id") or None,
        "enabled": enabled,
        "keyword": (data.get("keyword") or "").strip() or None,
    }


def _normalized_fields(data: dict) -> dict:
    """取创建/编辑共用的规则字段并整体校验。"""
    definition = data.get("definition")
    threshold = data.get("threshold")
    validate_rule_fields(
        name=data.get("name"),
        asset_type=data.get("asset_type"),
        asset_id=data.get("asset_id"),
        dimension=data.get("dimension"),
        severity=data.get("severity"),
        check_type=data.get("check_type"),
        definition=definition,
        threshold=threshold,
    )
    data_source_id, _table, _column = parse_asset(
        data.get("asset_type"), data.get("asset_id"),
    )
    kb_id = data.get("kb_id")
    if kb_id is not None and (
        not isinstance(kb_id, str) or len(kb_id) > 128
    ):
        _invalid("kb_id 不能超过 128 字符", field="kb_id")
    return {
        "name": str(data.get("name")).strip(),
        "asset_type": data.get("asset_type"),
        "asset_id": data.get("asset_id"),
        "data_source_id": data_source_id,
        "kb_id": kb_id,
        "dimension": data.get("dimension"),
        "severity": data.get("severity"),
        "check_type": data.get("check_type"),
        "definition": definition,
        "threshold": threshold,
        "enabled": bool(data.get("enabled")),
    }


async def _require_data_source(session, tenant_id: str, data_source_id: str) -> None:
    row = (
        await session.execute(
            text(
                "SELECT 1 FROM knowledge_base.knowledge_data_sources "
                "WHERE tenant_id=:tenant_id AND id=:data_source_id "
                "AND is_deleted=0 LIMIT 1"
            ),
            {"tenant_id": tenant_id, "data_source_id": data_source_id},
        )
    ).scalar()
    if row is None:
        raise ResourceNotFoundError(message="数据源不存在或已删除")


def _infer_dimension(metric: str | None) -> str:
    if metric in ("duplicateValues", "unique"):
        return "uniqueness"
    if metric == "invalidValues":
        return "validity"
    return "completeness"


def _contract_payload(raw: Any) -> dict:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return {}
    return raw if isinstance(raw, dict) else {}


def _quality_items(node: dict) -> list[dict]:
    items = node.get("quality")
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _map_quality_item(item: dict, *, asset_type: str, asset_id: str) -> dict | None:
    """合同 quality 条目 → 规则字段；映射不了返回 None。

    W2.3 合同把比较运算符放在 quality 条目**顶层**（如
    ``{"metric": "rowCount", "mustBeGreaterThan": 0}``），没有嵌套
    ``threshold``；这里收集恰好一个顶层运算符组成规则的 threshold。
    """
    check_type = item.get("type")
    if check_type not in CHECK_TYPES:
        check_type = "library"
    definition: dict = {}
    arguments = item.get("arguments")
    if isinstance(arguments, dict):
        definition.update(arguments)
    for key in ("metric", "query", "engine", "implementation"):
        if key in item and item[key] is not None:
            definition[key] = item[key]

    metric = definition.get("metric")
    dimension = item.get("dimension")
    if dimension not in DIMENSIONS:
        dimension = _infer_dimension(metric if isinstance(metric, str) else None)
    severity = item.get("severity")
    if severity not in SEVERITIES:
        severity = "warning"
    threshold = {
        operator: item[operator]
        for operator in THRESHOLD_OPERATORS
        if operator in item and item[operator] is not None
    }
    if len(threshold) != 1:
        return None
    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        label = metric if isinstance(metric, str) and metric else check_type
        name = f"{label}@{asset_id}"
    if len(name) > 255:
        name = name[:255]
    return {
        "name": name,
        "asset_type": asset_type,
        "asset_id": asset_id,
        "dimension": dimension,
        "severity": severity,
        "check_type": check_type,
        "definition": definition,
        "threshold": threshold,
        "enabled": False,
    }


class QualityRuleService:
    """规则 CRUD 与合同导入。"""

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    async def list_rules(self, tenant_id: str, user_id, data: dict | None) -> dict:
        tenant_id = require_tenant(tenant_id)
        require_login(user_id, "需要登录后查看质量规则")
        data = data or {}
        page, page_size = paging(data)
        filters = _filters(data)
        async with get_db_session() as session:
            items = await repo.list_page(
                session, tenant_id=tenant_id, page=page, page_size=page_size,
                filters=filters,
            )
            total = await repo.count(session, tenant_id=tenant_id, filters=filters)
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    async def get_rule(self, tenant_id: str, user_id, rule_id: str) -> dict:
        tenant_id = require_tenant(tenant_id)
        require_login(user_id, "需要登录后查看质量规则")
        async with get_db_session() as session:
            row = await repo.get_by_id(
                session, tenant_id=tenant_id, rule_id=rule_id,
            )
        if row is None:
            raise ResourceNotFoundError(message="质量规则不存在")
        return row

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------

    async def create_rule(self, tenant_id: str, user_id, data: dict) -> dict:
        tenant_id = require_tenant(tenant_id)
        await require_admin(tenant_id, user_id, "仅租户管理员可维护质量规则")
        fields = _normalized_fields(data or {})
        fields["enabled"] = 1 if fields["enabled"] else 0
        async with get_db_session() as session:
            await _require_data_source(session, tenant_id, fields["data_source_id"])
            row = await repo.create_rule(
                session, tenant_id=tenant_id,
                created_by=user_id or None, updated_by=user_id or None,
                **fields,
            )
        audit(
            tenant_id=tenant_id, user_id=user_id, action="quality_rule_create",
            resource=_RESOURCE, resource_id=row["id"],
            params={
                "asset_type": row["asset_type"], "asset_id": row["asset_id"],
                "dimension": row["dimension"], "severity": row["severity"],
                "check_type": row["check_type"], "enabled": row["enabled"],
            },
        )
        return row

    async def update_rule(
        self, tenant_id: str, user_id, rule_id: str,
        expected_revision, data: dict,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        await require_admin(tenant_id, user_id, "仅租户管理员可维护质量规则")
        revision = _require_revision(expected_revision)
        data = data or {}
        async with get_db_session() as session:
            existing = await repo.get_by_id(
                session, tenant_id=tenant_id, rule_id=rule_id,
            )
            if existing is None:
                raise ResourceNotFoundError(message="质量规则不存在")

            merged = {
                **{key: existing.get(key) for key in (
                    "name", "asset_type", "asset_id", "kb_id", "dimension",
                    "severity", "check_type", "definition", "threshold", "enabled",
                )},
            }
            for key in (
                "name", "asset_type", "asset_id", "kb_id", "dimension",
                "severity", "check_type", "definition", "threshold", "enabled",
            ):
                if key in data:
                    merged[key] = data[key]
            normalized = _normalized_fields(merged)
            update_fields = {
                key: normalized[key] for key in (
                    "name", "asset_type", "asset_id", "data_source_id", "kb_id",
                    "dimension", "severity", "check_type", "definition",
                    "threshold", "enabled",
                )
                if key in data or key == "data_source_id"
            }
            await _require_data_source(
                session, tenant_id, normalized["data_source_id"],
            )
            rowcount = await repo.update_rule(
                session, tenant_id=tenant_id, rule_id=rule_id,
                expected_revision=revision, user_id=user_id or None,
                fields=update_fields,
            )
            if rowcount == 0:
                raise ResourceConflictError(
                    message="规则已被修改，请刷新后重试",
                    details={"rule_id": rule_id, "expected_revision": revision},
                )
            fresh = await repo.get_by_id(
                session, tenant_id=tenant_id, rule_id=rule_id,
            )
        audit(
            tenant_id=tenant_id, user_id=user_id, action="quality_rule_update",
            resource=_RESOURCE, resource_id=rule_id,
            params={
                "changed": sorted(data.keys()),
                "revision": fresh.get("revision") if fresh else None,
            },
        )
        return fresh

    async def delete_rule(self, tenant_id: str, user_id, rule_id: str) -> dict:
        tenant_id = require_tenant(tenant_id)
        await require_admin(tenant_id, user_id, "仅租户管理员可维护质量规则")
        async with get_db_session() as session:
            rowcount = await repo.soft_delete(
                session, tenant_id=tenant_id, rule_id=rule_id, user_id=user_id or None,
            )
        if rowcount == 0:
            raise ResourceNotFoundError(message="质量规则不存在")
        audit(
            tenant_id=tenant_id, user_id=user_id, action="quality_rule_delete",
            resource=_RESOURCE, resource_id=rule_id, params={},
        )
        return {"deleted": True, "rule_id": rule_id}

    # ------------------------------------------------------------------
    # 合同导入
    # ------------------------------------------------------------------

    async def import_from_contract(
        self, tenant_id: str, user_id, contract_id: str,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        await require_admin(tenant_id, user_id, "仅租户管理员可导入质量规则")
        if not isinstance(contract_id, str) or not contract_id.strip():
            _invalid("contract_id 必填")

        created = 0
        skipped = 0
        warnings: list[str] = []
        async with get_db_session() as session:
            contract = (
                await session.execute(
                    text(
                        "SELECT data_source_id, payload, status "
                        "FROM data_governance.data_contracts "
                        "WHERE tenant_id=:tenant_id AND id=:contract_id "
                        "AND is_deleted=0"
                    ),
                    {"tenant_id": tenant_id, "contract_id": contract_id},
                )
            ).mappings().first()
            if contract is None:
                raise ResourceNotFoundError(message="数据合同不存在")
            if contract["status"] not in _CONTRACT_STATUSES:
                raise ResourceConflictError(message="只有草稿或已发布合同可以导入质量规则")

            data_source_id = contract["data_source_id"]
            payload = _contract_payload(contract["payload"])
            schema = payload.get("schema")
            if not isinstance(schema, list) or not schema:
                audit(
                    tenant_id=tenant_id, user_id=user_id,
                    action="quality_rule_import", resource=_RESOURCE,
                    resource_id=contract_id,
                    params={"created": 0, "skipped": 0, "reason": "no_schema"},
                )
                return {"created": 0, "skipped": 0, "warnings": ["合同没有 schema 段"]}

            active = await load_active_schema(
                session, tenant_id=tenant_id, data_source_id=data_source_id,
            )
            if active is None:
                audit(
                    tenant_id=tenant_id, user_id=user_id,
                    action="quality_rule_import", resource=_RESOURCE,
                    resource_id=contract_id,
                    params={
                        "created": 0, "skipped": 0, "reason": "no_active_schema",
                    },
                )
                return {
                    "created": 0, "skipped": 0,
                    "warnings": ["数据源没有 active compiled schema，无法校验资产"],
                }
            # 资产索引与 W2.3 脚手架同口径：实体取 source_object_id（回落 name），
            # 属性取 source_attribute_id（回落 name）。
            entities: dict[str, dict] = {}
            for entity in active.get("entity_types") or []:
                if not isinstance(entity, dict):
                    continue
                key = entity_name(entity).casefold()
                if key:
                    entities[key] = entity

            for obj in schema:
                if not isinstance(obj, dict):
                    continue
                table = str(obj.get("name") or "").strip()
                if not table:
                    continue
                table_entity = entities.get(table.casefold())
                if table_entity is None:
                    warnings.append(f"资产不存在，跳过：{table}")
                    skipped += 1
                    continue
                attribute_names = {
                    attribute_name(attr).casefold()
                    for attr in table_entity.get("attributes") or []
                    if isinstance(attr, dict) and attribute_name(attr)
                }

                targets: list[tuple[str, str, dict]] = [
                    ("table", f"{data_source_id}:{table}", obj),
                ]
                properties = obj.get("properties")
                for prop in properties if isinstance(properties, list) else []:
                    if not isinstance(prop, dict):
                        continue
                    column = str(prop.get("name") or "").strip()
                    if not column:
                        continue
                    if column.casefold() not in attribute_names:
                        warnings.append(f"列不存在，跳过：{table}.{column}")
                        skipped += 1
                        continue
                    targets.append(
                        ("column", f"{data_source_id}:{table}.{column}", prop),
                    )

                for asset_type, asset_id, container in targets:
                    for item in _quality_items(container):
                        fields = _map_quality_item(
                            item, asset_type=asset_type, asset_id=asset_id,
                        )
                        if fields is None:
                            warnings.append(
                                "规则缺少或包含多个比较运算符，跳过：" + asset_id,
                            )
                            skipped += 1
                            continue
                        try:
                            normalized = _normalized_fields(fields)
                        except InvalidParameterError as exc:
                            warnings.append(f"规则非法，跳过：{asset_id}（{exc.message}）")
                            skipped += 1
                            continue
                        if await repo.exists_imported(
                            session, tenant_id=tenant_id, contract_id=contract_id,
                            name=normalized["name"], asset_id=asset_id,
                        ):
                            skipped += 1
                            continue
                        await repo.create_rule(
                            session, tenant_id=tenant_id,
                            created_by=user_id or None, updated_by=user_id or None,
                            contract_id=contract_id,
                            **{**normalized, "enabled": 0},
                        )
                        created += 1

        audit(
            tenant_id=tenant_id, user_id=user_id, action="quality_rule_import",
            resource=_RESOURCE, resource_id=contract_id,
            params={"created": created, "skipped": skipped},
        )
        return {"created": created, "skipped": skipped, "warnings": warnings}
