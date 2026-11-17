#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""各 agent 循环共用的小工具：协议解析、消息包装、usage 累加。

抽出来的原因：模型调用层的错误口径（协议错误 / 工具结果挂哪个 role / usage
怎么累加）必须**只有一份**。指标 agent 与 join 复核 agent 各写一遍，早晚会出现
「一个改了 role、另一个没改」的漂移 —— 2026-11-14 那次 `role="tool"` 事故正是
这种漂移的代价。
"""
from __future__ import annotations

import json
from typing import Awaitable, Callable


def safe_json(value) -> str:
    """JSON 序列化，**绝不抛**（序列化失败退化成 repr）。"""
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return str(value)


def tool_message(result) -> dict:
    """工具结果挂 **user** 角色。

    上游 MaaS 拒收 `role="tool"`（400，2026-11-14 实测，带 tool_call_id / name
    也无效）。协议的完整性靠「上一轮 assistant 就是那个 JSON 动作」保证。
    """
    return {"role": "user", "content": "工具结果：\n" + safe_json(result)}


def merge_usage(total: dict, usage) -> None:
    """把一步的 `_usage` 累加到总账；缺失字段记 None，不臆造成 0。"""
    if not isinstance(usage, dict):
        usage = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(key)
        if value is None:
            total.setdefault(key, None)
            continue
        try:
            total[key] = (total.get(key) or 0) + int(value)
        except (TypeError, ValueError):
            total.setdefault(key, None)


async def call_once(
        call_step: Callable[[list[dict]], Awaitable[dict]],
        messages: list[dict],
) -> tuple[dict | None, str]:
    """调一次模型；返回 `(解析后的 dict, 修复提示用的原始片段)`。

    传进去的是 messages 的**浅拷贝**：注入的 `call_step` 若顺手改列表，
    不该污染循环自己的对话历史。
    """
    try:
        parsed = await call_step(list(messages))
    except Exception as exc:  # noqa: BLE001 - 传输/解析异常统一按协议错误兜底
        return None, str(exc)
    if not isinstance(parsed, dict):
        return None, safe_json(parsed)
    return parsed, ""


__all__ = ["call_once", "merge_usage", "safe_json", "tool_message"]
