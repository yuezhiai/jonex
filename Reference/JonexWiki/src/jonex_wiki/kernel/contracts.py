"""Stable data types that cross the ContextEngine boundary.

All models are Pydantic v1 compatible.  No concrete engine class is imported here.
"""

from __future__ import annotations

import os
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field, model_validator, field_validator, ConfigDict


def default_models_from_env() -> Dict[str, str]:
    """role → 默认模型名。模型名唯一来源是环境变量。

    解析链 ``LLM_MODEL`` → ``DEFAULT_LLM_MODEL``（与平台侧
    ``jonex_core.common.model_config`` 同构；本模块属 kernel 层，不 import infra）。

    解析不到时返回**空 dict** 而不是空字符串：空字符串会被原样发给上游、换回一句
    语焉不详的 400；空 dict 让下游按「未配置」处理并响亮报错。

    历史上这里写死过具体模型名，上游改名后它会静默把旧名字捡回来。
    """
    model = os.getenv("LLM_MODEL", "").strip() or os.getenv("DEFAULT_LLM_MODEL", "").strip()
    if not model:
        return {}
    return {"curation": model, "query": model}

# ──────────────────────────────────────────────
#  Enums
# ──────────────────────────────────────────────


class CurationPhase(str, Enum):
    """Phases of a single curation run, presented as a step rail in the UI."""

    DELTA = "delta"
    REPLAY = "replay"
    CLEANUP = "cleanup"
    SOURCES = "sources"
    AGGREGATE = "aggregate"
    VALIDATE = "validate"
    PUBLISH = "publish"


class CurationTaskStatus(str, Enum):
    """Lifecycle states for a curation task row."""

    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"


class RevisionStatus(str, Enum):
    """Lifecycle states for an immutable WikiRevision."""

    STAGING = "staging"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    FAILED = "failed"


# ──────────────────────────────────────────────
#  EngineDescriptor
# ──────────────────────────────────────────────


class EngineDescriptor(BaseModel):
    """Stable metadata that a ContextEngine implementation advertises.

    The Design page renders a generic form from ``schema`` so that adding a
    normal parameter does not require frontend changes.
    """

    engine_id: str = Field(..., description="Unique engine identifier, e.g. 'llm_wiki'")
    engine_version: str = Field(..., description="Semantic version, e.g. '1.0.0'")
    schema_version: int = Field(
        default=1, description="Version of the schema field itself"
    )

    models: Dict[str, str] = Field(
        default_factory=dict,
        description="Role → default model name, e.g. {'curation':'<model-from-env>'}",
    )
    curation_config: Dict[str, Any] = Field(
        default_factory=dict,
        description="Default curation parameters",
    )
    query_config: Dict[str, Any] = Field(
        default_factory=dict,
        description="Default query parameters",
    )

    json_schema: Dict[str, Any] = Field(
        default_factory=dict,
        alias="schema",
        description="JSON Schema that the Design form uses to render engine settings",
    )

    model_config = ConfigDict(extra="ignore", validate_by_name=True)
# ──────────────────────────────────────────────
#  ManifestData
# ──────────────────────────────────────────────


class ManifestData(BaseModel):
    """Versioned, immutable configuration snapshot for a Context.

    Schema mirrors the plan's default manifest (implementation-plan.md § 4).
    """

    schemaVersion: int = Field(default=1, ge=1, le=1)
    engine: Dict[str, str] = Field(
        default_factory=lambda: {"id": "llm_wiki", "version": "1.0.0"},
        description="Engine identity and semantic version locked by this manifest",
    )
    models: Dict[str, str] = Field(
        default_factory=default_models_from_env,
    )
    curation: Dict[str, Any] = Field(
        default_factory=lambda: {
            "pageSelectionLimit": 5,
            "llmReviewEveryBatches": 10,
            "finalRepairRounds": 3,
            "allowPartialSources": False,
        },
    )
    query: Dict[str, Any] = Field(
        default_factory=lambda: {
            "strategy": "browse-first",
            "maxToolCalls": 32,
            "emptySearchPatience": 3,
        },
    )

    model_config = ConfigDict(extra="ignore")
    @classmethod
    def default_manifest(cls, *, engine_id: str = "llm_wiki", engine_version: str = "1.0.0") -> "ManifestData":
        """Return a ManifestData pre-filled with the plan's defaults."""
        return cls(
            schemaVersion=1,
            engine={"id": engine_id, "version": engine_version},
            models=default_models_from_env(),
            curation={
                "pageSelectionLimit": 5,
                "llmReviewEveryBatches": 10,
                "finalRepairRounds": 3,
                "allowPartialSources": False,
            },
            query={
                "strategy": "browse-first",
                "maxToolCalls": 32,
                "emptySearchPatience": 3,
            },
        )


