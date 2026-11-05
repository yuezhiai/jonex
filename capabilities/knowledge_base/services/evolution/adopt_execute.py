# -*- coding:utf-8 -*-
"""采纳执行侧（父本臂 / 候选臂 / apply / smoke / 激活）的 mixin。

依赖一律经 `common.<name>` 解析，保证测试对 `adopt_common` 的 patch 生效。
"""
from __future__ import annotations

import logging
import time

from jonex_core.common.exceptions import (
    InvalidParameterError,
    ResourceConflictError,
)

from . import adopt_common as common
from . import smoke as smoke_mod
from .bench import bench_verdict, compare_arms, merge_retry, slim_arm
from .builders import adhoc_case_id
from .ledger_service import ledger_type_for_kind

logger = logging.getLogger(f"{__package__}.adopt_service")


def constraint_ledger_entry(item: dict, source_entry: dict, *,
                            row_item: dict | None = None, kb: str,
                            ds: str | None, version_before, version_after,
                            backup_hint: str, run_id: str, at, note: str) -> dict:
    """constraint 采纳台账行（R4）：`entry.kind="constraint"`，写 query_rule 那本。

    `op` 是本次改动（add/update/disable/enable）；`before` 原样带出 apply 记账
    的内容（update 的旧 expression / disable、enable 的旧 enabled），供人工核对
    与回滚复盘。`target_code` / `constraint_type` 优先取入参条目（add 带着
    它们），否则从 apply 后的行按 name 找到的 `row_item` 补全 —— update /
    disable / enable 的台账不再出现两个 null。与 relation 的采纳行不同，这里
    **不写** `op="rollback"`（那是回滚行的判据）。
    """
    row_item = row_item if isinstance(row_item, dict) else {}
    source_entry = source_entry if isinstance(source_entry, dict) else {}
    return {
        "kind": "constraint",
        "op": item.get("op"),
        "name": item.get("name"),
        "before": dict(item.get("before") or {}),
        "target_code": (source_entry.get("target_code")
                        or row_item.get("target_code")),
        "constraint_type": (source_entry.get("constraint_type")
                            or row_item.get("constraint_type")),
        "expression": source_entry.get("expression"),
        "kb": kb,
        "ds_id": ds,
        "version_before": version_before,
        "version_after": version_after,
        "verified": True,
        "from_file": common._patch_hint(run_id),
        "backup": backup_hint,
        "at": at,
        "note": note,
    }


def mapping_ledger_entry(item: dict, source_entry: dict, *, kb: str,
                         version_before, version_after, backup_hint: str,
                         run_id: str, at, note: str) -> dict:
    """mapping 采纳台账行（P1-5）：`entry.kind="mapping"`，写 alias 那本。

    身份取 apply 记账的复合键（`target_code` + 物理三元组），`source_entry`
    只在 apply 记录缺字段时兜底 —— 人审 `--pick` 按 `entry_key(item,"mapping")`
    匹配到的就是同一条。`before` / `after` 原样带出（add 无 before；update 的
    before 只含被改且前态存在的槽；disable/enable 记旧 `enabled`），供人工核对
    与回滚复盘。**不写** `op="rollback"`（那是回滚行的判据）。
    """
    item = item if isinstance(item, dict) else {}
    source_entry = source_entry if isinstance(source_entry, dict) else {}

    def field(name):
        value = item.get(name)
        return source_entry.get(name) if value in (None, "") else value

    def slots(name):
        value = item.get(name)
        if not isinstance(value, dict):
            value = source_entry.get(name)
        return dict(value) if isinstance(value, dict) else {}

    return {
        "kind": "mapping",
        "op": field("op"),
        "target_code": field("target_code"),
        "data_source_id": field("data_source_id"),
        "entity_name": field("entity_name"),
        "attribute_name": field("attribute_name"),
        "before": slots("before"),
        "after": slots("after"),
        "kb": kb,
        "version_before": version_before,
        "version_after": version_after,
        "verified": True,
        "from_file": common._patch_hint(run_id),
        "backup": backup_hint,
        "at": at,
        "note": note,
    }


def tool_schema_ledger_entry(item: dict, source_entry: dict, *, kb: str,
                             version_before, version_after, backup_hint: str,
                             run_id: str, at, note: str) -> dict:
    """tool_schema 采纳台账行（Tool-Schema 线）：`entry.kind="tool_schema"`，
    写 alias 那本。

    `op` 是本次改动（update/disable/enable）；`before` 原样带出 apply 记账的
    内容（新建覆盖项时为 None，供人工核对「这条是不是本批新增的」）。update
    另带目标 `description`（disable/enable 从源条目兜底，可能为空）。KB 级
    改动没有 `ds_id`。**不写** `op="rollback"`（那是回滚行的判据）。
    """
    item = item if isinstance(item, dict) else {}
    source_entry = source_entry if isinstance(source_entry, dict) else {}
    description = item.get("description")
    if description in (None, ""):
        description = source_entry.get("description")
    return {
        "kind": "tool_schema",
        "op": item.get("op"),
        "name": item.get("name"),
        "before": item.get("before"),
        "description": description,
        "kb": kb,
        "version_before": version_before,
        "version_after": version_after,
        "verified": True,
        "from_file": common._patch_hint(run_id),
        "backup": backup_hint,
        "at": at,
        "note": note,
    }


def _smoke_run_with_picked_changes(run: dict, entries: list[dict]) -> dict:
    """把**未被本次 picks 选中**的 ad-hoc `expected_changes` 从 smoke 输入剔除。

    `patch.candidate.expected_changes` 来自完整补丁（不是 picks 裁剪后的）：
    alias 条目会为每条问句生成一条 `adhoc:` 判据。用户取消勾选后，那条 case
    根本没被 apply；若仍留在判据里，smoke 会误报 `EXPECTED_CHANGE_MISMATCH`
    并阻断采纳。语料 case（非 `adhoc:` 前缀）一律保留。

    `entries` 是 `_pick_entries` 选中的条目（**不改看**它们的 verification：
    旧 truths 路径在 smoke 前已把通过的 case 标 verified，用 verification 过滤
    会误删这些判据、改变旧路径行为）。没有任何条目带 question 时保守不过滤。
    """
    patch = (run or {}).get("patch") if isinstance(run, dict) else None
    candidate = patch.get("candidate") if isinstance(patch, dict) else None
    changes = (candidate.get("expected_changes")
               if isinstance(candidate, dict) else None)
    if not isinstance(changes, list):
        return run
    picked = {adhoc_case_id(entry.get("question"))
              for entry in entries or []
              if isinstance(entry, dict) and entry.get("question")}
    if not picked:
        return run

    def _keep(change) -> bool:
        if not isinstance(change, dict):
            return True
        case_id = str(change.get("case_id") or "")
        return not (case_id.startswith("adhoc:") and case_id not in picked)

    kept = [change for change in changes if _keep(change)]
    if len(kept) == len(changes):
        return run
    return {**run, "patch": {**patch,
                             "candidate": {**candidate,
                                           "expected_changes": kept}}}


