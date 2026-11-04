"""平台监控 · 任务监控读接口编排层（T1–T4）。

职责（薄编排，业务规则在此，取数在 :mod:`capabilities.platform.repository.monitor_task`）：

- 时间区间解析与 ``from >= to`` 校验（抛 ``1001 InvalidParameterError``）；
- ``metric`` / ``bucket`` 白名单校验；
- 分页换算 ``page`` / ``page_size`` → ``offset`` / ``limit``（T3 出参**不自加**
  ``page`` / ``page_size``，只回 ``items`` + ``total``）；
- 两张表（``stage`` 白名单已落在 SQL；``reason`` 在此归并）；
  ``task_type`` **不翻译** —— 直出原始 ``definition_id``；
- 组装 ``dtos/monitor.py`` 的出参 DTO。

租户
----
**不按租户 / 空间过滤**（显式例外）：可见性只凭权限码
``platform:monitor:read``。本层不调 ``require_tenant``，route 层先判权后查询。
"""

from datetime import datetime, timedelta
from typing import Any, List, Optional, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from capabilities.platform.dtos.monitor import (
    FailedTaskItem,
    MonitorFlags,
    TaskStageItem,
    TaskSummary,
    TaskTypeOverview,
    TrendPoint,
)
from capabilities.platform.repository.monitor_task import MonitorTaskRepository
from capabilities.platform.services.monitor_buckets import bucket_starts
from capabilities.platform.services.monitor_range import enforce_max_range
from capabilities.platform.services.monitor_resilience import (
    MODULE_ERROR,
    resilient as _resilient,
)
from jonex_core.common.exceptions import InvalidParameterError

#: 任务中心时间口径：``task_center`` 两表是 ``timestamp without time zone``，写入侧
#: ``datetime.now()`` 跑在 ``TZ=Asia/Shanghai`` 的容器里 ⇒ 列里存的是**北京墙钟的
#: naive 值**。本模块显式用这个 ``ZoneInfo`` 换算，不依赖进程 ``TZ``（降级/测试/
#: 其他部署画像下进程时区未必是北京）。
_BJS = ZoneInfo("Asia/Shanghai")

#: 降级态 ``module_error`` 取值（脱敏：只标「本模块暂不可用」，不带底层异常内容）。
_MODULE_ERROR = "module_unavailable"

#: T2 ``metric`` 白名单 → 仓储列名。
_METRICS = {
    "task_count": "task_count",
    "success_rate": "success_rate",
    "avg_duration": "avg_duration_ms",
    "failed_count": "failed_count",
}

#: T2 ``bucket`` 白名单 → 桶宽（用于计算 ``closed``）。
#: ``5m`` 是唯一会补空桶的粒度；``hour`` / ``day`` 仍只返回有数据的桶。
_BUCKETS = {
    "5m": timedelta(minutes=5),
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
}

#: ``reason`` 归并表（``tasks.error_code`` → 前端 5 档之一）。未命中的一切
#: （含无 ``error_code`` 的老数据）一律回落 ``"ontology"``。
_REASON_COMPILE = frozenset(
    {
        "err.kb.task_center.compile_failed",
        "err.kb.task_center.wiki_compile_failed",
        # ⚠️ ``err.task_center.task.aggregate_failed`` **不在这里**：它是任务收口外壳，
        # 真正的失败节点在 ``error_params["parts"]``。只看外壳代号会把 parse 步挂掉
        # 也标成编译失败（2026-10-23 bug），判档见 ``_translate_reason``。
    }
)
_REASON_PARSE = frozenset(
    {
        "err.kb.task_center.step_hard_timeout",
        "err.kb.task_center.downstream_failed",
        "err.task_center.subtask.timed_out",
        "err.task_center.subtask.pending_stuck",
        "err.task_center.subtask.runner_unbound",
        "err.task_center.subtask.dispatch_failed",
        "err.task_center.subtask.dispatch_exhausted",
    }
)
#: 任务中心收口时写的**外壳**代号（零件数组在 ``error_params["parts"]``）。
_AGGREGATE_FAILED = "err.task_center.task.aggregate_failed"
#: 归并兜底档：认不出阶段的一切（含无代号的老数据）。
_REASON_FALLBACK = "ontology"
#: 阶段先后 —— 一条任务同时挂了多个节点时，**取最早的那一步**（下游失败多半是后果）。
_REASON_ORDER = ("detect", "format", "parse", "ontology", "compile")
#: 步骤代号 / 编排节点 id 前缀 → 失败原因档。``step``（写在 ``error_params`` 里）
#: 最精确；没有它才退回节点 id（``parse_1`` / ``compile_1`` / 改版前的 ``ingest_1``）。
_STEP_REASON = {
    "detect": "detect",
    "format": "format",
    "parse": "parse",
    "ingest": "parse",  # 改版前遗留步骤，仍属解析入库阶段
    "extract": "ontology",
    "compile": "compile",
}


