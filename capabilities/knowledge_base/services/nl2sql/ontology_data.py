# -*- coding: utf-8 -*-
"""「本体 + 数据」检索的进程内编排：先本体答案，再按候选补一次 ②。

对应 onto-router 的 `/search/ontology-data`。行为取一个**安全子集**：
- 本体答上：按 `concept_candidates` 的 (kb, entity, ds) 补数据；成功块进
  `data_references`，失败块进 `data_blocks`；不调 LLM 融合（保留文档原文）。
- 候选歧义（同一 KB 多个实体）且没有点名 pin：下发候选 chips，等用户点名。
- 零候选：下发「未命中任何已对齐的数据实体」提示块（与 router 同文案）。
- 本体没答上：回落纯数据检索，并把数据答案并回本体响应（保留 ontology_instances）。
"""

from __future__ import annotations

from typing import Any

from .data_search import ask_targets, run_data_search

_FAILURE_MARKERS = (
    "所有知识库的语义检索均未返回有效结果",
    "未返回有效结果",
    "no relevant knowledge",
    "INSUFFICIENT",
)


def _answered(data: dict[str, Any]) -> bool:
    if str(data.get("source") or "") != "ontology":
        return False
    answer = str(data.get("answer") or "").strip()
    if not answer:
        return False
    return not any(marker in answer for marker in _FAILURE_MARKERS)


def _chips(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        ds = str(cand.get("data_source_id") or "").strip()
        ent = str(cand.get("entity_name") or "").strip()
        if not ds or not ent or (ds, ent) in seen:
            continue
        seen.add((ds, ent))
        out.append(
            {
                "entity_name": ent,
                "display_name": cand.get("display_name") or "",
                "entity_display_name": cand.get("entity_display_name") or "",
                "kind": "concept",
                "concept": cand.get("concept") or "",
                "matched_term": cand.get("matched_term") or "",
                "data_source_id": ds,
                "source_name": cand.get("source_name") or "",
            }
        )
    return out


def _attempt_block(text: str) -> dict[str, Any]:
    return {
        "data_source_id": "",
        "source_name": "数据问答",
        "engine": "nl2sql",
        "kind": "error",
        "text": text,
        "row_count": 0,
    }


def _candidates_block(chips: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "data_source_id": "",
        "source_name": "数据问答",
        "engine": "nl2sql",
        "kind": "answer",
        "text": "命中多个数据实体/源，请选择一个再问（点击候选）：",
        "candidates": chips,
        "row_count": 0,
    }


def _targets(
    candidates: list[dict[str, Any]],
    pins: dict[str, str],
) -> list[tuple[str, str, str, str]]:
    """候选 → (kb, ds, source_name, pinned_entity)。

    pins 优先；未被 pin 的 KB 里只有**单一实体**的才自动查（多实体 = 真歧义，
    交给候选 chips，避免猜错表）。
    """
    meta: dict[str, tuple[str, str, str]] = {}
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        ds = str(cand.get("data_source_id") or "").strip()
        kb = str(cand.get("knowledge_base_id") or "").strip()
        if not ds or not kb:
            continue
        ent = str(cand.get("entity_name") or "").strip()
        meta[ds] = (kb, str(cand.get("source_name") or ds), ent)

    targets: list[tuple[str, str, str, str]] = []
    pinned_kbs: set[str] = set()
    for ds, ent in pins.items():
        got = meta.get(ds)
        if got is None:
            continue
        kb, name, _ = got
        pinned_kbs.add(kb)
        targets.append((kb, ds, name, str(ent or "")))

    groups: dict[str, dict[str, list[tuple[str, str, str]]]] = {}
    for ds, (kb, name, ent) in meta.items():
        if kb in pinned_kbs:
            continue
        key = ent or f"__ds__:{ds}"
        groups.setdefault(kb, {}).setdefault(key, []).append((ds, name, ent))

    for kb, entries in groups.items():
        if len(entries) != 1:
            # 多实体（真歧义）→ 不猜，留在候选 chips。
            continue
        for ds, name, ent in next(iter(entries.values())):
            targets.append((kb, ds, name, ent))
    return targets


def _merge(ontology_data: dict[str, Any], data_res: dict[str, Any]) -> dict[str, Any]:
    out = dict(ontology_data)
    out["answer"] = data_res.get("answer") or "数据查询未返回结果"
    out["source"] = "data"
    out["rag_used"] = False
    out["data_blocks"] = data_res.get("data_blocks") or []
    return out


async def run_ontology_data_search(
    ontology_call,
    nl2sql: Any,
    tenant_id: str,
    kb_ids: list[str],
    question: str,
    *,
    pins: dict[str, str] | None = None,
    user_id: str = "",
    username: str = "",
    ip: str = "",
) -> dict[str, Any]:
    """执行「本体 + 数据」。`ontology_call` 是无参异步函数，返回本体检索的 data dict。"""
    ontology_data = await ontology_call()

    if not _answered(ontology_data):
        fallback = await run_data_search(
            nl2sql,
            tenant_id,
            kb_ids,
            question,
            user_id=user_id,
            username=username,
            ip=ip,
        )
        return _merge(ontology_data, fallback)

    candidates = ontology_data.get("concept_candidates") or []
    targets = _targets(candidates, pins or {})
    if not targets:
        chips = _chips(candidates)
        ontology_data["data_blocks"] = [
            (
                _candidates_block(chips)
                if chips
                else _attempt_block("未命中任何已对齐的数据实体，未调用 NL→SQL")
            )
        ]
        return ontology_data

    blocks = await ask_targets(
        nl2sql,
        tenant_id,
        question,
        targets,
        user_id=user_id,
        username=username,
        ip=ip,
    )
    successes = [b for b in blocks if b.get("kind") == "answer"]
    failures = [b for b in blocks if b.get("kind") != "answer"]
    if not successes:
        ontology_data["data_blocks"] = failures or [
            _attempt_block("数据查询未返回结果")
        ]
        return ontology_data

    ontology_data["data_references"] = [
        {
            "data_source_id": b.get("data_source_id"),
            "source_name": b.get("source_name"),
            "sql": b.get("sql"),
            "table": b.get("table"),
            "row_count": b.get("row_count"),
            "truncated": b.get("truncated"),
        }
        for b in successes
    ]
    ontology_data["data_augmented"] = True
    if failures:
        ontology_data["data_blocks"] = failures
    return ontology_data


__all__ = ["run_ontology_data_search"]
