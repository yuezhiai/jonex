"""Layered page organization: deterministic projection + bounded window (S7.2).

The layered chain separates two questions the legacy chain asked in one model
call: *what does this source say* (``understanding.py``) and *where does it
belong in the wiki*.  This module answers the second question.

Pages are source-scoped reading projections of validated contributions. Within
one source, an explicitly supported alias can join related passages. A name or
alias appearing in different sources does not automatically merge their pages:
the claims and original passages remain searchable, and the article-writing
agent can examine the identity and context before connecting them in prose.

Scheduling (S7.2):

* understanding runs ahead of organization inside a **bounded window** --
  in-flight plus understood-but-uncommitted passages never exceed ``2*C``, so
  the corpus is never fully materialized as tasks and then merely throttled by
  a semaphore;
* organization commits in **stable input order**, at most ``C`` consecutive
  completed passages per batch, so completion order cannot change the
  published fact set;
* model calls, checkpoint I/O and retries happen *outside* the commit lock;
  the lock covers only the snapshot/version check and the short commit;
* a passage that fails understanding within its retry bound is accounted and
  skipped in place (the existing failure-rate gate still decides whether the
  compile may publish at all), so one bad source never blocks the rest.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field

from jonex_wiki.domain.engine.compile import pipeline, understanding as understanding_module
from jonex_wiki.domain.engine.compile.content_validation import (
    ClaimDraft,
    validate_claim_evidence,
)
from jonex_wiki.domain.engine.compile.identity import (
    KIND_ENTITY,
    RELATION_KINDS,
    NameForm,
    normalize_name,
)
from jonex_wiki.domain.engine.models.compiler_state import (
    MAX_PAGE_LINK_MENTIONS,
    CompilerState,
    LinkMention,
    PageIndexEntry,
    WikiPageSnapshot,
)
from jonex_wiki.domain.engine.models.claim import SUPPORT_RULE_VERSION
from jonex_wiki.kernel.ports import CancelPort
from jonex_wiki.kernel.telemetry import count_work, telemetry_stage, timed_async_stage

logger = logging.getLogger(__name__)

ORGANIZATION_RULE_VERSION = "source-scoped-organization-v2"
# Source-understanding aliases are proposals; this rule is applied when they
# enter organization, including when an older understanding is reused.
ALIAS_EVIDENCE_RULE = "source-alias-evidence-v3-acronym-variants"
#: Page namespace for material projected from a source's own understanding.
PAGE_PREFIX = "pages/"


def _alias_name_pattern(value: str) -> str:
    """Match the name itself, not a prefix of another Latin name."""
    text = value.strip()
    escaped = re.sub(r"\\\s+", r"\\s+", re.escape(text))
    left = r"(?<![A-Za-z0-9_])" if text and text[0].isascii() and text[0].isalnum() else ""
    right = r"(?![A-Za-z0-9_])" if text and text[-1].isascii() and text[-1].isalnum() else ""
    return left + escaped + right


def _in_order(needle: str, text: str) -> bool:
    characters = iter(text.casefold())
    return all(character in characters for character in needle.casefold())


def _alias_evidence(text: str, subject: str, alias: str) -> str:
    """Return an explicit source wording, or leave the proposed alias open.

    Supported forms are direct naming statements, quoted parenthetical names
    and conventional initialisms. Other forms remain candidates for an agent.
    """
    subject, alias = subject.strip(), alias.strip()
    if not text or not subject or not alias or normalize_name(subject) == normalize_name(alias):
        return ""
    marker = (r"(?:以下简称|下称|简称(?:为)?|又称(?:为)?|亦称(?:为)?|"
              r"也称(?:为)?|别名(?:为)?|also\s+known\s+as|better\s+known\s+as|"
              r"known\s+officially\s+as|known\s+also\s+by\s+the\s+acronym|also\s+called|"
              r"also\s+referred\s+to\s+as|known\s+as|short\s+for|stands\s+for|"
              r"abbreviated\s+(?:as|to)|hereinafter\s+(?:referred\s+to\s+as|called))")
    quotes = r"(?:\\?[\"'“”‘’])*"
    for name, other in ((subject, alias), (alias, subject)):
        # A topic may already include its parenthesized abbreviation.
        name = re.sub(r"\s*[（(]\s*" + re.escape(other) + r"\s*[）)]$", "", name,
                      flags=re.IGNORECASE).strip()
        if not name:
            continue
        a, b = _alias_name_pattern(name), _alias_name_pattern(other)
        patterns = [
            a + r"\s*[（(]\s*" + marker + r"\s*[:：]?\s*" + quotes + b + quotes + r"\s*[）)]",
            a + r"\s*[（(]\s*(?:\\?\"" + b + r"\\?\"|“" + b + r"”|‘" + b + r"’)\s*[）)]",
            a + r"(?:\s*[（(][^()（）\n]{0,100}[）)])?\s*[,，]?\s*(?:(?:is|was)\s+)?" + marker
            + r"\s*[:：]?\s*" + quotes + b + quotes
            + r"(?=\s*(?:[,，。.;；：:!！?？)）]|$))",
        ]
        words = [word for word in re.findall(r"[A-Za-z][A-Za-z0-9]*", name)
                 if word.casefold() not in {"a", "an", "and", "for", "of", "the"}]
        initials = "".join(word[0] for word in words)
        if (len(words) >= 2 and 2 <= len(other) <= 10 and other.isascii() and other.isalpha()
                and (_in_order(other, initials)
                     or (other[0].casefold() == initials[0].casefold()
                         and _in_order(initials, other) and _in_order(other, "".join(words))))):
            patterns.append(a + r"\s*[（(]\s*" + quotes + b + quotes + r"\s*[）)]")
            patterns.append(b + r"\s*[（(]\s*" + quotes + a + quotes + r"\s*[）)]")
        for pattern in patterns:
            for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                start = max((text.rfind(mark, 0, match.start())
                             for mark in ".。!?！？;；\n"), default=-1) + 1
                ends = [text.find(mark, match.end()) for mark in ".。!?！？;；\n"]
                end = min((position for position in ends if position >= 0), default=len(text))
                clause = text[start:end]
                if end < len(text) and text[end] in "?？":
                    continue
                if not re.search(r"\b(?:falsely|incorrectly|mistakenly|misidentified|false|incorrect|disputed|hypothetically)\b"
                                 r"|\bnot\s+(?:an?\s+alias|the\s+same|correct|true)\b"
                                 r"|误称|误认|并非|不是同一|不等同|不正确|错误|假设", clause, re.IGNORECASE):
                    return match.group(0)
    return ""


def _review_aliases(record: Any, *, source_text: str, passage_id: str) -> Any:
    """Keep unverified model aliases as leads, including old cache entries."""
    qualifiers = []
    changed = False
    for item in getattr(record, "qualifiers", ()) or ():
        if not isinstance(item, Mapping) or item.get("kind") not in {"declared_alias", "alias_candidate"}:
            qualifiers.append(item)
            continue
        subject, alias = str(item.get("subject") or ""), str(item.get("value") or "")
        changed = True
        evidence = _alias_evidence(source_text, subject, alias)
        # Recompute rather than trust a stored flag or a prior model claim.
        reviewed = {"kind": "declared_alias" if evidence else "alias_candidate",
                    "subject": subject, "value": alias}
        if evidence:
            reviewed.update({"source_evidence": evidence, "evidence_passage_id": passage_id,
                             "alias_evidence_rule": ALIAS_EVIDENCE_RULE})
        qualifiers.append(reviewed)
    return record.model_copy(update={"qualifiers": qualifiers}) if changed else record


def name_form(record: Any, name: str) -> NameForm:
    """The record's own identity vocabulary, in the S6 ``NameForm`` shape.

    Aliases count only after the current organization rule has checked their
    source wording; an unchecked name is not identity evidence.
    """
    wanted = normalize_name(name)
    aliases = []
    # Co-occurring names never assert identity. Old and new model suggestions
    # must first be reviewed against the current source passage.
    for item in getattr(record, "qualifiers", ()) or ():
        if (not isinstance(item, Mapping) or item.get("kind") != "declared_alias"
                or item.get("alias_evidence_rule") != ALIAS_EVIDENCE_RULE):
            continue
        subject, alias = str(item.get("subject") or ""), str(item.get("value") or "")
        if subject and alias and wanted in {normalize_name(subject), normalize_name(alias)}:
            for value in (subject, alias):
                if normalize_name(value) != wanted and value not in aliases:
                    aliases.append(value)
    relations: Dict[str, List[str]] = {}
    for proposal in getattr(record, "link_proposals", ()) or ():
        kind = str(proposal.get("relation") or "").strip()
        target = str(proposal.get("target_mention") or "").strip()
        if kind in RELATION_KINDS and target:
            relations.setdefault(kind, []).append(target)
    context = " | ".join(sorted({
        "{}={}".format(str(item.get("kind") or ""), str(item.get("value") or ""))
        for item in (getattr(record, "qualifiers", ()) or ())
        if isinstance(item, Mapping) and item.get("kind") not in {"declared_alias", "alias_candidate"}
        and (item.get("value") or item.get("kind"))
    }))
    return NameForm(name=name, kind=KIND_ENTITY, context=context,
                    aliases=aliases, relations=relations)


class LayeredCompilation(BaseModel):
    """What the layered stage produced, plus the numbers S7.3 must report."""

    model_config = ConfigDict(extra="forbid")

    state: Any
    claims: List[Any] = Field(default_factory=list)
    #: The canonical contribution records this run organized, in stable page
    #: order, each carrying the displays (page_path, claim_id) it was projected
    #: as.  The publication tail takes *these* as the ledger: the claims are a
    #: view of them, never a second extraction from which the ledger is rebuilt.
    contributions: List[Any] = Field(default_factory=list)
    understanding: Any = None
    passages_organized: int = 0
    pages_written: int = 0
    #: Pages whose previous publication snapshot was carried unchanged: their
    #: sources did not move and their record set is exactly the one the
    #: previous Bundle projected, so nothing was re-validated or re-rendered.
    pages_reused: int = 0
    organization_batches: int = 0
    organization_model_calls: int = 0
    #: Compatibility counters retained for existing callers. The source-scoped
    #: organizer no longer performs model identity judgments, so these are zero.
    identity_judgments: int = 0
    identity_judgment_cases: int = 0
    identity_judgment_applied: int = 0
    identity_judgment_invalid: int = 0
    identity_judgment_limited: int = 0
    identity_unresolved: int = 0
    peak_uncommitted: int = 0
    backpressure_waits: int = 0
    #: Milliseconds inside the commit lock (snapshot/version check + commit).
    commit_lock_ms: int = 0
    #: Sum of the understanding stage's per-passage durations, queueing
    #: included (copied from the stage, counted once by the engine).  It is a
    #: sum over passages rather than a wall-clock interval, so concurrent
    #: passages can make it exceed the compile's wall time; queue, model, store
    #: and stage-lock parts are reported separately and are *inside* it.
    #:
    #: This stage never reuses a previous publication: every layered compile
    #: rebuilds the page set from the current sources, so a deleted source
    #: cannot survive as a stale snapshot.  That choice is counted by the
    #: engine (``layered_incremental_not_used``), not stored as a field here.
    understanding_ms: int = 0


@dataclass
class _Pending:
    """One understood passage waiting for its batch to be committed."""

    passage_id: str
    checkpoint: Any


def _resolve_task(*, index: int, task: Any, order: Sequence[str],
                  stage: understanding_module.UnderstandingStage) -> Optional[_Pending]:
    """Turn one finished understanding task into a committable item.

    Only a passage whose bounded protocol retries were exhausted *and* that the
    stage recorded in ``failures`` may be skipped.  Publishing a wiki that is
    silently missing a contribution the failure gate never saw is exactly the
    dishonesty the layered contract forbids, so a cancelled task, a checkpoint
    or storage error, a permission error and every other unexpected exception
    abort the compile instead of degrading into that same ``None``.

    ``SourceUnderstandingStoreUnsupported`` is included in that rule: a store
    that cannot checkpoint cannot honour the layered contract at all, and the
    checkpoints already written stay in place for the next run.
    """
    passage_id = order[index]
    if task.cancelled():
        # A cancelled understanding is a cancellation, not a passage failure.
        raise asyncio.CancelledError()
    error = task.exception()
    if error is not None:
        raise error
    checkpoint = task.result()
    if checkpoint is None:
        if passage_id not in stage.result.failures:
            raise RuntimeError(
                "layered organizer received an unaccounted passage failure for "
                "{}; refusing to publish".format(passage_id))
        return None
    return _Pending(passage_id=passage_id, checkpoint=checkpoint)


def page_path_for(source_id: str, name: str) -> str:
    """Deterministic, collision-free path for one (source, name) pair.

    The digest covers **both** source and name: the wiki path normalizer slugs
    non-ASCII characters away, so a name-only readable part would let two
    different Chinese names from one source collide onto one page -- a silent
    merge.  The readable ASCII part is kept only as a hint; identity lives in
    the digest.  The result is normalized by the same function every page path
    goes through, so the state's key and the page's own path always agree.
    """
    from jonex_wiki.domain.engine.models.compiler_state import normalize_wiki_path

    key = normalize_name(name)
    digest = hashlib.sha256((source_id + "\x00" + key).encode("utf-8")).hexdigest()[:12]
    readable = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", key)).strip("-")[:60]
    stem = (readable + "-" if readable else "") + digest
    return normalize_wiki_path(PAGE_PREFIX + stem + ".md")


def dominant_name(record: Any) -> str:
    """The name this record is *about*, from the source's own vocabulary.

    Preference order is fixed so the projection is reproducible: the model's
    topic hint, then an explicit original name, then a deterministic entity
    mention, then the proposition itself.  A record is never dropped for
    lacking a name.
    """
    hint = str(getattr(record, "topic_hint", "") or "").strip()
    if hint:
        return hint
    # Alias suggestions also occur in original_names for discovery. Without a
    # topic hint they must not silently become the page identity themselves.
    candidates = {normalize_name(str(item.get("value") or ""))
                  for item in (getattr(record, "qualifiers", ()) or ())
                  if isinstance(item, Mapping) and item.get("kind") == "alias_candidate"}
    names = [str(value).strip() for value in (getattr(record, "original_names", {}) or {}).values()
             if str(value).strip() and normalize_name(str(value)) not in candidates]
    if names:
        return sorted(names, key=lambda value: (-len(value), value))[0]
    mentions = [str(value).strip() for value in (getattr(record, "entity_mentions", []) or [])
                if str(value).strip() and normalize_name(str(value)) not in candidates]
    if mentions:
        return sorted(mentions, key=lambda value: (-len(value), value))[0]
    proposition = " ".join(str(getattr(record, "proposition", "") or "").split())
    return proposition[:80] or "material"


def mention_limits(records: Sequence[Any], *, own: str = "") -> List[LinkMention]:
    """Keep readable optional navigation, deduplicating targets and self-links."""
    own_keys = {normalize_name(own)} if normalize_name(own) else set()
    seen: Dict[str, LinkMention] = {}
    self_links = 0
    duplicates = 0
    for record in records:
        for proposal in getattr(record, "link_proposals", ()) or ():
            target = str(proposal.get("target_mention", "")).strip()
            if not target:
                continue
            key = normalize_name(target)
            if not key or key in own_keys:
                self_links += 1
                continue
            if key in seen:
                duplicates += 1
                continue
            seen[key] = LinkMention(
                target_mention=target,
                why_visit=str(proposal.get("why_visit", "")),
                relation=str(proposal.get("relation", "")),
                evidence_refs=list(proposal.get("evidence_refs", ()) or ()),
            )
    mentions = sorted(seen.values(), key=lambda item: normalize_name(item.target_mention))
    if self_links:
        count_work("compile_organization_link_proposals_self_link", self_links)
    if duplicates:
        count_work("compile_organization_link_proposals_deduplicated", duplicates)
    return mentions


def project_page(
    path: str,
    title: str,
    records: Sequence[Any],
    claims: Sequence[Any],
    *,
    content: Optional[str] = None,
    version: int = 1,
) -> WikiPageSnapshot:
    """Build one page deterministically from its validated contributions.

    No LLM rewrites the page: the body is the ordered list of the claims the
    evidence gate already accepted, one bullet each, deduplicated by text.
    Order is the order the records arrived in (stable input order), not
    completion order and not an alphabetical reshuffle of someone's prose.

    ``content`` lets the caller supply a body it rendered incrementally (the
    page's earlier body plus the newly appended lines).  The body text is
    deterministic, so the caller's rendering is the same string this function
    would have built; passing it only avoids re-joining everything each time a
    long page is touched again.
    """
    body: List[str] = page_body_lines(claims)
    source_ids = sorted({str(span.source_id) for record in records
                         for span in getattr(record, "source_spans", ()) or ()})
    aliases = sorted({
        str(value) for record in records
        for value in name_form(record, title).aliases
        if str(value).strip() and normalize_name(str(value)) != normalize_name(title)
    }, key=normalize_name)[:8]
    entry = PageIndexEntry(
        path=path,
        title=title[:200] or "material",
        aliases=aliases,
        description=body[0][:200] if body else "",
        why_visit=body[0][:120] if body else "",
    )
    return WikiPageSnapshot(
        index=entry,
        content=content if content is not None else render_page_body(body),
        related_pages=[],
        links=[],
        link_mentions=mention_limits(records, own=title),
        source_ids=source_ids,
        version=version,
    )


def page_body_lines(claims: Sequence[Any]) -> List[str]:
    """The page's body lines: claim text, order preserved, deduplicated."""
    body: List[str] = []
    seen = set()
    for claim in claims:
        text = " ".join(str(claim.text).split())
        if text and text not in seen:
            seen.add(text)
            body.append(text)
    return body


def render_page_body(body: Sequence[str]) -> str:
    return "\n".join("- " + line for line in body)


class Organizer:
    """Deterministic assignment of understood records to pages.

    The organizer owns the *rules*; the driver owns scheduling.  Every rule is
    a pure function of the records accepted so far in stable input order, which
    is why concurrency cannot change the published fact set.
    """

    def __init__(self) -> None:
        # Retain zero-valued counters in the result contract for old consumers.
        self.identity_model_calls = 0
        self.records: Dict[str, List[Any]] = {}
        self._record_ids: Dict[str, set[str]] = {}
        self.claims: Dict[str, List[Any]] = {}
        #: page -> [(record, claim_id)] for the records that passed the
        #: evidence gate, replaced (never appended) on each materialization.
        self.bound: Dict[str, List[Tuple[Any, str]]] = {}
        self.titles: Dict[str, str] = {}
        self.unresolved = 0
        self._names: set[str] = set()
        self._source_names: Dict[Tuple[str, str], List[str]] = {}
        #: (page, record, evidence, source version, support rule) -> validated
        #: claim or None.  Validation is a pure function of the record, the
        #: page it is shown on and the archived passage text, so an unchanged
        #: fact is validated once per run however often its page is rewritten.
        self._validation: Dict[Tuple[Any, ...], Any] = {}
        #: page -> the body lines and rendered content of the last
        #: materialization.  A page that only gained facts appends to its body
        #: instead of re-joining every line it ever carried.
        self._body_lines: Dict[str, Tuple[str, ...]] = {}
        self._body_content: Dict[str, str] = {}
        self.identity_judgments = 0
        self.identity_judgment_cases = 0
        self.identity_judgment_applied = 0
        self.identity_judgment_invalid = 0
        self.identity_judgment_limited = 0

    # -- source-scoped name index -------------------------------------------

    def _register(self, path: str, name: str, record: Any, source_id: str) -> None:
        form = name_form(record, name)
        for value in [name, self.titles.get(path, ""), *form.aliases]:
            key = normalize_name(value)
            if not key:
                continue
            self._names.add(key)
            bucket = self._source_names.setdefault((str(source_id), key), [])
            if path not in bucket:
                bucket.append(path)

    def _target_for(self, record: Any, name: str, source_id: str) -> str:
        form = name_form(record, name)
        keys = {normalize_name(value) for value in [name, *form.aliases] if value}
        candidates = []
        for key in sorted(keys):
            for path in self._source_names.get((str(source_id), key), []):
                if path not in candidates:
                    candidates.append(path)
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            # Ambiguous aliases within one source are leads, not an arbitrary
            # choice between already independent pages.
            self._keep_independent(name)
            return page_path_for(str(source_id) + ":" + str(record.record_id), name)
        if any(key in self._names for key in keys):
            # A matching name in another source is a discoverable candidate,
            # not enough evidence to assign its material to that page.
            self._keep_independent(name)
        return page_path_for(str(source_id), name)

    def _keep_independent(self, name: str) -> None:
        self.unresolved += 1
        count_work("compile_organization_identity_unresolved")
        logger.info("layered organization keeps %r independent: identity undetermined", name)

    def add(self, checkpoint: Any, *, source_id: str, source_text: str = "") -> List[str]:
        """Project source facts without a cross-source identity judgment."""
        touched: List[str] = []
        for original in getattr(checkpoint, "records", ()) or ():
            text = source_text or "\n".join(str(span.text) for span in original.source_spans
                                            if str(span.source_id) == str(source_id))
            record = _review_aliases(original, source_text=text,
                                     passage_id=str(getattr(checkpoint, "passage_id", "")))
            name = dominant_name(record)
            path = self._target_for(record, name, source_id)
            if self._assign(path, name, record, source_id):
                touched.append(path)
        return touched

    def _assign(self, path: str, name: str, record: Any, source_id: str) -> bool:
        """Place one record on a page; ``False`` when it is already there."""
        bucket = self.records.setdefault(path, [])
        ids = self._record_ids.setdefault(path, set())
        if str(record.record_id) in ids:
            return False
        ids.add(str(record.record_id))
        bucket.append(record)
        self.titles.setdefault(path, name)
        self._register(path, name, record, source_id)
        return True

    def materialize(self, path: str, *, state: CompilerState) -> Tuple[WikiPageSnapshot, List[Any]]:
        """Validate and project one page's accumulated records.

        Re-validating through the *same* evidence gate the legacy chain uses is
        what makes the projection trustworthy: a claim whose excerpt is not in
        the archive cannot reach a page, whatever the source model said.  The
        gate runs against the compile's own archive, so a page whose records
        come from several passages validates each of them.
        """
        records = self.records.get(path, [])
        claims: List[Any] = []
        kept: List[Any] = []
        bound: List[Tuple[Any, str]] = []
        for record in records:
            claim = self._validated_claim(path, record, state)
            if claim is None:
                continue
            kept.append(record)
            claims.append(claim)
            bound.append((record, str(claim.claim_id)))
        self.claims[path] = claims
        self.bound[path] = bound
        body = page_body_lines(claims)
        previous = state.pages.get(path)
        version = (previous.version + 1) if previous is not None else 1
        return project_page(path, self.titles.get(path, "material"), kept, claims,
                            content=self._page_content(path, body),
                            version=version), claims

    def _validated_claim(self, path: str, record: Any, state: CompilerState) -> Any:
        """Validate one record against the archive, reusing a proven result.

        The key covers everything the evidence gate reads: the page it is
        shown on, the fact itself (identity plus the cited excerpts), the
        version of every archived passage it cites, and the support rule that
        decides what "supported" means.  A re-read source or an upgraded rule
        therefore re-validates; a page rewritten for its *other* facts does not.
        """
        key = _validation_key(path, record, state)
        if key in self._validation:
            count_work("compile_organization_validation_reused")
            return self._validation[key]
        draft = ClaimDraft(page_path=path, text=record.proposition,
                           evidence=list(record.evidence))
        validated = validate_claim_evidence(
            _gate_state(state, path), [draft]).validated_claims
        claim = (validated[0].model_copy(update={"page_path": path})
                 if validated else None)
        if claim is None:
            count_work("compile_organization_claims_dropped")
        else:
            count_work("compile_organization_validation_calls")
        self._validation[key] = claim
        return claim

    def _page_content(self, path: str, body: List[str]) -> str:
        """The page body, appended to when the page only gained facts."""
        lines = tuple(body)
        previous_lines = self._body_lines.get(path, ())
        previous_content = self._body_content.get(path, "")
        if lines == previous_lines and previous_lines:
            count_work("compile_organization_body_reused")
            return previous_content
        if (previous_lines and len(lines) > len(previous_lines)
                and lines[:len(previous_lines)] == previous_lines):
            appended = render_page_body(lines[len(previous_lines):])
            separator = "\n" if previous_content else ""
            content = previous_content + separator + appended
            count_work("compile_organization_body_appended_lines",
                       len(lines) - len(previous_lines))
            # The separator is generated too: the counter is the real number of
            # bytes this commit added to the page, not just the new lines.
            count_work("compile_organization_body_bytes",
                       len((separator + appended).encode("utf-8")))
        else:
            content = render_page_body(lines)
            count_work("compile_organization_body_bytes", len(content.encode("utf-8")))
            count_work("compile_organization_body_rendered", 1)
        self._body_lines[path] = lines
        self._body_content[path] = content
        return content

    def carry_page(self, path: str, snapshot: Any, claims: Sequence[Any]) -> bool:
        """Adopt a previous publication's page without re-projecting it.

        The page's records are exactly the records the previous Bundle
        projected (the caller proved that), so the carried claims *are* this
        page's claims: pairing them back to the records keeps the contribution
        ledger's display bindings honest -- a reused page still shows the
        facts it shows.  ``False`` means pairing failed and the caller must
        materialize the page instead of reusing it.
        """
        records = self.records.get(path, [])
        remaining = list(claims)
        bound: List[Tuple[Any, str]] = []
        for record in records:
            proposition = str(getattr(record, "proposition", ""))
            match = None
            for index, claim in enumerate(remaining):
                if (str(getattr(claim, "page_path", "")) == path
                        and str(getattr(claim, "text", "")) == proposition):
                    match = index
                    break
            if match is None:
                return False
            bound.append((record, str(remaining.pop(match).claim_id)))
        if remaining:
            # A carried claim this page's records do not explain: reusing the
            # snapshot would publish a fact the current material no longer
            # supports.
            return False
        self.claims[path] = list(claims)
        self.bound[path] = bound
        return True

    def contributions(self) -> List[Any]:
        """The curated records of this run, with their display bindings.

        One record per source fact: two pages showing the same fact are two
        displays of one record, exactly as the contribution rule requires.  A
        record whose claim the evidence gate dropped is absent here, so the
        ledger can never claim a page shows something it does not.
        """
        from jonex_wiki.domain.engine.compile.source_contribution import (
            ContributionDisplay,
        )

        records: Dict[str, Any] = {}
        for path in self.records:
            for record, claim_id in self.bound.get(path, ()):
                display = ContributionDisplay(page_path=path, claim_id=claim_id)
                existing = records.get(str(record.record_id))
                if existing is None:
                    # Displays and link proposals are *page* bindings, so the
                    # page-free record the understanding stage produced is
                    # copied with both emptied and rebuilt per displayed page.
                    existing = record.model_copy(update={"displays": [],
                                                         "link_proposals": []})
                    records[str(record.record_id)] = existing
                if display not in existing.displays:
                    existing.displays.append(display)
                for proposal in getattr(record, "link_proposals", ()) or ():
                    value = {**proposal,
                             "page_path": str(proposal.get("page_path") or path)}
                    if value not in existing.link_proposals:
                        existing.link_proposals.append(value)
        for record in records.values():
            record.displays.sort(key=lambda item: (item.page_path, item.claim_id))
        return [records[key] for key in sorted(records)]


def _validation_key(path: str, record: Any, state: CompilerState) -> Tuple[Any, ...]:
    """Everything the evidence gate's verdict for one record depends on."""
    cited = []
    for ref in record.evidence or ():
        passage_id = str(getattr(ref, "passage_id", ""))
        archived = state.source_archive.get(passage_id)
        cited.append((passage_id, str(getattr(ref, "excerpt", "")),
                      str(getattr(archived, "sha256", "")) if archived else ""))
    return (path, str(record.record_id), tuple(cited), SUPPORT_RULE_VERSION)


def _gate_state(state: CompilerState, path: str) -> CompilerState:
    """The evidence gate's minimum: this page declared, the archive present."""
    # The pure gate reads only page membership and the validated archive.
    # Construct a local view without copying/revalidating the entire corpus
    # for every claim. The caller's state and all evidence stay untouched.
    page = state.pages.get(path) or WikiPageSnapshot(
        index=PageIndexEntry(path=path, title="draft"))
    return CompilerState.model_construct(
        batch_number=0,
        pages={path: page},
        index={},
        source_archive=state.source_archive,
        processed_passage_ids=[],
        active_constraints=[],
    )


def apply_batch(state: CompilerState, organizer: Organizer,
                paths: Sequence[str]) -> Tuple[CompilerState, List[Any]]:
    """Rebuild the touched pages and hand back their claims (direct callers)."""
    return commit_prepared(state, organizer,
                           {path: organizer.materialize(path, state=state)
                            for path in paths})


def commit_prepared(state: CompilerState, organizer: Organizer,
                    prepared: Mapping[str, Tuple[WikiPageSnapshot, List[Any]]],
                    carried: Optional[Mapping[str, WikiPageSnapshot]] = None,
                    ) -> Tuple[CompilerState, List[Any]]:
    """Short commit: version-check and write snapshots validated outside.

    The lock's whole job is "the pages this projection was based on are still
    the pages in the state" plus a fresh state object; validation and
    projection already happened.  The check is what makes that split safe if a
    second writer is ever introduced.

    ``carried`` are pages the increment reused: their snapshot comes from the
    previous publication and is *not* rewritten, so it has no version to
    re-check (the store already holds these bytes) and no claim list to append
    (the caller registered the carried claims on the organizer).
    """
    pages = dict(state.pages)
    index = dict(state.index)
    claims: List[Any] = []
    for path, snapshot in (carried or {}).items():
        pages[path] = snapshot
        index[path] = snapshot.index
    for path, (snapshot, page_claims) in prepared.items():
        previous = pages.get(path)
        expected = (previous.version + 1) if previous is not None else 1
        if snapshot.version != expected:
            raise RuntimeError(
                "layered organizer page {} changed while it was being "
                "materialized ({} -> {})".format(path, snapshot.version,
                                                 expected))
        pages[path] = snapshot
        index[path] = snapshot.index
        claims.extend(page_claims)
    return state.model_copy(update={"pages": pages, "index": index}), claims


def _page_is_reusable(path: str, organizer: Organizer, *, reusable: Any,
                      previous_records: Any, previous_claims: Any) -> bool:
    """Is this page exactly what the previous publication already holds?

    Three proofs are required and each is cheap: the caller cleared every
    source of the page (``reusable``), the page's record set is the previous
    record set (same facts, no additions and no withdrawals), and the previous
    publication has claims for it to carry.  Anything less is rebuilt.
    """
    if path not in reusable or path not in previous_records or path not in previous_claims:
        return False
    current = {str(getattr(record, "record_id", ""))
               for record in organizer.records.get(path, ())}
    return bool(current) and current == set(previous_records[path])


@timed_async_stage('organization_driver')
async def organize_sources(
    state: CompilerState,
    *,
    stage: understanding_module.UnderstandingStage,
    concurrency: int = pipeline.DEFAULT_COMPILE_CONCURRENCY,
    max_passage_failure_rate: float = pipeline.DEFAULT_MAX_PASSAGE_FAILURE_RATE,
    claim_policy: object = "production",
    telemetry: Any = None,
    cancel: Optional[CancelPort] = None,
    reusable_pages: Any = None,
    previous_pages: Any = None,
    previous_records: Any = None,
    previous_claims: Any = None,
) -> LayeredCompilation:
    """Understand and organize the corpus inside one bounded window.

    ``2*C`` bounds uncommitted passages; ``C`` bounds simultaneous model calls
    (inside the stage) and the size of one organization batch.

    ``reusable_pages`` (S7-B) is the set of page paths the caller proved safe
    to carry: every source that produced them is still live with an unchanged
    version.  A page in that set whose record set is *exactly* the one the
    previous publication projected is adopted from ``previous_pages`` /
    ``previous_claims`` instead of being validated and rendered again; anything
    else -- a changed record set, a failed claim pairing -- is rebuilt from the
    current material, so the increment never unions a stale snapshot.
    """
    if cancel:
        cancel.raise_if_cancelled()
    order = list(stage.order)
    reusable = set(reusable_pages or ())
    previous_pages = dict(previous_pages or {})
    previous_records = dict(previous_records or {})
    previous_claims = dict(previous_claims or {})
    concurrency = max(1, int(concurrency))
    window = 2 * concurrency
    organizer = Organizer()
    result = LayeredCompilation(state=state, understanding=stage.result)
    commit_lock = asyncio.Lock()
    tasks: Dict[int, "asyncio.Task[Any]"] = {}
    results: Dict[int, Optional[_Pending]] = {}
    organized: List[str] = []
    #: Claims per page, replaced (never appended) on each commit: a page that
    #: is touched twice must contribute its claim set once, or the query
    #: validator sees the same claim id twice.
    claims_by_page: Dict[str, List[Any]] = {}
    #: The *final* disposition of every page the run touched: True when the
    #: page's last write was a carried previous-publication snapshot, False
    #: when it was materialized.  A page carried in an early batch and extended
    #: by a later one ends up rebuilt -- counting carries would report a page
    #: as reused that the run in fact rewrote.
    carried_pages: Dict[str, bool] = {}
    next_submit = 0
    next_commit = 0
    peak = 0
    # Seconds, not milliseconds: a batch commit is normally far below 1 ms and
    # integer milliseconds would report the whole run as lock-free.
    lock_seconds = 0.0

    def _source_of(pid: str) -> str:
        return str(stage.state.source_archive[pid].source_id)

    try:
        while next_commit < len(order):
            if cancel:
                cancel.raise_if_cancelled()
            # 1. fill the bounded window.  Understood-but-uncommitted work
            #    counts toward the bound, exactly as the plan requires: the
            #    corpus is never fully materialized and then throttled.
            while next_submit < len(order) and (next_submit - next_commit) < window:
                tasks[next_submit] = asyncio.create_task(
                    stage.understand_one(order[next_submit]))
                next_submit += 1
            peak = max(peak, next_submit - next_commit)

            # 2. organize the longest run of finished passages at the commit
            #    frontier, at most one batch of C consecutive items.
            group: List[_Pending] = []
            cursor = next_commit
            while cursor < len(order) and len(group) < concurrency and cursor in results:
                pending = results[cursor]
                if pending is not None:
                    group.append(pending)
                cursor += 1
            if cursor > next_commit:
                touched: List[str] = []
                # Assign the whole batch first, then materialize each touched
                # page exactly once: validating inside the loop would validate
                # the pages an earlier passage in the same batch already
                # touched, and re-appending their claims.
                for pending in group:
                    for path in organizer.add(
                            pending.checkpoint,
                            source_id=_source_of(pending.passage_id),
                            source_text=stage.state.source_archive[pending.passage_id].text):
                        if path not in touched:
                            touched.append(path)
                    organized.append(pending.passage_id)
                    result.passages_organized += 1
                # The expensive half -- evidence validation and page projection
                # -- happens here, outside the lock.  The lock below only
                # re-checks the page versions the projection was based on and
                # writes the snapshots; it never calls the gate.
                prepared: Dict[str, Any] = {}
                carried: Dict[str, Any] = {}
                for path in touched:
                    if _page_is_reusable(path, organizer, reusable=reusable,
                                         previous_records=previous_records,
                                         previous_claims=previous_claims):
                        snapshot = previous_pages[path]
                        if organizer.carry_page(path, snapshot,
                                                previous_claims[path]):
                            carried[path] = snapshot
                            continue
                        count_work("compile_organization_page_reuse_refused")
                    prepared[path] = organizer.materialize(path, state=result.state)
                started = time.monotonic()
                async with commit_lock:
                    result.state, _page_claims = commit_prepared(
                        result.state, organizer, prepared, carried=carried)
                    for path in touched:
                        claims_by_page[path] = list(organizer.claims.get(path, ()))
                lock_seconds += time.monotonic() - started
                for index in range(next_commit, cursor):
                    results.pop(index, None)
                    tasks.pop(index, None)
                result.pages_written = len(organizer.records)
                for path in touched:
                    carried_pages[path] = path in carried
                if carried:
                    count_work("compile_organization_pages_carried", len(carried))
                if group:
                    result.organization_batches += 1
                else:
                    # The frontier advanced over passages that failed their
                    # bounded retries.  That is progress, not an organization
                    # batch, and it is counted under its own name.
                    count_work("compile_organization_skipped_passages", cursor - next_commit)
                if touched:
                    count_work("compile_organization_pages", len(set(touched)))
                next_commit = cursor
                continue

            # 3. Nothing ready at the frontier: wait for the head passage.  A
            #    head failure is recorded by the stage (its own retry bound
            #    already ran) and skipped in place on the next iteration.
            head = tasks.get(next_commit)
            if head is None:
                raise RuntimeError("layered organizer lost the head passage task")
            result.backpressure_waits += 1
            count_work("compile_organization_backpressure_wait")
            await asyncio.wait({head}, return_when=asyncio.FIRST_COMPLETED)
            # Harvest *every* passage that has finished, not just the head.
            # The batch formed on the next iteration is the contiguous run of
            # finished passages at the frontier, up to C; resolving only the
            # head would commit one passage per batch however far ahead the
            # rest of the window already is.
            for index in sorted(index for index, task in tasks.items()
                                if index not in results and task.done()):
                results[index] = _resolve_task(index=index, task=tasks[index],
                                               order=order, stage=stage)
    finally:
        for task in tasks.values():
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks.values(), return_exceptions=True)

    result.state = result.state.model_copy(update={
        "processed_passage_ids": [pid for pid in order if pid in set(organized)],
        "passage_failures": dict(stage.result.failures),
    })
    # Pages whose *last* write was a carried snapshot: the run reused them
    # whole, and it is that number a report must show (a page carried early and
    # extended later was rewritten and counts as rebuilt).
    result.pages_reused = sum(1 for reused in carried_pages.values() if reused)
    if result.pages_reused:
        count_work("compile_organization_pages_reused", result.pages_reused)
    # Page creation order, which is stable input order, not completion order.
    result.claims = [claim for path in organizer.records
                     for claim in claims_by_page.get(path, ())]
    # The canonical ledger of this compile: the very records the pages were
    # projected from, with the display bindings the projection produced.
    result.contributions = organizer.contributions()
    result.identity_unresolved = organizer.unresolved
    result.organization_model_calls = organizer.identity_model_calls
    result.identity_judgments = organizer.identity_judgments
    result.identity_judgment_cases = organizer.identity_judgment_cases
    result.identity_judgment_applied = organizer.identity_judgment_applied
    result.identity_judgment_invalid = organizer.identity_judgment_invalid
    result.identity_judgment_limited = organizer.identity_judgment_limited
    result.peak_uncommitted = peak
    result.commit_lock_ms = int(lock_seconds * 1000)
    # Copied, not counted: the engine reports this stage's total once.  Emitting
    # it here as well made ``layered_understanding_ms`` the sum of two copies of
    # the same interval, which is exactly the double counting a report cannot do.
    result.understanding_ms = int(getattr(stage.result, "understanding_ms", 0) or 0)
    count_work("compile_organization_batches", result.organization_batches)
    # Local measurement only: the driver is single-writer, so this is the time
    # the commit lock was actually held (version check plus state write), not a
    # throughput figure for a contended lock.
    count_work("compile_organization_commit_lock_ms", result.commit_lock_ms)
    count_work("compile_organization_organized_passages", result.passages_organized)
    count_work("compile_organization_model_calls", result.organization_model_calls)
    if result.peak_uncommitted:
        count_work("compile_organization_peak_uncommitted", result.peak_uncommitted)
    pipeline.check_passage_failure_rate(
        result.state,
        order,
        dict(stage.result.failures),
        max_passage_failure_rate=max_passage_failure_rate,
        claim_policy=claim_policy)
    return result