# ──────────────────────────────────────────────
#  SourceArtifact
# ──────────────────────────────────────────────


class SourceArtifact(BaseModel):
    """Normalised document chunk produced by existing parsers.

    This is the stable projection that engines consume through
    ``SourceArtifactPort`` — it does not expose the internal structure of any
    concrete parser.
    """

    source_id: str = Field(..., description="Stable identifier for this source within the context")
    document_id: str = Field(..., description="Knowledge Base document ID")
    content_generation: int = Field(
        ...,
        description="Monotonic generation counter; incremented on every re-parse of the same document",
    )
    parser_id: str = Field(..., description="Parser type that produced this chunk, e.g. 'mineru.cloud.v1'")
    parser_version: str = Field(..., description="Parser version, carried through for provenance and replay")
    chunk_id: str = Field(..., description="Unique chunk identifier from the parser")
    text: str = Field(..., description="Chunk plain text content")
    page_number: Optional[int] = Field(default=None, description="1-based page number if available")
    position: Optional[int] = Field(default=None, description="Ordinal position of this chunk within the document")
    source_metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Opaque metadata from the parser (file type, resolution, encoding, etc.)",
    )
    sha256: str = Field(
        ...,
        description="SHA-256 hex digest of the *original* document bytes (before parsing)",
    )

    model_config = ConfigDict(extra="ignore")


# Claim policy governs how a compilation treats claim-level evidence.
ClaimPolicy = Literal["production", "benchmark"]


# ──────────────────────────────────────────────
#  Immutable publication provenance
# ──────────────────────────────────────────────


class SourceInventoryEntry(BaseModel):
    """One logical source pinned by a compiled Revision.

    ``artifact_digest`` covers the ordered parser chunks while
    ``compiler_fingerprint`` covers the effective compiler inputs.  Keeping
    both makes generation-only and content/config changes independently
    auditable.
    """

    source_id: str
    document_id: str
    content_generation: int = Field(..., ge=0)
    parser_id: str
    document_sha256: str
    artifact_count: int = Field(..., ge=1)
    artifact_digest: str
    compiler_fingerprint: str

    model_config = ConfigDict(extra="forbid")
class SourceInventorySnapshot(BaseModel):
    """Canonical, immutable source set consumed by one compilation."""

    schema_version: int = Field(default=1, ge=1, le=1)
    entries: List[SourceInventoryEntry] = Field(default_factory=list)
    digest: str

    @property
    def source_count(self) -> int:
        return len(self.entries)

    def covers(self, current_digest: str) -> bool:
        """Return whether this snapshot exactly covers ``current_digest``."""
        return bool(current_digest) and self.digest == current_digest

    model_config = ConfigDict(extra="forbid")
class CodeIdentity(BaseModel):
    """Build identity recorded without inspecting Git at request time."""

    commit: str = Field(default="unknown")
    source_tree_sha256: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    compiler_sha256: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    dirty: Optional[bool] = Field(default=None)

    model_config = ConfigDict(extra="forbid")
