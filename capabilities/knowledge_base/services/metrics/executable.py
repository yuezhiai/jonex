# -*- coding: utf-8 -*-
"""指标可执行定义（W3.3 M1/M2）：归一化 + **结构**校验。

边界（刻意划死）：
- 本模块是纯函数：不读库、不看编译 schema、不判断输入指标是否存在；
- 校验只管**形状**（枚举、必填/禁止项、表达式可解析、长度/体积）；
- 输入存在、依赖成环、列是否存在、方言是否支持，分别在编译器和 M3
  preflight 里判断 —— 那里才有编译 schema / 指标作用域。

为什么「未知键一律拒绝」：`executable` 会被写进发布后不可变的口径卡；
静默丢键会让一张卡看起来有定义、实际少了语义。抽象 schema 的归一化是
另一条口径（形状透传，不校验）—— 见 spec D7。
"""
from __future__ import annotations

import copy
import json

import sqlglot
from sqlglot import exp

#: 四类指标（CONVERSION 明确不做，见 spec / MetricFlow 对照）。
METRIC_TYPES = ("simple", "ratio", "derived", "cumulative")

#: 聚合白名单（对齐 MetricFlow 的 AggregationType，去掉 CONVERSION 相关项）。
AGGREGATIONS = (
    "sum", "count", "count_distinct", "avg", "min", "max", "median", "percentile",
)

#: 时间粒度（window 与 grain_to_date 共用）。
GRANULARITIES = ("day", "week", "month", "quarter", "year")

#: 输入指标角色；RATIO 必须恰好 numerator + denominator 各一。
ROLES = ("numerator", "denominator", "operand")

#: 可加性值域（与 DI `/v1/metrics` 官方值对齐）。
ADDITIVITY_VALUES = ("additive", "semi_additive", "non_additive", "unknown")

_MAX_EXPR_CHARS = 2000
_MAX_EXECUTABLE_BYTES = 8192
_MAX_INPUTS = 20
_MAX_WINDOW_COUNT = 10000

_TOP_KEYS = frozenset({"metric_type", "measure", "inputs", "metric_expr", "time"})
_MEASURE_KEYS = frozenset(
    {"agg", "expr", "filter", "percentile", "time_column", "non_additive_dim"})
_NON_ADDITIVE_KEYS = frozenset({"column", "window", "group_by"})
_INPUT_KEYS = frozenset({"name", "role", "filter"})
_TIME_KEYS = frozenset({"window", "grain_to_date"})
_WINDOW_KEYS = frozenset({"count", "granularity"})


def normalize_executable(value):
    """深拷贝并统一枚举大小写；非 dict / null → None。

    只归一**已知**键的枚举与首尾空白，不删未知键（删键是校验的职责），
    也不物化缺省值 —— 「没写」与「写了 unknown」在语义上不同。
    """
    if not isinstance(value, dict):
        return None
    out = copy.deepcopy(value)

    _lower_text(out, "metric_type")
    _strip_text(out, "metric_expr")

    measure = out.get("measure")
    if isinstance(measure, dict):
        _lower_text(measure, "agg")
        for key in ("expr", "filter", "time_column"):
            _strip_text(measure, key)

    inputs = out.get("inputs")
    if isinstance(inputs, list):
        for item in inputs:
            if not isinstance(item, dict):
                continue
            _strip_text(item, "name")
            _lower_text(item, "role")
            _strip_text(item, "filter")

    time = out.get("time")
    if isinstance(time, dict):
        _lower_text(time, "grain_to_date")
        window = time.get("window")
        if isinstance(window, dict):
            _lower_text(window, "granularity")
    return out


def _strip_text(container: dict, key: str) -> None:
    value = container.get(key)
    if isinstance(value, str):
        container[key] = value.strip()


def _lower_text(container: dict, key: str) -> None:
    value = container.get(key)
    if isinstance(value, str):
        container[key] = value.strip().lower()


