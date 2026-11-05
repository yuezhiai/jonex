#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""候选生成 + 成对选择（S3，OpenSQL 选择器的窄移植）。

只对**难例**（有 join 或问句较长）多生成一条候选，再让一次 LLM 调用比较两条
SQL 的语义，选一条交给现有 guard/治理/执行链路。选择失败、超时、非法下标一律
回退 baseline（第 1 条）—— 候选是增能，不能把「没有候选」变成新的失败出口。

不做执行结果去重/投票（那需要把两条 SQL 都执行一遍；成本 2× 且要复用整条
治理链，留作后续）。
"""
from __future__ import annotations

import asyncio
import json
import re

from jonex_core.common.config import config
from jonex_core.common.ontology_llm import call_llm_with_tools

MIN_WORDS = 14
#: 窄触发（2026-10-30 复测）：问句达到这个长度就不再要求 join/cue，直接多生成一条。
WIDE_WORDS = 22

_COMPLEX_CUE = re.compile(
    r"\b(highest|lowest|oldest|youngest|largest|smallest|earliest|latest|"
    r"maximum|minimum|most|least|best|worst|fastest|slowest|heaviest|"
    r"lightest|peak|majority|top|rank|older|younger|higher|lower|larger|"
    r"smaller|earlier|later|compare|compared|difference|respectively)\b",
    re.IGNORECASE)

_SYSTEM = (
    "You compare two SQL candidates for the same question. Decide which one "
    "answers the question more faithfully: correct tables/joins, correct "
    "output columns, filters, aggregation, and row limit. Favor the candidate "
    "whose result shape matches what the question asks for. Return JSON: "
    "{\"choice\": 1 or 2, \"reason\": \"<= 30 words\"}."
)


def should_generate_candidate(question: str, joins: list[dict] | None = None,
                              *, enabled: int = 0, narrow: bool = False) -> bool:
    """只在开关开、且题目够难时多生成一条。

    `narrow=False`（默认）保留旧宽触发：有 join **或** 问句 ≥14 词。
    `narrow=True`（2026-10-30 复测）收窄到真正需要多候选的难例：
    问句 ≥22 词，或「有 join 且 ≥14 词且带最高级/比较线索」——后者是 S3 首轮
    正收益题（cal_12/36、fin_95）的共同特征；单纯有 join 不再触发。
    """
    if int(enabled or 0) < 2:
        return False
    words = re.findall(r"[A-Za-z0-9_]+", str(question or ""))
    if narrow:
        if len(words) >= WIDE_WORDS:
            return True
        return (bool(joins) and len(words) >= MIN_WORDS
                and bool(_COMPLEX_CUE.search(str(question or ""))))
    if joins:
        return True
    return len(words) >= MIN_WORDS


def build_select_prompt(question: str, candidates: list[dict]) -> str:
    lines = [f"Question: {question}", "Candidates:"]
    for i, candidate in enumerate(candidates, 1):
        lines.append(f"{i}. {str(candidate.get('sql') or '').strip()}")
    return "\n".join(lines)


def parse_choice(raw: object, count: int) -> int:
    """模型选择 → 0-based 下标；非法/越界/非数字回 0（baseline）。"""
    if isinstance(raw, str):
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end > start:
            try:
                raw = json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                return 0
    if not isinstance(raw, dict):
        return 0
    try:
        choice = int(raw.get("choice"))
    except (TypeError, ValueError):
        return 0
    return choice - 1 if 1 <= choice <= count else 0


def _reason(raw: object) -> str:
    if isinstance(raw, str):
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end > start:
            try:
                raw = json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                return ""
    if isinstance(raw, dict):
        return str(raw.get("reason") or "")[:200]
    return ""


async def select_candidate(question: str, candidates: list[dict], *,
                           tenant_id: str | None = None,
                           kb_id: str | None = None,
                           user_id: str | None = None,
                           trace_id: str | None = None) -> dict:
    """一次 LLM 调用 → `{"index": 0-based, "reason": str}`；任何失败回 0。"""
    if len(candidates) < 2:
        return {"index": 0, "reason": ""}
    prompt = build_select_prompt(question, candidates)
    timeout = float(getattr(config, "NL2SQL_CANDIDATE_TIMEOUT", 15.0) or 15.0)
    if timeout <= 0:
        timeout = 15.0
    try:
        reply = await asyncio.wait_for(
            call_llm_with_tools(
                [{"role": "system", "content": _SYSTEM},
                 {"role": "user", "content": prompt}],
                tools=None, model=None, scene="nl2sql_candidate_select",
                max_tokens=256, tenant_id=tenant_id or "unknown",
                kb_id=kb_id or None, user_id=user_id or None,
                trace_id=trace_id),
            timeout=timeout,
        )
    except Exception:  # noqa: BLE001 —— 选择是增能，失败回 baseline
        return {"index": 0, "reason": ""}
    content = str((reply or {}).get("content") or "").strip()
    return {"index": parse_choice(content, len(candidates)),
            "reason": _reason(content)}


__all__ = ["should_generate_candidate", "build_select_prompt", "parse_choice",
           "select_candidate", "MIN_WORDS", "WIDE_WORDS"]
