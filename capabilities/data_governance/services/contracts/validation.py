"""ODCS v3.x 数据合同 payload 校验与归一化（W2.3）。

只实现平台用到的子集，不引入 jsonschema：所有错误统一抛
``InvalidParameterError``，消息格式为 ``payload.<path>: <原因>``，便于前端
直接展示字段级原因。
"""
from __future__ import annotations

import copy
import json

from jonex_core.common.exceptions import InvalidParameterError

API_VERSIONS = frozenset({"v3.2.0", "v3.1.0", "v3.0.2", "v3.0.1", "v3.0.0"})

LOGICAL_TYPES = frozenset(
    {
        "string",
        "date",
        "timestamp",
        "time",
        "number",
        "integer",
        "object",
        "array",
        "boolean",
        "map",
        "vector",
    }
)

# 对象级 logicalType 只允许 object；缺省视为 object。
_SCHEMA_LOGICAL_TYPES = frozenset({"object"})

_QUALITY_TYPES = frozenset({"text", "library", "sql", "custom"})
_QUALITY_DIMENSIONS = frozenset(
    {"accuracy", "completeness", "conformity", "consistency", "coverage", "timeliness", "uniqueness"}
)

# 比较运算符集合：质量规则必须恰好命中一个。
# 用元组且排序，报错信息不随进程哈希种子漂移。
_COMPARISON_OPERATORS = (
    "mustBe",
    "mustBeBetween",
    "mustBeGreaterOrEqualTo",
    "mustBeGreaterThan",
    "mustBeLessOrEqualTo",
    "mustBeLessThan",
    "mustNotBe",
    "mustNotBeBetween",
)
_BETWEEN_OPERATORS = frozenset({"mustBeBetween", "mustNotBeBetween"})

_SCHEMA_BOOL_FIELDS = (
    "criticalDataElement",
    "deprecated",
    "partitioned",
    "primaryKey",
    "required",
    "unique",
)
_PROPERTY_BOOL_FIELDS = _SCHEMA_BOOL_FIELDS + ("encrypted",)
_POSITION_FIELDS = ("partitionKeyPosition", "primaryKeyPosition")

# slaveProperties.property 常用集合（含常见写法）；未列出的自定义 SLA 走
# customProperties，不在这里开口子。
_SLA_PROPERTIES = frozenset(
    {
        "availability",
        "throughput",
        "errorRate",
        "error_rate",
        "generalAvailability",
        "general_availability",
        "endOfSupport",
        "end_of_support",
        "endOfLife",
        "end_of_life",
        "retention",
        "frequency",
        "latency",
        "timeToDetect",
        "time_to_detect",
        "timeToNotify",
        "time_to_notify",
        "timeToRepair",
        "time_to_repair",
    }
)

_ROOT_KEYS = frozenset(
    {
        "id",
        "version",
        "apiVersion",
        "kind",
        "name",
        "status",
        "domain",
        "tenant",
        "description",
        "authoritativeDefinitions",
        "tags",
        "customProperties",
        "servers",
        "support",
        "price",
        "team",
        "roles",
        "schema",
        "slaProperties",
        "context",
        "contractCreatedTs",
    }
)

# 对象/属性未知键一律拒绝（扩展走 customProperties）。
_SCHEMA_KEYS = frozenset(
    {
        "name",
        "physicalName",
        "physicalType",
        "logicalType",
        "description",
        "businessName",
        "dataGranularityDescription",
        "properties",
        "relationships",
        "quality",
        "tags",
        "customProperties",
        "authoritativeDefinitions",
        "required",
        "unique",
        "primaryKey",
        "partitioned",
        "deprecated",
        "criticalDataElement",
        "primaryKeyPosition",
        "partitionKeyPosition",
    }
)

