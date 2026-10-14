"""Evidence-bound policy helpers for the LLM-Wiki+ traversal controller.

Pre-decomposition supplies only ordered retrieval directions.  A direction is
not a completed reasoning hop: the controller may advance only after a model
selects a bridge value from a short handle in the current active evidence
window.  The backend resolves and validates that handle, builds the next query
from the supported bridge plus the next direction, and records every decision
as typed control events.

This module is deterministic and has no model, clock, filesystem, environment,
or network access.  Runtime orchestration can therefore validate a complete
transition before publishing any of its events to a trace.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, Iterable, Optional, Tuple

from pydantic import BaseModel, Field, model_validator, field_validator, ConfigDict

from jonex_wiki.domain.engine.query.controller.control_state import (
    ControlEvent,
    ControlState,
    EvidenceWindowUpdated,
    FailureClass,
    FrontierReadObserved,
    HopAdvanced,
    HopStarted,
    RequiredSlot,
    RunTerminal,
    SearchProgressObserved,
    SlotClosed,
    SlotEvidenceBound,
    initial_control_state,
    reduce_control_state,
)
from jonex_wiki.domain.engine.query.controller.evidence_state import QueryState


_HANDLE_RE = re.compile(r"^\[E([1-9][0-9]*)\]$")


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
class BridgeBindingDraft(_FrozenModel):
    """A model-selected bridge grounded only by a visible evidence handle."""

    slot_id: str
    evidence_handle: str
    value: str

    @field_validator("slot_id", "value")
    def _non_blank(cls, value: str, field: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("CONTROL_BRIDGE_DRAFT_INVALID:" + field.field_name)
        return value

    @field_validator("evidence_handle")
    def _valid_handle(cls, value: str) -> str:
        if not isinstance(value, str) or _HANDLE_RE.fullmatch(value) is None:
            raise ValueError("CONTROL_HANDLE_INVALID")
        return value


class ControllerRuntime(_FrozenModel):
    """Immutable runtime cursor over one typed control-state event stream."""

    state: ControlState
    directions: Tuple[str, ...]
    direction_index: int = Field(ge=0)

    @field_validator("directions")
    def _valid_directions(cls, values: Tuple[str, ...]) -> Tuple[str, ...]:
        if not values:
            raise ValueError("CONTROL_DIRECTIONS_EMPTY")
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError("CONTROL_DIRECTION_INVALID")
        normalized = [value.strip().casefold() for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError("CONTROL_DIRECTION_DUPLICATE")
        return values

    @model_validator(mode="after")
    def _cursor_matches_state(self):
        state = self.state
        directions = tuple(getattr(self, "directions", ()))
        index = self.direction_index
        if state is None or index is None or not directions:
            return self
        if index >= len(directions):
            raise ValueError("CONTROL_DIRECTION_CURSOR_INVALID")
        expected_order = tuple(
            "H{}".format(number) for number in range(1, index + 2)
        )
        if state.hop_order != expected_order:
            raise ValueError("CONTROL_DIRECTION_HOPS_MISMATCH")
        return self


def _slot_for_direction(
    index: int,
    directions: Tuple[str, ...],
) -> RequiredSlot:
    hop_id = "H{}".format(index + 1)
    if index < len(directions) - 1:
        return RequiredSlot(
            slot_id=hop_id + ".bridge",
            description=directions[index],
            role="bridge",
            required_for="advance",
        )
    return RequiredSlot(
        slot_id=hop_id + ".answer",
        description=directions[index],
        role="fact",
        required_for="answer",
    )


def _hop_slots(index: int, directions: Tuple[str, ...]) -> Tuple[RequiredSlot, ...]:
    return (_slot_for_direction(index, directions),)


def _runtime(
    runtime: ControllerRuntime,
    *,
    state: ControlState,
    direction_index: Optional[int] = None,
) -> ControllerRuntime:
    """Rebuild through validators instead of Pydantic's unchecked copy update."""

    return ControllerRuntime(
        state=state,
        directions=runtime.directions,
        direction_index=(
            runtime.direction_index
            if direction_index is None
            else direction_index
        ),
    )


def start_controller(
    question: str,
    *,
    directions: Iterable[str] = (),
) -> Tuple[ControllerRuntime, HopStarted]:
    """Start H1 from a direction list or the original single-hop question."""

    if not isinstance(question, str) or not question.strip():
        raise ValueError("CONTROL_QUESTION_INVALID")
    planned = tuple(value.strip() for value in directions)
    if not planned:
        planned = (question.strip(),)
    event = HopStarted(
        hop_id="H1",
        query=planned[0],
        slots=_hop_slots(0, planned),
    )
    state = reduce_control_state(initial_control_state(), event)
    return (
        ControllerRuntime(
            state=state,
            directions=planned,
            direction_index=0,
        ),
        event,
    )


