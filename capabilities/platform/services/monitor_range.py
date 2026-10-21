"""监控三族读接口共用的「查询时间范围上限」校验。

口径：

- 自定义查询上限 **30 天**（``MONITOR_QUERY_MAX_RANGE_DAYS``，``<= 0`` 关闭校验）；
- 保留期 **90 天**（``MONITOR_RETENTION_DAYS``，``<= 0`` 关闭校验）；**默认不开**，
  只有调用族（``monitor_call._parse_range``）显式传 ``enforce_retention=True`` ——
  清理只删 ``monitor.call_log`` / ``monitor.call_attempt``，Token / Task 的底层数据
  仍在，统一拒绝等于让「查询策略」超越「数据保留事实」；
- 超限返回 **4004** ``QuotaExceededError``，**不是** ``1001``
  —— 参数格式合法、只是数据量/时窗超出允许范围，因此复用既有 4004，不新增错误码。

边界：恰好等于上限（如 ``30d`` 预设、``now - 90d``）放行，只有**严格越界**才拒绝；
否则「近 30 天 / 近 90 天」按钮会在毫秒级的时间戳差异上被误判。

文案：4004 的通用词条是「配额已用尽」，直接被 i18n 本地化会盖掉这里的显式 message。
故本模块在抛出前按当前 locale 用 :func:`translate` 生成显式文案，并**不再传 ``params``**
（``exception_handler._resolve_error`` 分支②：显式 message + 无 params ⇒ 原样返回）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Tuple

from jonex_core.common.config import get_config
from jonex_core.common.exceptions import QuotaExceededError
from jonex_core.common.i18n import translate

#: 专用消息键（不占用 4004 的通用词条）。
_MESSAGE_TOO_WIDE = "monitor.query_range.too_wide"
_MESSAGE_OUT_OF_RETENTION = "monitor.query_range.out_of_retention"

#: details 的 ``reason`` 取值，供前端区分两种拒绝原因。
REASON_OUT_OF_RETENTION = "out_of_retention"
REASON_RANGE_TOO_WIDE = "range_too_wide"


def _as_aware(value: datetime) -> datetime:
    """朴素时间按 UTC 兜底（与 ``monitor_call._parse_ts`` 同口径），避免比较时 TypeError。"""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    """UTC ISO8601（``Z`` 结尾），与监控读接口出参同形。"""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def enforce_max_range(
    start: datetime,
    end: datetime,
    *,
    enforce_retention: bool = False,
    now_fn: Optional[Callable[[], datetime]] = None,
) -> Tuple[datetime, datetime]:
    """校验 ``[start, end)`` 的保留期与跨度；越界抛 ``4004``，合规原样返回。

    ``enforce_retention`` 默认 ``False``（Token / Task 族不传）；``now_fn`` 仅供
    单测注入固定时钟，生产走真实 UTC 当前时间。返回值保持调用方传入的原对象，
    不做时区改写（``_parse_ts`` 已保证生产路径为 tz-aware）。
    """
    cfg = get_config()
    max_days = int(cfg.MONITOR_QUERY_MAX_RANGE_DAYS)
    retention_days = int(cfg.MONITOR_RETENTION_DAYS)

    start_aware = _as_aware(start)
    end_aware = _as_aware(end)
    now = _as_aware((now_fn or (lambda: datetime.now(timezone.utc)))())

    if enforce_retention and retention_days > 0:
        earliest = now - timedelta(days=retention_days)
        if start_aware < earliest:
            raise QuotaExceededError(
                message=translate(
                    _MESSAGE_OUT_OF_RETENTION,
                    params={"retention_days": retention_days},
                    fallback=f"查询起点超出数据保留期（最多可查 {retention_days} 天）",
                ),
                details={
                    "reason": REASON_OUT_OF_RETENTION,
                    "retention_days": retention_days,
                    "queryable_range": {
                        "from": _iso(earliest),
                        "to": _iso(now),
                    },
                    "from": _iso(start_aware),
                    "to": _iso(end_aware),
                    "max_days": max_days,
                },
            )

    if max_days > 0 and (end_aware - start_aware) > timedelta(days=max_days):
        raise QuotaExceededError(
            message=translate(
                _MESSAGE_TOO_WIDE,
                params={"max_days": max_days},
                fallback=f"查询时间范围超过上限（最多 {max_days} 天）",
            ),
            details={
                "reason": REASON_RANGE_TOO_WIDE,
                "max_days": max_days,
                "from": _iso(start_aware),
                "to": _iso(end_aware),
            },
        )
    return start, end