def _reason_from_one(
    code: Optional[str], params: Any, node_id: Optional[str]
) -> Optional[str]:
    """单条失败原因（聚合零件 / 任务级直出）→ 五档之一；认不出返回 ``None``。

    判据顺序：**编译代号最权威**（它本身就点名了阶段）→ ``params.step`` → 节点 id
    前缀 → 其余代号表。``step_hard_timeout`` / ``downstream_failed`` 这类代号本身
    不带阶段，必须靠 ``step`` 或 ``node_id`` 才能分清 parse / compile。
    """
    if code in _REASON_COMPILE:
        return "compile"
    if isinstance(params, dict):
        step = str(params.get("step") or "").strip().lower()
        if step in _STEP_REASON:
            return _STEP_REASON[step]
    node = str(node_id or "").strip().lower()
    if node:
        head = node.split("_", 1)[0]
        if head in _STEP_REASON:
            return _STEP_REASON[head]
    if code in _REASON_PARSE:
        return "parse"
    return None


def _parse_ts(value: str, field: str) -> datetime:
    """ISO8601 字符串 → tz-aware ``datetime``（缺时区按 **Asia/Shanghai** 解释）。

    前端可能传 ``Z`` / ``+08:00`` / 完全无时区。无时区的输入按北京时间解释：
    任务中心的库列本身就是北京墙钟，前端（以及 ``datetime-local``）给出的也是
    北京时间，再按 UTC 兜底会整体偏移 8 小时。
    """
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise InvalidParameterError(
            message=f"{field} 必须是 ISO8601 时间字符串",
            details={"field": field, "value": str(value)},
        )
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_BJS)


