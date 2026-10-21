"""平台监控 · Token 监控读接口编排层（K1–K4）。

职责（薄编排，业务规则在此，取数在 :mod:`capabilities.platform.repository.monitor_token`）：

- 时间区间解析与 ``from >= to`` 校验（抛 ``1001 InvalidParameterError``）；
- ``metric`` 白名单校验（``total`` / ``input`` / ``output``）—— **只校验、不裁剪响应**：
  无论 ``metric`` 取何值，K2 响应恒返四个量（2026-10-12 需求方拍板）；
- 分页换算 ``page`` / ``page_size`` → ``offset`` / ``limit``；
- 把仓储返回的行 / 字典组装成 ``dtos/monitor.py`` 的出参 DTO。

租户
----
**不按租户过滤**（显式例外）：可见性只凭权限码 ``platform:monitor:read``。
本层同样不调 ``require_tenant``。

口径偏差留痕
------------
``avg_tokens_per_call`` 的分母只算「有用量回传」的调用；``completeness_rate`` /
``unmetered_count`` 也基于 ``total_tokens > 0`` 判定。三者都建立在
「``total_tokens`` 默认 0、**无法区分真 0 与未回传**」这一已知偏差上，属**有偏近似**
（详见仓储模块 docstring）。
"""

from datetime import datetime, timezone
from typing import List, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from capabilities.platform.dtos.monitor import (
    MonitorFlags,
    TokenModelTopItem,
    TokenSummary,
    TokenTrendPoint,
    TokenUsageItem,
)
from capabilities.platform.repository.monitor_token import MonitorTokenRepository
from capabilities.platform.services.monitor_range import enforce_max_range
from capabilities.platform.services.monitor_resilience import (
    MODULE_ERROR,
    resilient as _resilient,
)
from jonex_core.common.exceptions import InvalidParameterError

#: 降级态 ``module_error`` 取值（脱敏：只标「本模块暂不可用」，不带底层异常内容）。
_MODULE_ERROR = MODULE_ERROR

#: K2 ``metric`` 白名单（仅校验，不影响响应形状）。
_METRICS = ("total", "input", "output")


