"""Deterministic evidence disposition and explicitly scoped isolation.

Source records attest to an exact source interval, not a generated proposition.
They deliberately use a different identity namespace from ValidatedClaim.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from jonex_wiki.domain.engine.compile.error_book import ErrorBook, ErrorBookEntry

RULE_VERSION = "evidence-disposition-v2"
LEGACY_RULE_VERSION = "evidence-disposition-v1"
KEEP_AND_SEARCH_RULE_VERSION = "keep-and-search-v1"

# The keep-and-search default archives every decodable source interval and
# generated Wiki page.  The two evidence-disposition rules are retained only so
# previously published bundles can still be cold-opened byte-for-byte.
_OLD_RULE_VERSIONS = (RULE_VERSION, LEGACY_RULE_VERSION)


def is_old_rule(version: str | None) -> bool:
    return version in _OLD_RULE_VERSIONS


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def quarantine_scope(book: ErrorBook | None) -> dict[str, set[str]]:
    scope = {key: set() for key in ("claims", "pages", "sources", "passages")}
    for entry in book.entries if book else ():
        if entry.status != "open" or entry.error_class != "Quarantine":
            continue
        scope["claims"].update(entry.affected_claim_ids)
        scope["sources"].update(entry.affected_source_ids)
        scope["passages"].update(entry.affected_passage_ids)
        # Older discoveries only name pages. Conservatively isolate that page,
        # but never infer that all of its otherwise valid sources are unsafe.
        if not (entry.affected_claim_ids or entry.affected_source_ids or entry.affected_passage_ids):
            scope["pages"].update(entry.affected_paths)
    return scope


def claim_allowed(claim, scope) -> bool:
    return (claim.support_status == "SUPPORTED" and not claim.unresolved_evidence
            and claim.claim_id not in scope["claims"] and claim.page_path not in scope["pages"]
            and not any(ref.source_id in scope["sources"] or ref.passage_id in scope["passages"]
                        for ref in claim.evidence))


def source_allowed(passage, scope) -> bool:
    return passage.source_id not in scope["sources"] and passage.passage_id not in scope["passages"]


def _source_intervals(text: str, limit: int, table_row_limit: int = 12000):
    """Split exact source text without cutting a structured table row.

    A table row carries its row key and column values together.  Cutting it at
    an arbitrary character budget destroys that relationship even when the
    parser preserved it.  Ordinary prose remains bounded by ``limit``; a
    single Markdown/HTML table row may grow to ``table_row_limit``.
    """
    start = 0
    while start < len(text):
        target = min(start + limit, len(text))
        if target == len(text):
            yield start, target, "text"
            break
        line_start = text.rfind("\n", 0, target) + 1
        line_end = text.find("\n", target)
        line_end = len(text) if line_end < 0 else line_end + 1
        line = text[line_start:line_end].lstrip()
        structured = line.startswith("|") or line.startswith("<tr")
        if structured and line_end - line_start <= table_row_limit:
            end = line_end
            kind = "table_row"
        else:
            end = text.rfind("\n", start + max(1, limit // 2), target)
            end = end + 1 if end >= 0 else target
            kind = "text"
        if end <= start:
            end = target
        yield start, end, kind
        start = end


def _coalesced_intervals(text: str, limit: int, *, legacy: bool) -> list[tuple[int, int, str]]:
    """Split ``text`` and coalesce blank units for one rule version."""
    intervals = _legacy_source_intervals(text, limit) if legacy else _source_intervals(text, limit)
    raw_intervals = list(intervals)
    if legacy:
        # Preserve byte-for-byte reconstruction of previously published
        # query snapshots, including their historical omission of blank units.
        return [interval for interval in raw_intervals
                if text[interval[0]:interval[1]].strip()]
    coalesced = []
    pending_start = None
    for start, end, unit_kind in raw_intervals:
        if not text[start:end].strip():
            if coalesced:
                old_start, _, old_kind = coalesced[-1]
                coalesced[-1] = (old_start, end, old_kind)
            elif pending_start is None:
                pending_start = start
            continue
        if pending_start is not None:
            start, pending_start = pending_start, None
        coalesced.append((start, end, unit_kind))
    return coalesced


def _source_unit(passage, start: int, end: int, unit_kind: str,
                 *, include_unit_kind: bool):
    """Build one source record/location with the existing identity algorithm."""
    pid = passage.passage_id
    excerpt = passage.text[start:end]
    rid = "source_span_" + digest([pid, passage.sha256, start, end, excerpt])[:32]
    path = "source-evidence/" + rid.replace("_", "-") + ".md"
    record = dict(claim_id=rid, passage_id=pid, source_id=passage.source_id,
                  document_id=passage.document_id, chunk_id=passage.chunk_id, excerpt=excerpt)
    location = dict(kind="source", record_id=rid, source_sha256=passage.sha256,
                    passage_text_sha256=hashlib.sha256(passage.text.encode()).hexdigest(),
                    start_char=start, end_char=end,
                    metadata=passage.source_metadata)
    if include_unit_kind:
        location["unit_kind"] = unit_kind
    return path, record, location


def source_units(state, book=None, limit: int = 1000, unit_rule_version: str | None = None):
    """Yield exact intervals while preserving bounded table-row semantics.

    The new default (``keep-and-search-v1``) archives every decodable interval
    regardless of ``compile_table_values`` or semantic Quarantine.  The legacy
    evidence-disposition rules remain byte-for-byte compatible for cold-open.
    """
    if is_old_rule(unit_rule_version):
        yield from _legacy_source_units(state, book, limit, unit_rule_version)
        return
    for pid, passage in sorted(state.source_archive.items()):
        for start, end, unit_kind in _coalesced_intervals(passage.text, limit, legacy=False):
            path, record, location = _source_unit(
                passage, start, end, unit_kind, include_unit_kind=True)
            yield path, passage, record, location


def _legacy_source_units(state, book, limit: int, unit_rule_version: str | None):
    """Original evidence-disposition publication rules, preserved for old assets."""
    scope = quarantine_scope(book)
    legacy = unit_rule_version == LEGACY_RULE_VERSION
    for pid, passage in sorted(state.source_archive.items()):
        # Skipping generated table claims does not preclude a separately
        # admitted, provenance-backed raw article interval. Legacy passages
        # retain their original publication disposition.
        if (passage.source_metadata.get("compile_table_values", True) is False
                and passage.source_metadata.get("source_readable") is not True):
            continue
        if not source_allowed(passage, scope):
            continue
        for start, end, unit_kind in _coalesced_intervals(passage.text, limit, legacy=legacy):
            path, record, location = _source_unit(
                passage, start, end, unit_kind, include_unit_kind=not legacy)
            yield path, passage, record, location


def _legacy_source_intervals(text: str, limit: int):
    start = 0
    while start < len(text):
        end = min(start + limit, len(text))
        if end < len(text):
            boundary = text.rfind("\n", start + limit // 2, end)
            if boundary >= 0:
                end = boundary + 1
        yield start, end, "text"
        start = end


def disposition_ledger(state, claims, book=None, rule_version: str | None = None):
    scope = quarantine_scope(book)
    rows = []
    by_passage = {}
    for claim in claims:
        for pid in {r.passage_id for r in claim.evidence + claim.unresolved_evidence}:
            by_passage.setdefault(pid, []).append(claim)
    processed = set(state.processed_passage_ids)
    for pid, passage in sorted(state.source_archive.items()):
        linked = by_passage.get(pid, [])
        allowed = [c.claim_id for c in linked if claim_allowed(c, scope)]
        rows.append(dict(passage_id=pid, source_id=passage.source_id,
                         source_sha256=passage.sha256, processed=pid in processed,
                         claim_ids=[c.claim_id for c in linked], query_claim_ids=allowed,
                         raw_readable=source_allowed(passage, scope),
                         disposition="quarantined" if not source_allowed(passage, scope) else
                         "claims_and_source" if allowed else "source_only",
                         semantic_coverage="not_proven",
                         claim_compilation_disposition=(
                             state.claim_compilation_dispositions.get(pid)
                         ),
                         failure=state.passage_failures.get(pid)))
    return {"rule_version": rule_version or RULE_VERSION, "passages": rows}


def reconcile_claim_issues(state, claims, book):
    """Update owned findings only. Closing requires revalidation or withdrawal.

    Caller must pass claims validated against state. External/manual isolation
    is never closed by this automatic reconciliation.
    """
    result = book.model_copy(deep=True)
    current = {}
    for claim in claims:
        if claim.support_status == "SUPPORTED" and not claim.unresolved_evidence:
            continue
        issue_id = "error_" + digest([RULE_VERSION, claim.claim_id])[:24]
        current[issue_id] = ErrorBookEntry(
            id=issue_id, category="unsupported_fact", code="CLAIM_NOT_ADMITTED",
            phenomenon=claim.support_reason, error_class="Quarantine",
            root_cause=claim.support_reason, constraint="Do not publish this generated claim as supported.",
            verification="Revalidate the claim against its exact current source intervals.",
            affected_paths=[claim.page_path], affected_claim_ids=[claim.claim_id],
            rule_version=RULE_VERSION, first_seen_batch=max(1, state.batch_number), last_seen_batch=max(1, state.batch_number),
            source_versions={r.source_id: state.source_archive[r.passage_id].sha256
                             for r in claim.evidence if r.passage_id in state.source_archive})
    admitted_pids = {r.passage_id for c in claims if c.support_status == "SUPPORTED" for r in c.evidence}
    processed = set(state.processed_passage_ids)
    for pid, passage in state.source_archive.items():
        if pid in admitted_pids:
            continue
        issue_id = "error_" + digest([RULE_VERSION, "source-disposition", pid])[:24]
        table_reference = (passage.source_metadata.get("block_type") == "table_reference"
                           and passage.source_metadata.get("compile_table_values") is False
                           and pid not in state.passage_failures)
        code = ("TABLE_REFERENCE_SOURCE_ONLY" if table_reference else
                "COMPILE_PASSAGE_FAILED" if pid not in processed else "NO_ADMITTED_CLAIM")
        root_cause = ("Table reference intentionally retained as source material without generated value claims"
                      if table_reference else "No generated claim admitted for this passage")
        verification = ("Read the registered full logical table and verify its structure and source."
                        if table_reference else "Recompile or review this passage and revalidate resulting claims.")
        current[issue_id] = ErrorBookEntry(id=issue_id, category="incomplete_page", code=code,
            phenomenon=code, error_class="Advisory", root_cause=root_cause,
            constraint="Preserve exact source evidence; do not assert semantic coverage.",
            verification=verification,
            affected_passage_ids=[pid], affected_source_ids=[passage.source_id], rule_version=RULE_VERSION,
            source_versions={passage.source_id: passage.sha256},
            first_seen_batch=max(1, state.batch_number), last_seen_batch=max(1, state.batch_number))
    for pid, retry in state.recovered_retries.items():
        if pid not in state.source_archive:
            continue
        passage = state.source_archive[pid]
        issue_id = "error_" + digest([RULE_VERSION, "recovered-retry", pid, passage.sha256])[:24]
        current[issue_id] = ErrorBookEntry(id=issue_id, category="malformed_reference", code="COMPILER_RETRY_RECOVERED",
            phenomenon="A failed compiler attempt passed bounded retry", error_class="Transient",
            root_cause=str(retry["reason"])[:1500], constraint="Retry locally within the configured budget.",
            verification="The retried proposal passed the same acceptance gates.", affected_passage_ids=[pid],
            source_versions={passage.source_id: passage.sha256}, rule_version=RULE_VERSION, status="closed",
            resolution="retry_passed", first_seen_batch=max(1, state.batch_number), last_seen_batch=max(1, state.batch_number))
    claims_by_id = {c.claim_id: c for c in claims}
    for entry in result.entries:
        if entry.rule_version not in {"compiler-discovery-v1", "compiler-semantic-v1"} or not entry.affected_claim_ids:
            continue
        live = [claims_by_id[cid] for cid in entry.affected_claim_ids if cid in claims_by_id]
        if not live or (entry.rule_version == "compiler-discovery-v1" and all(
                c.support_status == "SUPPORTED" and not c.unresolved_evidence for c in live)):
            entry.status = "closed"
            entry.resolution = "withdrawn" if not live else "deterministic_revalidation_passed"
            entry.last_seen_batch = max(1, state.batch_number)
    entries = {e.id: e for e in result.entries}
    for key, old in entries.items():
        if old.rule_version == RULE_VERSION and key not in current and old.status == "open":
            old.status = "closed"
            old.resolution = "revalidated_or_withdrawn"
            old.last_seen_batch = max(1, state.batch_number)
    for key, entry in current.items():
        if key in entries:
            entry.first_seen_batch = entries[key].first_seen_batch
        entries[key] = entry
    result.entries = sorted(entries.values(), key=lambda e: e.id)
    return result
