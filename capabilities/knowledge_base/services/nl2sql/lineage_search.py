# -*- coding: utf-8 -*-
"""W4.5 血缘检索（MVP）：问题/概念 → 资产候选 → 沿血缘扩展 → 确定性排序。

设计：docs/superpowers/specs/2026-11-01-w4.5-lineage-search-design.md（工作区文档，不入库）。

规则（**无 LLM**）：
- 召回：抽象 schema 的指标/实体（`match_metrics` / `match_entities`）+ 编译 schema 的表/列
  （词面匹配表名、实体名、列名）；
- 扩展：对表/列/指标候选做一跳（可选两跳）血缘邻居，按 `0.5^hop` 衰减；
- 排序：匹配档 > 资产类型 > 扩展跳数；同分按 `asset_id` 字典序（可复现）；
- 实体是抽象层派生物：返回候选但 `navigable=false`（目录里没有它的资产 ID）。

本模块不写库、不改图；DB 失败只让对应数据源降级（`degraded_data_sources`）。
"""
from __future__ import annotations

import logging

from jonex_core.common.exceptions import InvalidParameterError
from jonex_core.common.tenant import require_tenant

from ..concept_expansion import match_entities, match_metrics
from . import schema_read

logger = logging.getLogger(__name__)

_KIND_SCORE = {"exact": 100, "alias": 80, "core": 70,
               "attribute": 65, "description": 40}
_TYPE_BONUS = {"metric": 20, "entity": 12, "table": 10, "column": 5}
_MAX_DATASOURCES = 10
_MAX_SEEDS = 10


def rank_candidates(entries: list[dict]) -> list[dict]:
    """统一排序：score 降序 → 类型序 → asset_id（稳定、可复现）。"""
    type_order = {"metric": 0, "entity": 1, "table": 2, "column": 3}
    return sorted(
        entries,
        key=lambda item: (-int(item.get("score") or 0),
                          type_order.get(item.get("asset_type"), 9),
                          str(item.get("asset_id") or "")),
    )


def _candidate(asset_type: str, asset_id: str, label: str, score: int,
               reason: str, **extra) -> dict:
    return {"asset_type": asset_type, "asset_id": asset_id,
            "label": label, "score": int(score),
            "reasons": [reason], **extra}


def _merge(existing: dict | None, incoming: dict) -> dict:
    if existing is None:
        return incoming
    if incoming["score"] > existing["score"]:
        existing.update({k: v for k, v in incoming.items()
                         if k not in ("reasons",)})
    for reason in incoming.get("reasons") or []:
        if reason not in existing["reasons"]:
            existing["reasons"].append(reason)
    return existing


def _landing_ids(items, *, key: str) -> list[str]:
    found: list[str] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        for landing in item.get(key) or []:
            if isinstance(landing, dict):
                ds = str(landing.get("data_source_id") or "").strip()
                if ds and ds not in found:
                    found.append(ds)
    return found


def _canonical_asset_id(asset_id: str, asset_type: str,
                        table_maps: dict[str, dict[str, str]] | None) -> str:
    """血缘图节点 ID → 目录资产 ID（把剥了 schema 的表名换回编译行全名）。"""
    if not table_maps:
        return asset_id
    ds, sep, tail = asset_id.partition(":")
    mapping = table_maps.get(ds) if sep else None
    if not mapping or not tail:
        return asset_id
    if asset_type == "column":
        table, dot, column = tail.rpartition(".")
        if not dot:
            return asset_id
        full = mapping.get(table.casefold())
        return f"{ds}:{full}.{column}" if full else asset_id
    full = mapping.get(tail.casefold())
    return f"{ds}:{full}" if full else asset_id


def _data_source_ids(abstract: dict | None, explicit: str) -> list[str]:
    if explicit:
        return [explicit]
    schema = (abstract.get("schema") or {}) if isinstance(abstract, dict) else {}
    found = _landing_ids(schema.get("entities"), key="sources")
    for ds in _landing_ids(schema.get("metrics"), key="landings"):
        if ds not in found:
            found.append(ds)
    return found[:_MAX_DATASOURCES]


