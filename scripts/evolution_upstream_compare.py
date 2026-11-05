#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""演化台 W4 Task 4：三路上游口径对照（进程内 ② / router data-only / router mix）。

同一批语料、同一个问题，同时跑三条上游：

1. **ask** —— 进程内 `Nl2SqlService.ask_data_source`（`upstream.in_process_asker_for`，
   **与 `EVOLUTION_UPSTREAM` 开关无关**，否则对照就成了自己比自己）；
2. **data_only** —— router `POST /api/v1/knowledge-base/search/data-only`
   （`upstream.router_asker_for`，T3）；
3. **mix** —— router `POST /api/v1/knowledge-base/search/mix`，body 显式
   `save_history=False`（不污染检索历史）；只取 `data.data_blocks` 里的行，
   **不把自然语言 answer 当数据**。

⚠️ **mix 是「真实用户路径」参照，不是可比臂**：mix 是否有行取决于**本体是否
答上** —— 本体答上 → router 的 `_supplement_data` 因请求体 `save_history is
False` 不补（直接返回 `None`）；本体认输 → 数据路径照常产行（2026-10-27 125
真机 3/3 有行）。为不污染检索历史，脚本**不翻转** `save_history` —— 只把 mix
如实标成 `mix_comparable`（按 outcome.ok）并从一致率里排除（见 `compare_row` /
`summarize`）。要拿「本体答上时」的 mix 行数据，须另开一次允许写历史的对照
（不在本脚本范围）。

判分用包内 `services.evolution.scorer.judge`（与 `tests/eval/nl2sql/scorer.py`
逐字节同源的副本），真值用 `upstream.truth_runner_for` 现跑 control（与
`eval.run_eval` 同口径：`expect != "answer"` 不跑真值；control 报错 / 0 行 →
三路一律 error）。

输出每题 `{case, ask:{ok,rows,sql,reason_code,source_name,data_source_id,
elapsed_ms}, data_only:{...}, mix:{...}, agreement, mix_comparable}` 与汇总
（`ask`/`data_only` 一致率、`mix_graded` 可比题数、各臂 pass/error 数、
p50/p95）；`--out` 落 JSON。**只读**：不建 run、不写库、mix 不写检索历史。

单臂异常（连接/超时/坏 JSON）记该臂 `verdict="error"` 并带 `why`，
**绝不吞成 pass**，也不中止其余两臂。

