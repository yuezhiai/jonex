#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""探查→摘要（S4，DSR-SQL SAA 的窄移植）。

输入仍是编译证据里**已经探到的列样例**，不额外查库；一次 LLM 调用把样例
摘要成「枚举含义 / 单位 / 同义词」，作为 hints 注入。摘要必须落在给定列上，
失败/超时 fail-open 到空字典。
"""
from __future__ import annotations

import asyncio
import json

from jonex_core.common.config import config
from jonex_core.common.ontology_llm import call_llm_with_tools

MAX_COLUMNS = 5
MAX_SUMMARY_CHARS = 240

_SYSTEM = (
    "You summarize database column values for a data question. For each given "
    "column, write one short note (max 20 words) explaining what the stored "
    "values mean: enum meanings, units, or synonyms the question may use. "
    "Only use the provided values; never invent values. Return JSON mapping "
    "column -> summary string."
)


def _norm_values(values) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = str(value or "").strip()
        if not text or text.casefold() in seen:
            continue
        seen.add(text.casefold())
        out.append(text)
    return out


def build_summary_prompt(question: str,
                         values_by_column: dict[str, list[str]]) -> str:
    lines = [f"Question: {question}", "Columns and their sample values:"]
    for column, values in list(values_by_column.items())[:MAX_COLUMNS]:
        lines.append(f"- {column}: {', '.join(_norm_values(values)[:20])}")
    return "\n".join(lines)


def parse_summaries(raw: object,
                    values_by_column: dict[str, list[str]]) -> dict[str, str]:
    by_fold = {str(col).casefold(): str(col) for col in (values_by_column or {})}
    out: dict[str, str] = {}
    if isinstance(raw, dict):
        items = raw.items()
    elif isinstance(raw, list):
        items = [(item.get("column") or item.get("name"), item.get("summary")
                  or item.get("text"))
                 for item in raw if isinstance(item, dict)]
    else:
        items = []
    for column, summary in items:
        canonical = by_fold.get(str(column or "").strip().casefold())
        text = str(summary or "").strip()
        if canonical and text:
            out[canonical] = text[:MAX_SUMMARY_CHARS]
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


async def summarize_columns(question: str,
                            values_by_column: dict[str, list[str]], *,
                            tenant_id: str | None = None,
                            kb_id: str | None = None,
                            user_id: str | None = None,
                            trace_id: str | None = None) -> dict[str, str]:
    """一次调用 → `{column: summary}`；任何失败返回 `{}`。"""
    if not values_by_column:
        return {}
    prompt = build_summary_prompt(question, values_by_column)
    timeout = float(getattr(config, "NL2SQL_PROBE_SUMMARY_TIMEOUT", 8.0) or 8.0)
    if timeout <= 0:
        timeout = 8.0
    try:
        reply = await asyncio.wait_for(
            call_llm_with_tools(
                [{"role": "system", "content": _SYSTEM},
                 {"role": "user", "content": prompt}],
                tools=None, model=None, scene="nl2sql_probe_summary",
                max_tokens=512, tenant_id=tenant_id or "unknown",
                kb_id=kb_id or None, user_id=user_id or None,
                trace_id=trace_id),
            timeout=timeout,
        )
    except Exception:  # noqa: BLE001 —— 摘要是增能，失败退空
        return {}
    return parse_summaries(_extract_json((reply or {}).get("content") or ""),
                           values_by_column)


__all__ = ["build_summary_prompt", "parse_summaries", "summarize_columns",
           "MAX_COLUMNS", "MAX_SUMMARY_CHARS"]
