#!/usr/bin/env python3
# -*- coding:utf-8 -*-
"""W3.3 M4 指标编译影子对比报告（只读）。

用法（容器内，需平台 DB 环境变量）：

    python scripts/metric_shadow_report.py --tenant tenant_jonex_demo \\
        [--days 28] [--kb kb_xxx] [--ds ds_xxx] [--format md] [--out report.md]

口径：
- **覆盖率** = `exec_ok / 总行`（总行 = 开关打开期间采到的每一次真实提问）；
- `no_metric` / `not_executable` / `compile_error` / `exec_failed` 分档单列；
- 延迟只统计 `exec_ok` 的影子耗时与对应主链路耗时（p50 / p95，最近秩法）；
- 不读原始数据值（表里也没有）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import pathlib

from sqlalchemy import text

from jonex_core.common.database import get_db_session
from jonex_core.common.tenant import require_tenant

_STATUSES = ("no_metric", "not_executable", "compile_error", "exec_failed", "exec_ok")
_AGREEMENTS = ("main_failed_shadow_ok", "both_failed", "both_empty",
               "row_count_mismatch", "equal_scalar", "value_mismatch",
               "unknown", "not_comparable")


def _quantile(values: list[float], q: float) -> float:
    """最近秩法：ceil(q*n)-1（偶数样本的 p50 取较低中位数）。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
    return ordered[idx]


def build_report(rows: list[dict]) -> dict:
    """纯函数：DB 行 → 报告结构（单测直接喂 dict）。"""
    total = len(rows)
    status_counts = {status: 0 for status in _STATUSES}
    agreement_counts = {agreement: 0 for agreement in _AGREEMENTS}
    reason_counts: dict[str, int] = {}
    per_metric: dict[str, dict] = {}
    shadow_latencies: list[float] = []
    main_latencies: list[float] = []
    for row in rows:
        status = str(row.get("status") or "")
        agreement = str(row.get("agreement") or "")
        if status in status_counts:
            status_counts[status] += 1
        if agreement in agreement_counts:
            agreement_counts[agreement] += 1
        scene = row.get("scene") or {}
        if isinstance(scene, str):
            try:
                scene = json.loads(scene)
            except ValueError:
                scene = {}
        if status in ("compile_error", "exec_failed") and isinstance(scene, dict):
            reason = str(scene.get("reason") or "")
            if reason:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
        # 按指标归因要覆盖失败样本：compile_error/exec_failed 行也带 metric_name，
        # 否则「哪个指标定义坏了」永远统计不出来。
        name = str(row.get("metric_name") or "")
        if name:
            bucket = per_metric.setdefault(
                name, {"name": name, "total": 0, "exec_ok": 0,
                       "equal_scalar": 0, "value_mismatch": 0})
            bucket["total"] += 1
            if status == "exec_ok":
                bucket["exec_ok"] += 1
                if agreement in ("equal_scalar", "value_mismatch"):
                    bucket[agreement] += 1
        if status == "exec_ok":
            if row.get("shadow_elapsed_ms") is not None:
                shadow_latencies.append(float(row["shadow_elapsed_ms"]))
            if row.get("main_elapsed_ms") is not None:
                main_latencies.append(float(row["main_elapsed_ms"]))
    for bucket in per_metric.values():
        bucket["exec_rate"] = (bucket["exec_ok"] / bucket["total"]
                               if bucket["total"] else 0.0)
    return {
        "total": total,
        "coverage": (status_counts["exec_ok"] / total) if total else 0.0,
        "status": status_counts,
        "agreement": agreement_counts,
        "reasons": dict(sorted(reason_counts.items(),
                               key=lambda item: (-item[1], item[0]))),
        "latency_ms": {
            "shadow_p50": _quantile(shadow_latencies, 0.50),
            "shadow_p95": _quantile(shadow_latencies, 0.95),
            "main_p50": _quantile(main_latencies, 0.50),
            "main_p95": _quantile(main_latencies, 0.95),
        },
        "metrics": sorted(per_metric.values(),
                          key=lambda item: (-item["exec_ok"], item["name"])),
    }


async def _fetch(*, tenant_id: str, days: int, kb_id: str, ds_id: str) -> list[dict]:
    where = ["tenant_id = :tenant_id",
             "created_at >= now() - (:days || ' days')::interval"]
    params: dict = {"tenant_id": tenant_id, "days": str(days)}
    if kb_id:
        where.append("knowledge_base_id = :kb_id")
        params["kb_id"] = kb_id
    if ds_id:
        where.append("data_source_id = :ds_id")
        params["ds_id"] = ds_id
    sql = ("SELECT status, agreement, metric_name, metric_type, main_outcome, "
           "main_reason_code, shadow_elapsed_ms, main_elapsed_ms, scene "
           "FROM knowledge_base.nl2sql_metric_shadow WHERE " + " AND ".join(where) +
           " ORDER BY created_at DESC LIMIT 50000")
    async with get_db_session() as session:
        result = await session.execute(text(sql), params)
        return [dict(row) for row in result.mappings()]


def _fmt(value: float) -> str:
    return f"{value:.1f}"


def render_markdown(report: dict) -> str:
    lines = [f"# 指标影子对比报告", "",
             f"- 样本：**{report['total']}** 次提问",
             f"- 覆盖率（exec_ok）：**{report['coverage'] * 100:.1f}%**", "",
             "## 状态分布", "", "| status | count |", "|---|---|"]
    lines += [f"| {key} | {value} |" for key, value in report["status"].items()]
    lines += ["", "## 差异口径", "", "| agreement | count |", "|---|---|"]
    lines += [f"| {key} | {value} |" for key, value in report["agreement"].items()]
    lines += ["", "## 失败原因", "", "| reason | count |", "|---|---|"]
    lines += [f"| {key} | {value} |" for key, value in report["reasons"].items()] or ["| — | 0 |"]
    latency = report["latency_ms"]
    lines += ["", "## 延迟（ms，exec_ok）", "",
              f"- 影子 p50 / p95：{_fmt(latency['shadow_p50'])} / {_fmt(latency['shadow_p95'])}",
              f"- 主链路 p50 / p95：{_fmt(latency['main_p50'])} / {_fmt(latency['main_p95'])}"]
    lines += ["", "## 按指标", "",
              "| metric | total | exec_ok | exec_rate | equal_scalar | value_mismatch |",
              "|---|---|---|---|---|---|"]
    lines += [f"| {item['name'] or '—'} | {item['total']} | {item['exec_ok']} | "
              f"{item['exec_rate'] * 100:.1f}% | {item['equal_scalar']} | "
              f"{item['value_mismatch']} |"
              for item in report["metrics"]] or ["| — | 0 | 0 | 0.0% | 0 | 0 |"]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="指标编译影子对比报告")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--days", type=int, default=28)
    parser.add_argument("--kb", default="")
    parser.add_argument("--ds", default="")
    parser.add_argument("--format", choices=("json", "md"), default="json")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    days = max(1, min(int(args.days), 90))
    rows = asyncio.run(_fetch(
        tenant_id=require_tenant(args.tenant), days=days,
        kb_id=args.kb, ds_id=args.ds,
    ))
    report = build_report(rows)
    payload = (render_markdown(report) if args.format == "md"
               else json.dumps(report, ensure_ascii=False, indent=2))
    if args.out:
        pathlib.Path(args.out).write_text(payload, encoding="utf-8")
    else:
        print(payload)


if __name__ == "__main__":
    main()
