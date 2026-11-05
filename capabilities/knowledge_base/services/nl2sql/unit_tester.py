#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""可执行断言的单元测试器（CHESS Unit Tester 的窄移植，2026-11-01）。

CHESS 生成 5 条自然语言断言，对候选 SQL 执行并逐条判 Passed/Failed。这里先做
**单候选验证 + 一次修复**（不引入多候选执行链）：

1. 只看问句 + 列清单生成 ≤5 条**结果级**断言（行数、唯一性、空值、列数、范围）；
2. 用真实结果逐条判定；
3. 有失败 → 通过现有 `retry_note` 通道重生成一次。

断言生成**不看候选结果**（否则会写出“结果恰好长这样”的同义反复）；判定才看结果。
任何一步失败都 fail-open，不影响原始答案。
"""
from __future__ import annotations

import asyncio
import json
import re

from jonex_core.common.config import config
from jonex_core.common.ontology_llm import call_llm_with_tools

MAX_ASSERTIONS = 5
MAX_SAMPLE_ROWS = 20
MAX_CELL_CHARS = 80
MAX_RESULT_CHARS = 4000

_ASSERT_SYSTEM = (
    "You write short, objectively checkable assertions about the RESULT of a "
    "text-to-SQL query. Do not describe the SQL. Each assertion must be true "
    "for every correct answer. Focus on: result cardinality, uniqueness of "
    "entity rows, NULLs, column count/position, value ranges or formats. "
    "Return JSON only: {\"assertions\": [\"...\"]} with at most 5 assertions."
)

_EVAL_SYSTEM = (
    "You evaluate whether a query result satisfies each assertion. Judge only "
    "from the provided result sample and the question. Return JSON only: "
    "{\"results\": [{\"id\": 1, \"passed\": true, \"reason\": \"...\"}]} "
    "with one entry per assertion id."
)


def _sample(rows: list[list] | None, columns: list[dict] | None = None) -> str:
    rows = list(rows or [])[:MAX_SAMPLE_ROWS]
    if not rows:
        return "[]"
    names = [str(c.get("name") or c.get("logical_name") or "") for c in (columns or [])]
    lines = []
    if names:
        lines.append("columns: " + ", ".join(names))
    for row in rows:
        cells = [str(v)[:MAX_CELL_CHARS] for v in row]
        lines.append("(" + ", ".join(cells) + ")")
    text = "\n".join(lines)
    return text[:MAX_RESULT_CHARS]


def build_assertion_prompt(question: str, columns: list[dict] | None) -> str:
    names = [
        str(c.get("name") or c.get("logical_name") or "").strip()
        for c in (columns or []) if isinstance(c, dict)
    ]
    names = [n for n in names if n][:80]
    return (
        f"Question: {question}\n"
        f"Available columns: {', '.join(names) if names else '(unknown)'}\n"
        "Write at most 5 result-level assertions."
    )


def build_eval_prompt(question: str, assertions: list[str], sql: str,
                      rows: list[list] | None,
                      columns: list[dict] | None = None) -> str:
    lines = [f"Question: {question}", f"SQL: {sql}", "Assertions:"]
    for i, assertion in enumerate(assertions, 1):
        lines.append(f"{i}. {assertion}")
    lines += ["Result sample:", _sample(rows, columns)]
    return "\n".join(lines)


def _json_blob(raw: object) -> object:
    # `call_llm_with_tools` 回的是 `{"content": "...", ...}`，不是裸字符串。
    if isinstance(raw, dict) and "content" in raw:
        raw = raw.get("content")
    if isinstance(raw, str):
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                return None
    return raw


def parse_assertions(raw: object) -> list[str]:
    blob = _json_blob(raw)
    if not isinstance(blob, dict):
        return []
    items = blob.get("assertions")
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        text = str(item or "").strip()
        if text:
            out.append(text[:300])
    return out[:MAX_ASSERTIONS]


def parse_evaluation(raw: object, count: int) -> list[dict]:
    """返回 `[{id, passed, reason}]`；解析不了返回空列表（调用方 fail-open）。"""
    blob = _json_blob(raw)
    if not isinstance(blob, dict):
        return []
    out = []
    for item in blob.get("results") or []:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if not 1 <= idx <= count:
            continue
        out.append({
            "id": idx,
            "passed": bool(item.get("passed")),
            "reason": str(item.get("reason") or "")[:200],
        })
    return out


def failed_assertions(assertions: list[str], evaluation: list[dict]) -> list[str]:
    by_id = {e["id"]: e for e in evaluation}
    failed = []
    for i, assertion in enumerate(assertions, 1):
        item = by_id.get(i)
        if item is None or not item["passed"]:
            reason = (item or {}).get("reason") or "not verified"
            failed.append(f"{assertion} ({reason})")
    return failed


def build_repair_note(failed: list[str]) -> str:
    lines = [
        "Your SQL returned a result that failed these result-level checks:",
        *[f"- {item}" for item in failed[:MAX_ASSERTIONS]],
        "Rewrite the SQL so the result satisfies them. Do not change the "
        "question's intent; fix row grain, DISTINCT/aggregation, filters, or "
        "projection as needed.",
    ]
    return "\n".join(lines)


async def generate_assertions(question: str, columns: list[dict] | None, *,
                              tenant_id: str | None = None,
                              kb_id: str | None = None,
                              user_id: str | None = None) -> list[str]:
    timeout = float(getattr(config, "NL2SQL_UNIT_TEST_TIMEOUT", 15.0) or 15.0)
    if timeout <= 0:
        return []
    try:
        reply = await asyncio.wait_for(
            call_llm_with_tools(
                [{"role": "system", "content": _ASSERT_SYSTEM},
                 {"role": "user", "content": build_assertion_prompt(question, columns)}],
                tools=None, model=None, scene="nl2sql_unit_test",
                max_tokens=512, tenant_id=tenant_id or "unknown",
                kb_id=kb_id or None, user_id=user_id or None,
            ),
            timeout=timeout,
        )
        return parse_assertions(reply)
    except Exception:  # noqa: BLE001 —— 增能项，任何失败都跳过
        return []


async def evaluate_assertions(question: str, assertions: list[str], sql: str,
                              rows: list[list] | None, columns: list[dict] | None, *,
                              tenant_id: str | None = None,
                              kb_id: str | None = None,
                              user_id: str | None = None) -> list[dict]:
    if not assertions:
        return []
    timeout = float(getattr(config, "NL2SQL_UNIT_TEST_TIMEOUT", 15.0) or 15.0)
    if timeout <= 0:
        return []
    try:
        reply = await asyncio.wait_for(
            call_llm_with_tools(
                [{"role": "system", "content": _EVAL_SYSTEM},
                 {"role": "user", "content": build_eval_prompt(
                     question, assertions, sql, rows, columns)}],
                tools=None, model=None, scene="nl2sql_unit_test",
                max_tokens=512, tenant_id=tenant_id or "unknown",
                kb_id=kb_id or None, user_id=user_id or None,
            ),
            timeout=timeout,
        )
        return parse_evaluation(reply, len(assertions))
    except Exception:  # noqa: BLE001 —— 增能项，任何失败都跳过
        return []


__all__ = [
    "MAX_ASSERTIONS", "MAX_SAMPLE_ROWS", "MAX_CELL_CHARS", "MAX_RESULT_CHARS",
    "build_assertion_prompt", "build_eval_prompt", "parse_assertions",
    "parse_evaluation", "failed_assertions", "build_repair_note",
    "generate_assertions", "evaluate_assertions",
]
