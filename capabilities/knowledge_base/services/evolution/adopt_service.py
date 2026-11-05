# -*- coding:utf-8 -*-
"""演化运行的人审写操作：预览 / 采纳 / 否决 / 回滚 / 条目调整。

语义逐条对齐旧 CLI `tests/eval/nl2sql/evolve.py` 的
`cmd_adopt` / `cmd_reject` / `cmd_rollback` / `cmd_adjust` / `cmd_edit`。
服务化后文件系统载体换成 DB：

- 备份文件（`patches/adopt-backup-*.json`）→ `evolution_runs.backup`；
- `patch.json` → `evolution_runs.patch`；
- `*.jsonl` 台账 → `evolution_ledgers`（`ledger_type` = alias / query_rule / reject）。

状态守卫统一用 `ResourceConflictError`（409）：对终态运行的写操作是**状态冲突**，
不是参数错误；`InvalidParameterError`（1001/400）只用于坏参数（未知 picks / 条目 /
恰给一个字段等）与采纳的「零条生效（全被跳过）」——后者在写库前判定、同样不改状态。
预览额外允许 `adopted`（复核场景）；采纳/否决/条目调整仍只认 `evaluated`。

本模块保留受理 / 预览 / 否决 / 条目调整等入口，执行与回滚分别由
`AdoptExecuteMixin` / `AdoptRecoveryMixin` 提供。所有协作方与纯函数助手都在
`adopt_common` 中单点导入；这里显式再导出，测试仍可读 `adopt_service.<name>`。
"""
from __future__ import annotations

import logging
import time

from sqlalchemy.exc import IntegrityError

from jonex_core.common.exceptions import (
    InvalidParameterError,
    ResourceConflictError,
)

from . import adopt_common as common
from .adopt_common import (
    LedgerService,
    RunStore,
    VersionStore,
    _ADHOC_EXPECTS,
    _AdoptAbort,
    _FACET_LABELS,
    _FAILED_ROLLBACK_STAGES,
    _GATE_DECISIONS,
    _ONLY_EVALUATED,
    _PREVIEW_STATUSES,
    _ROLLBACK_STATUSES,
    _ROLLBACK_TARGET_STATUSES,
    _STAGE_RESERVED_KEYS,
    _active_as_parent,
    _adhoc_cases,
    _apply_may_have_written,
    _arm_results,
    _backup_hint,
    _bench_arm,
    _candidate_status,
    _corpus_cases,
    _execute_failed_receipt,
    _execute_receipt,
    _facet_labels,
    _find_entry,
    _gate_effective,
    _identity,
    _now,
    _now_utc,
    _parse_bench_started_at,
    _patch_hint,
    _patch_of,
    _patch_with_evidence,
    _pick_entries,
    _record_audit,
    _release_evaluated,
    _replay_accepted,
    _replay_failure,
    _replay_success,
    _require_adhoc_coverage,
    _require_verdict_coverage,
    _require_same_chain,
    _require_status,
    _restore_after_failure,
    _restore_evaluated,
    _reject_version,
    _rollback_session,
    _ruler_changed,
    _ruler_facets_changed,
    _ruler_hashes,
    _smoke_summary,
    _stage_log,
    _trial,
    _undo_rollback_switch,
    _unadopted_facets,
    _unverified_adhoc_cases,
    advisory_key,
    audit,
    bench_mode,
    build_revise_messages,
    canonical_snapshot_hash,
    chain_lock,
    heartbeat,
    load_corpus,
    normalise_request_id,
    normalise_version_id,
    normalize_adhoc_truths,
    normalize_adhoc_verdicts,
    pruned_backup,
    run_bench_arm_with_retry,
    run_smoke,
    schema_io,
    scope_for_kind,
    verify_adhoc,
)
from .adopt_execute import AdoptExecuteMixin
from .adopt_recovery import AdoptRecoveryMixin
from .engine import MAX_EXPRESSION_CHARS
from .eval import plan_smoke

logger = logging.getLogger(f"{__package__}.adopt_service")


def _entry_for_case(entries: list[dict], case_id: str) -> dict | None:
    """按 `adhoc_case_id(entry.question)` 找 case 对应的条目（没有返回 None）。"""
    if not case_id:
        return None
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        if common.builders.adhoc_case_id(entry.get("question")) == case_id:
            return entry
    return None


def _concept_aliases_of(pre: dict | None, concept: str) -> list[str]:
    """`pre` 快照里该概念**已有**的别名（不含本批候选别名）。

    证据卡同时展示「候选别名」和「概念已有别名」：前者来自本批条目，后者
    必须取 apply **之前**的抽象行 —— apply 后候选别名会混进列表，用户就分不清
    哪些是既有、哪些是正在验真的改动。
    """
    concept = str(concept or "").strip()
    if not concept:
        return []
    content = (pre or {}).get("content")
    schema = content.get("schema") if isinstance(content, dict) else None
    entities = schema.get("entities") if isinstance(schema, dict) else None
    for entity in entities or []:
        if not isinstance(entity, dict):
            continue
        if str(entity.get("name") or "").strip() != concept:
            continue
        return [str(alias) for alias in entity.get("aliases") or []]
    return []


