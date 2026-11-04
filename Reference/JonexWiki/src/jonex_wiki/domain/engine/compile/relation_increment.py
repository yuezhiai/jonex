"""Bounded relation and link increments (S6.2).

A model may propose a *mention*, a reason to visit and the evidence it saw; it
may not name an identity.  The coordination layer here decides the canonical
target, whether a relation is supported, and what stays pending.

The rules that matter:

* navigation comes first and is the default; ``supported_relation`` requires
  independent relation evidence that really is in the source material - filling
  in a relation label is not evidence, and legacy ``related`` content is never
  upgraded by it;
* a predecessor is a relation, never an alias;
* a target that does not exist yet is kept as a *pending* reference, resolved at
  the end of the batch, and never rendered as a clickable page that is not
  there;
* ``links`` are the records, ``related`` is their compatibility projection -
  one set of facts, two views;
* concurrent increments merge by page (S5 rules), never by unioning every field.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jonex_wiki.kernel.telemetry import count_work

from jonex_wiki.domain.engine.compile.identity import (
    RELATION_KINDS,
    normalize_name,
)
from jonex_wiki.domain.engine.models.compiler_state import PageLink

RELATION_RULE_VERSION = "wiki-relation-increment-v2"
# Explicit directed predicates, not a general natural-language entailment rule.
CHINESE_RELATIONS = ("负责审批", "负责执行", "移交付款材料", "隶属于", "属于", "位于", "适用于", "管理", "制定", "替代", "前身", "继任")
ALLOWED_RELATIONS = frozenset((*RELATION_KINDS, *CHINESE_RELATIONS))


def _allowed_relation(value: str) -> bool:
    # Domain-specific Chinese predicates may keep their exact source wording.
    # Admission is still the bounded directed-clause proof below, never the
    # presence of a label or shared entities alone.
    if re.match(r"(?:不|未|非|无|应当|应该|须|需|可|能|将|拟|欲|禁止|严禁|计划)", value):
        return False
    return value in ALLOWED_RELATIONS or (bool(re.fullmatch(r"[\u3400-\u9fff]{2,16}", value))
                                           and value not in ("相关", "关联", "有关", "提及", "联系"))
KIND_NAVIGATION = "navigation"
KIND_SUPPORTED = "supported_relation"
RESOLVED = "resolved"
PENDING = "pending"


class LinkProposal(BaseModel):
    """What a caller (or a compile model) may propose - no identities."""

    page_path: str
    target_mention: str
    why_visit: str
    evidence_refs: List[Dict[str, Any]] = Field(default_factory=list)
    relation: str = ""
    #: The relation's subject, when the proposal came from source understanding
    #: (a compile model may propose a mention without one).
    subject_mention: str = ""
    mentioned_aliases: List[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")

    @field_validator("why_visit")
    def _why(cls, value: str) -> str:
        text = " ".join(str(value or "").split())
        return text or "source mentions this target"

    @field_validator("relation")
    def _relation(cls, value: str) -> str:
        text = " ".join(str(value or "").split()).casefold()
        return text


class LinkRecord(BaseModel):
    """One decided link: identity, kind and resolution fixed by this layer."""

    page_path: str
    target_id: str
    why_visit: str
    kind: str
    evidence_refs: List[Dict[str, Any]] = Field(default_factory=list)
    relation: str = ""
    qualifiers: List[Dict[str, str]] = Field(default_factory=list)
    resolution: str = RESOLVED
    aliases: List[str] = Field(default_factory=list)
    # Only for pending records: the mention that has no page yet.
    target_mention: str = ""

    model_config = ConfigDict(extra="forbid")

    def as_page_link(self) -> Optional[PageLink]:
        """The existing published shape - only for links that resolve."""
        if self.resolution != RESOLVED:
            return None
        return PageLink(target_id=self.target_id, why_visit=self.why_visit,
                        evidence_refs=list(self.evidence_refs), kind=self.kind, relation=self.relation)


class LinkIncrement(BaseModel):
    links: List[LinkRecord] = Field(default_factory=list)
    pending: List[LinkRecord] = Field(default_factory=list)
    version: str = RELATION_RULE_VERSION

    model_config = ConfigDict(extra="forbid")

    def related_for(self, page_path: str) -> List[str]:
        """The compatibility projection: resolved navigation targets, sorted."""
        return sorted({record.target_id for record in self.links
                       if record.page_path == page_path})

    def page_links(self, page_path: str) -> List[PageLink]:
        return [link for link in (record.as_page_link() for record in self.links
                                  if record.page_path == page_path) if link is not None]


def _target_index(page_index: Mapping[str, Any]) -> Dict[str, str]:
    """mention -> canonical page path, from the published page index."""
    paths = {normalize_name(str(path)): str(path) for path in page_index}
    names: Dict[str, set[str]] = {}
    for path, entry in sorted(page_index.items()):
        values = ([entry.get("title", "")] + list(entry.get("aliases", ()))
                  if isinstance(entry, Mapping) else
                  [getattr(entry, "title", "")] + list(getattr(entry, "aliases", ())))
        for value in values:
            key = normalize_name(str(value))
            if key:
                names.setdefault(key, set()).add(str(path))
    return {**{key: next(iter(values)) for key, values in names.items()
               if len(values) == 1 and key not in paths}, **paths}


def _evidence_is_real(refs: Sequence[Mapping[str, Any]], contributions: Any) -> bool:
    if not refs:
        return False
    known = {(ref.passage_id, " ".join(ref.excerpt.split()))
             for record in getattr(contributions, "records", ())
             if record.support_status == "SUPPORTED" and not record.unresolved_evidence
             for ref in record.evidence}
    return all((str(ref.get("passage_id", "")), " ".join(str(ref.get("excerpt", "")).split()))
               in known for ref in refs)


def _relation_supported(proposal: LinkProposal, target: str,
                        page_index: Mapping[str, Any], contributions: Any) -> bool:
    """Conservative directed relation proof, using the existing support gate.

    Co-occurrence and a genuine quote do not prove a relation. The canonical
    proposition must name both endpoints and the requested direction/type.
    Unsupported paraphrases stay useful navigation without a fact assertion.
    """
    from jonex_wiki.domain.engine.models.claim import assess_claim_support
    if not target or not _allowed_relation(proposal.relation) or not _evidence_is_real(proposal.evidence_refs, contributions):
        return False
    def title(path):
        entry = page_index.get(path)
        return str(entry.get("title", "") if isinstance(entry, Mapping) else getattr(entry, "title", ""))
    subject, obj = title(proposal.page_path), title(target)
    if not subject or not obj:
        return False
    proposition = f"{subject} is the {proposal.relation} of {obj}."
    requested = {(str(ref.get("passage_id", "")), " ".join(str(ref.get("excerpt", "")).split()))
                 for ref in proposal.evidence_refs}
    spans = [span for record in contributions.records
             if record.support_status == "SUPPORTED" and not record.unresolved_evidence
             for span in record.source_spans
             if (span.passage_id, " ".join(span.text.split())) in requested]
    if re.fullmatch(r"[\u3400-\u9fff]+", proposal.relation):
        # Only a direct, positive, source-stated subject→predicate→object clause.
        # Keep the *whole source statement* as a qualifier, so monetary, temporal
        # and other restrictions are never erased by shortening the edge label.
        names = lambda path: ([title(path)] + list(
            (page_index[path].get("aliases", []) if isinstance(page_index[path], Mapping)
             else getattr(page_index[path], "aliases", []))))
        for span in spans:
            statement = str(span.text)
            if re.search(r"是否|能否|不属实|并非|不是|不负责|不适用|无权|不得|不能|假设|可能|拟由", statement):
                continue
            for clause in re.split(r"[。；;\n]", statement):
                for source_name in names(proposal.page_path):
                    for target_name in names(target):
                        if not source_name or not target_name:
                            continue
                        pattern = (r"^\s*" + re.escape(source_name) +
                                   r"(?:[（(][^）)]{0,40}[）)])?\s*" + re.escape(proposal.relation) +
                                   r"(?P<conditions>[^，,。；;\n]{0,120}?)" + re.escape(target_name) + r"(?:[，,\s].*)?$")
                        match = re.fullmatch(pattern, clause.strip())
                        if match and not re.search(r"并|以及|同时|且|而|但|与|和|及|或", match["conditions"]) and assess_claim_support(
                                statement, [span], has_unresolved_evidence=False)[0] == "SUPPORTED":
                            return True
        return False
    exact_relation = normalize_name(proposition)
    # Require the complete directed sentence, never just shared entities/verbs.
    exact = any(exact_relation in [normalize_name(part.strip()) + "."
                for part in str(span.text).split(".") if part.strip()] for span in spans)
    return exact and assess_claim_support(proposition, spans, has_unresolved_evidence=False)[0] == "SUPPORTED"


def _qualifiers(refs, contributions):
    requested = {(str(r.get("passage_id", "")), " ".join(str(r.get("excerpt", "")).split())) for r in refs}
    found = []
    for record in getattr(contributions, "records", ()):
        if record.support_status != "SUPPORTED" or not any(
                (ref.passage_id, " ".join(ref.excerpt.split())) in requested for ref in record.evidence):
            continue
        # Preserve the actual supporting quote, not a shortened proposition or
        # unvalidated model hints about currency/date/amount boundaries.
        for value in [{"source_statement": ref.excerpt} for ref in record.evidence
                      if (ref.passage_id, " ".join(ref.excerpt.split())) in requested]:
            if value not in found:
                found.append(value)
    return found


def _relation_key(record):
    return (record.page_path, record.target_id, record.kind, record.relation,
            json.dumps(record.qualifiers, ensure_ascii=False, sort_keys=True))


def build_link_increment(contributions: Any, *,
                         proposals: Iterable[LinkProposal] = (),
                         page_index: Optional[Mapping[str, Any]] = None,
                         legacy_related: Optional[Mapping[str, Iterable[str]]] = None,
                         ) -> LinkIncrement:
    """Decide every proposed link, and keep the legacy projection compatible."""
    index = _target_index(page_index or {})
    links: List[LinkRecord] = []
    pending: List[LinkRecord] = []
    seen: Dict[Tuple[str, ...], LinkRecord] = {}
    for proposal in proposals or ():
        target = index.get(normalize_name(proposal.target_mention), "")
        relation = str(proposal.relation or "")
        supported = _relation_supported(proposal, target, page_index or {}, contributions)
        kind = KIND_SUPPORTED if supported else KIND_NAVIGATION
        aliases = [normalize_name(alias) for alias in proposal.mentioned_aliases
                   if normalize_name(alias) not in
                   {normalize_name(item) for item in (proposal.relation,) if item}]
        record = LinkRecord(
            page_path=str(proposal.page_path),
            target_id=target or "",
            why_visit=" ".join(str(proposal.why_visit).split())[:120],
            kind=kind,
            # Evidence offered for a relation that did not verify is not carried
            # over: a navigation link must not smuggle unproven references.
            evidence_refs=list(proposal.evidence_refs) if supported or not target else [],
            relation=relation if supported or not target else "",
            qualifiers=_qualifiers(proposal.evidence_refs, contributions) if supported else [],
            resolution=RESOLVED if target else PENDING,
            aliases=aliases,
            target_mention="" if target else str(proposal.target_mention),
        )
        if not target:
            # The target does not exist yet: keep the reference, render nothing.
            pending.append(record)
            continue
        if target == record.page_path:
            # S7-C: a page never links to itself.  A mention of one of the
            # page's *own* declared names (its title or an alias) resolves back
            # to the page it came from; publishing that as a navigation or
            # relation entry is a structural defect (the tree audit rejects it
            # as SELF_STRUCTURED_LINK) and no reader gains a destination.
            count_work("relation_self_links_dropped")
            continue
        key = _relation_key(record)
        existing = seen.get(key)
        if existing is None:
            seen[key] = record
        elif len(record.evidence_refs) > len(existing.evidence_refs):
            seen[key] = record
    legacy_pages: Dict[str, List[str]] = {}
    for page_path, targets in sorted(dict(legacy_related or {}).items()):
        planned = legacy_pages.setdefault(str(page_path), [])
        for target in targets:
            resolved = index.get(normalize_name(str(target)), "")
            if not resolved:
                pending.append(LinkRecord(page_path=str(page_path), target_id="",
                    target_mention=str(target), why_visit="legacy related entry",
                    kind=KIND_NAVIGATION, resolution=PENDING))
                continue
            if resolved and resolved != str(page_path) and resolved not in planned:
                planned.append(resolved)
    seen_targets = {(record.page_path, record.target_id) for record in seen.values()}
    for page_path, targets in legacy_pages.items():
        why = "legacy related entry"
        for target in targets:
            if (page_path, target) in seen_targets or not target:
                continue
            record = LinkRecord(page_path=page_path, target_id=target, why_visit=why,
                                kind=KIND_NAVIGATION, resolution=RESOLVED)
            seen[_relation_key(record)] = record
            seen_targets.add((page_path, target))
    links = sorted(seen.values(),
                   key=lambda item: (item.page_path,
                                     0 if item.kind == KIND_NAVIGATION else 1,
                                     item.target_id))
    pending.sort(key=lambda item: (item.page_path, normalize_name(item.target_mention)))
    return LinkIncrement(links=links, pending=pending)


def resolve_pending(increment: LinkIncrement, *,
                    page_index: Mapping[str, Any], contributions: Any = None) -> LinkIncrement:
    """End-of-batch resolution: a target that now exists becomes a real link."""
    index = _target_index(page_index or {})
    links = list(increment.links)
    still_pending: List[LinkRecord] = []
    for record in increment.pending:
        target = index.get(normalize_name(record.target_mention), "")
        if not target:
            target = index.get(normalize_name(record.target_id), "")
        if not target:
            still_pending.append(record)
            continue
        supported = contributions is not None and _relation_supported(LinkProposal(
            page_path=record.page_path, target_mention=target, why_visit=record.why_visit,
            relation=record.relation, evidence_refs=record.evidence_refs), target, page_index, contributions)
        links.append(record.model_copy(update={"target_id": target,
            "resolution": RESOLVED, "target_mention": "",
            "kind": KIND_SUPPORTED if supported else KIND_NAVIGATION,
            "relation": record.relation if supported else "",
            "qualifiers": _qualifiers(record.evidence_refs, contributions) if supported else [],
            "evidence_refs": record.evidence_refs if supported else []}))
    links.sort(key=lambda item: (item.page_path,
                                 0 if item.kind == KIND_NAVIGATION else 1,
                                 item.target_id))
    return LinkIncrement(links=links, pending=still_pending)


def withdraw_links(increment: LinkIncrement, *, live_evidence: Any,
                   removed_pages: Iterable[str] = ()) -> LinkIncrement:
    """A source going away takes its relation support with it, nothing else.

    A ``supported_relation`` whose evidence no longer exists is not deleted
    silently and never kept as supported: it falls back to navigation, because
    the pages it connects still exist.
    """
    gone = {str(page) for page in removed_pages}
    links: List[LinkRecord] = []
    for record in increment.links:
        if record.page_path in gone:
            continue
        if record.kind == KIND_SUPPORTED and not _evidence_is_real(
                record.evidence_refs, live_evidence):
            record = record.model_copy(update={"kind": KIND_NAVIGATION, "relation": "", "evidence_refs": [], "aliases": [], "qualifiers": []})
        links.append(record)
    return LinkIncrement(
        links=links,
        pending=[record for record in increment.pending if record.page_path not in gone])


def merge_link_increments(current: LinkIncrement,
                          incoming: LinkIncrement, *,
                          pages: Iterable[str] = ()) -> LinkIncrement:
    """Merge two concurrent increments.

    Additions from both sides survive and are deduplicated by
    ``(page, target, kind)``, keeping the better-evidenced record.  A page listed
    in ``pages`` was rebuilt from scratch, so its *old* records are dropped
    first - deletion and modification follow S5's rules instead of a field-wise
    union.
    """
    touched = {str(page) for page in pages}
    kept = [record for record in current.links if record.page_path not in touched]
    merged = {record.page_path: [] for record in kept}
    for record in kept:
        merged[record.page_path].append(record)
    for record in incoming.links:
        merged.setdefault(record.page_path, []).append(record)
    links: List[LinkRecord] = []
    for page_path in sorted(merged):
        seen: Dict[Tuple[str, ...], LinkRecord] = {}
        for record in merged[page_path]:
            key = _relation_key(record)
            existing = seen.get(key)
            if existing is None or len(record.evidence_refs) > len(existing.evidence_refs):
                seen[key] = record
        links.extend(seen.values())
    links.sort(key=lambda item: (item.page_path,
                                 0 if item.kind == KIND_NAVIGATION else 1,
                                 item.target_id))
    pending = [record for record in current.pending if record.page_path not in touched]
    pending.extend(record for record in incoming.pending)
    pending.sort(key=lambda item: (item.page_path, item.target_id, item.why_visit))
    return LinkIncrement(links=links, pending=pending)


def requires_rebuild(current: LinkIncrement, rebuilt: LinkIncrement) -> bool:
    """Whether a full rebuild would produce exactly the incremental result."""
    return current.model_dump() != rebuilt.model_dump()


def is_relation_supported(record: LinkRecord) -> bool:
    return record.kind == KIND_SUPPORTED and bool(record.relation)


__all__ = [
    "KIND_NAVIGATION",
    "KIND_SUPPORTED",
    "PENDING",
    "RELATION_RULE_VERSION",
    "RESOLVED",
    "LinkIncrement",
    "LinkProposal",
    "LinkRecord",
    "build_link_increment",
    "is_relation_supported",
    "merge_link_increments",
    "requires_rebuild",
    "resolve_pending",
    "withdraw_links",
]


# ──────────────────────────────────────────────
#  Publication: proposals from the rendered pages, payload, load
# ──────────────────────────────────────────────

FILE_NAME = "query/relations.json"
RELATIONS_SCHEMA_VERSION = 1
DOWNGRADED_NO_EVIDENCE = "supported_relation_without_relation_evidence"
DOWNGRADED_UNKNOWN_RELATION = "supported_relation_without_relation_kind"
MAX_LINK_WHY_CHARS = 120


def page_declared_relations(files: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Published links that declared a relation, for the downgrade audit."""
    from jonex_wiki.domain.engine.compile.mention_index import _parse_frontmatter_fields
    out: List[Dict[str, Any]] = []
    for path, raw in sorted(dict(files or {}).items()):
        text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        for entry in _parse_frontmatter_fields(text).get("links") or ():
            if not isinstance(entry, Mapping):
                continue
            if str(entry.get("kind", "")) != KIND_SUPPORTED:
                continue
            out.append({"page_path": str(path),
                        "target_id": str(entry.get("target_id", "") or ""),
                        "relation": str(entry.get("relation", "") or "").casefold()})
    return out