class EmbeddingIdentity(BaseModel):
    """Historical provenance shape; new Wiki revisions record it as disabled."""

    provider: str = Field(default="llm-gateway")
    host: str = Field(default="")
    model: str = Field(default="")
    revision: str = Field(default="")
    dimension: Optional[int] = Field(default=None, ge=1)
    status: str = Field(default="unavailable")

    model_config = ConfigDict(extra="forbid")
class EffectiveRuntimeSnapshot(BaseModel):
    """Safe, reproducible runtime inputs frozen into a bundle.

    Only an explicit allowlist of non-secret environment overrides is stored;
    credentials, prompts, source text, and provider responses never enter this
    structure.
    """

    schema_version: int = Field(default=1, ge=1, le=1)
    prompt_checksum: str
    tool_checksum: str
    models: Dict[str, str] = Field(default_factory=dict)
    embedding: EmbeddingIdentity = Field(default_factory=EmbeddingIdentity)
    environment_overrides: Dict[str, str] = Field(default_factory=dict)
    code: CodeIdentity = Field(default_factory=CodeIdentity)

    model_config = ConfigDict(extra="forbid")
class BundleProvenance(BaseModel):
    """Owner and immutable-input identity mirrored in the outer container."""

    tenant_id: str
    context_id: str
    source_inventory_digest: str
    runtime_snapshot_checksum: str

    model_config = ConfigDict(extra="forbid")
# ──────────────────────────────────────────────
#  Runtime telemetry (LWP-OBS-CONTRACT-001)
# ──────────────────────────────────────────────


_RUNTIME_METRIC_NAME_PATTERN = r"^[a-z][a-z0-9_.-]{0,63}$"


def _validate_reported_runtime_fields(self):
    """Reject fabricated totals while preserving genuinely unknown values."""

    provider_calls = self.provider_call_count
    usage_reports = self.usage_reported_calls or 0
    retry_reports = self.retry_reported_calls or 0
    cache_reports = self.cache_reported_calls or 0

    if usage_reports > 0 and provider_calls is None:
        raise ValueError("provider usage reports require an observed call count")
    if retry_reports > 0 and provider_calls is None:
        raise ValueError("provider retry reports require an observed call count")
    if provider_calls is not None and usage_reports > provider_calls:
        raise ValueError("usage_reported_calls cannot exceed provider_call_count")
    if provider_calls is not None and retry_reports > provider_calls:
        raise ValueError("retry_reported_calls cannot exceed provider_call_count")

    token_values = (self.input_tokens, self.output_tokens)
    if usage_reports == 0 and any(value is not None for value in token_values):
        raise ValueError("token totals require at least one provider usage report")
    if usage_reports > 0 and any(value is None for value in token_values):
        raise ValueError("reported provider usage requires both token totals")

    retries = self.provider_retries
    if retry_reports == 0 and retries is not None:
        raise ValueError("provider_retries requires at least one retry report")
    if retry_reports > 0 and retries is None:
        raise ValueError("reported retry data requires provider_retries")

    cache_values = (self.cache_hits, self.cache_misses)
    if cache_reports == 0 and any(value is not None for value in cache_values):
        raise ValueError("cache totals require at least one cache report")
    if cache_reports > 0 and any(value is None for value in cache_values):
        raise ValueError("reported cache data requires hit and miss totals")
    if cache_reports > 0 and sum(cache_values) != cache_reports:
        raise ValueError("cache_hits + cache_misses must equal cache_reported_calls")

    return self