_PROPERTY_KEYS = frozenset(
    {
        "name",
        "physicalName",
        "physicalType",
        "logicalType",
        "description",
        "businessName",
        "displayName",
        "required",
        "unique",
        "primaryKey",
        "primaryKeyPosition",
        "partitioned",
        "partitionKeyPosition",
        "criticalDataElement",
        "deprecated",
        "encrypted",
        "classification",
        "examples",
        "valueDomain",
        "precision",
        "scale",
        "format",
        "quality",
        "tags",
        "customProperties",
        "authoritativeDefinitions",
        "relationships",
    }
)

_ROOT_TEXT_FIELDS = ("name", "status", "domain", "tenant", "contractCreatedTs")
_MAX_PAYLOAD_BYTES = 4 * 1024 * 1024


def _bad(path: str, reason: str):
    raise InvalidParameterError(message=f"{path}: {reason}", details={"path": path})


def _text(value, path: str, *, max_len: int | None = None) -> str:
    """存在即必须为非空字符串；顺带拦下写不进 PostgreSQL 文本列的字符。

    ``None`` 表示「未提供」，由各调用方决定是否允许；空串与纯空白一律拒绝。
    """
    if not isinstance(value, str) or not value.strip():
        _bad(path, "必须是非空字符串")
    if "\x00" in value:
        # NUL 字符无法写入 PostgreSQL 文本列（asyncpg 直接拒绝）。
        _bad(path, "含非法字符（NUL）")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        # 孤立代理字符（\ud800-\udfff）无法编码，放行会在下游 json/encode 炸成 500。
        _bad(path, "含非法字符（孤立代理项）")
    if max_len is not None and len(value) > max_len:
        _bad(path, f"超长（>{max_len}）")
    return value.strip()


def _optional_text(value, path: str) -> None:
    """可选字符串：None 与不传同义。"""
    if value is not None:
        _text(value, path)


def _is_number(value) -> bool:
    # bool 是 int 的子类，必须显式排除。
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_no_nul(value, path: str, _seen: set[int] | None = None) -> None:
    """递归检查所有字符串值与字典键中的 NUL 字符。

    校验器只对白名单字段调用 ``_text``；``customProperties.value``、
    ``servers``、``relationships`` 等原样接受的结构此前会绕过检查，
    直到写入 PostgreSQL 才炸成 500。这里在序列化前统一拦成 1001。

    同时检测循环引用：YAML 的 ``&anchor`` / ``*anchor`` 可以构造自引用对象，
    ``json.dumps`` 与递归遍历都会无限展开，必须在校验期回 1001（而不是 500）。
    """
    seen = _seen if _seen is not None else set()
    marker = None
    if isinstance(value, (dict, list, tuple)):
        marker = id(value)
        if marker in seen:
            _bad(path, "不支持循环引用的结构")
        seen.add(marker)
    try:
        if isinstance(value, str):
            if "\x00" in value:
                _bad(path, "含非法字符（NUL）")
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(key, str) and "\x00" in key:
                    _bad(f"{path}[{key!r}]", "含非法字符（NUL）")
                child = f"{path}.{key}" if isinstance(key, str) else f"{path}[{key!r}]"
                _validate_no_nul(item, child, seen)
            return
        if isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                _validate_no_nul(item, f"{path}[{index}]", seen)
    finally:
        if marker is not None:
            # 退出时移除：同一对象被多处（非循环）引用是合法的。
            seen.discard(marker)


def _payload_size(payload: dict) -> int:
    try:
        # allow_nan=False：Python 默认把 NaN/Infinity 序列化成非法 JSON 字面量，
        # YAML 导入链路会带进这些值，必须在校验期拒绝。
        return len(
            json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        )
    except (TypeError, ValueError, UnicodeEncodeError):
        _bad("payload", "必须是可 JSON 序列化的对象（不支持 NaN/Infinity）")


