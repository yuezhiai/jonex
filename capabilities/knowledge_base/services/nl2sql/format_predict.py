#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""生成前格式预测（S1，ReFoRCE `get_format_prompt` 的窄移植）。

只回答一件事：这道题**应该返回哪些列 / 什么形状**。结果只作为生成提示
（payload `answer_format` + 提示词规则 4u），不参与安全判定 —— 模型编造的
列名不会让 guard/执行层放行不存在的列。

一次 LLM 调用，失败/超时 fail-open 到空列表：没有预测时生成请求与引入前
逐字节相同。
"""
from __future__ import annotations

import asyncio
import json

from jonex_core.common.config import config
from jonex_core.common.ontology_llm import call_llm_with_tools

MAX_COLUMNS = 5
MAX_CHARS = 40

_SYSTEM = (
    "You predict the output shape of a SQL answer. Given a question and the "
    "available columns, return ONLY the columns the question asks for, in the "
    "order they should appear. A single-value aggregate question returns one "
    "column. Never add descriptive columns, ids next to names, or columns the "
    "question does not ask for. Return JSON: {\"columns\": [\"...\"]}."
)


def sanitize_format(raw: object, *, max_columns: int = MAX_COLUMNS,
                    max_chars: int = MAX_CHARS) -> list[str]:
    """把模型输出收成有界、去重、非空的列名列表。"""
    if isinstance(raw, dict):
        raw = raw.get("columns") or raw.get("answer_format") or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        text = item.strip().strip("`\"'").strip()
        if not text:
            continue
        text = text[:max_chars]
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= max_columns:
            break
    return out


def build_format_prompt(question: str, *, table_name: str,
                        columns: list[dict],
                        joins: list[dict] | None = None,
                        hints: list[str] | None = None) -> str:
    """问题 + 紧凑列清单（纯函数，便于单测）。"""
    lines = [f"Question: {question}", f"Main table: {table_name}"]
    if columns:
        cols = ", ".join(
            f"{c.get('name')} ({c.get('type') or '?'})" for c in columns[:80])
        lines.append(f"Columns: {cols}")
    if joins:
        edges = "; ".join(
            f"{j.get('from_table')}.{j.get('from_column')} -> "
            f"{j.get('to_table')}.{j.get('to_column')}" for j in joins[:12])
        lines.append(f"Join edges: {edges}")
    if hints:
        lines.append("Reference knowledge: " + " | ".join(hints[:8]))
    return "\n".join(lines)


def _parse_columns(content: str) -> list[str]:
    text = str(content or "").strip()
    if not text:
        return []
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return sanitize_format(json.loads(text[start:end + 1]))
        except json.JSONDecodeError:
            pass
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        try:
            return sanitize_format(json.loads(text[start:end + 1]))
        except json.JSONDecodeError:
            pass
    return []


async def predict_format(question: str, *, engine: str, table_name: str,
                         columns: list[dict],
                         joins: list[dict] | None = None,
                         hints: list[str] | None = None,
                         tenant_id: str | None = None,
                         kb_id: str | None = None,
                         user_id: str | None = None,
                         trace_id: str | None = None) -> list[str]:
    """一次调用 → 应返回的列名；任何失败返回 `[]`。"""
    prompt = build_format_prompt(
        question, table_name=table_name, columns=columns, joins=joins, hints=hints)
    timeout = float(getattr(config, "NL2SQL_FORMAT_TIMEOUT", 10.0) or 10.0)
    if timeout <= 0:
        timeout = 10.0
    try:
        reply = await asyncio.wait_for(
            call_llm_with_tools(
                [{"role": "system", "content": _SYSTEM},
                 {"role": "user", "content": prompt}],
                tools=None, model=None, scene="nl2sql_format", max_tokens=256,
                tenant_id=tenant_id or "unknown", kb_id=kb_id or None,
                user_id=user_id or None, trace_id=trace_id),
            timeout=timeout,
        )
    except Exception:  # noqa: BLE001 —— 格式预测是增能，失败退空
        return []
    content = str((reply or {}).get("content") or "").strip()
    return _parse_columns(content)


__all__ = ["sanitize_format", "build_format_prompt", "predict_format",
           "MAX_COLUMNS", "MAX_CHARS"]
