"""Shared model-call / trace-span / token-usage plumbing for the query engine.

lwp-arch-001 INFRA-0: extracted verbatim from
``engines/llm_wiki_plus/query_algorithm.py`` (span timing helpers
CR-TRACE-002, token-usage aggregation, ``_model_complete`` and
``_record_model_span``).  Pure with respect to the environment: only
``ModelPort`` is called plus the system clock for truthful span timing.
The old import path stays available through the re-export shim.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from jonex_wiki.kernel.contracts import (
    QueryEventType,
    TraceSpan,
    TraceSpanType,
)
from jonex_wiki.kernel.ports import ModelPort
from jonex_wiki.kernel.runtime_env import current_env
from jonex_wiki.kernel.telemetry import (current_runtime_telemetry, telemetry_stage)



def _query_thinking(default=None):
    """Keep per-step choices when on; disable every query-agent call when off."""
    mode = current_env().get("JONEX_WIKI_AGENT_THINKING", "on")
    if mode == "on":
        return default
    if mode != "off":
        raise ValueError("JONEX_WIKI_AGENT_THINKING must be on or off")
    return {"type": "disabled"}


async def _model_complete(
    model_port: ModelPort, *args: Any, **kwargs: Any
) -> Dict[str, Any]:
    """Measure one logical model turn with a bounded transient-timeout retry.

    Provider requests that stop producing a response must not occupy a query
    worker until the outer whole-query timeout. Successful calls are returned
    unchanged; only ``asyncio.TimeoutError`` is retried, so schema, truncation,
    authentication and other deterministic failures are never hidden.
    """

    if str(kwargs.get("scene", "")).startswith("jonex_wiki_query_"):
        thinking = _query_thinking(kwargs.get("thinking"))
        if thinking is not None:
            kwargs["thinking"] = thinking
    with telemetry_stage(current_runtime_telemetry(), "model_turn"):
        try:
            timeout = max(0.001, float(current_env().get("QUERY_MODEL_CALL_TIMEOUT_SECONDS", "75")))
        except ValueError:
            timeout = 75.0
        try:
            retries = max(0, min(2, int(current_env().get("QUERY_MODEL_CALL_TIMEOUT_RETRIES", "1"))))
        except ValueError:
            retries = 1
        for attempt in range(retries + 1):
            try:
                response = await asyncio.wait_for(
                    model_port.complete(*args, **kwargs), timeout=timeout
                )
                return response
            except asyncio.TimeoutError:
                if attempt >= retries:
                    raise TimeoutError(
                        "model call timed out after {:.1f}s ({} attempt{})".format(
                            timeout, attempt + 1, "" if attempt == 0 else "s"
                        )
                    )
        raise AssertionError("unreachable model timeout loop")

# ──────────────────────────────────────────────
#  Span timing helpers (CR-TRACE-002)
# ──────────────────────────────────────────────


def _utc_now_iso(dt: Optional[datetime] = None) -> str:
    """ISO-8601 UTC timestamp with milliseconds and a trailing ``Z``.

    This is the public ``started_at`` wire format for trace spans.  It is the
    only clock access in the module and carries no secret or chain-of-thought.
    """
    dt = dt or datetime.now(timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class _SpanBuilder:
    """Sequentially builds deterministic, nested trace spans for one run.

    ``span_id`` follows the frozen contract ``run:<run-id>:span:<n>`` where
    ``n`` is the span ordinal within the run.  Every non-root span references
    an existing ``parent_id`` (the run root by default) so the span tree is
    always valid.
    """

    def __init__(self, run_id: Optional[str] = None) -> None:
        self._run_id = run_id or "unknown"
        self._counter = 0
        self._root_id = self._next_id()
        self._spans: List[TraceSpan] = [
            TraceSpan(
                span_id=self._root_id,
                parent_id=None,
                seq=self._counter,
                type=TraceSpanType.STEP,
                title="query traversal",
                started_at=_utc_now_iso(),
                decision="n/a",
                # Container span — the worker skips spans with no event_type
                # (it emits its own run.started / run.completed bookends).
                event_type=None,
            )
        ]

    @property
    def spans(self) -> List[TraceSpan]:
        return list(self._spans)

    @property
    def root_id(self) -> str:
        return self._root_id

    def _next_id(self) -> str:
        self._counter += 1
        return "run:{}:span:{}".format(self._run_id, self._counter)

    def add(
        self,
        *,
        type_: TraceSpanType,
        title: str,
        event_type: str,
        parent_id: Optional[str] = None,
        tool: Optional[str] = None,
        category: Optional[str] = None,
        started_at: Optional[str] = None,
        duration_ms: Optional[int] = None,
        model: Optional[str] = None,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        input_summary: Optional[str] = None,
        result_summary: Optional[str] = None,
        progress_details: Optional[Dict[str, Any]] = None,
        evidence: Optional[List[Dict[str, Any]]] = None,
        decision: str = "n/a",
    ) -> str:
        span_id = self._next_id()
        span = TraceSpan(
            span_id=span_id,
            parent_id=parent_id if parent_id is not None else self._root_id,
            seq=self._counter,
            type=type_,
            title=title,
            tool=tool,
            category=category,
            started_at=started_at or _utc_now_iso(),
            duration_ms=duration_ms,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            input_summary=input_summary,
            result_summary=result_summary,
            evidence=evidence or [],
            decision=decision,
            event_type=event_type,
        )
        self._spans.append(span)
        from jonex_wiki.kernel.query_progress import publish_query_progress
        progress_event = span.model_dump(mode="json", exclude={"event_type"})
        if progress_details is not None:
            progress_event["details"] = progress_details
        publish_query_progress(progress_event)
        return span_id


def _finalize_token_usage(acc: Dict[str, int]) -> Dict[str, int]:
    """Return the aggregated run token usage, or ``{}`` when no provider ever
    reported usage (never fabricate a 0)."""
    if acc.get("input_tokens") or acc.get("output_tokens"):
        return dict(acc)
    return {}


def _merge_usage(acc: Dict[str, int], raw: Any) -> None:
    """Merge one provider usage dict (``raw.usage``) into the run accumulator."""
    usage = getattr(raw, "usage", None)
    if not isinstance(usage, dict):
        return
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if prompt is not None:
        acc["input_tokens"] += int(prompt)
    if completion is not None:
        acc["output_tokens"] += int(completion)


def _record_model_span(
    spans: _SpanBuilder,
    raw: Any,
    model_name: Optional[str],
    started_dt: datetime,
    duration_ms: int,
    usage_acc: Dict[str, int],
    *,
    title: str = "model call",
    parent_id: Optional[str] = None,
) -> None:
    """Record one ``model.call`` span and merge the provider usage.

    Token fields come only from the Provider usage attached to ``raw``; when
    the provider reported none they stay ``None`` (never fabricated 0).
    """
    _merge_usage(usage_acc, raw)
    usage = getattr(raw, "usage", None)
    input_tokens = output_tokens = None
    if isinstance(usage, dict):
        input_tokens = usage.get("prompt_tokens")
        output_tokens = usage.get("completion_tokens")
    spans.add(
        type_=TraceSpanType.MODEL,
        title=title,
        model=model_name,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        started_at=_utc_now_iso(started_dt),
        duration_ms=duration_ms,
        decision="decide",
        event_type=QueryEventType.MODEL_CALL.value,
        parent_id=parent_id,
    )
