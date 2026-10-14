"""AC-006 batch acceptance orchestration (lwp-arch-001 ALG1).

Extracted verbatim from ``engines/llm_wiki_plus/algorithm1.py``:
``run_algorithm1_batch`` — the fail-closed passage batch compilation that
composes candidate blocking, the compile planner, the deterministic target /
structural / evidence gates and the bounded semantic review.  It calls only
the injected ``ModelPort`` plus the pure compile modules; it never touches
storage, the filesystem, a database, a clock, randomness or the network, and
never mutates caller input.  The old import path stays available through the
re-export shim.
"""

from __future__ import annotations

from jonex_wiki.domain.engine.compile.repair.planner import compile_output_tokens, semantic_review_output_tokens

import json
import logging
import re
from copy import deepcopy
from typing import Any, Dict, List, Literal, Optional

from jonex_wiki.domain.engine.compile.repair.blocking import (MAX_CANDIDATE_PAGE_SNAPSHOTS, _copy_state_for_mutation, _high_risk_content, _semantic_review_mode, derive_page_candidates)
from jonex_wiki.domain.engine.models.repair_models import (
    Algorithm1BatchResult,
    CombinedCompileResponse,
    CompileProposal,
    NoClaimDecision,
    PageMutation,
    PassageCompileResult,
)
from jonex_wiki.domain.engine.compile.repair.planner import _call, _messages
from jonex_wiki.domain.engine.compile.repair.mutation import _repair_combined_response_paths
from pydantic import ValidationError

from jonex_wiki.kernel.ports import ModelPort
from jonex_wiki.domain.engine.models.compiler_state import (
    LINK_PROPOSAL_LIMIT_CODE,
    MAX_PAGE_LINK_MENTIONS,
    MAX_SOURCE_LINK_TARGETS,
    ArchivedPassage,
    CompilerState,
    LinkMention,
    PageIndexEntry,
    PageSelection,
    WikiPageSnapshot,
    canonical_wiki_path,
    normalize_wiki_path,
    resolve_page_selection,
)
from jonex_wiki.domain.engine.compile.content_validation import (
    ClaimDraft,
    ContentIssue,
    ContentReport,
    SemanticReview,
    is_substantive_passage,
    merge_semantic_review,
    normalize_evidence_text,
    validate_claim_evidence,
)
from jonex_wiki.domain.engine.compile.identity import normalize_name
from jonex_wiki.domain.engine.compile.rendering import render_wiki_delta
from jonex_wiki.domain.engine.compile.structural import (
    StructuralIssue,
    StructuralReport,
    validate_update_targets,
    validate_wiki_delta,
)
from jonex_wiki.domain.engine.compile.incremental import (
    RenderIndexDelta,
    WikiRenderIndex,
    WikiRenderIndexOverlay,
    plan_mutation_impact,
)

logger = logging.getLogger(__name__)




def _self_link_keys(path: str, title: str = "") -> set:
    """The normalized names under which a page would link to itself."""
    text = str(path or "")
    stem = text.rsplit("/", 1)[-1]
    if stem.endswith(".md"):
        stem = stem[:-3]
    keys = {normalize_name(text), normalize_name(stem), normalize_name(title)}
    return {key for key in keys if key}


def _mutation_link_mentions(mutation) -> List[LinkMention]:
    """Every link one mutation proposes, from either published channel.

    A ``facts`` mutation carries its proposals on the mutation; a legacy
    ``create``/``update`` mutation carries a whole page, and that page has its
    own ``link_mentions``.  Both are proposals from the same source
    understanding output and are counted together.
    """
    mentions = list(getattr(mutation, "link_mentions", ()) or ())
    if str(getattr(mutation, "operation", "")) != "facts":
        page = getattr(mutation, "page", None)
        mentions.extend(list(getattr(page, "link_mentions", ()) or ()))
    return mentions


def bound_link_mentions(mutations, titles: Optional[Dict[str, str]] = None) -> Dict[str, List[LinkMention]]:
    """Deduplicate optional navigation without rejecting source facts."""
    title_map = dict(titles or {})
    by_path: Dict[str, List[LinkMention]] = {}
    seen: Dict[str, str] = {}
    proposed = 0
    self_links = 0
    for mutation in mutations or ():
        path = str(getattr(mutation, "path", ""))
        own = _self_link_keys(path, title_map.get(path, ""))
        for mention in _mutation_link_mentions(mutation):
            proposed += 1
            key = normalize_name(mention.target_mention)
            if not key:
                continue
            if key in own:
                self_links += 1
                continue
            if key in seen:
                continue
            seen[key] = path
            by_path.setdefault(path, []).append(mention)
    from jonex_wiki.kernel.telemetry import count_work
    count_work("compile_link_proposals_proposed", proposed)
    count_work("compile_link_proposals_deduplicated", len(seen))
    count_work("compile_link_proposals_self_link", self_links)
    if seen:
        count_work("compile_link_mentions_serialized_characters", len(json.dumps(
            [mention.model_dump() for items in by_path.values() for mention in items],
            ensure_ascii=False)))
    return by_path



