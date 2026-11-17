#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""落点 Agent 的有界判定循环：模型每轮只出一个 JSON 动作，循环负责执行与兜底。

为什么是「有界外壳 + 只做协议」：

- **步数有界**。`max_steps` 是硬上限，耗尽了就 `give_up("budget_exhausted")`，
  绝不拿半成品落点硬凑 —— 半成品比没落点更难排查（分不清是模型判错还是被截断）。
- **协议错误只修一次**。偶发坏 JSON 是可恢复的（追加一条修复提示再要一次）；
  **连续**两次都拿不到 JSON 说明修复提示没起作用，再重试只是烧 token 与时间。
  传输异常（超时）与坏 JSON 共用这条路：对循环来说两者都是「上一轮没有可用答复」。
- **工具错误不打断**。非法 ref / 缺参数由 `dispatch` 变成结构化错误回给模型，
  这是**正常的一轮**（计一步）：模型看到错误后换参数重试，本来就是工具调用的常态。
- **不做守卫**。`landings` 原样返回，白名单 / 重算矩阵在 `apply.py`（Task 9）。
  在两层各写一半校验，出问题时「到底是谁拒的」就会漂移。
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Awaitable, Callable

from .prompts import concept_message, repair_message, system_prompt
from .tools import ToolContext, dispatch


@dataclass
class ConceptDecision:
    """一个概念的判定结果；`origin` 区分 B 档（agent 判）与 A 档（名字确定性命中）。"""

    action: str                 # "decide" | "give_up"
    #: 每条落点：{"ref": "c1", "ds_id": "d1", "entity_name": "asset",
    #:            "attributes": [{"attribute": "资产编码", "column": "asset_code"}]}
    #: `ds_id`/`entity_name` 由 `run_concept` 从 ref 查候选表解析，**不取模型自报值**。
    landings: list[dict]
    confidence: str             # high|medium|low
    rationale: str
    reason: str = ""
    steps: int = 0
    origin: str = "agent"       # "agent"（B 档）| "name"（A 档确定性命中）


#: 置信度枚举。模型偶尔会写 "High" / "HIGH" / 干脆不写 —— 统一降到 `low`
#: （规则 6 的「没把握就降置信」）。真正的写入守卫仍在 `apply.py`。
_CONFIDENCE_LEVELS = ("high", "medium", "low")


def _text(value) -> str:
    """模型输出里的任意值 → 去空白字符串（缺键/None 都回 ""，不留 "None"）。"""
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return str(value).strip()


def _confidence(value) -> str:
    """置信度归一：大小写不敏感；枚举外的一律 `low`（宁降不升）。"""
    level = _text(value).lower()
    return level if level in _CONFIDENCE_LEVELS else "low"


def _safe_json(value) -> str:
    """JSON 序列化，**绝不抛**。

    `call_step` 是外部注入的，它回来的对象里可能有 JSON 不认识的叶子（自定义
    对象、datetime、以及回环引用）：`default=str` 覆盖前者，`except` 兜住后者
    （循环引用会让 `json.dumps` 抛 `ValueError`，`default` 救不了）。回显/记账
    是增益，不能因为它把「循环永不抛」这条承诺破掉。
    """
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001 - 序列化失败退化成 repr，绝不打断判定
        return str(value)


def _resolve_landings(landings, ctx: ToolContext) -> list[dict]:
    """模型给的 `{ref, attributes}` → 落点契约 `{ref, ds_id, entity_name, attributes}`。

    **标识符一律从候选表解析，模型自己写的 ds_id/entity_name 一律忽略**：模型唯一
    该复述的是 `ref`（候选卡上就印着），源名/实体名由 `ctx.candidates` 决定。让模型
    手抄 `d1`/`asset` 只是多开一条编造通道（幻觉 ds_id、`asset ` 多一个空格就成了
    另一个名字），而 ref→候选的映射是这一轮里唯一权威的对应关系。

    未知 ref 的落点**直接丢弃**，不算协议错误：模型编 ref 是常见退化，整轮判定不
    该因此作废；全被丢光也照常返回 `decide`（`landings=[]`），由 `apply.py` 看到
    「本轮没有落点」——`give_up` 会额外写一条「模型放弃了」的语义，两者含义不同。
    """
    resolved: list[dict] = []
    for raw in landings if isinstance(landings, list) else []:
        if not isinstance(raw, dict):
            continue
        ref = _text(raw.get("ref"))
        candidate = (ctx.candidates or {}).get(ref)
        if candidate is None:
            continue
        attributes = raw.get("attributes")
        resolved.append({
            "ref": ref,
            "ds_id": candidate.ds_id,
            "entity_name": candidate.entity_name,
            "attributes": ([dict(item) for item in attributes
                            if isinstance(item, dict)]
                           if isinstance(attributes, list) else []),
        })
    return resolved


async def _call_once(
        call_step: Callable[[list[dict]], Awaitable[dict]],
        messages: list[dict],
) -> tuple[dict | None, str]:
    """调一次模型，返回 `(解析后的 dict, 修复提示用的原始片段)`；失败时 dict 为 None。

    传进去的是 messages 的**浅拷贝**：`call_step` 是外部注入的（生产实现走 HTTP），
    它若顺手在列表上 append，不该污染循环自己的对话历史（否则 tool 消息会和
    模型看到的顺序错位，下一轮就基于错位的历史做判断）。
    """
    try:
        parsed = await call_step(list(messages))
    except Exception as exc:  # noqa: BLE001 - 传输/解析异常统一按协议错误兜底
        return None, str(exc)
    if not isinstance(parsed, dict):
        # 非对象（列表/字符串/None）：同样算协议错误，把原样回显给模型看。
        return None, _safe_json(parsed)
    return parsed, ""


