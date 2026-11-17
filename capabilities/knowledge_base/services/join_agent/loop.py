#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""join 边复核的有界循环（与指标 agent 同一套协议）。

差异只有一处：终止动作提交的是**边列表**而不是单条公式，所以「形状校验」
按边逐条做 —— 缺字段的边丢弃并回灌一次，而不是让整轮作废（模型常一次给三条、
其中一条拼错）。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from ..agent_common import call_once, merge_usage, safe_json, tool_message
from .prompts import repair_message, system_prompt, task_message
from .tools import JoinContext, dispatch

logger = logging.getLogger(__name__)

_EDGE_KEYS = ("source_entity", "source_column", "target_entity", "target_column")


@dataclass
class JoinDecision:
    action: str                 # "propose" | "give_up"
    edges: list[dict] = field(default_factory=list)
    rationale: str = ""
    steps: int = 0
    reason: str = ""
    usage: dict = field(default_factory=dict)


def _text(value) -> str:
    return "" if value is None else str(value).strip()


async def run_join_review(
        task: str, candidates: list[dict], inventory: list[dict],
        ctx: JoinContext,
        call_step: Callable[[list[dict]], Awaitable[dict]],
        *, max_steps: int = 6, retry_backoff_s: float = 1.0,
) -> JoinDecision:
    """复核一轮候选；**永不抛**（预算/协议/工具失败都收敛成 `give_up`）。"""
    messages: list[dict] = [
        {"role": "system", "content": system_prompt()},
        {"role": "user", "content": task_message(task, candidates, inventory)},
    ]
    limit = max(0, int(max_steps))
    steps = 0
    usage: dict = {}
    repaired = False

    async def _repair(raw_text: str) -> bool:
        nonlocal repaired
        if repaired:
            return False
        repaired = True
        await asyncio.sleep(max(0.0, float(retry_backoff_s)))
        messages.append(repair_message(raw_text))
        return True

    while steps < limit:
        parsed, raw = await call_once(call_step, messages)
        if parsed is None:
            if not await _repair(raw):
                return JoinDecision("give_up", steps=steps,
                                    reason="protocol_error", usage=usage)
            continue

        merge_usage(usage, parsed.get("_usage"))
        action = _text(parsed.get("action"))
        args = parsed.get("args")
        payload = args if isinstance(args, dict) else {}

        repaired = False
        steps += 1
        messages.append({"role": "assistant", "content": safe_json(parsed)})

        result = await dispatch(action, payload, ctx)
        terminal = result.get("terminal")

        if terminal == "propose":
            edges = [edge for edge in (result.get("edges") or [])
                     if isinstance(edge, dict)
                     and all(_text(edge.get(key)) for key in _EDGE_KEYS)]
            dropped = len(result.get("edges") or []) - len(edges)
            if dropped > 0:
                # 有形状坏的边：回灌一次（模型常常一次给多条、某条拼错）。
                messages.append(tool_message({
                    "ok": False,
                    "error": f"{dropped} 条边缺少必填字段（{', '.join(_EDGE_KEYS)}），"
                             "请修正后重新 propose_join",
                }))
                continue
            messages.append(tool_message(result))
            return JoinDecision("propose", edges=edges,
                                rationale=_text(result.get("rationale")),
                                steps=steps, usage=usage)
        if terminal == "give_up":
            messages.append(tool_message(result))
            return JoinDecision("give_up", steps=steps,
                                reason=_text(result.get("reason")) or "no_reason",
                                usage=usage)

        messages.append(tool_message(result))

    return JoinDecision("give_up", steps=steps,
                        reason="budget_exhausted", usage=usage)


__all__ = ["JoinDecision", "run_join_review"]