class LineageSearchMixin:
    async def search_lineage_assets(
        self, tenant_id: str, kb_id: str, query: str, *,
        data_source_id: str = "", limit: int = 20, max_hops: int = 1,
        user_id: str = "",
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        q = str(query or "").strip()
        if not q:
            raise InvalidParameterError(message="query 不能为空")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
            raise InvalidParameterError(message="limit 必须在 1..50")
        if max_hops not in (0, 1, 2):
            raise InvalidParameterError(message="max_hops 只能是 0/1/2")

        abstract = await schema_read.load_abstract_row(tenant_id, kb_id)
        schema = (abstract.get("schema") or {}) if isinstance(abstract, dict) else {}
        if abstract is None or abstract.get("status") != "active":
            schema = {}
        pool: dict[str, dict] = {}

        # 指标 / 实体（抽象层）
        for hit in match_metrics(q, schema.get("metrics") or []):
            metric = hit.get("metric") or {}
            name = str(hit.get("name") or "")
            score = _KIND_SCORE.get(hit.get("match_kind"), 30) + _TYPE_BONUS["metric"]
            item = _candidate("metric", f"{kb_id}:{name}",
                              str(metric.get("display_name") or name),
                              score, f"match:{hit.get('match_kind')}")
            pool[item["asset_id"]] = _merge(pool.get(item["asset_id"]), item)
        for hit in match_entities(q, schema.get("entities") or []):
            name = str(hit.get("entity") or "")
            score = _KIND_SCORE.get(hit.get("match_kind"), 30) + _TYPE_BONUS["entity"]
            item = _candidate("entity", f"{kb_id}:{name}", name, score,
                              f"match:{hit.get('match_kind')}",
                              navigable=False)
            pool[item["asset_id"]] = _merge(pool.get(item["asset_id"]), item)

        # 表 / 列（编译 schema，逐源）
        degraded: list[str] = []
        table_maps: dict[str, dict[str, str]] = {}
        q_cf = q.casefold()
        for ds in _data_source_ids(abstract, data_source_id):
            try:
                row = await schema_read.load_compiled_row(tenant_id, kb_id, ds)
            except Exception as exc:  # noqa: BLE001 —— 单源失败只降级
                logger.warning("[lineage-search] 读编译 schema 失败 ds=%s: %s",
                               ds, exc)
                degraded.append(ds)
                continue
            if not row:
                continue
            mapping: dict[str, str] = {}
            table_maps[ds] = mapping
            for entity in row.get("entity_types") or []:
                if not isinstance(entity, dict):
                    continue
                table = str(entity.get("source_object_id") or "").strip()
                if not table:
                    continue
                # 血缘图按规范化表名（剥 schema）建节点；目录资产 ID 用编译行全名。
                mapping[table.rsplit(".", 1)[-1].casefold()] = table
                mapping[table.casefold()] = table
                if q_cf in table.casefold() or q_cf in str(
                        entity.get("name") or "").casefold():
                    asset_id = f"{ds}:{table}"
                    item = _candidate("table", asset_id, table,
                                      70 + _TYPE_BONUS["table"],
                                      "match:table_name")
                    pool[asset_id] = _merge(pool.get(asset_id), item)
                for attribute in entity.get("attributes") or []:
                    if not isinstance(attribute, dict):
                        continue
                    column = str(attribute.get("name") or "").strip()
                    if not column or q_cf not in column.casefold():
                        continue
                    asset_id = f"{ds}:{table}.{column}"
                    item = _candidate("column", asset_id, column,
                                      65 + _TYPE_BONUS["column"],
                                      "match:column_name")
                    pool[asset_id] = _merge(pool.get(asset_id), item)

        if max_hops:
            await self._expand_lineage(pool, tenant_id, list(pool.values()),
                                       max_hops=max_hops,
                                       table_maps=table_maps)

        ranked = rank_candidates(list(pool.values()))
        return {
            "query": q,
            "candidates": ranked[:limit],
            "truncated": len(ranked) > limit,
            "degraded_data_sources": degraded,
        }

    async def _expand_lineage(self, pool: dict[str, dict], tenant_id: str,
                              seeds: list[dict], *, max_hops: int,
                              table_maps: dict[str, dict[str, str]] | None = None,
                              ) -> None:
        """沿 `asset_lineage.build_data_asset_graph` 扩展；失败静默（扩展是增强项）。"""
        from ....data_governance.services.asset_lineage import (  # noqa: PLC0415
            build_data_asset_graph,
        )

        current = sorted(seeds, key=lambda item: -int(item.get("score") or 0))
        current = [item for item in current
                   if item.get("asset_type") in ("metric", "table", "column")][
                       :_MAX_SEEDS]
        seen_seeds = {item["asset_id"] for item in current}
        for hop in range(1, max_hops + 1):
            next_seeds: list[dict] = []
            for seed in current[:_MAX_SEEDS]:
                try:
                    graph = await build_data_asset_graph(
                        tenant_id,
                        {"asset_type": seed["asset_type"],
                         "asset_id": seed["asset_id"], "name": seed["label"]},
                    )
                except Exception as exc:  # noqa: BLE001 —— 扩展失败不阻断
                    logger.warning("[lineage-search] 扩展失败 %s: %s",
                                   seed["asset_id"], exc)
                    continue
                for node in graph.get("nodes") or []:
                    asset_type = str(node.get("type") or "")
                    asset_id = str(node.get("id") or "")
                    if asset_type not in ("table", "column") or not asset_id:
                        continue
                    asset_id = _canonical_asset_id(asset_id, asset_type, table_maps)
                    if asset_id == seed["asset_id"]:
                        continue
                    score = int(int(seed["score"]) * (0.5 ** hop)) - 5 * hop
                    if score <= 0:
                        continue
                    item = _candidate(asset_type, asset_id,
                                      str(node.get("label") or asset_id),
                                      score, f"lineage:hop{hop}")
                    pool[asset_id] = _merge(pool.get(asset_id), item)
                    if asset_id not in seen_seeds:
                        next_seeds.append(item)
            if hop < max_hops:
                current = next_seeds[:_MAX_SEEDS]
                seen_seeds.update(item["asset_id"] for item in current)
            if not current:
                break


__all__ = ["LineageSearchMixin", "rank_candidates"]
