#!/usr/bin/env python3
# -*- coding:utf-8 -*-
"""两份 `tests/eval/nl2sql/run_eval.py` 报告的配对比较。

用法（任意机器，不连库、不出网）：

    python scripts/ab_compare.py control.json fix.json \\
        [--label-control control] [--label-fix fix] [--out compare.md]

口径（与 `.superpowers/sdd/.../progress.md` 的 A/B 台账一致）：

- **主口径 = BIRD 官方 EX**（`official_outcome`，positional tuple set）；
  平台口径（`outcome`）同时给出，避免单看一边。
- 分母 = 各自报告里的 graded（pass + fail）；**error 单列、不入分母**。
- 配对只统计「两边都是 pass/fail」的同一题：`fix` 把 `fail` 变成 `pass` = 得，
  反向 = 失；`pass→pass` / `fail→fail` = 持平。
- 延迟用每题最坏那次（`elapsed_ms`，run_eval 的汇总口径），取 p50 / p95，
  并给出相对变化；A/B 门的「p50 < 1.3×」直接看这里。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys


def _pct(n: int, d: int) -> str:
    return f"{n}/{d}（{n / d * 100:.1f}%）" if d else "0/0"


def _pct_delta(n1: int, d1: int, n2: int, d2: int) -> str:
    if not d1 or not d2:
        return "—"
    delta = (n2 / d2 - n1 / d1) * 100
    return f"{delta:+.1f}pp"


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    # 最近秩法：小样本下与人工核对一致，不引入插值歧义。
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def _load(path: str) -> tuple[dict, dict]:
    data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    rows = {r["name"]: r for r in data.get("results") or []}
    return rows, data


def _count(rows: dict, key: str) -> tuple[int, int]:
    graded = [r for r in rows.values() if r.get(key) in ("pass", "fail")]
    return sum(1 for r in graded if r[key] == "pass"), len(graded)


def compare(control: dict, fix: dict) -> str:
    lines: list[str] = []
    lines.append("# A/B 配对比较")
    lines.append("")
    lines.append(f"- control：{control['name']}（run_at={control['data']['run_at']}，"
                 f"repeat={control['data'].get('repeat')}）")
    lines.append(f"- fix：{fix['name']}（run_at={fix['data']['run_at']}，"
                 f"repeat={fix['data'].get('repeat')}）")
    lines.append("")

    for key, title in (("official_outcome", "BIRD 官方 EX"), ("outcome", "平台判分")):
        cp, cd = _count(control["rows"], key)
        fp, fd = _count(fix["rows"], key)
        lines.append(f"## {title}")
        lines.append("")
        lines.append(f"| 臂 | 通过 | 变化 |")
        lines.append("|---|---|---|")
        lines.append(f"| {control['name']} | {_pct(cp, cd)} | — |")
        lines.append(f"| {fix['name']} | {_pct(fp, fd)} | {_pct_delta(cp, cd, fp, fd)} |")
        lines.append("")

    # 配对：只用官方口径（主口径），另附平台口径的得/失。
    paired = [n for n in control["rows"]
              if n in fix["rows"]
              and control["rows"][n].get("official_outcome") in ("pass", "fail")
              and fix["rows"][n].get("official_outcome") in ("pass", "fail")]
    gained = [n for n in paired
              if control["rows"][n]["official_outcome"] == "fail"
              and fix["rows"][n]["official_outcome"] == "pass"]
    lost = [n for n in paired
            if control["rows"][n]["official_outcome"] == "pass"
            and fix["rows"][n]["official_outcome"] == "fail"]
    lines.append("## 配对（官方口径）")
    lines.append("")
    lines.append(f"- 可配对题：{len(paired)}")
    lines.append(f"- **得 {len(gained)} / 失 {len(lost)}**"
                 f"（净 {len(gained) - len(lost):+d}）")
    if gained:
        lines.append(f"- 得：{', '.join(f'`{n}`' for n in sorted(gained))}")
    if lost:
        lines.append(f"- 失：{', '.join(f'`{n}`' for n in sorted(lost))}")
    lines.append("")

    # 延迟：每题最坏那次 elapsed_ms。
    c_ms = [float(r.get("elapsed_ms") or 0) for r in control["rows"].values()]
    f_ms = [float(r.get("elapsed_ms") or 0) for r in fix["rows"].values()]
    c50, c95 = _quantile(c_ms, 0.5), _quantile(c_ms, 0.95)
    f50, f95 = _quantile(f_ms, 0.5), _quantile(f_ms, 0.95)
    ratio = (f50 / c50) if c50 else 0.0
    lines.append("## 延迟")
    lines.append("")
    lines.append("| 指标 | control | fix | 变化 |")
    lines.append("|---|---|---|---|")
    lines.append(f"| p50 | {c50:.0f} ms | {f50:.0f} ms | "
                 f"{ratio:.2f}× |" if c50 else "| p50 | — | — | — |")
    lines.append(f"| p95 | {c95:.0f} ms | {f95:.0f} ms | "
                 f"{(f95 / c95):.2f}× |" if c95 else "| p95 | — | — | — |")
    lines.append("")

    # 分 tier：官方口径。
    tiers = sorted({r.get("tier", "") for r in control["rows"].values()}
                   | {r.get("tier", "") for r in fix["rows"].values()})
    lines.append("## 分 tier（官方口径）")
    lines.append("")
    lines.append("| tier | control | fix | 变化 |")
    lines.append("|---|---|---|---|")
    for tier in tiers:
        def _tier_count(rows: dict) -> tuple[int, int]:
            rs = [r for r in rows.values()
                  if r.get("tier") == tier and r.get("official_outcome") in ("pass", "fail")]
            return sum(1 for r in rs if r["official_outcome"] == "pass"), len(rs)
        tcp, tcd = _tier_count(control["rows"])
        tfp, tfd = _tier_count(fix["rows"])
        lines.append(f"| `{tier}` | {_pct(tcp, tcd)} | {_pct(tfp, tfd)} | "
                     f"{_pct_delta(tcp, tcd, tfp, tfd)} |")
    lines.append("")

    # 分库：多语料合并后的报告带 `source`（= target.name）；单语料报告没有就不列。
    sources = sorted({r.get("source") for r in control["rows"].values() if r.get("source")}
                     | {r.get("source") for r in fix["rows"].values() if r.get("source")})
    if sources:
        lines.append("## 分库（官方口径）")
        lines.append("")
        lines.append("| 库 | control | fix | 变化 |")
        lines.append("|---|---|---|---|")
        for source in sources:
            def _source_count(rows: dict) -> tuple[int, int]:
                rs = [r for r in rows.values()
                      if r.get("source") == source
                      and r.get("official_outcome") in ("pass", "fail")]
                return sum(1 for r in rs if r["official_outcome"] == "pass"), len(rs)
            scp, scd = _source_count(control["rows"])
            sfp, sfd = _source_count(fix["rows"])
            lines.append(f"| `{source}` | {_pct(scp, scd)} | {_pct(sfp, sfd)} | "
                         f"{_pct_delta(scp, scd, sfp, sfd)} |")
        lines.append("")

    # 门判定辅助：显式写出门槛，不替人下结论。
    # ⚠️ 上面的分 tier / 分库循环复用了 cp/cd/fp/fd 这些名字，会覆盖总体计数；
    # 这里**重新取一次总体官方口径**，否则门判定会拿最后一个库的数字下结论。
    cp, cd = _count(control["rows"], "official_outcome")
    fp, fd = _count(fix["rows"], "official_outcome")
    lines.append("## 过门判定（供人工确认）")
    lines.append("")
    gate_ok = len(gained) - len(lost) >= 0 and (fp / fd - cp / cd if cd and fd else 0) >= 0.02
    lines.append(f"- 准确率门 ≥ +2pp：**{'满足' if gate_ok else '未满足'}**"
                 f"（{_pct_delta(cp, cd, fp, fd)}）")
    lines.append(f"- 延迟门 p50 < 1.3×：**{'满足' if ratio and ratio < 1.3 else '未满足'}**"
                 f"（{ratio:.2f}×）")
    lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("control")
    parser.add_argument("fix")
    parser.add_argument("--label-control", default="control")
    parser.add_argument("--label-fix", default="fix")
    parser.add_argument("--out", default=None, help="Markdown 输出路径（缺省只打印）")
    args = parser.parse_args()

    c_rows, c_data = _load(args.control)
    f_rows, f_data = _load(args.fix)
    report = compare(
        {"name": args.label_control, "rows": c_rows, "data": c_data},
        {"name": args.label_fix, "rows": f_rows, "data": f_data},
    )
    sys.stdout.write(report)
    if args.out:
        pathlib.Path(args.out).write_text(report, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