def audit_declared_relations(declared: Sequence[Mapping[str, Any]],
                             increment: LinkIncrement,
                             contributions: Any) -> List[Dict[str, Any]]:
    """Record every published supported_relation that lost its support."""
    supported = {(record.page_path, normalize_name(record.target_id), record.relation)
                 for record in increment.links if record.kind == KIND_SUPPORTED}
    out: List[Dict[str, Any]] = []
    for item in declared:
        page_path = str(item.get("page_path", ""))
        target = str(item.get("target_id", ""))
        relation = str(item.get("relation", "") or "")
        if (page_path, normalize_name(target), relation) in supported:
            continue
        reason = (DOWNGRADED_UNKNOWN_RELATION if not _allowed_relation(relation)
                  else DOWNGRADED_NO_EVIDENCE)
        out.append({"page_path": page_path, "target_id": target,
                    "declared_relation": relation, "reason": reason})
    out.sort(key=lambda item: (item["page_path"], item["target_id"]))
    return out


def proposals_from_pages(files: Mapping[str, Any]) -> Dict[str, Any]:
    """Read the links the compile model already published on its pages.

    The page frontmatter is the only producer here: this layer does not invent
    links, it audits and indexes the ones the compiler emitted.
    """
    from jonex_wiki.domain.engine.compile.mention_index import _parse_frontmatter_fields
    proposals: List[LinkProposal] = []
    legacy: Dict[str, List[str]] = {}
    for path, raw in sorted(dict(files or {}).items()):
        text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        fields = _parse_frontmatter_fields(text)
        for entry in fields.get("links") or ():
            if not isinstance(entry, Mapping):
                continue
            target = str(entry.get("target_id", "") or "").strip()
            if not target:
                continue
            relation = str(entry.get("relation", "") or "").casefold()
            proposals.append(LinkProposal(
                page_path=str(path),
                target_mention=target,
                # A published link always says why it is worth visiting; a link
                # without a reason is not a link this layer will render.
                why_visit=str(entry.get("why_visit", "") or "linked page")[:MAX_LINK_WHY_CHARS],
                evidence_refs=list(entry.get("evidence_refs") or ()),
                relation=relation if _allowed_relation(relation) else "",
                mentioned_aliases=list(entry.get("aliases") or ()),
            ))
        related = [str(item) for item in (fields.get("related") or ()) if str(item).strip()]
        if related:
            legacy[str(path)] = related
    return {"proposals": proposals, "related": legacy}


