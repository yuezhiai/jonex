# -*- coding:utf-8 -*-
"""演化运行存储与状态机（迁移 016 / 024）。

生命周期：`queued` → `running` → `evaluated` / `failed` / `dry-run` / `empty-draft`。
信任闭环（024）在状态白名单里新增 `adopting` / `smoke-failed`（状态迁移由后续任务接入）。
仅 `queued` / `running` 视为活跃；`(tenant_id) WHERE status IN ('queued','running')`
的部分唯一索引保证同租户同时只有一个活跃运行。

租户隔离：`get` / `list` / `log_tail` 一律带 `tenant_id` 条件；`update` 以
`run_id` 为主键（调用方已做租户校验），仍支持可选 `tenant_id` 作为双保险。
日志表 `evolution_run_logs` 自身没有 `tenant_id`，租户过滤经由父表 join。
"""
from __future__ import annotations

import datetime
import json
import logging
import uuid

from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import IntegrityError

from jonex_core.common.exceptions import (
    ResourceConflictError,
    ResourceNotFoundError,
)

from ...models.evolution import EvolutionRun, EvolutionRunLog

logger = logging.getLogger(__name__)

# 与 024 迁移的 ck_evo_run_status 逐字一致（016 的 9 个 + adopting / smoke-failed）
ACTIVE_STATUSES = {"queued", "running"}
STATUSES = ACTIVE_STATUSES | {"evaluated", "adopting", "adopted", "rejected",
                              "rolled-back", "smoke-failed", "failed",
                              "dry-run", "empty-draft"}


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _kind_predicate(column, kind: str):
    """同链判定按 **scope** 家族取 kind（compiled = query_rule + relation + constraint）。

    R1 之后 `relation` 与 `query_rule` 共用同一条 compiled 链：只按单个 kind
    过滤会让两条 run 同时看到「链上没人」，并发写同一条链。单 kind 的 scope
    （abstract）仍渲染成等值谓词，保持既有 SQL 形状。
    """
    from .locking import kinds_for_scope, scope_for_kind

    try:
        kinds = kinds_for_scope(scope_for_kind(kind))
    except ValueError:  # 未知 kind：保持旧的精确匹配（调用方本应已校验）
        kinds = (kind,)
    return column == kinds[0] if len(kinds) == 1 else column.in_(kinds)


def recover_cutoff(now: datetime.datetime, minutes: int = 30) -> datetime.datetime:
    """重启恢复截止时间：早于此的 queued/running 行视为中断。"""
    return now - datetime.timedelta(minutes=minutes)


# 「stale」新鲜度窗口（分钟）：running 且超过这么久没更新 → 疑似中断（摘要展示用，不阻塞新操作）。
ACTIVE_TTL_MINUTES = 30
#: 库可能停在候选态的 stage（旧桥接 `failed@*` 形态不在迁移 CHECK 白名单内，此处只保留合法值）。
STUCK_STAGES = {"applied", "candidate-ran"}

#: 每个 run 保留的日志行数上限；超出的旧行在写入新行时同一次提交里删掉。
LOG_MAX_LINES = 2000


def _trim_before(seq: int, keep: int = LOG_MAX_LINES) -> int:
    """`seq` 为本次插入的序号时，需要删除的边界（`seq <= 返回值` 的行删掉）。

    `keep` 行之内不删（返回 0）；超出后返回 `seq - keep`，与 `log()` 的
    DELETE 条件共用同一口径，避免边界两处各算一遍。
    """
    return max(0, int(seq) - int(keep))


class _PendingRollbackInvalid(RuntimeError):
    """pending 回滚的确定性非法状态：清标记、保持 adopted、不再重试。"""


def _gate_summary(gate) -> dict | None:
    """列表页专用：投影成前端三字段 decision/delta/passes（与旧桥接 scan_runs 一致）。

    详情页不走这里——`get` 返回原始完整 gate（`by_tier` / `new_red` / `reasons` /
    `targets` / `probe` / `jitter` 等字段投影会丢）。无 gate 时 None。
    """
    if not isinstance(gate, dict) or not gate:
        return None
    return {"decision": gate.get("decision"),
            "delta": gate.get("delta"),
            "passes": gate.get("passes")}


def _is_stale(row, now: datetime.datetime) -> bool:
    """running 且 `updated_at` 早于 `now - ACTIVE_TTL_MINUTES` → 疑似中断；非 running 恒 False。"""
    if row.status != "running" or row.updated_at is None:
        return False
    ts = row.updated_at
    if ts.tzinfo is None:  # 防御：timestamptz 正常返回 aware，naive 值不该让比较炸掉
        ts = ts.replace(tzinfo=datetime.timezone.utc)
    return ts < now - datetime.timedelta(minutes=ACTIVE_TTL_MINUTES)


def _run_derived(row, now: datetime.datetime) -> dict:
    """列表摘要 / 详情共用的派生字段（对齐旧桥接 scan_runs 的字段契约）。

    gate 不在此处理：列表走三字段投影、详情要原始完整 gate，两处口径不同。
    """
    patch = row.patch if isinstance(row.patch, dict) else None
    return {
        "stuck": row.stage in STUCK_STAGES,
        "stale": _is_stale(row, now),
        "error": row.error or "",
        "entries_count": len(patch.get("entries") or []) if patch else None,
        "target_cases": (patch or {}).get("target_cases"),
    }


#: 信任闭环（迁移 024/025）新增列的详情投影：键名与列名一致。
#: 取值缺失时按列类型回落（datetime→None、smallint→0、jsonb→None），
#: 这样老夹具 / 尚未迁移的行也能安全读出。
_NEW_DETAIL_COLUMNS = (
    "parent_version_id", "gate_decision", "gate_adhoc", "gate_effective",
    "adopted_version_id", "adopted_by", "adopted_at", "adopt_request_id",
    "gate_override", "override_by", "override_reason", "smoke_status",
    "smoke_mode", "smoke", "rolled_back_at", "rolled_back_by",
    "rollback_reason", "rollback_target_version_id",
    "rollback_from_version_id", "rollback_started_at",
    "legacy_adopt",
    # 迁移 026：完整语料基准（异步跑分母题）
    "bench", "bench_status", "bench_mode", "bench_started_at",
    "bench_ended_at",
    # 迁移 031：本次运行记录的语料版本 / 全链快照 hash（Task 4）
    "corpus_version_id", "corpus_hash",
)
_NEW_DATETIME_COLUMNS = frozenset({
    "adopted_at", "rolled_back_at", "rollback_started_at",
    "bench_started_at", "bench_ended_at"})
_NEW_INT_COLUMNS = frozenset({"gate_override", "legacy_adopt"})


def _new_detail_fields(row) -> dict:
    """迁移 024/025 新列（Task 1 / A3）的详情投影；列表摘要不暴露。"""
    out: dict = {}
    for name in _NEW_DETAIL_COLUMNS:
        value = getattr(row, name, None)
        if name in _NEW_DATETIME_COLUMNS:
            out[name] = value.isoformat() if value is not None else None
        elif name in _NEW_INT_COLUMNS:
            out[name] = int(value or 0)
        else:
            out[name] = value
    return out


