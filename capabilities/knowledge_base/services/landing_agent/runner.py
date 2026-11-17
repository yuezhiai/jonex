#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""落点 Agent 的运行编排（设计 §5.6）：召回 → 判定 → 合并，并按概念上报进度。

这一层只做**编排**，不做 I/O、不碰库：召回（Task 4/5）、工具（Task 6）、有界循环
（Task 7）、合并守卫（Task 9）都是注入/直接调用的纯组件。两个职责：

1. **把「确定性」与「模型判断」串成一条流水线**：逐字命中的概念直接生成 A 档
   decision（`origin="name"`），其余交给有界循环；两类结果最后统一走
   `apply_decisions`，守卫与 provenance 只有一个入口。
2. **进度与失败隔离**：单概念异常只记 `unresolved` 并继续，绝不打断整批；每个
   概念处理完回调一次 `on_progress`（Task 12 用它写 run 的 progress/phase）。

三条容易写错的语义（与 Task 9 的守卫强耦合）：

- **不调 `attach_landings_by_name`**：它写出的是「无 origin」的历史形状，Task 9
  的规则 7 会把这种落点当受保护历史数据，永远升不成 agent/name。名字命中只用来
  生成 A 档 decision 与 `seed_indices`（图信号种子）。
- **既有 `origin="agent"` 落点必须钉进候选卡**：Task 9 规则 5 在 `decide` 时撤销
  「本次没再确认」的 agent 落点。召回（仅 top_k）没给出某张表、模型就看不到它，
  撤销就成了「召回漏掉的正确落点被静默删掉」。钉住的候选 `score=0`、`verbatim=False`、
  `coverage=0`（`signals.pinned` 明确标记，且不会因此进 A 档）。
- **取消只发生在概念之间，且中止时不落点**：`apply_decisions` 会原地改写落点，
  半途调用等于留下半份结果（构造语义是「失败/中止不落库」）。已判定的概念不算数，
  由调用方决定整 run 的终态。

一个 `LandingAgentRunner` 实例只服务一次 run（内部有当前 run 的进度/预算状态）。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Awaitable, Callable

from ..synonym_normalizer import _normalize_key
from .apply import ApplyStats, apply_decisions
from .loop import ConceptDecision, run_concept
from .recall import (
    Candidate,
    SourceIndex,
    build_index,
    embed_scores_for_concept,
    score_candidates,
    tier_of,
)
from .tools import ToolContext, build_cards

#: 配置默认值（设计 §5.10）。`max_tokens` 是**单概念**上限，不是单步 ——
#: 单步预算由 `ontology_llm.landing_agent_step` 自己的 2048 管，两处不要互相抄。
DEFAULT_CONFIG: dict = {
    "top_k": 8,
    "max_concepts": 300,
    "max_steps": 6,
    "max_tokens": 24000,
    "embedding": True,
}


@dataclass
class LandingProgress:
    """一次 run 的进度快照（Task 12 原样写进 run 的 progress jsonb）。

    计数口径：

    - `concepts_total`：截断/过滤后**实际参与**的概念数，run 开始时即确定。
    - `concepts_done`：已处理完的概念数（含异常/放弃）。
    - `landed`：产出至少一条落点的概念数。
    - `unresolved`：没产出落点的概念数（give_up、`decide` 但落点全空、异常）。
      因此 `landed + unresolved == concepts_done` 恒成立。
    - `low_confidence`：`landed` 中置信度为 `low` 的概念数（是 `landed` 的子集）。
    - `tool_calls`：循环实际 dispatch 的轮次（含 decide/give_up 终止轮）。Task 7
      只暴露 `steps`，这里按它累计；不是「模型调用次数」（协议修复会多调）。
    - `tokens`：仅默认 `call_step` 记账；注入 fake 时不累加（测试可控）。
    """

    phase: str = "recall"           # recall | judge | merge | done
    concepts_total: int = 0
    concepts_done: int = 0
    landed: int = 0
    unresolved: int = 0
    low_confidence: int = 0
    tool_calls: int = 0
    tokens: int = 0


