#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""落点召回：把多源 compiled bundle 压成一块一表的索引，并给文档概念做确定性打分。

这一层只回答「**哪些源表看起来像这个概念**」，不做任何 LLM 判断、不写库：

- `build_index`：逐源 `build_chunks`（与 NL→SQL 检索同一构造），关系边按 `sorted(bundles)`
  顺序合并。跨源同名实体的边归并只是 **best-effort**（边不带 ds_id），不承诺任何
  「先到者胜」的确定性结论；落点身份始终以 `(ds_id, entity_name)` 为准。
- `score_candidates`：S1 表级词面 / S2 列级词面 / S3 证据值 / S4 语义（由调用方传入）/
  S5 关系图，五路 RRF 融合；`verbatim`（名字逐字命中）优先，再按融合分、再按
  `(ds_id, entity_name)` 稳定。**纯函数**：同一输入必须给出同一顺序，且不改 `index`。
- `tier_of`：`verbatim` 且属性覆盖率 ≥ 0.6 才算 A 档（可自动落点），其余 B 档（交人看）。

**无证据的块不占候选位**：融合分为 0 且不是逐字命中的块直接丢弃（全 0 概念返回
`[]`，由 runner 归 C 档）；逐字命中即使其他信号全 0 也保留。`top_k` 只截断数量。
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field, replace

from ..evidence_probe import evidence_key
from ..nl2sql.schema_retrieval import (
    # 直接复用 NL→SQL 的 S4 实现（同为私有）：缓存键、并发预算、`_cosine` 口径
    # 只有一份 —— 复制一份必然在「超时按已完成部分算」这类细节上分叉。
    _embedding_scores,
    TableChunk,
    build_chunks,
    column_lexical_scores,
    graph_scores,
    rrf_fuse,
    rrf_ranks,
    table_lexical_scores,
    value_scores,
)
# 归一化复用同义词链上那一个：同一把尺子（trim + 全角转半角 + casefold）才保证
# 「概念名 == 表名」的判定与保存/去重侧一致；各写一份必然在边角字符上分叉。
from ..synonym_normalizer import _normalize_key

#: S1/S2/S3/S4/S5 的 RRF 权重。词面（S1/S2）与证据值（S3）是落点的主判据，
#: 语义（S4）同为强信号但可降级，图（S5）只做邻接补充。
SIGNAL_WEIGHTS: tuple[float, ...] = (0.5, 0.4, 0.6, 0.4, 0.3)

#: A 档要求的属性覆盖率下限：只命中表名但列全不对，仍需人工确认。
TIER_A_COVERAGE = 0.6

#: 标识符拆词：驼峰边界（`MesQcRecord` → Mes/Qc/Record）。只用于词面召回。
_IDENT_PART = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")


def _identity_key(value: str) -> str:
    """标识符身份键：casefold + 去掉所有非字母数字。

    `WorkOrder` / `work_order` / `WORK-ORDER` 是同一个标识符；`MesQcRecord` 与
    `qc_record` 则**不相等**（多一个 Mes 前缀），后者交给词元交集，不能冒充
    逐字命中 —— 这正是 A/B 首轮里 `qc_record` 该进 B 档而不是自动接受的原因。
    """
    folded = _normalize_key(value)
    return re.sub(r"[^0-9a-z㐀-鿿]", "", folded)


def _split_identifier(value: str) -> set[str]:
    """按 `_`/`-`/空白 + 驼峰边界拆词，长度 <2 的碎片丢弃。

    `MesQcRecord` → {mes, qc, record}；`work_order` → {work, order}。
    线上缺口：概念 `work_order` 与表 `WorkOrder` 在**旧分词**下没有任何共同
    词元（下划线算词内字符），候选为空，agent 只能 give_up。
    """
    tokens: set[str] = set()
    for word in re.split(r"[^0-9A-Za-z]+", str(value or "")):
        for part in _IDENT_PART.findall(word):
            token = part.casefold()
            if len(token) >= 2:
                tokens.add(token)
    return tokens