def validate_executable(value, *, additivity=None) -> list[str]:
    """结构校验；返回字段路径化的中文问题（空 = 通过）。

    `additivity` 来自宿主的同条目字段（抽象指标 / 口径卡 payload）：它不在
    `executable` 内部，但 `non_additive_dim` 只允许配 `semi_additive`，所以
    必须由调用方带进来。缺省视作 `unknown`（→ 不允许 non_additive_dim）。
    """
    if not isinstance(value, dict):
        return ["executable 必须是对象"]
    norm = normalize_executable(value)
    issues = _unknown_keys(norm, _TOP_KEYS, "executable")

    metric_type = norm.get("metric_type")
    if not isinstance(metric_type, str) or not metric_type:
        issues.append("executable.metric_type 必须是 simple/ratio/derived/cumulative")
        return issues
    if metric_type not in METRIC_TYPES:
        issues.append("executable.metric_type 必须是 simple/ratio/derived/cumulative")
        return issues

    measure = norm.get("measure")
    inputs = norm.get("inputs")
    metric_expr = norm.get("metric_expr")
    time = norm.get("time")

    if metric_type == "simple":
        # 必须是对象：`[]` / `"abc"` / `0` 这类真值会让后续 .get() 抛
        # AttributeError（绕过 MetricCompileError 契约），必须在结构校验就拒绝。
        if not isinstance(measure, dict):
            issues.append("executable.measure 是 simple 指标的必填对象")
        if inputs is not None:
            issues.append("executable.inputs 只能用于 ratio/derived/cumulative")
        if metric_expr is not None:
            issues.append("executable.metric_expr 只能用于 derived")
        if time is not None:
            issues.append("executable.time 只能用于 cumulative")
    elif metric_type == "ratio":
        if measure is not None:
            issues.append("executable.measure 不能出现在 ratio 指标上")
        if metric_expr is not None:
            issues.append("executable.metric_expr 不能出现在 ratio 指标上（比率口径固定）")
        if time is not None:
            issues.append("executable.time 只能用于 cumulative")
        _validate_ratio_inputs(inputs, issues)
    elif metric_type == "derived":
        if measure is not None:
            issues.append("executable.measure 不能出现在 derived 指标上")
        if time is not None:
            issues.append("executable.time 只能用于 cumulative")
        if not isinstance(metric_expr, str) or not metric_expr:
            issues.append("executable.metric_expr 是 derived 指标的必填项")
        _validate_inputs(inputs, issues, require_one=False, operand_only=True)
    else:  # cumulative
        if measure is not None:
            issues.append("executable.measure 不能出现在 cumulative 指标上")
        if metric_expr is not None:
            issues.append("executable.metric_expr 不能出现在 cumulative 指标上")
        _validate_inputs(inputs, issues, require_one=True, operand_only=True)
        _validate_time(time, issues)

    if isinstance(measure, dict):
        _validate_measure(measure, additivity, issues)
    if isinstance(inputs, list):
        for index, item in enumerate(inputs):
            if isinstance(item, dict):
                _validate_input_item(item, index, issues)
    if isinstance(metric_expr, str) and metric_expr:
        _validate_metric_expr(metric_expr, inputs, issues)

    try:
        raw = json.dumps(norm, ensure_ascii=False, sort_keys=True).encode("utf-8")
    except (TypeError, ValueError):
        issues.append("executable 含无法序列化的值")
        return issues
    if len(raw) > _MAX_EXECUTABLE_BYTES:
        issues.append(f"executable 超长（>{_MAX_EXECUTABLE_BYTES} 字节）")
    return issues


def _unknown_keys(container: dict, allowed: frozenset, prefix: str) -> list[str]:
    return [f"{prefix}.unknown:{key}" for key in sorted(set(container) - allowed)]