class AdoptService(AdoptExecuteMixin, AdoptRecoveryMixin):

    def __init__(self, session) -> None:
        self.session = session

    # ------------------------------------------------------------ 预览

    async def preview(self, tenant_id: str, run_id: str,
                      picks: list[str] | None = None) -> dict:
        """confirm=false 的预览：读补丁 → 过滤 → 取当前行 → 纯函数试算（不写库）。

        允许 `evaluated` 与 `adopted`（复核已采纳的运行）：已采纳时条目多已生效、
        会落进 `skipped`，`already_adopted` 按运行状态如实回报。

        预览**不校验 gate、不写库**；只把门禁现状与采纳所需信息摊开：
        `gate` / `gate_effective` / `override_required` / `adhoc_cases`（待补
        truth 的 unverified case）/ `smoke_plan`（Task 5 复用 `plan_smoke`）。
        """
        run = await common.RunStore(self.session).get(tenant_id, run_id)
        common._require_status(run, common._PREVIEW_STATUSES, "采纳预览")
        patch = common._patch_of(run)
        entries = common._pick_entries(patch, picks)
        planned, skipped = await common._trial(tenant_id, patch, entries)
        gate = run.get("gate") if isinstance(run.get("gate"), dict) else None
        decision = (gate or {}).get("decision")
        effective = common._gate_effective(decision, run.get("gate_adhoc"))
        # S9：ad-hoc 补 truth 后验证失败是硬否决，override 也放不出来 ——
        # 此时不能报 override_required（会诱导用户白填理由），改暴露 adhoc_veto。
        adhoc_veto = run.get("gate_adhoc") == "reject"
        override_required = (decision in common._GATE_DECISIONS
                             and effective != "accept" and not adhoc_veto)
        return {
            "planned": planned,
            "skipped": skipped,
            "backup_hint": common._backup_hint(run_id),
            "already_adopted": run.get("status") == "adopted",
            "gate": gate,
            "gate_effective": effective,
            "override_required": override_required,
            "adhoc_veto": adhoc_veto,
            # alias / synonym 批只列**本次 picks 影响到的** case：采纳校验
            # （`_preflight`）与执行阶段都按 picks 过滤，预览必须同口径；否则
            # 取消勾选相关条目后验真卡仍列出该 case，用户既取不到证据（1001）
            # 也无法确认采纳（Critical 1）。其它 kind 走旧 truths 通道，保持
            # 全量口径不变。
            "adhoc_cases": (common.picked_adhoc_cases(run, picks)
                            if patch.get("kind") in ("alias", "synonym")
                            else common._unverified_adhoc_cases(run)),
            "smoke_plan": plan_smoke(run, await self._load_corpus_safe(
                tenant_id, run)),
        }

    async def _load_corpus_safe(self, tenant_id: str, run: dict) -> dict:
        """预览用的语料读取：失败退空集（预览不能因语料缺失而 500）。

        KB 级 run（alias / mapping）`ds=None`，语料源走 `corpus_ds`
        （mapping 由开库校验写入的 `request.bench_ds`）。
        """
        try:
            return await common.load_corpus(
                self.session, tenant_id, run.get("kb"),
                common.corpus_ds(run, run.get("ds")))
        except Exception as exc:  # noqa: BLE001 —— 只影响 smoke_plan 的估计
            common.logger.warning("预览读取语料失败 run=%s: %s", run.get("run_id"), exc)
            return {"cases": []}

    # ------------------------------------------------------------ 前置校验

    async def _preflight(self, tenant_id: str, run: dict,
                         picks: list[str] | None, override: str,
                         truths: list[dict] | None,
                         verdicts: list[dict] | None = None):
        """受理（`accept`）与执行（Task 6 `execute`）共用的**写库前**校验。

        返回 CAS 需要的全部事实：

        `(patch, entries, kind, kb, ds, scope, active_version, legacy,
        parent_id, decision, gate_effective, override_applied)`

        顺序：ad-hoc verdicts 覆盖/fail → abstract scope/status → parent →
        gate/override → ad-hoc truths 覆盖 / 证据票据；**不取锁**。首次采纳
        （全新链、无任何版本）会用当前行快照补一条 v0 基线并激活 —— 这是
        CAS 之前**唯一**的写库动作（`_seed_baseline_version` 一次插入 + 一次
        激活）。受理若在后续校验（空语料等）被拒绝，已提交的 v0 基线**不回滚**
        （它只是当前行的真实快照）。`accept` 完成后，`execute` 只做执行侧检查
        （stale / 基准），不再重复这些校验。
        """
        common._require_status(run, common._ONLY_EVALUATED, "采纳")
        # 两条 ad-hoc 通道互斥（spec §4.4 第 1 条）：truths 走自动 judge，
        # verdicts 走人工判定；同时提供必须 1001，不能有一条静默胜出。
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

        # ---- ad-hoc 人工判定：覆盖 + fail 必须在 parent/seed **之前**完成。
        # legacy 首次采纳（parent_version_id=None）会在 parent 阶段补 v0 基线
        # 并提交；fail 若排在其后，就已经写库了（spec §4.4 第 5/7 条）。
        picked_unverified = None
        # alias 批：picks 已排除全部待验真 case 时，「无需验真」是合法状态。
        # 老前端/旧调用方可能整键省略 `adhoc_verdicts`，此时不能回落到旧
        # truths 覆盖校验（那会拿全量 unverified 把采纳 1001 卡死）。
        skip_adhoc_coverage = False
        if verdicts is not None:
            # 人工验真本期做 alias 与 synonym（spec §5）：其它 kind 仍走旧
            # truths 表单，拿 verdicts 进来必须显式 1001，不能静默当成
            # alias 口径处理。
            if kind not in ("alias", "synonym"):
                raise InvalidParameterError(
                    message="该演化类型暂不支持人工验真")
            picked_unverified = common.picked_adhoc_cases(run, picks)
            _require_verdict_coverage(picked_unverified, verdicts)
            failed = next((v for v in verdicts
                           if v.get("verdict") == "fail"), None)
            if failed is not None:
                # spec §4.4 第 7 条：失败原因写审计 + run log（沿用
                # evolution.adhoc_fail）。两者都是旁路：写失败只 warning，
                # 不改变下面的 409 行为。
                case_id = failed.get("case_id")
                try:
                    await common.audit.record(
                        self.session, tenant_id=tenant_id,
                        action="evolution.adhoc_fail", outcome="failed",
                        run_id=run.get("run_id"),
                        request_params={"verdict": "fail", "human": True,
                                        "case_id": case_id},
                        error_message="人工验真不通过")
                except Exception as exc:  # noqa: BLE001 —— 审计是旁路
                    logger.warning(
                        "人工验真失败审计写入失败 run=%s: %s",
                        run.get("run_id"), exc)
                try:
                    await common.RunStore(self.session).log(
                        tenant_id, run.get("run_id"),
                        f"adhoc_verdicts: 人工验真不通过（case_id={case_id}）")
                except Exception as exc:  # noqa: BLE001 —— 日志是旁路
                    logger.warning(
                        "人工验真失败日志写入失败 run=%s: %s",
                        run.get("run_id"), exc)
                raise ResourceConflictError(
                    message="人工验真不通过，不能采纳")
        elif (kind in ("alias", "synonym")
              and common._unverified_adhoc_cases(run)
              and not common.picked_adhoc_cases(run, picks)):
            # 兼容整键省略 `adhoc_verdicts` 的 alias / synonym 调用方：run 确实有未验真
            # 的 case，但 picks 已把它们全部排除 ⇒ 本次没有需要验真的对象。
            # `unverified` 按空集处理，门禁也不能凭空把 uncertain 升成
            # accept（没有「投票」可言）。
            # 注意：run 本来就没有 unverified case 时仍走旧 truths 覆盖校验
            # （多带 truths 依旧 1001，行为不变）。
            picked_unverified = []
            skip_adhoc_coverage = True

        # ---- scope/status：abstract 必须 active（proposed 先激活）。
        # 必须排在 parent 之前：首次采纳会拿**当前行**快照补 v0 基线，
        # proposed 行不该被落进版本（且错误文案应是「先激活」而非「没有 v0」）。
        if scope == "abstract":
            row = await common.schema_io.get_row(tenant_id, kind, kb, ds)
            schema_status = row.get("status") if isinstance(row, dict) else None
            if schema_status is not None and schema_status != "active":
                raise ResourceConflictError(
                    message=(f"抽象 schema 状态是 {schema_status!r}，"
                             "请先激活再采纳"))

        # ---- parent：必须等于该链当前 active；迁移前 run（None）走 legacy v0
        versions = common.VersionStore(self.session)
        active_version = await versions.active_for(tenant_id, kb, scope, ds)
        parent_id = run.get("parent_version_id")
        legacy = False
        if parent_id is None:
            v0 = active_version or await common._active_as_parent(
                versions, tenant_id, kb, scope, ds)
            if v0 is None:
                # 全新链从未有过版本：补一条 v0 基线，让「首次采纳」可按
                # legacy 流程落地。快照为空/读取失败时返回 None，保留原错误。
                v0 = await common._seed_baseline_version(
                    self.session, tenant_id, kind, kb, ds, scope,
                    run.get("run_id"))
            if v0 is None:
                raise ResourceConflictError(
                    message=(f"{run.get('run_id')} 没有父版本，且该版本链还"
                             "没有 v0，无法按 legacy 流程采纳"))
            parent_id = v0["version_id"]
            legacy = True
            active_version = v0
        elif (not active_version
              or active_version.get("version_id") != parent_id):
            raise ResourceConflictError(
                message=(f"{run.get('run_id')} 的父版本已不是该链 active"
                         "（stale candidate，父版本已被别的采纳推进），"
                         "请基于最新版本重跑"))

        # ---- gate / override（任何写库之前返回）
        gate = run.get("gate") if isinstance(run.get("gate"), dict) else {}
        decision = gate.get("decision")
        if decision not in common._GATE_DECISIONS:
            raise ResourceConflictError(
                message=f"门禁结论缺失或未知（{decision!r}），不能采纳")
        adhoc = run.get("gate_adhoc")
        # verdicts 路径的必填集合 = picks 选中条目关联的 case；未选中的 case
        # 不阻塞采纳（旧 truths 路径保持全量口径，行为逐字节不变）。
        # `picked_unverified=[]`（含上面的省略键兼容分支）表示「本次无需验真」。
        unverified = (picked_unverified if picked_unverified is not None
                      else common._unverified_adhoc_cases(run))
        # I1：ad-hoc 验证失败后 case 仍 `unverified`；本次带了覆盖它们的
        # 新 truths 才允许重试（与执行阶段 verify_adhoc 同口径）。
        adhoc_retry = adhoc == "reject" and bool(unverified) and bool(truths)
        if adhoc == "reject" and not adhoc_retry:
            raise ResourceConflictError(
                message="ad-hoc 补 truth 后验证失败（gate_adhoc=reject），"
                        "override 也不能采纳")
        gate_effective = common._gate_effective(decision, adhoc)
        override_applied = gate_effective != "accept" and bool(override)
        # spec §4.1 第三析取项：uncertain + unverified ad-hoc 走「补 truth 后
        # 重算」路径；ad-hoc 失败后的重试同理。
        pending_adhoc = ((decision == "uncertain" and bool(unverified))
                         or adhoc_retry)
        if (gate_effective != "accept" and not override_applied
                and not pending_adhoc):
            raise ResourceConflictError(
                message=(f"门禁结论是 {decision!r}（effective="
                         f"{gate_effective!r}），需要人工 override_reason 才能采纳"))

        # ---- ad-hoc 通道校验：truths（自动 judge）或 verdicts（人工判定）
        if verdicts is not None:
            # fail 已在 parent 之前拒绝；这里只剩 pass 的票据校验。
            # pass 必须出示与「本次 picks 选中的条目 + case」绑定的新鲜票据：
            # 换 picks / 换候选 / 超过 30 分钟都会让票据失效（spec §4.4 第 3 条）。
            for verdict in verdicts:
                if not common.check_evidence_id(
                        verdict.get("evidence_id"), entries,
                        verdict.get("case_id")):
                    raise ResourceConflictError(
                        message=(f"{verdict.get('case_id')} 的证据已过期或与"
                                 "本次候选不一致，请重新查看数据"))
            # 人工全部判通过：ad-hoc 投票等价于 accept，uncertain 可升 accept。
            # 但 picks 没有影响任何 case（空 verdicts / 无待验真对象）时没有
            # 「投票」可言，不能凭空升级门禁（uncertain 仍需 override）。
            if picked_unverified:
                gate_effective = common._gate_effective(decision, "accept")
        elif not skip_adhoc_coverage:
            # ---- ad-hoc truth 覆盖校验（只接受覆盖仍 unverified 的 case）
            common._require_adhoc_coverage(run, truths)

        return (patch, entries, kind, kb, ds, scope, active_version, legacy,
                parent_id, decision, gate_effective, override_applied)

    # ------------------------------------------------------------ ad-hoc 证据

    async def adhoc_evidence(self, tenant_id: str, run_id: str, case_id: str,
                             picks: list[str] | None = None) -> dict:
        """现场验真证据：临时 apply 候选（按 picks）→ 跑一次问句 → 恢复快照。

        只读接口（不改运行状态、不写台账），用来让用户在点「通过/不通过」前
        真的看到数据与概念全名。返回值的 `evidence_id` 绑定「选中的 entries +
        case_id + 签发时间」：改 picks / 换候选都会让旧票据失效（Task 3 校验）。

        - **必须走进程内 ②**（`in_process_asker_for`，`record_evidence=False`）：
          这是人工验真辅助，不是评测，不能污染观测/审计或受 `EVOLUTION_UPSTREAM`
          开关影响；
        - **恢复是条件式的**：apply 成功、或 apply 可能已写库（回读核对失败等
          `_apply_may_have_written` 为真）才恢复；apply 在写库前就失败（零条
          生效 / JonexException）绝不恢复 —— 否则会覆盖并发采纳赢家的写入。
          执行异常时保留 `run.backup` 的 pre 快照（唯一人工恢复源）；
          恢复失败升级成 run `failed` + 审计，绝不把半应用状态留在线上；
        - 执行异常（apply / ask）→ `ok=false` + `error`，**不签发票据**；正常
          返回（有结果、空结果或合规拒答）→ 发票据，用户仍可据此判「不通过」。
        """
        store = common.RunStore(self.session)
        run = await store.get(tenant_id, run_id)
        common._require_status(run, frozenset({"evaluated"}), "ad-hoc 验真")
        # 人工验真本期做 alias 与 synonym（spec §5；值同义词线的验真卡只显示
        # term/canonical + 真实结果）：其它 kind 的验真卡形态未实现，直接拒绝
        # 而不是拿它们去临时 apply（apply 后的数据也解释不了）。
        run_kind = str(run.get("kind") or "")
        if run_kind not in ("alias", "synonym"):
            raise InvalidParameterError(message="该演化类型暂不支持人工验真")

        cid = str(case_id or "").strip()
        if not cid:
            raise InvalidParameterError(message="case_id 不能为空")
        cases = common._unverified_adhoc_cases(run)
        case = next((item for item in cases
                     if str(item.get("case_id") or "").strip() == cid), None)
        if case is None:
            raise InvalidParameterError(
                message=f"{cid} 不是该 run 待验真的 ad-hoc case")

        active = await store.has_active(tenant_id)
        if active:
            raise ResourceConflictError(
                message="有演化运行正在进行，稍后再试")

        patch = common._patch_of(run)
        entries = common._pick_entries(patch, picks)
        kind, kb, ds = patch.get("kind"), patch.get("kb"), patch.get("ds")
        try:
            scope = common.scope_for_kind(kind)
        except ValueError as exc:
            raise InvalidParameterError(message=str(exc)) from exc

        # 证据只反映「本次真正要采纳的改动」：case 不在 picks 影响的范围内时，
        # 候选条目根本没被 apply，签发的票据会误导用户判 pass。
        picked_ids = {str(item.get("case_id") or "").strip()
                      for item in common.picked_adhoc_cases(run, picks)}
        if cid not in picked_ids:
            raise InvalidParameterError(
                message=f"{cid} 不在本次 picks 影响的 ad-hoc case 内")

        # `has_active` 只看 queued/running，**不覆盖 adopting**；同链在飞采纳
        # 会在链锁内改同一行，必须在这里先快速 409（不能白等链锁）。
        other = await store.chain_adopting(
            tenant_id, kb, kind, ds, exclude_run_id=run_id)
        if other:
            raise ResourceConflictError(
                message=f"同一版本链已有采纳正在进行（{other}），稍后再试")

        question = str(case.get("question") or "")
        all_entries = [item for item in patch.get("entries") or []
                       if isinstance(item, dict)]
        # synonym 同义词不在 schema 行上：没有「临时 apply → 恢复」这回事，
        # 只把本次选中条目的 term→canonical 作为请求级值同义词交给 ②。
        if run_kind == "synonym":
            return await self._synonym_adhoc_evidence(
                tenant_id, cid, question, entries, all_entries, run)
        entry = (_entry_for_case(entries, cid)
                 or _entry_for_case(all_entries, cid))
        alias = str((entry or {}).get("alias") or "")
        concept = str((entry or {}).get("concept") or "")

        applied: dict = {}
        outcome = None
        exec_error: Exception | None = None
        restore_error: Exception | None = None
        concept_aliases: list[str] = []
        # `rollback` 用独立副本：apply_patch 会把本批 added 记进去，不能污染
        # run.patch（证据接口是只读的，运行行其它字段一个都不改）。
        apply_doc = {**patch, "entries": entries,
                     "rollback": dict(patch.get("rollback") or {})}
        try:
            async with common.chain_lock(
                    common.advisory_key(tenant_id, kb, scope, ds)):
                # 二次守卫：锁外的快速检查可能已过期 —— 等锁期间同链可能刚
                # 受理完一次采纳（evaluated → adopting）并释放锁。拿到锁后
                # 必须重查，否则会在别人的采纳窗口里临时 apply。
                other = await store.chain_adopting(
                    tenant_id, kb, kind, ds, exclude_run_id=run_id)
                if other:
                    raise ResourceConflictError(
                        message=f"同一版本链已有采纳正在进行（{other}），稍后再试")
                # pre 必须在**锁内**取：锁外取的快照可能落后于并发采纳刚写入的
                # 内容，恢复时会把对方的改动覆盖回去（Critical #1）。
                pre = await common.schema_io.snapshot_for(
                    tenant_id, kind, kb, ds)
                if not isinstance(pre, dict) or pre.get("content") is None:
                    raise InvalidParameterError(
                        message="无法读取快照内容，暂时不能验真")
                # run 仍是 `evaluated`：把**完整 pre 快照**先落库（CAS）。这是
                # 进程在 apply 与 finally 之间崩掉时唯一的人工恢复源
                # （`restore_source` 可直接用它恢复）。
                if not await store.update(
                        run_id, tenant_id=tenant_id, backup=pre,
                        expect_statuses={"evaluated"}):
                    raise ResourceConflictError(
                        message=f"{run_id} 状态已变化，证据查询未开始")
                concept_aliases = _concept_aliases_of(pre, concept)
                wrote = False
                # apply 与 asker **分开** try：`_apply_may_have_written` 只回答
                # 「apply 是否可能已写库」。asker 抛 JonexException 时 apply 其
                # 实已经成功，若混在同一个 except 里会被误判成「未写」，恢复
                # 被跳过 → 半应用 schema 留在线上（fix round 2）。
                try:
                    applied = await common.schema_io.apply_patch(
                        tenant_id, apply_doc)
                    wrote = True
                except Exception as exc:  # noqa: BLE001 —— 证据查询失败不 500
                    exec_error = exc
                    # 写库前失败（零条生效 / 乐观锁冲突）没有可恢复内容：这时候
                    # 恢复快照反而会把并发赢家刚写的内容覆盖掉。
                    wrote = common._apply_may_have_written(exc)
                    logger.warning("ad-hoc 证据查询 apply 失败 run=%s case=%s: %s",
                                   run_id, cid, exc)
                except BaseException:
                    # 取消 / 键盘中断（CancelledError 不是 Exception）：可能正好
                    # 发生在 put_row 之后、回读之前。按「可能已写」处理，让
                    # finally 恢复快照，再原样上抛。正常异常仍走上面的语义。
                    wrote = True
                    raise
                else:
                    try:
                        # 函数内 import：测试可 monkeypatch `upstream` 源模块。
                        from .upstream import in_process_asker_for

                        ask = in_process_asker_for(
                            tenant_id, kb, common.corpus_ds(run, ds),
                            evolution_run_id=run_id)
                        outcome = await ask(question)
                    except Exception as exc:  # noqa: BLE001 —— 证据查询失败不 500
                        exec_error = exc
                        logger.warning(
                            "ad-hoc 证据查询 asker 失败 run=%s case=%s: %s",
                            run_id, cid, exc)
                finally:
                    if wrote:
                        try:
                            await common.schema_io.restore_snapshot(
                                tenant_id, common.schema_io.restore_source(
                                    pre, (applied or {}).get("backup"),
                                    scope, kb, ds))
                        except Exception as exc:  # noqa: BLE001 —— 恢复失败显式收口
                            restore_error = exc
                            logger.warning(
                                "ad-hoc 证据查询恢复快照失败 run=%s case=%s: %s",
                                run_id, cid, exc)
        except (InvalidParameterError, ResourceConflictError):
            # 参数 / 状态冲突是预期控制流（pre 内容缺失、CAS 未命中、同链在飞），
            # 直接上抛，不能收成 ok=false 或把 run 标 failed。
            raise
        except Exception as exc:  # noqa: BLE001 —— 链锁本身不可用（DB 故障等）
            exec_error = exec_error or exc
            logger.warning("ad-hoc 证据查询拿链锁失败 run=%s: %s", run_id, exc)

        data = outcome if isinstance(outcome, dict) else {}
        nested = data.get("table")
        table = nested if isinstance(nested, dict) else data
        rows = list(table.get("rows") or [])
        columns = list(table.get("columns") or [])
        out = {
            "case_id": cid,
            "question": question,
            "alias": alias,
            "concept": concept,
            "concept_aliases": concept_aliases,
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            # spec §4.4 第 6 条：人工判定只落摘要（行数 + 内容指纹），不落 rows。
            "rows_hash": common.rows_hash(rows),
            "ok": False,
            "error": None,
            "evidence_id": None,
        }
        if exec_error is None:
            if rows:
                out["ok"] = True
            else:
                # 空结果 / 拒答也是「正常返回」：给出原因，票据照发。
                out["error"] = (table.get("reason_code")
                                or table.get("answer")
                                or table.get("reason")
                                or "查询没有返回数据")
        else:
            out["error"] = str(exec_error)

        if restore_error is not None:
            # 半应用状态无法撤销：升级 run 为 failed（便于识别与人工恢复），
            # 保留 run.backup，并写 run log + 审计。
            out["ok"] = False
            out["evidence_id"] = None
            prefix = f"{out['error']}；" if out["error"] else ""
            out["error"] = f"{prefix}恢复快照失败：{restore_error}"
            try:
                # CAS evaluated：run 已被并发收口时不要把自己的失败文本盖上去。
                await store.update(
                    run_id, tenant_id=tenant_id, status="failed",
                    error=f"ad-hoc 证据查询恢复快照失败：{restore_error}",
                    expect_statuses={"evaluated"})
            except Exception as exc:  # noqa: BLE001 —— 收口失败只记日志
                logger.warning("ad-hoc 证据查询标记 failed 失败 run=%s: %s",
                               run_id, exc)
            try:
                await store.log(
                    tenant_id, run_id,
                    f"adhoc-evidence 恢复快照失败：{restore_error}")
            except Exception:  # noqa: BLE001 —— 日志是旁路
                pass
            await common.audit.record(
                self.session, tenant_id=tenant_id,
                action="evolution.adhoc_evidence_restore_fail",
                outcome="failed", run_id=run_id,
                request_params={"case_id": cid, "picks": list(picks or [])},
                error_message=str(restore_error)[:200])
            return out

        # 只有**正常返回**才清临时区。执行异常（含 asker 抛错）时保留完整
        # pre 快照：它是唯一的人工恢复源，不能因为「restore 已调用」就抹掉。
        if exec_error is None:
            try:
                await store.update(run_id, tenant_id=tenant_id, backup={},
                                   expect_statuses={"evaluated"})
            except Exception as exc:  # noqa: BLE001 —— 清理是旁路
                logger.warning("ad-hoc 证据查询清理 run.backup 失败 run=%s: %s",
                               run_id, exc)

        if exec_error is None:
            out["evidence_id"] = common.make_evidence_id(
                entries, cid, int(time.time()))
        return out

    async def _synonym_adhoc_evidence(self, tenant_id: str, case_id: str,
                                      question: str, entries: list[dict],
                                      all_entries: list[dict],
                                      run: dict) -> dict:
        """synonym 验真：**不 apply schema**，直接带请求级值同义词跑一次 ②。

        同义词存储是 `ontology_synonyms` 行，不在抽象/编译 schema 上，所以
        没有「临时应用 → 恢复快照」这回事。验真只回答一件事：把这批
        `term → canonical` 交给 ② 后，问句能否答上。必须用**进程内** ② 且
        `record_evidence=False`（人工验真辅助不是评测，不污染观测/审计）。

        返回形状与 alias 证据卡同构（`rows` / `columns` / `ok` / `error` /
        `evidence_id`），另带 synonym 特有的 `term` / `canonical`；票据仍绑定
        「本次选中的 entries + case_id」，与 `_preflight` 的校验同口径。
        """
        entry = (_entry_for_case(entries, case_id)
                 or _entry_for_case(all_entries, case_id) or {})
        terms = [str(term).strip() for term in entry.get("terms") or []
                 if str(term).strip()]
        canonical = str(entry.get("canonical") or "").strip()
        # 卡片显示用 term：优先取**与 canonical 不同**的第一个词（即真正的
        # 别名）。若原样取 terms[0]，而 terms[0] 恰好就是 canonical，卡片会
        # 渲染成「标准值 → 标准值」，把有用的别名藏起来。全部与 canonical
        # 相同才回退 terms[0]。
        term = next((item for item in terms if item != canonical), "") or (
            terms[0] if terms else "")
        # 请求级值同义词 = 本次 picks 选中的全部条目。一组内的**所有 term**
        # 都要注入（`value_aliases._merge_extra` 支持 `{terms: [...]}`）：apply
        # 后整组 term 都生效，验真只看 `terms[0]` 会对「问句用了第二个词」
        # 的组给出假的未命中。
        entry_aliases: list[dict] = [
            {"terms": [str(t).strip() for t in item.get("terms") or []
                       if str(t).strip()],
             "canonical": str(item.get("canonical") or "").strip()}
            for item in entries or []
            if isinstance(item, dict)
            and [str(t).strip() for t in item.get("terms") or []
                 if str(t).strip()]
            and str(item.get("canonical") or "").strip()
        ]

        # 取源顺序：条目自带的观测源 → 语料目标源（与 bench 臂同源）→ 空串
        # （② 自行选择）。多源 KB 上空串会 ambiguous 或跑到错误的源。
        data_source_id = str(entry.get("data_source_id") or "").strip()
        if not data_source_id:
            data_source_id = (await self._synonym_corpus_ds(tenant_id, run)
                              or "")

        outcome = None
        exec_error: Exception | None = None
        try:
            # 函数内 import：测试可 monkeypatch 源模块；也避免把 ② 依赖拖进
            # 只做纯计算/受理的路径。
            from ..nl2sql.service import Nl2SqlService
            from .upstream import EVOLUTION_USER_ID, EVOLUTION_USERNAME

            outcome = await Nl2SqlService().ask_data_source(
                tenant_id, run.get("kb"), question,
                data_source_id=data_source_id,
                user_id=EVOLUTION_USER_ID, username=EVOLUTION_USERNAME,
                record_evidence=False,
                extra_value_aliases=entry_aliases,
            )
        except Exception as exc:  # noqa: BLE001 —— 证据查询失败不 500
            exec_error = exc

        data = outcome if isinstance(outcome, dict) else {}
        nested = data.get("table")
        table = nested if isinstance(nested, dict) else data
        rows = list(table.get("rows") or [])
        columns = list(table.get("columns") or [])
        out = {
            "case_id": case_id,
            "question": question,
            "term": term,
            "terms": list(terms),
            "canonical": canonical,
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "rows_hash": common.rows_hash(rows),
            "ok": False,
            "error": None,
            "evidence_id": None,
        }
        if exec_error is None:
            if rows:
                out["ok"] = True
            else:
                out["error"] = (table.get("reason_code")
                                or table.get("answer")
                                or table.get("reason")
                                or "查询没有返回数据")
            # 正常返回（含空结果/合规拒答）才签发票据：用户仍可据此判「不通过」。
            out["evidence_id"] = common.make_evidence_id(
                entries, case_id, int(time.time()))
        else:
            out["error"] = str(exec_error)
        return out

    async def _synonym_corpus_ds(self, tenant_id: str, run: dict) -> str | None:
        """验真回退源：语料 `target.data_source_id`（与 bench 臂同口径）。

        读不到语料 / 没有目标源时返回 None（调用方回退空串，由 ② 自选）。
        这是旁路：读失败只 warning，不能把验真变成 500。
        """
        try:
            corpus = await common.load_corpus(
                self.session, tenant_id, run.get("kb"),
                common.corpus_ds(run, run.get("ds")))
        except Exception as exc:  # noqa: BLE001 —— 取源失败不阻断验真
            logger.warning("synonym 验真读取语料源失败 run=%s: %s",
                           run.get("run_id"), exc)
            return None
        target = (corpus.get("target")
                  if isinstance(corpus, dict) else None)
        if not isinstance(target, dict):
            return None
        return str(target.get("data_source_id") or "").strip() or None

    # ------------------------------------------------------------ 异步受理

    async def accept(self, tenant_id: str, run_id: str,
                     picks: list[str] | None = None, note: str = "",
                     user_id: str | None = None, *,
                     request_id: str | None = None,
                     override_reason: str | None = None,
                     adhoc_truths: list[dict] | None = None,
                     adhoc_verdicts: list[dict] | None = None) -> dict:
        """受理采纳请求：全部前置校验 + CAS `evaluated → adopting` 后**立刻返回**。

        Task 5 只落「受理」：不 apply schema、不建**采纳结果**版本、不跑 smoke、
        不写台账（这些留给 Task 6 的后台 `execute`）。例外：首次采纳会在前置
        阶段补一条 v0 基线并激活（拒绝路径不回滚已提交的基线）。CAS 同语句写
        `bench_status="pending"` 与 `bench_mode`，后台执行收口时再推进。

        链锁：前置校验（含 v0 补种）到 CAS 全程持有**非阻塞**版本链锁；同链
        正在验真 / 采纳时快速 409。锁不跨后台 `execute`（它按执行语义重新
        拿阻塞锁），避免把长基准挂在受理请求里。

        幂等：`adopting` + 同 request_id → 受理重放（不是 409）；不同
        request_id → 409。`adopt()` 本任务保持原同步实现不变。
        """
        store = common.RunStore(self.session)
        await store.recover_stale(tenant_id)
        run = await store.get(tenant_id, run_id)

        request_id = common.normalise_request_id(request_id)
        replay = self._idempotency(run, request_id)
        if replay is not None:
            return replay

        active = await store.has_active(tenant_id)
        if active:
            raise ResourceConflictError(
                message="有演化运行正在进行，稍后再试")

        override = str(override_reason or "").strip()
        truths = common.normalize_adhoc_truths(adhoc_truths)
        verdicts = common.normalize_adhoc_verdicts(adhoc_verdicts)

        common._require_status(run, common._ONLY_EVALUATED, "采纳")
        patch = common._patch_of(run)
        kind, kb, ds = patch.get("kind"), patch.get("kb"), patch.get("ds")
        try:
            scope = common.scope_for_kind(kind)
        except ValueError as exc:
            raise InvalidParameterError(message=str(exc)) from exc

        # 受理必须与证据接口 / 后台执行**串行**：v0 补种与 CAS 都是写同一
        # 条链的落库动作。用**非阻塞**链锁 —— 链上正在验真（一轮 LLM）或
        # 已有采纳时快速 409，绝不把用户请求挂在 `pg_advisory_lock` 上。
        # 锁只覆盖「前置校验 → CAS」，不跨后台 execute（它自己会再拿锁）。
        async with common.try_chain_lock(
                common.advisory_key(tenant_id, kb, scope, ds)) as acquired:
            if not acquired:
                raise ResourceConflictError(
                    message="同一版本链正在验真/采纳，稍后再试")

            (patch, entries, kind, kb, ds, scope, _active_version, legacy,
             parent_id, decision, gate_effective,
             override_applied) = await self._preflight(
                tenant_id, run, picks, override, truths, verdicts)

            # 同链不允许第二个在飞受理（链锁由 Task 6 的执行阶段持有）。
            other = await store.other_adopting(tenant_id, run_id, kb, ds, kind)
            if other:
                raise ResourceConflictError(
                    message=f"同一版本链已有采纳正在进行（{other}），稍后再试")

            # ---- 语料闸门：空语料默认阻止；读取失败只 warning（后台按
            #      executor_error 收口，不能因为读不到语料就拒绝受理）。
            corpus_size: int | None = None
            try:
                corpus = await common.load_corpus(
                    self.session, tenant_id, kb, common.corpus_ds(run, ds))
                cases = corpus.get("cases") if isinstance(corpus, dict) else None
                corpus_size = len(cases or [])
            except Exception as exc:  # noqa: BLE001 —— 读不到语料不阻断受理
                common.logger.warning("受理读取语料失败 run=%s: %s", run_id, exc)
                await common._rollback_session(self.session)
            if corpus_size == 0 and not override:
                raise ResourceConflictError(
                    message="无语料，默认阻止采纳；确需采纳请带 override_reason")

            mode = common.bench_mode()
            # ---- CAS evaluated → adopting（同语句写幂等键 / 门禁事实 / 基准待跑）
            fields: dict = {
                "gate_decision": decision,
                "gate_effective": gate_effective,
                "gate_override": 1 if override_applied else 0,
                "adopted_by": common._identity(user_id),
                # 只要本次**带了** override 理由就落库（不论是否门禁 override）：
                # 空语料 + override 这条路径门禁本身是 accept，`gate_override` 仍为
                # 0，但后台 execute（可能从恢复/另一个 worker 启动）必须能信任
                # `run.override_reason`。没带则显式清空，防残留上一次尝试的痕迹。
                "override_by": common._identity(user_id) if override else None,
                "override_reason": override or None,
                "bench_status": "pending",
                "bench_mode": mode,
                # 新一次尝试从干净状态开始：上一次尝试的基准结果 / 起止时间
                # 不能跟着这次受理留在详情页里（前端会显示上一次的通过数）。
                "bench": None,
                "bench_started_at": None,
                "bench_ended_at": None,
                # 新一次尝试从干净状态开始：上一次 stale / 执行器错误写下的
                # `error` 不能跟着成功采纳留在详情页里。
                "error": None,
            }
            if request_id is not None:
                fields["adopt_request_id"] = request_id
            if legacy:
                fields.update(parent_version_id=parent_id, legacy_adopt=1)
            try:
                ok = await store.update(
                    run_id, tenant_id=tenant_id, status="adopting",
                    expect_statuses=common._ONLY_EVALUATED, **fields)
            except IntegrityError as exc:
                raise ResourceConflictError(
                    message="该 request_id 已被其他采纳使用") from exc
            if not ok:
                raise ResourceConflictError(
                    message=f"{run_id} 状态已变化，采纳未开始")

        warnings: list[str] = []
        await common._record_audit(
            store, self.session, tenant_id=tenant_id, run_id=run_id,
            action="evolution.adopt_accept", outcome="success",
            request_params={
                "request_id": request_id,
                "gate_decision": decision,
                "gate_effective": gate_effective,
                "picked_count": len(entries),
                # Task 7：受理时基准尚未跑，三个事实如实为 pending / 0。
                "bench_status": "pending",
                "corpus_size": corpus_size,
                "new_red_count": 0,
                "bench_mode": mode,
                "override_reason": override or None,
            },
            user_id=user_id, warnings=warnings)

        return {
            "ok": True, "rc": 0,
            "accepted": True,
            "run_id": run_id,
            "status": "adopting",
            "request_id": request_id,
            "bench_status": "pending",
            "gate": run.get("gate") or None,
            "gate_effective": gate_effective,
            "gate_adhoc": run.get("gate_adhoc"),
            # 「本次是否带了 override 理由」——与 gate override 区分：空语料
            # 放行路径门禁是 accept，但人工理由同样必须让前端看见。
            "override": bool(override),
            "override_reason": override or None,
            "idempotent_replay": False,
        }

    # ------------------------------------------------------------ 采纳

    async def adopt(self, tenant_id: str, run_id: str,
                    picks: list[str] | None = None, note: str = "",
                    user_id: str | None = None, *,
                    request_id: str | None = None,
                    override_reason: str | None = None,
                    adhoc_truths: list[dict] | None = None,
                    adhoc_verdicts: list[dict] | None = None) -> dict:
        """同步采纳 = `accept()`（受理 + 前置校验 + CAS）+ `execute()`（执行）。

        Task 5 把受理抽成 `accept`；Task 6 在这里串起来，保持脚本 / 单测的
        同步语义：
        - 幂等重放 / 未受理：直接把 `accept` 的回执原样返回（重放已采纳的
          run 仍会补台账缺失告警）；
        - 新受理：内联 `execute()`（Task 9 再把执行改成后台调度）；
        - `execute()` 抛**除参数/并发冲突外**的异常：回滚会话 + 补一条
          `evolution.adopt_error` failed 审计（旁路），原始异常原样上抛。

        **入口取舍（P2，2026-10-25）**：生产 HTTP 入口**不调用本方法**——网关
        `POST /evolution/runs/{run_id}/adopt` 走 `accept()` 回 202，再由
        `_schedule_evolution_adopt` 在后台跑 `execute()`；`adopt()` 只保留给
        脚本 / 单测，用同步返回值覆盖「受理 + 执行」的完整顺序。两条写路径
        共用同一 `accept` / `execute`，不产生第二套状态机。
        """
        accepted = await self.accept(
            tenant_id, run_id, picks, note, user_id,
            request_id=request_id, override_reason=override_reason,
            adhoc_truths=adhoc_truths, adhoc_verdicts=adhoc_verdicts)
        if accepted.get("idempotent_replay") or not accepted.get("accepted"):
            if accepted.get("adopted") and accepted.get("run"):
                missing = await self._replay_artifacts(
                    tenant_id, accepted["run"])
                if missing:
                    accepted["warnings"] = missing
            return accepted
        try:
            return await self.execute(
                tenant_id, run_id, picks=picks, note=note, user_id=user_id,
                request_id=request_id, override_reason=override_reason,
                adhoc_truths=adhoc_truths,
                adhoc_verdicts=adhoc_verdicts)
        except (InvalidParameterError, ResourceConflictError):
            # 参数错误（1001/400）与状态/并发冲突（1003/409）是**预期控制流**：
            # 内部恢复漏斗 + 各自审计口径（`evolution.adhoc_fail` /
            # `bench_fail` / `smoke_fail` 等）已处理，不补通用错误审计——
            # 避免同一失败记两遍，也避免把可修正前置（如 smoke 无法执行）
            # 误记成执行器故障。
            raise
        except Exception as exc:
            # 其余异常（含 DatabaseError / DataIntegrityError / ServiceError /
            # UpstreamServiceError 等 4xxx/5xxx）都算执行侧故障：可能已让
            # 会话进入失败事务，先回滚再补一条 failed 审计（审计是旁路：
            # 写失败只 warning），原始异常**原样上抛**。
            await common._rollback_session(self.session)
            try:
                await common._record_audit(
                    common.RunStore(self.session), self.session,
                    tenant_id=tenant_id, run_id=run_id,
                    action="evolution.adopt_error", outcome="failed",
                    request_params={"request_id": request_id},
                    user_id=user_id, warnings=[],
                    error_message=str(exc)[:200])
            except Exception as audit_exc:  # noqa: BLE001 —— 审计是旁路
                common.logger.warning("采纳通用异常审计写入失败 run=%s: %s",
                               run_id, audit_exc)
            raise

    async def _replay_artifacts(self, tenant_id: str, run: dict) -> list[str]:
        """幂等重放的旁路检测：台账是否真的落过（I3）。

        正常路径在 run CAS 之后写台账；若那次写台账失败（历史版本/进程崩溃），
        同 request_id 的重放会直接命中幂等分支、永远不再补写。这里至少把
        「台账缺失」如实带回响应 `warnings`，让人工可发现（P0 不自动补写：
        重放时无法可靠区分「台账缺失」与「run 本来就没有条目」）。
        """
        run_id = run.get("run_id")
        try:
            has = await common.LedgerService(self.session).has_entries(
                tenant_id, run_id)
        except Exception as exc:  # noqa: BLE001 —— 检测失败按「未知」处理
            common.logger.warning("采纳重放检测台账失败 run=%s: %s", run_id, exc)
            await common._rollback_session(self.session)
            return []
        if not has:
            return ["台账缺失：该采纳的台账行未落库，请人工核对补记"]
        return []
    @staticmethod
    def _idempotency(run: dict, request_id: str | None):
        """request_id 幂等分支（控制器 R3 / Task 5 异步受理语义）。

        返回预先构造的结果表示「本次不再执行」；返回 None 表示继续正常流程。

        - `adopted` + 同 request_id → 原成功重放；
        - `adopting` + 同 request_id → **受理重放**（Task 5：不是 409，后台
          执行还没收口，前端继续轮询）；
        - `adopting` + 不同 request_id → 409（同一条链不能有两个在飞请求）；
        - 失败态（`smoke-failed` / `failed`）+ 同 request_id → 原失败重放。
        """
        status = run.get("status")
        same = (request_id is not None
                and run.get("adopt_request_id") == request_id)
        if status == "adopted":
            if same:
                return common._replay_success(run)
            raise ResourceConflictError(
                message="该 run 已被采纳（request_id 不同，拒绝重复采纳）",
                details={"adopted_version_id": run.get("adopted_version_id")})
        if status == "adopting":
            if same:
                return common._replay_accepted(run, request_id)
            raise ResourceConflictError(
                message="该 run 正在采纳中（不是同一个 request_id，拒绝重复受理）")
        if same and status in ("smoke-failed", "failed"):
            return common._replay_failure(run)
        return None

    # ------------------------------------------------------------ 否决

    async def reject(self, tenant_id: str, run_id: str,
                     picks: list[str] | None = None, note: str = "") -> dict:
        """否决本批：proposal_id 保序去重后逐条写 reject 台账（形状钉死）。

        `picks` 非空时只否决按 `entry_key` 选中的条目：未知键 → 1001；
        过滤后为空 → 1001（不允许退化成「全部否决」）。全空白键同理
        （`[""]` 会作为未知键落进 1001，而不是被当成「没给 picks」）。
        """
        store = common.RunStore(self.session)
        run = await store.get(tenant_id, run_id)
        common._require_status(run, common._ONLY_EVALUATED, "否决")
        patch = common._patch_of(run)
        if picks:
            entries = common._pick_entries(patch, picks)
        else:
            entries = [e for e in patch.get("entries") or []
                       if isinstance(e, dict)]
        ids = list(dict.fromkeys(
            e.get("proposal_id") for e in entries
            if e.get("proposal_id")))

        ok = await store.update(
            run_id, tenant_id=tenant_id, status="rejected",
            expect_statuses=common._ONLY_EVALUATED)
        if not ok:
            raise ResourceConflictError(message=f"{run_id} 状态已变化，否决未生效")

        if ids:
            at = common._now()
            ledger = common.LedgerService(self.session)
            try:
                for proposal_id in ids:
                    # 形状必须钉住：reject 台账只有 {"id","at","note"} 三键
                    # （Task 7b 跨任务审查的 Important）。
                    await ledger.append(
                        tenant_id, "reject",
                        {"id": proposal_id, "at": at, "note": note},
                        run_id=run_id)
            except Exception as exc:  # noqa: BLE001 —— 台账是旁路
                common.logger.warning("否决台账写入失败 run=%s: %s", run_id, exc)

        # I6：否决是候选的终态判决。
        await common._candidate_status(self.session, tenant_id, run_id, "rejected",
                                gate=run.get("gate"), supersede_others=True)

        updated = await store.get(tenant_id, run_id)
        return {
            "ok": True, "rc": 0,
            "stdout_tail": f"已否决 {len(ids)} 条",
            "run": updated,
            "patch": updated.get("patch"),
            "rejected": True,
        }

    # ------------------------------------------------------------ 条目调整

    async def adjust(self, tenant_id: str, run_id: str, entry_key: str,
                     messages: list[dict]) -> dict:
        """对话一轮修订：只读 run 产物 + 调 LLM，**不落盘**。"""
        store = common.RunStore(self.session)
        run = await store.get(tenant_id, run_id)
        common._require_status(run, common._ONLY_EVALUATED, "条目调整")
        # mapping / tool_schema / synonym 条目没有「别名/口径」这类可自由改写
        # 的文本靶点：adjust 走 LLM 改写、edit 只写 alias/expression，两者都会
        # 把条目改成非本 kind 形状。tool_schema 的 description 是**工具函数
        # 定义**的一部分，目录外的自由改写会把工具说明写歪；synonym 的靶点是
        # terms/canonical（不是 alias/expression），故与 mapping 同档显式 1001。
        _run_kind = str(run.get("kind") or "")
        if _run_kind in ("mapping", "tool_schema", "synonym"):
            raise InvalidParameterError(
                message=f"{_run_kind} 条目不支持修订/调整")
        patch = common._patch_of(run)
        entry = common._find_entry(patch, entry_key)
        # disable/enable 只翻转启用状态，没有可改写的文本靶点：带 target_code
        # 也不代表能写 expression（P1-4 遗留的 adjust/edit 入口在此显式拒绝）。
        if entry.get("op") in ("disable", "enable"):
            raise InvalidParameterError(
                message=f"{entry_key} 是 {entry.get('op')} 条目没有可修订的口径")
        if not isinstance(messages, list):
            raise InvalidParameterError(
                message="messages 必须是消息数组 [{role, content}, …]")
        built = common.build_revise_messages(entry, messages)
        if len(built) < 2:
            raise InvalidParameterError(
                message="messages 里没有一条可用的 user/assistant 消息")

        # 延迟 import：其余操作与纯函数单测不该被 openai 依赖拖下水。
        from jonex_core.common.ontology_llm import revise_evolution_entry

        out = await revise_evolution_entry(
            built, tenant_id=tenant_id, kb_id=patch.get("kb"))
        return {"ok": True, "rc": 0, "entry": entry,
                "revised": out.get("revised"),
                "rationale": out.get("rationale") or ""}

    # ------------------------------------------------------------ 条目写回

    async def edit(self, tenant_id: str, run_id: str, entry_key: str,
                   set_alias: str | None = None,
                   set_expression: str | None = None) -> dict:
        """把一条修订写回 patch（唯一写盘点）：只改 alias/expression，靶点封死。"""
        store = common.RunStore(self.session)
        run = await store.get(tenant_id, run_id)
        common._require_status(run, common._ONLY_EVALUATED, "条目调整")
        _run_kind = str(run.get("kind") or "")
        if _run_kind in ("mapping", "tool_schema"):
            raise InvalidParameterError(
                message=f"{_run_kind} 条目不支持修订/调整")
        patch = common._patch_of(run)
        entry = common._find_entry(patch, entry_key)
        # disable/enable 没有可修订的口径（同上；target_code 只是身份，不是可改文本）。
        # 守卫放在「恰给一个字段」之前：坏参数也不该掩盖 op 语义错误。
        if entry.get("op") in ("disable", "enable"):
            raise InvalidParameterError(
                message=f"{entry_key} 是 {entry.get('op')} 条目没有可修订的口径")
        if (set_alias is None) == (set_expression is None):
            raise InvalidParameterError(
                message="恰给一个字段：alias 或 expression")

        if set_alias is not None:
            if "concept" not in entry:
                raise InvalidParameterError(
                    message=f"{entry_key} 是 query_rule（用 expression）")
            new_value = str(set_alias).strip()
            if not new_value:
                raise InvalidParameterError(message="alias 不能为空")
            if len(new_value) > 32:
                raise InvalidParameterError(
                    message=f"别名超长（{len(new_value)} > 32 字；"
                            "与 nl2sql_alias_suggest 的 32 字口径对齐）")
            entry["alias"] = new_value
        else:
            # constraint 的 add/update 只按 `name` 定位：本批之前生成的旧 run
            # 计划条目没有 `target_code`，但 UI 现在也对它们开放「调整」。
            # 其他 kind 仍要求 `target_code` 才认 expression（否则是 alias）。
            is_constraint_entry = (str(run.get("kind") or "") == "constraint"
                                   and entry.get("op") in ("add", "update"))
            if "target_code" not in entry and not is_constraint_entry:
                raise InvalidParameterError(
                    message=f"{entry_key} 是 alias（用 alias）")
            new_value = str(set_expression).strip()
            if not new_value:
                raise InvalidParameterError(message="expression 不能为空")
            if len(new_value) > MAX_EXPRESSION_CHARS:
                raise InvalidParameterError(
                    message=f"expression 超长（{len(new_value)} > "
                            f"{MAX_EXPRESSION_CHARS} 字）")
            entry["expression"] = new_value

        ok = await store.update(
            run_id, tenant_id=tenant_id, patch=patch,
            expect_statuses=common._ONLY_EVALUATED)
        if not ok:
            raise ResourceConflictError(
                message=f"{run_id} 状态已变化，条目修改未生效")
        updated = await store.get(tenant_id, run_id)
        return {"ok": True, "rc": 0, "run": updated, "patch": patch}


__all__ = ["AdoptService", "build_revise_messages"]
