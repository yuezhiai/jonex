"""治理 P1：等级序与有效等级（纯函数）。"""
from __future__ import annotations

from typing import Iterable

LEVELS: dict[str, int] = {
    "public": 0, "internal": 1, "confidential": 2, "secret": 3,
}
_ORDER = ("public", "internal", "confidential", "secret")


def level_rank(level: str | None) -> int:
    return LEVELS.get(str(level or "").strip().lower(), 0)


def max_level(*levels: str | None) -> str:
    best = "public"
    for lv in levels:
        if level_rank(lv) > level_rank(best):
            best = str(lv).strip().lower()
    return best


def effective_level(rows: Iterable[dict]) -> str:
    """单个资源的生效等级：manual 钉住；无 manual 时 imported/derived 取 max。"""
    rows = list(rows or ())
    manual = [r for r in rows if str(r.get("origin") or "").lower() == "manual"]
    if manual:
        return max_level(*(r.get("level") for r in manual))
    return max_level(*(r.get("level") for r in rows))


def effective_table_level(table_level: str | None,
                          column_levels: Iterable[str]) -> str:
    """表级 = max(表级标签, 所有列级)。表级是列级的下限。"""
    return max_level(table_level, *list(column_levels or ()))
