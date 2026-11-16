"""平台监控 · 未归因调用（``tenant_id='unknown'``）汇总仓储。

⛔ 本仓是监控读侧**唯一**不做生效租户过滤的仓储，属**受控例外**，理由：

  这些行**不属于任何租户**（网关兜底值 ``unknown``），按租户过滤等于永远查不到。
  但「查不到」不等于「不存在」——它们在 ``metering.llm_usage_log`` 里真实占用了
  Token，租户维度的页面把这块抹掉后，用户看到的总量与账单对不上却无从解释。
  所以单独开一个**只读、只聚合、不返回任何租户标识**的汇总面。

与之配套的三条红线：

1. **不返回任何逐行 / 可归属数据**：只出总次数、总 Token、时间边界与 scene 分布，
   不选 ``request_id`` / ``user_id`` / ``kb_id`` / ``doc_id``（这些列在 unknown 行里
   本就全空，选它们只会制造「可能归因」的错觉）。
2. **调用次数恒用 ``SUM(call_count)``**，⛔ 不用 ``COUNT(*)``：embedding 走聚合写入
   （一行代表 N 次请求，见 ``repository/monitor_token.py`` 模块 docstring 硬约束 1），
   按行数统计会系统性低估。
3. **调用侧仍不收 unknown**：采集器 ``_ILLEGAL_TENANT_LITERALS`` 与
   ``services/monitor_scope.require_monitor_tenant`` 都不改。本仓只服务「说明缺了多少」，
   不把 unknown 变成一种租户。

时间过滤沿用监控族口径：左闭右开 ``[start, to)``，禁 ``BETWEEN``。
"""

from datetime import datetime, timezone
from typing import Any, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from capabilities.platform.repository.base import BaseRepository
from jonex_core.common.monitor_models import LlmUsageLog

#: 网关在调用方未携带租户上下文时落的兜底字面量（``jonex_core/llm_gateway``）。
#: 与 ``services/monitor_scope._ILLEGAL_EFFECTIVE_TENANTS`` 同源语义、不同用途：
#: 那边是「不准当租户用」，这里是「要统计它们有多少」。
UNATTRIBUTED_TENANT = "unknown"

#: scene 分布取前几名（页面提示里只是「这些调用大致来自哪些场景」，不需要全量）。
_TOP_SCENES = 5


def _iso(value: Optional[datetime]) -> Optional[str]:
    """``datetime`` → UTC ISO8601（``Z`` 结尾）；``None`` 原样透传。"""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _cell(row: Any, name: str) -> Any:
    """取聚合行某一列；属性缺失时返 ``None``（替身聚合行兜底，同 ``monitor_token``）。"""
    return getattr(row, name, None)


class MonitorUnattributedRepository(BaseRepository[LlmUsageLog]):
    """未归因调用汇总取数（``tenant_id='unknown'``，平台级、不按租户过滤）。

    ``session`` 由 ``BaseRepository`` 提供；本仓**不收** ``tenant_id`` —— 收了就会
    引导后来者把它落到 ``WHERE`` 里，正好破坏本仓存在的意义。
    """

    model = LlmUsageLog

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    # ------------------------------------------------------------------
    # 口径件
    # ------------------------------------------------------------------

    def _time_filter(self, start: datetime, to: datetime) -> list:
        """左闭右开 ``[start, to)``；调用方决定是否叠加未归因条件。"""
        return [LlmUsageLog.created_at >= start, LlmUsageLog.created_at < to]

    def _unattributed_filter(self, start: datetime, to: datetime) -> list:
        """未归因行 + 时间窗（全仓唯一出处）。"""
        return [LlmUsageLog.tenant_id == UNATTRIBUTED_TENANT, *self._time_filter(start, to)]

    # ------------------------------------------------------------------
    # 汇总
    # ------------------------------------------------------------------

    async def summary(self, start: datetime, to: datetime) -> dict:
        """未归因总量。

        返回 ``call_count`` / ``total_tokens`` / ``first_seen_at`` / ``last_seen_at``；
        空窗口下三个数值为 0 / 0、两个时间边界为 ``None``（不兜当前时刻 ——
        否则「这段时间没漏」会被画成「刚刚漏了」）。
        """
        stmt = select(
            func.sum(LlmUsageLog.call_count).label("call_count"),
            func.sum(LlmUsageLog.total_tokens).label("total_tokens"),
            func.min(LlmUsageLog.created_at).label("first_seen_at"),
            func.max(LlmUsageLog.created_at).label("last_seen_at"),
        ).where(*self._unattributed_filter(start, to))
        row = (await self.session.execute(stmt)).one()
        return {
            "call_count": int(_cell(row, "call_count") or 0),
            "total_tokens": int(_cell(row, "total_tokens") or 0),
            "first_seen_at": _cell(row, "first_seen_at"),
            "last_seen_at": _cell(row, "last_seen_at"),
        }

    async def scenes(self, start: datetime, to: datetime, limit: int = _TOP_SCENES) -> List[dict]:
        """未归因调用按 ``scene`` 的分布（Token 降序前 ``limit`` 条）。

        ``scene`` 与 ``tenant`` 是两条独立头：链式网关只剥租户时 scene 仍保留
        （如 ``lightrag_embed``），所以这里能给出「漏的是哪类调用」的线索。
        """
        scene_col = LlmUsageLog.scene
        stmt = (
            select(
                scene_col.label("scene"),
                func.sum(LlmUsageLog.call_count).label("call_count"),
                func.sum(LlmUsageLog.total_tokens).label("total_tokens"),
            )
            .where(*self._unattributed_filter(start, to))
            .group_by(scene_col)
            .order_by(func.sum(LlmUsageLog.total_tokens).desc(), scene_col.asc())
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).all()
        return [
            {
                "scene": str(_cell(row, "scene") or UNATTRIBUTED_TENANT),
                "call_count": int(_cell(row, "call_count") or 0),
                "total_tokens": int(_cell(row, "total_tokens") or 0),
            }
            for row in rows
        ]

    async def platform_totals(self, start: datetime, to: datetime) -> dict:
        """同窗口**全平台**总量（占比分母）。

        ⚠️ 这是本仓唯一会扫到全部租户的查询，**只用于算比率，绝不把分母本身
        返回给调用方** —— 分母 + 未归因量可反推平台调用规模，对租户管理员是泄漏。
        """
        stmt = select(
            func.sum(LlmUsageLog.call_count).label("call_count"),
            func.sum(LlmUsageLog.total_tokens).label("total_tokens"),
        ).where(*self._time_filter(start, to))
        row = (await self.session.execute(stmt)).one()
        return {
            "call_count": int(_cell(row, "call_count") or 0),
            "total_tokens": int(_cell(row, "total_tokens") or 0),
        }


__all__ = ["MonitorUnattributedRepository", "UNATTRIBUTED_TENANT"]