def validate_contract(payload: dict, *, require_schema: bool = False) -> None:
    """校验 ODCS v3.x 子集；非法时抛 ``InvalidParameterError``（1001）。"""
    if not isinstance(payload, dict):
        _bad("payload", "必须是对象")
    # NUL 可能藏在任意嵌套结构（customProperties.value / servers /
    # relationships）里，必须在序列化与落库前统一拦下。
    _validate_no_nul(payload, "payload")
    if _payload_size(payload) > _MAX_PAYLOAD_BYTES:
        _bad("payload", "序列化后超过 4MB")

    for key in ("id", "version", "apiVersion", "kind"):
        if key not in payload or payload[key] is None:
            _bad(f"payload.{key}", "缺少必填字段")
    _text(payload["id"], "payload.id", max_len=128)
    _text(payload["version"], "payload.version", max_len=64)
    api_version = _text(payload["apiVersion"], "payload.apiVersion")
    if isinstance(payload["kind"], str) and payload["kind"] == "DataContract":
        pass
    else:
        _bad("payload.kind", "必须是 DataContract")
    if api_version.startswith("v2."):
        # v2 是最常见的导入误用来源，单独给出可操作提示。
        _bad("payload.apiVersion", f"仅支持 ODCS v3.x（收到 {api_version}）")
    if api_version not in API_VERSIONS:
        _bad(
            "payload.apiVersion",
            "仅支持 ODCS v3.x（v3.0.0/v3.0.1/v3.0.2/v3.1.0/v3.2.0）",
        )

    unknown = set(payload) - _ROOT_KEYS
    if unknown:
        # 取排序后的第一个：报错内容稳定，不受哈希种子影响。
        # 键可能是 YAML 解析出的非字符串（如 ``{1: x}``）；统一转成字符串再
        # 排序，混类型键不能把校验炸成 500。
        _bad(f"payload.{sorted(map(str, unknown))[0]}", "未知字段")

    for key in _ROOT_TEXT_FIELDS:
        _optional_text(payload.get(key), f"payload.{key}")
    _validate_description(payload.get("description"), "payload.description")

    _validate_tags(payload.get("tags"), "payload.tags")
    _validate_custom_properties(payload.get("customProperties"), "payload.customProperties")
    _validate_authoritative_definitions(
        payload.get("authoritativeDefinitions"), "payload.authoritativeDefinitions"
    )
    _validate_roles(payload.get("roles"), "payload.roles")
    _validate_sla_properties(payload.get("slaProperties"), "payload.slaProperties")

    schema = payload.get("schema")
    if schema is None:
        if require_schema:
            _bad("payload.schema", "发布时必须提供非空 schema")
        return
    if not isinstance(schema, list):
        _bad("payload.schema", "必须是数组")
    if require_schema and not schema:
        _bad("payload.schema", "发布时必须提供非空 schema")
    for index, item in enumerate(schema):
        _validate_schema_object(item, f"payload.schema[{index}]")


# ---------------------------------------------------------------------------
# schema 对象 / 属性
# ---------------------------------------------------------------------------


def _validate_unknown_keys(item: dict, allowed: frozenset, path: str) -> None:
    unknown = set(item) - allowed
    if unknown:
        _bad(f"{path}.{sorted(map(str, unknown))[0]}", "未知字段")


def _validate_schema_object(item, path: str) -> None:
    if not isinstance(item, dict):
        _bad(path, "必须是对象")
    _validate_unknown_keys(item, _SCHEMA_KEYS, path)
    if "name" not in item or item["name"] is None:
        _bad(f"{path}.name", "缺少必填字段")
    _text(item["name"], f"{path}.name")

    if item.get("logicalType") is not None:
        logical_type = item["logicalType"]
        if not isinstance(logical_type, str) or logical_type not in _SCHEMA_LOGICAL_TYPES:
            _bad(f"{path}.logicalType", "对象级 logicalType 只能是 object")

    _validate_bool_fields(item, _SCHEMA_BOOL_FIELDS, path)
    _validate_positions(item, path)
    _validate_optional_strings(item, ("physicalName", "physicalType", "description", "businessName"), path)

    properties = item.get("properties")
    if properties is not None:
        if not isinstance(properties, list):
            _bad(f"{path}.properties", "必须是数组")
        seen: set[str] = set()
        for index, prop in enumerate(properties):
            _validate_property(prop, f"{path}.properties[{index}]", seen)

    _validate_quality(item.get("quality"), f"{path}.quality")
    _validate_tags(item.get("tags"), f"{path}.tags")
    _validate_custom_properties(item.get("customProperties"), f"{path}.customProperties")
    _validate_authoritative_definitions(
        item.get("authoritativeDefinitions"), f"{path}.authoritativeDefinitions"
    )
    # relationships 结构原样接受（P1 不校验 from/to 类型）。


