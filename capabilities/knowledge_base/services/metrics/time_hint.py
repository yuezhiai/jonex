#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""问句里的中文时间范围 → `(time_from, time_to)`。

为什么需要：`ask_metric` 的时间窗是**显式参数**，问句里的「7月份」不会自动生效。
真机（2026-11-15）用户问「7月份的AOI良率」拿到的是全期值，数字与问题对不上。

解析范围刻意保守 —— 只认**写法明确、含义唯一**的几种：

- `2026年7月` / `2026年7月份` / `7月份` / `7月`（无年份时按当前年；若该月在未来则取去年）
- `2026年`
- `本月` / `这个月` / `上个月` / `上月`
- `近N天` / `最近N天` / `过去N天`
- `今天` / `今日`

不认「最近一段时间」「上半年」这类需要业务口径的模糊说法 —— 猜错比不猜更糟
（会用一个看起来正确、实际口径不同的数字回答）。
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

_YM = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月")
_YEAR = re.compile(r"(\d{4})\s*年(?!\s*\d)")
_MONTH = re.compile(r"(?<!\d)(\d{1,2})\s*月份?(?!\d)")
_RECENT_DAYS = re.compile(r"(?:近|最近|过去)\s*(\d{1,3})\s*天")
_THIS_MONTH = re.compile(r"(?:本月|这个月|当月)")
_LAST_MONTH = re.compile(r"(?:上个月|上月)")
_TODAY = re.compile(r"(?:今天|今日)")


@dataclass(frozen=True)
class TimeHint:
    time_from: str
    time_to: str
    label: str


def _month_range(year: int, month: int) -> TimeHint:
    last = calendar.monthrange(year, month)[1]
    return TimeHint(f"{year:04d}-{month:02d}-01",
                    f"{year:04d}-{month:02d}-{last:02d}",
                    f"{year:04d}-{month:02d}")


def parse_time_hint(question: str,
                    now: Optional[datetime] = None) -> Optional[TimeHint]:
    """从问句里解析时间范围；没有明确说法时返回 None（**不猜**）。"""
    text = str(question or "")
    if not text.strip():
        return None
    today = (now or datetime.now()).date()

    match = _YM.search(text)
    if match:
        year, month = int(match.group(1)), int(match.group(2))
        if 1 <= month <= 12:
            return _month_range(year, month)

    match = _RECENT_DAYS.search(text)
    if match:
        days = max(1, min(int(match.group(1)), 3660))
        start = today - timedelta(days=days - 1)
        return TimeHint(start.isoformat(), today.isoformat(), f"近{days}天")

    if _TODAY.search(text):
        return TimeHint(today.isoformat(), today.isoformat(), "今天")

    if _THIS_MONTH.search(text):
        return _month_range(today.year, today.month)

    if _LAST_MONTH.search(text):
        year, month = (today.year, today.month - 1) if today.month > 1 \
            else (today.year - 1, 12)
        return _month_range(year, month)

    match = _YEAR.search(text)
    if match:
        year = int(match.group(1))
        return TimeHint(f"{year:04d}-01-01", f"{year:04d}-12-31", f"{year:04d}")

    match = _MONTH.search(text)
    if match:
        month = int(match.group(1))
        if 1 <= month <= 12:
            # 无年份：取当前年；若该月还没到，按「去年同月」理解（问历史数据更常见）。
            year = today.year if month <= today.month else today.year - 1
            return _month_range(year, month)

    return None


__all__ = ["TimeHint", "parse_time_hint"]
