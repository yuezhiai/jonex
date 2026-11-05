#!/usr/bin/python3
"""配对门（半自动自进化闭环）：候选 vs 父本 → accept / reject / uncertain。**纯函数**。

输入是两份 run_eval 报告（父本臂 / 候选臂，同窗口、同 repeat），逐题配对：
pass / fail / error（error 单列不入分母）。

判据（spec §六「三条硬约束」）：
  1. 配对差不降：passes_c − passes_p >= margin
  2. 目标题翻绿：每个 target_case 候选臂 == pass（require_target_green=False 时跳过）
  3a. 无新翻红：parent=pass ∧ candidate=fail 为空
  3b. 无新增 error：parent≠error ∧ candidate=error 为空
  4. 抖动面只报不判；目标题在任一臂抖动（best≠worst）或在**探针**里不稳 → uncertain

输入完整性（早退）：缺 results / 两臂题集不一致 / **error 超半**（任一臂 error 数超过半数
→ 对比不成立）→ uncertain。

档位：硬约束任一破 → reject；无硬伤但目标题抖动 → uncertain；否则 accept。
**门只出机器结论 —— 应用（adopt）永远要人跑。**
"""
from __future__ import annotations


def _outcomes(report: dict) -> dict[str, str] | None:
    results = (report or {}).get("results")
    if not isinstance(results, list):
        return None
    out: dict[str, str] = {}
    for item in results:
        if isinstance(item, dict) and item.get("name"):
            out[str(item["name"])] = str(item.get("outcome") or "error")
    return out or None


