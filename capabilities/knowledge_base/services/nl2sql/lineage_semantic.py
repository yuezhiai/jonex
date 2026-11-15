#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""W4.5 语义召回：问题/概念 → 资产候选的**向量**通道（与词面通道互补）。

词面通道（`lineage_search` 里的 `match_metrics` / `match_entities` / 表列子串）
要求问题与资产有**字面重叠**；这条通道把资产文本与问题分别嵌入后取余弦，
捞出「说法不同、语义相同」的资产（例：`卖东西赚的钱` → 指标「销售额」）。

边界（与 N1-2 的 S4 同款）：

- **默认关**（`NL2SQL_LINEAGE_SEMANTIC`）；关时调用方完全不进本模块，响应形状不变。
- 嵌入失败/超时**只降级本通道**（返回 `degraded=True`），绝不拖垮词面召回。
- 资产文本与问题向量都有进程内缓存：一次提问只多一次 query 嵌入（资产向量按
  `(asset_id, text_hash)` 缓存，schema 不变就不重算）。
- 分数带**低于词面档**：语义满分（85）仍低于词面别名（100）与正名（120），
  只负责补漏，不抢词面命中的排序。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from jonex_core.common.config import config

logger = logging.getLogger(__name__)

#: 资产文本上限（字符）：表/列的描述可能很长，截断保证嵌入输入稳定。
MAX_TEXT_CHARS = 400
#: 单次请求最多嵌入的资产数（资产按类型序 + id 排序后截断，确定性）。
MAX_ASSETS = 200
#: 语义分基线与跨度：`base + span*cos`，再加资产类型加成（与词面档同表）。
_BASE = 30.0
_SPAN = 35.0
_TYPE_BONUS = {"metric": 20, "entity": 12, "table": 10, "column": 5}

#: 向量缓存上限（条）。资产上千的 KB 也只缓存最近用到的部分。
_MAX_VECTORS = 4096

_vector_cache: OrderedDict[str, list[float]] = OrderedDict()
_query_cache: OrderedDict[str, list[float]] = OrderedDict()


@dataclass(frozen=True)
class SemanticAsset:
    asset_type: str
    asset_id: str
    label: str
    text: str


