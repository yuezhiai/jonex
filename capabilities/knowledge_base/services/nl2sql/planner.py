#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""P2-1 planner：生成 SQL 前的一次结构化分析（不写 SQL）。

对应 AV-SQL 的 planner agent：先定 join 路径、隐藏公式、投影、单行/多行、
DISTINCT、排序限制，再让 SQL 生成器按计划写。BIRD 消融里 planner 对弱模型
+2.2pp、对强模型 ≈0；我们放在值接地/evidence 之后作为 P2 试验项。

只做一次 LLM 调用，失败 fail-open（返回空 plan，生成与引入前逐字节相同）。
"""
from __future__ import annotations

import json

from jonex_core.common.ontology_llm import call_llm_with_tools

MAX_PLAN_CHARS = 1200

_SYSTEM = (
    "You are a senior data analyst. Given a question and a compact database schema, "
    "write a short SQL plan. Do NOT write SQL. Cover: relevant tables/columns; "
    "join path; hidden formulas/derived metrics; filters and value semantics; "
    "whether the answer is one row (aggregate) or many rows; DISTINCT; sort/limit. "
    "Use only the given tables/columns. Be concise (<= 200 words)."
)


def build_plan_text(question: str, *, table_name: str, columns: list[dict],
                    joins: list[dict] | None = None,
                    related_tables: list[dict] | None = None,
                    constraints: list[dict] | None = None,
                    hints: list[str] | None = None,
                    today: str | None = None) -> str:
    """把生成用的上下文压成 planner 的 user prompt（纯函数，便于单测）。"""
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
    if related_tables:
        rel = "; ".join(str(t.get("table") or "") for t in related_tables[:12])
        lines.append(f"Related tables: {rel}")
    if constraints:
        rules = " | ".join(str(c.get("expression") or "") for c in constraints[:8])
        lines.append(f"Business rules: {rules}")
    if hints:
        lines.append("Reference knowledge: " + " | ".join(hints[:8]))
    if today:
        lines.append(f"Today: {today}")
    return "\n".join(lines)


async def plan_sql(question: str, *, table_name: str, columns: list[dict],
                   joins: list[dict] | None = None,
                   related_tables: list[dict] | None = None,
                   constraints: list[dict] | None = None,
                   hints: list[str] | None = None, today: str | None = None,
                   tenant_id: str = "", kb_id: str = "",
                   user_id: str = "") -> str:
    """一次 planner 调用 → 计划文本；任何失败返回 `""`。"""
    prompt = build_plan_text(
        question, table_name=table_name, columns=columns, joins=joins,
        related_tables=related_tables, constraints=constraints, hints=hints,
        today=today)
    try:
        reply = await call_llm_with_tools(
            [{"role": "system", "content": _SYSTEM},
             {"role": "user", "content": prompt}],
            tools=None, model=None, scene="nl2sql_generate", max_tokens=512,
            tenant_id=tenant_id or "unknown", kb_id=kb_id or None,
            user_id=user_id or None)
    except Exception:  # noqa: BLE001 —— planner 是增能，失败退空
        return ""
    content = str((reply or {}).get("content") or "").strip()
    if not content:
        return ""
    # 容忍模型返回 JSON：优先取 plan/plan_text 字段。
    start, end = content.find("{"), content.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(content[start:end + 1])
            if isinstance(data, dict):
                content = str(data.get("plan") or data.get("plan_text")
                              or data.get("analysis") or content).strip()
        except json.JSONDecodeError:
            pass
    return content[:MAX_PLAN_CHARS]


__all__ = ["build_plan_text", "plan_sql", "MAX_PLAN_CHARS"]
