"""Typed, deterministic compiler state and page-selection contracts.

This module defines the data and validation boundary consumed by the
``SELECTPAGES(x, I)`` and ``COMPILEWIKIPAGES(x, S, C)`` algorithm tasks.  It
only declares types and pure helpers: it never calls a model, mutates the
engine, renders Wiki files, or implements algorithm orchestration.

Paper-required facts fixed here:

- page selection is a separate operation for one passage;
- selected existing pages are drawn from index candidates;
- at most five existing pages may be selected;
- selected page snapshots, source archive state and active constraints are
  explicit inputs to later compilation.

All models are Pydantic v1 compatible and reject unknown fields.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, PrivateAttr, model_validator, field_validator, ConfigDict

# ──────────────────────────────────────────────
#  Constants and canonical path validation
# ──────────────────────────────────────────────

MAX_SELECTED_EXISTING_PAGES = 5
MAX_NEW_PAGE_PROPOSALS = 5

NON_CANONICAL_WIKI_PATH = "NON_CANONICAL_WIKI_PATH"

# Every directory and filename stem must match lowercase kebab-case.
_SEGMENT_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")

_MAX_PATH_LENGTH = 240
_MAX_SEGMENT_LENGTH = 80  # includes the `.md` suffix


def normalize_wiki_path(value: str) -> str:
    """宽容规范化 LLM 生成的路径为 canonical 形式。

    真实 LLM 常输出大写/带空格/无 .md/无 category 的路径（如 "Atlas API"）。
    这里小写化、把连续的非 ASCII 字母数字分隔符折叠为连字符、补齐 .md
    与 category，再经 canonical 校验。路径遍历、绝对路径和 URL 片段仍
    直接拒绝，不能被宽容规范化吞掉。
    """
    if not isinstance(value, str):
        raise ValueError(NON_CANONICAL_WIKI_PATH)
    v = value.strip().lower()
    if (
        not v
        or "\\" in v
        or "?" in v
        or "#" in v
        or v.startswith("/")
        or v.endswith("/")
    ):
        raise ValueError(NON_CANONICAL_WIKI_PATH)

    raw_parts = v.split("/")
    if any(part in ("", ".", "..") for part in raw_parts):
        raise ValueError(NON_CANONICAL_WIKI_PATH)

    def _slug(part: str) -> str:
        # The final ``.md`` suffix is structural; all other punctuation (for
        # example ``u.s.``) is a word separator, not a legal slug character.
        part = re.sub(r"[^a-z0-9]+", "-", part)
        return re.sub(r"-+", "-", part).strip("-")

    suffix = ".md"
    if raw_parts[-1].endswith(suffix):
        raw_parts[-1] = raw_parts[-1][: -len(suffix)]
    else:
        # A provider may omit the extension; append it after slugging rather
        # than allowing a dot in the filename stem.
        raw_parts[-1] = raw_parts[-1]
    parts = [_slug(part) for part in raw_parts]
    if any(not part for part in parts):
        raise ValueError(NON_CANONICAL_WIKI_PATH)
    if len(parts) == 1:
        parts.insert(0, "pages")
    v = "/".join(parts) + suffix
    return canonical_wiki_path(v)


def canonical_wiki_path(value: str) -> str:
    """Return ``value`` unchanged if it is a canonical Wiki path.

    Raises ``ValueError(NON_CANONICAL_WIKI_PATH)`` otherwise.  This validator
    rejects rather than silently normalizing its input.
    """
    if not isinstance(value, str):
        raise ValueError(NON_CANONICAL_WIKI_PATH)
    if not (1 <= len(value) <= _MAX_PATH_LENGTH):
        raise ValueError(NON_CANONICAL_WIKI_PATH)
    if "\\" in value:
        raise ValueError(NON_CANONICAL_WIKI_PATH)
    if value.startswith("/") or value.endswith("/"):
        raise ValueError(NON_CANONICAL_WIKI_PATH)
    if "?" in value or "#" in value:
        raise ValueError(NON_CANONICAL_WIKI_PATH)
    if any(c.isspace() for c in value):
        raise ValueError(NON_CANONICAL_WIKI_PATH)
    if any(c.isupper() for c in value):
        raise ValueError(NON_CANONICAL_WIKI_PATH)
    if not value.endswith(".md"):
        raise ValueError(NON_CANONICAL_WIKI_PATH)

    parts = value.split("/")
    # Must have at least a category and a file component (root `index.md` is
    # thereby rejected as a single-component path).
    if len(parts) < 2:
        raise ValueError(NON_CANONICAL_WIKI_PATH)
    if any(p == "" or p in (".", "..") for p in parts):
        raise ValueError(NON_CANONICAL_WIKI_PATH)

    filename = parts[-1]
    # Any `_index` file is reserved; a category-level `index.md` is allowed.
    if filename[: -len(".md")] == "_index":
        raise ValueError(NON_CANONICAL_WIKI_PATH)

    # Directory segments must directly match the slug; they are never `.md`
    # files, so no suffix is stripped here.
    for directory in parts[:-1]:
        if len(directory) > _MAX_SEGMENT_LENGTH:
            raise ValueError(NON_CANONICAL_WIKI_PATH)
        if not _SEGMENT_RE.fullmatch(directory):
            raise ValueError(NON_CANONICAL_WIKI_PATH)

    # The final file segment is exactly `{slug}.md` within the length bound.
    if len(filename) > _MAX_SEGMENT_LENGTH:
        raise ValueError(NON_CANONICAL_WIKI_PATH)
    if not _SEGMENT_RE.fullmatch(filename[: -len(".md")]):
        raise ValueError(NON_CANONICAL_WIKI_PATH)

    return value


def _dedupe_keep_order(items: List[str]) -> List[str]:
    """Return unique items preserving caller order."""
    seen = set()
    result = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


# ──────────────────────────────────────────────
#  Models
# ──────────────────────────────────────────────


class PageIndexEntry(BaseModel):
    """Metadata describing one Wiki page."""

    path: str = Field(..., description="Canonical Wiki path")
    title: str = Field(..., description="Non-blank page title")
    aliases: List[str] = Field(default_factory=list, description="Alternate names")
    tags: List[str] = Field(default_factory=list, description="Category tags")
    description: str = Field(default="", description="One-paragraph summary")
    object_id: Optional[str] = None
    scope: Dict[str, Any] = Field(default_factory=dict)
    fields: List[str] = Field(default_factory=list)
    why_visit: str = ""
    limitations: List[str] = Field(default_factory=list)
    capabilities: List[str] = Field(default_factory=list)
    interpretation_status: Optional[str] = None

    model_config = ConfigDict(extra="forbid")
    @field_validator("path")
    def _validate_path(cls, v: str) -> str:
        return normalize_wiki_path(v)

    @field_validator("title")
    def _validate_title(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("title must be non-blank")
        if len(v) > 200:
            raise ValueError("title exceeds 200 characters")
        return v

    @field_validator("description")
    def _validate_description(cls, v: str) -> str:
        # LLM 常生成长描述，截断而非拒绝。
        return (v or "")[:500]

    @field_validator("why_visit")
    def _validate_why_visit(cls, value: str) -> str:
        # Truncation must be idempotent: the published Bundle re-validates its
        # own stored metadata when it is read back, and the corpus checksum
        # covers this value.  A cut landing after a space would leave a trailing
        # space that the next normalisation removes, so an over-long
        # ``why_visit`` would make a Bundle fail to reopen as its own content.
        return " ".join((value or "").split())[:120].strip()

    @field_validator("fields", "limitations", "capabilities")
    def _validate_metadata_lists(cls, value: List[str]) -> List[str]:
        return _dedupe_keep_order([item for item in value if isinstance(item, str) and item.strip()])

    @field_validator("aliases")
    def _validate_aliases(cls, v: List[str]) -> List[str]:
        for item in v:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("aliases must be non-blank")
        if len(v) > 20:
            raise ValueError("aliases exceed 20 entries")
        return _dedupe_keep_order(v)

    @field_validator("tags")
    def _validate_tags(cls, v: List[str]) -> List[str]:
        for item in v:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("tags must be non-blank")
        if len(v) > 20:
            raise ValueError("tags exceed 20 entries")
        return _dedupe_keep_order(v)


# Historical constants kept for old callers; navigation no longer enforces
# these cardinality limits. Delivery is bounded by actual request capacity.
MAX_SOURCE_LINK_TARGETS = 4
LINK_PROPOSAL_LIMIT_CODE = "LINK_PROPOSAL_LIMIT_EXCEEDED"
MAX_PAGE_LINK_MENTIONS = 100

class LinkMention(BaseModel):
    target_mention: str
    why_visit: str = ""
    evidence_refs: List[Dict[str, Any]] = Field(default_factory=list)
    relation: str = ""
    model_config = ConfigDict(extra="ignore")


class PageLink(BaseModel):
    relation: str = ""
    target_id: str
    why_visit: str
    evidence_refs: List[Dict[str, Any]] = Field(default_factory=list)
    kind: Literal["navigation", "supported_relation"]

    model_config = ConfigDict(extra="forbid")
    @field_validator("target_id")
    def _target(cls, value: str) -> str:
        return canonical_wiki_path(value)

    @field_validator("why_visit")
    def _why(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            return "source mentions this target"
        return normalized


class WikiPageSnapshot(BaseModel):
    """Immutable snapshot of a page's index metadata and content."""

    index: PageIndexEntry
    content: str = Field(default="", description="Markdown page body")
    related_pages: List[str] = Field(default_factory=list)
    links: List[PageLink] = Field(default_factory=list)
    link_mentions: List[LinkMention] = Field(default_factory=list)
    source_ids: List[str] = Field(default_factory=list)
    version: int = Field(default=0)

    model_config = ConfigDict(extra="forbid")
    @field_validator("link_mentions", mode="before")
    @classmethod
    def _readable_links(cls, value):
        readable = []
        for item in value if isinstance(value, list) else []:
            try:
                mention = LinkMention.model_validate(item)
                if mention.target_mention.strip():
                    readable.append(mention)
            except (ValueError, TypeError):
                continue
        return readable

    @field_validator("related_pages")
    def _validate_related(cls, v: List[str]) -> List[str]:
        v = [normalize_wiki_path(item) for item in v]
        return _dedupe_keep_order(v)

    @field_validator("source_ids")
    def _validate_source_ids(cls, v: List[str]) -> List[str]:
        for item in v:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("source_ids must be non-blank")
        return _dedupe_keep_order(v)

    @field_validator("version")
    def _validate_version(cls, v: int) -> int:
        if v < 0:
            raise ValueError("version must be non-negative")
        return v