def _validate_property(item, path: str, seen: set[str]) -> None:
    if not isinstance(item, dict):
        _bad(path, "必须是对象")
    _validate_unknown_keys(item, _PROPERTY_KEYS, path)
    if "name" not in item or item["name"] is None:
        _bad(f"{path}.name", "缺少必填字段")
    name = _text(item["name"], f"{path}.name")
    if name in seen:
        # 同一对象内重名列会静默覆盖下游映射，必须拒绝。
        _bad(f"{path}.name", f"属性名重复（{name}）")
    seen.add(name)

    if item.get("logicalType") is not None:
        logical_type = item["logicalType"]
        if not isinstance(logical_type, str) or logical_type not in LOGICAL_TYPES:
            _bad(f"{path}.logicalType", "非法 logicalType")

    _validate_bool_fields(item, _PROPERTY_BOOL_FIELDS, path)
    _validate_positions(item, path)
    _validate_optional_strings(
        item,
        (
            "physicalName",
            "physicalType",
            "description",
            "businessName",
            "displayName",
            "classification",
            "format",
        ),
        path,
    )

    _validate_quality(item.get("quality"), f"{path}.quality")
    _validate_tags(item.get("tags"), f"{path}.tags")
    _validate_custom_properties(item.get("customProperties"), f"{path}.customProperties")
    _validate_authoritative_definitions(
        item.get("authoritativeDefinitions"), f"{path}.authoritativeDefinitions"
    )


def _validate_bool_fields(item: dict, fields, path: str) -> None:
    for key in fields:
        value = item.get(key)
        if value is not None and not isinstance(value, bool):
            _bad(f"{path}.{key}", "必须是布尔值")


def _validate_positions(item: dict, path: str) -> None:
    for key in _POSITION_FIELDS:
        value = item.get(key)
        if value is None:
            continue
        # bool 是 int 子类，True 不能当作位置 1。
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            _bad(f"{path}.{key}", "必须是 ≥1 的整数")


def _validate_optional_strings(item: dict, fields, path: str) -> None:
    for key in fields:
        _optional_text(item.get(key), f"{path}.{key}")


# ---------------------------------------------------------------------------
# quality
# ---------------------------------------------------------------------------


def _validate_quality(quality, path: str) -> None:
    if quality is None:
        return
    if not isinstance(quality, list):
        _bad(path, "必须是数组")
    for index, rule in enumerate(quality):
        _validate_quality_rule(rule, f"{path}[{index}]")


def _validate_quality_rule(rule, path: str) -> None:
    if not isinstance(rule, dict):
        _bad(path, "必须是对象")
    quality_type = rule.get("type", "library")
    if not isinstance(quality_type, str) or quality_type not in _QUALITY_TYPES:
        _bad(f"{path}.type", "必须是 text/library/sql/custom")

    if quality_type == "library":
        _require_text(rule, "metric", f"{path}.metric")
    elif quality_type == "sql":
        _require_text(rule, "query", f"{path}.query")
    elif quality_type == "custom":
        _require_text(rule, "engine", f"{path}.engine")
        _require_text(rule, "implementation", f"{path}.implementation")
    else:  # text
        description = rule.get("description")
        name = rule.get("name")
        if not _non_empty_text(description) and not _non_empty_text(name):
            _bad(path, "text 类型必须提供 description 或 name")

    # 运算符「存在」以值非 None 为准：``{"mustBe": None}`` 与未提供等价，
    # 否则归一化剔除 None 后再次校验会得到不同结论。
    present = [
        operator
        for operator in _COMPARISON_OPERATORS
        if operator in rule and rule[operator] is not None
    ]
    if len(present) != 1:
        _bad(path, f"比较运算符必须恰好一个（实际 {len(present)} 个）")
    operator = present[0]
    if operator in _BETWEEN_OPERATORS:
        bounds = rule[operator]
        if (
            not isinstance(bounds, list)
            or len(bounds) != 2
            or not all(_is_number(value) for value in bounds)
        ):
            _bad(f"{path}.{operator}", "必须是 2 个数字组成的数组")

    dimension = rule.get("dimension")
    if dimension is not None:
        if not isinstance(dimension, str) or dimension not in _QUALITY_DIMENSIONS:
            _bad(f"{path}.dimension", "非法 dimension")
    _validate_optional_strings(rule, ("severity", "scheduler", "schedule"), path)


