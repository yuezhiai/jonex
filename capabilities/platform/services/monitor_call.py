"""平台监控 · 调用监控读接口编排层（C1–C9 + M1）。

职责（薄编排，业务规则在此，取数在仓储）：

- 时间区间解析与 ``from >= to`` 校验（抛 ``1001 InvalidParameterError``）；
- 分页换算 ``page``/``page_size`` → ``offset``/``limit``；
- **降级模式判定一次、整次查询统一透传**（禁按行切换）；
- 把仓储的行对象组装成 ``dtos/monitor.py`` 的出参 DTO。

两个页签的切分
--------------
``monitor.call_log`` **没有** ``object_type`` 列，切不开「外部依赖」与「开放能力」。
改用 ``direction`` 作两页签的分界 —— ``outbound``=外部依赖（C1/C2）、
``inbound``=开放能力（C3/C4）。故 C1/C3 与 C2/C4 是同一方法换 direction 调用。

租户
----
**不按租户过滤**（显式例外）：可见性只凭权限码
``platform:monitor:read``。本层同样不调 ``require_tenant``。

已知缺口（不虚构数据，缺就返 null / 空串）
------------------------------------------
``CallDetail`` 的 12 个字段里，物理表只有 ``request_id`` / ``error_message`` 有落点，
``method`` / ``path`` / ``status_code`` / ``request_payload`` / ``response_payload`` /
三个 Token 字段均无列 —— 本期取「薄壳返回」（不补采集列、不碰 metering）。
"""

from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Sequence, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from capabilities.platform.dtos.monitor import (
    AttemptItem,
    CallDetail,
    CallModeMeta,
    CallObjectDetail,
    CallObjectItem,
    CallRecordItem,
    CallSummary,
    MetaFilters,
    MonitorFlags,
    TrendPoint,
)
from capabilities.platform.repository.monitor_aggregate import (
    OBJECT_TYPE_BY_PROTOCOL,
    MonitorAggregateRepository,
)
from capabilities.platform.repository.monitor_detail import MonitorDetailRepository
from capabilities.platform.services.monitor_range import enforce_max_range
from capabilities.platform.services.monitor_resilience import (
    MODULE_ERROR,
    resilient as _resilient,
)
from jonex_core.common.exceptions import (
    InvalidParameterError,
    ResourceNotFoundError,
)
from jonex_core.common.monitor_models import CallAttempt, CallLog

#: 降级态 ``module_error`` 取值（脱敏：只标「本模块暂不可用」，不带底层异常内容）。
_MODULE_ERROR = MODULE_ERROR

#: 模式元数据的版本号（本期常量，供日后口径变更时分段识别）。
_MODE_VERSION = "v1"

#: 两个页签的 direction 取值。
_DIRECTION_EXTERNAL = "outbound"  # 外部依赖（C1/C2）
_DIRECTION_OPEN = "inbound"  # 开放能力（C3/C4）

_MODE_DEGRADED = "degraded"

#: ``object_type`` → ``protocol`` 反查（对象类型由 ``protocol`` 推导，
#: 筛选同源，见 :func:`repository.monitor_aggregate.list_objects`）。
_PROTOCOL_BY_OBJECT_TYPE = {label: proto for proto, label in OBJECT_TYPE_BY_PROTOCOL.items()}

#: M1 状态字典（产品六态 → 既有枚举）。
#: 「超时」在任务级**无枚举可承载**（子任务超时并入失败），故 ``code`` 记 ``None``
#: —— 前端据此把它渲染成不可筛的说明项。「取消」对应 task_center 的 ``stopped``
#: 终态（``TaskStatus.STOPPED``），可筛；但 T1 成功率分母只含 ``completed`` /
#: ``failed``，被用户停掉的任务不计入 ⇒ ``in_denominator=False``。
_STATUS_DICT: List[dict] = [
    {"code": "pending", "label": "等待中", "pm_label": "等待中", "in_denominator": False},
    {"code": "running", "label": "执行中", "pm_label": "执行中", "in_denominator": False},
    {"code": "completed", "label": "成功", "pm_label": "成功", "in_denominator": True},
    {"code": "failed", "label": "失败", "pm_label": "失败", "in_denominator": True},
    {"code": None, "label": "超时", "pm_label": "超时", "in_denominator": True},
    {"code": "stopped", "label": "取消", "pm_label": "取消", "in_denominator": False},
]


