# -*- coding: utf-8 -*-
"""纯数据检索（`/search/data`）的进程内编排：KB → database 源 → ② NL→SQL → data_blocks。

这是 onto-router `_handle_data_search` 的 Jonex 内实现，供没有 router 的部署
（如 57）使用。**只做纯数据档**；`/search/ontology-data` 仍是 router 端点。

契约与前端 `knowledgeSearch.ts` / `DataBlock` 对齐：
- 成功：`{answer, source:"data", data_blocks:[...], ...}`
- 每个源一块；全部失败时 answer 固定「数据查询未返回结果」
- 有源答上时剪掉 `no_candidate` 块（用户 2026-11-02 反馈的噪音口径，与 router 一致）
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from jonex_core.common.database import get_db_session

from ...repository.data_source_repository import KnowledgeDataSourceRepository
from ...repository.knowledge_info_repository import KnowledgeInfoRepository
from ..metrics.time_hint import TimeHint, parse_time_hint

logger = logging.getLogger(__name__)

_MAX_WORKERS = 8
_MAX_TEXT_ROWS = 50


def _render_success(cols: list[str], rows: list[list[Any]], sql: str,
                    *, engine_label: str = "NL→SQL（②）",
                    window: TimeHint | None = None) -> str:
    """成功块的正文（与 router `render_ask_answer` 同口径）。"""
    if len(rows) == 1 and len(cols) == 1:
        body = f"**{cols[0]}** = {rows[0][0]}"
    else:
        head = " | ".join(cols)
        lines = [head, "-" * len(head)]
        lines += [" | ".join(str(v) for v in row) for row in rows[:_MAX_TEXT_ROWS]]
        body = "```\n" + "\n".join(lines) + "\n```"
    if sql:
        body += f"\n\nsql：\n```\n{sql}\n```"
    body += f"\n\n引擎：{engine_label}"
    if window is not None:
        body += f"（时间窗：{window.time_from} ~ {window.time_to}）"
    return body


def _candidates(data: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for cand in data.get("candidates") or []:
        if isinstance(cand, dict) and str(cand.get("entity_name") or "").strip():
            out.append(cand)
        if len(out) >= 12:
            break
    return out


def _block_from_ask(
    source_name: str, data: dict[str, Any], ds_id: str
) -> dict[str, Any]:
    """一个源的 `ask_data_source` 响应 → `DataBlock`。"""
    if data.get("ok"):
        cols = [str(c) for c in (data.get("columns") or [])]
        rows = [list(r) for r in (data.get("rows") or [])]
        sql = str(data.get("sql") or "")
        return {
            "data_source_id": str(data.get("data_source_id") or ds_id),
            "source_name": source_name,
            "engine": "nl2sql",
            "kind": "answer",
            "text": _render_success(cols, rows, sql),
            "table": {"columns": cols, "rows": rows},
            "sql": sql or None,
            "row_count": data.get("row_count"),
            "truncated": data.get("truncated"),
            "reason": None,
            # W4.6 结果契约：前端优先按 text/chart/table 渲染
            "blocks": data.get("blocks") or None,
        }

    reason_code = str(data.get("reason_code") or "")
    reason = str(data.get("reason") or reason_code or "未知原因")
    body = f"答不了：{reason}"
    cands = _candidates(data)
    if cands:
        body += "\n\n候选：\n" + "\n".join(f"- {c.get('entity_name')}" for c in cands)
    if reason_code == "rejected_unsafe_sql" and data.get("rejected_sql"):
        body += f"\n\n被拒 SQL：\n```\n{data['rejected_sql']}\n```"
    return {
        "data_source_id": str(data.get("data_source_id") or ds_id),
        "source_name": source_name,
        "engine": "nl2sql",
        "kind": "error",
        "text": body,
        "table": None,
        "sql": None,
        "reason": f"{reason_code}: {reason}" if reason_code else reason,
        "reason_code": reason_code or None,
        "candidates": cands or None,
    }


def _block_from_metric(
    source_name: str, data: dict[str, Any], ds_id: str,
    window: TimeHint | None,
) -> dict[str, Any]:
    """`ask_metric` 的成功响应 → `DataBlock`（engine=`metric`）。"""
    cols = [str(c) for c in (data.get("columns") or [])]
    rows = [list(r) for r in (data.get("rows") or [])]
    sql = str(data.get("sql") or "")
    metric = data.get("metric") or {}
    if not rows:
        text = f"指标「{metric.get('display_name') or metric.get('name')}」在该时间窗内没有数据"
    else:
        text = _render_success(cols, rows, sql, engine_label="指标（确定性口径）",
                               window=window)
    return {
        "data_source_id": str(data.get("data_source_id") or ds_id),
        "source_name": source_name,
        "engine": "metric",
        "kind": "answer",
        "text": text,
        "table": {"columns": cols, "rows": rows},
        "sql": sql or None,
        "row_count": data.get("row_count"),
        "truncated": data.get("truncated"),
        "reason": None,
        "blocks": data.get("blocks") or None,
        "metric_name": metric.get("name") or "",
    }


async def _try_metric(
    nl2sql: Any, tenant_id: str, kb_id: str, question: str, ds_id: str,
    source_name: str, window: TimeHint | None, *,
    user_id: str = "", username: str = "", ip: str = "",
) -> dict[str, Any] | None:
    """先试指标：命中的是**已登记的可执行指标**就直接答，不走 NL→SQL。

    只接受 `exact` / `alias` 档：`partial` 说明只是沾边（问句里包含指标名的词而已），
    让它抢 ② 的活会答非所问 —— 那正是「7月份的AOI良率」被 NL→SQL 当成没候选的同类问题。
    """
    try:
        data = await nl2sql.ask_metric(
            tenant_id, kb_id, question,
            data_source_id=ds_id,
            time_from=window.time_from if window else None,
            time_to=window.time_to if window else None,
            user_id=user_id, username=username, ip=ip,
        )
    except Exception as exc:  # noqa: BLE001 —— 指标不可用就回落到 ②
        logger.warning("指标优先路径失败 ds=%s: %s", ds_id, exc)
        return None
    if not isinstance(data, dict) or not data.get("ok"):
        return None
    kind = str((data.get("metric") or {}).get("match_kind") or "")
    if kind not in ("exact", "alias"):
        return None
    return _block_from_metric(source_name, data, ds_id, window)


def _prune(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """有源答上时，省略「该源与问题无关」的 `no_candidate` 块（与 router 同口径）。"""
    if not any(b.get("kind") == "answer" for b in blocks):
        return blocks
    return [
        b
        for b in blocks
        if not (
            str(b.get("reason_code") or "") == "no_candidate"
            or str(b.get("reason") or "").startswith("no_candidate:")
        )
    ]


def _answer(blocks: list[dict[str, Any]]) -> str:
    texts = [
        str(b.get("text") or "").strip()
        for b in blocks
        if b.get("kind") == "answer" and str(b.get("text") or "").strip()
    ]
    if not texts:
        return "数据查询未返回结果"
    return "数据查询结果如下。\n\n**数据引用**\n\n" + "\n\n".join(texts)


async def _targets(
    tenant_id: str,
    kb_ids: list[str],
    pins: dict[str, str],
) -> list[tuple[str, str, str, str]]:
    """展开 (kb_id, data_source_id, source_name, pinned_entity)。"""
    out: list[tuple[str, str, str, str]] = []
    for kb_id in kb_ids:
        async with get_db_session() as session:
            kb = await KnowledgeInfoRepository(session).get_required(kb_id, tenant_id)
            if not bool(getattr(kb, "structured_enabled", False)):
                # 与 router 同口径：结构化关闭按「零源」处理，不是故障。
                continue
            sources = await KnowledgeDataSourceRepository(session).list_by_kb(
                tenant_id, kb_id
            )
        db_sources = [
            s for s in sources if str(getattr(s, "access_type", "")) == "database"
        ]
        if not db_sources:
            continue
        ids = {str(s.id): s for s in db_sources}
        pinned = [(ds, ent) for ds, ent in pins.items() if ds in ids]
        if pinned:
            out.extend((kb_id, ds, str(ids[ds].name or ds), ent) for ds, ent in pinned)
        else:
            out.extend((kb_id, str(s.id), str(s.name or s.id), "") for s in db_sources)
    return out


async def ask_targets(
    nl2sql: Any,
    tenant_id: str,
    question: str,
    targets: list[tuple[str, str, str, str]],
    *,
    user_id: str = "",
    username: str = "",
    ip: str = "",
    trace_id: str | None = None,
    time_hint: TimeHint | None = None,
) -> list[dict[str, Any]]:
    """对目标 `(kb, ds, name, pin)` 并发调用：**先指标、后 ②**，返回 `DataBlock` 列表。

    `trace_id` 原样下传 `ask_data_source`：本次检索的模型/embedding 调用都归到它。
    """
    sem = asyncio.Semaphore(_MAX_WORKERS)

    async def one(target: tuple[str, str, str, str]) -> dict[str, Any]:
        kb_id, ds_id, name, pin = target
        async with sem:
            metric_block = await _try_metric(
                nl2sql, tenant_id, kb_id, question, ds_id, name, time_hint,
                user_id=user_id, username=username, ip=ip,
            )
            if metric_block is not None:
                return metric_block
            try:
                data = await nl2sql.ask_data_source(
                    tenant_id,
                    kb_id,
                    question,
                    data_source_id=ds_id,
                    entity_name=pin,
                    user_id=user_id,
                    username=username,
                    ip=ip,
                    trace_id=trace_id,
                )
            except Exception as exc:  # noqa: BLE001 —— 一个源失败只影响它自己
                logger.warning("数据检索单源失败: ds=%s err=%s", ds_id, exc)
                data = {
                    "ok": False,
                    "reason_code": "call_failed",
                    "reason": f"NL→SQL 调用出错：{type(exc).__name__}: {exc}",
                }
        return _block_from_ask(name, data, ds_id)

    return list(await asyncio.gather(*(one(t) for t in targets)))


async def run_data_search(
    nl2sql: Any,
    tenant_id: str,
    kb_ids: list[str],
    question: str,
    *,
    pins: dict[str, str] | None = None,
    user_id: str = "",
    username: str = "",
    ip: str = "",
    trace_id: str | None = None,
) -> dict[str, Any]:
    """纯数据检索。`nl2sql` 是 `NL2SQLService`（复用其 `ask_data_source`）。"""
    started = time.monotonic()
    targets = await _targets(tenant_id, kb_ids, pins or {})
    if not targets:
        return {
            "answer": "所选知识库没有可查询的结构化数据源",
            "source": "data",
            "data_blocks": [],
            "references": [],
            "history_id": None,
            "reasoning": None,
            "rag_used": False,
            "data_supplement": False,
            "timings": {"total_ms": int((time.monotonic() - started) * 1000)},
        }

    time_hint = parse_time_hint(question)
    blocks = await ask_targets(
        nl2sql, tenant_id, question, targets,
        user_id=user_id, username=username, ip=ip, trace_id=trace_id,
        time_hint=time_hint,
    )
    blocks = _prune(blocks)
    return {
        "answer": _answer(blocks),
        "source": "data",
        "data_blocks": blocks,
        "references": [],
        "history_id": None,
        "reasoning": None,
        "rag_used": False,
        "data_supplement": False,
        "timings": {"total_ms": int((time.monotonic() - started) * 1000)},
    }


__all__ = ["ask_targets", "run_data_search"]
