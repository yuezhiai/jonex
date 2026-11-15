"""表名的唯一比较口径：段序列后缀匹配。

`jobs` ⊑ `dbo.jobs`（裸名是限定名的后缀）；`sales.orders` 与 `archive.orders`
互不匹配。这是「禁止同查」与安全闸白名单共用的规则 —— 两处口径必须同源，否则
一边放行一边拦截。末尾孤点（`dbo.`）回空元组：那是提取器没收到表名，交给调用方
的哨兵处理，绝不能归一成 `dbo` 把 schema 当表名。
"""
from __future__ import annotations

from typing import Iterable

_QUOTES = "[]`\""


def table_ref(name) -> tuple[str, ...]:
    """限定名 → 段元组（小写、去引号、滤空段）。末尾孤点 → 空元组。"""
    cleaned = str(name or "").strip()
    for ch in _QUOTES:
        cleaned = cleaned.replace(ch, "")
    if cleaned.endswith("."):
        return ()
    return tuple(p.strip().lower() for p in cleaned.split(".") if p.strip())


def refs_same(left, right) -> bool:
    """**已解析**段元组的后缀匹配（`is_same_table` 的免重复解析版）。

    给「存量行预解析一次、候选逐个比较」这类场景用：口径与 `is_same_table`
    逐字节一致，只是不再对同一批表名反复 `table_ref()`。
    """
    if not left or not right:
        return False
    short, long = (left, right) if len(left) <= len(right) else (right, left)
    return long[-len(short):] == short


def is_same_table(a, b) -> bool:
    """段序列后缀匹配；任一侧为空元组 → False（空名不代表任何表）。"""
    return refs_same(table_ref(a), table_ref(b))


def ref_matches(ref, candidates: Iterable) -> bool:
    """`ref` 是否命中候选集合中的任意一个。"""
    return any(is_same_table(ref, item) for item in candidates or ()
               if str(item or "").strip())


def pair_key(a, b) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """按段元组字典序排序的稳定键（**不负责**后缀归一）。"""
    ka, kb = table_ref(a), table_ref(b)
    return (ka, kb) if ka <= kb else (kb, ka)


__all__ = ["table_ref", "is_same_table", "refs_same", "ref_matches", "pair_key"]
