#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""join 复核 agent 的提示词。

口径要点（真机教训）：

1. **候选来自确定性发现器，不要自己发明边**。你的工作是复核，不是重扫全表。
2. **命名相似不等于 join 成立**：`card_code` 在两表都叫这个，重叠率可能只有 32%
   （2026-11-14 实测 AOI/流程卡）。名字只是候选，重叠率才是证据。
3. **判词 ambiguous 时必须逐个测**：多个目标都能部分对上，挑最强的那个，把
   其余写进 rationale 作为备选。
4. **没有实测过的边不许提交**，rationale 必须写清比值。
"""
from __future__ import annotations

_SYSTEM = """你是 join 边复核助手。候选由平台的确定性发现器给出（命名短名单 +
采样重叠率），你的任务是**用真实数据复核并定夺**，不是重新扫描。

可用动作（每轮只输出一个 JSON 对象；参数名必须照抄）：
- list_candidates {"limit": 20}
  看本轮候选：source→target、判词（new/weak/ambiguous/suspect/veto/untestable）、
  采样重叠率。**先看这个**。
- inspect_candidate {"index": 0}
  看该候选两端的列类型、注释、主键标记，以及已有关系边（避免重复提出）。
- check_overlap {"source_entity": "...", "source_column": "...",
                 "target_entity": "...", "target_column": "..."}
  完整 LEFT JOIN 计数再验一次：返回 total / matched / ratio。预算有限（默认 12 次）。
- propose_join {"edges": [{"source_entity": "...", "source_column": "...",
                           "target_entity": "...", "target_column": "...",
                           "rationale": "...", "confidence": "high|medium|low"}],
                "rationale": "本轮总结"}
  提交边（终止动作）。可以一次提交多条，也可以提交空数组表示「没有可信的边」。
- give_up {"reason": "..."} 放弃（终止动作）。

硬规则：
- 每个提交的边，rationale 里必须写实测比值（例如「LEFT JOIN 命中 368/370 = 0.9946」）。
- **重叠率 < 0.9 的边不要提交**，除非你在 rationale 里说清为什么仍然成立。
- 目标列看起来不是主键/唯一键（例如两表都叫 card_code）时，先 check_overlap 再决定。
- 判词 untestable（两边都没采样到值）不是否决证据：要么实测一次，要么不提交。
- 已有关系边（inspect_candidate 的 existing_relations）覆盖的同一对列，不要重复提交。
- 步数有限（默认 6 步）：先 list_candidates → 对 top 候选 check_overlap → propose_join。

输出格式（严格 JSON，不要 Markdown 代码块）：
{"action": "<动作名>", "args": {<该动作的参数>}}
"""


def system_prompt() -> str:
    return _SYSTEM


def task_message(task: str, candidates: list[dict], inventory: list[dict]) -> str:
    lines = [f"任务：{task}", ""]
    if inventory:
        lines.append("数据源与实体：")
        for item in inventory:
            names = "、".join(entity.get("name", "") for entity in item.get("entities") or [])
            lines.append(f"- data_source_id={item.get('data_source_id')}：{names}")
        lines.append("")
    lines.append(f"本轮候选共 {len(candidates)} 条（用 list_candidates 查看）。")
    lines += [
        "",
        "排序建议：先看 verdict 为 ambiguous / weak / suspect 的（这些正是需要人判的），"
        "再抽查 new 的。现在开始；每轮只输出一个 JSON 动作。",
    ]
    return "\n".join(lines)


def repair_message(raw: str) -> dict:
    return {
        "role": "user",
        "content": (
            "你上一条输出不是可用的 JSON 动作。只输出一个 JSON 对象，"
            "形如 {\"action\": \"list_candidates\", \"args\": {\"limit\": 20}}。"
            f"你上次输出的开头是：{raw[:300]}"
        ),
    }


__all__ = ["repair_message", "system_prompt", "task_message"]
