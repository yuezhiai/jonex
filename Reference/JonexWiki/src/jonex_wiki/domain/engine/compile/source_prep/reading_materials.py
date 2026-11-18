"""Reading-material catalog built after parsing, before compilation.

This module owns the *catalog*, not the raw content.  For every source it
records which readable units exist (one document, or native spreadsheet
sheets/tables), their identity, name, actual content location and content
size.  The complete bytes stay where they already are (``sources/articles``,
``sources/**/tables.sqlite``); nothing here copies whole tables into chunks.

The same builder is used by the normal parse path and by offline reindexing so
the two preparation routes can never drift.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from jonex_wiki.domain.engine.compile.source_prep.blocks import InputRoute, ParseResult
from jonex_wiki.domain.engine.compile.source_prep.chunking import PackingOptions, estimate_tokens

READING_MATERIALS_SCHEMA = "jonex_wiki.reading_materials.v1"


def _table_sqlite_path(result: ParseResult) -> Optional[str]:
    for block in result.blocks:
        path = block.metadata.get("tables_sqlite_local_path")
        if path:
            return str(path)
    return None


def _article_text(result: ParseResult) -> str:
    """Reproduce the publishable article text from parser blocks.

    Mirrors ``bundle_codec.build_source_snapshots`` (``snapshot_text`` first,
    then the plain chunk text) so the sections and sizes refer to exactly the
    text that will be published as ``sources/articles/<slug>.md``.
    """
    ordered = sorted(result.blocks, key=lambda b: (b.position if b.position is not None else 0))
    return "".join(
        (block.metadata.get("snapshot_text") or block.text) for block in ordered
    )


def _heading_scan(text: str) -> List[Dict[str, Any]]:
    """Return ``{title, level, start, end, parent}`` sections from Markdown.

    Only ATX and Setext headings are recognised, ignoring fenced code blocks.
    ``parent`` is the start offset of the enclosing heading (``None`` for
    top-level sections).  Ranges are half-open character offsets.
    """
    atx = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*$")
    setext = re.compile(r"^[ \t]*(=+|-+)[ \t]*$")
    headings: List[Tuple[int, int, str]] = []
    lines = text.splitlines(keepends=True)
    offset = 0
    in_fence = False
    fence = ""
    previous = None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            marker = stripped[:3]
            if not in_fence:
                in_fence, fence = True, marker
            elif stripped.startswith(fence):
                in_fence = False
            previous = None
            offset += len(line)
            continue
        if not in_fence:
            match = atx.match(stripped)
            if match and match.group(2).strip():
                headings.append((offset, len(match.group(1)), match.group(2).strip()))
            elif previous and setext.fullmatch(stripped):
                prior_offset, prior_title = previous
                headings.append((prior_offset, 1 if stripped.startswith('=') else 2, prior_title))
            previous = (offset, stripped) if stripped and not match and not setext.fullmatch(stripped) else None
        else:
            previous = None
        offset += len(line)
    headings.sort(key=lambda item: item[0])

    sections: List[Dict[str, Any]] = []
    stack: List[Dict[str, Any]] = []
    for start, level, title in headings:
        while stack and stack[-1]["level"] >= level:
            stack[-1]["end"] = start
            stack.pop()
        parent = stack[-1]["start"] if stack else None
        node = {"title": title, "level": level, "parent": parent,
                "start": start, "end": len(text)}
        sections.append(node)
        stack.append(node)
    while stack:
        stack[-1]["end"] = len(text)
        stack.pop()
    return sections


def _document_material(
    *,
    source_id: str,
    document_id: str,
    source_name: str,
    article_text: str,
) -> Dict[str, Any]:
    unit: Dict[str, Any] = {
        "unit_id": source_id,
        "kind": "document",
        "name": source_name or source_id,
        "content_kind": "article",
        "content_location": {"article_path": None},
        "content_size": len(article_text),
        "content_token_estimate": estimate_tokens(article_text),
    }
    capacity = PackingOptions.from_env().max_tokens
    if estimate_tokens(article_text) > capacity:
        unit["sections"] = _heading_scan(article_text)
    return unit


def _sheet_rows(connection: sqlite3.Connection, sheet_id: str):
    """All cells in one sheet in original coordinate order."""
    return connection.execute(
        "SELECT row_index,column_index,coordinate,raw_type,raw_value,formula,"
        "cached_value,number_format,date_candidate,empty_kind,hidden_row,hidden_column "
        "FROM cells WHERE sheet_id=? ORDER BY row_index,column_index",
        (sheet_id,),
    ).fetchall()


def _sheet_merges(connection: sqlite3.Connection, sheet_id: str):
    return connection.execute(
        "SELECT row_start,column_start,row_end,column_end,anchor FROM merges "
        "WHERE sheet_id=? ORDER BY row_start,column_start",
        (sheet_id,),
    ).fetchall()


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


def _sheet_extents(connection: sqlite3.Connection, sheet_id: str) -> Dict[str, Any]:
    row = connection.execute(
        "SELECT MIN(row_index), MAX(row_index), MIN(column_index), MAX(column_index), COUNT(*) "
        "FROM cells WHERE sheet_id=? AND (raw_value IS NOT NULL OR cached_value IS NOT NULL)",
        (sheet_id,),
    ).fetchone()
    if row is None or row[0] is None:
        return {"row_start": None, "row_end": None, "column_start": None,
                "column_end": None, "nonempty_cells": 0}
    return {"row_start": int(row[0]), "row_end": int(row[1]),
            "column_start": int(row[2]), "column_end": int(row[3]),
            "nonempty_cells": int(row[4])}


def _table_extents(connection: sqlite3.Connection, table: Any) -> Dict[str, Any]:
    row = connection.execute(
        "SELECT COUNT(*) FROM cells WHERE sheet_id=? AND row_index BETWEEN ? AND ? "
        "AND column_index BETWEEN ? AND ? AND (raw_value IS NOT NULL OR cached_value IS NOT NULL)",
        (table["sheet_id"], table["row_start"], table["row_end"],
         table["column_start"], table["column_end"]),
    ).fetchone()
    return {
        "bounds": {"row_start": int(table["row_start"]),
                   "column_start": int(table["column_start"]),
                   "row_end": int(table["row_end"]),
                   "column_end": int(table["column_end"])},
        "nonempty_cells": int(row[0]) if row is not None else 0,
    }


def _spreadsheet_materials(
    *,
    source_id: str,
    document_id: str,
    source_name: str,
    tables_sqlite_path: str,
) -> List[Dict[str, Any]]:
    from jonex_wiki.domain.engine.query.table_reader import (
        render_full_sheet_markdown,
        render_full_table_markdown,
    )

    database = Path(tables_sqlite_path).expanduser().resolve()
    connection = sqlite3.connect("file:{}?mode=ro".format(database.as_posix()), uri=True)
    connection.row_factory = sqlite3.Row
    try:
        sheets = connection.execute(
            "SELECT sheet_id,name,order_index,visibility FROM sheets ORDER BY order_index"
        ).fetchall()
        sheet_names = {str(sheet["sheet_id"]): str(sheet["name"]) for sheet in sheets}
        units: List[Dict[str, Any]] = []
        workbook_parts: List[str] = []
        workbook_sheets: List[Dict[str, Any]] = []
        workbook_table_ids: List[str] = []
        for sheet in sheets:
            sheet_id = str(sheet["sheet_id"])
            sheet_cells = [dict(row) for row in _sheet_rows(connection, sheet_id)]
            sheet_merges = [dict(row) for row in _sheet_merges(connection, sheet_id)]
            rendered = render_full_sheet_markdown(
                source_id=source_id, sheet=dict(sheet), cells=sheet_cells, merges=sheet_merges
            )
            extents = _sheet_extents(connection, sheet_id)
            table_ids = [
                str(row["table_id"])
                for row in connection.execute(
                    "SELECT table_id FROM logical_tables WHERE sheet_id=? ORDER BY ordinal",
                    (sheet_id,),
                ).fetchall()
            ]
            workbook_parts.append(rendered)
            workbook_table_ids.extend(table_ids)
            workbook_sheets.append({
                "sheet_id": sheet_id,
                "name": str(sheet["name"]),
                "order": int(sheet["order_index"]),
                "visibility": str(sheet["visibility"]),
                "table_ids": table_ids,
            })
            units.append({
                "unit_id": sheet_id,
                "kind": "sheet",
                "name": str(sheet["name"]),
                "sheet_id": sheet_id,
                "sheet_order": int(sheet["order_index"]),
                "visibility": str(sheet["visibility"]),
                "content_kind": "spreadsheet_sheet",
                "content_location": {"tables_path": None, "sheet_id": sheet_id},
                "content_size": len(rendered),
                "content_token_estimate": estimate_tokens(rendered),
                "nonempty_cells": extents["nonempty_cells"],
                "bounds": extents,
                "table_ids": table_ids,
            })

        # A native spreadsheet is read as one workbook by default.  Sheet and
        # logical-table units remain available as secondary navigation targets,
        # but they must not force an ordinary workbook through many reads.
        workbook_body = "\n\n".join(workbook_parts)
        units.insert(0, {
            "unit_id": source_id,
            "kind": "workbook",
            "name": source_name,
            "document_id": document_id,
            "content_kind": "spreadsheet_workbook",
            "content_location": {"tables_path": None},
            "content_size": len(workbook_body),
            "content_token_estimate": estimate_tokens(workbook_body),
            "sheets": workbook_sheets,
            "table_ids": workbook_table_ids,
        })

        tables = connection.execute(
            "SELECT * FROM logical_tables ORDER BY ordinal, table_id"
        ).fetchall()
        for table in tables:
            table_id = str(table["table_id"])
            cells = [dict(row) for row in connection.execute(
                "SELECT row_index,column_index,coordinate,raw_type,raw_value,formula,"
                "cached_value,number_format,date_candidate,empty_kind,hidden_row,hidden_column "
                "FROM cells WHERE sheet_id=? AND row_index BETWEEN ? AND ? "
                "AND column_index BETWEEN ? AND ? ORDER BY row_index,column_index",
                (table["sheet_id"], table["row_start"], table["row_end"],
                 table["column_start"], table["column_end"]),
            ).fetchall()]
            merges = [dict(row) for row in connection.execute(
                "SELECT row_start,column_start,row_end,column_end,anchor FROM merges "
                "WHERE sheet_id=? AND row_start<=? AND row_end>=? AND column_start<=? "
                "AND column_end>=? ORDER BY row_start,column_start",
                (table["sheet_id"], table["row_end"], table["row_start"],
                 table["column_end"], table["column_start"]),
            ).fetchall()]
            rendered = render_full_table_markdown(
                source_id=source_id, table=dict(table), cells=cells, merges=merges,
                sheet_name=sheet_names.get(str(table["sheet_id"])),
            )
            extents = _table_extents(connection, table)
            fields = []
            if _table_exists(connection, "interpretations") and _table_exists(connection, "field_map"):
                fields = [
                    row["label"]
                    for row in connection.execute(
                        "SELECT fm.label FROM interpretations i "
                        "JOIN field_map fm ON fm.interpretation_id=i.interpretation_id "
                        "WHERE i.table_id=? ORDER BY fm.column_index",
                        (table_id,),
                    ).fetchall()
                ]
            units.append({
                "unit_id": table_id,
                "kind": "table",
                "name": str(table["title"]),
                "sheet_id": str(table["sheet_id"]),
                "table_id": table_id,
                "content_kind": "spreadsheet_table",
                "content_location": {"tables_path": None, "table_id": table_id},
                "content_size": len(rendered),
                "content_token_estimate": estimate_tokens(rendered),
                "nonempty_cells": extents["nonempty_cells"],
                "bounds": extents["bounds"],
                "interpretation_status": str(table["status"]),
                "fields": [str(value) for value in fields],
            })
        return units
    finally:
        connection.close()


def build_reading_materials(
    *,
    source_id: str,
    document_id: str,
    source_name: str,
    source_sha256: str,
    route: InputRoute | str,
    article_text: Optional[str] = None,
    tables_sqlite_path: Optional[str] = None,
    parser_id: str = "",
    parser_version: str = "",
) -> Dict[str, Any]:
    """Build one transport-agnostic reading-material catalog.

    ``article_text`` is the publishable article for document routes;
    ``tables_sqlite_path`` is the existing native-spreadsheet SQLite grid.
    The returned catalog stores *identities*, names and sizes only; Bundle
    publication later fills in the relative ``article_path``/``tables_path``.
    """
    route = route if isinstance(route, InputRoute) else InputRoute(str(route))
    payload: Dict[str, Any] = {
        "schema": READING_MATERIALS_SCHEMA,
        "source_identity": {
            "source_id": source_id,
            "document_id": document_id,
            "source_name": source_name,
            "source_sha256": source_sha256,
            "parser_id": parser_id,
            "parser_version": parser_version,
        },
        "route": route.value,
        "units": [],
    }
    if route is InputRoute.SPREADSHEET_NATIVE:
        if not tables_sqlite_path:
            raise ValueError("SPREADSHEET_TABLE_ARTIFACT_MISSING_FOR_READING_MATERIALS")
        payload["units"] = _spreadsheet_materials(
            source_id=source_id,
            document_id=document_id,
            source_name=source_name,
            tables_sqlite_path=tables_sqlite_path,
        )
    else:
        payload["units"] = [_document_material(
            source_id=source_id,
            document_id=document_id,
            source_name=source_name,
            article_text=article_text or "",
        )]
    return payload


def prepare_reading_materials(result: ParseResult) -> Dict[str, Any]:
    """Post-process an already-normalized ``ParseResult`` into a catalog.

    Never re-runs a parser.  For native spreadsheets the catalog is derived
    from the already-saved ``tables.sqlite`` grid; for document routes it uses
    the same article join that ``build_source_snapshots`` publishes.
    """
    route = result.route if isinstance(result.route, InputRoute) else InputRoute(str(result.route))
    if route is InputRoute.SPREADSHEET_NATIVE:
        tables_path = _table_sqlite_path(result)
        if not tables_path:
            raise ValueError("SPREADSHEET_TABLE_ARTIFACT_MISSING_FOR_READING_MATERIALS")
        return build_reading_materials(
            source_id=result.source_id,
            document_id=result.document_id,
            source_name=result.source_name,
            source_sha256=result.source_sha256,
            route=route,
            tables_sqlite_path=tables_path,
            parser_id=result.parser_id,
            parser_version=result.parser_version,
        )
    return build_reading_materials(
        source_id=result.source_id,
        document_id=result.document_id,
        source_name=result.source_name,
        source_sha256=result.source_sha256,
        route=route,
        article_text=_article_text(result),
        parser_id=result.parser_id,
        parser_version=result.parser_version,
    )


def materials_from_inputs(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """Build a catalog from the same inputs the offline reindexer already has."""
    return build_reading_materials(
        source_id=str(inputs["source_id"]),
        document_id=str(inputs.get("document_id") or inputs["source_id"]),
        source_name=str(inputs.get("source_name") or inputs["source_id"]),
        source_sha256=str(inputs.get("source_sha256") or ""),
        route=InputRoute(str(inputs.get("route") or InputRoute.TEXT_DIRECT.value)),
        article_text=inputs.get("article_text"),
        tables_sqlite_path=inputs.get("tables_sqlite_path"),
        parser_id=str(inputs.get("parser_id") or ""),
        parser_version=str(inputs.get("parser_version") or ""),
    )


def encode_materials(materials: Dict[str, Any]) -> str:
    """Canonical JSON string for digest storage."""
    return json.dumps(materials, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = [
    "READING_MATERIALS_SCHEMA",
    "build_reading_materials",
    "prepare_reading_materials",
    "materials_from_inputs",
    "encode_materials",
]
