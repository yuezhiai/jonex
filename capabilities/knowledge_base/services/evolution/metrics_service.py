# -*- coding:utf-8 -*-
"""演化信任闭环 Task 9：采纳/基准指标聚合（spec §7 / §9）。

口径（与 spec §7 一致）：

- **按 run 漏斗**：窗口内（`created_at ∈ [since, until)`）创建的全部 run。
  受理数以审计 `evolution.adopt_accept` 为准（重放不重复写，天然去重）；
  上线前的历史窗口无法回填 —— 响应里的 `since` 就是这句声明的载体。
- **两类计数的口径不同**：`adopted / smoke_failed / bench_failed / bench_skipped`
  是**窗口内创建**的 run 的状态计数；`adopt_accepted / stale_conflicts` 是
  窗口内的**审计事件**计数。跨窗口完成的 run 会让两者出现差值（受理数可以
  大于 `adopted` / 基准计数），这是按 run 漏斗的预期口径，不是漏算。
- `p95_adopt_seconds`：优先 `bench_started_at → bench_ended_at`（完整语料跑完
  的真实耗时）。**一条可用 bench 时长都没有**时才整体回退到
  `adopted_at - created_at`，再没有就给 `None` —— 不把「没测过」伪装成 0 秒。
- `avg_corpus_size`：只统计 `bench.corpus_size` 有值的 run；`None`/缺键跳过，
  一条都没有 → `None`。
- `active_versions`：**租户级**（`semantic_versions` 只按 `tenant_id` 过滤，
  **不按 `knowledge_base_id` 过滤**）统计 `status='active'` 的当前快照，按
  `scope` 分组给计数（`{"abstract": n, "compiled": n}`）；不受 `days` 窗口影响。
  即：该租户全部知识库的 active 版本之和，不是单库视图。

审计面 fail-open：`platform.audit_logs` 在部分环境可能未就绪（老库 / 只跑
knowledge-base 的进程），读失败只记 0 + `audit_error`，**绝不 500** —— 指标
面板本身不该因为旁路审计缺失而整个打不开。
"""
from __future__ import annotations

import datetime
import logging
import math

from sqlalchemy import func, select, text

from ...models.evolution import EvolutionRun, SemanticVersion

logger = logging.getLogger(__name__)

ADOPT_ACCEPT_ACTION = "evolution.adopt_accept"
ADOPT_STALE_ACTION = "evolution.adopt_stale"

#: 两条审计计数一条语句取回；`SUM(CASE …)` 让「没有行」也能得到 0。
_AUDIT_SQL = text(
    """
    SELECT
        COALESCE(SUM(
            CASE WHEN action = :accept_action THEN 1 ELSE 0 END), 0
        ) AS adopt_accepted,
        COALESCE(SUM(
            CASE WHEN action = :stale_action THEN 1 ELSE 0 END), 0
        ) AS stale_conflicts
    FROM platform.audit_logs
    WHERE tenant_id = :tenant_id
      -- audit_logs.created_at 是 timestamp without time zone，由 DB 的 now()
      -- 按会话时区写入；这里把 aware UTC 窗口显式换成同一时区的 naive 值，
      -- 否则 asyncpg 会以「aware vs naive」拒绝参数（2026-10-24 125 实测）。
      AND created_at >= (
        CAST(:since AS timestamptz) AT TIME ZONE current_setting('TimeZone'))
      AND created_at < (
        CAST(:until AS timestamptz) AT TIME ZONE current_setting('TimeZone'))
    """
)


