"""Traversal controller for the wiki query engine (lwp-arch-001 QA-3).

Extracted verbatim from ``engines/llm_wiki_plus/query_algorithm.py``: the
deterministic tool-execution loop (``_exec_tool``, ``_execute_read``,
``_validate_read_path``), message builders (``_system_message``,
``_user_message``, ``_answer_target_hint``, ``_decompose_messages``,
``_assistant_tool_message``, ``_tool_message``), the control-event publishing
helpers (``_trace_control_events``, ``_publish_control_transition``,
``_publish_incomplete_terminal``), the frozen tool contract (``QUERY_TOOLS``),
question decomposition (``_decompose_question``) and the controller budget
configuration.  The orchestration root ``run_query_algorithm`` stays on the
legacy module.  Pure: no provider, network, filesystem, storage or database
access happens here.  The old import path stays available through the
re-export shim.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, MutableMapping, Optional, Sequence

from pydantic import ValidationError

from jonex_wiki.kernel.contracts import (
    QueryEventType,
    QueryStrategy,
    TraceSpan,
    TraceSpanType,
)
from jonex_wiki.kernel.runtime_env import current_env
from jonex_wiki.domain.engine.query.traversal_evidence import (
    _activate_new_evidence,
    _active_evidence_payload,
    _cap_evidence_diversity,
    _compact_claim_for_model,
    _evidence_excerpt_limit,
    _evidence_key,
    _evidence_per_doc_max,
    _evidence_records,
    _evidence_units_max,
    _record_query_state_event,
)
from jonex_wiki.domain.engine.models.query_models import (
    QueryCorpus,
    SearchHit,
    SubQuestionList,
    TraceEntry,
)
from jonex_wiki.domain.engine.query.retrieval import (_SearchMaterialCache, _chapter_tree_hits, _diversity_rerank, _env_int, _hit_tokens, _llm_rerank, _make_snippet, _search_terms)

# Read-through seam (lwp-arch-001 QA-3): the traversal resolves these five
# knobs through the legacy ``query_algorithm`` namespace at call time so
# pre-split tests that ``monkeypatch.setattr(qa, <name>, ...)`` keep working
# unchanged.  Unpatched, each resolves to the real implementation, so
# production behaviour is identical.
def _fts_search(*args, **kwargs):
    """Defer to the shim so ``query_algorithm._fts_search`` patches apply."""

    from jonex_wiki.domain.engine.query.algorithm import _fts_search as _impl

    return _impl(*args, **kwargs)


def _rewrite_query_for_search(*args, **kwargs):
    """Defer to the shim so ``query_algorithm._rewrite_query_for_search``
    patches apply."""

    from jonex_wiki.domain.engine.query.algorithm import (
        _rewrite_query_for_search as _impl,
    )

    return _impl(*args, **kwargs)


def _llm_rerank_enabled(*args, **kwargs):
    """Defer to the shim so ``query_algorithm._llm_rerank_enabled`` patches
    apply."""

    from jonex_wiki.domain.engine.query.algorithm import (
        _llm_rerank_enabled as _impl,
    )

    return _impl(*args, **kwargs)


def _diversity_rerank_enabled(*args, **kwargs):
    """Defer to the shim so ``query_algorithm._diversity_rerank_enabled``
    patches apply."""

    from jonex_wiki.domain.engine.query.algorithm import (
        _diversity_rerank_enabled as _impl,
    )

    return _impl(*args, **kwargs)


def _search_read_force_limit_via_shim() -> int:
    """Defer to the shim so ``query_algorithm._search_read_force_limit``
    patches apply to the traversal."""

    from jonex_wiki.domain.engine.query.algorithm import (
        _search_read_force_limit as _impl,
    )

    return _impl()


def _execute_read_via_shim(
    corpus: QueryCorpus,
    paths: object,
    *,
    cursor: Optional[str] = None,
    page_size: int = 200,
) -> "_ReadOutcome":
    """Defer to the shim so ``query_algorithm._execute_read`` patches apply
    to the traversal."""

    from jonex_wiki.domain.engine.query.algorithm import _execute_read as _impl

    return _impl(corpus, paths, cursor=cursor, page_size=page_size)
from jonex_wiki.domain.engine.util.model_io import (
    _SpanBuilder,
    _model_complete,
    _record_model_span,
    _utc_now_iso,
)
from jonex_wiki.kernel.ports import ModelPort
from jonex_wiki.kernel.telemetry import (
    current_runtime_telemetry,
    telemetry_stage,
)
from jonex_wiki.domain.engine.models.compiler_state import PageIndexEntry
from jonex_wiki.domain.engine.compile.content_validation import (
    ValidatedClaim,
    validate_claim_evidence,
)
from jonex_wiki.domain.engine.query.controller.control_state import (
    FailureClass,
    ControlEvent,
    ControlState,
)
from jonex_wiki.domain.engine.query.controller.failure import (
    FailureDecision,
    RuntimeFailureSignals,
    classify_runtime_failure,
)
from jonex_wiki.domain.engine.query.controller.controller import (
    ControlEvent,
    ControlState,
    ControllerRuntime,
    controller_payload,
    finish_incomplete,
    observe_frontier_read,
    observe_search_progress,
    sync_evidence_window,
)
from jonex_wiki.domain.engine.query.controller.evidence_state import (
    EvidenceDropped,
    EvidenceRead,
    EvidenceRecord,
    EvidenceSelected,
    QueryEvent as EvidenceStateEvent,
    QueryState as EvidenceQueryState,
    SearchCandidate as EvidenceSearchCandidate,
    SearchRecorded,
    reduce_query_state,
)
from jonex_wiki.domain.engine.compile.rendering import render_wiki_tree

# ──────────────────────────────────────────────
#  Legacy-patchable surface (read-through seam)
# ──────────────────────────────────────────────
# ``FORCE_READ_NO_PROGRESS`` stays owned by the legacy ``query_algorithm``
# module namespace so pre-split tests that patch it (e.g.
# ``monkeypatch.setattr(qa, "FORCE_READ_NO_PROGRESS", MAX_TOOL_CALLS + 1)``)
# keep working; the moved traversal reads it back at call time.  Production
# behaviour is identical to reading the constant directly.
def _force_read_no_progress() -> int:
    """Force-read threshold, read through the legacy module namespace."""

    from jonex_wiki.domain.engine.query.algorithm import (
        FORCE_READ_NO_PROGRESS as _impl,
    )

    return _impl


# ──────────────────────────────────────────────
#  Constants and stable markers
# ──────────────────────────────────────────────

MAX_TOOL_CALLS = 32
# Wave 6 (exp 22/23): thinking is decisive on multi-hop queries (ON 0.761 vs
# OFF 0.226 musique auto).  With thinking on, reasoning_tokens crowd the
# traversal budget: a 2048 max_tokens call spent 608/687 completion tokens on
# reasoning (88%), leaving little room for the tool_calls envelope.  Widening
# to 4096 raised musique completed 26→29 and eliminated no_read failures
# (F1 0.7613→0.7935) with no harm on hotpotqa (0.842 vs baseline 0.768-0.782).
# Default is 4096 (the measured improvement); set QUERY_TRAVERSAL_MAX_TOKENS
# to reproduce the old 2048 budget.
TRAVERSAL_MAX_TOKENS_ENV = "QUERY_TRAVERSAL_MAX_TOKENS"

# Wave 3 P0.2: consecutive no-new-evidence searches (a non-empty search whose
# hits are all already-read pages) stop the traversal early as a sufficiency
# signal.  Both limits are overridable via environment variables.
EVIDENCE_STALL_LIMIT = 2
# Wave 3 P0.3: Rabbit-Hole guard.  A model that keeps issuing distinct
# ``wiki_search`` calls but never reads a hit wastes the whole tool budget
# (observed: 13 searches / 1 read).  After this many consecutive searches with
# zero intervening reads and at least one unread hit, the controller forces a
# ``wiki_read`` of the top unread hit instead of asking the model again.
# Once a search has produced an unread frontier, permit one refinement and
# then inspect evidence.  The old value (6) was equal to the whole PoC tool
# budget and therefore allowed a query to finish without reading any hit.
SEARCH_READ_FORCE_LIMIT = 2

# Wave 3 P1.1: GenDec-style query decomposition — one bounded model call that
# splits a composite question into self-contained sub-questions, each searched
# independently before the traversal loop.  Default on; off via
# ``QUERY_QUERY_DECOMPOSE=off``.  ``MAX_DECOMPOSED_QUERIES`` bounds the number
# of sub-question searches so the extra retrieval stays bounded.
QUERY_DECOMPOSE_ENV = "QUERY_QUERY_DECOMPOSE"
MAX_DECOMPOSED_QUERIES = 8

# Answer style selector.  ``natural`` (default) is the product mode: the model
# writes a complete natural-language answer (required for the Chinese demo
# stack).  ``entity`` restores the short-phrase answer style the English
# benchmarks were measured with, so ``QUERY_ANSWER_STYLE=entity`` keeps the
# ablation numbers reproducible.
QUERY_ANSWER_STYLE_ENV = "QUERY_ANSWER_STYLE"

# ──────────────────────────────────────────────
#  LWP-RET-002 — long-document routing (per-query runtime axis)
# ──────────────────────────────────────────────
# Route is read from ``corpus.route`` (set by ``query_orchestration.run_query``
# through ``query_router.resolve_route``).  Every route-aware behaviour below is
# gated so that ``route in (None, "proposition")`` keeps the pre-route hybrid
# trace byte-identical (D-1): the new trace events are emitted only on the three
# new routes, and the raw-source fallback is inert unless explicitly enabled.
#
# Raw-source fallback (P0 #1): the raw passage text is read ON DEMAND as a
# single segment through the injected ``corpus.raw_source_reader`` keyed by
# SourceSpan identity (source_id/document_id/chunk_id + start/end).  The whole
# source archive is never re-injected into the corpus.  In "matched" mode the
# fallback fails closed, strict in matched mode; in "production" mode
# it degrades with a CFG-001 DegradationEvent(component="fallback", False→True)
# plus a ``raw_source_fallback`` trace event and a degraded count on run.config.
RAW_SOURCE_FALLBACK_STRICT = "RAW_SOURCE_FALLBACK_STRICT"
RAW_SOURCE_SPAN_ANCHOR_MISMATCH = "RAW_SOURCE_SPAN_ANCHOR_MISMATCH"
RAW_FALLBACK_SPANS_MAX = 2
# Reserved pseudo read-path used by the typed evidence registry when raw-source
# handles are registered without a wiki page read (explicit, never a page).
RAW_FALLBACK_PSEUDO_PATH = "__raw_source_fallback__"
# Deterministic chapter-navigation budget (bounded reads, D-5).
CHAPTER_NAV_BUDGET = 3
# Summary route v1 budget (D-6): deterministic bounded summary of the active
# evidence, no RAPTOR.
SUMMARY_BUDGET_CHARS = 1500
# Context budget reconciled by the ``context_budget`` telemetry event: the
# evidence-only window is bounded by MAX_ACTIVE_EVIDENCE excerpts at the
# EVIDENCE_EXCERPT_LIMIT character cap.
CONTEXT_BUDGET_CHARS = 32 * 500


def _route_active(corpus: QueryCorpus) -> bool:
    """True when the run uses one of the three new LWP-RET-002 routes.

    Proposition (and unset) routes must keep the pre-route hybrid trace
    byte-identical, so every route-gated telemetry event checks this first.
    """
    return getattr(corpus, "route", None) not in (None, "proposition")


def _raw_fallback_enabled(corpus: QueryCorpus) -> bool:
    """True when the raw-source fallback is explicitly enabled for this run."""
    mode = getattr(corpus, "raw_fallback_mode", "off")
    return mode in ("production", "matched") and getattr(
        corpus, "raw_source_reader", None
    ) is not None


def _raw_fallback_candidate_spans(
    corpus: QueryCorpus, query: str, limit: int = RAW_FALLBACK_SPANS_MAX
) -> List[Dict[str, Any]]:
    """Deterministic top claim SourceSpans overlapping the query terms.

    Returns span identities (with ``claim_id`` attached) for the claims whose
    text shares the most query terms, bounded by ``limit``.  Pure; never reads
    the archive itself — the reader is invoked later, one segment at a time.
    """
    terms = {t.casefold() for t in _search_terms(query)}
    scored: List[tuple[int, str, str, Any]] = []
    for path in sorted(corpus.claims):
        for claim in corpus.claims[path]:
            if not claim.source_spans:
                continue
            overlap = sum(1 for t in terms if t in claim.text.casefold())
            if overlap:
                scored.append((overlap, path, claim.claim_id, claim.source_spans[0]))
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    spans: List[Dict[str, Any]] = []
    for overlap, _path, claim_id, span in scored[:limit]:
        spans.append(
            {
                "claim_id": claim_id,
                "passage_id": span.passage_id,
                "source_id": span.source_id,
                "document_id": span.document_id,
                "chunk_id": span.chunk_id,
                "start_char": span.start_char,
                "end_char": span.end_char,
                "text": span.text,
            }
        )
    return spans


def _raw_source_fallback_span(
    corpus: QueryCorpus,
    span: Dict[str, Any],
    trace: List[TraceEntry],
    *,
    reason: str,
) -> Dict[str, str]:
    """Code-forced raw-source read of one SourceSpan (on-demand single segment).

    P0 #1: the reader returns the raw passage text keyed by the span identity;
    the excerpt is char-anchored against ``raw_text[start_char:end_char]`` and
    must equal the frozen span text exactly.  Returns the six-field evidence
    handle (claim_id/passage_id/source_id/document_id/chunk_id/excerpt).

    Matched mode fails closed (RAW_SOURCE_FALLBACK_STRICT, aligned with
    the matched fallback policy).  Production mode emits the ``raw_source_fallback``
    trace event plus a CFG-001 DegradationEvent(component="fallback",
    from_value=False, to_value=True).
    """
    mode = getattr(corpus, "raw_fallback_mode", "off")
    if mode == "matched":
        raise ValueError(
            RAW_SOURCE_FALLBACK_STRICT
            + ": raw-source fallback triggered in matched mode (reason="
            + reason
            + ")"
        )
    reader = getattr(corpus, "raw_source_reader", None)
    if reader is None:
        raise ValueError(
            RAW_SOURCE_FALLBACK_STRICT + ": no raw source reader configured"
        )
    raw_text = reader(span)
    if not isinstance(raw_text, str):
        raise ValueError(
            RAW_SOURCE_SPAN_ANCHOR_MISMATCH + ": reader returned non-text"
        )
    start = int(span["start_char"])
    end = int(span["end_char"])
    if not (0 <= start < end <= len(raw_text)):
        raise ValueError(
            RAW_SOURCE_SPAN_ANCHOR_MISMATCH + ": span offset out of bounds"
        )
    anchored = raw_text[start:end]
    if anchored != span["text"]:
        raise ValueError(
            RAW_SOURCE_SPAN_ANCHOR_MISMATCH
            + ": span offset is not char-anchored to the raw passage"
        )
    excerpt = anchored[: _evidence_excerpt_limit()]
    handle = {
        "claim_id": str(span.get("claim_id", "")),
        "passage_id": span["passage_id"],
        "source_id": span["source_id"],
        "document_id": span["document_id"],
        "chunk_id": span["chunk_id"],
        "excerpt": excerpt,
    }
    trace.append(
        TraceEntry(
            event="raw_source_fallback",
            detail={
                "reason": reason,
                "span": {
                    key: span[key]
                    for key in (
                        "passage_id",
                        "source_id",
                        "document_id",
                        "chunk_id",
                        "start_char",
                        "end_char",
                    )
                },
                "excerpt_chars": len(excerpt),
                "mode": mode,
            },
        )
    )
    if mode == "production":
        trace.append(
            TraceEntry(
                event="degradation_event",
                detail={
                    "code": "CFG-001",
                    "schema_version": 1,
                    "component": "fallback",
                    "from_value": False,
                    "to_value": True,
                    "trigger": "raw_source_fallback:" + reason,
                    "time": _utc_now_iso(),
                },
            )
        )
    return handle


def _register_raw_handles(
    state: Dict[str, Any], handles: List[Dict[str, str]], trace: List[TraceEntry]
) -> None:
    """Register raw-source handles as active evidence via the typed machinery.

    The handles flow into the seen registry (EvidenceRead) and the bounded
    active set (EvidenceSelected), so the final synthesis can cite them — the
    six-field handle → citation chain.  The reserved pseudo path makes the
    registration explicit and replayable without pretending a wiki page read.
    """
    query_state: EvidenceQueryState = state["query_state"]
    seen_before = set(query_state.seen_evidence_by_id)
    _record_query_state_event(
        state,
        EvidenceRead(
            paths=(RAW_FALLBACK_PSEUDO_PATH,),
            evidence=tuple(EvidenceRecord.model_validate(h) for h in handles),
        ),
        trace,
    )
    query_state = state["query_state"]
    new_ids = [
        evidence_id
        for evidence_id in query_state.seen_evidence_order
        if evidence_id not in seen_before
    ]
    _activate_new_evidence(state, new_ids, trace)
    query_state = state["query_state"]
    state["collected_evidence"] = _active_evidence_payload(state)
    state["successful_read"] = bool(query_state.active_evidence_ids)
    state["stall_count"] = 0
    state["empty_streak"] = 0
    trace.append(
        TraceEntry(
            event="raw_source_fallback_registered",
            detail={"count": len(handles), "evidence_ids": new_ids},
        )
    )


def _with_evidence_page_path(item: Mapping[str, Any], read_files: Sequence[str],
                             source_records: Mapping[str, Any]) -> Dict[str, Any]:
    """[jonex] 给阅读/来源证据补上「承载它的 Wiki 页」路径。

    `page_path` 只服务平台侧「来源引用 → Wiki 页」定位，不参与证据身份
    （见 evidence_state.EVIDENCE_FIELDS / PAGE_PATH_FIELD），模型也看不到它。
    表格句柄自带页路径（见 _cell_handles）；来源区间证据（source_span_*）走的是
    `source-evidence/<rid>.md` 这些页，记录里没有路径，引用就会丢掉页归属。
    这里按「该页登记的记录」精确匹配，只有一个被读页时直接采用它；多页且匹配不上
    就留空——宁可空，也不猜一个错误页面。
    """
    if str(item.get("page_path") or ""):
        return dict(item)
    key = _evidence_key(item)
    for path in read_files or ():
        for record in source_records.get(path) or ():
            if _evidence_key(record) == key:
                return {**item, "page_path": str(path)}
    files = list(read_files or ())
    if len(files) == 1:
        return {**item, "page_path": str(files[0])}
    return dict(item)


def _register_table_handles(state: Dict[str, Any], table_id: str,
                            handles: List[Dict[str, str]], trace: List[TraceEntry]) -> None:
    if not handles:
        return
    query_state: EvidenceQueryState = state["query_state"]
    seen_before = set(query_state.seen_evidence_by_id)
    _record_query_state_event(state, EvidenceRead(paths=("table:" + table_id,),
        evidence=tuple(EvidenceRecord.model_validate(item) for item in handles)), trace)
    query_state = state["query_state"]
    new_ids = [evidence_id for evidence_id in query_state.seen_evidence_order
               if evidence_id not in seen_before]
    _activate_new_evidence(state, new_ids, trace)
    state["collected_evidence"] = _active_evidence_payload(state)
    # A read that only delivered a caption/footnote is not a read that supports
    # a value: the answer gate stays closed until a cell has been read.  The
    # note itself remains citable evidence for what it states.
    has_cell = any(not str(handle.get("claim_id") or "").startswith("table_note_")
                   for handle in handles)
    state["successful_read"] = bool(state["successful_read"]
                                    or (has_cell and state["query_state"].active_evidence_ids))
    state["stall_count"] = 0
    state["empty_streak"] = 0


def _chapter_dirs(corpus: QueryCorpus) -> List[str]:
    """Sorted directory (chapter) names implied by the knowledge pages."""
    return sorted({p.rsplit("/", 1)[0] for p in corpus.page_index if "/" in p})


def _chapter_nav_next(
    corpus: QueryCorpus, state: Dict[str, Any], just_read: set
) -> Optional[List[str]]:
    """Deterministic next chapter-tree navigation step (bounded).

    Chain: ``index.md`` -> ``<dir>/index.md`` -> chapter pages (ordered by the
    query-side page-number map).  Returns ``None`` when the chain is exhausted
    or the current read did not touch a navigation file.
    """
    if "index.md" in just_read:
        nav = [
            d + "/index.md"
            for d in _chapter_dirs(corpus)
            if d + "/index.md" not in state["read_pages"]
        ]
        return nav[:CHAPTER_NAV_BUDGET] or None
    read_dir_indexes = sorted({p for p in just_read if p.endswith("/index.md")})
    if read_dir_indexes:
        dirs = {p[: -len("/index.md")] for p in read_dir_indexes}
        chapters = [
            path
            for path in sorted(corpus.page_index)
            if path.rsplit("/", 1)[0] in dirs and path not in state["read_pages"]
        ]
        chapters.sort(key=lambda p: (corpus.page_number_map.get(p, 1 << 30), p))
        return chapters[:CHAPTER_NAV_BUDGET] or None
    return None


def _deterministic_summary(
    question: str,
    citations: Sequence[Dict[str, Any]],
    corpus: QueryCorpus,
    *,
    budget: int = SUMMARY_BUDGET_CHARS,
) -> str:
    """Summary route v1 (D-6): deterministic bounded digest of active evidence.

    Ordered by the verified citation sequence; each line carries the source
    document/chunk identity plus the page number when derivable.  Hard-bounded
    by ``budget`` characters (deterministic truncation at a line boundary).
    No RAPTOR, no extra model call.  Returns "" (empty) when there is no
    active evidence — the caller emits the explicit summary fallback lineage.
    """
    if not citations:
        return ""
    page_by_claim: Dict[str, str] = {}
    for path, page_claims in corpus.claims.items():
        for claim in page_claims:
            page_by_claim[claim.claim_id] = path
    lines: List[str] = []
    used = 0
    for index, citation in enumerate(citations, start=1):
        excerpt = " ".join(str(citation.get("excerpt", "")).split())
        if not excerpt:
            continue
        page = page_by_claim.get(str(citation.get("claim_id", "")), "")
        page_number = corpus.page_number_map.get(page)
        page_label = ""
        if page:
            page_label = " [page {}]".format(page_number) if page_number else " [{}]".format(page)
        line = "{}. (doc:{doc} chunk:{chunk}) {excerpt}{page}".format(
            index,
            doc=str(citation.get("document_id", "")),
            chunk=str(citation.get("chunk_id", "")),
            excerpt=excerpt,
            page=page_label,
        )
        if used and used + len(line) > budget:
            break
        lines.append(line)
        used += len(line)
        if used >= budget:
            break
    return "\n".join(lines)


def apply_summary_route(
    result: "QueryRunResult", corpus: QueryCorpus, question: str
) -> "QueryRunResult":
    """LWP-RET-002 summary route: replace the answer with the deterministic
    bounded summary of the active evidence; explicit fallback lineage when the
    active evidence is empty (D-6).  The verified citation set is the v1
    projection of the active evidence; the six-field chain is preserved.
    """
    citations = [c.model_dump() for c in result.citations]
    summary = _deterministic_summary(question, citations, corpus)
    trace = list(result.trace)
    if summary:
        trace.append(
            TraceEntry(
                event="summary_generated",
                detail={"chars": len(summary), "citations": len(citations)},
            )
        )
    else:
        trace.append(
            TraceEntry(
                event="summary_fallback",
                detail={
                    "reason": "no_active_evidence",
                    "citations": len(citations),
                    "budget_chars": SUMMARY_BUDGET_CHARS,
                },
            )
        )
    return result.model_copy(update={"answer": summary, "trace": trace})


def _max_tool_calls_limit() -> int:
    return _env_int("QUERY_MAX_TOOL_CALLS", MAX_TOOL_CALLS)


def _evidence_stall_limit() -> int:
    return _env_int("QUERY_EVIDENCE_STALL_LIMIT", EVIDENCE_STALL_LIMIT)


def _search_read_force_limit() -> int:
    return _env_int("QUERY_SEARCH_READ_FORCE_LIMIT", SEARCH_READ_FORCE_LIMIT)


def _traversal_max_tokens() -> int:
    """Traversal-loop max_tokens (default 4096).

    Exp 22/23: with thinking enabled (the query default), reasoning_tokens
    crowd a 2048 budget (608/687, 88%); widening to 4096 removed no_read
    failures and raised musique completed 26→29 (F1 0.7613→0.7935) with no
    harm on hotpotqa.  Set QUERY_TRAVERSAL_MAX_TOKENS=2048 for the old budget.
    """
    return _env_int(TRAVERSAL_MAX_TOKENS_ENV, 4096)


def _decomposition_enabled() -> bool:
    """Whether GenDec-style query decomposition is enabled (default on)."""
    raw = current_env().get(QUERY_DECOMPOSE_ENV, "on").strip().casefold()
    return raw not in ("0", "off", "false", "no")

# Wave 3 answer-target hint: OFF by default.  A first live run showed the hint
# can shift answer granularity in both directions (it fixed some "answered the
# wrong entity" cases but over-compacted others, e.g. "east of Ireland" ->
# "east").  Keep it opt-in via QUERY_ANSWER_TARGET_HINT=on for a matched A/B.
QUERY_ANSWER_TARGET_HINT_ENV = "QUERY_ANSWER_TARGET_HINT"


def _answer_target_hint_enabled() -> bool:
    raw = current_env().get(QUERY_ANSWER_TARGET_HINT_ENV, "off").strip().casefold()
    return raw not in ("0", "off", "false", "no")


def _answer_style_natural() -> bool:
    """Whether the final answer style is natural language (default on).

    ``entity`` (or ``0``/``off``/``false``/``no``) restores the concise
    short-phrase answer style used to measure the English benchmarks, keeping
    those numbers reproducible via ``QUERY_ANSWER_STYLE=entity``.
    """
    raw = current_env().get(QUERY_ANSWER_STYLE_ENV, "natural").strip().casefold()
    return raw not in ("0", "off", "false", "no", "entity")

# Stable marker that any malformed model tool envelope or tool argument shape
# fails closed with.  Every envelope/argument error message carries this marker
# (plus a more specific sub-code) so callers can recognise and surface it.
MALFORMED_TOOL_CALL = "MALFORMED_TOOL_CALL"

# Sub-codes for tool argument shapes that do not match the frozen QUERY_TOOLS.
_WIKI_SEARCH_ARGS_KEYS = f"{MALFORMED_TOOL_CALL}:WIKI_SEARCH_ARGS_KEYS"
_WIKI_SEARCH_QUERY_NOT_STRING = f"{MALFORMED_TOOL_CALL}:WIKI_SEARCH_QUERY_NOT_STRING"
_WIKI_READ_ARGS_KEYS = f"{MALFORMED_TOOL_CALL}:WIKI_READ_ARGS_KEYS"
_WIKI_READ_PATHS_NOT_STRING_LIST = f"{MALFORMED_TOOL_CALL}:WIKI_READ_PATHS_NOT_STRING_LIST"
_UNKNOWN_QUERY_TOOL = f"{MALFORMED_TOOL_CALL}:UNKNOWN_QUERY_TOOL"

# Stable stop reasons surfaced on the result.
_STOP_ANSWERED = "answered"
_STOP_TOOL_LIMIT = "tool_limit"
_STOP_EMPTY_SEARCH = "empty_search_exhausted"
_STOP_NO_READ = "no_successful_read"
_STOP_EMPTY_MODEL = "empty_model_exhausted"
# Wave 3 P0.2: consecutive no-new-evidence searches signal evidence sufficiency.
_STOP_EVIDENCE_SUFFICIENT = "evidence_sufficient"
# QUERY-001: an active-set eviction is a lossy boundary.  Stop and return an
# explicitly incomplete result instead of silently treating the retained slice
# as sufficient.
_STOP_EVIDENCE_BUDGET = "evidence_budget_exhausted"
_STOP_CONTROL_SLOT = "control_slot_incomplete"
_STOP_NO_PROGRESS = "no_progress_exhausted"
_SYSTEM_INSTRUCTION_COMMON = (
    "Answer the original question using the available Wiki and source material. "
    "Before calculating or answering, first identify in your reasoning the necessary evidence: "
    "its original words or labels, actual fact or relation, period, entity and source "
    "section, and what it supports. Check these against the original question. "
    "If a required source, scope or premise is missing, use the available original "
    "source navigation to read it before calculating; do not relabel related material "
    "as the required evidence. Then interpret what it supports and calculate or answer. "
    "Choose searches and reading paths according to what the question still needs. "
    "Read the original table or statement containing the inputs needed by the question. "
    "If the question restricts sources or sections, obtain inputs and citations from "
    "those sources or sections; identical values elsewhere are not substitutes. "
    "Identify the requested measure and classification first. When the question names "
    "a member selected by a measure, compare the relevant peers on that measure. "
    "A negative contribution to growth is not the same as a negative growth rate; "
    "keep the stated category level and read the peer values before selecting. When the question names "
    "statement components, organizational segments are a different classification. "
    "Locate original statement row labels for the requested concepts (for example, "
    "'Sales of products', 'Cost of sales', 'Inventories') and read the matching table. "
    "These are search candidates, not assumptions about what the source contains. "
    "When the question names "
    "component categories, start with separate table_locate searches for the category "
    "terms used in the question, including singular/plural and common row-label variants, "
    "before substituting organizational labels discovered in navigation. "
    "Check the hit's source identity, then wiki_read tables/<table_id>.md for the "
    "complete original table. Confirm the requested measure, period and unit. "
    "A related classification leaves the requested components unresolved. Refine broad "
    "labels when matches are partial; vary missed labels rather than repeating them. "
    "Keep each value's stated subject and denominator; read surrounding source headings "
    "or notes if the table alone does not establish that scope. If unresolved, explain "
    "the actual searched/read scope, rather than claiming the document lacks disclosure. "
    "A calculated metric need not be explicitly reported: compute it from its components. "
    "Lack of a disclosed metric, or discussion of other metrics, does not establish "
    "uselessness or inapplicability. Evaluate conditional alternatives rather than assuming "
    "them. Do not exhaustively search to prove a conditional negative: when the "
    "requested measure is computable, answer it with relevant caveats. Once the "
    "required values and context are read, submit a brief draft with the direct "
    "answer and citations; the final writer will provide the explanation. "
    "Prefer wiki_read on original table links to read their full context. "
    "Read complete related tables, worksheets or sections. When a complete original "
    "view is delivered, use it directly; do not inspect or read the same table again "
    "to obtain a structured interpretation. Interpret its headers and notes from "
    "the original view, and read another table only for genuinely missing inputs. "
    "Navigation and generated claims are reading hints; source text and tables "
    "remain available when a claim or table interpretation is unsupported. "
    "Use a disclosed comparable ratio or subtotal instead of rebuilding it from "
    "separate rows when it already answers the question. Preserve source signs: "
    "parenthesized amounts are negative, so subtracting them reverses their sign. "
    "Table views preserve headers, units and notes. Simple arithmetic may be stated "
    "as a formula in the brief draft using the delivered values. table_compute is "
    "available for counts, grouping and calculations when helpful; keep any plan "
    "to the requested measure, with operations in separate steps and references "
    "only to earlier results, never nested calculation operands. "
    "Cite the [E#] references supplied with the material. A table/range reference "
    "can cite the whole table; no sentence-by-sentence binding or copied quote "
    "is required. Keep the requested entity, relationship, periods and scope in view. "
    "Before stating acceleration, deceleration or another comparative trend, read "
    "both comparison values for the same named measure and reporting scope. An "
    "unmatched forecast may be stated alone but cannot be compared with another "
    "measure's baseline, including in a conditional or secondary branch. "
    "A positive growth rate does not establish acceleration: establish a rise "
    "from the preceding underlying period using both rates or an explicit source "
    "comparison of growth across those periods on the same population. Publication "
    "dates or revisions of a forecast for one period are not successive underlying "
    "periods and cannot replace that comparison. "
    "If a summary supplies a related measure, use the original source section index "
    "to read the requested measure before comparing. For direction questions, a source's "
    "explicit qualitative explanation can establish direction without a separately "
    "disclosed numerical line. State that direction for its actual measure and period, "
    "then qualify component or period differences; do not replace it with the trend "
    "of a broader aggregate or keep searching for an unnecessary exact value. "
)
_SYSTEM_INSTRUCTION = _SYSTEM_INSTRUCTION_COMMON + (
    "Return your answer when ready, explaining any remaining uncertainty."
)
_SYSTEM_INSTRUCTION_NATURAL = _SYSTEM_INSTRUCTION_COMMON + (
    "When ready, return a brief draft with its [E#] citations and no tool_calls, "
    "explaining any remaining uncertainty. Keep reasoning brief. "
    "A separate tool-free turn will write the final answer."
)

_DECOMPOSE_SYSTEM_INSTRUCTION = (
    "You are a query decomposition assistant. Split a composite question into "
    "ordered search directions. Mark requires_evidence_bridge true only when a "
    "later direction depends on an entity or value that must be learned from "
    "evidence for an earlier direction; independent parallel parts must be false. "
    "Never guess or prefill the bridge value."
)


# ──────────────────────────────────────────────
#  Frozen tool contract
# ──────────────────────────────────────────────

QUERY_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "wiki_search",
            "description": "Search knowledge pages. Returns up to ten ranked "
            "page hits with a numeric rank.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query over page metadata and body.",
                    }
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wiki_read",
            "description": "Read one to five exact corpus page paths. A read is "
            "successful only when every requested file exists and is valid; only "
            "then does the page's claim evidence become available.",
            "parameters": {
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 5,
                        "description": "Exact corpus page paths (no wiki/ prefix).",
                    },
                    "cursor": {
                        "type": "string",
                        "description": "Opaque revision-bound cursor returned by a prior navigation-page read.",
                    },
                    "page_size": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "description": "Maximum navigation entries to return; defaults to 200.",
                    },
                    "range": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 2,
                        "maxItems": 2,
                        "description": "Optional canonical half-open [start,end) character range into the single requested path's article text. Use only with exactly one path; coordinates come from the returned headings/blocks.",
                    },
                },
                "required": ["paths"],
                "additionalProperties": False,
            },
        },
    },
    {"type": "function", "function": {
        "name": "table_inspect",
        "description": "Inspect one logical table before reading values.",
        "parameters": {"type": "object", "properties": {"table_id": {"type": "string"}},
                       "required": ["table_id"], "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "table_locate",
        "description": "Locate literal text as a complete word or phrase in authorized native table cells; this is discovery, not aggregation. If a relevant table_id is known, provide it. Otherwise omit table_id to search authorized registered tables. Use a short candidate row label for the requested business concept or a literal value; do not pass the whole question or invent a field_id. You may use this tool even when wiki_search returns irrelevant pages or a related classification. Check the hit's source identity, then prefer wiki_read on tables/<table_id>.md for the complete original table. Inspect before choosing structured fields if a scoped table_read is needed. A miss is not proof of absence; partial results are not complete coverage. For counting without a literal, discover and inspect the table, then read its full necessary scope instead.",
        "parameters": {"type": "object", "properties": {
            "literal": {"type": "string"}, "table_id": {"type": "string"},
            "field_id": {"type": "string"}, "cursor": {"type": "string"}},
            "required": ["literal"], "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "table_read",
        "description": "Read one table scope with headers, coordinates and coverage. Use scope for a confirmed table read by field name and conditions: when require_all_matches is true the runner continues pages of the same scope automatically, so do not re-issue the returned cursor for the same request. To read a different range, fields or conditions, omit cursor - a changed scope with an old cursor is rejected and a new request is created. For a filtered count or distribution, prefer scope.constraints (field name -> exact value) over row_start/row_end. Use raw_scope instead when the table's header/unit is not confirmed (or you only need stored content at known coordinates): it takes an explicit row/column rectangle, resolves no field name and aggregates nothing, and its raw_delivery block states that the delivered content proves coordinates, not periods, metrics or a complete range for a computation.",
        "parameters": {"type": "object", "properties": {
            "scope": {"type": "object", "properties": {
                "table_id": {"type": "string"},
                "constraints": {"type": "object", "additionalProperties": {"type": "string"},
                                "description": "field name -> exact value; a row window may also be given here as row_start/row_end or rows=\"41-45\""},
                "target_fields": {"type": "array", "items": {"type": "string"}},
                "task_type": {"type": "string", "enum": ["lookup", "enumerate", "explain"]},
                "require_all_matches": {"type": "boolean"}, "include_hidden": {"type": "boolean"},
                "row_start": {"type": "integer", "description": "first row of the window, inclusive"},
                "row_end": {"type": "integer", "description": "last row of the window, inclusive"}},
                "required": ["table_id"], "additionalProperties": False},
            "raw_scope": {"type": "object", "properties": {
                "table_id": {"type": "string"},
                "row_start": {"type": "integer", "description": "first row of the coordinate rectangle, inclusive"},
                "row_end": {"type": "integer", "description": "last row of the coordinate rectangle, inclusive"},
                "column_start": {"type": "integer", "description": "first column; omit together with column_end to read every column"},
                "column_end": {"type": "integer"},
                "include_hidden": {"type": "boolean"}},
                "required": ["table_id", "row_start", "row_end"], "additionalProperties": False},
            "cursor": {"type": "string"}}, "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "table_compute",
        "description": "Two independent branches. Aggregation: count / group count / duplicate groups / select / exact cell lookup over the union of all completed table_read ranges for one table_id; use it to compute the answer BEFORE finalizing, since the result is computed in code and never recounted by a model. Calculation: operation=\"calculate\" with a short plan of add/subtract/multiply/divide/mean/compare/convert steps (max 16) whose operands are delivered [E*] handles, an exact character range inside a delivered excerpt, a result of an earlier step (\"$1\"), or a constant written in the question - never a number you typed. Multiplication and division of money are computed in Decimal, division at 28 digits, and rounding happens only when you ask for display_decimals. Cite the involved cells with the table_cell_* / [E*] handles already delivered by your table_read calls. When the completed ranges cannot merge into one grid the aggregation answers range_selection_required; then re-call it with request_id set to one of the listed ranges.",
        "parameters": {"type": "object", "properties": {
            "table_id": {"type": "string", "description": "The table_id whose completed table_read ranges are aggregated; required for every operation except calculate."},
            "request_id": {"type": "string", "description": "Optional. Compute over only this specific completed table_read request_id instead of the union of all completed reads. Use it only when a prior table_compute answered range_selection_required and list the available request_ids."},
            "operation": {"type": "string", "enum": ["count", "group_count", "duplicate_groups", "select", "lookup_cell", "count_columns", "find_dates", "compare_header_order", "calculate"],
                          "description": "count = number of matching rows; group_count = per-value counts (requires group_by); duplicate_groups = which group_by keys carry more than min_distinct distinct value_column values, with the row number witnessing each value (requires group_by + value_column) - use it for 'which X appear under more than one Y' or 'compare Y across X'; select = projected rows (requires return_columns); lookup_cell/find_dates = values of one target_column; count_columns = number of candidate columns matching a cell condition (requires return_columns + cell_match); calculate = run the independent calculation plan in `calculate` (no table_id, cross-table operands allowed)."},
            "calculate": {"type": "object", "properties": {
                "steps": {"type": "array", "minItems": 1, "maxItems": 16, "items": {
                    "type": "object", "properties": {
                        "step_id": {"type": "string"},
                        "operation": {"type": "string", "enum": ["add", "subtract", "multiply", "divide", "mean", "compare", "convert"]},
                        "operands": {"type": "array", "minItems": 1, "items": {
                            "type": "object", "properties": {
                                "result": {"type": "string", "description": "An earlier result by exact step_id ($profit) or 1-based position ($1). Numeric labels still use position; self/forward references are invalid."},
                                "operand": {"type": "object", "properties": {
                                    "kind": {"type": "string", "enum": ["table_cell", "text_span", "constant"]},
                                    "handle": {"type": "string", "description": "The [E*] handle of a delivered cell or excerpt."},
                                    "char_start": {"type": "integer"}, "char_end": {"type": "integer"},
                                    "source_text": {"type": "string", "description": "For kind=constant: text that occurs verbatim in the question."},
                                    "rule": {"type": "string", "enum": ["percent", "percentage_points", "permille", "unity"]},
                                    "label": {"type": "string"},
                                    "grounds": {"type": "object", "properties": {
                                        "metric": {"type": "string"}, "period": {"type": "string"},
                                        "unit": {"type": "string"}, "subject_scope": {"type": "string"}},
                                        "description": "Handles of delivered notes/captions that state the metric, period, unit or subject scope; anything not proven is reported as unproven."}},
                                    "required": ["kind"], "additionalProperties": False}},
                            "required": [], "additionalProperties": False}},
                        "display_decimals": {"type": "integer", "minimum": 0, "maximum": 12},
                        "unit": {"type": "string", "description": "Target unit for operation=convert."},
                        "label": {"type": "string"}},
                    "required": ["step_id", "operation", "operands"],
                    "additionalProperties": False}}},
                "additionalProperties": False,
                "description": "The calculation plan; only used when operation=calculate."},
            "filters": {"type": "array", "items": {"type": "object", "properties": {
                "column": {"type": "string"}, "operator": {"type": "string", "enum": ["equals", "contains", "in", "not_empty"]},
                "value": {"type": "string"}},
                "required": ["column", "operator"]},
                "description": "Row conditions; column names must be exact labels delivered by table_read."},
            "group_by": {"type": "string", "description": "Column label to group by (group_count / duplicate_groups)."},
            "value_column": {"type": "string", "description": "Column label whose distinct values are compared within each group (duplicate_groups)."},
            "min_distinct": {"type": "integer", "description": "How many distinct value_column values make a group reportable (duplicate_groups; default 2)."},
            "target_column": {"type": "string", "description": "Column label to read from (lookup_cell / find_dates)."},
            "return_columns": {"type": "array", "items": {"type": "string"}, "description": "Column labels to project (select / count_columns)."},
            "cell_match": {"type": "object", "properties": {
                "operator": {"type": "string", "enum": ["equals", "contains", "in", "not_empty"]},
                "value": {"type": "string"}},
                "required": ["operator"]},
        }, "required": ["operation"], "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "bridge_binding",
        "description": "Bind the current bridge slot to an active evidence handle, then advance the hop. Call alone.",
        "parameters": {"type": "object", "properties": {
            "slot_id": {"type": "string"}, "evidence_handle": {"type": "string"},
            "value": {"type": "string"}},
            "required": ["slot_id", "evidence_handle", "value"], "additionalProperties": False}}},
]

_QUERY_TOOL_NAMES = frozenset(tool["function"]["name"] for tool in QUERY_TOOLS)


def _query_tool_parameters(name: str) -> Optional[Dict[str, Any]]:
    """The frozen JSON Schema ``parameters`` for one known tool."""
    for tool in QUERY_TOOLS:
        if tool["function"]["name"] == name:
            return tool["function"].get("parameters")
    return None


# ──────────────────────────────────────────────
#  Read tool
# ──────────────────────────────────────────────


class _ReadOutcome:
    __slots__ = ("success", "files", "claims", "evidence", "message", "pagination")

    def __init__(
        self,
        success: bool,
        files: Dict[str, str],
        claims: Dict[str, List[Dict[str, Any]]],
        evidence: List[Dict[str, str]],
        message: str,
        pagination: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None:
        self.success = success
        self.files = files
        self.claims = claims
        self.evidence = evidence
        self.message = message
        self.pagination = pagination or {}


def _validate_read_path(corpus: QueryCorpus, path: object) -> str:
    """Validate one exact corpus read path; raise a stable marker otherwise."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("WIKI_READ_INVALID_PATH")
    if len(path) > 240:
        raise ValueError("WIKI_READ_INVALID_PATH")
    if path.startswith("/"):
        raise ValueError("WIKI_READ_INVALID_PATH")
    if "\\" in path:
        raise ValueError("WIKI_READ_INVALID_PATH")
    if "?" in path or "#" in path:
        raise ValueError("WIKI_READ_INVALID_PATH")
    if not path.endswith(".md"):
        raise ValueError("WIKI_READ_INVALID_PATH")
    if any(segment in ("", ".", "..") for segment in path.split("/")):
        raise ValueError("WIKI_READ_INVALID_PATH")
    if path not in corpus.files:
        raise ValueError("WIKI_READ_MISSING")
    return path


