# -*- coding:utf-8 -*-
"""基准判定（演化台信任闭环 P1，spec §5.3）：父本臂 vs 候选臂。**纯函数**。

输入是两臂的逐题结果（`eval.run_eval` 的 `results` 形状），不碰数据库/HTTP。
判据与 `gate.decide` 同口径：

  new_red   = 父本 pass ∧ 候选 fail（候选 error 只入 new_error）
  new_error = 父本 ≠ error ∧ 候选 == error
  passed    = 候选 passes >= 父本 passes ∧ 无 new_red ∧ 无 new_error

**有效性护栏先于比较**：`corpus_size <= 0` → `skipped/corpus_empty`；父本臂
题数与 `corpus_size` 不符、候选臂为空、或任一臂 `errors * 2 > corpus_size`
→ `skipped/executor_error`（默认阻止采纳，override 可放行）。候选臂只少几题
不算 skipped：缺失题名按 error 对齐，走 `new_error` 判失败。判定失败原因写进
`reasons`（`new_red` / `new_error` / `passes_dropped`）。

模式：`EVOLUTION_BENCH_MODE`，默认 `enforce`；`warn` 时判定失败不阻断
（调用方记 `bench_status="warn"`）；非法值按 `enforce`。

**执行侧**（Task 3）：`ask_body` / `parse_ask_envelope` / `_post_ask` /
`gateway_asker_for` 定义一条网关提问链路（`EVOLUTION_GATEWAY_BASE` →
`/api/v1/knowledge-base/data-sources/ask`，每次 ask 现签演化 JWT）；`run_bench_arm`
跑一臂 `run_eval(repeat=1)`，`run_bench_arm_with_retry` 在执行器整体抖动
（`errors * 2 > 分母`）时重跑一次。超时由 `EVOLUTION_BENCH_TIMEOUT_SECONDS`
覆盖（默认 `BENCH_TIMEOUT_SECONDS`）。

**心跳**（Task 7）：`heartbeat` 是异步上下文管理器，执行期间每
`HEARTBEAT_INTERVAL_SECONDS` 写一行 `{"stage":"bench_heartbeat"}` run log（顺带
刷新 `updated_at`，避免长跑被 `recover_stale` 误判中断）；`_write_heartbeat`
使用**独立会话**，可打桩。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import urllib.error
import urllib.request

from jonex_core.common.config import get_config

from .eval import run_eval
from .tokens import issue_evolution_token

logger = logging.getLogger(__name__)

#: 临时降级开关（spec §5.3）：只有显式 `warn` 才降级，其余一律 enforce。
BENCH_MODE_ENV = "EVOLUTION_BENCH_MODE"
BENCH_MODES = ("enforce", "warn")

#: 单次 ask 的 HTTP 超时（秒）：与 ② 单源一问的预算对齐。
BENCH_TIMEOUT_SECONDS = 90.0
#: 超时覆盖 env（Task 11 口径）：非法值 / 非正数回落到 `BENCH_TIMEOUT_SECONDS`。
BENCH_TIMEOUT_ENV = "EVOLUTION_BENCH_TIMEOUT_SECONDS"
#: 单臂最多跑几次（首次 + 一次重跑）：执行器整体抖动时的止损。
BENCH_ATTEMPT_LIMIT = 2
#: 基准臂提问的网关路径（与前端问答同一条 invoke 链路）。
BENCH_ASK_PATH = "/api/v1/knowledge-base/data-sources/ask"
#: 错误消息的截断长度（日志 / report 里不塞整段响应）。
BENCH_ERROR_MAX_CHARS = 200
#: 基准执行期间的心跳间隔（秒）：每行都是 run log + 刷新 `run.updated_at`，
#: 完整语料可能跑几十分钟，没有心跳会被 `recover_stale` 误判成中断。
HEARTBEAT_INTERVAL_SECONDS = 60.0
#: 心跳日志行：**只有 stage**，不写 SQL / 行数据。
HEARTBEAT_LINE = '{"stage":"bench_heartbeat"}'

#: 判定原因码（写进 `bench.reasons`）。
REASON_CORPUS_EMPTY = "corpus_empty"
REASON_EXECUTOR_ERROR = "executor_error"
REASON_NEW_RED = "new_red"
REASON_NEW_ERROR = "new_error"
REASON_PASSES_DROPPED = "passes_dropped"

#: 逐题落库的 SQL 文本上限（`bench.parent.cases` / `bench.candidate.cases`）。
BENCH_SQL_MAX_CHARS = 2000

#: `slim_case` 保留的键（其余一律丢弃，尤其 `rows` / `runs`）。
_SLIM_KEYS = ("name", "outcome", "best", "reason_code", "sql",
              "rejected_sql", "row_count", "elapsed_ms")
_SLIM_SQL_KEYS = ("sql", "rejected_sql")


def bench_timeout(value=None) -> float:
    """`EVOLUTION_BENCH_TIMEOUT_SECONDS` 解析：默认 90 秒，非法 / 非正数回落默认值。

    `value` 显式给值时优先（测试与调用方覆盖）；`None` 读 env。**非有限值
    （`inf` / `nan` / `-inf`）一律回落默认值**：`urllib` 的 `timeout=inf`
    会让单题请求永不超时（整臂卡死直到 30 分钟回收），配置错一个字符
    不该把采纳拖死。
    """
    raw = value if value is not None else os.environ.get(BENCH_TIMEOUT_ENV)
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        return BENCH_TIMEOUT_SECONDS
    if not math.isfinite(timeout) or timeout <= 0:
        return BENCH_TIMEOUT_SECONDS
    return timeout


def bench_mode(value: str | None = None) -> str:
    """`EVOLUTION_BENCH_MODE` 解析：默认 enforce，warn 才降级，非法值忽略。"""
    raw = value if value is not None else os.environ.get(BENCH_MODE_ENV)
    text = str(raw or "").strip().lower()
    return text if text in BENCH_MODES else "enforce"


# ---------------------------------------------------------------- 心跳
async def _write_heartbeat(tenant_id: str, run_id: str) -> None:
    """写一行心跳（`{"stage":"bench_heartbeat"}`）并刷新 `run.updated_at`。

    **打开自己的 `get_db_session()`**：执行侧的主会话正被长跑的阶段占用
    （甚至可能在链锁里），复用它写日志会让心跳和主流程的事务互相干扰。
    失败只 warning —— 心跳是旁路，绝不能让一次写日志失败打断基准/采纳。
    """
    try:
        from jonex_core.common.database import get_db_session

        from .run_store import RunStore

        async with get_db_session() as session:
            await RunStore(session).log(tenant_id, run_id, HEARTBEAT_LINE)
    except Exception as exc:  # noqa: BLE001 —— 心跳是旁路
        logger.warning("演化基准心跳写入失败 run=%s: %s", run_id, exc)


@contextlib.asynccontextmanager
async def heartbeat(tenant_id: str, run_id: str,
                    interval: float = HEARTBEAT_INTERVAL_SECONDS):
    """基准执行期间的后台心跳：每 `interval` 秒写一行 run log。

    退出时 cancel 并吞掉 `CancelledError`（正常收口不是异常）。`interval`
    非法 / 非正数回落到默认 60 秒，避免忙等。循环体自己兜异常：一次心跳
    失败不能让后续心跳停摆。
    """
    try:
        delay = float(interval)
    except (TypeError, ValueError):
        delay = HEARTBEAT_INTERVAL_SECONDS
    if not delay > 0:  # NaN / 0 / 负数都回落默认值
        delay = HEARTBEAT_INTERVAL_SECONDS

    async def _beat() -> None:
        while True:
            await asyncio.sleep(delay)
            try:
                await _write_heartbeat(tenant_id, run_id)
            except Exception as exc:  # noqa: BLE001 —— 下个周期继续
                logger.warning("演化基准心跳异常 run=%s: %s", run_id, exc)

    task = asyncio.create_task(_beat())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


# ---------------------------------------------------------------- 计数与比较
def arm_counts(results: list[dict]) -> dict:
    """单臂计数：`graded` = pass+fail（不入分母的 error 单列）。"""
    passes = graded = errors = 0
    for result in results or []:
        if not isinstance(result, dict):
            continue
        # 缺失/空 outcome 按 error 计（与 gate.decide 的 `_outcomes` 同口径）。
        outcome = str(result.get("outcome") or "error")
        if outcome == "pass":
            passes += 1
            graded += 1
        elif outcome == "fail":
            graded += 1
        elif outcome == "error":
            errors += 1
    return {"passes": passes, "graded": graded, "errors": errors}


def compare_arms(parent: list[dict], candidate: list[dict]) -> dict:
    """逐题对齐两臂。候选臂缺失的题名按 `error` 计（不能当「没跑过」放过）。"""
    rows = [r for r in parent or [] if isinstance(r, dict)]
    cand = {str(r.get("name")): r for r in candidate or []
            if isinstance(r, dict)}
    aligned = []
    for row in rows:
        name = str(row.get("name"))
        other = cand.get(name) or {"name": name}
        # 缺失/空 outcome 按 error 计（与 gate.decide 的 `_outcomes` 同口径），
        # 不能因为一行「有名字但没结论」就当它没跑过而放过。
        aligned.append({**other, "name": name,
                        "outcome": str(other.get("outcome") or "error")})
    new_red: list[str] = []
    new_error: list[str] = []
    for row, other in zip(rows, aligned):
        name = str(row.get("name"))
        parent_outcome = str(row.get("outcome") or "error")
        candidate_outcome = other["outcome"]
        # error 单独计入 new_error（与 gate.decide 同口径），不重复算翻红。
        if parent_outcome == "pass" and candidate_outcome == "fail":
            new_red.append(name)
        if parent_outcome != "error" and candidate_outcome == "error":
            new_error.append(name)
    p, c = arm_counts(parent), arm_counts(aligned)
    return {
        "passes": c["passes"], "graded": c["graded"], "errors": c["errors"],
        "new_red": new_red, "new_error": new_error,
        "passes_dropped": c["passes"] < p["passes"],
        "passed": (c["passes"] >= p["passes"]
                   and not new_red and not new_error),
    }


def bench_verdict(parent: list[dict], candidate: list[dict],
                  corpus_size: int) -> dict:
    """判定 + 有效性护栏。`status`: `passed` / `failed` / `skipped`。"""
    compare = compare_arms(parent, candidate)
    parent_counts = arm_counts(parent)
    candidate_counts = {"passes": compare["passes"],
                        "graded": compare["graded"],
                        "errors": compare["errors"]}
    try:
        size = int(corpus_size)
    except (TypeError, ValueError):
        size = 0
    if size <= 0:
        status, reasons = "skipped", [REASON_CORPUS_EMPTY]
    elif len(parent or []) != size or not candidate:
        # 父本臂没跑满语料（基线不成立）、或候选臂一行都没有（执行器整体
        # 没跑）→ 与执行器不可用同档。**候选臂只是少几题**不算 skipped：
        # 缺的题名已对齐成 error，按 new_error 判失败更安全（review R6）。
        status, reasons = "skipped", [REASON_EXECUTOR_ERROR]
    elif parent_counts["errors"] * 2 > size \
            or candidate_counts["errors"] * 2 > size:
        status, reasons = "skipped", [REASON_EXECUTOR_ERROR]
    else:
        reasons = []
        if compare["new_red"]:
            reasons.append(REASON_NEW_RED)
        if compare["new_error"]:
            reasons.append(REASON_NEW_ERROR)
        if compare["passes_dropped"]:
            reasons.append(REASON_PASSES_DROPPED)
        status = "passed" if compare["passed"] else "failed"
    return {"status": status, "reasons": reasons,
            "new_red": compare["new_red"], "new_error": compare["new_error"],
            "parent": parent_counts, "candidate": candidate_counts}


# ---------------------------------------------------------------- 抖动补跑
def merge_retry(candidate: list[dict], retry: list[dict]) -> list[dict]:
    """补跑结果按题名替换候选臂对应行，其余行与顺序保持不变。"""
    by_name = {str(r.get("name")): r for r in retry or []
               if isinstance(r, dict)}
    merged: list[dict] = []
    for row in candidate or []:
        if not isinstance(row, dict):
            merged.append(row)
            continue
        merged.append(by_name.get(str(row.get("name")), row))
    return merged


# ---------------------------------------------------------------- 落库瘦身
def slim_case(result: dict) -> dict:
    """逐题结果 → 落库形状（丢弃 `rows` / `runs`，SQL 截断）。"""
    source = result if isinstance(result, dict) else {}
    out: dict = {}
    for key in _SLIM_KEYS:
        value = source.get(key)
        if key in _SLIM_SQL_KEYS and isinstance(value, str):
            value = value[:BENCH_SQL_MAX_CHARS]
        out[key] = value
    return out


def slim_arm(report: dict) -> dict:
    """单臂报告 → `{passes, graded, errors, cases}`。

    `graded` / `errors` 优先取 `report["summary"]`（与 eval 的汇总口径一致）；
    summary 缺失时回退到 `arm_counts(report["results"])`。
    """
    report = report if isinstance(report, dict) else {}
    results = [r for r in report.get("results") or []
               if isinstance(r, dict)]
    counts = arm_counts(results)
    summary = report.get("summary")
    if isinstance(summary, dict):
        for key in ("graded", "errors"):
            value = summary.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                counts[key] = value
    return {**counts, "cases": [slim_case(r) for r in results]}


# ---------------------------------------------------------------- 执行侧
# 基准臂的问答走**平台网关**（`EVOLUTION_GATEWAY_BASE` → `/api/v1/knowledge-base/
# data-sources/ask`），与用户问答同一条 invoke 链路；不直连 router（那是点踩试跑）
# 也不进程内调服务（绕过网关 = 量不到线上链路）。身份是现场签发的演化 JWT。
def ask_body(kb: str, ds: str, question: str, run_id: str) -> dict:
    """基准臂的提问体：**恰好四键**（`evolution_run_id` 供观测归因，多一个都不带）。"""
    return {"knowledge_base_id": kb, "data_source_id": ds,
            "question": question, "evolution_run_id": run_id}


def parse_ask_envelope(raw) -> dict:
    """解析网关 `{success, data}` 信封，返回 `data`。

    非 `success`、信封不是对象、`data` 不是对象 → `RuntimeError`（`run_eval`
    会把 asker 的异常记成该题的 `error`，不入准确率分母）。消息截断 200。
    """
    try:
        doc = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"网关响应不可解析：{str(exc)[:BENCH_ERROR_MAX_CHARS]}") from exc
    # 严格 `is True`：`{"success": "false"}` 这类字符串/真值对象不能被当成成功
    # （网关契约里就是布尔，宽容比较会把失败信封静默放行成「答了」）。
    if not isinstance(doc, dict) or doc.get("success") is not True:
        detail = (doc if not isinstance(doc, dict)
                  else {"code": doc.get("code"), "message": doc.get("message")})
        raise RuntimeError(
            f"网关返回失败：{str(detail)[:BENCH_ERROR_MAX_CHARS]}")
    data = doc.get("data")
    if not isinstance(data, dict):
        raise RuntimeError(
            f"网关响应形状不对（期望对象）：{str(doc)[:BENCH_ERROR_MAX_CHARS]}")
    return data


def _post_ask(base: str, token: str, body: dict) -> dict:
    """同步 POST 一次网关 ask（模块级、可打桩）。

    **URL / 请求构造也在 try 里**：`urllib.request.Request` 对畸形 base（相对
    URL / 空串）会当场抛 `ValueError`，`json.dumps` 对不可序列化 body 抛
    `TypeError` —— 这些和 HTTP / 网络 / 解析错一样统一收成 `RuntimeError`，
    不能让裸 `ValueError` 穿出 asker 契约。
    """
    try:
        url = f"{str(base or '').rstrip('/')}{BENCH_ASK_PATH}"
        request = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(
                request, timeout=bench_timeout()) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            doc = json.loads(exc.read().decode("utf-8"))
            if isinstance(doc, dict) and doc.get("message"):
                detail = f"：{str(doc['message'])[:160]}"
        except Exception:  # noqa: BLE001 —— 读不出信封就回退到通用文案
            detail = ""
        raise RuntimeError(f"网关返回 HTTP {exc.code}{detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"网关不可达：{exc.reason}") from exc
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError(
            f"网关调用失败：{str(exc)[:BENCH_ERROR_MAX_CHARS]}") from exc
    return parse_ask_envelope(raw)


def gateway_asker_for(tenant_id: str, user_id, kb: str, ds: str, run_id: str):
    """返回 `async def(question) -> dict`：经平台网关问 ②（形状同 `/ask` 的 data）。

    **每次 ask 现签**一张 30 分钟的演化 JWT：完整语料可能跑几十分钟，只签一次
    会在后半程整臂 401（全部记 error，看起来像执行器挂了）。urllib 是阻塞调用，
    放 `asyncio.to_thread` 里跑，不卡评测的事件循环。
    """
    async def ask(question: str) -> dict:
        token = issue_evolution_token(tenant_id, user_id, 30)
        body = ask_body(kb, ds, question, run_id)
        base = str(get_config().EVOLUTION_GATEWAY_BASE or "").rstrip("/")
        return await asyncio.to_thread(_post_ask, base, token, body)

    return ask


async def run_bench_arm(tenant_id: str, *, kb: str, ds: str, corpus: dict,
                        run_id: str, user_id=None,
                        cases: list[str] | None = None, asker=None,
                        truth_runner=None) -> dict:
    """跑一臂全量语料：`asker` / `truth_runner` 缺省接网关与只读凭据真值。

    `repeat=1`（基准只量单次形态，抖动靠 `run_bench_arm_with_retry` 的重跑兜）。
    `cases` 透传给 `run_eval`：`None` = 全量（基准的默认口径），非 None 只跑子集。
    两个上游参数仅供测试注入。
    """
    if asker is None:
        asker = gateway_asker_for(tenant_id, user_id, kb, ds, run_id)
    if truth_runner is None:
        # 延迟导入：`upstream` 会拉起 nl2sql 服务（重依赖），纯函数单测不该被迫加载
        from .upstream import truth_runner_for

        truth_runner = truth_runner_for(tenant_id, ds)
    target = {"knowledge_base_id": kb, "data_source_id": ds}
    return await run_eval(tenant_id, target, corpus, 1, asker, truth_runner,
                          cases=cases)


async def run_bench_arm_with_retry(tenant_id: str, *, kb: str, ds: str,
                                   corpus: dict, run_id: str, user_id=None,
                                   cases: list[str] | None = None, asker=None,
                                   truth_runner=None, arm_runner=None) -> dict:
    """跑一臂；执行器抖动（`errors * 2 > corpus_size`）时**重跑一次**。

    返回 `{report, arm, attempt, valid}`：`arm` 是落库形状（`slim_arm`），
    `attempt` 是实际跑了几次，`valid` 表示最后一臂是否通过有效性护栏。
    判定口径与 `bench_verdict` 的 `REASON_EXECUTOR_ERROR` 同一条阈值。
    `arm_runner` 缺省 `run_bench_arm`（仅供测试注入）。
    """
    runner = arm_runner or run_bench_arm
    cases_list = [c for c in (corpus or {}).get("cases") or []
                  if isinstance(c, dict)]
    if cases is not None:
        # 子集臂（smoke / 指定题）的分母必须是**请求的子集大小**：拿全量语料当分母
        # 会把「子集全 error」判成 valid（errors*2 永远够不着全量的一半）。
        wanted = {str(name) for name in cases}
        corpus_size = sum(1 for case in cases_list
                          if str(case.get("name")) in wanted)
    else:
        corpus_size = len(cases_list)
    attempt = 0
    while True:
        attempt += 1
        report = await runner(tenant_id, kb=kb, ds=ds, corpus=corpus,
                              run_id=run_id, user_id=user_id, cases=cases,
                              asker=asker, truth_runner=truth_runner)
        arm = slim_arm(report)
        valid = arm["errors"] * 2 <= corpus_size
        if valid or attempt >= BENCH_ATTEMPT_LIMIT:
            return {"report": report, "arm": arm,
                    "attempt": attempt, "valid": valid}


__all__ = [
    "BENCH_ASK_PATH",
    "BENCH_ATTEMPT_LIMIT",
    "BENCH_ERROR_MAX_CHARS",
    "BENCH_MODES",
    "BENCH_MODE_ENV",
    "BENCH_SQL_MAX_CHARS",
    "BENCH_TIMEOUT_ENV",
    "BENCH_TIMEOUT_SECONDS",
    "HEARTBEAT_INTERVAL_SECONDS",
    "HEARTBEAT_LINE",
    "REASON_CORPUS_EMPTY",
    "REASON_EXECUTOR_ERROR",
    "REASON_NEW_ERROR",
    "REASON_NEW_RED",
    "REASON_PASSES_DROPPED",
    "arm_counts",
    "ask_body",
    "bench_mode",
    "bench_timeout",
    "bench_verdict",
    "compare_arms",
    "gateway_asker_for",
    "heartbeat",
    "merge_retry",
    "parse_ask_envelope",
    "run_bench_arm",
    "run_bench_arm_with_retry",
    "slim_arm",
    "slim_case",
]
