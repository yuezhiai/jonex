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
    cancel: CancelPort | None = None,
) -> QueryRunResult:
    """Query the published corpus using the runner-selected retrieval policy."""
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
    resolved_route = resolve_route(question, explicit=routing.pop("route", None))
    corpus.route = resolved_route.value
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