NAVIGATION_FRONT_MATTER_KEYS = (
    "kind", "type", "page_id", "path", "title", "tags", "description",
    "object_id", "scope", "fields", "why_visit", "limitations", "capabilities",
    "interpretation_status",
)
# Same rule the reader's own inspect preview uses: enough of the preview to
# show the shape, never a claim about the whole table.
NAVIGATION_PREVIEW_BYTES = 2048


def _trim_navigation_page(text: str) -> str:
    """Deliver a navigation page's navigation fields and its body, and nothing else."""
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end < 0:
        return text
    header = text[3:end].strip("\n")
    if "navigation-only" not in header and "kind: \"index\"" not in header:
        return text
    kept = []
    for line in header.splitlines():
        key = line.split(":", 1)[0].strip()
        if key == "links":
            targets = re.findall(r'"target_id"\s*:\s*"([^"]+)"', line)
            whys = re.findall(r'"why_visit"\s*:\s*"([^"]*)"', line)
            kinds = re.findall(r'"kind"\s*:\s*"([^"]*)"', line)
            entries = []
            for index, target in enumerate(targets):
                fields = ['"target_id":"{}"'.format(target)]
                if index < len(whys):
                    fields.append('"why_visit":"{}"'.format(whys[index]))
                if index < len(kinds):
                    fields.append('"kind":"{}"'.format(kinds[index]))
                entries.append("{" + ",".join(fields) + "}")
            if entries:
                kept.append("links: [" + ", ".join(entries) + "]")
            continue
        if key in NAVIGATION_FRONT_MATTER_KEYS:
            kept.append(line)
    body = _trim_navigation_preview(text[end + 4:])
    return "---\n" + "\n".join(kept) + "\n---\n" + body


def _trim_navigation_preview(body: str) -> str:
    """Keep a navigation page's preview to the shape it is there to show.

    The page states its own bound and its rows carry full cell text, so one
    page costs kilobytes of every later prompt.  ``TableReader.inspect`` already
    answers the same question with three sample rows; the preview follows it.
    """
    marker = ("\n（预览已按 {} 字节截断：本页只用于导航，取值必须用 table_read "
              "读取请求范围。）\n".format(NAVIGATION_PREVIEW_BYTES))
    for block in re.findall(r"```markdown\n.*?\n```", body, flags=re.S):
        lines = block.splitlines()
        used, cut = 0, None
        for index, line in enumerate(lines):
            used += len(line.encode("utf-8")) + 1
            if used > NAVIGATION_PREVIEW_BYTES:
                cut = index
                break
        if cut is None:
            continue
        trimmed = "\n".join(lines[:cut]) + marker + "```"
        body = body.replace(block, trimmed, 1)
    return body


def _navigation_cursor(corpus: QueryCorpus, path: str, offset: int) -> str:
    payload = {
        "revision_id": corpus.revision_id,
        "path": path,
        "offset": offset,
        "file_sha256": hashlib.sha256(corpus.files[path].encode("utf-8")).hexdigest(),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_navigation_cursor(corpus: QueryCorpus, cursor: str, path: str) -> int:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError("WIKI_CURSOR_INVALID") from exc
    expected_sha = hashlib.sha256(corpus.files[path].encode("utf-8")).hexdigest()
    if (payload.get("revision_id") != corpus.revision_id
            or payload.get("path") != path
            or payload.get("file_sha256") != expected_sha):
        raise ValueError("WIKI_CURSOR_BINDING_MISMATCH")
    offset = payload.get("offset")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("WIKI_CURSOR_INVALID")
    return offset


def _paginate_navigation(text: str, *, offset: int, limit: int) -> tuple[str, int, bool]:
    lines = text.splitlines(keepends=True)
    entry_indexes = [index for index, line in enumerate(lines) if line.startswith("- [[")]
    selected = set(entry_indexes[offset:offset + limit])
    if offset == 0 and len(entry_indexes) <= limit:
        return text, len(entry_indexes), False
    rendered = "".join(
        line for index, line in enumerate(lines)
        if index not in entry_indexes or index in selected
    )
    next_offset = min(len(entry_indexes), offset + limit)
    return rendered, next_offset, next_offset < len(entry_indexes)


def _execute_read(
    corpus: QueryCorpus,
    paths: object,
    *,
    cursor: Optional[str] = None,
    page_size: int = 200,
) -> _ReadOutcome:
    """Execute a read of one to five unique exact corpus paths.

    A successful read requires every requested file to exist and pass
    validation; otherwise it exposes no evidence and does not satisfy the
    mandatory-read gate.  A successful read returns, per requested path, the
    full UTF-8 Markdown, that page's validated claims, and complete six-field
    evidence.  Index artifacts expose no claim evidence.
    """
    if not isinstance(paths, list):
        return _ReadOutcome(False, {}, {}, [], "WIKI_READ_INVALID")
    if len(paths) < 1 or len(paths) > 5:
        return _ReadOutcome(False, {}, {}, [], "WIKI_READ_INVALID")
    if len(set(paths)) != len(paths):
        return _ReadOutcome(False, {}, {}, [], "WIKI_READ_INVALID")

    validated: List[str] = []
    try:
        for path in paths:
            validated.append(_validate_read_path(corpus, path))
    except ValueError as exc:
        return _ReadOutcome(False, {}, {}, [], str(exc))
    if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 200:
        return _ReadOutcome(False, {}, {}, [], "WIKI_READ_INVALID_PAGE_SIZE")
    if cursor is not None and (not isinstance(cursor, str) or len(validated) != 1):
        return _ReadOutcome(False, {}, {}, [], "WIKI_CURSOR_INVALID")

    # ``_execute_read`` keeps path validation, navigation and citation-source
    # lookup only.  It no longer merges every same-source span into a whole
    # object; the one range-selection step (``plan_read``) decides what body is
    # delivered, and reading-material units are delivered by the caller.
    files: Dict[str, str] = {}
    for path in validated:
        files[path] = _trim_navigation_page(corpus.files[path])
        if path.endswith("/index.md"):
            prefix = path[:-len("index.md")]
            source_ids = {ref.source_id for page, page_claims in corpus.claims.items()
                          if page.startswith(prefix) for claim in page_claims for ref in claim.evidence}
            source_ids.update(record.get("source_id") or record.get("document_id")
                              for page, source_paths in corpus.evidence_fallback.items()
                              if page.startswith(prefix) for source_path in source_paths
                              for record in corpus.source_records.get(source_path, []))
            table_links = ["- [[{}|{}]]".format(target, entry.title)
                           for target, entry in corpus.page_index.items()
                           if target.startswith("raw-tables/")
                           and (entry.scope or {}).get("source_id") in source_ids]
            if table_links:
                files[path] += "\n\n## Original source tables\n\n" + "\n".join(table_links) + "\n"

    pagination: Dict[str, Dict[str, Any]] = {}
    for path in files:
        if path == "index.md" or path.endswith("/index.md"):
            try:
                offset = _decode_navigation_cursor(corpus, cursor, path) if cursor else 0
            except ValueError as exc:
                return _ReadOutcome(False, {}, {}, [], str(exc))
            rendered, next_offset, has_more = _paginate_navigation(
                files[path], offset=offset, limit=page_size
            )
            files[path] = rendered
            pagination[path] = {
                "revision_id": corpus.revision_id,
                "entry_range": [offset, next_offset],
                "has_more": has_more,
                "next_cursor": _navigation_cursor(corpus, path, next_offset) if has_more else None,
            }
    claims: Dict[str, List[Dict[str, Any]]] = {}
    evidence: List[Dict[str, str]] = []
    fallback_keys: set = set()
    for path in files:
        page_claims = corpus.claims.get(path, [])
        claims[path] = [c.model_dump() for c in page_claims]
        for claim in page_claims:
            evidence.extend(_evidence_records(claim))
        page_records = corpus.source_records.get(path, [])
        evidence.extend(page_records)
        if page_claims or page_records:
            continue
        # keep-and-search-v1 raw fallback.  The page's claims were never
        # admitted, so it carries no claim evidence and a read of it alone
        # would leave the mandatory-read gate closed — even though the exact
        # intervals it was compiled from are archived and raw-readable.
        # Deliver those source records so the read still yields citable
        # evidence.  Bounded by the page's own passages, and deduplicated so a
        # multi-page read cannot attach the same interval twice.
        for span_path in corpus.evidence_fallback.get(path, ()):
            for record in corpus.source_records.get(span_path, ()):
                key = _evidence_key(record)
                if key in fallback_keys:
                    continue
                fallback_keys.add(key)
                evidence.append(record)
    return _ReadOutcome(True, files, claims, evidence, "OK", pagination)


# ──────────────────────────────────────────────
#  Traversal and messages
# ──────────────────────────────────────────────


def _system_message() -> Dict[str, str]:
    instruction = (
        _SYSTEM_INSTRUCTION_NATURAL if _answer_style_natural() else _SYSTEM_INSTRUCTION
    )
    return {"role": "system", "content": instruction}


def _user_message(question: str, strategy: QueryStrategy) -> Dict[str, str]:
    task = (
        "answer the question using Wiki and original source material; cite only evidence read from "
        "pages. When ready, return a brief draft with its [E#] citations "
        "and no tool_calls to finish reading."
        if _answer_style_natural()
        else "answer the question using Wiki and original source material; cite only evidence read from "
             "pages. Final answer must be concise (a name, number, or short phrase)."
    )
    payload = {
        "task": task,
        "question": question,
        "strategy": strategy,
    }
    if _answer_target_hint_enabled():
        hint = _answer_target_hint(question)
        if hint:
            payload["answer_target"] = hint
    return {
        "role": "user",
        "content": json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    }


_QUESTION_TARGET_RE = re.compile(
    r"\b(who|whom|whose|when|where|how many|how much|which|what)\b",
    re.I,
)


def _answer_target_hint(question: str) -> str:
    """Return a short answer-target requirement for the question word.

    Maps the question word to the entity type the final answer must BE:
    who → a person/group/organization name, when → a date/year/time, where →
    a place, how many → a number/count, which/what → the specific noun asked
    about.  Empty when the question has no recognized question word, in which
    case the caller keeps the plain instruction.  Pure and deterministic so
    the same question always yields the same hint.
    """
    text = question.strip()
    m = _QUESTION_TARGET_RE.search(text)
    if not m:
        return ""
    word = m.group(1).casefold()
    if word in ("who", "whom", "whose"):
        return "the question asks for a person, group, or organization's name"
    if word == "when":
        return "the question asks for a date, year, or time"
    if word == "where":
        return "the question asks for a place name"
    if word == "how many":
        return "the question asks for a number or count"
    if word == "how much":
        return "the question asks for an amount"
    rest = text[m.end():].strip()
    rest = re.sub(r"^of (?:these|the following)\s+", "", rest, flags=re.I)
    rest = re.sub(
        r"^(?:is|are|was|were|did|do|does|has|have|had)\s+",
        "",
        rest,
        flags=re.I,
    )
    noun = re.split(
        r"\s+(?:is|was|are|were|did|do|does|has|have|had|of|in|at|on|"
        r"based on|according to)\b",
        rest,
        maxsplit=1,
    )[0]
    noun = re.sub(r"^(?:the|a|an)\s+", "", noun, flags=re.I).strip(" ?\"'")
    if noun:
        return "the question asks for the specific " + noun
    return "the question asks for the specific entity asked about"


def _decompose_messages(question: str) -> List[Dict[str, str]]:
    """Build the single tool-free message pair for GenDec-style decomposition."""
    payload = {
        "task": (
            "decompose into ordered search directions and classify whether later "
            "directions require an evidence-derived bridge from an earlier one; "
            "do not invent the bridge value"
        ),
        "question": question,
    }
    return [
        {"role": "system", "content": _DECOMPOSE_SYSTEM_INSTRUCTION},
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        },
    ]


def _assistant_tool_message(call_id: str, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """A legal OpenAI assistant message announcing a tool call."""
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))},
            }
        ],
    }


def _tool_message(call_id: str, name: str, content: str) -> Dict[str, str]:
    return {"role": "tool", "tool_call_id": call_id, "name": name, "content": content}


def _validate_tool_arguments(name: str, args: object) -> Dict[str, Any]:
    """Strictly match parsed tool arguments against the frozen QUERY_TOOLS.

    ``wiki_search`` must carry exactly a string ``query``; ``wiki_read`` must
    carry exactly a list of strings ``paths``.  Any other key, a missing key, a
    wrong-typed value, or an unknown tool name fails closed with a stable
    ``MALFORMED_TOOL_CALL`` marker.  The actual legality of read path values is
    left to the existing read result, which rejects them stably.
    """
    if not isinstance(args, dict):
        raise ValueError(MALFORMED_TOOL_CALL + ":TOOL_ARGUMENTS_NOT_OBJECT")

    if name == "wiki_search":
        if set(args) != {"query"}:
            raise ValueError(_WIKI_SEARCH_ARGS_KEYS)
        if not isinstance(args.get("query"), str):
            raise ValueError(_WIKI_SEARCH_QUERY_NOT_STRING)
        return args

    if name == "wiki_read":
        # 容错：LLM 常把单个路径写成字符串而非 list、或带无关 key。仅要求
        # paths 存在且能规整为非空字符串列表。
        if "paths" not in args or set(args) - {"paths", "cursor", "page_size", "range"}:
            raise ValueError(_WIKI_READ_ARGS_KEYS)
        paths = args.get("paths")
        if isinstance(paths, str):
            paths = [paths]
        elif isinstance(paths, list):
            paths = [p if isinstance(p, str) else str(p) for p in paths]
        else:
            raise ValueError(_WIKI_READ_PATHS_NOT_STRING_LIST)
        if not paths:
            raise ValueError(_WIKI_READ_PATHS_NOT_STRING_LIST)
        if "cursor" in args and not isinstance(args["cursor"], str):
            raise ValueError(MALFORMED_TOOL_CALL + ":WIKI_READ_CURSOR")
        if "page_size" in args and (isinstance(args["page_size"], bool)
                or not isinstance(args["page_size"], int)
                or not 1 <= args["page_size"] <= 200):
            raise ValueError(MALFORMED_TOOL_CALL + ":WIKI_READ_PAGE_SIZE")
        if "range" in args:
            value = args.get("range")
            if (not isinstance(value, list) or len(value) != 2
                    or any(isinstance(item, bool) or not isinstance(item, int)
                           or item < 0 for item in value)
                    or value[1] <= value[0]):
                raise ValueError(MALFORMED_TOOL_CALL + ":WIKI_READ_RANGE")
            if len(paths) != 1:
                raise ValueError(MALFORMED_TOOL_CALL + ":WIKI_READ_RANGE_SINGLE_PATH")
        return {**args, "paths": paths}

    if name == "table_inspect":
        if set(args) != {"table_id"} or not isinstance(args.get("table_id"), str):
            raise ValueError(MALFORMED_TOOL_CALL + ":TABLE_INSPECT_ARGS")
        return args
    if name == "table_locate":
        if set(args) - {"literal", "table_id", "field_id", "cursor"} or not isinstance(args.get("literal"), str):
            raise ValueError(MALFORMED_TOOL_CALL + ":TABLE_LOCATE_ARGS")
        return args
    if name == "table_read":
        if "table_id" in args:
            structured, raw = args.get("scope"), args.get("raw_scope")
            scope = structured if raw is None else raw if structured is None else None
            # The nested scope already owns the table identity and request
            # binding. Discard only an identical transport-level repetition;
            # a conflict or missing identity must still fail closed.
            repeated = args["table_id"]
            if (not isinstance(scope, dict) or not isinstance(repeated, str)
                    or not repeated.strip() or repeated != scope.get("table_id")):
                raise ValueError(MALFORMED_TOOL_CALL + ":TABLE_READ_ARGS")
            args = {key: value for key, value in args.items() if key != "table_id"}
        if set(args) - {"scope", "raw_scope", "cursor"}:
            raise ValueError(MALFORMED_TOOL_CALL + ":TABLE_READ_ARGS")
        # Exactly one mode: a structured scope names fields and conditions, a
        # raw scope names an explicit coordinate rectangle.  Accepting both (or
        # neither) would leave which reading was meant to the reader.
        structured, raw = args.get("scope"), args.get("raw_scope")
        if (structured is None) == (raw is None):
            raise ValueError(MALFORMED_TOOL_CALL + ":TABLE_READ_ARGS")
        # The scope is the one argument with a nested shape, and an unknown key
        # in it used to reach ScopeSpec and raise a ValidationError that killed
        # the whole run.  Fail closed with a stable marker the model can correct.
        if structured is not None:
            if not isinstance(structured, dict):
                raise ValueError(MALFORMED_TOOL_CALL + ":TABLE_READ_ARGS")
            if set(structured) - _TABLE_READ_SCOPE_KEYS:
                raise ValueError(MALFORMED_TOOL_CALL + ":TABLE_READ_SCOPE")
            return args
        if not isinstance(raw, dict):
            raise ValueError(MALFORMED_TOOL_CALL + ":TABLE_READ_ARGS")
        if set(raw) - _TABLE_READ_RAW_KEYS:
            raise ValueError(MALFORMED_TOOL_CALL + ":TABLE_READ_RAW_SCOPE")
        if not isinstance(raw.get("table_id"), str) or not raw["table_id"].strip():
            raise ValueError(MALFORMED_TOOL_CALL + ":TABLE_READ_RAW_SCOPE")
        for key in ("row_start", "row_end"):
            if isinstance(raw.get(key), bool) or not isinstance(raw.get(key), int):
                raise ValueError(MALFORMED_TOOL_CALL + ":TABLE_READ_RAW_SCOPE")
        # Raw mode never filters by a field name or aggregates: only explicit
        # coordinates are accepted, and a half-open column window is refused
        # rather than widened to the whole table.
        if (raw.get("column_start") is None) != (raw.get("column_end") is None):
            raise ValueError(MALFORMED_TOOL_CALL + ":TABLE_READ_RAW_SCOPE")
        if "include_hidden" in raw and not isinstance(raw["include_hidden"], bool):
            raise ValueError(MALFORMED_TOOL_CALL + ":TABLE_READ_RAW_SCOPE")
        for key in ("column_start", "column_end"):
            if key in raw and (isinstance(raw[key], bool) or not isinstance(raw[key], int)):
                raise ValueError(MALFORMED_TOOL_CALL + ":TABLE_READ_RAW_SCOPE")
        for key in ("row_start", "row_end", "column_start", "column_end"):
            if key in raw and raw[key] < 1:
                raise ValueError(MALFORMED_TOOL_CALL + ":TABLE_READ_RAW_SCOPE")
        if raw["row_end"] < raw["row_start"] or (
                "column_start" in raw and raw["column_end"] < raw["column_start"]):
            raise ValueError(MALFORMED_TOOL_CALL + ":TABLE_READ_RAW_SCOPE")
        return args
    if name == "table_compute":
        from jonex_wiki.domain.engine.query.numeric_calculate import (
            CALCULATE_OPERATIONS,
        )
        from jonex_wiki.domain.engine.query.table_query import _ALLOWED_OPERATIONS
        if args.get("operation") == "calculate" and "steps" in args and "calculate" not in args:
            args = dict(args)
            args["calculate"] = {"steps": args.pop("steps")}
            if "label" in args:
                args["calculate"]["label"] = args.pop("label")
        if "operation" not in args and isinstance(args.get("calculate"), dict):
            args = {**args, "operation": "calculate"}
        if args.get("operation") == "calculate" and isinstance(args.get("calculate"), dict):
            # Known provider shorthand: flat evidence operands and a result
            # display precision. Normalize transport, then retain plan validation.
            args = dict(args)
            plan = dict(args["calculate"])
            steps = plan.get("steps")
            if isinstance(steps, list) and steps and all(isinstance(step, dict) for step in steps):
                steps = [dict(step) for step in steps]
                step_ids = {step.get("step_id") for step in steps
                            if isinstance(step.get("step_id"), str)}
                for step in steps:
                    operands = step.get("operands")
                    if isinstance(operands, list):
                        normalized = []
                        for value in operands:
                            if isinstance(value, dict):
                                value = dict(value)
                                if value.get("kind") == "result" and "result" in value:
                                    value.pop("kind")
                                result = value.get("result")
                                if isinstance(result, str) and result in step_ids and not result.isdigit():
                                    value["result"] = "$" + result
                                if "kind" in value and not ({"operand", "result"} & set(value)):
                                    value = {"operand": value}
                                if "operand" in value and "label" in value:
                                    operand = dict(value["operand"])
                                    label = value.pop("label")
                                    if "label" in operand and operand["label"] != label:
                                        raise ValueError(MALFORMED_TOOL_CALL + ":TABLE_COMPUTE_ARGS")
                                    value["operand"] = {**operand, "label": label}
                            normalized.append(value)
                        step["operands"] = normalized
                precisions = [container.pop("display_decimals") for container in (args, plan)
                              if "display_decimals" in container]
                if precisions:
                    precision = precisions[0]
                    if any(item != precision for item in precisions):
                        raise ValueError(MALFORMED_TOOL_CALL + ":TABLE_COMPUTE_ARGS")
                    if "display_decimals" in steps[-1] and steps[-1]["display_decimals"] != precision:
                        raise ValueError(MALFORMED_TOOL_CALL + ":TABLE_COMPUTE_ARGS")
                    steps[-1]["display_decimals"] = precision
                plan["steps"] = steps
            args["calculate"] = plan
        allowed = {"table_id", "operation", "filters", "group_by", "target_column",
                   "return_columns", "cell_match", "request_id",
                   "value_column", "min_distinct", "calculate"}
        if set(args) - allowed or "operation" not in args:
            raise ValueError(MALFORMED_TOOL_CALL + ":TABLE_COMPUTE_ARGS")
        # ``calculate`` is the one branch that is not about a single table's
        # completed read: its operands name delivered evidence themselves, so it
        # requires a plan instead of a table_id.
        if args.get("operation") == "calculate":
            if set(args) - {"operation", "calculate"} or not isinstance(
                    args.get("calculate"), dict):
                raise ValueError(MALFORMED_TOOL_CALL + ":TABLE_COMPUTE_ARGS")
            return args
        if "table_id" not in args:
            raise ValueError(MALFORMED_TOOL_CALL + ":TABLE_COMPUTE_ARGS")
        if not isinstance(args.get("table_id"), str) or not args["table_id"].strip():
            raise ValueError(MALFORMED_TOOL_CALL + ":TABLE_COMPUTE_ARGS")
        if "request_id" in args and (not isinstance(args.get("request_id"), str)
                                     or not args["request_id"].strip()):
            raise ValueError(MALFORMED_TOOL_CALL + ":TABLE_COMPUTE_ARGS")
        # The accepted operation set is the planner's set, not a copy of it: a
        # literal here silently rejected duplicate_groups for as long as the two
        # lists disagreed, and the model only saw MALFORMED_TOOL_CALL.
        if args.get("operation") not in _ALLOWED_OPERATIONS:
            raise ValueError(MALFORMED_TOOL_CALL + ":TABLE_COMPUTE_ARGS")
        if "filters" in args and not isinstance(args["filters"], list):
            raise ValueError(MALFORMED_TOOL_CALL + ":TABLE_COMPUTE_ARGS")
        if "return_columns" in args and (not isinstance(args["return_columns"], list)
                                         or not all(isinstance(c, str) for c in args["return_columns"])):
            raise ValueError(MALFORMED_TOOL_CALL + ":TABLE_COMPUTE_ARGS")
        for key in ("group_by", "target_column", "value_column"):
            if key in args and args[key] is not None and not isinstance(args[key], str):
                raise ValueError(MALFORMED_TOOL_CALL + ":TABLE_COMPUTE_ARGS")
        if "min_distinct" in args and args["min_distinct"] is not None:
            if isinstance(args["min_distinct"], bool) or not isinstance(args["min_distinct"], int):
                raise ValueError(MALFORMED_TOOL_CALL + ":TABLE_COMPUTE_ARGS")
        if "cell_match" in args and args["cell_match"] is not None and not isinstance(args["cell_match"], dict):
            raise ValueError(MALFORMED_TOOL_CALL + ":TABLE_COMPUTE_ARGS")
        return args

    raise ValueError(_UNKNOWN_QUERY_TOOL)


def _trace_control_events(
    trace: List[TraceEntry],
    events: Sequence[ControlEvent],
    ending_state: ControlState,
) -> None:
    """Publish already-validated control events with replay sequence numbers."""

    frozen = tuple(events)
    first_sequence = ending_state.event_count - len(frozen) + 1
    for offset, event in enumerate(frozen):
        trace.append(
            TraceEntry(
                event="control_state_event",
                detail={
                    "sequence": first_sequence + offset,
                    "event": event.model_dump(mode="json"),
                },
            )
        )