def sync_evidence_window(
    runtime: ControllerRuntime,
    query_state: QueryState,
) -> Tuple[ControllerRuntime, EvidenceWindowUpdated]:
    """Link the controller to one exact authoritative query-state snapshot."""

    event = EvidenceWindowUpdated(
        query_event_count=query_state.event_count,
        active_evidence=query_state.active_evidence(),
        seen_evidence=tuple(
            query_state.seen_evidence_by_id[evidence_id]
            for evidence_id in query_state.seen_evidence_order
        ),
        seen_evidence_order=query_state.seen_evidence_order,
    )
    state = reduce_control_state(runtime.state, event)
    return _runtime(runtime, state=state), event


def observe_search_progress(
    runtime: ControllerRuntime,
    *,
    query: str,
    candidate_paths: Iterable[str],
    unread_candidate_paths: Iterable[str],
) -> Tuple[ControllerRuntime, SearchProgressObserved]:
    """Record one retriever-qualified frontier without another retrieval call."""

    candidates = tuple(dict.fromkeys(candidate_paths))
    unread = tuple(dict.fromkeys(unread_candidate_paths))
    event = SearchProgressObserved(
        hop_id=runtime.state.current_hop_id or "",
        query=query,
        candidate_paths=candidates,
        # The current FTS ranker already defines the accepted candidate
        # set.  CTRL.3 deliberately does not invent a second score threshold.
        high_quality_candidate_paths=candidates,
        unread_candidate_paths=unread,
    )
    state = reduce_control_state(runtime.state, event)
    return _runtime(runtime, state=state), event


def observe_frontier_read(
    runtime: ControllerRuntime,
    paths: Iterable[str],
) -> Tuple[ControllerRuntime, FrontierReadObserved]:
    """Consume successfully read paths from the typed unread frontier."""

    event = FrontierReadObserved(
        hop_id=runtime.state.current_hop_id or "",
        paths=tuple(dict.fromkeys(paths)),
    )
    state = reduce_control_state(runtime.state, event)
    return _runtime(runtime, state=state), event