用法（仓库根 / 容器 `/app`）：

    python scripts/evolution_upstream_compare.py \\
      --tenant tenant_jonex_demo --kb kb_assets_r2 --ds <ds_id> \\
      [--limit 5] [--out compare.json] [--router-base http://host:8010] \\
      [--evolution-run-id run_xxx]

`--evolution-run-id` 非空时，data_only / mix 两个请求体都带 `evolution_run_id`，
router 侧据此把该路合成流量打标。该 run 必须是**本租户、status ∈ {running,
adopting}** 的 run，否则 Jonex fail-open（不标记、不阻断），仍按普通流量记账；
mix 档需 router 已支持该键。脚本本身仍不建 run、不写库。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import urllib.error
import urllib.request
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

#: 三臂的固定顺序（agreement 与输出都按它）。
_ARMS = ("ask", "data_only", "mix")

#: router mix 路由（router 拦下并走真实检索链路）。
_MIX_PATH = "/api/v1/knowledge-base/search/mix"

#: mix 请求的 top_k / with_reasoning（与 `dislike_service._call_router_mix` 同款）。
_MIX_TOP_K = 5


# ---------------------------------------------------------------- 纯函数
def build_router_mix_body(question: str, kb: str,
                          evolution_run_id: str = "") -> dict:
    """router `/search/mix` 的请求体（六键契约 + 可选 `evolution_run_id`）。

    `save_history=False` 是硬约束：对照脚本跑的是合成流量，落检索历史会污染
    诊断现场时间线（设计 §D4 / 失败模式表）。`evolution_run_id` 非空时才带
    （router 据此给合成流量打标），普通调用的请求体形状不变。
    """
    body = {
        "query": question,
        "mode": "mix",
        "top_k": _MIX_TOP_K,
        "with_reasoning": True,
        "save_history": False,
        "knowledge_base_ids": [kb],
    }
    run_id = str(evolution_run_id or "").strip()
    if run_id:
        body["evolution_run_id"] = run_id
    return body


def _outcome_summary(outcome: dict | None) -> dict:
    """outcome → 输出摘要（固定七键，避免各臂形状漂移）。"""
    outcome = outcome or {}
    rows = outcome.get("rows")
    elapsed = outcome.get("elapsed_ms")
    if not isinstance(elapsed, (int, float)) or isinstance(elapsed, bool):
        elapsed = 0
    return {
        "ok": bool(outcome.get("ok")),
        "rows": rows if isinstance(rows, list) else [],
        "sql": outcome.get("sql"),
        "reason_code": outcome.get("reason_code") or "",
        "source_name": outcome.get("source_name") or "",
        "data_source_id": outcome.get("data_source_id") or "",
        "elapsed_ms": int(elapsed),
    }


def _verdict_outcome(value) -> str:
    """verdict 可以是一条字符串，也可以是 `{outcome, why}`。取 outcome（去空白）。"""
    if isinstance(value, dict):
        value = value.get("outcome")
    return str(value or "").strip()


def _verdict_values(verdicts) -> list[str]:
    """把 dict（arm→verdict）或序列统一成按 `_ARMS` 排序的 outcome 列表。

    裸字符串（如 `"aaa"`）不是「三条结局」，按缺臂处理：否则它会按字符拆成
    `["a","a","a"]`，假装三臂一致。
    """
    if isinstance(verdicts, dict):
        return [_verdict_outcome(verdicts.get(arm)) for arm in _ARMS]
    if isinstance(verdicts, (str, bytes, bytearray)):
        return []
    try:
        return [_verdict_outcome(value) for value in verdicts]
    except TypeError:
        return []


def compare_row(case_name: str, ask_outcome: dict, data_only_outcome: dict,
                mix_outcome: dict, verdicts) -> dict:
    """三路结果 → 一行报告（纯函数）。

    `agreement` **只比 `ask` 与 `data_only`**：mix 是参照臂（是否有行取决于本体
    是否答上，见模块 docstring），算进一致率会让「无可比数据」冒充「不一致」。
    两条臂都必须判出 **pass/fail** 且结局相同才算一致；error/error、空/缺臂/
    空白一律 False —— 一起报错不是「口径一致」。

    `mix_comparable` **看 mix 的 outcome 而不是 verdict**：`ok=True`（含
    `rows=[]` 的合法零行答案）才算可比；`ok=False`（router 没补数据块 / 传输
    映射成不可答等）不是。`ok=False` 的 outcome 仍可能被 scorer 判成 `fail`
    甚至 `pass`（如拒答题的 `no_candidate`），按 verdict 判会把「无数据」误当
    「有可比结果」。
    """
    values = _verdict_values(verdicts)
    primary = values[:2]
    agreement = (len(primary) == 2
                 and all(value in ("pass", "fail") for value in primary)
                 and len(set(primary)) == 1)
    return {
        "case": case_name,
        "ask": _outcome_summary(ask_outcome),
        "data_only": _outcome_summary(data_only_outcome),
        "mix": _outcome_summary(mix_outcome),
        "agreement": agreement,
        "mix_comparable": bool(isinstance(mix_outcome, dict)
                               and mix_outcome.get("ok")),
    }


def _percentile(values: list[float], p: float) -> int:
    """最近秩百分位（整数毫秒）：升序第 `ceil(p/100*n)` 个，边界收敛。"""
    if not values:
        return 0
    ordered = sorted(values)
    index = math.ceil(p / 100.0 * len(ordered)) - 1
    index = max(0, min(index, len(ordered) - 1))
    return int(ordered[index])


def summarize(rows: list[dict]) -> dict:
    """汇总：一致率、各臂 pass/error 数、耗时 p50/p95（整数毫秒）。

    pass/error 读每行的 `verdicts`（`_run_case` 会带上；`compare_row` 单独产出
    的行没有判分信息，按 0 计）。耗时把三臂所有采样混在一起取分位。

    `agreement_rate` 的分母只算 **`ask` 与 `data_only` 都判出 pass/fail 且不是
    control-error** 的题（mix 不参与一致率，见 `compare_row`）；control 跑不通时
    各臂一律 error，若计入分母会把「一起失败」当成一致而虚高；分母为 0 时记
    `0.0`（不制造 NaN）。control-error 单列 `controls_error`；mix 的可比题数
    单列 `mix_graded`（`mix_comparable` 为真且非 control-error）。
    """
    rows = list(rows or [])
    total = len(rows)
    graded = 0
    agreed = 0
    mix_graded = 0
    controls_error = 0
    pass_counts = {arm: 0 for arm in _ARMS}
    error_counts = {arm: 0 for arm in _ARMS}
    samples: list[float] = []
    for row in rows:
        verdicts = row.get("verdicts") if isinstance(row.get("verdicts"), dict) else {}
        outcomes = [_verdict_outcome(verdicts.get(arm)) for arm in _ARMS]
        row_is_control_error = bool(row.get("control_error"))
        if row_is_control_error:
            controls_error += 1
        elif all(outcome in ("pass", "fail") for outcome in outcomes[:2]):
            graded += 1
            if row.get("agreement"):
                agreed += 1
        if row.get("mix_comparable") and not row_is_control_error:
            mix_graded += 1
        for arm in _ARMS:
            outcome = _verdict_outcome(verdicts.get(arm))
            if outcome == "pass":
                pass_counts[arm] += 1
            elif outcome == "error":
                error_counts[arm] += 1
            elapsed = (row.get(arm) or {}).get("elapsed_ms")
            if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool):
                samples.append(float(elapsed))
    return {
        "cases": total,
        "graded": graded,
        "controls_error": controls_error,
        "mix_graded": mix_graded,
        "agreement_rate": (agreed / graded) if graded else 0.0,
        "pass": pass_counts,
        "error": error_counts,
        "p50_ms": _percentile(samples, 50),
        "p95_ms": _percentile(samples, 95),
    }


