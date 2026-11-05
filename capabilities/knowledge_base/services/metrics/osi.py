# -*- coding: utf-8 -*-
"""OSI / Apache Ossie 指标导入导出（W3.3 M2）。

底本（`ossie-main/core-spec`）：OSI `Metric` 只有
`name / expression{dialects[]} / description / datatype / ai_context / custom_extensions`，
**没有** metric_type、两层结构、时间语义与可加性。因此：

- 可编译表达式 → `expression.dialects`（`ANSI_SQL`）；
- Jonex 语义 → `custom_extensions[vendor_name="JONEX"]`（`data` 是 JSON 字符串）；
- 导入只回导 JONEX 扩展；纯 OSI 表达式**不猜语义**（spec D5），只作为
  口径卡的 `formula_text` 供人阅读，绝不执行。

CUMULATIVE 导出为「输入指标的基础聚合表达式 + 扩展里的时间语义」，并带
`cumulative_semantics_loss`：第三方丢掉扩展时它会退化成基础指标（与
MetricFlow `msi_to_osi` 的行为同构）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from .compiler import MetricCompileError, value_expression
from .executable import (
    ADDITIVITY_VALUES,
    normalize_executable,
    validate_executable,
)

_EXTENSION_VENDOR = "JONEX"
_EXTENSION_SCHEMA = "jonex.metric.v1"


@dataclass(frozen=True)
class OsiExport:
    metric: dict
    issues: tuple[str, ...]


@dataclass(frozen=True)
class OsiImport:
    definition: dict
    warnings: tuple[str, ...]


def export_metric(
    definition: dict,
    *,
    scope: dict[str, dict],
    compiled_row: dict | None,
    data_source_id: str,
    alias: str | None = None,
) -> OsiExport:
    """抽象指标 → OSI metric dict（含 JONEX 扩展，可无损回导）。"""
    executable = definition.get("executable")
    if not isinstance(executable, dict):
        raise MetricCompileError("metric_not_executable", "指标没有可执行定义")
    name = str(alias or definition.get("name") or "").strip()
    if not name:
        raise MetricCompileError("metric_invalid", "指标缺少 name，不能导出")
    executable = normalize_executable(executable)
    issues = validate_executable(executable, additivity=definition.get("additivity"))
    if issues:
        raise MetricCompileError("metric_invalid", "；".join(issues[:3]))

    expression = value_expression(
        definition, scope=scope, compiled_row=compiled_row,
        data_source_id=data_source_id,
    )
    metric: dict = {
        "name": name,
        "expression": {"dialects": [
            {"dialect": "ANSI_SQL", "expression": expression}]},
    }
    description = definition.get("description")
    if isinstance(description, str) and description.strip():
        metric["description"] = description.strip()
    aliases = [str(item).strip() for item in definition.get("aliases") or []
               if isinstance(item, str) and item.strip()]
    if aliases:
        metric["ai_context"] = {"synonyms": aliases}

    extension = {
        "schema": _EXTENSION_SCHEMA,
        "executable": executable,
        "additivity": definition.get("additivity") or "unknown",
        "landings": definition.get("landings") or [],
    }
    metric["custom_extensions"] = [{
        "vendor_name": _EXTENSION_VENDOR,
        "data": json.dumps(extension, ensure_ascii=False, sort_keys=True),
    }]
    loss = ()
    if executable["metric_type"] == "cumulative":
        loss = ("cumulative_semantics_loss",)
    return OsiExport(metric=metric, issues=loss)


def import_metric(raw: dict) -> OsiImport:
    """OSI metric dict → 抽象指标片段（不可执行的纯表达式只留 `formula_text`）。"""
    if not isinstance(raw, dict):
        raise ValueError("OSI 指标必须是对象")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("OSI 指标缺少 name")
    definition: dict = {"name": name.strip()}
    description = raw.get("description")
    if isinstance(description, str) and description.strip():
        definition["description"] = description.strip()
    synonyms = _synonyms(raw)
    if synonyms:
        definition["aliases"] = synonyms
    expression = _ansi_expression(raw)

    warnings: list[str] = []
    extension = _jonex_extension(raw)
    if extension is not None:
        data = _load_extension(extension)
        if not isinstance(data, dict) or data.get("schema") != _EXTENSION_SCHEMA:
            warnings.append("unsupported_extension_schema")
        else:
            executable = data.get("executable")
            issues = (validate_executable(
                executable, additivity=data.get("additivity"))
                if isinstance(executable, dict)
                else ["executable 必须是对象"])
            if issues:
                warnings.append("invalid_extension")
            else:
                definition["executable"] = normalize_executable(executable)
                additivity = data.get("additivity")
                if additivity in ADDITIVITY_VALUES:
                    definition["additivity"] = additivity
                landings = [item for item in data.get("landings") or []
                            if isinstance(item, dict)]
                if landings:
                    definition["landings"] = landings

    if "executable" not in definition and expression:
        # 纯 OSI 表达式：只供口径卡 `formula` 阅读，标注不可执行（D5）。
        definition["formula_text"] = expression
        warnings.append("unparsed_expression")
    return OsiImport(definition=definition, warnings=tuple(warnings))


def _synonyms(raw: dict) -> list[str]:
    ai_context = raw.get("ai_context")
    if not isinstance(ai_context, dict):
        return []
    return [str(item).strip() for item in ai_context.get("synonyms") or []
            if isinstance(item, str) and item.strip()]


def _ansi_expression(raw: dict) -> str | None:
    expression = raw.get("expression")
    if not isinstance(expression, dict):
        return None
    dialects = [item for item in expression.get("dialects") or []
                if isinstance(item, dict)]
    chosen = next((item for item in dialects
                   if str(item.get("dialect") or "").upper() == "ANSI_SQL"), None)
    if chosen is None and dialects:
        chosen = dialects[0]
    if chosen is None:
        return None
    value = chosen.get("expression")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _jonex_extension(raw: dict) -> dict | None:
    extensions = raw.get("custom_extensions")
    if not isinstance(extensions, list):
        return None
    for item in extensions:
        if isinstance(item, dict) and str(item.get("vendor_name") or "") == _EXTENSION_VENDOR:
            return item
    return None


def _load_extension(extension: dict):
    data = extension.get("data")
    if isinstance(data, dict):
        return data
    if not isinstance(data, str):
        return None
    try:
        return json.loads(data)
    except ValueError:
        return None


__all__ = ["OsiExport", "OsiImport", "export_metric", "import_metric"]
