"""Query-facing DTOs used by the retrieval engine (lwp-arch-001 QA-1).

Extracted verbatim from ``engines/llm_wiki_plus/query_algorithm.py`` so the
retrieval module can be split out without an import cycle.  The old import
path stays available through the re-export shim; public field names and
validation semantics are unchanged.
"""

# NOTE: this module deliberately does NOT use ``from __future__ import
# annotations``: pydantic v1 cannot resolve ``Literal[...]`` forward refs
# (used by ToolCall/TraversalResponse) when schema() is requested under lazy
# annotations, and every annotation here refers to eagerly-imported names.

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator, field_validator, field_serializer, ConfigDict

from jonex_wiki.domain.engine.models.compiler_state import (
    PageIndexEntry,
    canonical_wiki_path,
    normalize_wiki_path,
)
from jonex_wiki.domain.engine.compile.content_validation import ValidatedClaim


class SearchHit(BaseModel):
    """One ranked search hit exposing the numeric FTS rank."""

    path: str
    rank: float
    title: str = ""
    aliases: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    description: str = ""
    # The numeric BM25 score is retained for lexical retrieval diagnostics.
    bm25_score: Optional[float] = None
    # Wave 4 Q1: a query-anchored body snippet so the model can judge whether a
    # hit actually contains the entity/evidence it needs before choosing to read
    # it — reduces wasted wiki_read calls on multi-hop corpora (musique).
    snippet: str = ""
    # Wave 5 evidence units: up to three claim/passage excerpts from this page
    # that actually match the query, so wiki_search surfaces paragraph/claim
    # level evidence instead of only page metadata.
    evidence_units: List[str] = Field(default_factory=list)
    scope: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")
    @field_validator("path")
    def _validate_path(cls, v: str) -> str:
        return canonical_wiki_path(v)