def _publish_control_transition(
    state: Dict[str, Any],
    runtime: ControllerRuntime,
    events: Sequence[ControlEvent],
    trace: List[TraceEntry],
) -> None:
    """Commit one atomically validated controller transition to state/trace."""

    state["controller"] = runtime
    _trace_control_events(trace, events, runtime.state)


def _publish_incomplete_terminal(
    state: Dict[str, Any],
    signals: RuntimeFailureSignals,
    trace: List[TraceEntry],
) -> FailureClass:
    """Classify and publish one incomplete terminal on the replay stream."""

    decision: FailureDecision = classify_runtime_failure(signals)
    incomplete, terminal = finish_incomplete(
        state["controller"],
        decision.failure_class,
    )
    _publish_control_transition(state, incomplete, (terminal,), trace)
    detail = decision.model_dump(mode="json")
    detail["stop_reason"] = signals.stop_reason
    trace.append(TraceEntry(event="failure_classified", detail=detail))
    return decision.failure_class


# ──────────────────────────────────────────────────────────────
#  History recomposition (X1)
# ──────────────────────────────────────────────────────────────

RECOMPOSE_KEEP_RECENT = {
    "wiki_read": 1,
    "wiki_search": 1,
    "table_inspect": 1,
    "table_locate": 1,
    "table_read": 1,
}
RECOMPOSE_FRESH_TOOL_MESSAGES = 5
RECOMPOSE_MAX_STATUS_BYTES = 1200
RECOMPOSE_MAX_PATHS = 8
RECOMPOSE_OPTIONAL_FIELDS = ("note", "fields", "header_labels", "evidence_handles",
                             "results")


def _recompose_encoded_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")).encode("utf-8"))