# ---------------------------------------------------------------- 判分 / 单题
def _judge(case: dict, outcome: dict, truths: list[list[list]]) -> dict:
    """包内 scorer（延迟 import：`--help` 不碰能力层依赖）。"""
    from capabilities.knowledge_base.services.evolution.scorer import judge

    return judge(case, outcome, truths)


async def _load_truths(case: dict, truth_runner) -> tuple[list[list[list]], str]:
    """跑 control 得真值表；返回 `(truths, control_error)`（与 `run_eval` 同口径）。"""
    truths: list[list[list]] = []
    control_error = ""
    if case.get("expect") != "answer":
        return truths, control_error
    for sql in case.get("control") or []:
        try:
            rows = await truth_runner(sql)
        except Exception as exc:  # noqa: BLE001 —— 真值跑不通整题 error
            control_error = f"真值执行失败：{str(exc)[:200]}"
            continue
        if not rows:
            # 真值 0 行 = 语料过期，量不出任何东西 —— 记 error，别让空↔空算匹配。
            control_error = "真值返回 0 行（语料可能过期）"
            continue
        truths.append(rows)
    return truths, control_error


async def _run_case(case: dict, arms: dict, truths: list[list[list]],
                    control_error: str = "") -> dict:
    """跑一题的三臂；**单臂异常只落该臂 error，不中止其余臂**。

    `arms` 是 `{arm: async callable(question) -> outcome}`——测试可注入假实现。
    """
    outcomes: dict[str, dict] = {}
    verdicts: dict[str, dict] = {}
    for arm in _ARMS:
        started = _now()
        outcome: dict = {}
        if control_error:
            verdicts[arm] = {"outcome": "error", "why": control_error}
        else:
            try:
                # 形状归一到判分都算这一臂的「处理」：畸形返回（非 dict）或 scorer
                # 抛错都只落该臂 error，绝不中止其余两臂。
                outcome = dict(await arms[arm](case.get("question") or "") or {})
                verdicts[arm] = _judge(case, outcome, truths)
            except Exception as exc:  # noqa: BLE001 —— 链路/形状/判分失败 = error
                # 判分抛错时**保留已规整的 outcome**（只有 verdict 记 error）：
                # 摘要里仍能看出该臂实际返回了什么，便于区分「链路错」与
                # 「scorer 自己崩了」。上游调用/形状归一抛错时 outcome 仍是初始 {}。
                verdicts[arm] = {
                    "outcome": "error",
                    "why": f"上游调用或判分失败：{str(exc)[:200]}",
                }
        outcome = dict(outcome or {})
        outcome["elapsed_ms"] = int((_now() - started) * 1000)
        outcomes[arm] = outcome

    row = compare_row(
        str(case.get("name") or ""),
        outcomes["ask"], outcomes["data_only"], outcomes["mix"],
        [verdicts[arm] for arm in _ARMS],
    )
    # 判分明细（含 error 的 why）与 control-error 标记随行输出；
    # `compare_row` 本体保持固定键形状（三臂摘要各七键 + case/agreement/mix_comparable）。
    row["verdicts"] = verdicts
    row["control_error"] = bool(control_error)
    return row


def _now() -> float:
    """单调时钟（单独包一层便于测试不需要；保持实现可读）。"""
    import time

    return time.monotonic()