def _text_hash(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _clip(value: Any) -> str:
    return str(value or "").strip()[:MAX_TEXT_CHARS]


def _join(*parts: Any) -> str:
    return " | ".join(_clip(part) for part in parts if _clip(part))


def _abstract_entities(schema: dict | None) -> list[dict]:
    return [e for e in ((schema or {}).get("entities") or [])
            if isinstance(e, dict)]


def _abstract_metrics(schema: dict | None) -> list[dict]:
    return [m for m in ((schema or {}).get("metrics") or [])
            if isinstance(m, dict)]


def build_assets(kb_id: str, schema: dict | None,
                 compiled_rows: dict[str, dict],
                 data_source_ids: Iterable[str]) -> list[SemanticAsset]:
    """抽象层（指标/实体）+ 编译层（表/列）→ 可嵌入的资产清单。

    顺序确定性：指标（schema 序）→ 实体（schema 序）→ 每个数据源的表/列
    （`data_source_ids` 序 → entity 序 → attribute 序）。同 `asset_id` 只保留
    第一次出现的文本（多源映射到同一抽象实体时，先出现的那个源为准）。
    """
    assets: dict[str, SemanticAsset] = {}

    def _add(asset_type: str, asset_id: str, label: str, text: str) -> None:
        if asset_id and asset_id not in assets:
            assets[asset_id] = SemanticAsset(asset_type, asset_id, label,
                                             _clip(text))

    for metric in _abstract_metrics(schema):
        name = str(metric.get("name") or "").strip()
        if not name:
            continue
        aliases = " ".join(str(a) for a in (metric.get("aliases") or []) if a)
        _add("metric", f"{kb_id}:{name}",
             str(metric.get("display_name") or name),
             _join(name, metric.get("display_name"), aliases,
                   metric.get("description"), metric.get("unit"),
                   metric.get("grain")))

    for entity in _abstract_entities(schema):
        name = str(entity.get("name") or "").strip()
        if not name:
            continue
        aliases = " ".join(str(a) for a in (entity.get("aliases") or []) if a)
        sources = " ".join(
            str(src.get("source_object_id") or "")
            for src in (entity.get("sources") or []) if isinstance(src, dict))
        _add("entity", f"{kb_id}:{name}",
             str(entity.get("display_name") or name),
             _join(name, entity.get("display_name"), aliases,
                   entity.get("description"), sources))

    for ds in data_source_ids:
        row = compiled_rows.get(ds)
        if not isinstance(row, dict):
            continue
        for entity in row.get("entity_types") or []:
            if not isinstance(entity, dict):
                continue
            table = str(entity.get("source_object_id") or "").strip()
            if not table:
                continue
            attributes = [a for a in (entity.get("attributes") or [])
                          if isinstance(a, dict)]
            column_names = " ".join(
                str(a.get("name") or "") for a in attributes)
            _add("table", f"{ds}:{table}", table,
                 _join(table, entity.get("name"), entity.get("description"),
                       column_names))
            for attribute in attributes:
                column = str(attribute.get("name") or "").strip()
                if not column:
                    continue
                _add("column", f"{ds}:{table}.{column}", column,
                     _join(table, column, attribute.get("description"),
                           attribute.get("role"), attribute.get("unit"),
                           attribute.get("grain"),
                           " ".join(str(v) for v in (attribute.get("values") or []))))

    type_order = {"metric": 0, "entity": 1, "table": 2, "column": 3}
    return sorted(assets.values(),
                  key=lambda a: (type_order.get(a.asset_type, 9), a.asset_id))


def cosine(left: list[float] | None, right: list[float] | None) -> float:
    """余弦相似度；任一向量为空/零向量 → 0.0（不产生伪命中）。"""
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = sum(a * a for a in left) ** 0.5
    right_norm = sum(b * b for b in right) ** 0.5
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return dot / (left_norm * right_norm)


def semantic_score(cosine_value: float, asset_type: str) -> int:
    """余弦 → 候选分（**低于词面档**：语义只补漏，不抢正名/别名的排序）。"""
    return int(round(_BASE + _SPAN * max(0.0, min(1.0, cosine_value)))) + \
        _TYPE_BONUS.get(asset_type, 0)


def _cache_get(cache: OrderedDict, key: str) -> list[float] | None:
    value = cache.get(key)
    if value is not None:
        cache.move_to_end(key)
    return value


def _cache_put(cache: OrderedDict, key: str, value: list[float]) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > _MAX_VECTORS:
        cache.popitem(last=False)


def clear_caches() -> None:
    _vector_cache.clear()
    _query_cache.clear()


async def _default_embed(text: str, *, tenant_id: str = "",
                          kb_id: str = "",
                          trace_id: str | None = None) -> list[float]:
    """默认 embedding：延迟 import（与 N1-2 同一网关/模型）。"""
    from jonex_core.common.ontology_embedding import embed  # noqa: PLC0415

    return await embed(text, tenant_id=tenant_id, kb_id=kb_id, trace_id=trace_id)


async def semantic_scores(
    assets: list[SemanticAsset], query: str, *,
    embedder: Callable[..., Any] | None = None,
    tenant_id: str = "", kb_id: str = "",
    timeout: float | None = None, floor: float | None = None,
    max_assets: int | None = None, trace_id: str | None = None,
) -> tuple[dict[str, float], bool]:
    """资产语义分 `{asset_id: 候选分}` + `degraded`；失败只降级不抛。"""
    if not assets:
        return {}, False
    embed = embedder or _default_embed
    budget = float(timeout if timeout is not None else
                   config.NL2SQL_LINEAGE_SEMANTIC_TIMEOUT)
    floor_value = float(floor if floor is not None else
                        config.NL2SQL_LINEAGE_SEMANTIC_FLOOR)
    cap = int(max_assets if max_assets is not None else
              config.NL2SQL_LINEAGE_SEMANTIC_MAX_ASSETS)
    selected = assets[:max(1, cap)]

    query_key = _text_hash(f"q\x00{query}")
    query_vector = _cache_get(_query_cache, query_key)
    try:
        if query_vector is None:
            query_vector = await asyncio.wait_for(
                embed(query, tenant_id=tenant_id, kb_id=kb_id, trace_id=trace_id),
                timeout=budget)
            _cache_put(_query_cache, query_key, list(query_vector))
        scores: dict[str, float] = {}
        pending: list[SemanticAsset] = []
        for asset in selected:
            key = _text_hash(f"a\x00{asset.asset_id}\x00{asset.text}")
            vector = _cache_get(_vector_cache, key)
            if vector is None:
                pending.append(asset)
                continue
            value = cosine(query_vector, vector)
            if value >= floor_value:
                scores[asset.asset_id] = semantic_score(value, asset.asset_type)
        if pending:
            async def _embed_all() -> list[list[float]]:
                return await asyncio.gather(*[
                    embed(asset.text, tenant_id=tenant_id, kb_id=kb_id,
                          trace_id=trace_id)
                    for asset in pending])

            vectors = await asyncio.wait_for(_embed_all(), timeout=budget)
            for asset, vector in zip(pending, vectors):
                key = _text_hash(f"a\x00{asset.asset_id}\x00{asset.text}")
                _cache_put(_vector_cache, key, list(vector))
                value = cosine(query_vector, vector)
                if value >= floor_value:
                    scores[asset.asset_id] = semantic_score(
                        value, asset.asset_type)
        return scores, False
    except Exception as exc:  # noqa: BLE001 —— 语义召回是增能项
        logger.info("血缘语义召回降级：%s: %s", type(exc).__name__, exc)
        return {}, True


__all__ = [
    "MAX_ASSETS", "SemanticAsset", "build_assets", "clear_caches",
    "cosine", "semantic_score", "semantic_scores",
]
