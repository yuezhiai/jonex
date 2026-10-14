"""Source understanding, separated from page organization (S7.1).

The legacy compile chain asks one model, once per passage, to *both* decide
which pages a passage belongs to and restate its facts.  That coupling has two
costs the plan targets: a page layout change re-asks the model about unchanged
source text, and a failed page organization throws away a perfectly good
reading of the source.

This module performs the first half on its own and persists it:

* the model sees **only the source passage** and its own context - never the
  wiki's page bodies, never other pages' prose;
* it returns page-free proposals: statements, verbatim excerpts, the names the
  material actually used, explicit aliases, periods/units/negation, mentions
  worth a page later and directed relations, plus what it could not resolve;
* the program assigns every identity - ``record_id``, passage/source/document
  ids, versions, evidence references - and never trusts a model-supplied id;
* the result becomes the *existing* ``SourceContributionRecord`` shape through
  the same evidence gate the legacy chain uses, so there is exactly one
  contribution ledger and no second source-of-truth;
* one checkpoint per source understanding is written to the artifact store
  atomically and keyed by a cache identity that covers the source, its parser
  and asset identity, the prompt/schema, the model and the understanding
  configuration - and deliberately *not* the page layout.

Page organization (S7.2, ``organization.organize_sources``) consumes these
checkpoints and rejoins the shared publication tail; a layered run never falls
back to the legacy chain and never publishes a half-compiled Bundle.  The
staged placeholder that used to fail loudly here was removed once the stage was
wired.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jonex_wiki.domain.engine.compile import content_validation, pipeline
from jonex_wiki.domain.engine.compile.identity import normalize_name
from jonex_wiki.domain.engine.compile.bundle import canonical_json_bytes, sha256_hex
from jonex_wiki.domain.engine.compile.content_validation import (
    ClaimDraft,
    EvidenceRef,
    validate_claim_evidence,
)
from jonex_wiki.domain.engine.compile.source_contribution import (
    SourceContributionRecord,
    _record_from_claim,
)
from jonex_wiki.domain.engine.models.claim import SUPPORT_RULE_VERSION
from jonex_wiki.domain.engine.models.compiler_state import (
    LINK_PROPOSAL_LIMIT_CODE,
    MAX_SOURCE_LINK_TARGETS,
    CompilerState,
)
from jonex_wiki.kernel.contracts import SourceArtifact
from jonex_wiki.kernel.ports import ArtifactStore, CancelPort, ModelPort
from jonex_wiki.kernel.telemetry import count_work, telemetry_stage

logger = logging.getLogger(__name__)

UNDERSTANDING_RULE_VERSION = "source-understanding-v5-source-excerpts-merged-hints"
UNDERSTANDING_PROMPT_VERSION = "understand-source-prompt-v4-source-excerpts"
UNDERSTANDING_SCHEMA_VERSION = "understand-source-schema-v2-tolerant"
UNDERSTANDING_NAMESPACE = "source-understanding"
#: Identity format of the understanding cache.  Bumped when the material below
#: changes shape, so checkpoints written under an older, *insufficient*
#: identity can never answer for the new one; they stay in the store, isolated
#: under their own keys, instead of being silently trusted.
UNDERSTANDING_IDENTITY_VERSION = "source-understanding-identity-v2"
#: The scene name sent to the gateway.  One constant, so the identity cannot
#: drift from what the model port actually receives.
UNDERSTANDING_SCENE = "jonex_wiki_source_understanding"

MAX_UNDERSTANDING_FACTS = 24
#: Names a fact may carry (its own subjects, not navigation targets).
MAX_UNDERSTANDING_MENTIONS = 8
#: R2.1: one source-understanding output -- one passage read once -- may name
#: Historical constants; model output is no longer restricted by target counts.
MAX_UNDERSTANDING_TARGETS = MAX_SOURCE_LINK_TARGETS
MAX_UNDERSTANDING_RELATIONS = MAX_SOURCE_LINK_TARGETS
MAX_UNDERSTANDING_UNBOUND = 8
MAX_UNDERSTANDING_EXCERPT = 2000
MAX_UNDERSTANDING_STATEMENT = 600
MAX_UNDERSTANDING_ALIASES = 6
#: Same bound as the legacy per-passage gate: one retry, then account and skip.
MAX_UNDERSTANDING_ATTEMPTS = 2

# Stable reason markers.  They are part of the checkpoint's audit trail and of
# the failure classification, so tests and operators can tell "the model
# hallucinated a quote" from "the store lost a checkpoint".
REASON_EXCERPT_NOT_IN_SOURCE = "UNDERSTANDING_EXCERPT_NOT_IN_SOURCE"
REASON_STALE_CHECKPOINT = "SOURCE_UNDERSTANDING_STALE_TASK"
REASON_FOREIGN_CHECKPOINT = "SOURCE_UNDERSTANDING_IDENTITY_MISMATCH"
REASON_CHECKPOINT_CORRUPT = "SOURCE_UNDERSTANDING_CHECKPOINT_CORRUPT"
REASON_STORE_CANNOT_CHECKPOINT = "SOURCE_UNDERSTANDING_STORE_UNSUPPORTED"

UNDERSTANDING_SYSTEM_INSTRUCTION = (
    "You are a rigorous source reader. Read the one source passage you are "
    "given and return exactly the requested structured JSON object. Never "
    "invent identifiers, versions or facts the passage does not state."
)

UNDERSTANDING_PROMPT_CHECKSUM = sha256_hex(
    canonical_json_bytes([UNDERSTANDING_SYSTEM_INSTRUCTION, UNDERSTANDING_PROMPT_VERSION,
                          UNDERSTANDING_SCHEMA_VERSION]))


# Design invariant, pinned by tests: page paths, page titles and target-page
# choices are deliberately absent from the cache identity, so reorganising
# pages never invalidates source understanding.


class SourceUnderstandingStoreUnsupported(RuntimeError):
    """The artifact store cannot persist source-understanding checkpoints.

    Failing is the honest outcome: continuing would mean layered compilation
    "works" only until the process restarts, which is exactly the property the
    plan refuses to fake.
    """

    code = REASON_STORE_CANNOT_CHECKPOINT


class UnderstandingRelation(BaseModel):
    """A directed relation the passage states, with no page identity."""

    kind: str
    subject: str
    target: str

    model_config = ConfigDict(extra="ignore")


class UnderstandingFact(BaseModel):
    """One page-free fact proposal from one source passage."""

    # Match the existing EvidenceRef limit while response errors can still retry.
    excerpt: str = Field(max_length=content_validation._MAX_EXCERPT)

    @field_validator("excerpt", mode="wrap")
    @classmethod
    def _literal_table_excerpt(cls, value, handler):
        if content_validation.source_text_limit(value) == content_validation._MAX_TABLE_EXCERPT:
            return content_validation._validate_text(value, "excerpt", content_validation._MAX_TABLE_EXCERPT)
        return handler(value)

    topic_hint: str = ""
    original_names: List[str] = Field(default_factory=list)
    aliases: List[str] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)
    mentions: List[str] = Field(default_factory=list)
    relations: List[UnderstandingRelation] = Field(
        default_factory=list)
    unbound: List[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore")

    @field_validator("relations", mode="before")
    def _readable_relations(cls, value):
        relations = []
        for item in value if isinstance(value, list) else ():
            try:
                relation = UnderstandingRelation.model_validate(item)
            except (ValueError, TypeError):
                continue
            if relation.subject.strip() and relation.target.strip():
                relations.append(relation)
        return relations

    @field_validator("original_names", "aliases", "notes", "mentions", "unbound", mode="before")
    def _text_lists(cls, value):
        return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


class SourceUnderstandingResponse(BaseModel):
    """What one source passage is asked to produce."""

    facts: List[UnderstandingFact] = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore")

    @field_validator("facts", mode="before")
    def _readable_facts(cls, value):
        facts = []
        for item in value if isinstance(value, list) else ():
            excerpt = item.get("excerpt") if isinstance(item, Mapping) else None
            if isinstance(excerpt, str) and len(excerpt) > content_validation.source_text_limit(excerpt):
                raise ValueError(f"excerpt exceeds {content_validation.source_text_limit(excerpt)} characters")
            try:
                fact = UnderstandingFact.model_validate(item)
            except (ValueError, TypeError):
                continue
            if fact.excerpt.strip():
                facts.append(fact)
        return facts


class SourceUnderstandingCheckpoint(BaseModel):
    """One persisted source understanding, with program-assigned identity."""

    rule: str = UNDERSTANDING_RULE_VERSION
    cache_key: str
    tenant_id: str
    kb_id: str
    passage_id: str
    source_id: str
    document_id: str
    chunk_id: str
    content_generation: int
    position: Optional[int] = None
    artifact_sha256: str
    text_sha256: str
    parser_id: str
    parser_version: str
    asset_digest: str = ""
    prompt_version: str = UNDERSTANDING_PROMPT_VERSION
    prompt_checksum: str = ""
    schema_version: str = UNDERSTANDING_SCHEMA_VERSION
    understanding_model: str = ""
    config_fingerprint: str = ""
    support_rule: str = SUPPORT_RULE_VERSION
    produced_at: str = ""
    records: List[SourceContributionRecord] = Field(default_factory=list)
    facts_seen: int = 0
    facts_accepted: int = 0
    rejected_reasons: List[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")

    def source_versions(self) -> Dict[str, str]:
        return {self.source_id: self.source_version()}

    def source_version(self) -> str:
        """The version a later organization run must still find unchanged."""
        return "{}:{}".format(self.content_generation, self.text_sha256)


class LayeredUnderstanding(BaseModel):
    """Everything one layered compile learned about its sources."""

    checkpoints: Dict[str, SourceUnderstandingCheckpoint] = Field(default_factory=dict)
    cache_hits: int = 0
    cache_misses: int = 0
    #: *Attempted* model calls: one per request actually sent, so a protocol
    #: failure followed by a successful retry is two.  This -- not the number
    #: of checkpoints -- is what a report must present as "model calls".
    model_calls: int = 0
    #: Checkpoints built from a model response this run (successful
    #: understandings).  ``model_calls - understandings`` is the call cost that
    #: produced nothing, and ``failures`` counts the passages left without one.
    understandings: int = 0
    #: Attempts that raised, and attempts beyond the first of a passage.
    attempt_failures: int = 0
    retries: int = 0
    corrupted: int = 0
    stale: int = 0
    #: Records built this run from a model response, and records *reused* from
    #: valid checkpoints.  A fully cached compile has no model calls and no new
    #: records, but it is still organizing ``reused_records`` facts -- reporting
    #: zero there would hide where the published pages came from.
    records: int = 0
    reused_records: int = 0
    facts_rejected: int = 0
    support_recomputed: int = 0
    #: Sum of the per-passage stage durations, queueing included.  It is a sum
    #: over passages, not a wall-clock interval: with concurrency > 1 it can
    #: exceed the compile's wall time because passages overlap.  The parts below
    #: are *inside* this sum, never added to it: ``model_ms`` (inside model
    #: calls), ``queue_ms`` (waiting for the concurrency semaphore before any
    #: work), ``store_ms`` (checkpoint load/write) and ``stage_lock_ms``
    #: (waiting for this stage's own bookkeeping lock).  The driver's commit
    #: lock is a different phase and is reported separately.
    understanding_ms: int = 0
    model_ms: int = 0
    queue_ms: int = 0
    store_ms: int = 0
    stage_lock_ms: int = 0
    #: Passage ids in archive order.  Organization consumes this, never a
    #: sorted() of the mapping keys: sorted ids are not input order.
    order: List[str] = Field(default_factory=list)
    #: Passage ids that failed both understanding attempts, with the same
    #: cause shape the legacy chain records.
    failures: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")


#: Per-process memo of local asset content digests, keyed by
#: ``(path, size, mtime_ns)``.  One compile reads the same table database for
#: many passages; hashing it once is the difference between O(1) and
#: O(passages) reads of a file that can be hundreds of megabytes.  A changed
#: size or mtime is a different key, so a re-parsed asset is re-hashed.
ASSET_DIGEST_MEMO: Dict[Tuple[str, int, int], str] = {}
ASSET_DIGEST_MEMO_MAX = 64
_ASSET_DIGEST_BLOCK = 1024 * 1024


def _file_digest(path: str) -> str:
    """Content digest of one local asset, memoized by (path, size, mtime_ns).

    Streams the file: an asset is never loaded into memory whole, and a file
    hashed once for this process is not hashed again per passage.  A missing or
    unreadable asset is a *value* rather than an exception, because a passage
    whose table database disappeared is a different identity, not a crash.
    """
    try:
        info = os.stat(path)
    except OSError:
        return "missing"
    memo_key = (path, int(info.st_size), int(info.st_mtime_ns))
    cached = ASSET_DIGEST_MEMO.get(memo_key)
    if cached is not None:
        return cached
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(_ASSET_DIGEST_BLOCK), b""):
                digest.update(block)
    except OSError:
        return "unreadable"
    value = digest.hexdigest()
    if len(ASSET_DIGEST_MEMO) >= ASSET_DIGEST_MEMO_MAX:
        ASSET_DIGEST_MEMO.clear()
    ASSET_DIGEST_MEMO[memo_key] = value
    count_work("compile_understanding_asset_hashed")
    return value


def local_asset_identity(key: str, artifact: SourceArtifact) -> Dict[str, Any]:
    """Identity of one attachment the passage depends on.

    A digest that already exists is reused rather than recomputed: the Bundle
    codec already enforces ``artifact.sha256 == sha256(original file)`` for the
    original document, and a parser that recorded ``<asset>_sha256`` next to
    ``<asset>_local_path`` is trusted for its own attachment.  Only when no
    digest exists is the file content hashed -- once per process and path.
    """
    metadata = artifact.source_metadata or {}
    declared = metadata.get(key[: -len("_local_path")] + "_sha256")
    if isinstance(declared, str) and len(declared) == 64:
        return {"path_key": key, "declared_sha256": declared}
    if key == "original_local_path" and artifact.sha256:
        return {"path_key": key, "declared_sha256": artifact.sha256}
    path = str(metadata.get(key) or "")
    if not path:
        return {"path_key": key, "content_sha256": "unset"}
    return {"path_key": key, "content_sha256": _file_digest(path)}


def _asset_digest(artifact: SourceArtifact) -> str:
    """Identity of the non-text assets this passage depends on.

    A table image, an embedded workbook or a page render is part of what the
    understanding read: the same text with a different asset set is not the
    same understanding, and losing the asset identity would let a stale
    checkpoint answer for a re-parsed document.  A ``*_local_path`` therefore
    contributes the *content* identity of the file behind it, not the path
    string, which is transport state and can change without the bytes changing.
    """
    metadata = artifact.source_metadata or {}
    material: List[Any] = []
    for key in sorted(metadata):
        if key in {"source_reading_materials", "contextual_text", "file_name"}:
            continue
        if key.endswith("_local_path"):
            material.append([key, local_asset_identity(key, artifact)])
            continue
        value = metadata[key]
        if isinstance(value, (dict, list, str, int, float, bool)) or value is None:
            material.append([key, value])
    return sha256_hex(canonical_json_bytes(material))


def understanding_prompt_identity(compile_main_prompt: str = "") -> Dict[str, str]:
    """Digests of the prompts this stage actually sends for one passage.

    The declared prompt version is not enough: the instruction body and the
    caller's compile prompt are model input, and editing either must make a
    stored understanding a miss.
    """
    return {
        "system_sha256": sha256_hex(UNDERSTANDING_SYSTEM_INSTRUCTION.encode("utf-8")),
        "instructions_sha256": sha256_hex(_instructions().encode("utf-8")),
        "compile_main_prompt_sha256": sha256_hex(
            (compile_main_prompt or "").encode("utf-8")),
    }


def understanding_schema_checksum() -> str:
    """Digest of the response schema this stage actually sends.

    Computed from the pydantic model itself, so adding a bound, renaming a
    field invalidates stored understanding. Python validator-body changes are
    not represented in JSON schema: bump UNDERSTANDING_RULE_VERSION for those
    changes and for changes in how a response becomes a contribution.
    """
    return sha256_hex(canonical_json_bytes(SourceUnderstandingResponse.model_json_schema()))


def understanding_cache_key(
    artifact: SourceArtifact,
    *,
    tenant_id: str,
    kb_id: str,
    model_name: str,
    config_fingerprint: str = "",
    compile_main_prompt: str = "",
    max_tokens: Optional[int] = None,
    active_constraints: Sequence[str] = (),
    schema_checksum: str = "",
) -> str:
    """The identity under which one source understanding may be reused.

    Covers the understanding *input*: the source version, its parser and the
    content identity of its local assets; the prompts, response schema and
    active constraints actually sent; and the generation-affecting
    configuration (model, output budget, thinking switch, retry bound, claim
    policy).  It deliberately excludes page layout and scheduling: page paths,
    page titles, target pages, concurrency and batch sizes are not part of what
    was read and must never invalidate it.

    Contains digests and declared identities only - never raw passage text,
    never a page path or a page title, and never a credential.
    """
    material = {
        "identity_version": UNDERSTANDING_IDENTITY_VERSION,
        "rule": UNDERSTANDING_RULE_VERSION,
        "tenant_id": tenant_id,
        "kb_id": kb_id,
        "source": {
            "source_id": artifact.source_id,
            "document_id": artifact.document_id,
            "chunk_id": artifact.chunk_id,
            "position": artifact.position,
            "content_generation": artifact.content_generation,
            "artifact_sha256": artifact.sha256,
            "text_sha256": sha256_hex(artifact.text.encode("utf-8")),
            "context_sha256": sha256_hex(str(
                (artifact.source_metadata or {}).get("contextual_text") or ""
            ).encode("utf-8")),
        },
        "parser": {"parser_id": artifact.parser_id, "parser_version": artifact.parser_version},
        "asset_digest": _asset_digest(artifact),
        "prompt": {"version": UNDERSTANDING_PROMPT_VERSION,
                   "checksum": UNDERSTANDING_PROMPT_CHECKSUM,
                   **understanding_prompt_identity(compile_main_prompt)},
        "schema": {"version": UNDERSTANDING_SCHEMA_VERSION,
                   "checksum": schema_checksum or understanding_schema_checksum()},
        "generation": {
            "max_tokens": max_tokens,
            "thinking": "disabled",
            "scene": UNDERSTANDING_SCENE,
            "attempts": MAX_UNDERSTANDING_ATTEMPTS,
        },
        "constraints_sha256": sha256_hex(canonical_json_bytes(
            [str(value) for value in active_constraints or ()])),
        "model": model_name,
        "config": config_fingerprint,
    }
    if _has_long_html_table(artifact.text):
        material["schema"]["literal_table_policy"] = "complete_html_table_16000_v1"
    return sha256_hex(canonical_json_bytes(material))


def _has_long_html_table(text: str) -> bool:
    return any(content_validation._MAX_EXCERPT < len(m.group())
               <= content_validation._MAX_TABLE_EXCERPT
               for m in re.finditer(r"<table\b[^>]*>[\s\S]*?</table>", text, re.IGNORECASE))


_TABLE_LITERAL_CONSTRAINT = (
    "Ordinary text excerpts remain limited to 4000 characters. A single complete HTML "
    "table may contain at most 16000 characters: copy its full literal markup, header "
    "and rows together as one fact. Keep separate tables and narrative in separate facts. "
    "Never truncate, paraphrase or discard rows to fit the ordinary text limit."
)


def _instructions() -> str:
    return (
        "Read the single source passage in `passage` and record what it states, "
        "with no page assignment and no page paths. For every substantive fact "
        "return an item in `facts`: "
        "`excerpt` (a verbatim contiguous span copied from passage.text that "
        "states it, including surrounding subject, period, unit, negation or "
        "condition needed to read it correctly; this span becomes the fact's "
        "published text). Copy source markup and escapes literally, including "
        "HTML tags and backslashes; do not replace them with rendered text. "
        "Select source wording rather than adding predicates, "
        "paraphrasing or deriving new facts. Keep facts that share essential "
        "context together in one excerpt instead of splitting away that context. "
        "`topic_hint` (what the fact is about, as the material names it), "
        "`original_names` (the names exactly as the material writes them, "
        "including Chinese names), `aliases` (only aliases the passage states "
        "explicitly for topic_hint; co-occurring entities are not aliases), "
        "`notes` (period, unit, negation or condition that changes "
        "how the fact reads), `mentions` (names that deserve their own page "
        "later; never a page id), and `relations` (only relations the passage "
        "states: kind describes the relationship in the material's own terms, with "
        "subject and target as the material names them). Put a pronoun or "
        "reference in the fact's `unbound` only when this passage cannot resolve it "
        "and it affects that fact. List it once. Omit empty or redundant optional "
        "reading hints; preserve substantive facts and their conditions. Never "
        "invent an excerpt, a name, an identifier or a version; an unsupported "
        "guess is worse than an empty list. "
        "Navigation and relations are optional reading hints. Keep substantive facts "
        "and their conditions even when no useful navigation can be extracted."
    )


def passage_payload(passage: Any, *, active_constraints: Sequence[str] = ()) -> Dict[str, Any]:
    """The model input: the passage and its own context, nothing global."""
    metadata = dict(getattr(passage, "source_metadata", {}) or {})
    context = metadata.pop("contextual_text", None)
    # The archive retains these provenance fields; the model already has text.
    metadata.pop("snapshot_text", None)
    metadata.pop("source_locations", None)
    for key in tuple(metadata):
        if key.endswith("_local_path"):
            metadata.pop(key, None)
    return {
        "stage": "understand_source",
        "instructions": _instructions(),
        "passage": {
            "passage_id": passage.passage_id,
            "source_id": passage.source_id,
            "document_id": passage.document_id,
            "chunk_id": passage.chunk_id,
            "position": getattr(passage, "position", None),
            "text": passage.text,
            "context_text": context,
            "source_metadata": metadata,
        },
        "active_constraints": list(active_constraints),
    }


def _gate_state(passage: Any) -> CompilerState:
    """A minimal state the existing evidence gate can validate against.

    Reusing that gate means layer one gets the same excerpt/page/excerpt-span
    checks as the legacy chain.  ``_DRAFT_PAGE`` only has to exist for the
    gate's page check; the records built from it carry no display.
    """
    from jonex_wiki.domain.engine.models.compiler_state import (
        PageIndexEntry,
        WikiPageSnapshot,
    )

    state = CompilerState()
    state.source_archive[passage.passage_id] = passage
    state.pages[_DRAFT_PAGE] = WikiPageSnapshot(
        index=PageIndexEntry(path=_DRAFT_PAGE, title="Source understanding"),
        content="",
    )
    return state


# Identity and version fields the *program* owns.  A model that echoes them is
# not trusted; the values are dropped (and counted) before validation rather
# than failing a whole passage over a harmless echo.
MODEL_ASSIGNED_FIELDS = ("record_id", "passage_id", "source_id", "document_id",
                         "chunk_id", "source_version", "content_generation",
                         "cache_key", "rule", "claim_id", "displays")


def strip_model_identity(raw: Mapping[str, Any]) -> Tuple[Dict[str, Any], int]:
    """Remove model-supplied identity fields; returns ``(payload, dropped)``."""
    payload = dict(raw)
    dropped = 0
    facts = []
    for item in payload.get("facts") or ():
        if not isinstance(item, Mapping):
            facts.append(item)
            continue
        fact = dict(item)
        for field in MODEL_ASSIGNED_FIELDS:
            if field in fact:
                fact.pop(field)
                dropped += 1
        relations = fact.get("relations")
        if isinstance(relations, list):
            cleaned = []
            for relation in relations:
                if isinstance(relation, Mapping):
                    relation = {key: value for key, value in relation.items()
                                if key not in MODEL_ASSIGNED_FIELDS}
                cleaned.append(relation)
            fact["relations"] = cleaned
        facts.append(fact)
    payload["facts"] = facts
    return payload, dropped


def _normalized(text: str) -> str:
    return " ".join(str(text or "").split())


def _excerpt_is_in_passage(excerpt: str, passage_text: str) -> bool:
    quote = _normalized(excerpt)
    return bool(quote) and quote in _normalized(passage_text)


def build_understanding(
    raw: Mapping[str, Any],
    passage: Any,
    *,
    cache_key: str,
    tenant_id: str,
    kb_id: str,
    artifact: SourceArtifact,
    model_name: str,
    config_fingerprint: str = "",
    produced_at: Optional[str] = None,
) -> SourceUnderstandingCheckpoint:
    """Turn one model response into contribution records, program-assigned.

    Every excerpt must really occur in the passage and supplies the fact's
    canonical wording. The model selects and organizes source text; its
    quote is the assertion itself. A quote that cannot
    be found is dropped and counted. Identifiers, versions and evidence
    references come from the program, never from the response.
    """
    from jonex_wiki.domain.engine.compile import source_contribution

    payload, dropped_identity = strip_model_identity(raw)
    if dropped_identity:
        count_work("compile_understanding_identity_field_ignored", dropped_identity)
    response = SourceUnderstandingResponse.model_validate(payload)
    drafts: List[ClaimDraft] = []
    pending: List[Tuple[Any, EvidenceRef]] = []
    rejected: List[str] = []
    for fact in response.facts:
        if not _excerpt_is_in_passage(fact.excerpt, passage.text):
            rejected.append(REASON_EXCERPT_NOT_IN_SOURCE)
            continue
        reference = EvidenceRef(
            passage_id=passage.passage_id,
            source_id=passage.source_id,
            document_id=passage.document_id,
            chunk_id=passage.chunk_id,
            excerpt=fact.excerpt,
        )
        drafts.append(ClaimDraft(page_path=_DRAFT_PAGE, text=fact.excerpt,
                                 evidence=[reference]))
        pending.append((fact, reference))

    state = _gate_state(passage)
    report = validate_claim_evidence(state, drafts)

    records: List[SourceContributionRecord] = []
    for draft, (fact, reference) in zip(drafts, pending):
        claim = next((item for item in report.validated_claims
                      if item.text == draft.text and item.evidence
                      and item.evidence[0].excerpt == reference.excerpt), None)
        if claim is None:
            rejected.append("UNDERSTANDING_CLAIM_REJECTED")
            continue
        record = _record_from_claim(claim, topic_hint=fact.topic_hint, provenance={
            "proposition": "verbatim_source_excerpt",
            "evidence": "validated_claim_evidence",
            "qualifiers": "deterministic_extractor_v1+model_notes",
            "entity_mentions": "model_original_names+capitalized_run_extractor_v1",
            "topic_hint": "model_topic_hint" if fact.topic_hint else "none",
            "understanding_rule": UNDERSTANDING_RULE_VERSION,
        })
        # The name forms the material itself used win over regex guesses, and
        # aliases the passage states explicitly are kept as names too.
        names: Dict[str, str] = {}
        for value in list(fact.original_names) + list(fact.aliases):
            text = _normalized(value)
            if text:
                names.setdefault(source_contribution.normalize_proposition(text), text)
        for key, value in record.original_names.items():
            names.setdefault(key, value)
        # Names found in the same fact are entity mentions, not aliases.
        # Preserve the model's explicit alias channel in the existing typed
        # qualifier metadata; older codecs accept this dictionary shape too.
        alias_subject = _normalized(fact.topic_hint)
        aliases = [{"kind": "declared_alias", "subject": alias_subject,
                    "value": _normalized(value)} for value in fact.aliases
                   if alias_subject and _normalized(value)
                   and alias_subject in _normalized(passage.text)
                   and _normalized(value) in _normalized(passage.text)]
        notes = [{"kind": "material_note", "value": _normalized(note)}
                 for note in fact.notes if _normalized(note)]
        unbound = sorted({_normalized(value) for value in
                          fact.unbound
                          if _normalized(value)})
        proposals = _link_proposals(fact, reference)
        records.append(record.model_copy(update={
            "original_names": names,
            "qualifiers": list(record.qualifiers) + notes + aliases,
            # Layer one assigns no page: a record is source understanding, and
            # its displays are filled in by page organization (S7.2).
            "displays": [],
            "link_proposals": proposals,
            "unbound": sorted(set(record.unbound) | set(unbound)),
        }))

    # Several selections may quote the same source fact while proposing
    # different reading hints. Its identity is shared, so merge those hints
    # before the organizer deduplicates by record_id; otherwise only the first
    # selection's names, conditions and navigation would survive publication.
    selected: Dict[str, SourceContributionRecord] = {}
    for record in records:
        existing = selected.get(record.record_id)
        if existing is None:
            selected[record.record_id] = record
            continue
        merged = {}
        for field in ("qualifiers", "entity_mentions", "link_proposals", "unbound"):
            values = list(getattr(existing, field))
            for value in getattr(record, field):
                if value not in values:
                    values.append(value)
            merged[field] = values
        names = dict(existing.original_names)
        for key, value in record.original_names.items():
            names.setdefault(key, value)
        selected[record.record_id] = existing.model_copy(update={
            **merged,
            # Keys are normalized names; retain the first spelling and page
            # hint when later selections use another form of the same fact.
            "original_names": names,
            "topic_hint": existing.topic_hint or record.topic_hint,
        })
    records = list(selected.values())

    return SourceUnderstandingCheckpoint(
        cache_key=cache_key,
        tenant_id=tenant_id,
        kb_id=kb_id,
        passage_id=passage.passage_id,
        source_id=passage.source_id,
        document_id=passage.document_id,
        chunk_id=passage.chunk_id,
        content_generation=artifact.content_generation,
        position=artifact.position,
        artifact_sha256=artifact.sha256,
        text_sha256=sha256_hex(passage.text.encode("utf-8")),
        parser_id=artifact.parser_id,
        parser_version=artifact.parser_version,
        asset_digest=_asset_digest(artifact),
        prompt_checksum=UNDERSTANDING_PROMPT_CHECKSUM,
        understanding_model=model_name,
        config_fingerprint=config_fingerprint,
        support_rule=SUPPORT_RULE_VERSION,
        produced_at=produced_at or datetime.now(timezone.utc).isoformat(),
        records=records,
        facts_seen=len(response.facts),
        facts_accepted=len(records),
        rejected_reasons=sorted(set(rejected)),
    )


# One synthetic path for the internal evidence gate only.  Layer one never
# publishes a page, and ``build_understanding`` clears ``displays``, so this
# value cannot reach a Bundle, an index or a record identity.
_DRAFT_PAGE = "unassigned/source-understanding.md"


def _link_proposals(fact: UnderstandingFact, reference: EvidenceRef) -> List[Dict[str, Any]]:
    """Mentions and relations in the *existing* link-proposal shape.

    Reusing ``relation_increment``'s proposal shape means page organization can
    publish these through the code path that already exists instead of growing
    a second link ledger.
    """
    proposals: List[Dict[str, Any]] = []
    seen = set()
    for mention in fact.mentions:
        text = _normalized(mention)
        if not text or text.casefold() in seen:
            continue
        seen.add(text.casefold())
        proposals.append({"target_mention": text, "why_visit": "source mentions it",
                          "relation": "", "evidence_refs": [reference.model_dump()]})
    for relation in fact.relations:
        target = _normalized(relation.target)
        if not target:
            continue
        key = "{}:{}".format(relation.kind, target.casefold())
        if key in seen:
            continue
        seen.add(key)
        proposals.append({
            "target_mention": target,
            "why_visit": _normalized(relation.kind) or "source states this relation",
            "relation": relation.kind,
            "subject_mention": _normalized(relation.subject),
            "evidence_refs": [reference.model_dump()],
        })
    return proposals


def checkpoint_identity_error(
    checkpoint: SourceUnderstandingCheckpoint,
    *,
    cache_key: str,
    artifact: SourceArtifact,
    tenant_id: str,
    kb_id: str,
) -> str:
    """Why a stored checkpoint may not answer for this source, or ``""``.

    A checkpoint is reused only when it is the *same* understanding of the
    *same* source version.  A late task carrying an older reading is refused
    here, before it can put a withdrawn fact back.
    """
    if checkpoint.cache_key != cache_key:
        return REASON_FOREIGN_CHECKPOINT
    if (checkpoint.tenant_id, checkpoint.kb_id) != (tenant_id, kb_id):
        return REASON_FOREIGN_CHECKPOINT
    identity = (checkpoint.passage_id, checkpoint.source_id, checkpoint.document_id,
                checkpoint.chunk_id)
    current = (pipeline.derive_passage_id(artifact), artifact.source_id,
               artifact.document_id, artifact.chunk_id)
    if identity != current:
        return REASON_FOREIGN_CHECKPOINT
    if (checkpoint.content_generation != artifact.content_generation
            or checkpoint.text_sha256 != sha256_hex(artifact.text.encode("utf-8"))
            or checkpoint.parser_id != artifact.parser_id
            or checkpoint.parser_version != artifact.parser_version
            or checkpoint.asset_digest != _asset_digest(artifact)):
        return REASON_STALE_CHECKPOINT
    return ""


def recompute_support(checkpoint: SourceUnderstandingCheckpoint, *,
                      passage: Any) -> SourceUnderstandingCheckpoint:
    """Refresh derived support labels under the current support rule.

    A support-rule change must not be inherited from an old checkpoint, and it
    must not cost a model call either: the stored statement and evidence are
    enough to re-run the same deterministic gate.
    """
    if checkpoint.support_rule == SUPPORT_RULE_VERSION:
        return checkpoint
    state = _gate_state(passage)
    drafts = []
    for record in checkpoint.records:
        if not record.evidence:
            continue
        drafts.append(ClaimDraft(page_path=_DRAFT_PAGE, text=record.proposition,
                                 evidence=list(record.evidence)))
    report = validate_claim_evidence(state, drafts) if drafts else None
    by_pair: Dict[Tuple[str, str], Any] = {}
    if report is not None:
        for claim in report.validated_claims:
            key = (claim.text, claim.evidence[0].excerpt if claim.evidence else "")
            by_pair[key] = claim
    refreshed: List[SourceContributionRecord] = []
    for record in checkpoint.records:
        key = (record.proposition,
               record.evidence[0].excerpt if record.evidence else "")
        claim = by_pair.get(key)
        if claim is None:
            refreshed.append(record)
            continue
        refreshed.append(record.model_copy(update={
            "support_status": str(claim.support_status),
            "support_reason": str(claim.support_reason),
        }))
    count_work("compile_understanding_support_recomputed", len(refreshed))
    return checkpoint.model_copy(update={"records": refreshed,
                                         "support_rule": SUPPORT_RULE_VERSION})


async def load_checkpoint(
    store: ArtifactStore,
    *,
    cache_key: str,
) -> Tuple[Optional[SourceUnderstandingCheckpoint], bool]:
    """Read one checkpoint.  Returns ``(checkpoint, was_corrupt)``.

    A corrupt, truncated or unsupported checkpoint is a miss that is *counted*:
    the run re-understands the source and rewrites a complete checkpoint rather
    than trusting bytes it cannot read.
    """
    try:
        data = await store.get_checkpoint(UNDERSTANDING_NAMESPACE, cache_key)
    except (FileNotFoundError, KeyError):
        return None, False
    except NotImplementedError:
        count_work("compile_understanding_store_unsupported")
        logger.warning("artifact store cannot checkpoint source understanding: %s",
                       REASON_STORE_CANNOT_CHECKPOINT)
        return None, False
    except (ValueError, OSError) as exc:
        count_work("compile_understanding_checkpoint_corrupt")
        logger.warning("source understanding checkpoint unreadable, re-understanding | key=%s error=%s",
                       cache_key[:12], type(exc).__name__)
        return None, True
    try:
        return SourceUnderstandingCheckpoint.model_validate_json(data), False
    except ValueError:
        count_work("compile_understanding_checkpoint_corrupt")
        logger.warning("source understanding checkpoint failed validation, re-understanding | key=%s",
                       cache_key[:12])
        return None, True


async def persist_checkpoint(
    store: ArtifactStore,
    checkpoint: SourceUnderstandingCheckpoint,
) -> None:
    """Write one checkpoint atomically; the store owns atomicity."""
    payload = checkpoint.model_dump_json().encode("utf-8")
    try:
        await store.put_checkpoint(UNDERSTANDING_NAMESPACE, checkpoint.cache_key, payload)
    except NotImplementedError as exc:
        count_work("compile_understanding_store_unsupported")
        raise SourceUnderstandingStoreUnsupported(
            "{}: this artifact store cannot persist source-understanding "
            "checkpoints, so layered compilation could not reuse its own work "
            "after a restart. Nothing was published.".format(
                REASON_STORE_CANNOT_CHECKPOINT)) from exc


def understanding_config_fingerprint(manifest: Mapping[str, Any]) -> str:
    """The understanding-affecting configuration, as one digest.

    Pure scheduling knobs are excluded: changing concurrency must not
    invalidate understanding.  The claim policy is included because the support
    labels computed in this stage depend on it.
    """
    curation = dict((manifest or {}).get("curation") or {})
    material = {
        "claim_policy": curation.get("claim_policy", ""),
        "understanding_rule": UNDERSTANDING_RULE_VERSION,
        "max_facts": MAX_UNDERSTANDING_FACTS,
    }
    return sha256_hex(canonical_json_bytes(material))


def _elapsed_ms(started: float) -> int:
    """Whole milliseconds since ``started`` (``time.monotonic``)."""
    return int((time.monotonic() - started) * 1000)


TRUNCATION_CONSTRAINT = (
    "The prior response exceeded the output budget. Preserve substantive facts "
    "and their source conditions. Compact optional reading hints instead: omit "
    "redundant notes, mentions, relations and unbound references; do not repeat "
    "the same excerpt in auxiliary fields. Return compact JSON."
)


async def _understand_attempt(
    state: CompilerState,
    passage: Any,
    *,
    model: ModelPort,
    model_name: str,
    routing: Mapping[str, Any],
    max_tokens: int,
    compile_main_prompt: str,
    extra_constraints: Sequence[str] = (),
) -> SourceUnderstandingResponse:
    """One model call.  Attempt policy lives in :func:`understand_one_passage`."""
    constraints = list(state.active_constraints) + list(extra_constraints)
    schema = SourceUnderstandingResponse.model_json_schema()
    if _has_long_html_table(passage.text):
        schema["$defs"]["UnderstandingFact"]["properties"]["excerpt"]["maxLength"] = content_validation._MAX_TABLE_EXCERPT
        constraints.append(_TABLE_LITERAL_CONSTRAINT)
    payload = passage_payload(passage, active_constraints=constraints)
    user = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    messages = [{"role": "system", "content": UNDERSTANDING_SYSTEM_INSTRUCTION}]
    if compile_main_prompt:
        messages.append({"role": "system", "content": compile_main_prompt})
    messages.append({"role": "user", "content": user})
    raw = await model.complete(
        messages,
        schema,
        max_tokens=max_tokens,
        model=model_name,
        tenant_id=routing.get("tenant_id", ""),
        context_id=routing.get("context_id", ""),
        trace_id=routing.get("trace_id", ""),
        thinking={"type": "disabled"},
        scene=UNDERSTANDING_SCENE,
    )
    payload, dropped_identity = strip_model_identity(raw)
    if dropped_identity:
        count_work("compile_understanding_identity_field_ignored", dropped_identity)
    return SourceUnderstandingResponse.model_validate(payload)


def understanding_failure(
    passage: Any,
    error: Optional[BaseException],
    *,
    attempts: int = MAX_UNDERSTANDING_ATTEMPTS,
) -> Dict[str, Any]:
    """Cause record with the same shape the legacy passage gate writes."""
    message = str(error) if error is not None else "understanding rejected"
    lowered = message.lower()
    if passage.source_metadata.get("compile_table_values", True) is False:
        category = "legitimate_no_claim"
    elif any(token in lowered for token in
             ("evidence", "excerpt", "claim", "source span", "verbatim")):
        category = "evidence_rejected"
    else:
        category = "model_protocol_failure"
    return {
        "passage_id": passage.passage_id,
        "category": category,
        "underlying_error_type": type(error).__name__ if error is not None else None,
        "message": message[:2000],
        "attempts": attempts,
        "source_id": passage.source_id,
        "content_chars": len(passage.text),
        "claim_compilation_degraded": True,
        "raw_source_archived": True,
    }


async def understand_one_passage(
    state: CompilerState,
    passage: Any,
    *,
    model: ModelPort,
    model_name: str,
    routing: Mapping[str, Any],
    max_tokens: int,
    compile_main_prompt: str,
    cancel: Optional[CancelPort] = None,
    attempt_sink: Optional[Callable[[str, int, int, Optional[BaseException]], None]] = None,
) -> Tuple[Optional[SourceUnderstandingResponse], Optional[BaseException]]:
    """One source, at most ``MAX_UNDERSTANDING_ATTEMPTS`` attempts.

    The bound is the legacy per-passage retry (one retry).  A truncated
    response is retried once with the compactness constraint instead of the
    identical prompt; other validation failures receive one bounded diagnostic.  Returns ``None`` when
    both attempts fail -- the caller accounts the passage with
    :func:`understanding_failure` rather than aborting the whole compile.

    ``attempt_sink`` observes each *sent request* as ``("call", n, 0, None)``
    before it goes out and then ``("ok" | "error", n, ms, exc)`` when it
    returns, so callers can count calls, retries and failures separately
    instead of inferring them from the number of successes.
    """
    last_error: Optional[BaseException] = None
    extra: Sequence[str] = ()
    for attempt in range(MAX_UNDERSTANDING_ATTEMPTS):
        if cancel:
            cancel.raise_if_cancelled()
        if attempt_sink is not None:
            attempt_sink("call", attempt + 1, 0, None)
        started = time.monotonic()
        try:
            response = await _understand_attempt(
                state, passage, model=model, model_name=model_name, routing=routing,
                max_tokens=max_tokens, compile_main_prompt=compile_main_prompt,
                extra_constraints=extra)
            if attempt_sink is not None:
                attempt_sink("ok", attempt + 1, _elapsed_ms(started), None)
            return response, None
        except (ValueError, RuntimeError) as exc:
            last_error = exc
            if attempt_sink is not None:
                attempt_sink("error", attempt + 1, _elapsed_ms(started), exc)
            if attempt == 0:
                extra = ((TRUNCATION_CONSTRAINT,) if getattr(exc, "output_truncated", False)
                         else (pipeline._compile_retry_constraint(exc),))
                if f"excerpt exceeds {content_validation._MAX_EXCERPT} characters" in str(exc):
                    extra += (
                        f"Each fact excerpt must contain at most {content_validation._MAX_EXCERPT} "
                        "characters for ordinary text. Do not copy a whole long passage into one fact. "
                        "Return multiple separate facts with contiguous verbatim excerpts; "
                        "each must retain its subject, period, unit and applicable conditions. "
                        "Do not truncate an excerpt or omit material facts to meet this limit.",
                    )
            logger.warning(
                "source understanding failure (attempt %d/%d) | pid=%s error=%s",
                attempt + 1, MAX_UNDERSTANDING_ATTEMPTS, passage.passage_id,
                str(exc)[:240],
            )
    logger.warning("source understanding rejected after %d attempts | pid=%s",
                   MAX_UNDERSTANDING_ATTEMPTS, passage.passage_id)
    return None, last_error


class UnderstandingStage:
    """One source-understanding stage bound to one compile's inputs.

    The stage exists so the layered driver (S7.2) can submit *individual*
    passages inside its own bounded window: the window bounds uncommitted work
    (in-flight + understood-but-not-yet-organized), while this stage's
    semaphore bounds simultaneous model calls to the same
    ``compile_concurrency``.  It holds no page layout, so a passage's
    understanding never depends on which pages exist.
    """

    def __init__(
        self,
        state: CompilerState,
        artifacts: Sequence[SourceArtifact],
        *,
        model: ModelPort,
        store: ArtifactStore,
        tenant_id: str,
        kb_id: str,
        model_name: str,
        routing: Mapping[str, Any],
        concurrency: int,
        manifest: Mapping[str, Any],
        telemetry: Any = None,
        cancel: Optional[CancelPort] = None,
        compile_main_prompt: str = "",
    ) -> None:
        import asyncio

        from jonex_wiki.domain.engine.compile.repair.planner import compile_output_tokens

        self.state = state
        self.model = model
        self.store = store
        self.tenant_id = tenant_id
        self.kb_id = kb_id
        self.model_name = model_name
        self.routing = dict(routing)
        self.telemetry = telemetry
        self.cancel = cancel
        self.compile_main_prompt = compile_main_prompt
        self.config_fingerprint = understanding_config_fingerprint(manifest)
        self.max_tokens = compile_output_tokens()
        #: Computed once per stage: the schema digest of what this stage sends.
        self.schema_checksum = understanding_schema_checksum()
        self.by_passage: Dict[str, Tuple[Any, SourceArtifact]] = {}
        for artifact in artifacts:
            passage = pipeline.artifact_to_passage(artifact)
            self.by_passage[passage.passage_id] = (passage, artifact)
        #: Keep programmatic table evidence in the archive for query reads,
        #: but honor the same compile flag as the shared compiler pipeline.
        self.order: List[str] = [pid for pid in state.source_archive
                                 if pid in self.by_passage
                                 and state.source_archive[pid].source_metadata.get(
                                     "compile_table_values", True) is not False]
        self.result = LayeredUnderstanding(order=list(self.order))
        self.semaphore = asyncio.Semaphore(max(1, int(concurrency)))
        self.lock = asyncio.Lock()

    def cache_key(self, pid: str) -> str:
        _passage, artifact = self.by_passage[pid]
        return understanding_cache_key(
            artifact, tenant_id=self.tenant_id, kb_id=self.kb_id,
            model_name=self.model_name, config_fingerprint=self.config_fingerprint,
            compile_main_prompt=self.compile_main_prompt,
            max_tokens=self.max_tokens,
            active_constraints=self.state.active_constraints,
            schema_checksum=self.schema_checksum)

    def _record_attempt(self, event: str, attempt: int, milliseconds: int,
                        error: Optional[BaseException]) -> None:
        """Account model calls separately from successful understandings.

        A protocol failure followed by a successful retry is two model calls;
        the checkpoint count is one.  Reporting the latter as the former is how
        retry cost disappears from the numbers, so the two are counted in
        different fields (and different telemetry counters).  This runs on the
        event loop: plain integer increments, no await, no lock.
        """
        result = self.result
        if event == "call":
            result.model_calls += 1
            count_work("compile_understanding_calls")
            if attempt > 1:
                result.retries += 1
                count_work("compile_understanding_retries")
        elif event == "error":
            result.attempt_failures += 1
            count_work("compile_understanding_attempt_failures")
        elif event == "ok":
            result.understandings += 1
        if milliseconds:
            result.model_ms += int(milliseconds)

    async def understand_one(self, pid: str) -> Optional[SourceUnderstandingCheckpoint]:
        """Understand one passage, reusing its checkpoint when it is valid.

        Returns ``None`` for a passage that failed both attempts; the failure
        is recorded in ``result.failures`` and in the compiler state, exactly
        like a legacy permanently-rejected passage.  Model calls, storage I/O
        and retry sleeps all happen *outside* any caller-held commit lock.

        ``understanding_ms`` accumulates this passage's whole stage duration,
        queueing included; it is a sum over passages, so concurrent passages
        can push it past the compile's wall clock.  ``model_ms``, ``queue_ms``,
        ``store_ms`` and ``stage_lock_ms`` are the parts *inside* that sum;
        they are reported for diagnosis and must never be added to it (or to
        each other) when a report is written.
        """
        started = time.monotonic()
        try:
            return await self._understand_one_timed(pid)
        finally:
            elapsed = _elapsed_ms(started)
            waited = time.monotonic()
            async with self.lock:
                self.result.understanding_ms += elapsed
                self.result.stage_lock_ms += _elapsed_ms(waited)

    async def _understand_one_timed(
        self, pid: str
    ) -> Optional[SourceUnderstandingCheckpoint]:
        passage, artifact = self.by_passage[pid]
        cache_key = self.cache_key(pid)
        #: Checkpoint I/O milliseconds for this passage, flushed once below.
        store_ms = 0
        queued = time.monotonic()
        async with self.semaphore:
            if self.cancel:
                self.cancel.raise_if_cancelled()
            waited = _elapsed_ms(queued)
            if waited:
                async with self.lock:
                    self.result.queue_ms += waited
            try:
                with telemetry_stage(self.telemetry, "source_understanding"):
                    reading = time.monotonic()
                    checkpoint, corrupt = await load_checkpoint(self.store, cache_key=cache_key)
                    store_ms += _elapsed_ms(reading)
                    stale = 0
                    if checkpoint is not None:
                        reason = checkpoint_identity_error(
                            checkpoint, cache_key=cache_key, artifact=artifact,
                            tenant_id=self.tenant_id, kb_id=self.kb_id)
                        if reason:
                            stale = 1
                            checkpoint = None
                    if checkpoint is not None:
                        if checkpoint.support_rule != SUPPORT_RULE_VERSION:
                            checkpoint = recompute_support(checkpoint, passage=passage)
                            # Durability: the refreshed labels are written back, so
                            # the next run reuses them instead of recomputing the
                            # same derived values on every compile.
                            writing = time.monotonic()
                            await persist_checkpoint(self.store, checkpoint)
                            store_ms += _elapsed_ms(writing)
                            async with self.lock:
                                self.result.support_recomputed += 1
                        async with self.lock:
                            self.result.checkpoints[pid] = checkpoint
                            self.result.cache_hits += 1
                            self.result.reused_records += len(checkpoint.records)
                            self.result.corrupted += int(corrupt)
                            self.result.stale += stale
                        count_work("compile_understanding_cache_hit")
                        count_work("compile_understanding_reused_records",
                                   len(checkpoint.records))
                        return checkpoint
                    if stale:
                        count_work("compile_understanding_stale_rejected")
                        logger.warning(
                            "source understanding checkpoint refused as stale | pid=%s reason=%s",
                            pid, REASON_STALE_CHECKPOINT)
                    # A miss is a passage that needed the model, whether or not
                    # the model then produced a usable checkpoint.
                    async with self.lock:
                        self.result.cache_misses += 1
                    response, error = await understand_one_passage(
                        self.state, passage, model=self.model, model_name=self.model_name,
                        routing=self.routing, max_tokens=self.max_tokens,
                        compile_main_prompt=self.compile_main_prompt, cancel=self.cancel,
                        attempt_sink=self._record_attempt)
                    if response is None:
                        failure = understanding_failure(passage, error)
                        async with self.lock:
                            self.result.failures[pid] = failure
                            self.result.corrupted += int(corrupt)
                            self.result.stale += stale
                        self.state.passage_failures[pid] = failure
                        count_work("compile_understanding_failed")
                        return None
                    checkpoint = build_understanding(
                        response.model_dump(), passage, cache_key=cache_key,
                        tenant_id=self.tenant_id, kb_id=self.kb_id, artifact=artifact,
                        model_name=self.model_name,
                        config_fingerprint=self.config_fingerprint)
                    writing = time.monotonic()
                    await persist_checkpoint(self.store, checkpoint)
                    store_ms += _elapsed_ms(writing)
                    async with self.lock:
                        self.result.checkpoints[pid] = checkpoint
                        self.result.corrupted += int(corrupt)
                        self.result.stale += stale
                        self.result.facts_rejected += len(checkpoint.rejected_reasons)
                        self.result.records += len(checkpoint.records)
                    count_work("compile_understanding_records", len(checkpoint.records))
                    if checkpoint.rejected_reasons:
                        count_work("compile_understanding_rejected_facts",
                                   len(checkpoint.rejected_reasons))
                    return checkpoint
            finally:
                if store_ms:
                    async with self.lock:
                        self.result.store_ms += store_ms


async def understand_sources(
    state: CompilerState,
    artifacts: Sequence[SourceArtifact],
    *,
    model: ModelPort,
    store: ArtifactStore,
    tenant_id: str,
    kb_id: str,
    model_name: str,
    routing: Mapping[str, Any],
    concurrency: int,
    manifest: Mapping[str, Any],
    telemetry: Any = None,
    cancel: Optional[CancelPort] = None,
    compile_main_prompt: str = "",
) -> LayeredUnderstanding:
    """Understand every passage once, reusing persisted understanding.

    Concurrency is the *existing* ``compile_concurrency``: understanding a
    source and organizing pages are two stages of one compilation, not two
    deployment knobs.  Ordering is the archive order, so the same inputs
    produce the same checkpoints regardless of completion order.  This
    all-passages form is the S7.1 entry point; the layered compile drives the
    same stage through its bounded window (``organization.py``).
    """
    import asyncio

    stage = UnderstandingStage(
        state, artifacts, model=model, store=store, tenant_id=tenant_id, kb_id=kb_id,
        model_name=model_name, routing=routing, concurrency=concurrency,
        manifest=manifest, telemetry=telemetry, cancel=cancel,
        compile_main_prompt=compile_main_prompt)
    await asyncio.gather(*[stage.understand_one(pid) for pid in stage.order])
    return stage.result


def published_records(understanding: LayeredUnderstanding) -> List[SourceContributionRecord]:
    """Every understanding record, in stable passage order."""
    records: List[SourceContributionRecord] = []
    for pid in sorted(understanding.checkpoints):
        records.extend(understanding.checkpoints[pid].records)
    return records