# ---------------------------------------------------------------- mix 臂
def _post_mix(base_url: str, body_bytes: bytes, headers: dict,
              timeout: float) -> bytes:
    """阻塞 HTTP POST（模块级、测试可打桩）：返回响应体原始字节。

    由 `router_mix_asker_for` 经 `asyncio.to_thread` 调用 —— 不在事件循环线程
    里读 socket。
    """
    url = f"{str(base_url or '').rstrip('/')}{_MIX_PATH}"
    req = urllib.request.Request(
        url, data=body_bytes, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _body_excerpt(raw, limit: int = 200) -> str:
    """响应体摘要（压缩空白、截断），便于定位错配的 router base。"""
    if not raw:
        return ""
    if isinstance(raw, (bytes, bytearray)):
        text = bytes(raw).decode("utf-8", "replace")
    else:
        text = str(raw)
    return " ".join(text.split())[:limit]


def router_mix_asker_for(tenant_id: str, kb: str, *,
                         evolution_run_id: str = ""):
    """mix 臂 asker：`async def ask(question) -> dict`（outcome 与进程内同形）。

    每题现签演化 JWT（30 分钟 TTL 足够单题）；租户从 JWT 解，**不发**
    `X-Tenant-ID`（与 `dislike_service._call_router_mix` 同款）。
    `evolution_run_id` 非空时随每题请求体带给 router（合成流量打标）。

    `sub` 必须用 router 档的**数值**身份（`router_asker._router_user_id`，默认
    `"1"`）：`EVOLUTION_USER_ID="evolution"` 会被签成 `sub=0`，Jonex 的空间/KB
    过滤对 `sub=0` 不可见任何 KB —— mix 臂会「静默什么都没测」。
    """
    from capabilities.knowledge_base.services.evolution import tokens
    from capabilities.knowledge_base.services.evolution.router_adapter import (
        outcome_from_data_only,
    )
    from capabilities.knowledge_base.services.evolution.router_asker import (
        _router_user_id,
    )
    from capabilities.knowledge_base.services.evolution.upstream import (
        _router_timeout,
    )
    from jonex_core.common.config import get_config
    from jonex_core.common.exceptions import UpstreamServiceError

    run_id = str(evolution_run_id or "").strip()
    if len(run_id) > 64:
        raise ValueError("evolution_run_id 超过 64 字符")

    async def ask(question: str) -> dict:
        base = str(get_config().EVOLUTION_ROUTER_BASE or "").rstrip("/")
        token = tokens.issue_evolution_token(tenant_id, _router_user_id())
        body = build_router_mix_body(question, kb, run_id)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")

        try:
            raw = await asyncio.to_thread(
                _post_mix, base, body_bytes, headers, _router_timeout())
        except urllib.error.HTTPError as exc:
            # 只有 router 信封（含 success / data 的 JSON 对象）才交给 adapter；
            # 网关 `{code,message}` / 代理派生 JSON 是链路错，必须上抛。
            raw_body = b""
            try:
                raw_body = exc.read()
                payload = json.loads(raw_body)
            except Exception:  # noqa: BLE001 —— 读不出/非 JSON = 链路错
                payload = None
            if (isinstance(payload, dict)
                    and ("success" in payload or "data" in payload)):
                return outcome_from_data_only(payload)
            excerpt = _body_excerpt(raw_body)
            detail = f"：{excerpt}" if excerpt else ""
            raise UpstreamServiceError(
                message=f"router 返回 HTTP {exc.code}：mix 调用失败"
                        f"（响应不是 mix 信封）{detail}") from exc
        except urllib.error.URLError as exc:
            raise UpstreamServiceError(
                message=f"router 不可达：mix 调用失败（{exc.reason}）") from exc
        except (OSError, ValueError) as exc:
            raise UpstreamServiceError(
                message=f"router 不可达或响应不可解析：mix 调用失败（{exc}）") from exc

        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise UpstreamServiceError(
                message=f"router 响应不是 JSON：mix 调用失败（{exc}）") from exc
        # Jonex/router 的 mix 也是 `{success, data:{data_blocks}}` 信封：
        # 整层交给同一个 adapter（只认 data_blocks，不解析 answer 文本）。
        return outcome_from_data_only(payload)

    return ask


# ---------------------------------------------------------------- CLI
def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"必须是正整数：{value!r}")
    if number < 1:
        raise argparse.ArgumentTypeError(f"必须 ≥ 1：{value!r}")
    return number


