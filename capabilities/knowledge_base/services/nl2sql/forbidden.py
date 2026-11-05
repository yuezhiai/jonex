"""血缘「禁止同查」的纯函数。

表对 = 两张**由血缘派生关系连接**的物理表；`active` 时禁止在同一查询里同时出现
（防跨层直连 / 防重复计数）。本模块不连库、不调模型 —— 输入就是表对与 SQL 引用/边/路径。

表名归一与 `guard._table_key` 同口径（最后一段小写）：`dbo.jobs` / `[dbo].[jobs]` /
`jobs` 视为同一张表。**已知取舍**：同源内不同 schema 的同名表会互相命中 —— 与安全闸
白名单同一限制，写进设计文档而不是另造一套匹配。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

FORBIDDEN_NOTE = "以下表对存在血缘派生关系，禁止在同一查询中同时使用（会重复计数）"


@dataclass(frozen=True)
class ForbiddenPair:
    table_a: str
    table_b: str

    @property
    def key(self) -> tuple[str, str]:
        return pair_key(self.table_a, self.table_b)


def table_key(name: str) -> str:
    """最后一段小写；去掉方括号/反引号/双引号。空段滤掉（同 `guard._table_key`）。"""
    cleaned = str(name or "").strip()
    for ch in "[]`\"":
        cleaned = cleaned.replace(ch, "")
    if cleaned.endswith("."):
        return ""
    parts = [p for p in cleaned.split(".") if p]
    return (parts[-1] if parts else "").strip().lower()


def pair_key(a: str, b: str) -> tuple[str, str]:
    ka, kb = table_key(a), table_key(b)
    return (ka, kb) if ka <= kb else (kb, ka)


def stored_order(a: str, b: str) -> tuple[str, str]:
    """存储用的规范化顺序（保留原始限定名，只按小写比较定先后）。"""
    left, right = str(a or "").strip(), str(b or "").strip()
    return (left, right) if left.lower() <= right.lower() else (right, left)


def hit_forbidden(pairs: Iterable[ForbiddenPair],
                  referenced_keys: Iterable[str]) -> ForbiddenPair | None:
    """本次 SQL 引用的表里，是否有某禁止对的**两端都出现**。"""
    referenced = set(referenced_keys or ())
    for pair in pairs or ():
        a, b = pair.key
        if a in referenced and b in referenced:
            return pair
    return None


def filter_joins(joins: Sequence[dict],
                 pairs: Iterable[ForbiddenPair]) -> list[dict]:
    """丢掉端点命中禁止对的 join 边；空 pairs 原样返回（不复制语义上的差异）。"""
    keys = {p.key for p in (pairs or ())}
    if not keys:
        return list(joins or ())
    return [j for j in (joins or ())
            if pair_key(j.get("from_table", ""), j.get("to_table", "")) not in keys]


def drop_forbidden_paths(paths: Sequence,
                         pairs: Iterable[ForbiddenPair]) -> list:
    """丢掉**同时包含**某禁止对两端的路径（`A→C→B` 也算）。"""
    keys = {p.key for p in (pairs or ())}
    if not keys:
        return list(paths or ())
    kept = []
    for path in paths or ():
        tables = {table_key(t) for step in path.steps
                  for t in (step.enter_table, step.leave_table)}
        if any(a in tables and b in tables for a, b in keys):
            continue
        kept.append(path)
    return kept


def prompt_note(pairs: Iterable[ForbiddenPair],
                tables: Iterable[str]) -> dict | None:
    """把与本次上下文（锚点/邻域）相关的禁止对渲染成 payload 键；无则 None。"""
    keys = {table_key(t) for t in (tables or ()) if str(t or "").strip()}
    picked = [p for p in (pairs or ())
              if table_key(p.table_a) in keys or table_key(p.table_b) in keys]
    if not picked:
        return None
    return {"note": FORBIDDEN_NOTE,
            "pairs": [[p.table_a, p.table_b] for p in picked]}
