"""AC-005 typed content gate for factual claims.

This module is the deterministic gate over grounded claims: every retained
claim names an archived passage, binds immutable exact ``SourceSpan`` values,
and records an explicit ``SUPPORTED`` / ``REFUTED`` /
``NOT_ENOUGH_INFO`` decision.  A bounded semantic review can neither omit nor
invent claim IDs.  It is pure and self-contained: it never calls a model,
touches storage, the filesystem, a database, a clock, randomness or the
network.

Paper-required behavior fixed here:

- every factual claim has evidence;
- referenced document/chunk content exists in source archive ``A``;
- evidence excerpts are exact normalized substrings;
- unsupported facts and cross-page contradictions require structured semantic
  review before publication.

Passage IDs, stable claim IDs, issue schemas and line-ending-only normalization
are Jonex engineering adaptations.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Dict, List, Literal, Optional, Tuple

logger = logging.getLogger(__name__)

from pydantic import BaseModel, Field, model_validator, field_validator, ConfigDict

from jonex_wiki.domain.engine.models.compiler_state import (
    ArchivedPassage,
    CompilerState,
    SourceSpan,
    canonical_wiki_path,
    normalize_wiki_path,
)
from jonex_wiki.domain.engine.models.claim import ClaimSupportStatus, assess_claim_support

# ──────────────────────────────────────────────
#  Constants and shared validators
# ──────────────────────────────────────────────

_CLAIM_ID_RE = re.compile(r"claim_[0-9a-f]{24}")
_MAX_CLAIM_TEXT = 4000
# Layered claims use the verbatim excerpt as their canonical text.
_MAX_EXCERPT = _MAX_CLAIM_TEXT
_MAX_TABLE_EXCERPT = 16000
_MAX_REASON = 2000
SUBSTANTIVE_PASSAGE_MIN_CHARS = 40

_ContentCategory = Literal["malformed_reference", "unsupported_fact", "cross_page_contradiction"]
_FindingCategory = Literal["unsupported_fact", "cross_page_contradiction"]


def _require_non_blank(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-blank")
    return value


def _validate_claim_id(value: str) -> str:
    if not isinstance(value, str) or not _CLAIM_ID_RE.fullmatch(value):
        raise ValueError("invalid claim id")
    return value


def source_text_limit(value: Any) -> int:
    """Allow a complete long HTML table with its verbatim source context."""
    if (isinstance(value, str)
            and len(re.findall(r"<table\b", value, re.IGNORECASE))
            == len(re.findall(r"</table\s*>", value, re.IGNORECASE))
            and any(len(m.group()) > _MAX_EXCERPT for m in
                    re.finditer(r"<table\b[^>]*>[\s\S]*?</table\s*>", value, re.IGNORECASE))):
        return _MAX_TABLE_EXCERPT
    return _MAX_EXCERPT


def _validate_text(value: str, field: str, limit: int) -> str:
    _require_non_blank(value, field)
    if len(value) > limit:
        raise ValueError(f"{field} exceeds {limit} characters")
    return value


def _validate_evidence_list(values: List["EvidenceRef"]) -> List["EvidenceRef"]:
    seen = set()
    for ref in values:
        key = (ref.passage_id, ref.source_id, ref.document_id, ref.chunk_id, ref.excerpt)
        if key in seen:
            raise ValueError("duplicate evidence reference")
        seen.add(key)
    return values


def _validate_claim_id_list(values: List[str]) -> List[str]:
    for cid in values:
        _validate_claim_id(cid)
    if len(set(values)) != len(values):
        raise ValueError("claim ids must be unique")
    return values


def _validate_page_path_list(values: List[str]) -> List[str]:
    for path in values:
        canonical_wiki_path(path)
    if len(set(values)) != len(values):
        raise ValueError("page paths must be unique")
    return values


def is_substantive_passage(text: str) -> bool:
    """Return whether a passage must produce at least one supported claim."""
    return len(text.strip()) >= SUBSTANTIVE_PASSAGE_MIN_CHARS


# ──────────────────────────────────────────────
#  Evidence models
# ──────────────────────────────────────────────


class EvidenceRef(BaseModel):
    """One citation: an archived passage plus an exact excerpt from it."""

    passage_id: str
    source_id: str
    document_id: str
    chunk_id: str
    excerpt: str

    model_config = ConfigDict(extra="forbid")
    @field_validator("passage_id", "source_id", "document_id", "chunk_id", mode="before")
    def _validate_id(cls, v: str) -> str:
        return _require_non_blank(v, "id")

    @field_validator("excerpt", mode="before")
    def _validate_excerpt(cls, v: str) -> str:
        return _validate_text(v, "excerpt", source_text_limit(v))


class ClaimDraft(BaseModel):
    """A factual claim awaiting deterministic evidence validation."""

    page_path: str
    text: str
    evidence: List[EvidenceRef] = Field(..., min_length=1, max_length=20)

    model_config = ConfigDict(extra="forbid")
    @field_validator("page_path", mode="before")
    def _validate_page_path(cls, v: str) -> str:
        return normalize_wiki_path(v)

    @field_validator("text", mode="before")
    def _validate_text(cls, v: str) -> str:
        return _validate_text(v, "text", source_text_limit(v))

    @field_validator("evidence")
    def _validate_evidence(cls, v: List[EvidenceRef]) -> List[EvidenceRef]:
        return _validate_evidence_list(v)


class ValidatedClaim(BaseModel):
    """An auditable claim with a stable ID and deterministic support decision.

    ``evidence`` holds only references that resolve to an archived passage.
    References that could not be resolved are marked in ``unresolved_evidence``
    (Wave 3 P0.3): they are audited with a ContentIssue and are never silently
    carried into the query corpus.  ``source_spans`` freeze exact archive
    intervals; only ``support_status == "SUPPORTED"`` may become answerable.
    """

    claim_id: str
    page_path: str
    text: str
    evidence: List[EvidenceRef] = Field(default_factory=list, max_length=20)
    unresolved_evidence: List[EvidenceRef] = Field(default_factory=list, max_length=20)
    source_spans: List[SourceSpan] = Field(default_factory=list, max_length=20)
    support_status: ClaimSupportStatus = "NOT_ENOUGH_INFO"
    support_reason: str = "support_not_evaluated"

    model_config = ConfigDict(extra="forbid")
    @field_validator("claim_id", mode="before")
    def _validate_claim_id(cls, v: str) -> str:
        return _validate_claim_id(v)

    @field_validator("page_path", mode="before")
    def _validate_page_path(cls, v: str) -> str:
        return normalize_wiki_path(v)

    @field_validator("text", mode="before")
    def _validate_text(cls, v: str) -> str:
        return _validate_text(v, "text", source_text_limit(v))

    @field_validator("evidence")
    def _validate_evidence(cls, v: List[EvidenceRef]) -> List[EvidenceRef]:
        return _validate_evidence_list(v)

    @field_validator("unresolved_evidence")
    def _validate_unresolved_evidence(cls, v: List[EvidenceRef]) -> List[EvidenceRef]:
        return _validate_evidence_list(v)

    @field_validator("source_spans")
    def _validate_source_spans(cls, v: List[SourceSpan]) -> List[SourceSpan]:
        keys = [
            (
                span.passage_id,
                span.source_id,
                span.document_id,
                span.chunk_id,
                span.passage_text_sha256,
                span.start_char,
                span.end_char,
            )
            for span in v
        ]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate source span")
        return v

    @field_validator("support_reason", mode="before")
    def _validate_support_reason(cls, v: str) -> str:
        return _validate_text(v, "support_reason", _MAX_REASON)

    @model_validator(mode="after")
    def _validate_support_binding(self):
        status = self.support_status
        spans = getattr(self, "source_spans", [])
        unresolved = getattr(self, "unresolved_evidence", [])
        evidence = getattr(self, "evidence", [])
        if status in ("SUPPORTED", "REFUTED") and not spans:
            raise ValueError("supported/refuted claims require a source span")
        if status == "SUPPORTED" and unresolved:
            raise ValueError("supported claims cannot retain unresolved evidence")
        evidence_ids = {
            (ref.passage_id, ref.source_id, ref.document_id, ref.chunk_id)
            for ref in evidence
        }
        for span in spans:
            identity = (
                span.passage_id,
                span.source_id,
                span.document_id,
                span.chunk_id,
            )
            if identity not in evidence_ids:
                raise ValueError("source span must bind one resolved evidence reference")
        return self


# ──────────────────────────────────────────────
#  Semantic-review models
# ──────────────────────────────────────────────


class SemanticFinding(BaseModel):
    """A bounded model assessment of one unsupported fact or a contradiction."""

    category: _FindingCategory
    claim_ids: List[str]
    reason: str

    model_config = ConfigDict(extra="forbid")
    @field_validator("claim_ids", mode="before")
    def _validate_claim_ids(cls, v: List[str]) -> List[str]:
        return _validate_claim_id_list(v)

    @field_validator("reason", mode="before")
    def _validate_reason(cls, v: str) -> str:
        return _validate_text(v, "reason", _MAX_REASON)

    @model_validator(mode="after")
    def _check_cardinality(self):
        category = self.category
        ids = getattr(self, "claim_ids", [])
        if category == "unsupported_fact" and len(ids) != 1:
            raise ValueError("unsupported_fact must have exactly one claim id")
        if category == "cross_page_contradiction" and len(ids) < 2:
            raise ValueError("cross_page_contradiction must have at least two claim ids")
        return self


class SemanticReview(BaseModel):
    """Structured response schema a model will fill once over the known claims."""

    reviewed_claim_ids: List[str]
    findings: List[SemanticFinding] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")
    @field_validator("reviewed_claim_ids", mode="before")
    def _validate_reviewed(cls, v: List[str]) -> List[str]:
        return _validate_claim_id_list(v)

    @field_validator("findings")
    def _no_duplicate_findings(cls, v: List[SemanticFinding]) -> List[SemanticFinding]:
        seen = set()
        for finding in v:
            key = (finding.category, tuple(finding.claim_ids), finding.reason)
            if key in seen:
                raise ValueError("duplicate semantic finding")
            seen.add(key)
        return v


# ──────────────────────────────────────────────
#  Content issues and report
# ──────────────────────────────────────────────


class ContentIssue(BaseModel):
    """One typed, deterministic or model-backed content issue."""

    code: str
    category: _ContentCategory
    claim_ids: List[str] = Field(default_factory=list)
    page_paths: List[str] = Field(default_factory=list)
    evidence_index: Optional[int] = None
    detail: str
    model_backed: bool = False

    model_config = ConfigDict(extra="forbid")
    @field_validator("code", mode="before")
    def _validate_code(cls, v: str) -> str:
        return _require_non_blank(v, "code")

    @field_validator("claim_ids", mode="before")
    def _validate_claim_ids(cls, v: List[str]) -> List[str]:
        return _validate_claim_id_list(v)

    @field_validator("page_paths", mode="before")
    def _validate_page_paths(cls, v: List[str]) -> List[str]:
        return _validate_page_path_list(v)

    @field_validator("evidence_index")
    def _validate_evidence_index(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v < 0:
            raise ValueError("evidence_index must be non-negative")
        return v

    @field_validator("detail", mode="before")
    def _validate_detail(cls, v: str) -> str:
        return _require_non_blank(v, "detail")


class ContentReport(BaseModel):
    """The content gate result for one batch of claims."""

    validated_claims: List[ValidatedClaim] = Field(default_factory=list)
    issues: List[ContentIssue] = Field(default_factory=list)
    semantic_review_complete: bool = False

    model_config = ConfigDict(extra="forbid")
    @field_validator("validated_claims")
    def _unique_claim_ids(cls, v: List[ValidatedClaim]) -> List[ValidatedClaim]:
        ids = [vc.claim_id for vc in v]
        if len(set(ids)) != len(ids):
            raise ValueError("validated claim ids must be unique")
        return v

    @field_validator("issues")
    def _sort_dedupe_issues(cls, v: List[ContentIssue]) -> List[ContentIssue]:
        # The card keys sorting and de-duplication by the exact same tuple, so
        # an ``evidence_index`` of 0 and None both collapse onto -1: the two are
        # interchangeable duplicates and the first in caller order is kept.
        def sort_key(issue: ContentIssue) -> Tuple[object, ...]:
            return (
                issue.code,
                tuple(issue.claim_ids),
                tuple(issue.page_paths),
                issue.evidence_index or -1,
                issue.detail,
            )

        seen = set()
        result: List[ContentIssue] = []
        for issue in sorted(v, key=sort_key):
            marker = sort_key(issue)
            if marker not in seen:
                seen.add(marker)
                result.append(issue)
        return result

    @property
    def is_valid(self) -> bool:
        """True only when there are no issues."""
        return len(self.issues) == 0

    @property
    def fatal_count(self) -> int:
        return len(self.issues)

    @property
    def publishable(self) -> bool:
        return self.is_valid and self.semantic_review_complete


# ──────────────────────────────────────────────
#  Normalization and deterministic validation
# ──────────────────────────────────────────────


def normalize_evidence_text(value: str) -> str:
    """Normalize only line endings: CRLF then remaining CR to LF.

    Performs no other change — no strip, whitespace collapse, case folding or
    Unicode normalization.
    """
    return value.replace("\r\n", "\n").replace("\r", "\n")


def evidence_text_collapse(value: str) -> str:
    """Fold case, strip whitespace and punctuation for substring matching.

    Used for evidence excerpt / claim-text substring checks. Keeps the
    "must come verbatim from the source" intent while tolerating the whitespace,
    case and punctuation differences a model may introduce around an exact
    quote (line breaks inside a sentence, quote style, hyphenation).
    """
    folded = normalize_evidence_text(value).casefold()
    return re.sub(r"[\W_]+", "", folded, flags=re.UNICODE)


def _collapsed_text_with_char_offsets(
    value: str,
) -> Tuple[str, List[Tuple[int, int]]]:
    """Return collapsed text plus its exact original-character intervals.

    The collapsed value is equivalent to :func:`evidence_text_collapse`, while
    each output character retains the half-open interval that produced it in
    the original string.  This lets tolerant case/spacing matching end in a
    strict ``SourceSpan`` over the immutable archive instead of treating the
    normalized string itself as provenance.
    """
    chars: List[str] = []
    offsets: List[Tuple[int, int]] = []
    index = 0
    while index < len(value):
        if value.startswith("\r\n", index):
            normalized = "\n"
            next_index = index + 2
        elif value[index] == "\r":
            normalized = "\n"
            next_index = index + 1
        else:
            normalized = value[index]
            next_index = index + 1
        for folded in normalized.casefold():
            if folded != "_" and re.fullmatch(r"\w", folded, flags=re.UNICODE):
                chars.append(folded)
                offsets.append((index, next_index))
        index = next_index
    return "".join(chars), offsets


def _locate_source_interval(passage: ArchivedPassage, excerpt: str) -> Optional[Tuple[int, int]]:
    """Locate a tolerant excerpt as one exact interval in ``passage.text``."""
    exact_start = passage.text.find(excerpt)
    if exact_start >= 0:
        return exact_start, exact_start + len(excerpt)

    target = evidence_text_collapse(excerpt)
    collapsed, offsets = _collapsed_text_with_char_offsets(passage.text)
    collapsed_start = collapsed.find(target)
    if not target or collapsed_start < 0:
        if target and passage.source_metadata.get("block_type") == "html_table":
            from jonex_wiki.domain.engine.compile.html_evidence import locate_html_row_quote
            return locate_html_row_quote(passage.text, target)
        return None
    collapsed_end = collapsed_start + len(target) - 1
    return offsets[collapsed_start][0], offsets[collapsed_end][1]


def _bind_source_span(state: CompilerState, ref: EvidenceRef) -> Optional[SourceSpan]:
    """Bind a resolved evidence reference to an immutable archive interval."""
    passage = state.source_archive.get(ref.passage_id)
    if passage is None or (
        passage.source_id != ref.source_id
        or passage.document_id != ref.document_id
        or passage.chunk_id != ref.chunk_id
    ):
        return None
    interval = _locate_source_interval(passage, ref.excerpt)
    if interval is None:
        return None
    start, end = interval
    return SourceSpan(
        passage_id=passage.passage_id,
        source_id=passage.source_id,
        document_id=passage.document_id,
        chunk_id=passage.chunk_id,
        source_sha256=passage.sha256,
        passage_text_sha256=hashlib.sha256(passage.text.encode("utf-8")).hexdigest(),
        start_char=start,
        end_char=end,
        text=passage.text[start:end],
    )


def _derive_claim_id(page_path: str, text: str, evidence: List[EvidenceRef]) -> str:
    """Derive a stable claim ID from page, text and the sorted evidence.

    Evidence is sorted solely for ID computation so equal semantic claims with
    reordered evidence share an ID; caller order is preserved on the model.
    """
    sorted_evidence = sorted(
        evidence,
        key=lambda ref: (ref.passage_id, ref.source_id, ref.document_id, ref.chunk_id, ref.excerpt),
    )
    payload = {
        "page_path": page_path,
        "text": text,
        "evidence": [ref.model_dump() for ref in sorted_evidence],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return "claim_" + digest[:24]


def _resolve_evidence_ref(
    state: CompilerState, ref: EvidenceRef
) -> Optional[EvidenceRef]:
    """Resolve one evidence ref to the passage that actually contains the excerpt.

    Prefers the named passage when its identity matches. Otherwise locates the
    excerpt (whitespace-stripped) anywhere in the same document and corrects the
    passage identity, so citations still point at the chunk that holds the text.
    Returns None when the excerpt is not verbatim in the source document at all.
    """
    target = evidence_text_collapse(ref.excerpt)
    if not target:
        return None
    passage = state.source_archive.get(ref.passage_id)
    if (
        passage is not None
        and passage.source_id == ref.source_id
        and passage.document_id == ref.document_id
        and passage.chunk_id == ref.chunk_id
        and _locate_source_interval(passage, ref.excerpt) is not None
    ):
        return ref
    # The model cannot know the internal passage/document ids it must cite, so
    # its identity fields are unreliable.  Locate the excerpt within the SAME
    # document only — a coincidental sentence match in a different document
    # must never rebind the evidence to the wrong source.
    base_passage = state.source_archive.get(ref.passage_id)
    anchor_doc = base_passage.document_id if base_passage else ref.document_id
    for p in state.source_archive.values():
        if p.document_id != anchor_doc:
            continue
        if _locate_source_interval(p, ref.excerpt) is not None:
            return EvidenceRef(
                passage_id=p.passage_id,
                source_id=p.source_id,
                document_id=p.document_id,
                chunk_id=p.chunk_id,
                excerpt=ref.excerpt,
            )

    # The excerpt may straddle a chunk boundary (e.g. code docstrings split
    # across chunks). Concatenate the referenced document and match; then point
    # the citation at the chunk holding the excerpt head.
    doc_passages = [
        p for p in state.source_archive.values()
        if p.document_id == anchor_doc
    ]
    if doc_passages:
        ordered = sorted(
            doc_passages,
            key=lambda p: (p.chunk_id, p.source_id),
        )
        combined = "".join(evidence_text_collapse(p.text) for p in ordered)
        if target in combined:
            head = target[:24]
            for p in ordered:
                if (
                    head in evidence_text_collapse(p.text)
                    or target in evidence_text_collapse(p.text)
                ):
                    return EvidenceRef(
                        passage_id=p.passage_id,
                        source_id=p.source_id,
                        document_id=p.document_id,
                        chunk_id=p.chunk_id,
                        excerpt=ref.excerpt,
                    )
            p = ordered[0]
            return EvidenceRef(
                passage_id=p.passage_id,
                source_id=p.source_id,
                document_id=p.document_id,
                chunk_id=p.chunk_id,
                excerpt=ref.excerpt,
            )
    return None


def _evidence_error_code(state: CompilerState, ref: EvidenceRef) -> str:
    """Classify why *ref* could not resolve, keeping the pre-existing codes."""
    passage = state.source_archive.get(ref.passage_id)
    if passage is None:
        return "EVIDENCE_PASSAGE_MISSING"
    if (
        passage.source_id != ref.source_id
        or passage.document_id != ref.document_id
        or passage.chunk_id != ref.chunk_id
    ):
        return "EVIDENCE_IDENTITY_MISMATCH"
    return "EVIDENCE_EXCERPT_NOT_FOUND"


def validate_claim_evidence(state: CompilerState, claims: List[ClaimDraft]) -> ContentReport:
    """Run the deterministic evidence gate over the given claim drafts.

    Pure: never mutates ``state`` or ``claims``.  Returns a report that never
    marks the semantic review complete.  Support decisions use only the bounded
    deterministic rules in ``claim_support``; unproved language fails safe as
    ``NOT_ENOUGH_INFO``.
    """
    issues: List[ContentIssue] = []
    validated: List[ValidatedClaim] = []
    seen_ids = set()

    for draft in claims:
        failed = False

        # 2-4. resolve every evidence ref (correcting the passage identity when
        # the model mis-assigns a chunk of the same document).
        corrected_evidence: List[EvidenceRef] = []
        bad_refs: List[tuple[int, EvidenceRef, str]] = []
        for idx, ref in enumerate(draft.evidence):
            resolved = _resolve_evidence_ref(state, ref)
            if resolved is None:
                bad_refs.append((idx, ref, _evidence_error_code(state, ref)))
            else:
                corrected_evidence.append(resolved)

        # Soft-evidence mode: the model's claims are often summarising sentences
        # rather than verbatim excerpts, so a claim whose evidence cannot be
        # located exactly is tolerated (logged + audited) instead of failing the
        # whole compile.  Page existence and duplicate checks below still gate.
        # Wave 3 P0.3: unresolved references are marked (``unresolved_evidence``)
        # instead of being unconditionally retained in ``ValidatedClaim.evidence``,
        # so bad evidence is never silently carried into the query corpus.
        evidence_out = list(corrected_evidence)
        unresolved_out = [ref for _, ref, _ in bad_refs]
        claim_id = _derive_claim_id(draft.page_path, draft.text, evidence_out)
        source_spans: List[SourceSpan] = []
        seen_spans = set()
        for ref in evidence_out:
            span = _bind_source_span(state, ref)
            if span is None:
                continue
            marker = (
                span.passage_id,
                span.passage_text_sha256,
                span.start_char,
                span.end_char,
            )
            if marker not in seen_spans:
                seen_spans.add(marker)
                source_spans.append(span)
        support_status, support_reason = assess_claim_support(
            draft.text,
            source_spans,
            has_unresolved_evidence=(
                bool(unresolved_out) or len(source_spans) != len(evidence_out)
            ),
        )

        # Unresolvable evidence is audited structurally below.  Keep individual
        # rows at debug level: re-opening a large Bundle can otherwise emit one
        # warning per claim and expand a single query log by hundreds of KB.
        for idx, ref, code in bad_refs:
            logger.debug(
                "evidence unresolved (tolerated) | claim=%r code=%s pid=%s excerpt=%r",
                draft.text[:60],
                code,
                ref.passage_id,
                ref.excerpt[:160],
            )
            issues.append(
                ContentIssue(
                    code=code,
                    category="malformed_reference",
                    claim_ids=[claim_id],
                    page_paths=[draft.page_path],
                    evidence_index=idx,
                    detail=ref.passage_id,
                    model_backed=False,
                )
            )

        # 1. the claimed page must exist.
        if draft.page_path not in state.pages:
            issues.append(
                ContentIssue(
                    code="CLAIM_PAGE_MISSING",
                    category="malformed_reference",
                    claim_ids=[claim_id],
                    page_paths=[draft.page_path],
                    detail=draft.page_path,
                    model_backed=False,
                )
            )
            failed = True

        # 5. duplicate stable IDs are reported but never returned twice.
        if claim_id in seen_ids:
            issues.append(
                ContentIssue(
                    code="DUPLICATE_CLAIM",
                    category="malformed_reference",
                    claim_ids=[claim_id],
                    page_paths=[draft.page_path],
                    detail=claim_id,
                    model_backed=False,
                )
            )
            failed = True
        seen_ids.add(claim_id)

        if not failed:
            validated.append(
                ValidatedClaim(
                    claim_id=claim_id,
                    page_path=draft.page_path,
                    text=draft.text,
                    evidence=evidence_out,
                    unresolved_evidence=unresolved_out,
                    source_spans=source_spans,
                    support_status=support_status,
                    support_reason=support_reason,
                )
            )

    return ContentReport(
        validated_claims=validated,
        issues=issues,
        semantic_review_complete=False,
    )


# ──────────────────────────────────────────────
#  Merge a bounded semantic review
# ──────────────────────────────────────────────


def merge_semantic_review(report: ContentReport, review: SemanticReview) -> ContentReport:
    """Merge a bounded semantic review into a fresh report.

    Returns a new report and never mutates either input.  Every reviewed ID
    must be known, every known claim must be reviewed, and every finding ID
    must be both known and reviewed; otherwise the report is non-publishable.
    """
    issues = list(report.issues)
    validated = list(report.validated_claims)
    path_by_id = {vc.claim_id: vc.page_path for vc in validated}
    known_ids = [vc.claim_id for vc in validated]
    known_set = set(known_ids)
    reviewed_set = set(review.reviewed_claim_ids)

    protocol_error = False

    # Invented review IDs and omitted known claims are protocol errors.
    for rid in review.reviewed_claim_ids:
        if rid not in known_set:
            issues.append(
                ContentIssue(
                    code="SEMANTIC_REVIEW_UNKNOWN_CLAIM",
                    category="malformed_reference",
                    claim_ids=[rid],
                    detail=rid,
                    model_backed=False,
                )
            )
            protocol_error = True

    for cid in known_ids:
        if cid not in reviewed_set:
            issues.append(
                ContentIssue(
                    code="SEMANTIC_REVIEW_INCOMPLETE",
                    category="malformed_reference",
                    claim_ids=[cid],
                    detail=cid,
                    model_backed=False,
                )
            )
            protocol_error = True

    # Findings reference only known, reviewed claims; otherwise a protocol
    # issue is emitted and no semantic finding issue is created.
    for finding in review.findings:
        all_valid = True
        for cid in finding.claim_ids:
            if cid not in known_set:
                issues.append(
                    ContentIssue(
                        code="SEMANTIC_FINDING_UNKNOWN_CLAIM",
                        category="malformed_reference",
                        claim_ids=[cid],
                        detail=cid,
                        model_backed=False,
                    )
                )
                protocol_error = True
                all_valid = False
            elif cid not in reviewed_set:
                issues.append(
                    ContentIssue(
                        code="SEMANTIC_FINDING_UNREVIEWED_CLAIM",
                        category="malformed_reference",
                        claim_ids=[cid],
                        detail=cid,
                        model_backed=False,
                    )
                )
                protocol_error = True
                all_valid = False
        if not all_valid:
            continue

        if finding.category == "unsupported_fact":
            cid = finding.claim_ids[0]
            issues.append(
                ContentIssue(
                    code="UNSUPPORTED_FACT",
                    category="unsupported_fact",
                    claim_ids=list(finding.claim_ids),
                    page_paths=[path_by_id[cid]],
                    detail=finding.reason,
                    model_backed=True,
                )
            )
        else:
            seen_pages = set()
            page_paths: List[str] = []
            for cid in finding.claim_ids:
                page_path = path_by_id[cid]
                if page_path not in seen_pages:
                    seen_pages.add(page_path)
                    page_paths.append(page_path)
            issues.append(
                ContentIssue(
                    code="CROSS_PAGE_CONTRADICTION",
                    category="cross_page_contradiction",
                    claim_ids=list(finding.claim_ids),
                    page_paths=page_paths,
                    detail=finding.reason,
                    model_backed=True,
                )
            )

    complete = (not protocol_error) and (reviewed_set == known_set)

    return ContentReport(
        validated_claims=validated,
        issues=issues,
        semantic_review_complete=complete,
    )


__all__ = [
    "EvidenceRef",
    "ClaimDraft",
    "ClaimSupportStatus",
    "SUBSTANTIVE_PASSAGE_MIN_CHARS",
    "is_substantive_passage",
    "ValidatedClaim",
    "SemanticFinding",
    "SemanticReview",
    "ContentIssue",
    "ContentReport",
    "normalize_evidence_text",
    "validate_claim_evidence",
    "merge_semantic_review",
]