def _augment_chunk(chunk: TableChunk) -> TableChunk:
    """给块的词元补上拆词形态，供 S1/S2 词面命中。

    不改 `schema_retrieval.build_chunks`：那份同时服务 ② NL→SQL 检索，动它等于
    改现有评分（有回归风险）；落点召回自己补一层，口径只在这里生效。
    """
    # ⚠️ **只拆实体名/表名，不拆列名**：列名里的 `code`/`name`/`id` 是通用词，
    # 拆出来后会让任意两张表都沾上同一个词元（`asset_name` × `warehouse_code`），
    # 把「召回」退化成噪声。列语义交给 S2（整词精确）与身份键覆盖率。
    extra: set[str] = set()
    for name in (chunk.entity_name, chunk.table):
        extra |= _split_identifier(name)
    return replace(chunk, tokens=frozenset(set(chunk.tokens) | extra))


@dataclass(frozen=True)
class Candidate:
    """一个候选源表（跨源可比：`(ds_id, entity_name)` 是身份）。"""

    ds_id: str
    entity_name: str
    table: str
    score: float
    verbatim: bool
    coverage: float
    signals: dict[str, float]


@dataclass
class SourceIndex:
    """一次落点 run 内固定的召回索引；`chunks` 与 `chunk_ds` 一一对齐。

    `seed_indices` 由调用方在本轮已确定性落点后回填（图信号的种子），
    因此它是**可变**字段 —— 其余字段在 `build_index` 后不再修改。
    """

    chunks: list[TableChunk]
    chunk_ds: list[str]
    relation_types: list[dict]
    entities: dict[tuple[str, str], dict]
    seed_indices: list[int] = field(default_factory=list)
    #: `(ds_id, entity_name)` → 该表的证据列字典（`{"列名": {...samples...}}`）。
    #: 默认空：Task 4/5 的既有构造与测试不必知道证据，工具层按 ref 查不到时
    #: 自然退化为「无证据」，而不是 AttributeError。
    evidence: dict[tuple[str, str], dict] = field(default_factory=dict)


def build_index(bundles: dict[str, dict]) -> SourceIndex:
    """`{ds_id: bundle}` → 跨源块索引。

    bundle 形状：`{"name", "entities": {entity_name: entity}, "relations": [...],
    "evidence": {...}}`。`build_chunks` 只认 compiled 形状，故这里就地合成
    `{"entity_types": list(entities.values()), "evidence": evidence}` —— 不做任何
    字段改写，保证落点侧与 NL→SQL 侧看到的是同一份表结构。

    `relation_types` 是**普通拼接**：所有源的边倒进同一个按实体名建的**无向图**，
    没有跨源优先级，同名实体在图上就是同一个节点（已知局限：另一源的同名表会
    因某源的种子拿到距离 0 的 S5）。拼接顺序按 `sorted(bundles)` 只为让输出确定，
    不表达任何优先关系。
    """
    chunks: list[TableChunk] = []
    chunk_ds: list[str] = []
    relation_types: list[dict] = []
    entities: dict[tuple[str, str], dict] = {}
    evidence_map: dict[tuple[str, str], dict] = {}

    for raw_ds_id in sorted(bundles, key=str):
        ds_id = str(raw_ds_id or "").strip()
        bundle = bundles.get(raw_ds_id) or {}
        raw_entities = bundle.get("entities") or {}
        compiled = {
            "entity_types": list(raw_entities.values()),
            "evidence": bundle.get("evidence") or {},
        }
        ds_chunks = [_augment_chunk(chunk) for chunk in build_chunks(compiled, [], ds_id)]
        chunks.extend(ds_chunks)
        chunk_ds.extend([ds_id] * len(ds_chunks))

        for raw_name, entity in raw_entities.items():
            if not isinstance(entity, dict):
                continue
            # 键取编译实体的 `name`（与 `build_chunks` 的 `entity_name` 同源），
            # 缺失才回退 bundle 里的实体键 —— 否则候选按 `entity_name` 查不回原实体。
            name = str(entity.get("name") or raw_name or "").strip()
            if name:
                entities[(ds_id, name)] = entity
                # 证据表的键由写入方 `evidence_key` 决定（`source_object_id` 优先），
                # 这里必须走同一个函数，否则 `"  "`/缺失这类边角会让工具层静默取不到证据。
                tables = (bundle.get("evidence") or {}).get("tables") or {}
                entry = tables.get(evidence_key(entity)) if isinstance(tables, dict) else None
                columns = entry.get("columns") if isinstance(entry, dict) else None
                evidence_map[(ds_id, name)] = dict(columns) if isinstance(columns, dict) else {}

        for relation in bundle.get("relations") or []:
            if isinstance(relation, dict):
                relation_types.append(relation)

    return SourceIndex(chunks=chunks, chunk_ds=chunk_ds,
                       relation_types=relation_types, entities=entities,
                       evidence=evidence_map)


