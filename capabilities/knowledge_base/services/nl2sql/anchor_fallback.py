#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""无候选回退锚点（2026-11-01）。

`_pick` 的零候选分支会直接回 `no_candidate`：问句只提人名/日期（例如
"Calculate the average overall rating of Pietro Marino"）时，词面匹配不到任何
已对齐实体，整题连生成都不进。preview 档下模型本来能看见全部表，缺的只是一个锚点。

这里给一个**确定性**的兜底锚点：问句词面命中的实体名优先，其次列数多的表
（信息量最大的那张）。只在 `NL2SQL_NO_CANDIDATE_FALLBACK=1` 且调用方没有点名实体时生效；
触发面只有「本来就要拒答」的题，不会改变已能作答的请求。
"""
from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def choose_anchor(question: str, entities: list | None) -> str | None:
    """从编译 schema 的实体里挑一个兜底锚点；空列表返回 None。"""
    tokens = set(_TOKEN_RE.findall(str(question or "").lower()))
    best_name: str | None = None
    best_key: tuple[int, int] | None = None
    for entity in entities or []:
        if not isinstance(entity, dict):
            continue
        name = str(entity.get("name") or "").strip()
        if not name:
            continue
        parts = [p for p in _TOKEN_RE.findall(name.lower()) if p]
        hits = sum(1 for p in parts if p in tokens or (p + "s") in tokens)
        columns = len(entity.get("attributes") or [])
        key = (hits, columns)
        if best_key is None or key > best_key:
            best_name, best_key = name, key
    return best_name


__all__ = ["choose_anchor"]
