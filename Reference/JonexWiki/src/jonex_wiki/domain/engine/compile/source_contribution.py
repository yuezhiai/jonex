"""Page-independent source contributions.

A claim as the compiler knows it today is a *display*: it belongs to one wiki
page, and its identity (``_derive_claim_id``) hashes that page path.  The same
source fact shown on two pages therefore has two unrelated identities, and
removing one page deletes "the fact" as far as the compiler can tell.

A ``SourceContributionRecord`` is the same material with the page taken out:

* identity = source version + evidence location + normalized proposition, so a
  fact displayed on two pages is *one* record with two displays;
* the shape reuses ``EvidenceRef``/``SourceSpan``/``ArchivedPassage`` rather than
  introducing a second archive;
* the original wording is kept next to the normalized form, and names that no
  rule recognized stay unbound instead of being guessed at;
* ``to_claims``/``from_claims`` adapt between this record and the existing
  ``ClaimDraft``/``ValidatedClaim`` shapes, so nothing downstream changes.

Nothing here calls a model.  A Bundle that predates these records is rebuilt
from the claims it already stores: the fields that material cannot supply come
back empty and are marked as such, never invented.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field

from jonex_wiki.domain.engine.compile.content_validation import (
    EvidenceRef,
    ValidatedClaim,
    _derive_claim_id,
)
from jonex_wiki.domain.engine.models.compiler_state import SourceSpan

CONTRIBUTION_RULE_VERSION = "source-contribution-v1"
STALE_TASK_CODE = "SOURCE_CONTRIBUTION_STALE_TASK"
NORMALIZATION_RULE = "nfkc-casefold-collapse-v1"
FILE_NAME = "source_contributions.json"
#: A contribution the previous Bundle published and this publication does not.
#: Withdrawal is a real loss of coverage, so it is reported, never silent.
WITHDRAWN_CODE = "SOURCE_CONTRIBUTION_WITHDRAWN"

# Deterministic, documented extractors.  They are hints for later merging, not
# semantic judgements: provenance records which fields came from where.
_ENTITY_RE = re.compile(r"\b[A-Z][A-Za-z0-9\-]*(?:\s+[A-Z][A-Za-z0-9\-]*)*\b")
_QUALIFIER_RES: Sequence[tuple] = (
    ("period", re.compile(r"\b(?:1[89]|20)\d{2}\b(?:\s*(?:-|–|to|until)\s*(?:1[89]|20)\d{2})?")),
    ("percent", re.compile(r"\b\d+(?:[.,]\d+)?\s?%")),
    ("amount", re.compile(r"(?:[$€£¥]|\b(?:USD|EUR|GBP|CNY|RMB|JPY)\b)\s?\d[\d,\.]*")),
    ("unit", re.compile(r"\b\d+(?:[.,]\d+)?\s?(?:kg|km|mi|m|ft|ha|acres?|tonnes?|t|MW|GW|kWh|MWh|bps)\b")),
)
_ENTITY_STOPWORDS = frozenset({
    "The", "A", "An", "In", "On", "At", "By", "For", "From", "With", "And", "But",
    "This", "That", "These", "Those", "It", "He", "She", "They", "We", "His", "Her",
    "Their", "Its", "Fact", "Line", "Note", "Source", "Page", "Section", "Table",
})


def normalize_proposition(text: str) -> str:
    """Normalization used *only* for identity: NFKC, case-fold, collapse space."""
    folded = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return " ".join(folded.split())


def contribution_id(*, spans: Sequence[SourceSpan], proposition: str) -> str:
    """Stable identity: source version + evidence location + normalized content.

    Deliberately independent of every wiki path, of evidence *order* and of
    Python's per-process ``hash()``.
    """
    locations = sorted(
        (str(span.source_id), str(span.document_id), str(span.chunk_id),
         str(span.source_sha256), str(span.passage_text_sha256), int(span.start_char), int(span.end_char))
        for span in spans
    )
    payload = {
        "rule": CONTRIBUTION_RULE_VERSION,
        "normalization": NORMALIZATION_RULE,
        "locations": locations,
        "proposition": normalize_proposition(proposition),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return "contrib_" + hashlib.sha256(encoded).hexdigest()[:24]


def entity_mentions(text: str, *, limit: int = 8) -> List[str]:
    """Capitalised runs, minus sentence-initial stopwords.  A hint, not a typing."""
    found: List[str] = []
    for match in _ENTITY_RE.finditer(str(text or "")):
        value = match.group(0).strip()
        if not value or value in _ENTITY_STOPWORDS or value in found:
            continue
        found.append(value)
        if len(found) >= limit:
            break
    return found


def qualifiers(text: str, *, limit: int = 8) -> List[Dict[str, str]]:
    """Periods, percentages, amounts and units that appear in the span text."""
    found: List[Dict[str, str]] = []
    seen = set()
    for kind, pattern in _QUALIFIER_RES:
        for match in pattern.finditer(str(text or "")):
            value = " ".join(match.group(0).split())
            key = (kind, value)
            if key in seen:
                continue
            seen.add(key)
            found.append({"kind": kind, "value": value})
            if len(found) >= limit:
                return found
    return found


class ContributionDisplay(BaseModel):
    """Where one source fact is shown.  Removing this never removes the fact."""

    page_path: str
    claim_id: str

    model_config = ConfigDict(extra="forbid")


class SourceContributionRecord(BaseModel):
    """A source fact, with no page path anywhere in its identity."""

    record_id: str
    proposition: str
    normalized_proposition: str
    evidence: List[EvidenceRef] = Field(default_factory=list, max_length=20)
    source_spans: List[SourceSpan] = Field(default_factory=list, max_length=20)
    # Each alternative is independently sufficient; all_of members within one
    # alternative are joint requirements. Missing means conservative joint spans.
    support_alternatives: Optional[List[Dict[str, Any]]] = None
    unresolved_evidence: List[EvidenceRef] = Field(default_factory=list, max_length=20)
    # 限定信息 / 实体提及 / 可选主题提示: hints for merging, never identity.
    qualifiers: List[Dict[str, str]] = Field(default_factory=list)
    entity_mentions: List[str] = Field(default_factory=list)
    topic_hint: str = ""
    # 原始名称保留: whatever the material called things, unchanged.
    original_names: Dict[str, str] = Field(default_factory=dict)
    link_proposals: List[Dict[str, Any]] = Field(default_factory=list)
    support_status: str = "NOT_ENOUGH_INFO"
    support_reason: str = "support_not_evaluated"
    displays: List[ContributionDisplay] = Field(default_factory=list)
    provenance: Dict[str, str] = Field(default_factory=dict)
    unbound: List[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class ContributionTask(BaseModel):
    """What a task read before it produced contributions.

    Every task carries the source versions it saw.  A task whose version no
    longer matches the store is refused, so an old concurrent snapshot cannot
    put a withdrawn contribution back.
    """

    rule: str = CONTRIBUTION_RULE_VERSION
    source_versions: Dict[str, str] = Field(default_factory=dict)
    read_at_revision: str = ""
    rejections: List[Dict[str, str]] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")

    def stale_sources(self, current_versions: Mapping[str, str]) -> List[str]:
        current = dict(current_versions or {})
        return sorted(
            source_id for source_id, version in self.source_versions.items()
            if current.get(source_id) != version
        )


class SourceContributionSet(BaseModel):
    """The versioned set carried by one Bundle."""

    version: str = CONTRIBUTION_RULE_VERSION
    records: List[SourceContributionRecord] = Field(default_factory=list)
    by_source: Dict[str, List[str]] = Field(default_factory=dict)
    # Normalized proposition -> record ids.  A fact index, not an identity: it
    # lets a caller see that a fact still stands after one of its sources goes.
    fact_index: Dict[str, List[str]] = Field(default_factory=dict)
    source_versions: Dict[str, str] = Field(default_factory=dict)
    rebuilt_from_legacy: bool = False

    model_config = ConfigDict(extra="forbid")

    def record(self, record_id: str) -> Optional[SourceContributionRecord]:
        for entry in self.records:
            if entry.record_id == record_id:
                return entry
        return None

    def displays_for(self, page_path: str) -> List[ContributionDisplay]:
        return [display for record in self.records for display in record.displays
                if display.page_path == page_path]

    def facts(self, proposition: str) -> List[str]:
        return list(self.fact_index.get(normalize_proposition(proposition), ()))


def _record_from_claim(claim: ValidatedClaim, *, topic_hint: str = "",
                       provenance: Mapping[str, str]) -> SourceContributionRecord:
    spans = list(claim.source_spans)
    text = str(claim.text or "")
    evidence = list(claim.evidence)
    names: Dict[str, str] = {}
    for ref in evidence:
        for value in entity_mentions(ref.excerpt):
            names.setdefault(normalize_proposition(value), value)
    return SourceContributionRecord(
        record_id=contribution_id(spans=spans, proposition=text),
        proposition=text,
        normalized_proposition=normalize_proposition(text),
        evidence=evidence,
        source_spans=spans,
        unresolved_evidence=list(claim.unresolved_evidence),
        qualifiers=qualifiers(" ".join([text] + [ref.excerpt for ref in evidence])),
        entity_mentions=entity_mentions(text) or entity_mentions(
            " ".join(ref.excerpt for ref in evidence)),
        topic_hint=topic_hint,
        original_names=names,
        support_status=str(claim.support_status),
        support_reason=str(claim.support_reason),
        displays=[ContributionDisplay(page_path=claim.page_path, claim_id=claim.claim_id)],
        provenance=dict(provenance),
        unbound=sorted(set(str(ref.passage_id) for ref in claim.unresolved_evidence)),
    )


def _index(records: Sequence[SourceContributionRecord]) -> tuple:
    by_source: Dict[str, List[str]] = {}
    fact_index: Dict[str, List[str]] = {}
    for record in records:
        for span in record.source_spans:
            ids = by_source.setdefault(str(span.source_id), [])
            if record.record_id not in ids:
                ids.append(record.record_id)
        keys = fact_index.setdefault(record.normalized_proposition, [])
        if record.record_id not in keys:
            keys.append(record.record_id)
    return ({key: sorted(value) for key, value in sorted(by_source.items())},
            {key: sorted(value) for key, value in sorted(fact_index.items())})


def from_claims(claims: Iterable[ValidatedClaim], *,
                topic_hints: Optional[Mapping[str, str]] = None,
                source_versions: Optional[Mapping[str, str]] = None,
                rebuilt_from_legacy: bool = False,
                pages: Optional[Mapping[str, Any]] = None) -> SourceContributionSet:
    """Build the record set from the claims the compiler already produced.

    The same fact on two pages becomes one record with two displays; the two
    display relationships are what differ, exactly as the plan requires.
    """
    hints = dict(topic_hints or {})
    provenance = {
        "proposition": "compiled_claim_text",
        "evidence": "validated_claim_evidence",
        "qualifiers": "deterministic_extractor_v1",
        "entity_mentions": "capitalized_run_extractor_v1",
        "topic_hint": "page_topic_hint" if hints else "none",
    }
    records: Dict[str, SourceContributionRecord] = {}
    expanded = []
    from jonex_wiki.domain.engine.models.claim import assess_claim_support
    for claim in claims or ():
        source_ids = sorted({span.source_id for span in claim.source_spans})
        if len(source_ids) <= 1:
            expanded.append(claim)
            continue
        for source_id in source_ids:
            spans = [span for span in claim.source_spans if span.source_id == source_id]
            refs = [ref for ref in claim.evidence if ref.source_id == source_id]
            unresolved = [ref for ref in claim.unresolved_evidence if ref.source_id == source_id]
            status, reason = assess_claim_support(claim.text, spans, has_unresolved_evidence=bool(unresolved))
            expanded.append(claim.model_copy(update={"source_spans": spans, "evidence": refs,
                "unresolved_evidence": unresolved, "support_status": status, "support_reason": reason}))
    for claim in expanded:
        candidate = _record_from_claim(claim, topic_hint=hints.get(claim.page_path, ""),
                                       provenance=provenance)
        existing = records.get(candidate.record_id)
        if existing is None:
            records[candidate.record_id] = candidate
        else:
            # Same source fact, another page: keep both displays, one identity.
            if not any(item.page_path == claim.page_path and item.claim_id == claim.claim_id
                       for item in existing.displays):
                existing.displays.append(
                    ContributionDisplay(page_path=claim.page_path, claim_id=claim.claim_id))
            if not existing.topic_hint and candidate.topic_hint:
                existing.topic_hint = candidate.topic_hint
    # Metadata travels with its source contribution, so source replacement and
    # withdrawal naturally remove it. Navigation also needs a live origin.
    for record in records.values():
        known = {(ref.passage_id, " ".join(ref.excerpt.split())) for ref in record.evidence}
        for display in record.displays:
            page = (pages or {}).get(display.page_path)
            for mention in getattr(page, "link_mentions", ()):
                refs = mention.evidence_refs
                if refs and all((str(ref.get("passage_id", "")), " ".join(str(ref.get("excerpt", "")).split())) in known for ref in refs):
                    value = {"page_path": display.page_path, **mention.model_dump()}
                    if value not in record.link_proposals:
                        record.link_proposals.append(value)
    ordered = [records[key] for key in sorted(records)]
    for record in ordered:
        record.displays.sort(key=lambda item: (item.page_path, item.claim_id))
    by_source, fact_index = _index(ordered)
    return SourceContributionSet(
        records=ordered,
        by_source=by_source,
        fact_index=fact_index,
        source_versions={str(key): str(value)
                         for key, value in sorted(dict(source_versions or {}).items())},
        rebuilt_from_legacy=bool(rebuilt_from_legacy),
    )


def reconcile_publication(
    previous: Optional[SourceContributionSet], *,
    incoming: Sequence[SourceContributionRecord],
    read_versions: Mapping[str, str],
    current_versions: Mapping[str, str],
    read_at_revision: str = "",
    affected_sources: Optional[Iterable[str]] = None,
) -> Tuple[SourceContributionSet, List[Dict[str, str]]]:
    """One compile's publication entry for the contribution ledger.

    The plan's order, in one place: the sources this run re-read are withdrawn
    first, a record whose source was replaced while the run was in flight is
    refused with a machine-readable rejection, and only then do this run's
    records land.  ``read_versions`` are the versions the readers actually saw
    (the understanding checkpoints record them), so the version guard is a real
    read-versus-store comparison and not a comparison of a map with itself.

    ``affected_sources`` defaults to *every* live source, which is what a
    full-corpus re-read needs: the views are rebuilt from what this run
    produced.  An incremental caller passes the sources it actually re-read, so
    the contributions of untouched sources keep their published identity.
    """
    rejections: List[Dict[str, str]] = []
    task = open_task(read_versions, source_ids=current_versions,
                     read_at_revision=read_at_revision)
    affected = (sorted(str(source_id) for source_id in current_versions)
                if affected_sources is None
                else sorted(str(source_id) for source_id in affected_sources))
    merged = reconcile(
        previous or SourceContributionSet(),
        incoming=incoming,
        current_versions=current_versions,
        task=task,
        affected_sources=affected,
        rejections=rejections,
    )
    return merged, rejections


def open_task(current_versions: Mapping[str, str], *,
              source_ids: Optional[Iterable[str]] = None,
              read_at_revision: str = "") -> ContributionTask:
    """Record which source versions this task is about to read."""
    current = dict(current_versions or {})
    wanted = current if source_ids is None else {
        str(source_id): str(current.get(str(source_id), "")) for source_id in source_ids
    }
    return ContributionTask(
        source_versions={key: value for key, value in sorted(wanted.items())},
        read_at_revision=str(read_at_revision or ""),
    )


def assert_fresh(task: ContributionTask, current_versions: Mapping[str, str]) -> None:
    """Refuse a task whose sources were replaced while it ran."""
    stale = task.stale_sources(current_versions)
    if stale:
        raise ValueError("{}:{}".format(STALE_TASK_CODE, ",".join(stale)))


def _prune(record: SourceContributionRecord, live_sources: Mapping[str, str],
           affected: Iterable[str] = ()) -> Optional[SourceContributionRecord]:
    """Drop the locations of sources that no longer stand; keep the rest.

    A source that is being replaced counts as not standing: its locations leave
    the record first, and the task's own records re-add whatever survives.  A
    record that still holds another source's location keeps the fact.
    """
    replaced = {str(source_id) for source_id in affected}
    keep = lambda source_id: str(source_id) in live_sources and str(source_id) not in replaced
    spans = [span for span in record.source_spans if keep(span.source_id)]
    if record.source_spans and not spans:
        return None
    evidence = [ref for ref in record.evidence if keep(ref.source_id)]
    if record.evidence and not evidence:
        return None
    if not spans and not evidence:
        return None
    return record.model_copy(update={"evidence": evidence, "source_spans": spans})


def reconcile(current: SourceContributionSet, *,
              incoming: Sequence[SourceContributionRecord] = (),
              current_versions: Mapping[str, str],
              task: Optional[ContributionTask] = None,
              affected_sources: Optional[Iterable[str]] = None,
              display_moves: Optional[Mapping[str, str]] = None,
              rejections: Optional[List[Dict[str, str]]] = None) -> SourceContributionSet:
    """Apply one task's contributions to the set the previous Bundle carried.

    Order matters and is the point of the plan: the affected sources'
    contributions are withdrawn first, the affected views are then rebuilt from
    the records that remain, and only then do the task's records land.  A
    record still supported by another source survives with that source's
    locations; a record whose every source is gone disappears.
    """
    live = {str(key): str(value) for key, value in dict(current_versions or {}).items()}
    notes = rejections if rejections is not None else []
    if task is not None:
        stale = task.stale_sources(live)
        for source_id in stale:
            notes.append({"source_id": source_id, "code": STALE_TASK_CODE,
                          "detail": "task read {} but the store holds {}".format(
                              task.source_versions.get(source_id, ""),
                              live.get(source_id, ""))})

    moved = {str(old): str(new) for old, new in dict(display_moves or {}).items()}
    affected = {str(source_id) for source_id in (affected_sources or ())}
    affected |= {
        source_id for source_id in dict(current.source_versions or {})
        if live.get(source_id) != current.source_versions[source_id]
    }
    affected |= {source_id for source_id in dict(current.source_versions or {})
                 if source_id not in live}

    kept: Dict[str, SourceContributionRecord] = {}
    for record in current.records:
        trimmed = _prune(record, live, affected) if affected else record
        if trimmed is None:
            continue
        if affected and any(str(span.source_id) in affected for span in record.source_spans):
            # This source's contribution is being replaced: drop its display
            # bindings only if the record survives on other sources.
            pass
        displays = []
        for display in trimmed.displays:
            path = moved.get(display.page_path, display.page_path)
            displays.append(ContributionDisplay(page_path=path, claim_id=display.claim_id))
        kept[trimmed.record_id] = trimmed.model_copy(update={"displays": displays})

    for record in incoming or ():
        sources = {str(span.source_id) for span in record.source_spans}
        stale = sorted(source_id for source_id in sources
                       if task is not None
                       and task.source_versions.get(source_id) != live.get(source_id))
        if stale:
            notes.append({"source_id": stale[0], "code": STALE_TASK_CODE,
                          "detail": "record " + record.record_id + " was withdrawn"})
            continue
        if not sources and record.evidence:
            sources = {str(ref.source_id) for ref in record.evidence}
        if sources and not sources & set(live):
            # Its only sources are gone: the contribution is withdrawn, not kept
            # as an orphan.  Reported, so the loss is auditable.
            notes.append({"source_id": sorted(sources)[0], "code": WITHDRAWN_CODE,
                          "detail": "contribution {} has no live source".format(
                              record.record_id)})
            continue
        displays = [ContributionDisplay(page_path=moved.get(item.page_path, item.page_path),
                                        claim_id=item.claim_id)
                    for item in record.displays]
        kept[record.record_id] = record.model_copy(update={"displays": displays})

    records = [kept[key] for key in sorted(kept)]
    for record in records:
        record.displays.sort(key=lambda item: (item.page_path, item.claim_id))
    published_now = {record.record_id for record in records}
    for record in current.records:
        if record.record_id in published_now:
            continue
        source_ids = sorted({str(span.source_id) for span in record.source_spans})
        if any(source_id in live for source_id in source_ids):
            # Its source still stands: the record was re-read and replaced by
            # this run's own version, which is an update, not a loss.
            continue
        notes.append({
            "source_id": source_ids[0] if source_ids else "",
            "code": WITHDRAWN_CODE,
            "detail": "contribution {} was published before and is not in this "
                      "publication (sources: {})".format(
                          record.record_id, ",".join(source_ids) or "none"),
        })
    by_source, fact_index = _index(records)
    merge_versions = dict(current.source_versions)
    merge_versions.update({key: value for key, value in live.items()})
    for source_id in list(merge_versions):
        if source_id not in live:
            merge_versions.pop(source_id)
    return SourceContributionSet(
        records=records,
        by_source=by_source,
        fact_index=fact_index,
        source_versions={key: merge_versions[key] for key in sorted(merge_versions)},
        rebuilt_from_legacy=current.rebuilt_from_legacy,
    )


def rebind_displays(current: SourceContributionSet,
                    moves: Mapping[str, str]) -> SourceContributionSet:
    """A page move renames display bindings; no source fact identity changes."""
    moved = {str(old): str(new) for old, new in dict(moves or {}).items()}
    if not moved:
        return current
    records = []
    for record in current.records:
        displays = sorted((ContributionDisplay(
            page_path=moved.get(display.page_path, display.page_path),
            claim_id=display.claim_id) for display in record.displays),
            key=lambda item: (item.page_path, item.claim_id))
        records.append(record.model_copy(update={"displays": displays}))
    by_source, fact_index = _index(records)
    return current.model_copy(update={"records": records, "by_source": by_source,
                                     "fact_index": fact_index})


def to_claims(contributions: SourceContributionSet,
              *, unresolved_page: str = "") -> List[ValidatedClaim]:
    """Distribute records back onto pages and re-form the legacy shape.

    The claim id is derived exactly as the pipeline derives it today
    (``_derive_claim_id(page_path, text, evidence)``), so a round trip through
    the internal records reproduces the previous output byte for byte.
    """
    claims: List[ValidatedClaim] = []
    for record in contributions.records:
        for display in record.displays:
            claims.append(ValidatedClaim(
                claim_id=_derive_claim_id(display.page_path, record.proposition,
                                          list(record.evidence)),
                page_path=display.page_path,
                text=record.proposition,
                evidence=list(record.evidence),
                unresolved_evidence=list(record.unresolved_evidence),
                source_spans=list(record.source_spans),
                support_status=record.support_status,
                support_reason=record.support_reason,
            ))
    claims.sort(key=lambda claim: (claim.page_path, claim.claim_id))
    return claims


def payload(contributions: SourceContributionSet) -> Dict[str, Any]:
    """The exact JSON written into the Bundle."""
    body = contributions.model_dump()
    digest = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")).hexdigest()
    return {**body, "digest": digest}


def verify(data: Mapping[str, Any]) -> None:
    """Reject a record set that does not match its own digest."""
    body = {key: value for key, value in dict(data).items() if key != "digest"}
    digest = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")).hexdigest()
    if digest != str(data.get("digest") or ""):
        raise ValueError("SOURCE_CONTRIBUTIONS_DIGEST_MISMATCH")


def load(data: Optional[Mapping[str, Any]], *,
         claims: Iterable[ValidatedClaim] = (),
         source_versions: Optional[Mapping[str, str]] = None) -> SourceContributionSet:
    """Read the record set, rebuilding it from claims when a Bundle has none."""
    if not data:
        return from_claims(claims, source_versions=source_versions,
                           rebuilt_from_legacy=True)
    if data.get("version") != CONTRIBUTION_RULE_VERSION:
        raise ValueError("SOURCE_CONTRIBUTIONS_VERSION_UNSUPPORTED")
    verify(data)
    body = {key: value for key, value in dict(data).items() if key != "digest"}
    return SourceContributionSet.model_validate(body)


__all__ = [
    "CONTRIBUTION_RULE_VERSION",
    "STALE_TASK_CODE",
    "ContributionDisplay",
    "ContributionTask",
    "FILE_NAME",
    "NORMALIZATION_RULE",
    "SourceContributionRecord",
    "SourceContributionSet",
    "contribution_id",
    "assert_fresh",
    "entity_mentions",
    "from_claims",
    "load",
    "open_task",
    "normalize_proposition",
    "payload",
    "qualifiers",
    "rebind_displays",
    "reconcile_publication",
    "WITHDRAWN_CODE",
    "reconcile",
    "to_claims",
    "verify",
]