def _non_empty_text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _require_text(item: dict, key: str, path: str) -> None:
    if key not in item or item[key] is None:
        _bad(path, "缺少必填字段")
    _text(item[key], path)


# ---------------------------------------------------------------------------
# slaProperties / roles / 扩展
# ---------------------------------------------------------------------------


def _validate_sla_properties(sla_properties, path: str) -> None:
    if sla_properties is None:
        return
    if not isinstance(sla_properties, list):
        _bad(path, "必须是数组")
    for index, item in enumerate(sla_properties):
        item_path = f"{path}[{index}]"
        if not isinstance(item, dict):
            _bad(item_path, "必须是对象")
        if "property" not in item or item["property"] is None:
            _bad(f"{item_path}.property", "缺少必填字段")
        prop = _text(item["property"], f"{item_path}.property")
        if prop not in _SLA_PROPERTIES:
            _bad(f"{item_path}.property", "非法 SLA 属性")
        if "value" not in item or item["value"] is None:
            _bad(f"{item_path}.value", "缺少必填字段")
        # SLA 值允许字符串（如 "99.9%"）或数字；bool 不算数字。
        value = item["value"]
        if not isinstance(value, str) and not _is_number(value):
            _bad(f"{item_path}.value", "必须是字符串或数字")
        _validate_optional_strings(
            item, ("scheduler", "schedule", "unit", "element", "driver"), item_path
        )


_DESCRIPTION_KEYS = frozenset({
    "purpose", "limitations", "usage",
    "authoritativeDefinitions", "customProperties",
})


def _validate_description(value, path: str) -> None:
    """根级 ``description``：ODCS v3 是对象；兼容纯字符串写法。

    ODCS 3.x 的根级 description 形如
    ``{purpose, limitations, usage, authoritativeDefinitions, customProperties}``。
    早期示例存在直接写字符串的用法，这里一并接受，避免导入即拒。
    """
    if value is None:
        return
    if isinstance(value, str):
        if not value.strip():
            _bad(path, "不能是空白字符串")
        return
    if not isinstance(value, dict):
        _bad(path, "必须是对象或字符串")
    unknown = set(value) - _DESCRIPTION_KEYS
    if unknown:
        _bad(f"{path}.{sorted(map(str, unknown))[0]}", "未知字段")
    for key in ("purpose", "limitations", "usage"):
        _optional_text(value.get(key), f"{path}.{key}")
    _validate_authoritative_definitions(
        value.get("authoritativeDefinitions"), f"{path}.authoritativeDefinitions"
    )
    _validate_custom_properties(
        value.get("customProperties"), f"{path}.customProperties"
    )


def _validate_roles(roles, path: str) -> None:
    if roles is None:
        return
    if not isinstance(roles, list):
        _bad(path, "必须是数组")
    for index, item in enumerate(roles):
        item_path = f"{path}[{index}]"
        if not isinstance(item, dict):
            _bad(item_path, "必须是对象")
        if "role" not in item or item["role"] is None:
            _bad(f"{item_path}.role", "缺少必填字段")
        _text(item["role"], f"{item_path}.role")
        _validate_optional_strings(item, ("firstLevelApprovers", "secondLevelApprovers"), item_path)


def _validate_tags(tags, path: str) -> None:
    if tags is None:
        return
    if not isinstance(tags, list):
        _bad(path, "必须是数组")
    for index, tag in enumerate(tags):
        _text(tag, f"{path}[{index}]")


