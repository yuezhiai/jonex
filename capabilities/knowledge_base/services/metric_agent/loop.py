#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""指标构建 Agent 的有界循环：模型每轮只出一个 JSON 动作。

与落点 agent 的循环同一套纪律（`landing_agent/loop.py`）：

- **步数有界**：`max_steps` 是硬上限，耗尽即 `give_up("budget_exhausted")`。
- **协议错误只修一次**：连续两次没有可用动作就放弃，再重试只是烧 token。
- **工具错误不打断**：非法参数/被安全闸拒绝都回结构化错误，计一步，模型换写法再来。
- **提案形状在入库前校验**：`propose_metric` 缺 `name`/`formula_sql` 时不作数，
  回一条错误让它补 —— 否则会存进一条「空提案」，比没提案更难排查。
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .prompts import repair_message, system_prompt, task_message
from ..agent_common import call_once, merge_usage, safe_json, tool_message
from .tools import ToolContext, dispatch

logger = logging.getLogger(__name__)


@dataclass
class MetricDecision:
    """一次构建的结论：要么一份提案，要么明确放弃。"""

    action: str                 # "propose" | "give_up"
    proposal: dict = field(default_factory=dict)
    steps: int = 0
    reason: str = ""
    usage: dict = field(default_factory=dict)
    #: 平台侧复核结果（`verify.Verification.to_dict()`）；未复核时为空字典。
    verification: dict = field(default_factory=dict)


def _text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


async def run_metric_task(
        task: str, inventory: list[dict], ctx: ToolContext,
        call_step: Callable[[list[dict]], Awaitable[dict]],
        *, max_steps: int = 8, max_tokens: int | None = None,
        verify=None, verify_retries: int = 1,
        retry_backoff_s: float = 1.0,
) -> MetricDecision:
    """任务 → 提案；**永不抛**（预算/协议/工具失败都收敛成 `give_up`）。

    `max_steps` 限的是「被 dispatch 的轮次」，模型调用上界是 `2 * max_steps`
    （每轮最多一次修复重试）。`max_tokens` 是**整次构建**的上限：到点即收口，
    不再发新步 —— 单步预算由模型调用层（2048）管，两者不是一回事。

    `verify` 是平台侧复核（`verify.verify_proposal`）：**提案提交后先过它**。
    不通过就把失败项回灌给模型再改一轮（`verify_retries` 次），而不是让一份
    「能跑但结果是 0」的公式入库。通过时把复核结果带回 `MetricDecision`。
    """
    messages: list[dict] = [
        {"role": "system", "content": system_prompt()},
        {"role": "user", "content": task_message(task, inventory)},
    ]
    limit = max(0, int(max_steps))
    steps = 0
    usage: dict = {}
    repaired = False
    retries_left = max(0, int(verify_retries))

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
                return MetricDecision("give_up", steps=steps,
                                      reason="protocol_error", usage=usage)
            continue

        merge_usage(usage, parsed.get("_usage"))
        if max_tokens is not None and (usage.get("total_tokens") or 0) >= int(max_tokens):
            return MetricDecision("give_up", steps=steps,
                                  reason="token_budget_exhausted", usage=usage)
        action = _text(parsed.get("action"))
        args = parsed.get("args")
        payload = args if isinstance(args, dict) else {}

        repaired = False
        steps += 1
        messages.append({"role": "assistant", "content": safe_json(parsed)})

        result = await dispatch(action, payload, ctx)
        terminal = result.get("terminal")

        if terminal == "propose":
            proposal = result.get("proposal") or {}
            missing = [key for key in ("name", "formula_sql")
                       if not _text(proposal.get(key))]
            if missing:
                # 提案缺必填项 = 这一轮没提交成功；回错误让模型补齐，不打断。
                result = {"ok": False,
                          "error": "提案缺少必填字段：" + "、".join(missing)}
            else:
                verification = None
                if verify is not None:
                    try:
                        verification = await verify(proposal)
                    except Exception as exc:  # noqa: BLE001 - 复核异常不打断
                        verification = None
                        logger.warning("[metric-agent] 复核异常：%s", exc)
                outcome = (verification.to_dict()
                           if hasattr(verification, "to_dict") else verification)
                if (isinstance(outcome, dict) and outcome.get("ok") is False
                        and retries_left > 0):
                    retries_left -= 1
                    messages.append(tool_message({
                        "ok": False,
                        "error": "平台复核未通过，请修正公式后重新 propose",
                        "verification": outcome,
                    }))
                    continue
                messages.append(tool_message(result))
                return MetricDecision("propose", proposal=proposal, steps=steps,
                                      usage=usage,
                                      verification=outcome
                                      if isinstance(outcome, dict) else {})
        elif terminal == "give_up":
            messages.append(tool_message(result))
            return MetricDecision("give_up", steps=steps,
                                  reason=_text(result.get("reason"))
                                         or "no_reason", usage=usage)

        messages.append(tool_message(result))

    return MetricDecision("give_up", steps=steps,
                          reason="budget_exhausted", usage=usage)


__all__ = ["MetricDecision", "run_metric_task"]
