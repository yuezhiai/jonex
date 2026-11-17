#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""按 trace 汇总单次检索 Token 的只读能力（带读侧重试与稳定判定）。

调用方（检索链路）在本轮检索的 LLM 调用结束后立即调用本函数。计量由
llm-gateway **异步、按批**写入 ``metering.llm_usage_log``（``LLMGW_PG_FLUSH_MAX_SECONDS``
默认 1.0s，即一批可能晚到 1 秒），与检索响应存在竞态。

**判定口径是「读稳了」而不是「读到了」**：一轮检索的多次模型调用会被网关拆成
多个落库批次，先到的批次会让查询立刻返回非空结果。若此时就收工，最后一批永远
漏掉（2026-11-10 测试服实测：ontology-data 响应 58412、真实 60386，少的那 1974
正是最后落库的 NL→SQL 行）。因此这里要求**连续 ``STABLE_OBSERVATIONS`` 次读数
完全一致**才返回：间隔 0.6s × 3 次 = 1.2s 的观测跨度，刚好覆盖一个 1.0s 的落库
周期。最坏仍是 6 次查询（约 3s）封顶。

契约（被其他模块按签名调用，勿改）：

* 只统计 ``is_estimated = false`` 的行，``SUM(total_tokens)``；
* 始终无行 → ``None``（不返回 0，避免把「没查到」伪装成「0 Token」）；
* 有过非空读数 → 返回**最后一次**观测到的合计（**即使合计为 0 也返回 0**）；
  窗口内始终没等到稳定也返回最后观测值，不把已知的量丢掉；
* 异常（含 DB 超时）→ ``None`` + ``logger.warning``，绝不向上抛；
* 每次查询用独立 ``get_db_session()``，不让一个长事务跨整个重试窗口；
* 返回值只有合计整数，不含 trace_id / tenant_id。
"""

import asyncio
import logging
from typing import Optional, Tuple

from jonex_core.common import get_db_session

from ..repository.trace_token_repository import TraceTokenRepository

logger = logging.getLogger(__name__)

#: 重试间隔（秒）。0.6 而非 0.5：×（稳定次数 - 1）后的观测跨度要**大于**
#: 网关 1.0s 的落库周期，0.5 × 2 = 1.0s 正好卡在边界上，仍可能漏掉恰好那一批。
RETRY_INTERVAL_SECONDS = 0.6

#: 最多查询次数（首次 + 5 次重试，约 3s 封顶）。
MAX_ATTEMPTS = 6

#: 连续多少次完全一致的读数才算「落库稳定」。
#: 3 次 × 0.6s 间隔 = 1.2s 观测跨度，足以跨过一个 1.0s 的 flush 周期。
STABLE_OBSERVATIONS = 3

#: 便于单测替换的等待函数（不 monkeypatch 全局 asyncio.sleep）。
_sleep = asyncio.sleep


async def _query_once(tenant_id: str, trace_id: str) -> Tuple[int, int]:
    """一次独立会话查询，返回 ``(合计, 行数)``。"""
    async with get_db_session() as session:
        return await TraceTokenRepository(session, tenant_id=tenant_id).summarize(trace_id)


async def summarize_trace_tokens(tenant_id: str, trace_id: str) -> Optional[int]:
    """按 trace 汇总本次检索的精确 Token 合计（读侧重试 + 稳定判定）。

    行为：

    * 每 ``0.6s`` 查一次，最多 6 次（约 3s 封顶）；
    * 连续 ``STABLE_OBSERVATIONS``（3）次读数完全相同 → 认为落库稳定，返回该合计；
      行数 > 0 但合计为 0 时返回 ``0``（那是真实结果，不是「没查到」）；
    * 窗口内出现过非空读数但始终没稳定 → 返回**最后一次**观测值（不丢掉已知的量）；
    * 6 次全部无行 → ``None``；
    * 查询异常 / 超时 → 立即 ``None``（不重试）+ ``logger.warning``，不向上抛；
    * 每次查询独立 ``get_db_session()``，避免长事务与连接被占满整个窗口。

    「连续一致」而不是「非空即返」是刻意的：网关按批落库，先到的批次会让查询立刻
    非空；见到行就收工会把最后一批永久漏掉（见模块 docstring 的实测数据）。
    稳定性用 ``(行数, 合计)`` 二元组判定——只比合计的话，两行一多一少恰好抵消时
    会被误判为稳定。
    """
    observed: Optional[Tuple[int, int]] = None
    streak = 0
    last_total: Optional[int] = None

    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            await _sleep(RETRY_INTERVAL_SECONDS)
        try:
            total_tokens, row_count = await _query_once(tenant_id, trace_id)
        except Exception as exc:  # noqa: BLE001 — 契约要求吞掉一切并降级
            logger.warning(
                "按 trace 汇总 Token 失败，降级返回 None：tenant=%s trace=%s attempt=%d error=%s",
                tenant_id,
                trace_id,
                attempt + 1,
                exc,
            )
            return None
        if row_count <= 0:
            observed, streak = None, 0
            continue
        last_total = total_tokens
        current = (row_count, total_tokens)
        if current == observed:
            streak += 1
        else:
            observed, streak = current, 1
        if streak >= STABLE_OBSERVATIONS:
            return total_tokens

    if last_total is not None:
        logger.debug(
            "按 trace 汇总 Token 未读稳（%d 次查询后按最后观测值返回）：tenant=%s trace=%s total=%s",
            MAX_ATTEMPTS,
            tenant_id,
            trace_id,
            last_total,
        )
        return last_total

    logger.debug(
        "按 trace 汇总 Token 未命中（%d 次查询后放弃）：tenant=%s trace=%s",
        MAX_ATTEMPTS,
        tenant_id,
        trace_id,
    )
    return None


async def attach_total_tokens(
    result: dict, tenant_id: str, trace_id: Optional[str],
) -> None:
    """把本次检索按 trace 汇总的精确 Token 挂到返回 dict。

    仅当汇总到值（非 ``None``）时才写 ``total_tokens`` 键——``None`` 表示计量
    尚未落库（数据延迟时整个 Token 区域不展示，也不写 ``null`` 键）。
    汇总异常一律降级为「不展示」，绝不掀翻检索。``trace_id`` 为空时直接跳过，
    避免空 trace 触发 6 次查询 / 约 3s 空转。
    """
    if not tenant_id or not trace_id:
        return
    try:
        total = await summarize_trace_tokens(tenant_id, trace_id)
    except Exception as exc:  # noqa: BLE001 —— Token 展示是锦上添花，失败不阻塞检索
        logger.warning(
            "[trace-token] 汇总失败，本次检索不展示 Token：tenant=%s trace=%s error=%s",
            tenant_id, trace_id, exc,
        )
        return
    if total is not None:
        result["total_tokens"] = int(total)


__all__ = [
    "summarize_trace_tokens",
    "attach_total_tokens",
    "MAX_ATTEMPTS",
    "RETRY_INTERVAL_SECONDS",
    "STABLE_OBSERVATIONS",
]