class RerankResponse(BaseModel):
    """Ordered paths returned by the optional LLM reranker."""

    paths: List[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")
class TraceEntry(BaseModel):
    """One auditable trace event: an action, a tool result or a decision."""

    event: str
    detail: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")
class SubQuestionList(BaseModel):
    """A bounded list of self-contained sub-questions from query decomposition.

    Each sub-question must stand alone and carry its own context entities so it
    can be searched independently without entity drift.
    """

    sub_questions: List[str] = Field(default_factory=list)
    requires_evidence_bridge: bool = Field(
        default=False,
        description=(
            "True only for an ordered dependency chain whose next search must "
            "be grounded by a value learned from earlier evidence."
        ),
    )

    model_config = ConfigDict(extra="forbid")
class RawWikiPage(BaseModel):
    """One prepared raw-readable page: full article text or serialized table cells.

    ``content_kind`` is a display/source-type marker, never an admission gate.
    ``source`` pages are original text slices; ``table_raw`` pages serialize the
    already-saved cells with their original values, formulas and merge ranges.
    """

    path: str
    body: str
    content_kind: Literal["source", "table_raw"]
    source_id: str
    table_id: Optional[str] = None
    title: str = ""
    aliases: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    structure_status: Optional[str] = None
    row_bounds: Optional[Dict[str, int]] = None
    related_pages: List[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")
    @field_validator("path")
    def _validate_path(cls, v: str) -> str:
        return normalize_wiki_path(v)

    @field_validator("source_id")
    def _validate_source_id(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("source_id must be non-blank")
        return v

    @field_validator("title")
    def _validate_title(cls, v: str) -> str:
        return (v or "")[:200]


class WorkbookOutlineDate(BaseModel):
    """One date a workbook's own cells resolve to, and where that column sits.

    ``date`` is the normalized ISO day; ``weekday`` is derived from it, never
    from the sheet's own header, because a header may hold an Excel serial
    number instead of a weekday name.  ``raw_value`` preserves what the sheet
    literally shows so the two can be compared.
    """

    date: str
    weekday: str
    column_index: int
    raw_value: str = ""

    model_config = ConfigDict(extra="forbid")


class WorkbookOutlineGroup(BaseModel):
    """One row band delimited by a vertical merge in the table's first column."""

    row_start: int
    row_end: int
    label: str = ""
    members: List[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class WorkbookOutlineTable(BaseModel):
    """Skeleton of one logical table, read from the full grid."""

    table_id: str
    sheet: str
    title: str = ""
    row_start: int
    row_end: int
    column_start: int
    column_end: int
    status: str
    dates: List[WorkbookOutlineDate] = Field(default_factory=list)
    groups: List[WorkbookOutlineGroup] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class WorkbookOutline(BaseModel):
    """A native spreadsheet's structure, collected before corpus build.

    Carries only what the grid says about its own shape — sheet and table
    inventory, date-to-column alignment, and merged row bands.  It never carries
    cell values: those stay in the grid, so a generated skeleton page cannot
    become a second, staler source of truth.
    """

    source_id: str
    source_name: str = ""
    sheet_count: int = 0
    tables: List[WorkbookOutlineTable] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class QueryCorpus(BaseModel):
    """A rendered Wiki corpus keyed without the ``wiki/`` prefix.

    Owns the UTF-8 Markdown files, per-page index metadata and body text, and
    revalidated ``SUPPORTED`` claims grouped by canonical knowledge-page path.
    """

    files: Dict[str, str]
    revision_id: str = "unpublished"
    page_index: Dict[str, PageIndexEntry]
    page_text: Dict[str, str]
    claims: Dict[str, List[ValidatedClaim]] = Field(default_factory=dict)
    # S6.3: the audited links published for each page (target, reason, kind).
    # Navigation only - a delivered link is never evidence.
    fact_mentions: Dict[str, List[str]] = Field(default_factory=dict, exclude=True)
    page_links: Dict[str, List[Dict[str, Any]]] = Field(default_factory=dict, exclude=True)
    source_scopes: Dict[str, Dict[str, Any]] = Field(default_factory=dict, exclude=True)
    # Source records have source_span_* identities, never claim_* identities.
    # The legacy citation envelope carries record_id in its claim_id slot.
    source_records: Dict[str, List[Dict[str, str]]] = Field(default_factory=dict)
    source_locations: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    # keep-and-search-v1 raw fallback: generated page path → the source-unit
    # pages carrying the exact intervals that page was compiled from.  It is
    # populated only for pages whose claims were never admitted
    # (``support_status != SUPPORTED``): such a page registers no claim
    # evidence on read, so without this index the read gate stays closed even
    # though the published interval is still archived and raw-readable.  Pages
    # that already have admitted claims, and legacy rule versions, leave this
    # empty and keep byte-identical behaviour.
    #
    # Derived at corpus construction, so it is excluded from ``model_dump()``:
    # the published ``query/corpus.json`` snapshot hashes that dump, and a new
    # serialized key would invalidate every already-published bundle.
    evidence_fallback: Dict[str, List[str]] = Field(
        default_factory=dict, exclude=True)
    # LWP-RET-002 long-document routing (per-query runtime axis, never part of
    # effective-config).  All four fields are optional with defaults so every
    # existing QueryCorpus construction is unchanged; the route layer sets them
    # from ``query_orchestration.run_query`` after ``build_query_corpus``.
    #
    # ``route`` — resolved route value ("proposition"/"lexical"/"chapter_tree"/
    # "summary"); the traversal controller reads it to make retrieval route
    # aware without touching ``query_algorithm`` / ``contracts``.
    route: Optional[str] = None
    # D-5: page_number derived query-side from ``source_metadata`` as an
    # independent map (PageIndexEntry stays frozen).  wiki path → page number.
    page_number_map: Dict[str, int] = Field(default_factory=dict)
    # Raw-source fallback (P0): an injected on-demand single-segment reader
    # keyed by SourceSpan identity (source_id/document_id/chunk_id +
    # start/end).  ``None`` (the default) disables the fallback entirely —
    # the whole source archive is never re-injected into the corpus.
    raw_source_reader: Optional[Any] = None
    # "off" (default) | "production" | "matched" — raw fallback mode.  Only
    # explicit production/matched callers enable it; the default stack is
    # byte-identical to the pre-route hybrid.
    raw_fallback_mode: str = "off"

    @field_serializer('source_records','source_locations','page_links','fact_mentions')
    def _serialize_mappings(self,value):
        return dict(value)

    model_config = ConfigDict(extra="forbid")
    @model_validator(mode="after")
    def _check_corpus(self):
        files = getattr(self, "files", {})
        page_index = getattr(self, "page_index", {})
        page_text = getattr(self, "page_text", {})
        claims = getattr(self, "claims", {})

        for key in files:
            if key.startswith("wiki/"):
                raise ValueError("files must be keyed without the wiki/ prefix")

        for path in page_index:
            if path == "index.md" or path.endswith("/index.md"):
                raise ValueError("page_index must contain knowledge pages only")
            if path not in files:
                raise ValueError("indexed path missing from files")
            if path not in page_text:
                raise ValueError("page_text missing for indexed page")

        if set(page_text) != set(page_index):
            raise ValueError("page_text keys must equal page_index keys")

        for path, page_claims in claims.items():
            if path not in page_index:
                raise ValueError("claims grouped under a non-knowledge page")
            for claim in page_claims:
                if claim.page_path != path:
                    raise ValueError("claim group must equal claim page_path")
        return self


class CitationDraft(BaseModel):
    """A citation as produced by the model: six identity/excerpt fields."""

    claim_id: str
    passage_id: str
    source_id: str
    document_id: str
    chunk_id: str
    excerpt: str

    model_config = ConfigDict(extra="forbid")
    @field_validator("claim_id", "passage_id", "source_id", "document_id", "chunk_id")
    def _non_blank_id(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("id must be non-blank")
        return v

    @field_validator("excerpt")
    def _non_blank_excerpt(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("excerpt must be non-blank")
        return v


class VerifiedCitation(CitationDraft):
    """A citation that exactly matches an evidence record exposed by a read."""

    # [jonex] 承载该证据的 Wiki 页路径（引擎侧页面路径，如 concepts/xxx.md）。
    # 由后端在还原引用时从证据记录带上；模型永远不写它——CitationDraft 没有这个字段，
    # 模型输出里若出现它会被 extra=forbid 直接判非法。
    page_path: str = ""

    # [jonex] 承载该引用的证据短句柄（如 "[E1]"）。平台侧靠它把最终答案正文里的
    # [E#] 内联引用标记映射回具体 citation，再改写为合并引用卡片下标 [n]。模型永远
    # 不写它——CitationDraft 没有这个字段，模型输出里若出现它会被 extra=forbid 判非法。
    handle: str = ""

    # A programmatic location record for a table cell.  It is attached by the
    # runtime from the read that exposed the handle and is never written by the
    # model; it carries the pinned revision, the exact cell, its context cells
    # and the value hash that a cold re-open must reproduce.
    evidence_ref: Optional[Dict[str, Any]] = None


class FinalDraft(BaseModel):
    """Strict structured final answer used by synthesis and citation repair."""

    answer: str = Field(default="")
    citations: List[CitationDraft] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")
class CitationExtraction(BaseModel):
    """Citations-only structured output for free-text answer grounding.

    Demo-stack finding: extraction previously asked the model to *rewrite* the
    full natural-language answer plus its citations, which on long answers made
    the model emit a large output near the token budget (slow, truncated JSON,
    all citations dropped).  Asking for the citations array only keeps the JSON
    small and stable; the traversal answer is kept verbatim as the final answer.
    ``extra`` tolerates a model that echoes the answer field anyway.
    """

    citations: List[CitationDraft] = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore")
# S2G-RAG controller (arXiv 2604.23783): structured sufficiency + gap judging.
# The judge reads only (question, accumulated evidence) and emits a binary
# sufficiency decision plus explicit gap items describing the missing fact for
# the next hop.  Gap-guided query construction turns the first K gaps into the
# next retrieval query (target+slot, else description), appended to the
# original question.
class RelationCheck(BaseModel):
    """Bounded entailment check for one node/candidate/evidence combination."""

    status: str
    reason: str = ""

    @field_validator("status")
    def _status(cls, value: str) -> str:
        if value not in {"supported", "contradicted", "insufficient"}:
            raise ValueError("RELATION_CHECK_STATUS_INVALID")
        return value

    model_config = ConfigDict(extra="forbid")