class RuntimeStageMetrics(BaseModel):
    """Aggregated measurements for one first-seen, repeatable runtime stage.

    ``wall_ms`` is elapsed monotonic wall time. ``cpu_ms`` is process CPU time,
    not a per-thread allocation. ``rss_peak_bytes`` is the process resident-set
    high-water mark observed at stage boundaries, in bytes. Nested stages may
    overlap, so summing their wall or CPU values can exceed the enclosing run.

    Provider call count, usage, retry, and cache totals remain ``None`` until
    the relevant upstream explicitly reports them. Companion
    ``*_reported_calls`` counters distinguish a real reported zero from
    unavailable telemetry.
    """

    name: str = Field(..., pattern=_RUNTIME_METRIC_NAME_PATTERN)
    count: int = Field(default=1, ge=1)
    wall_ms: float = Field(..., ge=0)
    cpu_ms: Optional[float] = Field(default=None, ge=0)
    rss_peak_bytes: Optional[int] = Field(default=None, ge=0)
    errors: int = Field(default=0, ge=0)

    provider_call_count: Optional[int] = Field(default=None, ge=0)
    usage_reported_calls: int = Field(default=0, ge=0)
    input_tokens: Optional[int] = Field(default=None, ge=0)
    output_tokens: Optional[int] = Field(default=None, ge=0)
    retry_reported_calls: int = Field(default=0, ge=0)
    provider_retries: Optional[int] = Field(default=None, ge=0)
    cache_reported_calls: int = Field(default=0, ge=0)
    cache_hits: Optional[int] = Field(default=None, ge=0)
    cache_misses: Optional[int] = Field(default=None, ge=0)

    _reported_fields_are_consistent = model_validator(mode="after")(_validate_reported_runtime_fields)

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
class PreviousBundleSkippedNote(BaseModel):
    """Structured reason a curation run skipped its previous bundle.

    LWP-CURATE-RECOVERY (P2-1): carried as an optional, strictly-typed
    ``RuntimeTelemetry.previous_bundle_skipped`` field.  ``reason`` is a closed
    literal; ``schema_version`` is the version found in the incompatible
    container so consumers can audit the schema upgrade.  ``extra=forbid`` keeps
    the note itself closed to arbitrary metadata.
    """

    reason: Literal["schema_incompatible"]
    schema_version: int

    model_config = ConfigDict(extra="forbid")
class RuntimeTelemetry(BaseModel):
    """Versioned JSON-safe resource report for one compile, query, or run.

    Run-level wall/CPU/RSS measurements cover the recorder lifetime. Stage
    measurements are independently aggregated and therefore are not additive
    when stages overlap. No prompt, source content, credentials, or arbitrary
    metadata are accepted by this public contract.
    """

    contract: Literal["llm_wiki.runtime_telemetry.v1"] = "llm_wiki.runtime_telemetry.v1"
    run_kind: str = Field(..., pattern=_RUNTIME_METRIC_NAME_PATTERN)
    status: str = Field(..., pattern=r"^(running|completed|failed|cancelled)$")
    started_at: str = Field(..., min_length=1)
    wall_ms: float = Field(..., ge=0)
    cpu_ms: Optional[float] = Field(default=None, ge=0)
    rss_peak_bytes: Optional[int] = Field(default=None, ge=0)
    stages: List[RuntimeStageMetrics] = Field(default_factory=list)
    work_counters: Dict[str, int] = Field(default_factory=dict)

    # LWP-CURATE-RECOVERY (P2-1): additive optional field, added under the
    # unchanged ``llm_wiki.runtime_telemetry.v1`` contract string.  It is
    # strictly typed and non-null only when a curation run skipped its previous
    # bundle (schema evolution); consumers must ignore it when absent/null.
    previous_bundle_skipped: Optional[PreviousBundleSkippedNote] = Field(
        default=None,
        description=(
            "Non-null only when a curation run skipped its previous bundle "
            "(schema_incompatible); carries the reason and the schema_version "
            "found in the skipped container."
        ),
    )

    provider_call_count: Optional[int] = Field(default=None, ge=0)
    usage_reported_calls: int = Field(default=0, ge=0)
    input_tokens: Optional[int] = Field(default=None, ge=0)
    output_tokens: Optional[int] = Field(default=None, ge=0)
    retry_reported_calls: int = Field(default=0, ge=0)
    provider_retries: Optional[int] = Field(default=None, ge=0)
    cache_reported_calls: int = Field(default=0, ge=0)
    cache_hits: Optional[int] = Field(default=None, ge=0)
    cache_misses: Optional[int] = Field(default=None, ge=0)

    _reported_fields_are_consistent = model_validator(mode="after")(_validate_reported_runtime_fields)

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
# ──────────────────────────────────────────────
#  WikiRevision
# ──────────────────────────────────────────────


