"""平台监控 · 调用监控读接口编排层（C1–C9 + M1）。

职责（薄编排，业务规则在此，取数在仓储）：

- 时间区间解析与 ``from >= to`` 校验（抛 ``1001 InvalidParameterError``）；
- 分页换算 ``page``/``page_size`` → ``offset``/``limit``；
- **降级模式判定一次、整次查询统一透传**（禁按行切换）；
- 把仓储的行对象组装成 ``dtos/monitor.py`` 的出参 DTO。

两个页签的切分
--------------
``monitor.call_log`` **没有** ``object_type`` 列，切不开「外部依赖」与「开放能力」。
用 ``direction`` + ``call_scope`` 两个维度切分 —— 外部依赖（C1/C2）=
``outbound`` + ``external``（排除任务中心派活/回报等平台内部互调），开放能力
（C3/C4）= ``inbound``。故 C1/C3 与 C2/C4 是同一方法换筛选条件调用。

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
from typing import Any, Dict, List, Optional, Sequence, Tuple

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
from capabilities.platform.repository.monitor_caller import (
    MonitorCallerRepository,
    SourceKey,
    parse_source_token,
)
from capabilities.platform.repository.monitor_detail import MonitorDetailRepository
from capabilities.platform.services.monitor_buckets import FIVE_MINUTES, bucket_starts
from capabilities.platform.services.monitor_range import enforce_max_range
from capabilities.platform.services.monitor_resilience import (
    MODULE_ERROR,
    resilient as _resilient,
)
from jonex_core.common.exceptions import (
    InvalidParameterError,
    ResourceNotFoundError,
)
from jonex_core.common.monitor_identity import (
    format_user_identity,
    is_user_identity,
)
from jonex_core.common.monitor_models import CallAttempt, CallLog, CallScope

#: 降级态 ``module_error`` 取值（脱敏：只标「本模块暂不可用」，不带底层异常内容）。
_MODULE_ERROR = MODULE_ERROR

#: 模式元数据的版本号（本期常量，供日后口径变更时分段识别）。
_MODE_VERSION = "v1"

#: 两个页签的 direction 取值。
_DIRECTION_EXTERNAL = "outbound"  # 外部依赖（C1/C2）
_DIRECTION_OPEN = "inbound"  # 开放能力（C3/C4）

#: 外部依赖页的调用边界。``direction=outbound`` 仍会混入任务中心派活/回报等
#: 平台内部互调，故外部依赖统计卡 / 对象列表 / 趋势统一再按 ``call_scope`` 收窄。
_CALL_SCOPE_EXTERNAL = CallScope.EXTERNAL.value

#: 外部依赖页允许的对象类型对应协议。
#: ⚠️ 当前 ``call_scope=external`` 与「``direction=outbound`` 且 ``protocol=openai``」
#: 等价（见 ``monitor_collector._fallback_call_scope``）。未来新增外部协议时，
#: 这里与前端 ``pmFailureDrillParams`` 的下钻口径须同步扩展。
_EXTERNAL_PROTOCOLS = frozenset({"openai"})

_MODE_DEGRADED = "degraded"

#: C5 桶粒度 → 桶宽（用于判断 ``closed``）。``5m`` 只有本 service 会补空桶。
_BUCKET_SPANS = {
    "5m": FIVE_MINUTES,
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
}

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


def _empty_summary(frm: str, to: str, *, direction: str, **_kwargs) -> CallSummary:
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
        self.caller = MonitorCallerRepository(session)

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
    async def summary(
        self, frm: str, to: str, *, direction: str, call_scope: Optional[str] = None
    ) -> CallSummary:
        """统计卡取数（C1 ``direction='outbound'`` / C3 ``direction='inbound'``）。

        ``call_scope`` 可选：外部依赖页传 ``external``，把平台内部互调排除在外；
        开放能力页不传（``direction=inbound`` 本身即外部调用方进入平台）。
        """
        start, end = self._parse_range(frm, to)
        mode = await self.agg.resolve_metric_mode(start, end)
        data = await self.agg.summary(
            start,
            end,
            direction=direction,
            call_scope=call_scope,
            degraded=(mode == _MODE_DEGRADED),
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
        """C1 外部依赖统计卡：``outbound`` + ``call_scope=external``。"""
        return await self.summary(
            frm, to, direction=_DIRECTION_EXTERNAL, call_scope=_CALL_SCOPE_EXTERNAL
        )

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
        call_scope: Optional[str] = None,
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

        ``call_scope=external`` 时还会校验推导出的 ``protocol`` 是否属于外部集合
        （当前只有 ``openai``）：``object_type=能力`` 推导出 ``invoke`` 属平台内部
        互调，与外部依赖互斥，直接返空而不发查询。
        """
        start, end = self._parse_range(frm, to)
        if object_type is not None:
            derived = _PROTOCOL_BY_OBJECT_TYPE.get(object_type)
            if derived is None or (protocol is not None and protocol != derived):
                return [], 0, None
            if call_scope == _CALL_SCOPE_EXTERNAL and derived not in _EXTERNAL_PROTOCOLS:
                return [], 0, None
            protocol = derived
        mode = await self.agg.resolve_metric_mode(start, end)
        rows, total = await self.agg.object_list(
            start,
            end,
            direction=direction,
            call_scope=call_scope,
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
        """C2 外部依赖对象列表（不含 ``protocol`` 筛选，固定 ``call_scope=external``）。"""
        return await self.object_list(
            frm, to, direction=_DIRECTION_EXTERNAL, call_scope=_CALL_SCOPE_EXTERNAL,
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
        call_scope: Optional[str] = None,
    ) -> Tuple[List[TrendPoint], Optional[CallModeMeta]]:
        """趋势取数。未闭合桶（右边界 > now）标 ``closed=False``。

        ``direction`` 切两个页签口径（outbound 外部依赖 / inbound 开放能力）；
        ``call_scope`` 进一步排除平台内部互调（外部依赖页传 ``external``）；
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
            call_scope=call_scope,
            degraded=(mode == _MODE_DEGRADED),
        )
        now = datetime.now(timezone.utc)
        span = _BUCKET_SPANS.get(bucket, timedelta(days=1))

        # 全部粒度按固定时间网格补空桶（``value=None``，⛔ 不兜 0）。
        # ``start`` / ``end`` 是 tz-aware UTC，网格同形态；仓储行可能带任意 tz，
        # 统一换算到 UTC 后作 key（aware datetime 相等/哈希按同一时刻比较）。
        # 真实行里落在网格之外的桶（窗口首部不完整桶）直接丢弃。
        if bucket in _BUCKET_SPANS:
            rows_by_start = {}
            for row in rows:
                bucket_start = row["bucket_start"]
                if bucket_start is None:
                    continue
                if bucket_start.tzinfo is None:
                    bucket_start = bucket_start.replace(tzinfo=timezone.utc)
                rows_by_start[bucket_start] = row
            grid_points: List[TrendPoint] = []
            for bucket_start in bucket_starts(start, end, bucket):
                row = rows_by_start.get(bucket_start)
                value = None if row is None else row["value"]
                grid_points.append(
                    TrendPoint(
                        bucket_start=_iso(bucket_start) or "",
                        value=None if value is None else float(value),
                        closed=(bucket_start + span) <= now,
                    )
                )
            return grid_points, self._mode_meta(mode, start, end)

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
    # 调用方展示名解析（读侧兜底历史数字 ID）
    # ------------------------------------------------------------------

    async def _resolve_callers(self, pairs: set[SourceKey]) -> dict:
        """批量解析 ``(tenant_id, source)`` → 用户名；非用户形态不进入解析集合。

        解析失败只返回空映射（仓储内已降级），展示层回退原始 source。
        """
        if not pairs:
            return {}
        return await self.caller.resolve_sources(pairs)

    @staticmethod
    def _caller_pairs(items) -> set[SourceKey]:
        """从 ``(tenant_id, source)`` 候选里筛出可解析的用户形态。"""
        pairs: set[SourceKey] = set()
        for tenant_id, source in items:
            if tenant_id and parse_source_token(source):
                pairs.add((tenant_id, source))
        return pairs

    async def _top_sources_with_callers(
        self, rows: Sequence[dict], *, limit: int = 3
    ) -> List[dict]:
        """来源条目：按**身份**合并同一个人 / 同一个系统标识的全部历史形态。

        聚合仓储额外带回 ``tenant_ids``（该 source 出现过的租户）；逐租户调用
        :meth:`MonitorCallerRepository.resolve_identities` 后分组：

        - 解析到用户 ⇒ 组键 ``("user", user_id)``，``name`` 输出规范形态
          ``user:<id>``，``caller_name`` 为该用户的 ``username``；
        - 解析不到（系统保留字 / 未知值）⇒ 组键 ``("raw", source)``，
          ``name`` 保持原值、``caller_name=None``。
        - 同一原始 source 在不同租户解析出**不同用户**（计数已跨租户合并、
          无法拆分归因）⇒ 归入 ``("raw", source)``，绝不把整份计数算到其中一人头上。

        ``call_count`` 组内求和，结果按计数降序、``name`` 升序稳定排序，
        最后截断到 ``limit`` 条（C9 契约为 TOP3）。

        上游聚合仓储按原始 ``source`` 取候选，这里按身份合并后条数会变少，
        因此上游多取一批候选（见 ``MonitorAggregateRepository.object_detail``），
        避免合并后不足 ``limit`` 项或漏掉落在截断外的形态。
        """
        pairs: set[SourceKey] = {
            (tenant_id, row["name"])
            for row in rows
            for tenant_id in (row.get("tenant_ids") or [])
            if tenant_id and row.get("name")
        }
        identities = await self.caller.resolve_identities(pairs) if pairs else {}

        grouped: Dict[Tuple[str, Any], dict] = {}
        order: List[Tuple[str, Any]] = []
        for row in rows:
            source = row["name"]
            tenant_ids = [t for t in (row.get("tenant_ids") or []) if t]
            resolved = [
                identities[(tenant_id, source)]
                for tenant_id in tenant_ids
                if (tenant_id, source) in identities
            ]
            distinct_ids = {identity.user_id for identity in resolved}
            if len(distinct_ids) == 1:
                user_id = distinct_ids.pop()
                key = ("user", user_id)
                name = format_user_identity(user_id) or f"user:{user_id}"
            else:
                # 无命中，或同一 source 跨租户解析出多个用户（计数不可拆）⇒ 原值。
                key = ("raw", source)
                name = source
                resolved = []
            entry = grouped.get(key)
            if entry is None:
                entry = {
                    "name": name,
                    "call_count": 0,
                    "caller_name": None,
                    "_usernames": set(),
                }
                grouped[key] = entry
                order.append(key)
            entry["call_count"] += int(row["call_count"] or 0)
            entry["_usernames"].update(
                identity.username for identity in resolved if identity.username
            )

        result: List[dict] = []
        for key in order:
            entry = grouped[key]
            usernames = entry.pop("_usernames")
            # 同一 id 跨租户解析出不同用户名属异常数据：取字典序最小者，保证稳定。
            entry["caller_name"] = min(usernames) if usernames else None
            result.append(entry)
        # 合并会改变相对计数，重新按 TOP 语义排序（同分按 name 稳定）。
        result.sort(key=lambda item: (-item["call_count"], item["name"]))
        return result[:limit]

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
        target_any: Optional[List[str]] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[CallRecordItem], int, Optional[CallModeMeta]]:
        """调用记录列表（正常模式 = 逻辑调用 / 降级模式 = 物理 Attempt）。

        ``target`` 是**精确匹配**（对象详情下钻用）：同一个对象标识可能互为前缀
        （如 ``svc-a`` / ``svc-ab``），模糊匹配会把别的对象一并带进来。
        其语义是「对外对象标识 = ``COALESCE(mcp_tool, target)``」，仓储按
        「``target`` 等于该值 或 ``mcp_tool`` 等于该值」匹配；出参的目标列同样
        取对外对象标识（MCP 调用显示工具名）。

        ``keyword`` 除既有 raw ILIKE 外，还会按 ``users.username`` 展开成
        ``(tenant_id, source)`` 精确对（按显示名搜索）；``target_any`` 是
        前端把能力释义展开后的原始标识列表，按同一目标列口径并入 OR 组。
        原始 ``caller`` / ``target`` 筛选值始终不变。

        ``source`` 是**单值**筛选（前端零改动），后端按 ``user:<id>`` 别名展开成
        该用户的全部历史形态 ``(tenant_id, source)``；展开结果与 keyword 的
        ``source_pairs`` **分开传参**（两者是 AND 关系，不能并入同一个 OR 组）。
        解析不到（保留字 / 未知值）时回退原有精确匹配。
        """
        start, end = self._parse_range(frm, to)
        source_pairs = (
            await self.caller.expand_keyword_sources(keyword) if keyword else []
        )
        source_filter_pairs = (
            await self.caller.expand_source_filter(source) if source else []
        )
        # 规范形态 ``user:<id>`` 以全局主键为准：模拟态下记录的租户是目标租户、
        # 用户属于原租户，按 ``(tenant, source)`` 对匹配会漏掉这些行。主键全局
        # 唯一，补一路「只按 source 匹配」不会串人；只对已能解析到用户的规范
        # 形态生效，解析不到时仍走原有精确匹配。
        source_global_forms = (
            [source]
            if source_filter_pairs and is_user_identity(source)
            else []
        )
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
            target_any=target_any,
            source_pairs=source_pairs,
            source_filter_pairs=source_filter_pairs,
            source_global_forms=source_global_forms,
            degraded=degraded,
            offset=self._offset(page, page_size),
            limit=page_size,
        )
        mode_meta = self._mode_meta(mode, start, end)
        if not degraded:
            mapping = await self._resolve_callers(
                self._caller_pairs((row.tenant_id, row.source) for row in rows)
            )
            return [
                self._record_from_log(
                    row,
                    caller_name=mapping.get((row.tenant_id, row.source)),
                )
                for row in rows
            ], total, mode_meta

        # 降级模式的行是 N2（call_attempt），缺 direction/trigger_type/target/source/trace_id
        # 五个 N1 列。按 request_id 批量回查 N1 补上（一页至多 page_size 行，一次 IN 查询）；
        # 补不到的（N2 先于 N1 落库的孤儿 attempt）留 None → 组装时空串兜底。
        n1 = await self.det.get_call_logs_by_request_ids([row.request_id for row in rows])
        # 降级模式下 source 借用 N1 字段，租户也取该 N1 行的 tenant_id。
        mapping = await self._resolve_callers(
            self._caller_pairs(
                (log.tenant_id, log.source)
                for log in (n1.get(row.request_id) for row in rows)
                if log is not None
            )
        )
        items = []
        for row in rows:
            log = n1.get(row.request_id)
            key = None if log is None else (log.tenant_id, log.source)
            items.append(
                self._record_from_attempt(
                    row,
                    log,
                    caller_name=None if key is None else mapping.get(key),
                )
            )
        return items, total, mode_meta

    @staticmethod
    def _record_from_log(
        row: CallLog, *, caller_name: Optional[str] = None
    ) -> CallRecordItem:
        """N1 行 → ``CallRecordItem``。

        目标列输出**对外对象标识** ``COALESCE(mcp_tool, target)``：
        MCP 调用展示工具名，其余链路回落能力标识 / 模型名。
        """
        attempt_count = int(row.attempt_count or 0)
        return CallRecordItem(
            request_id=row.request_id,
            attempt_id=None,  # 正常模式按 request_id 即唯一定位，无 attempt 概念
            trace_id=row.trace_id,
            direction=row.direction,
            trigger_type=row.trigger_type,
            caller=row.source,
            target=row.mcp_tool or row.target or "",
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
            caller_name=caller_name,
        )

    @staticmethod
    def _record_from_attempt(
        row: CallAttempt,
        log: Optional[CallLog],
        *,
        caller_name: Optional[str] = None,
    ) -> CallRecordItem:
        """N2 行 → ``CallRecordItem``（``log`` 为该 attempt 的 N1 行，可能为 ``None``）。

        目标列输出**对外对象标识** ``COALESCE(mcp_tool, target)``，工具名经 N1 借出
        （N2 本身没有该列）。
        """
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
            target=(
                getattr(log, "mcp_tool", None)
                or getattr(log, "target", None)
                or ""
            ),
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
            caller_name=caller_name,
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
        top_sources = await self._top_sources_with_callers(data["top_sources"])
        return CallObjectDetail(
            object_id=data["object_id"] or object_id,
            # 未知类型回空串（同 C2 的 ``object_list``）：⛔ 不拿对象名充当类型。
            object_type=data["object_type"] or "",
            call_count=data["call_count"],
            success_rate=data["success_rate"],
            avg_latency_ms=data["avg_latency_ms"],
            failed_count=data["failed_calls"],
            top_sources=top_sources,
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

        ``call_source_options`` 按**用户身份合并**：同一个人的
        ``username`` / 裸 ``id`` / ``user:<id>`` 等历史形态合并成一个选项，
        ``value="user:<id>"``、``label=username``（跨租户同一 id 合并成一项）；
        解析不到的系统标识（``apikey`` 等）原样输出 ``value=label=原值``。
        ``call_sources`` 保留但取 ``call_source_options[].value``，避免两套词汇
        并存 —— 前端把选中的值原样回传 ``source``，后端按身份展开命中全部形态。
        """
        source_rows = await self.agg.list_call_source_tenants()
        source_pairs: set[SourceKey] = {
            (tenant_id, row["source"])
            for row in source_rows
            for tenant_id in (row.get("tenant_ids") or [])
            if tenant_id and row.get("source")
        }
        identities = (
            await self.caller.resolve_identities(source_pairs) if source_pairs else {}
        )

        grouped: Dict[Tuple[str, Any], dict] = {}
        for row in source_rows:
            source = row["source"]
            for tenant_id in row.get("tenant_ids") or []:
                if not tenant_id:
                    continue
                identity = identities.get((tenant_id, source))
                if identity is None:
                    entry = grouped.setdefault(
                        ("raw", source),
                        {"value": source, "label": source, "_usernames": set()},
                    )
                    continue
                value = (
                    format_user_identity(identity.user_id) or f"user:{identity.user_id}"
                )
                entry = grouped.setdefault(
                    ("user", identity.user_id),
                    {"value": value, "label": None, "_usernames": set()},
                )
                if identity.username:
                    entry["_usernames"].add(identity.username)

        call_source_options: List[dict] = []
        seen_values: set[str] = set()
        identity_first = sorted(
            grouped, key=lambda key: (0 if key[0] == "user" else 1, str(key[1]))
        )
        for key in identity_first:
            entry = grouped[key]
            usernames = entry.pop("_usernames")
            # 同一 id 跨租户解析出不同用户名属异常数据：取字典序最小者，保证稳定。
            label = min(usernames) if usernames else entry["value"]
            value = entry["value"]
            if value in seen_values:
                # ``user:7`` 原始形态在别的租户未解析时可能另成 raw 选项；
                # 同一筛选值只保留身份项（身份项优先排序在前）。
                continue
            seen_values.add(value)
            call_source_options.append({"value": value, "label": label})
        call_source_options.sort(key=lambda opt: opt["value"])

        return MetaFilters(
            task_types=await self.agg.list_task_types(),
            statuses=[dict(item) for item in _STATUS_DICT],
            objects=await self.agg.list_objects(),
            call_sources=[opt["value"] for opt in call_source_options],
            call_source_options=call_source_options,
            call_targets=await self.agg.list_call_targets(),
        )


__all__ = ["MonitorCallService"]