def _iso(value: Optional[datetime]) -> Optional[str]:
    """``datetime`` → **北京时间** ISO8601（``+08:00`` 结尾）；``None`` 原样透传。

    naive 值来自 ``task_center`` 的无时区列，按北京墙钟补 ``_BJS`` 后再格式化；
    aware 值（如 ``monitor`` 自己 ``timezone=True`` 的列）先换算到北京时间。
    ⚠️ 显式 ``astimezone(_BJS)``，**不用**无参 ``astimezone()`` —— 后者依赖
    进程 ``TZ``，容器/测试进程时区不同会得到不同的偏移。
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=_BJS)
    return value.astimezone(_BJS).isoformat()


def _to_naive_beijing(value: datetime) -> datetime:
    """tz-aware → naive **北京墙钟**；naive 原样返回。

    ``task_center`` 两张表的时间列都是 ``timestamp without time zone``，绑定参数
    必须同为 naive，否则 asyncpg 抛 ``can't subtract offset-naive and offset-aware
    datetimes``（必 500）。写入侧存的是北京墙钟，所以这里必须换算到
    ``Asia/Shanghai`` 再剥 tzinfo —— 换算是**显式 ZoneInfo**，不依赖容器 ``TZ``。
    ``monitor`` 自己那两张表是 ``timezone=True``，所以 ``monitor_call`` 没有这一步，
    两边的时区约定并不通用。
    """
    if value.tzinfo is None:
        return value
    return value.astimezone(_BJS).replace(tzinfo=None)


def _float(value) -> Optional[float]:
    """数值归一成 ``float``；``None`` 原样透传（空集返 ``null``，不兜底 0）。"""
    return None if value is None else float(value)


# ══════════════════════════════════════════════════════════════════
# 降级保护层（共用装饰器在 ``monitor_resilience``；此处只放空壳工厂）
# ══════════════════════════════════════════════════════════════════


def _empty_summary(*_args, **_kwargs) -> TaskSummary:
    """T1 降级空壳（结构完整：全零 / ``None`` / 空 as-of + ``flags.module_error`` 非空）。

    承载形式与 C/K 族同形（``flags`` 子对象）—— 降级时置
    ``flags.module_error`` 非空。
    """
    return TaskSummary(
        total_tasks=0,
        success_rate=None,
        avg_duration_ms=None,
        waiting_now=0,
        incomplete_count=0,
        flags=MonitorFlags(module_error=_MODULE_ERROR),
        as_of="",
    )


def _empty_list(*_args, **_kwargs) -> list:
    """列表读接口降级空壳：``[]``（T2 趋势 / T4 概览）。"""
    return []


def _empty_pair(*_args, **_kwargs) -> Tuple[list, int]:
    """分页读接口降级空壳：``(items, total)``（T3 失败任务）。"""
    return [], 0


class MonitorTaskService:
    """任务监控读接口（T1–T4）编排层。"""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.repo = MonitorTaskRepository(session)

    # ------------------------------------------------------------------
    # 编排内部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_range(frm: str, to: str) -> Tuple[datetime, datetime]:
        """解析并校验 ``[from, to)``；``from >= to`` 抛 ``1001``。

        返回值剥掉时区、统一按 **Asia/Shanghai 墙钟**的 naive 值 —— 见
        ``_to_naive_beijing``：区间锚点落在 ``task_center.tasks.created_at`` 上，
        而那是无时区列，且写入侧存的是北京墙钟（容器 ``TZ=Asia/Shanghai`` +
        ``datetime.now()``）。按 UTC 比较会让整个筛选窗口偏 8 小时。
        """
        start = _parse_ts(frm, "from")
        end = _parse_ts(to, "to")
        if start >= end:
            raise InvalidParameterError(
                message="时间范围非法：from 必须早于 to",
                details={"from": _iso(start), "to": _iso(end)},
            )
        # 超过自定义查询上限（默认 30 天）→ 4004；与参数格式错误区分。
        start, end = enforce_max_range(start, end)
        return _to_naive_beijing(start), _to_naive_beijing(end)

    @staticmethod
    def _offset(page: int, page_size: int) -> int:
        return (page - 1) * page_size

    @staticmethod
    def _translate_reason(
        error_code: Optional[str], error_params: Optional[dict] = None
    ) -> str:
        """失败原因 → 前端五档之一（认不出一律 ``"ontology"``）。

        ``error_code == "err.task_center.task.aggregate_failed"`` 时**必须看零件**：
        那是任务中心的收口「外壳」代号，真正的失败节点在
        ``error_params["parts"]``（每项 ``{node_id, code, params, message}``）。
        只按外壳代号判档会把「parse 步挂掉」标成编译失败 —— 任务中心详情已经按零件
        渲染成「解析失败」，监控列表却显示「编译失败」，两处对不上（2026-10-23 修复）。

        零件之间取**最早失败的那一步**（``_REASON_ORDER``）：下游失败多半是上游的后果，
        把后果当成原因读会指错方向。零件全都认不出时回落兜底档；无零件的旧数据
        （``error_params`` 为空）也走兜底，不再沿用「外壳 ⇒ 编译」的猜测。
        """
        if error_code == _AGGREGATE_FAILED and isinstance(error_params, dict):
            parts = error_params.get("parts")
            if isinstance(parts, list):
                reasons = set()
                for part in parts:
                    if not isinstance(part, dict):
                        continue
                    reason = _reason_from_one(
                        part.get("code"), part.get("params"), part.get("node_id")
                    )
                    if reason:
                        reasons.add(reason)
                if reasons:
                    return min(reasons, key=_REASON_ORDER.index)
                return _REASON_FALLBACK
        reason = _reason_from_one(error_code, error_params, None)
        return reason or _REASON_FALLBACK

    # ------------------------------------------------------------------
    # T1 · 统计卡
    # ------------------------------------------------------------------

    @_resilient(_empty_summary)
    async def summary(self, frm: str, to: str) -> TaskSummary:
        """T1 任务统计卡。"""
        start, end = self._parse_range(frm, to)
        data = await self.repo.summary(start, end)
        return TaskSummary(
            total_tasks=int(data["total_tasks"] or 0),
            success_rate=_float(data["success_rate"]),
            avg_duration_ms=(
                None if data["avg_duration_ms"] is None else int(data["avg_duration_ms"])
            ),
            waiting_now=int(data["waiting_now"] or 0),
            incomplete_count=int(data["incomplete_count"] or 0),
            # 三个标志位走 ``flags`` 子对象（与 C/K 族同形），本期恒取默认
            # （``data_delayed=False`` / ``demo_data=False`` / ``module_error=None``）；
            # 降级态的非空 ``module_error`` 由 :func:`_empty_summary` 置。
            flags=MonitorFlags(),
            # 空表返空串（同 K1），不兜成当前时刻。
            as_of=_iso(data["as_of"]) or "",
        )

    # ------------------------------------------------------------------
    # T2 · 趋势
    # ------------------------------------------------------------------

    @_resilient(_empty_list)
    async def trend(
        self,
        frm: str,
        to: str,
        *,
        metric: str,
        bucket: str,
        task_type: Optional[str] = None,
    ) -> List[TrendPoint]:
        """T2 任务趋势（按 ``metric`` 单选一列作 ``value``、按 ``bucket`` 分桶）。"""
        start, end = self._parse_range(frm, to)
        if metric not in _METRICS:
            raise InvalidParameterError(
                message="metric 取值非法，仅支持 task_count / success_rate / "
                "avg_duration / failed_count",
                details={"metric": metric, "allowed": sorted(_METRICS)},
            )
        if bucket not in _BUCKETS:
            raise InvalidParameterError(
                message="bucket 取值非法，仅支持 5m / hour / day",
                details={"bucket": bucket, "allowed": sorted(_BUCKETS)},
            )

        column = _METRICS[metric]
        width = _BUCKETS[bucket]
        # 同一响应的所有桶由同一个 now() 快照判定未闭合。仓储返回的
        # ``bucket_start`` 是任务中心列的 naive 北京墙钟，``now`` 必须同口径。
        now = datetime.now(_BJS).replace(tzinfo=None)
        rows = await self.repo.trend(start, end, bucket, task_type)

        # 全部粒度按固定时间网格补空桶（``value=None``，⛔ 不兜 0）。
        # ``start`` / ``end`` 是 naive 北京墙钟，网格与仓储行同为 naive，可直接作 key。
        # 真实行里落在网格之外的桶（窗口首部不完整桶）直接丢弃。
        if bucket in _BUCKETS:
            rows_by_start = {
                row["bucket_start"]: row
                for row in rows
                if row["bucket_start"] is not None
            }
            points: List[TrendPoint] = []
            for bucket_start in bucket_starts(start, end, bucket):
                row = rows_by_start.get(bucket_start)
                value = None if row is None else row[column]
                points.append(
                    TrendPoint(
                        bucket_start=_iso(bucket_start) or "",
                        value=None if value is None else float(value),
                        closed=(bucket_start + width) <= now,
                    )
                )
            return points

        points = []
        for row in rows:
            bucket_start = row["bucket_start"]
            value = row[column]
            points.append(
                TrendPoint(
                    bucket_start=_iso(bucket_start) or "",
                    # 比率/均值空桶（NULL）原样透传 ``None`` —— 空桶即「无数据」，禁止兜 0。
                    value=None if value is None else float(value),
                    # 同 C5：``bucket_start`` 为 ``None`` 时不得参与加法（否则 500）。
                    closed=bucket_start is not None and (bucket_start + width) <= now,
                )
            )
        return points

    # ------------------------------------------------------------------
    # T3 · 失败任务列表（分页）
    # ------------------------------------------------------------------

    @_resilient(_empty_pair)
    async def failures(
        self,
        frm: str,
        to: str,
        *,
        task_type: Optional[str] = None,
        page: int = 1,
        page_size: int = 5,
    ) -> Tuple[List[FailedTaskItem], int]:
        """T3 失败任务列表，返回 ``(items, total)``（出参不含 ``page`` / ``page_size``）。"""
        start, end = self._parse_range(frm, to)
        rows, total = await self.repo.failures(
            start, end, task_type, self._offset(page, page_size), page_size
        )
        items = [
            FailedTaskItem(
                task_id=row["task_id"],
                task_name=row["task_name"],
                # 直出原始 `definition_id`，不再折叠成档位
                task_type=row["task_type"],
                status=row["status"],
                error_message=row["error_message"],
                finished_at=_iso(row["finished_at"]),
                # 聚合外壳要靠零件（error_params.parts）才判得准阶段；老行没有它
                reason=self._translate_reason(
                    row["error_code"], row.get("error_params")
                ),
            )
            for row in rows
        ]
        return items, total

    # ------------------------------------------------------------------
    # T4 · 运行概览（父表 + 嵌套 stages[]）
    # ------------------------------------------------------------------

    @_resilient(_empty_list)
    async def overview(
        self, frm: str, to: str, *, task_type: Optional[str] = None
    ) -> List[TaskTypeOverview]:
        """T4 运行概览：父表按 ``task_type`` 分组 + 嵌套按 ``step_code`` 分组的 ``stages[]``。"""
        start, end = self._parse_range(frm, to)
        parents = await self.repo.overview_parent(start, end, task_type)
        stages = await self.repo.overview_stages(start, end, task_type)

        stages_by_type: dict = {}
        for row in stages:
            stages_by_type.setdefault(row["task_type"], []).append(
                TaskStageItem(
                    stage_code=row["stage_code"],
                    stage_name=row["stage_name"] or row["stage_code"],
                    exec_count=int(row["exec_count"] or 0),
                    running_count=int(row["running_count"] or 0),
                    waiting_count=int(row["waiting_count"] or 0),
                    failed_count=int(row["failed_count"] or 0),
                    success_rate=_float(row["success_rate"]),
                    avg_duration_ms=(
                        None
                        if row["avg_duration_ms"] is None
                        else int(row["avg_duration_ms"])
                    ),
                )
            )

        return [
            TaskTypeOverview(
                # 分组键即原始 `definition_id`（仓储已直出，本层不再归并）
                task_type=row["task_type"],
                total_count=int(row["total_count"] or 0),
                running_count=int(row["running_count"] or 0),
                waiting_count=int(row["waiting_count"] or 0),
                failed_count=int(row["failed_count"] or 0),
                success_rate=_float(row["success_rate"]),
                avg_duration_ms=(
                    None if row["avg_duration_ms"] is None else int(row["avg_duration_ms"])
                ),
                stages=stages_by_type.get(row["task_type"], []),
            )
            for row in parents
        ]


__all__ = ["MonitorTaskService"]
