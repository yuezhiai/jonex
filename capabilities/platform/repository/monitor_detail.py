"""平台监控 · 调用明细族查询仓储（C6 / C7 / C8）。

覆盖「调用记录 / 单次调用详情 / Attempt 明细」三个**读**接口，是调用监控
「明细族」的唯一取数口（聚合族另有一文件，两文件零重叠）。

## 数据源与降级

两张 append-only 事件表（模型见 ``jonex_core/common/monitor_models.py``）：

- ``monitor.call_log``     N1 逻辑调用，``request_id`` **本表内唯一**（正常模式取数表）
- ``monitor.call_attempt`` N2 物理 Attempt，``request_id`` **可重复**（降级模式取数表）

降级判定：查询范围内
``call_log.attempt_count <> 该 request_id 的 call_attempt 行数`` 的记录数
（``unlinkable_rows``）``> 0`` ⇒ ``metric_mode='degraded'``。判定一次、整次查询
统一，**禁止按行切换**。判定由 :meth:`resolve_metric_mode` 提供，调用方据此把
``degraded`` 透传给查询方法。

- **调用记录降级取数 = ``call_log FULL OUTER JOIN call_attempt ON request_id``**：
  有 attempt 的调用按 attempt 展开，没有 attempt 的 call_log 行各计 1 行，
  孤儿 attempt（有 call_attempt 无 call_log）也保留，即每笔调用贡献 ``max(attempt 行数, 1)`` 行。
  写法与聚合族 ``monitor_aggregate.MonitorAggregateRepository`` 的
  ``_tenant_condition`` / ``_apply_from`` 同款，**刻意各写一份**（两族取的列不同，
  抽公共模块只会把明细与聚合绑死），改动时两处需同步。
- **单次调用详情不再整体切表**：``attempt_id`` 是**行选择器** —— 传了取该
  物理尝试行，不传取逻辑调用行；两者都取不到返 ``None``（service 转 404）。
- **尝试明细** 仍只读 ``monitor.call_attempt``。

## 硬约束

1. **一律按生效租户过滤**（2026-11-06 产品口径变更，原「显式不过滤」例外已撤销）。
   生效租户由 service 构造时传入（``tenant_id``）；本文件只把它落成 SQL 条件，
   **不调** ``require_tenant``、不读 ``TenantContext``（校验在 service 层一次完成）。
   降级模式的合并行用 ``COALESCE(call_log.tenant_id, call_attempt.tenant_id)``
   判租户，否则「有 attempt 无 call_log」的孤儿行会被 NULL 意外排除。
2. ``from``/``to`` 是唯一真值源，仓库以 **半开区间** ``[start, end)`` 承接
   （``>= start AND < end``，两种模式一致）。``range_preset`` 只做标签、
   不参与计算，**不进本层**。``from >= to`` 的 ``1001`` 由 service 层抛。
3. 分页：仓库层以 ``offset``/``limit`` 为载体，``page``/``page_size`` 的换算归
   route / service。
4. 排序固定：正常模式 ``logical_started_at DESC``；降级模式按**合并起始时间**
   （``COALESCE(attempt_started_at, logical_started_at)``）``DESC``，并以
   ``request_id``、``attempt_no`` 作 tiebreaker（同一秒多行时分页不重不漏）。

## 降级模式下 call_log 专属筛选的落点

调用记录的 ``direction`` / ``trigger_type`` / ``protocol`` / ``source`` / ``target`` 都是
**call_log 列**，``monitor.call_attempt`` 上没有。降级模式经
``call_log FULL OUTER JOIN call_attempt`` 取数后，这些列**直接落 ``CallLog`` 列**；
不再用 ``CallAttempt.request_id IN (SELECT ... FROM call_log)`` 子查询下推 ——
旧写法会把「无 attempt 的 call_log 行」整片漏掉（本次改造的核心）。
孤儿 attempt 没有 call_log 行，上述 call_log 筛选自然不命中（列值为 NULL），与聚合族口径一致。
``status`` / 时间取 ``COALESCE(attempt, call_log)``；``keyword`` 覆盖 call_log 的
``source`` / ``target`` / ``mcp_tool`` / ``request_id`` 与 call_attempt 的 ``attempt_id``。
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from capabilities.platform.repository.base import BaseRepository
from jonex_core.common.monitor_models import (
    CallAttempt,
    CallFinalStatus,
    CallLog,
    LlmUsageLog,
)


def _like(pattern_value: str) -> str:
    """构造 ``ILIKE`` 模糊匹配串（大小写不敏感，对齐既有仓储 keyword 写法）。"""
    return f"%{pattern_value}%"


#: 调用记录页隐藏的协议（2026-11-16 起的展示口径，与聚合族
#: ``_HIDDEN_CALL_PROTOCOL`` 一致）。只隐藏读侧展示，采集与历史行不动。
_HIDDEN_CALL_PROTOCOL = "invoke"


class MonitorDetailRepository(BaseRepository[CallLog]):
    """调用明细族（C6 / C7 / C8）查询仓储。

    ``model`` 取 :class:`CallLog`（N1）仅用于满足基类的泛型约束；本仓储跨
    ``call_log`` / ``call_attempt`` 两表取数，租户条件由 ``tenant_id`` 显式承载
    （明细表无软删语义，见模块 docstring）。
    """

    model = CallLog

    def __init__(self, session: AsyncSession, *, tenant_id: str):
        super().__init__(session)
        self.tenant_id = tenant_id

    # ------------------------------------------------------------------
    # 降级判定
    # ------------------------------------------------------------------
    async def count_unlinkable(self, start: datetime, end: datetime) -> int:
        """统计 **重试不可关联** 的逻辑调用数（``unlinkable_rows``）。

        ``call_log.attempt_count`` 与该 ``request_id`` 的 ``call_attempt`` 行数不等
        即计入。返回 ``> 0`` 即应整次查询走降级模式。

        **在途调用不参与对账**：``call_log`` 在调用开始时已落库（``attempt_count``
        先记 1），``call_attempt`` 要等调用结束才写，中间这段窗口里两者必然不等。
        若不排除 ``final_status='running'``，任何时刻只要有调用在跑，整页就被判成
        降级并改读 ``call_attempt`` —— 现场表现即「刷新一次只剩一条」。
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
                CallLog.tenant_id == self.tenant_id,
                CallLog.logical_started_at >= start,
                CallLog.logical_started_at < end,
                CallLog.final_status != CallFinalStatus.RUNNING.value,
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
        target_any: Optional[Sequence[str]] = None,
        source_pairs: Optional[Sequence[Tuple[str, str]]] = None,
        source_filter_pairs: Optional[List[Tuple[str, str]]] = None,
        source_global_forms: Optional[List[str]] = None,
        degraded: bool = False,
        offset: int = 0,
        limit: int = 20,
    ) -> Tuple[Sequence[Any], int]:
        """C6 调用明细列表。返回 ``(rows, total)``。

        - 正常模式（``degraded=False``）：取 ``monitor.call_log``，行 = 逻辑调用
          （``request_id`` 唯一），排序 ``logical_started_at DESC``。
        - 降级模式（``degraded=True``）：取 ``call_log FULL OUTER JOIN call_attempt``。
          行 = 物理尝试 ∪ 无尝试的逻辑调用：有 attempt 的调用按 attempt 展开，
          没有 attempt 的 call_log 行各计 1 行，孤儿 attempt 也保留（每笔调用贡献
          ``max(attempt 行数, 1)`` 行）。排序按合并起始时间 ``DESC``，并以
          ``request_id`` / ``attempt_no`` 兜底，保证同一秒多行翻页稳定。

        降级模式返回的是 SQLAlchemy ``Row``（不是 ORM 对象），列已在 SQL 里统一
        ``COALESCE(attempt, call_log)``；``row.attempt_id is None`` 即「无 attempt 的
        逻辑调用行」，调用方据此区分两类行（见 ``MonitorCallService.records``）。
        这是 2026-11-11 的修复：旧实现只读 ``call_attempt``，真降级时
        「没有 attempt 的调用」在记录页整片消失。

        ``source_filter_pairs`` 是 ``source`` 筛选值按用户身份展开后的
        ``(tenant_id, source)`` 精确对（非空时**替代** ``source`` 单值匹配）；
        它与关键字展开的 ``source_pairs`` 是 **AND** 关系，各自独立成条件，
        不得并入同一个 OR 组。筛选映射见模块 docstring
        「降级模式下 N1 专属筛选的落点」。

        ``source_global_forms`` 是规范形态 ``user:<id>`` 的全局兜底：主键全局唯一，
        模拟态（管理员切租户）产生的记录租户是目标租户、用户属于原租户，
        只按租户对会漏，因此额外按 ``source`` 单列匹配（与租户对是 OR）。
        """
        if degraded:
            columns = self._merged_columns()
            conditions = self._merged_record_conditions(
                start=start,
                end=end,
                status=status,
                keyword=keyword,
                direction=direction,
                trigger_type=trigger_type,
                protocol=protocol,
                source=source,
                target=target,
                target_any=target_any,
                source_pairs=source_pairs,
                source_filter_pairs=source_filter_pairs,
                source_global_forms=source_global_forms,
            )
            base = self._apply_from(
                select(*[expr.label(name) for name, expr in columns.items()]), True
            ).where(*conditions)
            count_stmt = self._apply_from(select(func.count()), True).where(*conditions)
            rows_result = await self.session.execute(
                base.order_by(
                    columns["started"].desc(),
                    columns["request_id"].asc(),
                    CallAttempt.attempt_no.asc(),
                )
                .offset(offset)
                .limit(limit)
            )
            rows = rows_result.all()
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
                target_any=target_any,
                source_pairs=source_pairs,
                source_filter_pairs=source_filter_pairs,
                source_global_forms=source_global_forms,
            )
            base = select(CallLog).where(*conditions)
            count_stmt = select(func.count()).select_from(CallLog).where(*conditions)
            rows_result = await self.session.execute(
                base.order_by(CallLog.logical_started_at.desc())
                .offset(offset)
                .limit(limit)
            )
            rows = rows_result.scalars().all()

        total_result = await self.session.execute(count_stmt)
        return rows, (total_result.scalar() or 0)

    def _tenant_condition(self, degraded: bool) -> Any:
        """生效租户条件。

        降级模式是 ``call_log FULL OUTER JOIN call_attempt``，孤儿 attempt 没有
        call_log 行 ⇒ 用 ``COALESCE`` 取两表租户，保证「物理尝试 ∪ 无尝试的逻辑调用」
        都在本租户内，而不是被 NULL 意外排除。与聚合族
        ``MonitorAggregateRepository._tenant_condition`` 同款（刻意各写一份）。
        """
        if degraded:
            return (
                func.coalesce(CallLog.tenant_id, CallAttempt.tenant_id)
                == self.tenant_id
            )
        return CallLog.tenant_id == self.tenant_id

    @staticmethod
    def _apply_from(stmt: Any, degraded: bool) -> Any:
        """降级模式挂上 ``call_log FULL OUTER JOIN call_attempt``；正常模式原样返回。

        有效行集 = 物理尝试 ∪ 无尝试的逻辑调用：每个 attempt 一行，没有 attempt 的
        call_log 行各计 1 行，孤儿 attempt（有 call_attempt 无 call_log）也保留。
        与聚合族 ``MonitorAggregateRepository._apply_from`` 同款，**刻意各写一份**：
        两族取的列不同，抽公共模块只会把明细与聚合绑死。
        """
        if degraded:
            return stmt.select_from(CallLog).join(
                CallAttempt,
                CallAttempt.request_id == CallLog.request_id,
                full=True,
            )
        return stmt

    def _merged_columns(self) -> Dict[str, Any]:
        """降级模式合并行要选的列（``{标签: 表达式}``）。

        状态 / 起止 / 错误取 ``COALESCE(attempt, call_log)`` —— 有 attempt 的调用按
        attempt 行展开，没有 attempt 的 call_log 行回落逻辑调用的值；其余 call_log 列
        （direction / trigger_type / source / target / protocol / trace_id）直接借
        ``call_log``（call_attempt 上不存在）。``request_id`` / ``tenant_id`` 也取 ``COALESCE``，
        供孤儿 attempt 行使用。
        """
        return {
            "request_id": func.coalesce(CallLog.request_id, CallAttempt.request_id),
            "tenant_id": func.coalesce(CallLog.tenant_id, CallAttempt.tenant_id),
            "trace_id": CallLog.trace_id,
            "direction": CallLog.direction,
            "trigger_type": CallLog.trigger_type,
            "source": CallLog.source,
            "mcp_tool": CallLog.mcp_tool,
            "target": CallLog.target,
            "protocol": CallLog.protocol,
            "status": func.coalesce(CallAttempt.status, CallLog.final_status),
            "error_code": func.coalesce(CallAttempt.error_code, CallLog.error_code),
            "error_message": func.coalesce(
                CallAttempt.error_message, CallLog.error_message
            ),
            "started": func.coalesce(
                CallAttempt.attempt_started_at, CallLog.logical_started_at
            ),
            "ended": func.coalesce(
                CallAttempt.attempt_ended_at, CallLog.final_ended_at
            ),
            # 行选择器：NULL ⇔ 本行来自「无 attempt 的逻辑调用」。
            "attempt_id": CallAttempt.attempt_id,
            "attempt_no": CallAttempt.attempt_no,
            # call_log 声明的尝试次数；孤儿 attempt 行由调用方退用 ``attempt_no``。
            "logical_attempt_count": CallLog.attempt_count,
        }

    def _merged_record_conditions(
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
        target_any: Optional[Sequence[str]],
        source_pairs: Optional[Sequence[Tuple[str, str]]],
        source_filter_pairs: Optional[List[Tuple[str, str]]] = None,
        source_global_forms: Optional[List[str]] = None,
    ) -> List[Any]:
        """降级模式（``call_log FULL OUTER JOIN call_attempt``）的行条件。

        时间窗与状态落在合并列（``COALESCE(attempt, call_log)``）；其余 call_log 专属筛选
        **直接落 ``CallLog`` 列**，不再用 ``CallAttempt.request_id IN (SELECT ...)``
        子查询下推 —— FULL JOIN 已把 call_log 挂在同一行上，下推反而会把
        「无 attempt 的 call_log 行」漏掉。孤儿 attempt 的 call_log 列为 NULL，任何 call_log
        筛选都不会命中它（与聚合族口径一致）。
        """
        columns = self._merged_columns()
        conditions: List[Any] = [
            self._tenant_condition(True),
            columns["started"] >= start,
            columns["started"] < end,
            # ``protocol`` 可空（孤儿 attempt 行为 NULL）：必须用 ``IS DISTINCT FROM``
            # 而非 ``!=``，否则三值逻辑会把整批无协议行一起误删。
            CallLog.protocol.is_distinct_from(_HIDDEN_CALL_PROTOCOL),
        ]
        if direction:
            conditions.append(CallLog.direction == direction)
        if status:
            conditions.append(columns["status"] == status)
        if trigger_type:
            conditions.append(CallLog.trigger_type == trigger_type)
        if protocol:
            conditions.append(CallLog.protocol == protocol)
        if source_filter_pairs:
            # 与正常模式同口径：按用户身份展开的 ``(tenant_id, source)`` 精确对，
            # 外加规范形态 ``user:<id>`` 的全局兜底（主键全局唯一）。
            matched = tuple_(CallLog.tenant_id, CallLog.source).in_(
                list(source_filter_pairs)
            )
            if source_global_forms:
                matched = or_(matched, CallLog.source.in_(list(source_global_forms)))
            conditions.append(matched)
        elif source:
            conditions.append(CallLog.source == source)
        if target:
            # 与正常模式同口径：对外对象标识 = target 或 mcp_tool。
            conditions.append(
                or_(CallLog.target == target, CallLog.mcp_tool == target)
            )
        if keyword or target_any or source_pairs:
            conditions.append(
                self._keyword_group(
                    keyword=keyword,
                    target_any=target_any,
                    source_pairs=source_pairs,
                    include_request_id=True,
                    include_attempt_id=True,
                )
            )
        return conditions

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
        target_any: Optional[Sequence[str]],
        source_pairs: Optional[Sequence[Tuple[str, str]]],
        source_filter_pairs: Optional[List[Tuple[str, str]]] = None,
        source_global_forms: Optional[List[str]] = None,
    ) -> List[Any]:
        conditions: List[Any] = [
            CallLog.tenant_id == self.tenant_id,
            CallLog.logical_started_at >= start,
            CallLog.logical_started_at < end,
            # 与降级模式同口径：``protocol='invoke'`` 的记录不在调用记录页展示。
            CallLog.protocol.is_distinct_from(_HIDDEN_CALL_PROTOCOL),
        ]
        if direction:
            conditions.append(CallLog.direction == direction)
        if status:
            conditions.append(CallLog.final_status == status)
        if trigger_type:
            conditions.append(CallLog.trigger_type == trigger_type)
        if protocol:
            conditions.append(CallLog.protocol == protocol)
        if source_filter_pairs:
            # ``source`` 按用户身份展开：同一个人的全部历史形态一次命中。
            # 必须带租户，禁止跨租户同值误命中（独立 AND 条件，不并 keyword OR 组）。
            # ``source_global_forms`` 是规范形态 ``user:<id>`` 的全局兜底：主键
            # 全局唯一，模拟态下记录租户与用户所属租户不同，只按租户对会漏。
            matched = tuple_(CallLog.tenant_id, CallLog.source).in_(
                list(source_filter_pairs)
            )
            if source_global_forms:
                matched = or_(matched, CallLog.source.in_(list(source_global_forms)))
            conditions.append(matched)
        elif source:
            # 精确匹配（候选值由 M1 的 call_sources 提供）
            conditions.append(CallLog.source == source)
        if target:
            # 精确匹配「对外对象标识」= COALESCE(mcp_tool, target)：
            # 对象详情「查看全部调用」按 object_id 下钻（MCP 对象传工具名），
            # 用 keyword 会把同前缀的其他对象一并带进来。
            conditions.append(
                or_(CallLog.target == target, CallLog.mcp_tool == target)
            )
        if keyword or target_any or source_pairs:
            conditions.append(
                self._keyword_group(
                    keyword=keyword,
                    target_any=target_any,
                    source_pairs=source_pairs,
                    include_request_id=True,
                )
            )
        return conditions

    @staticmethod
    def _keyword_group(
        *,
        keyword: Optional[str],
        target_any: Optional[Sequence[str]],
        source_pairs: Optional[Sequence[Tuple[str, str]]],
        include_request_id: bool,
        include_attempt_id: bool = False,
    ) -> Any:
        """关键字 OR 组。各部分同组 OR：

        1. 既有 raw ``ILIKE``（source / target / mcp_tool，记录页另含 request_id）；
        2. ``target_any``：前端把命中的能力释义展开成原始标识，
           按记录页目标列口径 ``target IN (...) OR mcp_tool IN (...)``；
        3. ``source_pairs``：后端按 ``users.username`` 展开的
           ``(tenant_id, source)`` 精确对 —— **必须带租户**，禁止跨租户同值误命中。
        4. ``include_attempt_id``（仅降级记录页）：``call_attempt.attempt_id``
           也参与模糊匹配 —— 用户从记录页复制的就是 attempt_id。
        """
        clauses: List[Any] = []
        if keyword:
            pattern_value = _like(keyword)
            clauses.extend(
                [
                    CallLog.source.ilike(pattern_value),
                    CallLog.target.ilike(pattern_value),
                    # 记录页目标列输出对外对象标识（MCP 为工具名），关键字同口径覆盖。
                    CallLog.mcp_tool.ilike(pattern_value),
                ]
            )
            if include_request_id:
                clauses.append(CallLog.request_id.ilike(pattern_value))
            if include_attempt_id:
                clauses.append(CallAttempt.attempt_id.ilike(pattern_value))
        if target_any:
            values = list(target_any)
            clauses.append(
                or_(
                    CallLog.target.in_(values),
                    CallLog.mcp_tool.in_(values),
                )
            )
        if source_pairs:
            clauses.append(
                tuple_(CallLog.tenant_id, CallLog.source).in_(list(source_pairs))
            )
        return or_(*clauses)

    # ------------------------------------------------------------------
    # C7 · 单次调用详情
    # ------------------------------------------------------------------
    async def get_call_log_by_request_id(self, request_id: str) -> Optional[CallLog]:
        """正常模式取数：按 ``request_id`` + 生效租户命中 N1 唯一行。

        跨租户请求「按不存在处理」——本方法返回 ``None``，由 service 抛
        ``ResourceNotFoundError``（与真的不存在完全一致，不暴露存在性）。
        """
        result = await self.session.execute(
            select(CallLog).where(
                CallLog.tenant_id == self.tenant_id,
                CallLog.request_id == request_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_call_logs_by_request_ids(
        self, request_ids: Sequence[str]
    ) -> dict:
        """按一组 ``request_id`` 批量取 N1 行 → ``{request_id: CallLog}``。

        一次 ``IN`` 查询替代逐行回查。现由 调用记录 的 Token 补值路径
        （``MonitorCallService.token_usage``）使用 —— 降级记录页已改走
        ``call_log FULL OUTER JOIN call_attempt``，call_log 列随行带出，不再需要
        「先取 attempt 再批量回查 call_log」的二次查询。查不到的 ``request_id``
        不出现在返回值里，由调用方兜底。
        """
        ids = {rid for rid in request_ids if rid}
        if not ids:
            return {}
        result = await self.session.execute(
            select(CallLog).where(
                CallLog.tenant_id == self.tenant_id,
                CallLog.request_id.in_(ids),
            )
        )
        return {row.request_id: row for row in result.scalars().all()}

    async def sum_token_usage(
        self,
        *,
        request_ids: Sequence[str],
        trace_ids: Sequence[str],
    ) -> Tuple[Dict[str, int], Dict[str, int]]:
        """批量汇总**精确** Token，返回 ``(按 request_id, 按 trace_id)`` 两路映射。

        口径（2026-11-10 需求方确认）：

        1. 只算 ``is_estimated = false`` 的行；估算行的值即使存在也完全不计。
        2. 两路映射是**不同语义**，不能互相替代：
           - 模型单次调用行（``openai``）的 ``call_log.request_id`` = ``<trace>:<哈希>``，
             与计量行 ``request_id`` 逐字相等 ⇒ 取**自身**那一行；
           - 能力 / MCP 调用行的 ``request_id`` 是整条链路 id，计量行按 ``trace_id`` 挂，
             一次逻辑调用会横跨多次模型调用 ⇒ 取 **trace 合计**。
           调用方按「先自身、后 trace」的优先级合并（见 ``MonitorCallService``），
           且 ``protocol='openai'`` 的行只认自身 —— 本方法只提供两路数据，取舍在 service。
        3. **不给计量行加时间窗**：调用记录已按 ``logical_started_at`` 筛过，
           ``trace_id`` / ``request_id`` 是这次逻辑调用的身份，计量落账晚于窗口也仍归属它。
           代价是跨窗口边界的长调用不会被截断，反而保证「响应 == 该调用全部精确计量」。

        两次查询都是 ``IN`` + ``GROUP BY``（各 1 条），行数不随分页 N 增长 —— 禁 N+1。
        没有任何精确计量时返回 ``{}``（调用方据此输出 ``None``，前端渲染 ``-``），
        ⛔ 不兜 0：0 与「没有精确值」是两件事。
        """
        request_map = await self._sum_tokens_by(
            LlmUsageLog.request_id, request_ids
        )
        trace_map = await self._sum_tokens_by(LlmUsageLog.trace_id, trace_ids)
        return request_map, trace_map

    async def _sum_tokens_by(self, key_column, values: Sequence[str]) -> Dict[str, int]:
        """按某一列汇总精确 Token；``values`` 为空时直接返回空映射（不发查询）。"""
        keys = {value for value in values if value}
        if not keys:
            return {}
        stmt = (
            select(key_column, func.sum(LlmUsageLog.total_tokens))
            .where(
                LlmUsageLog.tenant_id == self.tenant_id,
                LlmUsageLog.is_estimated.is_(False),
                key_column.in_(keys),
            )
            .group_by(key_column)
        )
        result = await self.session.execute(stmt)
        # ``SUM`` 全为 NULL 时返回 None：跳过，让调用方继续走下一优先级 / 兜 None。
        return {
            key: int(total)
            for key, total in result.all()
            if key is not None and total is not None
        }

    async def count_unlinkable_for_request(self, request_id: str) -> int:
        """**单条**对账：该 ``request_id`` 的 ``call_attempt`` 行数是否与 N1 对得上。

        与 :meth:`count_unlinkable` 同判据，只是把范围从「区间」收窄到
        「一条」—— C7 / C8 没有 ``from``/``to`` 参数，套不上区间判据。
        返回 ``> 0`` ⇒ 该条走降级模式。``call_log`` 无此 ``request_id`` 时返回 ``0``
        （无 N1 行可对账，交调用方按正常模式取，取不到再由 service 抛资源未找到）。

        与区间判定同口径：``final_status='running'`` 的在途调用不参与对账，
        详情页按正常模式（``call_log``）展示。
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
                CallLog.tenant_id == self.tenant_id,
                CallLog.request_id == request_id,
                CallLog.final_status != CallFinalStatus.RUNNING.value,
                CallLog.attempt_count != subquery,
            )
        )
        result = await self.session.execute(stmt)
        return int(result.scalar() or 0)

    async def get_attempt_by_attempt_id(self, attempt_id: str) -> Optional[CallAttempt]:
        """降级模式取数：按 ``attempt_id`` + 生效租户命中 N2 唯一行。

        跨租户 Attempt 同样返回 ``None``（按不存在处理）。
        """
        result = await self.session.execute(
            select(CallAttempt).where(
                CallAttempt.tenant_id == self.tenant_id,
                CallAttempt.attempt_id == attempt_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_attempt_for_request(
        self, request_id: str, attempt_id: str
    ) -> Optional[CallAttempt]:
        """单次调用详情 行选择器：按 ``(tenant_id, request_id, attempt_id)`` 三重过滤取物理尝试。

        同一 ``request_id`` 下可有多条 attempt，必须用 ``request_id`` 收口：只带
        ``attempt_id`` 会把**别的调用**的 attempt 取出来（跨 request 串行 / 越权）。
        「不属于该 request」与「不存在」表现一致（都返 ``None`` → service 转 404）。
        """
        result = await self.session.execute(
            select(CallAttempt).where(
                CallAttempt.tenant_id == self.tenant_id,
                CallAttempt.request_id == request_id,
                CallAttempt.attempt_id == attempt_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_detail(
        self,
        request_id: str,
        *,
        attempt_id: Optional[str] = None,
    ) -> Optional[Any]:
        """单次调用详情 单次调用详情（``attempt_id`` 为**行选择器**，不再是降级必填项）。

        - 传 ``attempt_id`` ⇒ 取该物理尝试行（须同时属于该 ``request_id`` 与生效租户）；
        - 不传 ⇒ 取该 ``request_id`` 的逻辑调用行（``call_log``）；
        - 两者都取不到返 ``None``，由 service 抛 ``ResourceNotFoundError``。

        2026-11-11 起删除「降级模式必须带 ``attempt_id``，否则抛 1001」整个分支：
        降级不再按查询整体切换取数表，而由前端逐行选择 —— 没有 attempt 的逻辑
        调用行（``attempt_id=None``）也能正常查看详情。
        """
        if attempt_id:
            return await self.get_attempt_for_request(request_id, attempt_id)
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
        conditions = [
            CallAttempt.tenant_id == self.tenant_id,
            CallAttempt.request_id == request_id,
        ]
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
                .where(
                    CallAttempt.tenant_id == self.tenant_id,
                    CallAttempt.request_id == request_id,
                )
            )
            attempt_count = result.scalar() or 0
        else:
            result = await self.session.execute(
                select(CallLog.attempt_count).where(
                    CallLog.tenant_id == self.tenant_id,
                    CallLog.request_id == request_id,
                )
            )
            attempt_count = result.scalar()
            if attempt_count is None:
                attempt_count = 0
        return max(int(attempt_count) - 1, 0)

    # 注：M1 的 `call_sources`（C6 `source` 筛选用）**不再**在本文件提供 ——
    # 曾与聚合族各写一份（同一 SQL 两处维护，行为易漂移），已收敛到
    # `MonitorAggregateRepository.list_call_sources` 一处。


__all__ = ["MonitorDetailRepository"]
