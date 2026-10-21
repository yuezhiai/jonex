"""平台监控 · Token 监控族查询仓储（K1–K4）。

数据源唯一 = 既有表 ``metering.llm_usage_log``（LLM 网关计量明细，见
``deploy/postgres/migrations/002_platform.sql``）。本仓**只读**聚合，不写、不建表。

⛔ 三条硬约束（K1–K4 一起守）
------------------------------
1. **调用次数恒用 ``SUM(call_count)``** —— 绝不用 ``COUNT(DISTINCT request_id)``。
   embedding 聚合行的 ``request_id`` 是合成键 ``emb-agg:{sha1(...)}``（一行代表 N 次请求），
   去重计数会系统性低估（实测 -84.5%）⇒ 本仓 SQL 里**不出现** ``request_id``。
2. 时间过滤左闭右开 ``[start, to)``：``created_at >= start AND created_at < to``，禁 ``BETWEEN``。
3. **绝不过滤 ``tenant_id``** —— 监控族可见性只凭权限码 ``platform:monitor:read``
   （2026-10-10 需求方拍板，显式例外）。⇒ 本仓只选**聚合列 / ``model`` 列**，
   ⛔ 不 ``select(LlmUsageLog)`` 整表选列（那会把 ``tenant_id`` 带进 SQL 被验收判红）。

口径（2026-10-12 需求方拍板；近似口径连同已知偏差一并留痕）
-------------------------------------------------------------
* ``avg_tokens_per_call`` 的分母**只算「有用量回传」的调用**：:

      SUM(total_tokens) FILTER (WHERE total_tokens > 0)
      / NULLIF(SUM(call_count) FILTER (WHERE total_tokens > 0), 0)

* ``completeness_rate`` = ``COALESCE(SUM(call_count) FILTER (WHERE total_tokens > 0), 0)
  / NULLIF(SUM(call_count), 0)``。
  分子 FILTER 无命中时 SQL 返回 ``NULL``（不是 0），故必须 ``COALESCE`` 成 0 ——
  否则「窗口内有调用、但全部未回传用量」会错误地返回 ``null``；分母 ``NULLIF`` 保留，
  空窗口仍返回 ``null``。
* ``unmetered_count`` = ``SUM(call_count) FILTER (WHERE total_tokens = 0)``。

⚠️ 后两条基于「``total_tokens`` 默认 0、**无法区分真 0 与未回传**」这一已知偏差，
属**有偏近似** —— 待采集侧补「是否回传」标志后再收紧。本期照拍板实现，不改采集链路。

``model_id`` / ``model_name`` 本期**同取**物理列 ``model``（库内只有一列）。
``bucket_start`` / ``as_of`` / ``last_called_at`` 在仓储层已转成 UTC ISO8601 字符串。
"""

from datetime import datetime, timezone
from typing import Any, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from capabilities.platform.repository.base import BaseRepository
from jonex_core.common.exceptions import InvalidParameterError
from jonex_core.common.monitor_models import LlmUsageLog

#: 趋势桶粒度白名单（自然小时 / 自然日）。
_BUCKETS = ("hour", "day")

#: 三族趋势统一按**北京自然时/日**切桶（固定口径，不读配置）。
#: 平台设置里的 timezone 仅存不用，⛔ 不得作为配置源；固定常量，便于测试防回退。
#: ⚠️ 只影响分桶边界：``bucket_start`` 仍由 ``_iso`` 转 UTC ``Z`` 输出。
_TZ = "Asia/Shanghai"

#: K4 排序字段白名单 —— **7 个合法值**（默认 ``total_tokens``）。
#: ⛔ 别删任何一个：前端明细表三个排序点（模型名 / 均值 / 最近调用）都依赖它，
#: 白名单缩水会让它们「点下去没反应」而非报错（越界才 1001）。
_SORT_BY = (
    "model",
    "call_count",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "avg_tokens_per_call",
    "last_called_at",
)

#: K4 排序方向白名单。
_SORT_ORDER = ("asc", "desc")

