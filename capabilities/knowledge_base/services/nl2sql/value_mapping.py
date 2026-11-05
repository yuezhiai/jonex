#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""语义值映射（S2）：把问题里的叫法映射到列里真实存在的值。

只吃**已存证据里的列样例**（`columns[].samples`），不额外查库：样例本来就是
从枚举列里探出来的真实值。一次 LLM 调用，输出必须经列字典校验 —— 模型编造
的值一律丢弃；失败/超时 fail-open 到空映射。
"""
from __future__ import annotations

import asyncio
import json

from jonex_core.common.config import config
from jonex_core.common.ontology_llm import call_llm_with_tools

MAX_COLUMNS = 5
MAX_VALUES_PER_COLUMN = 20

_SYSTEM = (
    "You map the values mentioned in a question to the exact stored values of "
    "database columns. You are given columns and their real stored values. "
    "Return only mappings where the question clearly refers to one of those "
    "stored values. Copy the stored value EXACTLY; never invent a value. "
    "Return JSON: {\"mapping\": {\"<column>\": [\"<stored value>\"]}}."
)


def _norm_values(values) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = str(value or "").strip()
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def build_mapping_prompt(question: str, values_by_column: dict[str, list[str]],
                         hints: list[str] | None = None) -> str:
    lines = [f"Question: {question}", "Columns and their real stored values:"]
    for column, values in list(values_by_column.items())[:MAX_COLUMNS]:
        vals = ", ".join(_norm_values(values)[:MAX_VALUES_PER_COLUMN])
        lines.append(f"- {column}: {vals}")
    if hints:
        lines.append("Reference knowledge: " + " | ".join(hints[:8]))
    return "\n".join(lines)


def _iter_mapping(raw: object):
    if isinstance(raw, dict):
        mapping = raw.get("mapping")
        if isinstance(mapping, dict):
            yield from mapping.items()
            return
        if isinstance(raw.get("columns"), dict):
            yield from raw["columns"].items()
            return
        yield from raw.items()
        return
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                column = item.get("column") or item.get("name")
                if column:
                    yield column, item.get("values") or item.get("value")


def parse_mapping(raw: object,
                  values_by_column: dict[str, list[str]]) -> dict[str, list[str]]:
    """只保留**确实存在于该列字典**的值（大小写不敏感匹配后回填原值）。"""
    by_fold = {str(col).casefold(): (str(col), _norm_values(vals))
               for col, vals in (values_by_column or {}).items()}
    out: dict[str, list[str]] = {}
    for column, raw_values in _iter_mapping(raw):
        key = str(column or "").strip().casefold()
        if key not in by_fold:
            continue
        canonical, stored = by_fold[key]
        candidates = raw_values if isinstance(raw_values, (list, tuple)) else [raw_values]
        picked: list[str] = []
        for candidate in candidates:
            text = str(candidate or "").strip().casefold()
            for value in stored:
                if value.casefold() == text and value not in picked:
                    picked.append(value)
                    break
        if picked:
            out.setdefault(canonical, []).extend(
                v for v in picked if v not in out.get(canonical, []))
    return out


def _extract_json(content: str) -> object:
    text = str(content or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


async def map_question_values(question: str,
                              values_by_column: dict[str, list[str]], *,
                              hints: list[str] | None = None,
                              tenant_id: str | None = None,
                              kb_id: str | None = None,
                              user_id: str | None = None,
                              trace_id: str | None = None) -> dict[str, list[str]]:
    """一次调用 → `{column: [exact stored values]}`；任何失败返回 `{}`。"""
    if not values_by_column:
        return {}
    prompt = build_mapping_prompt(question, values_by_column, hints=hints)
    timeout = float(getattr(config, "NL2SQL_VALUE_MAPPING_TIMEOUT", 8.0) or 8.0)
    if timeout <= 0:
        timeout = 8.0
    try:
        reply = await asyncio.wait_for(
            call_llm_with_tools(
                [{"role": "system", "content": _SYSTEM},
                 {"role": "user", "content": prompt}],
                tools=None, model=None, scene="nl2sql_value_map", max_tokens=512,
                tenant_id=tenant_id or "unknown", kb_id=kb_id or None,
                user_id=user_id or None, trace_id=trace_id),
            timeout=timeout,
        )
    except Exception:  # noqa: BLE001 —— 值映射是增能，失败退空
        return {}
    return parse_mapping(_extract_json((reply or {}).get("content") or ""),
                         values_by_column)


__all__ = ["build_mapping_prompt", "parse_mapping", "map_question_values",
           "MAX_COLUMNS", "MAX_VALUES_PER_COLUMN"]