def concept_text(concept: dict) -> str:
    """概念 → 检索文本：名字/显示名/别名/描述 + 属性名与属性描述。

    属性描述必须进文本：概念层经常只有「资产编码」这类列语义，表名对不上，
    S2 列级词面是唯一能把它与 `asset_code` 连起来的信号。
    """
    data = concept or {}
    parts = [str(data.get(key) or "").strip()
             for key in ("name", "display_name", "description")]
    parts.extend(str(alias).strip() for alias in data.get("aliases") or [])
    # 概念名/显示名/别名额外补一份拆词形态：查询侧与块侧都要有 `work`/`order`
    # 这类词元，S1 才能在 snake_case ↔ CamelCase 之间命中。**属性名不拆**：
    # 列里的 `code`/`name` 是通用词，拆了会让所有表都沾上同一个词元。
    identifiers = [str(data.get("name") or "").strip(),
                   str(data.get("display_name") or "").strip()]
    identifiers.extend(str(alias).strip() for alias in data.get("aliases") or [])
    for attribute in data.get("attributes") or []:
        if not isinstance(attribute, dict):
            continue
        parts.append(str(attribute.get("name") or "").strip())
        parts.append(str(attribute.get("description") or "").strip())
    for identifier in identifiers:
        split = _split_identifier(identifier)
        if split:
            parts.append(" ".join(sorted(split)))
    return " ".join(part for part in parts if part)


def _verbatim_flags(concept: dict, chunks: list[TableChunk]) -> list[bool]:
    """概念名/别名归一后是否逐字等于块的表名或实体名。

    只认 `name` 与 `aliases`，**不认 `display_name`**：display_name 是给人看的
    泛化标签（如「订单明细」的显示名是「订单」），拿它判逐字等于会把只沾了标签
    的表自动推进 A 档 —— 那正是需要人工确认的情形。

    判定走 `_identity_key`（`_normalize_key` 之后再去掉分隔符）：`Asset` /
    `asset` / `work_order` / `WorkOrder` 都算同一个标识符；`WorkOrder` 与
    `WorkOrderLog` 不相等。这比词元交集更严格 —— 是「就是这张表」的证据。
    """
    wanted: set[str] = set()
    value = str((concept or {}).get("name") or "").strip()
    if value:
        wanted.add(_identity_key(value))
    for alias in (concept or {}).get("aliases") or []:
        value = str(alias).strip()
        if value:
            wanted.add(_identity_key(value))

    flags: list[bool] = []
    for chunk in chunks:
        names = {_identity_key(chunk.entity_name), _identity_key(chunk.table)}
        flags.append(bool(wanted & {name for name in names if name}))
    return flags


