#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""行粒度提示（2026-11-01）。

失败分析里 `row` 桶 8/12、`value` 桶 9/21 的 DISTINCT 与真值不同：列表/计数题在
事实表粒度直接输出，join 把实体重复放大。这里只做**窄触发**：问句明确要实体列表/
计数时才加提示，标量聚合题不触发，关档逐字节不变。
"""
from __future__ import annotations

import re

_LIST = re.compile(
    r"\b(list|all|which|who|whom|whose|names?|give|state|find|show|display|"
    r"enumerate|identify|return)\b",
    re.IGNORECASE,
)
_COUNT = re.compile(
    r"\b(how\s+many|how\s+often|number\s+of|count|counts|total\s+number|"
    r"how\s+much)\b",
    re.IGNORECASE,
)
#: 明确标量题：问单一值/聚合，不要加 DISTINCT/实体粒度提示。
_SCALAR = re.compile(
    r"\b(what\s+is|what\s+was|what's|average|mean|median|sum|total|percentage|"
    r"percent|ratio|difference|highest|lowest|maximum|minimum|max|min)\b",
    re.IGNORECASE,
)


def wants_entity_grain(question: str) -> bool:
    """问句是否在要「实体列表/实体计数」。

    `how many` 既可能是实体计数（COUNT DISTINCT），也可能是事实行数；仍触发，
    由提示词让模型先判实体粒度。纯 `what is the average ...` 不触发。
    """
    q = str(question or "")
    if _COUNT.search(q):
        return True
    if _LIST.search(q) and not _SCALAR.search(q):
        return True
    return False


__all__ = ["wants_entity_grain"]
