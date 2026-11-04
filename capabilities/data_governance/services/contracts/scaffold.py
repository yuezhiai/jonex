"""从 active per-source compiled schema 生成 ODCS v3.2.0 脚手架（W2.3 任务 3）。

生成的 payload 只做「物理表/列 → ODCS 对象/属性」的机械映射：不推断业务语义、
不写质量规则，所有无法映射的类型按 string 兜底并记 warning。调用方拿到
``payload`` 后可走任务 2 的 ``validate_contract``；这里在返回前先做一次
``normalize_contract``（剔除 None、tags 去重）。
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .validation import normalize_contract

# 物理类型（小写、无精度）→ ODCS logicalType。
LOGICAL_TYPE_MAP: dict[str, str] = {
    "text": "string",
    "varchar": "string",
    "nvarchar": "string",
    "char": "string",
    "string": "string",
    "uuid": "string",
    "int": "integer",
    "integer": "integer",
    "bigint": "integer",
    "smallint": "integer",
    "number": "number",
    "numeric": "number",
    "decimal": "number",
    "float": "number",
    "double": "number",
    "date": "date",
    "timestamp": "timestamp",
    "datetime": "timestamp",
    "datetime2": "timestamp",
    "time": "time",
    "bool": "boolean",
    "boolean": "boolean",
    "json": "object",
    "jsonb": "object",
    "object": "object",
    "array": "array",
    "list": "array",
}

# brief 片段的命名；对外契约以 LOGICAL_TYPE_MAP 为准。
TYPE_MAP = LOGICAL_TYPE_MAP


def map_logical_type(physical: str) -> tuple[str, bool]:
    """物理类型 → (logicalType, 是否走了兜底)。

    先按原样查表，再剥掉长度/精度（``varchar(128)`` / ``numeric(18,2)``）；
    仍未命中时按 ``string`` 兜底，第二值 ``True`` 供调用方记 warning。
    """
    key = (physical or "").strip().lower()
    if key in LOGICAL_TYPE_MAP:
        return LOGICAL_TYPE_MAP[key], False
    base = key.split("(", 1)[0].strip()
    if base in LOGICAL_TYPE_MAP:
        return LOGICAL_TYPE_MAP[base], False
    return "string", True


def _truthy_flag(item: dict, keys: tuple[str, ...]) -> bool:
    """多键同名标记：任一键为真即真（编译器归一键与新老数据兼容）。"""
    return any(bool(item.get(key)) for key in keys)


def _is_required(attr: dict) -> bool | None:
    """``nullable`` / ``is_nullable`` / ``required`` 三种键名兼容。

    ``required`` 是正向语义，其余两个是反向语义；都没提供时返回 ``None``，
    调用方不写 ``required``（脚手架不臆造约束）。
    """
    if attr.get("nullable") is not None:
        return not bool(attr["nullable"])
    if attr.get("is_nullable") is not None:
        return not bool(attr["is_nullable"])
    if attr.get("required") is not None:
        return bool(attr["required"])
    return None


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def entity_name(entity: dict) -> str:
    """实体名：``source_object_id`` 优先，回落 ``name``。

    ``build_contract`` 只接受 ``source_object_id``（没有则跳过），质量合同导入
    的资产索引则用本函数——手写/旧合同的对象名可能只有逻辑实体名。
    """
    return _text(entity.get("source_object_id")) or _text(entity.get("name"))


def attribute_name(attr: dict) -> str:
    """属性名：``source_attribute_id`` 优先，回落 ``name``（脚手架与导入同口径）。"""
    return _text(attr.get("source_attribute_id")) or _text(attr.get("name"))


def _description(attr: dict) -> str:
    """展示名优先，其次描述；都为空或退化成属性名时不写该键。

    compiled schema 会把缺失的 ``display_name`` 归一成 ``name``，直接
    ``display_name or description`` 会让 description 永远等于列名；因此只在
    ``display_name`` 非空且不等于 ``name`` 时才采用，否则回落到 ``description``
    （``description`` 同样不允许退化成列名）。校验器拒绝空串，返回 ``""`` 表示
    不写该键。
    """
    name = _text(attr.get("name"))
    display_name = _text(attr.get("display_name"))
    if display_name and display_name != name:
        return display_name
    description = _text(attr.get("description"))
    if description and description != name:
        return description
    return ""


def _build_property(attr: dict, *, table: str) -> tuple[dict | None, list[str], bool]:
    """单列 → (property, warnings, 是否跳过)。"""
    name = attribute_name(attr)
    if not name:
        return None, [], True

    raw_type = attr.get("type")
    physical = _text(raw_type)
    logical_type, fallback = map_logical_type(physical)

    prop: dict = {"name": name, "logicalType": logical_type}
    # 空类型不写 physicalType：ODCS 校验拒绝空字符串。
    if physical:
        prop["physicalType"] = physical
    required = _is_required(attr)
    if required is not None:
        # 无 nullable/is_nullable/required 信息时不写 required（M4）。
        prop["required"] = required

    is_primary_key = _truthy_flag(
        attr, ("primary_key", "is_primary_key", "primaryKey")
    )
    prop["primaryKey"] = is_primary_key

    description = _description(attr)
    if description:
        prop["description"] = description

    warnings: list[str] = []
    if fallback:
        warnings.append(f"{table}.{name}: 未知类型 {physical}，按 string 处理")
    return prop, warnings, False


def build_contract(
    data_source: dict,
    entity_types: list,
    *,
    contract_id: str | None = None,
) -> tuple[dict, dict, list[str]]:
    """按 active per-source compiled schema 生成 ODCS 脚手架。

    返回 ``(payload, stats, warnings)``：

    - 无 ``source_object_id`` 的实体跳过（``skipped_tables``）；
    - 属性名（``source_attribute_id`` 优先，回落 ``name``）为空、或同一对象内
      重名时跳过（``skipped_columns``）；重名额外记 warning；
    - 主键位置在对象内按出现顺序从 1 递增。
    """
    schema: list[dict] = []
    warnings: list[str] = []
    stats = {
        "tables": 0,
        "columns": 0,
        "skipped_tables": 0,
        "skipped_columns": 0,
    }

    for entity in entity_types or []:
        if not isinstance(entity, dict):
            stats["skipped_tables"] += 1
            continue
        table = _text(entity.get("source_object_id"))
        if not table:
            stats["skipped_tables"] += 1
            continue

        properties: list[dict] = []
        seen_names: set[str] = set()
        primary_key_position = 0

        for attr in entity.get("attributes") or []:
            if not isinstance(attr, dict):
                stats["skipped_columns"] += 1
                continue
            prop, prop_warnings, skipped = _build_property(attr, table=table)
            if skipped or prop is None:
                stats["skipped_columns"] += 1
                continue
            if prop["name"] in seen_names:
                stats["skipped_columns"] += 1
                warnings.append(f"{table}.{prop['name']}: 属性名重复，已跳过")
                continue
            seen_names.add(prop["name"])

            if prop["primaryKey"]:
                primary_key_position += 1
                prop["primaryKeyPosition"] = primary_key_position

            warnings.extend(prop_warnings)
            properties.append(prop)
            stats["columns"] += 1

        schema.append(
            {
                "name": table,
                "logicalType": "object",
                "physicalType": "table",
                "properties": properties,
            }
        )
        stats["tables"] += 1

    source_id = _text(data_source.get("id")) or _text(data_source.get("data_source_id"))
    payload = {
        "id": contract_id or f"contract:{source_id}",
        "version": "1.0.0",
        "apiVersion": "v3.2.0",
        "kind": "DataContract",
        "name": _text(data_source.get("name")) or source_id,
        "status": "draft",
        "customProperties": [
            {"property": "platform.dataSourceId", "value": source_id}
        ],
        "schema": schema,
    }
    return normalize_contract(payload), stats, warnings


# ---------------------------------------------------------------------------
# SQL 读取（service/API 层只调用这里，不持有裸 SQL）
# ---------------------------------------------------------------------------


async def load_source(
    session: AsyncSession, *, tenant_id: str, data_source_id: str
) -> dict | None:
    """读取未删除的数据源 ``{id, name}``；不存在返回 ``None``。"""
    row = (
        await session.execute(
            text(
                """
                SELECT id, name
                FROM knowledge_base.knowledge_data_sources
                WHERE id = :data_source_id AND tenant_id = :tenant_id
                  AND is_deleted = 0
                LIMIT 1
                """
            ),
            {"tenant_id": tenant_id, "data_source_id": data_source_id},
        )
    ).mappings().first()
    if not row:
        return None
    return {"id": row["id"], "name": row["name"]}


async def load_active_schema(
    session: AsyncSession, *, tenant_id: str, data_source_id: str
) -> dict | None:
    """读取该数据源最新一版 active per-source compiled schema。

    返回 ``{"entity_types": list, "schema_version": int | None}``；无 active
    版本返回 ``None``。``entity_types`` 统一成 list（JSONB 驱动返回 str 时解析）。
    """
    row = (
        await session.execute(
            text(
                """
                SELECT entity_types, schema_version
                FROM knowledge_base.ontology_compiled_schemas
                WHERE tenant_id = :tenant_id AND status = 'active'
                  AND data_source_id = :data_source_id
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ),
            {"tenant_id": tenant_id, "data_source_id": data_source_id},
        )
    ).mappings().first()
    if not row:
        return None

    entity_types = row["entity_types"]
    if isinstance(entity_types, str):
        try:
            entity_types = json.loads(entity_types)
        except (TypeError, ValueError):
            entity_types = []
    if not isinstance(entity_types, list):
        entity_types = []
    return {
        "entity_types": entity_types,
        "schema_version": row["schema_version"],
    }
