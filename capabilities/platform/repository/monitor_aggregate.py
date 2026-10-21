"""平台监控 · 调用监控聚合族查询仓储（C1 / C2 / C3 / C4 / C5 / C9 / M1）。

本文件只负责**聚合族**取数；明细族（C6/C7/C8）在另一个仓储文件里，两者零重叠。

口径（本仓统一遵守，勿逐方法各写一套）
--------------------------------------
- 时延一律显式 ``ROUND(AVG(EXTRACT(EPOCH FROM (ended - started))) * 1000)::int``
  换算成毫秒（``*_ms: int``）；空集 ``AVG`` 返 ``NULL``，不得兜底为 0。
- 空集取值：计数类返 ``0``；比率 / 均值类返 ``null``（本仓以 ``None`` 表达，未取到不给 0）。
- 正常模式（``metric_mode='logical'``）取 ``monitor.call_log``。
- 降级模式（``metric_mode='degraded'``）取 ``monitor.call_attempt``。
- 降级触发判定 —— ``call_log.attempt_count`` 与 ``call_attempt`` 行数对账，
  ``unlinkable_rows > 0`` ⇒ 整次查询走降级公式。落在 :meth:`count_unlinkable_rows`
  与 :meth:`resolve_metric_mode`。

硬约束
------
**查询一律不过滤 ``tenant_id``**（显式例外）—— 监控可见性只凭权限码
``platform:monitor:read``，谁拿到码谁看全平台。因此本仓储：

* 不调用 ``require_tenant()``、不读 ``TenantContext``；
* 不调用 ``BaseRepository`` 里任何自带租户 / 软删过滤的通用方法
  （``list_all`` / ``count`` / ``get_by_id`` 等），只借用它持有的 ``self.session``。

继承 ``BaseRepository`` 仅是为对齐平台仓储写法（``model`` / ``session``）。

时间与分页口径
--------------
* 时间过滤一律左闭右开 ``[start, to)``：``col >= start AND col < to``（禁 ``BETWEEN``）。
* ``start`` / ``to`` 是唯一真值源（ISO8601 UTC 在 service 层解析成 ``datetime`` 后传入）；
  ``range_preset`` 只做标签、不参与计算，故**本仓不接收该参数**。
* 分页以 ``offset`` / ``limit`` 为载体，``page`` / ``page_size``（1..100）的换算在 route 层。

已知表结构缺口（未擅改，仅在此留痕）
------------------------------------
* ``monitor.call_log`` **无 ``object_type`` 列**：C2/C4 的「对象类型」列与 ``object_type``
  筛选无物理落点。这里改用既有 ``protocol`` 列**推导**对象类型
  （见 :data:`OBJECT_TYPE_BY_PROTOCOL`）—— ``object_id`` 仍映射 ``target``（C9 按对象标识
  寻址），但 ``object_type`` **不再是** ``target`` 的别名：筛选入参在 service 层换算成
  ``protocol``；「对象类型」实际只出推导得到的 2 类。
* 「外部依赖」与「开放能力」两页签（C1/C2 vs C3/C4）**无区分列**：物理列只有 ``target``
  与 ``protocol``。本仓只提供 ``target`` / ``protocol`` 两个通用筛选，两页签的切分留给 service。
* ``monitor.call_attempt`` **无 ``target`` / ``protocol`` / ``source`` 列**：降级模式下做
  对象聚合（C2/C4/C9）时，本仓以 ``call_attempt LEFT JOIN call_log ON request_id`` 借用
  这三列（LEFT JOIN 保证「N2 先于 N1 落库」的孤儿 attempt 不被漏计，计数仍走降级口径）。
"""

from datetime import datetime
from typing import Any, Optional, Tuple

from sqlalchemy import Integer, cast, extract, func, select, text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from capabilities.platform.repository.base import BaseRepository
from jonex_core.common.exceptions import InvalidParameterError
from jonex_core.common.monitor_models import CallAttempt, CallLog

#: 成功率分母取值集：cancelled / running / unknown 不进分母。
_DENOMINATOR_STATUSES = ("success", "failed")