#: **可空**排序列 —— 这些列的聚合值在「空组 / 组内全 NULL」时可能是 NULL。
#: 无 ``NULLS LAST`` 时 Postgres 默认 desc 把 NULL 排最前、asc 排最后，
#: 会让「没有均值的模型」霸占榜首 ⇒ 这些列一律显式 ``NULLS LAST``。
#: 可空性依据 ``jonex_core/common/monitor_models.py``：``total_tokens`` / ``prompt_tokens`` /
#: ``completion_tokens`` / ``created_at`` 在 DDL 里都没有 NOT NULL（故其 SUM / MAX 可为 NULL），
#: ``avg_tokens_per_call`` 经 ``NULLIF`` 兜底本身即可空。``model`` / ``call_count`` 是
#: NOT NULL，不补（补了是无意义噪音，且会让 NOT NULL 断言失去反例价值）。
_NULLABLE_SORT_BY = frozenset(
    {
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "avg_tokens_per_call",
        "last_called_at",
    }
)


def _iso(value: Optional[datetime]) -> Optional[str]:
    """``datetime`` → UTC ISO8601（``Z`` 结尾）；``None`` 原样透传。

    与 ``services/monitor_call.py`` 的 ``_iso`` 同义（不跨类 import，各自实现）。
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _to_float(value: Any) -> Optional[float]:
    """数值归一成 ``float``；``None`` 原样透传（空集返 ``null``，不兜底 0）。"""
    return None if value is None else float(value)


def _cell(row: Any, name: str) -> Any:
    """取聚合行某一列；属性缺失时返 ``None``。

    真 ``Row`` 恒带 ``select()`` 里 ``label()`` 声明的列，此处仅对「替身聚合行」
    做兜底 —— 让服务在假 DB / 空集下不因缺列抛 ``AttributeError`` 静默变 500。
    """
    return getattr(row, name, None)


class MonitorTokenRepository(BaseRepository[LlmUsageLog]):
    """Token 监控族（K1–K4）取数仓储。

    ``session`` 由 ``BaseRepository`` 提供；本类**不**复用其租户 / 软删语义
    （监控族不做租户过滤，见模块 docstring）。
    """

    model = LlmUsageLog

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    # ------------------------------------------------------------------
    # 口径件（本仓统一遵守，勿逐方法各写一套）
    # ------------------------------------------------------------------

    @staticmethod
    def _time_filter(start: datetime, to: datetime) -> list:
        """左闭右开 ``[start, to)`` 的时间条件（禁 ``BETWEEN``）。"""
        return [LlmUsageLog.created_at >= start, LlmUsageLog.created_at < to]

    @staticmethod
    def _metered_calls() -> Any:
        """已回传有效用量的调用次数 = ``SUM(call_count) FILTER (WHERE total_tokens > 0)``。"""
        return func.sum(LlmUsageLog.call_count).filter(LlmUsageLog.total_tokens > 0)

    @staticmethod
    def _unmetered_calls() -> Any:
        """未计量调用次数 = ``SUM(call_count) FILTER (WHERE total_tokens = 0)``。"""
        return func.sum(LlmUsageLog.call_count).filter(LlmUsageLog.total_tokens == 0)

    @staticmethod
    def _avg_tokens_expr() -> Any:
        """平均单次 Token；分母只算「有用量回传」的调用，分母 0 用 ``NULLIF`` 兜（返 ``null``）。"""
        return (
            func.sum(LlmUsageLog.total_tokens).filter(LlmUsageLog.total_tokens > 0)
            / func.nullif(MonitorTokenRepository._metered_calls(), 0)
        )

    # ------------------------------------------------------------------
    # K1 · 统计卡
    # ------------------------------------------------------------------

    async def summary(self, start: datetime, to: datetime) -> dict:
        """K1 Token 用量统计卡取数。

        返回 ``call_count`` / ``total_tokens`` / ``prompt_tokens`` / ``completion_tokens`` /
        ``model_count`` / ``completeness_rate`` / ``avg_tokens_per_call`` /
        ``unmetered_count`` / ``as_of``。

        * ``call_count`` = ``SUM(call_count)``（⛔ 非 ``COUNT(DISTINCT request_id)``）；
        * ``model_count`` = ``COUNT(DISTINCT model)``；
        * 比率 / 均值空集（分母 0）返 ``None``，不兜底 0；
        * ``as_of`` = ``MAX(created_at)``（空表返 ``None``，由 service 兜空串）。
        """
        metered = self._metered_calls()
        all_calls = func.sum(LlmUsageLog.call_count)
        stmt = select(
            all_calls.label("call_count"),
            func.sum(LlmUsageLog.total_tokens).label("total_tokens"),
            func.sum(LlmUsageLog.prompt_tokens).label("prompt_tokens"),
            func.sum(LlmUsageLog.completion_tokens).label("completion_tokens"),
            func.count(func.distinct(LlmUsageLog.model)).label("model_count"),
            metered.label("metered_calls"),
            (
                func.coalesce(metered, 0) / func.nullif(all_calls, 0)
            ).label("completeness_rate"),
            self._avg_tokens_expr().label("avg_tokens_per_call"),
            self._unmetered_calls().label("unmetered_count"),
            func.max(LlmUsageLog.created_at).label("as_of"),
        ).where(*self._time_filter(start, to))
        row = (await self.session.execute(stmt)).one()
        return {
            "call_count": int(_cell(row, "call_count") or 0),
            "total_tokens": int(_cell(row, "total_tokens") or 0),
            "prompt_tokens": int(_cell(row, "prompt_tokens") or 0),
            "completion_tokens": int(_cell(row, "completion_tokens") or 0),
            "model_count": int(_cell(row, "model_count") or 0),
            "completeness_rate": _to_float(_cell(row, "completeness_rate")),
            "avg_tokens_per_call": _to_float(_cell(row, "avg_tokens_per_call")),
            "unmetered_count": int(_cell(row, "unmetered_count") or 0),
            "as_of": _cell(row, "as_of"),
        }

    # ------------------------------------------------------------------
    # K2 · 趋势
    # ------------------------------------------------------------------

    async def trend(self, start: datetime, to: datetime, bucket: str) -> list:
        """K2 Token 用量趋势：按 ``bucket``（hour/day）分桶，桶升序。

        ``bucket`` 越界抛 ``1001``（白名单校验，存储层职责）。
        每点含 ``bucket_start``（UTC ISO8601 字符串）/ ``total_tokens`` /
        ``prompt_tokens`` / ``completion_tokens`` / ``call_count``。

        ⚠️ ``metric`` 只做校验、**不参与取数** —— 响应恒返四个量（2026-10-12 拍板），
        故本方法不接收 ``metric``。
        """
        if bucket not in _BUCKETS:
            raise InvalidParameterError(
                message="bucket 取值非法，仅支持 hour / day",
                details={"bucket": bucket, "allowed": list(_BUCKETS)},
            )
        # 先转北京墙钟再 date_trunc，最后转回 timestamptz —— 显式时区，不依赖
        # DB session 的 TimeZone 设置。输出仍是 UTC ISO8601（``_iso``）。
        bucket_expr = func.timezone(
            _TZ, func.date_trunc(bucket, func.timezone(_TZ, LlmUsageLog.created_at))
        )
        stmt = (
            select(
                bucket_expr.label("bucket_start"),
                func.sum(LlmUsageLog.total_tokens).label("total_tokens"),
                func.sum(LlmUsageLog.prompt_tokens).label("prompt_tokens"),
                func.sum(LlmUsageLog.completion_tokens).label("completion_tokens"),
                func.sum(LlmUsageLog.call_count).label("call_count"),
            )
            .where(*self._time_filter(start, to))
            .group_by(bucket_expr)
            .order_by(bucket_expr.asc())
        )
        rows = (await self.session.execute(stmt)).all()
        return [
            {
                "bucket_start": _iso(row.bucket_start) or "",
                "total_tokens": int(row.total_tokens or 0),
                "prompt_tokens": int(row.prompt_tokens or 0),
                "completion_tokens": int(row.completion_tokens or 0),
                "call_count": int(row.call_count or 0),
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # K3 · 模型 TOP N
    # ------------------------------------------------------------------

    async def top_models(self, start: datetime, to: datetime, limit: int = 5) -> list:
        """K3 Token 用量模型 TOP N（按 total_tokens 倒序）。

        占比 = 本模型 ``SUM(total_tokens)`` ÷ **全量** ``SUM(total_tokens)``，
        全量用窗口 ``SUM(...) OVER ()`` 取；分母 0 用 ``NULLIF`` 兜（返 ``null``，
        ⛔ 不得除零、也不得兜成 0）。每项 ``model_id`` / ``total_tokens`` / ``ratio``。
        """
        total = func.sum(LlmUsageLog.total_tokens)
        grand_total = func.sum(total).over()
        stmt = (
            select(
                LlmUsageLog.model.label("model_id"),
                total.label("total_tokens"),
                (total * 1.0 / func.nullif(grand_total, 0)).label("ratio"),
            )
            .where(*self._time_filter(start, to))
            .group_by(LlmUsageLog.model)
            .order_by(total.desc(), LlmUsageLog.model.asc())
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).all()
        return [
            {
                "model_id": row.model_id or "",
                "total_tokens": int(row.total_tokens or 0),
                "ratio": _to_float(row.ratio),
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # K4 · 用量明细列表（分页）
    # ------------------------------------------------------------------

    async def usage_list(
        self,
        start: datetime,
        to: datetime,
        keyword: Optional[str],
        sort_by: str,
        sort_order: str,
        offset: int,
        limit: int,
    ) -> Tuple[list, int]:
        """K4 按模型聚合的 Token 用量明细（分页），返回 ``(items, total)``。

        ``keyword`` 对 ``model`` 做大小写不敏感模糊匹配（无命中即空集，⛔ 不是 4xx）；
        ``sort_by`` / ``sort_order`` 越界抛 ``1001``。
        两段式分页：页查询（对分组取分页）+ 独立 ``count``（对分组子查询计数）。
        """
        if sort_by not in _SORT_BY:
            raise InvalidParameterError(
                message="sort_by 取值非法",
                details={"sort_by": sort_by, "allowed": list(_SORT_BY)},
            )
        if sort_order not in _SORT_ORDER:
            raise InvalidParameterError(
                message="sort_order 取值非法",
                details={"sort_order": sort_order, "allowed": list(_SORT_ORDER)},
            )

        conditions = self._time_filter(start, to)
        if keyword:
            conditions.append(LlmUsageLog.model.ilike(f"%{keyword}%"))

        # 排序表达式 = 页查询 SELECT 里同名聚合列的复算（Postgres 允许按聚合表达式排序）。
        # ``model`` / ``last_called_at`` 与 SELECT 里的 label 一一对应，排序口径不会漂。
        sort_columns = {
            "model": LlmUsageLog.model,
            "call_count": func.sum(LlmUsageLog.call_count),
            "prompt_tokens": func.sum(LlmUsageLog.prompt_tokens),
            "completion_tokens": func.sum(LlmUsageLog.completion_tokens),
            "total_tokens": func.sum(LlmUsageLog.total_tokens),
            "avg_tokens_per_call": self._avg_tokens_expr(),
            "last_called_at": func.max(LlmUsageLog.created_at),
        }
        order_expr = sort_columns[sort_by]
        order_expr = order_expr.desc() if sort_order == "desc" else order_expr.asc()
        if sort_by in _NULLABLE_SORT_BY:
            # 可空列恒 NULLS LAST：desc 时 NULL 不占榜首、asc 时也不吊车尾（口径统一）。
            order_expr = order_expr.nulls_last()

        page_stmt = (
            select(
                # model_id / model_name 本期同取物理列 model（库内只有一列）
                LlmUsageLog.model.label("model_id"),
                LlmUsageLog.model.label("model_name"),
                func.sum(LlmUsageLog.call_count).label("call_count"),
                func.sum(LlmUsageLog.prompt_tokens).label("prompt_tokens"),
                func.sum(LlmUsageLog.completion_tokens).label("completion_tokens"),
                func.sum(LlmUsageLog.total_tokens).label("total_tokens"),
                self._avg_tokens_expr().label("avg_tokens_per_call"),
                func.max(LlmUsageLog.created_at).label("last_called_at"),
                self._unmetered_calls().label("unmetered_count"),
            )
            .where(*conditions)
            .group_by(LlmUsageLog.model)
            .order_by(order_expr, LlmUsageLog.model.asc())
            .offset(offset)
            .limit(limit)
        )
        rows = (await self.session.execute(page_stmt)).all()

        grouped = (
            select(LlmUsageLog.model).where(*conditions).group_by(LlmUsageLog.model).subquery()
        )
        total = int(
            (
                await self.session.execute(select(func.count()).select_from(grouped))
            ).scalar()
            or 0
        )

        items = [
            {
                "model_id": row.model_id or "",
                "model_name": row.model_name or "",
                "call_count": int(row.call_count or 0),
                "prompt_tokens": int(row.prompt_tokens or 0),
                "completion_tokens": int(row.completion_tokens or 0),
                "total_tokens": int(row.total_tokens or 0),
                "avg_tokens_per_call": _to_float(row.avg_tokens_per_call),
                "last_called_at": _iso(row.last_called_at),
                "unmetered_count": int(row.unmetered_count or 0),
            }
            for row in rows
        ]
        return items, total


__all__ = ["MonitorTokenRepository"]