def _normalize_query_part(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def grounded_next_query(bridge_value: str, direction: str) -> str:
    """Build a deterministic query containing the evidence-supported bridge."""

    bridge = bridge_value.strip()
    planned = direction.strip()
    if not bridge or not planned:
        raise ValueError("CONTROL_NEXT_QUERY_PART_INVALID")
    if _normalize_query_part(bridge) in _normalize_query_part(planned):
        return planned
    return bridge + " " + planned


def _resolve_handle(state: ControlState, handle: str) -> str:
    match = _HANDLE_RE.fullmatch(handle)
    if match is None:
        raise ValueError("CONTROL_HANDLE_INVALID")
    index = int(match.group(1)) - 1
    order = state.seen_evidence_order
    if index >= len(order):
        raise ValueError("CONTROL_HANDLE_UNKNOWN")
    return order[index]


def _reduce_events(
    state: ControlState,
    events: Iterable[ControlEvent],
) -> Tuple[ControlState, Tuple[ControlEvent, ...]]:
    """Validate a complete transition before returning any publishable event."""

    frozen = tuple(events)
    candidate = state
    for event in frozen:
        candidate = reduce_control_state(candidate, event)
    return candidate, frozen


def apply_bridge_binding(
    runtime: ControllerRuntime,
    draft: BridgeBindingDraft,
) -> Tuple[ControllerRuntime, Tuple[ControlEvent, ...]]:
    """Atomically bind/close the current bridge and advance one grounded hop."""

    state = runtime.state
    current_hop = state.hops_by_id[state.current_hop_id or ""]
    slot = current_hop.slot(draft.slot_id)
    if slot.role != "bridge" or slot.required_for != "advance":
        raise ValueError("CONTROL_BRIDGE_SLOT_MISMATCH")
    next_index = runtime.direction_index + 1
    if next_index >= len(runtime.directions):
        raise ValueError("CONTROL_NEXT_DIRECTION_MISSING")

    evidence_id = _resolve_handle(state, draft.evidence_handle)
    record = state.seen_evidence_by_id[evidence_id]
    if _normalize_query_part(draft.value) not in _normalize_query_part(record.excerpt):
        raise ValueError("CONTROL_BRIDGE_VALUE_NOT_IN_EVIDENCE")
    next_query = grounded_next_query(
        draft.value,
        runtime.directions[next_index],
    )
    next_hop_id = "H{}".format(next_index + 1)
    events: Tuple[ControlEvent, ...] = (
        SlotEvidenceBound(
            hop_id=current_hop.hop_id,
            slot_id=slot.slot_id,
            evidence_handle=draft.evidence_handle,
            evidence_id=evidence_id,
            value=draft.value,
        ),
        SlotClosed(hop_id=current_hop.hop_id, slot_id=slot.slot_id),
        HopAdvanced(
            from_hop_id=current_hop.hop_id,
            hop_id=next_hop_id,
            query=next_query,
            slots=_hop_slots(next_index, runtime.directions),
        ),
    )
    reduced, frozen = _reduce_events(state, events)
    return (
        _runtime(runtime, state=reduced, direction_index=next_index),
        frozen,
    )


def answer_binding_events(
    runtime: ControllerRuntime,
    *,
    bindings: Iterable[Tuple[str, str]],
) -> Tuple[ControllerRuntime, Tuple[ControlEvent, ...]]:
    """Atomically bind cited handles and close the single current answer slot."""

    state = runtime.state
    current_hop = state.hops_by_id[state.current_hop_id or ""]
    answer_slots = [
        slot for slot in current_hop.slots if slot.required_for == "answer"
    ]
    if len(answer_slots) != 1:
        raise ValueError("CONTROL_ANSWER_SLOT_MISSING")
    slot = answer_slots[0]
    supplied = tuple(bindings)
    if not supplied:
        raise ValueError("CONTROL_ANSWER_BINDINGS_EMPTY")
    binding_events = tuple(
        SlotEvidenceBound(
            hop_id=current_hop.hop_id,
            slot_id=slot.slot_id,
            evidence_handle=evidence_handle,
            evidence_id=_resolve_handle(state, evidence_handle),
            value=value,
        )
        for evidence_handle, value in supplied
    )
    events: Tuple[ControlEvent, ...] = binding_events + (
        SlotClosed(hop_id=current_hop.hop_id, slot_id=slot.slot_id),
    )
    reduced, frozen = _reduce_events(state, events)
    return _runtime(runtime, state=reduced), frozen


def finish_answered(
    runtime: ControllerRuntime,
) -> Tuple[ControllerRuntime, RunTerminal]:
    """Apply the answered terminal only after every required answer slot closes."""

    event = RunTerminal(status="answered")
    state = reduce_control_state(runtime.state, event)
    return _runtime(runtime, state=state), event


def finish_incomplete(
    runtime: ControllerRuntime,
    failure_class: FailureClass,
) -> Tuple[ControllerRuntime, RunTerminal]:
    """Freeze one explicitly classified incomplete terminal."""

    event = RunTerminal(
        status="incomplete",
        failure_class=failure_class,
    )
    state = reduce_control_state(runtime.state, event)
    return _runtime(runtime, state=state), event


def controller_payload(runtime: ControllerRuntime) -> Dict[str, Any]:
    """Return the model-visible controller view without backend evidence IDs."""

    state = runtime.state
    current = state.hops_by_id[state.current_hop_id or ""]
    hop_baseline = set(current.evidence_ids_at_start)
    # Handles are numbered over the full delivered-material ledger (read order),
    # not over the bounded active window, so a citation never changes identity
    # merely because its evidence left the recent window (READING-REPAIR §1).
    handle_by_id = {
        evidence_id: "[E{}]".format(index + 1)
        for index, evidence_id in enumerate(state.seen_evidence_order)
    }
    active = [
        {
            "handle": handle_by_id[evidence_id],
            "excerpt": state.seen_evidence_by_id[evidence_id].excerpt,
            "eligible_for_current_hop": evidence_id not in hop_baseline,
        }
        for evidence_id in state.active_evidence_ids
    ]
    slots = []
    for slot in current.slots:
        bindings = current.bindings_by_slot.get(slot.slot_id, ())
        slots.append(
            {
                "slot_id": slot.slot_id,
                "description": slot.description,
                "role": slot.role,
                "required_for": slot.required_for,
                "closed": slot.slot_id in current.closed_slot_ids,
                "evidence_handles": [
                    binding.evidence_handle for binding in bindings
                ],
            }
        )
    return {
        "current_hop_id": current.hop_id,
        "current_query": current.query,
        "slots": slots,
        "active_evidence": active,
        "remaining_directions": len(runtime.directions)
        - runtime.direction_index
        - 1,
    }


__all__ = [
    "BridgeBindingDraft",
    "ControllerRuntime",
    "answer_binding_events",
    "apply_bridge_binding",
    "controller_payload",
    "finish_answered",
    "finish_incomplete",
    "grounded_next_query",
    "observe_frontier_read",
    "observe_search_progress",
    "start_controller",
    "sync_evidence_window",
]
