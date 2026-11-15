#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""检索式 schema 选择（N1-2）：把编译 schema 切成「一表一块」，供后续信号/嵌入检索。

大 schema 一次性全量注入会撑爆 prompt；`schema_select` 的规则选择器只看表名与题面
词面重叠。这里把每张表压成一个可检索的 `TableChunk`（表名/描述/列名/列描述/证据
示例值/抽象实体别名），后续任务在同一模块上加 BM25/嵌入信号与 MMR 选择。

Task 1 是**纯构造**，不读配置、不调用模型、不查库：

- `tokenize`：ASCII 词（长度 ≥2，casefold）+ CJK 连续段二元组 —— 中文没有空格，
  二元组是「零依赖 + 可解释」的下限方案。
- `build_chunks`：每张编译实体一个块；文本截 800 字符、证据值总预算 1200 字符
  （按表序→列序→值序确定性截断）。
- 抽象实体按 `sources[]` 的 `(data_source_id, entity_name)` 匹配，合并
  `display_name`/`aliases`/`description`（**同时写进 `text`**，Task 3 嵌入的是正文）；
  **没有映射的表照样产出块**（`aliases=()`），供后续 S1-S5 信号使用 —— 映射补齐在 Task 3 做。

Task 2 加五路确定性信号与融合：

- S1 `table_lexical_scores`：`|q ∩ tokens| + 0.5·|q ∩ union(column_tokens)|`。
- S2 `column_lexical_scores`：每列命中数取最高 + 0.5·次高，并回传逐列命中表。
- S3 `value_scores`：问题词元与 `value_tokens` 交集，按 `1+len/20` 加权，封顶 3 个。
- S5 `graph_scores`：种子表实体名沿 `relation_types` 无向 BFS，`1/(1+d)`，封顶 2 跳。
- `rrf_ranks` / `rrf_fuse`：名次（0-based，**只有正分才有名次**，同分按表名稳定）
  → `Σ w/(60+rank)`；无名次的信号不贡献质量。
- `mmr_select`：列集合 Jaccard 相似度下的 `λ·score - (1-λ)·max_sim` 贪心去重。

Task 3 是**编排层**（`retrieve_candidates`）：S4 embedding 余弦（只嵌入 S1/S2/S3
短名单表，带问题/表向量进程缓存与整体超时）→ 五路 RRF → MMR → 表映射回抽象实体
候选。embedding 失败/超时**只降级该路**（S4 全 0，`embed_degraded=True`），绝不抛错；
未映射到抽象实体的表只进 `unmapped`，不合成候选（抽象层仍是语义权威）。

除 `retrieve_candidates` 外，所有信号只依赖入参与块序，无随机、无第三方依赖；
同分一律按块序（或表名）稳定。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from collections import OrderedDict
from dataclasses import dataclass

from ..evidence_probe import evidence_key

#: 块文本上限（字符）。表名 + 描述 + 列清单，超了按列序截断。
MAX_TEXT_CHARS = 800
#: 证据示例值的总字符预算（跨列累计；按列序→值序，确定性）。
VALUE_TOKEN_BUDGET = 1200

#: ASCII 词：与 `schema_select` 同口径（下划线算词内字符，不拆）。
_ASCII_WORD = re.compile(r"[A-Za-z0-9_]+")
#: CJK 连续段：基本区 + 扩展 A + 兼容表意文字。
_CJK_RUN = re.compile(r"[㐀-䶿一-鿿豈-﫿]+")
#: 单词太短没有区分度（"a"/"s" 之类），丢弃。
MIN_ASCII_TOKEN = 2