#: 对象类型推导表：``monitor.call_log`` **无**
#: ``object_type`` 列，用既有 ``protocol`` 列推导对象类型 ——
#: ``openai`` ⇒ 「模型服务」（llm-gateway 出站把模型名写进 ``target``）、
#: ``invoke`` ⇒ 「能力」（sidecar 把能力 ID 写进 ``target``）。
#: 「外部服务 / 外部平台」本对应 ``mcp`` / ``http``，但这两个 protocol
#: 在全后端**无任何采集写入方** ⇒ 实际只能出 2 类，不补空列。
OBJECT_TYPE_BY_PROTOCOL: dict[str, str] = {
    "openai": "模型服务",
    "invoke": "能力",
}

#: 趋势桶粒度白名单（自然小时 / 自然日）。
_BUCKETS = ("hour", "day")

#: 三族趋势统一按**北京自然时/日**切桶（固定口径，不读配置）。
#: 平台设置里的 timezone 仅存不用，⛔ 不得作为配置源；固定常量，便于测试防回退。
#: ⚠️ 只影响分桶边界，不影响输出 ISO 口径：C5/K2 仍回 UTC ``Z``，T2 仍回 ``+08:00``。
_TZ = "Asia/Shanghai"

#: 趋势指标白名单（C5 契约）。
_METRICS = ("call_count", "success_rate", "avg_latency", "failed_count")

#: 模式元数据的两个取值。
_MODE_LOGICAL = "logical"
_MODE_DEGRADED = "degraded"


def _rate(success: Optional[int], failed: Optional[int]) -> Optional[float]:
    """成功率 = success ÷ (success + failed)，分母为 0 返 ``None``。"""
    numerator = int(success or 0)
    denominator = numerator + int(failed or 0)
    if denominator <= 0:
        return None
    return numerator / denominator


def _to_int(value: Any) -> Optional[int]:
    """``ROUND(...)::int`` 结果可能是 ``Decimal``；空集为 ``None``（不兜底 0）。"""
    return None if value is None else int(value)


