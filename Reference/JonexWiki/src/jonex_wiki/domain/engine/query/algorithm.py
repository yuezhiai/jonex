"""Shared publication compatibility and result types for agent queries.

The former Wiki tool traversal has been retired. The corpus/index helpers
remain because compiling and opening existing Bundles still use them.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jonex_wiki.domain.engine.models.query_models import TraceEntry, VerifiedCitation
from jonex_wiki.domain.engine.query.retrieval import (
    _TOKEN_RE,
    _open_fts_index,
    build_fts_index_bytes,
    build_query_corpus,
)
from jonex_wiki.kernel.contracts import TraceSpan

# Existing Bundle runtime snapshots include this value. Preserve it for
# compatibility; agent writer/skill identity is tracked separately.
TOOL_CHECKSUM = "04e6a6c6516f380916c2374e5faf62ea0255fe36ee7f01ec3c63cb480fd963b7"


class FailureClass(str, Enum):
    """Stable failure categories used in stored query results."""

    COMPILE_MISSING = "compile_missing"
    RETRIEVAL_MISS = "retrieval_miss"
    REASONING_MISS = "reasoning_miss"
    ANSWER_FORMAT_MISS = "answer_format_miss"
    EVIDENCE_STATE_MISS = "evidence_state_miss"


def _segmenter_available() -> bool:
    import jieba  # noqa: F401

    return True


def _segment_for_search(text: str) -> str:
    try:
        import jieba

        return " ".join(
            part for part in (word.strip() for word in jieba.cut(text)) if part
        )
    except Exception:
        return " ".join(_TOKEN_RE.findall(text.casefold()))


class QueryRunResult(BaseModel):
    """Auditable result of one file-reading agent query."""

    answer: str
    citations: List[VerifiedCitation] = Field(default_factory=list)
    unverified_citations: List[Dict[str, Any]] = Field(default_factory=list)
    incomplete: bool
    incomplete_reason: Optional[str] = None
    recovery_notes: Optional[str] = None
    incomplete_scope: Optional[Dict[str, Any]] = None
    grounded_incomplete_facts: List[Dict[str, Any]] = Field(default_factory=list)
    search_calls: int
    read_calls: int
    tool_calls: int
    judge_calls: int = 0
    model_calls: int = 0
    stop_reason: str
    failure_class: Optional[FailureClass] = None
    trace: List[TraceEntry] = Field(default_factory=list)
    spans: List[TraceSpan] = Field(default_factory=list)
    token_usage: Dict[str, int] = Field(default_factory=dict)
    config: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")

    @field_validator("search_calls", "read_calls", "tool_calls", "model_calls")
    @classmethod
    def _non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("counts must be non-negative")
        return value

    @model_validator(mode="after")
    def _failure_only_on_incomplete(self):
        if not self.incomplete and self.failure_class is not None:
            raise ValueError("complete query result cannot have failure_class")
        return self
