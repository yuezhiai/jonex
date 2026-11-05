#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""执行结果投票（OpenSearch-SQL `vote.py` 的窄移植，2026-11-01）。

它在两条"看起来都对"的 SQL 之间不靠 LLM 评判，而靠**执行结果是否一致**：
同结果集合的候选算一票（结果相同的候选互为多数），空结果不计票，
票数相同取最早生成的那条。

放进 Jonex 的约束：
- 只做**选择**，不做执行改写（真正执行仍走原来的 guard/治理/只读链路）；
- 任一候选执行失败 = 该候选弃权（不是整题失败）；
- 全部弃权 / 只有一个有效候选 ⇒ 退回候选 0（基线），行为与引入前一致。
"""
from __future__ import annotations


def rows_key(rows: list | None):
    """结果表的可哈希键：行序无关、重复折叠（与官方 EX 同口径）。"""
    if not rows:
        return None
    return frozenset(tuple(str(v) for v in row) for row in rows)


def vote_index(outcomes: list[list | None]) -> int:
    """`outcomes[i]` = 第 i 条候选的行列表 / None（执行失败）。返回应选的候选下标。"""
    groups: dict[object, list[int]] = {}
    for i, rows in enumerate(outcomes or []):
        key = rows_key(rows)
        if key is None:  # 空结果 / 失败：不计票
            continue
        groups.setdefault(key, []).append(i)
    if not groups:
        return 0
    best = max(groups.values(), key=lambda idxs: (len(idxs), -idxs[0]))
    return best[0]


__all__ = ["rows_key", "vote_index"]