def _tiers(report: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in (report or {}).get("results") or []:
        if isinstance(item, dict) and item.get("name"):
            out[str(item["name"])] = str(item.get("tier") or "")
    return out


def _jitter(report: dict) -> list[str]:
    """best ≠ worst 的题名（repeat>1 才有意义；repeat=1 恒为空）。"""
    out: list[str] = []
    for item in (report or {}).get("results") or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        best = item.get("best")
        if best and str(best) != str(item.get("outcome")):
            out.append(str(item["name"]))
    return sorted(out)


def _names(items: list[str]) -> str:
    """题名列表 → 人话拼写（`a`、`b`）。报告节与前端 GateCard 直吃 reasons 原文，
    格式必须在源头统一（与 evolve._bulleted 同款机器标识符口径）。"""
    return "、".join(f"`{n}`" for n in items) if items else "无"


def _by_tier(parent: dict[str, str], candidate: dict[str, str],
             tiers: dict[str, str], names: list[str]) -> dict:
    slots: dict[str, dict] = {}
    for n in names:
        slot = slots.setdefault(str(tiers.get(n) or ""), {
            "parent": {"pass": 0, "fail": 0, "error": 0},
            "candidate": {"pass": 0, "fail": 0, "error": 0}})
        slot["parent"][parent[n] if parent[n] in slot["parent"] else "error"] += 1
        slot["candidate"][candidate[n] if candidate[n] in slot["candidate"] else "error"] += 1
    return slots


def _uncertain(reason: str, parent_report: dict, candidate_report: dict,
               probe: dict | None = None) -> dict:
    return {
        "decision": "uncertain", "passes": [], "delta": 0, "by_tier": {},
        "new_red": [], "new_error": [], "targets": [],
        "jitter": {"parent": _jitter(parent_report), "candidate": _jitter(candidate_report),
                   "targets": []},
        "probe": probe or {},
        "reasons": [reason],
    }


def decide(parent_report: dict, candidate_report: dict, target_cases: list[str], *,
           margin: int = 0, require_target_green: bool = True,
           probe: dict | None = None) -> dict:
    """`probe` = `{case_name: {"identical": int, "total": int}}`（父本态目标题一致率探针）。

    目标题在探针里不稳（identical < total）与臂内 best≠worst 同档：该题这次读数不可信
    → 不算它红，但整体降 uncertain（spec §六）。
    """
    parent = _outcomes(parent_report)
    candidate = _outcomes(candidate_report)
    if parent is None or candidate is None:
        return _uncertain("评测报告为空或缺少结果（评测未跑完？），无法对比", parent_report, candidate_report,
                          probe=probe)
    if set(parent) != set(candidate):
        only_p = sorted(set(parent) - set(candidate))
        only_c = sorted(set(candidate) - set(parent))
        return _uncertain(f"改动前后跑到的题集不一致：只有改动前跑到的题 {_names(only_p)}；只有改动后跑到的题 {_names(only_c)}",
                          parent_report, candidate_report, probe=probe)

    names = sorted(parent)
    errors_p = sum(1 for n in names if parent[n] == "error")
    errors_c = sum(1 for n in names if candidate[n] == "error")
    if errors_p * 2 > len(names) or errors_c * 2 > len(names):
        return _uncertain(f"报错题太多（改动前 {errors_p} 题 / 改动后 {errors_c} 题，共 {len(names)} 题）"
                          "，对比不可信", parent_report, candidate_report, probe=probe)
    passes_p = sum(1 for n in names if parent[n] == "pass")
    passes_c = sum(1 for n in names if candidate[n] == "pass")
    delta = passes_c - passes_p
    new_red = sorted(n for n in names if parent[n] == "pass" and candidate[n] == "fail")
    new_error = sorted(n for n in names if parent[n] != "error" and candidate[n] == "error")
    targets = [{"case": t, "parent": parent.get(t), "candidate": candidate.get(t),
                "ok": candidate.get(t) == "pass"} for t in target_cases or []]

    jitter_p, jitter_c = _jitter(parent_report), _jitter(candidate_report)
    probe_unstable = sorted(q for q, v in (probe or {}).items()
                            if isinstance(v, dict)
                            and int(v.get("identical", 0)) < int(v.get("total", 0)))
    target_arm_jitter = sorted(set(target_cases or []) & (set(jitter_p) | set(jitter_c)))
    target_probe_unstable = sorted(set(target_cases or []) & set(probe_unstable))
    target_jitter = sorted(set(target_arm_jitter) | set(target_probe_unstable))

    reasons: list[str] = []
    if delta < margin:
        reasons.append(f"通过数不达标：改动后 {passes_c} 题 / 改动前 {passes_p} 题"
                       f"（要求：改动后比改动前至少多 {margin} 题）")
    if require_target_green:
        # 抖动的目标题**不算它红**（这一次读数本就不可信）→ 交给下面的 uncertain 档
        red_targets = [t["case"] for t in targets if not t["ok"] and t["case"] not in target_jitter]
        if red_targets:
            reasons.append(f"目标题未通过：{_names(red_targets)}")
    if new_red:
        reasons.append(f"原来通过、改动后失败的题：{_names(new_red)}")
    if new_error:
        reasons.append(f"新增报错的题：{_names(new_error)}")

    result = {
        "decision": "accept",
        "passes": [passes_p, passes_c], "delta": delta,
        "by_tier": _by_tier(parent, candidate, _tiers(parent_report) | _tiers(candidate_report), names),
        "new_red": new_red, "new_error": new_error, "targets": targets,
        "jitter": {"parent": jitter_p, "candidate": jitter_c, "targets": target_jitter},
        "probe": probe or {},
        "reasons": reasons,
    }
    if reasons:
        result["decision"] = "reject"
    elif target_jitter:
        result["decision"] = "uncertain"
        bits = []
        if target_arm_jitter:
            bits.append(f"目标题重复跑的结论不一致：{_names(target_arm_jitter)}")
        if target_probe_unstable:
            bits.append(f"目标题在改动前的稳定性探针里就不稳：{_names(target_probe_unstable)}")
        result["reasons"] = ["；".join(bits) + " —— 建议提高重复次数重跑，或人工裁决"]
    return result
