"""Deterministic candidate blocking for AC-006 (lwp-arch-001 ALG1).

Extracted verbatim from ``engines/llm_wiki_plus/algorithm1.py``: token
scoring constants, metadata overlap ranking (``derive_page_candidates``), the
hybrid retrieval wrapper (``derive_page_candidates_hybrid``), the bounded
semantic-review trigger (``_semantic_review_mode``/``_high_risk_content``),
the immutable state copy for mutation (``_copy_state_for_mutation``) and the
stable compile system instruction.  Pure: no provider, network, filesystem,
storage or database access.  The old import path stays available through the
re-export shim.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List

from jonex_wiki.domain.engine.models.compiler_state import (
    ArchivedPassage,
    CompilerState,
    PageIndexEntry,
)
from jonex_wiki.kernel.runtime_env import current_env

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
#  Deterministic candidate scoring
# ──────────────────────────────────────────────

# Tokenize case-folded text into ASCII word runs and single CJK characters.
_TOKEN_RE = re.compile(r"[a-z0-9]+|[一-鿿]")

# Metadata field weights (title, alias, tag, description).
_TITLE_WEIGHT = 8
_ALIAS_WEIGHT = 6
_TAG_WEIGHT = 4
_DESCRIPTION_WEIGHT = 3

# Wave 4: selective semantic review.  Values: ``off`` | ``selective`` (default)
# | ``always``.  ``selective`` restores the bounded semantic review ONLY for
# high-risk content (numbers/dates/units/currency, table-shaped lines, or
# same-page conflicting claim texts), so the anti-hallucination safety net
# returns at a fraction of the per-passage cost.  Findings remain non-fatal
# (audit + log) to keep the frozen benchmark contract unchanged.
SEMANTIC_REVIEW_ENV = "LLM_WIKI_SEMANTIC_REVIEW"
_HIGH_RISK_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9])\d{2,}([.,]\d+)?\s*(%|元|美元|€|£|万|亿|年|月|日)?"
    r"|[¥$€£]\s?\d"
)
_HIGH_RISK_TABLE_RE = re.compile(r"^\s*\|.*\|\s*$", re.M)



def _semantic_review_mode() -> str:
    raw = current_env().get(SEMANTIC_REVIEW_ENV, "off").strip().casefold()
    if raw in ("always", "on", "1", "true"):
        return "always"
    if raw in ("off", "0", "false", "no"):
        return "off"
    return "selective"


def _high_risk_content(passage: ArchivedPassage, claims: List[Any]) -> bool:
    """Return whether a passage/claim set warrants the bounded semantic review.

    High-risk signals are deterministic: explicit numbers/units/dates/currency
    in the passage or claim text, table-shaped lines, or two claims on the same
    page stating different facts (possible version/conflict).  Pure so the
    decision is reproducible across runs.
    """
    text = passage.text or ""
    if _HIGH_RISK_NUMBER_RE.search(text) or _HIGH_RISK_TABLE_RE.search(text):
        return True
    claim_texts = [c.text or "" for c in claims]
    if any(_HIGH_RISK_NUMBER_RE.search(t) for t in claim_texts):
        return True
    by_page: Dict[str, set] = {}
    for claim in claims:
        normalized = (claim.text or "").strip().casefold()
        if not normalized:
            continue
        by_page.setdefault(claim.page_path, set()).add(normalized)
    return any(len(texts) > 1 for texts in by_page.values())


def _copy_state_for_mutation(state: CompilerState) -> CompilerState:
    """Copy mutable compiler state while sharing the immutable source archive.

    A passage compilation only reads ``source_archive`` and existing page
    snapshots; writes replace or remove mapping entries rather than mutating
    snapshot values in place.  Deep-copying thousands of archived passages
    and page bodies once per in-flight worker made full-corpus compilation
    scale as ``O(concurrency × corpus_size)`` in memory.  The archive and
    snapshot values are therefore intentionally shared here; mapping/list
    containers are detached so caller state cannot be changed by assignment.
    """
    copied = CompilerState(
        batch_number=state.batch_number,
        pages={},
        index={},
        source_archive={},
        processed_passage_ids=[],
        active_constraints=list(state.active_constraints),
        passage_failures=dict(state.passage_failures),
        claim_compilation_dispositions=dict(state.claim_compilation_dispositions),
        recovered_retries=dict(state.recovered_retries),
    )
    # Pydantic defensively copies mappings passed to the constructor.  The
    # archive is read-only for this code path, so replace that empty mapping
    # with the original object after validation to get the intended sharing.
    object.__setattr__(copied, "source_archive", state.source_archive)
    object.__setattr__(copied, "pages", dict(state.pages))
    copied._fact_mentions = dict(state._fact_mentions)
    object.__setattr__(copied, "index", dict(state.index))
    object.__setattr__(
        copied, "processed_passage_ids", list(state.processed_passage_ids)
    )
    return copied

# Upper bound on candidate metadata snapshots. Fact-delta compilation omits
# old page prose from model requests; this bounds the remaining metadata.
MAX_CANDIDATE_PAGE_SNAPSHOTS = 12

def _field_tokens(value: str) -> set:
    """Tokenize one metadata string (case-folded) into a set of tokens."""
    return set(_TOKEN_RE.findall(value.casefold()))


def _sum_field_overlaps(values: List[str], passage_tokens: set) -> int:
    """Sum the distinct-overlap count of each field string individually.

    Each alias/tag entry is scored as its own field (weight x distinct shared
    tokens); duplicates within the list are not merged, so ``[alpha, alpha]``
    contributes twice.  This keeps the score faithful to "each alias 6, each
    tag 4".
    """
    return sum(len(_field_tokens(value) & passage_tokens) for value in values)


def derive_page_candidates(
    state: CompilerState,
    passage: ArchivedPassage,
    *,
    limit: int = 50,
) -> List[PageIndexEntry]:
    """Derive ranked candidate pages from metadata overlap with a passage.

    Pure: never mutates ``state`` or ``passage`` and never inspects page body
    content.  Tokens come from ``[a-z0-9]+|[一-鿿]`` on case-folded
    strings.  Each field's score is its weight times the number of distinct
    tokens it shares with the passage.  Zero-score pages are excluded, results
    are ordered by descending score then ascending canonical path, capped at
    ``limit``.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or not (1 <= limit <= 200):
        raise ValueError("limit must be between 1 and 200")

    # Structural context is a retrieval hint only: it can improve routing to
    # a page named by a preceding heading/table header, while evidence gates
    # continue to inspect the canonical ``passage.text`` exclusively.
    passage_tokens = _field_tokens(passage.text + "\n" + (passage.context_text or ""))

    scored: List[tuple] = []
    for entry in state.index.values():
        title_overlap = len(_field_tokens(entry.title) & passage_tokens)
        alias_overlap = _sum_field_overlaps(entry.aliases, passage_tokens)
        tag_overlap = _sum_field_overlaps(entry.tags, passage_tokens)
        desc_overlap = len(_field_tokens(entry.description) & passage_tokens)
        score = (
            _TITLE_WEIGHT * title_overlap
            + _ALIAS_WEIGHT * alias_overlap
            + _TAG_WEIGHT * tag_overlap
            + _DESCRIPTION_WEIGHT * desc_overlap
        )
        if score > 0:
            scored.append((score, entry))

    scored.sort(key=lambda t: (-t[0], t[1].path))
    ranked = [entry for _, entry in scored[:limit]]
    # Exact source-name mentions supplement candidate discovery only.
    for name, paths in sorted(state._fact_mentions.items()):
        if name and name in passage.text.casefold():
            for path in paths:
                if path in state.index and path not in {entry.path for entry in ranked}:
                    ranked.append(state.index[path])
    return ranked[:limit]