def _run_dict(row) -> dict:
    d = {
        "run_id": row.id,
        "tenant_id": row.tenant_id,
        "status": row.status,
        "stage": row.stage,
        "kind": row.kind,
        "kb": row.knowledge_base_id,
        "ds": row.data_source_id,
        "request": row.request,
        "parent": row.parent,
        "report": row.report,
        # 详情面返回原始完整 gate（by_tier/new_red/reasons/targets/probe/jitter 等）
        "gate": row.gate or None,
        "patch": row.patch,
        "backup": row.backup,
        "request_id": row.request_id,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
    d.update(_new_detail_fields(row))
    d.update(_run_derived(row, _now()))
    return d


def _run_summary(row) -> dict:
    """列表页摘要（沿用旧桥接 scan_runs 的字段名，前端零改动）。

    gate 只做三字段投影（完整 gate 走 `get` 详情面）。
    """
    d = {
        "run_id": row.id,
        "kind": row.kind,
        "kb": row.knowledge_base_id,
        "ds": row.data_source_id,
        "stage": row.stage,
        "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "gate": _gate_summary(row.gate),
        # 迁移 026：列表只暴露基准状态，明细走 `get` 详情面
        "bench_status": getattr(row, "bench_status", None),
    }
    d.update(_run_derived(row, _now()))
    return d


class RunStore:
    def __init__(self, session) -> None:
        self.session = session

    # ------------------------------------------------------------ 创建
    async def create(self, tenant_id: str, kb_id: str, ds_id: str | None,
                     kind: str, request: dict, request_id: str | None = None,
                     created_by: str | None = None,
                     parent_version_id: str | None = None) -> dict:
        """插入 `queued` 行。

        数据库侧两道唯一索引：`(tenant_id, request_id)` 幂等键与
        `(tenant_id) WHERE status IN ('queued','running')` 活跃唯一。两者冲突
        都是 409；**只有 IntegrityError** 走这条映射，其它异常原样上抛。

        `parent_version_id` 由调用方在 create 前解析（该版本链当前 active）；
        链上还没有 active 时为 None。采纳的 stale 守卫依赖它，不能留到采纳时补。
        """
        row = EvolutionRun(
            id=f"run_{uuid.uuid4().hex[:12]}",
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            data_source_id=ds_id,
            kind=kind,
            status="queued",
            stage="created",
            request=request or {},
            request_id=request_id,
            created_by=created_by,
            parent_version_id=parent_version_id,
        )
        self.session.add(row)
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise ResourceConflictError(
                message="已有演化运行在跑（或重复的 request_id）") from exc
        return {"run_id": row.id}

    # ------------------------------------------------------------ 抢占
    async def claim(self, tenant_id: str, run_id: str) -> bool:
        """按 id 抢占：只有仍为 `queued` 才置 `running`（多副本安全）。"""
        res = await self.session.execute(
            update(EvolutionRun).where(
                EvolutionRun.tenant_id == tenant_id,
                EvolutionRun.id == run_id,
                EvolutionRun.status == "queued",
            ).values(status="running", updated_at=_now()))
        await self.session.commit()
        return bool(res.rowcount)

    async def claim_next(self, tenant_id: str) -> dict | None:
        """抢占该租户下一条 `queued`（活跃唯一索引保证至多一条）。"""
        stmt = (
            update(EvolutionRun)
            .where(EvolutionRun.tenant_id == tenant_id,
                   EvolutionRun.status == "queued")
            .values(status="running", updated_at=_now())
            .returning(EvolutionRun.id)
        )
        row = (await self.session.execute(stmt)).first()
        await self.session.commit()
        return {"run_id": row[0]} if row else None

    # ------------------------------------------------------------ 读
    async def get(self, tenant_id: str, run_id: str) -> dict:
        row = (await self.session.execute(
            select(EvolutionRun).where(
                EvolutionRun.tenant_id == tenant_id,
                EvolutionRun.id == run_id,
            ))).scalars().first()
        if row is None:
            raise ResourceNotFoundError(message=f"找不到 run: {run_id}")
        return _run_dict(row)

    async def has_active(self, tenant_id: str) -> str | None:
        """返回该租户当前活跃（queued/running）运行的 id；没有则 None。

        人工写操作（采纳/回滚）与后台运行动共享同一份 schema 载体：活跃运行
        正在「应用 → 评测候选 → 回滚」的窗口里改同一行，人审写入插进去会让
        两边的乐观锁互相打架、甚至把候选态当成最终态回滚掉。因此写操作先问
        这一句，有活跃运行就 409 让用户稍后再试。

        `(tenant_id) WHERE status IN ('queued','running')` 的部分唯一索引保证
        至多一行，这里仍 `limit(1)` 只是防御性写法。
        """
        stmt = (select(EvolutionRun.id)
                .where(EvolutionRun.tenant_id == tenant_id,
                       EvolutionRun.status.in_(tuple(ACTIVE_STATUSES)))
                .limit(1))
        return (await self.session.execute(stmt)).scalars().first()

    async def other_adopting(self, tenant_id: str, run_id: str, kb: str,
                             ds: str | None, kind: str) -> str | None:
        """同一条版本链上**其它** `adopting` run 的 id；没有返回 None。

        链 = `(tenant, kb, scope, ds)`，scope 由 kind 推导；排除自己。调用方
        （adopt）已先跑 `recover_stale` 兜住 30 分钟前的僵尸 `adopting`，这里
        只负责在链锁内做「同链串行」的最后一道闸。
        """
        stmt = select(EvolutionRun.id).where(
            EvolutionRun.tenant_id == tenant_id,
            EvolutionRun.knowledge_base_id == kb,
            _kind_predicate(EvolutionRun.kind, kind),
            EvolutionRun.status == "adopting",
            EvolutionRun.id != run_id,
        )
        if ds is None:
            stmt = stmt.where(EvolutionRun.data_source_id.is_(None))
        else:
            stmt = stmt.where(EvolutionRun.data_source_id == ds)
        return (await self.session.execute(stmt.limit(1))).scalars().first()

    async def chain_adopting(self, tenant_id: str, kb: str, kind: str,
                             ds: str | None,
                             exclude_run_id: str | None = None
                             ) -> str | None:
        """同一条版本链上**任意** `adopting` run 的 id；没有返回 None。

        链 = `(tenant, kb, scope, ds)`，scope 由 kind 推导（对齐
        `other_adopting` 的谓词）。给「快速 409」用：发起新运行 / 回滚在取
        链锁之前先问这一句，避免在一个可能跑几十分钟的完整语料基准期间白等
        链锁，也避免 create 落到租户级活跃唯一索引上才发现冲突。

        `exclude_run_id`：回滚自己（run 本身可能就是 `adopting`）时排除自己，
        让状态机给出准确的状态错误，而不是「另一个采纳（就是它自己）」。
        """
        stmt = select(EvolutionRun.id).where(
            EvolutionRun.tenant_id == tenant_id,
            EvolutionRun.knowledge_base_id == kb,
            _kind_predicate(EvolutionRun.kind, kind),
            EvolutionRun.status == "adopting",
        )
        if exclude_run_id is not None:
            stmt = stmt.where(EvolutionRun.id != exclude_run_id)
        if ds is None:
            stmt = stmt.where(EvolutionRun.data_source_id.is_(None))
        else:
            stmt = stmt.where(EvolutionRun.data_source_id == ds)
        return (await self.session.execute(stmt.limit(1))).scalars().first()

    async def get_by_request_id(self, tenant_id: str,
                                request_id: str) -> dict | None:
        """按幂等键 `(tenant_id, request_id)` 取既有运行；没有返回 None。

        只给 `evolution_start_run` 用：命中即复用（重试回执丢失的场景）。
        迁移 016 的 `uq_evo_run_request` 保证同租户下该键至多一行。
        """
        stmt = select(EvolutionRun).where(
            EvolutionRun.tenant_id == tenant_id,
            EvolutionRun.request_id == request_id,
        )
        row = (await self.session.execute(stmt)).scalars().first()
        return _run_dict(row) if row is not None else None

    async def list(self, tenant_id: str, kb_id: str | None = None,
                   limit: int = 50, kb_ids: list[str] | None = None
                   ) -> list[dict]:
        """列表摘要。

        `kb_ids` 是**授权集合**（`None` = 不过滤，租户管理员路径；`[]` = 一个
        KB 都管不了 → 直接空页，不发查询）。非 None 时 SQL 追加
        `AND knowledge_base_id IN (...)`，与显式 `kb_id` 是 AND 关系 —— 即使
        调用方传了授权外的 kb，结果也是空集，不会放开。`None` 时语句与今天
        逐字节一致。
        """
        if kb_ids is not None and not kb_ids:
            return []
        stmt = select(EvolutionRun).where(EvolutionRun.tenant_id == tenant_id)
        if kb_id:
            stmt = stmt.where(EvolutionRun.knowledge_base_id == kb_id)
        if kb_ids is not None:
            stmt = stmt.where(EvolutionRun.knowledge_base_id.in_(list(kb_ids)))
        stmt = (stmt.order_by(EvolutionRun.created_at.desc(),
                              EvolutionRun.id.desc()).limit(limit))
        rows = (await self.session.execute(stmt)).scalars().all()
        return [_run_summary(row) for row in rows]

    # ------------------------------------------------------------ 更新
    async def update(self, run_id: str, *, status: str | None = None,
                     stage: str | None = None, tenant_id: str | None = None,
                     expect_statuses: set[str] | None = None,
                     **fields) -> bool:
        """按 `run_id` 更新产物/状态；返回是否命中（`rowcount > 0`）。

        `expect_statuses` 是**状态 CAS**：非空时 SQL 追加
        `AND status IN (...)` —— 只有仍处于这些状态的行才会被改写。典型用法
        `expect_statuses={"running"}`：`recover_stale` 已把超时运行置 `failed`
        后，存活线程的迟到写入会命中 0 行、**一个字段都不写**，返回 False。
        调用方据此判断「运行已被回收/结束」，而不是把终态覆盖回去。

        `tenant_id` 可选：调用方通常已按租户取过 run，带上可加一道双保险。
        `stage` 不在这里做白名单校验（迁移里已有 CHECK）；`status` 先按
        `STATUSES` 拦截，避免脏值打到数据库才报 500。
        """
        if status is not None and status not in STATUSES:
            raise ValueError(f"非法状态: {status}")
        values = dict(fields)
        if status is not None:
            values["status"] = status
        if stage is not None:
            values["stage"] = stage
        values["updated_at"] = _now()

        expect = tuple(expect_statuses) if expect_statuses else ()
        stmt = update(EvolutionRun).where(EvolutionRun.id == run_id)
        if tenant_id is not None:
            stmt = stmt.where(EvolutionRun.tenant_id == tenant_id)
        if expect:
            stmt = stmt.where(EvolutionRun.status.in_(expect))
        res = await self.session.execute(stmt.values(**values))
        try:
            await self.session.commit()
        except Exception:
            # commit 失败（约束冲突 / 连接断）后会话仍处于失败事务里：不先
            # rollback，后续这条会话上的任何语句都会连锁报错。update 是共享
            # 写路径（阶段推进 / 心跳 / 收口共用同一会话），一次失败绝不能
            # 升级成「整个运行会话中毒」。回滚后原样上抛，与 `log` 同口径。
            await self.session.rollback()
            raise
        return bool(res.rowcount)

    # ------------------------------------------------------------ 恢复
    async def recover_stale(self, tenant_id: str, minutes: int = 30,
                            now: datetime.datetime | None = None) -> int:
        """进程重启恢复：把超过 `minutes` 分钟没更新的活跃行收口。

        - `queued` / `running`：批量标 `failed`（服务重启中断）。孤立的 `queued`
          （create 之后、claim 之前进程就没了）一并兜住：活跃部分唯一索引按状态
          集合计，漏恢复会永久占住活跃槽、该租户再也建不出新运行。
        - `adopting`：逐行恢复（spec §5 / 控制器 S6）——先读 `run.smoke`：
          smoke 已 `passed` 则尝试完成激活（`adopted_version_id` 或该 run 的
          candidate 版本）；否则按该 run 的 candidate 版本恢复快照并置 `failed`。
          恢复/激活失败同样置 `failed` 并保留 error，绝不让 `adopting` 永久卡死。
        """
        now = now or _now()
        cutoff = recover_cutoff(now, minutes)
        res = await self.session.execute(
            update(EvolutionRun).where(
                EvolutionRun.tenant_id == tenant_id,
                EvolutionRun.status.in_(("running", "queued")),
                EvolutionRun.updated_at < cutoff,
            ).values(status="failed", error="服务重启中断", updated_at=now))
        await self.session.commit()
        recovered = res.rowcount or 0
        recovered += await self._recover_adopting(tenant_id, cutoff, now)
        recovered += await self._recover_pending_rollbacks(tenant_id, cutoff, now)
        return recovered

    async def _recover_adopting(self, tenant_id: str,
                                cutoff: datetime.datetime,
                                now: datetime.datetime) -> int:
        """回收超时的 `adopting` 行（逐行，因为每行都要恢复快照/推进版本）。"""
        rows = (await self.session.execute(
            select(EvolutionRun).where(
                EvolutionRun.tenant_id == tenant_id,
                EvolutionRun.status == "adopting",
                EvolutionRun.updated_at < cutoff,
            ))).scalars().all()
        count = 0
        for row in rows:
            # C1-4：只有 run 真正收口（update CAS 命中）才计数；CAS 未命中说明
            # 该行已被其它路径收口，不能把它算成「本次恢复了一行」。
            closed = False
            try:
                closed = await self._recover_one_adopting(tenant_id, row, now)
            except Exception as exc:  # noqa: BLE001 —— 单行失败不能拖垮其余
                logger.warning("adopting 恢复失败 run=%s: %s", row.id, exc)
                try:
                    closed = await self.update(
                        row.id, tenant_id=tenant_id, status="failed",
                        error=f"adopting 恢复失败：{str(exc)[:200]}",
                        expect_statuses={"adopting"})
                except Exception:  # noqa: BLE001 —— 收口失败只记日志
                    logger.warning("adopting 恢复失败后收口未命中 run=%s",
                                   row.id)
            if closed:
                count += 1
        return count

    async def _recover_one_adopting(self, tenant_id: str, row,
                                    now: datetime.datetime) -> bool:
        """单行 `adopting` 恢复：**尝试持同一条链的 advisory lock**（I2）。

        用 `try_chain_lock` 而非阻塞的 `chain_lock`：恢复是「下次再收」的
        后台扫尾，链上正有采纳在跑时不该把重启恢复流程卡住。未拿到锁返回
        False —— 该行仍是 `adopting`，下一次 `recover_stale` 再试。

        返回 run 是否真正收口（`update` CAS 命中）。
        """
        from .locking import advisory_key, scope_for_kind, try_chain_lock

        scope = scope_for_kind(row.kind)
        async with try_chain_lock(advisory_key(
                tenant_id, row.knowledge_base_id, scope,
                row.data_source_id)) as acquired:
            if not acquired:
                return False
            return await self._recover_one_adopting_locked(
                tenant_id, row, now, scope)

    async def _recover_one_adopting_locked(self, tenant_id: str, row,
                                           now: datetime.datetime,
                                           scope: str) -> bool:
        """持链锁后的单行恢复（延迟 import：避免 run_store ↔ schema_io 循环）。

        Task 8：在 P0 收口逻辑外补上**完整语料基准**事实（`bench_status` /
        `bench`）的恢复矩阵：

        - `bench_status is None`（迁移前的旧 run）：**逐字保持 P0 行为**；
        - `parent_ran` / `pending`：候选臂从未判定 → 恢复 pre 并回
          `evaluated`（smoke 通过/降级）；smoke 未通过走既有 restore → `failed`；
        - `skipped`：只有 `bench.override is True` 且 smoke 通过/降级才允许
          激活；否则「有 apply 证据则恢复」后回 `evaluated`；
        - `passed` / `warn`：smoke 通过/降级 → 激活并 `adopted`；否则 `failed`；
        - `failed`：判定失败（`new_red` / `new_error` / `passes_dropped`）→
          恢复 pre + 驳回候选版本 + `smoke-failed`；执行器/陈旧/空语料 →
          恢复 pre 回 `evaluated`（没有 apply 证据就只置 `evaluated`）。

        `bench_status` / `bench` 两列**不清空**：UI 复盘要看到失败事实。
        恢复失败一律升级 `failed`（既有约定）；链已被别的采纳推进时绝不
        restore / activate（`chain_is_ours` 守卫）。
        """
        from .bench import (
            REASON_CORPUS_EMPTY,
            REASON_EXECUTOR_ERROR,
            REASON_NEW_ERROR,
            REASON_NEW_RED,
            REASON_PASSES_DROPPED,
        )
        # `adopt_common` 在模块级 import 本模块：反向必须延迟到运行期，否则成环。
        from .adopt_common import pruned_backup
        from .schema_io import rollback_patch, restore_snapshot, restore_source
        from .version_store import VersionStore

        versions = VersionStore(self.session)
        smoke = row.smoke if isinstance(row.smoke, dict) else {}
        smoke_status = row.smoke_status or smoke.get("status")
        bench = getattr(row, "bench", None)
        bench = bench if isinstance(bench, dict) else {}
        bench_status = getattr(row, "bench_status", None)
        bench_reasons = bench.get("reasons")
        if not isinstance(bench_reasons, list):
            bench_reasons = []
        smoke_ok = smoke_status in ("passed", "warn")
        version = await self._candidate_version_for(
            versions, tenant_id, row, scope)
        active = await versions.active_for(
            tenant_id, row.knowledge_base_id, scope, row.data_source_id)
        active_id = (active or {}).get("version_id")
        # 「activate 成功、收口 CAS 前崩溃」窗口：版本已是 active 且
        # `source_run_id` 指向本 run，`adopted_version_id` 还没写。此时必须
        # 收口成 adopted（而不是把自家 active 版本误判成别人的）。
        owned_active = bool(active and active.get("source_run_id") == row.id)
        target = (row.adopted_version_id
                  or (version or {}).get("version_id")
                  or (active_id if owned_active else None))
        parent_id = (version or {}).get("parent_version_id") \
            or row.parent_version_id
        # I1：只有该版本仍是 candidate、且链头是本 run 的父版本（或已是目标
        # 版本本身）时才允许推进/回退；链头被别的采纳推进过 → 不复活旧版本，
        # 也不拿旧 backup 覆盖新内容。
        chain_is_ours = (active_id is None or active_id == parent_id
                         or active_id == target)

        async def _activate_and_adopt(*, gate_override: bool = False) -> bool:
            """P0 的激活收口：链头是本 run 的父版本才推进。

            `gate_override=True` 只给 `skipped + bench.override` 的恢复用：
            与正常 execute 路径同口径写 `gate_override=1`（override_by /
            override_reason 在受理/执行时已落库），UI 才能看出是人工放行。
            该事实**只在 adopted 收口写**；激活没完成的 failed fallback 不写
            （否则 UI 会把「未激活」误渲染成「已 override 采纳」）。
            """
            extra = {"gate_override": 1} if gate_override else {}
            if (chain_is_ours and target and version is not None
                    and version.get("status") == "candidate"):
                await versions.activate(
                    tenant_id, target, previous_id=active_id)
                return await self.update(
                    row.id, tenant_id=tenant_id, status="adopted",
                    adopted_version_id=target, adopted_at=now,
                    # 与正常 execute 路径同口径：收口成 adopted 才剪枝，
                    # 此后再没有恢复到 pre 的需求。
                    backup=pruned_backup(row.backup),
                    expect_statuses={"adopting"}, **extra)
            return await self.update(
                row.id, tenant_id=tenant_id, status="failed",
                error="smoke 已通过但候选版本已被其它采纳推进/取代，未完成激活",
                # override 事实只属于**真正采纳**的收口：激活没完成时写
                # `gate_override=1` 会让 UI 把 failed 行误显示成「已 override
                # 采纳」。failed fallback 只写状态/错误/期望状态。
                expect_statuses={"adopting"})

        async def _rollback_synonym_ledger() -> None:
            """synonym：只逆操作 `row.patch.rollback.added`（同 `_restore_after_failure`）。

            同义词行是共享表，按 pre 快照整表 reconcile 会软删采纳期间的人工新增组。
            没有 added = apply 未提交任何写，什么都不用做。
            """
            patch = row.patch if isinstance(row.patch, dict) else {}
            added = ((patch.get("rollback") or {}).get("added") or [])
            if added:
                await rollback_patch(tenant_id, patch)

        async def _restore_and_fail() -> bool:
            """既有 P0 失败路径：驳回候选版本 → 恢复 pre → `failed`。"""
            if version is not None:
                try:
                    await versions.reject_version(
                        tenant_id, version["version_id"])
                except Exception as exc:  # noqa: BLE001 —— 版本收口是旁路
                    logger.warning("adopting 恢复驳回版本失败 run=%s: %s",
                                   row.id, exc)
            error = "服务重启中断（adopting 未完成，已恢复采纳前快照）"
            if not chain_is_ours:
                error = ("服务重启中断（adopting 未完成）；"
                         "链已被其它采纳推进，未恢复快照")
            elif scope == "synonym":
                # synonym 只逆操作本批台账；整表 reconcile 会误删人工组。
                try:
                    await _rollback_synonym_ledger()
                except Exception as exc:  # noqa: BLE001 —— 恢复失败要显式收口
                    error = ("服务重启中断（adopting 未完成）；"
                             f"恢复采纳前内容失败：{str(exc)[:200]}")
            else:
                # C1：恢复源是**采纳前**内容，优先 run.backup
                # （pre 快照不落版本表）。
                source = restore_source(
                    None, row.backup, scope, row.knowledge_base_id,
                    row.data_source_id)
                if source is None:
                    error = ("服务重启中断（adopting 未完成）；"
                             "找不到采纳前快照（backup 为空），未恢复")
                else:
                    try:
                        await restore_snapshot(tenant_id, source)
                    except Exception as exc:  # noqa: BLE001 —— 恢复失败要显式收口
                        error = ("服务重启中断（adopting 未完成）；"
                                 f"恢复采纳前快照失败：{str(exc)[:200]}")
            return await self.update(row.id, tenant_id=tenant_id,
                                     status="failed", error=error,
                                     expect_statuses={"adopting"})

        def _pre_source() -> dict | None:
            """采纳前恢复源：**只认 `run.backup`**（pre 快照不落版本表）。

            候选版本是 apply 之后的 post-state（Task 6），只能证明「确实写过
            库」，**绝不能当恢复内容** —— 拿候选快照恢复等于把未获批的补丁
            留在线上，还对外声称「已恢复采纳前快照」。
            """
            return restore_source(
                None, row.backup, scope, row.knowledge_base_id,
                row.data_source_id)

        async def _log_recovery_note(payload: dict) -> None:
            """恢复旁路日志（失败只 warning，绝不影响收口）。"""
            try:
                await self.log(
                    tenant_id, row.id,
                    json.dumps(payload, ensure_ascii=False))
            except Exception as exc:  # noqa: BLE001 —— 日志是旁路
                logger.warning("adopting 恢复日志写入失败 run=%s: %s",
                               row.id, exc)

        async def _restore_then(status: str, error: str | None = None) -> bool:
            """恢复采纳前内容后按 `status` 收口。

            - 没有 pre 源（`run.backup` 为空）→ 不恢复，error 明确写「未恢复」，
              **不得声称已恢复**；
            - 链已被别的采纳推进 → **绝不恢复**，并写 run log 说明是有意跳过；
            - 恢复异常 → `failed`。

            `status="evaluated"` 时不强行给正常恢复加 error 文本；失败/半失败
            路径（`failed` / `smoke-failed`）会把结果追加到既有 error 上。
            """
            def _with(note: str, *, quiet: bool = False) -> str | None:
                if error:
                    return f"{error}；{note}"
                # evaluated 无既有错误：成功恢复不加噪音；未恢复必须明说。
                return None if quiet else note

            # 回到「待采纳」= 本次尝试没有采纳：采纳/override 事实必须清掉
            # （与 `_release_evaluated` / `_restore_after_failure` 同口径），
            # 否则重试时 run 会残留上一次的 adopted_by / override_by /
            # override_reason / gate_override=1，前端误渲染成「已 override
            # 采纳」。
            clear_override = ({"adopted_by": None,
                               "override_by": None, "override_reason": None,
                               "gate_override": 0}
                              if status == "evaluated" else {})

            if not chain_is_ours:
                await _log_recovery_note({
                    "stage": "adopting_recovered",
                    "reason": "chain_advanced_no_restore",
                    "status": status,
                    "note": "链已被其它采纳推进，跳过恢复（有意不覆盖新 active）",
                })
                return await self.update(
                    row.id, tenant_id=tenant_id, status=status,
                    error=_with("链已被其它采纳推进，未恢复快照"),
                    expect_statuses={"adopting"}, **clear_override)
            if scope == "synonym":
                # synonym 只逆操作本批台账，不按 pre 快照整表 reconcile。
                try:
                    await _rollback_synonym_ledger()
                except Exception as exc:  # noqa: BLE001 —— 恢复失败要显式收口
                    return await self.update(
                        row.id, tenant_id=tenant_id, status="failed",
                        error=_with(f"恢复采纳前内容失败：{str(exc)[:200]}"),
                        expect_statuses={"adopting"})
                return await self.update(
                    row.id, tenant_id=tenant_id, status=status,
                    error=_with("已恢复采纳前内容", quiet=True),
                    expect_statuses={"adopting"}, **clear_override)
            source = _pre_source()
            if source is None:
                return await self.update(
                    row.id, tenant_id=tenant_id, status=status,
                    error=_with("找不到采纳前快照（backup 为空），未恢复"),
                    expect_statuses={"adopting"}, **clear_override)
            try:
                await restore_snapshot(tenant_id, source)
            except Exception as exc:  # noqa: BLE001 —— 恢复失败要显式收口
                return await self.update(
                    row.id, tenant_id=tenant_id, status="failed",
                    error=_with(f"恢复采纳前快照失败：{str(exc)[:200]}"),
                    expect_statuses={"adopting"})
            return await self.update(
                row.id, tenant_id=tenant_id, status=status,
                error=_with("已恢复采纳前快照", quiet=True),
                expect_statuses={"adopting"}, **clear_override)

        # 自家 active 版本 = 激活已提交，只需补 run 收口（不需要再 activate）。
        if target and active_id == target:
            return await self.update(
                row.id, tenant_id=tenant_id, status="adopted",
                adopted_version_id=target, adopted_at=now,
                # 这条同样是真正的 adopted 收口（窗口 = 激活已提交、run 收口
                # 前崩溃）：必须与 `_activate_and_adopt` 同口径剪枝，否则这个
                # 窗口里的 run 会把完整快照永久留下。
                backup=pruned_backup(row.backup),
                expect_statuses={"adopting"})

        # ---- 迁移前旧 run（bench_status 为空）：逐字保持 P0 行为 ----
        if bench_status is None:
            if smoke_status == "passed":
                return await _activate_and_adopt()
            return await _restore_and_fail()

        # ---- parent_ran / pending：候选臂从未判定 ----
        if bench_status in ("parent_ran", "pending"):
            if smoke_ok:
                return await _restore_then("evaluated")
            return await _restore_and_fail()

        # ---- skipped：只有人工 override + smoke 通过/降级才允许激活 ----
        if bench_status == "skipped":
            if bench.get("override") is True and smoke_ok:
                return await _activate_and_adopt(gate_override=True)
            return await _restore_then("evaluated")

        # ---- passed / warn：判定通过 ----
        if bench_status in ("passed", "warn"):
            if smoke_ok:
                return await _activate_and_adopt()
            return await _restore_and_fail()

        # ---- failed：判定失败 / 执行器错误 ----
        if bench_status == "failed":
            reasons = [str(item) for item in bench_reasons]
            if any(item in (REASON_NEW_RED, REASON_NEW_ERROR,
                            REASON_PASSES_DROPPED) for item in reasons):
                if version is not None:
                    try:
                        await versions.reject_version(
                            tenant_id, version["version_id"])
                    except Exception as exc:  # noqa: BLE001 —— 旁路
                        logger.warning(
                            "adopting 恢复驳回版本失败 run=%s: %s",
                            row.id, exc)
                detail = "、".join(reasons) or "判定失败"
                # 不在这里声称「已恢复」：恢复结果由 `_restore_then` 按实际
                # 情况追加（成功 / 找不到 pre 源 / 恢复失败）。
                return await _restore_then(
                    "smoke-failed",
                    "服务重启中断（adopting 未完成）；"
                    f"基准判定失败（{detail}）")
            if any(item in (REASON_EXECUTOR_ERROR, "stale",
                            REASON_CORPUS_EMPTY) for item in reasons):
                # 与正常执行路径同口径：回到 evaluated 也意味着本次采纳作废，
                # 候选版本必须 rejected（否则会留下一个永远不评估的 candidate，
                # 还可能被后续回滚当成可复活的目标）。
                if version is not None:
                    try:
                        await versions.reject_version(
                            tenant_id, version["version_id"])
                    except Exception as exc:  # noqa: BLE001 —— 旁路
                        logger.warning(
                            "adopting 恢复驳回版本失败 run=%s: %s",
                            row.id, exc)
                return await _restore_then("evaluated")
            # 未知原因码：保守按执行器错误处理（恢复 → evaluated）。
            return await _restore_then("evaluated")

        # 未识别的 bench_status（未来新增枚举）：按 P0 行为兜底，绝不卡死。
        if smoke_status == "passed":
            return await _activate_and_adopt()
        return await _restore_and_fail()

    async def _candidate_version_for(self, versions, tenant_id: str, row,
                                     scope: str) -> dict | None:
        """该 run 落在这条链上的 **candidate** 版本（I1：superseded 不算）。

        C1-3：链很长（>keep）时本 run 的 candidate 可能已被搬进归档表，只查
        主表会让恢复误判「没有候选」而直接 failed。这里复用 `list` 的
        `include_archived=True`，在 Python 侧按 `source_run_id` + `status ==
        "candidate"` 过滤；**仍只取 candidate**，归档里的 superseded/active
        不得被复活。
        """
        page = await versions.list(
            tenant_id, row.knowledge_base_id, scope, row.data_source_id,
            include_archived=True, limit=200)
        matched = [item for item in page.get("items") or []
                   if isinstance(item, dict)
                   and item.get("source_run_id") == row.id
                   and item.get("status") == "candidate"]
        if not matched:
            return None
        # list 按 created_at DESC 排序，取最新一条；详情需带 include_archived，
        # 否则候选已在归档表时会抛 1002。
        return await versions.get(tenant_id, matched[0]["version_id"],
                                  include_archived=True)

    # ------------------------------------------------------------ pending 回滚恢复
    async def _recover_pending_rollbacks(self, tenant_id: str,
                                         cutoff: datetime.datetime,
                                         now: datetime.datetime) -> int:
        """回收超过 `minutes` 的 pending 回滚（A3）。

        判据：`status='adopted' AND rollback_target_version_id IS NOT NULL
        AND rollback_started_at < cutoff`。这类行来自「restore 已开始 / 版本
        未建 / run 未收口」的崩溃窗口 —— 不回滚就会永久卡在 adopted。

        逐行**尝试**持**同一条链**的 advisory lock（与 adopt/rollback 一致，
        但用 `pg_try_advisory_lock`）：链上有采纳在跑时跳过该行、下次再收，
        绝不让同步的 `recover_stale` 调用被长基准挂住。目标非法/不存在时清
        pending 标记并保持 adopted；瞬态失败保留标记，下一次重试（绝不让
        run 因恢复逻辑本身卡死）。
        """
        rows = (await self.session.execute(
            select(EvolutionRun).where(
                EvolutionRun.tenant_id == tenant_id,
                EvolutionRun.status == "adopted",
                EvolutionRun.rollback_target_version_id.is_not(None),
                EvolutionRun.rollback_started_at < cutoff,
            ))).scalars().all()
        count = 0
        for row in rows:
            # 与 C1-4 同口径：只有收口 CAS 真正命中才计数；瞬态失败/已被
            # 其它路径收口（CAS 未命中）都不算本次恢复了一行。
            closed = False
            try:
                closed = await self._recover_one_pending_rollback(
                    tenant_id, row, now)
            except Exception as exc:  # noqa: BLE001 —— 单行失败不能拖垮其余
                # 瞬态失败（DB / 乐观锁 / 图投影）保留 pending 标记，下一次
                # recover_stale 继续重试；只有「目标非法/不存在」才清标记。
                logger.warning("pending 回滚恢复失败 run=%s: %s", row.id, exc)
            if closed:
                count += 1
        return count

    async def _recover_one_pending_rollback(self, tenant_id: str, row,
                                            now: datetime.datetime) -> bool:
        """单行 pending 回滚恢复：**尝试持同一条链的 advisory lock**。

        用 `try_chain_lock` 而非阻塞的 `chain_lock`：恢复是「下次再收」的
        后台扫尾，链上正有采纳在跑（完整语料基准可能几十分钟）时不能把
        同步的 `recover_stale` 调用（accept / start_run / rollback 都会触发）
        挂到 sidecar 超时。未拿到锁返回 False —— 该行仍是 pending 回滚，
        下一次 `recover_stale` 再试。

        返回本次是否真正收口（清标记 / 置 rolled-back 的 CAS 命中）。
        """
        from .locking import advisory_key, scope_for_kind, try_chain_lock

        scope = scope_for_kind(row.kind)
        ds = row.data_source_id if scope == "compiled" else None
        async with try_chain_lock(advisory_key(
                tenant_id, row.knowledge_base_id, scope, ds)) as acquired:
            if not acquired:
                return False
            return await self._recover_one_pending_rollback_locked(
                tenant_id, row, now, scope, ds)

    async def _recover_one_pending_rollback_locked(
            self, tenant_id: str, row, now: datetime.datetime,
            scope: str, ds: str | None) -> bool:
        """持链锁后的单行恢复（延迟 import：避免循环依赖）。

        先做**链推进守卫**（C）：以回滚开始时的 active
        `rollback_from_version_id` 为基线 ——
        - 当前 active == from → 链未推进，可安全恢复/补建/激活；
        - 当前 active == 本 run 的回滚版本 或 == 目标版本 → 只需补收口；
        - 其它（含 from 非空但当前 active 为空）→ 链已推进/异常：清 pending、
          保持 `adopted`、写 run log + warning，**绝不 restore / activate / reject**。

        目标非法 / 不存在 / hash 不符 / candidate 形状不符 → 清 pending、
        保持 `adopted`、写 run log + warning；其它瞬态失败向上抛，由调用方
        保留标记待下次重试。收口成功写 run log + 审计
        `evolution.rollback`（recovered=true），旁路失败只 warning。
        """
        from .schema_io import restore_snapshot, snapshot_for
        from .version_store import VersionStore, canonical_snapshot_hash

        versions = VersionStore(self.session)
        target_id = row.rollback_target_version_id
        from_id = row.rollback_from_version_id
        try:
            target = await self._valid_rollback_target(
                versions, tenant_id, row, target_id, scope=scope, ds=ds)
            current = await versions.active_for(
                tenant_id, row.knowledge_base_id, scope, ds)
            current_id = (current or {}).get("version_id")
            rollback_version = await self._rollback_version_for(
                versions, tenant_id, row, scope, ds, target_id)
            rollback_id = (rollback_version or {}).get("version_id")

            if current_id == from_id:
                version_id = await self._finish_pending_rollback(
                    versions, tenant_id, row, target,
                    rollback_version=rollback_version, current_id=current_id,
                    scope=scope, ds=ds, snapshot_for=snapshot_for,
                    restore_snapshot=restore_snapshot,
                    canonical_snapshot_hash=canonical_snapshot_hash)
            elif current_id is not None \
                    and current_id in (rollback_id, target_id):
                # 崩溃点在 activate 之后、收口之前：只补 run 收口。
                version_id = current_id
            else:
                raise _PendingRollbackInvalid(
                    "chain_advanced" if current_id is not None
                    else "active_missing")
        except _PendingRollbackInvalid as exc:
            logger.warning("pending 回滚恢复中止 run=%s reason=%s",
                           row.id, exc)
            closed = await self.clear_rollback_pending(tenant_id, row.id)
            await self._log_rollback_recovery(
                tenant_id, row.id,
                {"stage": "rollback_recovered", "recovered": False,
                 "reason": str(exc), "target_version_id": target_id,
                 "from_version_id": from_id})
            # 防御分支：内容已恢复但候选 hash 不符 → 指针/内容可能错位，
            # 除日志外必须留下 failed 审计，便于人工核对。
            if str(exc) == "created_hash_mismatch":
                await self._record_recovered_rollback_failure_audit(
                    tenant_id, row.id, target_id, from_id, str(exc))
            return closed

        # 成功收口只清 `rollback_started_at`：保留 target/from 供重放展示真实目标。
        ok = await self.update(
            row.id, tenant_id=tenant_id, status="rolled-back",
            stage="rolled-back", rolled_back_at=now,
            rolled_back_by="recovery",
            rollback_reason="服务重启恢复：回滚中断后自动收口",
            rollback_started_at=None, expect_statuses={"adopted"})
        if not ok:
            logger.warning("pending 回滚收口 CAS 未命中 run=%s", row.id)
            return False
        await self._log_rollback_recovery(
            tenant_id, row.id,
            {"stage": "rollback_recovered", "recovered": True,
             "reason": "completed", "target_version_id": target_id,
             "from_version_id": from_id, "version_id": version_id})
        await self._record_recovered_rollback_audit(
            tenant_id, row.id, target_id, from_id, version_id)
        return True

    async def _valid_rollback_target(self, versions, tenant_id: str, row,
                                     target_id: str | None, *,
                                     scope: str, ds: str | None) -> dict:
        """读取并校验回滚目标；非法抛 `_PendingRollbackInvalid`。"""
        from .version_store import canonical_snapshot_hash

        if not target_id:
            raise _PendingRollbackInvalid("target_missing")
        try:
            target = await versions.get(tenant_id, target_id,
                                        include_archived=True)
        except ResourceNotFoundError:
            raise _PendingRollbackInvalid("target_not_found") from None
        if (target.get("kb") != row.knowledge_base_id
                or target.get("scope") != scope
                or target.get("ds") != ds):
            raise _PendingRollbackInvalid("target_cross_chain")
        if target.get("status") not in ("active", "superseded"):
            raise _PendingRollbackInvalid("target_status_invalid")
        snapshot = target.get("snapshot")
        content = (snapshot.get("content")
                   if isinstance(snapshot, dict) else None)
        if content is None or canonical_snapshot_hash(snapshot) != \
                target.get("snapshot_hash"):
            raise _PendingRollbackInvalid("target_snapshot_invalid")
        return target

    async def _finish_pending_rollback(
            self, versions, tenant_id: str, row, target: dict, *,
            rollback_version: dict | None, current_id: str | None,
            scope: str, ds: str | None, snapshot_for, restore_snapshot,
            canonical_snapshot_hash) -> str:
        """链未推进时把版本链推进到「回滚版本 active」；返回 active 版本 id。"""
        target_id = target["version_id"]
        if rollback_version is not None:
            # C4：candidate 形状必须与目标一致，否则不许激活。
            if rollback_version.get("snapshot_hash") != target["snapshot_hash"]:
                raise _PendingRollbackInvalid("candidate_hash_mismatch")
            if rollback_version.get("parent_version_id") \
                    != row.rollback_from_version_id:
                raise _PendingRollbackInvalid("candidate_parent_mismatch")
            # 有 run 行就用它自己的 kind（`relation` 与 `query_rule` 同属
            # compiled，内容形状一致；标签仍如实反映本 run）。
            current_snapshot = await snapshot_for(
                tenant_id, row.kind, row.knowledge_base_id, ds)
            if canonical_snapshot_hash(current_snapshot) \
                    != rollback_version["snapshot_hash"]:
                await restore_snapshot(tenant_id, target)
            await versions.activate(
                tenant_id, rollback_version["version_id"],
                previous_id=current_id)
            return rollback_version["version_id"]

        # 没有版本行：先判断线上内容是否已经是目标（崩溃点在 restore 之后、
        # create 之前），否则恢复目标快照。
        current_snapshot = await snapshot_for(
            tenant_id, row.kind, row.knowledge_base_id, ds)
        if canonical_snapshot_hash(current_snapshot) != target["snapshot_hash"]:
            await restore_snapshot(tenant_id, target)
            snapshot = target["snapshot"]
        else:
            snapshot = current_snapshot
        version = await versions.create_candidate(
            tenant_id, row.knowledge_base_id, scope, ds, snapshot,
            parent_version_id=current_id,
            source_run_id=row.id, source_version_id=target_id,
            created_by="recovery", note=f"rollback:{target_id}")
        if version.get("snapshot_hash") != target["snapshot_hash"]:
            # 防御分支（理论上不可达：create_candidate 用同一个 snapshot 计算
            # hash）。一旦出现，线上内容可能已恢复而版本指针未动 —— 清标记
            # 保持 adopted，并写 failed 审计供人工核对，**不**当瞬态重试。
            raise _PendingRollbackInvalid("created_hash_mismatch")
        await versions.activate(
            tenant_id, version["version_id"], previous_id=current_id)
        return version["version_id"]

    async def _rollback_version_for(self, versions, tenant_id: str, row,
                                    scope: str, ds: str | None,
                                    target_id: str) -> dict | None:
        """找本 run 为 `target_id` 建的回滚版本（candidate / active）。"""
        page = await versions.list(
            tenant_id, row.knowledge_base_id, scope, ds, limit=200)
        matched = [item for item in page.get("items") or []
                   if isinstance(item, dict)
                   and item.get("source_run_id") == row.id
                   and item.get("source_version_id") == target_id
                   and item.get("status") in ("candidate", "active")]
        if not matched:
            return None
        matched.sort(key=lambda item: item.get("status") != "active")
        return await versions.get(tenant_id, matched[0]["version_id"])

    async def clear_rollback_pending(self, tenant_id: str,
                                     run_id: str) -> bool:
        """清 pending 三个标记但**保持 adopted**（补偿/失败唯一收口点）。

        返回 CAS 是否命中（True = 标记实际被清掉）；失败只 warning、不掩盖
        原错误。adopt_service 的补偿路径也复用本方法。
        """
        try:
            return await self.update(
                run_id, tenant_id=tenant_id,
                rollback_target_version_id=None,
                rollback_from_version_id=None,
                rollback_started_at=None,
                expect_statuses={"adopted"})
        except Exception as exc:  # noqa: BLE001 —— 清理是旁路
            logger.warning("清 pending 回滚标记失败 run=%s: %s", run_id, exc)
            return False

    async def _log_rollback_recovery(self, tenant_id: str, run_id: str,
                                     payload: dict) -> None:
        """pending 回滚恢复的 run log（旁路：失败只 warning）。"""
        try:
            await self.log(tenant_id, run_id,
                           json.dumps(payload, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001 —— 日志是旁路
            logger.warning("pending 回滚恢复日志失败 run=%s: %s", run_id, exc)

    async def _record_recovered_rollback_audit(
            self, tenant_id: str, run_id: str, target_id: str,
            from_id: str | None, version_id: str) -> None:
        """恢复收口审计（旁路：失败只 warning）。"""
        try:
            from .audit import record as record_audit
            await record_audit(
                self.session, tenant_id=tenant_id,
                action="evolution.rollback", outcome="success",
                run_id=run_id,
                request_params={"recovered": True,
                                "to_version_id": target_id,
                                "from_version_id": from_id,
                                "version_id": version_id},
                user_id="recovery")
        except Exception as exc:  # noqa: BLE001 —— 审计不是关键路径
            logger.warning("pending 回滚恢复审计失败 run=%s: %s", run_id, exc)

    async def _record_recovered_rollback_failure_audit(
            self, tenant_id: str, run_id: str, target_id: str,
            from_id: str | None, reason: str) -> None:
        """防御分支的失败审计（旁路：失败只 warning）。"""
        try:
            from .audit import record as record_audit
            await record_audit(
                self.session, tenant_id=tenant_id,
                action="evolution.rollback", outcome="failed",
                run_id=run_id,
                request_params={"recovered": True,
                                "to_version_id": target_id,
                                "from_version_id": from_id,
                                "reason": reason},
                error_message=f"rollback_recover_hash_mismatch: {reason}",
                user_id="recovery")
        except Exception as exc:  # noqa: BLE001 —— 审计不是关键路径
            logger.warning("pending 回滚恢复失败审计写入失败 run=%s: %s",
                           run_id, exc)

    # ------------------------------------------------------------ 日志
    async def log(self, tenant_id: str, run_id: str, line: str) -> None:
        """追加一行日志，并在同一次提交里刷新 run 的 `updated_at`（心跳）。

        `seq` 用 `SELECT COALESCE(MAX(seq),0)+1`。并发写同一个 run 时存在
        竞争窗口（两个写入者可能拿到同一个 seq）；插入显式带
        `ON CONFLICT (run_id, seq) DO NOTHING`，后到者只是**丢掉自己那条
        旁路日志**，既不覆盖已有行、也不会把整次写入炸成主键冲突。
        `tenant_id` 由调用方在取 run 时已校验；日志表本身没有该列（隔离由
        父表承担）。

        **Task 7 心跳是第二个写入者**：基准执行期间 `bench.heartbeat` 会用
        独立会话并发调本方法。seq 撞车时可能丢掉一条**旁路日志**（心跳行或
        阶段行，按设计如此），但插入与心跳 UPDATE 在同一次提交里，**心跳
        永远落库** —— `updated_at` 的 30 分钟 stale 判定有足够余量，丢一行
        日志不影响状态机。

        心跳：长阶段（如评测）持续写日志，`recover_stale` 以 `updated_at`
        判断是否中断 —— 若日志不刷新 `updated_at`，跑得久的运行会被误判
        成「服务重启中断」。插入命中 0 行（seq 已被并发写入者占用）时心跳
        仍然执行并提交。

        截断：每个 run 只保留最近 `LOG_MAX_LINES` 行；超出后同一次提交里删掉
        `seq <= seq - LOG_MAX_LINES` 的旧行（完整语料基准会写上万行）。
        """
        # 整个日志体（取 seq / INSERT / 截断 / 心跳 / commit）共用一个回滚
        # 保护：任何一条语句失败（连接断 / 约束 / 提交失败）都会让会话处于
        # 失败事务里 —— 不先 rollback，后续这条会话上的任何语句都会连锁报错。
        # 日志只是旁路，绝不能把一次写日志失败升级成「整个运行会话中毒」。
        # 回滚后原样上抛，调用方（runner / pipeline）照旧吞掉日志异常。
        try:
            seq = (await self.session.execute(
                text("SELECT COALESCE(MAX(seq),0)+1 "
                     "FROM knowledge_base.evolution_run_logs WHERE run_id=:r"),
                {"r": run_id},
            )).scalar()
            # 显式 INSERT + ON CONFLICT：(run_id, seq) 是复合主键，并发心跳与
            # 阶段日志同时抢到同一 seq 时只丢一条旁路日志，不抛 IntegrityError。
            await self.session.execute(
                text("INSERT INTO knowledge_base.evolution_run_logs "
                     "(run_id, seq, line) VALUES (:r, :s, :l) "
                     "ON CONFLICT (run_id, seq) DO NOTHING"),
                {"r": run_id, "s": int(seq), "l": str(line)[:4000]})
            # 截断：每个 run 只保留最近 LOG_MAX_LINES 行（长跑 / 完整语料基准会
            # 写上万行）。与插入、心跳同一次提交，不留「删了但新行没进去」的窗口。
            trim = _trim_before(int(seq))
            if trim > 0:
                await self.session.execute(
                    delete(EvolutionRunLog).where(
                        EvolutionRunLog.run_id == run_id,
                        EvolutionRunLog.seq <= trim))
            await self.session.execute(
                update(EvolutionRun).where(EvolutionRun.id == run_id)
                .values(updated_at=_now()))
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise

    async def log_tail(self, tenant_id: str, run_id: str, n: int = 200) -> list[str]:
        """返回日志尾部 n 行（时间正序）；经父表 join 做租户过滤。"""
        stmt = (
            select(EvolutionRunLog.line)
            .join(EvolutionRun, EvolutionRun.id == EvolutionRunLog.run_id)
            .where(EvolutionRun.tenant_id == tenant_id,
                   EvolutionRunLog.run_id == run_id)
            .order_by(EvolutionRunLog.seq.desc())
            .limit(max(0, int(n)))
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return list(reversed(list(rows)))
