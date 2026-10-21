"""平台监控 · 调用明细族查询仓储（C6 / C7 / C8）。

覆盖「调用记录 / 单次调用详情 / Attempt 明细」三个**读**接口，是调用监控
「明细族」的唯一取数口（聚合族另有一文件，两文件零重叠）。

## 数据源与降级

两张 append-only 事件表（模型见 ``jonex_core/common/monitor_models.py``）：

- ``monitor.call_log``     N1 逻辑调用，``request_id`` **本表内唯一**（正常模式取数表）
- ``monitor.call_attempt`` N2 物理 Attempt，``request_id`` **可重复**（降级模式取数表）

降级判定：查询范围内
``call_log.attempt_count <> 该 request_id 的 call_attempt 行数`` 的记录数
（``unlinkable_rows``）``> 0`` ⇒ ``metric_mode='degraded'``，C6/C7/C8 一律改取
``monitor.call_attempt``；``= 0`` ⇒ ``metric_mode='logical'``，取 ``monitor.call_log``。
判定一次、整次查询统一，**禁止按行切换**。判定由 :meth:`resolve_metric_mode`
提供，调用方据此把 ``degraded`` 透传给三个查询方法。

## 硬约束

1. **一律不过滤 ``tenant_id``** —— **显式例外**：调用明细的
   可见性只凭 ``platform:monitor:read`` 一码，**不按租户/空间隔离**。故本文件
   **不调** ``require_tenant``、不读 ``TenantContext``，也不改 ``tenant.py``。
2. ``from``/``to`` 是唯一真值源，仓库以 **半开区间** ``[start, end)`` 承接
   （``>= start AND < end``，两种模式一致）。``range_preset`` 只做标签、
   不参与计算，**不进本层**。``from >= to`` 的 ``1001`` 由 service 层抛。
3. 分页：仓库层以 ``offset``/``limit`` 为载体，``page``/``page_size`` 的换算归
   route / service。
4. 排序固定：正常模式 ``logical_started_at DESC``，降级模式 ``attempt_started_at DESC``。

## 降级模式下 N1 专属筛选的落点（契约未规定，本层取法见下）

调用记录的 ``direction`` / ``trigger_type`` / ``protocol`` / ``source`` / ``target`` 都是
**N1 列**，``monitor.call_attempt`` 上**没有这些列**。契约只说
「降级改取 call_attempt」，未规定这几类筛选在降级下如何生效。本层取法：用
``CallAttempt.request_id IN (SELECT request_id FROM call_log WHERE <N1 条件>)``
**下推**到 N1 表求交集 —— ``call_log.request_id`` 唯一，子查询不会放大行数；找不到
对应 N1 行的 Attempt 会被排除。``status`` 与 ``keyword`` 中 Attempt 可自证的
部分（``attempt_id`` / ``request_id``）直接落在 N2 上。
"""

from datetime import datetime
from typing import Any, List, Optional, Sequence, Tuple

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from capabilities.platform.repository.base import BaseRepository
from jonex_core.common.exceptions import InvalidParameterError
from jonex_core.common.monitor_models import CallAttempt, CallLog

#: 降级模式下 C7 缺 ``attempt_id`` 时的报错文案（错误码沿用 1001 参数不合法）。
_MISSING_ATTEMPT_ID_MSG = "降级模式下查询调用详情必须提供 attempt_id"


def _like(pattern_value: str) -> str:
    """构造 ``ILIKE`` 模糊匹配串（大小写不敏感，对齐既有仓储 keyword 写法）。"""
    return f"%{pattern_value}%"


