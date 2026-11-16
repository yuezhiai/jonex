"""平台监控 · 未归因调用（``tenant_id='unknown'``）汇总编排层。

口径：**不把 ``unknown`` 建模成租户，而是建模成「归因失败状态」**。因此本服务：

- **不**参与租户维度的 18 条读接口，也**不**接收 ``unknown`` 作为生效租户；
- 只回答一个问题：「你看到的这些统计里，另有多少调用因为没带租户标识而没被算进去」；
- 返回的只有平台级汇总（次数 / Token / 时间边界 / scene 分布），**没有任何可归属到
  具体租户的行级字段**。

占比（``share_of_*``）对非平台管理员一律返 ``null``
-------------------------------------------------
占比的分母是「同窗口全平台总量」。分母与未归因绝对量一起给出，等于把平台整体
调用规模暴露给租户管理员。故本服务收一个 ``include_share``（由路由按
``platform:admin`` 权限码判定传入）：为假时**根本不查分母**（少一次全表聚合），
返回 ``null``；前端据此不渲染百分比文案。

为什么不做「本租户占比」替代：未归因行不属于任何租户，拿它去除某个租户的用量
得到的是一个没有业务含义的数字。
"""

from datetime import datetime, timezone
from typing import List, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from capabilities.platform.dtos.monitor import (
    MonitorFlags,
    UnattributedSceneItem,
    UnattributedSummary,
)
from capabilities.platform.repository.monitor_unattributed import (
    MonitorUnattributedRepository,
)
from capabilities.platform.services.monitor_range import enforce_max_range
from capabilities.platform.services.monitor_resilience import (
    MODULE_ERROR,
    resilient as _resilient,
)
from jonex_core.common.exceptions import InvalidParameterError

#: 降级态 ``module_error`` 取值（脱敏，同三族既有口径）。
_MODULE_ERROR = MODULE_ERROR


def _parse_ts(value: str, field: str) -> datetime:
    """ISO8601 字符串 → tz-aware ``datetime``（缺时区按 UTC 兜底）。

    与 ``services/monitor_token.py`` / ``services/monitor_call.py`` 的 ``_parse_ts``
    同义（按既有约定各自实现，不跨模块 import 私有函数）。
    """
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise InvalidParameterError(
            message=f"{field} 必须是 ISO8601 时间字符串",
            details={"field": field, "value": str(value)},
        )
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    """``datetime`` → UTC ISO8601（``Z`` 结尾）；``None`` 原样透传。"""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _ratio(numerator: int, denominator: int) -> Optional[float]:
    """比率；分母 0 ⇒ ``None``（不兜 0：口径同三族「空窗口不给假百分比」）。"""
    return None if denominator <= 0 else numerator / denominator


def _empty_summary(*_args, **_kwargs) -> UnattributedSummary:
    """降级空壳：结构完整、全零 / ``None`` / 空列表 + ``module_error`` 非空。"""
    return UnattributedSummary(
        call_count=0,
        total_tokens=0,
        first_seen_at=None,
        last_seen_at=None,
        scenes=[],
        share_of_calls=None,
        share_of_tokens=None,
        flags=MonitorFlags(module_error=_MODULE_ERROR),
    )


class MonitorUnattributedService:
    """未归因调用汇总编排层。

    ``include_share`` 由路由按权限判定后传入：``False`` 时不查平台分母。
    """

    def __init__(self, session: AsyncSession, *, include_share: bool = False):
        self.session = session
        self.include_share = include_share
        self.repo = MonitorUnattributedRepository(session)

    @staticmethod
    def _parse_range(frm: str, to: str) -> Tuple[datetime, datetime]:
        """解析并校验 ``[from, to)``；``from >= to`` 抛 ``1001``，超上限抛 ``4004``。"""
        start = _parse_ts(frm, "from")
        end = _parse_ts(to, "to")
        if start >= end:
            raise InvalidParameterError(
                message="时间范围非法：from 必须早于 to",
                details={"from": _iso(start), "to": _iso(end)},
            )
        return enforce_max_range(start, end)

    @staticmethod
    def _flags() -> MonitorFlags:
        """公共标志位；未归因汇总没有额外「采集滞后」依据，保持默认。"""
        return MonitorFlags()

    @_resilient(_empty_summary)
    async def summary(self, frm: str, to: str) -> UnattributedSummary:
        """未归因调用汇总。"""
        start, end = self._parse_range(frm, to)
        data = await self.repo.summary(start, end)
        scenes: List[dict] = await self.repo.scenes(start, end)

        share_of_calls: Optional[float] = None
        share_of_tokens: Optional[float] = None
        if self.include_share:
            totals = await self.repo.platform_totals(start, end)
            share_of_calls = _ratio(int(data["call_count"] or 0), int(totals["call_count"] or 0))
            share_of_tokens = _ratio(int(data["total_tokens"] or 0), int(totals["total_tokens"] or 0))

        return UnattributedSummary(
            call_count=int(data["call_count"] or 0),
            total_tokens=int(data["total_tokens"] or 0),
            first_seen_at=_iso(data["first_seen_at"]),
            last_seen_at=_iso(data["last_seen_at"]),
            scenes=[UnattributedSceneItem(**item) for item in scenes],
            share_of_calls=share_of_calls,
            share_of_tokens=share_of_tokens,
            flags=self._flags(),
        )


__all__ = ["MonitorUnattributedService"]
