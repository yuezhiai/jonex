# -*- coding:utf-8 -*-
"""采纳后 smoke 与 ad-hoc 补 truth 验证（演化台信任闭环 P0，spec §6 / §3.6）。

两个入口：

- `run_smoke(tenant_id, run, version, adhoc_truths, ...)`：采纳提交前的回归
  复跑。题集 = 目标题 ∩ 语料 + 非目标题固定 seed 抽样（≤10）+ verified
  ad-hoc；复用 `eval.run_eval` / `scorer.judge`，asker 已固定
  `record_evidence=False`（不写 `nl2sql_observations`）。整批 120s 上限。
- `verify_adhoc(tenant_id, run, truths, ...)`：对 `verification=unverified`
  的 ad-hoc case 补跑候选臂（正向答案 + control 真值），父本臂按「观测失败」
  记 `fail`；全部 pass → `gate_adhoc=accept`，否则 `reject`。结论回写候选
  `evidence.adhoc_cases`。

模式：`EVOLUTION_SMOKE_MODE`，默认 `enforce`；`warn` 时失败不阻断（调用方
把 `smoke_status` 记成 `warn` 并写 `evolution.smoke_warn`）；非法值按
`enforce`。无法执行（无语料/执行器异常）→ `skipped`，调用方默认阻止采纳。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from .eval import _target_case_names, plan_smoke, run_eval
from .scorer import ERROR_REASON_CODES, judge

logger = logging.getLogger(__name__)

#: 临时降级开关（spec §6 / 决策 7）：只有显式 `warn` 才降级，其余一律 enforce。
SMOKE_MODE_ENV = "EVOLUTION_SMOKE_MODE"
SMOKE_MODES = ("enforce", "warn")
#: 整批 smoke 的执行上限（秒）；超时按失败处理。
SMOKE_TIMEOUT_SECONDS = 120.0
#: 整批 ad-hoc 补 truth 的执行上限（秒，A4）。默认 120s；可用环境变量覆盖。
#: 同一次 adopt invoke 里 ad-hoc(≤120s) + smoke(≤120s) 共享预算，要求
#: `SIDECAR_PROXY_TIMEOUT ≥ 260s`（compose 默认 300s）；sidecar 代码兜底默认
#: 只有 180s（`jonex_core/sidecar/proxy.py`），不走 compose 的部署必须显式调高。
ADHOC_TIMEOUT_ENV = "EVOLUTION_ADHOC_TIMEOUT_SECONDS"
ADHOC_TIMEOUT_SECONDS = 120.0
#: 超时后的门禁理由码（写进响应 `reason`，case 保持 unverified）。
ADHOC_TIMEOUT = "adhoc_timeout"

#: 失败原因码（写进 `run.smoke.reasons`）。
TARGET_NOT_PASS = "target_not_pass"
EXPECTED_CHANGE_MISMATCH = "expected-change-mismatch"
CONTROL_REGRESSION = "control_regression"
NEW_ERROR = "new_error"
TIMEOUT = "timeout"
EXECUTOR_ERROR = "executor_error"
NO_CASES = "no_cases"


def smoke_mode(value: str | None = None) -> str:
    """`EVOLUTION_SMOKE_MODE` 解析：默认 enforce，warn 才降级，非法值忽略。"""
    raw = value if value is not None else os.environ.get(SMOKE_MODE_ENV)
    text = str(raw or "").strip().lower()
    return text if text in SMOKE_MODES else "enforce"


def adhoc_timeout(value: str | None = None) -> float:
    """`EVOLUTION_ADHOC_TIMEOUT_SECONDS` 解析：默认 120s，非法/非正数回落默认。"""
    raw = value if value is not None else os.environ.get(ADHOC_TIMEOUT_ENV)
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return ADHOC_TIMEOUT_SECONDS
    return seconds if seconds > 0 else ADHOC_TIMEOUT_SECONDS


# ---------------------------------------------------------------- 形状辅助
def candidate_record(run: dict) -> dict:
    patch = (run or {}).get("patch")
    if not isinstance(patch, dict):
        return {}
    record = patch.get("candidate")
    return record if isinstance(record, dict) else {}


def candidate_evidence(run: dict) -> dict:
    evidence = candidate_record(run).get("evidence")
    return evidence if isinstance(evidence, dict) else {}


def adhoc_cases(run: dict) -> list[dict]:
    """候选 evidence 里的 ad-hoc case（Task 3 写在 `patch.candidate.evidence`）。

    `patch.adhoc_cases` 作为旧形状兜底；非法条目丢弃。
    """
    evidence = candidate_evidence(run)
    cases = evidence.get("adhoc_cases")
    if not isinstance(cases, list):
        patch = (run or {}).get("patch")
        cases = (patch or {}).get("adhoc_cases") if isinstance(patch, dict) else None
    return [case for case in cases or []
            if isinstance(case, dict) and str(case.get("case_id") or "").strip()]


def expected_changes(run: dict) -> list[dict]:
    changes = candidate_record(run).get("expected_changes")
    return [change for change in changes or []
            if isinstance(change, dict) and str(change.get("case_id") or "")]


def _parent_report(report) -> dict:
    """`report` 可以是父本评测报告，也可以是 `run.report`（含 `parent`）。"""
    if not isinstance(report, dict):
        return {}
    parent = report.get("parent")
    if isinstance(parent, dict) and "results" in parent:
        return parent
    return report


def _parent_results(report) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for result in _parent_report(report).get("results") or []:
        if isinstance(result, dict) and result.get("name"):
            out[str(result["name"])] = result
    return out


def _sql_shape(value) -> str | None:
    """SQL 形态：大小写/空白/末尾分号归一（用于失败签名比较）。"""
    if value is None:
        return None
    text = " ".join(str(value).lower().split()).strip().rstrip(";")
    return text or None


def _signature(source) -> dict:
    """失败签名：outcome + reason_code + rejected_sql + SQL 形态（spec §6）。

    只看 reason_code 不够：补丁可能把 SQL 改成了另一种写法但仍是同一个
    reason_code。`to="changed"` 的语义是「失败方式变了」，SQL 形态也必须是
    签名的一部分（历史 Minor）。
    """
    source = source if isinstance(source, dict) else {}
    return {"outcome": source.get("outcome"),
            "reason_code": source.get("reason_code"),
            "rejected_sql": source.get("rejected_sql"),
            "sql_shape": _sql_shape(source.get("sql"))}


def _verdict_from_outcome(outcome: dict) -> str:
    """`asker` 原始返回体 → pass/fail/error（与 scorer 的 error 档同口径）。"""
    outcome = outcome or {}
    reason_code = str(outcome.get("reason_code") or "")
    if not outcome.get("ok") and reason_code in ERROR_REASON_CODES:
        return "error"
    return "pass" if outcome.get("ok") else "fail"


# ---------------------------------------------------------------- smoke
def _plan_sets(run: dict, corpus: dict,
               truths: list[dict] | None) -> tuple[list[dict], list[dict],
                                                   list[dict]]:
    """把 smoke 题集拆成 (语料题, 可判定的 ad-hoc, 仅签名检查的 ad-hoc)。

    语料题来自 `eval.plan_smoke`（目标题 ∩ 语料 + 非目标抽样）。
    ad-hoc 里带 truth（来自本次 `adhoc_truths`、或 evidence 里已 verified 且
    带 truth）的按标准 scorer 判；其余未 verified 的只做失败签名比较。
    """
    corpus_cases = list(plan_smoke(run, corpus).get("cases") or [])
    truth_by_id = {str(t.get("case_id")): t for t in truths or []
                   if isinstance(t, dict) and t.get("case_id")}
    graded: list[dict] = []
    signature_only: list[dict] = []
    for case in adhoc_cases(run):
        case_id = str(case["case_id"])
        truth = truth_by_id.get(case_id)
        if not isinstance(truth, dict):
            embedded = case.get("truth")
            truth = embedded if isinstance(embedded, dict) else None
        expect = str((truth or {}).get("expect") or "")
        control = (truth or {}).get("control")
        if expect in ("answer", "refuse") and (
                expect == "refuse"
                or (isinstance(control, list) and control)):
            graded.append({"name": case_id,
                           "question": str(case.get("question") or ""),
                           "tier": "adhoc", "expect": expect,
                           "control": list(control or [])})
        else:
            signature_only.append(case)
    return corpus_cases, graded, signature_only


def _case_entries(run: dict, corpus_cases: list[dict],
                  graded: list[dict], signature_only: list[dict]) -> list[dict]:
    targets = set(_target_case_names(run))
    entries: list[dict] = []
    for case in corpus_cases:
        name = str(case.get("name"))
        entries.append({"case_id": name,
                        "kind": "target" if name in targets else "control",
                        "question": case.get("question")})
    for case in graded:
        entries.append({"case_id": case["name"], "kind": "adhoc",
                        "question": case.get("question"),
                        "verification": "verified"})
    for case in signature_only:
        entries.append({"case_id": str(case["case_id"]), "kind": "adhoc",
                        "question": case.get("question"),
                        "verification": "unverified"})
    return entries


async def _execute_cases(tenant_id, corpus, corpus_cases, graded,
                         signature_only, asker, truth_runner) -> dict[str, dict]:
    """执行题集，返回 `{case_name: 结果}`。失败的执行异常由调用方收口。"""
    results: dict[str, dict] = {}
    if corpus_cases:
        report = await run_eval(
            tenant_id, corpus.get("target") or {},
            {**corpus, "cases": corpus_cases}, 1,
            asker=asker, truth_runner=truth_runner,
            cases=[str(case.get("name")) for case in corpus_cases])
        for result in report.get("results") or []:
            results[str(result.get("name"))] = result
    if graded:
        report = await run_eval(
            tenant_id, corpus.get("target") or {},
            {"target": corpus.get("target") or {}, "cases": graded}, 1,
            asker=asker, truth_runner=truth_runner,
            cases=[case["name"] for case in graded])
        for result in report.get("results") or []:
            results[str(result.get("name"))] = result
    for case in signature_only:
        case_id = str(case["case_id"])
        try:
            outcome = await asker(str(case.get("question") or ""))
        except Exception as exc:  # noqa: BLE001 —— 单题传输失败记 error
            outcome = {"ok": False, "reason_code": "llm_failed",
                       "why": str(exc)[:200]}
        results[case_id] = {
            "name": case_id,
            "outcome": _verdict_from_outcome(outcome),
            "reason_code": (outcome or {}).get("reason_code"),
            "rejected_sql": (outcome or {}).get("rejected_sql"),
            "sql": (outcome or {}).get("sql"),
        }
    return results


def _collect_reasons(entries: list[dict], results: dict[str, dict],
                     parents: dict[str, dict], changes: list[dict],
                     adhoc_by_id: dict[str, dict]) -> list[dict]:
    """逐条判据（spec §6）：目标题 pass / expected_changes 对齐 / 对照题回归 /
    新增 error。返回 `[{case_id, reason}]`（按出现顺序去重）。"""
    reasons: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(case_id: str, reason: str) -> None:
        key = (str(case_id), str(reason))
        if key not in seen:
            seen.add(key)
            reasons.append({"case_id": str(case_id), "reason": str(reason)})

    for entry in entries:
        case_id = entry["case_id"]
        result = results.get(case_id)
        if result is None:
            add(case_id, "not_run")
            continue
        outcome = result.get("outcome")
        parent = parents.get(case_id)
        if not isinstance(parent, dict) and entry["kind"] == "adhoc":
            # ad-hoc 的父本基线就是失败观测（不重跑父本）。
            parent = (adhoc_by_id.get(case_id) or {}).get("failure_signature")
        parent_outcome = (parent or {}).get("outcome")
        if entry["kind"] == "target" and outcome != "pass":
            add(case_id, TARGET_NOT_PASS)
        if entry["kind"] == "control" and parent_outcome == "pass" \
                and outcome == "fail":
            add(case_id, CONTROL_REGRESSION)
        if outcome == "error" and parent_outcome not in (None, "error"):
            add(case_id, NEW_ERROR)

    for change in changes:
        case_id = str(change.get("case_id"))
        result = results.get(case_id)
        target = change.get("to")
        if result is None:
            add(case_id, EXPECTED_CHANGE_MISMATCH)
            continue
        outcome = result.get("outcome")
        if target == "changed":
            baseline = parents.get(case_id) \
                or (adhoc_by_id.get(case_id) or {}).get("failure_signature")
            if not isinstance(baseline, dict):
                add(case_id, EXPECTED_CHANGE_MISMATCH)
            elif _signature(result) == _signature(baseline):
                # 失败签名没变：补丁对这条 ad-hoc 没起作用。
                add(case_id, EXPECTED_CHANGE_MISMATCH)
        elif target in ("pass", "fail", "error"):
            if outcome != target:
                add(case_id, EXPECTED_CHANGE_MISMATCH)
        else:
            add(case_id, EXPECTED_CHANGE_MISMATCH)
    return reasons


async def run_smoke(tenant_id: str, run: dict, version: dict | None,
                    adhoc_truths: list[dict] | None, *,
                    session=None, asker=None, truth_runner=None,
                    report=None,
                    timeout: float = SMOKE_TIMEOUT_SECONDS) -> dict:
    """采纳后 smoke（spec §6）。返回结果同时用于写 `run.smoke`。

    `status`: `passed` / `failed` / `skipped`；`mode`: `enforce` / `warn`。
    `skipped`（无语料/执行器异常）默认阻止采纳，由调用方按 override 放行。
    整批超过 `timeout` 秒 → `failed`（超时按失败）。
    """
    started = time.monotonic()
    mode = smoke_mode()
    patch = (run or {}).get("patch") if isinstance(run, dict) else {}
    patch = patch if isinstance(patch, dict) else {}
    kind, kb = patch.get("kind"), patch.get("kb")

    from .corpus_service import corpus_ds, load_corpus

    # KB 级 run（alias / mapping）`patch.ds` 是 None：smoke 必须和基准臂读
    # **同一个**语料源（`corpus_ds`，mapping 取 `request.bench_ds`），不能交给
    # `load_corpus` 自动挑「该 KB 按 ds_id 排序的第一个源」——多源 KB 里那会
    # 变成另一把尺子（或该源无语料 → 误报 skipped 阻断采纳）。
    ds = corpus_ds(run or {}, patch.get("ds"))

    def result(status: str, cases: list[dict], reasons: list[dict],
               **extra) -> dict:
        return {"mode": mode, "status": status, "cases": cases,
                "reasons": reasons,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "version_id": (version or {}).get("version_id"),
                **extra}

    corpus_error = False
    try:
        corpus = (await load_corpus(session, tenant_id, kb, ds)
                  if session is not None else {"cases": []})
    except Exception as exc:  # noqa: BLE001 —— 读不到语料按无法执行
        logger.warning("smoke 读取语料失败 run=%s: %s",
                       (run or {}).get("run_id"), exc)
        corpus = {"cases": []}
        corpus_error = True
    # I4：语料读取失败不能因为「还有 ad-hoc case」就当成通过 —— 目标题没跑，
    # 本次 smoke 一律 skipped（默认阻止采纳，override 可放行）。
    if corpus_error:
        return result("skipped", [],
                      [{"case_id": None, "reason": EXECUTOR_ERROR}],
                      planned=0, executed=0)

    corpus_cases, graded, signature_only = _plan_sets(
        run or {}, corpus, adhoc_truths)
    entries = _case_entries(run or {}, corpus_cases, graded, signature_only)
    if not entries:
        return result("skipped", [], [{"case_id": None, "reason": NO_CASES}],
                      planned=0, executed=0)

    if asker is None or truth_runner is None:
        from .upstream import asker_for, truth_runner_for
        eval_ds = ds or (corpus.get("target") or {}).get("data_source_id")
        asker = asker or asker_for(
            tenant_id, kb, eval_ds,
            evolution_run_id=str((run or {}).get("run_id") or ""))
        truth_runner = truth_runner or truth_runner_for(tenant_id, eval_ds)

    try:
        results = await asyncio.wait_for(
            _execute_cases(tenant_id, corpus, corpus_cases, graded,
                           signature_only, asker, truth_runner),
            timeout=timeout)
    except asyncio.TimeoutError:
        return result("failed", entries,
                      [{"case_id": None, "reason": TIMEOUT}],
                      planned=len(entries), executed=0)
    except Exception as exc:  # noqa: BLE001 —— 执行器整体起不来 → skipped
        logger.warning("smoke 执行失败 run=%s: %s",
                       (run or {}).get("run_id"), exc)
        return result("skipped", entries,
                      [{"case_id": None, "reason": EXECUTOR_ERROR}],
                      planned=len(entries), executed=0)

    if report is None:
        report = (run or {}).get("report")
    parents = _parent_results(report)
    adhoc_by_id = {str(case["case_id"]): case
                   for case in adhoc_cases(run or {})}
    reasons = _collect_reasons(entries, results, parents,
                               expected_changes(run or {}), adhoc_by_id)
    cases = []
    for entry in entries:
        case = {**entry}
        case.update(results.get(entry["case_id"]) or {})
        cases.append(case)
    return result("failed" if reasons else "passed", cases, reasons,
                  planned=len(entries), executed=len(results))


# ---------------------------------------------------------------- ad-hoc 补跑
def _adhoc_case_dict(case: dict, truth: dict) -> dict:
    """evidence case + truth → scorer 可判的 case 形状。"""
    return {"name": str(case.get("case_id")),
            "question": str(case.get("question") or ""),
            "tier": "adhoc",
            "expect": str((truth or {}).get("expect") or "answer"),
            "control": list((truth or {}).get("control") or [])}


async def verify_adhoc(tenant_id: str, run: dict, truths: list[dict] | None,
                       *, session=None, asker=None,
                       truth_runner=None) -> dict:
    """补 truth 后跑候选臂，得到 `gate_adhoc`（spec §3.6 / §4.1 step 4）。

    - 只处理 `verification=unverified` 的 ad-hoc；
    - control SQL 走 `truth_runner_for`（只读凭据）；为空/执行失败 → 该 case
      error，`gate_adhoc=reject`；
    - 候选臂 `judge(case, outcome, truths)`；父本臂按「观测失败」记 `fail`
      （不重跑父本，证据就是失败签名）；
    - 结果写回候选 `evidence.adhoc_cases`（best-effort：候选行缺失只 warning）。
    - **通过才转 `verified`**；失败保留 `verification="unverified"` 并记
      `last_verification="failed"` —— 否则失败一次后 case 变成 verified、
      重试时 `_require_adhoc_coverage` 认为「没有待补 truth 的 case」，
      新 truth 会被 1001 拒掉，用户永远补不了。

    返回 `{"gate_adhoc": "accept"|"reject"|None, "cases": [...],
    "evidence": {...}}`；没有 unverified case 时 `gate_adhoc=None`（不投票，
    避免把 `uncertain` 误升成 `accept`）。
    """
    patch = (run or {}).get("patch") if isinstance(run, dict) else {}
    patch = patch if isinstance(patch, dict) else {}
    kind, kb = patch.get("kind"), patch.get("kb")
    cases = adhoc_cases(run or {})
    unverified = [case for case in cases
                  if case.get("verification") == "unverified"]
    if not unverified:
        return {"gate_adhoc": None, "cases": [], "evidence": candidate_evidence(run or {})}

    truth_by_id = {str(t.get("case_id")): t for t in truths or []
                   if isinstance(t, dict) and t.get("case_id")}
    if asker is None or truth_runner is None:
        from .corpus_service import corpus_ds, load_corpus
        from .upstream import asker_for, truth_runner_for
        # 与 smoke / 基准臂同口径：KB 级 run（alias / mapping）先按 `corpus_ds`
        # 解析（mapping 取 `request.bench_ds`）；仍解析不到才回落语料目标。
        eval_ds = corpus_ds(run or {}, patch.get("ds"))
        if eval_ds is None and session is not None:
            try:
                corpus = await load_corpus(session, tenant_id, kb,
                                           patch.get("ds"))
                eval_ds = (corpus.get("target") or {}).get("data_source_id")
            except Exception as exc:  # noqa: BLE001 —— 仅用于解析数据源
                logger.warning("verify_adhoc 读取语料失败 run=%s: %s",
                               (run or {}).get("run_id"), exc)
        asker = asker or asker_for(
            tenant_id, kb, eval_ds,
            evolution_run_id=str((run or {}).get("run_id") or ""))
        truth_runner = truth_runner or truth_runner_for(tenant_id, eval_ds)

    async def _run_cases() -> tuple[str, list[dict]]:
        outcomes: list[dict] = []
        gate = "accept"
        for case in unverified:
            case_id = str(case["case_id"])
            truth = truth_by_id.get(case_id)
            if not isinstance(truth, dict):
                gate = "reject"
                outcomes.append({"case_id": case_id,
                                 "verification": "unverified",
                                 "last_verification": "failed",
                                 "truth": None, "parent_outcome": "fail",
                                 "outcome": "error",
                                 "why": "缺少 ad-hoc truth"})
                continue
            case_dict = _adhoc_case_dict(case, truth)
            truth_rows: list[list[list]] = []
            control_error = ""
            if case_dict["expect"] == "answer":
                for sql in case_dict["control"]:
                    try:
                        rows = await truth_runner(sql)
                    except Exception as exc:  # noqa: BLE001
                        control_error = f"真值执行失败：{str(exc)[:200]}"
                        break
                    if not rows:
                        control_error = "真值返回 0 行（control 可能过期）"
                        break
                    truth_rows.append(rows)
            if control_error:
                gate = "reject"
                outcomes.append({"case_id": case_id,
                                 "verification": "unverified",
                                 "last_verification": "failed", "truth": truth,
                                 "parent_outcome": "fail", "outcome": "error",
                                 "why": control_error})
                continue
            try:
                outcome = await asker(case_dict["question"])
            except Exception as exc:  # noqa: BLE001
                outcome = {"ok": False, "reason_code": "llm_failed"}
                verdict = {"outcome": "error",
                           "why": f"执行失败：{str(exc)[:200]}"}
            else:
                verdict = judge(case_dict, outcome or {}, truth_rows)
            passed = verdict["outcome"] == "pass"
            if not passed:
                gate = "reject"
            outcomes.append({
                "case_id": case_id,
                # I1：失败保留 unverified，重试才能再提交新 truth 覆盖它。
                "verification": "verified" if passed else "unverified",
                "last_verification": "verified" if passed else "failed",
                "truth": truth,
                "parent_outcome": "fail",
                "outcome": verdict["outcome"],
                "why": verdict["why"],
                "reason_code": (outcome or {}).get("reason_code"),
                "rejected_sql": (outcome or {}).get("rejected_sql"),
                "sql": (outcome or {}).get("sql"),
            })
        return gate, outcomes

    # A4：整批超时预算。超时按 `gate_adhoc=reject` 收口，所有 case 保持
    # `unverified`（不误标 verified），由调用方走快照恢复路径。
    budget = adhoc_timeout()
    try:
        gate, outcomes = await asyncio.wait_for(
            _run_cases(), timeout=budget)
    except asyncio.TimeoutError:
        logger.warning("ad-hoc 补 truth 整批超时 run=%s（>%.0fs）",
                       (run or {}).get("run_id"), budget)
        outcomes = [{
            "case_id": str(case["case_id"]),
            "verification": "unverified",
            "last_verification": "failed",
            "truth": truth_by_id.get(str(case["case_id"])),
            "parent_outcome": "fail",
            "outcome": "error",
            "why": f"ad-hoc 验证整批超时（>{budget:.0f}s）",
            "reason_code": ADHOC_TIMEOUT,
        } for case in unverified]
        evidence = await _write_adhoc_evidence(
            tenant_id, run, cases, outcomes, session)
        return {"gate_adhoc": "reject", "cases": outcomes,
                "evidence": evidence, "reason": ADHOC_TIMEOUT}

    # 回写候选 evidence：被验证的 case 换成 verified + 结论，其余原样保留。
    evidence = await _write_adhoc_evidence(
        tenant_id, run, cases, outcomes, session)
    return {"gate_adhoc": gate, "cases": outcomes, "evidence": evidence}


async def _write_adhoc_evidence(tenant_id: str, run: dict,
                                cases: list[dict], outcomes: list[dict],
                                session) -> dict:
    """回写候选 `evidence.adhoc_cases`（best-effort，失败只 warning）。"""
    by_id = {item["case_id"]: item for item in outcomes}
    updated_cases = []
    for case in cases:
        case_id = str(case["case_id"])
        if case_id in by_id:
            updated_cases.append({**case, **by_id[case_id]})
        else:
            updated_cases.append(case)
    evidence = {**candidate_evidence(run or {}), "adhoc_cases": updated_cases}
    try:
        from .candidate_service import CandidateService
        await CandidateService(session).update_evidence(
            tenant_id, (run or {}).get("run_id"), evidence)
    except Exception as exc:  # noqa: BLE001 —— 证据回写是旁路
        logger.warning("ad-hoc 证据回写失败 run=%s: %s",
                       (run or {}).get("run_id"), exc)
    return evidence


__all__ = [
    "ADHOC_TIMEOUT",
    "ADHOC_TIMEOUT_ENV",
    "ADHOC_TIMEOUT_SECONDS",
    "CONTROL_REGRESSION",
    "EXPECTED_CHANGE_MISMATCH",
    "NEW_ERROR",
    "SMOKE_MODES",
    "SMOKE_MODE_ENV",
    "SMOKE_TIMEOUT_SECONDS",
    "TARGET_NOT_PASS",
    "adhoc_cases",
    "adhoc_timeout",
    "candidate_evidence",
    "candidate_record",
    "expected_changes",
    "run_smoke",
    "smoke_mode",
    "verify_adhoc",
]