def _parse_ts(value: str, field: str) -> datetime:
    """ISO8601 字符串 → tz-aware ``datetime``（缺时区按 UTC 兜底）。

    与 ``services/monitor_call.py`` 的 ``_parse_ts`` 同义（不跨类 import，各自实现）。
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


def _float(value) -> Optional[float]:
    """数值归一成 ``float``；``None`` 原样透传（空集返 ``null``，不兜底 0）。"""
    return None if value is None else float(value)


# ══════════════════════════════════════════════════════════════════
# 降级保护层（共用装饰器在 ``monitor_resilience``；此处只放空壳工厂）
# ══════════════════════════════════════════════════════════════════


def _empty_summary(*_args, **_kwargs) -> TokenSummary:
    """K1 降级空壳（结构完整：全零 / ``None`` / 空 as-of + ``module_error`` 非空）。"""
    return TokenSummary(
        call_count=0,
        total_tokens=0,
        avg_tokens_per_call=None,
        model_count=0,
        completeness_rate=None,
        flags=MonitorFlags(module_error=_MODULE_ERROR),
        as_of="",
    )


def _empty_list(*_args, **_kwargs) -> list:
    """列表读接口降级空壳：``[]``（K2 趋势 / K3 TOP N）。"""
    return []


def _empty_pair(*_args, **_kwargs) -> Tuple[list, int]:
    """分页读接口降级空壳：``(items, total)``（K4 用量明细）。"""
    return [], 0


class MonitorTokenService:
    """Token 监控读接口（K1–K4）编排层。

    ``session`` 由路由经 ``get_db`` 注入；本服务**不**做租户过滤（见模块 docstring）。
    """

    def __init__(self, session: AsyncSession):
        self.session = session
        self.repo = MonitorTokenRepository(session)

    # ------------------------------------------------------------------
    # 编排内部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_range(frm: str, to: str) -> Tuple[datetime, datetime]:
        """解析并校验 ``[from, to)``；``from >= to`` 抛 ``1001``（参数不合法）。"""
        start = _parse_ts(frm, "from")
        end = _parse_ts(to, "to")
        if start >= end:
            raise InvalidParameterError(
                message="时间范围非法：from 必须早于 to",
                details={"from": _iso(start), "to": _iso(end)},
            )
        # 超过自定义查询上限（默认 30 天）→ 4004；与参数格式错误区分。
        return enforce_max_range(start, end)

    @staticmethod
    def _offset(page: int, page_size: int) -> int:
        """``page``（1 起）→ ``offset``（换算归 route / service 层，不由仓储承担）。"""
        return (page - 1) * page_size

    @staticmethod
    def _validate_metric(metric: Optional[str]) -> str:
        """``metric`` 白名单校验（只校验、不裁剪响应）。

        ``metric`` 是**选填**（默认 ``total``，2026-10-12 需求方拍板）：``None`` 按
        ``total`` 处理，**不报错**；只有传了非 ``total`` / ``input`` / ``output`` 的
        **非法值**（含空串）才抛 ``1001``。无论取何值，响应恒返四个量。
        """
        if metric is None:
            return "total"
        if metric not in _METRICS:
            raise InvalidParameterError(
                message="metric 取值非法，仅支持 total / input / output",
                details={"metric": metric, "allowed": list(_METRICS)},
            )
        return metric

    @staticmethod
    def _flags() -> MonitorFlags:
        """公共标志位；本期无「采集滞后」「桩数据」依据，保持默认（``module_error=None``）。"""
        return MonitorFlags()

    # ------------------------------------------------------------------
    # K1 · 统计卡
    # ------------------------------------------------------------------

    @_resilient(_empty_summary)
    async def summary(self, frm: str, to: str) -> TokenSummary:
        """K1 Token 用量统计卡。"""
        start, end = self._parse_range(frm, to)
        data = await self.repo.summary(start, end)
        return TokenSummary(
            call_count=int(data["call_count"] or 0),
            total_tokens=int(data["total_tokens"] or 0),
            avg_tokens_per_call=_float(data["avg_tokens_per_call"]),
            model_count=int(data["model_count"] or 0),
            completeness_rate=_float(data["completeness_rate"]),
            flags=self._flags(),
            # 空集时 `as_of` 返回**空串**（与验收测试一致，见
            # test_monitor_token_acceptance.TestEmptySetRedLine）—— 不得兜成当前时刻，
            # 否则前端把「这段时间没有用量」画成「刚刚更新」。
            as_of=_iso(data["as_of"]) or "",
        )

    # ------------------------------------------------------------------
    # K2 · 趋势
    # ------------------------------------------------------------------

    @_resilient(_empty_list)
    async def trend(
        self, frm: str, to: str, *, metric: Optional[str], bucket: str
    ) -> List[TokenTrendPoint]:
        """K2 Token 用量趋势。

        ``metric`` **选填**（缺省取 ``total``，不报错）、只校验不改响应：无论
        ``total`` / ``input`` / ``output``，每点恒返 ``total_tokens`` /
        ``prompt_tokens`` / ``completion_tokens`` / ``call_count``（2026-10-12 需求方拍板）。
        """
        start, end = self._parse_range(frm, to)
        self._validate_metric(metric)
        rows = await self.repo.trend(start, end, bucket)
        return [
            TokenTrendPoint(
                bucket_start=row["bucket_start"],
                total_tokens=int(row["total_tokens"] or 0),
                prompt_tokens=int(row["prompt_tokens"] or 0),
                completion_tokens=int(row["completion_tokens"] or 0),
                call_count=int(row["call_count"] or 0),
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # K3 · 模型 TOP N
    # ------------------------------------------------------------------

    @_resilient(_empty_list)
    async def top_models(
        self, frm: str, to: str, *, limit: int = 5
    ) -> List[TokenModelTopItem]:
        """K3 Token 用量模型 TOP N。"""
        start, end = self._parse_range(frm, to)
        rows = await self.repo.top_models(start, end, limit)
        return [
            TokenModelTopItem(
                model_id=row["model_id"],
                total_tokens=int(row["total_tokens"] or 0),
                ratio=_float(row["ratio"]),
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # K4 · 用量明细列表（分页）
    # ------------------------------------------------------------------

    @_resilient(_empty_pair)
    async def usage_list(
        self,
        frm: str,
        to: str,
        *,
        keyword: Optional[str] = None,
        sort_by: str = "total_tokens",
        sort_order: str = "desc",
        page: int = 1,
        page_size: int = 5,
    ) -> Tuple[List[TokenUsageItem], int]:
        """K4 按模型聚合的 Token 用量明细（分页），返回 ``(items, total)``。"""
        start, end = self._parse_range(frm, to)
        rows, total = await self.repo.usage_list(
            start,
            end,
            keyword,
            sort_by,
            sort_order,
            self._offset(page, page_size),
            page_size,
        )
        items = [
            TokenUsageItem(
                model_id=row["model_id"],
                model_name=row["model_name"],
                call_count=int(row["call_count"] or 0),
                prompt_tokens=int(row["prompt_tokens"] or 0),
                completion_tokens=int(row["completion_tokens"] or 0),
                total_tokens=int(row["total_tokens"] or 0),
                avg_tokens_per_call=_float(row["avg_tokens_per_call"]),
                last_called_at=row["last_called_at"],
                unmetered_count=int(row["unmetered_count"] or 0),
            )
            for row in rows
        ]
        return items, total


__all__ = ["MonitorTokenService"]
