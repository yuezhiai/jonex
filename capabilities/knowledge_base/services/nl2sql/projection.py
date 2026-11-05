#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""投影纪律的确定性检查（纯函数，不连库不调模型）。

只抓两类**高置信**问题：

1. `SELECT *` —— 问句再宽也几乎不需要整表所有列；
2. **标量问句却返回多列** —— `how many` / `what is the average` / 「多少个」「平均」这类
   问句期望一个值，模型却把名称/ID/人数一起投影。

**保守**是刻意的：`list / which / top-k / per / by / 列出 / 每个 / 按` 一律不判（这些问句
本来就可能要多列）；解析失败也不判（交给安全闸）。误伤一次会多花一次模型调用，
而漏判只是维持现状。

真机依据：BIRD Mini-Dev 71 条列数不符里 **62 条是模型多给列**（多出 128 列），
见 `documents/docs/reports/BIRD-MiniDev准确率提升分析-2026-10-28.md`。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .sql_ir import outer_select, parse, projection_shape


@dataclass(frozen=True)
class ProjectionViolation:
    code: str          # "select_star" | "wide_scalar"
    detail: str        # 给 retry_note 的人读说明


#: 允许/预期多列的问句信号（任一命中就不判 wide_scalar）。
_MULTI_COLUMN_HINTS = re.compile(
    r"\b(list|which|who|whom|names?|top\s+\d+|for each|per|each|every|by|rank|show me all)\b"
    r"|列出|哪些|哪个|每个|分别|各|按|排名|前\s*\d+",
    re.IGNORECASE,
)

#: 标量问句信号（英文 + 中文）。
_SCALAR_HINTS = re.compile(
    r"\bhow many\b|\bhow much\b|"
    r"\bwhat(?:'s| is) the\b[^.?]*\b(average|total|number|count|percentage|rate|ratio|"
    r"maximum|minimum|highest|lowest|largest|smallest)\b|"
    r"\bwhat percentage\b|\bwhat fraction\b|"
    r"多少|几(?:个|家|条|张|种)?|总数|平均|比例|占比|率|最高|最低|最大|最小",
    re.IGNORECASE,
)


def check_projection(sql: str, question: str) -> ProjectionViolation | None:
    """返回违规或 None。**不抛**：坏输入/解析失败一律按「不违规」处理。"""
    text = str(sql or "").strip()
    if not text:
        return None
    ir = parse(text)
    if not ir.ok:
        return None
    shape = projection_shape(ir)
    if shape is None:
        return None

    if shape["star"]:
        return ProjectionViolation(
            code="select_star",
            detail="SQL projects `*`; select only the columns the question asks for.",
        )

    asked = str(question or "")
    if _MULTI_COLUMN_HINTS.search(asked):
        return None
    if not _SCALAR_HINTS.search(asked):
        return None

    count = shape["width"]
    if count <= 1:
        return None
    return ProjectionViolation(
        code="wide_scalar",
        detail=(f"the question asks for a single value (expected 1 column), but the SQL "
                f"returns {count} columns; project only the requested value"),
    )


__all__ = ["ProjectionViolation", "check_projection"]