class MonitorDetailRepository(BaseRepository[CallLog]):
    """调用明细族（C6 / C7 / C8）查询仓储。

    ``model`` 取 :class:`CallLog`（N1）仅用于满足基类的泛型约束；本仓储跨
    ``call_log`` / ``call_attempt`` 两表取数，**不走**基类的租户/软删条件构造
    （明细表无软删语义，且**不做租户过滤**，见模块 docstring）。
    """

    model = CallLog

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    # ------------------------------------------------------------------
    # 降级判定
    # ------------------------------------------------------------------
    async def count_unlinkable(self, start: datetime, end: datetime) -> int:
        """统计 **重试不可关联** 的逻辑调用数（``unlinkable_rows``）。

        ``call_log.attempt_count`` 与该 ``request_id`` 的 ``call_attempt`` 行数不等
        即计入。返回 ``> 0`` 即应整次查询走降级模式。
        """
        subquery = (
            select(func.count())
            .select_from(CallAttempt)
            .where(CallAttempt.request_id == CallLog.request_id)
            .scalar_subquery()
        )
        stmt = (
            select(func.count())
            .select_from(CallLog)
            .where(
                CallLog.logical_started_at >= start,
                CallLog.logical_started_at < end,
                CallLog.attempt_count != subquery,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar() or 0

    async def resolve_metric_mode(self, start: datetime, end: datetime) -> str:
        """判定整次查询的模式：``'degraded'`` 或 ``'logical'``。

        判定一次、整响应统一 —— 调用方把结果转成 ``degraded: bool`` 后透传给
        :meth:`list_records` / :meth:`get_detail` / :meth:`list_attempts`。
        """
        return "degraded" if await self.count_unlinkable(start, end) > 0 else "logical"

    # ------------------------------------------------------------------
    # C6 · 调用明细列表（分页）
    # ------------------------------------------------------------------
    async def list_records(
        self,
        *,
        start: datetime,
        end: datetime,
        direction: Optional[str] = None,
        status: Optional[str] = None,
        trigger_type: Optional[str] = None,
        protocol: Optional[str] = None,
        source: Optional[str] = None,
        target: Optional[str] = None,
        keyword: Optional[str] = None,
        degraded: bool = False,
        offset: int = 0,
        limit: int = 20,
    ) -> Tuple[Sequence[Any], int]:
        """C6 调用明细列表。返回 ``(rows, total)``。

        - 正常模式（``degraded=False``）：取 ``monitor.call_log``，行 = 逻辑调用
          （``request_id`` 唯一），排序 ``logical_started_at DESC``。
        - 降级模式（``degraded=True``）：取 ``monitor.call_attempt``，行 = 物理
          Attempt（``request_id`` **可重复属预期**，非 bug），
          排序 ``attempt_started_at DESC``。

        筛选映射见模块 docstring「降级模式下 N1 专属筛选的落点」。
        """
        if degraded:
            conditions = self._attempt_record_conditions(
                start=start,
                end=end,
                status=status,
                keyword=keyword,
                direction=direction,
                trigger_type=trigger_type,
                protocol=protocol,
                source=source,
                target=target,
            )
            base = select(CallAttempt).where(*conditions)
            count_stmt = select(func.count()).select_from(CallAttempt).where(*conditions)
            order_by = CallAttempt.attempt_started_at.desc()
        else:
            conditions = self._logical_record_conditions(
                start=start,
                end=end,
                direction=direction,
                status=status,
                trigger_type=trigger_type,
                protocol=protocol,
                source=source,
                target=target,
                keyword=keyword,
            )
            base = select(CallLog).where(*conditions)
            count_stmt = select(func.count()).select_from(CallLog).where(*conditions)
            order_by = CallLog.logical_started_at.desc()

        rows_result = await self.session.execute(
            base.order_by(order_by).offset(offset).limit(limit)
        )
        rows = rows_result.scalars().all()
        total_result = await self.session.execute(count_stmt)
        return rows, (total_result.scalar() or 0)

    def _logical_record_conditions(
        self,
        *,
        start: datetime,
        end: datetime,
        direction: Optional[str],
        status: Optional[str],
        trigger_type: Optional[str],
        protocol: Optional[str],
        source: Optional[str],
        target: Optional[str],
        keyword: Optional[str],
    ) -> List[Any]:
        conditions: List[Any] = [
            CallLog.logical_started_at >= start,
            CallLog.logical_started_at < end,
        ]
        if direction:
            conditions.append(CallLog.direction == direction)
        if status:
            conditions.append(CallLog.final_status == status)
        if trigger_type:
            conditions.append(CallLog.trigger_type == trigger_type)
        if protocol:
            conditions.append(CallLog.protocol == protocol)
        if source:
            # 精确匹配（候选值由 M1 的 call_sources 提供）
            conditions.append(CallLog.source == source)
        if target:
            # 精确匹配：对象详情「查看全部调用」按 object_id（即 target）下钻，
            # 用 keyword 会把同前缀的其他对象一并带进来。
            conditions.append(CallLog.target == target)
        if keyword:
            pattern_value = _like(keyword)
            conditions.append(
                or_(
                    CallLog.source.ilike(pattern_value),
                    CallLog.target.ilike(pattern_value),
                    CallLog.request_id.ilike(pattern_value),
                )
            )
        return conditions

    def _attempt_record_conditions(
        self,
        *,
        start: datetime,
        end: datetime,
        status: Optional[str],
        keyword: Optional[str],
        direction: Optional[str],
        trigger_type: Optional[str],
        protocol: Optional[str],
        source: Optional[str],
        target: Optional[str],
    ) -> List[Any]:
        conditions: List[Any] = [
            CallAttempt.attempt_started_at >= start,
            CallAttempt.attempt_started_at < end,
        ]
        if status:
            conditions.append(CallAttempt.status == status)

        if keyword:
            pattern_value = _like(keyword)
            # Attempt 可自证的部分（attempt_id / request_id）直接落在 N2 上；
            # 「来源/调用方、目标」是 N1 列，用子查询下推求交集。
            conditions.append(
                or_(
                    CallAttempt.attempt_id.ilike(pattern_value),
                    CallAttempt.request_id.ilike(pattern_value),
                    CallAttempt.request_id.in_(
                        select(CallLog.request_id).where(
                            or_(
                                CallLog.source.ilike(pattern_value),
                                CallLog.target.ilike(pattern_value),
                            )
                        )
                    ),
                )
            )

        # N1 专属精确筛选（direction / trigger_type / protocol / source / target）：
        # call_attempt 无这些列，下推到 call_log 求 request_id 交集。
        n1_conditions: List[Any] = []
        if direction:
            n1_conditions.append(CallLog.direction == direction)
        if trigger_type:
            n1_conditions.append(CallLog.trigger_type == trigger_type)
        if protocol:
            n1_conditions.append(CallLog.protocol == protocol)
        if source:
            n1_conditions.append(CallLog.source == source)
        if target:
            n1_conditions.append(CallLog.target == target)
        if n1_conditions:
            conditions.append(
                CallAttempt.request_id.in_(
                    select(CallLog.request_id).where(*n1_conditions)
                )
            )
        return conditions

    # ------------------------------------------------------------------
    # C7 · 单次调用详情
    # ------------------------------------------------------------------
    async def get_call_log_by_request_id(self, request_id: str) -> Optional[CallLog]:
        """正常模式取数：按 ``request_id`` 命中 N1 唯一行（``call_log`` 内唯一）。"""
        result = await self.session.execute(
            select(CallLog).where(CallLog.request_id == request_id)
        )
        return result.scalar_one_or_none()

    async def get_call_logs_by_request_ids(
        self, request_ids: Sequence[str]
    ) -> dict:
        """按一组 ``request_id`` 批量取 N1 行 → ``{request_id: CallLog}``。

        供 **降级模式的 C6** 补 N1 列用（``call_attempt`` 行缺 ``direction`` /
        ``trigger_type`` / ``target`` / ``source`` / ``trace_id``）。一次 ``IN``
        查询替代逐行回查；查不到的 ``request_id``（N2 先于 N1 落库的孤儿 attempt）
        不出现在返回值里，由调用方兜底。
        """
        ids = {rid for rid in request_ids if rid}
        if not ids:
            return {}
        result = await self.session.execute(
            select(CallLog).where(CallLog.request_id.in_(ids))
        )
        return {row.request_id: row for row in result.scalars().all()}

    async def count_unlinkable_for_request(self, request_id: str) -> int:
        """**单条**对账：该 ``request_id`` 的 ``call_attempt`` 行数是否与 N1 对得上。

        与 :meth:`count_unlinkable` 同判据，只是把范围从「区间」收窄到
        「一条」—— C7 / C8 没有 ``from``/``to`` 参数，套不上区间判据。
        返回 ``> 0`` ⇒ 该条走降级模式。``call_log`` 无此 ``request_id`` 时返回 ``0``
        （无 N1 行可对账，交调用方按正常模式取，取不到再由 service 抛资源未找到）。
        """
        subquery = (
            select(func.count())
            .select_from(CallAttempt)
            .where(CallAttempt.request_id == CallLog.request_id)
            .scalar_subquery()
        )
        stmt = (
            select(func.count())
            .select_from(CallLog)
            .where(
                CallLog.request_id == request_id,
                CallLog.attempt_count != subquery,
            )
        )
        result = await self.session.execute(stmt)
        return int(result.scalar() or 0)

    async def get_attempt_by_attempt_id(self, attempt_id: str) -> Optional[CallAttempt]:
        """降级模式取数：按 ``attempt_id`` 命中 N2 唯一行（``uq_monitor_call_attempt_id``）。"""
        result = await self.session.execute(
            select(CallAttempt).where(CallAttempt.attempt_id == attempt_id)
        )
        return result.scalar_one_or_none()

    async def get_detail(
        self,
        request_id: str,
        *,
        attempt_id: Optional[str] = None,
        degraded: bool = False,
    ) -> Optional[Any]:
        """C7 单次调用详情。

        - 正常模式：``attempt_id`` **可省略**（带上则忽略），按 ``request_id`` 取 N1。
        - 降级模式：``attempt_id`` **必带**，缺失抛 ``InvalidParameterError``（码 ``1001``），
          按 ``attempt_id`` 取 N2（路径不变，``attempt_id`` 作可选查询参数）。

        返回 ``None`` 表示未命中，由 service 层决定是否抛资源未找到异常。
        """
        if degraded:
            if not attempt_id:
                raise InvalidParameterError(
                    message=_MISSING_ATTEMPT_ID_MSG,
                    details={"request_id": request_id},
                )
            return await self.get_attempt_by_attempt_id(attempt_id)
        return await self.get_call_log_by_request_id(request_id)

    # ------------------------------------------------------------------
    # C8 · Attempt 明细（分页）
    # ------------------------------------------------------------------
    async def list_attempts(
        self,
        request_id: str,
        *,
        offset: int = 0,
        limit: int = 20,
    ) -> Tuple[Sequence[CallAttempt], int]:
        """C8 某 ``request_id`` 下的 Attempt 明细（分页），按 ``attempt_no`` 升序。

        取数表恒为 ``monitor.call_attempt``（正常/降级同表）—— 降级模式下
        同一 ``request_id`` 可命中多行属预期。返回 ``(items, total)``。
        """
        conditions = [CallAttempt.request_id == request_id]
        rows_result = await self.session.execute(
            select(CallAttempt)
            .where(*conditions)
            .order_by(CallAttempt.attempt_no.asc())
            .offset(offset)
            .limit(limit)
        )
        items = rows_result.scalars().all()
        total_result = await self.session.execute(
            select(func.count()).select_from(CallAttempt).where(*conditions)
        )
        return items, (total_result.scalar() or 0)

    async def get_retry_count(self, request_id: str, *, degraded: bool = False) -> int:
        """``retry_count = max(attempt_count - 1, 0)``。

        - 正常模式：取 ``call_log.attempt_count``（N1 列承载）。
        - 降级模式：``attempt_count`` 本身即「不可关联」的那一列，故改按实际
          ``call_attempt`` 行数推导 ``max(count - 1, 0)``。

        未命中任何记录时返回 ``0``。
        """
        if degraded:
            result = await self.session.execute(
                select(func.count())
                .select_from(CallAttempt)
                .where(CallAttempt.request_id == request_id)
            )
            attempt_count = result.scalar() or 0
        else:
            result = await self.session.execute(
                select(CallLog.attempt_count).where(CallLog.request_id == request_id)
            )
            attempt_count = result.scalar()
            if attempt_count is None:
                attempt_count = 0
        return max(int(attempt_count) - 1, 0)

    # 注：M1 的 `call_sources`（C6 `source` 筛选用）**不再**在本文件提供 ——
    # 曾与聚合族各写一份（同一 SQL 两处维护，行为易漂移），已收敛到
    # `MonitorAggregateRepository.list_call_sources` 一处。


__all__ = ["MonitorDetailRepository"]
