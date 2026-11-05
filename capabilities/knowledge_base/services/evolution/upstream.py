# -*- coding:utf-8 -*-
"""评测上游接线：进程内 ② 问答 + 真值 SQL + 父本稳定性探针。

替代旧 CLI 的 HTTP 登录与子进程：
- `asker_for` → `Nl2SqlService.ask_data_source`（与 router 场景同一条服务链路）；
- `truth_runner_for` → `DataSourceService.query_config`（**只读凭据**）+ `SchemaSource.run_query`；
- `probe_targets` → 父本态下目标题的一致率（与 `flake_probe` 同一套计数口径）。

真值绝不回退到写凭据：`query_config` 没登记只读账号时返回 `None`，这里直接抛
`RuntimeError` —— 一次回退就是拿写账号跑模型生成的 SQL。
"""
from __future__ import annotations

import logging

from jonex_core.common.config import get_config

from ..data_source_service import DataSourceService
from ..nl2sql.service import Nl2SqlService

logger = logging.getLogger(__name__)

#: 真值 SQL 的单条执行超时（秒）；与旧 CLI 的 `run_eval.py` 同口径。
TRUTH_TIMEOUT_SECONDS = 90

#: `EVOLUTION_UPSTREAM` 的合法档位；其余值一律回落 `ask`。
_UPSTREAM_MODES = frozenset({"ask", "router"})

#: router 档超时的兜底值（秒）。
DEFAULT_ROUTER_TIMEOUT = 120

#: 演化运行发起 ② 问答时的身份（审计/观测里可辨识；不是真实用户）。
EVOLUTION_USER_ID = "evolution"
EVOLUTION_USERNAME = "evolution"

#: 探针每次运行次数（与旧 `_probe_targets` 一致）。
PROBE_REPEAT = 5


def _upstream_mode() -> str:
    """当前评测上游档位：`ask`（默认）或 `router`。

    非法值（含空串、未知档位）回落 `ask` 并告警——这是「配了却静默走另一条链路」
    的防线，宁可噪声也不要悄悄跑错上游。
    """
    raw = getattr(get_config(), "EVOLUTION_UPSTREAM", "ask")
    mode = str(raw or "").strip().lower()
    if mode not in _UPSTREAM_MODES:
        logger.warning(
            "未知的 EVOLUTION_UPSTREAM=%r，回落到 ask（合法值: ask|router）", raw)
        return "ask"
    return mode


def _router_timeout() -> int:
    """router 档单次问答超时（秒）；非整数 / 非正数回落 120。"""
    raw = getattr(get_config(), "EVOLUTION_ROUTER_TIMEOUT", DEFAULT_ROUTER_TIMEOUT)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "非法的 EVOLUTION_ROUTER_TIMEOUT=%r，回落到 %d 秒",
            raw, DEFAULT_ROUTER_TIMEOUT)
        return DEFAULT_ROUTER_TIMEOUT
    if value <= 0:
        logger.warning(
            "非正的 EVOLUTION_ROUTER_TIMEOUT=%r，回落到 %d 秒",
            raw, DEFAULT_ROUTER_TIMEOUT)
        return DEFAULT_ROUTER_TIMEOUT
    return value


def router_asker_for(tenant_id: str, kb: str, ds: str | None, *,
                     evolution_run_id: str = ""):
    """router 档 asker（W4）：延迟 import，T3 才建 `router_asker` 模块。

    模块级包装而非内联 import，便于测试 monkeypatch `upstream.router_asker_for`。
    `evolution_run_id` 原样透传（归一见 `router_asker.router_asker_for`）。
    """
    from .router_asker import router_asker_for as _router_asker_for

    return _router_asker_for(tenant_id, kb, ds,
                             evolution_run_id=evolution_run_id)


def in_process_asker_for(tenant_id: str, kb: str, ds: str | None, *,
                         evolution_run_id: str = ""):
    """进程内 ② asker：**与 `EVOLUTION_UPSTREAM` 开关无关**。

    对照脚本要同时跑「进程内 / router data-only / router mix」三路，不能因为
    开关指着 router 就把进程内那一臂也切走 —— 所以把闭包体拆到这里，
    `asker_for` 的 `ask` 分支只做转发（行为逐字节不变）。

    `evolution_run_id` 仅为签名统一而接受、**不转发**：进程内评测走
    `record_evidence=False`，本就不写观测/审计，传下去反而每题多一次归属校验。
    """
    async def ask(question: str) -> dict:
        # 评测是「同一批题反复问」的合成流量：写审计/观测会把真实用户提问的
        # 失败归因与采纳率统计冲淡（`record_evidence=False` 时两边都不落）。
        return await Nl2SqlService().ask_data_source(
            tenant_id, kb, question,
            data_source_id=ds or "",
            user_id=EVOLUTION_USER_ID, username=EVOLUTION_USERNAME,
            record_evidence=False,
        )

    return ask


def asker_for(tenant_id: str, kb: str, ds: str | None, *,
              evolution_run_id: str = ""):
    """返回 `async def(question) -> dict`：按 `EVOLUTION_UPSTREAM` 分派上游。"""
    if _upstream_mode() == "router":
        return router_asker_for(tenant_id, kb, ds,
                                evolution_run_id=evolution_run_id)
    return in_process_asker_for(tenant_id, kb, ds,
                                evolution_run_id=evolution_run_id)


def truth_runner_for(tenant_id: str, ds: str | None):
    """返回 `async def(sql) -> list[list]`：用**只读**凭据跑真值 SQL。"""
    async def truth(sql: str) -> list[list]:
        resolved = await DataSourceService().query_config(tenant_id, ds)
        if not resolved:
            raise RuntimeError(
                "该数据源没有登记只读凭据，无法执行真值 SQL（绝不回退写凭据）")
        source, cfg = resolved
        result = await source.run_query(cfg, sql, timeout=TRUTH_TIMEOUT_SECONDS)
        return [list(row) for row in (result or {}).get("rows") or []]

    return truth


async def probe_targets(tenant_id: str, kb: str, ds: str | None, corpus: dict,
                        cases: list[str], n: int = PROBE_REPEAT, *,
                        evolution_run_id: str = "") -> dict:
    """父本态一致率探针：每个目标题跑 `n` 次，按「SQL 或原因码」形态计数。

    返回 `{case_name: {"identical": 最多相同形态的次数, "total": n, "question": 题面}}`。
    语料里找不到对应 case 的静默跳过；单次调用抛错记为 `<error>`（也是一次「不一样」）。
    `evolution_run_id` 原样透传给 asker（router 档据此给合成流量打标记）。
    """
    index = {str(c.get("name")): str(c.get("question") or "")
             for c in (corpus or {}).get("cases") or []
             if isinstance(c, dict) and c.get("name")}
    ask = asker_for(tenant_id, kb, ds, evolution_run_id=evolution_run_id)
    total = max(1, int(n))
    out: dict[str, dict] = {}
    for case in cases or []:
        question = index.get(str(case))
        if not question:
            continue
        shapes: list[str] = []
        for _ in range(total):
            try:
                data = await ask(question)
            except Exception:  # noqa: BLE001 —— 传输失败同样是「这次不一样」
                shapes.append("<error>")
                continue
            data = data or {}
            shapes.append(str(data.get("sql")
                              or f"<{data.get('reason_code') or 'no-sql'}>"))
        out[str(case)] = {
            "identical": max(shapes.count(s) for s in set(shapes)),
            "total": total,
            "question": question,
        }
    return out


__all__ = [
    "asker_for",
    "in_process_asker_for",
    "probe_targets",
    "router_asker_for",
    "truth_runner_for",
]
