"""Explicit predicates over a verified native table already delivered in full.

This is an execution adapter, never a natural-language intent parser. A read
that failed admission cannot grant this adapter access to a query's evidence.
"""
from __future__ import annotations

import sqlite3
from collections import Counter

from .table_query import TableQueryPlan, execute_table_plan
from .table_reader import (
    full_table_body, project_saved_cell_semantics, read_logical_table,
    read_merge_ranges, stream_cells,
)


def execute_native_plan(view, info, table_id, delivered_text, arguments):
    path = str(info.get("path") or "")
    if not path or not info.get("document_id") or not delivered_text:
        raise ValueError("NATIVE_COMPUTE_READ_IDENTITY_MISSING")
    if hasattr(view, "verified_file_path"):
        view.verified_file_path(path)
    else:
        view.read_bytes(path)
    with view.open_sqlite(path) as connection:
        connection.row_factory = sqlite3.Row
        table = dict(read_logical_table(connection, table_id))
        original = full_table_body(connection, info.get("source_id") or info["document_id"], table_id)
        # A reader may append explicit quality notes after the original table.
        # It must have delivered the entire native body, not a matching preview.
        if not original or not (delivered_text == original or delivered_text.startswith(original + "\n\n")):
            raise ValueError("NATIVE_COMPUTE_WHOLE_READ_REQUIRED")
        interpretation = connection.execute(
            "SELECT * FROM interpretations WHERE table_id=? ORDER BY version DESC LIMIT 1",
            (table_id,),
        ).fetchone()
        if table["status"] != "accepted" or not interpretation or interpretation["status"] != "accepted":
            raise ValueError("NATIVE_COMPUTE_STRUCTURE_UNCONFIRMED")
        fields = [dict(row) for row in connection.execute(
            "SELECT * FROM field_map WHERE interpretation_id=? ORDER BY column_index",
            (interpretation["interpretation_id"],),
        )]
        expected_columns = list(range(table["column_start"], table["column_end"] + 1))
        if not fields or [f["column_index"] for f in fields] != expected_columns:
            raise ValueError("NATIVE_COMPUTE_FIELDS_INCOMPLETE")
        header_end = max(f["header_row"] for f in fields)
        if not table["row_start"] <= header_end < table["row_end"]:
            raise ValueError("NATIVE_COMPUTE_HEADER_UNCONFIRMED")
        cells = [dict(row) for row in stream_cells(connection, table)]
        if any(c.get("formula") and c.get("cached_value") is None
               for c in cells if c["row_index"] > header_end):
            raise ValueError("NATIVE_COMPUTE_UNRESOLVED_FORMULA")
        cells = project_saved_cell_semantics(cells, [dict(row) for row in read_merge_ranges(connection, table)])
    labels = Counter(f["label"] for f in fields if f["label"])
    columns = [f["label"] if f["label"] and labels[f["label"]] == 1 else f["field_id"] for f in fields]
    aliases = {f["field_id"]: index for index, f in enumerate(fields)}
    positions = {(c["row_index"], c["column_index"]): c for c in cells}
    rows = []
    for row_index in range(header_end + 1, table["row_end"] + 1):
        values = []
        for field in fields:
            cell = positions.get((row_index, field["column_index"]), {})
            value = cell.get("cached_value") if cell.get("formula") else cell.get("effective_value", cell.get("raw_value"))
            values.append("" if value is None else str(value))
        rows.append({"source_row": {"table_row": row_index,
            "passage_id": "table_range_" + table_id, "document_id": info["document_id"]}, "cells": values})
    catalog = [{"handle": table_id, "document_id": info["document_id"], "section": table["title"],
        "uncertain": False, "width_mismatch": False, "columns": columns, "field_aliases": aliases,
        "rows": rows, "fragment_ids": ["table_range_" + table_id], "excluded_fragments": [],
        "merge_proof": {"kind": "one_verified_native_table"},
        "range_proof": {"scope_kind": "full_table_request"}}]
    allowed = {"operation", "filters", "filter_groups", "return_columns", "group_by",
               "value_column", "min_distinct", "target_column", "cell_match"}
    if set(arguments) - allowed - {"table_id", "request_id"}:
        raise ValueError("NATIVE_COMPUTE_ARGUMENT_UNKNOWN")
    if arguments.get("table_id", table_id) != table_id or arguments.get("request_id"):
        raise ValueError("NATIVE_COMPUTE_SCOPE_MISMATCH")
    plan = TableQueryPlan(table=table_id, **{key: value for key, value in arguments.items() if key in allowed})
    result = execute_table_plan(catalog, plan)
    result["coverage"].update(scope_kind="full_table_request", execution_basis="delivered_native_grid",
        hidden_rows="included_as_saved", fields=[{"field_id": f["field_id"], "label": f["label"]} for f in fields])
    return plan, result