def _run_id_arg(value: str) -> str:
    """`--evolution-run-id`：≤64 字符（与 router/② 的上限同口径）。"""
    run_id = str(value or "").strip()
    if len(run_id) > 64:
        raise argparse.ArgumentTypeError(f"evolution_run_id 不得超过 64 字符：{value!r}")
    return run_id


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="演化台三路上游口径对照（进程内 ② / router data-only / router mix）")
    parser.add_argument("--tenant", required=True, help="租户 ID")
    parser.add_argument("--kb", required=True, help="知识库 ID")
    parser.add_argument("--ds", required=True, help="数据源 ID")
    parser.add_argument("--limit", type=_positive_int, default=None,
                        help="只跑前 N 题（默认全部）")
    parser.add_argument("--out", default=None, help="把 JSON 报告写到该路径")
    parser.add_argument("--router-base", default=None,
                        help="覆盖 EVOLUTION_ROUTER_BASE（如 http://host:8010）")
    parser.add_argument(
        "--evolution-run-id", type=_run_id_arg, default="",
        help="可选：带上时 router 侧把该路合成流量打标（需是本租户、"
             "status ∈ {running, adopting} 的 run，否则 Jonex fail-open 仍按"
             "普通流量记账；mix 档需 router 已支持该键）")
    return parser


def _print_report(report: dict) -> None:
    """短表格 + 汇总（机器可读 JSON 在 `--out`）。"""
    print(f"{'case':<32} {'ask':<7} {'data_only':<11} {'mix':<7} agree")
    print("-" * 68)
    for row in report.get("rows") or []:
        verdicts = row.get("verdicts") or {}

        def outcome(arm: str) -> str:
            return str((verdicts.get(arm) or {}).get("outcome") or "?")

        # mix 是否有行取决于本体是否答上（save_history=False 只关掉 supplement）；
        # 无行时显式 n/a，不把「无可比数据」印成一次失败。
        mix_label = outcome("mix") if row.get("mix_comparable") else "n/a"
        # agreement 只在 ask/data_only 都判出 pass/fail 时才有意义：
        # error/error 等不适用场景印 n/a，而不是误导性的 no。
        primary = (outcome("ask"), outcome("data_only"))
        if all(value in ("pass", "fail") for value in primary):
            agree_label = "yes" if row.get("agreement") else "no"
        else:
            agree_label = "n/a"
        print(f"{str(row.get('case') or '')[:32]:<32} "
              f"{outcome('ask'):<7} {outcome('data_only'):<11} "
              f"{mix_label:<7} {agree_label}")
    print("-" * 68)
    print(json.dumps(report.get("summary") or {}, ensure_ascii=False, default=str))


async def run(args: argparse.Namespace) -> dict:
    """读语料 → 三路逐题 → 报告（只读，不写库/历史）。"""
    from capabilities.knowledge_base.services.evolution import upstream
    from capabilities.knowledge_base.services.evolution.corpus_service import (
        load_corpus,
    )
    from jonex_core.common.config import get_config
    from jonex_core.common.database import get_db_session

    if args.router_base:
        get_config().EVOLUTION_ROUTER_BASE = args.router_base

    async with get_db_session() as session:
        corpus = await load_corpus(session, args.tenant, args.kb, args.ds)

    cases = list((corpus or {}).get("cases") or [])
    if args.limit is not None:
        cases = cases[:args.limit]

    truth_runner = upstream.truth_runner_for(args.tenant, args.ds)
    arms = {
        # 进程内臂**不受 EVOLUTION_UPSTREAM 影响**（否则会和开关指着的臂重合）。
        "ask": upstream.in_process_asker_for(args.tenant, args.kb, args.ds),
        "data_only": upstream.router_asker_for(
            args.tenant, args.kb, args.ds,
            evolution_run_id=args.evolution_run_id),
        "mix": router_mix_asker_for(
            args.tenant, args.kb, evolution_run_id=args.evolution_run_id),
    }

    rows: list[dict] = []
    for case in cases:
        truths, control_error = await _load_truths(case, truth_runner)
        rows.append(await _run_case(case, arms, truths, control_error))

    return {
        "tenant": args.tenant,
        "kb": args.kb,
        "ds": args.ds,
        "router_base": str(get_config().EVOLUTION_ROUTER_BASE or ""),
        "summary": summarize(rows),
        "rows": rows,
    }


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(run(args))
    except Exception as exc:  # noqa: BLE001 —— CLI 边界：打印原因并给非零退出码
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    _print_report(report)
    if args.out:
        try:
            Path(args.out).write_text(
                json.dumps(report, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8")
        except OSError as exc:  # 路径不可写/磁盘满：走 CLI 错误出口，不打 traceback
            print(json.dumps(
                {"error": f"写报告失败（{args.out}）：{exc}"},
                ensure_ascii=False), file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
