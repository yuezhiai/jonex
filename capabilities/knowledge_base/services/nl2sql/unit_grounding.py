#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""数值单位接地（2026-11-01）。

失败分析里 `mondial_geo_16` 问 `population growth > 3%`，数据存 `0.03`，模型写
`> 3` → 0 行。这里只做**百分比**的确定性缩放：问句含 `%`/percent 且最终 0 行时，
把比较谓词里的数字试一次 `/100`（或 `*100`），能出结果才接受。不是通用值索引。
"""
from __future__ import annotations

import re

_PERCENT = re.compile(r"%|\bper\s?cent\b|\bpercent(age)?\b", re.IGNORECASE)
_NUM = r"(\d+(?:\.\d+)?)"
_OP_NUM = re.compile(rf"(<=|>=|<>|!=|<|>|=)\s*{_NUM}")
_NUM_OP = re.compile(rf"{_NUM}\s*(<=|>=|<>|!=|<|>|=)")


def percent_question(question: str) -> bool:
    return bool(_PERCENT.search(str(question or "")))


def _fmt(value: float) -> str:
    text = f"{value:.12f}".rstrip("0").rstrip(".")
    return text or "0"


def scale_numeric_literals(sql: str, factor: float) -> str:
    """只缩放比较谓词两侧的数字（`> 3`、`3 <`）；LIMIT/OFFSET、算术常量不动。"""

    def _after(match: re.Match) -> str:
        return f"{match.group(1)} {_fmt(float(match.group(2)) * factor)}"

    def _before(match: re.Match) -> str:
        return f"{_fmt(float(match.group(1)) * factor)} {match.group(2)}"

    scaled = _OP_NUM.sub(_after, sql)
    return _NUM_OP.sub(_before, scaled)


def scale_candidates(sql: str, question: str) -> list[tuple[float, str]]:
    """返回 `[(factor, rewritten_sql)]`；非百分比问句/没有可缩放数字时为空。"""
    if not percent_question(question) or not sql:
        return []
    numbers = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", sql)]
    candidates: list[tuple[float, str]] = []
    factors: list[float] = []
    if any(abs(n) > 1 for n in numbers):
        factors.append(0.01)
    if any(0 < abs(n) < 1 for n in numbers):
        factors.append(100.0)
    for factor in factors:
        rewritten = scale_numeric_literals(sql, factor)
        if rewritten != sql:
            candidates.append((factor, rewritten))
    return candidates


__all__ = ["percent_question", "scale_numeric_literals", "scale_candidates"]