class WikiRevision(BaseModel):
    """Immutable bundle metadata for one successful curation run."""

    revision_id: str = Field(..., description="Unique revision identifier")
    context_id: str = Field(..., description="Owner Context ID")
    tenant_id: str = Field(..., description="Owning tenant")
    engine_id: str = Field(..., description="Engine that produced this revision, e.g. 'llm_wiki'")
    engine_version: str = Field(..., description="Engine semantic version at compile time")
    manifest_checksum: str = Field(
        ...,
        description="SHA-256 of the active Manifest's canonical JSON at curation time",
    )
    bundle_key: str = Field(
        ...,
        description="Object-storage key pointing to the bundle",
    )
    bundle_sha256: str = Field(..., description="SHA-256 of the bundle")
    source_inventory_digest: str = Field(
        default="",
        description="Digest of the exact logical Source set compiled into the bundle",
    )
    runtime_snapshot_checksum: str = Field(
        default="",
        description="Checksum of the effective runtime snapshot stored in the bundle",
    )
    source_count: int = Field(default=0, description="Number of pinned sources")
    page_count: int = Field(default=0, description="Number of Wiki pages produced")
    claim_count: int = Field(default=0, description="Number of claims in claims.jsonl")
    status: RevisionStatus = Field(default=RevisionStatus.STAGING)
    stats: Dict[str, Any] = Field(
        default_factory=dict,
        description="Compile statistics: token usage, elapsed seconds, error counts, etc.",
    )
    created_at: datetime = Field(default_factory=datetime.utcnow, description="UTC timestamp when this revision was created")

    model_config = ConfigDict(extra="ignore")
    def covers_source_inventory(self, current_digest: str) -> bool:
        """Return whether this Revision covers the current Source snapshot."""
        return bool(current_digest) and self.source_inventory_digest == current_digest


# ──────────────────────────────────────────────
#  Query contracts (Phase C)
# ──────────────────────────────────────────────


class QueryStrategy(str, Enum):
    DIRECT = "direct"
    SEARCH_FIRST = "search-first"
    BROWSE_FIRST = "browse-first"
    AUTO = "auto"


class QueryEventType(str, Enum):
    RUN_STARTED = "run.started"
    STRATEGY_SELECTED = "strategy.selected"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    MODEL_CALL = "model.call"
    EVIDENCE_ADDED = "evidence.added"
    SYNTHESIS_STARTED = "synthesis.started"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"


class Citation(BaseModel):
    """A verified citation from a claim's EvidenceRef."""

    document_id: str = Field(..., description="Source document ID")
    chunk_id: str = Field(..., description="Parse chunk ID")
    location: str = Field(default="", description="Page or position reference")
    excerpt: str = Field(default="", description="Verbatim excerpt from the source", max_length=500)
    claim_id: str = Field(default="", description="ID of the claim this citation backs")