def _as_utc(value):
    """比较/相减前统一成 aware UTC（naive 视为 UTC，与 `run_store._is_stale` 同姿态）。"""
    if not isinstance(value, datetime.datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value


def _duration_seconds(start, end) -> float | None:
    """`start → end` 秒数；缺端点 / 逆序（负值）一律视为无数据。"""
    start, end = _as_utc(start), _as_utc(end)
    if start is None or end is None:
        return None
    seconds = (end - start).total_seconds()
    return seconds if seconds >= 0 else None


def _nearest_rank_p95(values: list[float]) -> float | None:
    """最近秩 p95：排序后取 `ceil(0.95 * n) - 1`（n=1 时取唯一样本）。"""
    if not values:
        return None
    ordered = sorted(values)
    index = math.ceil(0.95 * len(ordered)) - 1
    index = max(0, min(index, len(ordered) - 1))
    return round(ordered[index], 3)


async def _load_runs(session, tenant_id: str, since, until) -> list:
    result = await session.execute(
        select(EvolutionRun).where(
            EvolutionRun.tenant_id == tenant_id,
            EvolutionRun.created_at >= since,
            EvolutionRun.created_at < until,
        )
    )
    return list(result.scalars().all())


async def _active_version_counts(session, tenant_id: str) -> dict[str, int]:
    result = await session.execute(
        select(SemanticVersion.scope, func.count(SemanticVersion.id))
        .where(
            SemanticVersion.tenant_id == tenant_id,
            SemanticVersion.status == "active",
        )
        .group_by(SemanticVersion.scope)
    )
    counts = {"abstract": 0, "compiled": 0}
    for scope, count in result.all():
        if scope in counts:
            counts[scope] = int(count or 0)
    return counts


async def _audit_counts(session, tenant_id: str, since, until) -> dict[str, int]:
    result = await session.execute(
        _AUDIT_SQL,
        {
            "accept_action": ADOPT_ACCEPT_ACTION,
            "stale_action": ADOPT_STALE_ACTION,
            "tenant_id": tenant_id,
            "since": since,
            "until": until,
        },
    )
    row = result.mappings().first()
    if row is None:
        return {"adopt_accepted": 0, "stale_conflicts": 0}
    return {
        "adopt_accepted": int(row["adopt_accepted"] or 0),
        "stale_conflicts": int(row["stale_conflicts"] or 0),
    }


async def collect(session, tenant_id: str, days: int = 7,
                  now: datetime.datetime | None = None) -> dict:
    """聚合窗口 `[now-days, now)` 内的采纳/基准指标（含 `since` 声明）。"""
    until = _as_utc(now) or datetime.datetime.now(datetime.timezone.utc)
    since = until - datetime.timedelta(days=days)

    runs = await _load_runs(session, tenant_id, since, until)

    adopted = 0
    smoke_failed = 0
    bench_failed = 0
    bench_skipped = 0
    bench_durations: list[float] = []
    adopt_durations: list[float] = []
    corpus_sizes: list[int] = []

    for run in runs:
        status = getattr(run, "status", None)
        if status == "adopted":
            adopted += 1
        if status == "smoke-failed":
            smoke_failed += 1

        bench_status = getattr(run, "bench_status", None)
        if bench_status == "failed":
            bench_failed += 1
        elif bench_status == "skipped":
            bench_skipped += 1

        bench = getattr(run, "bench", None)
        if isinstance(bench, dict):
            size = bench.get("corpus_size")
            # bool 是 int 的子类：True 不是「语料 1 条」。
            if isinstance(size, int) and not isinstance(size, bool):
                corpus_sizes.append(size)

        bench_seconds = _duration_seconds(
            getattr(run, "bench_started_at", None),
            getattr(run, "bench_ended_at", None))
        if bench_seconds is not None:
            bench_durations.append(bench_seconds)

        adopt_seconds = _duration_seconds(
            getattr(run, "created_at", None),
            getattr(run, "adopted_at", None))
        if adopt_seconds is not None:
            adopt_durations.append(adopt_seconds)

    active_versions = await _active_version_counts(session, tenant_id)

    result = {
        "since": since.isoformat(timespec="seconds"),
        "until": until.isoformat(timespec="seconds"),
        "days": days,
        "adopt_accepted": 0,
        "adopted": adopted,
        "smoke_failed": smoke_failed,
        "bench_failed": bench_failed,
        "bench_skipped": bench_skipped,
        "stale_conflicts": 0,
        "p95_adopt_seconds": _nearest_rank_p95(
            bench_durations or adopt_durations),
        "active_versions": active_versions,
        "avg_corpus_size": (
            round(sum(corpus_sizes) / len(corpus_sizes), 2)
            if corpus_sizes else None),
    }

    try:
        audit = await _audit_counts(session, tenant_id, since, until)
    except Exception as exc:  # noqa: BLE001 —— 审计旁路缺失不得让面板 500
        logger.warning("演化指标读取审计失败 tenant=%s: %s", tenant_id, exc)
        # 失败事务必须回滚：PostgreSQL 里不 rollback，同一会话上的后续语句会连锁报错。
        rollback = getattr(session, "rollback", None)
        if rollback is not None:
            try:
                await rollback()
            except Exception as rollback_exc:  # noqa: BLE001 —— 回滚失败只记录
                logger.warning("演化指标审计回滚失败: %s", rollback_exc)
        result["audit_error"] = str(exc) or exc.__class__.__name__
    else:
        result["adopt_accepted"] = audit["adopt_accepted"]
        result["stale_conflicts"] = audit["stale_conflicts"]

    return result
