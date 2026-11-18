"""Persist MinerU/PDF table observations in the shared logical-table schema.

A parsed observation is not a physical PDF cell grid: every table keeps its
own sheet, its own row/column origin and its own captured page/bbox, and a
fragment this parser cannot turn into a complete grid stays raw and is
marked ambiguous instead of being written as a wrong table.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from jonex_wiki.domain.engine.compile.source_prep.blocks import ParseResult
from jonex_wiki.domain.engine.compile.source_prep.chunking import PackingError
from jonex_wiki.domain.engine.compile.source_prep.header_interpretation import (
    HEADER_RULE_VERSION,
    interpretation_identity,
    interpret_table,
)
from jonex_wiki.domain.engine.compile.source_prep.spreadsheet_view import _create_schema
from jonex_wiki.domain.engine.compile.source_prep.tables import html_table_regions


def _observation_identity(result: ParseResult, block) -> str:
    """Sorted-JSON identity of one observation.

    The identity is built from the source identity, the document identity, the
    content hash and the position inside the source, so two different sources
    with byte-identical content never share a table id, and the sorted JSON
    encoding stays unambiguous when a value itself contains separators.
    """
    return json.dumps({
        "position": block.position,
        "source_id": str(result.source_id),
        "document_id": str(result.document_id),
        "source_sha256": str(result.source_sha256),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _table_ids(result: ParseResult, block) -> tuple[str, str]:
    identity = _observation_identity(result, block)
    table_id = "table-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    # Every table owns its own sheet, derived from its own table id.
    sheet_id = "sheet-" + hashlib.sha256((table_id + ":sheet").encode("utf-8")).hexdigest()[:16]
    return table_id, sheet_id


def _interpretation_id(table_id: str) -> str:
    # The shared header rule owns this identity: a reading produced by a new
    # rule is a new interpretation, so requests, cursors and caches bound to the
    # old one can never be replayed against it.
    return interpretation_identity(table_id)


def persist_table_observations(result: ParseResult, output_dir: Path) -> Path | None:
    observations = [block for block in result.blocks if block.block_type == "table_observation"]
    if not observations:
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    database = output_dir / "tables.sqlite"
    temporary = output_dir / (".tables-{}.sqlite".format(uuid.uuid4().hex))
    connection = sqlite3.connect(temporary)
    decisions: list[tuple[object, dict]] = []
    skipped: list[tuple[str, str]] = []
    omissions: list[tuple[str, str]] = []
    try:
        _create_schema(connection)
        connection.execute("CREATE TABLE table_observations(table_id TEXT,page_number INTEGER,"
            "bbox_json TEXT,caption_json TEXT,footnote_json TEXT,raw_item_json TEXT,status TEXT)")
        for ordinal, block in enumerate(observations, start=1):
            table_id, sheet_id = _table_ids(result, block)
            default_title = "PDF table" if Path(result.source_name).suffix.casefold() == ".pdf" else "Table"
            title = _first_text(block.metadata.get("caption")) or "{} {}".format(default_title, ordinal)
            rows, structure = _table_rows(block.text)
            decision = {"table_id": table_id, "sheet_id": sheet_id,
                        "interpretation_id": _interpretation_id(table_id),
                        "tables_sqlite_local_path": str(database), "compile_table_values": False,
                        "source_name": result.source_name}
            if structure is not None:
                # The fragment cannot be reconstructed as a grid: keep the raw
                # text and its page/bbox, claim no cells and say so explicitly.
                _write_raw_only(connection, table_id=table_id, sheet_id=sheet_id, ordinal=ordinal,
                                title=title, block=block, reason=structure,
                                source_id=result.source_id, document_id=result.document_id)
                decision.update(table_error=structure, interpretation_status="ambiguous",
                                observation_level="raw_fragment")
            else:
                try:
                    omitted, interpretation = _write_table(
                        connection, table_id=table_id, sheet_id=sheet_id,
                        ordinal=ordinal, title=title, block=block, rows=rows)
                except sqlite3.IntegrityError as error:
                    connection.execute("ROLLBACK TO table_observation")
                    connection.execute("RELEASE table_observation")
                    _record_table_error(connection, table_id, result, "TABLE_PERSIST_CONFLICT", str(error))
                    _write_raw_only(connection, table_id=table_id, sheet_id=sheet_id, ordinal=ordinal,
                                    title=title, block=block, reason="TABLE_PERSIST_CONFLICT",
                                    source_id=result.source_id, document_id=result.document_id)
                    skipped.append((table_id, "TABLE_PERSIST_CONFLICT"))
                    decision.update(table_error="TABLE_PERSIST_CONFLICT",
                                    interpretation_status="ambiguous",
                                    observation_level="raw_fragment")
                else:
                    decision.update(interpretation_status=interpretation.status,
                                    observation_level="table_region")
                    if omitted:
                        # The grid is complete and claimable; only the source's
                        # merge geometry is missing, and the compile says so.
                        omissions.append((table_id, omitted))
            decisions.append((block, decision))
        connection.commit()
    except BaseException:
        connection.close()
        temporary.unlink(missing_ok=True)
        raise
    connection.close()
    # The artifact is complete and closed; only now does it become the output.
    os.replace(temporary, database)
    for block, decision in decisions:
        block.metadata.update(decision)
        if decision["observation_level"] == "table_region":
            block.block_type = "table_reference"
    for table_id, code in skipped:
        result.warnings.append("table_skipped:{}:{}".format(code, table_id))
    for table_id, code in omissions:
        result.warnings.append("table_merges_omitted:{}:{}".format(code, table_id))
    return database


def _record_table_error(connection: sqlite3.Connection, table_id: str, result: ParseResult,
                        code: str, detail: str) -> None:
    connection.execute("INSERT INTO table_errors VALUES (?,?,?,?,?)",
                       (table_id, result.source_id, result.document_id, code, detail))


def _raw_only_payload(block, reason: str, table_id: str) -> dict:
    return {"fields": [], "observation_level": "raw_fragment", "cell_bbox_available": False,
            "structure_reason": reason, "raw_observation_text": block.text,
            "page_number": block.page_number, "bbox": block.metadata.get("bbox"),
            "coordinate_space": "parsed_table_row_column", "table_id": table_id,
            # A raw fragment claims no grid and no header: the raw text is
            # readable, nothing about its structure is confirmed.
            "header_rule": HEADER_RULE_VERSION,
            "grid": {"present": False, "complete": False, "reason": reason,
                     "row_count": 0, "column_count": 0, "evidence": "raw_fragment"},
            "header": {"confirmed": False, "reason": "GRID_ABSENT", "structure": "unconfirmed",
                       "header_rows": [], "unit": "", "unit_row": None, "unit_evidence": None,
                       "ambiguities": ["GRID_ABSENT"], "disambiguation": None, "fields": []}}


def _write_raw_only(connection: sqlite3.Connection, *, table_id: str, sheet_id: str, ordinal: int,
                    title: str, block, reason: str, source_id: str, document_id: str) -> None:
    """Register a table observation whose grid cannot be reconstructed."""
    connection.execute("SAVEPOINT raw_observation")
    try:
        connection.execute("INSERT INTO sheets VALUES (?,?,?,?,?,?,?)",
                           (sheet_id, title, ordinal, "visible", "not_applicable", "[]", "[]"))
        connection.execute("INSERT INTO logical_tables VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (table_id, sheet_id, ordinal, title, 1, 1, 1, 1, "ambiguous", "exclude",
             hashlib.sha256(block.text.encode("utf-8")).hexdigest()))
        connection.execute("INSERT INTO interpretations VALUES (?,?,?,?,?)",
            (_interpretation_id(table_id), table_id, 1, "ambiguous",
             json.dumps(_raw_only_payload(block, reason, table_id), ensure_ascii=False)))
        connection.execute("INSERT INTO table_observations VALUES (?,?,?,?,?,?,?)",
            (table_id, block.page_number, json.dumps(block.metadata.get("bbox")),
             json.dumps(block.metadata.get("caption"), ensure_ascii=False),
             json.dumps(block.metadata.get("footnote"), ensure_ascii=False),
             json.dumps({"text": block.text, "raw_item": block.metadata.get("raw_item")},
                        ensure_ascii=False),
             "ambiguous_raw_only"))
        connection.execute("INSERT INTO table_errors VALUES (?,?,?,?,?)",
                           (table_id, source_id, document_id, reason,
                            "no grid claimed; raw observation retained"))
    except BaseException:
        connection.execute("ROLLBACK TO raw_observation")
        connection.execute("RELEASE raw_observation")
        raise
    connection.execute("RELEASE raw_observation")


def _write_table(connection: sqlite3.Connection, *, table_id: str, sheet_id: str, ordinal: int,
                 title: str, block, rows: list[list[str]]) -> tuple[str, Any]:
    """Write one observed table, or leave no partial rows behind.

    A savepoint makes a single table atomic, so one conflict cannot abort the
    archive of the whole source or leave half of its cells behind.  Returns the
    reason the source's merge geometry was not stored (or an empty string) and
    the shared header interpretation this grid was admitted under.
    """
    connection.execute("SAVEPOINT table_observation")
    try:
        connection.execute("INSERT INTO sheets VALUES (?,?,?,?,?,?,?)",
                           (sheet_id, title, ordinal, "visible", "not_applicable", "[]", "[]"))
        width = max((len(row) for row in rows), default=0)
        regions, omitted = _merge_regions(block, rows, width)
        covered = {(row, column)
                   for r1, c1, r2, c2 in regions
                   for row in range(r1, r2 + 1) for column in range(c1, c2 + 1)
                   if (row, column) != (r1, c1)}
        for row_index, row in enumerate(rows, start=1):
            for column_index in range(1, width + 1):
                value = row[column_index - 1] if column_index <= len(row) else None
                # Row and column are this parsed table's own coordinates, never
                # a claim about physical PDF cell positions.
                empty_kind = None if value is not None else "unknown"
                if (row_index, column_index) in covered:
                    # The source merged this position into an anchor cell, so the
                    # text here is the anchor's own value repeated by the grid
                    # expansion -- not this cell's value.  Record that, instead of
                    # leaving an expanded repeat indistinguishable from a value
                    # the source really wrote twice.
                    empty_kind = "merged_covered"
                connection.execute("INSERT INTO cells VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sheet_id, row_index, column_index, "R{}C{}".format(row_index, column_index),
                     "str" if value is not None else "unknown", value, None, None, "General", 0,
                     empty_kind, 0, 0))
                if value:
                    connection.execute("INSERT INTO table_terms VALUES (?,?,?,?,?)",
                                       (table_id, value, sheet_id, row_index, column_index))
        # The merge geometry the expanded grid cannot carry, in this table's own
        # coordinate space; the anchor is the top-left cell of the region, which
        # is exactly the cell the expanded grid kept the text in.
        for row_start, column_start, row_end, column_end in regions:
            connection.execute("INSERT INTO merges VALUES (?,?,?,?,?,?)",
                               (sheet_id, row_start, column_start, row_end, column_end,
                                "R{}C{}".format(row_start, column_start)))
        # Header and unit interpretation: one shared deterministic rule, driven
        # by the grid plus this same merge geometry.  A grid whose header cannot
        # be confirmed is stored as ambiguous -- its raw text stays readable and
        # nothing about its structure is claimed.
        interpretation = interpret_table(rows, merges=regions, table_id=table_id)
        status = interpretation.status
        interpretation_id = _interpretation_id(table_id)
        connection.execute("INSERT INTO logical_tables VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (table_id, sheet_id, ordinal, title, 1, 1, max(1, len(rows)), max(1, width), status,
             "exclude", hashlib.sha256(block.text.encode("utf-8")).hexdigest()))
        fields = [field.payload() for field in interpretation.header.fields]
        payload = {"fields": fields, "observation_level": "table_region",
                   "cell_bbox_available": False,
                   "coordinate_space": "parsed_table_row_column",
                   "raw_observation_text": block.text, "page_number": block.page_number,
                   "bbox": block.metadata.get("bbox"),
                   **interpretation.payload()}
        repair=block.metadata.get("pdf_native_repair") or {}
        if repair.get("status")=="accepted":
            payload["pdf_native_repair"]={k:v for k,v in repair.items() if k not in {"original_observation","cell_locations"}}
            payload["cell_bbox_available"]=True
            connection.execute("CREATE TABLE IF NOT EXISTS table_source_cells(table_id TEXT,row_index INTEGER,column_index INTEGER,page_number INTEGER,bbox_json TEXT,word_ids_json TEXT,PRIMARY KEY(table_id,row_index,column_index))")
            for cell in repair.get("cell_locations",[]):
                connection.execute("INSERT INTO table_source_cells VALUES(?,?,?,?,?,?)",(table_id,cell["row"],cell["column"],block.page_number,json.dumps(cell["bbox"]),json.dumps(cell["word_ids"])))
        if omitted:
            payload["structure_reason"] = omitted
        connection.execute("INSERT INTO interpretations VALUES (?,?,?,?,?)",
            (interpretation_id, table_id, 1, status, json.dumps(payload, ensure_ascii=False)))
        for field in interpretation.header.fields:
            connection.execute("INSERT INTO field_map VALUES (?,?,?,?,?)",
                (interpretation_id, field.field_id, field.column, field.header_row, field.label))
        connection.execute("INSERT INTO table_observations VALUES (?,?,?,?,?,?,?)",
            (table_id, block.page_number, json.dumps(block.metadata.get("bbox")),
             json.dumps(block.metadata.get("caption"), ensure_ascii=False),
             json.dumps(block.metadata.get("footnote"), ensure_ascii=False),
             json.dumps({"text": block.text, "raw_item": block.metadata.get("raw_item")},
                        ensure_ascii=False),
             status))
    except BaseException:
        connection.execute("ROLLBACK TO table_observation")
        connection.execute("RELEASE table_observation")
        raise
    connection.execute("RELEASE table_observation")
    return omitted, interpretation


# A merge row is ~39 B against ~124 B for the cell row it annotates, so keeping
# regions under a quarter of the grid's cells bounds the merges table at well
# under a tenth of the grid's own bytes.  Measured on the 171x6 commission
# table: 44 regions (37 valued) = ~1.7 KB of 356 KB artifact; 256 is the hard
# ceiling for a table whose geometry is worth carrying at all.
_MERGE_REGION_LIMIT = 256
_MERGE_REGION_CELL_RATIO = 4


def _merge_regions(block, rows: list[list[str]], width: int) -> tuple[list[tuple[int, int, int, int]], str]:
    """The source's explicit spans, or the reason they are not stored.

    The block keeps the parser's raw item, so the HTML table the markdown grid
    was expanded from is still available here: the geometry is read back from
    the source, never guessed from the blanks the expansion left behind.  A
    table whose geometry is unreadable, does not fit the stored grid, or costs
    more than the budget below keeps its complete grid and claims no merges --
    an explicit omission the compile warnings carry, not a silent guess.
    """
    raw_item = block.metadata.get("raw_item")
    html = raw_item.get("table_body") if isinstance(raw_item, dict) else None
    if not html:
        return [], ""
    try:
        regions = html_table_regions(str(html))
    except PackingError:
        return [], "TABLE_SPANS_UNREADABLE"
    if not regions:
        return [], ""
    if any(row_end > len(rows) or column_end > max(1, width)
           for _row_start, _column_start, row_end, column_end in regions):
        return [], "TABLE_MERGES_GRID_MISMATCH"
    cells = sum(len(row) for row in rows)
    if len(regions) > _MERGE_REGION_LIMIT or len(regions) * _MERGE_REGION_CELL_RATIO > max(1, cells):
        return [], "TABLE_MERGES_OVERSIZED"
    return regions, ""


def _first_text(value) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return next((str(item).strip() for item in value if str(item).strip()), "")
    return ""


def _markdown_rows(text: str) -> list[list[str]]:
    rows = []
    for line in text.splitlines():
        if not (line.strip().startswith("|") and line.strip().endswith("|")):
            continue
        cells = [cell.strip().replace(r"\|", "|") for cell in re.split(r"(?<!\\)\|", line.strip()[1:-1])]
        if cells and all(re.fullmatch(r":?-+:?", cell.replace(" ", "")) for cell in cells):
            continue
        rows.append(cells)
    return rows


def _span_value(raw) -> int:
    """Read a colspan/rowspan attribute; only a positive integer widens a cell."""
    if raw is None:
        return 1
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return 1
    return value if value > 1 else 1


class _HTMLTable(HTMLParser):
    """Collect HTML rows plus anything a plain grid cannot represent."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self.spans: list[tuple[str, int, int]] = []
        self.nested = False
        self._table_depth = 0
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        lowered = tag.casefold()
        if lowered == "table":
            self._table_depth += 1
            if self._table_depth > 1:
                self.nested = True
        elif lowered == "tr":
            self._row = []
        elif lowered in {"td", "th"} and self._row is not None:
            self._cell = []
            attributes = {name.casefold(): value for name, value in attrs}
            colspan = _span_value(attributes.get("colspan"))
            rowspan = _span_value(attributes.get("rowspan"))
            if colspan > 1 or rowspan > 1:
                self.spans.append((lowered, colspan, rowspan))

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered == "table":
            self._table_depth = max(0, self._table_depth - 1)
        elif lowered in {"td", "th"} and self._row is not None and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif lowered == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


def _table_rows(text: str) -> tuple[list[list[str]], str | None]:
    """Return parsed rows and the reason a grid cannot be claimed, if any."""
    rows = _markdown_rows(text)
    if rows:
        return rows, None
    parser = _HTMLTable()
    parser.feed(text)
    if parser.nested:
        return parser.rows, "NESTED_TABLE_NOT_SUPPORTED"
    if parser.spans:
        return parser.rows, "SPAN_NOT_REPRESENTABLE"
    return parser.rows, None


__all__ = ["persist_table_observations"]