def _coverage_flags(concept: dict, chunks: list[TableChunk]) -> list[float]:
    """概念属性名落在块列名集合里的比例；概念没有属性时记 0.0。

    「比例」而不是「个数」：属性多的概念不该因为绝对命中多就压过属性少的精确概念；
    概念无属性时没有可核对的列，给 0.0（不是 1.0），避免 A 档被空概念白拿。

    归一后先**去重**：同一属性在概念里重复出现（`{name:'code'}` 写两遍）是录入噪声，
    不能靠重复把覆盖率抬上去（否则 `code`×2 命中 1 列也显示 1.0）。
    """
    attributes: list[str] = []
    seen: set[str] = set()
    for attribute in (concept or {}).get("attributes") or []:
        if not isinstance(attribute, dict):
            continue
        name = str(attribute.get("name") or "").strip()
        if not name:
            continue
        key = _identity_key(name)
        if key in seen:
            continue
        seen.add(key)
        attributes.append(key)
    if not attributes:
        return [0.0 for _ in chunks]

    coverage: list[float] = []
    for chunk in chunks:
        # 列名同样走身份键：概念 `work_order_code` 与列 `workOrderCode` 是同一列。
        column_keys = {_identity_key(name) for name in chunk.column_names}
        hits = sum(1 for name in attributes if name in column_keys)
        coverage.append(hits / len(attributes))
    return coverage


def _aligned_embed_scores(embed_scores: list[float] | None,
                          size: int) -> list[float]:
    """把调用方给的语义分对齐到块数：缺失补 0、多余截断（降级时就是全 0）。"""
    if not embed_scores:
        return [0.0] * size
    values = [float(value) for value in list(embed_scores)[:size]]
    if len(values) < size:
        values.extend([0.0] * (size - len(values)))
    return values


def score_candidates(concept: dict, index: SourceIndex, *,
                     embed_scores: list[float] | None = None,
                     top_k: int = 8) -> list[Candidate]:
    """概念 → 候选源表（verbatim 优先，再融合分降序，再 `(ds_id, entity_name)`）。

    五路信号全部复用 `schema_retrieval` 的实现（同一套 tokenize/RRF 口径），
    这里只做「分块 → 对齐 → 融合 → 排序」。函数无副作用：`index.seed_indices`
    只读，绝不回写（调用方在两次召回之间自己维护）。
    """
    chunks = index.chunks
    if not chunks:
        return []

    text = concept_text(concept)
    s1 = table_lexical_scores(text, chunks)
    s2, _ = column_lexical_scores(text, chunks)
    s3 = value_scores(text, chunks)
    s4 = _aligned_embed_scores(embed_scores, len(chunks))
    s5 = graph_scores(chunks, index.relation_types, index.seed_indices)

    # 名次并列时按 `ds:entity` 定序：块序来自 JSONB 解码的实体字典，虽确定但
    # 与「表名」无关；用身份串做并列键，跨源近似同分也不会被存储顺序左右。
    names = [f"{chunk.data_source_id}:{chunk.entity_name}" for chunk in chunks]
    ranks = [rrf_ranks(scores, names=names) for scores in (s1, s2, s3, s4, s5)]
    fused = rrf_fuse(ranks, list(SIGNAL_WEIGHTS))

    verbatim = _verbatim_flags(concept, chunks)
    coverage = _coverage_flags(concept, chunks)

    candidates: list[Candidate] = []
    for position, chunk in enumerate(chunks):
        # 无证据的块不占候选位：融合 0 且非逐字命中 = 这一轮没有任何信号指向它，
        # 让它进入 B 档会制造「看过但没看出」的假象。逐字命中例外 —— 名字就是证据。
        if fused[position] <= 0.0 and not verbatim[position]:
            continue
        ds_id = (index.chunk_ds[position]
                 if position < len(index.chunk_ds) else chunk.data_source_id)
        candidates.append(Candidate(
            ds_id=str(ds_id or chunk.data_source_id or ""),
            entity_name=chunk.entity_name,
            table=chunk.table,
            score=float(fused[position]),
            verbatim=verbatim[position],
            coverage=coverage[position],
            signals={"s1": float(s1[position]),
                     "s2": float(s2[position]),
                     "s3": float(s3[position]),
                     "s4": float(s4[position]),
                     "s5": float(s5[position])},
        ))

    # 三段排序：逐字命中永远在最前（哪怕融合分被其他信号压低）；同分用身份做
    # 全序，确保跨进程/跨批次的输出逐字节可比。
    candidates.sort(key=lambda candidate: (0 if candidate.verbatim else 1,
                                           -candidate.score,
                                           candidate.ds_id,
                                           candidate.entity_name))
    return candidates[:max(0, int(top_k))]


