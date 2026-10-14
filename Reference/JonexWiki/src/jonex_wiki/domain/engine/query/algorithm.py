"""AC-008 query traversal and citation invariants.

This module is the smallest trustworthy query core over an already accepted
``CompilerState``.  The code, rather than prompt wording, must force the first
traversal action, require a successful Wiki read before answering, and retain
only citations that exactly match evidence exposed by those reads.

Frozen behaviour fixed here:

- ``build_query_corpus`` re-runs the deterministic evidence/support gate,
  rejects stale or hand-constructed decisions, and renders a detached
  query-facing tree whose prose contains only ``SUPPORTED`` claim text;
- an in-memory SQLite FTS5 index over knowledge pages only ranks by BM25 column
  weights 8, 6, 4, 3, 1 (title, aliases, tags, description, body); there is no
  ``LIKE`` or other fallback;
- traversal forces the first action in code, counts actual executed tool calls,
  records a legal assistant ``tool_calls`` message before every tool result,
  rejects final answers until a successful read, stops after three consecutive
  empty searches or at the 15-call limit, and retains only exact-match
  citations;
- the model is injected through ``ModelPort`` and its tool responses are parsed
  from the real OpenAI tool-call envelope (``id``/``type``/``function`` with a
  JSON-string ``arguments``); malformed output fails closed.

The module is pure with respect to the environment: only ``ModelPort`` is
called.  No provider, network, filesystem, storage or database access happens
here.  CR-TRACE-002 adds a real wall-clock measurement for span timing
(``time.monotonic`` / ``datetime``) — the only environment access is the
system clock, used solely to emit truthful ``started_at``/``duration_ms``
span fields; never randomness, UUIDs, or secrets.

Wave 3 P1 adds two bounded retrieval-efficiency steps.  ``P1.1`` decomposes a
composite question once (one tool-free model call, disabled via
``QUERY_QUERY_DECOMPOSE=off``) into self-contained sub-questions that are each
searched before the traversal loop; single questions skip the call at zero
cost.  ``P1.2`` runs at most one deterministic query rewrite after an empty
search (``_rewrite_query_for_search``) and only counts the empty search toward
the three-empty stop when the rewritten query is also empty.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Mapping, Optional, Sequence, Tuple

from pydantic import BaseModel, Field, ValidationError, model_validator, field_validator, ConfigDict

from jonex_wiki.kernel.contracts import (
    QueryEventType,
    QueryStrategy,
    TraceSpan,
    TraceSpanType,
)
from jonex_wiki.kernel.runtime_env import current_env

from jonex_wiki.domain.engine.util.model_io import (
    _query_thinking,
    _SpanBuilder,
    _finalize_token_usage,
    _merge_usage,
    _model_complete,
    _record_model_span,
    _utc_now_iso,
)

from jonex_wiki.domain.engine.models.query_models import (
    CitationDraft,
    CitationExtraction,
    FinalDraft,
    QueryCorpus,
    RelationCheck,
    RerankResponse,
    SearchHit,
    SubQuestionList,
    TraceEntry,
    VerifiedCitation,
)

from jonex_wiki.domain.engine.query.synthesis_orchestration import (
    EXTRACTION_ANSWER_LIMIT,
    ENTITY_ANSWER_VERBOSE_LIMIT,
    _active_request_views,
    _call_clean_synthesis,
    _citation_matches,
    _compact_entity_answer,
    _deterministic_citations_from_answer,
    _entity_answer_verbose_limit,
    _extractive_answer_from_evidence,
    _finish_controller_with_citations,
    _resolve_citations,
    _resolve_evidence_handles,
    _resolve_synthesis_response,
    _synthesize_active_evidence,
)
from jonex_wiki.domain.engine.query.numeric_consistency import (
    ComputedClaimRef,
    computed_answer_conflict,
    computed_claim_without_program,
)
from jonex_wiki.domain.engine.query.table_query import build_active_request_views, computed_count_conflict

from jonex_wiki.domain.engine.query.traversal_executor import (
    EVIDENCE_STALL_LIMIT,
    MALFORMED_TOOL_CALL,
    MAX_DECOMPOSED_QUERIES,
    MAX_TOOL_CALLS,
    QUERY_ANSWER_STYLE_ENV,
    QUERY_ANSWER_TARGET_HINT_ENV,
    QUERY_DECOMPOSE_ENV,
    QUERY_TOOLS,
    SEARCH_READ_FORCE_LIMIT,
    TRAVERSAL_MAX_TOKENS_ENV,
    _DECOMPOSE_SYSTEM_INSTRUCTION,
    _QUESTION_TARGET_RE,
    _ReadOutcome,
    _STOP_ANSWERED,
    _STOP_CONTROL_SLOT,
    _STOP_EMPTY_MODEL,
    _STOP_EMPTY_SEARCH,
    _STOP_EVIDENCE_BUDGET,
    _STOP_EVIDENCE_SUFFICIENT,
    _STOP_NO_PROGRESS,
    _STOP_NO_READ,
    _STOP_TOOL_LIMIT,
    _SYSTEM_INSTRUCTION,
    _SYSTEM_INSTRUCTION_NATURAL,
    _UNKNOWN_QUERY_TOOL,
    _WIKI_READ_ARGS_KEYS,
    _WIKI_READ_PATHS_NOT_STRING_LIST,
    _WIKI_SEARCH_ARGS_KEYS,
    _WIKI_SEARCH_QUERY_NOT_STRING,
    _answer_style_natural,
    _answer_target_hint,
    _answer_target_hint_enabled,
    _assistant_tool_message,
    _decompose_messages,
    _decompose_question,
    _decomposition_enabled,
    _evidence_stall_limit,
    _execute_read,
    _exec_tool,
    _max_tool_calls_limit,
    _publish_control_transition,
    _publish_incomplete_terminal,
    _recompose_consumed_history,
    _search_read_force_limit,
    _system_message,
    _tool_message,
    _trace_control_events,
    _traversal_max_tokens,
    _user_message,
    _validate_read_path,
    _validate_tool_arguments,
)

from jonex_wiki.domain.engine.query.traversal_evidence import (
    EVIDENCE_COMPACT_FIELDS,
    EVIDENCE_EXCERPT_LIMIT,
    EVIDENCE_PER_DOC_MAX,
    EVIDENCE_UNITS_MAX,
    MAX_ACTIVE_EVIDENCE,
    MAX_ACTIVE_EVIDENCE_ENV,
    QUERY_EVIDENCE_PER_DOC_ENV,
    QUERY_EVIDENCE_UNITS_MAX_ENV,
    _activate_new_evidence,
    _active_evidence_payload,
    _cap_evidence_diversity,
    _compact_claim_for_model,
    _compact_evidence_record,
    _evidence_excerpt_limit,
    _evidence_key,
    _evidence_per_doc_max,
    _evidence_records,
    _evidence_units_max,
    _max_active_evidence,
    _normalize_excerpt,
    _record_query_state_event,
)

from jonex_wiki.domain.engine.query.retrieval import (DIVERSITY_RERANK_ALPHA, FTS5_UNAVAILABLE, LLM_RERANK_KEEP, LLM_RERANK_POOL, MAX_SEARCH_HITS, MAX_SEARCH_HITS_ENV, PASSAGE_MAX_CHARS, QUERY_DECOMPOSE_MULTIHOP_ENV, QUERY_DIVERSITY_RERANK_ENV, QUERY_LLM_RERANK_ENV, QUERY_SNIPPET_ENV, _COMPOSITE_COMPARATIVES, _COMPOSITE_CONJUNCTIONS, _COMPOSITE_QUESTION_WORDS, _MULTIHOP_ENTITY_OF, _QUERY_STOPWORDS, _TOKEN_RE, _build_fts_index, _claim_hits, _diversity_rerank, _diversity_rerank_enabled, _env_int, _fts_query, _fts_search, _hit_tokens, _is_composite_question, _is_multihop_single_question, _llm_rerank, _llm_rerank_enabled, _make_snippet, _max_search_hits, _multihop_decomposition_enabled, _open_fts_index, _passage_hits, _rerank_messages, _rewrite_query_for_search, _search_terms, _snippet_enabled, _split_passages, build_fts_index_bytes, build_query_corpus)

# ──────────────────────────────────────────────
#  Legacy patchable surface (lwp-arch-001 QA-1)
# ──────────────────────────────────────────────
# These knobs stay defined on this module so pre-split tests that patch them
# through ``query_algorithm`` keep working; the moved retrieval module reads
# them back through call-time accessors.
# Legacy WAVE5_EMB_CACHE knob removed (§15); fixed default directory.
LLM_RERANK_MIN_CANDIDATES = 6

# Stable checksum over the query tool surface; feeds EffectiveRuntimeSnapshot.
TOOL_CHECKSUM = hashlib.sha256(
    json.dumps(QUERY_TOOLS, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()


def _segmenter_available() -> bool:
    """True; jieba is a hard dependency in dev (no optional-degrade path).

    The import is still performed per call so tests can patch this function to
    simulate a jieba-less environment; a genuinely missing jieba now raises
    loudly instead of silently degrading CJK search.
    """
    import jieba  # noqa: F401

    return True


def _segment_for_search(text: str) -> str:
    """Segment mixed CJK/English text into space-joined FTS5 terms via jieba.

    Both the index side (``_build_fts_index``) and the query side
    (``_fts_query`` / ``_rewrite_query_for_search``) must tokenize with the
    *same* segmenter — FTS5 only matches a query term that equals an indexed
    token.  ``unicode61`` merges a whole CJK run into one long token, so a
    single-character query (the old ``_TOKEN_RE`` strategy) can never match it;
    jieba makes both sides agree on word boundaries.  English/alphanumeric runs
    pass through unchanged, so English behaviour does not regress.  Falls back
    to the same character/alphanumeric tokenization used by queries.
    """
    try:
        import jieba
        return " ".join(
            p for p in (w.strip() for w in jieba.cut(text)) if p
        )
    except Exception:
        return " ".join(_TOKEN_RE.findall(text.casefold()))

logger = logging.getLogger(__name__)
from jonex_wiki.kernel.ports import CancelPort, ModelPort
from jonex_wiki.kernel.telemetry import (
    current_runtime_telemetry,
    telemetry_stage,
)
from jonex_wiki.domain.engine.util.tool_text import is_xml_tool_call_text
from jonex_wiki.domain.engine.models.compiler_state import (
    CompilerState,
    PageIndexEntry,
    WikiPageSnapshot,
    canonical_wiki_path,
)
from jonex_wiki.domain.engine.compile.content_validation import (
    ClaimDraft,
    ValidatedClaim,
    normalize_evidence_text,
    validate_claim_evidence,
)
from jonex_wiki.domain.engine.query.answer_binding import (
    MAX_ANSWER_BINDINGS,
    MAX_UNVERIFIED_MARKS,
    AnswerBinding,
    verify_answer_bindings,
)
from jonex_wiki.domain.engine.query.controller.controller import (
    BridgeBindingDraft,
    ControllerRuntime,
    answer_binding_events,
    apply_bridge_binding,
    controller_payload,
    finish_answered,
    finish_incomplete,
    observe_frontier_read,
    observe_search_progress,
    start_controller,
    sync_evidence_window,
)
from jonex_wiki.domain.engine.query.controller.control_state import ControlEvent, ControlState, FailureClass
from jonex_wiki.domain.engine.query.controller.failure import (
    FailureDecision,
    RuntimeFailureSignals,
    classify_runtime_failure,
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
    SynthesisStarted,
    initial_query_state,
    reduce_query_state,
)
from jonex_wiki.domain.engine.query.controller.synthesis import (
    HandleFinalDraft,
    HandleResolution,
    SynthesisBoundary,
    build_handle_correction_messages,
    build_synthesis_boundary,
    resolve_handle_draft,
)
from jonex_wiki.domain.engine.compile.rendering import render_wiki_tree


# --- Pre-request input estimate ------------------------------------------
# Provider usage is the authority for what a call cost; this estimate only
# decides whether a call may be sent at all, and it is replaced by the real
# usage as soon as the provider reports one.
#
# Method and evidence: the persisted provider requests of the checkout were
# re-priced offline against their own `usage.prompt_tokens`.  The model reads
# between ~0.23 and ~0.33 prompt tokens per byte of the JSON payload the
# transport sends (Chinese prose and JSON table results included); the
# calibration split peak is rounded up to 0.347 units per byte.  A tokenizer
# matching the served model is not available in this environment, so no
# unverifiable tokenizer is claimed.
BUDGET_ESTIMATE_METHOD = "bytes_calibrated_v2"
BUDGET_ESTIMATE_VERSION = "2.0.0"
BUDGET_UNITS_PER_BYTE = 0.347
# The rate is a property of the served model's tokenizer, so it applies to the
# model it was calibrated on and to nothing else.  The fallback prices one unit
# per byte - the strict byte bound this estimator replaced - because a false
# refusal is honest while a silent under-count is not.
#
# 模型名唯一来源是环境变量：``JONEX_WIKI_BUDGET_CALIBRATED_MODELS``（逗号分隔）
# 显式声明标定过的模型；未设置时回退到当前服务的模型（``LLM_MODEL`` →
# ``DEFAULT_LLM_MODEL``）—— 与历史上写死部署模型等价，但源码里不再有字面量。
#
# ⚠️ 换模型时请**显式设置**该变量：速率是 tokenizer 的属性，沿用旧标定会静默少算，
#    而少算比误拒危险（见上面注释）。
BUDGET_CALIBRATED_MODELS_ENV = "JONEX_WIKI_BUDGET_CALIBRATED_MODELS"


def _budget_calibrated_models() -> Tuple[str, ...]:
    """标定过单位字节速率的模型清单（显式变量优先，否则取当前服务的模型）。"""
    explicit = os.getenv(BUDGET_CALIBRATED_MODELS_ENV, "")
    models = tuple(m.strip() for m in explicit.split(",") if m.strip())
    if models:
        return models
    served = os.getenv("LLM_MODEL", "").strip() or os.getenv("DEFAULT_LLM_MODEL", "").strip()
    # 一个都取不到时留空：unknown-model 档（1.0 单价）是保守方向，不会少算预算。
    return (served,) if served else ()


BUDGET_CALIBRATED_MODELS: Tuple[str, ...] = _budget_calibrated_models()
BUDGET_UNITS_PER_BYTE_UNKNOWN_MODEL = 1.0
# A call needs room to answer even after a run of very short completions.
BUDGET_OUTPUT_PLAN_FLOOR = 512
# The query transport sends tools OR a schema-as-system-message, never both.
SCHEMA_INSTRUCTION_PREFIX = (
    "Return only one JSON object valid against this JSON Schema: ")

# Calls/time and per-request context are the default guards. Explicit legacy
# cumulative allowances remain recorded for reproducible historical experiments.
QUERY_MAX_LOGICAL_CALLS_ENV = "QUERY_MAX_LOGICAL_CALLS"
QUERY_MAX_TOTAL_TOKENS_ENV = "QUERY_MAX_TOTAL_TOKENS"
QUERY_MAX_SECONDS_ENV = "QUERY_MAX_SECONDS"


def _priced_payload_bytes(messages: Any, schema: Any, tools: Any) -> int:
    """UTF-8 bytes of the content the model reads for one request."""
    parts = [_compact_json(messages)]
    if tools:
        parts.append(_compact_json(tools))
    elif schema is not None:
        parts.append(SCHEMA_INSTRUCTION_PREFIX)
        parts.append(json.dumps(schema, ensure_ascii=False, separators=(",", ":"),
                                default=str))
    return sum(len(part.encode("utf-8")) for part in parts)


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def _provider_usage(raw: Any) -> Optional[Dict[str, int]]:
    """Extract prompt/completion tokens from a usage mapping or object.

    Returns ``None`` when the provider reported nothing usable (missing,
    negative, boolean or partially filled fields).  Callers must treat that as
    unknown consumption, never as zero.
    """
    if raw is None:
        return None
    extracted: Dict[str, int] = {}
    for name, aliases in (("prompt_tokens", ("prompt_tokens", "input_tokens")),
                          ("completion_tokens", ("completion_tokens", "output_tokens"))):
        value = None
        for alias in aliases:
            if isinstance(raw, dict):
                value = raw.get(alias)
            else:
                value = getattr(raw, alias, None)
            if value is not None:
                break
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        extracted[name] = value
    return extracted


def _query_budget_limits() -> Dict[str, Any]:
    """Default calls/time guards and a per-request context allowance.

    Usage is unlimited by default. An explicit positive legacy allowance
    remains available for reproducing frozen experiments; 0 disables it.
    """
    return {
        "max_calls": _env_int(QUERY_MAX_LOGICAL_CALLS_ENV, 24),
        "max_tokens": (_env_int(QUERY_MAX_TOTAL_TOKENS_ENV, 360000)
                       if current_env().get(QUERY_MAX_TOTAL_TOKENS_ENV, "0").strip() not in ("", "0")
                       else None),
        "max_context_tokens": _env_int("QUERY_MAX_CONTEXT_TOKENS", 400000),
        "max_seconds": _env_int(QUERY_MAX_SECONDS_ENV, 180),
    }


class _BudgetedQueryModel:
    """Hard provider-call, token and wall-clock boundary for one query.

    Reported provider usage and conservative estimates are tracked separately,
    so a missing usage report can never be recorded as zero consumption.  The
    running budget is their sum.  The estimate charged before a call is
    replaced by that call's reported usage afterwards, instead of being
    counted twice, and an honest overrun is recorded once.
    """

    def __init__(self, inner: ModelPort, *, max_calls: int = 24,
                 max_tokens: Optional[int] = None, max_seconds: int = 180,
                 max_context_tokens: int = 400000,
                 model_name: Optional[str] = None) -> None:
        self.inner = inner
        self.model_name = model_name or getattr(inner, "model_name", None) or ""
        self.max_calls = max_calls
        self.max_tokens = max_tokens
        self.max_context_tokens = max_context_tokens
        self.max_seconds = max_seconds
        self.started = time.monotonic()
        self.calls = 0
        self.reported_tokens = 0
        self.estimated_tokens = 0
        self.budget_overrun_tokens = 0
        self.usage_reported_calls = 0
        self.usage_estimated_calls = 0
        self.last_usage_source: Optional[str] = None
        self.pending_estimate = 0
        self.pending_entry: Optional[Dict[str, Any]] = None
        self.exhausted_reason: Optional[str] = None
        self.overrun_recorded = False
        self.call_accounting: List[Dict[str, Any]] = []

    @property
    def tokens(self) -> int:
        """Budget units consumed: reported usage plus conservative estimates."""
        return self.reported_tokens + self.estimated_tokens

    @property
    def usage_source(self) -> Optional[str]:
        if not self.calls:
            return None
        if self.usage_reported_calls and self.usage_estimated_calls:
            return "mixed"
        return "reported" if self.usage_reported_calls else "estimated"

    @property
    def reported_usage(self) -> Dict[str, int]:
        """All provider-reported tokens, including calls whose output failed."""
        return {
            "input_tokens": sum(entry["effective_prompt_tokens"] for entry in self.call_accounting),
            "output_tokens": sum(entry["effective_completion_tokens"] for entry in self.call_accounting),
        }

    def units_per_byte(self) -> float:
        """The byte rate in force for the model this run actually calls."""
        if self.model_name in BUDGET_CALIBRATED_MODELS:
            return BUDGET_UNITS_PER_BYTE
        return BUDGET_UNITS_PER_BYTE_UNKNOWN_MODEL

    def _estimate_input_units(self, messages: Any, schema: Any, tools: Any) -> int:
        """Priced units for the content this request would send.

        The result is a budget unit derived from a calibrated byte rate, not a
        claim about the provider's own accounting.  An unknown or uncalibrated
        model falls back to the strict byte bound rather than borrowing a rate
        that was measured on another tokenizer.
        """
        payload_bytes = _priced_payload_bytes(messages, schema, tools)
        return int(math.ceil(payload_bytes * self.units_per_byte()))

    @staticmethod
    def _part_bytes(value: Any) -> int:
        if value is None:
            return 0
        return len(_compact_json(value).encode("utf-8"))

    def planned_output_units(self, requested_output: int) -> int:
        """How much output this call is expected to produce."""
        ceiling = max(0, int(requested_output or 0))
        observed = [entry.get("effective_completion_tokens") or 0
                    for entry in self.call_accounting
                    if entry.get("effective_usage_source") == "reported"]
        if not observed:
            return ceiling
        return min(ceiling, max(BUDGET_OUTPUT_PLAN_FLOOR, 2 * max(observed)))

    def estimate_units(self, messages: Any, schema: Any, tools: Any,
                       requested_output: int) -> int:
        return (self._estimate_input_units(messages, schema, tools)
                + self.planned_output_units(requested_output))

    def context_messages(self, messages: Any, schema: Any, tools: Any,
                         requested_output: int) -> list:
        prepared = list(messages or [])
        if not isinstance(schema, dict) or schema.get("title") != "TraversalResponse":
            return prepared
        # Remove only whole old conversation turns. Never split an assistant
        # tool-call batch from its tool outputs, or remove the current question.
        while (self._estimate_input_units(prepared, schema, tools)
               + max(0, int(requested_output or 0)) > self.max_context_tokens):
            turns = [i for i, msg in enumerate(prepared)
                     if i >= 2 and msg.get("role") == "assistant"]
            if len(turns) < 2:
                break
            del prepared[turns[0]:turns[1]]
        return prepared

    def fits_context(self, messages: Any, schema: Any, tools: Any,
                     requested_output: int) -> bool:
        return (self._estimate_input_units(
                    self.context_messages(messages, schema, tools, requested_output), schema, tools)
                + max(0, int(requested_output or 0)) <= self.max_context_tokens)

    def fits_request(self, messages: Any, schema: Any, tools: Any,
                     requested_output: int) -> bool:
        """Preview the same remaining allowance complete() will enforce."""
        if (self.exhausted_reason or self.calls >= self.max_calls
                or time.monotonic() - self.started >= self.max_seconds):
            return False
        prepared = self.context_messages(messages, schema, tools, requested_output)
        return (self.fits_context(prepared, schema, tools, requested_output)
                and (self.max_tokens is None or self.tokens + self.estimate_units(
                    prepared, schema, tools, requested_output) <= self.max_tokens))

    def fits_retrieval_and_answer(self, messages: Any, schema: Any, tools: Any,
                                  requested_output: int, answer_messages: Any,
                                  answer_output: int = 8192, answer_schema: Any = None) -> bool:
        """Admit another retrieval turn only while one answer turn still fits.

        Use the existing estimator and ceilings, without increasing the budget
        or claiming to predict the next tool's unknown payload. The next loop
        checks the actual delivered payload again before spending another turn.
        """
        if self.calls + 2 > self.max_calls:
            return False
        if not self.fits_request(messages, schema, tools, requested_output):
            return False
        answer_schema = schema if answer_schema is None else answer_schema
        if not self.fits_request(answer_messages, answer_schema, None, answer_output):
            return False
        retrieval = self.context_messages(messages, schema, tools, requested_output)
        answer = self.context_messages(answer_messages, answer_schema, None, answer_output)
        # Short traversal replies must not shrink the final answer reservation.
        return (self.max_tokens is None or self.tokens
                + self.estimate_units(retrieval, schema, tools, requested_output)
                + self._estimate_input_units(answer, answer_schema, None)
                + max(0, int(answer_output))
                <= self.max_tokens)

    def _account(self, messages: Any, schema: Any, tools: Any, requested_output: int,
                 estimate: int) -> Dict[str, Any]:
        dump = current_env().get("POC_BUDGET_MESSAGE_DUMP")
        if dump:
            # Development diagnostic: the exact messages a bounded call would
            # have sent, so the accounting can be checked against real bytes.
            with open(Path(dump).expanduser(), "a", encoding="utf-8") as stream:
                stream.write(json.dumps({"logical_call": self.calls + 1,
                                         "messages": messages}, ensure_ascii=False) + "\n")
        input_estimate = self._estimate_input_units(messages, schema, tools)
        output_reserve = max(0, int(requested_output or 0))
        planned_output = self.planned_output_units(requested_output)
        entry = {
            "logical_call": self.calls + 1,
            "message_bytes": self._part_bytes(messages),
            "schema_bytes": self._part_bytes(schema),
            "tools_bytes": self._part_bytes(tools),
            "priced_payload_bytes": _priced_payload_bytes(messages, schema, tools),
            "schema_sent_as_message": not tools and schema is not None,
            "requested_output": output_reserve,
            "input_estimate_units": input_estimate,
            "output_reserve_units": output_reserve,
            "planned_output_units": planned_output,
            "estimate_units": estimate,
            "estimate_method": BUDGET_ESTIMATE_METHOD,
            "estimate_version": BUDGET_ESTIMATE_VERSION,
            "tokens_before": self.tokens,
            "accepted": self.exhausted_reason is None,
            "usage_source": self.last_usage_source,
            "effective_prompt_tokens": 0,
            "effective_completion_tokens": 0,
            "effective_usage_source": "not_reported",
            "estimate_error_units": None,
        }
        self.call_accounting.append(entry)
        return entry

    def _settle(self, entry: Optional[Dict[str, Any]], usage: Optional[Dict[str, Any]]) -> None:
        """Replace this call's estimate with what the provider actually billed."""
        if entry is None:
            return
        if usage is None:
            entry["effective_usage_source"] = "missing"
            entry["estimate_error_units"] = None
            return
        entry["effective_prompt_tokens"] = usage["prompt_tokens"]
        entry["effective_completion_tokens"] = usage["completion_tokens"]
        entry["effective_usage_source"] = "reported"
        entry["estimate_error_units"] = (
            entry["estimate_units"]
            - usage["prompt_tokens"] - usage["completion_tokens"])

    def _fallback(self, schema: Any) -> Dict[str, Any]:
        title = schema.get("title", "") if isinstance(schema, dict) else ""
        if "RelationCheck" in title:
            return {"status": "insufficient", "reason": "query budget exhausted"}
        if "SubQuestionList" in title:
            return {"sub_questions": [], "requires_evidence_bridge": False}
        if "Gap" in title:
            return {"sufficient": False, "gaps": []}
        if "CitationExtraction" in title:
            return {"citations": []}
        if "Draft" in title:
            return {"answer": "", "citations": []}
        return {}

    def _set_exhausted(self, reason: str) -> None:
        """Keep the first limit that closed the budget as the run's reason."""
        if self.exhausted_reason is None:
            self.exhausted_reason = reason

    def _record_overrun(self) -> None:
        if self.max_tokens is not None and self.tokens >= self.max_tokens:
            self._set_exhausted("max_total_tokens")
            if self.tokens > self.max_tokens and not self.overrun_recorded:
                self.budget_overrun_tokens = self.tokens - self.max_tokens
                self.overrun_recorded = True

    async def complete(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        schema = args[1] if len(args) > 1 else kwargs.get("schema")
        messages = args[0] if args else kwargs.get("messages", [])
        tools = kwargs.get("tools")
        requested_output = max(0, int(kwargs.get("max_tokens") or 0))
        original_message_count = len(messages)
        messages = self.context_messages(messages, schema, tools, requested_output)
        if args:
            args = (messages, *args[1:])
        else:
            kwargs = {**kwargs, "messages": messages}
        estimate = self.estimate_units(messages, schema, tools, requested_output)
        elapsed = time.monotonic() - self.started
        if not self.fits_context(messages, schema, tools, requested_output):
            self._set_exhausted("max_context_tokens")
        if elapsed >= self.max_seconds:
            self._set_exhausted("max_seconds")
        elif self.calls >= self.max_calls:
            self._set_exhausted("max_model_calls")
        elif self.max_tokens is not None and self.tokens + estimate > self.max_tokens:
            self._set_exhausted("max_total_tokens")
        entry = self._account(messages, schema, tools, requested_output, estimate)
        entry["context_history_messages_removed"] = original_message_count - len(messages)
        self.pending_entry = entry
        if self.exhausted_reason:
            entry["refused_reason"] = self.exhausted_reason
            return self._fallback(schema)
        remaining = max(0.001, self.max_seconds - elapsed)
        self.calls += 1
        self.estimated_tokens += estimate
        self.pending_estimate = estimate
        self.last_usage_source = "estimated"
        self.usage_estimated_calls += 1
        try:
            response = await asyncio.wait_for(
                self.inner.complete(*args, **kwargs), timeout=remaining)
        except asyncio.TimeoutError:
            self._set_exhausted("model_call_timeout")
            self.last_usage_source = "estimated"
            self._settle(entry, None)
            return self._fallback(schema)
        except BaseException as exc:
            usage = _provider_usage(getattr(exc, "usage", None))
            if usage is None:
                self.last_usage_source = "estimated"
            else:
                self.estimated_tokens -= estimate
                self.pending_estimate = 0
                self.usage_estimated_calls -= 1
                self.usage_reported_calls += 1
                self.reported_tokens += usage["prompt_tokens"] + usage["completion_tokens"]
                self.last_usage_source = "reported"
                entry["usage_source"] = "reported"
            self._settle(entry, usage)
            self._record_overrun()
            raise
        usage = _provider_usage(getattr(response, "usage", None))
        if usage is None:
            self.last_usage_source = "estimated"
            self._settle(entry, None)
            self._record_overrun()
            return response
        self.estimated_tokens -= estimate
        self.pending_estimate = 0
        self.usage_estimated_calls -= 1
        self.usage_reported_calls += 1
        self.reported_tokens += usage["prompt_tokens"] + usage["completion_tokens"]
        self.last_usage_source = "reported"
        entry["usage_source"] = "reported"
        self._settle(entry, usage)
        self._record_overrun()
        return response


    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


class ToolCall(BaseModel):
    """Normalize native and JSON tool calls into the same execution request.

    Arguments may already be an object or still be a JSON string. The
    executor assigns a missing call ID and checks the actual tool arguments.
    """

    id: str = Field(default="")
    name: Literal["wiki_search", "wiki_read", "table_inspect", "table_locate", "table_read", "table_compute"]
    arguments: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")
    @model_validator(mode="before")
    def _parse_envelope(cls, values: Any) -> Dict[str, Any]:
        if not isinstance(values, dict):
            raise ValueError(_WIKI_TOOL_ENVELOPE_INVALID)

        function = values.get("function", values)
        if not isinstance(function, dict):
            raise ValueError(_WIKI_TOOL_ENVELOPE_FUNCTION)
        name = function.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(_WIKI_TOOL_ENVELOPE_NAME)
        args = function.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (ValueError, TypeError):
                raise ValueError(_WIKI_TOOL_ARGUMENTS_JSON)
        if not isinstance(args, dict):
            raise ValueError(_WIKI_TOOL_ARGUMENTS_OBJECT)
        call_id = values.get("id")
        return {"id": call_id if isinstance(call_id, str) else "",
                "name": name, "arguments": args}


class TraversalResponse(BaseModel):
    """The envelope a model returns during traversal: tools or a final draft."""

    tool_calls: List[ToolCall] = Field(default_factory=list)
    answer: str = Field(default="")
    incomplete: bool = Field(default=False, description="True when stopping with missing evidence; explain what is missing in answer.")
    citations: List[str] = Field(
        default_factory=list,
        description=(
            "Stable [E#] handles for the delivered evidence this answer uses. "
            "Copy each handle exactly from the evidence you actually read; the "
            "backend restores the full source/location/excerpt."
        ),
    )
    model_config = ConfigDict(extra="ignore")

    @field_validator("citations", mode="before")
    def _citation_is_a_handle(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = [value]
        normalized = []
        for item in value if isinstance(value, (list, tuple)) else ():
            if not isinstance(item, str):
                continue
            handle = item.strip()
            if re.fullmatch(r"E[1-9][0-9]*", handle):
                handle = "[{}]".format(handle)
            if re.fullmatch(r"\[E[1-9][0-9]*\]", handle):
                normalized.append(handle)
        return list(dict.fromkeys(normalized))


MAX_EMPTY_MODEL_TURNS = 5
# At most this many invalid bridge/final attempts are corrected before the run
# stops incomplete. Required slots are never silently waived after the bound.
MAX_SLOT_TURNS = 3
BLANK_QUESTION = "BLANK_QUESTION"
UNSUPPORTED_QUERY_STRATEGY = "UNSUPPORTED_QUERY_STRATEGY"
READ_REQUIRED = "READ_REQUIRED"

# Specific sub-codes for malformed model tool output (fail closed).  Each value
# embeds the public MALFORMED_TOOL_CALL marker so every failure is uniformly
# attributable and never leaks a bare TypeError.
_WIKI_TOOL_ENVELOPE_INVALID = f"{MALFORMED_TOOL_CALL}:WIKI_TOOL_ENVELOPE_INVALID"
_WIKI_TOOL_ENVELOPE_ID = f"{MALFORMED_TOOL_CALL}:WIKI_TOOL_ENVELOPE_ID"
_WIKI_TOOL_ENVELOPE_TYPE = f"{MALFORMED_TOOL_CALL}:WIKI_TOOL_ENVELOPE_TYPE"
_WIKI_TOOL_ENVELOPE_FUNCTION = f"{MALFORMED_TOOL_CALL}:WIKI_TOOL_ENVELOPE_FUNCTION"
_WIKI_TOOL_ENVELOPE_NAME = f"{MALFORMED_TOOL_CALL}:WIKI_TOOL_ENVELOPE_NAME"
_WIKI_TOOL_ARGUMENTS_NOT_STRING = f"{MALFORMED_TOOL_CALL}:WIKI_TOOL_ARGUMENTS_NOT_STRING"
_WIKI_TOOL_ARGUMENTS_JSON = f"{MALFORMED_TOOL_CALL}:WIKI_TOOL_ARGUMENTS_JSON"
_WIKI_TOOL_ARGUMENTS_OBJECT = f"{MALFORMED_TOOL_CALL}:WIKI_TOOL_ARGUMENTS_OBJECT"

# CTRL.3: one search without a newly discovered unread ranked candidate makes
# code prefer the accumulated read frontier; two with no frontier stop the hop.
FORCE_READ_NO_PROGRESS = 1
NO_PROGRESS_STOP_LIMIT = 2
MAX_FORCED_READ_PATHS = 5

# Programmatic (code-decided) terminations share one explicit answer shape so a
# bounded run never presents partial entities as if they answered the question.
_TERMINATION_LIMIT_LABELS = {
    "max_model_calls": ("模型调用次数", "model-call"),
    "max_total_tokens": ("token 预算", "token-budget"),
    "max_context_tokens": ("单次上下文容量", "context-window"),
    "max_seconds": ("整体运行时间", "wall-clock"),
    "model_call_timeout": ("单次模型调用时间", "single-model-call time"),
    "model_budget_exhausted": ("模型预算", "model-budget"),
    "tool_limit": ("工具调用次数", "tool-call"),
    "no_successful_read": ("来源读取", "source-read"),
    "no_progress_exhausted": ("检索进展", "search-progress"),
    "empty_model_exhausted": ("模型有效输出", "model-output"),
    "empty_search_exhausted": ("检索命中", "search-hit"),
    "evidence_budget_exhausted": ("证据读取预算", "evidence-budget"),
    "control_slot_incomplete": ("必要回答槽位", "required-slot"),
    "answered": ("回答核查", "answer-verification"),
    "table_artifact_inconsistent": ("表附件一致性检查", "table-artifact consistency check"),
}

_CJK_RANGES = ((0x3400, 0x9FFF), (0xF900, 0xFAFF), (0x3040, 0x30FF), (0xAC00, 0xD7AF))

MIN_TARGET_LABEL_CHARS = 4


def _question_prefers_english(question: str) -> bool:
    """True when the question carries no CJK text and therefore is not Chinese."""
    text = question or ""
    if any(start <= ord(char) <= end for char in text for start, end in _CJK_RANGES):
        return False
    return bool(text.strip())


def _normalize_target_text(text: Any) -> str:
    """Unicode NFKC, casefold and no whitespace; every other symbol is kept."""
    import unicodedata

    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return "".join(char for char in normalized if not char.isspace())


def _identifier_in_text(text: str, literal: str) -> bool:
    """True when ``literal`` occurs in ``text`` as a whole identifier."""
    if not literal:
        return False
    start = text.find(literal)
    while start != -1:
        before = text[start - 1] if start > 0 else ""
        after = text[start + len(literal)] if start + len(literal) < len(text) else ""
        neighbours = [char for char in (before, after) if char]
        if not any(char.isascii() and char.isalnum() for char in neighbours):
            return True
        start = text.find(literal, start + 1)
    return False


def _facts_for_explicit_target(question: str, facts: Sequence[Dict[str, Any]],
                               table_labels_by_id: Mapping[str, Sequence[str]],
                               ) -> List[Dict[str, Any]]:
    """Facts a bounded refusal may rest on, only for one explicit target table.

    Conservative by construction: the question must name exactly one table (by
    a full trusted label of at least ``MIN_TARGET_LABEL_CHARS`` characters, or
    by the literal table_id), and only that table's facts are eligible.
    """
    question_text = _normalize_target_text(question)
    if not question_text:
        return []
    matched: Dict[str, set] = {}
    for table_id, labels in (table_labels_by_id or {}).items():
        for label in labels or []:
            normalized = _normalize_target_text(label)
            if len(normalized) < MIN_TARGET_LABEL_CHARS:
                continue
            if normalized in question_text:
                matched.setdefault(str(table_id), set()).add("label")
    for fact in facts:
        table_id = str(fact.get("table_id") or "")
        if table_id and table_id in question_text:
            matched.setdefault(table_id, set()).add("table_id")
    if len(matched) != 1:
        return []
    target = next(iter(matched))
    selected: List[Dict[str, Any]] = []
    for fact in facts:
        if str(fact.get("table_id") or "") != target:
            continue
        kind = fact.get("kind")
        if kind == "structure_ambiguous":
            if fact.get("interpretation_status") != "ambiguous":
                continue
            selected.append(fact)
            continue
        if kind == "no_match_in_complete_table_scope":
            if fact.get("scope_complete") is not True:
                continue
            if fact.get("field_valid") is False or fact.get("cursor"):
                continue
            literal = _normalize_target_text(fact.get("literal"))
            if not literal or not _identifier_in_text(question_text, literal):
                continue
            selected.append(fact)
    return selected


def _render_grounded_incomplete(facts: Sequence[Mapping[str, Any]], question: str) -> str:
    """Render the bounded refusal from the matched facts only, in code."""
    english = _question_prefers_english(question)
    lines: List[str] = []
    for fact in facts:
        labels = [str(label) for label in fact.get("table_labels") or [] if str(label).strip()]
        table = labels[0] if labels else str(fact.get("table_id") or "")
        if fact.get("kind") == "structure_ambiguous":
            lines.append(
                'Cannot determine the asked value uniquely from "{}": the table '
                'structure is ambiguous. The header or field meaning needs '
                'clarification.'.format(table) if english else
                "无法从「{}」唯一确定所问取值：该表结构存在歧义。需要澄清表头或字段含义。".format(table))
            continue
        literal = str(fact.get("literal") or "")
        field = str(fact.get("field_label") or "").strip()
        if english:
            scope = "column {}".format(field) if field else "whole table text"
            lines.append(
                'The {} of "{}" was searched and the identifier "{}" was not found. '
                'The corresponding value therefore cannot be determined from that '
                'scope.'.format(scope, table, literal))
        else:
            scope = field if field else "整表文本"
            lines.append(
                "已完整检查「{}」的{}，未找到标识符「{}」。因此无法从该范围确定其对应取值。"
                .format(table, scope, literal))
    return "\n".join(lines)


def _fact_identity_consistent(fact: Mapping[str, Any], revision_id: Any) -> bool:
    """Whether one observed fact may still justify a refusal."""
    fact_revision = fact.get("revision_id")
    if fact_revision and revision_id and str(fact_revision) != str(revision_id):
        return False
    kind = fact.get("kind")
    if kind == "structure_ambiguous":
        return str(fact.get("interpretation_status") or "") == "ambiguous"
    if kind == "no_match_in_complete_table_scope":
        if fact.get("scope_complete") is not True:
            return False
        if fact.get("field_valid") is False or fact.get("cursor"):
            return False
        return bool(_normalize_target_text(fact.get("literal")))
    return False


def _fact_overturned(state: Mapping[str, Any], fact: Mapping[str, Any]) -> bool:
    """True when this run's own ledger already observed the missing literal."""
    literal = _normalize_target_text(fact.get("literal"))
    if not literal:
        return False
    table_id = str(fact.get("table_id") or "")
    for entry in (state.get("table_reads_by_request") or {}).values():
        if str(entry.get("table_id") or "") != table_id:
            continue
        for handle in (entry.get("candidate_handles") or {}).values():
            excerpt = _normalize_target_text(handle.get("excerpt"))
            if excerpt and literal in excerpt:
                return True
    return False


def _try_grounded_termination(state: Mapping[str, Any],
                              question: str) -> Optional[Dict[str, Any]]:
    """The shared bounded-refusal decision, or ``None`` to keep going."""
    facts = list(state.get("incomplete_facts") or [])
    if not facts:
        return None
    revision_id = getattr(state.get("bundle_view"), "revision_id", None)
    usable = [fact for fact in facts
              if _fact_identity_consistent(fact, revision_id)
              and not _fact_overturned(state, fact)]
    if not usable:
        return None
    selected = _facts_for_explicit_target(question, usable,
                                          state.get("table_labels_by_id") or {})
    if not selected:
        return None
    return {
        "answer": _render_grounded_incomplete(selected, question),
        "facts": selected,
        "scope": {
            "target_binding": "explicit_table_name",
            "tables": sorted({str(fact.get("table_id")) for fact in selected}),
            "kinds": sorted({str(fact.get("kind")) for fact in selected}),
            "literals": sorted({str(fact.get("literal")) for fact in selected
                                if fact.get("literal")}),
            "fields": sorted({str(fact.get("field_label")) for fact in selected
                              if fact.get("field_label")}),
            "fact_ids": [fact.get("fact_id") for fact in selected],
        },
    }


def _accept_grounded_termination(state: Dict[str, Any], record: Mapping[str, Any],
                                 trace: List[TraceEntry], *,
                                 draft: Optional[str] = None,
                                 superseded_stop_reason: Optional[str] = None) -> None:
    """Record the decision; the terminal block publishes it once, later on."""
    facts = list(record["facts"])
    scope = dict(record["scope"])
    state["grounded_incomplete"] = facts
    state["grounded_incomplete_answer"] = str(record["answer"])
    detail: Dict[str, Any] = {
        "facts": len(facts),
        "fact_ids": scope["fact_ids"],
        "kinds": scope["kinds"],
        "tables": scope["tables"],
        "table_labels": sorted({str(label) for fact in facts
                                for label in fact.get("table_labels") or []}),
        "target_binding": scope["target_binding"],
        "independent_of_successful_read": True,
        "successful_read": bool(state.get("successful_read")),
    }
    if draft is not None:
        detail["draft_ignored"] = True
        detail["draft_length"] = len(draft)
        detail["draft_sha256"] = hashlib.sha256(draft.encode("utf-8")).hexdigest()
    if superseded_stop_reason:
        detail["superseded_stop_reason"] = superseded_stop_reason
    trace.append(TraceEntry(event="grounded_incomplete_accepted", detail=detail))


def _computed_conclusion_correction(answer: str, state: Dict[str, Any], *,
                                    budgeted_model: Any) -> Optional[Dict[str, Any]]:
    """The single-shot correction for a computed conclusion, or ``None``.

    Two cases are separated.  When the answer states a derived number that no
    program result supports (or that it does not cite), the conflict is reported
    with both numbers and the handles to use.  When there is no program result at
    all but the run read table evidence, directly disclosed values can pass
    through their source citations. Unsupported numbers receive one request to
    supply source evidence or a supported calculation, or declare the gap.
    No separate classification call is introduced; a correction can consume
    another answer turn within the existing budget.

    Returns ``{"message", "detail", "event"}`` for the first correction and
    ``{"repeat": True, ...}`` when the answer conflicts again after that.
    """
    failures = state.get("table_calculate_failures") or []
    computations = state.get("table_compute_results") or []
    conflict = computed_answer_conflict(answer, computations, failures=failures, state=state,
        bindings=state.get("computed_claim_bindings") or (), citation_handles=state.get("numeric_citation_handles") or ())
    event = "computed_answer_conflict"
    if conflict is None and (state.get("delivered_workbooks") or state.get("table_reads_by_request")):
        conflict = computed_claim_without_program(answer, computations, failures=failures, state=state,
            citation_handles=state.get("numeric_citation_handles") or ())
        event = "computed_claim_without_program"
    if conflict is None:
        return None
    # One shared correction allowance; alternating failure categories cannot
    # buy extra rounds. A remaining conflict, including an exhausted budget,
    # becomes an explicit incomplete result rather than silently passing.
    if state.get("numeric_correction_sent") or not _correction_budget_available(budgeted_model):
        return {"event": event, "detail": conflict, "repeat": True}
    state["numeric_correction_sent"] = True
    return {"event": event, "detail": conflict,
            "message": "数值结论还没有可核验的依据：" + json.dumps(conflict, ensure_ascii=False)
            + "。直接披露值请读取并引用原文；需要运算时使用 table_compute 的结构化参数和已读操作数，"
              "再用 computed_claims 绑定具体 computation_id/field。不要重算已有查找值。"
              "不得忽略未确认的指标、期间、单位或主体；仍有缺口请明确说明并以 incomplete=true 停止。"}


def _local_answer_repair_messages(question: str, draft: str, issues: dict,
                                  state: Dict[str, Any],
                                  messages: Sequence[dict]) -> List[dict]:
    """Keep recent delivered reading context and the run's stable handles."""
    from jonex_wiki.domain.engine.query.controller.synthesis import _reading_context_payload

    query_state = state["query_state"]
    evidence, _ = _resolve_evidence_handles(query_state, [
        query_state.evidence_handle(eid) for eid in query_state.seen_evidence_order])
    views = list((state.get("read_views") or [])[-2:])
    # Historical generated pages have no article view. Keep their delivered
    # body as reading context, never turn it into source evidence or a handle.
    reads = [m for m in messages if m.get("role") == "tool"
             and m.get("name") == "wiki_read"][-2:]
    for message in reads:
        try:
            payload = json.loads(message.get("content") or "{}")
        except (TypeError, ValueError):
            continue
        files = payload.get("files") if isinstance(payload, dict) else None
        if not isinstance(files, dict):
            continue
        for path, body in files.items():
            if (not isinstance(body, str) or not body.strip()
                    or body.startswith("material:") or path.endswith("index.md")
                    or any(view.get("article_path") == path for view in views)):
                continue
            views.append({"article_path": path, "blocks": [{"body": body}],
                          "structure_origin": "wiki_page", "selected_level": "page"})
    reading_context, _ = _reading_context_payload(views)
    return [_system_message(), {"role": "user", "content": json.dumps({
        "question": question, "draft": draft, "issues": issues,
        "evidence": evidence, "source_reading_context": reading_context,
        "instruction": "Address this local issue and return your answer. Explain any remaining gap."
    }, ensure_ascii=False, separators=(",", ":"))}]


def _correction_budget_available(budgeted_model: Any, *, reserve: int = 3) -> bool:
    """Whether a corrective turn still leaves room for the answer it corrects.

    A correction that consumes the final model calls turns a nearly finished
    answer into "the run did not finish": the draft the model already produced
    is strictly better than a budget refusal.
    """

    try:
        remaining = int(budgeted_model.max_calls) - int(budgeted_model.calls)
    except (AttributeError, TypeError, ValueError):
        return True
    return remaining > max(1, int(reserve))


#: How a failed claim is rewritten in the returned answer.  The original text
#: is kept verbatim inside the marker (nothing is silently dropped) but it is
#: no longer an affirmative sentence, and the handles it carried are removed
#: because those handles are exactly what failed the check.
UNVERIFIED_MARK = "〔未核实，不作为结论：{}〕"


def _remember_answer_bindings(state: Dict[str, Any], parsed: Any) -> None:
    """Keep the bound draft's declarations for the final-text re-check.

    Bindings only cover the text they were declared for.  If synthesis rewrites
    the answer afterwards, the final check has to run against the *returned*
    text with these declarations, so they are kept on the run state rather than
    only on the draft object.
    """
    state["final_answer_bindings"] = [
        item.model_dump() for item in (getattr(parsed, "answer_bindings", None) or ())]
    state["final_answer_bound_text"] = str(getattr(parsed, "answer", "") or "")


def _unverified_marks(conflicts: Sequence[Mapping[str, Any]]) -> List[str]:
    """The exact phrases a conflict payload says are unsupported."""
    marks: List[str] = []
    for item in conflicts or ():
        if item.get("claim"):
            marks.append(str(item["claim"]))
        marks.extend(str(proposition) for proposition in (item.get("propositions") or ()))
    return list(dict.fromkeys(mark for mark in marks if mark))


def _mark_unverified_claims(answer: str, claims: Sequence[str]) -> Tuple[str, List[str]]:
    """Rewrite failed claims as explicit non-conclusions, then note them.

    A fact whose binding failed must not stay an affirmative sentence with the
    citation that failed attached, with only a trailing "unverified" sentence
    added.  Each failed phrase is therefore rewritten in place as
    ``〔未核实，不作为结论：...〕`` with its own handles stripped, and the
    bounded note still names what was marked.
    """
    marked = [claim for claim in dict.fromkeys(str(item) for item in claims)
              if claim and claim in answer]
    if not marked:
        return answer, []
    bounded = marked[:MAX_UNVERIFIED_MARKS]
    rewritten = answer
    for claim in bounded:
        neutral = re.sub(r"\s*\[[^\]]*\]", "", claim).strip()
        rewritten = rewritten.replace(claim, UNVERIFIED_MARK.format(neutral), 1)
    listed = "；".join(bounded)
    # Only the bounded, actually rewritten phrases are reported: the event says
    # what the returned text now marks, not what the conflict listed.
    return (rewritten.rstrip() + "\n\n（未验证：{} —— 本次交付证据中没有可核验的依据，"
            "以上内容按待核实对待。）".format(listed), bounded)


def _gap_correction_message(candidates: Sequence[str]) -> str:
    listed = ", ".join(candidates[:8])
    return ("EVIDENCE_GAP_WITH_UNREAD_CANDIDATES: 本轮已出现可访问但未读的候选："
            + (listed or "（无）")
            + "。请针对缺口做一次定向读取（wiki_read 这些路径，或改用更精确的 wiki_search），"
              "然后用已读 [E#] 给出答案；仍无法建立依据的部分保留为缺口并标 incomplete=true，"
              "不要把候选内容当成已读证据。")


def _gap_correction(state: Dict[str, Any], trace: List[TraceEntry], budgeted_model: Any,
                    candidates: Sequence[str], *, reason: str) -> bool:
    """The single bounded gap correction allowed per run (R1.2).

    Returns ``True`` when the caller must continue the loop with one
    gap-directed turn.  Exactly one correction is ever granted: the second gap
    is accepted with its uncertainty, so a run can neither loop on prompts nor
    widen its own budget.
    """
    if not candidates or state.get("gap_correction_sent"):
        return False
    if not _correction_budget_available(budgeted_model):
        state["gap_correction_budget_denied"] = True
        trace.append(TraceEntry(event="gap_correction_denied", detail={
            "reason": reason, "candidates": list(candidates[:8]),
            "denied_by": "correction_budget_reserve"}))
        return False
    state["gap_correction_sent"] = True
    trace.append(TraceEntry(event="gap_correction_requested", detail={
        "reason": reason, "candidates": list(candidates[:8])}))
    return True


def _unread_candidate_paths(state: Mapping[str, Any]) -> List[str]:
    """Accessible-but-unread candidates observed by search, in observation order.

    The frontier is a hint list, not evidence: it is deduplicated by page and
    excludes anything this run already read, and it is never registered with a
    handle.
    """
    observations = state.get("candidate_observations") or []
    read_pages = set(state.get("read_pages") or ())
    ordered: List[str] = []
    for item in observations:
        path = str((item or {}).get("path") or "")
        if not path or path in read_pages or path in ordered:
            continue
        ordered.append(path)
    return ordered


def _programmatic_incomplete_answer(question: str, reason: Optional[str]) -> str:
    """Explicit unfinished answer for a run that a programmatic limit stopped."""
    label = _TERMINATION_LIMIT_LABELS.get(reason or "", ("运行", "run"))
    if _question_prefers_english(question):
        return ("This question is not complete: the {} limit was reached and the "
                "run stopped before an answer was produced.").format(label[1])
    return "当前问题未完成：已达到{}限制。".format(label[0])


# K6c: the bridge relation check must judge the *whole* relation, not the part
# of it the candidate happens to satisfy.
_BRIDGE_RELATION_SYSTEM = (
    "Check whether the evidence satisfies EVERY part of the relation in "
    "relation.question for the entity in candidate, in the role described by "
    "relation.role and required_for, and for the output_object the value has to "
    "serve. Judge identity, time, negation, scope and unit, and every composite "
    "requirement the relation states: a superlative (largest, first, most, "
    "only, highest, earliest), an aggregate, a count, an ordinal or a rank. "
    "Evidence that satisfies only part of the relation is insufficient - a "
    "candidate that is merely in the same category as the relation (a city "
    "that is in the state the relation asks about, when the relation asks for "
    "the largest city of that state) does not satisfy it, evidence that names "
    "an album does not satisfy a relation about the label that released it, and "
    "evidence from the same document does not satisfy a relation it never "
    "states. Treat supported_input_bindings as established by prior hops; do "
    "not demand that this evidence repeat those earlier relations. Return "
    "supported only when no part of the relation is left unsatisfied; return "
    "insufficient when a part is missing, or contradicted when the evidence "
    "denies the relation, and name the unsatisfied part of the relation in "
    "reason. Do not use outside knowledge."
)


_HOP_SLOT_RE = re.compile(r"H([1-9][0-9]*)")


def _next_hop_question(runtime: Any, slot_id: str) -> str:
    """The *planned* question of the hop after ``slot_id``'s hop, if any.

    The future question lives in the runtime cursor (``directions`` plus
    ``direction_index``), not in the hops that already exist: while H1's bridge
    is being bound only H1 has been created, so reading the created hops always
    returned "" for a bridge that is meant to serve an already-planned H2.
    Creating the next hop here would be a state change of its own, and the last
    hop legitimately has no successor.
    """
    directions = tuple(getattr(runtime, "directions", ()) or ())
    index = getattr(runtime, "direction_index", None)
    if not directions or not isinstance(index, int):
        return ""
    match = _HOP_SLOT_RE.fullmatch(str(slot_id).split(".")[0])
    if match is None:
        return ""
    if int(match.group(1)) - 1 != index:
        # The slot does not belong to the hop the runtime cursor points at.
        return ""
    following = index + 1
    if following >= len(directions):
        return ""
    return str(directions[following]).strip()


def _bridge_relation_input(current_slot: Mapping[str, Any], *, candidate: str,
                           evidence: str, supported_inputs: Sequence[str],
                           final_question: str, next_hop_question: str,
                           ) -> Dict[str, Any]:
    """Everything the bridge relation check must see (K6c)."""
    role = str(current_slot.get("role") or "fact")
    if role == "bridge":
        output_object = ("a value the next hop's question can consume: "
                         + (next_hop_question or "the next hop's question"))
    else:
        output_object = "the final answer value for the final question"
    return {
        "relation": {
            "slot_id": str(current_slot.get("slot_id") or ""),
            "question": str(current_slot.get("description") or ""),
            "role": role,
            "required_for": str(current_slot.get("required_for") or "answer"),
        },
        "output_object": output_object,
        "final_question": final_question,
        "next_hop_question": next_hop_question,
        "supported_input_bindings": list(supported_inputs),
        "candidate": candidate,
        "evidence": evidence,
    }


def _tools_for_controller(runtime: Any) -> list:
    """Ordinary tools; path selection belongs to the agent."""
    return QUERY_TOOLS[:-1]



# Questions whose answer must come from the deterministic table tool rather than from
# reading rows.  Two families qualify: counting/enumeration, and the cross-group
# comparison that asks which key repeats under more than one category or which values
# differ inside one key.  The second family, answered by eye, reported neighbouring
# product names as one product listed in two exchanges.
_COMPUTATIONAL_QUESTION = re.compile(
    r"统计|分布|有哪些序号|权限有哪些|多少(?:个|项|种|天|次|条)|几(?:个|项|种|天|次)|总(?:数|计)|分别.*(?:日期|哪天)|所有.*(?:列出|列举)|\bcount(?:s|ed|ing)?\b|how many"
    r"|出现在(?:多个|几个|两个|两个以上|不止一个)|跨(?:交易所|机构|类别|分组)"
    r"|多个(?:交易所|机构|类别)|多档|不一致|不统一"
    r"|重(?:复|合)出现|重复|去重|不同的(?:交易所|类别|分组|取值|值)|对应的?不同"
    r"|按.*分(?:组|类)|各(?:自|不相同)|more than one"
    r"|对比|比较|across (?:exchanges|categories|groups)|compare",
    re.IGNORECASE,
)


def needs_deterministic_computation(question: str) -> bool:
    """True when the question asks for a count, an enumeration, or a cross-group comparison."""

    return bool(_COMPUTATIONAL_QUESTION.search(question or ""))


_READ_REQUIRED_MESSAGE = (
    "READ_REQUIRED: no page evidence has been read yet; you must successfully "
    "read a knowledge page before producing a final answer."
)
_PRODUCE_TOOL_OR_ANSWER = (
    "Your response contained neither a tool call nor a final answer. "
    "Call a tool or produce a final answer."
)
_BRIDGE_BINDING_REQUIRED_MESSAGE = (
    "BRIDGE_BINDING_REQUIRED: the current hop has an open bridge slot. "
    "Read evidence if needed, then return bridge_binding with the exact slot_id, "
    "one active [E#] handle marked eligible_for_current_hop, and a bridge value "
    "copied from that handle's excerpt."
)
_BRIDGE_BINDING_REJECTED_MESSAGE = (
    "BRIDGE_BINDING_REJECTED: use the current open bridge slot and an active "
    "[E#] handle marked eligible_for_current_hop; the bridge value must occur "
    "in that handle's excerpt."
)
# XML tool-call rejection (traversal): the model wrote a tool call as XML text
# instead of using the native tool_calls channel or answering.  Correct it so a
# subsequent turn uses the native protocol or gives the answer.
_XML_TOOL_CALL_CORRECTION = (
    "Your response contained a tool-call XML block instead of an answer. "
    "Use the native tool-calling protocol to call a tool, or give the final "
    "answer directly from the evidence you read."
)


class QueryRunResult(BaseModel):
    """The auditable outcome of one query traversal."""

    answer: str
    citations: List[VerifiedCitation] = Field(default_factory=list)
    incomplete: bool
    # A grounded incomplete names the observed fact that prevents a value and
    # the scope that observation covers; it is never a bare refusal.
    incomplete_reason: Optional[str] = None
    incomplete_scope: Optional[Dict[str, Any]] = None
    grounded_incomplete_facts: List[Dict[str, Any]] = Field(default_factory=list)
    search_calls: int
    read_calls: int
    tool_calls: int
    judge_calls: int = 0
    model_calls: int = 0
    stop_reason: str
    failure_class: Optional[FailureClass] = None
    trace: List[TraceEntry] = Field(default_factory=list)
    # CR-TRACE-002: real span records and aggregated provider token usage.
    spans: List[TraceSpan] = Field(default_factory=list)
    token_usage: Dict[str, int] = Field(default_factory=dict)
    # Frozen runtime config that actually governed this run (not the declared
    # benchmark metadata), so a before/after is attributable.
    config: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")
    @field_validator("search_calls", "read_calls", "tool_calls", "model_calls")
    def _non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("counts must be non-negative")
        return v

    @model_validator(mode="after")
    def _failure_only_on_incomplete(self):
        if not self.incomplete and self.failure_class is not None:
            raise ValueError("complete query result cannot have failure_class")
        return self


def _completed_request_ids(state: Mapping[str, Any]) -> set:
    """Completed same-scope requests that may trigger an automatic readiness check.

    Reads only the original ledger; never copies evidence or applies a row-count
    or question-type rule.  ``require_all_matches`` defaults to True exactly as
    ``ScopeSpec`` does.
    """
    return {
        request_id
        for request_id, entry in state.get("table_reads_by_request", {}).items()
        if entry.get("status") == "complete"
        and (entry.get("requested_scope") or {}).get("require_all_matches", True) is True
    }


def filter_table_registry(
    table_registry: Optional[Dict[str, Dict[str, str]]],
    allowed_documents: Sequence[str],
) -> Dict[str, Dict[str, str]]:
    """Keep only the tables whose owner is inside the allowed documents.

    A grid follows the same scope as the pages it belongs to: a table outside
    the allowed documents or sources is not inspectable, locatable or readable.
    """
    allowed = {str(value) for value in allowed_documents}
    return {
        table_id: info for table_id, info in (table_registry or {}).items()
        if str(info.get("document_id")) in allowed or str(info.get("source_id")) in allowed
    }


# ──────────────────────────────────────────────
#  Entry point
# ──────────────────────────────────────────────


async def run_query_algorithm(
    model: ModelPort,
    corpus: QueryCorpus,
    question: str,
    strategy: QueryStrategy,
    *,
    model_name: Optional[str] = None,
    tenant_id: str = "",
    context_id: str = "",
    trace_id: str = "",
    run_id: Optional[str] = None,
    lexical_index: bytes | None = None,
    frozen_indexes: bool = False,
    traversal_thinking: Optional[Dict[str, str]] = None,
    synthesis_thinking: Optional[Dict[str, str]] = None,
    document_scope: Sequence[str] = (),
    bundle_view: Any = None,
    table_registry: Optional[Dict[str, Dict[str, str]]] = None,
    cancel: CancelPort | None = None,
) -> QueryRunResult:
    """Run the enforced query traversal over the accepted corpus.

    Only ``ModelPort`` is called plus the system clock for real span timing.
    Raises ``ValueError`` with a stable marker for a blank question, an
    unsupported strategy or an unavailable FTS5 backend.  Never exceeds the
    tool-call cap (default ``MAX_TOOL_CALLS``, overridable via
    ``QUERY_MAX_TOOL_CALLS``).  Duplicate searches/re-reads short-circuit to
    cached results, and ``QUERY_EVIDENCE_STALL_LIMIT`` consecutive no-new-
    evidence searches stop early with ``evidence_sufficient``.

    The result carries real ``spans`` (unique ``run:<run-id>:span:<n>`` ids)
    and aggregated ``token_usage`` from Provider usage (``{}`` when no provider
    ever reported usage).
    """
    if not isinstance(question, str) or not question.strip():
        raise ValueError(BLANK_QUESTION)
    budgeted_model = _BudgetedQueryModel(model, model_name=model_name,
                                        **_query_budget_limits())
    model = budgeted_model
    if _answer_style_natural() and traversal_thinking is None:
        traversal_thinking = {"type": "enabled"}
    if synthesis_thinking is None:
        synthesis_mode = current_env().get("QUERY_SYNTHESIS_THINKING", "scene_default")
        if synthesis_mode not in {"scene_default", "on", "off"}:
            raise ValueError("QUERY_SYNTHESIS_THINKING must be on, off, or scene_default")
        if synthesis_mode != "scene_default":
            synthesis_thinking = {"type": "enabled" if synthesis_mode == "on" else "disabled"}
        elif _answer_style_natural():
            synthesis_thinking = {"type": "enabled"}
    traversal_thinking = _query_thinking(traversal_thinking)
    synthesis_thinking = _query_thinking(synthesis_thinking)
    document_scope = tuple(document_scope)
    if len(document_scope) != len(set(document_scope)) or any(
        not isinstance(value, str) or not value.strip() for value in document_scope
    ):
        raise ValueError("QUERY_DOCUMENT_SCOPE_INVALID")
    if document_scope:
        allowed_documents = set(document_scope)
        allowed_pages = {
            path for path, claims in corpus.claims.items()
            if claims and all(
                evidence.document_id in allowed_documents
                for claim in claims for evidence in claim.evidence
            )
        }
        allowed_sources = {
            path for path, records in corpus.source_records.items()
            if records and all(record.get("document_id") in allowed_documents for record in records)
        }
        allowed_files = allowed_pages | allowed_sources | {
            path for path in corpus.files if path == "index.md" or path.endswith("/index.md")
        }
        corpus = corpus.model_copy(deep=False)
        corpus.files = {path: corpus.files[path] for path in corpus.files if path in allowed_files}
        corpus.page_index = {path: value for path, value in corpus.page_index.items() if path in allowed_pages}
        corpus.page_text = {path: corpus.page_text[path] for path in corpus.page_text if path in allowed_pages}
        corpus.claims = {path: value for path, value in corpus.claims.items() if path in allowed_pages}
        corpus.source_records = {path: value for path, value in corpus.source_records.items() if path in allowed_sources}
        corpus.source_locations = {path: value for path, value in corpus.source_locations.items() if path in allowed_sources}
        table_registry = filter_table_registry(table_registry, allowed_documents)
    if strategy == QueryStrategy.AUTO:
        # Browse-first is the only supported default route.  Keep AUTO as a
        # wire-compatible enum value, but never let it silently select the
        # legacy search-first traversal.
        resolved = QueryStrategy.BROWSE_FIRST
    elif strategy == QueryStrategy.BROWSE_FIRST:
        resolved = strategy
    elif strategy == QueryStrategy.SEARCH_FIRST:
        resolved = strategy
    else:
        raise ValueError(UNSUPPORTED_QUERY_STRATEGY)

    file_index = bundle_view is not None and lexical_index is None
    if frozen_indexes and lexical_index is None and not file_index:
        raise ValueError("PUBLISHED_LEXICAL_INDEX_REQUIRED")
    index_stage = (
        "bm25_index_open" if lexical_index is not None or file_index else "bm25_index_build"
    )
    with telemetry_stage(current_runtime_telemetry(), index_stage):
        fts = (
            _open_fts_index(lexical_index)
            if lexical_index is not None
            else bundle_view.open_sqlite("indexes/lexical.sqlite")
            if file_index else _build_fts_index(corpus)
        )

    # Wave 3 P0.2: the 15-call cap and the evidence-sufficiency stall are
    # configurable via environment overrides, keeping the frozen defaults.
    max_tool_calls = _max_tool_calls_limit()
    stall_limit = _evidence_stall_limit()
    max_active_evidence = _max_active_evidence()
    runtime_config = {
        "agent_thinking": current_env().get("JONEX_WIKI_AGENT_THINKING", "on"),
        "model": model_name,
        "answer_style": "natural" if _answer_style_natural() else "entity",
        "thinking_traversal": (traversal_thinking or {}).get("type", "scene_default"),
        "thinking_synthesis": (synthesis_thinking or {}).get("type", "scene_default"),
        "thinking_decomposition": "off",
        "thinking_citation_extraction": "off",
        "traversal_max_tokens": _traversal_max_tokens(),
        "max_tool_calls": max_tool_calls,
        "max_nodes": 8,
        "max_model_calls": budgeted_model.max_calls,
        "max_total_tokens": budgeted_model.max_tokens,
        "max_context_tokens": budgeted_model.max_context_tokens,
        "completion_policy": "model",
        "query_budget_limits": _query_budget_limits(),
        "budget_estimator": {
            "method": BUDGET_ESTIMATE_METHOD,
            "version": BUDGET_ESTIMATE_VERSION,
            "units_per_byte": BUDGET_UNITS_PER_BYTE,
            "calibrated_models": list(BUDGET_CALIBRATED_MODELS),
            "units_per_byte_unknown_model": BUDGET_UNITS_PER_BYTE_UNKNOWN_MODEL,
            "rate_in_force": budgeted_model.units_per_byte(),
            "calibrated_model_match": budgeted_model.model_name in BUDGET_CALIBRATED_MODELS,
            "output_reserve": "requested max_tokens, replaced by reported usage",
        },
        "max_seconds": budgeted_model.max_seconds,
        "evidence_stall_limit": stall_limit,
        "search_read_force_limit": None,
        "max_active_evidence": max_active_evidence,
        "llm_rerank_enabled": _llm_rerank_enabled(),
        "diversity_rerank_enabled": _diversity_rerank_enabled(),
        "evidence_units_max": _evidence_units_max(),
        "evidence_per_doc_max": _evidence_per_doc_max(),
        "decompose_multihop_enabled": False,
        "force_read_no_progress": FORCE_READ_NO_PROGRESS,
        "no_progress_stop_limit": NO_PROGRESS_STOP_LIMIT,
        "answer_target_hint_enabled": _answer_target_hint_enabled(),
        "document_scope": list(document_scope),
    }

    messages: List[Dict[str, Any]] = [_system_message(), _user_message(question, resolved)]
    trace: List[TraceEntry] = []
    spans = _SpanBuilder(run_id=run_id)
    usage_acc: Dict[str, int] = {"input_tokens": 0, "output_tokens": 0}

    state: Dict[str, Any] = {
        "original_question": question,
        "total": 0,
        "search_calls": 0,
        "read_calls": 0,
        "empty_streak": 0,
        "successful_read": False,
        "query_state": initial_query_state(
            max_active_evidence=max_active_evidence
        ),
        "evidence_budget_exhausted": False,
        "tool_index": 0,
        # Wave 3 P0.2: duplicate-search / re-read short-circuit caches and the
        # evidence-sufficiency stall signal.
        "search_cache": {},
        "read_cache": {},
        "read_pages": set(),
        "stall_count": 0,
        "searches_since_read": 0,
        # Wave 3 P1.2: at most one deterministic empty-search rewrite per run.
        "rewrite_used": False,
        # The rewrite re-search is itself a tool call, so it must not push the
        # run past the 15-call cap (P0 invariant).
        "max_tool_calls": max_tool_calls,
        # Candidate-frontier diagnostics remain separate from the typed
        # read/active/citable evidence state above.
        "frontier_paths": [],
        # Compatibility mirror for the pre-existing gap judge and candidate
        # controller.  Every successful read refreshes it from typed active
        # evidence, so it can never widen the synthesis/citation allowlist.
        "collected_evidence": [],
        # S2G-RAG controller: active gap focus terms (drives evidence
        # prioritisation on the next read) and the judge call counter.
        "agent_driven": True,
        "active_gap_terms": None,
        "judge_calls": 0,
        "relation_check_cache": {},
        "bundle_view": bundle_view,
        "table_registry": table_registry or {},
        # L4 cross-page table ledger: what each request has really read, the
        # blockers it still owes, and a fatal artifact inconsistency.
        "table_reads_by_request": {},
        "table_scope_blockers": [],
        "table_fatal_error": None,
        # L5: claim_id -> programmatic table citation reference for this run.
        "table_evidence_refs": {},
        # The absolute wall-clock deadline of this run.
        "query_deadline_mono": budgeted_model.started + budgeted_model.max_seconds,
        # Grounded-incomplete inputs: observed table facts that may justify a
        # bounded refusal, plus the trusted table labels keyed by table_id.
        "incomplete_facts": [],
        "table_labels_by_id": {},
        # READING-REPAIR §3: the actual source/table ranges delivered this run.
        "read_views": [],
    }

    routing: Dict[str, Any] = {
        "model": model_name,
        "tenant_id": tenant_id,
        "context_id": context_id,
        "trace_id": trace_id,
    }


    # The agent chooses searches from the original question and observed text.
    # No predetermined hop slots or separate relation judge are needed.
    controller_directions = []
    # ControllerRuntime and the delivered-evidence ledger own the query loop.
    runtime_config["controller_version"] = "agent_v1"
    runtime_config["decomposition_requires_evidence_bridge"] = False

    # Force the first action in code before any model response.  This bypasses
    # model envelope parsing via a private execution path: the name/args are
    # constructed here, never parsed from a model tool-call envelope.
    if resolved == QueryStrategy.BROWSE_FIRST:
        trace.append(TraceEntry(event="forced_browse_first", detail={"paths": ["index.md"]}))
        controller, started = start_controller(
            question,
            directions=controller_directions,
        )
        _publish_control_transition(state, controller, (started,), trace)
        await _exec_tool(
            corpus,
            fts,
            "wiki_read",
            {"paths": ["index.md"]},
            "call_0",
            state,
            messages,
            trace,
            model=model,
            routing=routing,
            spans=spans,
            usage_acc=usage_acc,
            origin="forced",
        )
    else:
        trace.append(TraceEntry(event="forced_search_first", detail={"query": question}))
        controller, started = start_controller(
            question,
            directions=controller_directions,
        )
        _publish_control_transition(state, controller, (started,), trace)
        await _exec_tool(
            corpus,
            fts,
            "wiki_search",
            {"query": question},
            "call_0",
            state,
            messages,
            trace,
            model=model,
            routing=routing,
            spans=spans,
            usage_acc=usage_acc,
            origin="forced",
        )

    state["consumed_history_messages"] = 0
    final_draft: Optional[HandleFinalDraft] = None
    force_reason: Optional[str] = None
    empty_model_turns = 0
    slot_turns = 0
    protocol_errors = 0
    citation_corrections = 0
    tool_final_turn = False
    compact_answer_retry_used = False
    compact_answer_retry_pending = False
    last_model_span_id: Optional[str] = None
    finalization_requested = False
    completion_checked_request_ids: set = set()
    pending_trigger: Optional[str] = None
    pending_ids: set = set()
    pending_draft: Optional[HandleFinalDraft] = None

    def _open_advance_slots() -> list:
        control_view = controller_payload(state["controller"])
        return [
            slot for slot in control_view["slots"]
            if slot["required_for"] == "advance" and not slot["closed"]
        ]

    def _settle_not_executed(call: Any, reason: str) -> None:
        cid = call.id or "call_{}".format(state["tool_index"])
        state["tool_index"] += 1
        messages.append(_assistant_tool_message(cid, call.name, call.arguments))
        messages.append(_tool_message(
            cid, call.name,
            json.dumps({"status": "not_executed", "correctable": False,
                        "reason": reason}, ensure_ascii=False, separators=(",", ":"))))
        trace.append(TraceEntry(event="tool_not_executed", detail={
            "tool": call.name, "reason": reason}))

    async def _resolve_pending() -> str:
        """Finalize on the model's submitted draft.

        Returns ``none`` (nothing pending) or ``break`` (the caller must leave
        the loop with ``finalization_requested``/``final_draft`` set).
        """
        nonlocal finalization_requested, final_draft
        nonlocal pending_trigger, pending_draft
        trigger = pending_trigger
        pending_trigger = None
        if trigger is None:
            return "none"
        # A draft is the query model's stop decision.  No lexical gate or
        # extra sufficiency call may silently reopen retrieval.
        finalization_requested = True
        final_draft = pending_draft
        trace.append(TraceEntry(event="model_finalization_requested", detail={
            "trigger": trigger,
        }))
        return "break"

    while True:
        pending_trigger = None
        pending_ids = set()
        pending_draft = None

        if cancel:
            cancel.raise_if_cancelled()
        if budgeted_model.exhausted_reason is not None:
            force_reason = "model_budget_exhausted"
            trace.append(TraceEntry(event="query_budget_exhausted", detail={
                "reason": budgeted_model.exhausted_reason,
                "logical_calls": budgeted_model.calls,
                "reported_tokens": budgeted_model.reported_tokens,
                "estimated_tokens": budgeted_model.estimated_tokens,
                "usage_source": budgeted_model.usage_source,
                "budget_overrun_tokens": budgeted_model.budget_overrun_tokens,
                "call_accounting": budgeted_model.call_accounting,
                "elapsed_seconds": round(time.monotonic() - budgeted_model.started, 3),
            }))
            break
        # A complete workbook is already sufficient raw material for ordinary
        # reading.  Once the controller has blocked a repeated read, the next
        # model turn must be answer-only.  This transition precedes the generic
        # tool/no-progress boundaries so those guards cannot terminate before
        # the promised final-answer turn.
        if state.pop("force_answer_after_workbook", False):
            tool_final_turn = True
            trace.append(TraceEntry(
                event="workbook_answer_only_turn",
                detail={"reason": "redundant_workbook_read_blocked"},
            ))
            messages.append({"role": "user", "content": (
                "The complete workbook is already present and repeated tools "
                "cannot add evidence. Do not call more tools. Submit the answer "
                "now with the [E#] handles already read."
            )})
        if state["evidence_budget_exhausted"]:
            force_reason = _STOP_EVIDENCE_BUDGET
            break
        if state["total"] >= max_tool_calls:
            if tool_final_turn:
                force_reason = _STOP_TOOL_LIMIT
                break
            tool_final_turn = True
            messages.append({"role": "user", "content": (
                "Tool allowance exhausted. Do not call more tools. Submit your answer "
                "with the already-read [E#] citations now; mark incomplete and explain "
                "missing information if the available material is insufficient.")})
        control_state = state["controller"].state
        # X1: recompose consumed navigation history in place before every call
        # so the newest result of each tool kind stays verbatim while older
        # navigation shrinks to short statuses.
        recomposed = _recompose_consumed_history(messages, state)
        if recomposed.get("recomposed"):
            trace.append(TraceEntry(event="history_recomposed", detail=recomposed))

        if _answer_style_natural():
            from jonex_wiki.domain.engine.query.synthesis_orchestration import (
                _answer_material_messages, _answer_material_schema)
            answer_messages, evidence_count = _answer_material_messages(state, question)
            if (evidence_count and not budgeted_model.fits_retrieval_and_answer(
                    messages, TraversalResponse.model_json_schema(),
                    None if tool_final_turn else QUERY_TOOLS[:-1],
                    _traversal_max_tokens(), answer_messages,
                    answer_schema=_answer_material_schema())):
                finalization_requested = True
                trace.append(TraceEntry(event="budget_answer_only_turn", detail={
                    "reason": "reserve_answer_within_existing_budget",
                    "tokens_consumed": budgeted_model.tokens,
                    "logical_calls": budgeted_model.calls,
                }))
                break

        start_dt = datetime.now(timezone.utc)
        start_mono = time.monotonic()
        try:
            raw = await _model_complete(
                model,
                messages,
                TraversalResponse.model_json_schema(),
                max_tokens=(2048 if compact_answer_retry_pending
                            else _traversal_max_tokens()),
                tools=(None if tool_final_turn else QUERY_TOOLS[:-1]),
                thinking=({"type": "disabled"} if compact_answer_retry_pending
                          else traversal_thinking),
                scene="jonex_wiki_query_traversal",
                **routing,
            )
            state["consumed_history_messages"] = len(messages)
        except RuntimeError as exc:
            # Preserve any earlier complete draft, never the truncated output.
            error_text = str(exc)
            if "MODEL_INVALID_JSON" in error_text:
                protocol_errors += 1
                trace.append(TraceEntry(
                    event="query_protocol_rejected",
                    detail={"attempt": protocol_errors,
                            "reason": "MODEL_INVALID_JSON"},
                ))
                if state.get("final_repair_sent"):
                    force_reason = _STOP_NO_PROGRESS
                    break
                state["final_repair_sent"] = True
                messages.append({"role": "user", "content": "Return a valid JSON answer or tool call."})
                continue
            if "model output truncated" not in error_text.lower():
                raise
            has_delivered_evidence = bool(
                state.get("successful_read")
                or state["query_state"].seen_evidence_order
            )
            can_retry_compact = bool(
                not state.get("final_repair_sent") and has_delivered_evidence)
            trace.append(TraceEntry(event="traversal_output_truncated", detail={
                "budget": (2048 if compact_answer_retry_pending
                           else _traversal_max_tokens()),
                "active_evidence_count": len(_active_evidence_payload(state)),
                "recovery": ("compact_answer_retry" if can_retry_compact
                             else "stop_after_compact_retry")}))
            if can_retry_compact:
                state["final_repair_sent"] = True
                compact_answer_retry_used = True
                compact_answer_retry_pending = True
                tool_final_turn = True
                messages.append({"role": "user", "content": (
                    "Your previous output was truncated. Use only the evidence "
                    "already delivered. Do not call tools or repeat analysis. "
                    "Return one concise final answer, the supporting [E#] "
                    "handles, and the incomplete flag."
                )})
                trace.append(TraceEntry(
                    event="compact_answer_retry_requested",
                    detail={"max_tokens": 2048,
                            "evidence_count": len(
                                state["query_state"].seen_evidence_order)},
                ))
                continue
            force_reason = "model_output_truncated"
            break
        duration_ms = int((time.monotonic() - start_mono) * 1000)
        if spans is not None:
            _record_model_span(
                spans, raw, model_name, start_dt, duration_ms, usage_acc,
                parent_id=None,
            )
            last_model_span_id = spans.spans[-1].span_id
        try:
            parsed = TraversalResponse.model_validate(raw)
            compact_answer_retry_pending = False
        except (ValueError, ValidationError) as exc:
            protocol_errors += 1
            trace.append(TraceEntry(event="query_protocol_rejected", detail={
                "attempt": protocol_errors, "reason": str(exc).splitlines()[0]}))
            if state.get("final_repair_sent"):
                force_reason = _STOP_NO_PROGRESS
                break
            state["final_repair_sent"] = True
            messages.append({"role": "user", "content":
                "Return a readable answer or a valid search/read tool call."})
            continue

        if parsed.tool_calls:
            if tool_final_turn:
                force_reason = _STOP_TOOL_LIMIT
                break
            empty_model_turns = 0
            stop_batch_reason = None
            for call in parsed.tool_calls:
                if stop_batch_reason is not None:
                    _settle_not_executed(call, stop_batch_reason)
                    continue
                await _exec_tool(
                    corpus, fts, call.name, call.arguments, call.id, state, messages, trace,
                    model=model,
                            routing=routing,
                    spans=spans, usage_acc=usage_acc, parent_span_id=last_model_span_id,
                )
                control_state = state["controller"].state
                if state.get("table_fatal_error"):
                    stop_batch_reason = "table_artifact_inconsistent"
                elif state["evidence_budget_exhausted"]:
                    stop_batch_reason = _STOP_EVIDENCE_BUDGET
                elif state["total"] >= max_tool_calls:
                    stop_batch_reason = "tool_limit"
                elif state["empty_streak"] >= 3:
                    stop_batch_reason = _STOP_EMPTY_SEARCH
            if state.get("table_fatal_error"):
                break
            if state.get("force_answer_after_workbook"):
                # The workbook stop signal owns the next transition. Generic
                # readiness/no-progress resolution must not terminate between
                # the blocked redundant tool and the promised answer-only turn.
                continue
            result_signal = await _resolve_pending()
            if result_signal in ("break", "finalize"):
                break
            continue

        # Non-tool turn: a final answer or a malformed/early/empty response.
        if parsed.answer.strip() or parsed.citations:
            # Some providers correctly place [E#] citations in the answer text
            # but omit the duplicate structured array.  Recover them
            # deterministically instead of discarding valid citations.
            if parsed.answer.strip() and not parsed.citations:
                parsed.citations = list(dict.fromkeys(
                    re.findall(r"\[E[1-9][0-9]*\]", parsed.answer)
                ))
            if is_xml_tool_call_text(parsed.answer):
                # The model serialised a tool call as XML inside content (native
                # channel not used, budget pressure).  Never accept that as the
                # final answer — correct it like an empty turn.
                empty_model_turns += 1
                trace.append(
                    TraceEntry(
                        event="xml_tool_call_rejected",
                        detail={"answer_length": len(parsed.answer)},
                    )
                )
                if state.get("final_repair_sent"):
                    force_reason = _STOP_EMPTY_MODEL
                    break
                state["final_repair_sent"] = True
                messages.append({"role": "user", "content": _XML_TOOL_CALL_CORRECTION})
                continue
            state["model_declared_incomplete"] = parsed.incomplete
            if _answer_style_natural():
                # A citations-only response is the existing end-of-reading
                # signal. Legacy answers also stop reading; their text is not
                # passed to the separate, tool-free answering turn.
                pending_trigger = "model_draft"
                pending_draft = HandleFinalDraft(answer=parsed.answer,
                                                 citations=tuple(parsed.citations))
                await _resolve_pending()
                break
            # Preserve the draft before any optional local repair. Unknown
            # references and concrete computed-count conflicts share one turn.
            final_draft = HandleFinalDraft(answer=parsed.answer,
                                          citations=tuple(parsed.citations))
            allowed, invalid = _resolve_evidence_handles(
                state["query_state"], list(parsed.citations))
            conflict = (computed_answer_conflict(
                parsed.answer, state.get("table_compute_results") or [], state=state)
                or computed_count_conflict(parsed.answer, state.get("table_compute_results") or []))
            # Only an observed numerical/direction mismatch is actionable.
            # Missing citation bindings or unproven semantics are not conflicts.
            if conflict and conflict.get("reason") not in (
                    "not_a_program_result", "comparison_direction_conflict", None):
                conflict = None
            issues = {}
            if invalid:
                issues["unknown_citations"] = list(invalid)
            if conflict:
                issues["computed_result_conflict"] = conflict
            if issues:
                trace.append(TraceEntry(event="answer_local_issue", detail=issues))
                repair_messages = _local_answer_repair_messages(
                    question, parsed.answer, issues, state, messages)
                fits = getattr(budgeted_model, "fits_request", None)
                if (not state.get("final_repair_sent") and fits is not None
                        and fits(repair_messages, TraversalResponse.model_json_schema(),
                                 None, _traversal_max_tokens())):
                    state["final_repair_sent"] = True
                    trace.append(TraceEntry(event="answer_local_repair_requested", detail=issues))
                    messages[:] = repair_messages
                    tool_final_turn = True
                    continue
            state["final_local_issues"] = issues
            # A source-support check that cannot prove a claim never rejects
            # this draft. The raw-source reading fallback remains available.
            pending_trigger = "model_draft"
            pending_draft = HandleFinalDraft(answer=parsed.answer,
                                             citations=tuple(parsed.citations))
            result_signal = await _resolve_pending()
            if result_signal in ("break", "finalize"):
                break
            continue

        empty_model_turns += 1
        if empty_model_turns >= MAX_EMPTY_MODEL_TURNS:
            force_reason = _STOP_EMPTY_MODEL
            break
        messages.append({"role": "user", "content": _PRODUCE_TOOL_OR_ANSWER})

    def _final_counts() -> Dict[str, int]:
        """The count fields, read from the settled ledger *at return time*."""
        return dict(
            search_calls=state["search_calls"],
            read_calls=state["read_calls"],
            tool_calls=state["total"],
            judge_calls=state["judge_calls"],
            model_calls=budgeted_model.calls,
        )

    compiled_supported_claim_count = sum(
        len(page_claims) for page_claims in corpus.claims.values()
    )

    def _query_budget_settled_trace() -> None:
        trace.append(TraceEntry(event="query_budget_settled", detail={
            "logical_calls": budgeted_model.calls,
            "reported_tokens": budgeted_model.reported_tokens,
            "estimated_tokens": budgeted_model.estimated_tokens,
            "usage_source": budgeted_model.usage_source,
            "budget_overrun_tokens": budgeted_model.budget_overrun_tokens,
            "estimate_method": BUDGET_ESTIMATE_METHOD,
            "estimate_version": BUDGET_ESTIMATE_VERSION,
            "call_accounting": budgeted_model.call_accounting,
            "elapsed_seconds": round(time.monotonic() - budgeted_model.started, 3),
        }))

    def _programmatic_termination(stop_reason, signals, *, limit_reason=None):
        """Single exit for a run code stopped rather than a model answer."""
        failure_class = _publish_incomplete_terminal(state, signals, trace)
        detail_reason = limit_reason or stop_reason
        trace.append(TraceEntry(event="programmatic_termination", detail={
            "stop_reason": stop_reason, "limit_reason": detail_reason}))
        _query_budget_settled_trace()
        return QueryRunResult(
            answer=_programmatic_incomplete_answer(question, detail_reason),
            citations=[],
            incomplete=True,
            stop_reason=stop_reason,
            failure_class=failure_class,
            trace=trace,
            spans=spans.spans,
            token_usage=_finalize_token_usage(budgeted_model.reported_usage),
            config=runtime_config,
            **_final_counts(),
        )

    # K4: the same bounded-refusal decision guards every generic unfinished exit
    # below.  Order matters: a fatal asset inconsistency still wins, a run that
    # already carries a grounded refusal never re-derives it, and a budget stop
    # renders the facts already obtained instead of paying for another call.
    limit_stop_reason = None
    if (force_reason == "model_budget_exhausted"
            or budgeted_model.exhausted_reason is not None):
        limit_stop_reason = budgeted_model.exhausted_reason or force_reason
    if final_draft is not None and not finalization_requested:
        # The run stopped on a limit while a complete model draft was in hand.
        # The draft is preserved verbatim and only its handles are restored, so
        # an abnormal stop never discards an answer the model already produced;
        # the result stays incomplete because the readiness check never passed.
        state["model_declared_incomplete"] = True
        answer, citations, _ = await _synthesize_active_evidence(
            model, state, question, routing, trace, spans, usage_acc,
            discarded_history_count=len(messages), traversal_draft_present=True,
            selected_citations=final_draft.citations,
            preserve_answer=final_draft.answer)
        stopped = _programmatic_termination(
            force_reason or _STOP_EMPTY_MODEL,
            RuntimeFailureSignals(stop_reason=force_reason or _STOP_EMPTY_MODEL,
                                  compiled_supported_claim_count=compiled_supported_claim_count,
                                  synthesis_invalid=True,
                                  successful_read=state["successful_read"],
                                  answer_not_produced=False))
        return stopped.model_copy(update={"answer": answer, "citations": citations,
                                          "incomplete": True})
    if not state.get("grounded_incomplete"):
        grounded = _try_grounded_termination(state, question)
        if grounded is not None:
            _accept_grounded_termination(
                state, grounded, trace,
                draft=(final_draft.answer if final_draft is not None else None),
                superseded_stop_reason=(limit_stop_reason or force_reason or None))
    if state.get("grounded_incomplete") and limit_stop_reason:
        # The limit that would have ended the run and the grounded result are
        # recorded separately, so neither claim hides the other.
        trace.append(TraceEntry(event="limit_superseded_by_grounded_refusal", detail={
            "stop_reason": "model_budget_exhausted",
            "limit_reason": limit_stop_reason,
            "answer_source": "programmatic_grounded_refusal",
            "model_calls_used": budgeted_model.calls,
        }))

    if state.get("table_fatal_error"):
        # A table artifact that cannot be read after the registry accepted it is
        # a deterministic inconsistency, not a model failure.
        return _programmatic_termination(
            "table_artifact_inconsistent",
            RuntimeFailureSignals(
                stop_reason="table_artifact_inconsistent",
                compiled_supported_claim_count=compiled_supported_claim_count,
                successful_read=state["successful_read"],
                evidence_budget_exhausted=state["evidence_budget_exhausted"],
            ),
        )

    if state.get("grounded_incomplete") and state.get("grounded_incomplete_answer"):
        # A grounded incomplete is the run's answer: there is no value to
        # synthesize and no active evidence to cite.
        facts = list(state["grounded_incomplete"])
        answer = str(state["grounded_incomplete_answer"])
        failure_class = _publish_incomplete_terminal(
            state,
            RuntimeFailureSignals(
                stop_reason=_STOP_ANSWERED,
                compiled_supported_claim_count=compiled_supported_claim_count,
                successful_read=state["successful_read"],
                grounded_incomplete=True,
            ),
            trace,
        )
        _query_budget_settled_trace()
        return QueryRunResult(
            answer=answer,
            citations=[],
            incomplete=True,
            stop_reason=_STOP_ANSWERED,
            failure_class=failure_class,
            incomplete_reason="; ".join(str(fact.get("statement")) for fact in facts),
            incomplete_scope={
                "tables": sorted({str(fact.get("table_id")) for fact in facts}),
                "kinds": sorted({str(fact.get("kind")) for fact in facts}),
                "literals": sorted({str(fact.get("literal")) for fact in facts
                                    if fact.get("literal")}),
            },
            grounded_incomplete_facts=facts,
            trace=trace,
            spans=spans.spans,
            token_usage=_finalize_token_usage(budgeted_model.reported_usage),
            config=runtime_config,
            **_final_counts(),
        )

    if limit_stop_reason is not None:
        # The provider boundary was reached and no bounded refusal was available:
        # terminate programmatically instead of paying for another synthesis.
        return _programmatic_termination(
            "model_budget_exhausted",
            RuntimeFailureSignals(
                stop_reason="model_budget_exhausted",
                compiled_supported_claim_count=compiled_supported_claim_count,
                successful_read=state["successful_read"],
                answer_not_produced=True,
                evidence_budget_exhausted=state["evidence_budget_exhausted"],
            ),
            limit_reason=limit_stop_reason,
        )

    if finalization_requested:
        # Natural mode spends one tool-free answering turn on delivered
        # material. Entity mode preserves the traversal draft and restores
        # its handles deterministically. Abnormal stops above never buy this
        # answering turn, and no post-hoc table program is executed.
        if _answer_style_natural() and cancel:
            cancel.raise_if_cancelled()
        answer, citations, incomplete = await _synthesize_active_evidence(
            model,
            state,
            question,
            routing,
            trace,
            spans,
            usage_acc,
            discarded_history_count=len(messages),
            traversal_draft_present=(final_draft is not None),
            synthesis_thinking=synthesis_thinking,
            answer_only=_answer_style_natural(),
            selected_citations=(final_draft.citations if final_draft is not None
                                else ()),
            preserve_answer=(
                final_draft.answer if (
                    final_draft is not None
                    and (final_draft.answer or "").strip()
                ) else None
            ),
        )
        if (
            budgeted_model.exhausted_reason is not None
            and not (answer or "").strip()
        ):
            return _programmatic_termination(
                "model_budget_exhausted",
                RuntimeFailureSignals(
                    stop_reason="model_budget_exhausted",
                    compiled_supported_claim_count=compiled_supported_claim_count,
                    successful_read=state["successful_read"],
                    answer_not_produced=True,
                    evidence_budget_exhausted=state["evidence_budget_exhausted"],
                ),
                limit_reason=budgeted_model.exhausted_reason,
            )
        result_incomplete = (incomplete or state["evidence_budget_exhausted"]
                             or state.get("model_declared_incomplete", False) or not corpus.page_index)
        if _answer_style_natural():
            conflict = (computed_answer_conflict(
                answer, state.get("table_compute_results") or [], state=state)
                or computed_count_conflict(answer, state.get("table_compute_results") or []))
            # Natural answers have no explicit scalar/result bindings. A
            # mismatch with the last result cannot refute another step or a
            # value quoted from a source/table range. A lookup amount is not
            # a derived result, even when it cites the calculation's inputs.
            multiple_steps = any(len((item.get("payload") or {}).get("steps") or []) > 1
                                 for item in state.get("table_compute_results") or [])
            directly_cited = bool(conflict and set(conflict.get("citations_to_use") or [])
                                  & {citation.handle for citation in citations})
            if conflict and (conflict.get("reason") in ("comparison_direction_conflict", None)
                             or (conflict.get("reason") == "not_a_program_result"
                                 and conflict.get("marker")
                                 and not multiple_steps and directly_cited)):
                state.setdefault("final_local_issues", {})["computed_result_conflict"] = conflict
            elif conflict:
                trace.append(TraceEntry(event="numeric_check_advisory", detail=conflict))
        remaining_conflict = (state.get("final_local_issues") or {}).get("computed_result_conflict")
        if remaining_conflict:
            result_incomplete = True
            results = remaining_conflict.get("program_results") or []
            expected = ", ".join(str(item.get("value", "")) + str(item.get("unit") or "")
                                 for item in results)
            if not expected:
                expected = str(remaining_conflict.get("computed_count") or "")
            answer += ("\n\n计算核对：已计算结果为 {}，这部分答案仍有不一致。".format(expected)
                       if re.search(r"[\u4e00-\u9fff]", question)
                       else "\n\nCalculation check: the computed result is {}; this part of the answer remains inconsistent.".format(expected))
        answer_produced = bool(answer.strip())
        if not answer_produced:
            result_incomplete = True
            answer = _programmatic_incomplete_answer(question, _STOP_ANSWERED)
        controller_answer_rejected = False
        trace.append(TraceEntry(event="answer_delivered", detail={
            "citation_count": len(citations), "model_incomplete": result_incomplete}))
        failure_class = None
        if result_incomplete:
            # A parsed, delivered draft can honestly declare a semantic gap.
            # That is not a malformed synthesis response; keep its completion
            # flag while recording only the model's observed declaration.
            declared_semantic_gap = bool(
                state.get("model_declared_incomplete") and answer_produced
                and citations
                and not (state.get("final_local_issues") or {}).get("unknown_citations")
                and not remaining_conflict and not state["evidence_budget_exhausted"])
            failure_class = _publish_incomplete_terminal(
                state,
                RuntimeFailureSignals(
                    stop_reason=("model_declared_incomplete" if declared_semantic_gap
                                 else _STOP_ANSWERED),
                    compiled_supported_claim_count=(
                        compiled_supported_claim_count
                    ),
                    successful_read=state["successful_read"],
                    synthesis_invalid=bool(incomplete and not declared_semantic_gap),
                    grounded_incomplete=bool(remaining_conflict),
                    answer_not_produced=not answer_produced,
                    evidence_budget_exhausted=state[
                        "evidence_budget_exhausted"
                    ],
                    controller_answer_rejected=controller_answer_rejected,
                ),
                trace,
            )
        _query_budget_settled_trace()
        return QueryRunResult(
            answer=answer,
            citations=citations,
            incomplete=result_incomplete,
            stop_reason=_STOP_ANSWERED,
            failure_class=failure_class,
            trace=trace,
            spans=spans.spans,
            token_usage=_finalize_token_usage(budgeted_model.reported_usage),
            config=runtime_config,
            **_final_counts(),
        )

    if state.get("model_incomplete_answer"):
        trace.append(TraceEntry(event="model_stopped_incomplete", detail={
            "reason": "model_declared_missing_evidence",
        }))
        _query_budget_settled_trace()
        return QueryRunResult(
            answer=state["model_incomplete_answer"], citations=[], incomplete=True,
            stop_reason=_STOP_ANSWERED, failure_class=None, trace=trace,
            spans=spans.spans, token_usage=_finalize_token_usage(budgeted_model.reported_usage),
            config=runtime_config, **_final_counts(),
        )

    if not state["successful_read"]:
        no_read_reason = (
            _STOP_NO_PROGRESS
            if force_reason == _STOP_NO_PROGRESS
            else _STOP_NO_READ
        )
        failure_class = _publish_incomplete_terminal(
            state,
            RuntimeFailureSignals(
                stop_reason=no_read_reason,
                compiled_supported_claim_count=compiled_supported_claim_count,
                successful_read=False,
            ),
            trace,
        )
        trace.append(TraceEntry(event="stop", detail={"reason": no_read_reason}))
        return QueryRunResult(
            answer=_programmatic_incomplete_answer(question, no_read_reason),
            citations=[],
            incomplete=True,
            stop_reason=no_read_reason,
            failure_class=failure_class,
            trace=trace,
            spans=spans.spans,
            token_usage=_finalize_token_usage(budgeted_model.reported_usage),
            config=runtime_config,
            **_final_counts(),
        )

    # An abnormal stop never purchases an additional synthesis.  Preserve the
    # actual termination reason: a readiness check that could not produce a
    # verdict reports why, instead of hiding behind the generic stop reason.
    result_stop_reason = force_reason or _STOP_EMPTY_MODEL
    return _programmatic_termination(
        result_stop_reason,
        RuntimeFailureSignals(
            stop_reason=result_stop_reason,
            compiled_supported_claim_count=compiled_supported_claim_count,
            successful_read=state["successful_read"],
            answer_not_produced=True,
            required_slots_incomplete=(
                force_reason == _STOP_CONTROL_SLOT
            ),
            evidence_budget_exhausted=state["evidence_budget_exhausted"],
        ),
        # 这里必须传本函数作用域里的 limit_stop_reason（模型预算耗尽原因，可能为
        # None → _programmatic_termination 回落到 stop_reason）。此前写成裸
        # limit_reason（只是内层函数 _programmatic_termination 的关键字参数），
        # 走到这条「异常终止」分支就 NameError，把整次检索打成失败。
        limit_reason=limit_stop_reason,
    )


__all__ = [
    "MAX_TOOL_CALLS",
    "EVIDENCE_EXCERPT_LIMIT",
    "EVIDENCE_STALL_LIMIT",
    "MAX_ACTIVE_EVIDENCE",
    "MAX_ACTIVE_EVIDENCE_ENV",
    "QUERY_DECOMPOSE_ENV",
    "QUERY_ANSWER_STYLE_ENV",
    "MAX_DECOMPOSED_QUERIES",
    "QueryStrategy",
    "FTS5_UNAVAILABLE",
    "BLANK_QUESTION",
    "UNSUPPORTED_QUERY_STRATEGY",
    "READ_REQUIRED",
    "MALFORMED_TOOL_CALL",
    "QUERY_TOOLS",
    "SearchHit",
    "CitationDraft",
    "VerifiedCitation",
    "ToolCall",
    "TraversalResponse",
    "FinalDraft",
    "CitationExtraction",
    "SubQuestionList",
    "TraceEntry",
    "QueryCorpus",
    "QueryRunResult",
    "build_query_corpus",
    "build_fts_index_bytes",
    "run_query_algorithm",
    "_decomposition_enabled",
    "_answer_style_natural",
    "_is_composite_question",
    "_rewrite_query_for_search",
    "_segment_for_search",
]
