"""概念候选的 LLM 相关性筛选（spec: 2026-10-03-concept-matching-v2 §3.4 / §四）。

与 `concept_expansion.py` 分家的理由：那个模块被文档钉死为「纯函数，不碰 DB、
不碰 LLM、不碰 Neo4j」——检索热路径依赖这条不变量；本模块是策略层 + 一次 LLM
调用，放一起会把 IO 引进那条路径的导入面。

失败纪律：LLM 超时 / 坏答案 / 网关错 → **原样回退**（宁多勿漏）；
合法空列表是**筛空**（成立）——两者是不同路径，不许合并。
"""
from __future__ import annotations

import asyncio
import logging
import os

from jonex_core.common import ontology_llm

logger = logging.getLogger(__name__)

#: LLM 筛选开关（spec §七，默认开；黑名单式「关」，与 ONTOLOGY_CONCEPT_EXPAND_ENABLED 同口径）。
ONTOLOGY_CONCEPT_FILTER_ENABLED = (
    os.getenv("ONTOLOGY_CONCEPT_FILTER_ENABLED", "true").lower()
    not in ("0", "false", "no", "off")
)
#: 触发阈值：最高分低于它时，即使单候选也送筛（覆盖「弱单命中确认」）。
CONCEPT_FILTER_MIN_SCORE = int(os.getenv("CONCEPT_FILTER_MIN_SCORE", "3"))
#: 筛选调用超时（秒）。LLM 调用自身没有上界，必须由 wait_for 兜住。
CONCEPT_FILTER_TIMEOUT_S = float(os.getenv("CONCEPT_FILTER_TIMEOUT_S", "15"))


def strip_private_keys(candidates: list[dict]) -> list[dict]:
    """剥掉全部私有键（`_` 前缀：`_score` / `_matches` / `_attr_hits`）。

    `concept_candidates` 的形状契约与今天逐字一致——响应边界与筛选返回路径都
    用本函数收口（T6 起 search_service 也直接用它）。"""
    out: list[dict] = []
    for c in candidates or []:
        if isinstance(c, dict) and any(k.startswith("_") for k in c):
            c = {k: v for k, v in c.items() if not k.startswith("_")}
        out.append(c)
    return out


async def filter_candidates(
    question: str,
    candidates: list[dict],
    *,
    tenant_id: str | None = None,
    kb_id: str | None = None,
    user_id: str | None = None,
    trace_id: str | None = None,
    pinned: bool = False,
) -> tuple[list[dict], str]:
    """多候选 / 低分时问 LLM「哪些候选真被问到」；其余路径原样放行。

    返回 `(筛后候选, status)`，status ∈ `skipped` / `filtered` / `passthrough`
    （供调用方推理链采集器分档记录）。`pinned=True`（② 带 entity_name 点名）整段跳过。
    """
    cands = [c for c in (candidates or []) if isinstance(c, dict)]
    if not cands:
        return [], "skipped"
    if pinned or not ONTOLOGY_CONCEPT_FILTER_ENABLED:
        return strip_private_keys(cands), "skipped"
    scores = [c.get("_score") for c in cands]
    known = [s for s in scores if s is not None]
    low = bool(known) and max(known) < CONCEPT_FILTER_MIN_SCORE
    if len(cands) < 2 and not low:
        return strip_private_keys(cands), "skipped"           # 单候选高分直通
    try:
        keep = await asyncio.wait_for(
            ontology_llm.filter_relevant_concepts(
                question, cands, tenant_id=tenant_id, kb_id=kb_id,
                user_id=user_id, trace_id=trace_id,
            ),
            timeout=CONCEPT_FILTER_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 —— 筛选失败绝不掀翻检索
        logger.warning("[concept-filter] 筛选失败，原样放行: %s: %s", type(exc).__name__, exc)
        return strip_private_keys(cands), "passthrough"
    picked = [cands[i] for i in keep if isinstance(i, int) and 0 <= i < len(cands)]
    return strip_private_keys(picked), "filtered"
