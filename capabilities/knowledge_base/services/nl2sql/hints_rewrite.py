#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""P1-2 evidence 改写：把 question + hints 变成显式的四段需求。

对应 AV-SQL 的 rewriter：它先把 question 与 external knowledge 改写成
「输出列 / 过滤条件 / 公式 / 排序限制」，下游再生成 SQL；比把 evidence 原文
直接塞进 prompt 更稳定（尤其 evidence 是长句/多条时）。

只做一次 LLM 调用，**失败 fail-open**：改写不出来就继续用原始 hints。
输出被截成 ≤4 条、每条 ≤500 字，交给 `hints.merge_hints` 合并。
"""
from __future__ import annotations

import json

from jonex_core.common.ontology_llm import call_llm_with_tools

MAX_REWRITE_HINTS = 4
MAX_REWRITE_CHARS = 500

_SYSTEM = (
    "You rewrite a business question and its reference knowledge into an explicit "
    "SQL requirements note. Do NOT write SQL and do NOT invent tables/columns. "
    "Return JSON only with keys: outputs (list of strings), filters (list of strings), "
    "formula (string), sort_limit (string). Keep each value short and literal."
)


def format_rewrite(data: dict | None) -> list[str]:
    """LLM 的 JSON → hints 列表（纯函数；稳定键序、去空、截断）。"""
    if not isinstance(data, dict):
        return []
    out: list[str] = []
    for key, label in (("outputs", "输出列"), ("filters", "过滤条件"),
                       ("formula", "公式"), ("sort_limit", "排序/限制")):
        value = data.get(key)
        if isinstance(value, (list, tuple)):
            text = "；".join(str(v).strip() for v in value if str(v).strip())
        else:
            text = str(value or "").strip()
        if not text:
            continue
        text = f"{label}：{text}"
        out.append(text[:MAX_REWRITE_CHARS])
        if len(out) >= MAX_REWRITE_HINTS:
            break
    return out


def _parse_content(content: str) -> list[str]:
    text = str(content or "").strip()
    if not text:
        return []
    # 容忍 ```json fences 与前后说明：取第一个 { 到最后一个 }。
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return format_rewrite(json.loads(text[start:end + 1]))
        except json.JSONDecodeError:
            pass
    return [text[:MAX_REWRITE_CHARS]]


async def rewrite_evidence_hints(question: str, hints: list[str], *, tenant_id: str,
                                 kb_id: str = "", user_id: str = "") -> list[str]:
    """一次 LLM 调用 → 四段式 hints；任何失败返回 `[]`（fail-open）。"""
    hints = [str(h).strip() for h in (hints or []) if str(h).strip()]
    if not hints:
        return []
    user = (
        f"Question:\n{question}\n\n"
        f"Reference knowledge:\n- " + "\n- ".join(hints[:10]) +
        "\n\nReturn the JSON requirements note."
    )
    try:
        reply = await call_llm_with_tools(
            [{"role": "system", "content": _SYSTEM},
             {"role": "user", "content": user}],
            tools=None, model=None, scene="nl2sql_generate", max_tokens=512,
            tenant_id=tenant_id, kb_id=kb_id or None, user_id=user_id or None)
    except Exception:  # noqa: BLE001 —— 改写是增能，失败退回原 hints
        return []
    return _parse_content((reply or {}).get("content") or "")


__all__ = ["format_rewrite", "rewrite_evidence_hints", "MAX_REWRITE_HINTS"]
