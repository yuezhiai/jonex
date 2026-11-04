"""W2.5 质量规则字段校验（纯函数，统一 1001）。"""
from __future__ import annotations

from typing import Any

from capabilities.data_governance.asset_identity import split_asset_id
from jonex_core.common.exceptions import InvalidParameterError

ASSET_TYPES = frozenset({"table", "column"})
DIMENSIONS = frozenset({
    "completeness", "uniqueness", "validity", "consistency", "timeliness",
})
SEVERITIES = frozenset({"info", "warning", "error", "critical"})
CHECK_TYPES = frozenset({"library", "sql", "custom"})
LIBRARY_METRICS = frozenset({
    "rowCount", "nullValues", "missingValues", "duplicateValues",
    "invalidValues", "unique",
})
TABLE_METRICS = frozenset({"rowCount"})
SQL_MAX_CHARS = 16 * 1024

THRESHOLD_OPERATORS = frozenset({
    "mustBe", "mustNotBe", "mustBeGreaterThan", "mustBeGreaterOrEqualTo",
    "mustBeLessThan", "mustBeLessOrEqualTo", "mustBeBetween", "mustNotBeBetween",
})
BETWEEN_OPERATORS = frozenset({"mustBeBetween", "mustNotBeBetween"})


def _invalid(message: str, **details) -> None:
    raise InvalidParameterError(message=message, details=details or None)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def parse_asset(asset_type: str, asset_id: str) -> tuple[str, str, str | None]:
    """解析规则资产 → `(data_source_id, table, column|None)`；非法 1001。"""
    if asset_type not in ASSET_TYPES:
        _invalid("asset_type 必须是 table 或 column", asset_type=asset_type)
    try:
        scope, table, column = split_asset_id(asset_type, asset_id)
    except InvalidParameterError as exc:
        _invalid("asset_id 格式非法", asset_type=asset_type, asset_id=asset_id,
                 cause=exc.message)
    if not scope or not table:
        _invalid("asset_id 必须包含数据源与表名", asset_id=asset_id)
    if asset_type == "column" and not column:
        _invalid("column 规则必须包含列名", asset_id=asset_id)
    return scope, table, (column or None)


def validate_threshold(threshold: Any) -> None:
    """threshold 必须且只能有一个比较运算符（Between 为两个有序数字）。"""
    if not isinstance(threshold, dict) or len(threshold) != 1:
        _invalid("threshold 必须包含且只包含一个比较运算符", threshold=threshold)
    operator, value = next(iter(threshold.items()))
    if operator not in THRESHOLD_OPERATORS:
        _invalid("threshold 比较运算符不受支持", operator=operator)
    if operator in BETWEEN_OPERATORS:
        if (
            not isinstance(value, (list, tuple))
            or len(value) != 2
            or not all(_is_number(item) for item in value)
        ):
            _invalid("Between 运算符需要两个数字", operator=operator, value=value)
        if value[0] > value[1]:
            _invalid("Between 的下界不能大于上界", operator=operator, value=value)
        return
    if not _is_number(value):
        _invalid("threshold 期望值必须是数字", operator=operator, value=value)


def validate_rule_fields(
    *,
    name: Any,
    asset_type: Any,
    asset_id: Any,
    dimension: Any,
    severity: Any,
    check_type: Any,
    definition: Any,
    threshold: Any,
) -> None:
    """创建/编辑共用：公共字段 + check_type 专属参数 + threshold。"""
    text = _text(name)
    if not text:
        _invalid("规则名称不能为空")
    if len(text) > 255:
        _invalid("规则名称不能超过 255 字符")
    if dimension not in DIMENSIONS:
        _invalid("dimension 不受支持", dimension=dimension)
    if severity not in SEVERITIES:
        _invalid("severity 不受支持", severity=severity)
    if check_type not in CHECK_TYPES:
        _invalid("check_type 不受支持", check_type=check_type)
    parse_asset(asset_type, asset_id)
    if isinstance(asset_id, str) and len(asset_id.strip()) > 255:
        _invalid("asset_id 不能超过 255 字符")
    if not isinstance(definition, dict):
        _invalid("definition 必须是对象")
    validate_threshold(threshold)

    if check_type == "library":
        metric = definition.get("metric")
        if metric not in LIBRARY_METRICS:
            _invalid("library 规则的 metric 不受支持", metric=metric)
        if metric in TABLE_METRICS and asset_type != "table":
            _invalid("该 metric 只适用于 table 资产", metric=metric)
        if metric not in TABLE_METRICS and asset_type != "column":
            _invalid("该 metric 只适用于 column 资产", metric=metric)
        if metric == "missingValues":
            values = definition.get("missingValues")
            if (
                not isinstance(values, list)
                or not values
                or not all(isinstance(item, str) for item in values)
            ):
                _invalid("missingValues 必须是非空字符串数组")
        if metric == "invalidValues":
            pattern = definition.get("pattern")
            valid_values = definition.get("validValues")
            has_pattern = isinstance(pattern, str) and bool(pattern.strip())
            has_values = (
                isinstance(valid_values, list)
                and bool(valid_values)
                and all(isinstance(item, str) for item in valid_values)
            )
            if not has_pattern and not has_values:
                _invalid("invalidValues 需要 pattern 或非空 validValues")
        return

    if check_type == "sql":
        query = definition.get("query")
        if not isinstance(query, str) or not query.strip():
            _invalid("sql 规则需要非空 query")
        if len(query) > SQL_MAX_CHARS:
            _invalid("sql query 过大（上限 16KB）")
        return

    engine = definition.get("engine")
    implementation = definition.get("implementation")
    if not isinstance(engine, str) or not engine.strip():
        _invalid("custom 规则需要 engine")
    if not isinstance(implementation, str) or not implementation.strip():
        _invalid("custom 规则需要 implementation")
