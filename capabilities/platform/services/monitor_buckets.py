"""平台监控 · 桶时间网格（T2 / C5 / K2 三族共用）。

``bucket = 5m / hour / day`` 三档都由这里补空桶（2026-10-30 起 hour / day 也补，
此前只有 5m 补）。三族 service 统一走 :func:`bucket_starts` 按粒度分派。

网格语义
--------
:func:`five_minute_starts` 返回的是 ``[start, end)`` 内**最后 n 个** 5 分钟槽的
起点（``n = max(1, ceil((end - start) / 5min))``），首尾都对齐到 5 分钟边界：

* ``last = floor(end - 1µs)`` —— ``end`` 恰好落在 5 分钟边界时，不额外产生一个
  起点等于 ``end`` 的空桶；
* 由 ``last`` 向前回推 ``n`` 个槽。由于 ``n`` 是向上取整，**最左侧那个不完整的
  槽可能被排除**（最多丢 5 分钟）。这是为了满足产品口径「1 小时窗恰好 12 个点」
  的既定取舍：``10:32:17 ~ 11:32:17``（跨度恰好 1h）会返回 ``10:35 ~ 11:30``
  共 12 个点，``10:30~10:35`` 这个只覆盖了 2 分 43 秒的首槽不返回。

时区
----
对 ``naive`` 输入返回 ``naive``（按**墙钟**截断，北京 +8 是整小时偏移，不影响
5 分钟边界）；对 ``aware`` 输入按 **UTC 时间戳**对齐到 300 秒，再换回原时区，
因此返回值与入参同「naive / aware + 时区」形态，各 service 可直接与仓储行作
key 合并。
"""

from datetime import datetime, timedelta, timezone
from typing import List
from zoneinfo import ZoneInfo

#: 小时 / 日桶宽。
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)

#: ``hour`` / ``day`` 的自然边界按平台时区（北京）截断。日界尤其不能按 UTC：
#: 北京 00:00 在 UTC 是前一天 16:00。
SHANGHAI = ZoneInfo("Asia/Shanghai")

#: 5 分钟桶宽。
FIVE_MINUTES = timedelta(minutes=5)

#: 5 分钟对应的微秒数（用于精确向上取整，避免 timedelta 浮点除法）。
_FIVE_MINUTE_MICROS = 5 * 60 * 1_000_000


def floor_to_five_minutes(value: datetime) -> datetime:
    """截断到最近的 5 分钟边界（保留入参的 naive / aware 形态）。"""
    if value.tzinfo is None:
        # naive：墙钟直接截断。北京 +8 是整小时偏移，5 分钟边界与 UTC 一致。
        return value.replace(
            minute=value.minute - value.minute % 5, second=0, microsecond=0
        )
    # aware：先换算到 UTC，按 300 秒（5 分钟）对齐，再换回原时区。
    utc = value.astimezone(timezone.utc)
    floored = utc.replace(second=0, microsecond=0) - timedelta(minutes=utc.minute % 5)
    return floored.astimezone(value.tzinfo)


def five_minute_starts(start: datetime, end: datetime) -> List[datetime]:
    """生成覆盖 ``[start, end)`` 最后 n 个 5 分钟槽的起点（升序）。

    ``n = max(1, ceil((end - start) / 5min))``；``last = floor(end - 1µs)``。
    最左侧不完整槽可能被排除（见模块 docstring），这是「1 小时 12 点」的既定取舍。

    调用方需保证 ``start < end``（三族 service 均已在 ``_parse_range`` 校验）。
    """
    span = end - start
    total_micros = (span.days * 86_400 + span.seconds) * 1_000_000 + span.microseconds
    count = max(1, (total_micros + _FIVE_MINUTE_MICROS - 1) // _FIVE_MINUTE_MICROS)
    last = floor_to_five_minutes(end - timedelta(microseconds=1))
    return [last - (count - 1 - index) * FIVE_MINUTES for index in range(count)]


def _align_to_natural_boundary(value: datetime, bucket: str) -> datetime:
    """截断到自然小时 / 自然日边界（保留入参的 naive / aware 形态）。"""
    if bucket == "hour":
        return value.replace(minute=0, second=0, microsecond=0)
    return value.replace(hour=0, minute=0, second=0, microsecond=0)


def bucket_starts(start: datetime, end: datetime, bucket: str) -> List[datetime]:
    """生成 ``[start, end)`` 内全部桶起点（升序），按粒度分派。

    * ``5m``：原样委托 :func:`five_minute_starts`（语义见其 docstring，保持不变）。
    * ``hour`` / ``day``：覆盖 ``[start, end)`` 内全部自然小时/日桶，不含右端点
      （``bucket_start == end`` 不产生）。**首部不完整桶保留**：第一个桶的起点取
      自然边界（可能早于 ``start``），其值仍只统计窗口内数据 —— 这样起点差几分钟
      也不会丢掉整个小时/天的数据。``14:00–16:00`` 仍是 2 个点（14:00 本身即边界）。

    时区：``naive`` 入参按墙钟截断（任务族是北京墙钟）；``aware`` 入参先在
    **Asia/Shanghai** 截断，再换回入参原时区（调用族 / Token 族是 UTC），
    日桶因此以北京日界为准。返回值与入参同「naive / aware + 时区」形态。
    """
    if bucket == "5m":
        return five_minute_starts(start, end)
    if bucket not in ("hour", "day"):
        raise ValueError(f"unsupported bucket: {bucket}")
    if start >= end:
        return []

    aware = start.tzinfo is not None
    if aware:
        local_start = start.astimezone(SHANGHAI)
        local_end = end.astimezone(SHANGHAI)
    else:
        local_start, local_end = start, end

    step = HOUR if bucket == "hour" else DAY
    # 首部桶保留：起点取自然边界，即使它早于 ``start``。该桶的值仍只统计
    # ``[start, end)`` 内的数据（仓储按窗口过滤），所以不会把窗口外数据算进来，
    # 也不会因为起点差几分钟就丢掉一整个小时/一天。
    current = _align_to_natural_boundary(local_start, bucket)
    # 右端点不含：``end`` 恰好落在边界时，最后一个起点取它的前一格。
    last = _align_to_natural_boundary(local_end - timedelta(microseconds=1), bucket)

    starts: List[datetime] = []
    while current <= last:
        starts.append(current)
        current += step

    if aware:
        origin_tz = start.tzinfo
        return [value.astimezone(origin_tz) for value in starts]
    return starts


__all__ = [
    "DAY",
    "FIVE_MINUTES",
    "HOUR",
    "SHANGHAI",
    "bucket_starts",
    "floor_to_five_minutes",
    "five_minute_starts",
]