async def run_concept(
        concept: dict, cards: list[dict], ctx: ToolContext,
        call_step: Callable[[list[dict]], Awaitable[dict]],
        *, max_steps: int = 6, retry_backoff_s: float = 1.0,
) -> ConceptDecision:
    """概念 → 判定结果；**永不抛**（预算/协议/工具失败都收敛成 `give_up`）。

    **`max_steps` 限的是「被 dispatch 的轮次」，不是 `call_step` 的调用次数**：
    每轮最多允许一次修复重试（连续两次协议失败即放弃），所以一次 `run_concept`
    的模型调用上界是 `2 * max_steps`（`max_steps=0` 时为 0）。Task 11 按概念累计
    token/耗时、按 `steps` 记进度时，必须按 `call_step` 的实际调用次数算，不能拿
    `steps` 当调用次数 —— 否则协议抖动会让耗时/token 超出预算核算。

    `steps` 只数 dispatch 轮次还有一个原因：「最后一步踩到坏 JSON」若记成
    budget_exhausted，会把协议问题伪装成预算问题，排障时看错方向。
    """
    messages: list[dict] = [
        {"role": "system", "content": system_prompt()},
        concept_message(concept, cards),
    ]
    limit = max(0, int(max_steps))
    steps = 0
    #: 上一条追加的是不是修复提示。拿到一次可 dispatch 的动作就复位 —— 允许模型
    #: 「每一步各有一次」坏 JSON 的机会，但绝不允许连续两次，否则修复提示显然没读懂。
    repaired = False

    async def _repair(raw_text: str) -> bool:
        """退避 + 追加修复提示；返回 False 表示这次得放弃（已经修过一次）。

        两个入口（解析失败、`decide` 的 args 形状不对）共用这一段，退避与「只修
        一次」的规则才不会在两边漂移：先退避再要求重答 —— 坏 JSON 常见于模型被
        截断/上下文抖动，立刻重问往往原样再错一次，也给上游限流留出恢复窗口。
        """
        nonlocal repaired
        if repaired:
            return False
        repaired = True
        await asyncio.sleep(max(0.0, float(retry_backoff_s)))
        messages.append(repair_message(raw_text))
        return True

    while steps < limit:
        parsed, raw = await _call_once(call_step, messages)
        if parsed is None:
            if not await _repair(raw):
                return ConceptDecision("give_up", [], "", "",
                                       reason="protocol_error", steps=steps)
            continue

        action = _text(parsed.get("action"))
        args = parsed.get("args")
        payload = args if isinstance(args, dict) else {}

        # `landings` 的形状也属于协议：给了字典/字符串说明模型没按契约输出，
        # 退回一步让它改（缺键 = 空落点，是合法的「本轮没有映射」，不算错误）。
        # 放在 dispatch 之前：这一轮没有被执行，就不该计一步、也不该留下
        # assistant/tool 消息对 —— 否则历史里会出现一条「已 decide」的假动作。
        if (action == "decide" and "landings" in payload
                and not isinstance(payload.get("landings"), list)):
            if not await _repair(_safe_json(parsed)):
                return ConceptDecision("give_up", [], "", "",
                                       reason="protocol_error", steps=steps)
            continue

        repaired = False
        steps += 1
        # 回显模型这一轮的动作：tool 消息必须紧跟一条 assistant 消息，否则严格
        # 实现的 OpenAI 兼容网关会拒掉整段对话。回显的是重新序列化的等价 JSON
        # （`call_step` 只回解析后的对象，拿不到原始文本），语义一致即可。
        messages.append({"role": "assistant", "content": _safe_json(parsed)})

        # 动作名是否在 `TOOL_NAMES` 里由 `dispatch` 一处校验（它自己导入同一常量
        # 并回同样的「未知动作 + 可用列表」错误）：这里再查一遍只会复制文案，
        # 两套文案早晚漂移，而循环对两种失败的处理完全相同（回错误、继续下一轮）。
        result = await dispatch(action, payload, ctx)
        # ⚠️ 上游 MaaS 直接拒收 `role="tool"`（400，2026-11-14 实测：
        #   工具结果带不带 tool_call_id / name 都一样）。工具结果改挂 user 角色，
        #   用固定前缀标出「这不是用户的话」。协议仍然成立：模型的上一轮 assistant
        #   消息就是那个 JSON 动作，结果紧跟在它后面。
        messages.append({"role": "user",
                         "content": "工具结果：\n" + _safe_json(result)})

        terminal = result.get("terminal")
        if terminal == "decide":
            return ConceptDecision(
                "decide",
                # 只补标识符，不做白名单：列/表是否存在、重算矩阵统一在 apply.py。
                _resolve_landings(result.get("landings"), ctx),
                _confidence(result.get("confidence")),
                _text(result.get("rationale")),
                steps=steps,
            )
        if terminal == "give_up":
            return ConceptDecision("give_up", [], "", "",
                                   reason=_text(result.get("reason")) or "no_reason",
                                   steps=steps)
        # 普通工具：继续下一轮（工具错误也是普通一轮，不额外记账）。

    return ConceptDecision("give_up", [], "", "",
                           reason="budget_exhausted", steps=steps)


__all__ = ["ConceptDecision", "run_concept"]
