"""平台监控 · 调用监控聚合族查询仓储（C1 / C2 / C3 / C4 / C5 / C9 / M1）。

本文件只负责**聚合族**取数；明细族（C6/C7/C8）在另一个仓储文件里，两者零重叠。

口径（本仓统一遵守，勿逐方法各写一套）
--------------------------------------
- 时延一律显式 ``ROUND(AVG(EXTRACT(EPOCH FROM (ended - started))) * 1000)::int``
  换算成毫秒（``*_ms: int``）；空集 ``AVG`` 返 ``NULL``，不得兜底为 0。
- 空集取值：计数类返 ``0``；比率 / 均值类返 ``null``（本仓以 ``None`` 表达，未取到不给 0）。
- 正常模式（``metric_mode='logical'``）取 ``monitor.call_log``。
- 降级模式（``metric_mode='degraded'``）取**物理尝试 ∪ 无尝试的逻辑调用**：
  ``monitor.call_log FULL OUTER JOIN monitor.call_attempt ON request_id``。每个
  attempt 一行；没有 attempt 的 call_log 各计 1 行；有 call_attempt 无 call_log 的孤儿行仍保留。
  状态 / 起止列取 ``COALESCE(attempt, call_log)``。
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
  （见 :data:`OBJECT_TYPE_BY_PROTOCOL`）—— ``object_id`` 是**对外对象标识**
  ``COALESCE(mcp_tool, target)``（MCP 调用展示工具名，其余链路回落能力标识 / 模型名；
  C9 按对象标识寻址），但 ``object_type`` **不再是**对象标识的别名：筛选入参在 service
  层换算成 ``protocol``；「对象类型」实际出推导得到的 3 类（openai / invoke / mcp）。
* 「外部依赖」与「开放能力」两页签（C1/C2 vs C3/C4）由 ``direction`` +
  ``call_scope`` 共同切分：外部依赖 = ``outbound`` + ``external``，开放能力 =
  ``inbound``。本仓只提供通用筛选，具体取值由 service 传入。
* ``monitor.call_attempt`` **无对象标识 / ``protocol`` / ``source`` 列**：降级模式下做
  对象聚合（对象列表与对象详情）时，本仓以 ``call_log FULL OUTER JOIN call_attempt ON request_id``
  借用三列（含 ``mcp_tool``）。FULL JOIN 同时满足两件事：没有 call_attempt 的 call_log 不被漏计
  （本次修复核心），「call_attempt 先于 call_log 落库」的孤儿 attempt 也不被漏计。
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
#: ``invoke`` ⇒ 「能力」（sidecar 把能力 ID 写进 ``target``）、
#: ``mcp`` ⇒ 「MCP 工具」（MCP 链路把工具名写进 ``mcp_tool``，对象标识取
#: ``COALESCE(mcp_tool, target)``）。``http`` 在全后端**无采集写入方** ⇒ 不出，
#: 不补空列。故实际出 3 类。
OBJECT_TYPE_BY_PROTOCOL: dict[str, str] = {
    "openai": "模型服务",
    "invoke": "能力",
    "mcp": "MCP 工具",
}

#: 趋势桶粒度白名单（5 分钟 / 自然小时 / 自然日）。
#: ``5m`` 只有 service 层会补空桶；``hour`` / ``day`` 仍只返回有数据桶。
_BUCKETS = ("5m", "hour", "day")

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
    # 取数源：正常模式 = call_log；降级模式 = call_log FULL JOIN call_attempt
    # （物理尝试 ∪ 无尝试的逻辑调用，并借 call_log 的对象列）
    # ------------------------------------------------------------------

    def _metric_columns(
        self, degraded: bool
    ) -> Tuple[Any, Any, Any, Any, Any, Any]:
        """按模式返回 ``(status, started, ended, object_id, protocol, source)`` 六列。

        * 正常模式：六列全部来自 ``monitor.call_log``。
        * 降级模式：状态 / 起止取 ``COALESCE(attempt, call_log)`` —— 有 attempt
          的调用按 attempt 行展开，没有 attempt 的 call_log 回落逻辑调用的状态与时间；
          ``object_id`` / ``protocol`` / ``source`` **借用** ``monitor.call_log``
          （见 docstring 缺口条）。

        ``object_id`` 是**对外对象标识**，两种模式统一取
        ``COALESCE(mcp_tool, target)``：MCP 调用显示工具名，其余链路回落
        能力标识 / 模型名。降级模式下经 ``FULL OUTER JOIN call_log`` 借出这两列。
        """
        object_id_col = func.coalesce(CallLog.mcp_tool, CallLog.target)
        if degraded:
            return (
                func.coalesce(CallAttempt.status, CallLog.final_status),
                func.coalesce(CallAttempt.attempt_started_at, CallLog.logical_started_at),
                func.coalesce(CallAttempt.attempt_ended_at, CallLog.final_ended_at),
                object_id_col,
                CallLog.protocol,
                CallLog.source,
            )
        return (
            CallLog.final_status,
            CallLog.logical_started_at,
            CallLog.final_ended_at,
            object_id_col,
            CallLog.protocol,
            CallLog.source,
        )

    @staticmethod
    def _apply_from(stmt: Any, degraded: bool) -> Any:
        """降级模式下挂上 ``call_log FULL OUTER JOIN call_attempt``；正常模式原样返回。

        有效行集 = 物理尝试 ∪ 无尝试的逻辑调用：每个 attempt 一行，没有 attempt 的
        call_log 行各计 1 行，孤儿 attempt（有 call_attempt 无 call_log）也保留。
        """
        if degraded:
            return stmt.select_from(CallLog).join(
                CallAttempt,
                CallAttempt.request_id == CallLog.request_id,
                full=True,
            )
        return stmt

    def _time_and_filters(
        self,
        degraded: bool,
        start: datetime,
        to: datetime,
        *,
        direction: Optional[str] = None,
        call_scope: Optional[str] = None,
        target: Optional[str] = None,
        protocol: Optional[str] = None,
        source: Optional[str] = None,
        exclude_null_target: bool = False,
    ) -> list[Any]:
        """统一构造 ``[start, to)`` + 可选筛选的 WHERE 条件列表。

        ``target`` 入参按**对外对象标识**匹配（``COALESCE(mcp_tool, target)``），
        与 C2/C4 分组、C5 对象筛选、C9 寻址同口径。

        ``direction`` 恒取 ``CallLog.direction``：正常模式本就从 ``call_log`` 起，
        降级模式由 :meth:`_apply_from` 挂上了 ``call_log FULL OUTER JOIN call_attempt``，
        故两种模式下这一列都可达（``call_attempt`` 自身没有 ``direction``）。

        ``call_scope`` 同取 ``CallLog.call_scope``（调用边界 external / internal /
        open）。降级模式经同一个 FULL JOIN 可达，孤儿 attempt 该列为 NULL，自然
        不命中任何取值 —— 与「NULL 不计入外部依赖」的口径一致。
        """
        _, started, _, object_id_col, protocol_col, source_col = self._metric_columns(degraded)
        conditions: list[Any] = [started >= start, started < to]
        if direction is not None:
            conditions.append(CallLog.direction == direction)
        if call_scope is not None:
            conditions.append(CallLog.call_scope == call_scope)
        if target is not None:
            conditions.append(object_id_col == target)
        if protocol is not None:
            conditions.append(protocol_col == protocol)
        if source is not None:
            conditions.append(source_col == source)
        if exclude_null_target:
            # 合并表达式为 NULL ⇔ mcp_tool 与 target 同时为空。
            conditions.append(object_id_col.isnot(None))
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
        call_scope: Optional[str] = None,
        target: Optional[str] = None,
        protocol: Optional[str] = None,
        degraded: bool = False,
    ) -> dict:
        """外部依赖 / 开放能力统计卡取数。

        正常模式取 ``call_log``；降级模式取「物理尝试 ∪ 无尝试的逻辑调用」
        （``call_log FULL OUTER JOIN call_attempt``）。

        ``direction`` 是两个页签的唯一分界（``outbound``=外部依赖 /
        ``inbound``=开放能力）—— ``monitor.call_log`` 没有 ``object_type`` 列，
        不靠它切，C1 与 C3 会返回同一组数字。

        ``call_scope`` 进一步收窄「跨不跨平台边界」：外部依赖页只传
        ``external``，把任务中心派活/回报等平台内部互调排除在外。

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
                degraded,
                start,
                to,
                direction=direction,
                call_scope=call_scope,
                target=target,
                protocol=protocol,
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
        call_scope: Optional[str] = None,
        target: Optional[str] = None,
        protocol: Optional[str] = None,
        degraded: bool = False,
        offset: int = 0,
        limit: int = 20,
    ) -> Tuple[list[dict], int]:
        """C2 / C4 对象列表取数，返回 ``(items, total)``。

        ``direction`` 同 :meth:`summary`：``outbound``=外部依赖(C2) / ``inbound``=开放能力(C4)。
        ``call_scope`` 同 :meth:`summary`：外部依赖页固定传 ``external``。

        分组键 = ``(对外对象标识, protocol)``；对外对象标识为
        ``COALESCE(mcp_tool, target)``（``object_id``）。行内 ``object_type``
        **由 ``protocol`` 推导**，不再是对象标识的别名。
        每项含调用量 / 成功率 / 平均响应时间 / 失败数 / 最近调用时间。
        对象标识为 NULL 的行不进对象列表（NULL 不构成「对象」）。
        排序：调用量倒序，再按对象标识升序稳定排序（契约未规定，取稳定排序的常用默认）。
        """
        status, started, ended, object_id_col, protocol_col, _ = self._metric_columns(degraded)
        select_cols = (
            object_id_col.label("object_id"),
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
            call_scope=call_scope,
            target=target,
            protocol=protocol,
            exclude_null_target=True,
        )

        page_stmt = select(*select_cols)
        page_stmt = self._apply_from(page_stmt, degraded)
        page_stmt = (
            page_stmt.where(*base_conditions)
            .group_by(object_id_col, protocol_col)
            .order_by(func.count().desc(), object_id_col.asc())
            .offset(offset)
            .limit(limit)
        )
        rows = (await self.session.execute(page_stmt)).all()

        count_stmt = select(object_id_col, protocol_col)
        count_stmt = self._apply_from(count_stmt, degraded)
        grouped = (
            count_stmt.where(*base_conditions).group_by(object_id_col, protocol_col).subquery()
        )
        total = int(
            (await self.session.execute(select(func.count()).select_from(grouped))).scalar() or 0
        )

        items = [
            {
                "object_id": row.object_id,
                # object_type 由 protocol 推导（不再是对象标识的别名）；
                # 未知 protocol（http / NULL）⇒ None，由 service 兜空串。
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
        call_scope: Optional[str] = None,
        tz: str = _TZ,
        degraded: bool = False,
    ) -> list[dict]:
        """C5 趋势取数：按 ``metric`` 与 ``bucket``（5m/hour/day）分桶，返回桶序列。

        每个桶返回 ``{bucket_start, value, call_count, success_calls, failed_calls, avg_latency_ms}``；
        ``value`` 由 ``metric`` 决定，比率 / 均值在空集时为 ``None``。
        「未闭合桶标进行中」由 service 层按同一个 ``now()`` 快照判定，本仓不判。

        分桶默认按 ``_TZ``（Asia/Shanghai）的自然时/日切，**不依赖 DB session 时区**；
        ``bucket_start`` 仍是 ``timestamptz``，由 service 统一转 UTC ``Z`` 输出。
        """
        if bucket not in _BUCKETS:
            raise InvalidParameterError(
                message="bucket 取值非法，仅支持 5m / hour / day",
                details={"bucket": bucket, "allowed": list(_BUCKETS)},
            )
        if metric not in _METRICS:
            raise InvalidParameterError(
                message="metric 取值非法",
                details={"metric": metric, "allowed": list(_METRICS)},
            )

        status, started, ended, _, _, _ = self._metric_columns(degraded)
        if bucket == "5m":
            # 5m：先转北京墙钟，按 UTC 1970 起点对齐 5 分钟网格，再转回 timestamptz。
            # （北京 +8 是整小时偏移，不影响 5 分钟边界；输出仍由 service 转 UTC ``Z``。）
            bucket_expr = func.timezone(
                tz,
                func.date_bin(
                    sa_text("INTERVAL '5 minutes'"),
                    func.timezone(tz, started),
                    sa_text("TIMESTAMP '1970-01-01'"),
                ),
            )
        else:
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
                    degraded,
                    start,
                    to,
                    target=target,
                    protocol=protocol,
                    direction=direction,
                    call_scope=call_scope,
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

        # 多取几倍候选：同一个人的历史形态（用户名 / 裸 id / user:<id>）在 SQL 里是
        # 不同的 source 值，服务层按身份合并后才截断到 ``top_sources_limit``。
        # 按原始值只取 limit 条会让合并后的条目不足、或漏掉落在截断外的形态。
        top_sources = await self._top_sources(
            start, to, object_id=object_id, protocol=protocol, degraded=degraded,
            limit=max(top_sources_limit * 8, 50),
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
        """来源 / 调用方 TOP N（每项 ``{name, call_count, tenant_ids}``）。

        ``object_id`` 按对外对象标识筛选（见 :meth:`_time_and_filters`）。
        ``name`` 是**原始 source**（展示层保持原值）；``tenant_ids`` 是该 source
        出现过的租户集合（``array_agg(DISTINCT tenant_id)``），供 service 用
        「该行租户」解析用户名并判定多租户歧义 —— 计数与排序不变。
        """
        _, _, _, _, _, source_col = self._metric_columns(degraded)
        stmt = select(
            source_col.label("name"),
            func.count().label("call_count"),
            func.array_agg(func.distinct(CallLog.tenant_id)).label("tenant_ids"),
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
        return [
            {
                "name": row.name,
                "call_count": int(row.call_count or 0),
                "tenant_ids": list(row.tenant_ids or []),
            }
            for row in rows
        ]

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

        降级模式的有效行集是「物理尝试 ∪ 无尝试的逻辑调用」，故 ``request_id`` /
        ``error_message`` 取 ``COALESCE(attempt, call_log)`` —— 有 attempt 的调用
        request_id 可重复（按 attempt 行展开），没有 attempt 的失败 call_log 也能进榜。
        """
        status, started, _, _, _, _ = self._metric_columns(degraded)
        request_col = (
            func.coalesce(CallAttempt.request_id, CallLog.request_id)
            if degraded
            else CallLog.request_id
        )
        error_col = (
            func.coalesce(CallAttempt.error_message, CallLog.error_message)
            if degraded
            else CallLog.error_message
        )
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

    async def list_call_source_tenants(self, limit: int = 200) -> list[dict]:
        """``source`` → 出现过的租户集合（``call_source_options`` 用）。

        与 :meth:`list_call_sources` 同源、同序、同上限；额外带上
        ``array_agg(DISTINCT tenant_id)``，供 service 按「该 source 的租户」
        解析用户名 —— 多租户解析出不同名字时回退原值，绝不猜。
        """
        stmt = (
            select(
                CallLog.source.label("source"),
                func.array_agg(func.distinct(CallLog.tenant_id)).label("tenant_ids"),
            )
            .where(CallLog.source.isnot(None))
            .group_by(CallLog.source)
            .order_by(CallLog.source.asc())
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).all()
        return [
            {"source": row.source, "tenant_ids": list(row.tenant_ids or [])}
            for row in rows
            if row.source
        ]

    async def list_call_targets(self, limit: int = 200) -> list[str]:
        """对外对象标识的 distinct 值（``call_targets``）。

        口径与记录页目标列一致：``COALESCE(mcp_tool, target)``（MCP 工具名与
        能力 ID / 模型名的并集），排除 NULL。
        """
        object_id_col = func.coalesce(CallLog.mcp_tool, CallLog.target)
        stmt = (
            select(object_id_col)
            .where(object_id_col.isnot(None))
            .distinct()
            .order_by(object_id_col.asc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return [value for value in result.scalars().all() if value]

    async def list_task_types(self, limit: int = 200) -> list[dict]:
        """任务类型列表（M1 ``task_types``，取自 ``task_center.task_definitions``）。

        每项 ``{"code", "name"}``：``code`` 是**筛选值**（= ``definition_id``，
        与 T2/T3/T4 的 ``task_type`` 同源，前端拿它直接筛）；``name`` 是**定义名**
        （前端下拉直接显示，⛔ 前端不得本地写死）。

        ⚠️ 2026-10-23 起「任务类型」由**编排定义**承载：``task_catalog`` 表已删除，
        一版定义就是一种任务。历史任务的 ``tasks.definition_id`` 是快照，
        所以这里读定义表即可覆盖存量任务；已删除的定义不再出现在下拉里
        （那些任务仍可在「全部」下看到）。

        ⚠️ 这是本仓**唯一**一处跨能力取数：``task_types`` 是「任务监控」页的筛选下拉，
        数据不在 ``monitor`` 数据层，真值源是任务中心的编排定义表。
        这里取「直读字典表」而非新建跨服务调用链，避免为一张只读字典引入运行时耦合。

        ⚠️ platform 容器不含 ``capabilities/task_center/`` 代码（见
        ``deploy/docker/capability.Dockerfile``），禁止 import task_center ORM 模型
        ——容器内必抛 ``ModuleNotFoundError``。故同 ``mcp_service_repository.py`` 先例，
        跨能力只读一律用 ``sqlalchemy.text()`` 原始 SQL。
        """
        result = await self.session.execute(
            sa_text("""
                SELECT id AS code, name
                FROM task_center.task_definitions
                WHERE is_deleted = 0
                ORDER BY name
                LIMIT :limit
            """),
            {"limit": limit},
        )
        return [{"code": row[0], "name": row[1]} for row in result.all() if row[0]]

    async def list_objects(self, limit: int = 200) -> list[dict]:
        """对象类型列表（M1 ``objects``，每项 ``{object_type, protocol}``）。

        ``object_type`` 由 ``protocol`` 推导（见 :data:`OBJECT_TYPE_BY_PROTOCOL`）
        —— **不再**按 ``target`` 出「对象名」，故只保留有写入方的三个 protocol
        （``openai`` / ``invoke`` / ``mcp``），出参收口为 3 类；``http`` 无写入方，
        不会出现。
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