def _validate_measure(measure: dict, additivity, issues: list[str]) -> None:
    issues.extend(_unknown_keys(measure, _MEASURE_KEYS, "executable.measure"))
    agg = measure.get("agg")
    if not isinstance(agg, str) or agg not in AGGREGATIONS:
        issues.append("executable.measure.agg 不在白名单内："
                      + "/".join(AGGREGATIONS))
        return
    expr = measure.get("expr")
    if agg == "count" and expr in (None, ""):
        pass  # COUNT(*)
    elif not isinstance(expr, str) or not expr:
        issues.append("executable.measure.expr 不能为空（count 除外）")
    else:
        _check_expression(expr, "executable.measure.expr", issues)
        node, _problem = _parse_scalar_expression(
            expr, "executable.measure.expr")
        if agg != "count" and node is not None and node.find(exp.Star):
            # `SUM(*)` 是 PG 语法错，只在执行期炸；结构校验就该拦下。
            issues.append("executable.measure.expr 的 * 只能用于 agg=count")
    if isinstance(measure.get("filter"), str) and measure["filter"]:
        _check_expression(measure["filter"], "executable.measure.filter", issues)
    elif measure.get("filter") is not None:
        issues.append("executable.measure.filter 必须是非空字符串")

    percentile = measure.get("percentile")
    if agg == "percentile":
        if (not isinstance(percentile, (int, float)) or isinstance(percentile, bool)
                or not 0 < float(percentile) < 100):
            issues.append("executable.measure.percentile 必须是 (0,100) 之间的数字")
    elif percentile is not None:
        issues.append("executable.measure.percentile 只允许配 agg=percentile")

    time_column = measure.get("time_column")
    if time_column is not None and (not isinstance(time_column, str) or not time_column):
        issues.append("executable.measure.time_column 必须是非空字符串")

    non_additive = measure.get("non_additive_dim")
    if non_additive is None:
        return
    if not isinstance(non_additive, dict):
        issues.append("executable.measure.non_additive_dim 必须是对象")
        return
    issues.extend(_unknown_keys(non_additive, _NON_ADDITIVE_KEYS,
                                "executable.measure.non_additive_dim"))
    if additivity != "semi_additive":
        issues.append("executable.measure.non_additive_dim 仅在 additivity=semi_additive 时允许")
    if not isinstance(non_additive.get("column"), str) or not non_additive.get("column"):
        issues.append("executable.measure.non_additive_dim.column 不能为空")
    if non_additive.get("window") not in ("min", "max"):
        issues.append("executable.measure.non_additive_dim.window 必须是 min 或 max")
    group_by = non_additive.get("group_by")
    if group_by is not None and not (
            isinstance(group_by, list) and all(isinstance(x, str) and x for x in group_by)):
        issues.append("executable.measure.non_additive_dim.group_by 必须是字符串数组")


def _validate_inputs(inputs, issues: list[str], *, require_one: bool,
                     operand_only: bool) -> None:
    if not isinstance(inputs, list) or not inputs:
        issues.append("executable.inputs 必须是非空数组")
        return
    if len(inputs) > _MAX_INPUTS:
        issues.append(f"executable.inputs 不能超过 {_MAX_INPUTS} 个")
    for index, item in enumerate(inputs):
        if not isinstance(item, dict):
            issues.append(f"executable.inputs[{index}] 必须是对象")
    if require_one and len(inputs) != 1:
        issues.append("executable.inputs 对 cumulative 必须恰好一个")
    names = [item.get("name") for item in inputs if isinstance(item, dict)]
    if len({name for name in names if isinstance(name, str)}) != len(
            [name for name in names if isinstance(name, str)]):
        issues.append("executable.inputs 的指标名不得重复")
    if operand_only:
        for index, item in enumerate(inputs):
            if isinstance(item, dict) and item.get("role") not in (None, "operand"):
                issues.append(
                    f"executable.inputs[{index}].role 只能省略或为 operand")


def _validate_ratio_inputs(inputs, issues: list[str]) -> None:
    if not isinstance(inputs, list) or len(inputs) != 2:
        issues.append("executable.inputs 对 ratio 必须恰好两个"
                      "（numerator 与 denominator 各一个）")
        return
    roles = [item.get("role") for item in inputs if isinstance(item, dict)]
    if sorted(roles) != ["denominator", "numerator"]:
        issues.append("executable.inputs 必须恰好包含 numerator 与 denominator 各一个")
    _validate_inputs(inputs, issues, require_one=False, operand_only=False)