def _validate_authoritative_definitions(definitions, path: str) -> None:
    if definitions is None:
        return
    if not isinstance(definitions, list):
        _bad(path, "必须是数组")
    for index, item in enumerate(definitions):
        item_path = f"{path}[{index}]"
        if not isinstance(item, dict):
            _bad(item_path, "必须是对象")
        for key in ("url", "type"):
            if key not in item or item[key] is None:
                _bad(f"{item_path}.{key}", "缺少必填字段")
            _text(item[key], f"{item_path}.{key}")


def _validate_custom_properties(properties, path: str) -> None:
    if properties is None:
        return
    if not isinstance(properties, list):
        _bad(path, "必须是数组")
    for index, item in enumerate(properties):
        item_path = f"{path}[{index}]"
        if not isinstance(item, dict):
            _bad(item_path, "必须是对象")
        if "property" not in item or item["property"] is None:
            _bad(f"{item_path}.property", "缺少必填字段")
        _text(item["property"], f"{item_path}.property")
        # value 可以是任意 JSON 值（含 null），只要求键存在。
        if "value" not in item:
            _bad(f"{item_path}.value", "缺少必填字段")
        _optional_text(item.get("vendor"), f"{item_path}.vendor")


# ---------------------------------------------------------------------------
# normalize
# ---------------------------------------------------------------------------


def normalize_contract(payload: dict) -> dict:
    """深拷贝并归一化：tags 去重保序、可选键的 None 值剔除。

    只清理合同语义层（根 / schema 对象 / 属性 / quality / slaProperties /
    roles / authoritativeDefinitions / customProperties）的 None；任意嵌套
    内容（如 ``customProperties.value``、``relationships``）原样保留。
    """
    if not isinstance(payload, dict):
        _bad("payload", "必须是对象")
    out = copy.deepcopy(payload)

    _drop_none(out)
    _dedupe_tags(out)

    for item in out.get("schema") or []:
        if not isinstance(item, dict):
            continue
        _drop_none(item)
        _dedupe_tags(item)
        _normalize_quality(item.get("quality"))
        _normalize_nested_lists(item)
        for prop in item.get("properties") or []:
            if not isinstance(prop, dict):
                continue
            _drop_none(prop)
            _dedupe_tags(prop)
            _normalize_quality(prop.get("quality"))
            _normalize_nested_lists(prop)

    _normalize_quality(out.get("quality"))
    _normalize_nested_lists(out)
    # 根级 description 是对象时，其 customProperties / authoritativeDefinitions
    # 也是合同语义层，按同一口径清理 None（M6）。
    description = out.get("description")
    if isinstance(description, dict):
        _drop_none(description)
        _normalize_nested_lists(description)
    return out


def _normalize_quality(quality) -> None:
    if not isinstance(quality, list):
        return
    for rule in quality:
        if isinstance(rule, dict):
            # 比较运算符键即使值为 None 也保留：校验口径以「值非 None」为准，
            # 保留键可让归一化前后的校验结论一致（不会从 1 个变成 0 个）。
            _drop_none(rule, skip=_COMPARISON_OPERATORS)


def _normalize_nested_lists(container: dict) -> None:
    """清理合同语义层数组中各对象的 None 值。"""
    for key in ("slaProperties", "roles", "authoritativeDefinitions"):
        for item in container.get(key) or []:
            if isinstance(item, dict):
                _drop_none(item)
    # customProperties.value 可能是合法的 null，只清理其余键。
    for item in container.get("customProperties") or []:
        if isinstance(item, dict):
            _drop_none(item, skip=("value",))


def _drop_none(item: dict, *, skip=()) -> None:
    for key in list(item):
        if key not in skip and item[key] is None:
            del item[key]


def _dedupe_tags(container: dict) -> None:
    tags = container.get("tags")
    if not isinstance(tags, list):
        return
    seen: list = []
    for tag in tags:
        if tag not in seen:
            seen.append(tag)
    container["tags"] = seen