class AdoptExecuteMixin:
    # ------------------------------------------------------------ 后台执行
    async def execute(self, tenant_id: str, run_id: str, *,
                      picks: list[str] | None = None, note: str = "",
                      user_id: str | None = None,
                      request_id: str | None = None,
                      override_reason: str | None = None,
                      adhoc_truths: list[dict] | None = None,
                      adhoc_verdicts: list[dict] | None = None) -> dict:
        """执行已受理（`adopting`）的采纳：父本臂 → 链锁 → apply/smoke → 判定。

        只由 `accept()` 之后调用（`adopt()` 内联；Task 9 后台调度）。本方法
        **不自己排程**；run 已不在 `adopting` 时返回只读回执（`adopted` 按
        状态，`bench_status` 原样）。

        顺序（Task 6 brief / spec §5.3）：
        1. stale 检查（父版本已不是链头 → 基准 skipped + `adopt_stale`）；
        2. pre 快照 + `bench_started_at` 计时；
        3. 语料（空语料：无 override 失败收口；有 override 跳过两臂）；
        4. 父本臂（臂后快照哈希必须不变）→ 写 `bench_status="parent_ran"`；
        5. 持链锁重读 active + 同链在飞检查；
        6. apply / ad-hoc / smoke 后跑候选臂并判定；
        7. 判定通过才激活版本、收口 `adopted`（判定失败恢复快照）。
        """
        store = common.RunStore(self.session)
        run = await store.get(tenant_id, run_id)
        if run.get("status") != "adopting":
            return common._execute_receipt(run)

        override = str(
            run.get("override_reason") or override_reason or "").strip()
        truths = common.normalize_adhoc_truths(adhoc_truths)
        verdicts = common.normalize_adhoc_verdicts(adhoc_verdicts)
        # 两条 ad-hoc 通道互斥（与 `_preflight` 同口径）：直接调 execute 的
        # 防御分支也不能让其中一条静默胜出。
        if truths and verdicts:
            raise InvalidParameterError(
                message="adhoc_truths 与 adhoc_verdicts 不能同时提供")
        patch = common._patch_of(run)
        entries = common._pick_entries(patch, picks)
        kind, kb, ds = patch.get("kind"), patch.get("kb"), patch.get("ds")
        try:
            scope = common.scope_for_kind(kind)
        except ValueError as exc:
            raise InvalidParameterError(message=str(exc)) from exc
        # alias / synonym 批省略 `adhoc_verdicts` 且 picks 已排除全部待验真
        # case：等价于「本次无需验真」，不能回落到 verify_adhoc 自动 judge
        # （未被 apply 的 case 本来就没有候选数据可判）。与 `_preflight` 的
        # 兼容分支同口径。
        if (verdicts is None and kind in ("alias", "synonym")
                and common._unverified_adhoc_cases(run)
                and not common.picked_adhoc_cases(run, picks)):
            verdicts = []
        # `execute` 是公开后台入口（accept 之后、恢复/另一个 worker 都可能直接
        # 调用）：人工判定与 accept 同口径，kind / 覆盖 / fail 在任何写库之前
        # 校验。人工验真本期做 alias 与 synonym（spec §5）。
        if verdicts is not None:
            if kind not in ("alias", "synonym"):
                raise InvalidParameterError(
                    message="该演化类型暂不支持人工验真")
            common._require_verdict_coverage(
                common.picked_adhoc_cases(run, picks), verdicts)
            if any(v.get("verdict") == "fail" for v in verdicts):
                raise ResourceConflictError(
                    message="人工验真不通过，不能采纳")

        versions = common.VersionStore(self.session)
        active = await versions.active_for(tenant_id, kb, scope, ds)
        parent_id = run.get("parent_version_id")
        if parent_id is None:
            # 直接调 execute（未经 accept）的防御分支：legacy 用当前 active。
            if active is None:
                return await self._stale_receipt(
                    store, tenant_id, run_id, user_id=user_id,
                    request_id=request_id,
                    message=(f"{run_id} 没有父版本，且该版本链还没有 v0，"
                             "无法按 legacy 流程采纳"))
            parent_id = active.get("version_id")
        elif active is None or active.get("version_id") != parent_id:
            return await self._stale_receipt(
                store, tenant_id, run_id, user_id=user_id,
                request_id=request_id,
                parent_version_id=parent_id,
                active_version_id=(active or {}).get("version_id"))

        gate = run.get("gate") if isinstance(run.get("gate"), dict) else {}
        decision = run.get("gate_decision") or gate.get("decision")
        gate_adhoc = run.get("gate_adhoc")
        gate_effective = run.get("gate_effective")
        if gate_effective is None:
            gate_effective = common._gate_effective(decision, gate_adhoc)
        # accept 已把「门禁 override」写进 gate_override；这里按同一判据复原。
        override_applied = gate_effective != "accept" and bool(override)
        # Task 7：warn 模式按**当前部署开关**解析（`evolution.bench_warn` 的
        # 语义就是「这次执行时 EVOLUTION_BENCH_MODE=warn」），accept 落库的
        # `run.bench_mode` 只作展示，不覆盖运行时开关。
        mode = common.bench_mode()

        # ---- pre 快照（只用于父本臂哈希校验；恢复源在锁内另取）
        pre = await common.schema_io.snapshot_for(tenant_id, kind, kb, ds)
        pre_hash = common.canonical_snapshot_hash(pre)
        bench_started_at = common._now_utc()
        started = time.monotonic()
        if not await store.update(
                run_id, tenant_id=tenant_id,
                bench_started_at=bench_started_at,
                # Task 7：把**本次执行生效**的模式落库（accept 时存的是受理
                # 当刻的模式；执行可能由另一个 worker 在改过 env 后启动）。
                bench_mode=mode,
                expect_statuses={"adopting"}):
            current = await store.get(tenant_id, run_id)
            if current.get("status") != "adopting":
                return common._execute_receipt(current)

        # ---- 语料：读不到按空语料收口（不能因为语料就 500）
        # KB 级 run（alias / mapping）自身 ds=None，语料源走 `corpus_ds`
        # （mapping 由开库校验写入的 `request.bench_ds`）。
        try:
            corpus = await common.load_corpus(
                self.session, tenant_id, kb, common.corpus_ds(run, ds))
        except Exception as exc:  # noqa: BLE001 —— 空语料由下方分支收口
            common.logger.warning("采纳读取语料失败 run=%s: %s", run_id, exc)
            await common._rollback_session(self.session)
            corpus = {"cases": []}
        cases = common._corpus_cases(corpus)
        corpus_size = len(cases)
        target = corpus.get("target") if isinstance(corpus, dict) else None
        bench_ds = (target.get("data_source_id")
                    if isinstance(target, dict) else None)
        bench_base = {"mode": "gateway", "corpus_size": corpus_size,
                      "parent": None, "candidate": None, "bench_mode": mode}

        verdict_runner = None
        # P0-1：父本臂后的双链 ruler hash，交给锁内闭两臂之间的窗口。
        # 空语料 / 无父本臂时保持空字典（守卫按「缺 facet 即跳过」处理）。
        parent_ruler: dict = {}
        # P0-1：父本臂落库的 bench 事实（mode/corpus_size/parent/attempt）。
        # 跨臂守卫在候选臂之前触发，不能把这份事实重写成只剩 reasons。
        parent_bench: dict = {}
        async with common.heartbeat(tenant_id, run_id):
            if not cases:
                # 空语料：无 override 失败收口；有 override 跳过两臂（仍跑 smoke）。
                if not override:
                    error = "无语料，无法跑完整语料基准"
                    await common._release_evaluated(
                        store, tenant_id, run_id, error=error,
                        bench={**bench_base, "reasons": ["corpus_empty"],
                               "override": False},
                        bench_status="failed",
                        bench_started_at=bench_started_at,
                        bench_ended_at=common._now_utc())
                    await self._record_execute_audit(
                        store, tenant_id, run_id, action="evolution.bench_error",
                        request_id=request_id, decision=decision,
                        gate_effective=gate_effective, user_id=user_id,
                        error_message=error, bench_status="failed",
                        corpus_size=0, new_red_count=0)
                    current = await store.get(tenant_id, run_id)
                    return common._execute_failed_receipt(
                        current,
                        "无语料，默认阻止采纳；确需采纳请带 override_reason")
                skip = {"bench": {**bench_base, "reasons": ["corpus_empty"],
                                  "override": True},
                        "bench_status": "skipped",
                        "bench_ended_at": common._now_utc(),
                        "bench_skip_override": True}
                # `bench.override=True` 必须先落库：进程若在 apply/smoke 之间崩掉，
                # Task 8 的恢复要凭这个事实决定「允许激活」而不是恢复。
                await store.update(
                    run_id, tenant_id=tenant_id, bench=skip["bench"],
                    bench_status="skipped", bench_ended_at=skip["bench_ended_at"],
                    expect_statuses={"adopting"})

                async def verdict_runner():
                    return skip
            else:
                # ---- 父本臂：先跑基线，臂后哈希必须不变（执行器不得改 schema）
                parent_started = time.monotonic()
                await common._stage_log(store, tenant_id, run_id, "bench-parent-start",
                                 extra={"corpus_size": corpus_size})
                # Critical #1：父本臂前先给**两条链**（抽象 + compiled）拍 hash。
                ruler_before = await common._ruler_hashes(tenant_id, kb, bench_ds)
                parent = await common._bench_arm(
                    tenant_id, kb=kb, ds=bench_ds, corpus=corpus,
                    run_id=run_id, user_id=run.get("adopted_by") or user_id)
                parent_arm = (parent.get("arm")
                              if isinstance(parent.get("arm"), dict) else {})
                await common._stage_log(
                    store, tenant_id, run_id, "bench-parent-end",
                    started=parent_started,
                    extra={"corpus_size": corpus_size,
                           "passes": parent_arm.get("passes"), "new_red": 0})
                post_parent = await common.schema_io.snapshot_for(tenant_id, kind, kb, ds)
                ruler_after = await common._ruler_hashes(tenant_id, kb, bench_ds)
                # P0-1：父本臂结束时（锁之前）的尺子 → 锁内候选 baseline 比对。
                parent_ruler = ruler_after
                if (common.canonical_snapshot_hash(post_parent) != pre_hash
                        or common._ruler_changed(ruler_before, ruler_after)):
                    error = "父本臂执行期间 schema 内容发生变化（执行器错误）"
                    await common._release_evaluated(
                        store, tenant_id, run_id, error=error,
                        bench={**bench_base, "parent": parent.get("arm"),
                               "attempt": parent.get("attempt"),
                               "reasons": ["executor_error"], "override": False},
                        bench_status="failed",
                        bench_started_at=bench_started_at,
                        bench_ended_at=common._now_utc())
                    await self._record_execute_audit(
                        store, tenant_id, run_id, action="evolution.bench_error",
                        request_id=request_id, decision=decision,
                        gate_effective=gate_effective, user_id=user_id,
                        error_message=error, bench_status="failed",
                        corpus_size=corpus_size, new_red_count=0)
                    current = await store.get(tenant_id, run_id)
                    return common._execute_failed_receipt(
                        current, "父本臂期间 schema 内容变化，默认阻止采纳")
                if parent.get("valid") is False:
                    error = "父本臂无效（执行器错误），默认阻止采纳"
                    if not override:
                        await common._release_evaluated(
                            store, tenant_id, run_id, error=error,
                            bench={**bench_base, "parent": parent.get("arm"),
                                   "attempt": parent.get("attempt"),
                                   "reasons": ["executor_error"],
                                   "override": False},
                            bench_status="failed",
                            bench_started_at=bench_started_at,
                            bench_ended_at=common._now_utc())
                        await self._record_execute_audit(
                            store, tenant_id, run_id,
                            action="evolution.bench_error",
                            request_id=request_id, decision=decision,
                            gate_effective=gate_effective, user_id=user_id,
                            error_message=error, bench_status="failed",
                            corpus_size=corpus_size, new_red_count=0)
                        current = await store.get(tenant_id, run_id)
                        return common._execute_failed_receipt(
                            current,
                            "父本臂无效（执行器错误），默认阻止采纳；"
                            "确需采纳请带 override_reason")
                    # 人工 override 放行：跳过候选臂，仍要 apply + smoke。
                    skip = {"bench": {**bench_base, "parent": parent.get("arm"),
                                      "attempt": parent.get("attempt"),
                                      "reasons": ["executor_error"],
                                      "override": True},
                            "bench_status": "skipped",
                            "bench_ended_at": common._now_utc(),
                            "bench_skip_override": True}
                    parent_bench = skip["bench"]
                    # 同空语料路径：override 放行的 skipped 事实先落库再 apply。
                    await store.update(
                        run_id, tenant_id=tenant_id, bench=skip["bench"],
                        bench_status="skipped",
                        bench_ended_at=skip["bench_ended_at"],
                        expect_statuses={"adopting"})

                    async def verdict_runner():
                        return skip
                else:
                    # 中间 CAS（bench_started_at / parent_ran / skip 落库）的返回值
                    # 不承担并发判定；真正权威的守卫是 `_execute_locked` 里
                    # `_persist_before_write` 的 CAS（PUT 前最后一道）。
                    parent_bench = {**bench_base,
                                    "parent": parent.get("arm"),
                                    "attempt": parent.get("attempt"),
                                    "reasons": []}
                    await store.update(
                        run_id, tenant_id=tenant_id, bench=parent_bench,
                        bench_status="parent_ran",
                        bench_started_at=bench_started_at,
                        expect_statuses={"adopting"})

                    async def verdict_runner():
                        return await self._run_candidate_verdict(
                            tenant_id, kb=kb, ds=bench_ds, corpus=corpus,
                            run_id=run_id,
                            user_id=run.get("adopted_by") or user_id,
                            parent=parent, corpus_size=corpus_size,
                            override=override, mode=mode, started=started)

            # ---- 链锁：父本臂跑完才拿锁；重读 active + 同链在飞检查
            async with common.chain_lock(
                    common.advisory_key(tenant_id, kb, scope, ds)):
                active = await versions.active_for(tenant_id, kb, scope, ds)
                if active is None or active.get("version_id") != parent_id:
                    return await self._stale_receipt(
                        store, tenant_id, run_id, user_id=user_id,
                        request_id=request_id,
                        # 走到锁内时语料已读：已知分母就如实写（只有锁外
                        # 早退的 stale 才用 None，因为那时语料还没读）。
                        bench={**bench_base, "corpus_size": corpus_size,
                               "reasons": ["stale"], "override": False},
                        parent_version_id=parent_id,
                        active_version_id=(active or {}).get("version_id"))
                other = await store.other_adopting(tenant_id, run_id, kb, ds, kind)
                if other:
                    raise ResourceConflictError(
                        message=f"同一版本链已有采纳正在进行（{other}），稍后再试")
                await common._stage_log(store, tenant_id, run_id, "locked",
                                 started=started, picked_count=len(entries))
                return await self._execute_locked(
                    tenant_id, run_id, run, patch, entries, kind, kb, ds, scope,
                    note=note, user_id=user_id, request_id=request_id,
                    override=override, truths=truths, verdicts=verdicts,
                    picks=picks,
                    override_applied=override_applied, decision=decision,
                    previous_version_id=parent_id, bench_ds=bench_ds,
                    parent_ruler=parent_ruler, parent_bench=parent_bench,
                    verdict_runner=verdict_runner, started=started)

    async def _stale_receipt(self, store, tenant_id: str, run_id: str, *,
                             user_id, request_id: str | None,
                             message: str | None = None,
                             bench: dict | None = None,
                             parent_version_id: str | None = None,
                             active_version_id: str | None = None) -> dict:
        """stale：回 `evaluated`（基准 skipped）并审计 `evolution.adopt_stale`。"""
        message = message or (
            "父版本已不是该链 active（stale candidate，父版本已被别的采纳"
            "推进），请基于最新版本重跑")
        stale_bench = bench or {"mode": "gateway", "corpus_size": None,
                                "parent": None, "candidate": None,
                                "reasons": ["stale"], "override": False}
        # 基准起止：`bench_started_at` 在 execute 读语料**之前**就已落库，
        # 所以锁内 stale（含空语料 + override 之后才 stale）都带开始时间；
        # 只有锁外早退（父版本一开始就不是 active，还没走到落库）才是 None。
        # `bench_ended_at` 是本次收口时刻。详情面的开始时间是 ISO 字符串，
        # 必须先解析再绑 DateTime 列；读取失败只降级为「省略开始时间」。
        try:
            current = await store.get(tenant_id, run_id)
        except Exception as exc:  # noqa: BLE001 —— 起止时间是旁路
            common.logger.warning("stale 读取基准开始时间失败 run=%s: %s",
                           run_id, exc)
            current = None
        await common._release_evaluated(
            store, tenant_id, run_id, error=message,
            # `corpus_size=None`：stale 时语料可能还没读，不能伪装成空语料。
            bench=stale_bench, bench_status="skipped",
            bench_started_at=common._parse_bench_started_at(current),
            bench_ended_at=common._now_utc())
        await self._record_execute_audit(
            store, tenant_id, run_id, action="evolution.adopt_stale",
            request_id=request_id, decision=None, gate_effective=None,
            user_id=user_id, error_message=message,
            bench_status="skipped",
            corpus_size=stale_bench.get("corpus_size"),
            new_red_count=len(stale_bench.get("new_red") or []),
            extra={"reasons": ["stale"],
                   "parent_version_id": parent_version_id,
                   "active_version_id": active_version_id})
        current = await store.get(tenant_id, run_id)
        return common._execute_failed_receipt(current, message)

    async def _record_execute_audit(self, store, tenant_id: str, run_id: str,
                                    *, action: str, request_id: str | None,
                                    decision, gate_effective, user_id,
                                    error_message: str,
                                    bench_status: str | None = None,
                                    corpus_size: int | None = None,
                                    new_red_count: int = 0,
                                    extra: dict | None = None) -> None:
        """执行侧失败审计（旁路：写失败只记日志，不能推翻收口）。

        Task 7：所有 bench 审计动作（`bench_fail` / `bench_warn` /
        `bench_error` / `adopt_stale` / `adopt_accept`）统一带
        `request_id` / `bench_status` / `corpus_size` / `new_red_count`。
        """
        params: dict = {"request_id": request_id,
                        "gate_decision": decision,
                        "gate_effective": gate_effective,
                        "bench_status": bench_status,
                        "corpus_size": corpus_size,
                        "new_red_count": new_red_count}
        if extra:
            params.update(extra)
        try:
            await common._record_audit(
                store, self.session, tenant_id=tenant_id, run_id=run_id,
                action=action, outcome="failed", request_params=params,
                user_id=user_id, warnings=[], error_message=error_message)
        except Exception as exc:  # noqa: BLE001 —— 审计是旁路
            common.logger.warning("执行审计写入失败 run=%s action=%s: %s",
                           run_id, action, exc)

    async def _run_candidate_verdict(self, tenant_id: str, *, kb: str,
                                     ds: str | None, corpus: dict,
                                     run_id: str, user_id, parent: dict,
                                     corpus_size: int, override: str,
                                     mode: str, started: float) -> dict:
        """跑候选臂 → 抖动补跑 → 判定；返回收口用的基准结果。

        判定失败 / 无效用 `_AdoptAbort` 走统一恢复漏斗（恢复快照 + 候选
        rejected）；`warn` 模式下判定失败**不阻断**（激活并按 `bench_status
        ="warn"` 收口，调用方补审计 `evolution.bench_warn`）。
        `skipped/executor_error` 有 override 时返回 skipped 放行。

        抖动补跑（Task 7）：只对首次**不是 pass**、补跑后为 **pass** 的题写
        `bench["flaky"] = [{"name","first","retry"}]`；补跑仍失败的题不入列。
        """
        store = common.RunStore(self.session)
        candidate_started = time.monotonic()
        await common._stage_log(store, tenant_id, run_id, "bench-candidate-start",
                         extra={"corpus_size": corpus_size})
        candidate = await common._bench_arm(
            tenant_id, kb=kb, ds=ds, corpus=corpus, run_id=run_id,
            user_id=user_id)
        parent_results = common._arm_results(parent)
        candidate_results = common._arm_results(candidate)
        compare = compare_arms(parent_results, candidate_results)
        retry_names = list(dict.fromkeys(
            compare["new_red"] + compare["new_error"]))
        flaky: list[dict] = []
        if retry_names:
            first_outcomes = {
                str(row.get("name")): str(row.get("outcome") or "error")
                for row in candidate_results if isinstance(row, dict)}
            retry_results = common._arm_results(await common._bench_arm(
                tenant_id, kb=kb, ds=ds, corpus=corpus, run_id=run_id,
                user_id=user_id, cases=retry_names))
            retry_outcomes = {
                str(row.get("name")): str(row.get("outcome") or "error")
                for row in retry_results if isinstance(row, dict)}
            flaky = [
                {"name": name, "first": first_outcomes.get(name),
                 "retry": retry_outcomes.get(name)}
                for name in retry_names
                if first_outcomes.get(name) != "pass"
                and retry_outcomes.get(name) == "pass"]
            candidate_results = merge_retry(candidate_results, retry_results)
            report = (candidate.get("report")
                      if isinstance(candidate.get("report"), dict) else {})
            merged_report = {key: value for key, value in report.items()
                             if key != "summary"}
            merged_report["results"] = candidate_results
            candidate = {**candidate, "report": merged_report,
                         "arm": slim_arm(merged_report)}
        verdict = bench_verdict(
            parent_results, candidate_results, corpus_size)
        await common._stage_log(
            store, tenant_id, run_id, "bench-candidate-end",
            started=candidate_started,
            extra={"corpus_size": corpus_size,
                   "passes": verdict["candidate"]["passes"],
                   "new_red": len(verdict["new_red"])})
        bench = {
            "mode": "gateway", "corpus_size": corpus_size,
            "bench_mode": mode,
            "parent": parent.get("arm"), "candidate": candidate.get("arm"),
            "attempt": parent.get("attempt"),
            "candidate_attempt": candidate.get("attempt"),
            "new_red": verdict["new_red"], "new_error": verdict["new_error"],
            "reasons": verdict["reasons"], "flaky": flaky,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "override": False,
        }
        ended = common._now_utc()
        if verdict["status"] == "passed":
            return {"bench": bench, "bench_status": "passed",
                    "bench_ended_at": ended, "bench_skip_override": False,
                    "bench_warn": False}
        if verdict["status"] == "failed":
            detail = "、".join(verdict["reasons"]) or "判定失败"
            if mode == "warn":
                # Task 7 降级：判定失败不恢复、不驳回；正常激活并按 warn 收口。
                return {
                    "bench": bench, "bench_status": "warn",
                    "bench_ended_at": ended, "bench_skip_override": False,
                    "bench_warn": True,
                    "bench_warn_message":
                        f"基准判定失败（{detail}），warn 模式未阻断采纳",
                }
            raise common._AdoptAbort(
                "smoke-failed",
                f"采纳后基准判定失败（{detail}），已恢复采纳前快照",
                audit_action="evolution.bench_fail",
                error=f"基准判定失败：{detail}",
                extra_fields={"bench": bench, "bench_status": "failed",
                              "bench_ended_at": ended})
        # ---- 候选臂无效 / 判定 skipped（`corpus_empty` 已在 execute 处理）
        if override:
            bench["override"] = True
            return {"bench": bench, "bench_status": "skipped",
                    "bench_ended_at": ended, "bench_skip_override": True,
                    "bench_warn": False}
        detail = "、".join(verdict["reasons"]) or "executor_error"
        raise common._AdoptAbort(
            "evaluated",
            "基准臂无效（执行器错误），默认阻止采纳；确需采纳请带 "
            "override_reason",
            audit_action="evolution.bench_error",
            error=f"基准臂无效：{detail}",
            extra_fields={"bench": bench, "bench_status": "failed",
                          "bench_ended_at": ended},
            raise_conflict=False)

    async def _execute_locked(self, tenant_id: str, run_id: str, run: dict,
                              patch: dict, entries: list[dict], kind: str,
                              kb: str, ds: str | None, scope: str, *,
                              note: str, user_id, request_id: str | None,
                              override: str, truths: list[dict] | None,
                              verdicts: list[dict] | None = None,
                              picks: list[str] | None = None,
                              override_applied: bool, decision,
                              previous_version_id: str | None,
                              verdict_runner, started: float,
                              bench_ds: str | None = None,
                              parent_ruler: dict | None = None,
                              parent_bench: dict | None = None) -> dict:
        """持链锁后的执行主体（snapshot → apply → verify → smoke → 判定 → 激活）。

        前置校验（status / parent / scope / gate / CAS `evaluated→adopting`）
        已由 `accept` 完成；`verdict_runner` 在 smoke 通过后、**激活之前**跑
        候选臂并返回 `{bench, bench_status, bench_ended_at, bench_skip_override}`。
        """
        store = common.RunStore(self.session)
        versions = common.VersionStore(self.session)
        warnings: list[str] = []
        version: dict | None = None
        pre = await common.schema_io.snapshot_for(tenant_id, kind, kb, ds)
        await common._stage_log(store, tenant_id, run_id, "snapshot", started=started)
        persisted_backup: dict = {}

        async def _persist_before_write(backup: dict, staged: dict) -> None:
            """PUT 之前先把备份与回滚账落库（CAS adopting）。

            落库的是**运行行的完整 patch**（未被 picks 裁剪），只把 `added` /
            `skipped` 换成 apply 阶段的结果 —— 这样后续预览/回滚仍看得到全部条目。
            CAS 未命中说明运行已在采纳中被并发改走：立刻中止，绝不再 PUT。
            """
            persisted_backup.clear()
            persisted_backup.update(backup or {})
            persisted = {
                **patch,
                "skipped": list(staged.get("skipped") or []),
                "rollback": {
                    **(patch.get("rollback") or {}),
                    "added": list((staged.get("rollback") or {}).get("added") or []),
                },
            }
            if not await store.update(
                    run_id, tenant_id=tenant_id, backup=backup,
                    patch=persisted, expect_statuses={"adopting"}):
                raise ResourceConflictError(
                    message=f"{run_id} 状态已变化，采纳未写入")
            # 回写闭包里的 `patch`：后续 `store.update(patch=patch)` 会
            # **整块覆盖** run.patch 列。只落库不回写的话，apply 后的第一次
            # 更新就会把刚持久化的回滚账清空 —— 进程中断（或恢复）时
            # `_rollback_synonym_patch` 将无账可逆，本批行变孤儿。
            patch["rollback"] = persisted["rollback"]
            patch["skipped"] = persisted["skipped"]

        # 只应用选中的条目；rollback 用独立副本，防止提前污染待持久化的 patch。
        apply_doc = {**patch, "entries": entries,
                     "rollback": dict(patch.get("rollback") or {})}
        try:
            applied = await common.schema_io.apply_patch(
                tenant_id, apply_doc, before_write=_persist_before_write)
        except Exception as exc:
            # apply 抛错：只有「PUT 可能已落地」才按 **pre** 恢复；零条生效 /
            # JonexException（乐观锁冲突）意味着 PUT 未成功，恢复快照反而会
            # 误伤并发赢家的写入（Task 4 的 Important 口径）。
            if common._apply_may_have_written(exc):
                await common._restore_after_failure(
                    store, versions, tenant_id, run_id,
                    common.schema_io.restore_source(
                        pre, persisted_backup, scope, kb, ds),
                    status="evaluated", error=str(exc), patch=apply_doc)
            else:
                await common._restore_evaluated(store, tenant_id, run_id)
            if isinstance(exc, RuntimeError) and "零条生效" in str(exc):
                # 零条生效（全被跳过）：apply 在**写库前**抛出（added 尚未记账），
                # 没有可撤销的落库状态 —— 转 1001，run 回到 evaluated。
                raise InvalidParameterError(
                    message="零条生效（全被跳过）——不写库") from exc
            raise

        added = applied.get("added") or []
        backup = applied.get("backup") or dict(persisted_backup)
        await common._stage_log(store, tenant_id, run_id, "applied", started=started,
                         picked_count=len(entries))
        # C1：失败恢复始终回到 apply 前内容（pre，缺失时回退 run.backup）。
        restore_source = common.schema_io.restore_source(pre, backup, scope, kb, ds)
        # synonym 失败只逆操作本批 `rollback.added`（见 `_restore_after_failure`）：
        # 不能拿 pre 快照整表 reconcile，否则会软删失败期间的人工新增组。
        restore_patch = {
            **patch,
            "rollback": {**(patch.get("rollback") or {}),
                         "added": list(added)},
        }
        gate_adhoc = run.get("gate_adhoc")
        gate_effective = common._gate_effective(decision, gate_adhoc)
        unverified = common._unverified_adhoc_cases(run)
        smoke_status: str | None = None
        adhoc_result: dict = {}
        bench_result: dict = {}
        bench_facts: dict = {}
        bench_warn = False
        # constraint 台账补全用：apply 后的行按 name 索引（target_code /
        # constraint_type 不随 op 变化，取这一份即可）。非 constraint 为空。
        constraint_meta: dict[str, dict] = {}

        try:
            # I3：apply 之后的读库与记账也放进 try —— 任何异常都走恢复漏斗。
            version_before = backup.get("schema_version")
            current = await common.schema_io.get_row(tenant_id, kind, kb, ds)
            version_after = (current or {}).get("schema_version")
            if kind == "constraint":
                constraint_meta = {
                    str(c.get("name")): c
                    for c in (current or {}).get("constraints") or []
                    if isinstance(c, dict) and c.get("name")
                }
            backup_hint = common._backup_hint(run_id)
            # `_persist_before_write` 已把 apply 的 `rollback.added` /
            # `skipped` 回写到 `patch`。这里**不能清空**：后续预览 / 中断
            # 恢复都只看库里的这一份账，`store.update(patch=patch)` 会整块
            # 覆盖。成功收口成 adopted 时才清账（step 12）。
            run = {**run, "patch": patch}

            # C1：apply 成功后才生成版本行，快照必须是 **post-state**。
            post = await common.schema_io.snapshot_for(tenant_id, kind, kb, ds)
            if post.get("content") is None:
                raise RuntimeError("采纳后快照内容为空，不能生成版本行")
            version = await versions.create_candidate(
                tenant_id, kb, scope, ds, post,
                parent_version_id=previous_version_id,
                source_run_id=run_id, created_by=common._identity(user_id),
                note=note or None)
            # ---- step 7：ad-hoc 验真 → gate_adhoc
            if unverified:
                if verdicts is not None:
                    # 人工判定取代自动 judge（spec §4.4 第 6 条）：不再调
                    # verify_adhoc，直接把 pass/fail 结论写成候选 evidence，
                    # 全部 pass 时 ad-hoc 投票等价于 accept。case 集合只取
                    # **picks 选中条目**关联的 unverified case：取消勾选后
                    # 剩下的 case 不参与投票，也不能回落自动 judge（否则
                    # 用户取消勾选也仍会被 ad-hoc 卡住）。
                    picked = common.picked_adhoc_cases(run, picks)
                    if picked:
                        outcomes = common.manual_adhoc_outcomes(
                            picked, verdicts)
                        gate_adhoc = "accept"
                        adhoc_result = {
                            "gate_adhoc": gate_adhoc,
                            "cases": outcomes,
                            "evidence": await smoke_mod._write_adhoc_evidence(
                                tenant_id, run, common._adhoc_cases(run),
                                outcomes, self.session),
                        }
                    else:
                        # picks 排除了全部 ad-hoc case：不投票、evidence 不变，
                        # `gate_adhoc` 保持进入分支前的值，继续采纳。
                        adhoc_result = {}
                else:
                    adhoc_result = await common.verify_adhoc(
                        tenant_id, run, truths, session=self.session)
                    if adhoc_result.get("gate_adhoc") is not None:
                        gate_adhoc = adhoc_result["gate_adhoc"]
                evidence = adhoc_result.get("evidence")
                if isinstance(evidence, dict):
                    patch = common._patch_with_evidence(patch, evidence)
                    run = {**run, "patch": patch}
                # A4：超时等门禁理由要落 run log（不只在返回值里）。
                if adhoc_result.get("reason"):
                    try:
                        await store.log(
                            tenant_id, run_id,
                            f"adhoc_verify: {adhoc_result['reason']}")
                    except Exception:  # noqa: BLE001 —— 日志是旁路
                        pass
                gate_effective = common._gate_effective(decision, gate_adhoc)
                if not await store.update(
                        run_id, tenant_id=tenant_id, gate_adhoc=gate_adhoc,
                        gate_effective=gate_effective, patch=patch,
                        expect_statuses={"adopting"}):
                    raise common._AdoptAbort(
                        "evaluated", f"{run_id} 状态已变化，采纳未完成")

            gate_fields = {"gate_adhoc": gate_adhoc,
                           "gate_effective": gate_effective}
            await common._stage_log(store, tenant_id, run_id, "verified",
                             started=started, gate_effective=gate_effective,
                             version_id=version["version_id"])

            # ---- step 9：ad-hoc 一票否决（override 也不能翻案）
            if gate_adhoc == "reject":
                adhoc_reason = adhoc_result.get("reason")
                raise common._AdoptAbort(
                    "evaluated",
                    "ad-hoc 补 truth 后验证失败（gate_adhoc=reject），"
                    "override 也不能采纳",
                    audit_action="evolution.adhoc_fail",
                    error=(f"ad-hoc 验证失败（{adhoc_reason}）"
                           if adhoc_reason else "ad-hoc 验证失败"))

            # ---- step 10：effective 仍不是 accept 且没有 override
            if gate_effective != "accept" and not override_applied:
                raise common._AdoptAbort(
                    "evaluated",
                    f"门禁结论是 {decision!r}（effective="
                    f"{gate_effective!r}），需要人工 override_reason 才能采纳")

            # ---- step 11：采纳后 smoke
            report = run.get("report")
            parent_report = (report.get("parent")
                             if isinstance(report, dict) else None)
            smoke = await common.run_smoke(
                tenant_id, _smoke_run_with_picked_changes(run, entries),
                version, truths, session=self.session, report=parent_report)
            smoke_mode = smoke.get("mode") or "enforce"
            smoke_raw = smoke.get("status")
            if smoke_raw == "failed" and smoke_mode == "warn":
                smoke_status = "warn"
            elif smoke_raw == "skipped":
                smoke_status = "skipped"
            else:
                smoke_status = smoke_raw
            if not await store.update(
                    run_id, tenant_id=tenant_id, smoke=smoke,
                    smoke_status=smoke_status, smoke_mode=smoke_mode,
                    patch=patch, expect_statuses={"adopting"}):
                raise common._AdoptAbort(
                    "evaluated", f"{run_id} 状态已变化，采纳未完成")
            await common._stage_log(store, tenant_id, run_id, "smoke",
                             started=started, smoke_status=smoke_status,
                             version_id=version["version_id"])

            # spec §6：无法执行默认阻止采纳；管理员显式 override_reason
            # （哪怕门禁本身是 accept）可放行。
            if smoke_status == "skipped" and not override:
                raise common._AdoptAbort(
                    "evaluated",
                    "smoke 无法执行（skipped），默认阻止采纳；"
                    "确需采纳请带 override_reason")
            # C4-1：门禁本身是 accept、只因 smoke 无法执行而绕过的，同样要
            # 在 run 上留 override 事实（否则 gate_override=0 却放了行）。
            smoke_skip_override = (smoke_status == "skipped"
                                   and bool(override)
                                   and not override_applied)
            if smoke_status == "failed":
                raise common._AdoptAbort(
                    "smoke-failed",
                    "采纳后 smoke 失败，已恢复采纳前快照",
                    audit_action="evolution.smoke_fail",
                    error=common._smoke_summary(smoke))
            if smoke_status == "warn":
                await common._record_audit(
                    store, self.session, tenant_id=tenant_id, run_id=run_id,
                    action="evolution.smoke_warn", outcome="failed",
                    request_params={
                        "request_id": request_id,
                        "gate_decision": decision,
                        "gate_effective": gate_effective,
                        "version_id": version["version_id"],
                        "smoke_status": smoke_status,
                        "picked_count": len(entries),
                    },
                    user_id=user_id, warnings=warnings,
                    error_message=common._smoke_summary(smoke))

            # ---- 候选臂判定（Task 6）：必须在激活之前 —— 判定失败恢复快照，
            #      绝不能出现「版本已上线再回滚」。判定失败 / 无效由
            #      `_AdoptAbort` 走统一恢复漏斗。
            # Critical #1：候选臂前后再给两条链拍 hash（apply 之后、激活之前）。
            candidate_before = await common._ruler_hashes(tenant_id, kb, bench_ds)
            # ---- P0-1：闭合父本臂结束 → 候选 baseline 之间的尺子窗口。
            #      父本臂的双链守卫在**拿到链锁之前**就结束了，而 apply /
            #      verify / smoke 都在之后；这段时间里**非采纳**链若被并发
            #      采纳/回滚推进，候选臂就不是父本臂那把尺子。被采纳的 facet
            #      本来就该被本次 apply 改掉，所以只比非采纳 facet。
            crossed = common._ruler_facets_changed(
                parent_ruler, candidate_before,
                common._unadopted_facets(kind, ds, bench_ds))
            if crossed:
                # 与候选臂期间变化同口径：恢复已应用内容、run 回 evaluated、
                # 候选驳回、绝不激活；返回回执而不是 409。
                # 父本臂事实（mode/corpus_size/parent/attempt）原样保留 ——
                # 候选臂没跑，但父本臂的测量是真实且有用的诊断依据。
                ended = common._now_utc()
                detail = ("基准两臂之间非采纳链 schema 变化"
                          f"（{common._facet_labels(crossed)}）")
                raise common._AdoptAbort(
                    "evaluated", detail,
                    audit_action="evolution.bench_error",
                    error=detail,
                    extra_fields={
                        "bench": {
                            **(parent_bench
                               if isinstance(parent_bench, dict) else {}),
                            "reasons": ["executor_error"],
                            "override": False,
                            # 自诊断：哪几个 facet 在两臂之间变了。
                            "ruler_crossed": sorted(crossed),
                            # R2：bench 是 JSONB（序列化器无 `default=`），
                            # 嵌套值必须是 JSON 原生类型 —— datetime 会
                            # 在这里抛 TypeError 并把收口整个丢掉。
                            "bench_ended_at": ended.isoformat(),
                        },
                        "bench_status": "failed",
                        "bench_ended_at": ended,
                    },
                    raise_conflict=False)
            bench_result = await verdict_runner()
            candidate_after = await common._ruler_hashes(tenant_id, kb, bench_ds)
            bench_skip_override = bool(
                bench_result.get("bench_skip_override"))
            bench_warn = bool(bench_result.get("bench_warn"))
            bench_facts = (bench_result.get("bench")
                           if isinstance(bench_result.get("bench"), dict)
                           else {})
            if common._ruler_changed(candidate_before, candidate_after):
                # 候选臂期间另一条链被并发采纳/回滚推进：两臂不是同一把
                # 尺子，判定不成立。与「没跑成」同口径收口：恢复已应用的
                # 内容、run 回 evaluated、绝不激活。
                detail = "候选臂执行期间 schema 内容发生变化（执行器错误）"
                raise common._AdoptAbort(
                    "evaluated", detail,
                    audit_action="evolution.bench_error",
                    error="基准候选臂期间 schema 内容变化，判定不成立",
                    extra_fields={
                        "bench": {**bench_facts,
                                  "reasons": ["executor_error"],
                                  "override": False},
                        "bench_status": "failed",
                        "bench_ended_at": common._now_utc(),
                    },
                    raise_conflict=False)

            # ---- Fix #10：激活前最后一道防御断言。有效路径只可能是
            #      passed / warn / skipped（skipped 必须带人为 override 事实）；
            #      其它值说明收口分支有 bug，宁可抛错也绝不静默上线。
            effective_bench_status = bench_result.get("bench_status")
            if effective_bench_status not in ("passed", "warn", "skipped"):
                raise RuntimeError(
                    "内部错误：激活前 bench_status="
                    f"{effective_bench_status!r}（只允许 passed/warn/skipped）")
            if (effective_bench_status == "skipped"
                    and bench_facts.get("override") is not True):
                raise RuntimeError(
                    "内部错误：bench_status=skipped 但 bench.override 不为 True")

            # ---- Fix #5：bench 事实先于 activate 落库（CAS adopting）。
            #      否则「版本已激活、run 收口 CAS 前崩溃」的窗口里
            #      recover_stale 只看到 pending/parent_ran，会把已生效的
            #      采纳误判成中断并恢复快照。CAS 未命中说明 run 已被并发
            #      收口，立刻中止（绝不继续激活）。
            if not await store.update(
                    run_id, tenant_id=tenant_id,
                    bench=bench_result.get("bench"),
                    bench_status=bench_result.get("bench_status"),
                    bench_ended_at=bench_result.get("bench_ended_at"),
                    expect_statuses={"adopting"}):
                raise common._AdoptAbort(
                    "evaluated", f"{run_id} 状态已变化，采纳未完成")

            # ---- step 12：激活新版本 → run adopted
            await versions.activate(tenant_id, version["version_id"],
                                    previous_id=previous_version_id)
            final_fields: dict = {}
            if smoke_skip_override or bench_skip_override:
                # 与门禁 override 同口径落库：run 必须能看出这次是人工放行。
                final_fields.update(
                    gate_override=1,
                    override_by=common._identity(user_id),
                    override_reason=override)
            # 只有**成功收口**才清账（设计口径：采纳后旧补丁回滚恒为
            # no-op）。adopting 期间（含中断恢复窗口）必须保留
            # `rollback.added`，见 `_persist_before_write`。
            adopted_patch = {
                **patch,
                "rollback": {**(patch.get("rollback") or {}), "added": []},
            }
            ok = await store.update(
                run_id, tenant_id=tenant_id, status="adopted",
                adopted_at=common._now_utc(),
                adopted_version_id=version["version_id"],
                # 只有真正收口成 adopted 才剪枝：此后恢复路径不再读 backup
                # （只有 adopting 期间读），完整快照没有保留价值。
                patch=adopted_patch,
                backup=common.pruned_backup(backup), smoke=smoke,
                smoke_status=smoke_status, smoke_mode=smoke_mode,
                gate_adhoc=gate_adhoc, gate_effective=gate_effective,
                bench=bench_result.get("bench"),
                bench_status=bench_result.get("bench_status"),
                bench_ended_at=bench_result.get("bench_ended_at"),
                # 成功收口必须清掉上一次尝试的错误文本。
                error=None,
                expect_statuses={"adopting"}, **final_fields)
            if not ok:
                # 已激活但收口 CAS 未命中：把新版本驳回并恢复旧 active。
                try:
                    await versions.reject_version(
                        tenant_id, version["version_id"])
                    if previous_version_id:
                        await versions.activate(tenant_id, previous_version_id,
                                                previous_id=None)
                except Exception as exc:  # noqa: BLE001 —— 补偿失败只记日志
                    common.logger.warning("采纳收口失败后补偿版本链失败 run=%s: %s",
                                   run_id, exc)
                raise common._AdoptAbort(
                    "evaluated", f"{run_id} 状态已变化，采纳已撤销",
                    error="采纳收口 CAS 未命中")
            await common._stage_log(store, tenant_id, run_id, "activated",
                             started=started, gate_effective=gate_effective,
                             smoke_status=smoke_status,
                             version_id=version["version_id"])
            # 值同义词：采纳已提交，读口（value_aliases 进程缓存）必须立即
            # 看到新内容。只在 synonym 生效；失败只 warning（旁路）。
            common.bump_value_aliases(kind, tenant_id, kb)

        except common._AdoptAbort as abort:
            gate_fields = {"gate_adhoc": gate_adhoc,
                           "gate_effective": gate_effective}
            final_status, message = await common._restore_after_failure(
                store, versions, tenant_id, run_id, restore_source,
                version=version, status=abort.status,
                error=abort.error or abort.message,
                gate_fields=gate_fields,
                extra_fields=abort.extra_fields,
                patch=restore_patch)
            # Task 7：bench 审计（bench_fail / bench_error）带统一四字段；
            # 其它中止（adhoc / smoke）没有基准事实，按 None / 0 如实回报。
            abort_fields = abort.extra_fields or {}
            abort_bench = (abort_fields.get("bench")
                           if isinstance(abort_fields.get("bench"), dict)
                           else {})
            await common._record_audit(
                store, self.session, tenant_id=tenant_id, run_id=run_id,
                action=abort.audit_action, outcome="failed",
                request_params={
                    "request_id": request_id,
                    "gate_decision": decision,
                    "gate_effective": gate_effective,
                    "version_id": (version or {}).get("version_id"),
                    "smoke_status": smoke_status,
                    "picked_count": len(entries),
                    "override_reason": override or None,
                    "bench_status": abort_fields.get("bench_status"),
                    "corpus_size": abort_bench.get("corpus_size"),
                    "new_red_count": len(abort_bench.get("new_red") or []),
                },
                user_id=user_id, warnings=warnings,
                error_message=abort.error or abort.message)
            # I6：smoke / ad-hoc / 收口失败都是候选的终态判决。
            await common._candidate_status(self.session, tenant_id, run_id, "rejected",
                                    gate=run.get("gate"))
            if not abort.raise_conflict:
                # 执行器错误 / 预检型失败：收口后返回结果字典（不是 409）。
                current = await store.get(tenant_id, run_id)
                return common._execute_failed_receipt(current, abort.message)
            raise ResourceConflictError(
                message=(abort.message if final_status != "failed"
                         else message),
                details={"warnings": list(warnings)} if warnings else None,
            ) from None
        except Exception as exc:
            # 任一步异常（读库/记账/create_candidate/verify/smoke/激活）：
            # 恢复 **pre** 快照；恢复失败 → failed。
            await common._restore_after_failure(
                store, versions, tenant_id, run_id, restore_source,
                version=version, status="evaluated", error=str(exc),
                gate_fields={"gate_adhoc": gate_adhoc,
                             "gate_effective": gate_effective},
                patch=restore_patch)
            raise

        # 收口命中后才记台账：被并发改走的运行不能留下「采纳过」的假记录。
        # I3：台账是**旁路** —— 任何一条写失败都不能让已成功的采纳变成 500，
        # 更不能吞掉紧随其后的审计。失败进 warnings + run_logs。
        at = common._now()
        ledger = common.LedgerService(self.session)
        try:
            for item in added:
                if kind == "alias":
                    proposal_id = next(
                        (x.get("proposal_id") or "" for x in entries
                         if str(x.get("concept") or "").strip()
                         == str(item["concept"]).strip()
                         and str(x.get("alias") or "").strip()
                         == str(item["alias"]).strip()), "")
                    entry = {
                        "id": proposal_id,
                        "concept": item["concept"],
                        "alias": item["alias"],
                        "kb": kb,
                        "version_before": version_before,
                        "version_after": version_after,
                        "verified": True,
                        "backup": backup_hint,
                        "at": at,
                        "note": note,
                    }
                    await ledger.append(tenant_id, "alias", entry, run_id=run_id)
                elif kind == "relation":
                    # R2：relation 仍写 query_rule 那本账，用 `entry.kind` 区分。
                    source_entry = next(
                        (x for x in entries
                         if str(x.get("name") or "") == str(item["name"])), {})
                    entry = {
                        "kind": "relation",
                        # P1 收尾：台账记 op（add/remove）；remove 另带 before
                        # （原边对象），人工核对/回滚复盘能看到删掉的是什么。
                        "op": item.get("op") or "add",
                        "name": item["name"],
                        "source": source_entry.get("source"),
                        "source_column": source_entry.get("source_column"),
                        "target": source_entry.get("target"),
                        "target_column": source_entry.get("target_column"),
                        "cardinality": source_entry.get("cardinality"),
                        "description": source_entry.get("description"),
                        "before": item.get("before"),
                        "kb": kb,
                        "ds_id": ds,
                        "version_before": version_before,
                        "version_after": version_after,
                        "verified": True,
                        "from_file": common._patch_hint(run_id),
                        "backup": backup_hint,
                        "at": at,
                        "note": note,
                    }
                    await ledger.append(
                        tenant_id, ledger_type_for_kind("relation"), entry,
                        run_id=run_id)
                elif kind == "mapping":
                    # P1-5：写抽象 schema 的落点，落 alias 那本账，用
                    # `entry.kind="mapping"` 区分。source_entry 按复合键匹配
                    # （同一 target_code 的多个落点不能互相串选）。
                    source_entry = next(
                        (x for x in entries
                         if isinstance(x, dict)
                         and common.entry_key(x, "mapping")
                         == common.entry_key(item, "mapping")), {})
                    entry = mapping_ledger_entry(
                        item, source_entry,
                        kb=kb, version_before=version_before,
                        version_after=version_after,
                        backup_hint=backup_hint, run_id=run_id, at=at, note=note)
                    await ledger.append(
                        tenant_id, ledger_type_for_kind("mapping"), entry,
                        run_id=run_id)
                elif kind == "tool_schema":
                    # Tool-Schema 线：写抽象 schema 的 tools 覆盖块，落 alias
                    # 那本账，用 `entry.kind="tool_schema"` 区分。source_entry
                    # 按 name 匹配（`entry_key` 对三 op 都是 name）。
                    source_entry = next(
                        (x for x in entries
                         if isinstance(x, dict)
                         and common.entry_key(x, "tool_schema")
                         == common.entry_key(item, "tool_schema")), {})
                    entry = tool_schema_ledger_entry(
                        item, source_entry,
                        kb=kb, version_before=version_before,
                        version_after=version_after,
                        backup_hint=backup_hint, run_id=run_id, at=at, note=note)
                    await ledger.append(
                        tenant_id, ledger_type_for_kind("tool_schema"), entry,
                        run_id=run_id)
                elif kind == "synonym":
                    # 值同义词线：同义词行写在 `ontology_synonyms`（不是 schema
                    # 行），台账仍落 alias 那本，用 `entry.kind="synonym"` 区分。
                    # `op` = add/update/delete；`before` 原样带出 apply 记账内容
                    # （update 是旧 canonical、delete 是旧组），旧补丁逆操作
                    # （schema_io.rollback_patch）据此恢复。
                    entry = {
                        "kind": "synonym",
                        "op": item.get("op"),
                        "terms": list(item.get("terms") or []),
                        "canonical": item.get("canonical"),
                        "before": item.get("before"),
                        "kb": kb,
                        "version_before": version_before,
                        "version_after": version_after,
                        "verified": True,
                        "from_file": common._patch_hint(run_id),
                        "backup": backup_hint,
                        "at": at,
                        "note": note,
                    }
                    await ledger.append(
                        tenant_id, ledger_type_for_kind("synonym"), entry,
                        run_id=run_id)
                elif kind == "constraint":
                    # R4：仍写 query_rule 那本账，用 `entry.kind="constraint"` 区分；
                    # `op` 记录本条的改动类型（add/update/disable/enable）。
                    source_entry = next(
                        (x for x in entries
                         if str(x.get("name") or "") == str(item["name"])), {})
                    entry = constraint_ledger_entry(
                        item, source_entry,
                        row_item=constraint_meta.get(str(item.get("name"))),
                        kb=kb, ds=ds,
                        version_before=version_before,
                        version_after=version_after,
                        backup_hint=backup_hint, run_id=run_id, at=at, note=note)
                    await ledger.append(
                        tenant_id, ledger_type_for_kind("constraint"), entry,
                        run_id=run_id)
                else:
                    expression = next(
                        (x.get("expression") for x in entries
                         if x.get("target_code") == item["target_code"]), "")
                    entry = {
                        "target_code": item["target_code"],
                        "expression": expression,
                        "kb": kb,
                        "ds_id": ds,
                        "version_before": version_before,
                        "version_after": version_after,
                        "verified": True,
                        "from_file": common._patch_hint(run_id),
                        "backup": backup_hint,
                        "at": at,
                        "note": note,
                    }
                    await ledger.append(tenant_id, "query_rule", entry,
                                        run_id=run_id)
        except Exception as exc:  # noqa: BLE001 —— 台账失败不推翻已成功的采纳
            message = f"台账写入失败：{exc}"
            warnings.append(message)
            common.logger.warning("采纳台账写入失败 run=%s: %s", run_id, exc)
            try:
                await store.log(tenant_id, run_id, message)
            except Exception:  # noqa: BLE001 —— 日志也是旁路
                pass

        # 操作审计（旁路；失败只 warning）：override 采纳单独记一条。
        # C4-1：smoke-skip 的人工放行也算 override，审计同样走 override 动作；
        # Task 6 的基准 skip（空语料 / 父本无效）同口径。
        override_used = (override_applied or smoke_skip_override
                         or bench_skip_override)
        audit_params = {
            "request_id": request_id,
            "gate_decision": decision,
            "gate_effective": gate_effective,
            "version_id": (version or {}).get("version_id"),
            "picked_count": len(entries),
            "override_reason": override or None,
            "smoke_status": smoke_status,
            # Task 7：成功审计同样带三个 bench 事实（与 bench_warn 同口径）。
            "bench_status": bench_result.get("bench_status"),
            "corpus_size": bench_facts.get("corpus_size"),
            "new_red_count": len(bench_facts.get("new_red") or []),
        }
        if smoke_skip_override:
            audit_params["smoke_skipped_override"] = True
        if bench_skip_override:
            audit_params["bench_skipped_override"] = True
        await common._record_audit(
            store, self.session, tenant_id=tenant_id, run_id=run_id,
            action=("evolution.adopt_override" if override_used
                    else "evolution.adopt"),
            outcome="success",
            request_params=audit_params,
            user_id=user_id, warnings=warnings)

        if bench_warn:
            # Task 7 降级审计：判定失败但 warn 模式放行 —— 必须在成功审计
            # **之后**写，保证它是本次执行的最后一条 `evolution.bench_warn`
            # 事实（前端/排障按动作过滤时不会与采纳成功混淆）。
            await common._record_audit(
                store, self.session, tenant_id=tenant_id, run_id=run_id,
                action="evolution.bench_warn", outcome="failed",
                request_params={
                    "request_id": request_id,
                    "gate_decision": decision,
                    "gate_effective": gate_effective,
                    "version_id": (version or {}).get("version_id"),
                    "picked_count": len(entries),
                    "bench_status": "warn",
                    "corpus_size": bench_facts.get("corpus_size"),
                    "new_red_count": len(bench_facts.get("new_red") or []),
                },
                user_id=user_id, warnings=warnings,
                error_message=(bench_result.get("bench_warn_message")
                               or "基准判定失败（warn 模式未阻断）"))

        # I6：候选生命周期收口（adopted；同 run 后续候选 superseded）。
        await common._candidate_status(self.session, tenant_id, run_id, "adopted",
                                gate=run.get("gate"),
                                supersede_others=True)

        # S8：采纳提交后 best-effort 归档旧版本；失败不回滚主流程。
        try:
            await versions.archive_old(tenant_id, kb, scope, ds, keep=50)
        except Exception as exc:  # noqa: BLE001 —— 归档失败只 warning
            common.logger.warning("采纳后归档旧版本失败 run=%s: %s", run_id, exc)

        updated = await store.get(tenant_id, run_id)
        return {
            "ok": True, "rc": 0,
            "stdout_tail": (f"已采纳 {len(added)} 条"
                            f"（跳过 {len(applied.get('skipped') or [])}）"),
            "run": updated,
            "patch": updated.get("patch"),
            "smoke": updated.get("smoke"),
            "adopted": True,
            "version_id": updated.get("adopted_version_id"),
            "gate": updated.get("gate") or None,
            "gate_effective": gate_effective,
            "gate_adhoc": updated.get("gate_adhoc"),
            "override": override_used,
            "bench": updated.get("bench"),
            "bench_status": updated.get("bench_status"),
            "accepted": False,
            "warnings": list(warnings),
            "idempotent_replay": False,
        }
