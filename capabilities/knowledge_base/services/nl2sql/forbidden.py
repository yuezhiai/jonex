"""血缘「禁止同查」的纯函数。

表对 = 两张**由血缘派生关系连接**的物理表；`active` 时禁止在同一查询里同时出现
（防跨层直连 / 防重复计数）。本模块不连库、不调模型 —— 输入就是表对与 SQL 引用/边/路径。

表名比较统一走 `table_ref.is_same_table`（段序列后缀匹配）：`dbo.jobs` / `[dbo].[jobs]` /
`jobs` 视为同一张表，而 `sales.orders` 与 `archive.orders` 互不匹配。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from .table_ref import is_same_table, pair_key

FORBIDDEN_NOTE = "以下表对存在血缘派生关系，禁止在同一查询中同时使用（会重复计数）"


@dataclass(frozen=True)
class ForbiddenPair:
    table_a: str
    table_b: str

    @property
    def key(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """排序/展示用的规范键；**不**代表等价（归一由 `is_same_table` 负责）。"""
        return pair_key(self.table_a, self.table_b)


def stored_order(a: str, b: str) -> tuple[str, str]:
    """存储用的规范化顺序（保留原始限定名，只按小写比较定先后）。"""
    left, right = str(a or "").strip(), str(b or "").strip()
    return (left, right) if left.lower() <= right.lower() else (right, left)


def _path_refs(path) -> list[str]:
    return [t for step in path.steps
            for t in (step.enter_table, step.leave_table)
            if str(t or "").strip()]


def _pair_hit(pair: ForbiddenPair, refs: list[str]) -> bool:
    return (any(is_same_table(ref, pair.table_a) for ref in refs)
            and any(is_same_table(ref, pair.table_b) for ref in refs))


def hit_forbidden(pairs, referenced) -> ForbiddenPair | None:
    """本次 SQL 引用的表里，是否有某禁止对的**两端都出现**。

    `referenced` 是表名集合（`guard` 保留原始限定名）；比较用后缀匹配，
    因此 `archive.orders` 不会再撞上 `sales.orders`。
    """
    refs = [str(r) for r in (referenced or ()) if str(r or "").strip()]
    for pair in pairs or ():
        if _pair_hit(pair, refs):
            return pair
    return None


def pair_hits(pairs, refs) -> bool:
    """任一对的**两端**都被 refs 命中（后缀匹配）。

    `guard` 的禁止同查判定、join 边过滤、边注入（`joins.py`）共用这一个口径 ——
    禁止对存限定名（`dbo.orders`）、调用方只拿到裸名（`orders`）时必须照样命中；
    不同 schema 的同名表（`archive.orders` vs `sales.orders`）不命中。
    """
    items = list(pairs or ())
    refs = [str(r) for r in (refs or ()) if str(r or "").strip()]
    return any(_pair_hit(pair, refs) for pair in items)


def filter_joins(joins: Sequence[dict],
                 pairs: Iterable[ForbiddenPair]) -> list[dict]:
    """丢掉端点命中禁止对的 join 边（端点用后缀匹配，不比键集合）。"""
    items = list(pairs or ())
    if not items:
        return list(joins or ())
    return [j for j in (joins or ())
            if not pair_hits(items, [str(j.get("from_table") or ""),
                                     str(j.get("to_table") or "")])]


def drop_forbidden_paths(paths: Sequence,
                         pairs: Iterable[ForbiddenPair]) -> list:
    """丢掉**同时包含**某禁止对两端的路径（`A→C→B` 也算）。"""
    items = list(pairs or ())
    if not items:
        return list(paths or ())
    return [path for path in (paths or ())
            if not any(_pair_hit(pair, _path_refs(path)) for pair in items)]


def prompt_note(pairs: Iterable[ForbiddenPair],
                tables: Iterable[str]) -> dict | None:
    """把与本次上下文（锚点/邻域）相关的禁止对渲染成 payload 键；无则 None。"""
    refs = [str(t) for t in (tables or ()) if str(t or "").strip()]
    picked = [p for p in (pairs or ())
              if any(is_same_table(ref, p.table_a) or is_same_table(ref, p.table_b)
                     for ref in refs)]
    if not picked:
        return None
    return {"note": FORBIDDEN_NOTE,
            "pairs": [[p.table_a, p.table_b] for p in picked]}
