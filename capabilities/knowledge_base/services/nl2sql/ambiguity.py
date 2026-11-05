#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""确定性歧义预检（S7）：不调模型，只报告有证据的列/值歧义。

两类规则（纯函数）：
1. 问句里的词同时命中 ≥2 个列名的片段（如 `date` → start_date / end_date）；
2. 问句里**带引号**的字面量不在任何列样例里 → 提示核对拼写。

输出最多 3 条 hint；只作提示，不阻断生成。
"""
from __future__ import annotations

import re

MAX_HINTS = 3
_STOP = frozenset({
    "the", "a", "an", "of", "in", "on", "for", "and", "or", "to", "is",
    "are", "what", "which", "who", "how", "many", "much", "list", "give",
    "show", "with", "by", "from", "that", "this", "has", "have", "was",
    "were", "all", "any", "per", "than", "more", "less", "between",
})


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9_]+", str(text or "").lower())
            if len(t) >= 3 and t not in _STOP]


def _column_segments(name: str) -> set[str]:
    return {p for p in re.split(r"[^a-z0-9]+", str(name or "").lower()) if p}


def _quoted_literals(question: str) -> list[str]:
    found = re.findall(r'"([^"]{2,120})"', str(question or ""))
    found += re.findall(r"'([^']{2,120})'", str(question or ""))
    out: list[str] = []
    for value in found:
        value = value.strip()
        if value and value not in out:
            out.append(value)
    return out


def find_ambiguities(question: str, columns: list[dict],
                     value_dict: dict[str, list[str]] | None = None) -> list[str]:
    """返回 ≤3 条歧义 hint；没有证据时返回空列表。"""
    names = [str(c.get("name") or "") for c in (columns or []) if c.get("name")]
    hints: list[str] = []
    seen: set[str] = set()

    for token in _tokens(question):
        hit = [name for name in names
               if token in _column_segments(name) or token in name.lower()]
        if len(hit) >= 2:
            key = tuple(sorted(hit))
            if key in seen:
                continue
            seen.add(key)
            hints.append(
                f"question mentions '{token}'; ambiguous columns: "
                f"{', '.join(hit[:6])}; pick the one the question means.")
            if len(hints) >= MAX_HINTS:
                return hints

    values = {str(v).casefold() for vals in (value_dict or {}).values()
              for v in (vals or [])}
    if values:
        for literal in _quoted_literals(question):
            if literal.casefold() not in values:
                hints.append(
                    f"quoted value '{literal}' is not in any sampled column "
                    f"value; verify its exact spelling before filtering.")
                if len(hints) >= MAX_HINTS:
                    break
    return hints[:MAX_HINTS]


__all__ = ["find_ambiguities", "MAX_HINTS"]