def _parse_ts(value: str, field: str) -> datetime:
    """ISO8601 字符串 → tz-aware ``datetime``（缺时区按 UTC 兜底）。

    ``monitor`` 两张表的时间列均为 ``DateTime(timezone=True)``，故必须给 tz-aware
    值 —— 传朴素值会被 asyncpg 拒（对比 ``jonex_core/common/monitor_models.py``
    的 ``_utcnow``）。
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


def _duration_ms(started: Optional[datetime], ended: Optional[datetime]) -> Optional[int]:
    """``(ended - started)`` 的毫秒数；任一端缺失返 ``None``。"""
    if started is None or ended is None:
        return None
    return round((ended - started).total_seconds() * 1000)


# ══════════════════════════════════════════════════════════════════
# 降级保护层（共用装饰器在 ``monitor_resilience``；此处只放空壳工厂）
# ══════════════════════════════════════════════════════════════════


def _degraded_flags() -> MonitorFlags:
    """降级态标志位：``module_error`` 非空 ⇒ 前端据此区分「模块出错」与「确实没数据」。"""
    return MonitorFlags(module_error=_MODULE_ERROR)


def _empty_summary(frm: str, to: str, *, direction: str) -> CallSummary:
    """C1 / C3 统计卡降级空壳（结构完整：全零 / ``None`` / 空 as-of）。"""
    return CallSummary(
        total_calls=0,
        success_rate=None,
        avg_latency_ms=None,
        failed_count=0,
        mode_meta=None,
        flags=_degraded_flags(),
        as_of="",
    )


def _empty_pair(*_args, **_kwargs) -> Tuple[list, int, None]:
    """分页读接口降级空壳：``(items, total, mode_meta)``；降级态 ``mode_meta=None``。"""
    return [], 0, None


def _empty_list(*_args, **_kwargs) -> Tuple[list, None]:
    """列表读接口降级空壳：``(items, mode_meta)``；降级态 ``mode_meta=None``。"""
    return [], None


def _empty_triple(*_args, **_kwargs) -> Tuple[list, int, int]:
    """C8 降级空壳：``(items, total, retry_count)``。"""
    return [], 0, 0


def _empty_call_detail(request_id: str, *, attempt_id: Optional[str] = None) -> CallDetail:
    """C7 降级空壳（薄壳：无落点的字段一律 ``None``）。"""
    return CallDetail(
        request_id=request_id,
        method=None,
        path=None,
        status_code=None,
        error_message=None,
        request_payload=None,
        response_payload=None,
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
        stage=None,
        meta=None,
        mode_meta=None,
    )


def _empty_object_detail(
    object_id: str, frm: str, to: str, *, protocol: Optional[str] = None
) -> CallObjectDetail:
    """C9 降级空壳。"""
    return CallObjectDetail(
        object_id=object_id,
        object_type="",
        call_count=0,
        success_rate=None,
        avg_latency_ms=None,
        failed_count=0,
        top_sources=[],
        recent_failures=[],
        meta=None,
        mode_meta=None,
    )


def _empty_meta_filters(*_args, **_kwargs) -> MetaFilters:
    """M1 降级空壳（四类下拉均为空列表）。"""
    return MetaFilters()


class MonitorCallService:
    """调用监控读接口编排层。

    ``session`` 由路由经 ``get_db`` 注入；本服务**不**做租户过滤（见模块 docstring）。
    """

    def __init__(self, session: AsyncSession):
        self.session = session
        self.agg = MonitorAggregateRepository(session)
        self.det = MonitorDetailRepository(session)

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
        # 超过保留期（默认 90 天）/ 自定义查询上限（默认 30 天）→ 4004。
        # 只有调用族开启保留期：清理只删 call_log / call_attempt。
        return enforce_max_range(start, end, enforce_retention=True)

    @staticmethod
    def _offset(page: int, page_size: int) -> int:
        """``page``（1 起）→ ``offset``（换算归 route / service 层，不由仓储承担）。"""
        return (page - 1) * page_size

    @staticmethod
    def _mode_meta(metric_mode: str, start: datetime, end: datetime) -> CallModeMeta:
        """模式元数据；本期不做历史重算，``recalc_status`` 恒 ``None``。"""
        return CallModeMeta(
            metric_mode=metric_mode,
            mode_version=_MODE_VERSION,
            mode_applicable_range={"from": _iso(start), "to": _iso(end)},
            recalc_status=None,
        )

    @staticmethod
    def _mode_meta_no_range(metric_mode: str) -> CallModeMeta:
        """C7 专用模式元数据：无 ``from``/``to`` ⇒ ``mode_applicable_range`` 两端为 ``None``。"""
        return CallModeMeta(
            metric_mode=metric_mode,
            mode_version=_MODE_VERSION,
            mode_applicable_range={"from": None, "to": None},
            recalc_status=None,
        )

    @staticmethod
    def _flags() -> MonitorFlags:
        """公共标志位。

        本期**无**「采集滞后」「桩数据」的判定依据，故三项保持默认
        （``data_delayed=False`` / ``demo_data=False`` / ``module_error=None``）；
        降级模式的语义由 ``mode_meta.metric_mode='degraded'`` 承载，不重复塞进这里。
        """
        return MonitorFlags()

    # ------------------------------------------------------------------
    # C1 / C3 · 统计卡
    # ------------------------------------------------------------------

    @_resilient(_empty_summary)
    async def summary(self, frm: str, to: str, *, direction: str) -> CallSummary:
        """统计卡取数（C1 ``direction='outbound'`` / C3 ``direction='inbound'``）。"""
        start, end = self._parse_range(frm, to)
        mode = await self.agg.resolve_metric_mode(start, end)
        data = await self.agg.summary(
            start, end, direction=direction, degraded=(mode == _MODE_DEGRADED)
        )
        return CallSummary(
            total_calls=data["total_calls"],
            success_rate=data["success_rate"],
            avg_latency_ms=data["avg_latency_ms"],
            failed_count=data["failed_calls"],
            mode_meta=self._mode_meta(mode, start, end),
            flags=self._flags(),
            as_of=_iso(data["as_of"]) or "",
        )

    async def external_summary(self, frm: str, to: str) -> CallSummary:
        """C1 外部依赖统计卡。"""
        return await self.summary(frm, to, direction=_DIRECTION_EXTERNAL)

    async def open_summary(self, frm: str, to: str) -> CallSummary:
        """C3 开放能力统计卡。"""
        return await self.summary(frm, to, direction=_DIRECTION_OPEN)

    # ------------------------------------------------------------------
    # C2 / C4 · 对象列表
    # ------------------------------------------------------------------

    @_resilient(_empty_pair)
    async def object_list(
        self,
        frm: str,
        to: str,
        *,
        direction: str,
        object_type: Optional[str] = None,
        protocol: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[CallObjectItem], int, Optional[CallModeMeta]]:
        """对象列表取数（C2 ``outbound`` / C4 ``inbound``，C4 另有 ``protocol``）。

        ``object_type`` 无物理列，**按 ``protocol`` 推导**——
        旧的「按对象名精确匹配 ``target``」用法已作废。未知 ``object_type``
        （如无采集写入方的「外部服务 / 外部平台」）或 ``object_type`` 与
        ``protocol`` 互斥 ⇒ 直接返空结果（筛选语义，不抛错）。
        """
        start, end = self._parse_range(frm, to)
        if object_type is not None:
            derived = _PROTOCOL_BY_OBJECT_TYPE.get(object_type)
            if derived is None or (protocol is not None and protocol != derived):
                return [], 0, None
            protocol = derived
        mode = await self.agg.resolve_metric_mode(start, end)
        rows, total = await self.agg.object_list(
            start,
            end,
            direction=direction,
            protocol=protocol,
            degraded=(mode == _MODE_DEGRADED),
            offset=self._offset(page, page_size),
            limit=page_size,
        )
        items = [
            CallObjectItem(
                object_id=row.get("object_id"),
                object_type=row["object_type"] or "",
                protocol=row["protocol"],
                call_count=row["call_count"],
                success_rate=row["success_rate"],
                avg_latency_ms=row["avg_latency_ms"],
                failed_count=row["failed_calls"],
                last_called_at=_iso(row["last_called_at"]),
            )
            for row in rows
        ]
        return items, total, self._mode_meta(mode, start, end)

    async def external_objects(
        self,
        frm: str,
        to: str,
        *,
        object_type: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[CallObjectItem], int, Optional[CallModeMeta]]:
        """C2 外部依赖对象列表（不含 ``protocol`` 筛选）。"""
        return await self.object_list(
            frm, to, direction=_DIRECTION_EXTERNAL,
            object_type=object_type, page=page, page_size=page_size,
        )

    async def open_objects(
        self,
        frm: str,
        to: str,
        *,
        object_type: Optional[str] = None,
        protocol: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[CallObjectItem], int, Optional[CallModeMeta]]:
        """C4 开放能力对象列表（补 ``protocol`` 四值枚举筛选）。"""
        return await self.object_list(
            frm, to, direction=_DIRECTION_OPEN,
            object_type=object_type, protocol=protocol, page=page, page_size=page_size,
        )

    # ------------------------------------------------------------------
    # C5 · 趋势
    # ------------------------------------------------------------------

    @_resilient(_empty_list)
    async def trend(
        self,
        frm: str,
        to: str,
        *,
        metric: str,
        bucket: str,
        object_id: Optional[str] = None,
        direction: Optional[str] = None,
    ) -> Tuple[List[TrendPoint], Optional[CallModeMeta]]:
        """趋势取数。未闭合桶（右边界 > now）标 ``closed=False``。

        ``direction`` 切两个页签口径（outbound 外部依赖 / inbound 开放能力）；
        不传 = 全量（与原有行为一致）。
        """
        start, end = self._parse_range(frm, to)
        mode = await self.agg.resolve_metric_mode(start, end)
        rows = await self.agg.trend(
            start,
            end,
            metric=metric,
            bucket=bucket,
            target=object_id,
            direction=direction,
            degraded=(mode == _MODE_DEGRADED),
        )
        now = datetime.now(timezone.utc)
        span = timedelta(hours=1) if bucket == "hour" else timedelta(days=1)
        points: List[TrendPoint] = []
        for row in rows:
            bucket_start = row["bucket_start"]
            if bucket_start is not None and bucket_start.tzinfo is None:
                bucket_start = bucket_start.replace(tzinfo=timezone.utc)
            closed = bucket_start is not None and (bucket_start + span) <= now
            points.append(
                TrendPoint(
                    bucket_start=_iso(bucket_start) or "",
                    # 比率/均值空桶（NULL）原样透传 ``None`` —— 空桶即「无数据」，禁止兜 0。
                    value=None if row["value"] is None else float(row["value"]),
                    closed=closed,
                )
            )
        return points, self._mode_meta(mode, start, end)

    # ------------------------------------------------------------------
    # C6 · 调用记录列表
    # ------------------------------------------------------------------

    @_resilient(_empty_pair)
    async def records(
        self,
        frm: str,
        to: str,
        *,
        direction: Optional[str] = None,
        status: Optional[str] = None,
        trigger_type: Optional[str] = None,
        protocol: Optional[str] = None,
        source: Optional[str] = None,
        target: Optional[str] = None,
        keyword: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[CallRecordItem], int, Optional[CallModeMeta]]:
        """调用记录列表（正常模式 = 逻辑调用 / 降级模式 = 物理 Attempt）。

        ``target`` 是**精确匹配**（对象详情下钻用）：同一个对象标识可能互为前缀
        （如 ``svc-a`` / ``svc-ab``），模糊匹配会把别的对象一并带进来。
        """
        start, end = self._parse_range(frm, to)
        mode = await self.det.resolve_metric_mode(start, end)
        degraded = mode == _MODE_DEGRADED
        rows, total = await self.det.list_records(
            start=start,
            end=end,
            direction=direction,
            status=status,
            trigger_type=trigger_type,
            protocol=protocol,
            source=source,
            target=target,
            keyword=keyword,
            degraded=degraded,
            offset=self._offset(page, page_size),
            limit=page_size,
        )
        mode_meta = self._mode_meta(mode, start, end)
        if not degraded:
            return [self._record_from_log(row) for row in rows], total, mode_meta

        # 降级模式的行是 N2（call_attempt），缺 direction/trigger_type/target/source/trace_id
        # 五个 N1 列。按 request_id 批量回查 N1 补上（一页至多 page_size 行，一次 IN 查询）；
        # 补不到的（N2 先于 N1 落库的孤儿 attempt）留 None → 组装时空串兜底。
        n1 = await self.det.get_call_logs_by_request_ids([row.request_id for row in rows])
        return [
            self._record_from_attempt(row, n1.get(row.request_id)) for row in rows
        ], total, mode_meta

    @staticmethod
    def _record_from_log(row: CallLog) -> CallRecordItem:
        """N1 行 → ``CallRecordItem``。"""
        attempt_count = int(row.attempt_count or 0)
        return CallRecordItem(
            request_id=row.request_id,
            attempt_id=None,  # 正常模式按 request_id 即唯一定位，无 attempt 概念
            trace_id=row.trace_id,
            direction=row.direction,
            trigger_type=row.trigger_type,
            caller=row.source,
            target=row.target or "",
            protocol=row.protocol,
            # `call_log` 无 method / path 列（已知缺口，见模块 docstring）
            method=None,
            path=None,
            status=row.final_status,
            error_code=row.error_code,
            duration_ms=_duration_ms(row.logical_started_at, row.final_ended_at),
            logical_started_at=_iso(row.logical_started_at) or "",
            final_ended_at=_iso(row.final_ended_at),
            attempt_count=attempt_count,
            retry_count=max(attempt_count - 1, 0),
        )

    @staticmethod
    def _record_from_attempt(row: CallAttempt, log: Optional[CallLog]) -> CallRecordItem:
        """N2 行 → ``CallRecordItem``（``log`` 为该 attempt 的 N1 行，可能为 ``None``）。"""
        # 降级模式下 `attempt_count` 恰恰是与实际行数对不上的那一列，
        # 此处单行拿不到「本 request_id 的总行数」，退用 N1 值；N1 缺失时退用 attempt_no。
        attempt_count = int(getattr(log, "attempt_count", None) or row.attempt_no or 0)
        return CallRecordItem(
            request_id=row.request_id,
            attempt_id=row.attempt_id,  # N2 自身主键：降级模式下 C7 靠它消歧
            trace_id=getattr(log, "trace_id", None),
            direction=getattr(log, "direction", None) or "",
            trigger_type=getattr(log, "trigger_type", None) or "",
            caller=getattr(log, "source", None),
            target=getattr(log, "target", None) or "",
            protocol=getattr(log, "protocol", None),
            method=None,
            path=None,
            status=row.status,
            error_code=row.error_code,
            duration_ms=_duration_ms(row.attempt_started_at, row.attempt_ended_at),
            logical_started_at=_iso(row.attempt_started_at) or "",
            final_ended_at=_iso(row.attempt_ended_at),
            attempt_count=attempt_count,
            retry_count=max(attempt_count - 1, 0),
        )

    # ------------------------------------------------------------------
    # C7 · 单次调用详情
    # ------------------------------------------------------------------

    @_resilient(_empty_call_detail)
    async def call_detail(
        self, request_id: str, *, attempt_id: Optional[str] = None
    ) -> CallDetail:
        """C7 单次调用详情。

        模式判定收窄到**单条**（``count_unlinkable_for_request``）—— C7 没有
        ``from``/``to``，套不上区间判据。降级模式必须带 ``attempt_id``，
        缺失抛 ``1001``（路径不变，attempt_id 作可选查询参数）。
        """
        degraded = await self.det.count_unlinkable_for_request(request_id) > 0
        if degraded:
            if not attempt_id:
                raise InvalidParameterError(
                    message="降级模式下查询调用详情必须提供 attempt_id",
                    details={"request_id": request_id},
                )
            row: Optional[Any] = await self.det.get_attempt_by_attempt_id(attempt_id)
        else:
            row = await self.det.get_call_log_by_request_id(request_id)
        if row is None:
            raise ResourceNotFoundError(
                message=f"调用记录不存在: {request_id}",
            )
        return self._detail_from_row(
            request_id, row, _MODE_DEGRADED if degraded else "logical"
        )

    @staticmethod
    def _detail_from_row(request_id: str, row: Any, metric_mode: str) -> CallDetail:
        """N1 / N2 行 → ``CallDetail``（薄壳：无落点的字段一律 ``None``）。"""
        if isinstance(row, CallAttempt):
            return CallDetail(
                request_id=row.request_id,
                method=None,
                path=None,
                status_code=None,
                error_message=row.error_message,
                request_payload=None,
                response_payload=None,
                prompt_tokens=None,
                completion_tokens=None,
                total_tokens=None,
                stage=row.attempt_no,  # 降级模式下 Attempt 序号即 stage
                meta=None,
                mode_meta=MonitorCallService._mode_meta_no_range(metric_mode),
            )
        return CallDetail(
            request_id=row.request_id,
            method=None,
            path=None,
            status_code=None,
            error_message=row.error_message,
            request_payload=None,
            response_payload=None,
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            stage=None,  # 正常模式无「当前 stage」语义
            meta=None,
            mode_meta=MonitorCallService._mode_meta_no_range(metric_mode),
        )

    # ------------------------------------------------------------------
    # C8 · Attempt 明细
    # ------------------------------------------------------------------

    @_resilient(_empty_triple)
    async def attempts(
        self, request_id: str, *, page: int = 1, page_size: int = 20
    ) -> Tuple[List[AttemptItem], int, int]:
        """C8 某 ``request_id`` 下的 Attempt 明细，返回 ``(items, total, retry_count)``。"""
        rows, total = await self.det.list_attempts(
            request_id, offset=self._offset(page, page_size), limit=page_size
        )
        # 降级下 `call_log.attempt_count` 正是对不上账的那一列，改按实际行数推导
        degraded = await self.det.count_unlinkable_for_request(request_id) > 0
        retry_count = await self.det.get_retry_count(request_id, degraded=degraded)
        items = [
            AttemptItem(
                attempt_id=row.attempt_id,
                attempt_no=row.attempt_no,
                attempt_started_at=_iso(row.attempt_started_at) or "",
                attempt_ended_at=_iso(row.attempt_ended_at),
                duration_ms=_duration_ms(row.attempt_started_at, row.attempt_ended_at),
                status=row.status,
                error_code=row.error_code,
                error_message=row.error_message,
                is_final=bool(row.is_final),
                wait_before_ms=row.wait_before_ms,
            )
            for row in rows
        ]
        return items, total, retry_count

    # ------------------------------------------------------------------
    # C9 · 对象 / 能力详情
    # ------------------------------------------------------------------

    @_resilient(_empty_object_detail)
    async def object_detail(
        self,
        object_id: str,
        frm: str,
        to: str,
        *,
        protocol: Optional[str] = None,
    ) -> CallObjectDetail:
        """C9 对象 / 能力详情（基础指标 + 来源 TOP3 + 最近失败调用）。"""
        start, end = self._parse_range(frm, to)
        mode = await self.agg.resolve_metric_mode(start, end)
        data = await self.agg.object_detail(
            start,
            end,
            object_id=object_id,
            protocol=protocol,
            degraded=(mode == _MODE_DEGRADED),
        )
        if data is None:
            raise ResourceNotFoundError(message=f"对象不存在：{object_id}")
        return CallObjectDetail(
            object_id=data["object_id"] or object_id,
            # 未知类型回空串（同 C2 的 ``object_list``）：⛔ 不拿对象名充当类型。
            object_type=data["object_type"] or "",
            call_count=data["call_count"],
            success_rate=data["success_rate"],
            avg_latency_ms=data["avg_latency_ms"],
            failed_count=data["failed_calls"],
            top_sources=[
                {"name": s["name"], "call_count": s["call_count"]}
                for s in data["top_sources"]
            ],
            recent_failures=[
                {
                    "request_id": f["request_id"],
                    "error_message": f["error_message"],
                    "logical_started_at": _iso(f["logical_started_at"]),
                }
                for f in data["recent_failures"]
            ],
            meta=None,  # 采集元数据当前不填（契约中列为可选）
            mode_meta=self._mode_meta(mode, start, end),
        )

    # ------------------------------------------------------------------
    # M1 · 筛选元数据
    # ------------------------------------------------------------------

    @_resilient(_empty_meta_filters)
    async def meta_filters(self) -> MetaFilters:
        """M1 四类下拉候选值。

        ``objects`` / ``call_sources`` 来自 ``monitor`` 数据层；``task_types``
        来自任务中心内置清单表（跨能力只读，见仓储该方法 docstring）；
        ``statuses`` 是静态映射常量。
        """
        return MetaFilters(
            task_types=await self.agg.list_task_types(),
            statuses=[dict(item) for item in _STATUS_DICT],
            objects=await self.agg.list_objects(),
            call_sources=await self.agg.list_call_sources(),
        )


__all__ = ["MonitorCallService"]
