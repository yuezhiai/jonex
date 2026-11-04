"""Version-bound table inspection, literal location and scoped reading.

This module deliberately exposes no SQL, aggregation, sorting, joins or
expression language.  It runs fixed parameterised statements over the source
grid and returns explicit structure/range/context coverage states.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from dataclasses import replace
from datetime import date
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Literal, Mapping, Optional, Sequence

from pydantic import (BaseModel, Field, ConfigDict, field_validator,
                      model_validator, ValidationInfo)

from jonex_wiki.domain.engine.compile.source_prep.header_interpretation import (
    HEADER_RULE_VERSION,
    interpret_table,
)
from jonex_wiki.domain.engine.compile.source_prep.spreadsheet_view import excel_serial_iso


def _env_int(name: str, default: int) -> int:
    try:
        value = int(str(os.environ.get(name, "")).strip() or default)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


# One constant used to serve three different jobs - the size of one returned
# page, the size of one request's merged evidence, and the size of the relation
# judge's input - so that a page could never grow without also loosening the
# judge.  They are now separate: ``READ_RESPONSE_LIMIT`` keeps its original
# meaning (the judge input ceiling) and the page budget is its own knob.
READ_RESPONSE_LIMIT = 32 * 1024
MAX_MERGED_PAGES = 4
MISSING_RANGE_LIMIT = 32
MAX_PAGE_CELLS = 1600
# A page is assembled from the same verbose per-cell records as before; the
# delivery layer is what renders them compactly.  So the page budget may be
# spent on more rows without changing what the model is charged: the encoded
# page is internal, and only the compact delivery reaches a message.
PAGE_BYTES_LIMIT = _env_int("QUERY_MAX_PAGE_BYTES", READ_RESPONSE_LIMIT)
# An exhaustive scope ("read the whole table") is read to the end in one call:
# the page and cell budgets are raised for it, and the driver keeps the model
# context bounded by the *delivered* size instead.
EXHAUSTIVE_PAGE_BYTES = _env_int("QUERY_EXHAUSTIVE_PAGE_BYTES", 4 * 1024 * 1024)
EXHAUSTIVE_MAX_CELLS = _env_int("QUERY_EXHAUSTIVE_MAX_CELLS", 40000)
EXHAUSTIVE_PAGE_LIMIT = _env_int("QUERY_EXHAUSTIVE_PAGE_LIMIT", 12)


def scope_is_exhaustive(scope: Any) -> bool:
    """True when a scope *asks* for the whole table.

    This is an intent test only: it never claims the read succeeded.  Whether
    every requested cell really arrived is a separate fact
    (:func:`request_is_exhaustive`), and the two must not be conflated - an
    exhaustive intent whose read stopped early is exactly the case that used to
    be reported as if it were complete.
    """
    if not isinstance(scope, Mapping):
        scope = getattr(scope, "__dict__", {}) or {}
    constraints = scope.get("constraints") or {}
    row_keys = [key for key in ROW_WINDOW_KEYS if key in constraints]
    return not constraints and not row_keys


def request_is_exhaustive(request: Mapping[str, Any]) -> bool:
    """True when a completed request really saw every requested cell."""
    if str(request.get("status") or "") != "complete":
        return False
    expected = request.get("expected") or {}
    cell_count = expected.get("cell_count")
    if cell_count is None:
        return False
    return len(request.get("seen_cells") or {}) == int(cell_count)


# Row-window keys: the reader's own vocabulary for a row block
# (`bounds.row_start`, `missing_ranges[].row_start`) is what a model naturally
# asks for, so a window may arrive either as scope fields or as constraint keys
# in any of these spellings.
ROW_WINDOW_KEYS = ("row_start", "row_end", "rows", "row_range", "row_window")


class ScopeSpec(BaseModel):
    table_id: str
    constraints: Dict[str, str] = Field(default_factory=dict)
    target_fields: list[str] = Field(default_factory=list)
    task_type: Literal["lookup", "enumerate", "explain"] = "lookup"
    require_all_matches: bool = True
    include_hidden: bool = False
    # An explicit row window.  Everything else in ``constraints`` filters rows
    # by field value; this bounds which rows are considered at all, which is
    # how a caller pages through a long table by row block.
    row_start: Optional[int] = None
    row_end: Optional[int] = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("table_id")
    def _table_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("table_id must be non-blank")
        return value

    @field_validator("row_end")
    def _row_window(cls, value: Optional[int], info: ValidationInfo) -> Optional[int]:
        start = info.data.get("row_start")
        if value is None and start is None:
            return None
        if value is None or start is None:
            raise ValueError("TABLE_ROW_WINDOW_INCOMPLETE")
        if int(start) < 1 or int(value) < int(start):
            raise ValueError("TABLE_ROW_WINDOW_INVALID")
        return int(value)


class RawScopeSpec(BaseModel):
    """An explicit coordinate rectangle for a raw read.

    There is deliberately no field name, filter, target-field or aggregation
    parameter here: raw mode proves the stored content at known coordinates and
    nothing about which period, unit or metric a value belongs to.
    """

    table_id: str
    row_start: int
    row_end: int
    column_start: Optional[int] = None
    column_end: Optional[int] = None
    include_hidden: bool = False

    model_config = ConfigDict(extra="forbid")

    @field_validator("table_id")
    def _table_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("table_id must be non-blank")
        return value

    @field_validator("row_start", "row_end", "column_start", "column_end")
    def _positive(cls, value: Optional[int]) -> Optional[int]:
        if value is None:
            return None
        if isinstance(value, bool) or int(value) < 1:
            raise ValueError("TABLE_RAW_RANGE_INVALID")
        return int(value)

    @model_validator(mode="after")
    def _rectangle(self) -> "RawScopeSpec":
        if self.row_end < self.row_start:
            raise ValueError("TABLE_RAW_RANGE_INVALID")
        if (self.column_start is None) != (self.column_end is None):
            raise ValueError("TABLE_RAW_COLUMN_RANGE_INCOMPLETE")
        if self.column_start is not None and self.column_end < self.column_start:
            raise ValueError("TABLE_RAW_RANGE_INVALID")
        return self


def normalize_row_window(scope: ScopeSpec) -> tuple[ScopeSpec, Optional[tuple[int, int]]]:
    """Lift a row window out of ``constraints`` and return (scope, window).

    Accepts ``row_start``/``row_end`` (the spelling the artifact itself uses)
    and the compact forms ``rows``/``row_range``/``row_window`` such as
    ``"41-45"``.  A window that cannot be parsed is left in ``constraints`` so
    the normal unknown-field rejection reports it.
    """
    start, end = scope.row_start, scope.row_end
    remaining: Dict[str, str] = {}
    for name, value in scope.constraints.items():
        key = name.strip().lower()
        if start is None and key in ("row_start", "row_from") and str(value).strip().isdigit():
            start = int(str(value).strip())
            continue
        if end is None and key in ("row_end", "row_to") and str(value).strip().isdigit():
            end = int(str(value).strip())
            continue
        if start is None and end is None and key in ("rows", "row_range", "row_window"):
            parsed = _parse_row_window(str(value))
            if parsed is not None:
                start, end = parsed
                continue
        remaining[name] = value
    if start is None or end is None:
        # A lone bound is not a window: keep the original keys so the caller
        # sees an explicit rejection instead of a silently widened read.
        if scope.row_start is None and scope.row_end is None:
            return scope, None
        if start is None or end is None:
            merged = dict(scope.constraints)
            if start is not None:
                merged.setdefault("row_start", str(start))
            if end is not None:
                merged.setdefault("row_end", str(end))
            return scope.model_copy(update={"constraints": merged, "row_start": None, "row_end": None}), None
    if int(start) < 1 or int(end) < int(start):
        return scope, None
    normalized = scope.model_copy(update={"constraints": remaining, "row_start": int(start),
                                          "row_end": int(end)})
    return normalized, (int(start), int(end))


def _parse_row_window(text: str) -> Optional[tuple[int, int]]:
    match = re.fullmatch(r"\s*(\d+)\s*(?:-|–|~|\.\.|,|:)\s*(\d+)\s*", text or "")
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def _payload_json(interpretation: Mapping[str, Any] | None) -> Dict[str, Any]:
    """Read an interpretation payload; an unreadable payload is not evidence."""
    if interpretation is None:
        return {}
    try:
        payload = json.loads(interpretation["payload_json"] or "{}")
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _excerpt(text: str, limit: int) -> tuple[str, bool]:
    """Return the text, or an explicitly marked excerpt beyond ``limit`` bytes."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, False
    return encoded[:limit].decode("utf-8", errors="ignore"), True