def tier_of(candidate: Candidate) -> str:
    """A 档 = 名字逐字命中且属性覆盖率 ≥ 0.6；其余一律 B 档。"""
    return ("A" if candidate.verbatim and candidate.coverage >= TIER_A_COVERAGE
            else "B")


def _source_schema_hash(chunks: list[TableChunk]) -> str:
    """某源块文本的稳定短哈希（进 embedding 缓存键）。

    只哈希该源的块文本：同源 schema 一变缓存就失效；与源顺序无关（块序由
    `build_index` 的 `sorted(bundles)` 固定），跨进程仍逐字节一致。
    """
    payload = "|".join(chunk.text for chunk in chunks)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


async def embed_scores_for_concept(
        concept: dict, index: SourceIndex, *,
        tenant_id: str, kb_id: str,
        embed_fn=None, timeout_s: float = 20.0,
) -> tuple[list[float], bool]:
    """概念 → 每块的语义分（S4），**有界且永不抛**。

    返回 `(scores, degraded)`：`scores` 与 `index.chunks` 一一对齐（失败源的块记
    0.0），`degraded` 表示至少有一路 embedding 没拿到（调用方据此决定是否信 S4）。

    按 `index.chunk_ds` **逐源**调用 `schema_retrieval._embedding_scores`：它的缓存
    键含 `ds_id`/`schema_hash`，一次传入跨源块会把不同源的 schema 混进同一个键。

    预算是**单一全局截止时刻**（`timeout_s`），各源顺序执行、各自只能消耗剩余额度：
    多源时宁可后面的源降级，也不让整轮落点等待成倍放大；源数再多总耗时不超
    `timeout_s`。任一源异常/超时只把它自己的分留 0，绝不上抛打断确定性召回。
    """
    chunks = index.chunks
    if not chunks:
        return [], False

    question = concept_text(concept)
    groups: dict[str, list[int]] = {}
    for position, chunk in enumerate(chunks):
        raw_ds = (index.chunk_ds[position]
                  if position < len(index.chunk_ds) else chunk.data_source_id)
        # 与 `build_index`/`build_chunks` 同一口径：ds_id 先 strip 再当分组键，
        # 否则 " d1" 与 "d1" 会各调一次 embedding、缓存也各存一份。
        groups.setdefault(str(raw_ds or "").strip(), []).append(position)

    scores = [0.0] * len(chunks)
    degraded = False
    deadline = time.monotonic() + max(0.0, float(timeout_s))

    for ds_id, positions in groups.items():
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            # 预算耗尽：剩余源全部按 0 记，并明确标记降级（不是「没有相似」）。
            degraded = True
            break
        subset = [chunks[position] for position in positions]
        # 降级粒度说明：源内失败由 `_embedding_scores` 按「已完成部分算」——
        # 只有失败/超时的**块**记 0，同源其余表的余弦仍保留并置 `degraded=True`；
        # 只有整个源拿不到任何向量（全失败或预算耗尽）时该源分数才全为 0。
        # 两种情形都只影响本源，绝不外溢到其他源，也绝不上抛。
        try:
            source_scores, source_degraded, _calls, _elapsed_ms = (
                await _embedding_scores(
                    subset, question, list(range(len(subset))),
                    tenant_id=tenant_id, kb_id=kb_id, ds_id=ds_id,
                    schema_hash=_source_schema_hash(subset),
                    embed_fn=embed_fn, timeout=remaining,
                )
            )
        except Exception:  # noqa: BLE001 - 语义召回是可选信号，不允许打断落点
            degraded = True
            continue
        degraded = degraded or bool(source_degraded)
        for local, global_position in enumerate(positions):
            if local < len(source_scores):
                scores[global_position] = float(source_scores[local])

    return scores, degraded


__all__ = ["Candidate", "SourceIndex", "build_index", "concept_text",
           "embed_scores_for_concept", "score_candidates", "tier_of",
           "SIGNAL_WEIGHTS", "TIER_A_COVERAGE"]