def _recompose_fit_status(status: Dict[str, Any],
                          ) -> tuple[Optional[str], List[str], Dict[str, Any]]:
    """Serialise one status inside the byte bound, or refuse to recompose it.

    Returns ``(content, omitted_fields, detail)``.  ``content`` is ``None`` only
    when the required context alone cannot fit: then the caller keeps the
    original message byte-for-byte rather than publishing a broken payload.
    """
    omitted: List[str] = []
    if _recompose_encoded_size(status) <= RECOMPOSE_MAX_STATUS_BYTES:
        return (json.dumps(status, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")), omitted,
                {"bytes": _recompose_encoded_size(status),
                 "kept_uncompressed_required_context": False})
    for field in RECOMPOSE_OPTIONAL_FIELDS:
        if field not in status:
            continue
        del status[field]
        omitted.append(field)
        status["omitted_fields"] = list(omitted)
        size = _recompose_encoded_size(status)
        if size <= RECOMPOSE_MAX_STATUS_BYTES:
            return (json.dumps(status, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")), omitted,
                    {"bytes": size, "kept_uncompressed_required_context": False})
    status.pop("omitted_fields", None)
    return (None, omitted,
            {"required_bytes": _recompose_encoded_size(status),
             "kept_uncompressed_required_context": True,
             "reason": "required_context_exceeds_limit"})


def _recompose_status(name: str, payload: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The short status a consumed tool result is replaced with."""
    status: Dict[str, Any] = {
        "recomposed": True,
        "tool": name,
        "note": ("content was delivered to the model when it was produced and "
                 "has been read; the full result stays in the run trace"),
    }
    for key in ("request_id", "table_id", "interpretation_id", "literal",
                "field_id", "search_scope_complete", "title", "bounds",
                "has_more", "next_cursor", "status", "pages", "coverage", "request_coverage",
                "returned_ranges", "pagination", "match_count", "continuation",
                "next_step", "page_index", "start_record_index"):
        if key in payload:
            status[key] = payload[key]
    if "table_id" not in status:
        for alias in ("searched_table_id", "table"):
            if payload.get(alias):
                status["table_id"] = payload[alias]
                break
    if "field_id" not in status and payload.get("searched_field_id"):
        status["field_id"] = payload["searched_field_id"]
    files = payload.get("files")
    if isinstance(files, dict):
        status["files"] = sorted(str(path) for path in files)[:RECOMPOSE_MAX_PATHS]
    elif isinstance(files, list):
        paths = [item.get("path") or item.get("file") for item in files
                 if isinstance(item, dict)]
        status["files"] = [path for path in paths if path][:RECOMPOSE_MAX_PATHS] or len(files)
    results = payload.get("results")
    if isinstance(results, list):
        status["result_count"] = len(results)
        status["results"] = [
            {"path": item.get("path"), "title": str(item.get("title") or "")[:80]}
            for item in results[:RECOMPOSE_MAX_PATHS] if isinstance(item, dict)
            and item.get("path")]
    if payload.get("query"):
        status["query"] = payload["query"]
    notes = []
    for note in payload.get("footnote_refs") or []:
        if isinstance(note, dict) and note.get("text"):
            notes.append({"kind": note.get("kind"), "text": str(note["text"])[:200]})
    if notes:
        status["footnote_refs"] = notes[:4]
    labels = []
    for item in payload.get("context_items") or []:
        if isinstance(item, dict) and item.get("kind") == "header" and item.get("raw_value"):
            labels.append("{}={}".format(item.get("coordinate"), item["raw_value"])[:60])
    if labels:
        status["header_labels"] = labels[:12]
    if isinstance(payload.get("matches"), list):
        status["matches"] = len(payload["matches"])
    fields = [field.get("label") for field in payload.get("fields") or []
              if isinstance(field, dict)]
    if fields:
        status["fields"] = [label for label in fields if label is not None]
    handles = payload.get("evidence")
    if isinstance(handles, list):
        brief = []
        for handle in handles:
            if isinstance(handle, dict):
                brief.append({key: handle.get(key) for key in ("handle", "kind", "table_id")
                              if handle.get(key) is not None})
        if brief:
            status["evidence_handles"] = brief
    if isinstance(payload.get("cells"), list):
        status["returned_cell_count"] = len(payload["cells"])
    return status


def _recompose_subject(name: str, payload: Mapping[str, Any]) -> str:
    """What a result is *about*: its table, or the pages it delivered."""
    if name == "wiki_search":
        return str(payload.get("query") or "")
    if name == "table_locate":
        return json.dumps([payload.get("table_id") or payload.get("searched_table_id"),
                           payload.get("literal")], separators=(",", ":"))
    if payload.get("table_id") or payload.get("searched_table_id"):
        return str(payload.get("table_id") or payload.get("searched_table_id"))
    files = payload.get("files")
    if isinstance(files, dict):
        subject = "|".join(sorted(str(path) for path in files))
        if name == "wiki_read" and payload.get("views"):
            # Two ranges of the same article are different reading results.
            subject += "::" + json.dumps([
                [view.get("article_path"), view.get("spans")]
                for view in payload["views"] if isinstance(view, dict)
            ], sort_keys=True, separators=(",", ":"))
        return subject
    return ""


def _recompose_consumed_history(messages: List[Dict[str, Any]],
                                state: MutableMapping[str, Any]) -> Dict[str, Any]:
    """Shorten consumed tool results in place; return a small audit report.

    The newest result of every (tool, subject) pair is kept verbatim *while it
    is fresh* - it is one of the last ``RECOMPOSE_FRESH_TOOL_MESSAGES`` tool
    results - so the evidence the model is currently working on, the coverage of
    the read in flight and the handles it may cite are never reduced to a status
    line.  Once it is older than that window it becomes a status too.
    """
    keep: Dict[str, int] = {}
    indexes: Dict[str, List[int]] = {}
    read_positions: List[int] = []
    for index, message in enumerate(messages):
        if message.get("role") != "tool":
            continue
        name = str(message.get("name") or "")
        subject = ""
        try:
            payload = json.loads(message.get("content") or "{}")
        except (TypeError, ValueError):
            payload = {}
        if isinstance(payload, dict):
            subject = _recompose_subject(name, payload)
            if name == "wiki_read" or any(
                    block.get("body") for view in payload.get("views") or []
                    if isinstance(view, dict) for block in view.get("blocks") or []
                    if isinstance(block, dict)):
                read_positions.append(index)
        indexes.setdefault("{}::{}".format(name, subject), []).append(index)
    tool_positions = [index for index, message in enumerate(messages)
                      if message.get("role") == "tool"]
    fresh = set(tool_positions[-RECOMPOSE_FRESH_TOOL_MESSAGES:])
    consumed = int(state.get("consumed_history_messages", len(messages)))
    fresh.update(i for i in tool_positions if i >= consumed)
    recent_reads = set(read_positions[-2:]) if read_positions else set()
    for name, positions in indexes.items():
        configured = max(0, int(RECOMPOSE_KEEP_RECENT.get(name.split("::")[0],
                                                          len(positions))))
        keep[name] = configured if positions and positions[-1] in fresh else 0
        if positions and positions[-1] in read_positions:
            keep[name] = 1 if positions[-1] in recent_reads else 0
    changed: Dict[str, int] = {}
    omitted_report: Dict[str, List[str]] = {}
    kept_reason: Dict[str, str] = {}
    kept_uncompressed = 0
    freed = 0
    for name, positions in indexes.items():
        superseded = positions[: len(positions) - keep[name]]
        for index in superseded:
            if index >= consumed:
                continue  # This result has not reached a model request yet.
            message = messages[index]
            try:
                payload = json.loads(message.get("content") or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict) or payload.get("recomposed"):
                continue
            tool = str(message.get("name") or name.split("::")[0])
            status = _recompose_status(tool, payload)
            if status is None:
                continue
            content, omitted, detail = _recompose_fit_status(status)
            if content is None:
                kept_uncompressed += 1
                kept_reason[tool] = str(detail.get("reason"))
                continue
            before = len(str(message.get("content") or "").encode("utf-8"))
            after = len(content.encode("utf-8"))
            if after >= before:
                continue
            message["content"] = content
            changed[tool] = changed.get(tool, 0) + 1
            if omitted:
                omitted_report[tool] = list(omitted)
            freed += before - after
    report = {"recomposed": changed, "bytes_freed": freed,
              "kept_verbatim": keep, "messages": len(messages),
              "omitted_fields": omitted_report,
              "kept_uncompressed_required_context": kept_uncompressed,
              "kept_reason": kept_reason}
    if changed or kept_uncompressed:
        state["recomposed_history"] = report
    return report


# ──────────────────────────────────────────────────────────────
#  Observed blocking facts (grounded-incomplete inputs)
# ──────────────────────────────────────────────────────────────

# Only these two observations may justify a grounded refusal.  A search that
# found nothing is not one of them: it never claims a table was searched.
INCOMPLETE_FACT_KINDS = ("no_match_in_complete_table_scope", "structure_ambiguous")


def _observation_table_labels(observation: Mapping[str, Any]) -> List[str]:
    """The table's own labels, read from the observation the reader already made.

    Source captions first (the document's own name for the table), then the
    artifact title, then any caption/table-note context item the read returned.
    Never a search snippet, never a column name, and never a database access.
    """
    labels: List[str] = []

    def add(value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in labels:
            labels.append(text)

    for caption in observation.get("captions") or []:
        add(caption)
    add(observation.get("table_title") or observation.get("title"))
    for item in observation.get("context_items") or []:
        if item.get("kind") in ("caption", "table_note"):
            add(item.get("text") or item.get("label"))
    return labels


def _remember_table_labels(state: MutableMapping[str, Any], table_id: str,
                           labels: Sequence[str]) -> None:
    """Cache this query's table-name metadata; it never reads cells."""
    if not table_id:
        return
    cache = state.setdefault("table_labels_by_id", {})
    merged = list(cache.get(str(table_id)) or [])
    for label in labels:
        if label and label not in merged:
            merged.append(label)
    cache[str(table_id)] = merged


def _fact_id(fact: Mapping[str, Any]) -> str:
    """Canonical content hash of one observed fact."""
    payload = {key: fact.get(key) for key in (
        "kind", "revision_id", "table_id", "interpretation_id", "field_id",
        "literal", "scope_complete")}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _record_incomplete_fact(state: MutableMapping[str, Any], fact: Dict[str, Any],
                            trace: List[TraceEntry], *,
                            revision_id: Optional[str] = None) -> None:
    """Record one observed fact that may justify a grounded incomplete answer.

    Evidence that supports a value and an observed blocking fact that explains
    why no value can be given are two different things.  Only the second kind is
    recorded here, only from a *complete* table observation - a locate over the
    whole target table with zero matches, or a table whose structure is
    ambiguous - and only with the scope the observation actually covered.

    Every fact carries the identity a refusal is bound to: the pinned revision,
    the table's own labels and a canonical content hash, so the refusal template
    is rendered from these fields in code and never from a model draft.
    """
    kind = fact.get("kind")
    if kind not in INCOMPLETE_FACT_KINDS:
        raise ValueError("INCOMPLETE_FACT_KIND_INVALID:" + str(kind))
    recorded = dict(fact)
    if revision_id is not None and not recorded.get("revision_id"):
        recorded["revision_id"] = revision_id
    if not recorded.get("revision_id"):
        recorded["revision_id"] = getattr(state.get("bundle_view"), "revision_id", None)
    recorded.setdefault("table_labels", [])
    recorded["fact_id"] = _fact_id(recorded)
    _remember_table_labels(state, str(recorded.get("table_id") or ""),
                           recorded.get("table_labels") or [])
    facts = state.setdefault("incomplete_facts", [])
    if any(existing.get("fact_id") == recorded["fact_id"] for existing in facts):
        return
    facts.append(recorded)
    trace.append(TraceEntry(event="incomplete_fact_observed", detail={
        "kind": kind, "table_id": recorded.get("table_id"),
        "literal": recorded.get("literal"),
        "scope_complete": recorded.get("scope_complete"),
        "table_labels": list(recorded.get("table_labels") or []),
        "fact_id": recorded["fact_id"],
        "statement": recorded.get("statement"),
    }))


def _record_ambiguity_fact(state: MutableMapping[str, Any], table_id: str,
                           observation: Mapping[str, Any],
                           trace: List[TraceEntry], *,
                           table_labels: Optional[Sequence[str]] = None,
                           interpretation_status: Optional[str] = None,
                           revision_id: Optional[str] = None) -> None:
    """Record an ambiguous table structure as a fact a refusal may rest on.

    Only a table whose own interpretation status is ``ambiguous`` may become
    this fact.  A misspelled field, an illegal argument or a truncated read is
    not a structural ambiguity, so those refusals never reach this recorder.
    """
    status = interpretation_status or observation.get("interpretation_status")
    if str(status or "") != "ambiguous":
        return
    labels = [str(field.get("label")) for field in observation.get("fields") or []
              if field.get("label") is not None]
    if not labels:
        labels = [str(item.get("label")) for item in observation.get("context_items") or []
                  if item.get("kind") == "header" and item.get("label")]
    duplicates = sorted({label for label in labels if labels.count(label) > 1})
    observation_labels = (list(table_labels) if table_labels is not None
                          else _observation_table_labels(observation))
    _record_incomplete_fact(state, {
        "kind": "structure_ambiguous",
        "table_id": table_id,
        "interpretation_id": observation.get("interpretation_id"),
        "interpretation_status": "ambiguous",
        "field_id": None,
        "table_labels": observation_labels,
        "labels": duplicates,
        "statement": ("表 {} 的结构被判为歧义（{}），无法据此唯一确定取值。"
                      .format(table_id, "、".join(duplicates) or "structure_ambiguous")),
    }, trace, revision_id=(revision_id or observation.get("revision_id")
                           or observation.get("source_version")))


def _record_locate_miss_fact(state: MutableMapping[str, Any], table_id: str,
                             literal: str, field_id: Optional[str],
                             located: Mapping[str, Any],
                             trace: List[TraceEntry], *,
                             table_labels: Optional[Sequence[str]] = None,
                             field_valid: bool = True,
                             field_label: Optional[str] = None,
                             cursor: Optional[str] = None,
                             revision_id: Optional[str] = None) -> None:
    """Record a zero-match locate only when it covered the whole target table.

    Absence is claimable only when all of this holds: the search returned no
    match, it covered the complete search scope, it was not one page of a cursor
    walk, and the field it searched is a field the table really has.
    """
    if located.get("matches") or not located.get("search_scope_complete"):
        return
    if cursor:
        return
    if not field_valid:
        return
    labels = list(table_labels or [])
    _record_incomplete_fact(state, {
        "kind": "no_match_in_complete_table_scope",
        "table_id": table_id,
        "literal": literal,
        "field_id": field_id,
        "field_label": field_label,
        "table_labels": labels,
        "scope_complete": True,
        "statement": ("表 {} 的整个可搜索范围内没有字面值「{}」（该次搜索范围完整）；"
                      "这只能说明这张表内没有该值。".format(table_id, literal)),
    }, trace, revision_id=(revision_id or located.get("revision_id")
                           or located.get("source_version")))


def _locate_next_step(located: Mapping[str, Any], field_id: Optional[str]) -> Dict[str, Any]:
    """Name the one read that returns every match a locate just found.

    The matches carry their rows, so the controller already knows that a single
    constrained read returns them all in one page.  Without that sentence the
    model pages the table row block by row block and spends its whole budget on
    rows a single read had returned.
    """
    matches = list(located.get("matches") or [])
    if not matches:
        return {}
    rows = sorted({int(match["row_index"]) for match in matches
                   if match.get("row_index") is not None})
    tables = sorted({str(match.get("table_id")) for match in matches if match.get("table_id")})
    table_id = tables[0] if len(tables) == 1 else None
    constraints: Dict[str, str] = {}
    if field_id:
        constraints[str(field_id)] = str(located.get("literal"))
    step: Dict[str, Any] = {
        "match_count": len(matches),
        "matched_rows": rows[:32],
        "one_read_returns_all": True,
        "advice": (
            "these matches are rows {} of one table; a single table_read with "
            "constraints {} returns every matched row in one page - do not read "
            "the table block by block".format(rows[:32], constraints or "on the searched field")
            if table_id else
            "these matches span several tables; read each table once with the "
            "searched field as a constraint"),
    }
    if table_id:
        step["scope"] = {"table_id": table_id, "constraints": constraints,
                         "task_type": "enumerate"}
    return step


# ──────────────────────────────────────────────────────────────
#  Table citation handles (cells, notes, ranges)
# ──────────────────────────────────────────────────────────────

def _cell_handles(cells: List[Dict[str, Any]], table_id: str, info: Mapping[str, Any],
                  state: Dict[str, Any], *, context_cells: Sequence[Sequence[int]],
                  revision_id: Optional[str] = None,
                  interpretation_id: Optional[str] = None,
                  refs: Optional[Dict[str, Dict[str, Any]]] = None) -> List[Dict[str, str]]:
    """Build one page's evidence handles plus its table citation references.

    The excerpt, the claim id and the value hash come from the one shared record
    builder, so registration and cold re-open cannot drift apart.  A caller
    preparing a *candidate* can pass its own ``refs`` dict so the run's
    ``table_evidence_refs`` index is only populated after the candidate is
    admitted.
    """
    from jonex_wiki.domain.engine.query.table_citation import (
        build_table_citation_record,
    )

    handles = []
    target_refs = refs if refs is not None else state.setdefault("table_evidence_refs", {})
    for cell in cells:
        ref = {
            "kind": "table_cell", "revision_id": revision_id,
            "source_id": info.get("source_id"), "document_id": info.get("document_id"),
            "path": info.get("path"), "table_id": table_id,
            "interpretation_id": interpretation_id, "sheet_id": cell.get("sheet_id"),
            "row": int(cell.get("row_index")), "column": int(cell.get("column_index")),
            "coordinate": cell.get("coordinate"), "context_cells": list(context_cells),
        }
        record = build_table_citation_record(ref, cell)
        target_refs[record["claim_id"]] = {**ref, **record}
        handles.append({
            "claim_id": record["claim_id"],
            "passage_id": "table_range_" + table_id,
            "source_id": info["source_id"], "document_id": info["document_id"],
            "chunk_id": str(cell.get("coordinate") or "R{}C{}".format(
                cell.get("row_index"), cell.get("column_index"))),
            "excerpt": record["excerpt"],
            "page_path": str(info.get("path") or "")})
    return handles


def _note_handles(result: Mapping[str, Any], table_id: str, info: Mapping[str, Any],
                  state: Dict[str, Any], *, revision_id: Optional[str] = None,
                  interpretation_id: Optional[str] = None,
                  refs: Optional[Dict[str, Dict[str, Any]]] = None) -> List[Dict[str, str]]:
    """Handles for the caption/footnote text a read delivered.

    A table's own note is source text the relation check already uses; without a
    handle it can be shown to the model and still be uncitable, so the final
    answer can never state the condition it was shown.
    """
    from jonex_wiki.domain.engine.query.table_citation import build_table_note_record

    handles = []
    target_refs = refs if refs is not None else state.setdefault("table_evidence_refs", {})
    for item in result.get("footnote_refs") or []:
        text = str(item.get("text") or "")
        note_id = str(item.get("id") or "")
        if not text.strip() or not note_id:
            continue
        ref = {
            "kind": "table_note", "revision_id": revision_id,
            "source_id": info.get("source_id"), "document_id": info.get("document_id"),
            "path": info.get("path"), "table_id": table_id,
            "interpretation_id": interpretation_id, "sheet_id": result.get("sheet_id"),
            "note_id": note_id, "note_kind": item.get("kind"),
        }
        record = build_table_note_record(ref, item)
        target_refs[record["claim_id"]] = {**ref, **record}
        handles.append({
            "claim_id": record["claim_id"], "passage_id": "table_note_" + table_id,
            "source_id": info["source_id"], "document_id": info["document_id"],
            "chunk_id": note_id, "excerpt": record["excerpt"],
            "page_path": str(info.get("path") or "")})
    return handles


def _excel_coordinate(value: Any) -> tuple[int, int] | None:
    match = re.fullmatch(r"([A-Z]+)([0-9]+)", str(value or "").strip().upper())
    if match is None:
        return None
    column = 0
    for character in match.group(1):
        column = column * 26 + ord(character) - 64
    return int(match.group(2)), column


def _merge_anchor_coordinate(value: Any) -> tuple[int, int] | None:
    """Resolve a merge anchor in either of the two coordinate spaces.

    An xlsx artifact stores a merge anchor in the Excel A1 form; a PDF table
    stores the grid-native R{row}C{column} form, which is also the form the
    reader stamps onto every merge_anchor it delivers (see table_artifact and
    table_reader). Both name a cell in the same row/column space.

    Reading only A1 here is not a cosmetic miss: _freeze_table_request_projection
    is the one place that attaches the anchor cell own handle to a
    merged-covered cell, and table_query refuses the whole request with
    merge_anchor_evidence_missing when that handle is absent. With an
    unparsable anchor every merged cell of a PDF table silently loses its
    anchor evidence and every table_compute over that table is blocked.
    """
    match = re.fullmatch(r"R([0-9]+)C([0-9]+)", str(value or "").strip().upper())
    if match is not None:
        return int(match.group(1)), int(match.group(2))
    return _excel_coordinate(value)


def _record_table_blocker(state: Dict[str, Any], *, table_id: str, request_id: str,
                          coverage: Mapping[str, Any], reasons: Sequence[str],
                          trace: List[TraceEntry]) -> None:
    entry = {"table_id": table_id, "request_id": request_id, "coverage": dict(coverage),
             "ambiguities": list(reasons)}
    state.setdefault("table_scope_blockers", []).append(entry)
    trace.append(TraceEntry(event="table_evidence_withheld", detail=entry))


def _relation_cells_compact(cells: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """One relation-input entry per spreadsheet row.

    Same values and coordinates as the per-cell records, without repeating ten
    default metadata fields per cell.
    """
    columns = sorted({int(cell["column_index"]) for cell in cells
                      if cell.get("column_index") is not None})
    rows: Dict[int, Dict[int, Any]] = {}
    for cell in cells:
        rows.setdefault(int(cell.get("row_index")), {})[int(cell.get("column_index"))] = (
            _delivered_cell_value(cell))
    return {
        "column_indexes": columns,
        "rows": [[row] + [values.get(column, "") for column in columns]
                 for row, values in sorted(rows.items())],
    }


def _relation_record_groups(records: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Losslessly share repeated field names at the model-message boundary.

    Consecutive records with identical keys share a field vector. Missing keys
    remain distinct from null values; ordering, all values and nested objects
    are preserved. No question-dependent selection or truncation is applied.
    """
    groups: List[Dict[str, Any]] = []
    for record in records:
        fields = sorted(record)
        if not groups or groups[-1]["fields"] != fields:
            groups.append({"fields": fields, "records": []})
        groups[-1]["records"].append([record[field] for field in fields])
    return groups


def _sheet_name_for(state: Dict[str, Any], info: Mapping[str, Any],
                    sheet_id: Any) -> Optional[str]:
    """Resolve one worksheet name from the published grid (best effort)."""
    import sqlite3

    sheet_id = str(sheet_id or "")
    if not sheet_id:
        return None
    bundle_view = state.get("bundle_view")
    tables_path = (info or {}).get("path")
    if bundle_view is None or not tables_path:
        return None
    try:
        connection = bundle_view.open_sqlite(tables_path)
    except Exception:
        return None
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT name FROM sheets WHERE sheet_id=?", (sheet_id,)).fetchone()
        return str(row["name"]) if row is not None else None
    except Exception:
        return None
    finally:
        connection.close()


# ──────────────────────────────────────────────────────────────
#  Bounded table delivery
# ──────────────────────────────────────────────────────────────

# A table message may not grow without bound: the model pays for it again on
# every later call of the run.  The cap is spent on what was read (cells,
# context, coverage); only the citation allowlist is shortened, and the
# shortening is stated in the payload.
TABLE_DELIVERY_LIMIT = 16 * 1024
# The citation allowlist is echoed in every table message, and the window it
# echoes grows with the run.  The newest records are the ones the model is about
# to cite; the rest keep their number and are counted, not silently dropped.
# Twelve was low enough that a single delivered page (84 cells) could not be
# cited from: the model had read the cells but could only name the newest
# twelve, so a "list every row" answer could never carry its evidence.  The
# echo is now compact (handle + coordinate + value, ~45 B instead of ~180 B),
# so the same byte budget covers a whole page.
TABLE_DELIVERY_EVIDENCE_MAX = _env_int("QUERY_TABLE_DELIVERY_EVIDENCE_MAX", 64)
# The delivered grid is what the model is charged for, so it has its own
# ceiling: a table that fits is delivered whole (that is the point), and one
# that does not is trimmed *and says so*, with the read itself still complete.
TABLE_GRID_DELIVERY_LIMIT = _env_int("QUERY_TABLE_GRID_BYTES", 64 * 1024)
# A whole delivered table message (grid + context + citation echo).  The grid
# ceiling above bounds the values; this bounds the message.
TABLE_MESSAGE_LIMIT = _env_int("QUERY_TABLE_MESSAGE_BYTES", 96 * 1024)
# Above this many delivered cells the relation input switches to its row-wise
# form, which is what keeps a complete whole-table read from being blocked by
# the judge's own input ceiling.
RELATION_COMPACT_CELLS = _env_int("QUERY_RELATION_COMPACT_CELLS", 256)
# An exhaustive read is allowed a larger judge input than a scoped probe, for
# the same reason: it is the request shape whose evidence is legitimately the
# whole table.
EXHAUSTIVE_RELATION_INPUT_LIMIT = _env_int("QUERY_EXHAUSTIVE_RELATION_BYTES", 256 * 1024)


# A source interval's excerpt is plain text (the exact archived slice), so it
# used to fall through to the table branch's 120-character fallback: the model
# read a silent, mid-word cut of an interval whose archived page carried the
# whole 1000 characters, and nothing in the message said so or pointed at the
# rest.  The echo now spends the configured evidence-excerpt budget (the same
# ``QUERY_EVIDENCE_EXCERPT_LIMIT``, 500 by default, that the compact claim
# listing already uses), states when it had to stop, and - for an archived
# interval - returns the exact ``wiki_read`` range that continues it.  The head
# stays bounded by the historical 120 characters once this message has spent
# the whole context budget on plain-text excerpts, so a large active window
# cannot make one tool result unbounded.
EVIDENCE_ECHO_HEAD_CHARS = 120
EVIDENCE_ECHO_KIND_SOURCE = "source_interval"
EVIDENCE_ECHO_KIND_CLAIM = "claim_excerpt"


def _echo_excerpt(excerpt: Any, *, claim_id: str = "", page_path: str = "",
                  continuation: Optional[Mapping[str, Any]] = None,
                  spent: int = 0) -> Dict[str, Any]:
    """One evidence entry as the model reads it: handle, coordinate, value.

    The stored excerpt stays the canonical citation text; only this listing is
    shortened, and nothing downstream reads it.  A shortened plain-text entry
    says how much it delivered, how much it omitted and - when the interval is
    archived at a known range - which ``wiki_read`` call delivers the rest.
    """
    text = str(excerpt or "")
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, dict):
        value = payload.get("value")
        if value is None:
            value = payload.get("text")
        entry: Dict[str, Any] = {}
        if payload.get("coordinate"):
            entry["cell"] = payload["coordinate"]
        elif payload.get("row") is not None and payload.get("column") is not None:
            entry["cell"] = "R{}C{}".format(payload["row"], payload["column"])
        if value is not None:
            rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            entry["value"] = rendered if len(rendered) <= 120 else rendered[:117] + "..."
        if payload.get("note_kind"):
            entry["note"] = payload.get("note_kind")
        if not entry:
            entry["evidence"] = text[:120]
        return entry
    source_interval = str(claim_id).startswith("source_span_")
    allowance = _evidence_excerpt_limit()
    allowance = max(0, min(allowance, CONTEXT_BUDGET_CHARS - spent))
    entry = {
        "kind": EVIDENCE_ECHO_KIND_SOURCE if source_interval else EVIDENCE_ECHO_KIND_CLAIM,
        "evidence": text if len(text) <= allowance else text[:allowance],
    }
    if len(text) > allowance:
        entry["truncated"] = True
        entry["chars"] = allowance
        entry["total_chars"] = len(text)
        entry["omitted_chars"] = len(text) - allowance
        rest = _continuation_remainder(continuation, allowance)
        if rest is not None:
            entry["continue"] = rest
    return entry


def _continuation_remainder(
    continuation: Optional[Mapping[str, Any]], delivered: int
) -> Optional[Dict[str, Any]]:
    """The ``wiki_read`` call that delivers what this entry could not.

    The archived interval's range is known in article coordinates, and the
    entry delivered its first ``delivered`` characters, so the remainder is one
    explicit request.  Unsuitable or absent coordinates stay absent: a wrong
    range is worse than no range.
    """
    if not continuation or delivered <= 0:
        return None
    path = str(continuation.get("path") or "")
    span = continuation.get("range") or ()
    if not path or len(span) != 2:
        return None
    start, end = int(span[0]), int(span[1])
    rest_start, rest_end = start + delivered, end
    if not 0 <= rest_start < rest_end:
        return None
    return {
        "path": path,
        "range": [rest_start, rest_end],
        "note": ("this listing delivered the first {delivered} characters of the "
                 "archived interval; re-read the same page with this exact range "
                 "for the rest. It is the same evidence, not a new one.".format(
                     delivered=delivered)),
    }


def _source_interval_continuations(state: Mapping[str, Any], corpus: Any,
                                   paths: Any) -> Dict[str, Dict[str, Any]]:
    """Map archived source intervals to their exact continuation ranges.

    Keyed by the interval's ``claim_id`` (its stable record id).  An interval
    whose coordinates cannot be located in the published article is left out,
    so the echo never advertises a range that would read the wrong text.
    """
    from jonex_wiki.domain.engine.query.read_views import _read_context, resolve_focus

    continuations: Dict[str, Dict[str, Any]] = {}
    for path in paths or ():
        if not str(path).startswith("source-evidence/"):
            continue
        records = corpus.source_records.get(str(path)) or ()
        spans: List[Any] = []
        try:
            focus = resolve_focus(_read_context(state), corpus, str(path))
            spans = list(getattr(focus, "spans", ()) or ())
        except Exception:  # noqa: BLE001 - a missing continuation is not fatal
            spans = []
        if not spans:
            continue
        try:
            start, end = int(spans[0][0]), int(spans[0][1])
        except (TypeError, ValueError, IndexError):
            continue
        for record in records:
            claim_id = str(record.get("claim_id") or "")
            if claim_id:
                continuations[claim_id] = {"path": str(path),
                                           "range": [start, end]}
    return continuations


def _bounded_evidence_echo(state: Mapping[str, Any],
                           view: Optional[Dict[str, Any]] = None,
                           limit: int = TABLE_DELIVERY_EVIDENCE_MAX,
                           continuations: Optional[Mapping[str, Mapping[str, Any]]] = None):
    """Return the citation allowlist as it may be echoed in one tool result.

    Every tool result repeats the window so the model can name a handle it just
    read.  Repeating all of it in every message is what made a seven-row table
    question cost more than the whole run.  The newest ``limit`` records are
    echoed as ``handle``/``excerpt`` pairs and the rest are counted; their
    numbers are stable read-order handles, so an omitted handle stays citable
    and the omission is never silent.

    Each plain-text excerpt may spend up to the configured excerpt budget, and
    the whole message may spend ``CONTEXT_BUDGET_CHARS`` on them; beyond either
    bound the entry states the cut and, for an archived interval, carries the
    continuation range (``continuations`` keyed by the interval's record id).
    """
    query_state = state["query_state"]
    continuations = continuations or {}
    entries: List[Dict[str, Any]] = []
    spent = 0
    for evidence_id in query_state.active_evidence_ids:
        record = query_state.seen_evidence_by_id[evidence_id]
        entry = _echo_excerpt(record.excerpt, claim_id=record.claim_id,
                              page_path=record.page_path,
                              continuation=continuations.get(str(record.claim_id)),
                              spent=spent)
        evidence_text = entry.get("evidence")
        if isinstance(evidence_text, str):
            spent += len(evidence_text)
        entry["handle"] = query_state.evidence_handle(evidence_id)
        entries.append(entry)
    if len(entries) <= limit:
        return entries, None
    omitted = len(entries) - limit
    if view is not None:
        view["active_evidence"] = view["active_evidence"][omitted:]
    delivery = {
        "evidence_omitted": omitted,
        "note": ("handles are numbered in read order over the delivered ledger; "
                 "the {newest} newest are listed and every omitted handle stays "
                 "citable".format(newest=limit)),
    }
    return entries[omitted:], delivery


def _table_continuation_hint(table_payload: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The one copyable continuation a partial table read offers the model.

    ``next_cursor`` and ``requested_scope`` already ride the reader payload, but
    as separate fields, so a model re-derives a different row window and re-uses
    the old cursor, which the reader correctly rejects.  Coupling them in one
    ``continuation`` object and saying when to *drop* the cursor turns the
    reader's binding checks from a trap into a one-step instruction.
    """
    if not table_payload.get("has_more") or not table_payload.get("next_cursor"):
        return None
    return {
        "note": (
            "续读本请求下一页：用完全相同的 scope 与 cursor=next_cursor 再次调用 "
            "table_read。若要读不同的行/字段/条件，请省略 cursor（创建新请求；"
            "带旧 cursor 改 scope 会被拒绝）。"
        ),
        "scope": table_payload.get("requested_scope"),
        "cursor": table_payload.get("next_cursor"),
    }


def _delivered_cell_value(cell: Mapping[str, Any]) -> Any:
    """The value a reader would quote for one saved cell.

    A formula's cached value wins over the formula text (the reader's own rule),
    and a cell that states no value of its own reports the anchor value it was
    shown - the same fact ``effective_value`` states, so the delivered grid and
    ``table_compute`` never disagree about one cell.  An empty string is no value
    of its own: an expanded HTML span writes empty text into every position it
    covers.
    """
    if cell.get("formula") and cell.get("cached_value") is not None:
        return cell.get("cached_value")
    raw_value = cell.get("raw_value")
    if raw_value is not None and str(raw_value).strip() != "":
        return raw_value
    if cell.get("cached_value") is not None:
        return cell.get("cached_value")
    if cell.get("effective_value") is not None:
        return cell.get("effective_value")
    return raw_value


def _table_grid_view(cells: Sequence[Mapping[str, Any]],
                     context_items: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Render delivered cells as a value-oriented grid.

    One per-cell record costs ~260 B to carry ~6 characters of content (the
    coordinate, raw type, formula, number format and date-candidate of every
    cell, whether or not it differs from the default), so a 32 KiB page held 14
    rows of a table whose whole content is 5.9 KB.  The grid states the columns
    once and each row as its row number followed by its values; the identity of
    every cell (table, row, column, revision, interpretation, sheet) is what the
    citation handle already carries, so nothing citable is lost.  Measured on
    the 171-row commission table: 114 KB of cell records become 16.4 KB.
    """
    columns = sorted({int(cell["column_index"]) for cell in cells
                      if cell.get("column_index") is not None})
    headers: Dict[int, Any] = {}
    for item in context_items:
        if item.get("kind") != "header" or item.get("column_index") is None:
            continue
        headers.setdefault(int(item["column_index"]), _delivered_cell_value(item))
    rows: Dict[int, Dict[int, Any]] = {}
    for cell in cells:
        row_index = int(cell.get("row_index"))
        column_index = int(cell.get("column_index"))
        rows.setdefault(row_index, {})[column_index] = _delivered_cell_value(cell)
    grid = {
        "column_indexes": columns,
        "header": [headers.get(column, "") for column in columns],
        "rows": [[row] + [values.get(column, "") for column in columns]
                 for row, values in sorted(rows.items())],
        "row_numbers": "the first element of every row is its spreadsheet row number",
        "cells_returned": len(cells), "rows_returned": len(rows),
        "coordinates": "cell RnCm; blank means the saved cell is empty or inherits the merged cell above it",
    }
    if columns and len(columns) != (max(columns) - min(columns) + 1):
        grid["note"] = "only the requested columns are shown; column_indexes lists them"
    merged = _merged_inheritance(cells)
    if merged:
        # Values inherited from a merged anchor are already shown in place; this
        # states which cells those are, so nothing looks like a claim of its own.
        grid["merged_cells"] = merged
    return grid


_MERGE_INHERITANCE_LIMIT = 24


def _merge_range_text(anchor: str, column: int, first: int, last: int) -> str:
    if first == last:
        return "{} -> R{}C{}".format(anchor, first, column)
    return "{} -> R{}C{}-R{}C{}".format(anchor, first, column, last, column)


def _merged_inheritance(cells: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """State which delivered cells are covered by a merged anchor, and from where.

    Covered cells are those the stored merge geometry proves are not stating a
    value of their own: the compile marks them ``merged_covered`` and the grid
    expansion repeated the anchor's text into them (or the cell holds no value
    and inherits it).  Stating one range per anchor column (~30 B) beats a marker
    on every covered cell (~22 B each, 3.2 KB on the 147-cell commission table),
    and the raw page still lists the geometry in full.
    """
    grouped: Dict[tuple, List[int]] = {}
    for cell in cells:
        anchor = cell.get("merge_anchor")
        if not anchor or cell.get("row_index") is None or cell.get("column_index") is None:
            continue
        grouped.setdefault((str(anchor), int(cell["column_index"])), []).append(int(cell["row_index"]))
    if not grouped:
        return {}
    ranges: List[str] = []
    for (anchor, column), row_indexes in sorted(
            grouped.items(), key=lambda item: (item[0][1], min(item[1]))):
        start = previous = None
        for row_index in sorted(set(row_indexes)):
            if previous is not None and row_index == previous + 1:
                previous = row_index
                continue
            if previous is not None:
                ranges.append(_merge_range_text(anchor, column, start, previous))
            start = previous = row_index
        ranges.append(_merge_range_text(anchor, column, start, previous))
    result: Dict[str, Any] = {
        "anchor_to_covered": ranges[:_MERGE_INHERITANCE_LIMIT],
        "note": ("these cells are covered by the anchor's merge; the shown value is the "
                 "anchor's, not a value the cell stated on its own"),
    }
    if len(ranges) > _MERGE_INHERITANCE_LIMIT:
        result["omitted_ranges"] = len(ranges) - _MERGE_INHERITANCE_LIMIT
    return result


def _table_delivery_payload(table_payload: Mapping[str, Any],
                            state: Mapping[str, Any],
                            limit: int = TABLE_MESSAGE_LIMIT,
                            grid_limit: int = TABLE_GRID_DELIVERY_LIMIT) -> Dict[str, Any]:
    """Build the model-visible table tool result.

    The reader's payload is delivered as the model must reason over it, minus
    the parts that only repeat: per-cell default metadata, and the active
    evidence window, which is named once as the six-field citation allowlist
    instead of being echoed again as the controller's excerpts.
    """
    from jonex_wiki.domain.engine.query.table_reader import _compact_cell_for_delivery

    delivered = dict(table_payload)
    delivered.pop("handles", None)
    request = (state.get("table_reads_by_request") or {}).get(delivered.get("request_id"))
    # One resolver for this request's physical cells, shared by the structured
    # view below and by the parallel handle rows, so the two can never disagree
    # about which [E#] names a cell.
    handle_index = _cell_handle_index(state, request)
    if request is not None:
        expected = request.get("expected") or {}
        delivered["request_coverage"] = {
            "scope": "all_pages_of_this_request",
            "status": request["status"],
            "blocker_reason": request.get("blocker_reason"),
            "received_cells": len(request.get("seen_cells") or {}),
            "expected_cells": expected.get("cell_count"),
            "received_context_items": len(request.get("seen_context") or {}),
            "expected_context_items": expected.get("context_count"),
        }
        if request.get("status") == "complete":
            from jonex_wiki.domain.engine.query.table_query import (
                build_request_structured_view,
            )
            delivered["request_view"] = build_request_structured_view(
                request,
                handle_lookup=lambda row, column: handle_index.get((int(row), int(column))))
    # ``header_refs``/``label_refs`` and ``returned_ranges`` repeat what
    # ``context_items`` (same coordinates, with ``kind``) and ``cells`` already
    # carry; the reader no longer emits them.  The pop stays defensive so a
    # stale reader payload is named rather than silently forwarded.
    omitted = [key for key in ("header_refs", "label_refs", "returned_ranges")
               if delivered.pop(key, None)]
    if omitted:
        delivered["delivery"] = {
            "omitted": omitted,
            "note": "the same coordinates are in context_items and cells",
        }
    if delivered.get("mode") == "raw":
        # A raw page already delivered its cells with their coordinates and its
        # own handle rows.  A grid view would be a second copy of the same
        # values, and the ledger-derived handle rows do not exist for a mode
        # that keeps no request.
        delivered.pop("grid", None)
    else:
        delivered["grid"] = _table_grid_view(delivered.get("cells") or [],
                                             delivered.get("context_items") or [])
    # The grid is what the model reasons over, so it must also be what the model
    # can cite: one handle per delivered cell, in the exact row-major order of
    # ``grid["rows"]``.  Without this the only handles in the message are the
    # newest ``limit`` of the active window, so a whole-table answer about the
    # first rows had nothing to cite and came back with zero citations.
    if delivered.get("mode") != "raw":
        delivered["cell_handles"] = _table_cell_handle_rows(
            state, delivered.get("grid") or {}, request)
    # The per-cell records stay in the reader payload and in the ledger (they
    # are what citation handles are built from), but the model is sent the grid:
    # the same values in ~1/19 of the bytes.  ``cells`` is not forwarded,
    # because forwarding both would double the cost of every read.
    delivered.pop("cells", None)
    if delivered.get("context_items"):
        delivered["context_items"] = [_compact_cell_for_delivery(item)
                                      for item in delivered["context_items"]]
    view = controller_payload(state["controller"])
    view["active_evidence"] = [
        {"handle": item.get("handle"),
         "eligible_for_current_hop": item.get("eligible_for_current_hop")}
        for item in view.get("active_evidence") or []
    ]
    delivered["evidence"], echo_note = _bounded_evidence_echo(state, view)
    delivered["controller"] = view
    if echo_note:
        delivered.setdefault("delivery", {}).update(echo_note)

    evidence = delivered["evidence"]
    grid = delivered.get("grid") or {}
    if grid.get("rows") and _recompose_encoded_size(grid) > grid_limit:
        # The grid is bulk, so it is what shrinks - but never silently: the
        # omitted rows are counted and the note says the read itself completed.
        kept_rows = list(grid["rows"])
        omitted_rows = 0
        while kept_rows and _recompose_encoded_size(grid) > grid_limit:
            kept_rows.pop()
            omitted_rows += 1
            grid["rows"] = kept_rows
            grid["rows_returned"] = len(kept_rows)
        if omitted_rows:
            # The handles are per row and positional, so they are trimmed by the
            # same count - a stale handle row would name the wrong cells.
            handle_block = delivered.get("cell_handles")
            if isinstance(handle_block, dict) and handle_block.get("handles"):
                handle_block["handles"] = handle_block["handles"][:len(kept_rows)]
                handle_block["rows_omitted"] = omitted_rows
            grid["rows_omitted"] = omitted_rows
            grid["note"] = ("the read is complete, but this listing was trimmed to fit one "
                            "message; use table_compute over the completed range instead of "
                            "reading the remaining rows again")
    if _recompose_encoded_size(delivered) > limit and evidence:
        delivered.setdefault("delivery", {})
        kept = list(evidence)
        while kept and _recompose_encoded_size(delivered) > limit:
            kept.pop()
            delivered["evidence"] = kept
            view["active_evidence"] = view["active_evidence"][:len(kept)]
            delivered["delivery"]["evidence_omitted"] = (
                delivered["delivery"].get("evidence_omitted", 0) + 1)
            delivered["delivery"]["note"] = (
                "handles are numbered in read order over the delivered ledger; "
                "the omitted handles keep their number and stay citable")
        if not kept:
            delivered["evidence"] = []
    # R0.1: with the handle now bound inside each structured cell, the grid's
    # values and the parallel ``cell_handles`` rows only repeat what those cells
    # already say - and reading them against each other is exactly the alignment
    # work that produced wrong-handle citations.  The co-located view replaces
    # them only when it lists exactly the same cells and the message still fits;
    # a partial read, a trimmed listing or an oversized view keeps the previous
    # delivery unchanged (the ledger, the handle numbers and the raw reader
    # payload are untouched either way).
    _collapse_table_cell_arrays(delivered, limit, grid_limit)
    continuation = _table_continuation_hint(delivered)
    if continuation is not None:
        delivered["continuation"] = continuation
    compute_ready = _table_compute_hint(delivered)
    if compute_ready is not None:
        delivered["compute_ready"] = compute_ready
    return delivered


def _collapse_table_cell_arrays(delivered: Dict[str, Any], limit: int,
                                grid_limit: int) -> bool:
    """Replace grid + parallel handles with the co-located structured cells.

    Returns ``True`` when the swap happened.  The delivered message then carries
    one cell object per cell - value, physical coordinate, column and row
    semantics, merge anchor and its own ``[E#]`` handle - and says so in
    ``delivery.projection``; nothing else about the message changes.
    """
    request_view = delivered.get("request_view")
    grid = delivered.get("grid") or {}
    if not _request_view_covers_grid(request_view, grid):
        return False
    if _recompose_encoded_size(request_view) > grid_limit:
        return False
    cells = sum(len(row.get("cells") or []) for row in request_view.get("rows") or [])
    bound = sum(1 for row in request_view.get("rows") or []
                for cell in row.get("cells") or [] if cell.get("handle"))
    candidate = {key: value for key, value in delivered.items()
                 if key not in ("grid", "cell_handles")}
    projection = {
        "shape": "cells_with_handles",
        "cells": cells,
        "cells_with_handle": bound,
        "note": ("each cell states its value, its saved coordinate, its column and row "
                 "label, its merge anchor and the [E#] handle already delivered for it; "
                 "a null handle means no evidence was bound to that cell and must not be "
                 "guessed; blank means the saved cell is empty or inherits the merged cell "
                 "above it"),
    }
    previous = dict(delivered.get("delivery") or {})
    previous.pop("omitted", None)
    candidate["delivery"] = {**previous, "projection": projection}
    if _recompose_encoded_size(candidate) > limit:
        return False
    delivered.clear()
    delivered.update(candidate)
    return True


def _table_compute_hint(delivered: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Name the exact computation a completed read makes possible.

    A complete grid is comfortable to read, so a model tends to answer a
    cross-row question by eye - and then reports neighbouring product names as
    if they were one product listed in two exchanges.  The tool that settles it
    is one call away, so the completed read says so, in the same message the
    model is already reading.
    """
    coverage = delivered.get("request_coverage") or {}
    grid_rows = (delivered.get("grid") or {}).get("rows")
    # A collapsed delivery carries the same completed cells as the structured
    # view instead of a grid; the computation is exactly as available.
    view_rows = (delivered.get("request_view") or {}).get("rows")
    if coverage.get("status") != "complete" or not (grid_rows or view_rows):
        return None
    return {
        "table_id": delivered.get("table_id"),
        "request_id": delivered.get("request_id"),
        "instruction": (
            "This read is complete, so table_compute over it is exact and "
            "authoritative. For 'which X appear under more than one Y' or 'compare Y "
            "across X', call table_compute with operation=\"duplicate_groups\", group_by "
            "set to the repeated key column and value_column to the compared column, and "
            "answer from its result. For a count or a per-value grouping use count / "
            "group_count. A cross-row comparison answered by eye is not accepted."),
    }


def _cell_handle_index(state: Mapping[str, Any],
                       request: Optional[Mapping[str, Any]]) -> Dict[tuple, Optional[str]]:
    """Resolve every frozen cell of one request to the handle the run delivered.

    Handles are numbered over the append-only ``seen_evidence_order``, which is
    why a cell read many turns ago is still citable by name.  A cell whose
    evidence never entered the ledger maps to ``None``: an unbound cell is
    stated, never guessed from a neighbour.  This is the single resolver the
    structured request view and the parallel handle rows both read, so the value
    a model sees and the handle it may cite are decided in one place.
    """
    if request is None:
        return {}
    cell_evidence = request.get("cell_evidence") or {}
    if not cell_evidence:
        return {}
    query_state = state.get("query_state")
    if query_state is None:
        return {}
    handle_by_claim: Dict[str, str] = {}
    for evidence_id in query_state.seen_evidence_order:
        record = query_state.seen_evidence_by_id.get(evidence_id)
        if record is None:
            continue
        claim_id = str(getattr(record, "claim_id", "") or "")
        if claim_id and claim_id not in handle_by_claim:
            handle_by_claim[claim_id] = query_state.evidence_handle(evidence_id)
    index: Dict[tuple, Optional[str]] = {}
    for key, claim_ids in cell_evidence.items():
        parts = str(key).split(":")
        if len(parts) != 2:
            continue
        try:
            coordinate = (int(parts[0]), int(parts[1]))
        except ValueError:
            continue
        index[coordinate] = next((handle_by_claim[claim_id] for claim_id in claim_ids
                                  if claim_id in handle_by_claim), None)
    return index


def _table_cell_handle_rows(state: Mapping[str, Any], grid: Mapping[str, Any],
                            request: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """One citation handle per delivered grid cell, in grid order.

    ``grid["rows"]`` is ``[row_number, value...]`` in ``column_indexes`` order,
    so the handles are returned as the same nested shape.  A cell whose handle
    cannot be resolved keeps ``None`` instead of shifting the row, because a
    silent shift would make every later handle name the wrong cell.
    """
    rows = grid.get("rows") or []
    columns = list(grid.get("column_indexes") or [])
    if not rows or not columns or request is None:
        return None
    index = _cell_handle_index(state, request)
    if not index:
        return None
    resolved = 0
    handle_rows: List[List[Optional[str]]] = []
    for row in rows:
        row_number = int(row[0])
        handles: List[Optional[str]] = []
        for column in columns:
            handle = index.get((row_number, int(column)))
            resolved += 1 if handle else 0
            handles.append(handle)
        handle_rows.append(handles)
    if not resolved:
        return None
    return {
        "order": "row-major over grid.rows and grid.column_indexes",
        "handles": handle_rows,
    }


def _request_view_covers_grid(view: Any, grid: Mapping[str, Any]) -> bool:
    """Whether the structured view lists exactly the cells the grid lists.

    Only then may the two parallel arrays (``grid`` values, ``cell_handles``
    rows) be replaced by the co-located view: dropping a representation that
    covers *more* or *fewer* cells than the other would silently change what the
    model can read or cite.  Trimming already recorded in the grid (``rows_omitted``)
    makes the shapes differ, so a trimmed listing keeps the old delivery.
    """
    if not isinstance(view, dict) or view.get("status") != "ready":
        return False
    if not view.get("view_complete") or int(view.get("rows_omitted") or 0):
        return False
    view_columns = [int(column.get("col", -1)) for column in view.get("columns") or []]
    grid_columns = [int(column) for column in grid.get("column_indexes") or []]
    if not view_columns or view_columns != grid_columns:
        return False
    view_rows = [(int(row.get("row", -1)), len(row.get("cells") or []))
                 for row in view.get("rows") or []]
    grid_rows = [(int(row[0]), max(0, len(row) - 1)) for row in grid.get("rows") or []]
    return bool(grid_rows) and view_rows == grid_rows


def _consume_tool_action(state: Dict[str, Any], trace: List[TraceEntry], *,
                         tool: str, origin: str) -> Optional[str]:
    """Admit one tool action against time and tool-call budgets.

    Returns ``None`` when the action is admitted (and ``state["total"]`` is
    incremented exactly once) or a stable reason string when it is refused.
    ``origin`` is an observation source only and is never handed to the model.
    """
    if origin not in ("model", "forced", "table_page"):
        raise ValueError("TOOL_ACTION_ORIGIN_INVALID")
    deadline = state.get("query_deadline_mono")
    if deadline is not None and time.monotonic() > deadline:
        return "query_time_limit"
    if state.get("total", 0) >= state.get("max_tool_calls", 0):
        return "tool_limit"
    state["total"] = state.get("total", 0) + 1
    trace.append(TraceEntry(event="tool_action_admitted", detail={
        "action_index": state["total"], "tool": tool, "origin": origin}))
    return None


# ──────────────────────────────────────────────────────────────
#  table_compute (L5) + per-request table read ledger (L4)
# ──────────────────────────────────────────────────────────────

# Metadata that carries no information when it holds its default: repeating it
# on every cell of every page is what made a two-read q003 step 124 kB.
CELL_DEFAULT_KEYS = ("formula", "cached_value", "empty_kind", "number_format",
                     "date_candidate", "unresolved_formula", "normalized_value")
CELL_DEFAULT_VALUES = {"number_format": "General", "date_candidate": 0}


def _compact_cell(cell: Mapping[str, Any]) -> Dict[str, Any]:
    """Drop default metadata; keep every key that says something.

    An explicit blank (``empty_kind``), a formula with its cached value and a
    normalized date are all meaningful and stay.  ``sheet_id`` is constant for
    a table and its coordinate already names the row and column.
    """
    compact = {key: value for key, value in cell.items() if key != "sheet_id"}
    for key in CELL_DEFAULT_KEYS:
        if key in compact and compact[key] == CELL_DEFAULT_VALUES.get(key, None):
            del compact[key]
    return compact


def _encoded_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _render_table_compute(plan: Any, result: Mapping[str, Any]) -> str:
    """Deterministic render of an executed plan; mirrors the finalization renderer."""
    value = result["result"]
    if plan.operation == "count":
        return "共{}项。".format(value)
    if plan.operation == "group_count":
        return "；".join("{}：{}项".format(key if key != "" else "空值", count)
                         for key, count in value.items()) + "。"
    if plan.operation == "duplicate_groups":
        groups = value.get("groups") or []
        if not groups:
            return "没有任何{}取到{}个及以上不同的{}（共{}个取值）。".format(
                value.get("group_by") or "分组列", value.get("min_distinct", 2),
                value.get("value_column") or "对比列", value.get("group_count", 0))
        parts = []
        for item in groups:
            witnesses = "、".join(
                "{}（第{}行）".format(cell if cell != "" else "空值",
                                    "、".join(str(row) for row in rows))
                for cell, rows in (item.get("values") or {}).items())
            parts.append("{}：{}".format(item.get("key") if item.get("key") != "" else "空值",
                                        witnesses))
        return "共{}个{}出现了{}个及以上不同的{}：{}。".format(
            len(groups), value.get("group_by") or "分组", value.get("min_distinct", 2),
            value.get("value_column") or "取值", "；".join(parts))
    if plan.operation == "select":
        columns = list(plan.return_columns or [])
        if len(columns) == 1:
            items = [row["values"].get(columns[0], "") for row in result["matched_rows"]]
            return "共{}项：{}。".format(len(items), "、".join(
                "空" if item == "" else str(item) for item in items))
        details = "；".join("、".join("{}={}".format(key, value if value != "" else "空")
                                       for key, value in row["values"].items())
                            for row in result["matched_rows"])
        return "共{}项：{}。".format(len(value), details)
    if plan.operation in ("lookup_cell", "find_dates"):
        return "；".join(item if item != "" else "空" for item in value) + "。"
    if plan.operation == "count_columns":
        columns = value.get("columns") or []
        detail = "；".join("{}：{}处".format(item["column"], item["witness_count"])
                           for item in columns)
        return "共{}个候选列满足条件（{}）。".format(value.get("count"), detail)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _table_compute_citation_handles(
    state: Dict[str, Any],
    *,
    entry: Dict[str, Any],
) -> List[str]:
    """Return the stable [E#] handles of the cells this computation used."""
    query_state = state["query_state"]
    cell_evidence = entry.get("cell_evidence") or {}
    covered_claim_ids = {
        claim_id
        for claim_ids in cell_evidence.values()
        for claim_id in claim_ids
    }
    handle_by_evidence_id = {
        evidence_id: "[E{}]".format(index + 1)
        for index, evidence_id in enumerate(query_state.seen_evidence_order)
    }
    handles: List[str] = []
    for evidence_id in query_state.seen_evidence_order:
        record = query_state.seen_evidence_by_id[evidence_id]
        if record.claim_id in covered_claim_ids:
            handles.append(handle_by_evidence_id[evidence_id])
    return handles


def _table_compute_selectable_ranges(
    eligible: Sequence[tuple[str, Mapping[str, Any]]],
) -> List[Dict[str, Any]]:
    """Describe each completed read a model may select when ranges cannot merge."""
    ranges = []
    for request_id, entry in eligible:
        scope = entry.get("requested_scope") or {}
        ranges.append({
            "request_id": request_id,
            "row_start": scope.get("row_start"),
            "row_end": scope.get("row_end"),
            "columns": [str(column.get("label")) for column in (entry.get("columns") or [])],
            "constraints": dict(scope.get("constraints") or {}),
            "received_cells": len(entry.get("seen_cells") or {}),
        })
    return ranges


def _record_table_compute_outcome(state: Dict[str, Any], args: Mapping[str, Any],
                                  result: Mapping[str, Any]) -> None:
    """Remember what a computation reported, for the answer path (S3.3).

    A completed computation becomes a program result the final answer may cite;
    a refused calculation becomes a named gap, so the answer is asked for what is
    missing (an operand, a unit, an exchange rate) instead of being allowed to
    state a number nobody could compute.
    """
    if result.get("status") == "complete":
        if isinstance(result, dict):
            result["computation_id"] = "C" + str(len(state.get("table_compute_results") or []) + 1)
        state.setdefault("table_compute_results", []).append({
            "arguments": dict(args), "payload": dict(result)})
        return
    if result.get("operation") == "calculate":
        state.setdefault("table_calculate_failures", []).append({
            "reason": result.get("reason"),
            "failed_step_id": result.get("failed_step_id")})


def _table_calculate_payload(state: Dict[str, Any],
                             args: Mapping[str, Any]) -> Dict[str, Any]:
    """The independent calculation branch: strict plan, operands from evidence."""
    from jonex_wiki.domain.engine.query.numeric_calculate import (
        execute_calculate,
        parse_plan,
    )

    plan, reason = parse_plan(args.get("calculate"))
    if plan is None:
        return {"status": "rejected", "correctable": True, "operation": "calculate",
                "reason": "TABLE_CALCULATE_PLAN_INVALID:" + str(reason),
                "allowed_schema": _query_tool_parameters("table_compute")}
    try:
        payload = execute_calculate(plan, state=state,
                                    question=str(state.get("original_question") or ""))
    except (ValueError, TypeError) as error:
        return {"status": "rejected", "correctable": True, "operation": "calculate",
                "reason": "TABLE_CALCULATE_PLAN_INVALID:" + str(error).splitlines()[0]}
    payload["table_id"] = None
    return payload


def _table_compute_payload(state: Dict[str, Any], args: Mapping[str, Any]) -> Dict[str, Any]:
    """Deterministic aggregation over one selected read range of one table.

    Computes in code over the union of the completed ranges of one table, using
    each request's own frozen projection (``cell_evidence`` carries the
    table-qualified claim id per cell), so no model recounts rows.  When the
    completed ranges cannot merge into one grid the tool answers
    ``range_selection_required`` and lists the request ids a caller may select.
    """
    table_id = str(args.get("table_id") or "")
    operation = args.get("operation")
    if operation == "calculate":
        return _table_calculate_payload(state, args)
    selected_request_id = str(args.get("request_id") or "").strip() or None
    ledger = state.get("table_reads_by_request") or {}
    eligible = [
        (request_id, entry) for request_id, entry in ledger.items()
        if entry.get("status") == "complete" and str(entry.get("table_id") or "") == table_id
    ]
    if not eligible:
        return {"status": "no_completed_reads", "correctable": True,
                "reason": "TABLE_COMPUTE_NO_COMPLETED_READS", "table_id": table_id}
    if selected_request_id is not None:
        if selected_request_id not in {request_id for request_id, _ in eligible}:
            return {"status": "rejected", "correctable": True,
                    "reason": "TABLE_COMPUTE_REQUEST_NOT_FOUND", "table_id": table_id,
                    "available_request_ids": [request_id for request_id, _ in eligible]}

    try:
        from jonex_wiki.domain.engine.query.table_query import (
            CellMatch,
            TableFilter,
            TableQueryPlan,
            build_completed_request_catalog,
            execute_table_plan,
            merge_completed_request_entries,
        )
    except ImportError:
        return {
            "status": "capability_not_available",
            "correctable": False,
            "reason": "TABLE_COMPUTE_CATALOG_NOT_AVAILABLE",
            "table_id": table_id,
            "available_request_ids": [request_id for request_id, _ in eligible],
        }

    if selected_request_id is not None:
        request_id, catalog_entry = next(
            (rid, entry) for rid, entry in eligible if rid == selected_request_id)
        built = build_completed_request_catalog(catalog_entry, request_id=request_id)
        if built["status"] != "ready":
            return {"status": "blocked", "correctable": False,
                    "reason": "TABLE_COMPUTE_CATALOG_NOT_READY", "table_id": table_id}
        catalogs = built["catalog"]
    else:
        groups: Dict[tuple, list] = {}
        for request_id, entry in eligible:
            key = (str(entry.get("source_version") or ""), table_id,
                   str(entry.get("interpretation_id") or ""))
            groups.setdefault(key, []).append((request_id, entry))
        merged_eligible: list = []
        for group in groups.values():
            if len(group) > 1:
                merged = merge_completed_request_entries(
                    [entry for _, entry in group], [request_id for request_id, _ in group])
                if merged is not None:
                    merged_eligible.append((merged["request_id"], merged))
                    continue
            merged_eligible.extend(group)

        catalogs: List[Dict[str, Any]] = []
        catalog_entries: List[Dict[str, Any]] = []
        unavailable_ranges = []
        for request_id, entry in merged_eligible:
            built = build_completed_request_catalog(entry, request_id=request_id)
            if built["status"] == "ready":
                catalogs.extend(built["catalog"])
                catalog_entries.append(entry)
            else:
                unavailable_ranges.append({"request_id": request_id,
                                           "reason": built.get("reason")})
        if unavailable_ranges:
            return {"status": "range_selection_required", "correctable": True,
                    "reason": "TABLE_COMPUTE_RANGE_UNAVAILABLE", "table_id": table_id,
                    "ranges": _table_compute_selectable_ranges(eligible),
                    "unavailable_ranges": unavailable_ranges}
        if len(catalogs) > 1:
            return {"status": "range_selection_required", "correctable": True,
                    "reason": "TABLE_COMPUTE_RANGE_SELECTION_REQUIRED",
                    "table_id": table_id,
                    "ranges": _table_compute_selectable_ranges(eligible),
                    "note": ("The completed table_read ranges for this table_id cannot "
                             "be merged into one grid (different columns or conflicting "
                             "constraints). Re-call table_compute with ``request_id`` set "
                             "to exactly one of the ranges below; the tool will not "
                             "silently compute over only the first range.")}
        if not catalogs:
            return {"status": "blocked", "correctable": False,
                    "reason": "TABLE_COMPUTE_CATALOG_NOT_READY", "table_id": table_id}
        catalog_entry = catalog_entries[0]

    table = catalogs[0]
    try:
        plan = TableQueryPlan(
            table=table["handle"],
            operation=operation,
            filters=[TableFilter(**item) for item in (args.get("filters") or [])],
            return_columns=[str(item) for item in (args.get("return_columns") or [])],
            group_by=args.get("group_by"),
            value_column=args.get("value_column"),
            min_distinct=int(args.get("min_distinct") or 2),
            target_column=args.get("target_column"),
            cell_match=CellMatch(**args["cell_match"]) if args.get("cell_match") else None,
        )
    except (ValueError, ValidationError) as exc:
        return {"status": "rejected", "correctable": True,
                "reason": "TABLE_COMPUTE_PLAN_INVALID:" + str(exc).splitlines()[0],
                "table_id": table_id}

    try:
        result = execute_table_plan([table], plan, question="")
    except (ValueError, ValidationError) as exc:
        return {"status": "blocked", "correctable": False,
                "reason": "TABLE_COMPUTE_EXECUTION:" + str(exc).splitlines()[0],
                "table_id": table_id}

    return {
        "status": "complete",
        "table_id": table_id,
        "operation": operation,
        "matched_count": result["matched_count"],
        "result": result["result"],
        "rendered": _render_table_compute(plan, result),
        "coverage": {
            "included_fragments": result["coverage"]["included_fragments"],
            "source_row_count": result["coverage"]["source_row_count"],
            "scope_kind": (table.get("range_proof") or {}).get("scope_kind"),
        },
        "citations": _table_compute_citation_handles(state, entry=catalog_entry),
        "note": "Deterministic computation over the selected completed table_read "
                "range for this table_id. Cite the involved cells with the [E*] handles "
                "listed in ``citations`` (the backend restores each to its full source "
                "and location).",
    }


def _freeze_table_request_projection(entry: Dict[str, Any], reader: Any,
                                     state: Dict[str, Any],
                                     result: Mapping[str, Any]) -> None:
    """Freeze the immutable source projection of a completed table request.

    The projection is derived only from cells the Reader actually returned and
    handles the controller actually created; it never queries a database again
    and never trusts a model-supplied column or value.
    """
    seen = entry.get("seen_cells") or {}
    handles = entry.get("candidate_handles") or {}
    refs = state.get("table_evidence_refs") or {}

    def own_handles(row: int, column: int) -> list[str]:
        found = []
        for claim_id, ref in refs.items():
            if claim_id not in handles:
                continue
            if int(ref.get("row", -1)) == row and int(ref.get("column", -1)) == column:
                found.append(claim_id)
        return found

    labels_by_column: Dict[int, str] = {}
    if entry.get("table_id") and reader is not None and hasattr(reader, "inspect"):
        try:
            inspection = reader.inspect(entry["table_id"])
            labels_by_column = {int(field["column_index"]): str(field["label"])
                                for field in inspection.get("fields") or []}
        except (KeyError, OSError, ValueError, TypeError):
            labels_by_column = {}
    if not labels_by_column:
        # Test page doubles do not expose ``inspect``: derive the deepest
        # non-empty header per column from the page's own header references.
        deepest: Dict[int, tuple[int, str]] = {}
        for item in result.get("context_items") or []:
            if item.get("kind") != "header":
                continue
            label = str(item.get("raw_value") or item.get("cached_value") or "").strip()
            if not label:
                continue
            column = int(item.get("column_index", -1))
            row = int(item.get("row_index", 0))
            if column < 0 or (column in deepest and deepest[column][0] > row):
                continue
            deepest[column] = (row, label)
        labels_by_column = {column: label for column, (_, label) in deepest.items()}

    columns = []
    data_rows = []
    cell_evidence: Dict[str, list[str]] = {}
    row_evidence: Dict[str, list[str]] = {}
    for (row, column), cell in sorted(seen.items()):
        row, column = int(row), int(column)
        if row not in data_rows:
            data_rows.append(row)
        columns.append({"col": column, "label": labels_by_column.get(
            column, "column_{}".format(column))})
        evidence = own_handles(row, column)
        anchor = _merge_anchor_coordinate(cell.get("merge_anchor"))
        if anchor is not None:
            anchor_row, anchor_column = anchor
            for claim_id in own_handles(anchor_row, anchor_column):
                if claim_id not in evidence:
                    evidence.append(claim_id)
        cell_evidence["{}:{}".format(row, column)] = evidence
        row_evidence.setdefault(str(row), [])
        for claim_id in own_handles(row, column):
            if claim_id not in row_evidence[str(row)]:
                row_evidence[str(row)].append(claim_id)

    # A column may appear several times across rows; keep one entry per column
    # in ascending column order.
    unique_columns = []
    seen_columns = set()
    for column in columns:
        if column["col"] in seen_columns:
            continue
        seen_columns.add(column["col"])
        unique_columns.append(column)

    frozen = {
        "columns": unique_columns,
        "data_rows": data_rows,
        "row_evidence": row_evidence,
        "cell_evidence": cell_evidence,
    }
    for key, value in frozen.items():
        existing = entry.get(key)
        if existing is not None and existing != value:
            raise ValueError("TABLE_REQUEST_PROJECTION_CHANGED:{}".format(key))
        entry[key] = value


def _raw_cell_handle_rows(state: Mapping[str, Any],
                          cells: Sequence[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """One [E#] handle per delivered raw cell, in the order the page lists them.

    Raw mode keeps no request ledger, so the mapping is taken from the run's own
    citation index: a raw cell is registered like any other table cell, and this
    is the same claim -> handle resolution the structured grid uses.
    """
    query_state = state.get("query_state")
    refs = state.get("table_evidence_refs") or {}
    if query_state is None or not refs or not cells:
        return None
    by_claim: Dict[str, str] = {}
    for evidence_id in query_state.seen_evidence_order:
        record = query_state.seen_evidence_by_id.get(evidence_id)
        if record is None:
            continue
        claim_id = str(getattr(record, "claim_id", "") or "")
        if claim_id and claim_id not in by_claim:
            by_claim[claim_id] = query_state.evidence_handle(evidence_id)
    by_position: Dict[tuple, str] = {}
    for claim_id, ref in refs.items():
        if ref.get("kind") != "table_cell" or ref.get("row") is None:
            continue
        by_position.setdefault((int(ref["row"]), int(ref["column"])), claim_id)
    handles: List[Optional[str]] = []
    resolved = 0
    for cell in cells:
        claim_id = by_position.get((int(cell["row_index"]), int(cell["column_index"])))
        handle = by_claim.get(claim_id) if claim_id else None
        resolved += 1 if handle else 0
        handles.append(handle)
    if not resolved:
        return None
    return {"order": "row-major over raw_delivery order", "handles": handles}


# A raw page is bounded like a structured page: the same per-cell payload, the
# same 32 KiB response budget, and its own delivery ceiling so a raw read can
# never become the way to bypass the delivery budget.  The page size is a
# delivery decision (the cursor carries the next cell index), so a response that
# would not fit is re-read with a smaller window instead of being trimmed - a
# trimmed page would leave cells the cursor no longer points at.
RAW_PAGE_CELL_LADDER = (400, 200, 100, 50, 20, 8, 3, 1)


def _raw_delivery_payload(result: Dict[str, Any]) -> Dict[str, Any]:
    """Build the model-visible raw result: content, coordinates, context.

    The registered handle list is dropped (the run keeps it; the model is sent
    the [E#] handles below), and the cells are compacted the same way a
    structured page compacts its own, so a raw page costs about what a
    structured page costs.
    """
    from jonex_wiki.domain.engine.query.table_reader import _compact_cell_for_delivery

    delivered = dict(result)
    delivered.pop("handles", None)
    delivered.pop("context_items", None)
    delivered["cells"] = [_compact_cell_for_delivery(cell)
                          for cell in delivered.get("cells") or []]
    return delivered


def _table_read_raw(args: Mapping[str, Any], *, table_id: str, info: Mapping[str, Any],
                    reader: Any, state: Dict[str, Any],
                    trace: List[TraceEntry]) -> Dict[str, Any]:
    """Deliver explicit raw coordinates as content evidence.

    Raw mode is this same tool, not a second table reader: it resolves no field
    name and aggregates nothing.  It registers the delivered cells (and the
    header cells shown as their context) as citable handles, and it creates no
    completed-request ledger entry - so a raw page can never stand in for the
    structured range that ``table_compute`` requires before it aggregates.
    """
    from jonex_wiki.domain.engine.query.table_reader import READ_RESPONSE_LIMIT

    revision_id = getattr(reader, "revision_id", None)
    scope = args["raw_scope"]
    cursor = args.get("cursor")
    result: Dict[str, Any] = {}
    delivered: Dict[str, Any] = {}
    shrunk_to: Optional[int] = None
    for budget in RAW_PAGE_CELL_LADDER:
        result = dict(reader.read_raw(scope, cursor=cursor, max_cells=budget))
        delivered = _raw_delivery_payload(result)
        # A table with no grid, or a rectangle outside it, has nothing to page:
        # its own refusal is the answer, and re-reading cannot change it.
        if result.get("ambiguities"):
            break
        if _recompose_encoded_size(delivered) <= READ_RESPONSE_LIMIT:
            if budget != RAW_PAGE_CELL_LADDER[0]:
                shrunk_to = budget
            break
    else:  # pragma: no cover - the ladder ends at one cell, which always fits
        shrunk_to = RAW_PAGE_CELL_LADDER[-1]

    interpretation_id = result.get("interpretation_id")
    context_items: List[Dict[str, Any]] = []
    for item in result.get("header_context") or []:
        for cell in item.get("header_cells") or []:
            context_items.append({
                **cell, "id": "cell:{}:{}:{}".format(result.get("sheet_id"),
                                                     cell["row_index"], cell["column_index"]),
                "kind": "header", "sheet_id": result.get("sheet_id")})
    context_pairs = [[int(item["row_index"]), int(item["column_index"])] for item in context_items]
    # The reader's page is built before the delivery shrinks it, so the handles
    # are taken from the delivered cells themselves: a handle must name a cell
    # the model was actually shown.
    cells_by_position = {(int(cell["row_index"]), int(cell["column_index"])): cell
                         for cell in delivered.get("cells") or []}
    shown = [cells_by_position[key] for key in sorted(cells_by_position)]
    handles = _cell_handles(shown, table_id, info, state, context_cells=context_pairs,
                            revision_id=revision_id, interpretation_id=interpretation_id)
    handles += _cell_handles(context_items, table_id, info, state, context_cells=[],
                             revision_id=revision_id, interpretation_id=interpretation_id)
    handles += _note_handles(delivered, table_id, info, state, revision_id=revision_id,
                             interpretation_id=interpretation_id)
    if handles:
        _register_table_handles(state, table_id, handles, trace)
    delivered["cell_handles"] = _raw_cell_handle_rows(state, delivered.get("cells") or [])
    delivered["delivery"] = {
        "mode": "raw",
        "registered_evidence": len(handles),
        "completed_request": None,
        "page_cell_budget": shrunk_to or RAW_PAGE_CELL_LADDER[0],
        "note": ("raw cells are citable content evidence for this table; they are not a "
                 "completed structured read, so table_compute still needs a structured "
                 "table_read of the fields and range it aggregates"),
    }
    if shrunk_to is not None:
        delivered["delivery"]["page_shrunk_to_cells"] = shrunk_to
    trace.append(TraceEntry(event="table_raw_read", detail={
        "table_id": table_id, "mode": "raw",
        "cells": len(delivered.get("cells") or []),
        "rectangle_status": (delivered.get("coverage") or {}).get("rectangle_status"),
        "has_more": delivered.get("has_more"), "handles": len(handles)}))
    return delivered


# A structural refusal repeated with no new evidence is a loop, not a read.
# After the second one for the same table and the same cause, the run names the
# one way out - the same table's raw context - exactly once.  Nothing about the
# budget changes, no call is made on the model's behalf, and the suggestion
# never offers a different table: the question's own source restriction stands.
TABLE_STRUCTURAL_RECOVERY_ATTEMPTS = 2
TABLE_RECOVERY_CONTEXT_ROWS = 5


def _structural_recovery_suggestion(state: MutableMapping[str, Any], table_id: str,
                                    result: Mapping[str, Any], reader: Any,
                                    trace: List[TraceEntry]) -> Optional[Dict[str, Any]]:
    """Name the raw-context recovery once, after the second identical failure.

    The counter is per (table, interpretation, cause), so two different tables
    and two different failure causes are never merged into one loop, and a read
    that delivered new evidence resets it.  The suggestion is a property of the
    tool result the model is already reading: it costs no extra call and grants
    no extra budget.
    """
    interpretation_id = str(result.get("interpretation_id") or "unknown")
    key = "{}::{}::structure_ambiguous".format(table_id, interpretation_id)
    counters = state.setdefault("table_structural_failures", {})
    entry = counters.setdefault(key, {"table_id": table_id,
                                      "interpretation_id": interpretation_id,
                                      "cause": "structure_ambiguous",
                                      "attempts": 0, "attempts_without_new_evidence": 0,
                                      "suggested": False})
    entry["attempts"] += 1
    if result.get("cells"):
        entry["attempts_without_new_evidence"] = 0
    else:
        entry["attempts_without_new_evidence"] += 1
    if entry["suggested"] or entry["attempts_without_new_evidence"] < (
            TABLE_STRUCTURAL_RECOVERY_ATTEMPTS):
        return None
    try:
        inspection = reader.inspect(table_id)
    except (KeyError, sqlite3.Error, OSError):
        return None
    bounds = inspection.get("bounds") or {}
    grid = inspection.get("grid_state") or {}
    if not bounds or not grid.get("present"):
        # Nothing raw exists to read: never suggest a call that cannot work.
        return None
    row_start = int(bounds["row_start"])
    scope = {"table_id": table_id, "row_start": row_start,
             "row_end": min(int(bounds["row_end"]), row_start + TABLE_RECOVERY_CONTEXT_ROWS - 1),
             "column_start": int(bounds["column_start"]),
             "column_end": int(bounds["column_end"])}
    entry["suggested"] = True
    entry["suggested_at_attempt"] = entry["attempts"]
    suggestion = {
        "kind": "read_same_table_raw_context",
        "table_id": table_id,
        "cause": "structure_ambiguous",
        "attempts": entry["attempts"],
        "suggested_call": {"tool": "table_read", "raw_scope": scope},
        "budget_unchanged": True,
        "table_substitution_allowed": False,
        "note": ("同一张表连续两次没有新增证据：用上面的 raw_scope 读一次这张表的原始上下文"
                 "（坐标与原文），据此说明能确认什么、不能确认什么。不要换成其他表，"
                 "也不要重复同一次失败调用。"),
    }
    trace.append(TraceEntry(event="table_structural_recovery_suggested", detail={
        "table_id": table_id, "interpretation_id": interpretation_id,
        "cause": "structure_ambiguous", "attempts": entry["attempts"],
        "raw_scope": scope}))
    return suggestion


async def _table_read_with_ledger(args: Dict[str, Any], *, table_id: str,
                                  info: Mapping[str, Any],
                                  reader: Any, state: Dict[str, Any], model: Any,
                                  routing: Optional[Dict[str, Any]], trace: List[TraceEntry],
                                  spans: Any, parent_span_id: Optional[str],
                                  usage_acc: Dict[str, int], max_merged_pages: int,
                                  response_limit: int, sha256_json: Any) -> Dict[str, Any]:
    """Read one page and keep the per-request ledger of what was really read."""
    from jonex_wiki.domain.engine.query.table_reader import (
        EXHAUSTIVE_MAX_CELLS,
        EXHAUSTIVE_PAGE_BYTES,
        scope_is_exhaustive,
    )

    exhaustive = scope_is_exhaustive(args.get("scope") or {})
    result = reader.read(args["scope"], cursor=args.get("cursor"),
                         max_cells=EXHAUSTIVE_MAX_CELLS if exhaustive else 800,
                         page_bytes=EXHAUSTIVE_PAGE_BYTES if exhaustive else None)
    coverage = dict(result.get("coverage") or {})
    labels = _observation_table_labels(result)
    if labels:
        _remember_table_labels(state, table_id, labels)
    # ``structure_ambiguous`` is the reader's own marker for a table whose
    # interpretation status is not accepted.  A refusal caused by a misspelled
    # field or an illegal scope carries a different reason and is never a
    # structural ambiguity.
    if "structure_ambiguous" in [str(item) for item in result.get("ambiguities") or []]:
        _record_ambiguity_fact(state, table_id, result, trace, table_labels=labels,
                               interpretation_status="ambiguous",
                               revision_id=result.get("source_version"))
        recovery = _structural_recovery_suggestion(state, table_id, result, reader, trace)
        if recovery is not None:
            result["recovery"] = recovery
    request_id = str(result.get("request_id") or "")
    if not request_id:
        raise ValueError("TABLE_REQUEST_IDENTITY_MISSING")
    ledger = state.setdefault("table_reads_by_request", {})
    expected = {"cell_count": result.get("requested_cell_count"),
                "cell_sha256": result.get("requested_cells_sha256"),
                "context_count": result.get("required_context_count"),
                "context_sha256": result.get("required_context_sha256")}
    entry = ledger.get(request_id)
    if entry is None:
        entry = {"request_id": request_id, "expected": expected,
                 "seen_cells": {}, "seen_context": {}, "seen_pages": {},
                 "candidate_handles": {}, "relation_input_bytes": 0, "status": "collecting",
                 "blocker_reason": None, "table_id": table_id,
                 "source_id": info.get("source_id"), "document_id": info.get("document_id"),
                 "source_version": result.get("source_version"),
                 "sheet_id": result.get("sheet_id"),
                 "interpretation_id": result.get("interpretation_id"),
                 "requested_scope": result.get("requested_scope"),
                 "columns": None, "data_rows": None, "row_evidence": None,
                 "cell_evidence": None}
        ledger[request_id] = entry
    else:
        if entry.get("request_id") != request_id:
            raise ValueError("TABLE_REQUEST_IDENTITY_CHANGED:request_id")
        if expected["cell_count"] is not None and entry["expected"] != expected:
            raise ValueError("TABLE_REQUEST_EXPECTATION_CHANGED")
        for key in ("source_version", "sheet_id", "interpretation_id", "requested_scope"):
            if entry.get(key) != result.get(key):
                raise ValueError("TABLE_REQUEST_IDENTITY_CHANGED:{}".format(key))
    oversized = result.get("status") in ("oversized_record", "request_metadata_too_large")
    if not oversized:
        page_index = int(result.get("page_index") or 0)
        payload_hash = sha256_json(result.get("cells") or [])
        known = entry["seen_pages"].get(page_index)
        if known is not None and known["payload_sha256"] != payload_hash:
            raise ValueError("TABLE_PAGE_CONTENT_CHANGED")
        if known is None:
            entry["seen_pages"][page_index] = {
                "start_record_index": int(result.get("start_record_index") or 0),
                "payload_sha256": payload_hash}
            for cell in result.get("cells") or []:
                key = (int(cell.get("row_index")), int(cell.get("column_index")))
                existing = entry["seen_cells"].get(key)
                if existing is not None and existing != cell:
                    raise ValueError("TABLE_PAGE_CONTENT_CHANGED")
                entry["seen_cells"][key] = cell
            for item in result.get("context_items") or []:
                key = str(item.get("id"))
                existing = entry["seen_context"].get(key)
                if existing is not None and existing != item:
                    raise ValueError("TABLE_PAGE_CONTENT_CHANGED")
                entry["seen_context"][key] = item
        context_pairs = [[int(item.get("row_index")), int(item.get("column_index"))]
                         for item in result.get("context_items") or []
                         if item.get("kind") in ("header", "label")
                         and item.get("row_index") is not None
                         and item.get("column_index") is not None]
        for handle in _cell_handles(result.get("cells") or [], table_id, info, state,
                                    context_cells=context_pairs,
                                    revision_id=getattr(reader, "revision_id", None),
                                    interpretation_id=result.get("interpretation_id")):
            entry["candidate_handles"].setdefault(handle["claim_id"], handle)
        # READING-REPAIR §2: header/label cells are registered uniformly with
        # data cells, so a merged cell's anchor coordinate has a citable handle
        # that the table program can later resolve (previously the anchor cell
        # was never a data cell and therefore never produced a handle).
        context_cell_items = [
            {**item, "sheet_id": item.get("sheet_id") or result.get("sheet_id")}
            for item in result.get("context_items") or []
            if item.get("kind") in ("header", "label")
            and item.get("row_index") is not None
            and item.get("column_index") is not None
        ]
        for handle in _cell_handles(context_cell_items, table_id, info, state,
                                    context_cells=[],
                                    revision_id=getattr(reader, "revision_id", None),
                                    interpretation_id=result.get("interpretation_id")):
            entry["candidate_handles"].setdefault(handle["claim_id"], handle)
        for handle in _note_handles(result, table_id, info, state,
                                    revision_id=getattr(reader, "revision_id", None),
                                    interpretation_id=result.get("interpretation_id")):
            entry["candidate_handles"].setdefault(handle["claim_id"], handle)
    seen_cells = sorted([[row, column] for row, column in entry["seen_cells"]])
    seen_context = sorted(entry["seen_context"])
    cells_complete = (expected["cell_count"] is not None
                      and len(seen_cells) == expected["cell_count"]
                      and sha256_json(seen_cells) == expected["cell_sha256"])
    context_complete = (expected["context_count"] is not None
                        and len(seen_context) == expected["context_count"]
                        and sha256_json(seen_context) == expected["context_sha256"])
    if not (cells_complete and context_complete):
        entry["status"] = "collecting"
        entry["blocker_reason"] = "missing_cells" if not cells_complete else "missing_context"
        _record_table_blocker(state, table_id=table_id, request_id=request_id, coverage=coverage,
                              reasons=[entry["blocker_reason"]] + list(result.get("ambiguities") or []),
                              trace=trace)
        return result
    seen_cell_list = [entry["seen_cells"][key] for key in sorted(entry["seen_cells"])]
    # An exhaustive read converges on hundreds or thousands of cells; the
    # lossless per-cell form of that set is what used to push relation_input
    # past the judge's byte ceiling and block a read that had in fact completed.
    # The judge needs the values and their coordinates, not each cell's default
    # metadata sixteen times over, so a large set is stated row by row.
    if exhaustive or len(seen_cell_list) > RELATION_COMPACT_CELLS:
        merged_cells: Any = _relation_cells_compact(seen_cell_list)
        merged_cells_shape = ("one entry per spreadsheet row; the first element is the row "
                              "number and column_indexes names the columns")
    else:
        merged_cells = _relation_record_groups(seen_cell_list)
        merged_cells_shape = "lossless per-cell records grouped by field vector"
    merged = {
        "question": state.get("original_question", ""),
        "requested_scope": result.get("requested_scope"),
        "sheet_name": _sheet_name_for(state, info, entry.get("sheet_id") or result.get("sheet_id")),
        "cells": merged_cells,
        "cells_shape": merged_cells_shape,
        "headers": _relation_record_groups(
            [item for item in entry["seen_context"].values() if item.get("kind") == "header"]),
        "labels": _relation_record_groups(
            [item for item in entry["seen_context"].values() if item.get("kind") == "label"]),
        "footnotes": _relation_record_groups(
            [item for item in entry["seen_context"].values()
             if item.get("kind") in ("caption", "footnote", "table_note")]),
    }
    relation_input = json.dumps(merged, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    entry["relation_input_bytes"] = len(relation_input.encode("utf-8"))
    approved_workbook_scope = bool(state.get("agent_driven")) or (
        str(info.get("source_id") or "") in (state.get("delivered_workbooks") or set())
        and coverage.get("structure_status") == "confirmed")
    relation_limit = (EXHAUSTIVE_RELATION_INPUT_LIMIT if exhaustive else response_limit)
    if (not approved_workbook_scope and (
            len(entry["seen_pages"]) > max_merged_pages
            or entry["relation_input_bytes"] > relation_limit)):
        entry["status"], entry["blocker_reason"] = "blocked", "read_scope_too_large"
        coverage["relation_status"] = "insufficient"
        result["coverage"] = coverage
        result["read_scope_too_large"] = True
        _record_table_blocker(state, table_id=table_id, request_id=request_id, coverage=coverage,
                              reasons=["read_scope_too_large"], trace=trace)
        return result
    supported = coverage.get("relation_status") == "supported"
    # One evidence set, one verdict.  The signature covers the *complete*
    # relation input - question, requested scope, cells, headers, labels and
    # footnotes - plus the revision and interpretation identity, so only a
    # replay of exactly the same evidence may reuse a verdict.  An earlier
    # version dropped the row window from the scope and reused one row's
    # "supported" for another row's cells; a verdict is evidence for the cells
    # it was earned on and for nothing else.  Pages of one request are merged
    # above (cells_complete and context_complete) and judged once, so a paged
    # request still pays a single verdict for its merged set.
    relation_signature = sha256_json({
        "revision_id": getattr(reader, "revision_id", None),
        "interpretation_id": result.get("interpretation_id"),
        "relation_input_sha256": hashlib.sha256(
            relation_input.encode("utf-8")).hexdigest(),
    })
    verdicts = state.setdefault("table_relation_verdicts", {})
    known_verdict = verdicts.get(relation_signature)
    if approved_workbook_scope:
        # This selects a proved grid already delivered in the admitted workbook.
        # Data coverage is not a verdict that the user's premise must be true.
        supported = True
        coverage["relation_status"] = "supported"
        result["relation_check"] = {"status": "supported", "reason": ("saved_grid_scope" if state.get("agent_driven") else "approved_workbook_grid_scope"), "structural_only": True}
        trace.append(TraceEntry(event="table_compute_scope_verified", detail={
            "table_id": table_id, "request_id": request_id,
            "cells": len(entry["seen_cells"]), "context_items": len(entry["seen_context"])}))
    elif model is not None and known_verdict is not None:
        supported = known_verdict["status"] == "supported"
        coverage["relation_status"] = known_verdict["status"]
        result["relation_check"] = {"status": known_verdict["status"],
                                    "reason": known_verdict["reason"], "reused": True}
        trace.append(TraceEntry(event="table_relation_reused", detail={
            "table_id": table_id, "request_id": request_id,
            "status": known_verdict["status"], "pages": len(entry["seen_pages"]),
            "relation_input_bytes": entry["relation_input_bytes"]}))
    elif model is not None:
        from jonex_wiki.domain.engine.query.algorithm import RelationCheck
        relation_started = datetime.now(timezone.utc)
        relation_mono = time.monotonic()
        relation_raw = await _model_complete(
            model,
            [{"role": "system", "content": (
                "Verify that the exact cells and labels support the portion of "
                "the question this table can support. A single table request "
                "reads exactly one table, identified by its sheet_name (given "
                "when known) and requested_scope.table_id; the question may span "
                "several tables or worksheets. Judge only whether THIS table's "
                "evidence is identity-correct and internally consistent with the "
                "part of the question it addresses: return supported when this "
                "table's cells, headers, labels and footnotes verify that part "
                "even when other tables are required for the complete answer; "
                "contradicted when this table's own evidence conflicts with the "
                "part it claims to support; insufficient when this table cannot "
                "support any addressed part of the question. Each "
                "cells/headers/labels/footnotes group lists fields once; each "
                "records array gives values in that exact field order. All values "
                "are preserved. Distinguish raw_value from effective_value and "
                "use merge_anchor to interpret inherited values. Use no outside "
                "knowledge."
            )}, {"role": "user", "content": relation_input}],
            RelationCheck.model_json_schema(), max_tokens=500, tools=None,
            thinking={"type": "disabled"}, scene="jonex_wiki_query_relation",
            **(routing or {}),
        )
        relation = RelationCheck.model_validate(relation_raw)
        supported = relation.status == "supported"
        coverage["relation_status"] = relation.status
        result["relation_check"] = relation.model_dump()
        if supported:
            # Only a passing verdict is reusable; a refusal must be re-earned
            # by whatever evidence the model reads next.
            verdicts[relation_signature] = {"status": relation.status,
                                            "reason": relation.reason}
        trace.append(TraceEntry(event="table_relation_checked", detail={
            "table_id": table_id, "request_id": request_id, "status": relation.status,
            "reason": relation.reason, "pages": len(entry["seen_pages"]),
            "relation_input_bytes": entry["relation_input_bytes"]}))
        if spans is not None:
            _record_model_span(
                spans, relation_raw, (routing or {}).get("model"),
                relation_started, int((time.monotonic() - relation_mono) * 1000),
                usage_acc, title="table relation verification", parent_id=parent_span_id)
    result["coverage"] = coverage
    # The relation judge answers "does this read support the addressed part of
    # the question?".  For a partial read that is a real scope question and its
    # refusal withholds the cells.  For an exhaustive read it is not: the whole
    # table *is* the scope, and withholding it produced runs that held every
    # cell and still refused ("key evidence not obtained"), because an LLM
    # verdict - not the data - decided whether the data existed.  An exhaustive
    # read therefore delivers on a refusing verdict too, and records the verdict
    # as a caveat instead of a blocker.
    whole_table_read = bool(exhaustive and cells_complete and context_complete)
    deliverable = supported or whole_table_read
    if deliverable and not supported:
        coverage["relation_caveat"] = "relation_not_supported"
        trace.append(TraceEntry(event="table_relation_overridden", detail={
            "table_id": table_id, "request_id": request_id,
            # The verdict may be absent when no model judge ran for this read
            # (a caller that supplies no model, or a replayed verdict); the
            # override then records the status it is overriding.
            "reason": (result.get("relation_check") or {}).get("reason")
            or coverage.get("relation_status"), "scope_kind": "exhaustive",
            "cells": len(entry["seen_cells"])}))
    if deliverable:
        entry["status"], entry["blocker_reason"] = "complete", None
        _freeze_table_request_projection(entry, reader, state, result)
        _register_table_handles(state, table_id, list(entry["candidate_handles"].values()), trace)
        # A completed exhaustive read *is* the whole table: record it so the
        # same table is never read twice in one run.
        if whole_table_read:
            state.setdefault("delivered_tables", set()).add(table_id)
            coverage["delivered_whole_table"] = True
        state["table_scope_blockers"] = [item for item in state.get("table_scope_blockers", [])
                                         if item.get("request_id") != request_id]
        result["request_status"] = "complete"
        result["completed_request"] = {
            "request_id": request_id, "table_id": table_id,
            "status": "complete", "requested_scope": entry["requested_scope"],
            "row_count": len(entry["data_rows"]), "cell_count": len(entry["seen_cells"]),
            "columns": entry["columns"],
            "note": "The complete request grid is frozen for computation. cells/coverage in this response describe only the final physical page, not the entire request.",
            "next_step": "Call table_compute with this table_id and request_id for deterministic results; do not recount the final page or claim a computation was executed before calling it.",
        }
        trace.append(TraceEntry(event="table_request_completed", detail={
            "table_id": table_id, "request_id": request_id, "pages": len(entry["seen_pages"]),
            "cells": len(entry["seen_cells"]), "context_items": len(entry["seen_context"]),
            "relation_input_bytes": entry["relation_input_bytes"]}))
    else:
        entry["status"], entry["blocker_reason"] = "blocked", "relation_not_supported"
        _record_table_blocker(state, table_id=table_id, request_id=request_id, coverage=coverage,
                              reasons=["relation_not_supported"], trace=trace)
    return result


# Reader argument errors the model can fix in a later turn.  Nothing about the
# artifacts or the revision is wrong when one of these is raised.
_TABLE_ARGUMENT_ERRORS = frozenset({
    "TABLE_ROW_WINDOW_INCOMPLETE",
    "TABLE_ROW_WINDOW_INVALID",
    "TABLE_CURSOR_INVALID",
    "TABLE_CURSOR_BINDING_MISMATCH",
    "TABLE_CURSOR_OUT_OF_RANGE",
    "TABLE_LOCATE_LITERAL_EMPTY",
    "table_id must be non-blank",
})

# Keys a ``table_read`` ``scope`` object may carry.  An unknown key is a model
# protocol error, answered with a stable marker instead of a run-killing
# pydantic ValidationError.
_TABLE_READ_SCOPE_KEYS = frozenset({
    "table_id", "constraints", "target_fields", "task_type",
    "require_all_matches", "include_hidden", "row_start", "row_end",
})

# Keys a ``table_read`` ``raw_scope`` object may carry.  There is no field name,
# condition, target field or aggregation key here by design: raw mode addresses
# stored coordinates and states content, never confirmed semantics.
_TABLE_READ_RAW_KEYS = frozenset({
    "table_id", "row_start", "row_end", "column_start", "column_end", "include_hidden",
})


async def _execute_table_tool(name: str, args: Dict[str, Any], *, state: Dict[str, Any],
                              model: Any, routing: Optional[Dict[str, Any]],
                              trace: List[TraceEntry], spans: Any,
                              parent_span_id: Optional[str],
                              usage_acc: Dict[str, int]) -> Dict[str, Any]:
    """Run one table tool, with the bounded cross-page ledger and fatal errors."""
    import sqlite3

    from jonex_wiki.domain.engine.query.table_reader import (
        EXHAUSTIVE_MAX_CELLS,
        EXHAUSTIVE_PAGE_BYTES,
        EXHAUSTIVE_PAGE_LIMIT,
        MAX_MERGED_PAGES,
        READ_RESPONSE_LIMIT,
        TableReader,
        _sha256_json,
        scope_is_exhaustive,
    )

    # The calculation branch is not about one table: its operands name the
    # delivered evidence themselves, so it needs neither a workbook nor a
    # table_id from the registry.
    if name == "table_compute" and args.get("operation") == "calculate":
        result = _table_compute_payload(state, dict(args))
        _record_table_compute_outcome(state, dict(args), result)
        return result

    registry = state.get("table_registry") or {}
    bundle_view = state.get("bundle_view")
    if bundle_view is None or not registry:
        return {"status": "capability_not_available", "reason": "no_native_table_artifacts"}
    table_id = (args.get("table_id") or (args.get("scope") or {}).get("table_id")
                or (args.get("raw_scope") or {}).get("table_id"))

    def owner_context(info: Mapping[str, Any]) -> Dict[str, Any]:
        # Discovery must say whose cells these are. The registry owns identity;
        # the already-published material catalog owns the readable source name.
        from jonex_wiki.domain.engine.query.read_views import (
            _read_context, load_reading_materials,
        )
        context = {key: info[key] for key in ("source_id", "document_id") if info.get(key)}
        source_id = str(info.get("source_id") or "")
        catalog = load_reading_materials(_read_context(state), source_id) if source_id else None
        source_name = ((catalog or {}).get("source_identity") or {}).get("source_name")
        if source_name:
            context["source_name"] = source_name
        return context

    def fatal(reason: str, **detail: Any) -> Dict[str, Any]:
        payload = {"status": "table_artifact_inconsistent", "reason": reason, **detail}
        state["table_fatal_error"] = dict(payload)
        trace.append(TraceEntry(event="table_artifact_inconsistent", detail=payload))
        return payload

    try:
        if name == "table_locate" and not args.get("table_id"):
            matches = []
            partial_tables = []
            for candidate, candidate_info in sorted(registry.items()):
                located = TableReader(bundle_view, revision_id=bundle_view.revision_id,
                                      bundle_path=candidate_info["path"]).locate(
                    args["literal"], table_id=candidate,
                    field_id=args.get("field_id"), limit=50)
                if located["matches"]:
                    owner = owner_context(candidate_info)
                    matches.extend({**match, **owner} for match in located["matches"])
                if located["has_more"]:
                    partial_tables.append(candidate)
            has_more = len(matches) > 50 or bool(partial_tables)
            matches.sort(key=lambda item: (
                item.get("matched_term") != args["literal"], str(item.get("table_id") or ""),
                int(item.get("row_index", 0)), int(item.get("column_index", 0)),
                str(item.get("matched_term") or "")))
            return {"revision_id": bundle_view.revision_id, "literal": args["literal"],
                    "matches": matches[:50], "has_more": has_more,
                    "partial_table_ids": partial_tables,
                    "discovery": "table_value", "search_scope_complete": not has_more}
        info = registry.get(table_id)
        if info is None:
            return {"status": "not_found", "reason": "TABLE_ID_UNKNOWN", "table_id": table_id}
        reader = TableReader(bundle_view, revision_id=bundle_view.revision_id,
                             bundle_path=info["path"])
        if name == "table_inspect":
            inspection = reader.inspect(table_id)
            labels = _observation_table_labels(inspection)
            _remember_table_labels(state, table_id, labels)
            # Only the table's own interpretation status makes a structural
            # ambiguity; an ambiguous *refusal* (bad field, bad scope) does not.
            if inspection.get("interpretation_status") == "ambiguous":
                _record_ambiguity_fact(state, table_id, inspection, trace,
                                       table_labels=labels,
                                       interpretation_status="ambiguous",
                                       revision_id=bundle_view.revision_id)
            return {**inspection, **owner_context(info)}
        if name == "table_locate":
            located = reader.locate(args["literal"], table_id=table_id,
                                    field_id=args.get("field_id"), cursor=args.get("cursor"))
            owner = owner_context(info)
            located.update(owner)
            located["matches"] = [{**match, **owner} for match in located["matches"]]
            next_step = _locate_next_step(located, args.get("field_id"))
            if next_step:
                located["next_step"] = next_step
            labels: List[str] = []
            field_valid = True
            field_label: Optional[str] = None
            if (not located.get("matches") and located.get("search_scope_complete")
                    and not args.get("cursor")):
                # A zero match may only become a fact after the identity and the
                # field map have been read; no cell is touched here.
                inspection = reader.inspect(table_id)
                labels = _observation_table_labels(inspection)
                _remember_table_labels(state, table_id, labels)
                if args.get("field_id"):
                    fields = {str(field.get("field_id")): field
                              for field in inspection.get("fields") or []}
                    entry = fields.get(str(args["field_id"]))
                    field_valid = entry is not None
                    field_label = None if entry is None else entry.get("label")
            _record_locate_miss_fact(state, table_id, args["literal"],
                                     args.get("field_id"), located, trace,
                                     table_labels=labels, field_valid=field_valid,
                                     field_label=field_label, cursor=args.get("cursor"),
                                     revision_id=bundle_view.revision_id)
            return located
        if args.get("raw_scope"):
            return _table_read_raw(args, table_id=table_id, info=info, reader=reader,
                                   state=state, trace=trace)
        return await _table_read_with_ledger(
            args, table_id=table_id, info=info, reader=reader, state=state, model=model,
            routing=routing, trace=trace, spans=spans, parent_span_id=parent_span_id,
            usage_acc=usage_acc,
            # An exhaustive scope ("the whole table") is the one request shape
            # whose whole point is to see every requested cell, so it gets the
            # raised page/cell budget; a scoped read keeps the original bounds.
            max_merged_pages=(EXHAUSTIVE_PAGE_LIMIT
                              if scope_is_exhaustive(args.get("scope") or {})
                              else MAX_MERGED_PAGES),
            response_limit=READ_RESPONSE_LIMIT, sha256_json=_sha256_json)
    except KeyError as exc:
        if "TABLE_ID_UNKNOWN" in str(exc):
            return fatal("TABLE_ARTIFACT_MISSING", table_id=table_id, detail=str(exc))
        raise
    except sqlite3.Error as exc:
        return fatal("TABLE_ARTIFACT_UNREADABLE", table_id=table_id,
                     detail="{}: {}".format(type(exc).__name__, exc))
    except OSError as exc:
        return fatal("TABLE_ARTIFACT_UNREADABLE", table_id=table_id, detail=str(exc))
    except ValueError as exc:
        # A bad argument is the model's to correct: reusing a cursor with a
        # changed scope, an impossible row window or an out-of-range page is a
        # protocol mistake, not a broken run.  It is answered as a rejected tool
        # result so the model can repair its call; integrity failures (changed
        # page content, changed request expectations) stay fatal because they
        # mean the evidence itself moved underneath the ledger.
        reason = str(exc).split(":", 1)[0].strip()
        if reason not in _TABLE_ARGUMENT_ERRORS:
            raise
        payload = {"table_id": table_id, "status": "rejected", "reason": reason,
                   "correctable": True}
        trace.append(TraceEntry(event="table_argument_rejected", detail=payload))
        return payload


async def _read_table_request(
    args: Dict[str, Any], *, state: Dict[str, Any], model: Any,
    routing: Optional[Dict[str, Any]], trace: List[TraceEntry], spans: Any,
    usage_acc: Dict[str, int], parent_span_id: Optional[str],
    origin: str = "model",
) -> Dict[str, Any]:
    """Read every page of one same-scope request, bounded by the tool budget.

    The first physical page was already admitted by ``_exec_tool``; only each
    additional page is admitted here with ``origin='table_page'``.  It calls the
    single-page ``_execute_table_tool`` directly and never re-enters
    ``_exec_tool``, so no model ``call_id`` is fabricated and the logical read
    counter is not incremented per physical page.
    """
    from jonex_wiki.domain.engine.query.table_reader import (
        MAX_MERGED_PAGES,
        _decode_cursor,
    )

    if args.get("raw_scope"):
        # A raw call pages on demand, one page per call: it has no
        # same-scope request to continue, so the runner must not treat its
        # cursor as a structured request page and must not require
        # require_all_matches to see the whole rectangle.
        return await _execute_table_tool(
            "table_read", args, state=state, model=model, routing=routing,
            trace=trace, spans=spans, parent_span_id=parent_span_id, usage_acc=usage_acc)

    scope = dict(args["scope"])
    info = (state.get("table_registry") or {}).get(str(scope.get("table_id"))) or {}
    selecting_delivered_workbook = str(info.get("source_id") or "") in (
        state.get("delivered_workbooks") or set())
    # Existing per-action/deadline admission still applies to every page.
    # The four-page relation-judge limit cannot truncate a grid whose complete
    # original workbook has already passed delivery admission.
    page_limit = (max(MAX_MERGED_PAGES, int(state.get("max_tool_calls") or MAX_MERGED_PAGES))
                  if selecting_delivered_workbook else MAX_MERGED_PAGES)
    cursor = args.get("cursor")
    seen_cursors = set()
    if cursor:
        seen_cursors.add(cursor)
    last_progress = None
    first = True
    bound_identity = None
    page = None

    def _attach_stop(result: Dict[str, Any], reason: str) -> Dict[str, Any]:
        stopped = dict(result)
        stopped["delivery_stop_reason"] = reason
        return stopped

    while True:
        if not first:
            reason = _consume_tool_action(state, trace, tool="table_read",
                                          origin="table_page")
            if reason is not None:
                if page is None:
                    return {"status": "not_executed", "correctable": False,
                            "reason": reason, "table_id": scope.get("table_id")}
                return _attach_stop(page, reason)

        if cursor is not None:
            try:
                decoded = _decode_cursor(cursor)
            except ValueError:
                payload = {"table_id": scope.get("table_id"), "status": "rejected",
                           "reason": "TABLE_CURSOR_INVALID", "correctable": True}
                trace.append(TraceEntry(event="table_argument_rejected", detail=payload))
                return payload
            page_index = decoded.get("page_index", 0)
            if (isinstance(page_index, bool) or not isinstance(page_index, int)
                    or page_index < 0):
                payload = {"table_id": scope.get("table_id"), "status": "rejected",
                           "reason": "TABLE_CURSOR_OUT_OF_RANGE", "correctable": True}
                trace.append(TraceEntry(event="table_argument_rejected", detail=payload))
                return payload
            if page_index >= page_limit:
                payload = {"table_id": scope.get("table_id"), "status": "rejected",
                           "reason": "request_page_limit", "correctable": False}
                trace.append(TraceEntry(event="table_argument_rejected", detail=payload))
                return payload

        page = await _execute_table_tool(
            "table_read",
            {"scope": scope, "cursor": cursor} if cursor is not None else {"scope": scope},
            state=state, model=model, routing=routing, trace=trace,
            spans=spans, parent_span_id=parent_span_id, usage_acc=usage_acc,
        )
        page = dict(page) if isinstance(page, dict) else {"result": page}
        request_id = page.get("request_id")
        if not request_id:
            return page
        if page.get("page_index") is not None:
            trace.append(TraceEntry(event="table_page_read", detail={
                "request_id": str(request_id),
                "page_index": page.get("page_index"),
                "start_record_index": page.get("start_record_index"),
                "cell_count": len(page.get("cells") or []),
                "origin": origin if first else "table_page",
            }))
        ledger = state.get("table_reads_by_request") or {}
        entry = ledger.get(str(request_id))
        if entry is None:
            raise ValueError("TABLE_REQUEST_LEDGER_MISSING:" + str(request_id))
        if bound_identity is None:
            bound_identity = (str(request_id), entry.get("source_version"),
                              entry.get("interpretation_id"))
        elif bound_identity != (str(request_id), entry.get("source_version"),
                                entry.get("interpretation_id")):
            raise ValueError("TABLE_REQUEST_IDENTITY_CHANGED")
        if (entry.get("status") in ("complete", "blocked")
                or page.get("status") == "rejected" or page.get("correctable")):
            return page
        if scope.get("require_all_matches", True) is False:
            return page
        if page.get("has_more") is False and entry.get("status") != "complete":
            return page
        if len(entry.get("seen_pages") or {}) >= page_limit:
            return _attach_stop(page, "request_page_limit")
        next_cursor = page.get("next_cursor")
        cur_page_index = page.get("page_index")
        cur_start = page.get("start_record_index")
        if not next_cursor or next_cursor in seen_cursors:
            return _attach_stop(page, "cursor_no_progress")
        if not first and last_progress is not None:
            prev_page_index, prev_start = last_progress
            if (cur_page_index is None or cur_start is None
                    or int(cur_page_index) <= prev_page_index
                    or int(cur_start) <= prev_start):
                return _attach_stop(page, "cursor_no_progress")
        seen_cursors.add(next_cursor)
        last_progress = (int(cur_page_index or 0), int(cur_start or 0))
        cursor = next_cursor
        first = False


# ──────────────────────────────────────────────────────────────
#  Workbook one-shot delivery (READING-REPAIR §3)
# ──────────────────────────────────────────────────────────────


def _table_range_handle(table_id: str, info: Mapping[str, Any], body: str,
                        refs: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    """Make an advertised complete-table range a real delivered citation."""
    claim_id = "table_range_" + table_id
    source_id = str(info.get("source_id") or "")
    document_id = str(info.get("document_id") or source_id)
    refs[claim_id] = {
        "kind": "table_range", "source_id": source_id,
        "document_id": document_id, "path": str(info.get("path") or ""),
        "table_id": table_id,
    }
    return {
        "claim_id": claim_id, "passage_id": claim_id,
        "source_id": source_id, "document_id": document_id,
        "chunk_id": table_id, "excerpt": body,
        "page_path": str(info.get("path") or ""),
    }


def _bind_payload_citation_handles(state: Dict[str, Any], payload: Dict[str, Any]) -> None:
    """Expose one citation dialect: stable handles for delivered evidence."""
    query_state: EvidenceQueryState = state["query_state"]
    claim_handles: Dict[str, str] = {}
    for evidence_id in query_state.seen_evidence_order:
        record = query_state.seen_evidence_by_id[evidence_id]
        handle = query_state.evidence_handle(evidence_id)
        if handle and record.claim_id not in claim_handles:
            claim_handles[record.claim_id] = handle
    for item in payload.get("evidence") or []:
        if not item.get("handle") and item.get("claim_id") in claim_handles:
            item["handle"] = claim_handles[item.pop("claim_id")]
    for view in payload.get("views") or []:
        bound = []
        for ref in view.get("citation_refs") or []:
            handle = claim_handles.get(str(ref.get("claim_id") or ""))
            if handle:
                bound.append({
                    "handle": handle,
                    "source_id": str(ref.get("source_id") or ""),
                    "cell_count": str(ref.get("cell_count") or ""),
                })
        view["citation_refs"] = bound


def _fits_actual_request(model: Any, state: Dict[str, Any],
                         messages: List[Dict[str, Any]],
                         tool_payload: Dict[str, Any]) -> Optional[bool]:
    """Check whether the exact next traversal request fits the single-call cap."""
    try:
        from jonex_wiki.domain.engine.query.algorithm import (
            TraversalResponse,
            _tools_for_controller,
        )
        next_messages = list(messages) + [{
            "role": "tool",
            "content": json.dumps(
                tool_payload, ensure_ascii=False, separators=(",", ":")),
        }]
        tools = _tools_for_controller(state["controller"])
    except Exception:
        return False

    fits = getattr(model, "fits_request", None) or getattr(model, "fits_context", None)
    if fits is not None:
        return fits(next_messages, TraversalResponse.model_json_schema(), tools,
                    _traversal_max_tokens())
    estimate_units = getattr(model, "estimate_units", None)
    max_context = getattr(model, "max_context_tokens", None)
    if estimate_units is None or max_context is None:
        return None
    return estimate_units(
        next_messages, TraversalResponse.model_json_schema(), tools,
        _traversal_max_tokens()) <= max_context


MAX_DELIVERED_LINKS_PER_PAGE = 8


def _bounded_link_delivery(model: Any, state: Dict[str, Any],
                           messages: List[Dict[str, Any]],
                           payload: Dict[str, Any],
                           links: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Deliver navigation only in the room the payload budget leaves (R2.1).

    The body, claims and evidence echo are already inside ``payload`` and are
    never traded away for navigation.  Two bounds apply, in order:

    1. at most ``MAX_DELIVERED_LINKS_PER_PAGE`` published links per page — a
       page may have accumulated navigation long before this run;
    2. the exact next-request budget: if the payload with those links does not
       fit, every link is dropped, because an article pushed out of the request
       answers nothing.

    Whatever is left out is counted, so the delivery never claims the
    navigation list is complete.
    """
    published = {path: list(items or ()) for path, items in dict(links or {}).items()
                 if items}
    if not published:
        return None
    shown = {path: items[:MAX_DELIVERED_LINKS_PER_PAGE]
             for path, items in published.items()}
    omitted = {path: len(published[path]) - len(shown[path])
               for path in published if len(published[path]) > len(shown[path])}
    dropped_for_budget = False
    if shown and _fits_actual_request(
            model, state, messages, dict(payload, links=shown)) is False:
        # A definite over-budget answer: navigation goes, body stays.
        dropped_for_budget = True
        omitted = {path: len(items) for path, items in published.items()}
        shown = {}
    return {
        "links": shown,
        "omitted": {path: count for path, count in omitted.items() if count},
        "published": {path: len(items) for path, items in published.items()},
        "delivered": {path: len(items) for path, items in shown.items()},
        "omitted_counts": {path: count for path, count in omitted.items() if count},
        "dropped_for_budget": dropped_for_budget,
    }


def _make_read_preview(model: Any, state: Dict[str, Any],
                       messages: List[Dict[str, Any]],
                       base_payload: Optional[Dict[str, Any]] = None) -> Optional[Any]:
    """Budget voice for the article ``plan_read`` selection.

    The callable accepts one ``ReadingView`` and prices the exact tool payload
    the read tool would append: the already-built ``base_payload`` (files,
    claims, evidence echo, pagination, controller, message, table_scope) plus
    the selected views.  Only the actual next traversal request capacity is
    checked; no hypothetical final-synthesis or gap-judge reserve is priced.
    """
    base = dict(base_payload or {})
    if _fits_actual_request(model, state, messages, dict(base, views=[])) is None:
        return None

    def preview(view) -> bool:
        from jonex_wiki.domain.engine.query.read_views import reading_view_payload
        view_payload = reading_view_payload(view)
        return bool(_fits_actual_request(
            model, state, messages, dict(base, views=[view_payload])))

    return preview


def _make_material_read_preview(model: Any, state: Dict[str, Any],
                                messages: List[Dict[str, Any]]) -> Optional[Any]:
    """Price the exact tool payload the material delivery will append.

    The payload priced here is the payload serialized and sent, so the preview
    and the model-visible message cannot diverge.  Only the actual next
    traversal request capacity is checked.
    """
    if _fits_actual_request(model, state, messages, {}) is None:
        return None

    def preview(payload) -> bool:
        return bool(_fits_actual_request(model, state, messages, payload))

    return preview


def _read_repeat_note(state: MutableMapping[str, Any], cache_key: Optional[tuple],
                      step: int) -> Optional[Dict[str, Any]]:
    """Mark a read that repeats one already delivered in this run."""
    if cache_key is None:
        return None
    seen = state.setdefault("read_echo_steps", {})
    first = seen.get(cache_key)
    if first is None:
        seen[cache_key] = step
        return None
    return {
        "of_step": first,
        "note": ("the same read was already delivered at step {}; the corpus did "
                 "not change and the bytes above are identical - do not read it "
                 "again. For a table, values must come from table_read on the "
                 "requested range.".format(first)),
    }


def _article_span_handles(view: Any) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
    """A citation to actual delivered article ranges, independent of Claims."""
    source_id = str(view.source_id or "")
    path = str(view.article_path or "")
    body = "\n".join(str(block.get("body") or "") for block in view.blocks)
    spans = [(int(start), int(end)) for start, end in view.spans if int(end) > int(start)]
    if not source_id or not path.startswith("sources/articles/") or not spans or not body.strip():
        return [], {}
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    identity = json.dumps([view.object_key[0], source_id, path, spans, digest],
                          ensure_ascii=False, separators=(",", ":"))
    claim_id = "source_read_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
    handle = {"claim_id": claim_id, "passage_id": claim_id,
              "source_id": source_id, "document_id": source_id,
              "chunk_id": "chars:" + json.dumps(spans, separators=(",", ":")),
              "excerpt": body[:300]}
    ref = {"kind": "source_span", "source_id": source_id, "document_id": source_id,
           "path": path, "spans": [list(span) for span in spans],
           "body_sha256": digest, "revision_id": view.object_key[0]}
    return [handle], {claim_id: ref}


def _register_read_views(state: Dict[str, Any], views: Sequence[Any]) -> None:
    """Register actually-delivered views, deduplicating fully-covered ranges."""
    from jonex_wiki.domain.engine.query.read_views import (
        _fully_covered,
        reading_view_payload,
    )

    registered = state.setdefault("read_views", [])
    for view in views:
        payload = reading_view_payload(view)
        source_id = str(payload.get("source_id") or "")
        article_path = str(payload.get("article_path") or "")
        new_spans = [
            (int(span[0]), int(span[1]))
            for span in payload.get("spans") or []
            if span and len(span) >= 2 and int(span[1]) > int(span[0])
        ]
        existing_spans = [
            (int(span[0]), int(span[1]))
            for entry in registered
            if str(entry.get("source_id") or "") == source_id
            and str(entry.get("article_path") or "") == article_path
            for span in entry.get("spans") or []
            if span and len(span) >= 2 and int(span[1]) > int(span[0])
        ]
        if new_spans and existing_spans and all(
                _fully_covered(span, existing_spans) for span in new_spans):
            continue
        registered.append(payload)


def _table_source_index(state: Dict[str, Any], corpus: Any, source_id: str) -> tuple:
    """Native parser order shared by source navigation and exact context reads."""
    cache = state.setdefault("table_source_context_index", {})
    if source_id not in cache:
        locations, tables = {}, {}
        for path, records in corpus.source_records.items():
            if not any(str(record.get("source_id") or record.get("document_id") or "")
                       == source_id for record in records):
                continue
            metadata = (corpus.source_locations.get(path) or {}).get("metadata") or {}
            if metadata.get("table_id") and isinstance(metadata.get("source_position"), int):
                tables[str(metadata["table_id"])] = metadata
            for location in metadata.get("source_locations") or []:
                if isinstance(location.get("position"), int):
                    locations[location["position"]] = location
        cache[source_id] = (locations, tables)
    return cache[source_id]


def _table_source_context_view(state: Dict[str, Any], corpus: Any,
                               source_id: str, table_id: str,
                               *, heading_only: bool = False) -> Optional[Any]:
    """Read a native table's single-table heading block from its own article.

    Source/page/parser order establish membership, not a nearby unit string.
    Every text block must map in order to the immutable article; ambiguous
    headings, multiple tables and incomplete mappings retain the plain table.
    """
    from jonex_wiki.domain.engine.query.read_views import (
        _blocks_view, _read_context, load_document, unique_offset,
    )
    if corpus is None:
        return None
    locations, tables = _table_source_index(state, corpus, source_id)
    target = tables.get(table_id)
    if target is None:
        return None
    position, page = target["source_position"], target.get("page_number")
    if not isinstance(page, int):
        return None
    headings = sorted(pos for pos, loc in locations.items()
                      if loc.get("page_number") == page
                      and (loc.get("raw_item") or {}).get("text_level"))
    preceding = [pos for pos in headings if pos < position]
    if not preceding:
        return None
    start = preceding[-1]
    stop = next((pos for pos in headings if pos > position), None)
    # A single-table section can end at the page boundary rather than another
    # heading on that page. Read its exact prefix only: the next page's unit
    # or another table's context must not be attached to this one.
    prefix_only = stop is None
    if prefix_only:
        next_positions = [pos for pos, loc in locations.items()
                          if pos > position and loc.get("page_number") != page]
        next_positions += [item["source_position"] for item in tables.values()
                           if item["source_position"] > position
                           and item.get("page_number") != page]
        stop = min(next_positions) if next_positions else max(
            [position] + list(locations) + [item["source_position"] for item in tables.values()]) + 1
    if sum(start < item["source_position"] < stop
                          and item.get("page_number") == page
                          for item in tables.values()) != 1 and not heading_only:
        return None
    document = load_document(_read_context(state), source_id)
    if document is None:
        return None
    def heading_offset(text):
        matches = list(re.finditer(r"(?m)^#{1,6}[ \t]+(" + re.escape(text) + r")[ \t]*$",
                                   document.text)) if text else []
        if matches:
            return matches[0].start(1) if len(matches) == 1 else None
        return unique_offset(document.text, text)
    first = str((locations[start].get("raw_item") or {}).get("text") or "")
    next_heading = next((pos for pos, loc in sorted(locations.items())
                         if pos >= stop and (loc.get("raw_item") or {}).get("text_level")), None)
    last = str((locations.get(stop if not prefix_only else next_heading, {}).get("raw_item") or {}).get("text") or "")
    begin = heading_offset(first)
    end = heading_offset(last) if last else len(document.text)
    if begin is None or end is None or begin >= end:
        return None
    cursor = begin
    for pos, loc in sorted(locations.items()):
        limit = start + 1 if heading_only else (position if prefix_only else stop)
        if not start <= pos < limit or loc.get("page_number") != page:
            continue
        text = str((loc.get("raw_item") or {}).get("text") or "")
        offset = unique_offset(document.text[cursor:end], text)
        if offset is None:
            return None
        cursor += offset + len(text)
    return _blocks_view(document, [(begin, cursor)], coverage="ranges_partial",
                        selected_level="table_source_context", origin="parser_heading",
                        covered=[(begin, cursor)], citation_refs=[])


def _table_source_navigation(state: Dict[str, Any], corpus: Any,
                             views: Sequence[Any]) -> List[Dict[str, Any]]:
    """Navigation to authorized tables whose mapped original context was read."""
    from jonex_wiki.domain.engine.query.read_views import _fully_covered
    table_navigation = []
    for table_id, info in (state.get("table_registry") or {}).items():
        source_id = str(info.get("source_id") or "")
        source_views = [view for view in views if view.source_id == source_id]
        if not source_views:
            continue
        context_view = _table_source_context_view(state, corpus, source_id, table_id)
        if context_view is None:
            # A read heading can authorize navigation to several original
            # tables in its section, without attaching shared units to them.
            context_view = _table_source_context_view(
                state, corpus, source_id, table_id, heading_only=True)
        if context_view is not None and all(_fully_covered(span,
                [covered for view in source_views for covered in view.spans])
                for span in context_view.spans):
            table_navigation.append({"tool": "wiki_read", "path": "tables/{}.md".format(table_id),
                "table_id": table_id, "source_id": source_id,
                "title": context_view.text.splitlines()[0],
                "reason": "Original table belonging to this read source heading block."})
    return table_navigation


def _source_table_sections(state: Dict[str, Any], corpus: Any,
                           views: Sequence[Any]) -> List[Dict[str, Any]]:
    """Original section index for read sources; table targets remain unread.

    Multiple tables may belong to one heading. Listing that membership does
    not attach a unit, give a citation or expand an evidence read's scope.
    """
    result = []
    delivered = state.get("source_table_sections_delivered") or set()
    registry = state.get("table_registry") or {}
    for source_id in dict.fromkeys(
            str(view.get("source_id") or "") if isinstance(view, Mapping)
            else view.source_id for view in views):
        if source_id in delivered:
            continue
        locations, tables = _table_source_index(state, corpus, source_id)
        headings = sorted((pos, str((loc.get("raw_item") or {}).get("text") or ""))
                          for pos, loc in locations.items()
                          if (loc.get("raw_item") or {}).get("text_level"))
        sections = {}
        for table_id, metadata in sorted(tables.items(), key=lambda item: item[1]["source_position"]):
            if str((registry.get(table_id) or {}).get("source_id") or "") != source_id:
                continue
            preceding = [(pos, title) for pos, title in headings
                         if pos < metadata["source_position"]]
            if not preceding:
                continue
            position, title = preceding[-1]
            sections.setdefault((position, title), []).append("tables/{}.md".format(table_id))
        if sections:
            result.append({"source_id": source_id, "sections": [
                {"title": title, "tool": "wiki_read", "paths": paths}
                for (_, title), paths in sections.items()],
                "note": "Original parser section index. Navigation only; read table targets for evidence."})
    return result


def _append_source_section_index(payload: Dict[str, Any], state: Dict[str, Any],
                                 corpus: Any, model: Any, messages: List[Dict[str, Any]]) -> None:
    """Attach navigation after evidence admission, for every reading entry."""
    if not payload.get("success") or not payload.get("views"):
        return
    sections = _source_table_sections(state, corpus, payload["views"])
    if sections and _fits_actual_request(model, state, messages,
            {**payload, "source_table_sections": sections}) is True:
        payload["source_table_sections"] = sections
        state.setdefault("source_table_sections_delivered", set()).update(
            section["source_id"] for section in sections)


def _build_table_material_views(
    state: Dict[str, Any], material: Dict[str, Any], path: str,
    source_id: str, unit: Dict[str, Any], article_path: str,
    revision_id: str, scope: Optional[Any] = None, corpus: Any = None,
) -> Optional[Tuple[List[Any], List[List[Dict[str, str]]], Dict[str, Dict[str, Any]]]]:
    """Citation-bearing views for one table unit (whole-table candidate).

    It renders the *complete* table in a single view from the bulk grid reader
    (one body, one ``table_range_*`` citation reference), never a first-page
    default.  Whether that whole body is admitted is the caller's final
    single-request capacity decision, so no cell-count heuristic is applied
    here.  The citation index is built into a temporary ``refs`` dict and
    returned; the caller merges it into ``state["table_evidence_refs"]`` only
    after the view is admitted, so a rejected candidate registers no reference.
    """
    import sqlite3

    from jonex_wiki.domain.engine.query.read_views import ReadingView
    from jonex_wiki.domain.engine.query.table_reader import (
        full_table_body,
        read_logical_table,
        stream_cells,
    )

    location = unit.get("content_location") or {}
    tables_path = location.get("tables_path")
    if not tables_path:
        return None
    table_id = str(unit.get("table_id") or "")
    if not table_id:
        return None
    bundle_view = state["bundle_view"]
    registry_info = (state.get("table_registry") or {}).get(table_id) or {}
    info = {
        "source_id": registry_info.get("source_id") or source_id,
        "document_id": registry_info.get("document_id") or source_id,
        "path": registry_info.get("path") or tables_path,
    }
    try:
        with bundle_view.open_sqlite(tables_path) as connection:
            connection.row_factory = sqlite3.Row
            table = read_logical_table(connection, table_id)
            body = full_table_body(connection, info["source_id"], table_id)
            cells = [dict(row) for row in stream_cells(connection, table)]
            interpretation = connection.execute(
                "SELECT interpretation_id FROM interpretations WHERE table_id=? "
                "ORDER BY version DESC LIMIT 1", (table_id,)).fetchone()
            interpretation_id = (str(interpretation["interpretation_id"])
                                 if interpretation is not None else None)
            captions: List[str] = []
            footnotes: List[str] = []
            has_obs = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND "
                "name='table_observations'").fetchone()
            if has_obs is not None:
                obs = connection.execute(
                    "SELECT caption_json,footnote_json FROM table_observations "
                    "WHERE table_id=?", (table_id,)).fetchone()
                if obs is not None:
                    try:
                        captions = json.loads(obs["caption_json"] or "[]") or []
                        footnotes = json.loads(obs["footnote_json"] or "[]") or []
                    except (TypeError, ValueError):
                        captions, footnotes = [], []
    except Exception:
        return None
    if not body.strip():
        return None
    refs: Dict[str, Dict[str, Any]] = {}
    handles = _cell_handles(
        cells, table_id, info, state, context_cells=[],
        revision_id=revision_id, interpretation_id=interpretation_id, refs=refs)
    note_refs: List[Dict[str, Any]] = []
    for index, text in enumerate(captions):
        if str(text).strip():
            note_refs.append({"id": "caption:{}:{}".format(table_id, index),
                              "kind": "caption", "text": text})
    for index, text in enumerate(footnotes):
        if str(text).strip():
            note_refs.append({"id": "footnote:{}:{}".format(table_id, index),
                              "kind": "footnote", "text": text})
    if note_refs:
        handles += _note_handles(
            {"footnote_refs": note_refs, "sheet_id": table["sheet_id"]},
            table_id, info, state, revision_id=revision_id,
            interpretation_id=interpretation_id, refs=refs)
    handles.append(_table_range_handle(table_id, info, body, refs))
    row_indices = sorted({int(cell.get("row_index")) for cell in cells
                          if cell.get("row_index") is not None})
    row_span = (min(row_indices), max(row_indices) + 1) if row_indices else (0, 0)
    body_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    citation_refs = [{
        "claim_id": "table_range_" + table_id,
        "source_id": source_id,
        "cell_count": str(len(handles)),
    }] if handles else []
    view = ReadingView(
        object_key=(revision_id, source_id, article_path, body_sha),
        source_id=source_id,
        article_path=article_path,
        spans=[row_span],
        blocks=[{"start": 0, "end": len(body), "body": body}],
        citation_refs=citation_refs,
        coverage="table_complete",
        structure_origin="reading_materials",
        selected_level="full_table",
        title_path=[{"title": source_name, "origin": "source_identity"}]
        if (source_name := ((material.get("catalog") or {}).get(
            "source_identity") or {}).get("source_name")) else [],
    )
    views, handle_groups = [view], [handles]
    context_view = _table_source_context_view(state, corpus, source_id, table_id)
    if context_view is not None:
        context_handles, context_refs = _article_span_handles(context_view)
        context_view.citation_refs = [{"claim_id": handle["claim_id"], "source_id": source_id}
                                      for handle in context_handles]
        views.append(context_view)
        handle_groups.append(context_handles)
        refs.update(context_refs)
    return views, handle_groups, refs


def _build_workbook_material_views(
    state: Dict[str, Any], material: Dict[str, Any], path: str,
    source_id: str, unit: Dict[str, Any], article_path: str,
    revision_id: str,
) -> Optional[tuple]:
    """Render one native spreadsheet as one sheet-delimited, citable view."""
    import sqlite3 as _sqlite3

    from jonex_wiki.domain.engine.query.read_views import ReadingView
    from jonex_wiki.domain.engine.query.table_reader import (
        full_sheet_body,
        full_table_body,
        read_logical_table,
        stream_cells,
    )

    tables_path = (unit.get("content_location") or {}).get("tables_path")
    if not tables_path:
        return None
    bundle_view = state["bundle_view"]
    refs: Dict[str, Dict[str, Any]] = {}
    handles: List[Dict[str, str]] = []
    citation_refs: List[Dict[str, str]] = []
    try:
        with bundle_view.open_sqlite(tables_path) as connection:
            connection.row_factory = _sqlite3.Row
            parts = []
            for sheet in unit.get("sheets") or []:
                sheet_id = str(sheet.get("sheet_id") or "")
                if sheet_id:
                    rendered = full_sheet_body(connection, source_id, sheet_id)
                    if rendered.strip():
                        parts.append(rendered)
            for table_id_value in unit.get("table_ids") or []:
                table_id = str(table_id_value)
                table = read_logical_table(connection, table_id)
                cells = [dict(row) for row in stream_cells(connection, table)]
                registry_info = (state.get("table_registry") or {}).get(table_id) or {}
                info = {
                    "source_id": registry_info.get("source_id") or source_id,
                    "document_id": registry_info.get("document_id") or source_id,
                    "path": registry_info.get("path") or tables_path,
                }
                interpretation = connection.execute(
                    "SELECT interpretation_id FROM interpretations WHERE table_id=? "
                    "ORDER BY version DESC LIMIT 1", (table_id,),
                ).fetchone()
                interpretation_id = (str(interpretation["interpretation_id"])
                                     if interpretation is not None else None)
                table_handles = _cell_handles(
                    cells, table_id, info, state, context_cells=[],
                    revision_id=revision_id,
                    interpretation_id=interpretation_id, refs=refs,
                )
                table_handles.append(_table_range_handle(
                    table_id, info,
                    full_table_body(connection, source_id, table_id), refs,
                ))
                handles.extend(table_handles)
                if table_handles:
                    citation_refs.append({
                        "claim_id": "table_range_" + table_id,
                        "source_id": source_id,
                        "cell_count": str(len(table_handles)),
                    })
    except Exception:
        return None
    preamble = "# 完整工作簿 {}\n\n以下按工作表列出已保存的完整网格。\n".format(
        unit.get("name") or source_id)
    body = preamble + "\n\n".join(parts)
    if not body.strip():
        return None
    body_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    view = ReadingView(
        object_key=(revision_id, source_id, article_path, body_sha),
        source_id=source_id,
        article_path=article_path,
        spans=[(0, len(body))],
        blocks=[{"start": 0, "end": len(body), "body": body}],
        citation_refs=citation_refs,
        coverage="workbook_complete",
        structure_origin="reading_materials",
        selected_level="full_workbook",
    )
    return [view], [handles], refs


def _table_tool_workbook_candidate(
    corpus: QueryCorpus, name: str, args: Dict[str, Any], state: Dict[str, Any],
) -> Optional[Tuple[str, str, Dict[str, Any], Dict[str, Any]]]:
    """Resolve a table probe to its canonical native workbook, if any."""
    from jonex_wiki.domain.engine.query.read_views import (
        _read_context,
        _workbook_unit,
        load_reading_materials,
    )

    if name not in {"table_inspect", "table_locate", "table_read"}:
        return None
    table_id = (args.get("table_id") if name != "table_read"
                else (args.get("scope") or {}).get("table_id"))
    if not table_id:
        return None
    info = (state.get("table_registry") or {}).get(str(table_id)) or {}
    source_id = str(info.get("source_id") or "")
    if not source_id or source_id in (state.get("delivered_workbooks") or set()):
        return None
    catalog = load_reading_materials(_read_context(state), source_id)
    unit = _workbook_unit(catalog, source_id)
    if unit is None:
        return None
    path = "tables/{}.md".format(table_id)
    article_path = "sources/articles/{}.md".format(source_id)
    return source_id, path, unit, {
        "catalog": catalog, "unit": unit, "source_id": source_id,
        "article_path": article_path,
    }


def _try_deliver_workbook_for_table_tool(
    corpus: QueryCorpus, name: str, args: Dict[str, Any], call_id: str,
    state: Dict[str, Any], messages: List[Dict[str, Any]],
    trace: List[TraceEntry], model: Any,
    *, tool_result: Optional[Dict[str, Any]] = None,
) -> bool:
    """Atomically replace a first XLSX table probe with its whole workbook."""
    progress_started = time.monotonic()
    from jonex_wiki.domain.engine.query.read_views import reading_view_payload

    if tool_result is not None and tool_result.get("status"):
        # Unavailable/rejected probes retain their original outcome.
        return False
    candidate = _table_tool_workbook_candidate(corpus, name, args, state)
    table_id = str(args.get("table_id") or (args.get("scope") or {}).get("table_id") or "")
    if candidate is None:
        from jonex_wiki.domain.engine.query.read_views import resolve_reading_material
        if corpus is None or name not in {"table_inspect", "table_locate", "table_read"} or not table_id:
            return False
        path = "tables/{}.md".format(table_id)
        material = resolve_reading_material(state, corpus, path)
        if material is None:
            # Older publications already authorize native tables in the
            # registry, but may predate the reading-material catalog. Reuse
            # that location and the same complete-view builder and budget.
            info = (state.get("table_registry") or {}).get(table_id) or {}
            if info.get("source_id") and info.get("path"):
                material = {"source_id": info["source_id"], "unit": {
                    "kind": "table", "unit_id": table_id, "table_id": table_id,
                    "content_location": {"tables_path": info["path"]}}}
        if not material or material.get("unit", {}).get("kind") != "table":
            return False
        unit = material["unit"]
        source_id = str(material["source_id"])
        candidate = source_id, path, unit, material
    source_id, path, unit, material = candidate
    is_workbook = unit.get("kind") == "workbook"
    unit_kind = "workbook" if is_workbook else "table"
    unit_id = unit.get("unit_id") or table_id or source_id
    key = (source_id, unit_kind, unit_id)
    if state.get("delivered_material_ranges", {}).get(key):
        return False
    revision_id = getattr(state.get("bundle_view"), "revision_id", "") or ""
    article_path = str(material.get("article_path") or "sources/articles/{}.md".format(source_id))
    if is_workbook:
        built = _build_workbook_material_views(
            state, material, path, source_id, unit, article_path, revision_id)
    else:
        built = _build_table_material_views(
            state, material, path, source_id, unit, article_path, revision_id, corpus=corpus)
    if built is None:
        return False
    views, handles_by_page, refs = built
    view_payloads = [reading_view_payload(view) for view in views]
    for payload in view_payloads:
        payload["entry_points"] = [{"tool": name, "table_id": str(
            args.get("table_id") or (args.get("scope") or {}).get("table_id") or "")}]
    all_handles = [handle for page in handles_by_page for handle in page]
    payload = {
        **(tool_result or {}),
        "evidence": [{"claim_id": handle["claim_id"], "excerpt": handle["excerpt"]}
                     for handle in all_handles],
        "success": True,
        "status": ("table_tool_normalized_to_workbook" if is_workbook else "table_tool_normalized_to_table"),
        "requested_tool": name,
        "source_id": source_id,
        "files": {path: "material:{}:{}".format(unit_kind, unit_id)},
        "views": view_payloads,
        "message": "The complete saved material is available in views.",
        "next_step": {"note": "Use the complete views and their citation handles. "
                      "Read another table only if additional inputs are needed."},
    }
    if _fits_actual_request(model, state, messages, payload) is not True:
        trace.append(TraceEntry(
            event="table_material_too_large",
            detail={"tool": name, "source_id": source_id,
                    "table_id": str(args.get("table_id") or
                                    (args.get("scope") or {}).get("table_id") or "")},
        ))
        return False

    state.setdefault("table_evidence_refs", {}).update(refs)
    _register_table_handles(state, source_id, all_handles, trace)
    _register_read_views(state, views)
    for view in views:
        ranges = state.setdefault("delivered_material_ranges", {}).setdefault(key, [])
        for span in view.spans:
            normalized = (int(span[0]), int(span[1]))
            if normalized not in ranges:
                ranges.append(normalized)
    if is_workbook:
        state.setdefault("delivered_workbooks", set()).add(source_id)
    else:
        state.setdefault("delivered_tables", set()).add(table_id)
    state.setdefault("read_pages", set()).add(path)
    state["read_calls"] += 1
    state["searches_since_read"] = 0
    state["successful_read"] = bool(state.get("successful_read") or all_handles)
    _append_source_section_index(payload, state, corpus, model, messages)
    _bind_payload_citation_handles(state, payload)
    messages.append(_tool_message(
        call_id, name,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"))))
    trace.append(TraceEntry(
        event=("table_tool_normalized_to_workbook" if is_workbook else "table_tool_normalized_to_table"),
        detail={"tool": name, "source_id": source_id,
                "table_id": str(args.get("table_id") or
                                (args.get("scope") or {}).get("table_id") or ""),
                "body_bytes": sum(len(block.get("body", "").encode("utf-8"))
                                  for view in view_payloads
                                  for block in view.get("blocks") or []),
                "citation_count": len(all_handles)},
    ))
    trace.append(TraceEntry(
        event="reading_material_delivered",
        detail={"path": path, "source_id": source_id,
                "unit_id": unit.get("unit_id") or source_id,
                "unit_kind": unit_kind, "selected_level": "full_" + unit_kind,
                "pages": len(views), "page_count": len(views),
                "truncated": False, "whole_unit": True},
    ))
    from jonex_wiki.kernel.query_progress import publish_query_progress
    from jonex_wiki.domain.engine.util.query_progress import tool_progress_details
    publish_query_progress({"type": "tool", "tool": name, "title": "source material read",
                            "duration_ms": int((time.monotonic() - progress_started) * 1000),
                            "details": tool_progress_details(args, views=view_payloads)})
    return True


async def _exec_tool(
    corpus: QueryCorpus,
    fts: "sqlite3.Connection",
    name: str,
    args: Dict[str, Any],
    call_id: str,
    state: Dict[str, Any],
    messages: List[Dict[str, Any]],
    trace: List[TraceEntry],
    *,
    model: Any = None,
    routing: Optional[Dict[str, Any]] = None,
    spans: Optional[_SpanBuilder] = None,
    usage_acc: Optional[Dict[str, int]] = None,
    parent_span_id: Optional[str] = None,
    stall_sensitive: bool = True,
    origin: str = "model",
) -> None:
    """Execute one tool in code, updating counts, evidence and trace.

    Takes an already-parsed ``name``/``args``/``call_id`` directly so that both
    code-forced actions and model-requested calls share one execution path
    without round-tripping a model envelope.  Every tool call (forced or
    model-requested) records a legal assistant ``tool_calls`` message with a
    matching id immediately before its tool result message.

    ``origin`` is ``model``, ``forced`` or ``table_page``; it is only used for
    the tool-action admission trace and is never handed to the model.

    When ``spans`` is supplied, the tool emits ``tool.started`` and
    ``tool.completed`` spans with real wall-clock timing, and a successful
    ``wiki_read`` emits an ``evidence.added`` span (public refs only).
    """
    call_id = call_id or "call_{}".format(state["tool_index"])
    state["tool_index"] += 1

    if name not in _QUERY_TOOL_NAMES:
        raise ValueError(_UNKNOWN_QUERY_TOOL)

    messages.append(_assistant_tool_message(call_id, name, args))

    # Once an XLSX has been delivered as one complete workbook, all ordinary
    # reads and structural probes for tables inside that workbook are strictly
    # redundant. Return a compact correction without spending another tool
    # action or re-sending evidence. The first such request asks the traversal
    # loop for an answer-only turn. A first explicit table_compute remains allowed.
    delivered_workbooks = state.get("delivered_workbooks") or set()
    if delivered_workbooks and name == "table_compute":
        table_id = str(args.get("table_id") or "")
        info = (state.get("table_registry") or {}).get(table_id) or {}
        compute_source = str(info.get("source_id") or "")
        if compute_source in delivered_workbooks:
            if not args.get("request_id"):
                payload = {
                    "status": "range_selection_required",
                    "correctable": True,
                    "source_id": compute_source,
                    "table_id": table_id,
                    "instruction": (
                        "The workbook is complete. If deterministic calculation "
                        "is necessary, call table_inspect and table_read for the required scope first, then select its completed explicit request_id."
                    ),
                }
                messages.append(_tool_message(
                    call_id, name, json.dumps(
                        payload, ensure_ascii=False, separators=(",", ":"))))
                trace.append(TraceEntry(
                    event="workbook_compute_range_required",
                    detail={"source_id": compute_source, "table_id": table_id},
                ))
                return
            used = state.setdefault("workbook_compute_used", set())
            compute_key = (table_id, str(args.get("request_id")), json.dumps(args, sort_keys=True, ensure_ascii=False))
            if compute_key in used:
                state["force_answer_after_workbook"] = True
                payload = {
                    "status": "workbook_compute_already_used",
                    "correctable": False,
                    "source_id": compute_source,
                    "instruction": "Submit the answer from the completed workbook and computation.",
                }
                messages.append(_tool_message(
                    call_id, name, json.dumps(
                        payload, ensure_ascii=False, separators=(",", ":"))))
                trace.append(TraceEntry(
                    event="workbook_redundant_tool_blocked",
                    detail={"tool": name, "source_id": compute_source,
                            "attempt": 2, "force_answer": True},
                ))
                return
    redundant_source = None
    if delivered_workbooks and name in {
            "wiki_read"}:
        # Deliberately narrow: table_read / table_inspect probes on a delivered
        # workbook stay admitted, because selecting a range and then calling
        # table_compute is exactly what the workbook delivery instructs the
        # model to do next (a widened set is caught by
        # test_workbook_structural_probe_can_select_compute_range).
        if name == "wiki_read":
            from jonex_wiki.domain.engine.query.read_views import resolve_reading_material
            resolved_sources = set()
            all_paths_are_workbooks = bool(args.get("paths"))
            for requested_path in args.get("paths") or []:
                material = resolve_reading_material(state, corpus, str(requested_path))
                if material and material.get("unit", {}).get("kind") == "workbook":
                    resolved_sources.add(str(material.get("source_id") or ""))
                else:
                    all_paths_are_workbooks = False
            if all_paths_are_workbooks and len(resolved_sources) == 1:
                candidate = next(iter(resolved_sources))
                if candidate in delivered_workbooks:
                    redundant_source = candidate
        else:
            table_id = (args.get("table_id") if name != "table_read"
                        else (args.get("scope") or {}).get("table_id"))
            info = (state.get("table_registry") or {}).get(str(table_id)) or {}
            candidate = str(info.get("source_id") or "")
            if candidate in delivered_workbooks:
                redundant_source = candidate
    if redundant_source:
        counts = state.setdefault("redundant_workbook_requests", {})
        counts[redundant_source] = int(counts.get(redundant_source, 0)) + 1
        state["force_answer_after_workbook"] = True
        payload = {
            "success": True,
            "status": "workbook_already_complete",
            "source_id": redundant_source,
            "evidence_added": False,
            "instruction": (
                "The complete workbook, every sheet, and every saved cell are "
                "already in the evidence ledger. Answer from that material now. "
                "Use table_inspect and table_read to select a scope, then table_compute if deterministic "
                "calculation is still required."
            ),
        }
        messages.append(_tool_message(
            call_id, name,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"))))
        trace.append(TraceEntry(event="workbook_redundant_tool_blocked", detail={
            "tool": name, "source_id": redundant_source,
            "attempt": counts[redundant_source],
            "force_answer": bool(state.get("force_answer_after_workbook")),
        }))
        return

    # A table whose exhaustive read already completed has its whole grid in the
    # ledger, so reading it again adds no evidence - it only spends the tool
    # budget and repeats the same rows.  Unlike the workbook case this does not
    # force an answer on the first attempt: the model may legitimately still
    # need table_compute over the delivered grid.
    delivered_tables = state.get("delivered_tables") or set()
    if delivered_tables and name == "table_read":
        requested_table = str(args.get("table_id")
                              or (args.get("scope") or {}).get("table_id")
                              or (args.get("raw_scope") or {}).get("table_id") or "")
        if (requested_table and requested_table in delivered_tables
                and (not ((args.get("scope") or {}).get("task_type") in {"enumerate"})
                     or any(entry.get("status") == "complete" for entry in
                            (state.get("table_reads_by_request") or {}).values()
                            if entry.get("table_id") == requested_table))):
            attempts = state.setdefault("redundant_table_reads", {})
            attempts[requested_table] = int(attempts.get(requested_table, 0)) + 1
            if attempts[requested_table] >= 2:
                state["force_answer_after_workbook"] = True
            payload = {
                "success": True,
                "status": "table_already_complete",
                "table_id": requested_table,
                "evidence_added": False,
                "evidence": [
                    {"handle": state["query_state"].evidence_handle(evidence_id),
                     "excerpt": record.excerpt}
                    for evidence_id in state["query_state"].seen_evidence_order
                    for record in [state["query_state"].seen_evidence_by_id[evidence_id]]
                    if record.passage_id == "table_range_" + requested_table
                    and record.claim_id.startswith(("table_cell_", "table_note_"))
                ],
                "instruction": (
                    "This table's complete grid was already delivered in this run "
                    "with the cell handles below. Use those handles for arithmetic. "
                    "For count/grouping, select a structured scope with task_type=enumerate "
                    "before table_compute. Do not inspect or reread it for its contents."
                ),
            }
            messages.append(_tool_message(
                call_id, name,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"))))
            trace.append(TraceEntry(event="table_redundant_tool_blocked", detail={
                "tool": name, "table_id": requested_table,
                "attempt": attempts[requested_table],
                "force_answer": bool(state.get("force_answer_after_workbook")),
            }))
            return

    # One admission per tool action, against the wall-clock deadline and the
    # tool-call cap.  It is the only place ``state["total"]`` grows, so a
    # forced action, a model call and an extra table page are all counted once
    # and by the same rule.
    reason = _consume_tool_action(state, trace, tool=name, origin=origin)
    if reason is not None:
        messages.append(_tool_message(
            call_id, name,
            json.dumps({"status": "not_executed", "correctable": False,
                        "reason": reason}, ensure_ascii=False, separators=(",", ":"))))
        trace.append(TraceEntry(event="tool_not_executed", detail={
            "tool": name, "reason": reason}))
        return

    try:
        args = _validate_tool_arguments(name, args)
        if name == "table_read":
            from jonex_wiki.domain.engine.query.table_reader import RawScopeSpec, ScopeSpec
            scope_key = "raw_scope" if "raw_scope" in args else "scope"
            scope_model = RawScopeSpec if scope_key == "raw_scope" else ScopeSpec
            scope_spec = scope_model.model_validate(args[scope_key])
            args[scope_key] = scope_spec.model_dump()
            if ("cursor" in args and args["cursor"] is not None
                    and not isinstance(args["cursor"], str)):
                raise ValueError(MALFORMED_TOOL_CALL + ":TABLE_READ_CURSOR")
    except (ValueError, ValidationError) as exc:
        if isinstance(exc, ValidationError):
            first_error = exc.errors()[0] if exc.errors() else {}
            reason = "TABLE_SCOPE_INVALID:" + (
                ".".join(str(part) for part in first_error.get("loc") or [])
                if first_error.get("loc") else "unknown")
            errors = [{"loc": list(error.get("loc") or []),
                       "type": error.get("type"),
                       "msg": error.get("msg")} for error in exc.errors()]
        else:
            reason = str(exc).splitlines()[0].strip()
            errors = [{"reason": reason}]
        result = {"status": "rejected", "correctable": True, "reason": reason,
                  "allowed_schema": _query_tool_parameters(name),
                  "errors": errors}
        messages.append(_tool_message(
            call_id, name,
            json.dumps(result, ensure_ascii=False, separators=(",", ":"))))
        trace.append(TraceEntry(event="tool_argument_rejected", detail={
            "tool": name, "reason": reason}))
        return

    started_dt = datetime.now(timezone.utc)
    started_mono = time.monotonic()
    # Whole-material delivery supplements an inspection/location request; it
    # must retain that request's fields or matches for the next tool action.
    table_probe_result = None
    if name in {"table_inspect", "table_locate"}:
        table_probe_result = await _execute_table_tool(
            name, args, state=state, model=model, routing=routing, trace=trace,
            spans=spans, parent_span_id=parent_span_id, usage_acc=usage_acc)
    if _try_deliver_workbook_for_table_tool(
            corpus, name, args, call_id, state, messages, trace, model,
            tool_result=table_probe_result):
        return

    category = "RETRIEVAL" if name in ("wiki_search", "wiki_read", "table_inspect", "table_locate", "table_read", "table_compute") else None
    if spans is not None:
        from jonex_wiki.domain.engine.util.query_progress import tool_progress_details
        spans.add(
            type_=TraceSpanType.TOOL,
            title="{} started".format(name),
            tool=name,
            category=category,
            started_at=_utc_now_iso(started_dt),
            decision="execute",
            progress_details=tool_progress_details(args),
            event_type=QueryEventType.TOOL_STARTED.value,
            parent_id=parent_span_id,
        )

    if name == "table_compute":
        result = _table_compute_payload(state, args)
        _record_table_compute_outcome(state, args, result)
        if result.get("status") == "complete":
            if delivered_workbooks:
                state.setdefault("workbook_compute_used", set()).add((
                    str(args.get("table_id") or ""), str(args.get("request_id")),
                    json.dumps(args, sort_keys=True, ensure_ascii=False)))
        state["read_calls"] += 1
        state["searches_since_read"] = 0
        content = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        messages.append(_tool_message(call_id, name, content))
        trace.append(TraceEntry(event="tool_result", detail={
            "tool": name, "status": result.get("status"),
            "table_id": result.get("table_id"),
            "operation": result.get("operation"),
            "matched_count": result.get("matched_count"),
            "reason": result.get("reason"),
            "payload_bytes": len(content.encode("utf-8")),
        }))
        if spans is not None:
            spans.add(type_=TraceSpanType.TOOL, title="{} completed".format(name), tool=name,
                category=category, started_at=_utc_now_iso(started_dt),
                duration_ms=int((time.monotonic() - started_mono) * 1000), decision="execute",
                event_type=QueryEventType.TOOL_COMPLETED.value, parent_id=parent_span_id)
        return

    if name in {"table_inspect", "table_locate", "table_read"}:
        if name == "table_read":
            result = await _read_table_request(
                args, state=state, model=model, routing=routing, trace=trace,
                spans=spans, usage_acc=usage_acc, parent_span_id=parent_span_id,
                origin=origin)
        else:
            result = table_probe_result
        # The reader's own payload is what the model must reason over.  Without
        # this delivery the call fell through to the wiki_read serializer, which
        # answered with ``success=false``/``WIKI_READ_INVALID`` and no cells at
        # all, so a table could be read but never used.
        table_payload = dict(result) if isinstance(result, dict) else {"result": result}
        if name == "table_inspect" and str(args.get("table_id") or "") in delivered_tables:
            # Keep inspection fields for legitimate range/grouping selection.
            # Content is already available; the old next_step demanded a reread.
            table_payload["next_step"] = {"note": "The complete original table is already "
                "delivered. Use its cell handles for arithmetic; select a structured "
                "scope with task_type=enumerate only when aggregation is needed."}
        state["read_calls"] += 1
        state["searches_since_read"] = 0
        # The six-field allowlist is delivered exactly as a wiki read exposes
        # it: the model can only cite a cell whose handle it can name.
        content = json.dumps(
            _table_delivery_payload(table_payload, state),
            ensure_ascii=False, separators=(",", ":"))
        table_payload.pop("handles", None)
        messages.append(_tool_message(call_id, name, content))
        trace.append(TraceEntry(event="tool_result", detail={
            "tool": name,
            "table_id": table_payload.get("table_id"),
            "status": table_payload.get("status") or (
                table_payload.get("coverage") or {}).get("range_status"),
            "cells": len(table_payload.get("cells") or []),
            "has_more": bool(table_payload.get("has_more")),
            "payload_bytes": len(content.encode("utf-8")),
        }))
        if spans is not None:
            spans.add(type_=TraceSpanType.TOOL, title="{} completed".format(name), tool=name,
                category=category, started_at=_utc_now_iso(started_dt),
                duration_ms=int((time.monotonic() - started_mono) * 1000), decision="execute",
                event_type=QueryEventType.TOOL_COMPLETED.value, parent_id=parent_span_id)
        return

    if name == "wiki_search":
        state["search_calls"] += 1
        state["searches_since_read"] += 1
        query = str(args.get("query", ""))
        original_question = str(state.get("original_question", ""))
        protected = list(dict.fromkeys(re.findall(
            # A suffixed value may be immediately followed by another value
            # (``4月14日``), so only the bare-number alternative needs the
            # right-hand boundary.  Otherwise ``4月`` degrades to bare ``4``.
            r"(?<![A-Za-z0-9])(?:\d{1,4}(?:\.\d+)?(?:%|万|亿|年|月|日|岁)|[RT]\+?\d+|\d+(?:\.\d+)?(?![A-Za-z0-9]))",
            original_question,
            re.IGNORECASE,
        )))
        missing = [term for term in protected if term.casefold() not in query.casefold()]
        if missing:
            query = " ".join([query, *missing]).strip()
            trace.append(TraceEntry(
                event="search_constraints_preserved",
                detail={"terms": missing, "query": query},
            ))
        with telemetry_stage(
            current_runtime_telemetry(), "search_cache"
        ) as cache_stage:
            cached = state["search_cache"].get(query)
            cached_hit = cached is not None
            if cache_stage is not None:
                cache_stage.observe_cache(hit=cached_hit)
        if cached is not None:
            hits = cached
        else:
            with telemetry_stage(
                current_runtime_telemetry(), "candidate_retrieval"
            ):
                material_cache = state.get("search_material_cache")
                if material_cache is None or material_cache.corpus is not corpus:
                    material_cache = _SearchMaterialCache(corpus)
                    state["search_material_cache"] = material_cache
                hits = _fts_search(fts, corpus, query, material_cache=material_cache)
                # LWP-RET-002 chapter_tree route: merge deterministic
                # chapter-tree navigation hits (page-number ordered) ahead of
                # the FTS hits so structural questions surface chapter pages
                # even when the lexical index misses.
                if getattr(corpus, "route", None) == "chapter_tree":
                    nav_hits = _chapter_tree_hits(corpus, query)
                    if nav_hits:
                        merged = list(nav_hits)
                        merged_paths = {h.path for h in merged}
                        for hit in hits:
                            if hit.path not in merged_paths:
                                merged.append(hit)
                        hits = merged
                        trace.append(
                            TraceEntry(
                                event="chapter_tree_navigated",
                                detail={
                                    "paths": [h.path for h in nav_hits],
                                    "page_numbers": [
                                        corpus.page_number_map.get(h.path)
                                        for h in nav_hits
                                    ],
                                },
                            )
                        )
            state["search_cache"][query] = hits
        if _llm_rerank_enabled() and model is not None:
            with telemetry_stage(current_runtime_telemetry(), "rerank"):
                reranked_paths = await _llm_rerank(
                    model,
                    query,
                    hits,
                    routing,
                    spans,
                    usage_acc,
                    (routing or {}).get("model"),
                )
            if reranked_paths:
                by_path = {h.path: h for h in hits}
                hits = [by_path[p] for p in reranked_paths if p in by_path]
                state["search_cache"][query] = hits
                trace.append(
                    TraceEntry(
                        event="llm_reranked",
                        detail={"paths": [h.path for h in hits]},
                    )
                )
        if _diversity_rerank_enabled():
            with telemetry_stage(current_runtime_telemetry(), "rerank"):
                hits = _diversity_rerank(hits)
            state["search_cache"][query] = hits
            trace.append(
                TraceEntry(
                    event="diversity_reranked",
                    detail={"paths": [h.path for h in hits]},
                )
            )
        rewritten = ""
        unread_paths: List[str] = []
        # R1.1: search hits and fact mentions are one observation: material this
        # run can still access but has not read.  Order is preserved (hits keep
        # their ranked order, mentions follow), duplicates are dropped, and a
        # candidate never receives an evidence handle.
        observations = _candidate_observations(corpus, query, hits, state)
        _record_candidate_observations(state, observations)
        candidate_unread = [item["path"] for item in observations if not item["read"]]
        progress = _search_progress(
            hit_paths=[hit.path for hit in hits], read_pages=state["read_pages"],
            candidate_unread=candidate_unread, stall_count=state["stall_count"],
            stall_sensitive=stall_sensitive)
        state["stall_count"] = progress["stall_count"]
        if progress["empty_streak"] is not None:
            state["empty_streak"] = progress["empty_streak"]
        unread_paths = progress["unread_paths"]
        if not hits:
            # LWP-RET-002 raw-source fallback (P0 #1): an empty search degrades
            # to a code-forced, on-demand read of the raw source spans of the
            # top matching claims — never the whole archive.  Matched mode fails
            # closed (strict in matched mode); production mode emits
            # the raw_source_fallback trace + CFG-001 DegradationEvent and the
            # run can cite the raw handles (six-field chain).
            raw_handles: List[Dict[str, str]] = []
            if not state.get("raw_fallback_fired") and _raw_fallback_enabled(corpus):
                candidate_spans = _raw_fallback_candidate_spans(corpus, query)
                if candidate_spans:
                    state["raw_fallback_fired"] = True
                    raw_handles = [
                        _raw_source_fallback_span(corpus, span, trace, reason="search_empty")
                        for span in candidate_spans
                    ]
                    _register_raw_handles(state, raw_handles, trace)
            if not raw_handles:
                # Wave 3 P1.2: one deterministic empty-search rewrite per run.
                # The empty streak only advances when a rewrite is unavailable
                # or when the rewritten search is also empty, so a recoverable
                # query never burns an empty-search turn.  The rewrite is itself
                # a tool call, so it never fires when it would push the run past
                # the call cap.
                if not state["rewrite_used"] and state["total"] < state["max_tool_calls"]:
                    candidate = _rewrite_query_for_search(query)
                    if candidate and candidate != query:
                        rewritten = candidate
                if not rewritten and stall_sensitive and not candidate_unread:
                    state["empty_streak"] += 1
        messages.append(
            _tool_message(call_id, "wiki_search", json.dumps({"results": [h.model_dump() for h in hits],
                "candidates": observations}, ensure_ascii=False, separators=(",", ":")))
        )
        trace.append(
            TraceEntry(
                event="tool_result",
                detail={
                    "tool": "wiki_search",
                    "query": query,
                    "count": len(hits),
                    "cached": cached_hit,
                    # Public candidate frontier: enough to attribute a miss
                    # to recall/ranking without persisting prompts or source
                    # text.  ``rank`` is the deterministic FTS score
                    # ordering value already present on SearchHit.
                    "candidate_paths": [hit.path for hit in hits],
                    "candidate_ranks": [hit.rank for hit in hits],
                    "frontier_paths": [
                        hit.path for hit in hits if hit.path not in state["read_pages"]
                    ],
                    # R1.1: one observation per accessible page, with where it
                    # came from and whether it is still unread.
                    "observed_candidates": [
                        {"path": item["path"], "origin": item["origin"],
                         "read": item["read"]}
                        for item in observations
                    ],
                    "unread_candidate_paths": candidate_unread,
                },
            )
        )
        _record_query_state_event(
            state,
            SearchRecorded(
                query=query,
                candidates=tuple(
                    EvidenceSearchCandidate(
                        path=hit.path,
                        rank=rank,
                        snippet=hit.snippet[:250],
                    )
                    for rank, hit in enumerate(hits, start=1)
                ),
            ),
            trace,
        )
        controller = state.get("controller")
        if isinstance(controller, ControllerRuntime):
            controller, progress = observe_search_progress(
                controller,
                query=query,
                candidate_paths=(hit.path for hit in hits),
                unread_candidate_paths=(
                    hit.path
                    for hit in hits
                    if hit.path not in state["read_pages"]
                ),
            )
            _publish_control_transition(state, controller, (progress,), trace)
            state["frontier_paths"] = list(
                controller.state.unread_frontier_paths
            )
        else:
            # Low-level retrieval probes may exercise this private tool helper
            # without the full traversal runtime. Keep those probes focused on
            # retrieval/cache behavior while production runs always publish typed
            # controller progress through the branch above.
            state["frontier_paths"] = [
                hit.path for hit in hits if hit.path not in state["read_pages"]
            ]
        if spans is not None:
            spans.add(
                type_=TraceSpanType.TOOL,
                title="{} completed".format(name),
                tool=name,
                category=category,
                started_at=_utc_now_iso(started_dt),
                duration_ms=int((time.monotonic() - started_mono) * 1000),
                input_summary="query={}".format(query[:200]),
                result_summary="{} hit(s)".format(len(hits)),
                progress_details=tool_progress_details({"query": query}, pages=(
                    {"path": hit.path, "title": hit.title, "excerpt": hit.snippet} for hit in hits)),
                decision="execute",
                event_type=QueryEventType.TOOL_COMPLETED.value,
                parent_id=parent_span_id,
            )
        if (
            rewritten
            and isinstance(controller, ControllerRuntime)
            and controller.state.no_progress_searches
            >= _force_read_no_progress()
            and controller.state.unread_frontier_paths
        ):
            # The current hop already has something useful to read.  Prefer it
            # over spending another retrieval call on the legacy empty-query
            # rewrite path.
            rewritten = ""
        # Rabbit-Hole guard: after N consecutive searches with no intervening
        # read, stop asking the model and read the top unread hit directly.
        if (
            not state.get("agent_driven")
            and state["searches_since_read"] >= _search_read_force_limit_via_shim()
            and unread_paths
            and state["total"] < state["max_tool_calls"]
        ):
            forced_path = unread_paths[0]
            trace.append(
                TraceEntry(
                    event="forced_read_after_search",
                    detail={
                        "path": forced_path,
                        "searches_since_read": state["searches_since_read"],
                    },
                )
            )
            await _exec_tool(
                corpus, fts, "wiki_read", {"paths": [forced_path]}, None,
                state, messages, trace,
                model=model, routing=routing,
                spans=spans, usage_acc=usage_acc, parent_span_id=parent_span_id,
            )
            return
        if rewritten:
            state["rewrite_used"] = True
            trace.append(
                TraceEntry(event="search_rewritten", detail={"from": query, "to": rewritten})
            )
            await _exec_tool(
                corpus, fts, "wiki_search", {"query": rewritten}, None,
                state, messages, trace,
                model=model, routing=routing,
                spans=spans, usage_acc=usage_acc, parent_span_id=parent_span_id,
                # A rewrite of a controller-forced search is also controller
                # recovery, so it inherits the outer stall sensitivity: a
                # deterministic gap-search rewrite must not advance the
                # model-empty-search streak and pre-empt the answer gate.
                stall_sensitive=stall_sensitive,
            )
        return

    state["read_calls"] += 1
    state["searches_since_read"] = 0
    paths = args.get("paths", [])
    read_selection: Optional[Any] = None
    evidence_snapshot: Dict[str, Any] = {}
    cache_key: Optional[tuple] = None
    try:
        cache_key = (
            tuple(sorted(_validate_read_path(corpus, p) for p in paths)),
            args.get("cursor"),
            args.get("page_size", 200),
        )
    except ValueError:
        cache_key = None  # invalid path — _execute_read reports it stably
    with telemetry_stage(
        current_runtime_telemetry(), "read_cache"
    ) as cache_stage:
        cached_hit = cache_key is not None and cache_key in state["read_cache"]
        if cache_stage is not None:
            cache_stage.observe_cache(hit=cached_hit)
    if cached_hit:
        outcome = state["read_cache"][cache_key]
    else:
        outcome = _execute_read_via_shim(
            corpus,
            paths,
            cursor=args.get("cursor"),
            page_size=args.get("page_size", 200),
        )
        if cache_key is not None:
            state["read_cache"][cache_key] = outcome
    if outcome.success:
        source_paths = [path for path in outcome.files if path in corpus.source_records]
        if source_paths:
            trace.append(TraceEntry(event="source_evidence_read", detail={
                "paths": source_paths, "kind": "source", "generated_claim": False,
                "locations": {path: {key: value for key, value in corpus.source_locations[path].items() if key != "metadata"} for path in source_paths}}))
        fallback_paths = [
            path for path in outcome.files
            if path not in corpus.source_records
            and not corpus.claims.get(path)
            and corpus.evidence_fallback.get(path)
        ]
        if fallback_paths:
            trace.append(TraceEntry(event="evidence_fallback_read", detail={
                "paths": fallback_paths, "kind": "keep_and_search_raw",
                "generated_claim": False,
                "spans": {path: list(corpus.evidence_fallback[path])
                          for path in fallback_paths}}))
        # Snapshot the fields the registration block mutates so a fully
        # budget-rejected read can roll the citation state back (nothing
        # delivered → nothing registered).
        evidence_snapshot = {
            "query_state": state["query_state"],
            "collected_evidence": state["collected_evidence"],
            "successful_read": state["successful_read"],
            "read_pages": set(state["read_pages"]),
            "stall_count": state["stall_count"],
            "empty_streak": state["empty_streak"],
            "controller": state["controller"],
            "frontier_paths": list(state.get("frontier_paths") or []),
        }
        before_keys = {_evidence_key(r) for r in state["collected_evidence"]}
        evidence_before = len(state["collected_evidence"])
        query_state: EvidenceQueryState = state["query_state"]
        seen_before = set(query_state.seen_evidence_by_id)
        _record_query_state_event(
            state,
            EvidenceRead(
                paths=tuple(outcome.files),
                evidence=tuple(
                    EvidenceRecord.model_validate(
                        _with_evidence_page_path(item, outcome.files,
                                                 corpus.source_records))
                    for item in outcome.evidence
                ),
            ),
            trace,
        )
        query_state = state["query_state"]
        new_evidence_ids = [
            evidence_id
            for evidence_id in query_state.seen_evidence_order
            if evidence_id not in seen_before
        ]

        # Re-rank the complete append-only registry. Previously only the active
        # subset plus the newest read was considered, so a fact cropped before
        # its gap was known could never return to the bounded active window.
        seen_records = [
            {
                key: str(value)
                for key, value in query_state.seen_evidence_by_id[evidence_id]
                .model_dump(exclude={"evidence_id"})
                .items()
            }
            for evidence_id in query_state.seen_evidence_order
        ]
        capped_evidence = _cap_evidence_diversity(
            seen_records,
            _evidence_units_max(),
            _evidence_per_doc_max(),
            focus_terms=state.get("active_gap_terms"),
        )

        # The pre-existing diversity controller chooses the relevant working
        # set; QUERY-001 makes those choices explicit typed transitions.  Facts
        # that were read but not selected remain immutable in the seen registry.
        capped_ids = []
        for item in capped_evidence:
            evidence_id = EvidenceRecord.model_validate(item).evidence_id
            if evidence_id not in capped_ids:
                capped_ids.append(evidence_id)
        from jonex_wiki.domain.engine.query.traversal_evidence import pinned_evidence_ids
        capped_id_set = set(capped_ids) | pinned_evidence_ids(state)
        relevance_drops = [
            evidence_id
            for evidence_id in query_state.active_evidence_ids
            if evidence_id not in capped_id_set
        ]
        if relevance_drops:
            _record_query_state_event(
                state,
                EvidenceDropped(evidence_ids=tuple(relevance_drops)),
                trace,
            )
        query_state = state["query_state"]
        _activate_new_evidence(
            state,
            [
                evidence_id
                for evidence_id in capped_ids
                if evidence_id not in query_state.active_evidence_ids
            ],
            trace,
        )
        query_state = state["query_state"]
        state["collected_evidence"] = _active_evidence_payload(state)
        # A navigation/index read is observable and replayable, but only a read
        # that registered citable evidence unlocks an answer.
        state["successful_read"] = bool(query_state.active_evidence_ids)
        for path in outcome.files:
            if not outcome.pagination.get(path, {}).get("has_more", False):
                state["read_pages"].add(path)
        # LWP-RET-002 route-aware read telemetry (only on the three new routes,
        # so the proposition/auto trace stays byte-identical to the hybrid):
        #   - chapter_page_read: page number / chapter of every read chapter
        #     page (chapter_tree navigation, D-5);
        #   - evidence_placement: where each newly registered evidence record
        #     landed (page + page number + identity);
        #   - evidence_crop_metrics: bounded-evidence crop with retention_ratio;
        #   - context_budget: the bounded evidence window vs the char budget.
        if _route_active(corpus):
            if getattr(corpus, "route", None) == "chapter_tree":
                for read_path in sorted(outcome.files):
                    page_number = corpus.page_number_map.get(read_path)
                    if page_number is not None:
                        trace.append(
                            TraceEntry(
                                event="chapter_page_read",
                                detail={
                                    "path": read_path,
                                    "page_number": page_number,
                                    "chapter": (
                                        read_path.rsplit("/", 1)[0]
                                        if "/" in read_path
                                        else ""
                                    ),
                                },
                            )
                        )
            path_by_claim = {
                str(claim.get("claim_id", "")): path
                for path, page_claims in outcome.claims.items()
                for claim in page_claims
            }
            for seq, evidence_id in enumerate(new_evidence_ids):
                record = query_state.seen_evidence_by_id.get(evidence_id)
                if record is None:
                    continue
                path = path_by_claim.get(record.claim_id, "")
                trace.append(
                    TraceEntry(
                        event="evidence_placement",
                        detail={
                            "seq": seq,
                            "path": path,
                            "page_number": corpus.page_number_map.get(path),
                            "claim_id": record.claim_id,
                            "passage_id": record.passage_id,
                            "source_id": record.source_id,
                            "document_id": record.document_id,
                            "chunk_id": record.chunk_id,
                        },
                    )
                )
            total_candidates = len(query_state.seen_evidence_by_id)
            kept = len(capped_evidence)
            dropped = max(0, total_candidates - kept)
            if dropped > 0:
                retention_ratio = (
                    round(kept / total_candidates, 4) if total_candidates else 1.0
                )
                trace.append(
                    TraceEntry(
                        event="evidence_crop_metrics",
                        detail={
                            "kept": kept,
                            "dropped": dropped,
                            "retention_ratio": retention_ratio,
                            "reason": "evidence_diversity_cap",
                            "units_max": _evidence_units_max(),
                            "per_doc_max": _evidence_per_doc_max(),
                        },
                    )
                )
            evidence_chars = sum(
                len(str(item.get("excerpt", "")))
                for item in state["collected_evidence"]
            )
            trace.append(
                TraceEntry(
                    event="context_budget",
                    detail={
                        "evidence_units": len(state["collected_evidence"]),
                        "evidence_chars": evidence_chars,
                        "units_limit": _evidence_units_max(),
                        "chars_limit": CONTEXT_BUDGET_CHARS,
                        "used_ratio": round(
                            evidence_chars / CONTEXT_BUDGET_CHARS, 4
                        ),
                    },
                )
            )
        runtime: ControllerRuntime = state["controller"]
        consumed_frontier = tuple(
            path
            for path in outcome.files
            if path in runtime.state.unread_frontier_paths
        )
        if consumed_frontier:
            controller, frontier_read = observe_frontier_read(
                runtime,
                consumed_frontier,
            )
            _publish_control_transition(
                state,
                controller,
                (frontier_read,),
                trace,
            )
            state["frontier_paths"] = list(
                controller.state.unread_frontier_paths
            )
        after_keys = {_evidence_key(r) for r in state["collected_evidence"]}
        if after_keys - before_keys:
            # New evidence was collected: the sufficiency stall is reset.
            state["stall_count"] = 0
        if len(state["collected_evidence"]) < evidence_before:
            trace.append(
                TraceEntry(
                    event="evidence_capped",
                    detail={
                        "dropped": evidence_before - len(state["collected_evidence"]),
                        "kept": len(state["collected_evidence"]),
                    },
                )
            )
        trace.append(
            TraceEntry(
                event="sufficiency_boundary",
                detail={
                    "read_paths": list(outcome.files),
                    "evidence_ids": sorted(
                        {
                            str(item.get("claim_id"))
                            for item in outcome.evidence
                            if item.get("claim_id")
                        }
                    ),
                    "registry_evidence_ids": new_evidence_ids,
                    "active_evidence_ids": list(query_state.active_evidence_ids),
                    "frontier_remaining": sorted(
                        set(state.get("frontier_paths", [])) - set(state["read_pages"])
                    ),
                },
            )
        )
    # A source hit resolves to its published reading-material unit.  A
    # sheet/table page is delivered as row-aligned, citation-bearing views; a
    # document stays on the article planner.  Only actually-delivered row ranges
    # are registered, so a budget-limited read stays continuable instead of
    # marking the whole unit delivered.
    material_candidates: List[tuple] = []  # (view_payload, dedupe_key, spans, handles)
    material_meta: Dict[tuple, Dict[str, Any]] = {}
    material_refs: Dict[tuple, Dict[str, Dict[str, Any]]] = {}
    material_view_payloads: Dict[tuple, List[Dict[str, Any]]] = {}
    material_paths: Dict[str, str] = {}
    table_scope_entries: List[Dict[str, Any]] = []
    material_read_paths = list(paths)
    if outcome.success and state.get("bundle_view") is not None:
        from jonex_wiki.domain.engine.query.read_views import (
            resolve_reading_material,
            reading_view_payload,
            _fully_covered,
        )
        from jonex_wiki.domain.engine.query.read_views import related_table_paths
        material_read_paths = list(dict.fromkeys(list(paths) + (
            related_table_paths(corpus, outcome.evidence, paths)
            if args.get("range") is None and not args.get("cursor") else [])))
        def append_material(path, entry=None):
            try:
                material = resolve_reading_material(state, corpus, path)
                if material is None:
                    return
                unit = material["unit"]
                source_id = str(material["source_id"])
                if unit.get("kind") == "document":
                    # Documents are delivered by the article planner above.
                    # Never also append a duplicate full-document body here, and
                    # keep ``files`` as a compact marker so the old full page
                    # text cannot bypass the planner's range budget.
                    material_paths[path] = "document:{}".format(
                        unit.get("unit_id") or unit.get("document_id") or source_id)
                    return
                dedupe_key = (source_id, unit.get("kind"), unit.get("unit_id"))
                location = unit.get("content_location") or {}
                revision_id = getattr(state.get("bundle_view"), "revision_id", "")
                article_path = location.get("article_path") or path
                requested_entry = {"path": path, **(entry or {})}
                requested_page = corpus.page_index.get(path)
                if requested_page is not None:
                    requested_object = getattr(requested_page, "object_id", None)
                    requested_scope = getattr(requested_page, "scope", None) or {}
                    if requested_object:
                        requested_entry["object_id"] = str(requested_object)
                    if requested_scope.get("sheet_id"):
                        requested_entry["sheet_id"] = str(requested_scope["sheet_id"])
                    if requested_scope.get("table_id"):
                        requested_entry["table_id"] = str(requested_scope["table_id"])
                if dedupe_key in material_view_payloads:
                    # Several search hits can be tables/spans from one XLSX.
                    # They are entry points into one workbook, not permission
                    # to serialize that workbook once per requested path.
                    for existing_payload in material_view_payloads[dedupe_key]:
                        existing_payload.setdefault("entry_points", []).append(
                            requested_entry)
                    material_paths[path] = "{}:{}".format(
                        unit.get("kind"), unit.get("unit_id"))
                    return
                if unit.get("kind") == "workbook":
                    built = _build_workbook_material_views(
                        state, material, path, source_id, unit, article_path,
                        revision_id,
                    )
                else:
                    built = _build_table_material_views(
                        state, material, path, source_id, unit, article_path,
                        revision_id, corpus=corpus,
                    )
                table_scope_entry = {
                    "source_id": source_id,
                    "unit_kind": unit.get("kind"),
                    "unit_id": unit.get("unit_id"),
                    "table_id": unit.get("table_id"),
                    "sheet_id": unit.get("sheet_id"),
                    "article_path": article_path,
                }
                if built is None:
                    # Unreadable / sheet / non-table unit: keep a compact path
                    # marker and expose the table identity so the model can
                    # follow up with table_locate/inspect/table_read.
                    table_id = unit.get("table_id")
                    if table_id:
                        material_paths[path] = "table:{}:inspect:table_inspect(table_id=\"{}\")".format(
                            unit.get("unit_id"), table_id)
                    else:
                        material_paths[path] = "{}:{}:raw-only-no-logical-table".format(
                            unit.get("kind"), unit.get("unit_id"))
                    table_scope_entries.append(table_scope_entry)
                    return
                chunk_views, handles_by_page, built_refs = built
                material_refs[dedupe_key] = built_refs
                delivered_ranges = (
                    state.setdefault("delivered_material_ranges", {})
                    .setdefault(dedupe_key, [])
                )
                material_paths[path] = "{}:{}".format(
                    unit.get("kind"), unit.get("unit_id"))
                selected_level = (
                    chunk_views[0].selected_level
                    if chunk_views else "full_table")
                material_meta[dedupe_key] = {
                    "path": path,
                    "source_id": source_id,
                    "unit_id": unit.get("unit_id"),
                    "unit_kind": unit.get("kind"),
                    "selected_level": selected_level,
                    "body_bytes": sum(
                        len(view.blocks[0]["body"].encode("utf-8"))
                        for view in chunk_views),
                    "article_path": article_path,
                    "page_count": len(chunk_views),
                    "whole_unit": selected_level in {"full_table", "full_workbook"},
                    "table_scope": table_scope_entry,
                }
                serialized_views: List[Dict[str, Any]] = []
                for index, chunk_view in enumerate(chunk_views):
                    chunk_spans = [
                        (int(span[0]), int(span[1]))
                        for span in chunk_view.spans
                        if span and len(span) >= 2 and int(span[1]) > int(span[0])
                    ]
                    if chunk_spans and delivered_ranges and all(
                            _fully_covered(span, delivered_ranges) for span in chunk_spans):
                        # Already delivered by an earlier read; never re-send it.
                        continue
                    view_payload = reading_view_payload(chunk_view)
                    view_payload["entry_points"] = [requested_entry]
                    serialized_views.append(view_payload)
                    material_candidates.append(
                        (view_payload, dedupe_key, chunk_spans,
                         handles_by_page[index]))
                material_view_payloads[dedupe_key] = serialized_views
            except Exception as exc:  # noqa: BLE001 - never break a wiki_read
                trace.append(TraceEntry(
                    event="reading_material_failed",
                    detail={"path": path, "error": str(exc)},
                ))
        for path in material_read_paths:
            append_material(path)
    # QUERY-001: this model-facing flat ``evidence`` list is the exact active
    # citation allowlist at the time of this read.  QUERY.3 separately isolates
    # final synthesis from older tool messages that contained earlier active
    # sets.
    # The outcome cache holds the page metadata, independent of view selection.
    # Repeat detection must also identify the requested article range.
    read_key = (cache_key, tuple(args["range"]) if args.get("range") else None)
    repeat_note = _read_repeat_note(
        state, read_key if cache_key is not None else None, state["total"])
    if repeat_note:
        trace.append(TraceEntry(event="read_repeated", detail={
            "paths": list(paths) if isinstance(paths, list) else paths,
            "of_step": repeat_note["of_step"],
        }))
    compacted_claims = {
        path: [_compact_claim_for_model(c) for c in page_claims]
        for path, page_claims in outcome.claims.items()
    }
    controller_view = controller_payload(state["controller"])
    # A truncated listing of an archived interval hands the model the exact
    # range that reads the rest of that same interval.
    read_evidence, read_echo_note = _bounded_evidence_echo(
        state, controller_view,
        continuations=_source_interval_continuations(state, corpus, paths))
    # For a material-resolved source, ``files`` keeps only path/status; the
    # one full body lives in ``views``.  The old full grid must not ride along
    # in ``files`` and thereby bypass the range budget.
    files = dict(outcome.files)
    for path, marker in material_paths.items():
        files[path] = "material:{}".format(marker)
    read_payload: Dict[str, Any] = {
        "success": outcome.success,
        "files": files,
        "evidence": [{key: value for key, value in item.items() if key != "excerpt"}
                     for item in read_evidence],
        "pagination": outcome.pagination,
        "message": outcome.message,
    }
    table_indexes = sorted({"/".join(path.split("/")[:2]) + "/index.md"
                            for path in material_read_paths if path.startswith("raw-tables/")})
    table_indexes = [path for path in table_indexes if path in corpus.files]
    if table_indexes:
        read_payload["message"] += "\nOriginal source table contents: " + ", ".join(table_indexes)
    # S6.3: a read shows where the page points and why, from the published link
    # index.  Navigation only: nothing here enters ``evidence``, so a link
    # answers nothing until its target is read.
    # R2.1: body and evidence are priced first; navigation rides along only
    # while the payload budget has room, and what was left out is stated.
    link_delivery = _bounded_link_delivery(model, state, messages, read_payload, {
        path: list(getattr(corpus, "page_links", {}).get(path) or ()) for path in files})
    if link_delivery is not None:
        if link_delivery["links"]:
            read_payload["links"] = link_delivery["links"]
        if link_delivery["omitted"]:
            read_payload["links_omitted"] = link_delivery["omitted"]
            read_payload["links_note"] = (
                "Navigation is incomplete: 'links_omitted' counts the published "
                "links this delivery left out. Body and evidence are never given "
                "up for navigation.")
        trace.append(TraceEntry(event="read_link_delivery", detail={
            "published": link_delivery["published"],
            "delivered": link_delivery["delivered"],
            "omitted": link_delivery["omitted_counts"],
            "dropped_for_budget": link_delivery["dropped_for_budget"],
        }))
    if table_scope_entries:
        read_payload["table_scope"] = table_scope_entries
    # Plan the verifiable article delivery with the exact-payload budget
    # preview.  ``base_payload`` already carries the fixed tool fields
    # (files/claims/evidence/controller/table_scope), so the article planner
    # prices the same payload the read tool will send, not a simplified
    # {views:[...]} shell.
    if outcome.success:
        try:
            from jonex_wiki.domain.engine.query.read_views import plan_read, reading_view_payload
            read_preview = _make_read_preview(model, state, messages, read_payload)
            explicit_ranges = None
            range_value = args.get("range")
            if range_value is not None:
                explicit_ranges = {
                    str(paths[0]): (int(range_value[0]), int(range_value[1]))
                }
            read_selection = plan_read(
                state,
                corpus,
                [str(path) for path in paths],
                args.get("cursor"),
                read_preview,
                explicit_ranges=explicit_ranges,
            )
        except Exception as exc:  # pragma: no cover - defensive fallback
            trace.append(TraceEntry(event="read_repair_legacy_fallback",
                                    detail={"error": str(exc)}))
            read_selection = None
    source_marked_files: Dict[str, str] = {}
    if read_selection is not None:
        # Source views keep evidence identities, and replace duplicate content
        # only when every mapped focus for that page is actually covered.
        from jonex_wiki.domain.engine.query.read_views import reading_view_payload
        article_handle_candidates = {}
        seen_claims = {record.claim_id: record.excerpt
                       for record in state["query_state"].seen_evidence_by_id.values()}
        for view in read_selection.views:
            if any(seen_claims.get(str(ref.get("claim_id") or "")) == view.text
                   for ref in view.citation_refs):
                continue
            handles, refs = _article_span_handles(view)
            if handles:
                article_handle_candidates[view.object_key] = (handles, refs)
                view.citation_refs = list(view.citation_refs) + [
                    {"claim_id": handle["claim_id"], "source_id": handle["source_id"]}
                    for handle in handles]
        read_payload["views"] = [
            reading_view_payload(view) for view in read_selection.views
        ]
        if read_selection.navigation:
            read_payload["navigation"] = [dict(item) for item in read_selection.navigation]
        table_navigation = _table_source_navigation(state, corpus, read_selection.views)
        navigation = list(read_payload.get("navigation") or []) + table_navigation
        if table_navigation and _fits_actual_request(model, state, messages,
                {**read_payload, "navigation": navigation}) is True:
            read_payload["navigation"] = navigation
        if read_selection.next_cursor:
            read_payload["next_cursor"] = read_selection.next_cursor
        if read_selection.views:
            from jonex_wiki.domain.engine.query.read_views import (
                _focuses_from_page_sources, _fully_covered, _read_context, resolve_focus,
            )
            for path in paths:
                if str(path).startswith(("sources/", "source-evidence/")):
                    continue
                if str(path).startswith("source-articles/"):
                    focus = resolve_focus(_read_context(state), corpus, str(path))
                    focuses = [focus] if focus is not None and not focus.open_request else []
                else:
                    focuses = _focuses_from_page_sources(_read_context(state), corpus, str(path))
                if focuses and all(
                    all(_fully_covered(span, [selected for view in read_selection.views
                        if view.source_id == focus.source_id and any(
                            str(ref.get("claim_id") or "").startswith("source_read_")
                            for ref in view.citation_refs) for selected in view.spans])
                        for span in focus.spans)
                    for focus in focuses
                ):
                    # Price this same marker/body payload before committing.
                    # Partial/unmapped pages retain their explanatory body.
                    source_marked_files[str(path)] = read_payload["files"][str(path)]
                    read_payload["files"][str(path)] = "material:source-ranges"

    # Reading a packed source unit includes its original, unambiguously
    # associated grid. Reuse the same atomic material admission as other
    # table entry points; explicit ranges/cursors keep their original scope.
    if read_selection is not None and args.get("range") is None and not args.get("cursor"):
        for entry in table_navigation:
            append_material(entry["path"], entry)

    # Admit the material candidates by pricing the EXACT payload that would be
    # serialized, not a simplified {views:[candidate]} shell.  When the whole
    # batch does not fit, shrink the current range (drop the last chunk); never
    # fall back to the old full-text files body.
    payload_preview = _make_material_read_preview(model, state, messages)
    admitted: List[tuple] = []
    dropped: List[tuple] = []
    if material_candidates:
        base_views = read_payload.get("views") or []
        base_table_scope = list(table_scope_entries)
        dropped_scope_entries: List[Dict[str, Any]] = []
        # Group candidates by reading-material unit.  An open whole-table unit
        # is atomic: it is either delivered complete (all pages) or not at all,
        # so a budget-rejected open table never leaks a first-page prefix.
        unit_order: List[tuple] = []
        unit_chunks: Dict[tuple, List[tuple]] = {}
        for chunk in material_candidates:
            key = chunk[1]
            if key not in unit_chunks:
                unit_chunks[key] = []
                unit_order.append(key)
            unit_chunks[key].append(chunk)

        def _unread_ranges(chunks: List[tuple]) -> List[Dict[str, Any]]:
            return [
                {
                    "source_id": key[0],
                    "unit_kind": key[1],
                    "unit_id": key[2],
                    "article_path": view_payload.get("article_path"),
                    "spans": chunk_spans,
                }
                for view_payload, key, chunk_spans, _handles in chunks
            ]

        pending_keys = list(unit_order)
        accepted_payload = None
        while pending_keys:
            trial_payload = dict(read_payload)
            trial_payload["views"] = base_views + [
                chunk[0] for key in pending_keys for chunk in unit_chunks[key]
            ]
            if dropped:
                trial_payload["unread_ranges"] = _unread_ranges(dropped)
            if base_table_scope or dropped_scope_entries:
                trial_payload["table_scope"] = (
                    base_table_scope + dropped_scope_entries)
            if read_echo_note:
                trial_payload["delivery"] = read_echo_note
            if repeat_note:
                trial_payload["repeat"] = repeat_note
            elif cached_hit:
                trial_payload["cached"] = True
            if payload_preview is None or payload_preview(trial_payload):
                admitted = [chunk for key in pending_keys for chunk in unit_chunks[key]]
                accepted_payload = trial_payload
                break
            drop_key = pending_keys[-1]
            pending_keys = pending_keys[:-1]
            for chunk in unit_chunks[drop_key]:
                dropped.append(chunk)
            drop_entry = (material_meta.get(drop_key) or {}).get("table_scope")
            if drop_entry and drop_entry not in dropped_scope_entries:
                dropped_scope_entries.append(drop_entry)
        # A dropped open whole-table unit still needs a locatable follow-up:
        # expose its table identity, but never its raw grid.
        table_scope_entries = base_table_scope + dropped_scope_entries
        if accepted_payload is not None:
            read_payload = accepted_payload
        else:
            # Every material candidate was budget-rejected.  Article views (if
            # any) were already priced and approved by the article planner, so
            # they stay delivered.  A pure table read with no admitted body is
            # a successful *navigation* outcome, never a failed read.
            read_payload["views"] = base_views
            if base_views:
                read_payload["unread_ranges"] = _unread_ranges(dropped)
            if table_scope_entries:
                read_payload["table_scope"] = table_scope_entries
            if read_echo_note:
                read_payload["delivery"] = read_echo_note
            if repeat_note:
                read_payload["repeat"] = repeat_note
            elif cached_hit:
                read_payload["cached"] = True
        # Register delivered ranges and cell citations only after the candidate
        # was approved.
        for _view_payload, dedupe_key, chunk_spans, handles in admitted:
            state.setdefault("read_views", []).append(_view_payload)
            ranges = (state.setdefault("delivered_material_ranges", {})
                      .setdefault(dedupe_key, []))
            for span in chunk_spans:
                if span not in ranges:
                    ranges.append(span)
            if material_refs.get(dedupe_key):
                state.setdefault("table_evidence_refs", {}).update(
                    material_refs[dedupe_key])
            if handles:
                _register_table_handles(
                    state, str(dedupe_key[2]), handles, trace)
            if dedupe_key[1] == "workbook":
                state.setdefault("delivered_workbooks", set()).add(
                    str(dedupe_key[0]))
            elif dedupe_key[1] == "table":
                state.setdefault("delivered_tables", set()).add(str(dedupe_key[2]))
        # Trace exactly what was delivered and what remains unread.
        admitted_by_key: Dict[tuple, int] = {}
        for _view_payload, dedupe_key, _spans, _handles in admitted:
            admitted_by_key[dedupe_key] = admitted_by_key.get(dedupe_key, 0) + 1
        for dedupe_key, meta in material_meta.items():
            count = admitted_by_key.get(dedupe_key, 0)
            if count == 0:
                # A fully budget-rejected unit is not recorded as delivered.
                continue
            trace.append(TraceEntry(
                event="reading_material_delivered",
                detail={
                    **meta,
                    "pages": count,
                    "truncated": meta["page_count"] > count,
                },
            ))
        for view_payload, dedupe_key, dropped_spans, _handles in dropped:
            trace.append(TraceEntry(
                event="reading_material_budget_limited",
                detail={
                    "path": material_meta[dedupe_key]["path"],
                    "source_id": dedupe_key[0],
                    "unit_id": dedupe_key[2],
                    "unit_kind": dedupe_key[1],
                    "page_count": material_meta[dedupe_key]["page_count"],
                    "spans": dropped_spans,
                    "reason": "preview_rejected",
                },
            ))
    else:
        if read_echo_note:
            read_payload["delivery"] = read_echo_note
        if repeat_note:
            read_payload["repeat"] = repeat_note
        elif cached_hit:
            read_payload["cached"] = True
        # Pure document path: price the FINAL whole payload (body views +
        # navigation + cursor + notes) once, not each individual view.
        if read_payload.get("views") and _fits_actual_request(
                model, state, messages, read_payload) is False:
            read_payload["views"] = []
            if read_selection is not None:
                read_selection.views = []
                read_payload["budget_limited"] = True
                read_payload["reason"] = (
                    read_selection.reason or "budget_limited")
            if evidence_snapshot:
                for key, value in evidence_snapshot.items():
                    state[key] = value
                restored_controller_view = controller_payload(
                    state["controller"])
                read_payload["controller"] = restored_controller_view
                read_payload["evidence"], _restored_note = _bounded_evidence_echo(
                    state, restored_controller_view)
            trace.append(TraceEntry(
                event="read_budget_rejected_no_delivery",
                detail={"reason": read_payload.get("reason", "budget_limited"),
                        "pending_ranges": []},
            ))

    # When this read delivered neither a body view, a navigation locator, nor a
    # table candidate, it must not pretend a successful empty read and must not
    # commit its citations.  Roll the citation registration back so a fully
    # rejected read leaves no new evidence/read state behind.
    if (
        read_selection is not None
        and read_selection.is_budget_limited
        and not read_selection.views
        and not read_selection.navigation
        and not admitted
        and not table_scope_entries
    ):
        for key, value in evidence_snapshot.items():
            state[key] = value
        restored_controller_view = controller_payload(state["controller"])
        read_payload["controller"] = restored_controller_view
        read_payload["evidence"], _restored_note = _bounded_evidence_echo(
            state, restored_controller_view)
        read_payload["success"] = False
        first_pending = (read_selection.pending_focuses[0]
                         if read_selection.pending_focuses else None)
        pending_source_id = first_pending.source_id if first_pending else ""
        pending_article_path = (first_pending.object_key[2]
                                if first_pending else "")
        if read_selection.pending_ranges:
            read_payload["unread_ranges"] = [
                {"source_id": pending_source_id,
                 "unit_kind": "document",
                 "unit_id": pending_source_id,
                 "article_path": pending_article_path,
                 "spans": [list(span) for span in read_selection.pending_ranges]}
            ]
        read_payload["budget_limited"] = True
        read_payload["reason"] = read_selection.reason or "budget_limited"
        trace.append(TraceEntry(
            event="read_budget_rejected_no_delivery",
            detail={
                "reason": read_selection.reason or "budget_limited",
                "pending_ranges": [
                    list(span) for span in read_selection.pending_ranges
                ],
            },
        ))

    if source_marked_files and not (
        read_selection is not None and read_selection.views and read_payload.get("success")
    ):
        read_payload["files"].update(source_marked_files)

    # Article views are registered only after the exact serialized payload was
    # approved and is about to be sent.
    if read_selection is not None and read_selection.views and read_payload.get("success"):
        for view in read_selection.views:
            candidate = article_handle_candidates.get(view.object_key)
            if candidate:
                handles, refs = candidate
                state.setdefault("table_evidence_refs", {}).update(refs)
                _register_table_handles(state, view.article_path, handles, trace)
        _register_read_views(state, read_selection.views)
        if read_selection.cursor_queue and read_selection.cursor_token:
            state.setdefault("read_cursors", {})[read_selection.cursor_token] = (
                list(read_selection.cursor_queue)
            )
        trace.append(TraceEntry(
            event="read_views_selected",
            detail={
                "view_count": len(read_selection.views),
                "covered_focuses": [
                    list(span) for span in read_selection.covered_focuses
                ],
                "pending_ranges": [
                    list(span) for span in read_selection.pending_ranges
                ],
                "next_cursor": read_selection.next_cursor,
                "budget_limited": read_selection.is_budget_limited,
                "reason": read_selection.reason,
            },
        ))

    # All admitted views count as source reads, including direct table entry
    # points. Body admission precedes navigation, so a source index never
    # displaces an original table or source range.
    _append_source_section_index(read_payload, state, corpus, model, messages)

    # Bind once, after both table and article references are registered.
    # An earlier pass drops pending article refs; a second pass then drops
    # already-bound table refs because they no longer carry claim_id.
    _bind_payload_citation_handles(state, read_payload)

    from jonex_wiki.domain.engine.compile.coverage_diagnostic import question_terms
    terms = question_terms(state.get("original_question", "") or state.get("question", ""))
    # Observe the actual final payload, excluding navigation and the controller.
    observed = json.dumps({key: read_payload.get(key) for key in ("views", "evidence", "claims")}, ensure_ascii=False).casefold()
    trace.append(TraceEntry(event="coverage_delivery_probe", detail={
        "terms": terms, "matched": bool(terms) and all(term in observed for term in terms)}))
    content = json.dumps(read_payload, ensure_ascii=False, separators=(",", ":"))
    messages.append(_tool_message(call_id, "wiki_read", content))
    trace.append(
        TraceEntry(
            event="tool_result",
            detail={
                "tool": "wiki_read",
                "paths": list(paths) if isinstance(paths, list) else paths,
                "success": outcome.success,
                "count": len(outcome.files),
                "cached": cached_hit,
            },
        )
    )
    if spans is not None:
        duration_ms = int((time.monotonic() - started_mono) * 1000)
        tool_span_id = spans.add(
            type_=TraceSpanType.TOOL,
            title="{} completed".format(name),
            tool=name,
            category=category,
            started_at=_utc_now_iso(started_dt),
            duration_ms=duration_ms,
            input_summary="paths={}".format(len(paths)),
            result_summary="success={}; files={}".format(outcome.success, len(outcome.files)),
            progress_details=tool_progress_details(args, pages=(
                {"path": path, "title": getattr(corpus.page_index.get(path), "title", path)}
                for path in outcome.files), views=read_payload.get("views") or ()),
            decision="execute",
            event_type=QueryEventType.TOOL_COMPLETED.value,
            parent_id=parent_span_id,
        )
        if outcome.success and outcome.evidence:
            spans.add(
                type_=TraceSpanType.EVIDENCE,
                title="evidence added",
                started_at=_utc_now_iso(),
                evidence=[dict(e) for e in outcome.evidence[:5]],
                decision="n/a",
                event_type=QueryEventType.EVIDENCE_ADDED.value,
                parent_id=tool_span_id,
            )
    # LWP-RET-002 chapter_tree route: code-forced bounded navigation along the
    # render-tree chain index.md -> <dir>/index.md -> chapter pages.  Each
    # forced step is itself a tool call (bounded by CHAPTER_NAV_BUDGET per
    # step and the run tool cap), so structural questions reach chapter pages
    # without depending on the model to walk the tree by hand.
    if (
        outcome.success
        and getattr(corpus, "route", None) == "chapter_tree"
        and state["total"] < state["max_tool_calls"]
    ):
        nav_paths = _chapter_nav_next(corpus, state, set(outcome.files))
        if nav_paths:
            trace.append(
                TraceEntry(
                    event="chapter_forced_navigation",
                    detail={
                        "paths": nav_paths,
                        "from": sorted(outcome.files),
                        "page_numbers": [
                            corpus.page_number_map.get(p) for p in nav_paths
                        ],
                    },
                )
            )
            await _exec_tool(
                corpus, fts, "wiki_read", {"paths": nav_paths}, None,
                state, messages, trace,
                model=model, routing=routing,
                spans=spans, usage_acc=usage_acc, parent_span_id=parent_span_id,
            )
            return


async def _decompose_question(
    model: ModelPort,
    question: str,
    routing: Dict[str, Any],
    spans: Optional[_SpanBuilder],
    usage_acc: Dict[str, int],
    model_name: Optional[str],
) -> tuple[List[str], bool]:
    """Decompose a question into directions plus a dependency-chain marker.

    Exactly one bounded model call (tool-free, small ``max_tokens``). Returns an
    empty plan when the output is malformed, so the caller uses the original
    single question.
    """
    start_dt = datetime.now(timezone.utc)
    start_mono = time.monotonic()
    raw = await _model_complete(
        model,
        _decompose_messages(question),
        SubQuestionList.model_json_schema(),
        max_tokens=512,
        tools=None,
        thinking={"type": "disabled"},
        scene="jonex_wiki_query_decompose",
        **routing,
    )
    if spans is not None and usage_acc is not None:
        _record_model_span(
            spans, raw, model_name, start_dt,
            int((time.monotonic() - start_mono) * 1000),
            usage_acc,
            title="query decomposition",
        )
    try:
        parsed = SubQuestionList.model_validate(raw)
    except Exception:
        return [], False
    sub_questions: List[str] = []
    seen = set()
    for item in parsed.sub_questions:
        text = item.strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        sub_questions.append(text)
        if len(sub_questions) >= MAX_DECOMPOSED_QUERIES:
            break
    return sub_questions, bool(parsed.requires_evidence_bridge)


def _fact_candidates(corpus: Any, query: str) -> List[Dict[str, Any]]:
    from jonex_wiki.domain.engine.compile.identity import normalize_name
    folded = normalize_name(query)
    candidates = []
    for name, paths in getattr(corpus, "fact_mentions", {}).items():
        if not name or name not in folded:
            continue
        for path in paths:
            if path in corpus.page_index:
                candidates.append({"path": path, "mention": name,
                    "why_visit": "source contribution mentions this name; read the page to verify identity and evidence"})
                if len(candidates) >= 8:
                    return candidates
    return candidates


CANDIDATE_OBSERVATION_LIMIT = 8
CANDIDATE_LEDGER_LIMIT = 32


def _search_progress(*, hit_paths: Sequence[str], read_pages: Any,
                     candidate_unread: Sequence[str], stall_count: int,
                     stall_sensitive: bool) -> Dict[str, Any]:
    """The counters one search advances, as a pure decision (R1.1).

    A search that exposed accessible-but-unread material — a lexical hit or a
    fact mention — is progress: it clears the empty streak and the sufficiency
    stall.  Only a search with nothing to visit at all is empty, and that case
    leaves the empty streak to the rewrite/fallback path that owns it.
    """
    if hit_paths or candidate_unread:
        new_pages = [path for path in hit_paths if path not in (read_pages or ())]
        fresh = bool(new_pages or candidate_unread)
        return {
            "empty_streak": 0,
            "stall_count": 0 if (fresh or not stall_sensitive) else stall_count + 1,
            "unread_paths": new_pages or list(candidate_unread),
        }
    return {"empty_streak": None, "stall_count": stall_count, "unread_paths": []}


def _candidate_observations(corpus: Any, query: str, hits: Sequence[Any],
                            state: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """One ordered, deduplicated frontier of accessible-but-unread material.

    Search results keep their ranked order and fact mentions follow in theirs;
    the merge never reorders either list, and neither kind is registered as
    evidence (no handle, no excerpt).
    """
    observations: List[Dict[str, Any]] = []
    seen: set = set()

    def add(path: str, origin: str, **extra: Any) -> None:
        if not path or path in seen or len(observations) >= CANDIDATE_OBSERVATION_LIMIT:
            return
        seen.add(path)
        item: Dict[str, Any] = {"path": path, "origin": origin,
                                "read": path in (state.get("read_pages") or set())}
        item.update({key: value for key, value in extra.items() if value is not None})
        observations.append(item)

    for position, hit in enumerate(hits or (), start=1):
        add(hit.path, "search", rank=getattr(hit, "rank", None), position=position)
    for item in _fact_candidates(corpus, query):
        add(str(item.get("path") or ""), "fact_mention", mention=item.get("mention"))
    return observations


def _record_candidate_observations(state: Dict[str, Any],
                                   observations: Sequence[Mapping[str, Any]]) -> None:
    """Remember the frontier for the bounded gap correction (order preserved)."""
    existing = list(state.get("candidate_observations") or [])
    seen = {str((item or {}).get("path") or "") for item in existing}
    for item in observations:
        path = str(item.get("path") or "")
        if not path or path in seen:
            continue
        seen.add(path)
        existing.append(dict(item))
    state["candidate_observations"] = existing[:CANDIDATE_LEDGER_LIMIT]