def tokenize(text: str) -> set[str]:
    """ASCII 词（casefold、长度 ≥2）+ CJK 二元组；单字段保留单字。"""
    raw = str(text or "")
    tokens = {word.casefold() for word in _ASCII_WORD.findall(raw)
              if len(word) >= MIN_ASCII_TOKEN}
    for run in _CJK_RUN.findall(raw):
        if len(run) == 1:
            tokens.add(run)
        else:
            tokens.update(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


@dataclass(frozen=True)
class TableChunk:
    """一张表的检索单元；字段只读，构造后不再修补。"""

    data_source_id: str
    entity_name: str
    table: str
    text: str
    tokens: frozenset[str]
    column_names: tuple[str, ...]
    column_tokens: dict[str, frozenset[str]]
    value_tokens: frozenset[str]
    aliases: tuple[str, ...]
    #: 每列 `(物理名, 逻辑名)`，按 schema 序；`column_names` 是**名字**的并集（物理名与
    #: 逻辑名不同时各占一项），按名字迭代会把一列算两次。默认 `()` 只为兼容旧的手工构造，
    #: `build_chunks` 总会填。
    column_pairs: tuple[tuple[str, str], ...] = ()


def _entities(compiled: dict | None) -> list[dict]:
    raw = (compiled or {}).get("entity_types") or []
    return [entity for entity in raw if isinstance(entity, dict)]


def _column_entries(entity: dict) -> list[tuple[str, str, str]]:
    """`(逻辑名, 物理名, 描述)`，按 schema 顺序；全空的属性跳过。"""
    entries: list[tuple[str, str, str]] = []
    for attr in entity.get("attributes") or []:
        if not isinstance(attr, dict):
            continue
        logical = str(attr.get("name") or "").strip()
        physical = str(attr.get("physical_name") or "").strip() or logical
        description = str(attr.get("description") or "").strip()
        if not logical and not physical:
            continue
        entries.append((logical or physical, physical or logical, description))
    return entries


def _column_names(entries: list[tuple[str, str, str]]) -> tuple[str, ...]:
    """物理名在前、逻辑名在后（两者不同才各占一项），去重保序。"""
    names: list[str] = []
    for logical, physical, _ in entries:
        for name in (physical, logical):
            if name and name not in names:
                names.append(name)
    return tuple(names)


def _column_tokens(entries: list[tuple[str, str, str]]) -> dict[str, frozenset[str]]:
    """列名 → 词元；逻辑名与物理名各自作键，值同一份（便于按任一名命中）。

    多列可能撞到同一个键（如某列的逻辑名恰好是另一列的物理名）；此时**合并**词元，
    否则后一列的描述词元会被静默丢掉。
    """
    result: dict[str, frozenset[str]] = {}
    for logical, physical, description in entries:
        tokens = frozenset(tokenize(" ".join(
            part for part in (logical, physical, description) if part)))
        for key in (logical, physical):
            if key:
                result[key] = result.get(key, frozenset()) | tokens
    return result


def _abstract_summary(abstracts: list[dict]) -> str:
    """抽象实体的显示名/摘要/别名压成一行，进 `text` 供嵌入（Task 3）。"""
    parts: list[str] = []
    for abstract in abstracts:
        name = str(abstract.get("name") or "").strip()
        display = str(abstract.get("display_name") or "").strip()
        description = str(abstract.get("description") or "").strip()
        aliases = [str(alias).strip() for alias in abstract.get("aliases") or []
                   if str(alias).strip()]
        head = display or name
        if display and name and display != name:
            head = f"{display} ({name})"
        segment = [part for part in (head, description) if part]
        if aliases:
            segment.append("aliases: " + ", ".join(aliases))
        if segment:
            parts.append("; ".join(segment))
    return " | ".join(parts)


def _build_text(entity_name: str, table: str, description: str,
                entries: list[tuple[str, str, str]],
                abstract_line: str = "") -> str:
    header = entity_name if not table or entity_name == table else f"{entity_name} ({table})"
    lines = [header.strip()]
    # 抽象行紧跟表头：别名是最强的词面信号，不能被后面的列清单挤掉。
    if abstract_line:
        lines.append(f"abstract: {abstract_line}")
    if description:
        lines.append(description)
    if entries:
        parts = []
        for logical, physical, col_desc in entries:
            head = logical if logical == physical else f"{physical} ({logical})"
            parts.append(f"{head}: {col_desc}" if col_desc else head)
        lines.append("columns: " + "; ".join(parts))
    return "\n".join(lines)[:MAX_TEXT_CHARS]


def _value_tokens(compiled: dict | None, table: str, *,
                  budget: int = VALUE_TOKEN_BUDGET) -> frozenset[str]:
    """已存证据的示例值（原样 + casefold + 词元）；超预算按序停。

    `samples` 里的短值（如 "F"/"T"）过不了 ASCII 词长度闸，所以**原样保留**；
    同时补一份 casefold 供后续词面匹配不区分大小写。
    """
    tables = ((compiled or {}).get("evidence") or {}).get("tables") or {}
    entry = tables.get(table) or {}
    columns = entry.get("columns") or {}
    if not isinstance(columns, dict):
        return frozenset()
    values: set[str] = set()
    remaining = max(0, int(budget))
    for column in columns.values():
        if not isinstance(column, dict):
            continue
        for raw in column.get("samples") or []:
            text = str(raw).strip()
            if not text:
                continue
            cost = len(text) + 1
            if cost > remaining:
                # 只跳过这一条超大示例，继续后面的列/值（return 会吞掉整段后续证据）。
                continue
            remaining -= cost
            values.add(text)
            folded = text.casefold()
            if folded != text:
                values.add(folded)
            values.update(tokenize(text))
    return frozenset(values)


def _abstract_sources(abstract: dict) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for source in abstract.get("sources") or []:
        if not isinstance(source, dict):
            continue
        # 落点被人工排除（`included=False`）就不是映射：表仍在块里供信号使用，
        # 但别名/摘要不能经同表别的落点漏进来（否则被排除的连接会变成候选）。
        if source.get("included") is False:
            continue
        ds_id = str(source.get("data_source_id") or "").strip()
        entity_name = str(source.get("entity_name") or "").strip()
        if ds_id or entity_name:
            pairs.append((ds_id, entity_name))
    return pairs


def _match_abstract(entity_name: str, ds_id: str,
                    abstract_entities: list[dict] | None) -> list[dict]:
    """按 `(ds, entity_name)` 匹配抽象实体；**ds 为空视为通配**。"""
    wanted = entity_name.casefold()
    matched: list[dict] = []
    for abstract in abstract_entities or []:
        if not isinstance(abstract, dict):
            continue
        for source_ds, source_entity in _abstract_sources(abstract):
            if source_entity.casefold() != wanted:
                continue
            if source_ds and ds_id and source_ds != ds_id:
                continue
            matched.append(abstract)
            break
    return matched


def _abstract_aliases(abstracts: list[dict]) -> tuple[str, ...]:
    aliases: list[str] = []
    for abstract in abstracts:
        for raw in abstract.get("aliases") or []:
            alias = str(raw).strip()
            if alias and alias not in aliases:
                aliases.append(alias)
    return tuple(aliases)


def _abstract_tokens(abstracts: list[dict]) -> set[str]:
    tokens: set[str] = set()
    for abstract in abstracts:
        for key in ("name", "display_name", "description"):
            tokens.update(tokenize(str(abstract.get(key) or "")))
        for raw in abstract.get("aliases") or []:
            tokens.update(tokenize(str(raw)))
    return tokens


def build_chunks(compiled: dict, abstract_entities: list[dict],
                 ds_id: str) -> list[TableChunk]:
    """每张编译实体一个 `TableChunk`；无 `source_object_id` 的实体用实体名作表名。"""
    data_source_id = str(ds_id or "").strip()
    chunks: list[TableChunk] = []
    for entity in _entities(compiled):
        entity_name = str(entity.get("name") or "").strip()
        table = evidence_key(entity)
        if not table and not entity_name:
            continue
        description = str(entity.get("description") or "").strip()
        entries = _column_entries(entity)
        abstracts = _match_abstract(entity_name, data_source_id, abstract_entities)
        aliases = _abstract_aliases(abstracts)
        text = _build_text(entity_name, table, description, entries,
                           abstract_line=_abstract_summary(abstracts))
        tokens = set(tokenize(text))
        tokens.update(_abstract_tokens(abstracts))
        tokens.update(tokenize(" ".join(aliases)))
        chunks.append(TableChunk(
            data_source_id=data_source_id,
            entity_name=entity_name or table,
            table=table or entity_name,
            text=text,
            tokens=frozenset(tokens),
            column_names=_column_names(entries),
            column_tokens=_column_tokens(entries),
            value_tokens=_value_tokens(compiled, table),
            aliases=aliases,
            column_pairs=tuple((physical, logical) for logical, physical, _ in entries),
        ))
    return chunks


# ---------------------------------------------------------------------------
# 五路信号（S1/S2/S3/S5）。全部只依赖入参与块序：无随机、无 I/O、无第三方依赖。
# ---------------------------------------------------------------------------

#: S3 最多计入几个匹配到的证据值（长尾值会把分数拉爆）。
MAX_VALUE_MATCHES = 3
#: RRF 的平滑常数（Cormack et al. 的 60）。
DEFAULT_RRF_K = 60
#: MMR 的相关性/多样性权衡默认值。
DEFAULT_MMR_LAMBDA = 0.7


def _column_union(chunk: TableChunk) -> set[str]:
    tokens: set[str] = set()
    for column in chunk.column_tokens.values():
        tokens.update(column)
    return tokens


def table_lexical_scores(question: str, chunks: list[TableChunk]) -> list[float]:
    """S1 表级词面：`|q ∩ tokens| + 0.5·|q ∩ union(column_tokens)|`。

    `tokens`（表名/描述/列描述正文）每命中计 1 分；列词元并集再按 0.5 分叠加，
    与正文命中不互斥 —— 列级证据是表内的局部信号，单独再加权。
    """
    query = tokenize(question)
    if not query:
        return [0.0 for _ in chunks]
    return [float(len(query & chunk.tokens)) +
            0.5 * len(query & _column_union(chunk))
            for chunk in chunks]


def column_lexical_scores(
        question: str, chunks: list[TableChunk]
) -> tuple[list[float], list[dict[str, float]]]:
    """S2 列级词面：每列命中数取「最高 + 0.5·次高」，并回传逐列命中表。

    计分按**去重后的词元集**：同一列在逻辑名与物理名下各存一份同样的词元集，
    若按名字键逐一计分，单列会同时占据「最高」与「次高」两个名次（别名冒充第二列）。

    返回 `(scores, column_hits)`；`column_hits[i]` 与 `chunks[i]` 对齐，键为列名
    （逻辑名与物理名各占一个键、值相同，与 `column_tokens` 一致），供下游按名取用。
    """
    query = tokenize(question)
    scores: list[float] = []
    column_hits: list[dict[str, float]] = []
    for chunk in chunks:
        hits = {name: float(len(query & tokens))
                for name, tokens in chunk.column_tokens.items()}
        ordered: list[float] = []
        seen_sets: set[frozenset[str]] = set()
        for tokens in chunk.column_tokens.values():
            unique = frozenset(tokens)
            if unique in seen_sets:
                continue
            seen_sets.add(unique)
            ordered.append(float(len(query & unique)))
        ordered.sort(reverse=True)
        top = ordered[0] if ordered else 0.0
        second = ordered[1] if len(ordered) > 1 else 0.0
        scores.append(top + 0.5 * second)
        column_hits.append(hits)
    return scores, column_hits


def value_scores(question: str, chunks: list[TableChunk]) -> list[float]:
    """S3 证据值：问题词元命中示例值，按 `1+len/20` 加权，封顶 3 个。

    排序取「长值优先、再按字典序」—— 长值信息量更大，且保证同分确定性。
    """
    query = tokenize(question)
    scores: list[float] = []
    for chunk in chunks:
        matched = sorted(query & chunk.value_tokens,
                         key=lambda value: (-len(value), value))
        score = sum(1.0 + len(value) / 20.0
                    for value in matched[:MAX_VALUE_MATCHES])
        scores.append(score)
    return scores


def _undirected_graph(relation_types: list[dict]) -> dict[str, set[str]]:
    """`relation_types[{source,target}]` → 无向邻接表（节点名 casefold）。"""
    graph: dict[str, set[str]] = {}
    for relation in relation_types or []:
        if not isinstance(relation, dict):
            continue
        source = str(relation.get("source") or "").strip().casefold()
        target = str(relation.get("target") or "").strip().casefold()
        if not source or not target or source == target:
            continue
        graph.setdefault(source, set()).add(target)
        graph.setdefault(target, set()).add(source)
    return graph


def _bfs_distances(seeds: set[str], graph: dict[str, set[str]],
                   max_hops: int) -> dict[str, int]:
    distances = {seed: 0 for seed in seeds}
    frontier = sorted(distances)
    depth = 0
    while frontier and depth < max_hops:
        depth += 1
        next_frontier: list[str] = []
        for node in frontier:
            for neighbour in sorted(graph.get(node, ())):
                if neighbour not in distances:
                    distances[neighbour] = depth
                    next_frontier.append(neighbour)
        frontier = next_frontier
    return distances


def graph_scores(chunks: list[TableChunk], relation_types: list[dict],
                 seed_indices: list[int], *, max_hops: int = 2) -> list[float]:
    """S5 关系图：种子表实体名沿 join 关系无向 BFS，得分 `1/(1+d)`，封顶 `max_hops`。

    越界/非整数种子下标被忽略；无有效种子、或 `max_hops < 0` 时全 0。
    """
    graph = _undirected_graph(relation_types)
    seeds: set[str] = set()
    for index in seed_indices or []:
        try:
            position = int(index)
        except (TypeError, ValueError):
            continue
        if 0 <= position < len(chunks):
            name = str(chunks[position].entity_name or "").strip().casefold()
            if name:
                seeds.add(name)
    if not seeds or max_hops < 0:
        return [0.0 for _ in chunks]
    distances = _bfs_distances(seeds, graph, max_hops)
    scores: list[float] = []
    for chunk in chunks:
        reachable = []
        for key in (chunk.entity_name, chunk.table):
            folded = str(key or "").strip().casefold()
            distance = distances.get(folded) if folded else None
            if distance is not None:
                reachable.append(distance)
        scores.append(1.0 / (1.0 + min(reachable)) if reachable else 0.0)
    return scores


# ---------------------------------------------------------------------------
# RRF 融合与 MMR 选择
# ---------------------------------------------------------------------------

def rrf_ranks(scores: list[float], *,
              names: list[str] | None = None) -> list[int]:
    """分数 → 0-based 名次（降序）；只有 `score > 0` 参与排名，其余记 `-1`。

    `-1` 表示**该路信号没有给这张表任何证据**，`rrf_fuse` 会跳过它。否则全 0
    的信号（如无图时的 S5、嵌入失败时的 S4）会按块序白送 RRF 质量，甚至让
    与问题无关的表靠 schema 顺序拿到候选资格。

    同分按 `names[index]`（表名）升序、再按块序稳定 —— 块序是 schema 顺序，
    不能让 schema 顺序决定近似并列的先后。
    """
    order = sorted(
        (index for index, score in enumerate(scores) if float(score) > 0.0),
        key=lambda index: (
            -float(scores[index]),
            (str(names[index]) if names is not None and index < len(names) else ""),
            index))
    ranks = [-1] * len(scores)
    for rank, index in enumerate(order):
        ranks[index] = rank
    return ranks


def rrf_fuse(signal_ranks: list[list[int]], weights: list[float], *,
             k: int = DEFAULT_RRF_K) -> list[float]:
    """Reciprocal Rank Fusion：`out[i] = Σ w_j / (k + rank_j[i])`。

    `signal_ranks[j][i]` 是第 j 路信号给第 i 个块的名次。各路长度必须一致；
    `rank < 0`（该路没有证据）不贡献质量。返回长度为块数的融合分（越大越靠前）。
    """
    rank_lists = [list(ranks) for ranks in (signal_ranks or [])]
    weight_list = [float(weight) for weight in (weights or [])]
    if len(rank_lists) != len(weight_list):
        raise ValueError("signal_ranks 与 weights 长度必须一致")
    if not rank_lists:
        return []
    size = len(rank_lists[0])
    if any(len(ranks) != size for ranks in rank_lists):
        raise ValueError("signal_ranks 各信号长度必须一致")
    if k < 1:
        raise ValueError("k 必须 ≥ 1")
    fused = [0.0] * size
    for ranks, weight in zip(rank_lists, weight_list):
        for index, rank in enumerate(ranks):
            if rank < 0:
                continue
            fused[index] += weight / (k + rank)
    return fused


def _column_set(chunk: TableChunk) -> set[str]:
    return {str(name).strip().casefold() for name in chunk.column_names
            if str(name or "").strip()}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 0.0
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def mmr_select(scores: list[float], chunks: list[TableChunk], *,
               top_k: int, lambda_: float = DEFAULT_MMR_LAMBDA) -> list[int]:
    """最大边际相关：`λ·score - (1-λ)·max_sim(已选)` 贪心选取，返回块下标。

    相似度是列集合的 Jaccard —— 同列同构的表会被去重。同分按块序（下标从小到大）。
    """
    if len(scores) != len(chunks):
        raise ValueError("scores 与 chunks 长度必须一致")
    count = min(max(0, int(top_k)), len(chunks))
    if count <= 0:
        return []
    weight = float(lambda_)
    column_sets = [_column_set(chunk) for chunk in chunks]
    selected: list[int] = []
    remaining = list(range(len(chunks)))
    while remaining and len(selected) < count:
        best_index = remaining[0]
        best_value: float | None = None
        for index in remaining:
            max_sim = (max(_jaccard(column_sets[index], column_sets[chosen])
                           for chosen in selected) if selected else 0.0)
            value = weight * float(scores[index]) - (1.0 - weight) * max_sim
            if best_value is None or value > best_value:
                best_value = value
                best_index = index
        selected.append(best_index)
        remaining.remove(best_index)
    return selected


# ---------------------------------------------------------------------------
# S4：embedding 余弦 + 进程缓存
# ---------------------------------------------------------------------------

#: 五路信号的 RRF 权重：S1 表名、S2 列名、S3 证据值、S4 embedding、S5 关系图。
SIGNAL_WEIGHTS: tuple[float, ...] = (1.0, 0.8, 0.6, 1.0, 0.4)
#: 问题向量缓存 TTL（秒）与 LRU 容量。
QUERY_CACHE_TTL_S = 300.0
QUERY_CACHE_MAX = 256
#: 表向量缓存 LRU 容量（键含 `schema_hash` 与 `text_hash`，schema 改了自然失效）。
CHUNK_CACHE_MAX = 2048
#: 每个候选最多回报多少个命中列（供 Task 4 列注入；再多会撑爆 payload）。
MAX_CANDIDATE_COLUMNS = 10

#: `(question, tenant) -> (写入时间, 向量)`；只缓存**成功**结果，失败下次重试。
_QUERY_CACHE: "OrderedDict[tuple[str, str], tuple[float, tuple[float, ...]]]" = OrderedDict()
#: `(tenant, kb, ds, schema_hash, text_hash) -> 向量`。
_CHUNK_CACHE: "OrderedDict[tuple[str, str, str, str, str], tuple[float, ...]]" = OrderedDict()


def clear_embedding_caches() -> None:
    """清空两个进程缓存（单测隔离用；生产不必调用）。"""
    _QUERY_CACHE.clear()
    _CHUNK_CACHE.clear()


def _query_cache_get(key: tuple[str, str]) -> list[float] | None:
    entry = _QUERY_CACHE.get(key)
    if entry is None:
        return None
    stamp, vector = entry
    if time.monotonic() - stamp > QUERY_CACHE_TTL_S:
        _QUERY_CACHE.pop(key, None)
        return None
    _QUERY_CACHE.move_to_end(key)
    return list(vector)


def _query_cache_put(key: tuple[str, str], vector: list[float]) -> None:
    _QUERY_CACHE[key] = (time.monotonic(), tuple(vector))
    _QUERY_CACHE.move_to_end(key)
    while len(_QUERY_CACHE) > QUERY_CACHE_MAX:
        _QUERY_CACHE.popitem(last=False)


def _chunk_cache_get(key: tuple[str, str, str, str, str]) -> list[float] | None:
    vector = _CHUNK_CACHE.get(key)
    if vector is None:
        return None
    _CHUNK_CACHE.move_to_end(key)
    return list(vector)


def _chunk_cache_put(key: tuple[str, str, str, str, str], vector: list[float]) -> None:
    _CHUNK_CACHE[key] = tuple(vector)
    _CHUNK_CACHE.move_to_end(key)
    while len(_CHUNK_CACHE) > CHUNK_CACHE_MAX:
        _CHUNK_CACHE.popitem(last=False)


def _schema_hash(compiled: dict | None) -> str:
    """编译 schema → 稳定哈希（键排序；不可序列化时回退 `repr`，仍然确定）。"""
    try:
        payload = json.dumps(compiled or {}, sort_keys=True, default=str)
    except (TypeError, ValueError):
        payload = repr(compiled)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _text_hash(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _cosine(left: list[float] | None, right: list[float] | None) -> float:
    """余弦相似度；任一为零向量/维度不符 → 0（零向量没有方向，不能当成“相似”）。"""
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if norm_left == 0.0 or norm_right == 0.0:
        return 0.0
    return dot / (norm_left * norm_right)


async def _default_embed(text: str, *, tenant_id: str = "", kb_id: str = "",
                         trace_id: str | None = None):
    """默认 embedding：**延迟 import**（`ontology_embedding` 在模块级拉 openai）。"""
    from jonex_core.common.ontology_embedding import embed
    return await embed(text, tenant_id=tenant_id or None, kb_id=kb_id or None,
                       trace_id=trace_id)


async def _call_embed(fn, text: str, tenant_id: str, kb_id: str,
                      trace_id: str | None = None):
    """调用注入的 `embed_fn`；兼容只收文本的替换实现。"""
    try:
        return await fn(text, tenant_id=tenant_id, kb_id=kb_id, trace_id=trace_id)
    except TypeError as exc:
        if "unexpected keyword" not in str(exc):
            raise
        return await fn(text)


async def _embedding_scores(
        chunks: list[TableChunk], question: str, shortlist: list[int], *,
        tenant_id: str, kb_id: str, ds_id: str, schema_hash: str,
        embed_fn, timeout: float, trace_id: str | None = None,
) -> tuple[list[float], bool, int, int]:
    """S4：短名单表的 embedding 余弦 → `(scores, degraded, calls, elapsed_ms)`。

    预算按**全局截止时刻**算：每个请求各自 `asyncio.wait_for(剩余预算)`，所以并发跑完
    仍不超总预算，且超时/失败的表只丢自己的分（“按已完成部分算”）。
    失败（异常 / 返回 None）一律记 `degraded=True`，**从不抛**。
    """
    if not shortlist:
        return [0.0 for _ in chunks], False, 0, 0

    started = time.monotonic()
    deadline = started + max(0.0, float(timeout))
    fn = embed_fn or _default_embed
    calls = 0

    query_key = (str(question or ""), str(tenant_id or ""))
    query_vector = _query_cache_get(query_key)

    # 请求清单：`("query"/"chunk", 块下标, 文本, 表缓存键)`。
    requests: list[tuple[str, int, str, tuple | None]] = []
    if query_vector is None:
        requests.append(("query", -1, str(question or ""), None))
    table_vectors: dict[int, list[float]] = {}
    for index in shortlist:
        chunk = chunks[index]
        cache_key = (str(tenant_id or ""), str(kb_id or ""), str(ds_id or ""),
                     schema_hash, _text_hash(chunk.text))
        cached = _chunk_cache_get(cache_key)
        if cached is not None:
            table_vectors[index] = cached
        else:
            requests.append(("chunk", index, chunk.text, cache_key))

    async def run(kind: str, index: int, text: str, cache_key: tuple | None):
        nonlocal calls
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            raise asyncio.TimeoutError("schema retrieval embedding 预算已用完")
        calls += 1
        return await asyncio.wait_for(
            _call_embed(fn, text, str(tenant_id or ""), str(kb_id or ""),
                        trace_id=trace_id),
            timeout=remaining,
        )

    results = await asyncio.gather(
        *[run(*request) for request in requests], return_exceptions=True)

    degraded = False
    for request, result in zip(requests, results):
        if isinstance(result, BaseException) or result is None:
            degraded = True
            continue
        try:
            vector = [float(value) for value in result]
        except (TypeError, ValueError):
            degraded = True
            continue
        if request[0] == "query":
            query_vector = vector
            _query_cache_put(query_key, vector)
        else:
            table_vectors[request[1]] = vector
            if request[3] is not None:
                _chunk_cache_put(request[3], vector)

    scores = [0.0] * len(chunks)
    if query_vector:
        for index in shortlist:
            vector = table_vectors.get(index)
            if vector:
                scores[index] = _cosine(query_vector, vector)

    elapsed_ms = int(round((time.monotonic() - started) * 1000))
    return scores, degraded, calls, elapsed_ms


# ---------------------------------------------------------------------------
# 候选合成
# ---------------------------------------------------------------------------

def _ordered_query_tokens(question: str) -> list[str]:
    """问题词元按**首次出现**排序（`tokenize` 返回集合，丢了顺序）。

    与 `tokenize` 同一口径（ASCII 长度 ≥2 casefold + CJK 二元组），只多了顺序，
    供 `matched_term` / `_matches` 稳定取名。
    """
    spans: list[tuple[int, str]] = []
    raw = str(question or "")
    for match in _ASCII_WORD.finditer(raw):
        word = match.group()
        if len(word) >= MIN_ASCII_TOKEN:
            spans.append((match.start(), word.casefold()))
    for match in _CJK_RUN.finditer(raw):
        run = match.group()
        if len(run) == 1:
            spans.append((match.start(), run))
        else:
            spans.extend((match.start() + offset, run[offset:offset + 2])
                         for offset in range(len(run) - 1))
    spans.sort()
    ordered: list[str] = []
    seen: set[str] = set()
    for _, token in spans:
        if token not in seen:
            seen.add(token)
            ordered.append(token)
    return ordered


def _hit_columns(chunk: TableChunk, hits: dict[str, float],
                 *, limit: int = MAX_CANDIDATE_COLUMNS) -> tuple[str, ...]:
    """命中列按「命中数降序、列序升序」取前 `limit` 个（确定性）。

    ⚠️ 按**列**迭代，不按名字：`column_names` 在物理名与逻辑名不同时各占一项，
    `column_hits` 也按两个键各存一份相同的值 —— 按名字迭代会让一列吃两个槽位，并把
    **逻辑名**当成物理列交给 Task 4 注入预览（预览列是物理名）。这里每组
    `(物理名, 逻辑名)` 只保留**物理名**（物理名缺失才回退逻辑名）。
    """
    pairs = chunk.column_pairs or tuple((name, name) for name in chunk.column_names)
    scored: list[tuple[float, int, str]] = []
    seen: set[str] = set()
    for position, (physical, logical) in enumerate(pairs):
        name = physical or logical
        if not name or name in seen:
            continue
        value = hits.get(physical or logical)
        if value is None:
            value = hits.get(logical, 0.0)
        value = float(value or 0.0)
        if value > 0.0:
            seen.add(name)
            scored.append((-value, position, name))
    scored.sort()
    return tuple(name for _, _, name in scored[:max(0, int(limit))])


def _matched_term(question: str, chunk: TableChunk) -> str:
    """最强命中词：按「表 → 列 → 证据值」优先级，同级取问题里最先出现的。"""
    ordered = _ordered_query_tokens(question)
    column_tokens = _column_union(chunk)
    for pool in (chunk.tokens, column_tokens, chunk.value_tokens):
        for token in ordered:
            if token in pool:
                return token
    return ""


def _candidate_matches(question: str, chunk: TableChunk,
                       embed_score: float, graph_score: float) -> list[dict]:
    """`_matches`：全部命中词 + 级别（`table|column|value`），再补 `embedding`/`graph`。"""
    ordered = _ordered_query_tokens(question)
    column_tokens = _column_union(chunk)
    matches: list[dict] = []
    for kind, pool in (("table", chunk.tokens),
                       ("column", column_tokens),
                       ("value", chunk.value_tokens)):
        for token in ordered:
            if token in pool:
                matches.append({"term": token, "kind": kind})
    if embed_score > 0.0:
        matches.append({"term": "", "kind": "embedding"})
    if graph_score > 0.0:
        matches.append({"term": "", "kind": "graph"})
    return matches


def _top_indices(scores: list[float], limit: int) -> list[int]:
    """分数前 `limit` 的块下标（降序；同分按块序）。"""
    if limit <= 0:
        return []
    order = sorted(range(len(scores)), key=lambda index: (-float(scores[index]), index))
    return order[:limit]


def _shortlist_indices(s1: list[float], s2: list[float], s3: list[float],
                       limit: int, *, names: list[str] | None = None) -> list[int]:
    """S1/S2/S3 各自前 `limit` 的**并集**，按「最好名次、块序」排，再截到 `limit`。

    词面三路**全部无证据**（并集为空）时退化为「按表名、块序」的前 `limit` 块：
    嵌入是此时唯一还有分辨力的信号，不能因为词面空就整段跳过 S4。
    """
    if limit <= 0:
        return []
    best: dict[int, int] = {}
    for scores in (s1, s2, s3):
        for index, rank in enumerate(rrf_ranks(scores)):
            # `-1` = 该路无证据，不能当成「排在所有人前面」。
            if 0 <= rank < limit:
                best[index] = min(best.get(index, rank), rank)
    if not best:
        order = sorted(
            range(len(s1)),
            key=lambda index: (
                (str(names[index]) if names is not None and index < len(names) else ""),
                index))
        return order[:limit]
    ordered = sorted(best, key=lambda index: (best[index], index))
    return ordered[:limit]


def _seed_indices(s1: list[float], s2: list[float], limit: int = 3) -> list[int]:
    """S5 种子 = S1/S2 前 `limit` 张**有正分**的表的并集（全 0 时不给种子）。"""
    if limit <= 0:
        return []
    best: dict[int, int] = {}
    for scores in (s1, s2):
        for rank, index in enumerate(_top_indices(scores, limit)):
            if float(scores[index]) <= 0.0:
                continue
            best[index] = min(best.get(index, rank), rank)
    return sorted(best, key=lambda index: (best[index], index))


async def retrieve_candidates(
        question: str, *, tenant_id: str, kb_id: str, ds_id: str,
        compiled: dict, abstract_entities: list[dict],
        embed_fn=None, top_k: int = 8, embed_topn: int = 20,
        mmr_lambda: float = DEFAULT_MMR_LAMBDA, timeout: float = 8.0,
        trace_id: str | None = None,
) -> dict:
    """五路信号 → RRF → MMR → 抽象实体候选（N1-2 的编排入口）。

    返回 `{"candidates", "unmapped", "columns", "stats"}`：

    - `candidates`：只含有抽象实体映射的表；按 `_score` 降序、`entity_name` 升序。
      每条带 `concept/display_name/knowledge_base_id/data_source_id/entity_name/
      match_kind="schema_retrieval"/matched_term` 与私有键
      `_score/_matches/_attr_hits/_columns`（消费方须剥私有键后再外露）。
    - `unmapped`：映射不到抽象实体的表名（只进 scene/日志，不合成候选）。
    - `columns`：`{table: [命中列, ...]}`，供 Task 4 列注入。
    - `stats`：`embed_degraded/embed_calls/embed_ms/chunks/shortlist/
      unmapped_count/unmapped_top`（`unmapped_top` 是前 5 个表名）。

    候选资格 = **至少一路信号为正**（S1/S2/S3/S4/S5）。全 0 的块不参与排序，
    也不得靠 schema 顺序变成候选；所有块都无证据时返回空候选。

    embedding 失败、超时、未配置都只把 S4 记 0 并置 `embed_degraded=True`，**从不抛**。
    """
    tenant = str(tenant_id or "")
    kb = str(kb_id or "")
    ds = str(ds_id or "")
    entities = [item for item in (abstract_entities or []) if isinstance(item, dict)]
    chunks = build_chunks(compiled or {}, entities, ds)

    stats = {"embed_degraded": False, "embed_calls": 0, "embed_ms": 0,
             "chunks": len(chunks), "shortlist": [],
             "unmapped_count": 0, "unmapped_top": []}
    if not chunks:
        return {"candidates": [], "unmapped": [], "columns": {}, "stats": stats}

    table_names = [chunk.table for chunk in chunks]
    s1 = table_lexical_scores(question, chunks)
    s2, column_hits = column_lexical_scores(question, chunks)
    s3 = value_scores(question, chunks)
    shortlist = _shortlist_indices(s1, s2, s3, max(0, int(embed_topn)),
                                   names=table_names)
    s5 = graph_scores(chunks, (compiled or {}).get("relation_types") or [],
                      _seed_indices(s1, s2))
    s4, degraded, calls, elapsed_ms = await _embedding_scores(
        chunks, question, shortlist,
        tenant_id=tenant, kb_id=kb, ds_id=ds,
        schema_hash=_schema_hash(compiled), embed_fn=embed_fn, timeout=timeout,
        trace_id=trace_id)
    stats.update(embed_degraded=degraded, embed_calls=calls,
                 embed_ms=elapsed_ms,
                 shortlist=[chunks[index].table for index in shortlist])

    ranks = [rrf_ranks(scores, names=table_names)
             for scores in (s1, s2, s3, s4, s5)]
    fused = rrf_fuse(ranks, list(SIGNAL_WEIGHTS))
    max_rrf = max(fused) if fused else 0.0

    # 候选资格：至少一路信号为正。全 0 的块不排序、不进候选池 —— 否则 RRF 的
    # `1/(60+rank)` 会按 schema 顺序给无关表发「证据」。
    all_scores = (s1, s2, s3, s4, s5)
    eligible = [index for index in range(len(chunks))
                if any(float(scores[index]) > 0.0 for scores in all_scores)]

    # 候选池：RRF 前 `top_k * 2`，再 MMR 迭代选 `top_k`（列集合 Jaccard 去重）。
    limit = max(0, int(top_k))
    selected: list[int] = []
    if limit and eligible:
        pool = sorted(eligible,
                      key=lambda index: (-fused[index],
                                         chunks[index].entity_name, index))
        pool = pool[:min(len(pool), limit * 2)]
        picked = mmr_select([fused[index] for index in pool],
                            [chunks[index] for index in pool],
                            top_k=limit, lambda_=mmr_lambda)
        selected = [pool[position] for position in picked]

    unmapped: list[str] = []
    candidates: list[dict] = []
    columns: dict[str, list[str]] = {}
    for chunk in chunks:
        if (not _match_abstract(chunk.entity_name, ds, entities)
                and chunk.table not in unmapped):
            unmapped.append(chunk.table)
    stats["unmapped_count"] = len(unmapped)
    stats["unmapped_top"] = unmapped[:5]
    for index in selected:
        chunk = chunks[index]
        abstracts = _match_abstract(chunk.entity_name, ds, entities)
        if not abstracts:
            continue
        abstract = abstracts[0]
        hits = column_hits[index] if index < len(column_hits) else {}
        hit_columns = _hit_columns(chunk, hits)
        candidates.append({
            "concept": str(abstract.get("name") or "").strip(),
            "display_name": (str(abstract.get("display_name") or "").strip()
                             or str(abstract.get("name") or "").strip()),
            "knowledge_base_id": kb,
            "data_source_id": ds,
            "entity_name": chunk.entity_name,
            "match_kind": "schema_retrieval",
            "matched_term": _matched_term(question, chunk),
            # 1000 倍三位小数：保留检索池内部的近似并列次序（10 倍整数会把
            # 融合比 0.98~1.0 的候选压成同一个桶，服务层排序再退回 entity_name）。
            "_score": (round(1000.0 * fused[index] / max_rrf, 3)
                       if max_rrf > 0.0 else 0.0),
            "_matches": _candidate_matches(question, chunk, s4[index], s5[index]),
            "_attr_hits": list(hit_columns),
            "_columns": list(hit_columns),
        })
        if hit_columns:
            columns[chunk.table] = list(hit_columns)

    candidates.sort(key=lambda candidate: (-candidate["_score"],
                                           candidate["entity_name"]))
    return {"candidates": candidates, "unmapped": unmapped,
            "columns": columns, "stats": stats}


__all__ = ["TableChunk", "tokenize", "build_chunks",
           "table_lexical_scores", "column_lexical_scores", "value_scores",
           "graph_scores", "rrf_ranks", "rrf_fuse", "mmr_select",
           "retrieve_candidates", "clear_embedding_caches",
           "MAX_TEXT_CHARS", "VALUE_TOKEN_BUDGET", "MAX_VALUE_MATCHES",
           "DEFAULT_RRF_K", "DEFAULT_MMR_LAMBDA", "SIGNAL_WEIGHTS",
           "MAX_CANDIDATE_COLUMNS"]
