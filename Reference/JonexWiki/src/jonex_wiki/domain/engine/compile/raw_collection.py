"""Prepare full-article and complete-table raw pages for keep-and-search.

The compile path builds pages from ``SourceArtifact`` local attachment handles;
the migration path builds the same pages from an already-published directory
Bundle (``sources/registry.json`` + ``sources/articles`` + ``sources/**/tables.sqlite``).
Both call the same complete-table exporter and source-chapter reading
projection, so the two never drift. Fine evidence units remain separate.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
import json
import re
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from jonex_wiki.domain.engine.models.query_models import (
    RawWikiPage,
    WorkbookOutline,
    WorkbookOutlineDate,
    WorkbookOutlineGroup,
    WorkbookOutlineTable,
)
from jonex_wiki.kernel.contracts import SourceArtifact
from jonex_wiki.domain.engine.compile.table_navigation import full_table_path

def _article_pages(source_id: str, text: str, source_name: str = '', *, original_structure=None) -> List[RawWikiPage]:
    """Project original chapters as continuous, source-bound reading pages."""
    from jonex_wiki.domain.engine.compile.chapter_pages import article_pages
    yield from article_pages(source_id, text, source_name,original_structure=original_structure)


def _table_pages_from_path(source_id: str, path: str, source_name: str = "") -> List[RawWikiPage]:
    from jonex_wiki.domain.engine.query.table_reader import iter_full_table_page

    database = Path(path).expanduser().resolve()
    connection = sqlite3.connect("file:{}?mode=ro&immutable=1".format(database), uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("SELECT table_id FROM logical_tables ORDER BY ordinal, table_id").fetchall()
        table_ids = [str(row["table_id"]) for row in rows]
        pages: List[RawWikiPage] = []
        for table_id in table_ids:
            pages.extend(iter_full_table_page(table_id, source_id, connection,
                                              source_name=source_name))
        return pages
    finally:
        connection.close()


def _attach_chapter_table_links(chapters, artifacts, tables):
    """Keep the original chapter body intact and attach links by parser identity."""
    if not chapters or not tables:
        return
    starts = [chapter.scope["article_start"] for chapter in chapters]
    ends = [chapter.scope["article_end"] for chapter in chapters]
    start = 0
    for artifact in artifacts:
        metadata = artifact.source_metadata
        text = metadata.get("snapshot_text", artifact.text)
        end = start + len(text)
        table = tables.get(metadata.get("table_id"))
        if table is not None and text.strip():
            link = {"table_id": table.table_id, "title": table.title,
                    "page_path": table.scope["full_table_path"]}
            for chapter in chapters[bisect_right(ends, start):bisect_left(starts, end)]:
                links = chapter.scope.setdefault("preview_table_links", [])
                if link not in links:
                    links.append(link)
        start = end


def collect_article_and_table_pages(artifacts: List[SourceArtifact]) -> List[RawWikiPage]:
    """Collect raw pages for a fresh/incremental compile from parser artifacts."""
    by_source: Dict[str, List[SourceArtifact]] = {}
    for artifact in artifacts:
        by_source.setdefault(artifact.source_id, []).append(artifact)

    pages: List[RawWikiPage] = []
    seen_tables: set[Tuple[str, str]] = set()
    for source_id, items in sorted(by_source.items()):
        ordered = sorted(items, key=lambda a: (a.position if a.position is not None else 0, a.chunk_id))
        article_text = "".join(
            a.source_metadata.get("snapshot_text", a.text) for a in ordered
        )
        # 名称用于文章及 raw-tables 的阅读标签；同一来源的多个 artifact 里
        # 取第一个非空值（与 collect_workbook_outlines 同一套取法）。
        source_name = next(
            (str((a.source_metadata or {}).get("source_name")) for a in ordered
             if (a.source_metadata or {}).get("source_name")), "")
        structure=next(((a.source_metadata or {}).get('original_structure') for a in ordered
                        if (a.source_metadata or {}).get('original_structure')),None)
        chapters = list(_article_pages(source_id, article_text, source_name, original_structure=structure))
        # Bundle publication registers the first grid for a source. Preview
        # links must use identities from that same grid.
        published_grid = next((str(path) for item in items
                               if (path := item.source_metadata.get("tables_sqlite_local_path"))), None)
        table_pages = []
        registered_tables = {}
        for artifact in ordered:
            metadata = artifact.source_metadata or {}
            table_path = metadata.get("tables_sqlite_local_path")
            if not table_path:
                continue
            if (source_id, str(table_path)) in seen_tables:
                continue
            seen_tables.add((source_id, str(table_path)))
            attached = _table_pages_from_path(source_id, str(table_path), source_name)
            if str(table_path) == published_grid:
                for page in attached:
                    page.scope["full_table_path"] = full_table_path(source_id, page.table_id)
                    registered_tables[page.table_id] = page
            table_pages.extend(attached)
        _attach_chapter_table_links(chapters, ordered, registered_tables)
        pages.extend(chapters)
        pages.extend(table_pages)
    return pages


def collect_raw_pages_from_bundle(files: Dict[str, bytes]) -> List[RawWikiPage]:
    """Collect raw pages from a published directory Bundle (old-asset migration)."""
    registry = json.loads(files["sources/registry.json"].decode("utf-8"))
    source_by_id: Dict[str, Dict[str, Any]] = {
        str(entry["source_id"]): entry for entry in registry["sources"]
    }
    article_by_source: Dict[str, str] = {}
    original_structures = {}
    for path in files:
        if not path.startswith("sources/digests/") or not path.endswith(".json"):
            continue
        digest = json.loads(files[path].decode("utf-8"))
        source_id = str(digest.get("source_id"))
        original_structures[source_id]=(digest.get('source_metadata') or {}).get('original_structure')
        if not source_id or source_id in article_by_source:
            continue
        slug = path[len("sources/digests/"):-len(".json")]
        article = "sources/articles/{}.md".format(slug)
        if article in files:
            article_by_source[source_id] = article

    pages: List[RawWikiPage] = []
    for source_id, entry in sorted(source_by_id.items()):
        article_path = article_by_source.get(source_id)
        if article_path:
            pages.extend(_article_pages(source_id, files[article_path].decode("utf-8"),
                str(entry.get('source_name') or entry.get('name') or source_id),
                original_structure=original_structures.get(source_id)))
        table_path = entry.get("tables")
        if table_path and table_path in files:
            pages.extend(_table_pages_from_bundle_bytes(source_id, files[table_path]))
    return pages


def _table_pages_from_bundle_bytes(source_id: str, data: bytes) -> List[RawWikiPage]:
    from jonex_wiki.domain.engine.query.table_reader import iter_full_table_page

    import os
    import tempfile

    fd, tmp = tempfile.mkstemp(suffix=".sqlite")
    try:
        os.write(fd, data)
        os.close(fd)
        database = Path(tmp)
        connection = sqlite3.connect("file:{}?mode=ro&immutable=1".format(database), uri=True)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                "SELECT table_id FROM logical_tables ORDER BY ordinal, table_id").fetchall()
            table_ids = [str(row["table_id"]) for row in rows]
            pages: List[RawWikiPage] = []
            for table_id in table_ids:
                pages.extend(iter_full_table_page(table_id, source_id, connection))
            return pages
        finally:
            connection.close()
    finally:
        os.unlink(tmp)


# ──────────────────────────────────────────────
#  Workbook outline (skeleton for a generated concept page)
# ──────────────────────────────────────────────
# A native spreadsheet contributes no prose of its own, so it produces table
# pages and nothing else: the reader gets a grid and no orientation.  The
# outline below is the orientation — which sheet holds which dates, which
# column each date lives in, and how each sheet bands its rows — collected from
# the **full** grid rather than from the bounded 20-row parser preview, which
# stops mid-table on every real menu sheet and would silently drop whole
# sections.  It carries structure only; cell values stay in the grid so the
# skeleton can never become a second, staler source of truth.

_ISO_DATE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
_WEEKDAY_NAMES = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
_GROUP_MEMBER_LIMIT = 12


def _weekday_name(value: str) -> str:
    """Chinese weekday for an ISO day; empty when the value is not a real date."""
    try:
        day = date.fromisoformat(value)
    except ValueError:
        return ""
    return _WEEKDAY_NAMES[day.weekday()]


def _column_name(index: int) -> str:
    name = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        name = chr(ord("A") + remainder) + name
    return name


def build_workbook_outline(
    source_id: str, source_name: str, path: str,
) -> Optional[WorkbookOutline]:
    """Summarize one native workbook's skeleton from its own full grid.

    Returns ``None`` when the grid is unreadable or holds no logical table, so a
    damaged artifact degrades to "no skeleton page" instead of failing the
    compile.  Every iteration is ordered, so repeated calls are byte-identical.
    """
    database = Path(path).expanduser()
    if not database.is_file():
        return None
    try:
        connection = sqlite3.connect(
            "file:{}?mode=ro&immutable=1".format(database), uri=True)
    except sqlite3.Error:
        return None
    connection.row_factory = sqlite3.Row
    try:
        sheet_names = {
            str(row["sheet_id"]): str(row["name"])
            for row in connection.execute("SELECT sheet_id, name FROM sheets")
        }
        sheet_order = {
            str(row["sheet_id"]): int(row["order_index"] or 0)
            for row in connection.execute("SELECT sheet_id, order_index FROM sheets")
        }
        rows = connection.execute("""
            SELECT table_id, sheet_id, title, row_start, column_start,
                   row_end, column_end, status
            FROM logical_tables
        """).fetchall()
        ordered = sorted(
            rows,
            key=lambda row: (sheet_order.get(str(row["sheet_id"]), 0),
                             int(row["row_start"]), str(row["table_id"])),
        )
        if not ordered:
            return None

        tables: List[WorkbookOutlineTable] = []
        seen_groups: set[Tuple[str, int]] = set()
        for row in ordered:
            table_id = str(row["table_id"])
            sheet_id = str(row["sheet_id"])
            row_start = int(row["row_start"])
            row_end = int(row["row_end"])
            column_start = int(row["column_start"])
            column_end = int(row["column_end"])

            # Header labels: the interpretation's own field map, which exists
            # for ambiguous tables too (they map one label per column).
            labels: Dict[int, str] = {}
            header_row = row_start
            for field in connection.execute("""
                SELECT f.column_index, f.label, f.header_row
                FROM field_map f JOIN interpretations i
                  ON i.interpretation_id = f.interpretation_id
                WHERE i.table_id = ?
            """, (table_id,)):
                labels[int(field["column_index"])] = str(field["label"])
                header_row = min(header_row, int(field["header_row"]))

            def header_value(column_index: int) -> str:
                cell = connection.execute(
                    "SELECT raw_value FROM cells WHERE sheet_id=? AND row_index=? "
                    "AND column_index=?", (sheet_id, header_row, column_index)).fetchone()
                if cell is not None and cell["raw_value"] not in (None, ""):
                    return str(cell["raw_value"]).strip()
                return labels.get(column_index, "")

            # Dates: only ISO terms.  ``table_terms`` also stores raw dish names
            # and bare Excel serials for the same cells, so an unfiltered read
            # would write menu items into the date mapping.
            dates: List[WorkbookOutlineDate] = []
            for term in connection.execute(
                    "SELECT term, column_index FROM table_terms WHERE table_id=?",
                    (table_id,)):
                value = str(term["term"])
                if not _ISO_DATE.match(value):
                    continue
                column_index = int(term["column_index"])
                dates.append(WorkbookOutlineDate(
                    date=value,
                    weekday=_weekday_name(value),
                    column_index=column_index,
                    raw_value=header_value(column_index),
                ))
            dates.sort(key=lambda item: (item.column_index, item.date))

            # Row bands: vertical merges down the first column, deduplicated per
            # sheet because two overlapping logical tables can describe the same
            # band.
            groups: List[WorkbookOutlineGroup] = []
            if column_start == 1:
                for merge in connection.execute("""
                    SELECT row_start, row_end FROM merges
                    WHERE sheet_id=? AND column_start=1 AND column_end=1
                    ORDER BY row_start
                """, (sheet_id,)):
                    band_start = int(merge["row_start"])
                    band_end = int(merge["row_end"])
                    if band_end <= band_start:
                        continue
                    if not (row_start <= band_start and band_end <= row_end):
                        continue
                    if (sheet_id, band_start) in seen_groups:
                        continue
                    seen_groups.add((sheet_id, band_start))
                    head = connection.execute(
                        "SELECT raw_value FROM cells WHERE sheet_id=? AND row_index=? "
                        "AND column_index=1", (sheet_id, band_start)).fetchone()
                    members: List[str] = []
                    for cell in connection.execute("""
                        SELECT raw_value FROM cells
                        WHERE sheet_id=? AND row_index BETWEEN ? AND ?
                          AND column_index=? AND raw_value IS NOT NULL AND raw_value != ''
                        ORDER BY row_index
                    """, (sheet_id, band_start, band_end, column_start + 1)):
                        value = str(cell["raw_value"]).strip()
                        if value and value not in members:
                            members.append(value)
                    groups.append(WorkbookOutlineGroup(
                        row_start=band_start,
                        row_end=band_end,
                        label=(str(head["raw_value"]).strip()
                               if head is not None and head["raw_value"] else ""),
                        members=members[:_GROUP_MEMBER_LIMIT],
                    ))

            tables.append(WorkbookOutlineTable(
                table_id=table_id,
                sheet=sheet_names.get(sheet_id, sheet_id),
                title=str(row["title"] or ""),
                row_start=row_start,
                row_end=row_end,
                column_start=column_start,
                column_end=column_end,
                status=str(row["status"] or "unknown"),
                dates=dates,
                groups=groups,
            ))
        return WorkbookOutline(
            source_id=source_id,
            source_name=source_name,
            sheet_count=len(sheet_names),
            tables=tables,
        )
    except sqlite3.Error:
        return None
    finally:
        connection.close()


def collect_workbook_outlines(
    artifacts: List[SourceArtifact],
) -> Dict[str, WorkbookOutline]:
    """Collect one skeleton outline per native spreadsheet source."""
    by_source: Dict[str, List[SourceArtifact]] = {}
    for artifact in artifacts:
        by_source.setdefault(artifact.source_id, []).append(artifact)
    outlines: Dict[str, WorkbookOutline] = {}
    for source_id, items in sorted(by_source.items()):
        ordered = sorted(items, key=lambda a: (a.position if a.position is not None else 0, a.chunk_id))
        table_path = ""
        name = ""
        for artifact in ordered:
            metadata = artifact.source_metadata or {}
            if not table_path and metadata.get("tables_sqlite_local_path"):
                table_path = str(metadata["tables_sqlite_local_path"])
            if not name and metadata.get("source_name"):
                name = str(metadata["source_name"])
        if not table_path:
            continue
        outline = build_workbook_outline(source_id, name, table_path)
        if outline is not None:
            outlines[source_id] = outline
    return outlines
