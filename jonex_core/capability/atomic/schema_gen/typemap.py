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