# ──────────────────────────────────────────────
#  Batch orchestration
# ──────────────────────────────────────────────


async def run_algorithm1_batch(
    state: CompilerState,
    passage_ids: List[str],
    model: ModelPort,
    *,
    tenant_id: str = "",
    context_id: str = "",
    trace_id: str = "",
    model_name: Optional[str] = None,
    render_index: Optional[WikiRenderIndex | WikiRenderIndexOverlay] = None,
    compile_main_prompt: str = "",
) -> Algorithm1BatchResult:
    """Process an ordered batch of archived passages with fail-closed gates.

    Raises ``ValueError`` with a stable reason marker before any model call for
    duplicate, missing or already-processed passage IDs.  An empty batch makes
    no model call and returns a deeply equal, non-identical state.
    """
    # Pre-flight validation against the caller's state — before any model call.
    seen: set = set()
    for pid in passage_ids:
        if pid in seen:
            raise ValueError(f"DUPLICATE_PASSAGE_ID: {pid}")
        seen.add(pid)
        if pid not in state.source_archive:
            raise ValueError(f"PASSAGE_NOT_ARCHIVED: {pid}")
        if pid in state.processed_passage_ids:
            raise ValueError(f"PASSAGE_ALREADY_PROCESSED: {pid}")

    working = _copy_state_for_mutation(state)
    base_render_index = render_index or WikiRenderIndex.from_state(state)
    working_render_index = (
        base_render_index.fork()
        if isinstance(base_render_index, WikiRenderIndexOverlay)
        else base_render_index.overlay()
    )
    results: List[PassageCompileResult] = []
    halted: Optional[str] = None

    def accept_source_only(
        passage: ArchivedPassage,
        candidates: List[PageIndexEntry],
        selection: PageSelection,
        *,
        classification: str,
        reason: str,
        triage_model_called: bool,
    ) -> PassageCompileResult:
        """Commit an explicit no-Claim decision without a compile retry."""

        pid = passage.passage_id
        working.processed_passage_ids = list(working.processed_passage_ids) + [pid]
        working.claim_compilation_dispositions[pid] = {
            "passage_id": pid,
            "classification": classification,
            "reason": " ".join(reason.split())[:240],
            "retry_performed": False,
            "triage_model_called": triage_model_called,
            "claim_compilation_degraded": True,
            "raw_source_archived": pid in working.source_archive,
        }
        return PassageCompileResult(
            passage_id=pid,
            candidates=candidates,
            selection=selection,
            proposal=CompileProposal(mutations=[], claims=[]),
            candidate_state=None,
            files={},
            structural_report=StructuralReport(issues=[]),
            content_report=ContentReport(),
            accepted=True,
        )

    async def triage_empty_result(passage: ArchivedPassage) -> NoClaimDecision:
        """Classify only a failed empty result; successful compiles never call this."""

        exact_covered = any(
            other_id in working.processed_passage_ids
            and other_id != passage.passage_id
            and other.sha256 == passage.sha256
            for other_id, other in working.source_archive.items()
        )
        if exact_covered:
            return NoClaimDecision(
                action="accept_source_only",
                classification="duplicate_covered",
                reason="An identical archived passage was already processed.",
            )
        raw = await _call(
            model,
            {
                "stage": "claim_failure_triage",
                "instructions": (
                    "The normal Wiki compile returned an empty selection. Decide whether "
                    "this passage genuinely needs no standalone Claim or whether Claim "
                    "compilation should be retried. Accept source-only only for material "
                    "such as a directory, disclaimer, header/footer, isolated continuation "
                    "fragment, layout-only text, or content already fully covered. If the "
                    "passage contains any independently useful factual statement, number, "
                    "rule, condition, exception, entity relation, or table meaning, choose "
                    "retry_claim_compilation. Never judge source-only merely because the "
                    "first model response was empty. Give a concrete reason grounded in "
                    "passage.text."
                ),
                "passage": passage.model_dump(),
            },
            NoClaimDecision.model_json_schema(),
            512,
            model_name=model_name,
            tenant_id=tenant_id,
            context_id=context_id,
            trace_id=trace_id,
            scene="jonex_wiki_compile_triage",
            compile_main_prompt=compile_main_prompt,
        )
        decision = NoClaimDecision.model_validate(raw)
        if decision.classification == "duplicate_covered" and not exact_covered:
            return NoClaimDecision(
                action="retry_claim_compilation",
                classification="model_protocol_failure",
                reason="The duplicate claim was not supported by an already processed identical passage.",
            )
        return decision

    # Hybrid candidate retrieval (opt-in): the retriever indexes only accepted
    # pages (``working``), so uncommitted candidate states never enter the
    # index; it is refreshed after every accepted mutation.
    for pid in passage_ids:
        passage = working.source_archive[pid]
        candidates = derive_page_candidates(working, passage)

        # 1+2. SELECTPAGES + COMPILEWIKIPAGES collapsed into ONE model call.
        # Candidate page snapshots (with content) are included so the model can
        # edit existing pages; the deterministic gates below still run.
        snapshots = [
            working.pages[c.path].model_dump(exclude={"content"})
            for c in candidates[:MAX_CANDIDATE_PAGE_SNAPSHOTS]
            if c.path in working.pages
        ]
        raw = await _call(
            model,
            {
                "stage": "select_and_compile",
                "instructions": (
                    "Select up to five existing or new pages. Return a facts mutation "
                    f"{{operation: 'facts', path: '<category/name.md>'}} for each target, "
                    "with grounded claims in proposal.claims. Do not output page prose "
                    "or repeat old facts. The compiler appends admitted facts itself. "
                    "Return optional link_mentions for useful reading targets; a "
                    "link is a target worth visiting, never an enumeration of every "
                    "capitalised name, and never a reason to create a page): target_mention "
                    "(a source name, never invent a page ID), why_visit, evidence_refs "
                    "(exact source references), and optional directed relation: subject page "
                    "is the predecessor/successor/parent/subsidiary/brand of the target. "
                    "Use navigation (empty relation) unless that relation is explicitly stated. "
                    "New pages need path, title and reason in selection.new_pages; "
                    "existing pages go in selection.existing_paths. Paths must be "
                    "lowercase ASCII category/name.md. Cite exact excerpts from "
                    "passage.text with its passage_id, source_id, document_id, chunk_id. "
                    "Keep every substantive fact grounded; omit unsupported guesses."
                ),
                "passage": passage.model_dump(),
                "candidates": [c.model_dump() for c in candidates],
                "candidate_pages": snapshots,
                "active_constraints": list(working.active_constraints),
            },
            CombinedCompileResponse.model_json_schema(),
            compile_output_tokens(),
            model_name=model_name,
            tenant_id=tenant_id,
            context_id=context_id,
            trace_id=trace_id,
            scene="jonex_wiki_compile_page_edit",
            compile_main_prompt=compile_main_prompt,
        )
        try:
            combined = CombinedCompileResponse.model_validate(
                _repair_combined_response_paths(raw)
            )
        except ValidationError:
            logger.warning(
                "combined response paths: new=%s mut=%s claims=%s",
                [p.get("path") for p in raw.get("selection", {}).get("new_pages", [])][:3],
                [m.get("path") for m in raw.get("proposal", {}).get("mutations", [])][:3],
                [c.get("page_path") for c in raw.get("proposal", {}).get("claims", [])][:3],
            )
            raise
        selection = combined.selection
        proposal = combined.proposal
        resolved = resolve_page_selection(working, candidates, selection)

        # A model that drifts (or judges the passage as needing no page change)
        # may select nothing. A non-substantive passage is skipped as accepted;
        # a substantive passage (text >= 40) must NOT be silently dropped:
        # force one retry demanding a page + claim, then fail auditably if the
        # retry is still empty (Wave 3 P0.3).
        if not resolved.existing_pages and not resolved.new_pages:
            if not is_substantive_passage(passage.text):
                logger.warning("select_and_compile empty, skipping passage | pid=%s", pid)
                results.append(accept_source_only(
                    passage, candidates, selection,
                    classification="legitimate_no_claim",
                    reason="The passage is non-substantive under the deterministic content gate.",
                    triage_model_called=False,
                ))
                continue

            decision = await triage_empty_result(passage)
            if decision.action == "accept_source_only":
                logger.warning(
                    "claim failure triage accepted source-only | pid=%s classification=%s",
                    pid, decision.classification,
                )
                results.append(accept_source_only(
                    passage, candidates, selection,
                    classification=decision.classification,
                    reason=decision.reason,
                    triage_model_called=True,
                ))
                continue

            logger.warning(
                "claim failure triage requested one compile retry | pid=%s",
                pid,
            )
            raw = await _call(
                model,
                {
                    "stage": "select_and_compile",
                    "instructions": (
                    "Your previous selection was empty, but the passage "
                    "contains substantive facts. You MUST select at least "
                    "one page to create or update and produce at least one "
                    "grounded claim. Do not return an empty selection or "
                    "proposal. In each page snapshot, keep related_pages, "
                    "source_ids, and version outside the index object."
                    ),
                    "passage": passage.model_dump(),
                    "candidates": [c.model_dump() for c in candidates],
                    "candidate_pages": snapshots,
                    "active_constraints": list(working.active_constraints),
                },
                CombinedCompileResponse.model_json_schema(),
                compile_output_tokens(),
                model_name=model_name,
                tenant_id=tenant_id,
                context_id=context_id,
                trace_id=trace_id,
                scene="jonex_wiki_compile_page_edit",
                compile_main_prompt=compile_main_prompt,
            )
            combined = CombinedCompileResponse.model_validate(
                _repair_combined_response_paths(raw)
            )
            selection = combined.selection
            proposal = combined.proposal
            resolved = resolve_page_selection(working, candidates, selection)
            if not resolved.existing_pages and not resolved.new_pages:
                # Still empty: record the passage as failed (auditable) instead
                # of silently accepting it.
                logger.warning(
                    "substantive empty selection persists, failing passage | pid=%s",
                    pid,
                )
                results.append(
                    PassageCompileResult(
                        passage_id=pid,
                        candidates=candidates,
                        selection=selection,
                        proposal=CompileProposal(mutations=[], claims=[]),
                        candidate_state=None,
                        files={},
                        structural_report=StructuralReport(issues=[]),
                        content_report=ContentReport(
                            issues=[
                                ContentIssue(
                                    code="EMPTY_SELECTION_SUBSTANTIVE",
                                    category="malformed_reference",
                                    detail=pid,
                                    model_backed=False,
                                )
                            ]
                        ),
                        accepted=False,
                    )
                )
                halted = pid
                break
            # The retry produced a valid selection: fall through to the normal
            # compile flow with the retried selection/proposal.

        # A substantive passage must not be silently dropped: if the model
        # produced no mutations and no claims for a passage with real content,
        # ask once more with an explicit demand so the source is captured.
        if (
            not proposal.mutations
            and not proposal.claims
            and is_substantive_passage(passage.text)
        ):
            raw = await _call(
                model,
                {
                    "stage": "compile_wiki_pages",
                    "instructions": (
                        "Your previous output was empty, but the passage contains "
                        "substantive facts. You MUST create at least one page "
                        "mutation and one grounded claim. Do not return an empty "
                        "mutations or claims list."
                    ),
                    "passage": passage.model_dump(),
                    "existing_pages": [p.model_dump() for p in resolved.existing_pages],
                    "new_pages": [p.model_dump() for p in resolved.new_pages],
                    "index_entries": [c.model_dump() for c in candidates],
                    "active_constraints": list(working.active_constraints),
                },
                CompileProposal.model_json_schema(),
                compile_output_tokens(),
                model_name=model_name,
                tenant_id=tenant_id,
                context_id=context_id,
                trace_id=trace_id,
                scene="jonex_wiki_compile_pages",
                compile_main_prompt=compile_main_prompt,
            )
            proposal = CompileProposal.model_validate(raw)

        # Fact deltas carry no replacement page. Materialize them against the
        # selected snapshot; legacy page mutations remain explicitly supported.
        # R2.1: the response's link proposals are counted once, per source
        # understanding output, before any page keeps them.
        link_titles = {p.path: p.title for p in resolved.new_pages}
        link_titles.update({path: page.index.title
                            for path, page in working.pages.items()})
        bounded_links = bound_link_mentions(proposal.mutations, link_titles)
        materialized = []
        for mutation in proposal.mutations:
            if mutation.operation != "facts":
                # A legacy page mutation carries its own mentions; the response
                # bound applies to them too, and a self-link is dropped here
                # just as it is on the facts path.
                bounded = bounded_links.get(mutation.path)
                if mutation.page is not None and bounded != list(mutation.page.link_mentions):
                    mutation = mutation.model_copy(update={
                        "page": mutation.page.model_copy(update={
                            "link_mentions": list(bounded or ())})})
                materialized.append(mutation)
                continue
            path = mutation.path
            existing = working.pages.get(path)
            selected_new = next((p for p in resolved.new_pages if p.path == path), None)
            if existing is not None and path not in selection.existing_paths:
                raise ValueError("FACT_TARGET_NOT_SELECTED: " + path)
            if existing is None and selected_new is None:
                raise ValueError("FACT_TARGET_NOT_PROPOSED: " + path)
            texts = [c.text for c in proposal.claims if c.page_path == path]
            if not texts:
                # A selected target with no grounded claim is a no-op, not a
                # page mutation. Do not publish an empty page, and do not let
                # it discard other grounded targets from the same response.
                logger.warning("ignoring empty fact target | pid=%s path=%s", pid, path)
                continue
            if existing is None:
                page = WikiPageSnapshot(index=PageIndexEntry(path=path, title=selected_new.title,
                    description=selected_new.description, tags=selected_new.tags),
                    content="\n".join(dict.fromkeys(texts)), source_ids=[pid])
            else:
                page = existing.model_copy(update={"content": existing.content + "\n" + "\n".join(dict.fromkeys(texts)),
                    "source_ids": list(dict.fromkeys(existing.source_ids + [pid])),
                    "version": existing.version + 1})
            for mention in bounded_links.get(path, ()):
                # Each proposal must originate in this batch's actual passage.
                # S5 will bind it to admitted claims before it can be published.
                # The response-level bound already dropped duplicates, so this
                # only keeps the page's own history from repeating one.
                if mention in page.link_mentions:
                    continue
                page = page.model_copy(update={
                    "link_mentions": [*page.link_mentions, mention]})
            materialized.append(PageMutation(operation="update" if existing else "create", path=path, page=page))
        proposal = proposal.model_copy(update={"mutations": materialized})

        create_paths = [m.path for m in proposal.mutations if m.operation == "create"]
        update_paths = [m.path for m in proposal.mutations if m.operation == "update"]
        delete_paths = [m.path for m in proposal.mutations if m.operation == "delete"]

        # Selection is the explicit mutation scope, including legacy writes.
        target_report = validate_update_targets(
            working, selected_existing_paths=list(selection.existing_paths),
            create_paths=create_paths, update_paths=update_paths, delete_paths=delete_paths)
        new_paths = {p.path for p in resolved.new_pages}
        for path in create_paths:
            if path not in new_paths:
                target_report.issues.append(StructuralIssue(code="CREATE_TARGET_NOT_PROPOSED",
                    category="unseen_overwrite", path=path, detail="Creation was not selected", repairable=False))

        if not target_report.is_valid:
            logger.warning(
                "passage halted at target gate | pid=%s issues=%s",
                pid,
                [i.code for i in target_report.issues],
            )
            results.append(
                PassageCompileResult(
                    passage_id=pid,
                    candidates=candidates,
                    selection=selection,
                    proposal=proposal,
                    candidate_state=None,
                    files={},
                    structural_report=target_report,
                    content_report=ContentReport(),
                    accepted=False,
                )
            )
            halted = pid
            break

        # ── Candidate state: deep-copy and apply in caller order. ──
        candidate_state = _copy_state_for_mutation(working)
        for m in proposal.mutations:
            if m.operation == "delete":
                candidate_state.pages.pop(m.path, None)
                candidate_state.index.pop(m.path, None)
            else:
                candidate_state.pages[m.path] = m.page
                candidate_state.index[m.path] = m.page.index
        candidate_state.processed_passage_ids = (
            list(candidate_state.processed_passage_ids) + [pid]
        )

        impact, candidate_render_index = plan_mutation_impact(
            working,
            candidate_state,
            proposal.mutations,
            working_render_index,
        )
        render_delta = render_wiki_delta(
            candidate_state,
            impact,
            candidate_render_index,
        )
        tree_report = validate_wiki_delta(
            candidate_state,
            render_delta,
            impact,
            candidate_render_index,
        )
        structural_report = StructuralReport(
            issues=target_report.issues + list(tree_report.issues)
        )

        content_report = validate_claim_evidence(candidate_state, proposal.claims)

        # ── Deterministic evidence gate.  Semantic review is selective: the
        # bounded review call is restored ONLY for high-risk content (numbers/
        # dates/units, tables, same-page conflicts), so the anti-hallucination
        # safety net returns without the full per-passage cost.  Findings stay
        # non-fatal (audit + log), matching the frozen benchmark contract; the
        # deterministic evidence gate above still gates every claim.
        structural_ok = structural_report.is_valid
        review_mode = _semantic_review_mode()
        should_review = (
            structural_ok
            and bool(content_report.validated_claims)
            and review_mode != "off"
            and (
                review_mode == "always"
                or _high_risk_content(passage, content_report.validated_claims)
            )
        )
        if should_review:
            mutated_paths = sorted(
                m.path
                for m in proposal.mutations
                if m.operation == "update" and m.path in candidate_state.pages
            )
            sem_payload = {
                "stage": "semantic_review",
                "claims": [c.model_dump() for c in content_report.validated_claims],
                "page_snapshots": [
                    candidate_state.pages[p].model_dump() for p in mutated_paths
                ],
            }
            review_error = None
            for semantic_attempt in range(2):
                try:
                    payload = dict(sem_payload)
                    if semantic_attempt:
                        payload["protocol_correction"] = (
                            "A cross_page_contradiction finding must cite at least two distinct claim_ids. "
                            "Omit any finding that cannot satisfy the schema."
                        )
                    raw = await _call(
                        model,
                        payload,
                        SemanticReview.model_json_schema(),
                        semantic_review_output_tokens(),
                        model_name=model_name,
                        tenant_id=tenant_id,
                        context_id=context_id,
                        trace_id=trace_id,
                        scene="jonex_wiki_compile_review",
                        compile_main_prompt=compile_main_prompt,
                    )
                    review = SemanticReview.model_validate(raw)
                    content_report = merge_semantic_review(content_report, review)
                    review_error = None
                    break
                except Exception as exc:
                    review_error = exc
            if review_error is not None:
                logger.warning(
                    "semantic review invalid after bounded retry | pid=%s error=%s",
                    pid,
                    str(review_error).splitlines()[0],
                )
                content_report = ContentReport(
                    validated_claims=content_report.validated_claims,
                    issues=[*content_report.issues, ContentIssue(
                        code="SEMANTIC_REVIEW_INVALID",
                        category="malformed_reference",
                        claim_ids=[c.claim_id for c in content_report.validated_claims],
                        page_paths=mutated_paths,
                        detail=pid,
                        model_backed=True,
                    )],
                    semantic_review_complete=False,
                )
        elif structural_ok:
            content_report = ContentReport(
                validated_claims=content_report.validated_claims,
                issues=content_report.issues,
                semantic_review_complete=True,
            )

        # 确定性硬错误（claim 页面不存在）仍致命；语义 review 的 findings、
        # 软证据下的未解析 evidence、review 协议问题均降级为日志，不阻断编译。
        fatal_content = any(
            i.code == "CLAIM_PAGE_MISSING" for i in content_report.issues
        )
        accepted = structural_report.is_valid and not fatal_content
        if accepted:
            working = candidate_state
            from jonex_wiki.domain.engine.compile.source_contribution import from_claims
            from jonex_wiki.domain.engine.compile.identity import build_identity_index
            fresh_mentions = build_identity_index(from_claims(content_report.validated_claims)).entity_to_pages
            for name, paths in fresh_mentions.items():
                working._fact_mentions[name] = sorted(set(working._fact_mentions.get(name, ())) | set(paths))
            working_render_index = candidate_render_index
        else:
            halted = pid
            logger.warning(
                "passage halted | pid=%s structural_valid=%s content_publishable=%s "
                "structural_issues=%s content_issues=%s",
                pid,
                structural_report.is_valid,
                content_report.publishable,
                [i.code for i in structural_report.issues],
                [i.code for i in content_report.issues],
            )

        results.append(
            PassageCompileResult(
                passage_id=pid,
                candidates=candidates,
                selection=selection,
                proposal=proposal,
                candidate_state=candidate_state,
                render_delta=render_delta,
                render_index_delta=(
                    RenderIndexDelta.from_mapping(
                        {
                            path: candidate_render_index.page_edges(path)
                            for path in (m.path for m in proposal.mutations)
                        }
                    )
                    if accepted
                    else None
                ),
                files=render_delta.files,
                structural_report=structural_report,
                content_report=content_report,
                accepted=accepted,
            )
        )
        if not accepted:
            break


    return Algorithm1BatchResult(
        state=working,
        passages=results,
        render_index_delta=working_render_index.to_delta(),
        halted_at_passage_id=halted,
    )
