"""Production query orchestration (lwp-arch-001 PROD).

Extracted verbatim from ``engines/llm_wiki_plus/production.py``: the strict
production query runner (``run_query``) and bundle text inspector
(``inspect_bundle``).  Calls only the injected ``ModelPort`` plus the query
core; never touches the network, a clock, a database or object storage.  The
old import path stays available through the re-export shim.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from jonex_wiki.kernel.contracts import QueryStrategy
from jonex_wiki.domain.engine.models.compiler_state import CompilerState
from jonex_wiki.domain.engine.compile.content_validation import ValidatedClaim
from jonex_wiki.domain.engine.compile.bundle import decode_bundle, sha256_hex
from jonex_wiki.kernel.ports import CancelPort, ModelPort
from jonex_wiki.domain.engine.models.query_models import TraceEntry
from jonex_wiki.domain.engine.query.router import Route, resolve_route
from jonex_wiki.domain.engine.query.retrieval import build_query_corpus
from jonex_wiki.kernel.telemetry import (
    current_runtime_telemetry,
    telemetry_stage,
)


# ──────────────────────────────────────────────
#  Query and inspect
# ──────────────────────────────────────────────


async def run_query(
    model: ModelPort,
    state: CompilerState,
    claims: List[ValidatedClaim],
    question: str,
    strategy: QueryStrategy,
    routing: Dict[str, Any],
    route: Optional[Union[str, Route]] = None,
    cancel: CancelPort | None = None,
) -> QueryRunResult:
    """Build the query corpus and run the enforced strict traversal.

    LWP-RET-002: ``route`` is the per-query long-document routing override
    (default ``None`` = auto).  An explicit override may also arrive through the
    routing dict under ``_route_override`` (following the ``_published_*`` pop
    precedent); otherwise the question is classified deterministically by
    ``query_router.resolve_route``.  The resolved route lives on the corpus for
    the traversal controller and on ``result.config``/trace afterwards — never
    in effective-config.  The default auto->proposition path keeps the hybrid
    trace byte-identical.
    """
    routing = dict(routing)
    prepared_corpus = routing.pop("_prepared_corpus", None)
    if prepared_corpus is None:
        with telemetry_stage(current_runtime_telemetry(), "render"):
            corpus = build_query_corpus(state, claims)
    else:
        # Per-query routing are assigned on a detached envelope. No
        # query state is stored in the shared publication maps.
        corpus = prepared_corpus.model_copy(deep=False)
    lexical_index = routing.pop("_published_lexical_index", None)
    frozen_indexes = lexical_index is not None or routing.get("bundle_view") is not None
    # LWP-RET-002: engine-side query-intent classification (zero API/DB
    # changes, nothing persisted).  Explicit override flows through the routing
    # dict pop (the same pop precedent as ``_published_lexical_index`` above)
    # or the ``route`` argument; the default derives from the question via the
    # pure router.
    route_override = routing.pop("_route_override", None)
    if route is None:
        route = route_override
    resolved_route = resolve_route(question, route)
    # Raw-source fallback axis (P0 #1): an injected on-demand single-segment
    # reader plus an explicit mode ("off" default | "production" | "matched").
    # Nothing here re-injects the source archive into the corpus.
    raw_fallback_mode = routing.pop("_raw_fallback_mode", "off")
    raw_source_reader = routing.pop("_raw_source_reader", None)
    corpus.route = resolved_route.value
    corpus.raw_fallback_mode = raw_fallback_mode
    corpus.raw_source_reader = raw_source_reader
    if frozen_indexes:
        routing["lexical_index"] = lexical_index
        routing["frozen_indexes"] = True
    # Read-through seam: tests patch ``query.algorithm.run_query_algorithm``;
    # resolve at call time so the patch stays effective.
    from jonex_wiki.domain.engine.query.algorithm import run_query_algorithm

    result = await run_query_algorithm(model, corpus, question, strategy, cancel=cancel, **routing)
    # LWP-RET-002 post-processing: route goes into run.config/trace (D-7),
    # never effective-config.  Proposition/auto adds no trace event, keeping
    # the default-stack trace byte-identical to the pre-route hybrid.
    result.config["route"] = resolved_route.value
    result.config["raw_source_fallback_count"] = sum(
        1 for entry in result.trace if entry.event == "raw_source_fallback"
    )
    if resolved_route != Route.PROPOSITION:
        detail: Dict[str, Any] = {"route": resolved_route.value}
        result.trace.append(TraceEntry(event="route_selected", detail=detail))
    # The query already answered the requested scope from verified evidence.
    # Citation excerpts are previews, not a replacement for that answer.
    probes = [entry for entry in result.trace if entry.event == "coverage_delivery_probe"]
    matched = any(entry.detail.get("matched") for entry in probes)
    result.trace.append(TraceEntry(event="coverage_diagnosis", detail={
        "version": "delivery-observation-v2", "status": "delivered" if matched else "semantic_coverage_not_proven",
        "observation_method": "lexical_probe_of_actual_tool_payload",
        "layers": {"source_archived": None, "page_expressed": None,
                   "evidence_attached": None, "query_delivered": True if matched else None},
        "semantic_coverage": "not_proven", "candidate_count": 0}))
    return result


def inspect_bundle(bundle_bytes: bytes) -> Dict[str, str]:
    """Return text artifacts plus safe summaries for binary index artifacts."""
    decoded = decode_bundle(bundle_bytes)
    tree: Dict[str, str] = {}
    for rel in sorted(decoded.files):
        try:
            tree[rel] = decoded.files[rel].decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            tree[rel] = "<binary bytes={} sha256={}>".format(
                len(decoded.files[rel]), sha256_hex(decoded.files[rel])
            )
    return tree
