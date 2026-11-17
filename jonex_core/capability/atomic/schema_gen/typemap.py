"""PostgreSQL 类型 → nexonto 的 8 种属性类型。

nexonto 的词表是 string / text / number / integer / boolean / date / datetime / enum，
比 PG 窄得多，降级是必然的。降级本身不可怕，可怕的是**静默**降级——
jsonb、数组、几何类型都会丢结构，这些要记进损失清单交出去。
"""

from __future__ import annotations

import re

# 精度括号**不只在末尾**：`timestamp(6) without time zone`、`timestamp(0) with time zone`
# 都是合法写法。曾经用 `^([a-z_ ]+?)(?:\(.*\))?$` 去尾括号，结果带精度的 timestamp
# 匹配不到末尾 `$`，一路退到兜底的 `text`——时间列全被降级成文本。
# 正确做法是先把括号整体摘掉，再归一空白。
_PAREN = re.compile(r"\([^)]*\)")
_WS = re.compile(r"\s+")

_TYPE_MAP: dict[str, str] = {
    # 字符串族
    "character varying": "string",
    "character": "string",
    "varchar": "string",
    "char": "string",
    "bpchar": "string",
    "name": "string",
    "text": "text",
    "citext": "text",
    # 整数族
    "smallint": "integer",
    "integer": "integer",
    "int": "integer",
    "int2": "integer",
    "int4": "integer",
    "int8": "integer",
    "bigint": "integer",
    "serial": "integer",
    "bigserial": "integer",
    # 小数族
    "numeric": "number",
    "decimal": "number",
    "real": "number",
    "double precision": "number",
    "float4": "number",
    "float8": "number",
    "money": "number",
    # 布尔
    "boolean": "boolean",
    "bool": "boolean",
    # 时间族
    "date": "date",
    "timestamp without time zone": "datetime",
    "timestamp with time zone": "datetime",
    "timestamp": "datetime",
    "timestamptz": "datetime",
    "time without time zone": "string",
    "time with time zone": "string",
    "interval": "string",
}

# 这些类型在 nexonto 里没有对应，只能降级。降级目标记下来，供损失清单使用
_LOSSY: dict[str, str] = {
    "json": "text",
    "jsonb": "text",
    "xml": "text",
    "uuid": "string",
    "bytea": "text",
    "inet": "string",
    "cidr": "string",
    "macaddr": "string",
    "point": "text",
    "polygon": "text",
    "line": "text",
    "box": "text",
    "path": "text",
    "circle": "text",
    "tsvector": "text",
    "tsquery": "text",
    "ARRAY": "text",
    "USER-DEFINED": "text",
}


def base_of(pg_type: str) -> str:
    """`timestamp(6) without time zone` → `timestamp without time zone`"""
    s = _PAREN.sub("", pg_type.strip().lower())
    return _WS.sub(" ", s).strip()


# ── 引擎类型归一（W1）────────────────────────────────────────────────────
#: 引擎别名 → 归一键。postgres 家族直接原样透传（既有口径不变）。
_ENGINE_ALIASES = {
    "postgres": "postgres", "postgresql": "postgres", "pg": "postgres",
    "mysql": "mysql", "mariadb": "mysql",
    "sqlserver": "sqlserver", "mssql": "sqlserver",
    "clickhouse": "clickhouse", "ch": "clickhouse",
    "influx_v3": "influx_v3", "influxdb": "influx_v3", "influx": "influx_v3",
}

_WRAPPER = re.compile(
    r"^(nullable|lowcardinality)\s*\((.*)\)$", re.IGNORECASE | re.DOTALL)


def _unwrap_wrappers(type_name: str) -> str:
    """剥 `Nullable(T)` / `LowCardinality(T)` 外壳（可嵌套）。"""
    text = str(type_name or "").strip()
    while True:
        match = _WRAPPER.match(text)
        if not match:
            return text
        text = match.group(2).strip()


def _normalize_clickhouse(type_name: str) -> str:
    text = _unwrap_wrappers(type_name)
    lowered = text.lower()
    if lowered.startswith("array("):
        return "text[]"
    if (lowered.startswith("map(") or lowered.startswith("tuple(")
            or lowered.startswith("nested(")):
        return "json"
    if lowered.startswith("enum"):
        return "text"
    base = _PAREN.sub("", lowered).strip()
    mapping = {
        "string": "varchar", "fixedstring": "varchar",
        "int8": "integer", "int16": "integer", "int32": "integer",
        "uint8": "integer", "uint16": "integer", "uint32": "integer",
        "int64": "bigint", "uint64": "bigint",
        "int128": "bigint", "uint128": "bigint",
        "int256": "bigint", "uint256": "bigint",
        "float32": "real", "float64": "double precision",
        "decimal": "numeric", "decimal32": "numeric",
        "decimal64": "numeric", "decimal128": "numeric",
        "decimal256": "numeric",
        "bool": "boolean", "boolean": "boolean",
        "date": "date", "date32": "date",
        "datetime": "timestamp without time zone",
        "datetime64": "timestamp without time zone",
        "uuid": "uuid", "ipv4": "inet", "ipv6": "inet",
        "json": "json", "object": "json",
    }
    return mapping.get(base, text)


def _normalize_influx(type_name: str) -> str:
    text = _unwrap_wrappers(type_name)
    lowered = text.lower()
    if lowered.startswith("dictionary("):
        return "text"
    base = _PAREN.sub("", lowered).strip()
    mapping = {
        "utf8": "varchar", "string": "varchar",
        "int64": "bigint", "uint64": "bigint",
        "int32": "integer", "uint32": "integer",
        "float64": "double precision", "float32": "real",
        "boolean": "boolean", "bool": "boolean",
        "date": "date", "timestamp": "timestamp without time zone",
        "datetime": "timestamp without time zone",
    }
    if lowered.startswith("timestamp"):
        return "timestamp without time zone"
    return mapping.get(base, text)


def normalize_engine_type(engine: str, type_name: str) -> str:
    """把引擎原始类型串翻成 `_TYPE_MAP` 认识的规范名。

    只对 ClickHouse / InfluxDB v3 做翻译；postgres / mysql / sqlserver 原样返回
    （既有行为逐字节不变）。不认识的类型原样返回，由 `map_type` 落 text + 有损。
    """
    key = _ENGINE_ALIASES.get(str(engine or "").strip().lower(), "")
    if key == "clickhouse":
        return _normalize_clickhouse(type_name)
    if key == "influx_v3":
        return _normalize_influx(type_name)
    return str(type_name or "")


def map_type(pg_type: str) -> tuple[str, bool]:
    """返回 (nexonto 类型, 是否有损)。"""
    if pg_type.strip().endswith("[]"):
        # 数组在 nexonto 里无处安放，退成 text 并标记有损
        return "text", True
    base = base_of(pg_type)
    if base in _TYPE_MAP:
        return _TYPE_MAP[base], False
    if base in _LOSSY:
        return _LOSSY[base], True
    # 自建枚举类型（pg_type.typtype='e'）在 catalog 里显示为 USER-DEFINED，
    # 这里只能退成 text；真正的取值要从注释或 pg_enum 补
    if base in ("user-defined",):
        return "text", True
    return "text", True