class LandingAgentRunner:
    """一次落点 run 的编排器（接口见 Task 11 brief）。"""

    def __init__(self, *, tenant_id: str, kb_id: str, user_id: str = "",
                 config=None, call_step=None, embed_fn=None, probe=None,
                 probe_budget: int = 40):
        self.tenant_id = tenant_id
        self.kb_id = kb_id
        self.user_id = user_id
        self.embed_fn = embed_fn
        self.probe = probe
        #: 单概念判定内的 probe 次数上限（`ABSTRACT_CONSTRUCT_MAX_PROBE_QUERIES`）。
        self.probe_budget = max(0, int(probe_budget or 0))
        self._config = _merge_config(config)

        #: 注入的 call_step 用于测试/fake 模型；默认实现负责 token 记账。
        self._injected_step = call_step is not None
        self._call_step = call_step or self._default_step

        #: 当前 run 的可变状态（单实例单次 run，见模块 docstring）。
        self._progress = LandingProgress()
        self._concept_tokens = 0       # 当前概念的 token 用量（per-concept 预算）
        self._progress_warned = False
        self._embedding_degraded_warned = False

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    async def run(
        self,
        entities: list[dict],
        bundles: dict[str, dict],
        *,
        on_progress: Callable[[LandingProgress], Awaitable[None]] | None = None,
        cancel_check: Callable[[], Awaitable[bool]] | None = None,
    ) -> dict:
        """跑一整批概念，返回 `{"stats", "progress", "warnings"}`。

        `entities` 会被 `apply_decisions` **原地修改**（落点写入调用方给的概念
        列表）；`bundles` 只读，绝不改写。
        """
        bundles = bundles or {}
        progress = LandingProgress()
        self._progress = progress
        warnings: list[str] = []

        index = build_index(bundles)
        positions, chunks = _chunk_lookup(index)
        coverage = self._warn_on_evidence_gap(bundles, warnings)

        concepts = self._clip_concepts(entities, warnings)
        progress.concepts_total = len(concepts)
        progress.phase = "judge"

        decisions: dict[str, ConceptDecision] = {}
        cancelled = False

        for concept in concepts:
            # 取消只在概念之间检查：正在判定的概念要么完整结束，要么不开始。
            if cancel_check is not None and await cancel_check():
                cancelled = True
                break

            name = _concept_name(concept)
            if not name:
                # 无名概念无法进 decisions（Task 9 按概念名索引），判了也白判。
                warnings.append("概念缺少名称，已跳过判定")
                progress.unresolved += 1
                progress.concepts_done += 1
                await self._notify(on_progress, progress, warnings)
                continue

            self._concept_tokens = 0
            try:
                decision, seeds = await self._decide_concept(
                    concept, index, positions, chunks, warnings)
            except Exception as exc:  # noqa: BLE001 - 单概念异常绝不打断整批
                warnings.append(f"概念「{name}」判定异常：{exc}")
                decision, seeds = None, []

            if decision is None:
                progress.unresolved += 1
            else:
                if seeds:
                    index.seed_indices.extend(seeds)
                # 预检已按概念名去重，这里不会覆盖既有判定。
                decisions[name] = decision
                _account(progress, decision)

            progress.concepts_done += 1
            await self._notify(on_progress, progress, warnings)

        if cancelled:
            # 中止不落点：与「失败不落库」同口径，避免留下半份结果。
            # phase 保持「判定态」，不假装走完合并。
            stats = ApplyStats()
        else:
            progress.phase = "merge"
            stats = apply_decisions(entities, decisions, bundles)
            progress.phase = "done"

        return {"stats": asdict(stats), "progress": asdict(progress),
                "warnings": warnings, "coverage": coverage,
                "evidence_gap": bool(coverage.get("evidence_gap"))}

    # ------------------------------------------------------------------
    # 单概念判定
    # ------------------------------------------------------------------

    async def _decide_concept(
            self, concept: dict, index: SourceIndex,
            positions: dict[tuple[str, str], int],
            chunks: dict[tuple[str, str], object],
            warnings: list[str],
    ) -> tuple[ConceptDecision | None, list[int]]:
        """概念 → `(decision, seed_indices)`；A 档不调模型，B 档走有界循环。"""
        embed_scores = None
        if self._config["embedding"]:
            embed_scores, degraded = await embed_scores_for_concept(
                concept, index, tenant_id=self.tenant_id, kb_id=self.kb_id,
                embed_fn=self.embed_fn,
            )
            if degraded and not self._embedding_degraded_warned:
                # 只报一次：逐概念刷同一条 warning 会把真正的问题淹掉。
                self._embedding_degraded_warned = True
                warnings.append("语义召回（S4）降级：部分源未取得向量，按已完成部分计分")

        ranked = score_candidates(concept, index, embed_scores=embed_scores,
                                  top_k=self._config["top_k"])

        # A 档：名字/别名逐字命中且属性覆盖足够 —— 确定性判定，不花模型调用。
        # **前提**：概念上没有「A 档赢家之外的既有 agent 落点」。A 档不建卡，
        # 模型看不到那张别的表；若照样生成 decide，Task 9 规则 5 会把它当「本次
        # 不再确认」而撤销 —— 那正是「没展示给模型的落点被静默删掉」。
        # 有一条这样的落点就降级到 B 档，把它钉进卡片交给模型判断。
        exact = [candidate for candidate in ranked if tier_of(candidate) == "A"]
        # 种子与「档位」无关：逐字命中本身就是图信号种子。降级到 B 档时也要带上，
        # 否则「本该确定性落点」的概念会白白丢掉一次 S5 扩散。
        seeds = [position for position in
                 (positions.get(_identity(candidate)) for candidate in exact)
                 if position is not None]
        if exact and _agent_targets(concept) <= {_identity(c) for c in exact}:
            return _name_decision(concept, chunks, exact), seeds

        # B 档：把既有 agent 落点钉进候选集（否则 Task 9 的规则 5 会撤销看不到的落点）。
        candidates, missing = _pin_agent_landings(concept, ranked, chunks)
        for ds_id, entity_name in missing:
            warnings.append(
                f"既有 agent 落点指向不存在的源表（{ds_id}/{entity_name}），未钉入候选")
        refs = {f"c{i + 1}": candidate for i, candidate in enumerate(candidates)}
        cards = build_cards(list(refs.values()), index)
        ctx = ToolContext(index=index, candidates=refs, concept=concept,
                          probe=self.probe, probe_budget=self.probe_budget)
        decision = await run_concept(
            concept, cards, ctx, self._call_step,
            max_steps=self._config["max_steps"],
        )
        return decision, seeds

    # ------------------------------------------------------------------
    # 默认模型步（生产实现）
    # ------------------------------------------------------------------

    async def _default_step(self, messages: list[dict]) -> dict:
        """包一层 `ontology_llm.landing_agent_step`：记账 token + 单概念预算。

        - **延迟 import**：注入 fake `call_step` 的测试不必拉起 LLM 栈。
        - 单概念预算在**调用前**检查：超了就直接回一条 `give_up(budget_exhausted)`
          让循环按正常终止路径收口（不抛异常，不污染协议修复计数）。因为单步用量
          只有回包后才知道，预算最多被超出「一步」，这是有意的软上限。
        """
        if self._concept_tokens >= self._config["max_tokens"]:
            return {"action": "give_up", "args": {"reason": "budget_exhausted"}}

        from jonex_core.common import ontology_llm  # 延迟 import，见 docstring

        parsed = await ontology_llm.landing_agent_step(
            messages,
            tenant_id=self.tenant_id or None,
            kb_id=self.kb_id or None,
            user_id=self.user_id or None,
            # 空串 = 用默认模型（`ONTOLOGY_LLM_MODEL` 解析链），见任务步的 docstring。
            model=self._config.get("model") or None,
        )
        usage = parsed.get("_usage") if isinstance(parsed, dict) else None
        tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
        try:
            tokens = int(tokens or 0)
        except (TypeError, ValueError):
            tokens = 0
        if tokens > 0:
            self._concept_tokens += tokens
            self._progress.tokens += tokens
        return parsed

    # ------------------------------------------------------------------
    # 预检 / 回调
    # ------------------------------------------------------------------

    def _warn_on_evidence_gap(self, bundles: dict, warnings: list[str]) -> dict:
        """算证据覆盖率（总体 + 每源），低于 50% 时显式 warning 并标 `evidence_gap`。

        口径与 spec §5.1/§5.11 一致：覆盖率 = 有 samples 的列数 / 总列数。
        显式降级说明的价值在于复盘时能立刻区分「模型判错」与「本来就没有证据」。
        """
        coverage = _evidence_coverage(bundles)
        if coverage["evidence_gap"]:
            warnings.append(
                f"证据覆盖率 {coverage['coverage']:.0%}"
                f"（{coverage['sampled_columns']}/{coverage['total_columns']} "
                "列带 samples），低于 50%，落点退化为名字+关系判定")
        return coverage

    def _clip_concepts(self, entities, warnings: list[str]) -> list[dict]:
        concepts = [entity for entity in (entities or []) if isinstance(entity, dict)]
        # 先去重再截断：重名概念在 decisions 里只能留一条判定，先花掉上限只会
        # 白占名额、还会让 `landed` 高估真实落点数。
        concepts = _dedupe_concepts(concepts, warnings)
        limit = self._config["max_concepts"]
        if len(concepts) > limit:
            warnings.append(
                f"概念数 {len(concepts)} 超过上限 max_concepts={limit}，已截断处理")
            concepts = concepts[:limit]
        return concepts

    async def _notify(self, on_progress, progress: LandingProgress,
                      warnings: list[str]) -> None:
        """回调进度；进度上报失败只 warning，绝不打断真正的落点工作。"""
        if on_progress is None:
            return
        try:
            # 传副本：调用方若就地改字段，不能污染 runner 的计数。
            await on_progress(replace(progress))
        except Exception as exc:  # noqa: BLE001 - 可观测性不能反噬业务
            if not self._progress_warned:
                self._progress_warned = True
                warnings.append(f"进度回调失败（后续同类错误不再记录）：{exc}")