class MonitorAggregateRepository(BaseRepository[CallLog]):
    """调用监控聚合族取数仓储。

    ``session`` 由 ``BaseRepository`` 提供；本类**不**复用其租户 / 软删语义（见模块 docstring）。
    """

    model = CallLog

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    # ------------------------------------------------------------------
    # 取数源：正常模式 = call_log，降级模式 = call_attempt（借 call_log 的对象列）
    # ------------------------------------------------------------------

    def _metric_columns(
        self, degraded: bool
    ) -> Tuple[Any, Any, Any, Any, Any, Any]:
        """按模式返回 ``(status, started, ended, target, protocol, source)`` 六列。

        * 正常模式：六列全部来自 ``monitor.call_log``。
        * 降级模式：计数 / 时间 / 状态取自 ``monitor.call_attempt``；
          ``target`` / ``protocol`` / ``source`` **借用** ``monitor.call_log``（见 docstring 缺口条）。
        """
        if degraded:
            return (
                CallAttempt.status,
                CallAttempt.attempt_started_at,
                CallAttempt.attempt_ended_at,
                CallLog.target,
                CallLog.protocol,
                CallLog.source,
            )
        return (
            CallLog.final_status,
            CallLog.logical_started_at,
            CallLog.final_ended_at,
            CallLog.target,
            CallLog.protocol,
            CallLog.source,
        )

    @staticmethod
    def _apply_from(stmt: Any, degraded: bool) -> Any:
        """降级模式下挂上 ``call_attempt LEFT JOIN call_log``；正常模式从 ``call_log`` 起。"""
        if degraded:
            return stmt.select_from(CallAttempt).outerjoin(
                CallLog, CallLog.request_id == CallAttempt.request_id
            )
        return stmt

    def _time_and_filters(
        self,
        degraded: bool,
        start: datetime,
        to: datetime,
        *,
        direction: Optional[str] = None,
        target: Optional[str] = None,
        protocol: Optional[str] = None,
        source: Optional[str] = None,
        exclude_null_target: bool = False,
    ) -> list[Any]:
        """统一构造 ``[start, to)`` + 可选筛选的 WHERE 条件列表。

        ``direction`` 恒取 ``CallLog.direction``：正常模式本就从 ``call_log`` 起，
        降级模式由 :meth:`_apply_from` 挂上了 ``call_attempt LEFT JOIN call_log``，
        故两种模式下这一列都可达（``call_attempt`` 自身没有 ``direction``）。
        """
        _, started, _, target_col, protocol_col, source_col = self._metric_columns(degraded)
        conditions: list[Any] = [started >= start, started < to]
        if direction is not None:
            conditions.append(CallLog.direction == direction)
        if target is not None:
            conditions.append(target_col == target)
        if protocol is not None:
            conditions.append(protocol_col == protocol)
        if source is not None:
            conditions.append(source_col == source)
        if exclude_null_target:
            conditions.append(target_col.isnot(None))
        return conditions

    @staticmethod
    def _avg_latency_ms_expr(ended: Any, started: Any) -> Any:
        """``ROUND(AVG(EXTRACT(EPOCH FROM (ended - started))) * 1000)::int``。

        ``AVG`` 天然跳过 NULL，故「未结束的逻辑调用不进均值」自动成立。
        """
        return cast(func.round(func.avg(extract("epoch", ended - started)) * 1000), Integer)

    # ------------------------------------------------------------------
    # 降级触发判定
    # ------------------------------------------------------------------

    async def count_unlinkable_rows(self, start: datetime, to: datetime) -> int:
        """对账 ``call_log.attempt_count`` 与 ``call_attempt`` 实际行数，返回不一致行数。

        返回 ``> 0`` ⇒ 重试不可关联 ⇒ 整次查询走降级公式。
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
                CallLog.logical_started_at < to,
                CallLog.attempt_count != subquery,
            )
        )
        result = await self.session.execute(stmt)
        return int(result.scalar() or 0)

    async def resolve_metric_mode(self, start: datetime, to: datetime) -> str:
        """降级判定落点：``unlinkable_rows > 0`` ⇒ ``'degraded'``，否则 ``'logical'``。

        同一响应只判一次、整响应统一（禁按行切换）。
        """
        unlinkable = await self.count_unlinkable_rows(start, to)
        return _MODE_DEGRADED if unlinkable > 0 else _MODE_LOGICAL

    # ------------------------------------------------------------------
    # C1 / C3 统计卡（同形，靠入参区分页签）
    # ------------------------------------------------------------------

    async def summary(
        self,
        start: datetime,
        to: datetime,
        *,
        direction: Optional[str] = None,
        target: Optional[str] = None,
        protocol: Optional[str] = None,
        degraded: bool = False,
    ) -> dict:
        """C1 外部依赖 / C3 开放能力统计卡取数（正常模式取 call_log、降级模式取 call_attempt）。

        ``direction`` 是两个页签的唯一分界（``outbound``=外部依赖 /
        ``inbound``=开放能力）—— ``monitor.call_log`` 没有 ``object_type`` 列，
        不靠它切，C1 与 C3 会返回同一组数字。

        返回：``total_calls`` / ``success_calls`` / ``failed_calls`` / ``success_rate`` /
        ``avg_latency_ms`` / ``as_of``。成功率由本仓按口径算好（分母 0 ⇒ ``None``），
        ``as_of`` 取 ``MAX(call_log.created_at)``（恒用 ``call_log``，反映落库时刻）。
        """
        status, started, ended, _, _, _ = self._metric_columns(degraded)
        stmt = select(
            func.count().label("total_calls"),
            func.count().filter(status == "success").label("success_calls"),
            func.count().filter(status == "failed").label("failed_calls"),
            self._avg_latency_ms_expr(ended, started).label("avg_latency_ms"),
        )
        stmt = self._apply_from(stmt, degraded)
        stmt = stmt.where(
            *self._time_and_filters(
                degraded, start, to, direction=direction, target=target, protocol=protocol
            )
        )
        row = (await self.session.execute(stmt)).one()

        return {
            "total_calls": int(row.total_calls or 0),
            "success_calls": int(row.success_calls or 0),
            "failed_calls": int(row.failed_calls or 0),
            "success_rate": _rate(row.success_calls, row.failed_calls),
            "avg_latency_ms": _to_int(row.avg_latency_ms),
            "as_of": await self.latest_ingest_at(),
        }

    async def latest_ingest_at(self) -> Optional[datetime]:
        """模块 as-of = ``MAX(monitor.call_log.created_at)``（反映落库时刻）。"""
        result = await self.session.execute(select(func.max(CallLog.created_at)))
        return result.scalar()

    # ------------------------------------------------------------------
    # C2 / C4 对象列表（分页）
    # ------------------------------------------------------------------

    async def object_list(
        self,
        start: datetime,
        to: datetime,
        *,
        direction: Optional[str] = None,
        target: Optional[str] = None,
        protocol: Optional[str] = None,
        degraded: bool = False,
        offset: int = 0,
        limit: int = 20,
    ) -> Tuple[list[dict], int]:
        """C2 / C4 对象列表取数，返回 ``(items, total)``。

        ``direction`` 同 :meth:`summary`：``outbound``=外部依赖(C2) / ``inbound``=开放能力(C4)。

        分组键 = ``(target, protocol)``（``target`` 即对象标识 ``object_id``）。行内
        ``object_type`` **由 ``protocol`` 推导**，不再是 ``target`` 别名。
        每项含调用量 / 成功率 / 平均响应时间 / 失败数 / 最近调用时间。
        ``target`` 为 NULL 的行不进对象列表（NULL 不构成「对象」）。
        排序：调用量倒序，再按 ``target`` 升序稳定排序（契约未规定，取稳定排序的常用默认）。
        """
        status, started, ended, target_col, protocol_col, _ = self._metric_columns(degraded)
        select_cols = (
            target_col.label("object_id"),
            protocol_col.label("protocol"),
            func.count().label("call_count"),
            func.count().filter(status == "success").label("success_calls"),
            func.count().filter(status == "failed").label("failed_calls"),
            self._avg_latency_ms_expr(ended, started).label("avg_latency_ms"),
            func.max(started).label("last_called_at"),
        )
        base_conditions = self._time_and_filters(
            degraded,
            start,
            to,
            direction=direction,
            target=target,
            protocol=protocol,
            exclude_null_target=True,
        )

        page_stmt = select(*select_cols)
        page_stmt = self._apply_from(page_stmt, degraded)
        page_stmt = (
            page_stmt.where(*base_conditions)
            .group_by(target_col, protocol_col)
            .order_by(func.count().desc(), target_col.asc())
            .offset(offset)
            .limit(limit)
        )
        rows = (await self.session.execute(page_stmt)).all()

        count_stmt = select(target_col, protocol_col)
        count_stmt = self._apply_from(count_stmt, degraded)
        grouped = (
            count_stmt.where(*base_conditions).group_by(target_col, protocol_col).subquery()
        )
        total = int(
            (await self.session.execute(select(func.count()).select_from(grouped))).scalar() or 0
        )

        items = [
            {
                "object_id": row.object_id,
                # object_type 由 protocol 推导（不再是 target 别名）；
                # 未知 protocol（mcp / http / NULL）⇒ None，由 service 兜空串。
                "object_type": OBJECT_TYPE_BY_PROTOCOL.get(row.protocol),
                "protocol": row.protocol,
                "call_count": int(row.call_count or 0),
                "success_calls": int(row.success_calls or 0),
                "failed_calls": int(row.failed_calls or 0),
                "success_rate": _rate(row.success_calls, row.failed_calls),
                "avg_latency_ms": _to_int(row.avg_latency_ms),
                "last_called_at": row.last_called_at,
            }
            for row in rows
        ]
        return items, total

    # ------------------------------------------------------------------
    # C5 趋势
    # ------------------------------------------------------------------

    async def trend(
        self,
        start: datetime,
        to: datetime,
        *,
        metric: str,
        bucket: str,
        target: Optional[str] = None,
        protocol: Optional[str] = None,
        direction: Optional[str] = None,
        tz: str = _TZ,
        degraded: bool = False,
    ) -> list[dict]:
        """C5 趋势取数：按 ``metric`` 与 ``bucket``（hour/day）分桶，返回桶序列。

        每个桶返回 ``{bucket_start, value, call_count, success_calls, failed_calls, avg_latency_ms}``；
        ``value`` 由 ``metric`` 决定，比率 / 均值在空集时为 ``None``。
        「未闭合桶标进行中」由 service 层按同一个 ``now()`` 快照判定，本仓不判。

        分桶默认按 ``_TZ``（Asia/Shanghai）的自然时/日切，**不依赖 DB session 时区**；
        ``bucket_start`` 仍是 ``timestamptz``，由 service 统一转 UTC ``Z`` 输出。
        """
        if bucket not in _BUCKETS:
            raise InvalidParameterError(
                message="bucket 取值非法，仅支持 hour / day",
                details={"bucket": bucket, "allowed": list(_BUCKETS)},
            )
        if metric not in _METRICS:
            raise InvalidParameterError(
                message="metric 取值非法",
                details={"metric": metric, "allowed": list(_METRICS)},
            )

        status, started, ended, _, _, _ = self._metric_columns(degraded)
        bucket_expr = func.timezone(tz, func.date_trunc(bucket, func.timezone(tz, started)))
        stmt = select(
            bucket_expr.label("bucket_start"),
            func.count().label("call_count"),
            func.count().filter(status == "success").label("success_calls"),
            func.count().filter(status == "failed").label("failed_calls"),
            self._avg_latency_ms_expr(ended, started).label("avg_latency_ms"),
        )
        stmt = self._apply_from(stmt, degraded)
        stmt = (
            stmt.where(
                *self._time_and_filters(
                    degraded, start, to, target=target, protocol=protocol, direction=direction
                )
            )
            .group_by(bucket_expr)
            .order_by(bucket_expr.asc())
        )
        rows = (await self.session.execute(stmt)).all()

        points = []
        for row in rows:
            call_count = int(row.call_count or 0)
            failed_count = int(row.failed_calls or 0)
            if metric == "call_count":
                value: Optional[float] = call_count
            elif metric == "failed_count":
                value = failed_count
            elif metric == "success_rate":
                value = _rate(row.success_calls, row.failed_calls)
            else:  # avg_latency
                value = _to_int(row.avg_latency_ms)
            points.append(
                {
                    "bucket_start": row.bucket_start,
                    "value": value,
                    "call_count": call_count,
                    "success_calls": int(row.success_calls or 0),
                    "failed_calls": failed_count,
                    "avg_latency_ms": _to_int(row.avg_latency_ms),
                }
            )
        return points

    # ------------------------------------------------------------------
    # C9 对象 / 能力详情
    # ------------------------------------------------------------------

    async def object_detail(
        self,
        start: datetime,
        to: datetime,
        *,
        object_id: str,
        protocol: Optional[str] = None,
        degraded: bool = False,
        top_sources_limit: int = 3,
        recent_failures_limit: int = 3,
    ) -> Optional[dict]:
        """C9 对象 / 能力详情取数：基础指标 + 来源 TOP3 + 最近失败调用。

        未命中任何行 ⇒ 返 ``None``（由 service 决定 404 还是空壳）。
        """
        items, _ = await self.object_list(
            start,
            to,
            target=object_id,
            protocol=protocol,
            degraded=degraded,
            offset=0,
            limit=1,
        )
        if not items:
            return None
        base = items[0]

        top_sources = await self._top_sources(
            start, to, object_id=object_id, protocol=protocol, degraded=degraded,
            limit=top_sources_limit,
        )
        recent_failures = await self._recent_failures(
            start, to, object_id=object_id, protocol=protocol, degraded=degraded,
            limit=recent_failures_limit,
        )

        return {
            "object_id": base["object_id"],
            "object_type": base["object_type"],
            "protocol": base["protocol"],
            "call_count": base["call_count"],
            "success_calls": base["success_calls"],
            "failed_calls": base["failed_calls"],
            "success_rate": base["success_rate"],
            "avg_latency_ms": base["avg_latency_ms"],
            "top_sources": top_sources,
            "recent_failures": recent_failures,
        }

    async def _top_sources(
        self,
        start: datetime,
        to: datetime,
        *,
        object_id: str,
        protocol: Optional[str],
        degraded: bool,
        limit: int,
    ) -> list[dict]:
        """来源 / 调用方 TOP N（每项 ``{name, call_count}``）。"""
        _, started, _, target_col, protocol_col, source_col = self._metric_columns(degraded)
        stmt = select(
            source_col.label("name"),
            func.count().label("call_count"),
        )
        stmt = self._apply_from(stmt, degraded)
        stmt = (
            stmt.where(
                *self._time_and_filters(
                    degraded, start, to, target=object_id, protocol=protocol
                ),
                source_col.isnot(None),
            )
            .group_by(source_col)
            .order_by(func.count().desc(), source_col.asc())
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).all()
        return [{"name": row.name, "call_count": int(row.call_count or 0)} for row in rows]

    async def _recent_failures(
        self,
        start: datetime,
        to: datetime,
        *,
        object_id: str,
        protocol: Optional[str],
        degraded: bool,
        limit: int,
    ) -> list[dict]:
        """最近失败调用（每项 ``{request_id, error_message, logical_started_at}``），按时间倒序。

        降级模式取 ``call_attempt``（其 ``request_id`` 可重复、``attempt_started_at`` 顶替时间列）。
        """
        status, started, _, target_col, protocol_col, _ = self._metric_columns(degraded)
        request_col = CallAttempt.request_id if degraded else CallLog.request_id
        error_col = CallAttempt.error_message if degraded else CallLog.error_message
        conditions = self._time_and_filters(
            degraded, start, to, target=object_id, protocol=protocol
        )
        stmt = select(
            request_col.label("request_id"),
            error_col.label("error_message"),
            started.label("logical_started_at"),
        )
        stmt = self._apply_from(stmt, degraded)
        stmt = (
            stmt.where(*conditions, status == "failed")
            .order_by(started.desc())
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).all()
        return [
            {
                "request_id": row.request_id,
                "error_message": row.error_message,
                "logical_started_at": row.logical_started_at,
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # M1 筛选元数据
    # ------------------------------------------------------------------

    async def list_call_sources(self, limit: int = 200) -> list[str]:
        """``monitor.call_log.source`` 的 distinct 值（M1 ``call_sources``，跳过 NULL）。"""
        stmt = (
            select(CallLog.source)
            .where(CallLog.source.isnot(None))
            .distinct()
            .order_by(CallLog.source.asc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return [value for value in result.scalars().all() if value]

    async def list_task_types(self, limit: int = 200) -> list[dict]:
        """任务类型列表（M1 ``task_types``，取自 ``task_center.task_catalog``）。

        每项 ``{"code", "name"}``：``code`` 是**筛选值**（= ``task_catalog_code``，
        与 T2/T3/T4 的 ``task_type`` 同源，前端拿它直接筛）；``name`` 是**中文名**
        （``task_catalog.name``，前端下拉直接显示，⛔ 前端不得本地写死）。

        ⚠️ 这是本仓**唯一**一处跨能力取数：``task_types`` 是「任务监控」页的筛选下拉，
        数据不在 ``monitor`` 数据层，唯一的真值源是任务中心的内置任务清单表
        （``capabilities/task_center/models/catalog.py`` 明写「平台预置、只读、不带租户」）。
        这里取「直读字典表」而非新建跨服务调用链，避免为一张只读字典引入运行时耦合。

        该表是**全局字典**、无 ``tenant_id``、无软删列，故同样不涉及租户过滤。
        ⚠️ platform 容器不含 ``capabilities/task_center/`` 代码（见
        ``deploy/docker/capability.Dockerfile``），禁止 import task_center ORM 模型
        ——容器内必抛 ``ModuleNotFoundError``。故同 ``mcp_service_repository.py`` 先例，
        跨能力只读一律用 ``sqlalchemy.text()`` 原始 SQL。
        """
        result = await self.session.execute(
            sa_text("""
                SELECT code, name
                FROM task_center.task_catalog
                ORDER BY code
                LIMIT :limit
            """),
            {"limit": limit},
        )
        return [{"code": row[0], "name": row[1]} for row in result.all() if row[0]]

    async def list_objects(self, limit: int = 200) -> list[dict]:
        """对象类型列表（M1 ``objects``，每项 ``{object_type, protocol}``）。

        ``object_type`` 由 ``protocol`` 推导（见 :data:`OBJECT_TYPE_BY_PROTOCOL`）
        —— **不再**按 ``target`` 出「对象名」，故只保留有写入方的两个 protocol
        （``openai`` / ``invoke``），出参收口为 2 类。
        ⚠️ 任务类型列表（``task_types``）与状态字典（``statuses``）落在 task_center 枚举，
        不由本仓提供。
        """
        stmt = (
            select(CallLog.protocol.label("protocol"))
            .where(CallLog.protocol.in_(tuple(OBJECT_TYPE_BY_PROTOCOL)))
            .distinct()
            .order_by(CallLog.protocol.asc())
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).all()
        return [
            {"object_type": OBJECT_TYPE_BY_PROTOCOL[row.protocol], "protocol": row.protocol}
            for row in rows
        ]


__all__ = ["MonitorAggregateRepository", "OBJECT_TYPE_BY_PROTOCOL"]