def _cursor(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    envelope = {"payload": payload, "digest": hashlib.sha256(raw).hexdigest()}
    return base64.urlsafe_b64encode(json.dumps(envelope, ensure_ascii=False, sort_keys=True,
                                               separators=(",", ":")).encode()).decode()


def _decode_cursor(value: str) -> Dict[str, Any]:
    try:
        envelope = json.loads(base64.urlsafe_b64decode(value.encode()))
        payload = envelope["payload"]
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        if envelope["digest"] != hashlib.sha256(raw).hexdigest():
            raise ValueError
        return payload
    except Exception as exc:
        raise ValueError("TABLE_CURSOR_INVALID") from exc


class TableReader:
    def __init__(self, source: Path | str | Any, *, revision_id: str,
                 bundle_path: str | None = None) -> None:
        self.source = source
        self.revision_id = revision_id
        self.bundle_path = bundle_path

    @contextmanager
    def _connection(self):
        if hasattr(self.source, "open_sqlite"):
            if not self.bundle_path:
                raise ValueError("bundle_path is required for BundleView table reads")
            connection = self.source.open_sqlite(self.bundle_path)
        else:
            path = Path(self.source).expanduser().resolve()
            connection = sqlite3.connect("file:{}?mode=ro&immutable=1".format(path), uri=True)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    # One raw observation is returned whole up to this size; beyond it the
    # caller gets an explicitly marked excerpt instead of an unbounded string.
    RAW_OBSERVATION_LIMIT = 32 * 1024

    # A read-time header interpretation reads the header region only: the
    # header and its unit note live in the first rows, and scanning a whole
    # large grid to name its columns would make every inspection cost grow with
    # the table.  The rules the interpretation applies are bounded by
    # ``header_interpretation.MAX_HEADER_ROWS`` anyway.
    RUNTIME_HEADER_ROWS = 3
    # Verifying a legacy grid's rectangle by counting cells is bounded as well;
    # above this the stored interpretation status is the only evidence there is
    # and the grid completeness stays unverified rather than assumed.
    RUNTIME_GRID_SCAN_ROWS = 2000

    def _cell_text(self, connection, sheet_id: str, row_index: int, column_index: int) -> str:
        row = connection.execute(
            "SELECT raw_value,cached_value,formula FROM cells "
            "WHERE sheet_id=? AND row_index=? AND column_index=?",
            (sheet_id, row_index, column_index)).fetchone()
        if row is None:
            return ""
        if row["formula"] and row["cached_value"] is not None:
            return str(row["cached_value"])
        return "" if row["raw_value"] is None else str(row["raw_value"])

    def _grid_completeness(self, connection, table, stored_status: str) -> tuple[bool, str]:
        """Whether the stored grid is a complete rectangle, and on what evidence.

        A stored ``accepted`` reading already means the compile confirmed a
        complete rectangle; anything else is verified cell by cell while the
        grid is small enough for that to stay cheap.
        """
        row_count = int(table["row_end"]) - int(table["row_start"]) + 1
        width = int(table["column_end"]) - int(table["column_start"]) + 1
        if row_count <= 0 or width <= 0:
            return False, "grid_bounds_empty"
        if stored_status == "accepted":
            return True, "stored_status"
        if row_count > self.RUNTIME_GRID_SCAN_ROWS:
            return False, "grid_completeness_unverified"
        counts = {int(row["row_index"]): int(row["cells"])
                  for row in connection.execute(
                      "SELECT row_index, COUNT(*) AS cells FROM cells WHERE sheet_id=? "
                      "AND row_index BETWEEN ? AND ? GROUP BY row_index",
                      (table["sheet_id"], table["row_start"], table["row_end"])).fetchall()}
        complete = all(counts.get(row_index) == width
                       for row_index in range(int(table["row_start"]), int(table["row_end"]) + 1))
        return complete, "cell_counts"

    def _runtime_interpretation(self, connection, table, payload: Mapping[str, Any],
                                stored_interpretation_id):
        """Interpret a frozen Bundle's grid under the current header rule.

        Read-only by construction: the Bundle is never rewritten, and the
        derived reading carries its own identity, so a request, cursor or cache
        bound to the stored reading can never be replayed against it.  Returns
        ``None`` when the stored reading was already produced by this rule.
        """
        if payload.get("header_rule") == HEADER_RULE_VERSION:
            return None
        row_start, column_start = int(table["row_start"]), int(table["column_start"])
        last_scanned = min(int(table["row_end"]), row_start + self.RUNTIME_HEADER_ROWS - 1)
        rows = [[self._cell_text(connection, table["sheet_id"], row_index, column_index)
                 for column_index in range(column_start, int(table["column_end"]) + 1)]
                for row_index in range(row_start, last_scanned + 1)]
        localized = []
        if rows:
            for merge in connection.execute(
                    "SELECT row_start,column_start,row_end,column_end FROM merges "
                    "WHERE sheet_id=? AND row_end>=? AND row_start<=?",
                    (table["sheet_id"], row_start, last_scanned)).fetchall():
                localized.append((int(merge["row_start"]) - row_start + 1,
                                  int(merge["column_start"]) - column_start + 1,
                                  int(merge["row_end"]) - row_start + 1,
                                  int(merge["column_end"]) - column_start + 1))
        complete, evidence = self._grid_completeness(connection, table, table["status"])
        interpretation = interpret_table(
            rows, merges=localized, table_id=table["table_id"],
            grid_complete=complete, grid_rows_total=int(table["row_end"]) - row_start + 1,
            stored_interpretation_id=stored_interpretation_id, source="runtime")
        return replace(interpretation, grid=replace(interpretation.grid, evidence=evidence))

    def inspect(self, table_id: str) -> Dict[str, Any]:
        with self._connection() as connection:
            table = connection.execute("SELECT * FROM logical_tables WHERE table_id=?", (table_id,)).fetchone()
            if table is None:
                raise KeyError("TABLE_ID_UNKNOWN")
            interpretation = connection.execute(
                "SELECT * FROM interpretations WHERE table_id=? ORDER BY version DESC LIMIT 1", (table_id,)).fetchone()
            payload = _payload_json(interpretation)
            stored_id = None if interpretation is None else interpretation["interpretation_id"]
            stored_fields = [] if interpretation is None else connection.execute(
                "SELECT field_id,column_index,header_row,label FROM field_map WHERE interpretation_id=? ORDER BY column_index",
                (interpretation["interpretation_id"],)).fetchall()
            # A reading produced by an older header rule is re-interpreted in
            # memory from the frozen grid and its own merge ranges; the stored
            # reading itself is never rewritten.
            runtime = self._runtime_interpretation(connection, table, payload, stored_id)
            if runtime is None:
                interpretation_id = stored_id
                stored_status = "unknown" if interpretation is None else interpretation["status"]
                grid_state = dict(payload.get("grid") or {
                    "present": bool(stored_fields), "complete": stored_status == "accepted",
                    "reason": None, "row_count": table["row_end"] - table["row_start"] + 1,
                    "column_count": table["column_end"] - table["column_start"] + 1,
                    "evidence": "stored_reading"})
                header_state = dict(payload.get("header") or {
                    "confirmed": stored_status == "accepted", "reason": None,
                    "structure": "unconfirmed", "header_rows": [],
                    "unit": "", "unit_row": None, "unit_evidence": None,
                    "ambiguities": [], "disambiguation": None, "fields": []})
                header_state.setdefault("evidence", "stored_reading")
                # A reading written by the shared rule carries its own field
                # paths and units; the field_map rows are the fallback for a
                # Bundle written before the rule existed.  The stored payload
                # names a field's column ``column``, so it is exposed under the
                # reader's canonical ``column_index`` as well.
                stored_header_fields = []
                for field in header_state.get("fields") or []:
                    item = dict(field)
                    item.setdefault("column_index", item.get("column"))
                    stored_header_fields.append(item)
                fields = stored_header_fields or [dict(row) for row in stored_fields]
            else:
                interpretation_id = runtime.identity()
                stored_status = "unknown" if interpretation is None else interpretation["status"]
                grid_state = dict(runtime.grid.payload(), evidence=runtime.grid.evidence,
                                  source="runtime_read")
                header_state = dict(runtime.header.payload(), source="runtime_read")
                fields = [{"field_id": field.field_id, "column_index": field.column,
                           "header_row": field.header_row, "label": field.label,
                           "path": list(field.path), "header_rows": list(field.header_rows),
                           "unit": field.unit, "label_source": field.label_source}
                          for field in runtime.header.fields]
            grid_complete = bool(grid_state.get("complete"))
            header_confirmed = bool(header_state.get("confirmed"))
            # The three questions are answered separately: a complete grid makes
            # the raw coordinates addressable, only a confirmed header makes a
            # value addressable by field name.
            readable = grid_complete and header_confirmed
            capabilities = ["inspect"]
            if grid_complete:
                capabilities.append("locate_text")
            if readable:
                capabilities.append("read_range")
            sample = connection.execute(
                "SELECT row_index,column_index,coordinate,raw_type,raw_value,formula,cached_value,number_format,date_candidate,empty_kind "
                "FROM cells WHERE sheet_id=? AND row_index BETWEEN ? AND ? AND column_index BETWEEN ? AND ? "
                "ORDER BY row_index,column_index LIMIT 200",
                (table["sheet_id"], table["row_start"], min(table["row_end"], table["row_start"] + 20),
                 table["column_start"], table["column_end"])).fetchall()
            header_row = max((field["header_row"] for field in fields), default=table["row_start"])
            captions, _footnotes = self._observations(connection, table_id)
            header_merges = connection.execute(
                "SELECT row_start,column_start,row_end,column_end,anchor FROM merges "
                "WHERE sheet_id=? AND row_start<=? AND row_end>=? ORDER BY row_start,column_start",
                (table["sheet_id"], header_row, table["row_start"]),
            ).fetchall()
            sample_cells = [dict(row) for row in sample]
            raw_text = payload.get("raw_observation_text") or ""
            if readable:
                # The grid is readable, so the preview is a locator, not the
                # data: keep a small sample and a short raw excerpt, and mark
                # both so nothing is mistaken for the whole table.
                sample_cells, sample_cut = _bounded_preview_cells(sample_cells)
                raw_text, raw_cut = _bounded_text(raw_text, INSPECT_RAW_PREVIEW_LIMIT)
                raw_truncated = bool(raw_cut)
            else:
                raw_text, raw_truncated = _excerpt(raw_text, self.RAW_OBSERVATION_LIMIT)
                sample_cut = False
            if readable:
                note = ("结构已确认；用 table_read 读取回答问题所需的完整范围，sample 仅用于定位。")
            elif not grid_complete:
                note = ("原始网格不存在或不完整，禁止按字段名取值；用 wiki_read 读取 "
                        "wiki_page_path 的导航预览，预览可能截断，缺失内容不代表原文不存在。")
            else:
                note = ("原始网格完整但表头/单位未确认，禁止按字段名 table_read；"
                        "可按原始坐标读取，或用 wiki_read 读取导航预览。")
            return _fit_inspection({
                "revision_id": self.revision_id,
                "table_id": table_id,
                "sheet_id": table["sheet_id"],
                "title": table["title"],
                "bounds": {"row_start": table["row_start"], "column_start": table["column_start"],
                           "row_end": table["row_end"], "column_end": table["column_end"]},
                "interpretation_id": interpretation_id,
                "interpretation_status": "accepted" if readable else (
                    "unknown" if interpretation is None and runtime is None else "ambiguous"),
                "stored_interpretation_id": stored_id,
                "stored_interpretation_status": stored_status,
                "header_rule": HEADER_RULE_VERSION,
                # Whether a raw grid exists and is a complete rectangle...
                "grid_state": grid_state,
                # ...whether the header and the unit it declares are confirmed...
                "header_state": header_state,
                # ...and whether this request was delivered completely is decided
                # by the paging layer of each read, never here.
                "fields": fields,
                "stored_fields": [dict(row) for row in stored_fields],
                # The source's own captions for this table, already stored in
                # table_observations.  They are the table's name in the
                # document, so an inspection carries them instead of leaving
                # the caller to guess an identity from a search snippet.
                "captions": captions,
                "raw_observation_text": raw_text,
                "raw_observation_truncated": raw_truncated,
                "sample_truncated": sample_cut,
                "structure_reason": payload.get("structure_reason"),
                "coordinate_space": payload.get("coordinate_space"),
                "page_number": payload.get("page_number"),
                "region": payload.get("bbox"),
                "header_merges": [dict(row) for row in header_merges],
                "hidden_policy": table["hidden_policy"],
                # A table whose structure is not confirmed exposes inspection
                # only: its raw text is readable, its grid is not claimable.
                "capabilities": capabilities,
                "wiki_page_path": "tables/{}.md".format(table_id),
                "next_step": {"tool": "table_read" if readable else "wiki_read", "note": note},
                "sample": sample_cells,
                "limitations": ["no_aggregation", "no_sort", "no_join", "no_formula_recalculation"],
            }, READ_RESPONSE_LIMIT)

    # A raw page carries the same per-cell payload as a structured page, so it
    # keeps the same cell and page budgets: raw mode must not become the way to
    # bypass the delivery budget.
    RAW_MAX_CELLS = 800

    def read_raw(self, scope: "RawScopeSpec | Mapping[str, Any]", *, cursor: str | None = None,
                 max_cells: int = RAW_MAX_CELLS,
                 page_bytes: int | None = None) -> Dict[str, Any]:
        """Read explicit coordinates as raw content, never as confirmed semantics.

        This is the same ``table_read`` tool in its raw mode, not a second table
        reader: it resolves no field name, applies no filter and aggregates
        nothing.  Every delivery states what it proves (the stored content at
        the delivered coordinates, plus which of them inherit a merge anchor)
        and what it does not (that a value belongs to a period, that it is the
        metric a column names, or that the range is complete for a computation).
        Paging walks the requested rectangle in row-major order: the cursor
        carries the next cell index, and because the page size is a delivery
        decision rather than part of the request identity, a caller may continue
        with a different page budget.  The coordinates that were not delivered
        are reported, never implied.
        """
        scope = scope if isinstance(scope, RawScopeSpec) else RawScopeSpec.model_validate(scope)
        inspect = self.inspect(scope.table_id)
        bounds = inspect["bounds"]
        row_start, row_end = int(scope.row_start), int(scope.row_end)
        column_start = int(scope.column_start or bounds["column_start"])
        column_end = int(scope.column_end or bounds["column_end"])
        page_budget = max(1, int(max_cells))
        request_id = _sha256_json({
            "revision_id": self.revision_id, "table_id": scope.table_id,
            "interpretation_id": inspect.get("interpretation_id"), "mode": "raw",
            "requested_scope": scope.model_dump()})
        binding = {"request_id": request_id, "revision_id": self.revision_id,
                   "table_id": scope.table_id,
                   "interpretation_id": inspect.get("interpretation_id"),
                   "scope_sha256": _sha256_json(scope.model_dump()), "mode": "raw"}
        page_index, start_index = 0, 0
        if cursor:
            payload = _decode_cursor(cursor)
            if any(payload.get(key) != value for key, value in binding.items()):
                raise ValueError("TABLE_CURSOR_BINDING_MISMATCH")
            page_index = int(payload.get("page_index", 0))
            start_index = int(payload.get("next_index", 0))
        if page_index < 0 or start_index < 0:
            raise ValueError("TABLE_CURSOR_OUT_OF_RANGE")

        grid = inspect.get("grid_state") or {}
        out_of_bounds = (row_start < int(bounds["row_start"]) or row_end > int(bounds["row_end"])
                         or column_start < int(bounds["column_start"])
                         or column_end > int(bounds["column_end"]))
        base = {"source_version": self.revision_id, "table_id": scope.table_id,
                "sheet_id": inspect.get("sheet_id"),
                "interpretation_id": inspect.get("interpretation_id"),
                "request_id": request_id, "requested_scope": scope.model_dump(),
                "mode": "raw"}
        if not grid.get("present") or out_of_bounds:
            reasons = []
            if not grid.get("present"):
                reasons.append("TABLE_RAW_NO_GRID")
            if out_of_bounds:
                reasons.append("TABLE_RAW_RANGE_OUT_OF_BOUNDS")
            return {**base, "page_index": 0, "start_cell_index": 0, "cells": [],
                    "context_items": [], "footnote_refs": [], "header_context": [],
                    "delivered_cell_count": 0, "requested_cell_count": None,
                    "missing_cell_count": None, "coverage_summary_scope": "current_response",
                    "has_more": False, "next_cursor": None, "ambiguities": reasons,
                    "raw_delivery": self._raw_delivery_block(inspect, [], out_of_bounds),
                    "coverage": {"structure_status": "range_out_of_bounds" if out_of_bounds
                                 else "no_grid", "range_status": "unknown",
                                 "context_status": "unresolved", "relation_status": "insufficient",
                                 "requested_scope": scope.model_dump(),
                                 "read_scope": {"cell_count": 0},
                                 "missing_ranges": [{"row_start": row_start, "row_end": row_end,
                                                     "column_start": column_start,
                                                     "column_end": column_end}],
                                 "missing_ranges_truncated": False,
                                 "rectangle_status": "partial", "pages_delivered": 1,
                                 "complete_for_read_request": False, "logical_unit_complete": False}}

        with self._connection() as connection:
            table = connection.execute(
                "SELECT * FROM logical_tables WHERE table_id=?", (scope.table_id,)).fetchone()
            header_rows = self._header_rows(inspect, table["row_start"])
            header_context = self._raw_header_context(connection, inspect, table, header_rows,
                                                      column_start, column_end)
            captions, footnotes = self._observations(connection, scope.table_id)
            notes = self._table_notes(connection, table, max(header_rows))
            hidden_rows = self._hidden_rows(connection, table["sheet_id"])
            hidden_columns = self._hidden_columns(connection, table["sheet_id"])
            readable_columns = [column for column in range(column_start, column_end + 1)
                                if scope.include_hidden or column not in hidden_columns]
            candidate_rows = [row for row in range(row_start, row_end + 1)
                              if scope.include_hidden or row not in hidden_rows]
            candidates = [(row, column) for row in candidate_rows
                          for column in readable_columns]
            total_cells = len(candidates)
            if start_index > total_cells:
                raise ValueError("TABLE_CURSOR_OUT_OF_RANGE")
            limit = page_bytes or READ_RESPONSE_LIMIT

            def assemble(count: int) -> Dict[str, Any]:
                window = candidates[start_index:start_index + max(0, count)]
                cells = [self._raw_cell(connection, table, row, column)
                         for row, column in window]
                next_index = start_index + len(window)
                pending = candidates[next_index:]
                missing_ranges, missing_truncated = _missing_ranges(pending, MISSING_RANGE_LIMIT)
                complete = page_index == 0 and next_index >= total_cells
                return {**base, "page_index": page_index, "start_cell_index": start_index,
                        "cells": cells, "context_items": [],
                        "footnote_refs": self._raw_footnote_refs(scope.table_id, captions,
                                                                 footnotes, notes),
                        "header_context": header_context,
                        "delivered_cell_count": len(cells),
                        "requested_cell_count": total_cells,
                        "missing_cell_count": total_cells - len(cells),
                        "coverage_summary_scope": "current_response",
                        "has_more": next_index < total_cells,
                        "next_cursor": (None if next_index >= total_cells else _cursor(
                            {**binding, "page_index": page_index + 1, "next_index": next_index})),
                        "ambiguities": [],
                        "raw_delivery": self._raw_delivery_block(inspect, cells, False),
                        "coverage": {
                            "structure_status": ("grid_present" if grid.get("complete")
                                                 else "grid_incomplete"),
                            "range_status": "complete" if complete else "partial",
                            "context_status": ("confirmed"
                                               if (inspect.get("header_state")
                                                   or {}).get("confirmed") else "unconfirmed"),
                            "relation_status": "insufficient",
                            "requested_scope": scope.model_dump(),
                            "read_scope": _read_scope_summary(
                                [(int(cell["row_index"]), int(cell["column_index"]))
                                 for cell in cells]),
                            "missing_ranges": [] if complete else missing_ranges,
                            "missing_ranges_truncated": False if complete else missing_truncated,
                            # A continuation page never reports the rectangle as
                            # delivered by this response: it says the rectangle is
                            # finished across pages, so its tail cannot be read as
                            # a whole-table completion.
                            "rectangle_status": ("complete" if complete else
                                                 "complete_after_pages"
                                                 if page_index > 0 and next_index >= total_cells
                                                 else "partial"),
                            "pages_delivered": page_index + 1,
                            "complete_for_read_request": complete,
                            "logical_unit_complete": complete}}

            if not candidates:
                return assemble(0)
            # Assembling a page is the expensive part (one query per cell), so
            # the largest window that fits is found by bisection, and a fallback
            # ladder guarantees the response itself fits the byte budget.
            window = min(page_budget, total_cells - start_index)
            low, high = 0, window
            while low < high:
                middle = (low + high + 1) // 2
                if _encoded_size(assemble(middle)) <= limit:
                    low = middle
                else:
                    high = middle - 1
            response = assemble(low)
            for candidate in (16, 8, 4, 2, 1, 0):
                if _encoded_size(response) <= limit:
                    break
                response = assemble(min(candidate, window))
            return response

    def _header_rows(self, inspect: Mapping[str, Any], row_start: int) -> List[int]:
        """The stored header rows of the effective interpretation of this table."""
        rows: set[int] = set()
        for field in inspect.get("fields") or []:
            for row in field.get("header_rows") or [field.get("header_row")]:
                if row is not None:
                    rows.add(int(row))
        return sorted(rows) or [int(row_start)]

    def _raw_header_context(self, connection, inspect: Mapping[str, Any], table,
                            header_rows: Sequence[int], column_start: int,
                            column_end: int) -> List[Dict[str, Any]]:
        """The header cells above the requested columns, confirmed or not.

        Raw mode delivers this as *context*: it is what the source shows above
        these columns, together with whether the shared header rule could
        confirm it and which unit it declares.
        """
        fields = {int(field["column_index"]): field for field in inspect.get("fields") or []}
        context: List[Dict[str, Any]] = []
        for column_index in range(column_start, column_end + 1):
            cells = []
            for row_index in header_rows:
                row = connection.execute(
                    "SELECT coordinate,raw_value,cached_value,empty_kind FROM cells "
                    "WHERE sheet_id=? AND row_index=? AND column_index=?",
                    (table["sheet_id"], row_index, column_index)).fetchone()
                if row is None:
                    continue
                cells.append({"coordinate": row["coordinate"], "row_index": row_index,
                              "column_index": column_index,
                              "raw_value": (row["cached_value"] if row["cached_value"] is not None
                                            else row["raw_value"]),
                              "empty_kind": row["empty_kind"]})
            field = fields.get(column_index) or {}
            context.append({"column_index": column_index, "header_cells": cells,
                            "field_label": field.get("label"),
                            "header_path": list(field.get("path") or []),
                            "unit": field.get("unit") or None})
        return context

    @staticmethod
    def _raw_cell(connection, table, row_index: int, column_index: int) -> Dict[str, Any]:
        """One stored cell exactly as saved, with its merge anchor when covered.

        A coordinate inside the table bounds with no stored cell row is returned
        as an explicit absent coordinate rather than dropped, so a page never
        silently shifts the coordinates it claims to have delivered.
        """
        row = connection.execute(
            "SELECT coordinate,raw_type,raw_value,formula,cached_value,number_format,"
            "date_candidate,empty_kind,hidden_row,hidden_column FROM cells "
            "WHERE sheet_id=? AND row_index=? AND column_index=?",
            (table["sheet_id"], row_index, column_index)).fetchone()
        if row is None:
            return {"coordinate": "R{}C{}".format(row_index, column_index),
                    "row_index": row_index, "column_index": column_index,
                    "raw_type": None, "raw_value": None, "empty_kind": None,
                    "absent": True}
        cell = dict(row)
        cell["row_index"], cell["column_index"] = row_index, column_index
        if cell.get("formula") and cell.get("cached_value") is None:
            cell["value_status"] = "formula_without_cached_value"
        anchor = connection.execute(
            "SELECT anchor FROM merges WHERE sheet_id=? AND row_start<=? AND column_start<=? "
            "AND row_end>=? AND column_end>=?",
            (table["sheet_id"], row_index, column_index, row_index, column_index)).fetchone()
        if anchor is not None:
            cell["merge_anchor"] = anchor["anchor"]
            cell["merge_role"] = ("anchor" if anchor["anchor"] == cell["coordinate"]
                                  else "covered")
            if cell["merge_role"] == "covered":
                cell["effective_value"] = (cell["cached_value"] if cell["cached_value"] is not None
                                           else cell["raw_value"])
        return cell

    @staticmethod
    def _hidden_rows(connection, sheet_id: str) -> set[int]:
        row = connection.execute("SELECT hidden_rows_json FROM sheets WHERE sheet_id=?",
                                 (sheet_id,)).fetchone()
        if row is None or not row[0]:
            return set()
        try:
            return {int(value) for value in json.loads(row[0])}
        except (TypeError, ValueError):
            return set()

    @staticmethod
    def _raw_footnote_refs(table_id: str, captions: Sequence[str], footnotes: Sequence[str],
                           notes: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        """Caption/footnote/note entry points of this table, by coordinate.

        The ids are the same keys the structured read uses, so a raw note is
        citable through the same evidence path.
        """
        refs: List[Dict[str, Any]] = [{"id": "footnote:{}:{}".format(table_id, index),
                                       "kind": "caption", "index": index, "text": text}
                                      for index, text in enumerate(captions)]
        refs.extend({"id": "footnote:{}:{}".format(table_id, len(captions) + index),
                     "kind": "footnote", "index": index, "text": text}
                    for index, text in enumerate(footnotes))
        refs.extend({"id": "footnote:{}:{}".format(
            table_id, len(captions) + len(footnotes) + note["index"]),
            "kind": "table_note", "index": note["index"], "text": note["text"],
            "row_index": note["row_index"], "column_index": note["column_index"],
            "coordinate": note.get("coordinate")}
            for note in notes)
        return refs

    @staticmethod
    def _raw_delivery_block(inspect: Mapping[str, Any], cells: Sequence[Mapping[str, Any]],
                            out_of_bounds: bool) -> Dict[str, Any]:
        """Say exactly what a raw delivery proves, and what it does not."""
        header = inspect.get("header_state") or {}
        return {
            "mode": "raw",
            "coordinates": "RnCm inside this table's own parsed grid",
            "proves": ["the stored raw content at the delivered coordinates",
                       "which delivered cells are covered by a merge anchor"],
            "does_not_prove": ["that a value belongs to a period",
                               "that a value is the metric a column names",
                               "that the delivered range is complete for a computation"],
            "header_context_status": "confirmed" if header.get("confirmed") else "unconfirmed",
            "header_reason": header.get("reason"),
            "unit": header.get("unit") or None,
            "unit_row": header.get("unit_row"),
            "out_of_bounds": bool(out_of_bounds),
            "cell_count": len(cells),
            "note": ("raw coordinates are evidence of content, not of semantics; a completed "
                     "structured table_read is still required before table_compute"),
        }

    def locate(self, literal: str, *, table_id: str | None = None,
               field_id: str | None = None, cursor: str | None = None,
               limit: int = 50) -> Dict[str, Any]:
        if not literal:
            raise ValueError("TABLE_LOCATE_LITERAL_EMPTY")
        # A discovery literal can be a word/phrase inside a qualified label,
        # not only the entire cell. Escape it as text and keep word boundaries
        # so e.g. an identifier 0012 does not also locate 00123.
        pattern = re.escape(literal)
        if re.match(r"[A-Za-z0-9_]", literal[0]):
            pattern = r"(?<!\w)" + pattern
        if re.match(r"[A-Za-z0-9_]", literal[-1]):
            pattern += r"(?!\w)"
        literal_pattern = re.compile(pattern, re.IGNORECASE)
        limit = max(1, min(200, int(limit)))
        offset = 0
        binding = {"revision_id": self.revision_id, "table_id": table_id,
                   "field_id": field_id, "literal": literal}
        if cursor:
            payload = _decode_cursor(cursor)
            if any(payload.get(key) != value for key, value in binding.items()):
                raise ValueError("TABLE_CURSOR_BINDING_MISMATCH")
            offset = int(payload["offset"])
        with self._connection() as connection:
            connection.create_function(
                "table_literal_match", 1,
                lambda text: bool(literal_pattern.search(str(text or ""))),
                deterministic=True,
            )
            parameters: list[Any] = []
            where = ["table_literal_match(tt.term)"]
            if table_id:
                where.append("tt.table_id = ?")
                parameters.append(table_id)
            if field_id:
                where.append("fm.field_id = ?")
                parameters.append(field_id)
            rows = connection.execute(
                "SELECT tt.table_id,tt.sheet_id,tt.row_index,tt.column_index,tt.term AS matched_term,t.title AS table_title,c.coordinate,c.raw_type,c.raw_value,c.formula,c.cached_value,c.empty_kind "
                "FROM table_terms tt JOIN cells c ON c.sheet_id=tt.sheet_id AND c.row_index=tt.row_index AND c.column_index=tt.column_index "
                "JOIN logical_tables t ON t.table_id=tt.table_id "
                "LEFT JOIN interpretations i ON i.table_id=tt.table_id "
                "LEFT JOIN field_map fm ON fm.interpretation_id=i.interpretation_id AND fm.column_index=tt.column_index "
                "WHERE " + " AND ".join(where) + " ORDER BY (tt.term = ?) DESC,tt.table_id,tt.row_index,tt.column_index,tt.term LIMIT ? OFFSET ?",
                (*parameters, literal, limit + 1, offset)).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        return {"revision_id": self.revision_id, "literal": literal,
                "searched_table_id": table_id, "searched_field_id": field_id,
                "matches": [dict(row) for row in rows], "has_more": more,
                "next_cursor": _cursor({**binding, "offset": offset + limit}) if more else None,
                "discovery": "table_value", "search_scope_complete": not more}

    def read(self, scope: ScopeSpec | Mapping[str, Any], *, cursor: str | None = None,
             max_cells: int = 800, page_bytes: int | None = None) -> Dict[str, Any]:
        scope = scope if isinstance(scope, ScopeSpec) else ScopeSpec.model_validate(scope)
        scope, row_window = normalize_row_window(scope)
        page_limit = int(page_bytes) if page_bytes else PAGE_BYTES_LIMIT
        inspect = self.inspect(scope.table_id)
        interpretation_id = inspect["interpretation_id"]
        normalized_scope = scope.model_dump()
        # One request identity for every page of the same scope.  It never
        # contains a page index or an offset: those only describe where the
        # caller already is.
        request_id = _sha256_json({
            "revision_id": self.revision_id,
            "table_path": self.bundle_path or str(Path(self.source).expanduser()),
            "table_id": scope.table_id,
            "interpretation_id": interpretation_id,
            "scope": normalized_scope,
        })
        # q052: every page of one scope shares this identity dict.  It never
        # contains a page index or offset, so a later smaller request can still
        # be recognised as the same logical read.
        identity = {
            "source_version": self.revision_id,
            "table_id": scope.table_id,
            "interpretation_id": interpretation_id,
            "request_id": request_id,
            "sheet_id": inspect.get("sheet_id"),
            "requested_scope": normalized_scope,
        }
        page_budget = max(1, int(max_cells))
        binding = {"request_id": request_id, "revision_id": self.revision_id,
                   "table_id": scope.table_id, "interpretation_id": interpretation_id,
                   "scope_sha256": _sha256_json(normalized_scope), "max_cells": page_budget}
        page_index, start_record_index = 0, 0
        if cursor:
            payload = _decode_cursor(cursor)
            if any(payload.get(key) != value for key, value in binding.items()):
                raise ValueError("TABLE_CURSOR_BINDING_MISMATCH")
            page_index = int(payload.get("page_index", 0))
            start_record_index = int(payload.get("next_index", 0))
            if page_index < 0 or start_record_index < 0:
                raise ValueError("TABLE_CURSOR_OUT_OF_RANGE")
        if inspect["interpretation_status"] != "accepted":
            return self._incomplete(scope, inspect, "structure_ambiguous",
                                    structure="ambiguous", request_id=request_id)
        fields = {field["field_id"]: field for field in inspect["fields"]}
        labels: Dict[str, list] = {}
        for field in inspect["fields"]:
            labels.setdefault(field["label"], []).append(field)

        def resolve(name: str) -> Dict[str, Any] | None:
            """Resolve one field name; a repeated label is never last-wins."""
            candidates = [fields[name]] if name in fields else []
            candidates.extend(labels.get(name, []))
            unique = {item["field_id"]: item for item in candidates}
            return next(iter(unique.values())) if len(unique) == 1 else None

        constraints = []
        valid_keys = sorted(list(labels.keys()) + list(fields.keys()) + list(ROW_WINDOW_KEYS))
        for name, value in scope.constraints.items():
            field = resolve(name)
            if field is None:
                return self._incomplete(
                    scope, inspect,
                    "TABLE_FIELD_UNKNOWN_OR_AMBIGUOUS:{};valid_constraints={}".format(
                        name, ",".join(valid_keys)),
                    structure="ambiguous", request_id=request_id)
            constraints.append((field, value))
        targets, target_columns = [], []
        if scope.target_fields:
            for name in scope.target_fields:
                field = resolve(name)
                if field is None:
                    # An unknown name or a label that appears twice is never
                    # resolved by picking one of the candidates.
                    return self._incomplete(
                        scope, inspect,
                        "TABLE_FIELD_UNKNOWN_OR_AMBIGUOUS:{};valid_constraints={}".format(
                            name, ",".join(valid_keys)),
                        structure="ambiguous", request_id=request_id)
                if field["column_index"] in target_columns:
                    continue
                targets.append(field)
                target_columns.append(field["column_index"])
        else:
            for field in fields.values():
                if field["column_index"] in target_columns:
                    continue
                targets.append(field)
                target_columns.append(field["column_index"])
        target_columns = sorted(target_columns)
        with self._connection() as connection:
            table = connection.execute("SELECT * FROM logical_tables WHERE table_id=?", (scope.table_id,)).fetchone()
            header_row = max(field["header_row"] for field in fields.values())
            captions, footnotes = self._observations(connection, scope.table_id)
            notes = self._table_notes(connection, table, header_row)
            matching_rows, label_refs, hidden_rows_skipped = [], [], 0
            window_start, window_end = (
                (max(header_row + 1, row_window[0]), min(table["row_end"], row_window[1]))
                if row_window else (header_row + 1, table["row_end"]))
            for row_index in range(window_start, window_end + 1):
                if not scope.include_hidden:
                    hidden = connection.execute("SELECT MAX(hidden_row) FROM cells WHERE sheet_id=? AND row_index=?",
                                                (table["sheet_id"], row_index)).fetchone()[0]
                    if hidden:
                        hidden_rows_skipped += 1
                        continue
                ok = True
                row_label_refs = []
                for field, expected in constraints:
                    effective_row = row_index
                    cell = connection.execute("SELECT coordinate,raw_value,formula,cached_value FROM cells WHERE sheet_id=? AND row_index=? AND column_index=?",
                                              (table["sheet_id"], row_index, field["column_index"])).fetchone()
                    if cell is None or (cell["raw_value"] is None and cell["cached_value"] is None):
                        merge = connection.execute("SELECT row_start,column_start FROM merges WHERE sheet_id=? AND ? BETWEEN row_start AND row_end AND ? BETWEEN column_start AND column_end",
                                                   (table["sheet_id"], row_index, field["column_index"])).fetchone()
                        if merge is not None:
                            effective_row = merge["row_start"]
                            cell = connection.execute("SELECT coordinate,raw_value,formula,cached_value FROM cells WHERE sheet_id=? AND row_index=? AND column_index=?",
                                                      (table["sheet_id"], merge["row_start"], merge["column_start"])).fetchone()
                    actual = None if cell is None else (cell["cached_value"] if cell["formula"] and cell["cached_value"] is not None else cell["raw_value"])
                    if str(actual if actual is not None else "") != str(expected):
                        ok = False
                        break
                    row_label_refs.append({"field_id": field["field_id"], "requested_row": row_index,
                                           "effective_row": effective_row, "column_index": field["column_index"],
                                           **(dict(cell) if cell else {})})
                if ok:
                    # A year column without its row label does not identify a
                    # metric. Deliver the explicit stub as context, within the
                    # same paging/coverage contract as constraint labels.
                    stub = min(fields.values(), key=lambda item: item["column_index"])
                    if (stub["column_index"] not in target_columns
                            and (stub.get("label_source") == "row_stub"
                                 or stub["label"].casefold() in ("项目", "指标", "item", "metric"))):
                        label_cell = connection.execute(
                            "SELECT coordinate,raw_value,formula,cached_value FROM cells WHERE sheet_id=? AND row_index=? AND column_index=?",
                            (table["sheet_id"], row_index, stub["column_index"])).fetchone()
                        if label_cell is not None:
                            row_label_refs.append({"field_id": stub["field_id"], "requested_row": row_index,
                                                   "effective_row": row_index, "column_index": stub["column_index"],
                                                   **dict(label_cell)})
                    matching_rows.append(row_index)
                    label_refs.extend(row_label_refs)
            hidden_columns = self._hidden_columns(connection, table["sheet_id"])
            hidden_columns_excluded: list[int] = []
            if not scope.include_hidden and hidden_columns:
                hidden_columns_excluded = [column for column in target_columns if column in hidden_columns]
                target_columns = [column for column in target_columns if column not in hidden_columns]
            requested = sorted({(row, column) for row in matching_rows for column in target_columns})
            requested_hash = _sha256_json([[row, column] for row, column in requested])

            def cell_payload(row_index: int, column_index: int) -> Dict[str, Any]:
                row = connection.execute(
                    "SELECT coordinate,raw_type,raw_value,formula,cached_value,number_format,date_candidate,empty_kind "
                    "FROM cells WHERE sheet_id=? AND row_index=? AND column_index=?",
                    (table["sheet_id"], row_index, column_index)).fetchone()
                value = dict(row) if row is not None else {
                    "coordinate": "R{}C{}".format(row_index, column_index), "raw_type": "blank",
                    "raw_value": None, "formula": None, "cached_value": None,
                    "number_format": "General", "date_candidate": 0, "empty_kind": "blank"}
                value.update(sheet_id=table["sheet_id"], row_index=row_index, column_index=column_index)
                if value.get("formula") and value.get("cached_value") is None:
                    # A formula string is never a computed value.
                    value["empty_kind"] = "unresolved_formula"
                    value["unresolved_formula"] = True
                if value.get("date_candidate"):
                    epoch = connection.execute("SELECT date_epoch FROM sheets WHERE sheet_id=?",
                                               (table["sheet_id"],)).fetchone()[0]
                    source_value = value.get("cached_value") if value.get("formula") else value.get("raw_value")
                    value["normalized_value"] = excel_serial_iso(source_value, epoch)
                # Explicit merge facts are the only permitted value
                # inheritance, and the only permitted statement that a repeated
                # value is not this cell's own: a merge covering this cell that
                # starts inside the same legal region is that proof.  A cell with
                # no value of its own (missing, or saved empty) takes the
                # anchor's value as its effective value; a cell the compile
                # marked ``merged_covered`` keeps the text the grid expansion
                # repeated into it and is reported with its anchor, so a reader
                # can tell an expansion repeat from a value written twice.  A
                # plain empty cell no merge covers never inherits, and an
                # unresolved formula never does.
                covered = value.get("empty_kind") == "merged_covered"
                if covered or _carries_no_own_value(value):
                    merge = connection.execute(
                        "SELECT row_start,column_start,row_end,column_end,anchor FROM merges "
                        "WHERE sheet_id=? AND ? BETWEEN row_start AND row_end "
                        "AND ? BETWEEN column_start AND column_end",
                        (table["sheet_id"], row_index, column_index)).fetchone()
                    if merge is not None and (table["row_start"] <= merge["row_start"] <= table["row_end"]
                                              and table["column_start"] <= merge["column_start"] <= table["column_end"]):
                        anchor = connection.execute(
                            "SELECT coordinate,raw_value,formula,cached_value FROM cells "
                            "WHERE sheet_id=? AND row_index=? AND column_index=?",
                            (table["sheet_id"], merge["row_start"], merge["column_start"])).fetchone()
                        if anchor is not None:
                            anchor_value = (anchor["cached_value"]
                                            if anchor["formula"] and anchor["cached_value"] is not None
                                            else anchor["raw_value"])
                            if anchor_value is not None:
                                if _carries_no_own_value(value):
                                    value["effective_value"] = str(anchor_value)
                                value["merge_anchor"] = merge["anchor"]
                return value

            # Required context for the whole request, computed once.
            context_required: Dict[str, Dict[str, Any]] = {}

            def require(key: str, kind: str, row_index: int | None, column_index: int | None,
                        text: str | None = None) -> None:
                context_required.setdefault(key, {"id": key, "kind": kind, "row_index": row_index,
                                                  "column_index": column_index, "text": text})

            header_cells: Dict[str, Dict[str, Any]] = {}
            # Headers are collected for the target columns and for every label
            # column the request uses: the constraint column's own header names
            # the selected row and belongs in the response context.
            header_columns = sorted(set(target_columns) | {
                int(ref["column_index"]) for ref in label_refs})
            for row_index in range(table["row_start"], header_row + 1):
                for column_index in header_columns:
                    existing = connection.execute(
                        "SELECT 1 FROM cells WHERE sheet_id=? AND row_index=? AND column_index=?",
                        (table["sheet_id"], row_index, column_index)).fetchone()
                    if existing is None:
                        continue  # a merge-covered header has no cell of its own
                    key = "cell:{}:{}:{}".format(table["sheet_id"], row_index, column_index)
                    header_cells[key] = {"id": key, "kind": "header",
                                         "row_index": row_index, "column_index": column_index}
                    require(key, "header", row_index, column_index)
            label_items: Dict[str, Dict[str, Any]] = {}
            for ref in label_refs:
                key = "cell:{}:{}:{}".format(table["sheet_id"], ref["requested_row"], ref["column_index"])
                label_items.setdefault(key, {**ref, "id": key, "kind": "label"})
                require(key, "label", ref["requested_row"], ref["column_index"])
            for index, text in enumerate(captions):
                require("caption:{}:{}".format(scope.table_id, index), "caption", None, None, text)
            for index, text in enumerate(footnotes):
                require("footnote:{}:{}".format(scope.table_id, index), "footnote", None, None, text)
            for note in notes:
                require("footnote:{}:{}".format(scope.table_id, len(captions) + len(footnotes) + note["index"]),
                        "table_note", note["row_index"], note["column_index"], note["text"])
            context_hash = _sha256_json(sorted(context_required))

            rows_in_order = [(row, [column for candidate, column in requested if candidate == row])
                             for row in matching_rows]
            rows_in_order = [(row, columns) for row, columns in rows_in_order if columns]
            if start_record_index > len(rows_in_order):
                raise ValueError("TABLE_CURSOR_OUT_OF_RANGE")
            used_rows: list[tuple[int, list[int]]] = []
            used_cells = 0
            for row_index, columns in rows_in_order[start_record_index:]:
                if used_rows and used_cells + len(columns) > page_budget:
                    break
                used_rows.append((row_index, columns))
                used_cells += len(columns)

            def assemble(row_set: list[tuple[int, list[int]]], range_limit: int) -> Dict[str, Any]:
                page_keys = [(row, column) for row, columns in row_set for column in columns]
                present = set(page_keys)
                page_columns = sorted({column for _, columns in row_set for column in columns})
                page_rows = {row for row, _ in row_set}
                # A page's context also covers the header of every label column it
                # returns (the constraint column names the row it selected), so the
                # header of a constraint column is never silently dropped.
                page_context_columns = sorted(set(page_columns) | {
                    item["column_index"] for item in label_items.values()
                    if item["requested_row"] in page_rows})
                page_context: list[Dict[str, Any]] = []
                for row_index in range(table["row_start"], header_row + 1):
                    for column_index in page_context_columns:
                        key = "cell:{}:{}:{}".format(table["sheet_id"], row_index, column_index)
                        if key not in header_cells:
                            continue
                        item = dict(header_cells[key])
                        item.update(cell_payload(row_index, column_index))
                        page_context.append(item)
                for key, item in sorted(label_items.items()):
                    if item["requested_row"] not in page_rows:
                        continue
                    item = dict(item)
                    item.update(cell_payload(item["requested_row"], item["column_index"]))
                    page_context.append(item)
                for item in context_required.values():
                    if item["kind"] in ("caption", "footnote", "table_note"):
                        page_context.append(dict(item))
                # One cell that serves as both a header and a label is counted
                # once: the same context id is never emitted twice.
                deduped: list[Dict[str, Any]] = []
                emitted: set[str] = set()
                for item in page_context:
                    if item["id"] in emitted:
                        continue
                    emitted.add(item["id"])
                    deduped.append(item)
                page_context = deduped
                # Context records (headers and labels) carry the same default
                # cell metadata as data cells, but no consumer reads a header's
                # formula, number_format or date_candidate: a header is a label
                # string.  Emitting that metadata made a wide table's header row
                # (~15 KB for 32 columns x 2 header rows) dominate the page and
                # pushed a single-row read past READ_RESPONSE_LIMIT.  Data cells
                # below stay full; only the context records are compacted.
                page_context = [_compact_cell_for_delivery(item) for item in page_context]
                context_ids_present = {item["id"] for item in page_context}
                page_required = {key for key, item in context_required.items()
                                 if item["kind"] in ("caption", "footnote")
                                 or (item["kind"] == "header" and item["column_index"] in page_context_columns)
                                 or (item["kind"] == "label" and item["row_index"] in page_rows)}
                context_complete = page_required <= context_ids_present
                # A caption or note is returned with its text and position, but
                # the reader cannot decide which rows it applies to: that stays
                # unresolved for the relation check instead of being asserted.
                has_notes = bool(context_required) and any(
                    item["kind"] in ("caption", "footnote", "table_note")
                    for item in context_required.values())
                cells = [cell_payload(row, column) for row, column in page_keys]
                missing = [cell for cell in requested if cell not in present]
                ranges, truncated = _missing_ranges(missing, range_limit)
                more = start_record_index + len(row_set) < len(rows_in_order)
                unresolved = [cell["coordinate"] for cell in cells if cell.get("unresolved_formula")]
                complete = (page_index == 0 and not missing and context_complete
                            and not missing_ranges_context(range_limit, ranges, truncated) and not unresolved
                            and not hidden_columns_excluded)
                logical_context = context_complete and not has_notes
                all_columns = bool(table["column_end"]) and sorted(target_columns) == list(
                    range(table["column_start"], table["column_end"] + 1))
                logical_complete = (not scope.constraints and all_columns
                                    and len(matching_rows) == max(0, table["row_end"] - header_row)
                                    and not hidden_rows_skipped and not hidden_columns_excluded
                                    and complete and logical_context and not unresolved)
                return {
                    **identity,
                    "table_path": self.bundle_path, "table_title": inspect.get("title"),
                    "physical_location": self._physical_location(connection, scope.table_id),
                    # ``header_refs``/``label_refs`` and ``returned_ranges`` are
                    # not emitted: they repeated what ``context_items`` (same
                    # objects, with ``kind``) and ``coverage.read_scope`` already
                    # carry, and that duplication is what blew a wide table's
                    # page past READ_RESPONSE_LIMIT.  ``footnote_refs`` stays:
                    # it is small (notes are rare), and it names the note kind
                    # explicitly for the note-citation path.
                    "cells": cells,
                    "footnote_refs": [item for item in page_context
                                      if item["kind"] in ("caption", "footnote", "table_note")],
                    "context_items": page_context,
                    "requested_cell_count": len(requested), "requested_cells_sha256": requested_hash,
                    "required_context_count": len(context_required), "required_context_sha256": context_hash,
                    "missing_cell_count": len(missing), "page_index": page_index,
                    "start_record_index": start_record_index,
                    "has_more": more, "missing_ranges_truncated": truncated,
                    "coverage_summary_scope": "current_response",
                    "next_cursor": _cursor({**binding, "page_index": page_index + 1,
                                            "next_index": start_record_index + len(row_set)}) if more else None,
                    "ambiguities": (["unresolved_formula"] if unresolved else [])
                                   + (["hidden_columns_excluded"] if hidden_columns_excluded else []),
                    "coverage": {
                        "structure_status": "confirmed",
                        "range_status": "partial" if (more or missing) else "complete",
                        "context_status": ("unresolved" if (has_notes or not context_complete) else "complete"),
                        "relation_status": "insufficient",
                        "read_scope": _read_scope_summary(page_keys), "missing_ranges": ranges,
                        "missing_cell_count": len(missing),
                        "complete_for_read_request": complete,
                        "logical_unit_complete": logical_complete,
                    },
                    "derivation": "raw_grid_read",
                }

            response = assemble(list(used_rows), MISSING_RANGE_LIMIT)
            if _encoded_size(response) > page_limit:
                # Shrink the cheapest thing first: the missing-range detail is
                # bookkeeping, so a page keeps its rows as long as the detail
                # alone can pay for the overrun.
                row_set = list(used_rows)
                for limit in (16, 8, 4, 2, 1, 0):
                    if _encoded_size(response) <= page_limit:
                        break
                    response = assemble(row_set, limit)
                if _encoded_size(response) > page_limit:
                    # Then drop rows - but not one at a time.  Assembling the
                    # page is the expensive part (one query per cell), so the
                    # largest prefix that fits is found by bisection: the old
                    # one-row-at-a-time loop re-assembled the whole page up to
                    # `len(used_rows)` times and turned one read into minutes.
                    low, high = 0, len(row_set)
                    while low < high:
                        middle = (low + high + 1) // 2
                        if _encoded_size(assemble(row_set[:middle], 0)) <= page_limit:
                            low = middle
                        else:
                            high = middle - 1
                    row_set = row_set[:low]
                    response = assemble(row_set, MISSING_RANGE_LIMIT)
                    for limit in (16, 8, 4, 2, 1, 0):
                        if _encoded_size(response) <= page_limit:
                            break
                        response = assemble(row_set, limit)
            if _encoded_size(response) > page_limit:
                compact = {**identity,
                           "table_path": self.bundle_path, "table_title": inspect.get("title"),
                           "status": "request_metadata_too_large", "reason": "scope_too_large",
                           "requested_cell_count": len(requested), "requested_cells_sha256": requested_hash,
                           "missing_cell_count": len(requested), "has_more": False, "next_cursor": None,
                           "cells": [], "context_items": [], "coverage_summary_scope": "current_response",
                           "coverage": {"structure_status": "confirmed", "range_status": "unknown",
                                        "context_status": "unresolved", "relation_status": "insufficient",
                                        "complete_for_read_request": False, "logical_unit_complete": False}}
                return compact
            if used_rows and not response["cells"]:
                # q052: a single record larger than the whole page limit is
                # blocked, not paged forever.  ``has_more`` must be False so the
                # caller cannot re-enter an infinite cursor loop, and the known
                # requested scope is returned so a smaller request can be made.
                return {
                    **identity,
                    "table_path": self.bundle_path, "table_title": inspect.get("title"),
                    "status": "oversized_record", "reason": "context_too_large",
                    "cells": [], "context_items": [], "footnote_refs": [],
                    "requested_cell_count": len(requested), "requested_cells_sha256": requested_hash,
                    "required_context_count": len(context_required), "required_context_sha256": context_hash,
                    "missing_cell_count": len(requested), "page_index": page_index,
                    "start_record_index": start_record_index, "missing_ranges_truncated": True,
                    "coverage_summary_scope": "current_response", "has_more": False, "next_cursor": None,
                    "ambiguities": ["oversized_record"],
                    "coverage": {"structure_status": "confirmed", "range_status": "partial",
                                 "context_status": "unresolved", "relation_status": "insufficient",
                                 "requested_scope": normalized_scope, "read_scope": [], "missing_ranges": [],
                                 "missing_ranges_truncated": True, "missing_cell_count": len(requested),
                                 "coverage_summary_scope": "current_response",
                                 "complete_for_read_request": False, "logical_unit_complete": False},
                }
            return response

    @staticmethod
    def _hidden_columns(connection, sheet_id: str) -> set[int]:
        row = connection.execute("SELECT hidden_columns_json FROM sheets WHERE sheet_id=?", (sheet_id,)).fetchone()
        if row is None or not row[0]:
            return set()
        try:
            return {int(value) for value in json.loads(row[0])}
        except (TypeError, ValueError):
            return set()

    NOTE_PATTERN = re.compile(r"^\s*(note|notes|remark|remarks|footnote|备注|说明|注)[:：]?\s*$",
                              re.IGNORECASE)

    @classmethod
    def _table_notes(cls, connection, table, header_row: int) -> list[Dict[str, Any]]:
        """Return note rows that live inside this table's own bounds.

        Only this grid is searched: a remark in an unrelated table never blocks
        this read and is never returned as this table's footnote.
        """
        notes: list[Dict[str, Any]] = []
        for row_index in range(header_row + 1, table["row_end"] + 1):
            marker = connection.execute(
                "SELECT coordinate,raw_value FROM cells WHERE sheet_id=? AND row_index=? AND column_index=?",
                (table["sheet_id"], row_index, table["column_start"])).fetchone()
            if marker is None or not marker["raw_value"] or not cls.NOTE_PATTERN.match(str(marker["raw_value"])):
                continue
            parts = []
            first_column = None
            for cell in connection.execute(
                    "SELECT column_index,raw_value FROM cells WHERE sheet_id=? AND row_index=? "
                    "AND column_index>? ORDER BY column_index",
                    (table["sheet_id"], row_index, table["column_start"])):
                if cell["raw_value"] is None or not str(cell["raw_value"]).strip():
                    continue
                parts.append(str(cell["raw_value"]))
                first_column = cell["column_index"] if first_column is None else first_column
            if not parts:
                continue
            notes.append({"index": len(notes), "row_index": row_index,
                          "column_index": first_column or table["column_start"],
                          "text": " ".join(parts), "coordinate": marker["coordinate"]})
        return notes

    @staticmethod
    def _physical_location(connection, table_id: str) -> Dict[str, Any]:
        """The table's own physical location, or an explicit unknown.

        A native spreadsheet has no page or box; a PDF/DOCX grid may have both in
        its own observation row.  Nothing is ever inferred: when the observation
        is absent or carries no page/bbox the status is ``unknown``.
        """
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='table_observations'").fetchone()
        page_number, bbox = None, None
        if exists is not None:
            row = connection.execute(
                "SELECT page_number, bbox_json FROM table_observations WHERE table_id=? "
                "ORDER BY rowid LIMIT 1", (table_id,)).fetchone()
            if row is not None:
                page_number = row["page_number"]
                bbox = json.loads(row["bbox_json"]) if row["bbox_json"] else None
        return {"page_number": page_number, "bbox": bbox,
                "status": "known" if (page_number is not None or bbox) else "unknown"}

    @staticmethod
    def _observations(connection, table_id: str) -> tuple[list[str], list[str]]:
        """Return this table's own caption and footnote text.

        A grid without an observation table (a native spreadsheet) has no
        observations to read; notes are never searched for in other tables.
        """
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='table_observations'").fetchone()
        if exists is None:
            return [], []
        row = connection.execute(
            "SELECT caption_json,footnote_json FROM table_observations WHERE table_id=?",
            (table_id,)).fetchone()
        if row is None:
            return [], []
        return _text_list(row["caption_json"]), _text_list(row["footnote_json"])

    def _incomplete(self, scope: ScopeSpec, inspect: Mapping[str, Any], reason: str,
                    *, structure: str, request_id: str | None = None) -> Dict[str, Any]:
        return {"source_version": self.revision_id, "table_id": scope.table_id,
                "interpretation_id": inspect.get("interpretation_id"), "request_id": request_id,
                "sheet_id": inspect.get("sheet_id"),
                "requested_scope": scope.model_dump(), "cells": [], "context_items": [],
                "footnote_refs": [],
                "missing_cell_count": None, "requested_cell_count": None,
                "coverage_summary_scope": "current_response",
                "has_more": False, "next_cursor": None, "ambiguities": [reason],
                "coverage": {"structure_status": structure, "range_status": "unknown",
                    "context_status": "unresolved", "relation_status": "insufficient",
                    "requested_scope": scope.model_dump(), "read_scope": [], "missing_ranges": [inspect.get("bounds")],
                    "missing_ranges_truncated": False,
                    "complete_for_read_request": False, "logical_unit_complete": False}}


INSPECT_RAW_PREVIEW_LIMIT = 1024
INSPECT_SAMPLE_ROWS = 3
INSPECT_SAMPLE_BYTES = 4 * 1024


def _bounded_preview_cells(cells: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], bool]:
    """Keep one data row of a readable grid as a shape example.

    Header cells are already described by ``fields`` and ``header_merges``, and
    the payload is a locator for ``table_read`` rather than the data itself, so
    a single row is enough to show the coordinate convention.  The trim is
    reported, never silent.
    """
    if not cells:
        return [], False
    rows = sorted({cell.get("row_index") for cell in cells if cell.get("row_index") is not None})
    data_rows = rows[1:][:INSPECT_SAMPLE_ROWS] or rows[:1]
    kept: List[Dict[str, Any]] = []
    used = 0
    for row in data_rows:
        row_cells = [cell for cell in cells if cell.get("row_index") == row]
        encoded = _encoded_size(row_cells)
        if kept and used + encoded > INSPECT_SAMPLE_BYTES:
            break
        kept.extend(row_cells)
        used += encoded
    return kept, len(kept) != len(cells)


def _bounded_text(text: str, limit: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, False
    return encoded[:limit].decode("utf-8", "ignore"), True


# Context records (headers and labels) are emitted in the same compacted form
# the delivery layer sends to the model: constant ``sheet_id`` and default cell
# metadata carry nothing a consumer of a *header* or *label* reads.  Data cells
# stay full so citation registration keeps formula/cached/number_format.  This
# is what makes a wide table's header row (32 columns x 2 header rows ~ 15 KB)
# stop dominating the page and pushing a single-row read past
# READ_RESPONSE_LIMIT.  Keep this key set in sync with the controller's
# ``_compact_cell``.
_CELL_DEFAULT_KEYS = ("formula", "cached_value", "empty_kind", "number_format",
                      "date_candidate", "unresolved_formula", "normalized_value")
_CELL_DEFAULT_VALUES = {"number_format": "General", "date_candidate": 0}


def _compact_cell_for_delivery(cell: Mapping[str, Any]) -> Dict[str, Any]:
    compact = {key: value for key, value in cell.items() if key != "sheet_id"}
    for key in _CELL_DEFAULT_KEYS:
        if key in compact and compact[key] == _CELL_DEFAULT_VALUES.get(key, None):
            del compact[key]
    return compact


def _fit_inspection(payload: Dict[str, Any], limit: int) -> Dict[str, Any]:
    """Keep one inspection inside the model-facing byte bound.

    Identity, bounds, fields, merges and limitations always survive; the sample
    rows and the raw observation are bulk, so they shrink first and say so.
    Nothing is silently dropped: ``inspect_truncated`` and
    ``inspect_truncation`` record what was cut.
    """
    fitted = dict(payload)
    fitted["inspect_truncated"] = False
    if _encoded_size(fitted) <= limit:
        return fitted
    reasons: List[str] = []
    sample = list(fitted.get("sample") or [])
    # 1. Drop trailing sample rows: they are a preview, never the request.
    kept = sample
    while kept and _encoded_size({**fitted, "sample": kept}) > limit:
        kept = kept[: max(0, len(kept) - 5)]
    if len(kept) != len(sample):
        fitted["sample"] = kept
        reasons.append("sample_rows_dropped")
    # 2. Then shorten the raw observation, always flagged as an excerpt.
    raw = fitted.get("raw_observation_text") or ""
    while raw and _encoded_size({**fitted, "raw_observation_text": raw}) > limit:
        raw = raw[: int(len(raw) * 0.8)]
        fitted["raw_observation_text"] = raw
    if raw != (payload.get("raw_observation_text") or ""):
        fitted["raw_observation_text"] = raw
        fitted["raw_observation_truncated"] = True
        reasons.append("raw_observation_trimmed")
    fitted["inspect_truncated"] = bool(reasons)
    fitted["inspect_truncation"] = reasons
    # 3. A payload that still cannot fit keeps only the identity it needs.
    if _encoded_size(fitted) > limit:
        return {key: fitted.get(key) for key in (
            "revision_id", "table_id", "sheet_id", "title", "captions", "bounds",
            "interpretation_id",
            "interpretation_status", "capabilities", "limitations", "structure_reason",
            "page_number", "region")} | {
            "inspect_truncated": True, "inspect_truncation": reasons + ["identity_only"],
            "sample": [], "raw_observation_text": ""}
    return fitted


def _sha256_json(value: Any) -> str:
    """Canonical SHA-256 over a JSON value (the plan's fixed encoding)."""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _encoded_size(payload: Mapping[str, Any]) -> int:
    """Bytes of a payload exactly as this module measures it for page limits.

    本文件刻意只保留一份实现：曾经有第二份同名 ``_encoded_size``（``sort_keys=True``
    ＋紧凑分隔符）被这一份覆盖，而两者对**同一个对象算出的字节数不同**（紧凑分隔符
    少算默认分隔符里的空格）。``PAGE_BYTES_LIMIT`` / ``EXHAUSTIVE_*`` 这些分页阈值
    是按本实现标定的，换实现等于悄悄改变所有读取的分页行为。
    """
    return len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"))


def _text_list(value: Any) -> list[str]:
    if not value:
        return []
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        return []
    if isinstance(payload, str):
        return [payload] if payload.strip() else []
    if isinstance(payload, list):
        return [str(item) for item in payload if str(item).strip()]
    return []


def missing_ranges_context(limit: int, ranges: list, truncated: bool) -> bool:
    """True when the bounded summary cannot be treated as the whole picture."""
    return bool(truncated) or (limit == 0 and bool(ranges))


def _missing_ranges(missing: Iterable[tuple[int, int]],
                    limit: int = MISSING_RANGE_LIMIT) -> tuple[list[Dict[str, int]], bool]:
    """Compress missing cells into closed rectangles, capped at ``limit``."""
    items = sorted(set(missing))
    if not items:
        return [], False
    by_row: Dict[int, list[int]] = {}
    for row, column in items:
        by_row.setdefault(row, []).append(column)
    spans: list[tuple[int, int, int]] = []
    for row, columns in sorted(by_row.items()):
        start = previous = columns[0]
        for column in columns[1:]:
            if column != previous + 1:
                spans.append((row, start, previous))
                start = column
            previous = column
        spans.append((row, start, previous))
    merged: list[list[int]] = []
    for row, start, end in spans:
        if merged and merged[-1][1] == row - 1 and merged[-1][2] == start and merged[-1][3] == end:
            merged[-1][1] = row
        else:
            merged.append([row, row, start, end])
    ranges = [{"row_start": item[0], "row_end": item[1], "column_start": item[2], "column_end": item[3]}
              for item in merged]
    return ranges[:limit], len(ranges) > limit


def _ranges(cells: Iterable[tuple[int, int]]) -> list[Dict[str, int]]:
    items = list(cells)
    if not items:
        return []
    return [{"row_start": row, "row_end": row, "column_start": column, "column_end": column}
            for row, column in items]


def _read_scope_summary(cells: Iterable[tuple[int, int]]) -> Dict[str, Any]:
    """One page's own extent, as bounds instead of one record per cell.

    ``_ranges`` emitted a four-key record for every single cell, so a 14-row
    page spent ~5.8 KB of its 32 KB budget restating the coordinates that
    ``cells`` already carries - the single largest term in the page.  Nothing
    consumes the per-cell form (only ``coverage.range_status`` and the
    ``missing_*`` fields are read), so the extent is reported as bounds plus a
    count and the page keeps that budget for rows.
    """
    items = list(cells)
    if not items:
        return {"cell_count": 0}
    rows = [row for row, _ in items]
    columns = [column for _, column in items]
    return {"row_start": min(rows), "row_end": max(rows),
            "column_start": min(columns), "column_end": max(columns),
            "cell_count": len(items), "shape": "bounds"}


# ──────────────────────────────────────────────────────────────
#  keep-and-search raw-table pages
#
#  These pages serialize every already-saved cell in original coordinate order.
#  They never compute formulas, guess headers, fill merged values or drop a
#  single row because it lives past the bounded navigation preview.
# ──────────────────────────────────────────────────────────────


def read_logical_table(connection, table_id: str):
    """Return one logical table row; unknown ids are explicit errors."""
    row = connection.execute(
        "SELECT * FROM logical_tables WHERE table_id=?", (table_id,)).fetchone()
    if row is None:
        raise KeyError("TABLE_ID_UNKNOWN")
    return row


def stream_cells(connection, table):
    """All cells inside the table's own bounds, ordered by coordinate."""
    return connection.execute(
        "SELECT row_index,column_index,coordinate,raw_type,raw_value,formula,"
        "cached_value,number_format,date_candidate,empty_kind,hidden_row,hidden_column "
        "FROM cells WHERE sheet_id=? AND row_index BETWEEN ? AND ? "
        "AND column_index BETWEEN ? AND ? ORDER BY row_index,column_index",
        (table["sheet_id"], table["row_start"], table["row_end"],
         table["column_start"], table["column_end"]),
    ).fetchall()


def read_merge_ranges(connection, table):
    """Every merge range whose rows intersect this table's own bounds."""
    return connection.execute(
        "SELECT row_start,column_start,row_end,column_end,anchor FROM merges "
        "WHERE sheet_id=? AND row_start<=? AND row_end>=? AND column_start<=? "
        "AND column_end>=? ORDER BY row_start,column_start",
        (table["sheet_id"], table["row_end"], table["row_start"],
         table["column_end"], table["column_start"]),
    ).fetchall()


def _markdown_scalar(value: Any) -> str:
    """Render null / empty string / 0 / False without conflating them."""
    return "`{}`".format(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _render_cell(cell: Mapping[str, Any]) -> str:
    coordinate = str(cell.get("coordinate") or "R{}C{}".format(
        cell.get("row_index"), cell.get("column_index")))
    parts = [
        "`{}`".format(coordinate),
        "row={}".format(cell.get("row_index")),
        "column={}".format(cell.get("column_index")),
        "type=" + _markdown_scalar(cell.get("raw_type")),
        ("raw=<EMPTY>" if cell.get("raw_value") is None
         else "raw=" + _markdown_scalar(cell.get("raw_value"))),
    ]
    # READING-REPAIR §5.8: null formula/cached/empty_kind are noise, not data.
    # Only emit them when they carry semantics; 0, False, "0012", units and real
    # empty markers stay on the ``raw``/``type``/``empty_kind`` fields.
    formula = cell.get("formula")
    if formula is not None:
        parts.append("formula=" + _markdown_scalar(formula))
    cached_value = cell.get("cached_value")
    if cached_value is not None:
        parts.append("cached=" + _markdown_scalar(cached_value))
    empty_kind = cell.get("empty_kind")
    if empty_kind is not None:
        parts.append("empty_kind=" + _markdown_scalar(empty_kind))
    if "effective_value" in cell:
        parts.append("effective=" + _markdown_scalar(cell["effective_value"]))
        parts.append("merge_anchor=" + _markdown_scalar(cell.get("merge_anchor")))
    if cell.get("normalized_value") is not None:
        parts.append("normalized=" + _markdown_scalar(cell["normalized_value"]))
    if cell.get("column_date_header"):
        parts.append("column_date_header=" + _markdown_scalar(cell["column_date_header"]))
    if cell.get("hidden_row"):
        parts.append("hidden_row")
    if cell.get("hidden_column"):
        parts.append("hidden_column")
    return "- " + " ".join(parts)


def project_saved_cell_semantics(cells: List[Mapping[str, Any]],
                                 merges: List[Mapping[str, Any]],
                                 *, epoch: Optional[str] = None) -> List[Dict[str, Any]]:
    """Add source-derived semantics without mutating or filling ordinary blanks.

    Inheritance is restricted to saved merge ranges with a present, resolved
    anchor. Raw values, coordinates and hidden flags remain unchanged.
    """
    projected = [dict(cell) for cell in cells]
    by_position = {(int(cell["row_index"]), int(cell["column_index"])): cell
                   for cell in projected}
    for merge in merges:
        anchor = by_position.get((int(merge["row_start"]), int(merge["column_start"])))
        if anchor is None or (anchor.get("formula") and anchor.get("cached_value") is None):
            continue
        anchor_value = anchor.get("cached_value") if anchor.get("formula") else anchor.get("raw_value")
        if anchor_value is None:
            continue
        for row in range(int(merge["row_start"]), int(merge["row_end"]) + 1):
            for column in range(int(merge["column_start"]), int(merge["column_end"]) + 1):
                cell = by_position.get((row, column))
                if (cell is not None and cell.get("raw_value") is None
                        and cell.get("cached_value") is None and not cell.get("formula")):
                    cell["effective_value"] = anchor_value
                    cell["merge_anchor"] = merge["anchor"]
    if epoch is not None:
        for cell in projected:
            if cell.get("date_candidate") and not str(cell.get("number_format") or "").startswith("builtin:"):
                value = cell.get("cached_value") if cell.get("formula") else cell.get("raw_value")
                normalized = excel_serial_iso(value, epoch)
                if normalized is not None:
                    cell["normalized_value"] = normalized
    # Bind date columns only from a confirmed date/weekday header pair.
    # Ordinary date-valued rows do not become column headers. Saved horizontal
    # merge extents, rather than guessed neighbouring columns, define the axis.
    weekday_labels = {label: index for index, day in enumerate("一二三四五六日")
                      for label in ("星期" + day, "周" + day)}
    weekday_labels.update({"星期天": 6, "周天": 6})
    axes: Dict[int, List[tuple[int, str, str]]] = {}
    for cell in projected:
        normalized = cell.get("normalized_value")
        if not normalized:
            continue
        row, column = int(cell["row_index"]), int(cell["column_index"])
        try:
            actual_weekday = date.fromisoformat(str(normalized)[:10]).weekday()
        except ValueError:
            continue
        paired_row = None
        for neighbour_row in (row - 1, row + 1):
            neighbour = by_position.get((neighbour_row, column)) or {}
            label = neighbour.get("effective_value", neighbour.get("raw_value"))
            if weekday_labels.get(str(label).strip()) == actual_weekday:
                paired_row = neighbour_row
                break
        if paired_row is None:
            continue
        first_column = last_column = column
        for merge in merges:
            if int(merge["row_start"]) == row and int(merge["column_start"]) == column:
                first_column, last_column = column, int(merge["column_end"])
                break
        for axis_column in range(first_column, last_column + 1):
            axes.setdefault(axis_column, []).append((max(row, paired_row), str(normalized)[:10], str(cell["coordinate"])))
    for cell in projected:
        if (cell.get("raw_value") is None and cell.get("cached_value") is None
                or cell.get("normalized_value")):
            continue
        preceding = [axis for axis in axes.get(int(cell["column_index"]), [])
                     if axis[0] < int(cell["row_index"])]
        if preceding:
            latest_row = max(axis[0] for axis in preceding)
            latest_axes = [axis for axis in preceding if axis[0] == latest_row]
            if len({axis[1] for axis in latest_axes}) != 1:
                continue
            header_row, header_date, anchor = latest_axes[0]
            cell["column_date_header"] = header_date + "@" + anchor
    return projected


def _carries_no_own_value(cell: Mapping[str, Any]) -> bool:
    """True when a cell states no value of its own (missing or empty text).

    A saved empty string and a missing cell mean the same thing to a reader --
    this position holds no value -- so both are eligible for merge inheritance.
    A formula whose cache is missing is not: it is an unresolved claim, not an
    empty cell.  ``0`` and ``"0"`` are values and stay values.
    """
    if cell.get("unresolved_formula"):
        return False
    for key in ("raw_value", "cached_value"):
        value = cell.get(key)
        if value is not None and str(value).strip() != "":
            return False
    return True


def render_coordinate_records(cells: List[Mapping[str, Any]]) -> str:
    """One explicit Markdown record per saved cell."""
    return "\n".join(_render_cell(cell) for cell in cells) + ("\n" if cells else "")


def render_intersecting_merges(merges: List[Mapping[str, Any]],
                               rows: List[Mapping[str, Any]]) -> str:
    """List the merge ranges that intersect the rows serialized on this page."""
    if not rows or not merges:
        return ""
    row_indices = [int(cell.get("row_index")) for cell in rows if cell.get("row_index") is not None]
    if not row_indices:
        return ""
    row_min, row_max = min(row_indices), max(row_indices)
    selected = [merge for merge in merges
                if int(merge["row_end"]) >= row_min and int(merge["row_start"]) <= row_max]
    if not selected:
        return ""
    lines = ["\n## Merge ranges", ""]
    for merge in selected:
        lines.append("- `{}` rows {}-{} columns {}-{}".format(
            merge["anchor"], merge["row_start"], merge["row_end"],
            merge["column_start"], merge["column_end"]))
    return "\n".join(lines) + "\n"


def _compact_grid(cells, merges, *, epoch=None):
    """One row per source row; cell provenance stays in the SQLite asset."""
    cells = project_saved_cell_semantics(cells, merges, epoch=epoch)
    if not cells:
        return ""
    columns = sorted({int(cell["column_index"]) for cell in cells})
    rows = {}
    for cell in cells:
        value = cell.get("normalized_value")
        if value is None:
            value = cell.get("effective_value", cell.get("raw_value"))
        if cell.get("formula"):
            value = cell.get("cached_value")
            if value is None:
                value = str(cell["formula"]) + " (未计算)"
        text = "" if value is None else str(value)
        rows.setdefault(int(cell["row_index"]), {})[int(cell["column_index"])] = (
            text.replace("|", "\\|").replace("\n", "<br>").replace("\r", ""))
    lines = ["| 行 | " + " | ".join("C{}".format(c) for c in columns) + " |",
             "| --- | " + " | ".join("---" for _ in columns) + " |"]
    lines.extend("| {} | {} |".format(row, " | ".join(values.get(c, "") for c in columns))
                 for row, values in sorted(rows.items()))
    # Formatting can carry units (0.00%) or identifier padding (000000).
    # Group contiguous coordinates so that preserving it does not require a
    # second, verbose citation record for every cell. Raw values stay intact.
    formats = {}
    for cell in cells:
        number_format = str(cell.get("number_format") or "")
        if number_format not in {"", "General", "@"}:
            formats.setdefault((int(cell["column_index"]), number_format), set()).add(int(cell["row_index"]))
    notes = []
    for (column, number_format), positions in sorted(formats.items()):
        ordered = sorted(positions)
        runs = []
        for row in ordered:
            if runs and row == runs[-1][1] + 1:
                runs[-1][1] = row
            else:
                runs.append([row, row])
        ranges = ", ".join("R{}C{}:R{}C{}".format(first, column, last, column) for first,last in runs)
        notes.append("- {}: {}".format(ranges, json.dumps(number_format, ensure_ascii=False)))
    format_notes = ("\n\n## 原始数值格式\n\n格式与原值共同解释；百分比、前导零和未知格式不得忽略。\n" + "\n".join(notes)) if notes else ""
    return "\n".join(lines) + render_intersecting_merges(merges, cells) + format_notes


def render_full_table_markdown(source_id, table, cells, merges, sheet_name=None, epoch=None):
    title = table.get("title") or table.get("table_id") or ""
    header = "# {}\n\nsource_id=`{}` table_id=`{}`\n".format(
        title, source_id, table.get("table_id"))
    if sheet_name:
        header += "工作表：{}\n".format(sheet_name)
    return header + "\n" + _compact_grid(cells, merges, epoch=epoch)


def render_full_sheet_markdown(source_id, sheet, cells, merges):
    return "# 工作表 {}\n\n{}".format(
        sheet.get("name") or sheet.get("sheet_id"),
        _compact_grid(cells, merges, epoch=sheet.get("date_epoch")))


def full_table_body(connection, source_id: str, table_id: str) -> str:
    """Read and render one complete logical table from its SQLite grid."""
    table = read_logical_table(connection, table_id)
    cells = [dict(row) for row in stream_cells(connection, table)]
    merges = [dict(row) for row in read_merge_ranges(connection, table)]
    sheet_row = connection.execute(
        "SELECT name,date_epoch FROM sheets WHERE sheet_id=?", (table["sheet_id"],)).fetchone()
    sheet_name = str(sheet_row["name"]) if sheet_row is not None else None
    body = render_full_table_markdown(source_id, dict(table), cells, merges, sheet_name=sheet_name,
        epoch=sheet_row["date_epoch"] if sheet_row is not None else None)
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='table_observations'").fetchone()
    if exists:
        observation = connection.execute(
            "SELECT page_number,caption_json,footnote_json,raw_item_json FROM table_observations WHERE table_id=?",
            (table_id,)).fetchone()
        if observation:
            raw = json.loads(observation["raw_item_json"] or "{}")
            item = raw.get("raw_item") or raw
            html = item.get("table_body") if isinstance(item, dict) else None
            if html and not cells:
                # A usable saved grid is the compact, coordinate-bearing
                # reading surface. Retain raw HTML as fallback for raw-only
                # observations, without sending a second copy of every value.
                body = "# {}\n\n{}".format(table["title"], html)
            elif not cells:
                body = "# {}\n\n{}".format(table["title"], raw.get("text") or "")
            notes = [(json.loads(observation[key] or "[]") or []) for key in ("caption_json", "footnote_json")]
            body += "\n\n" + "\n".join(str(note) for group in notes for note in (group if isinstance(group, list) else [group]))
            body += "\n来源页：{}".format(observation["page_number"])
    return body


def full_sheet_body(connection, source_id: str, sheet_id: str) -> str:
    """Read and render one complete worksheet from its SQLite grid."""
    sheet = connection.execute(
        "SELECT sheet_id,name,order_index,visibility,date_epoch FROM sheets WHERE sheet_id=?",
        (sheet_id,)).fetchone()
    if sheet is None:
        raise KeyError("SHEET_ID_UNKNOWN")
    cells = [dict(row) for row in connection.execute(
        "SELECT row_index,column_index,coordinate,raw_type,raw_value,formula,"
        "cached_value,number_format,date_candidate,empty_kind,hidden_row,hidden_column "
        "FROM cells WHERE sheet_id=? ORDER BY row_index,column_index",
        (sheet_id,)).fetchall()]
    merges = [dict(row) for row in connection.execute(
        "SELECT row_start,column_start,row_end,column_end,anchor FROM merges "
        "WHERE sheet_id=? ORDER BY row_start,column_start",
        (sheet_id,)).fetchall()]
    return render_full_sheet_markdown(source_id, dict(sheet), cells, merges)


def batch_by_row(cells: List[Mapping[str, Any]], row_count: int = 100):
    """Yield cells grouped by row, bounded to ``row_count`` rows per page."""
    by_row: Dict[int, List[Mapping[str, Any]]] = {}
    for cell in cells:
        by_row.setdefault(int(cell["row_index"]), []).append(cell)
    ordered_rows = sorted(by_row)
    for start in range(0, len(ordered_rows), max(1, int(row_count))):
        chunk = ordered_rows[start:start + max(1, int(row_count))]
        yield [cell for row_index in chunk for cell in by_row[row_index]]


def _table_observation_text(connection, table_id: str) -> str:
    """Return this table's retained raw observation text, if any."""
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='table_observations'").fetchone()
    if exists is None:
        return ""
    row = connection.execute(
        "SELECT raw_item_json FROM table_observations WHERE table_id=?", (table_id,)).fetchone()
    if row is None:
        return ""
    try:
        payload = json.loads(row["raw_item_json"] or "{}")
    except (TypeError, ValueError):
        return ""
    if isinstance(payload, dict):
        return str(payload.get("text") or "")
    return ""


# ──────────────────────────────────────────────
#  Engine-owned artifact paths (``raw-tables/…``)
# ──────────────────────────────────────────────

_RAW_TABLES_DIR = "raw-tables"
_ARTIFACT_SLUG_MAX = 24
# 太短的 ASCII 残片没有意义（"表 4：今年下半年" 的 NFKD 残片是 "4"），
# 与其把它塞进路径，不如老实用 fallback / 纯身份后缀。
_ARTIFACT_SLUG_MIN = 3
_SLUG_SEPARATORS = re.compile(r"[^a-z0-9]+")


def artifact_slug(value: str, *, fallback: str = "") -> str:
    """Return an ASCII lowercase kebab slug for one artifact path segment.

    路径段必须满足 ``compiler_state`` 的 canonical 约定（每段 lowercase
    kebab-case），发布出去的整棵 wiki 树里也没有任何非 ASCII 路径——所以纯中文
    标题在这里只会得到空 slug，此时回退到 ``fallback``（调用方给 ``doc`` /
    ``table``）：**可读性由页面标题与父索引里的目录标签承担**，见
    ``compile/rendering._directory_title``。标题里的 ASCII 部分仍然保留，
    例如 "代理手续费标准（20260324）" 得到 ``20260324``、"2026 Fee Schedule"
    得到 ``2026-fee-schedule``；短于 ``_ARTIFACT_SLUG_MIN`` 的残片按空处理。
    """
    text = unicodedata.normalize("NFKD", str(value or ""))
    ascii_text = text.encode("ascii", "ignore").decode("ascii").casefold()
    slug = _SLUG_SEPARATORS.sub("-", ascii_text).strip("-")
    slug = slug[:_ARTIFACT_SLUG_MAX].rstrip("-")
    if len(slug) < _ARTIFACT_SLUG_MIN:
        slug = ""
    return slug or fallback


def raw_table_label(connection, table) -> str:
    """Human-readable label for one logical table: its title, else sheet name."""
    title = str(table["title"] or "").strip()
    if title:
        return title
    try:
        row = connection.execute(
            "SELECT name FROM sheets WHERE sheet_id=?", (str(table["sheet_id"]),)).fetchone()
    except sqlite3.Error:
        return ""
    return str(row["name"] or "").strip() if row is not None else ""


def raw_table_title(table_id: str, label: str, *, part: Optional[int] = None) -> str:
    """Page title for a raw-table page: readable label first, id always kept.

    ``Raw table table-…``（旧标题）对读者毫无信息；表自己的标题/工作表名在
    ``logical_tables.title`` / ``sheets.name`` 里本来就有，直接拿来用，同时把
    table_id 留在标题末尾，保证页面上永远能拿到可操作的表标识。
    """
    name = str(label or "").strip() or "Raw table"
    title = "{} · {}".format(name, table_id)
    if part:
        title += " · part {}".format(int(part))
    return title


def raw_table_page_path(source_id: str, table_id: str, *, title: str = "",
                        part: Optional[int] = None, source_name: str = "") -> str:
    """``raw-tables/<doc-slug>-<source16>/<table-slug>-<table_id>/<part>.md``.

    两段都带「可读前缀 + 可回查的身份后缀」：

    * 来源段尾部的 16 位十六进制就是 ``sources/source-<16 hex>.md`` 用的同一个
      哈希，渲染层据此把目录标签换回来源页的人类可读标题；
    * 表段尾部的 ``table-…`` 就是 table_id 本身，渲染层据此换回表导航页标题，
      查询链路也能从路径直接认出是哪张表，不必反解哈希。
    """
    source_digest = hashlib.sha256(str(source_id).encode("utf-8")).hexdigest()[:16]
    doc_dir = "{}-{}".format(artifact_slug(source_name, fallback="doc"), source_digest)
    table_slug = artifact_slug(title, fallback="")
    table_dir = "{}-{}".format(table_slug, table_id) if table_slug else str(table_id)
    return "{}/{}/{}/{:04d}.md".format(_RAW_TABLES_DIR, doc_dir, table_dir, int(part or 0))


def iter_full_table_page(table_id: str, source_id: str, connection,
                         source_name: str = ""):
    """Yield one complete raw-table page for one logical table.

    This is the keep-and-search search surface built from the shared full table
    reading representation (single metadata header + every saved cell), never
    the old row-paged messages with repeated headers.
    """
    from jonex_wiki.domain.engine.models.query_models import RawWikiPage

    table = read_logical_table(connection, table_id)
    body = full_table_body(connection, source_id, table_id)
    label = raw_table_label(connection, table)
    title = raw_table_title(table_id, label)
    yield RawWikiPage(
        path=raw_table_page_path(source_id, table_id, title=label, source_name=source_name),
        body=body,
        content_kind="table_raw",
        source_id=str(source_id),
        table_id=str(table_id),
        title=title[:200],
        structure_status=str(table["status"]),
        row_bounds={"row_start": int(table["row_start"]), "row_end": int(table["row_end"])},
    )


def iter_raw_table_pages(table_id: str, source_id: str, connection,
                         source_name: str = ""):
    """Yield complete raw-table pages for one logical table.

    ``connection`` is any read-only SQLite connection to a published grid; the
    compile and migration paths open it identically.  Every page is emitted and
    linked to its neighbours, never just the first preview page.
    """
    from jonex_wiki.domain.engine.models.query_models import RawWikiPage

    table = read_logical_table(connection, table_id)
    cells = [dict(row) for row in stream_cells(connection, table)]
    merges = [dict(row) for row in read_merge_ranges(connection, table)]
    status = str(table["status"])
    label = raw_table_label(connection, table)

    def make_page(part: int, rows: List[Mapping[str, Any]], body_prefix: str) -> RawWikiPage:
        row_indices = [int(cell.get("row_index")) for cell in rows if cell.get("row_index") is not None]
        row_bounds = {"row_start": min(row_indices), "row_end": max(row_indices)} if row_indices else None
        title = raw_table_title(table_id, label, part=part)
        body = (
            "# {}\n\n".format(title)
            + "source_id=`{}`\n\ntable_id=`{}`\n\n".format(source_id, table_id)
            + "structure_status=`{}`\n\n".format(status)
            + body_prefix
            + render_coordinate_records(rows)
            + render_intersecting_merges(merges, rows)
        )
        return RawWikiPage(
            path=raw_table_page_path(source_id, table_id, title=label, part=part,
                                     source_name=source_name),
            body=body,
            content_kind="table_raw",
            source_id=str(source_id),
            table_id=str(table_id),
            title=title[:200],
            structure_status=status,
            row_bounds=row_bounds,
        )

    pages: List[RawWikiPage] = []
    if cells:
        pages = [make_page(part, rows, "") for part, rows in enumerate(batch_by_row(cells, row_count=100))]
    else:
        observation = _table_observation_text(connection, table_id)
        if observation:
            pages = [make_page(0, [], "```markdown\n" + observation.rstrip("\n") + "\n```\n")]
        else:
            # Keep a directory/index entry for the empty artifact without
            # fabricating any content.
            pages = [make_page(0, [], "")]

    for index, page in enumerate(pages):
        related = [pages[index - 1].path] if index > 0 else []
        if index + 1 < len(pages):
            related.append(pages[index + 1].path)
        yield page.model_copy(update={"related_pages": related})


__all__ = ["ScopeSpec", "TableReader", "READ_RESPONSE_LIMIT", "MAX_MERGED_PAGES",
           "MISSING_RANGE_LIMIT", "iter_raw_table_pages", "iter_full_table_page",
           "batch_by_row",
           "render_coordinate_records", "render_intersecting_merges",
           "render_full_table_markdown", "render_full_sheet_markdown",
           "full_table_body", "full_sheet_body"]
