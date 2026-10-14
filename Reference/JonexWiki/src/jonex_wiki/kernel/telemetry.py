"""Per-operation work counters for compile diagnostics.

Provider usage and billing remain with ``MeteredGw``. Work counts and stage
wall times are task-local.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext, contextmanager
from contextvars import ContextVar
from collections import Counter
import time
from functools import wraps
from typing import Any, Optional


class RuntimeTelemetryRecorder:
    """Per-operation work counts; provider billing remains with MeteredGw."""

    def __init__(self) -> None:
        self.work_counts: Counter[str] = Counter()
        self.stage_wall_ms: Counter[str] = Counter()
        self.stage_calls: Counter[str] = Counter()
        self.started_ns = time.perf_counter_ns()
        self.stage_events = []


_active_recorder: ContextVar[Optional[RuntimeTelemetryRecorder]] = ContextVar(
    "jonex_runtime_recorder", default=None
)


def current_runtime_telemetry() -> Optional[RuntimeTelemetryRecorder]:
    return _active_recorder.get()


def telemetry_stage(
    recorder: Optional[RuntimeTelemetryRecorder], name: str
) -> AbstractContextManager:
    """Accumulate inclusive elapsed time, including failed and nested stages."""
    if recorder is None:
        return nullcontext()

    @contextmanager
    def measured():
        started = time.perf_counter_ns()
        try:
            yield
        finally:
            ended = time.perf_counter_ns()
            recorder.stage_wall_ms[name] += (ended - started) / 1_000_000
            recorder.stage_calls[name] += 1
            if len(recorder.stage_events) < 4096 or name == 'compile_total':
                recorder.stage_events.append({'stage':name,
                    'start_ms':round((started-recorder.started_ns)/1_000_000,3),
                    'end_ms':round((ended-recorder.started_ns)/1_000_000,3)})

    return measured()


def timed_async_stage(name):
    def decorate(function):
        @wraps(function)
        async def measured(*args, **kwargs):
            recorder = kwargs.get('telemetry') or current_runtime_telemetry()
            with telemetry_stage(recorder, name):
                return await function(*args, **kwargs)
        return measured
    return decorate




def bind_runtime_telemetry(recorder):
    @contextmanager
    def bound():
        token = _active_recorder.set(recorder)
        try:
            yield
        finally:
            _active_recorder.reset(token)
    return bound()


def count_work(name: str, amount: int = 1) -> None:
    recorder = current_runtime_telemetry()
    if recorder is not None:
        recorder.work_counts[name] += amount






__all__ = [
    "RuntimeTelemetryRecorder",
    "bind_runtime_telemetry",
    "count_work",
    "current_runtime_telemetry",
    "telemetry_stage",
]