class QueryRequest(BaseModel):
    """A query submitted by a user within a Session.

    An Agent may pass an explicit ``workflow_id`` to select a DomainProfile
    workflow.
    """

    question: str = Field(..., min_length=1, max_length=4096)
    strategy: QueryStrategy = Field(default=QueryStrategy.BROWSE_FIRST)
    model: Optional[str] = Field(default=None, description="Override default query model")
    run_id: Optional[str] = Field(
        default=None,
        description="Owner Query Run ID — used to derive deterministic trace span ids",
    )
    workflow_id: Optional[str] = Field(
        default=None,
        description="Explicit DomainProfile workflow selection (Agent may pass it through the query request)",
    )
    document_scope: Tuple[str, ...] = Field(
        default=(),
        description=(
            "Explicit source document IDs selected by the user/session. Gold or "
            "reference-source metadata must never populate this field."
        ),
    )
    thinking_traversal: str = Field(default="scene_default")
    thinking_synthesis: str = Field(default="scene_default")

    @field_validator("document_scope")
    def _valid_document_scope(cls, values: Tuple[str, ...]) -> Tuple[str, ...]:
        if len(values) != len(set(values)) or any(not value.strip() for value in values):
            raise ValueError("QUERY_DOCUMENT_SCOPE_INVALID")
        return values

    @field_validator("thinking_traversal", "thinking_synthesis")
    def _valid_thinking(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if normalized not in {"scene_default", "on", "off", "enabled", "disabled"}:
            raise ValueError("QUERY_THINKING_INVALID")
        return normalized


class TraceSpanType(str, Enum):
    """Public span kinds carried on ``payload.trace_span`` (frozen CR-TRACE-002)."""

    TOOL = "tool"
    MODEL = "model"
    STEP = "step"
    SYNTHESIS = "synthesis"
    EVIDENCE = "evidence"


class TraceSpan(BaseModel):
    """One auditable execution span produced by the query engine.

    Frozen wire shape for ``payload.trace_span``:

    - ``span_id`` is unique and deterministic (``run:<run-id>:span:<n>``);
    - ``parent_id`` encodes nesting (``None`` for a root span);
    - ``started_at`` / ``duration_ms`` are real measurements or ``None``;
    - ``input_tokens`` / ``output_tokens`` come only from Provider usage, and
      are ``None`` (never fabricated 0) when the provider reported nothing;
    - ``input_summary`` / ``result_summary`` / ``evidence`` carry only public
      information (never prompts, credentials, full source text, or CoT).

    ``event_type`` is an internal worker hint (a ``QueryEventType`` value) that
    is stripped before the span is serialized onto the wire.
    """

    span_id: str = Field(..., description="Unique deterministic span id")
    parent_id: Optional[str] = Field(default=None, description="Parent span id (nesting)")
    seq: int = Field(default=0, ge=0, description="Span ordinal within the run")
    type: TraceSpanType = Field(..., description="tool | model | step | synthesis | evidence")
    title: str = Field(default="", description="Public human-readable title")
    tool: Optional[str] = Field(default=None, description="Tool function name when type=tool")
    category: Optional[str] = Field(
        default=None, description="RETRIEVAL | IO (public workflow category)"
    )
    started_at: Optional[str] = Field(default=None, description="ISO-8601 UTC start (real measurement)")
    duration_ms: Optional[int] = Field(default=None, description="Real measured duration, or None")
    model: Optional[str] = Field(default=None, description="Model name for type=model")
    input_tokens: Optional[int] = Field(default=None, description="Provider prompt tokens, or None")
    output_tokens: Optional[int] = Field(default=None, description="Provider completion tokens, or None")
    input_summary: Optional[str] = Field(default=None, description="Public input summary")
    result_summary: Optional[str] = Field(default=None, description="Public result summary")
    evidence: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Public evidence refs (passage_id/source_id/excerpt), never full source",
    )
    decision: str = Field(default="n/a", description="decide | execute | n/a")
    event_type: Optional[str] = Field(
        default=None,
        description="Internal worker hint (QueryEventType value); stripped before wire serialization",
    )

    model_config = ConfigDict(extra="ignore")
class QueryResult(BaseModel):
    """Final result of a completed Query Run."""

    answer: str = Field(default="", description="Final answer text")
    citations: List[Citation] = Field(default_factory=list)
    incomplete: bool = Field(default=False, description="True when evidence is insufficient")
    token_usage: Dict[str, int] = Field(default_factory=dict)
    latency_ms: int = Field(default=0)
    tool_call_count: int = Field(default=0)


class QueryEvent(BaseModel):
    """A single trace event emitted during query execution."""

    run_id: str = Field(..., description="Owner Query Run ID")
    seq: int = Field(..., ge=1, description="Monotonic sequence number within the run")
    type: QueryEventType = Field(..., description="Event type")
    timestamp: str = Field(default="", description="ISO-8601 timestamp")
    payload: Dict[str, Any] = Field(default_factory=dict, description="Event-specific data")

    model_config = ConfigDict(extra="ignore")