def downgraded_links(proposals: Sequence[LinkProposal],
                     increment: LinkIncrement) -> List[Dict[str, Any]]:
    """Links that claimed a supported relation the material does not support."""
    decided = {(record.page_path, normalize_name(record.target_id)
                or normalize_name(record.target_mention)): record
               for record in list(increment.links) + list(increment.pending)}
    out: List[Dict[str, Any]] = []
    for proposal in proposals:
        if not proposal.relation:
            continue
        record = decided.get((proposal.page_path, normalize_name(proposal.target_mention)))
        if record is not None and record.kind == KIND_SUPPORTED:
            continue
        out.append({"page_path": proposal.page_path,
                    "target_id": proposal.target_mention,
                    "declared_relation": proposal.relation,
                    "reason": DOWNGRADED_NO_EVIDENCE})
    out.sort(key=lambda item: (item["page_path"], item["target_id"]))
    return out


def payload(increment: LinkIncrement, *, page_index: Mapping[str, Any] | None = None,
            downgraded: Optional[Sequence[Mapping[str, Any]]] = None) -> Dict[str, Any]:
    """The published, auditable view of the increment."""
    import hashlib
    import json as _json
    targets_by_page: Dict[str, set[str]] = {}
    for record in increment.links:
        targets_by_page.setdefault(record.page_path, set()).add(record.target_id)
    related = {page: sorted(targets) for page, targets in targets_by_page.items()}
    body = {
        "schema_version": RELATIONS_SCHEMA_VERSION,
        "version": increment.version,
        "links": [record.model_dump() for record in increment.links],
        "pending": [record.model_dump() for record in increment.pending],
        "related": {page: targets for page, targets in sorted(related.items())},
        "downgraded": [dict(item) for item in (downgraded or ())],
    }
    encoded = _json.dumps({"links": body["links"], "pending": body["pending"],
                           "related": body["related"], "downgraded": body["downgraded"],
                           "version": body["version"]},
                          ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")).encode("utf-8")
    body["digest"] = "sha256:" + hashlib.sha256(encoded).hexdigest()
    return body


def load(raw: Any, *, page_index: Mapping[str, Any] | None = None) -> Optional[LinkIncrement]:
    """Rebuild the increment from published bytes; a missing file means none."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping) or raw.get("schema_version") != RELATIONS_SCHEMA_VERSION or raw.get("version") not in ("wiki-relation-increment-v1", RELATION_RULE_VERSION):
        raise ValueError("RELATIONS_VERSION_UNSUPPORTED")
    if not raw.get("digest"):
        raise ValueError("RELATIONS_DIGEST_MISSING")
    links = [LinkRecord.model_validate(item) for item in raw.get("links") or ()]
    pending = [LinkRecord.model_validate(item) for item in raw.get("pending") or ()]
    version = str(raw.get("version") or RELATION_RULE_VERSION)
    expected = str(raw.get("digest") or "")
    if expected:
        # Validate historical bytes as published. Adding the new optional
        # qualifiers field must not invalidate an old immutable v1 bundle.
        if version == "wiki-relation-increment-v1":
            content = {key: raw.get(key, [] if key != "related" else {})
                       for key in ("links", "pending", "related", "downgraded")}
            content["version"] = version
            encoded = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
            related = {page: sorted({r.target_id for r in links if r.page_path == page})
                       for page in sorted({r.page_path for r in links})}
        else:
            rebuilt = payload(LinkIncrement(links=links, pending=pending, version=version),
                              downgraded=raw.get("downgraded") or ())
            digest, related = rebuilt["digest"], rebuilt["related"]
        if digest != expected or related != raw.get("related"):
            raise ValueError("published relations digest mismatch")
    return LinkIncrement(links=links, pending=pending, version=version)


def relation_targets(increment: Optional[LinkIncrement], page_path: str) -> List[PageLink]:
    """What the query side may show for a page: resolved links with their reasons."""
    if increment is None:
        return []
    return increment.page_links(page_path)


__all__ += [
    "DOWNGRADED_NO_EVIDENCE",
    "DOWNGRADED_UNKNOWN_RELATION",
    "FILE_NAME",
    "RELATIONS_SCHEMA_VERSION",
    "load",
    "payload",
    "proposals_from_pages",
    "relation_targets",
]


def project_state_relations(state: Any, claims: Sequence[Any]) -> Any:
    """Project live source contributions into the ordinary page graph.

    Preserve unresolved mentions in state, never as clickable links. Legacy
    snapshots are compatibility inputs, and cannot self-authorize support.
    """
    from jonex_wiki.domain.engine.compile.source_contribution import from_claims
    contributions = from_claims(claims, pages=state.pages)
    proposals = [LinkProposal.model_validate(value) for record in contributions.records
                 for value in record.link_proposals]
    proposal_targets = {}
    for item in proposals:
        proposal_targets.setdefault(item.page_path, set()).add(item.target_mention)
    legacy = {}
    for path, page in state.pages.items():
        detailed = proposal_targets.setdefault(path, set())
        for link in page.links:
            if link.target_id in detailed:
                continue
            proposals.append(LinkProposal(page_path=path, target_mention=link.target_id,
                why_visit=link.why_visit, evidence_refs=link.evidence_refs,
                relation=link.relation if _allowed_relation(link.relation) else ""))
            detailed.add(link.target_id)
        # Related entries only supplement pages that have no detailed record.
        legacy[path] = [target for target in page.related_pages if target not in detailed]
    links = build_link_increment(contributions, proposals=proposals,
                                 page_index=state.index, legacy_related=legacy)
    links_by_page = {}
    for record in links.links:
        links_by_page.setdefault(record.page_path, []).append(record)
    pages = dict(state.pages)
    for path, page in pages.items():
        records = links_by_page.get(path, ())
        resolved = [link for record in records if (link := record.as_page_link()) is not None]
        related = sorted({record.target_id for record in records})
        if page.links != resolved or page.related_pages != related:
            pages[path] = page.model_copy(update={"links": resolved, "related_pages": related})
    return state.model_copy(update={"pages": pages})