class NewPageProposal(BaseModel):
    """A proposed new page to be created during compilation."""

    path: str = Field(..., description="Canonical Wiki path")
    title: str = Field(..., description="Non-blank page title")
    description: str = Field(default="")
    tags: List[str] = Field(default_factory=list)
    reason: str = Field(..., description="Why this page is proposed")

    model_config = ConfigDict(extra="forbid")
    @field_validator("path")
    def _validate_path(cls, v: str) -> str:
        return normalize_wiki_path(v)

    @field_validator("title")
    def _validate_title(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("title must be non-blank")
        if len(v) > 200:
            raise ValueError("title exceeds 200 characters")
        return v

    @field_validator("reason")
    def _validate_reason(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("reason must be non-blank")
        if len(v) > 2000:
            raise ValueError("reason exceeds 2000 characters")
        return v

    @field_validator("description")
    def _validate_description(cls, v: str) -> str:
        # LLM 常生成长描述，截断而非拒绝。
        return (v or "")[:500]

    @field_validator("tags")
    def _validate_tags(cls, v: List[str]) -> List[str]:
        for item in v:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("tags must be non-blank")
        if len(v) > 20:
            raise ValueError("tags exceed 20 entries")
        return _dedupe_keep_order(v)


class PageSelection(BaseModel):
    """The set of pages selected for compiling one passage."""

    existing_paths: List[str] = Field(
        default_factory=list,
        max_length=MAX_SELECTED_EXISTING_PAGES,
        description="Selected existing page paths",
    )
    new_pages: List[NewPageProposal] = Field(
        default_factory=list,
        max_length=MAX_NEW_PAGE_PROPOSALS,
        description="Proposed new pages",
    )

    model_config = ConfigDict(extra="ignore")
    @field_validator("existing_paths")
    def _validate_existing_paths(cls, v: List[str]) -> List[str]:
        v = [normalize_wiki_path(item) for item in v]
        if len(v) > MAX_SELECTED_EXISTING_PAGES:
            raise ValueError("existing_paths exceed the selection limit")
        if len(set(v)) != len(v):
            raise ValueError("existing_paths must be unique")
        return v

    @model_validator(mode="after")
    def _check_selection(self):
        existing = getattr(self, "existing_paths", [])
        new_pages = getattr(self, "new_pages", [])
        new_paths = [p.path for p in new_pages]
        if len(set(new_paths)) != len(new_paths):
            raise ValueError("new_pages paths must be unique")
        overlap = set(existing) & set(new_paths)
        if overlap:
            raise ValueError("existing and new paths may not overlap")
        return self


class ArchivedPassage(BaseModel):
    """A source passage archived for later compilation.

    ``text`` is the canonical, evidence-addressable source interval.  A
    parser may additionally provide ``context_text`` containing a bounded
    heading/table/code anchor for the model prompt; it must never be used as
    evidence because it can contain text from a neighbouring interval.
    """

    passage_id: str
    source_id: str
    document_id: str
    chunk_id: str
    text: str
    context_text: Optional[str] = Field(
        default=None,
        description="Optional bounded structural context used only for retrieval/prompting",
    )
    sha256: str
    position: Optional[int] = None
    source_metadata: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")
    @field_validator("passage_id", "source_id", "document_id", "chunk_id")
    def _validate_id(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("id must be non-blank")
        return v

    @field_validator("sha256")
    def _validate_sha256(cls, v: str) -> str:
        if not isinstance(v, str) or not _SHA256_RE.fullmatch(v):
            raise ValueError("sha256 must be 64 lowercase hex characters")
        return v

    @field_validator("position")
    def _validate_position(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v < 0:
            raise ValueError("position must be non-negative")
        return v


class SourceSpan(BaseModel):
    """One immutable, exact character interval in an archived passage.

    ``start_char`` is inclusive and ``end_char`` is exclusive, using Python
    Unicode character indexes into ``ArchivedPassage.text``.  The source-level
    digest and the passage-text digest make the interval auditable even when a
    parser reuses the same document/chunk identifiers for changed content.
    Binding this typed value to a ``CompilerState`` remains the validator's
    responsibility; the model itself only freezes and validates its shape.
    """

    passage_id: str
    source_id: str
    document_id: str
    chunk_id: str
    source_sha256: str
    passage_text_sha256: str
    start_char: int
    end_char: int
    text: str

    model_config = ConfigDict(extra="forbid", frozen=True)
    @field_validator("passage_id", "source_id", "document_id", "chunk_id", mode="before")
    def _validate_id(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("id must be non-blank")
        return v

    @field_validator("source_sha256", "passage_text_sha256", mode="before")
    def _validate_digest(cls, v: str) -> str:
        if not isinstance(v, str) or not _SHA256_RE.fullmatch(v):
            raise ValueError("digest must be 64 lowercase hex characters")
        return v

    @field_validator("start_char", "end_char", mode="before")
    def _validate_offset_type(cls, v: int) -> int:
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError("character offsets must be integers")
        return v

    @field_validator("text", mode="before")
    def _validate_text(cls, v: str) -> str:
        if not isinstance(v, str) or not v:
            raise ValueError("span text must be non-empty")
        return v

    @model_validator(mode="after")
    def _validate_interval(self):
        start = self.start_char
        end = self.end_char
        text = self.text
        if isinstance(start, int) and isinstance(end, int):
            if start < 0 or end <= start:
                raise ValueError("source span must be a non-empty forward interval")
            if isinstance(text, str) and len(text) != end - start:
                raise ValueError("span text length must equal its character interval")
        return self


class CompilerState(BaseModel):
    """Deterministic compiler state carried across page-selection steps."""

    batch_number: int = Field(default=0)
    pages: Dict[str, WikiPageSnapshot] = Field(default_factory=dict)
    index: Dict[str, PageIndexEntry] = Field(default_factory=dict)
    source_archive: Dict[str, ArchivedPassage] = Field(default_factory=dict)
    processed_passage_ids: List[str] = Field(default_factory=list)
    active_constraints: List[str] = Field(default_factory=list)
    passage_failures: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    claim_compilation_dispositions: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    recovered_retries: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    # Stable navigation shards. Once a complete type directory crosses the
    # 200-entry boundary, its prefix pages remain recorded even if later
    # deletions reduce the object count; this prevents directory churn.
    directory_shards: Dict[str, List[str]] = Field(default_factory=dict)
    # Runtime-only publication cache.  Declaring it as a Pydantic private
    # attribute keeps the final rendered bytes out of ``state.model_dump()`` and
    # therefore out of the durable previous-bundle schema.
    _rendered_wiki_files: Optional[Dict[str, bytes]] = PrivateAttr(default=None)
    _publication: Optional[Any] = PrivateAttr(default=None)
    _fact_mentions: Dict[str, List[str]] = PrivateAttr(default_factory=dict)
    _compile_delta: Optional[Any] = PrivateAttr(default=None)

    model_config = ConfigDict(extra="forbid")
    @field_validator("batch_number")
    def _validate_batch(cls, v: int) -> int:
        if v < 0:
            raise ValueError("batch_number must be non-negative")
        return v

    @field_validator("directory_shards")
    def _validate_directory_shards(cls, value: Dict[str, List[str]]) -> Dict[str, List[str]]:
        normalized: Dict[str, List[str]] = {}
        for directory, prefixes in value.items():
            if directory not in {"sources", "tables"}:
                raise ValueError("directory shards are restricted to complete type directories")
            unique = sorted(set(prefixes))
            if any(not re.fullmatch(r"[0-9a-f]{1,16}", prefix) for prefix in unique):
                raise ValueError("directory shard prefix must be lowercase hexadecimal")
            normalized[directory] = unique
        return normalized

    @model_validator(mode="after")
    def _check_state(self):
        pages = getattr(self, "pages", {})
        index = getattr(self, "index", {})
        archive = getattr(self, "source_archive", {})
        processed = getattr(self, "processed_passage_ids", [])
        constraints = getattr(self, "active_constraints", [])

        for path, snapshot in pages.items():
            if snapshot.index.path != path:
                raise ValueError("pages key must equal snapshot index path")

        for path, entry in index.items():
            if entry.path != path:
                raise ValueError("index key must equal entry path")
            if path not in pages:
                raise ValueError("index entry must exist in pages")
            if entry != pages[path].index:
                raise ValueError("index entry must match page snapshot metadata")

        for pid, passage in archive.items():
            if passage.passage_id != pid:
                raise ValueError("source_archive key must equal passage_id")

        if len(set(processed)) != len(processed):
            raise ValueError("processed_passage_ids must be unique")
        for pid in processed:
            if pid not in archive:
                raise ValueError("processed passage id must be archived")

        if len(set(constraints)) != len(constraints):
            raise ValueError("active_constraints must be unique")
        for c in constraints:
            if not isinstance(c, str) or not c.strip():
                raise ValueError("active_constraints must be non-blank")

        return self


class ResolvedPageSelection(BaseModel):
    """Fully resolved page selection: current snapshots and new proposals."""

    existing_pages: List[WikiPageSnapshot] = Field(
        default_factory=list,
        max_length=MAX_SELECTED_EXISTING_PAGES,
    )
    new_pages: List[NewPageProposal] = Field(
        default_factory=list,
        max_length=MAX_NEW_PAGE_PROPOSALS,
    )

    model_config = ConfigDict(extra="forbid")
# ──────────────────────────────────────────────
#  Resolution function (pure)
# ──────────────────────────────────────────────


def resolve_page_selection(
    state: CompilerState,
    candidates: List[PageIndexEntry],
    selection: PageSelection,
) -> ResolvedPageSelection:
    """Resolve a page selection against the current compiler state.

    Pure: never mutates ``state``, ``candidates`` or ``selection``.  A real LLM
    in a merged select+compile call frequently selects existing pages that are
    not candidates, or proposes new pages colliding with existing ones; such
    invalid items are filtered out (with a warning) instead of aborting the
    whole compile.
    """
    # 1. Deduplicate candidate paths (keep first occurrence).
    seen: set = set()
    valid_candidates: List[PageIndexEntry] = []
    for cand in candidates:
        if cand.path not in seen:
            seen.add(cand.path)
            valid_candidates.append(cand)
    candidate_paths = {cand.path for cand in valid_candidates}
    state_paths = set(state.pages) | set(state.index)

    # 2. Keep only existing paths that are candidates and currently in state.
    existing_paths = [
        path for path in selection.existing_paths
        if path in candidate_paths and path in state.pages
    ]
    # 3. Keep only new proposals that do not collide with candidates/state.
    new_pages = [
        p for p in selection.new_pages
        if p.path not in candidate_paths and p.path not in state_paths
    ]
    existing_pages = [state.pages[path] for path in existing_paths]
    return ResolvedPageSelection(existing_pages=existing_pages, new_pages=new_pages)


__all__ = [
    "MAX_SELECTED_EXISTING_PAGES",
    "MAX_NEW_PAGE_PROPOSALS",
    "canonical_wiki_path",
    "PageIndexEntry",
    "WikiPageSnapshot",
    "NewPageProposal",
    "PageSelection",
    "ArchivedPassage",
    "SourceSpan",
    "CompilerState",
    "ResolvedPageSelection",
    "resolve_page_selection",
]
