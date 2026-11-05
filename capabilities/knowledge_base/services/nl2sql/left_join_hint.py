#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""否定/条件问句提示（2026-11-01）。

`hockey_8` 问「最高球员进名人堂了吗？如果进了，年份」——模型用 INNER JOIN hof，
没进名人堂的球员直接消失，答案变成 0 行；真值是 LEFT JOIN + CASE 'NO'。

窄触发：问句明确是「Did/Whether ...? If yes/if so」「Is there ...?」等需要保留
反面答案的形态。关档逐字节不变。
"""
from __future__ import annotations

import re

_NEGATIVE_CASE = re.compile(
    r"\b(did\s+.+\?|if\s+yes|if\s+so|whether|is\s+there|are\s+there|"
    r"has\s+.+\s+ever|have\s+.+\s+ever|was\s+.+\s+ever|were\s+.+\s+ever|"
    r"does\s+.+\s+have|do\s+.+\s+have|in\s+the\s+hall\s+of\s+fame)\b",
    re.IGNORECASE | re.DOTALL,
)


def wants_negative_case(question: str) -> bool:
    return bool(_NEGATIVE_CASE.search(str(question or "")))


__all__ = ["wants_negative_case"]