def _validate_input_item(item: dict, index: int, issues: list[str]) -> None:
    issues.extend(_unknown_keys(item, _INPUT_KEYS, f"executable.inputs[{index}]"))
    name = item.get("name")
    if not isinstance(name, str) or not name:
        issues.append(f"executable.inputs[{index}].name 不能为空")
    role = item.get("role")
    if role is not None and role not in ROLES:
        issues.append(f"executable.inputs[{index}].role 必须是 "
                      + "/".join(ROLES))
    filter_sql = item.get("filter")
    if filter_sql is not None:
        if not isinstance(filter_sql, str) or not filter_sql:
            issues.append(f"executable.inputs[{index}].filter 必须是非空字符串")
        else:
            _check_expression(filter_sql, f"executable.inputs[{index}].filter", issues)


def _validate_time(time, issues: list[str]) -> None:
    if not isinstance(time, dict):
        issues.append("executable.time 必须是对象")
        return
    issues.extend(_unknown_keys(time, _TIME_KEYS, "executable.time"))
    window = time.get("window")
    grain = time.get("grain_to_date")
    if (window is None) == (grain is None):
        issues.append("executable.time 必须恰好包含 window 或 grain_to_date 之一")
    if window is not None:
        if not isinstance(window, dict):
            issues.append("executable.time.window 必须是对象")
        else:
            issues.extend(_unknown_keys(window, _WINDOW_KEYS,
                                        "executable.time.window"))
            count = window.get("count")
            if (not isinstance(count, int) or isinstance(count, bool)
                    or not 1 <= count <= _MAX_WINDOW_COUNT):
                issues.append("executable.time.window.count 必须是 "
                              f"1..{_MAX_WINDOW_COUNT} 的整数")
            if window.get("granularity") not in GRANULARITIES:
                issues.append("executable.time.window.granularity 必须是 "
                              + "/".join(GRANULARITIES))
    if grain is not None and grain not in GRANULARITIES:
        issues.append("executable.time.grain_to_date 必须是 "
                      + "/".join(GRANULARITIES))


def _validate_metric_expr(expr: str, inputs, issues: list[str]) -> None:
    if len(expr) > _MAX_EXPR_CHARS:
        issues.append(f"executable.metric_expr 超长（>{_MAX_EXPR_CHARS} 字符）")
        return
    node, problem = _parse_scalar_expression(expr, "executable.metric_expr")
    if problem:
        issues.append(problem)
        return
    names = {
        item.get("name") for item in (inputs or [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    outside = sorted({column.name for column in node.find_all(exp.Column)}
                     - names)
    if outside:
        issues.append("executable.metric_expr 引用了非输入指标："
                      + "、".join(outside))


def _check_expression(expr: str, field: str, issues: list[str]) -> None:
    if len(expr) > _MAX_EXPR_CHARS:
        issues.append(f"{field} 超长（>{_MAX_EXPR_CHARS} 字符）")
        return
    _, problem = _parse_scalar_expression(expr, field)
    if problem:
        issues.append(problem)


def _parse_scalar_expression(expr: str, field: str):
    """解析单条标量表达式；含子查询/表引用 → 问题字符串。

    口径与 `nl2sql.guard` 同源：表达式只允许标识符、字面量、函数与运算符。
    真正执行前编译器还会逐列解析到编译 schema —— 这里只挡「明显不是表达式」
    和「偷偷塞了子查询」。
    """
    try:
        node = sqlglot.parse_one(expr, read="postgres")
    except Exception:  # noqa: BLE001 —— sqlglot 的解析异常类型不对外稳定
        return None, f"{field} 不是合法的 SQL 表达式"
    if node is None:
        return None, f"{field} 不是合法的 SQL 表达式"
    if (isinstance(node, (exp.Subquery, exp.Select))
            or node.find(exp.Select) or node.find(exp.Subquery)
            or node.find(exp.Join) or node.find(exp.Table)):
        return None, f"{field} 只允许标量表达式（不能含子查询或表引用）"
    return node, None


__all__ = [
    "ADDITIVITY_VALUES",
    "AGGREGATIONS",
    "GRANULARITIES",
    "METRIC_TYPES",
    "ROLES",
    "normalize_executable",
    "validate_executable",
]
