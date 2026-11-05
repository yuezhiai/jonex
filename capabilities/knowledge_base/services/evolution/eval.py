# -*- coding:utf-8 -*-
"""评测执行器：逐题真值 → 问 ② → 判分。ask/truth 由调用方注入（服务/测试）。

与 tests/eval/nl2sql/run_eval.py 的逐题逻辑同口径：
control 里每条 SQL 各执行一次（`truth_runner(sql)`），收集多张真值表；
control 跑不通或真值 0 行整题记 error（不入分母）。每档 repeat 跑
max(1,repeat) 次，最坏结局为 outcome、最好为 best，error 不入分母。
"""
from __future__ import annotations

import random
import time

from .scorer import judge

_ORDER = {"error": 0, "fail": 1, "pass": 2}

#: 采纳后 smoke 的非目标题抽样上限（设计 spec §6）。
SMOKE_SAMPLE_MAX = 10


def _target_case_names(run: dict) -> list[str]:
    """run 的目标题名（`report.target_cases` 优先，patch 兜底）。

    `run_eval` 的报告本身不带 `target_cases`（目标题在 `patch.target_cases`），
    这里两种形态都接住，避免 smoke 计划因报告形状差异变成空集。
    """
    run = run or {}
    report = run.get("report") if isinstance(run.get("report"), dict) else {}
    names = report.get("target_cases")
    if not names:
        patch = run.get("patch") if isinstance(run.get("patch"), dict) else {}
        names = patch.get("target_cases")
    out: list[str] = []
    for name in names or []:
        text = str(name or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def plan_smoke(run: dict, corpus: dict | None) -> dict:
    """纯函数：生成采纳后 smoke 的题集计划（设计 spec §6 / 控制器 R7）。

    题集 = `run.report.target_cases` ∩ 语料（保序去重）
          + 非目标题抽样（按 `run_id` 固定 seed，最多 `SMOKE_SAMPLE_MAX` 条）。

    返回 `{"cases": [语料 case dict...], "estimated_runs": N}`；`estimated_runs`
    是一轮 smoke 的提问次数（每个 case 一次），Task 5 的 smoke 复用本函数。
    """
    cases = [c for c in (corpus or {}).get("cases") or []
             if isinstance(c, dict) and str(c.get("name") or "").strip()]
    by_name: dict[str, dict] = {}
    for case in cases:
        by_name.setdefault(str(case["name"]), case)

    target_names = _target_case_names(run)
    targets = [by_name[n] for n in target_names if n in by_name]
    target_set = set(target_names)

    rest = [c for c in cases if str(c["name"]) not in target_set]
    rest.sort(key=lambda c: str(c["name"]))
    rng = random.Random(str((run or {}).get("run_id") or ""))
    sampled = rest if len(rest) <= SMOKE_SAMPLE_MAX else rng.sample(
        rest, SMOKE_SAMPLE_MAX)
    sampled.sort(key=lambda c: str(c["name"]))

    planned = targets + sampled
    return {"cases": planned, "estimated_runs": len(planned)}


async def run_eval(tenant_id: str, target: dict, corpus: dict, repeat: int,
                   asker, truth_runner, *,
                   cases: list[str] | None = None) -> dict:
    """逐题评测；`cases` 非 None 时只跑这些 case 名（采纳后 smoke 的子集）。

    默认 `cases=None` 行为与之前逐字节一致（遍历语料全部 case）。
    """
    results = []
    repeat = max(1, int(repeat))
    selected = corpus.get("cases") or []
    if cases is not None:
        wanted = {str(name) for name in cases}
        selected = [case for case in selected
                    if str(case.get("name")) in wanted]
    for case in selected:
        truths: list[list[list]] = []
        control_error = ""
        if case["expect"] == "answer":
            for sql in case.get("control") or []:
                try:
                    rows = await truth_runner(sql)
                except Exception as exc:  # noqa: BLE001
                    control_error = f"真值执行失败：{str(exc)[:200]}"
                    continue
                if not rows:
                    # 真值 0 行 = 语料过期，量不出任何东西 —— 记 error（不入分母），
                    # 别让「空↔空也算匹配」把准确率顶高。
                    control_error = "真值返回 0 行（语料可能过期）"
                    continue
                truths.append(rows)
        runs = []
        for _ in range(repeat):
            started = time.monotonic()
            if control_error:
                verdict = {"outcome": "error", "why": control_error}
                outcome = {}
            else:
                try:
                    outcome = await asker(case["question"])
                except Exception as exc:  # noqa: BLE001
                    outcome = {"ok": False}
                    verdict = {"outcome": "error",
                               "why": f"HTTP/信封失败：{str(exc)[:200]}"}
                else:
                    verdict = judge(case, outcome, truths)
            runs.append({"verdict": verdict["outcome"], "why": verdict["why"],
                         "reason_code": outcome.get("reason_code"),
                         "sql": outcome.get("sql"),
                         "rejected_sql": outcome.get("rejected_sql"),
                         "row_count": outcome.get("row_count"),
                         "rows": outcome.get("rows"),
                         "elapsed_ms": int((time.monotonic() - started) * 1000)})
        worst = min(runs, key=lambda r: _ORDER[r["verdict"]])
        best = max(runs, key=lambda r: _ORDER[r["verdict"]])
        results.append({"name": case["name"], "tier": case.get("tier"),
                        "expect": case["expect"], "question": case["question"],
                        "outcome": worst["verdict"], "best": best["verdict"],
                        "reason_code": worst["reason_code"], "sql": worst["sql"],
                        "rejected_sql": worst["rejected_sql"],
                        "row_count": worst["row_count"], "why": worst["why"],
                        "runs": runs})
    graded = [r for r in results if r["outcome"] in ("pass", "fail")]
    passed = [r for r in graded if r["outcome"] == "pass"]
    by_tier: dict[str, dict] = {}
    for r in results:
        slot = by_tier.setdefault(r["tier"], {"pass": 0, "fail": 0, "error": 0})
        slot[r["outcome"]] += 1
    return {"target": target, "repeat": repeat,
            "summary": {"overall": f"{len(passed)}/{len(graded)}" if graded else "0/0",
                        "graded": len(graded), "errors": len(results) - len(graded),
                        "by_tier": by_tier},
            "results": results}
