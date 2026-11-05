#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""大 schema 分块选择（P2-3 / P2-4c）。

BIRD 小库（≤15 表）由 `schema_preview.build_schema_preview` 给紧凑预览；**大 schema**
（>15 表或估算 token 超预算）一次性全量注入会撑爆 prompt、且论文实证负收益。这里做
「按题面 + 锚点/1 跳排序，沿 join 关系补连通闭包，按预算截断」的选择器，输出与
`schema_preview` 相同形状（`[{table, entity, columns}]`），直接替换预览那段。

口径（与路线图 P2-3 对齐，并收窄一处）：

- **只有大 schema 才激活**：表数 > `MAX_PREVIEW_TABLES`，或估算字符 >
  `budget_tokens × 4`。路线图原文的「或题面 ≥3 表 / ≥2 join」只作为**大 schema 内**的
  打分权重；若把它当独立触发，BIRD smoke 的 45 道 join 题会被激活，违背
  「BIRD 50 题不激活、逐字节不变」的验收，所以这里不采用。
- 关档（`NL2SQL_CTE_VIEW=False`）时本模块完全不被调用，payload 逐字节不变。
- 只来自该源编译 schema；选出表会并入安全闸白名单（与预览同一套）。

不做：revisor、多候选、一次性全量注入、完整 LSH 值索引。
"""
from __future__ import annotations

import re

from .schema_preview import _column_entries, _table_key

#: 估算口径：中文/英文混合 prompt 粗算 4 字符 ≈ 1 token（不引入 tokenizer 依赖）。
CHARS_PER_TOKEN = 4
DEFAULT_SCHEMA_BUDGET_TOKENS = 12000
DEFAULT_MAX_SELECTED_TABLES = 32
#: 视作「小 schema」的上限，与 `schema_preview` 保持一致。
SMALL_SCHEMA_TABLES = 15

_WORD = re.compile(r"[A-Za-z0-9_]+")


def _entities(entity_types: list | None) -> list[dict]:
    return [e for e in (entity_types or []) if isinstance(e, dict)]


def _entity_name(entity: dict) -> str:
    return str(entity.get("name") or "").strip()


def _column_names(entity: dict) -> list[str]:
    names: list[str] = []
    for attr in entity.get("attributes") or []:
        if not isinstance(attr, dict):
            continue
        logical = str(attr.get("name") or "").strip()
        physical = str(attr.get("physical_name") or "").strip() or logical
        if physical:
            names.append(physical)
        if logical and logical != physical:
            names.append(logical)
    return names


def estimate_schema_chars(entity_types: list | None) -> int:
    """把全部实体塞进 JSON payload 的粗略字符数（表名 + 列名 + 类型）。"""
    total = 0
    for entity in _entities(entity_types):
        total += len(_entity_name(entity)) + 16
        for name in _column_names(entity):
            total += len(name) + 8
    return total


def should_select_schema(entity_types: list | None, *,
                         budget_tokens: int = DEFAULT_SCHEMA_BUDGET_TOKENS) -> bool:
    """大 schema 才走选择器；小 schema 返回 False（与关档逐字节相同）。

    触发：**表数 > 15**（`schema_preview` 本身对 >15 表不工作），或估算 token 超预算
    （超宽表的大库）。二者任一成立即激活；BIRD 11 库（8–15 表）恒为 False。
    """
    entities = _entities(entity_types)
    budget_chars = max(1, int(budget_tokens)) * CHARS_PER_TOKEN
    if len(entities) > SMALL_SCHEMA_TABLES:
        return True
    return estimate_schema_chars(entities) > budget_chars


def _question_words(question: str) -> set[str]:
    return {w.casefold() for w in _WORD.findall(str(question or "")) if len(w) >= 2}


def _relation_graph(relation_types: list | None) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for rel in relation_types or []:
        if not isinstance(rel, dict):
            continue
        source = str(rel.get("source") or "").strip()
        target = str(rel.get("target") or "").strip()
        if not source or not target or source == target:
            continue
        graph.setdefault(source, set()).add(target)
        graph.setdefault(target, set()).add(source)
    return graph


def _norm_table(name) -> str:
    """表名归一：去 schema 前缀、转小写。血缘边可能写 `dbo.jobs`，实体写 `jobs`。"""
    text = str(name or "").strip().casefold()
    return text.rsplit(".", 1)[-1] if text else ""


def _lineage_graph(lineage_edges: list | None) -> dict[str, set[str]]:
    """血缘边（列级四元组）压成表级无向图；坏边跳过。"""
    graph: dict[str, set[str]] = {}
    for edge in lineage_edges or []:
        if isinstance(edge, dict):
            source = edge.get("from_table")
            target = edge.get("to_table")
        else:
            source = getattr(edge, "from_table", None)
            target = getattr(edge, "to_table", None)
        src, dst = _norm_table(source), _norm_table(target)
        if not src or not dst or src == dst:
            continue
        graph.setdefault(src, set()).add(dst)
        graph.setdefault(dst, set()).add(src)
    return graph


def _distance_from_seeds(seeds: set[str],
                         graph: dict[str, set[str]]) -> dict[str, int]:
    """BFS 距离（只沿 join 关系）；不可达不出现在结果里。"""
    distances: dict[str, int] = {}
    frontier = [s for s in seeds if s]
    for seed in frontier:
        distances[seed] = 0
    depth = 0
    while frontier:
        depth += 1
        nxt: list[str] = []
        for node in frontier:
            for neighbour in graph.get(node, ()):
                if neighbour not in distances:
                    distances[neighbour] = depth
                    nxt.append(neighbour)
        frontier = nxt
    return distances


def _score_entity(entity: dict, *, name: str, words: set[str], seeds: set[str],
                  distance: int | None,
                  lineage_distance: int | None = None) -> int:
    score = 0
    if name in seeds:
        score += 1_000_000                    # 锚点 / 1 跳，必须保留
    else:
        # join 连通优先于血缘相关：血缘只说明「同一条数据流」，不保证可 join。
        join_score = (10_000 - min(distance, 9) * 100
                      if distance is not None else 0)
        # 血缘只做**弱提示**：它不保证可 join，不能盖过题面命中的列/表。
        lineage_score = (300 - min(lineage_distance, 9) * 20
                         if lineage_distance is not None else 0)
        score += max(join_score, lineage_score)
    for word in words:
        if word in name:
            score += 200
        for column in _column_names(entity):
            if word in column.casefold():
                score += 20
                break
    return score


def select_schema(entity_types: list | None,
                  relation_types: list | None,
                  *,
                  question: str,
                  anchor: str,
                  related_tables: list | None = None,
                  budget_tokens: int = DEFAULT_SCHEMA_BUDGET_TOKENS,
                  max_tables: int = DEFAULT_MAX_SELECTED_TABLES,
                  lineage_edges: list | None = None,
                  entity_seeds: bool = False
                  ) -> tuple[list[dict], set[str]]:
    """按相关性 + 连通闭包挑表，按预算截断；`(preview, tables)`。

    `lineage_edges`（可选）只作为**弱相关**信号：同一条数据流上的表优先于完全无关的
    表，但排序低于 join 连通（血缘不保证可 join）。返回形状与 `schema_preview` 一致。
    """
    entities = _entities(entity_types)
    if not entities:
        return [], set()
    words = _question_words(question)
    # 默认关血缘时，表名处理与历史逐字节一致（casefold、不剥 schema）；
    # 只有开血缘才做 `_norm_table` 归一，让 `dbo.jobs` 与 `jobs` 能连上。
    normalize_names = bool(lineage_edges)

    def _seed_name(value) -> str:
        return (_norm_table(value) if normalize_names
                else str(value or "").strip().casefold())

    def _entity_name_for(entity: dict) -> str:
        return (_norm_table(_entity_name(entity)) if normalize_names
                else _entity_name(entity).casefold())

    seeds: set[str] = {_seed_name(anchor)} - {""}
    for table in related_tables or []:
        name = _seed_name((table or {}).get("table"))
        if name:
            seeds.add(name)
    # 题面点名的实体表提升为种子（2026-11-01，B 线 3 条多跳漏选的定向修复）。
    # 只在显式开关打开时做；4 字以下不命中，避免 "id"/"day" 这类噪声。
    if entity_seeds:
        for entity in entities:
            name = _entity_name_for(entity)
            if any(w == name
                   or (len(w) >= 4 and w in name)
                   or (len(name) >= 4 and name in w)
                   for w in words):
                seeds.add(name)
    graph = _relation_graph(relation_types)
    distances = _distance_from_seeds({s for s in seeds if s in graph} | seeds, graph)
    lineage_graph = _lineage_graph(lineage_edges) if lineage_edges else {}
    lineage_distances = (
        _distance_from_seeds({s for s in seeds if s in lineage_graph} | seeds,
                             lineage_graph)
        if lineage_graph else {}
    )

    ranked: list[tuple[int, dict]] = []
    for entity in entities:
        name = _entity_name_for(entity)
        distance = distances.get(name)
        lineage_distance = lineage_distances.get(name)
        ranked.append((_score_entity(entity, name=name, words=words, seeds=seeds,
                                     distance=distance,
                                     lineage_distance=lineage_distance), entity))
    ranked.sort(key=lambda item: item[0], reverse=True)
    ranked = ranked[:max(1, int(max_tables))]

    budget_chars = max(1, int(budget_tokens)) * CHARS_PER_TOKEN
    preview: list[dict] = []
    tables: set[str] = set()
    used = 0
    for score, entity in ranked:
        table = _table_key(entity)
        name = _entity_name(entity)
        if not table:
            continue
        columns = [c["name"] for c in _column_entries(entity)]
        cost = len(table) + len(name) + 8 + sum(len(c) for c in columns)
        if preview and used + cost > budget_chars:
            break
        preview.append({"table": table, "entity": name, "columns": columns})
        tables.add(table)
        used += cost
    return preview, tables


__all__ = ["estimate_schema_chars", "should_select_schema", "select_schema",
           "CHARS_PER_TOKEN", "DEFAULT_SCHEMA_BUDGET_TOKENS",
           "DEFAULT_MAX_SELECTED_TABLES", "SMALL_SCHEMA_TABLES"]
