"""Bounded reading projection of the saved grid, shared by every table angle.

No model, value filling, formula execution or domain-specific menu schema.
Dimensions always point to their own source cells (including merge anchors).
Date-axis inference is deliberately corroborated and remains a candidate.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from jonex_wiki.domain.engine.compile.source_prep.spreadsheet_view import excel_serial_iso
from jonex_wiki.domain.engine.query.table_reader import project_saved_cell_semantics

MAX_CELLS = 12000
MAX_RECORDS = 4000
_WEEKDAYS = {"星期一": 0, "周一": 0, "星期二": 1, "周二": 1, "星期三": 2,
             "周三": 2, "星期四": 3, "周四": 3, "星期五": 4, "周五": 4,
             "星期六": 5, "周六": 5, "星期日": 6, "星期天": 6, "周日": 6}


def _value(cell: dict) -> str:
    if cell.get("formula") and cell.get("cached_value") is None:
        return ""
    value = cell.get("effective_value", cell.get("raw_value"))
    return "" if value is None else str(value)


def _coordinate(row: int, column: int) -> str:
    letters = ""
    while column:
        column, remainder = divmod(column - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters + str(row)


def _field(cell: dict, *, label: str, key: str) -> dict:
    return {"key": key, "label": label, "value": _value(cell),
            "coordinate": str(cell.get("effective_coordinate") or cell.get("merge_anchor")
                              or cell.get("coordinate") or ""),
            "raw_coordinate": str(cell.get("coordinate") or ""),
            "inherited": bool(cell.get("merge_anchor") and not cell.get("raw_value"))}


def _date_axes(grid: dict, table: dict, sheet: dict) -> tuple[int, dict]:
    """Require a real date style or serial + weekday + worksheet month/day.

    A general-format number alone is never sufficient evidence of a date.
    Source filenames and sheet names are supporting metadata, never values.
    """
    for row in range(table["row_start"], min(table["row_start"] + 3, table["row_end"] + 1)):
        axes = {}
        for column in range(table["column_start"], table["column_end"] + 1):
            cell = grid.get((row, column), {})
            raw = _value(cell)
            explicit = cell.get("date_candidate") and not str(cell.get("number_format") or "").startswith("builtin:")
            iso = (cell.get("normalized_value") or excel_serial_iso(raw, sheet.get("date_epoch") or "1900")) if explicit else None
            evidence = []
            if not iso and cell.get("raw_type") == "n":
                iso = excel_serial_iso(raw, sheet.get("date_epoch") or "1900")
                if iso:
                    parsed = date.fromisoformat(iso[:10])
                    weekday = next((grid.get((r, column), {}) for r in (row - 1, row + 1)
                                    if _value(grid.get((r, column), {})) in _WEEKDAYS), None)
                    name_parts = re.findall(r"(\d{1,2})[.月/](\d{1,2})", sheet.get("name", ""))
                    in_range = (len(name_parts) == 2 and
                        tuple(map(int, name_parts[0])) <= (parsed.month, parsed.day) <= tuple(map(int, name_parts[1])))
                    if not weekday or _WEEKDAYS[_value(weekday)] != parsed.weekday() or not in_range:
                        iso = None
                    else:
                        evidence = [weekday.get("coordinate"), "sheet:" + sheet["name"]]
            if iso:
                axes[column] = {"value": iso[:10], "raw_value": raw,
                    "coordinate": cell.get("coordinate"),
                    "status": "formatted_date" if explicit else "candidate",
                    "evidence": evidence}
        if len(axes) >= 2:
            return row, axes
    return 0, {}


def project_table(connection: sqlite3.Connection, table: dict, sheet: dict,
                  *, max_cells: int = MAX_CELLS) -> dict:
    """One complete or explicitly bounded logical-table reading model."""
    bounds = [table[k] for k in ("row_start", "row_end", "column_start", "column_end")]
    r1, r2, c1, c2 = map(int, bounds)
    width = c2 - c1 + 1
    if width > max_cells:
        return {"table_id": table["table_id"], "sheet_id": sheet["sheet_id"],
            "title": table["title"], "structure_status": table["status"],
            "bounds": {"row_start": r1, "row_end": r2, "column_start": c1, "column_end": c2},
            "rows": [], "merges": [], "records": [], "dimensions": [], "has_dates": False,
            "date_inferred": False, "complete": False, "shown_row_end": r1 - 1, "record_count": 0}
    end = min(r2, r1 + max(1, max_cells // max(1, width)) - 1)
    cells = [dict(r) for r in connection.execute(
        "SELECT * FROM cells WHERE sheet_id=? AND row_index BETWEEN ? AND ? "
        "AND column_index BETWEEN ? AND ? ORDER BY row_index,column_index",
        (sheet["sheet_id"], r1, end, c1, c2))]
    merges = [dict(r) for r in connection.execute(
        "SELECT * FROM merges WHERE sheet_id=? AND row_end>=? AND row_start<=?",
        (sheet["sheet_id"], r1, end))]
    grid_coordinates = any(re.fullmatch(r"R[0-9]+C[0-9]+", str(cell.get("coordinate") or "")) for cell in cells)
    def coordinate(row: int, column: int) -> str:
        return "R{}C{}".format(row, column) if grid_coordinates else _coordinate(row, column)
    saved = {(int(cell["row_index"]), int(cell["column_index"])): cell for cell in cells}
    # OOXML can omit covered cells entirely. A dense, bounded display grid
    # makes those positions explicit; only a saved merge can supply a value.
    cells = [saved.get((r, c), {"row_index": r, "column_index": c,
        "coordinate": coordinate(r, c), "raw_value": None, "empty_kind": "not_stored"})
        for r in range(r1, end + 1) for c in range(c1, c2 + 1)]
    projected = project_saved_cell_semantics(cells, merges, epoch=sheet.get("date_epoch"))
    grid = {(int(c["row_index"]), int(c["column_index"])): c for c in projected}
    for (r, c), cell in grid.items():
        cell["coordinate"] = cell.get("coordinate") or coordinate(r, c)
        anchor = cell.get("merge_anchor")
        if anchor:
            match = re.fullmatch(r"R(\d+)C(\d+)", str(anchor))
            cell["effective_coordinate"] = coordinate(int(match[1]), int(match[2])) if match else str(anchor)
    rows = [{"row": r, "cells": [{"column": c, "coordinate": coordinate(r, c),
        "raw_value": grid.get((r, c), {}).get("raw_value"),
        "value": _value(grid.get((r, c), {})),
        "formula": grid.get((r, c), {}).get("formula"),
        "cached_value": grid.get((r, c), {}).get("cached_value"),
        "number_format": grid.get((r, c), {}).get("number_format"),
        "anchor": grid.get((r, c), {}).get("effective_coordinate"),
        "hidden": bool(grid.get((r, c), {}).get("hidden_row") or grid.get((r, c), {}).get("hidden_column")),
        "empty_kind": grid.get((r, c), {}).get("empty_kind")}
        for c in range(c1, c2 + 1)]} for r in range(r1, end + 1)]
    date_row, axes = _date_axes(grid, table, sheet)
    records = []
    if axes:
        columns = sorted(axes)
        for r in range(date_row + 1, end + 1):
            dimensions = [_field(grid.get((r, c), {}), label="分组 {}".format(c - c1 + 1), key="c" + str(c))
                          for c in range(c1, columns[0])]
            # A row with no source grouping is an annotation/signature, not a fact.
            if not dimensions or not any(d["value"] for d in dimensions):
                continue
            for i, c in enumerate(columns):
                cell = grid.get((r, c), {})
                if not _value(cell) or cell.get("hidden_row") or cell.get("hidden_column"):
                    continue
                measures = []
                next_column = columns[i + 1] if i + 1 < len(columns) else c2 + 1
                for mc in range(c + 1, next_column):
                    header = next((grid.get((hr, mc), {})
                        for hr in range(r1, min(date_row + 2, end + 1))
                        if _value(grid.get((hr, mc), {})) and
                        _value(grid.get((hr, mc), {})) not in _WEEKDAYS), {})
                    label = _value(header)
                    if label:
                        measure = _field(grid.get((r, mc), {}), label=label, key="c" + str(mc))
                        measure["label_coordinate"] = header.get("coordinate")
                        measures.append(measure)
                records.append({"id": "{}:{}:{}".format(table["table_id"], r, c),
                    "row": r, "value": _value(cell), "coordinate": cell["coordinate"],
                    "date": axes[c], "dimensions": dimensions, "measures": measures})
    else:
        interpretation = connection.execute(
            "SELECT payload_json FROM interpretations WHERE table_id=? ORDER BY version DESC LIMIT 1",
            (table["table_id"],)).fetchone()
        payload = json.loads(interpretation[0]) if interpretation else {}
        header_rows = (payload.get("header_rows") or (payload.get("header") or {}).get("header_rows")
                       or [f.get("header_row", r1) for f in payload.get("fields") or []] or [r1])
        header_end = max(int(row) for row in header_rows)
        field_labels = {int(f.get("column", f.get("column_index", 0))): str(f.get("label") or "")
                        for f in payload.get("fields") or []}
        labels = {c: field_labels.get(c) or _value(grid.get((r1, c), {})) or "第 {} 列".format(c)
                  for c in range(c1, c2 + 1)}
        for r in range(header_end + 1, end + 1):
            fields = [_field(grid.get((r, c), {}), label=labels[c], key="c" + str(c)) for c in range(c1, c2 + 1)]
            if not any(f["value"] for f in fields):
                continue
            records.append({"id": "{}:{}".format(table["table_id"], r), "row": r,
                "value": next(f["value"] for f in fields if f["value"]),
                "coordinate": next((f["coordinate"] for f in fields if f["value"]), ""),
                "date": None, "dimensions": fields, "measures": []})
    dimensions = []
    for key in dict.fromkeys(d["key"] for r in records for d in r["dimensions"]):
        values = [d for r in records for d in r["dimensions"] if d["key"] == key and d["value"]]
        counts = Counter(d["value"] for d in values)
        if 1 < len(counts) <= 40 and (axes or max(counts.values(), default=0) > 1):
            dimensions.append({"key": key, "label": values[0]["label"], "values": list(counts)})
    context = []
    if not axes:
        context = [" / ".join(dict.fromkeys(_value(grid.get((r, c), {}))
            for c in range(c1, c2 + 1) if _value(grid.get((r, c), {}))))
            for r in range(r1, min(header_end, end) + 1)]
    if connection.execute("SELECT 1 FROM sqlite_master WHERE name='table_observations'").fetchone():
        notes = connection.execute("SELECT caption_json,footnote_json FROM table_observations WHERE table_id=?",
                                   (table["table_id"],)).fetchone()
        if notes:
            for text in notes:
                value = json.loads(text or "[]")
                context.extend(str(item) for item in (value if isinstance(value, list) else [value]) if item)
    return {"table_id": table["table_id"], "sheet_id": sheet["sheet_id"],
        "context": list(dict.fromkeys(text for text in context if text)),
        "title": (sheet["name"] if axes else table["title"]), "structure_status": table["status"],
        "bounds": {"row_start": r1, "row_end": r2, "column_start": c1, "column_end": c2},
        "rows": rows, "merges": merges, "records": records[:MAX_RECORDS], "dimensions": dimensions,
        "has_dates": bool(axes), "date_inferred": any(a["status"] == "candidate" for a in axes.values()),
        "complete": end == r2 and len(records) <= MAX_RECORDS,
        "shown_row_end": end, "record_count": len(records),
        "missing_value_policy": "blank_is_unfilled; only_explicit_merges_inherit"}


def read_table_data(pub: Path, index: dict) -> dict | None:
    """Attach the page's source workbook, bound to its publication and owner."""
    table_id = str(index.get("object_id") or "")
    scope = index.get("scope") or {}
    if not table_id:
        table_id = str(scope.get("table_id") or "")
    if not table_id.startswith("table-"):
        return None
    from jonex_wiki.domain.engine.json_artifacts import load_json_artifact
    if not (pub / "sources/registry.json").is_file():
        return None
    registry = load_json_artifact(lambda name: (pub / name).read_bytes(), "sources/registry.json")
    source = next((s for s in registry.get("sources", []) if table_id in s.get("table_ids", [])), None)
    if not source or not source.get("tables"):
        return None
    expected_owner = str(scope.get("source_id") or "")
    if expected_owner and expected_owner not in {source["source_id"], source["document_id"]}:
        raise ValueError("TABLE_SOURCE_OWNER_MISMATCH")
    relative = str(source["tables"])
    path = (pub / relative).resolve()
    if not path.is_relative_to(pub.resolve()):
        raise ValueError("TABLE_PATH_OUTSIDE_PUBLICATION")
    manifest = json.loads((pub / "indexes/manifest.json").read_text())
    entry = (manifest.get("files") or {}).get(relative)
    expected = entry.get("sha256") if isinstance(entry, dict) else entry
    if not expected or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError("TABLE_ARTIFACT_HASH_MISMATCH")
    with sqlite3.connect("file:{}?mode=ro&immutable=1".format(path), uri=True) as conn:
        conn.row_factory = sqlite3.Row
        sheets = [dict(s) for s in conn.execute("SELECT * FROM sheets ORDER BY order_index")]
        remaining = MAX_CELLS
        for sheet in sheets:
            sheet["tables"] = []
            for row in conn.execute("SELECT * FROM logical_tables WHERE sheet_id=? ORDER BY ordinal", (sheet["sheet_id"],)):
                if remaining <= 0:
                    sheet["truncated"] = True
                    continue
                projected = project_table(conn, dict(row), sheet, max_cells=remaining)
                remaining -= sum(len(r["cells"]) for r in projected["rows"])
                sheet["tables"].append(projected)
    return {"schema_version": 1, "revision": pub.name,
        "document_id": source["document_id"], "source_id": source["source_id"],
        "selected_table_id": table_id, "sheets": sheets,
        "complete": all(not s.get("truncated") and all(t["complete"] for t in s["tables"]) for s in sheets)}
