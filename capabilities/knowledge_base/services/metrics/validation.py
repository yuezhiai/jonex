# -*- coding: utf-8 -*-
"""指标定义的作用域校验与执行前 preflight（W3.3 M3）。

与 `executable.py` 的分工：
- `executable.py` 只看**单条定义自身**的形状（枚举/必填/表达式可解析）；
- 本模块看**作用域**：输入指标是否存在、依赖是否成环、CUMULATIVE 的输入是否为
  SIMPLE、跨源落点是否有交集；
- `preflight` 在编译前把两者跑完，**错误全部硬失败** —— 调用方拿到的是
  `MetricCompileError`，绝不会把半合法定义送进执行。

作用域键：抽象 schema 用指标 `name`；口径卡用 `code`（见 spec §4.2）。
`key` 参数就是当前定义在 `scope` 里的键，用于成环判定（定义里的 `name`
可能是展示名，和 scope 键不同）。
"""
from __future__ import annotations

from .compiler import MetricCompileError, compile_metric
from .executable import normalize_executable, validate_executable

_MAX_DEPTH = 8


def validate_metric(definition: dict, *, scope: dict[str, dict],
                    key: str | None = None) -> list[str]:
    """作用域校验；返回字段路径化的中文问题（空 = 通过）。

    没有 `executable` 的 legacy 指标**不算错误**（它只是不可执行），返回空列表；
    要执行时由 `preflight`/编译器报 `metric_not_executable`。
    """
    if not isinstance(definition, dict):
        return ["指标定义必须是对象"]
    executable = definition.get("executable")
    if not isinstance(executable, dict):
        return []
    executable = normalize_executable(executable)
    issues = list(validate_executable(
        executable, additivity=definition.get("additivity")))
    if issues:
        return issues
    issues.extend(_input_issues(executable, scope))
    issues.extend(_cycle_issues(executable, scope, key or definition.get("name")))
    issues.extend(_cross_source_issues(definition, executable, scope))
    return issues


def validate_scope(metrics: dict[str, dict]) -> dict[str, list[str]]:
    """整份作用域校验：`{scope_key: issues}`（只返回有问题的条目）。"""
    out: dict[str, list[str]] = {}
    for key, definition in (metrics or {}).items():
        issues = validate_metric(definition, scope=metrics, key=str(key))
        if issues:
            out[str(key)] = issues
    return out


def preflight(
    definition: dict,
    *,
    scope: dict[str, dict],
    compiled_row: dict | None,
    data_source_id: str,
    key: str | None = None,
    group_by: list[str] | None = None,
    max_rows: int | None = None,
    dialect: str = "postgres",
    time_grain: str | None = None,
    offset: int | None = None,
    time_from: str | None = None,
    time_to: str | None = None,
):
    """校验通过才编译；任何定义错误都在这里硬失败（不进入执行）。"""
    issues = validate_metric(definition, scope=scope, key=key)
    if issues:
        # 保留稳定原因码：影子对比/观测按 code 归因，不能全部折叠成
        # metric_invalid（spec §8 / A10）。
        raise MetricCompileError(_code_for(issues), "；".join(issues[:5]))
    return compile_metric(
        definition, scope=scope, compiled_row=compiled_row,
        data_source_id=data_source_id, group_by=group_by,
        max_rows=max_rows, dialect=dialect, key=key,
        time_grain=time_grain, offset=offset,
        time_from=time_from, time_to=time_to,
    )


def _code_for(issues: list[str]) -> str:
    text = " ".join(issues)
    if "成环" in text:
        return "metric_cycle"
    if "不在作用域内" in text:
        return "metric_unresolved"
    if "共同数据源" in text:
        return "cross_source_unsupported"
    return "metric_invalid"


def _input_issues(executable: dict, scope: dict[str, dict]) -> list[str]:
    issues: list[str] = []
    metric_type = executable["metric_type"]
    for index, item in enumerate(executable.get("inputs") or []):
        name = item.get("name")
        target = (scope or {}).get(name)
        field = f"executable.inputs[{index}].name"
        if not isinstance(target, dict):
            issues.append(f"{field} 不在作用域内：{name}")
            continue
        sub_executable = target.get("executable")
        if not isinstance(sub_executable, dict):
            issues.append(f"{field} 的指标没有可执行定义：{name}")
            continue
        sub_executable = normalize_executable(sub_executable)
        sub_issues = validate_executable(
            sub_executable, additivity=target.get("additivity"))
        if sub_issues:
            issues.append(f"输入指标 {name} 定义非法：" + "；".join(sub_issues[:2]))
            continue
        if metric_type == "cumulative":
            if sub_executable["metric_type"] != "simple":
                issues.append(
                    f"executable.inputs[{index}] 对 cumulative 必须是 simple 指标：{name}")
                continue
            # V5：累计需要时间轴，输入 measure 必须显式声明时间列（不猜）。
            measure = sub_executable.get("measure")
            if not isinstance(measure, dict) or not str(
                    measure.get("time_column") or "").strip():
                issues.append(
                    f"累计指标 {name} 的 measure.time_column 不能为空")
    return issues


def _cycle_issues(executable: dict, scope: dict[str, dict],
                  key: str | None) -> list[str]:
    """从当前指标出发沿输入找环；`key` 是当前指标在 scope 里的键。"""
    if not key:
        return []
    stack = {str(key)}
    problems: list[str] = []

    def walk(name: str, depth: int) -> None:
        if depth > _MAX_DEPTH:
            problems.append("指标依赖层级过深")
            return
        target = (scope or {}).get(name)
        if not isinstance(target, dict):
            return
        sub_executable = target.get("executable")
        if not isinstance(sub_executable, dict):
            return
        # D7 允许脏形状落库（形状透传）；这里必须扛住，不能 500。
        inputs = normalize_executable(sub_executable).get("inputs")
        if not isinstance(inputs, list):
            return
        for item in inputs:
            if not isinstance(item, dict):
                continue
            sub_name = item.get("name")
            if not isinstance(sub_name, str):
                continue
            if sub_name in stack:
                problems.append(f"指标依赖成环：{' → '.join([*stack, sub_name])}")
                return
            stack.add(sub_name)
            walk(sub_name, depth + 1)
            stack.discard(sub_name)

    walk(str(key), 0)
    return problems


def _cross_source_issues(definition: dict, executable: dict,
                         scope: dict[str, dict]) -> list[str]:
    """输入指标的落点必须与主指标有共同 data_source_id（M2 限制）。

    只在**双方都登记了落点**时判定：口径卡没有落点（逻辑定义），跳过。
    """
    own = _landing_sources(definition)
    if not own:
        return []
    for item in executable.get("inputs") or []:
        target = (scope or {}).get(item.get("name"))
        if not isinstance(target, dict):
            continue
        other = _landing_sources(target)
        if other and not (own & other):
            return [f"输入指标 {item.get('name')} 与主指标没有共同数据源"
                    "（M2 不支持跨源指标）"]
    return []


def _landing_sources(definition: dict) -> set[str]:
    return {
        str(item.get("data_source_id")).strip()
        for item in definition.get("landings") or []
        if isinstance(item, dict) and item.get("data_source_id")
    }


__all__ = ["preflight", "validate_metric", "validate_scope"]
