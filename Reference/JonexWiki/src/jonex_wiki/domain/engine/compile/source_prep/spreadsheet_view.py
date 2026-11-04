"""Native XLSX/XLSM evidence extraction using only OOXML and SQLite.

Macros are never executed.  Raw cell values, formula text, cached values,
formats, merge anchors, hidden-row policy and logical-region interpretations
remain separate so an unknown value cannot collapse into a blank.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
import zipfile
from datetime import datetime, timedelta
from collections import deque
from pathlib import Path
from typing import Any, Dict, Iterable
from xml.etree import ElementTree as ET

from jonex_wiki.domain.engine.compile.source_prep.blocks import InputRoute, ParseBlock, ParseResult, sha256_file
# The repeated-label rule has exactly one implementation, shared with the
# parsed-table artifact path; it is re-exported here for the existing import
# sites and tests that name it in this module.
from jonex_wiki.domain.engine.compile.source_prep.header_interpretation import (
    _PLACEHOLDER_LABEL,
    qualify_repeating_labels as _qualify_repeating_labels,
)

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"


def _tag(ns: str, name: str) -> str:
    return "{" + ns + "}" + name


def _identity_id(prefix: str, length: int, source_id: str, document_id: str,
                 source_hash: str, position: Dict[str, Any]) -> str:
    """Stable table/sheet id from the source identity and the in-source position.

    Sorted JSON keeps a value that contains a separator from colliding with a
    different position, and two sources with byte-identical content still get
    their own ids.
    """
    identity = json.dumps({"source_id": str(source_id), "document_id": str(document_id),
                           "source_sha256": str(source_hash), "position": position},
                          ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return prefix + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:length]


_INSTRUCTION_MARKERS = ("填写", "逐条累加")
_SEQUENCE_FIELD_NAMES = ("序号", "编号")


def _merge_bounds_for_cell(merges: Iterable[str], row: int, column: int) -> tuple[int, int, int, int] | None:
    """Return (row_start, column_start, row_end, column_end) covering a cell."""
    for merge in merges:
        left, right = merge.split(":") if ":" in merge else (merge, merge)
        r1, c1 = _coordinates(left)
        r2, c2 = _coordinates(right)
        if r1 <= row <= r2 and c1 <= column <= c2:
            return (r1, c1, r2, c2)
    return None


def _instruction_header_decision(cells: Dict[tuple[int, int], Dict[str, Any]],
                                 merges: list[str], r1: int, c1: int, r2: int,
                                 c2: int) -> tuple[int, str | None]:
    """Narrow two-level "instruction + field-name" header detection.

    This is deliberately not a general header inference engine: it only fires
    when the region's first row is an explicit fill instruction and the second
    row is an unambiguous, unique field-name row with exactly one sequence
    column that is an independent integer id for every business row.  A present
    instruction with a conflicting candidate is reported as an ambiguity rather
    than silently guessed.
    """
    if r2 < r1 + 2 or c2 < c1:
        return 1, None
    first_values = [_display(cells.get((r1, column), {})) for column in range(c1, c2 + 1)]
    if not any(any(marker in value for marker in _INSTRUCTION_MARKERS) for value in first_values):
        return 1, None
    second_values = [_display(cells.get((r1 + 1, column), {})) for column in range(c1, c2 + 1)]
    if any(not value for value in second_values):
        return 1, "instruction_header_conflict_field_row_incomplete"
    if len(set(second_values)) != len(second_values):
        return 1, "instruction_header_conflict_duplicate_fields"
    sequence_columns = [column for column in range(c1, c2 + 1)
                        if second_values[column - c1] in _SEQUENCE_FIELD_NAMES]
    if len(sequence_columns) != 1:
        return 1, "instruction_header_conflict_sequence_label"
    sequence_column = sequence_columns[0]
    if _merge_bounds_for_cell(merges, r1 + 1, sequence_column) is not None:
        return 1, "instruction_header_conflict_sequence_header_merged"
    sequence_values = []
    for row in range(r1 + 2, r2 + 1):
        if _merge_bounds_for_cell(merges, row, sequence_column) is not None:
            return 1, "instruction_header_conflict_sequence_data_merged"
        sequence_values.append(_display(cells.get((row, sequence_column), {})))
    if len(sequence_values) < 2:
        return 1, "instruction_header_conflict_too_few_data_rows"
    if any(not value for value in sequence_values):
        return 1, "instruction_header_conflict_sequence_empty"
    if any(re.fullmatch(r"[0-9]+", value) is None for value in sequence_values):
        return 1, "instruction_header_conflict_sequence_not_integer"
    if len(set(sequence_values)) != len(sequence_values):
        return 1, "instruction_header_conflict_sequence_duplicate"
    return 2, None


def _column_number(cell_ref: str) -> int:
    letters = re.match(r"[A-Z]+", cell_ref.upper())
    if not letters:
        raise ValueError("invalid XLSX cell reference: " + cell_ref)
    value = 0
    for character in letters.group(0):
        value = value * 26 + ord(character) - 64
    return value


def _coordinates(cell_ref: str) -> tuple[int, int]:
    row = re.search(r"[0-9]+$", cell_ref)
    if row is None:
        raise ValueError("invalid XLSX cell reference: " + cell_ref)
    return int(row.group(0)), _column_number(cell_ref)


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return ["".join(node.text or "" for node in item.iter() if node.tag.endswith("}t"))
            for item in root.findall("m:si", NS)]


def _relationships(archive: zipfile.ZipFile, path: str) -> Dict[str, str]:
    try:
        root = ET.fromstring(archive.read(path))
    except KeyError:
        return {}
    return {item.attrib["Id"]: item.attrib["Target"] for item in root.findall(_tag(PKG_REL, "Relationship"))}


def _styles(archive: zipfile.ZipFile) -> tuple[list[str], set[int]]:
    try:
        root = ET.fromstring(archive.read("xl/styles.xml"))
    except KeyError:
        return ["General"], set()
    formats = {int(item.attrib["numFmtId"]): item.attrib.get("formatCode", "General")
               for item in root.findall("m:numFmts/m:numFmt", NS)}
    builtins = {0: "General", 14: "mm-dd-yy", 15: "d-mmm-yy", 16: "d-mmm",
                17: "mmm-yy", 22: "m/d/yy h:mm"}
    xfs = root.findall("m:cellXfs/m:xf", NS)
    style_formats = [formats.get(int(xf.attrib.get("numFmtId", 0)), builtins.get(int(xf.attrib.get("numFmtId", 0)),
                         "builtin:" + xf.attrib.get("numFmtId", "0"))) for xf in xfs]
    date_styles = {index for index, value in enumerate(style_formats)
                   if not value.startswith("builtin:")
                   and re.search(r"(?<!\\)[ymdhis]", re.sub(r'"[^"]*"', "", value), re.I)}
    return style_formats or ["General"], date_styles


def _regions(cells: Dict[tuple[int, int], Dict[str, Any]], merges: list[str]) -> list[tuple[int, int, int, int]]:
    occupied = set(cells)
    for merge in merges:
        left, right = merge.split(":") if ":" in merge else (merge, merge)
        r1, c1 = _coordinates(left)
        r2, c2 = _coordinates(right)
        occupied.update((row, column) for row in range(r1, r2 + 1) for column in range(c1, c2 + 1))
    output = []
    while occupied:
        start = min(occupied)
        queue, component = deque([start]), {start}
        occupied.remove(start)
        while queue:
            row, column = queue.popleft()
            for neighbor in ((row - 1, column), (row + 1, column), (row, column - 1), (row, column + 1)):
                if neighbor in occupied:
                    occupied.remove(neighbor)
                    component.add(neighbor)
                    queue.append(neighbor)
        rows, columns = zip(*component)
        output.append((min(rows), min(columns), max(rows), max(columns)))
    return sorted(output)


def _display(cell: Dict[str, Any]) -> str:
    value = cell.get("cached_value") if cell.get("formula") and cell.get("cached_value") is not None else cell.get("raw_value")
    return "" if value is None else str(value)


def excel_serial_iso(value: Any, epoch: str) -> str | None:
    """Return an ISO value for a confirmed Excel date serial, preserving raw data."""
    try:
        serial = float(value)
    except (TypeError, ValueError):
        return None
    if epoch == "1904":
        origin = datetime(1904, 1, 1)
    else:
        # Excel preserves the historical fictitious 1900-02-29 at serial 60.
        origin = datetime(1899, 12, 31) if serial < 60 else datetime(1899, 12, 30)
    resolved = origin + timedelta(days=serial)
    return resolved.date().isoformat() if serial.is_integer() else resolved.isoformat(timespec="seconds")


def parse_spreadsheet(path: Path, *, output_dir: Path, source_id: str,
                      document_id: str, source_url: str | None = None) -> ParseResult:
    source_hash = sha256_file(path)
    output_dir.mkdir(parents=True, exist_ok=True)
    database = output_dir / "tables.sqlite"
    # Build the complete artifact next to its final path, then swap it in: a
    # failed parse never truncates the previously published database.
    temporary = output_dir / (".tables-{}.sqlite".format(uuid.uuid4().hex))
    connection = sqlite3.connect(temporary)
    try:
        _create_schema(connection)
        blocks: list[ParseBlock] = []
        with zipfile.ZipFile(path) as archive:
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
            relationships = _relationships(archive, "xl/_rels/workbook.xml.rels")
            strings = _shared_strings(archive)
            formats, date_styles = _styles(archive)
            properties = workbook.find("m:workbookPr", NS)
            epoch = "1904" if properties is not None and properties.attrib.get("date1904") in {"1", "true"} else "1900"
            for sheet_order, sheet in enumerate(workbook.findall("m:sheets/m:sheet", NS), start=1):
                name = sheet.attrib["name"]
                sheet_id = _identity_id("sheet-", 16, source_id, document_id, source_hash,
                                        {"sheet": name, "order": sheet_order})
                relationship = sheet.attrib.get(_tag(NS["r"], "id"), "")
                target = relationships.get(relationship, "worksheets/sheet{}.xml".format(sheet_order)).lstrip("/")
                xml_path = target if target.startswith("xl/") else "xl/" + target
                root = ET.fromstring(archive.read(xml_path))
                hidden_rows = {int(row.attrib["r"]) for row in root.findall("m:sheetData/m:row", NS)
                               if row.attrib.get("hidden") in {"1", "true"}}
                hidden_columns = set()
                for column in root.findall("m:cols/m:col", NS):
                    if column.attrib.get("hidden") in {"1", "true"}:
                        hidden_columns.update(range(int(column.attrib["min"]), int(column.attrib["max"]) + 1))
                merges = [item.attrib["ref"] for item in root.findall("m:mergeCells/m:mergeCell", NS)]
                connection.execute("INSERT INTO sheets VALUES (?,?,?,?,?,?,?)",
                                   (sheet_id, name, sheet_order, sheet.attrib.get("state", "visible"), epoch,
                                    json.dumps(sorted(hidden_rows)), json.dumps(sorted(hidden_columns))))
                cells: Dict[tuple[int, int], Dict[str, Any]] = {}
                for node in root.findall("m:sheetData/m:row/m:c", NS):
                    coordinate = node.attrib.get("r")
                    if not coordinate:
                        continue
                    row_index, column_index = _coordinates(coordinate)
                    raw_type = node.attrib.get("t", "n")
                    style_id = int(node.attrib.get("s", 0))
                    value_node, formula_node = node.find("m:v", NS), node.find("m:f", NS)
                    value = value_node.text if value_node is not None else None
                    if raw_type == "s" and value is not None:
                        try:
                            raw_value = strings[int(value)]
                        except (IndexError, ValueError):
                            raw_value = value
                    elif raw_type == "inlineStr":
                        inline = node.find("m:is", NS)
                        raw_value = "".join(item.text or "" for item in inline.iter() if item.tag.endswith("}t")) if inline is not None else ""
                    else:
                        raw_value = value
                    formula = formula_node.text if formula_node is not None else None
                    cell = {"raw_type": raw_type, "raw_value": None if formula else raw_value,
                            "formula": formula, "cached_value": raw_value if formula else None,
                            "number_format": formats[style_id] if style_id < len(formats) else "General",
                            "date_candidate": style_id in date_styles, "empty_kind": "formula_cache_missing" if formula and raw_value is None else None}
                    cells[(row_index, column_index)] = cell
                    connection.execute("INSERT INTO cells VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (sheet_id, row_index, column_index, coordinate, raw_type, cell["raw_value"], formula,
                         cell["cached_value"], cell["number_format"], int(cell["date_candidate"]),
                         cell["empty_kind"], int(row_index in hidden_rows), int(column_index in hidden_columns)))
                for merge in merges:
                    left, right = merge.split(":") if ":" in merge else (merge, merge)
                    r1, c1 = _coordinates(left)
                    r2, c2 = _coordinates(right)
                    connection.execute("INSERT INTO merges VALUES (?,?,?,?,?,?)", (sheet_id, r1, c1, r2, c2, left))
                for ordinal, bounds in enumerate(_regions(cells, merges), start=1):
                    r1, c1, r2, c2 = bounds
                    table_id = _identity_id("table-", 20, source_id, document_id, source_hash,
                                            {"sheet": name, "bounds": list(bounds)})
                    first_row = [_display(cells.get((r1, column), {})) for column in range(c1, c2 + 1)]
                    horizontal_header_merges = []
                    for merge in merges:
                        left, right = merge.split(":") if ":" in merge else (merge, merge)
                        mr1, mc1 = _coordinates(left)
                        mr2, mc2 = _coordinates(right)
                        if mr1 == r1 == mr2 and mc2 > mc1 and c1 <= mc1 <= c2:
                            horizontal_header_merges.append((mr1, mc1, mr2, mc2))
                    instruction_header = False
                    header_ambiguity = None
                    header_depth = 2 if horizontal_header_merges and r2 > r1 else 1
                    if not horizontal_header_merges:
                        decision = _instruction_header_decision(cells, merges, r1, c1, r2, c2)
                        header_depth, header_ambiguity = decision
                        instruction_header = header_depth == 2 and header_ambiguity is None
                    header_row = r1 + header_depth - 1
                    # The instruction row is a whole-width caption, not a
                    # per-column label: it is retained in ``header_rows`` for
                    # context but never folded into the field names.
                    label_start_row = header_row if instruction_header else r1
                    labels = []
                    for column in range(c1, c2 + 1):
                        levels = []
                        for row in range(label_start_row, header_row + 1):
                            value = _display(cells.get((row, column), {}))
                            if not value:
                                anchor = next((item for item in horizontal_header_merges
                                               if item[0] == row and item[1] <= column <= item[3]), None)
                                if anchor:
                                    value = _display(cells.get((anchor[0], anchor[1]), {}))
                            if value and value not in levels:
                                levels.append(value)
                        labels.append(" / ".join(levels) or "column_{}".format(column))
                    nonempty_headers = [value for value in labels if value]
                    status = "accepted" if nonempty_headers and len(set(nonempty_headers)) == len(nonempty_headers) else "ambiguous"
                    header_disambiguation = None
                    if status != "accepted" and header_ambiguity is None:
                        # A repeated label is only ambiguous while it stands
                        # alone; under a regular block layout its group head
                        # identifies the column, so qualify instead of discard.
                        qualified, header_disambiguation = _qualify_repeating_labels(labels)
                        if header_disambiguation is not None:
                            labels = qualified
                            status = "accepted"
                    if header_ambiguity:
                        status = "ambiguous"
                        header_disambiguation = None
                    title = next((value for value in first_row if value), "{} 区域 {}".format(name, ordinal))
                    if re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]+)?", title):
                        # A serial date/year/amount is a cell value, not a
                        # useful standalone table title. Do not guess its type.
                        title = "{} · 表 {}".format(name, ordinal)
                    interpretation_id = "interpretation-" + hashlib.sha256((table_id + ":v1").encode()).hexdigest()[:16]
                    connection.execute("INSERT INTO logical_tables VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (table_id, sheet_id, ordinal, title, r1, c1, r2, c2, status,
                         "exclude", hashlib.sha256((sheet_id + repr(bounds)).encode()).hexdigest()))
                    fields = [{"field_id": "c{}".format(column), "column": column,
                               "label": labels[column - c1],
                               "header_row": header_row} for column in range(c1, c2 + 1)]
                    ambiguities = [header_ambiguity] if header_ambiguity else (
                        [] if status == "accepted" else ["header_not_unique_or_missing"])
                    payload = {"fields": fields,
                               "header_rows": list(range(r1, header_row + 1)),
                               "ambiguities": ambiguities}
                    if header_disambiguation is not None:
                        # Record that the header was rewritten, so an admitted
                        # table can be told apart from one that never needed it.
                        payload["header_disambiguation"] = header_disambiguation
                    connection.execute("INSERT INTO interpretations VALUES (?,?,?,?,?)",
                        (interpretation_id, table_id, 1, status, json.dumps(payload,
                         ensure_ascii=False)))
                    for field in fields:
                        connection.execute("INSERT INTO field_map VALUES (?,?,?,?,?)",
                                           (interpretation_id, field["field_id"], field["column"], field["header_row"], field["label"]))
                    for (row_index, column_index), cell in cells.items():
                        if r1 <= row_index <= r2 and c1 <= column_index <= c2:
                            term = _display(cell)
                            if term:
                                connection.execute("INSERT INTO table_terms VALUES (?,?,?,?,?)",
                                                   (table_id, term, sheet_id, row_index, column_index))
                                if cell.get("date_candidate"):
                                    normalized_date = excel_serial_iso(term, epoch)
                                    if normalized_date and normalized_date != term:
                                        connection.execute("INSERT INTO table_terms VALUES (?,?,?,?,?)",
                                            (table_id, normalized_date, sheet_id, row_index, column_index))
                    preview_rows = []
                    for row_index in range(r1, min(r2, r1 + 20) + 1):
                        preview_rows.append([_display(cells.get((row_index, column), {})) for column in range(c1, c2 + 1)])
                    summary = "# {}\n\n工作表：{}；范围：R{}C{}:R{}C{}；解释状态：{}。\n\n".format(
                        title, name, r1, c1, r2, c2, status)
                    if preview_rows:
                        summary += "样例（最多 20 行，不代表全表）：\n" + "\n".join(" | ".join(row) for row in preview_rows)
                    blocks.append(ParseBlock(block_id=table_id, position=len(blocks), text=summary,
                        block_type="table_reference", metadata={"table_id": table_id, "sheet_id": sheet_id,
                        "interpretation_id": interpretation_id, "bounds": list(bounds), "interpretation_status": status,
                        "fields": [field["label"] for field in fields],
                        "scope": {"sheet": name, "row_start": r1, "column_start": c1,
                                  "row_end": r2, "column_end": c2},
                        "tables_sqlite_local_path": str(database), "source_name": path.name, "compile_table_values": False}))
        connection.commit()
    except BaseException:
        connection.close()
        temporary.unlink(missing_ok=True)
        raise
    connection.close()
    os.replace(temporary, database)
    return ParseResult(source_id=source_id, document_id=document_id, source_name=path.name,
                       source_sha256=source_hash, route=InputRoute.SPREADSHEET_NATIVE,
                       parser_id="spreadsheet.native.ooxml", parser_version="1",
                       blocks=blocks, markdown="\n\n".join(block.text for block in blocks),
                       source_url=source_url, raw_output_dir=str(output_dir),
                       warnings=[] if blocks else ["no logical table regions found"])


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript("""
    PRAGMA journal_mode=DELETE;
    CREATE TABLE sheets(sheet_id TEXT PRIMARY KEY,name TEXT,order_index INTEGER,visibility TEXT,date_epoch TEXT,hidden_rows_json TEXT,hidden_columns_json TEXT);
    CREATE TABLE cells(sheet_id TEXT,row_index INTEGER,column_index INTEGER,coordinate TEXT,raw_type TEXT,raw_value TEXT,formula TEXT,cached_value TEXT,number_format TEXT,date_candidate INTEGER,empty_kind TEXT,hidden_row INTEGER,hidden_column INTEGER,PRIMARY KEY(sheet_id,row_index,column_index));
    CREATE TABLE merges(sheet_id TEXT,row_start INTEGER,column_start INTEGER,row_end INTEGER,column_end INTEGER,anchor TEXT);
    CREATE TABLE logical_tables(table_id TEXT PRIMARY KEY,sheet_id TEXT,ordinal INTEGER,title TEXT,row_start INTEGER,column_start INTEGER,row_end INTEGER,column_end INTEGER,status TEXT,hidden_policy TEXT,region_fingerprint TEXT);
    CREATE TABLE interpretations(interpretation_id TEXT PRIMARY KEY,table_id TEXT,version INTEGER,status TEXT,payload_json TEXT);
    CREATE TABLE field_map(interpretation_id TEXT,field_id TEXT,column_index INTEGER,header_row INTEGER,label TEXT,PRIMARY KEY(interpretation_id,field_id));
    CREATE TABLE table_terms(table_id TEXT,term TEXT,sheet_id TEXT,row_index INTEGER,column_index INTEGER);
    CREATE TABLE table_errors(table_id TEXT,source_id TEXT,document_id TEXT,code TEXT,detail TEXT);
    CREATE INDEX table_terms_lookup ON table_terms(table_id,term);
    """)


__all__ = ["excel_serial_iso", "parse_spreadsheet"]