# ---------------------------------------------------------------------------
# 纯函数辅助（便于单测与复算）
# ---------------------------------------------------------------------------

def _merge_config(config) -> dict:
    """配置合并 + 归一：非法值回落默认值，不把坏配置带进循环。"""
    merged = dict(DEFAULT_CONFIG)
    if isinstance(config, dict):
        merged.update({key: value for key, value in config.items()
                       if value is not None})
    return {
        # `model` 空串 = 用默认模型（`ONTOLOGY_LLM_MODEL`）；只做去空白，不校验取值。
        "model": str(merged.get("model") or "").strip(),
        "top_k": _as_int(merged.get("top_k"), DEFAULT_CONFIG["top_k"]),
        "max_concepts": _as_int(merged.get("max_concepts"),
                                DEFAULT_CONFIG["max_concepts"]),
        "max_steps": _as_int(merged.get("max_steps"), DEFAULT_CONFIG["max_steps"]),
        "max_tokens": _as_int(merged.get("max_tokens"),
                              DEFAULT_CONFIG["max_tokens"]),
        "embedding": bool(merged.get("embedding", True)),
    }


def _as_int(value, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return int(default)


def _concept_name(concept) -> str:
    """概念名去空白；非 dict / 缺 name 回 ""（调用方按「无名」跳过）。"""
    if not isinstance(concept, dict):
        return ""
    value = concept.get("name")
    return "" if value is None else str(value).strip()


def _identity(candidate: Candidate) -> tuple[str, str]:
    """候选身份键：`(strip(ds_id), casefold(entity_name))`，与查找表同口径。"""
    return (str(candidate.ds_id or "").strip().casefold(),
            str(candidate.entity_name or "").strip().casefold())


def _chunk_lookup(index: SourceIndex):
    """一次 run 的两张查找表：身份键 → 块下标 / 块对象。"""
    positions: dict[tuple[str, str], int] = {}
    chunks: dict[tuple[str, str], object] = {}
    for position, chunk in enumerate(index.chunks):
        ds_id = (index.chunk_ds[position]
                 if position < len(index.chunk_ds) else chunk.data_source_id)
        key = (str(ds_id or "").strip().casefold(),
               str(chunk.entity_name or "").strip().casefold())
        positions[key] = position
        chunks[key] = chunk
    return positions, chunks


def _dedupe_concepts(concepts: list[dict], warnings: list[str]) -> list[dict]:
    """按概念名去重（保留首个），并显式 warning。

    `apply_decisions` 的 decisions 以概念名为键：重名概念只能保留一条判定，若两条
    都判，`landed` 会把「只写了一次」数成两个概念落点。无名概念不参与去重
    （循环里会单独记 warning 跳过）。
    """
    seen: set[str] = set()
    unique: list[dict] = []
    dropped = 0
    for concept in concepts:
        name = _concept_name(concept)
        if name:
            if name in seen:
                dropped += 1
                continue
            seen.add(name)
        unique.append(concept)
    if dropped:
        warnings.append(f"存在 {dropped} 个重名概念，已按概念名去重（保留首个）")
    return unique


def _evidence_totals(bundle) -> tuple[int, int]:
    """单个源 `(带 samples 的列数, 总列数)`；形状异常按「没有证据」跳过。"""
    if not isinstance(bundle, dict):
        return 0, 0
    evidence = bundle.get("evidence")
    tables = evidence.get("tables") if isinstance(evidence, dict) else None
    if not isinstance(tables, dict):
        return 0, 0
    sampled = total = 0
    for entry in tables.values():
        columns = entry.get("columns") if isinstance(entry, dict) else None
        if not isinstance(columns, dict):
            continue
        for column in columns.values():
            total += 1
            if isinstance(column, dict) and column.get("samples"):
                sampled += 1
    return sampled, total


def _evidence_coverage(bundles: dict) -> dict:
    """证据覆盖率快照：总体 + 每源（spec §5.11）。

    `coverage` 是 0..1 的比例；总列数为 0 时按 0.0 处理并标 gap（没有任何列
    证据与「有列但都没样本」对判定是同一种降级）。`per_source` 的键是
    `data_source_id`（字符串），前端/排障可直接按源定位。
    """
    per_source: dict[str, dict] = {}
    sampled_total = total_columns = 0
    for ds_id, bundle in (bundles or {}).items():
        sampled, total = _evidence_totals(bundle)
        sampled_total += sampled
        total_columns += total
        per_source[str(ds_id)] = {
            "sampled_columns": sampled,
            "total_columns": total,
            "coverage": (sampled / total) if total else 0.0,
        }
    ratio = (sampled_total / total_columns) if total_columns else 0.0
    return {
        "coverage": ratio,
        "sampled_columns": sampled_total,
        "total_columns": total_columns,
        "per_source": per_source,
        "evidence_gap": ratio < 0.5,
    }


def _column_mappings(concept: dict, chunk) -> list[dict]:
    """概念属性 → 源真实列（只收归一后同名的属性；`column` 写物理列名）。

    与 `recall._coverage_flags` 同一把尺子（`_normalize_key`）：覆盖率 ≥ 0.6 才算
    A 档，所以这里命中率天然够高；映射不出来的属性留空（不编造列）。
    """
    wanted: dict[str, str] = {}
    for attribute in concept.get("attributes") or []:
        if not isinstance(attribute, dict):
            continue
        name = str(attribute.get("name") or "").strip()
        if name:
            wanted.setdefault(_normalize_key(name), name)

    mappings: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for physical, logical in getattr(chunk, "column_pairs", ()) or ():
        column = str(physical or logical or "").strip()
        if not column:
            continue
        attribute_name = (wanted.get(_normalize_key(column))
                          or wanted.get(_normalize_key(str(logical or ""))))
        if attribute_name is None:
            continue
        key = (attribute_name.casefold(), column.casefold())
        if key in seen:
            continue
        seen.add(key)
        mappings.append({"attribute": attribute_name, "column": column})
    return mappings


def _name_decision(concept: dict, chunks: dict[tuple[str, str], object],
                   candidates: list[Candidate]) -> ConceptDecision:
    """A 档：完整的、可直接写库的 decision（`origin="name"`，置信度 high）。"""
    landings: list[dict] = []
    for position, candidate in enumerate(candidates, start=1):
        chunk = chunks.get(_identity(candidate))
        landings.append({
            "ref": f"a{position}",
            "ds_id": candidate.ds_id,
            "entity_name": candidate.entity_name,
            # 覆盖率达标但个别属性可能对不上 —— 只写能对上的真实列。
            "attributes": _column_mappings(concept, chunk) if chunk else [],
        })
    coverage = (min((candidate.coverage for candidate in candidates), default=0.0))
    return ConceptDecision(
        "decide", landings, "high",
        f"名字/别名逐字命中源表；属性覆盖率 {coverage:.0%}",
        origin="name",
    )


def _active_agent_sources(concept: dict) -> list[tuple[str, str]]:
    """概念上仍然有效的既有 agent 落点目标 `(ds_id, entity_name)`。

    只收 `origin="agent"` 且未排除（`included != false`）的落点：`included: false`
    是「已排除」，Task 9 永不复活它，也就不存在「被误撤销」的问题。
    """
    if not isinstance(concept, dict):
        return []
    targets: list[tuple[str, str]] = []
    for source in concept.get("sources") or []:
        if not isinstance(source, dict):
            continue
        if str(source.get("origin") or "").strip().casefold() != "agent":
            continue
        if source.get("included") is False:
            continue
        ds_id = str(source.get("data_source_id") or "").strip()
        entity_name = str(source.get("entity_name") or "").strip()
        if ds_id and entity_name:
            targets.append((ds_id, entity_name))
    return targets


def _agent_targets(concept: dict) -> set[tuple[str, str]]:
    """既有 agent 落点的身份键集合（与 `_identity` 同口径）。"""
    return {(ds_id.casefold(), entity_name.casefold())
            for ds_id, entity_name in _active_agent_sources(concept)}


def _pin_agent_landings(
        concept: dict, ranked: list[Candidate],
        chunks: dict[tuple[str, str], object],
) -> tuple[list[Candidate], list[tuple[str, str]]]:
    """把概念既有 `origin="agent"` 的落点标成候选（`pinned`）。

    返回 `(candidates, missing)`：`missing` 是源表已不存在、钉不进去的目标（调用方
    记 warning，不静默）。

    两种情形都要带 `pinned` 标记：

    - 召回**没给出**：追加一条 `score=0`/`verbatim=False`/`coverage=0` 的候选
      —— 它是「必须让模型看到」的旧落点，不是逐字命中的证据（设 `verbatim=True`
      会让它排到最前，甚至被误判进 A 档）；
    - 召回**已给出**：原位打标，不复制。召回命中不等于「上轮落过」，模型需要
      这个区别来决定是保留还是放弃（提示词第 7 条）。
    """
    positions = {_identity(candidate): index for index, candidate in enumerate(ranked)}
    candidates: list[Candidate] = list(ranked)
    missing: list[tuple[str, str]] = []
    for ds_id, entity_name in _active_agent_sources(concept):
        key = (ds_id.casefold(), entity_name.casefold())
        index = positions.get(key)
        if index is not None:
            candidate = candidates[index]
            if not candidate.signals.get("pinned"):
                candidates[index] = replace(
                    candidate,
                    signals={**candidate.signals, "pinned": 1.0},
                )
            continue
        chunk = chunks.get(key)
        if chunk is None:
            missing.append((ds_id, entity_name))
            continue
        positions[key] = len(candidates)
        candidates.append(Candidate(
            ds_id=ds_id,
            entity_name=chunk.entity_name,
            table=chunk.table,
            score=0.0,
            verbatim=False,
            coverage=0.0,
            # 显式标记：排障时能区分「钉住的旧落点」与「真实召回」。
            signals={"pinned": 1.0},
        ))
    return candidates, missing


def _account(progress: LandingProgress, decision: ConceptDecision) -> None:
    """按判定结果记账；`landed + unresolved == concepts_done` 的不变量在这里维护。"""
    progress.tool_calls += max(0, int(getattr(decision, "steps", 0) or 0))
    landed = (decision.action == "decide"
              and isinstance(decision.landings, list) and bool(decision.landings))
    if not landed:
        # give_up / decide 但落点全空：都没有产出落点。
        progress.unresolved += 1
        return
    progress.landed += 1
    if decision.confidence == "low":
        progress.low_confidence += 1


__all__ = ["DEFAULT_CONFIG", "LandingAgentRunner", "LandingProgress"]
