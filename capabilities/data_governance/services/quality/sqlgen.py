"""W2.5 检查 SQL 生成（纯函数；平台只生成、不执行）。"""
from __future__ import annotations

from typing import Any

from capabilities.data_governance.services.quality.validation import (
    LIBRARY_METRICS,
    TABLE_METRICS,
    parse_asset,
)
from jonex_core.common.exceptions import InvalidParameterError

DIALECTS = frozenset({"tsql", "ansi"})


def _invalid(message: str, **details) -> None:
    raise InvalidParameterError(message=message, details=details or None)


def _require_dialect(dialect: str) -> str:
    if dialect not in DIALECTS:
        _invalid("dialect 必须是 tsql 或 ansi", dialect=dialect)
    return dialect


def quote_ident(name: str, dialect: str = "tsql") -> str:
    """按方言引用单个标识符；内部定界符双写转义。"""
    _require_dialect(dialect)
    if not isinstance(name, str) or not name.strip():
        _invalid("标识符不能为空", name=name)
    if dialect == "tsql":
        return "[" + name.replace("]", "]]") + "]"
    return '"' + name.replace('"', '""') + '"'


def quote_path(table: str, dialect: str = "tsql") -> str:
    """表路径按 `.` 拆段后逐段引用（`dbo.orders` → `[dbo].[orders]`）。"""
    _require_dialect(dialect)
    if not isinstance(table, str) or not table.strip():
        _invalid("表名不能为空", table=table)
    parts = table.split(".")
    if any(not part.strip() for part in parts):
        _invalid("表路径包含空段", table=table)
    return ".".join(quote_ident(part, dialect) for part in parts)


def _literal(value: Any) -> str:
    """字符串字面量：单引号双写转义。"""
    if not isinstance(value, str):
        _invalid("检查参数中的值必须是字符串", value=value)
    return "'" + value.replace("'", "''") + "'"


def build_rule_sql(rule: dict, dialect: str = "tsql") -> str | None:
    """单条规则 → 检查 SQL；`sql` / `custom` 类型由外部自行执行，返回 None。"""
    _require_dialect(dialect)
    check_type = (rule or {}).get("check_type") or "library"
    if check_type != "library":
        return None

    asset_type = rule.get("asset_type")
    asset_id = rule.get("asset_id")
    _, table, column = parse_asset(asset_type, asset_id)
    definition = rule.get("definition") if isinstance(rule.get("definition"), dict) else {}
    metric = definition.get("metric")
    if metric not in LIBRARY_METRICS:
        _invalid("library 规则的 metric 不受支持", metric=metric)
    if metric in TABLE_METRICS and asset_type != "table":
        _invalid("该 metric 只适用于 table 资产", metric=metric)
    if metric not in TABLE_METRICS and asset_type != "column":
        _invalid("该 metric 只适用于 column 资产", metric=metric)

    table_sql = quote_path(table, dialect)
    column_sql = quote_ident(column, dialect) if column else ""

    if metric == "rowCount":
        return f"SELECT COUNT(*) FROM {table_sql}"
    if metric == "nullValues":
        return f"SELECT COUNT(*) FROM {table_sql} WHERE {column_sql} IS NULL"
    if metric == "missingValues":
        values = definition.get("missingValues")
        if not isinstance(values, list) or not values:
            _invalid("missingValues 必须是非空字符串数组")
        in_list = ", ".join(_literal(item) for item in values)
        return (
            f"SELECT COUNT(*) FROM {table_sql} "
            f"WHERE {column_sql} IS NULL OR {column_sql} IN ({in_list})"
        )
    if metric in ("duplicateValues", "unique"):
        return (
            f"SELECT COUNT(*) - COUNT(DISTINCT {column_sql}) FROM {table_sql}"
        )
    if metric == "invalidValues":
        pattern = definition.get("pattern")
        if isinstance(pattern, str) and pattern.strip():
            return (
                f"SELECT COUNT(*) FROM {table_sql} "
                f"WHERE NOT ({column_sql} LIKE {_literal(pattern)})"
            )
        values = definition.get("validValues")
        if not isinstance(values, list) or not values:
            _invalid("invalidValues 需要 pattern 或非空 validValues")
        in_list = ", ".join(_literal(item) for item in values)
        return (
            f"SELECT COUNT(*) FROM {table_sql} "
            f"WHERE {column_sql} NOT IN ({in_list})"
        )
    _invalid("library 规则的 metric 不受支持", metric=metric)
    return None


def build_run_sql(rules: list[dict], dialect: str = "tsql") -> list[dict]:
    """一批规则 → `[{rule_id,name,sql}]`；非 library 的 sql 为 None。"""
    return [
        {
            "rule_id": (rule or {}).get("id"),
            "name": (rule or {}).get("name"),
            "sql": build_rule_sql(rule or {}, dialect),
        }
        for rule in (rules or [])
    ]