class EvidenceSpan(BaseModel):
    """An evidence reference collected during tool execution."""

    document_id: str = Field(...)
    chunk_id: str = Field(...)
    location: str = Field(default="")
    excerpt: str = Field(default="", max_length=500)
    page_path: str = Field(default="", description="Wiki page that contains the evidence")
    claim_index: int = Field(default=-1, description="Index of the claim in claims.jsonl")


class ToolCall(BaseModel):
    """A single tool invocation record in the trace."""

    tool_name: str = Field(..., description="wiki_search | wiki_read")
    tool_input: Dict[str, Any] = Field(default_factory=dict)
    result_summary: str = Field(default="", description="Summary of tool output (no chain-of-thought)")
    evidence_ids: List[str] = Field(default_factory=list, description="EvidenceRef IDs collected")


class SearchHit(BaseModel):
    """A single FTS5 search result."""

    path: str = Field(...)
    title: str = Field(...)
    snippet: str = Field(default="")
    score: float = Field(default=0.0)


class ReadResult(BaseModel):
    """Result of a wiki_read tool call."""

    path: str = Field(...)
    title: str = Field(default="")
    content: str = Field(default="")
    claims: List[Dict[str, Any]] = Field(default_factory=list, description="Claims from claims.jsonl for this page")


class QueryMetrics(BaseModel):
    """Aggregate metrics produced at the end of a Query Run."""

    strategy: QueryStrategy = Field(default=QueryStrategy.BROWSE_FIRST)
    tool_calls: int = Field(default=0)
    search_calls: int = Field(default=0)
    read_calls: int = Field(default=0)
    empty_searches: int = Field(default=0)
    evidence_collected: int = Field(default=0)
    citations_verified: int = Field(default=0)
    incomplete: bool = Field(default=False)
    answer_len: int = Field(default=0)


# ──────────────────────────────────────────────
#  Convenience aggregates
# ──────────────────────────────────────────────


class CurationTaskSummary(BaseModel):
    """Lightweight task row returned in list endpoints."""

    task_id: str = Field(...)
    context_id: str = Field(...)
    status: CurationTaskStatus = Field(...)
    phase: Optional[CurationPhase] = Field(default=None)
    progress_pct: int = Field(default=0, ge=0, le=100)
    message: Optional[str] = Field(default=None)
    attempt: int = Field(default=1, ge=1)
    created_at: Optional[datetime] = Field(default=None)
    updated_at: Optional[datetime] = Field(default=None)

    model_config = ConfigDict(extra="ignore")
class CurationTaskDetail(CurationTaskSummary):
    """Full task row returned by the GET /curations/{task_id} endpoint."""

    error: Optional[Dict[str, Any]] = Field(default=None)
    usage: Optional[Dict[str, Any]] = Field(default=None)
    lease_expires_at: Optional[datetime] = Field(default=None)

    model_config = ConfigDict(extra="ignore")
__all__ = [
    "BundleProvenance",
    "Citation",
    "CodeIdentity",
    "CurationPhase",
    "CurationTaskDetail",
    "CurationTaskStatus",
    "CurationTaskSummary",
    "EngineDescriptor",
    "EmbeddingIdentity",
    "EffectiveRuntimeSnapshot",
    "EvidenceSpan",
    "ManifestData",
    "QueryEvent",
    "QueryEventType",
    "QueryMetrics",
    "QueryRequest",
    "QueryResult",
    "QueryStrategy",
    "ReadResult",
    "RevisionStatus",
    "RuntimeStageMetrics",
    "RuntimeTelemetry",
    "SearchHit",
    "SourceArtifact",
    "SourceInventoryEntry",
    "SourceInventorySnapshot",
    "ToolCall",
    "TraceSpan",
    "TraceSpanType",
    "WikiRevision",
]
