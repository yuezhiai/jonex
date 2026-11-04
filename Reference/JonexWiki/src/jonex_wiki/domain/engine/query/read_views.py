"""Reading-repair view construction for wiki_read / workbook delivery.

Ported from ``llm_wiki_plus/engine/read_views.py`` onto the dev package
layout.  It keeps the pure data model (Document/Focus/Section/ReadingView/
ReadPlan), the source-map/digest/material-catalog loaders, workbook/table
resolution, and the payload serialization used by the query reading context.

Nothing here calls a model, reads the clock, or persists state.  The
``bundle_view`` adapter supplies ``list_entries``/``read_bytes``/
``read_text``/``open_sqlite`` and the query ``state`` dict owns the per-run
caches.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

try:  # dev compile-side chunker; optional and only used by block fallback.
    from jonex_wiki.domain.engine.compile.source_prep.chunking import identify_units
except ImportError:  # pragma: no cover - defensive for partial checkouts
    identify_units = None  # type: ignore[assignment]


# ──────────────────────────────────────────────────────────────
#  Data model
# ──────────────────────────────────────────────────────────────


@dataclass
class Document:
    """One readable source article, pinned to a revision."""

    source_id: str
    article_path: str
    revision_id: str
    text_sha256: str
    text: str

    @property
    def object_key(self) -> Tuple[str, str, str, str]:
        return (self.revision_id, self.source_id, self.article_path, self.text_sha256)


@dataclass
class Focus:
    """One resolved hit: a requested path plus its article character spans."""

    requested_path: str
    source_id: str
    object_key: Tuple[str, str, str, str]
    spans: List[Tuple[int, int]] = field(default_factory=list)
    citation_refs: List[Dict[str, str]] = field(default_factory=list)
    passage_refs: List[Dict[str, Any]] = field(default_factory=list)
    open_request: bool = False


@dataclass
class Section:
    """One heading node.  ``id`` is the article character offset of the title."""

    id: int
    parent_id: Optional[int]
    level: int
    title: str
    start: int
    end: int
    origin: str = "saved_metadata"


@dataclass
class ReadingView:
    """One chosen set of article ranges to deliver."""

    object_key: Tuple[str, str, str, str]
    source_id: str
    article_path: str
    spans: List[Tuple[int, int]] = field(default_factory=list)
    blocks: List[Dict[str, Any]] = field(default_factory=list)
    citation_refs: List[Dict[str, str]] = field(default_factory=list)
    coverage: str = "ranges_partial"
    structure_origin: str = "plain_range"
    selected_level: str = "range"
    covered_focuses: List[Tuple[int, int]] = field(default_factory=list)
    title_path: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(block.get("body", "") for block in self.blocks)

    @property
    def char_size(self) -> int:
        return sum(end - start for start, end in self.spans)


@dataclass
class ReadPlan:
    """The outcome of ``plan_read`` before any query state is committed."""

    is_budget_limited: bool = False
    views: List[ReadingView] = field(default_factory=list)
    covered_focuses: List[Tuple[int, int]] = field(default_factory=list)
    pending_focuses: List[Focus] = field(default_factory=list)
    pending_ranges: List[Tuple[int, int]] = field(default_factory=list)
    next_cursor: Optional[str] = None
    cursor_token: Optional[str] = None
    cursor_queue: List[Dict[str, Any]] = field(default_factory=list)
    navigation: List[Dict[str, Any]] = field(default_factory=list)
    reason: str = ""


Preview = Callable[[ReadingView], bool]


# ──────────────────────────────────────────────────────────────
#  Coordinate adaptation
# ──────────────────────────────────────────────────────────────


def unique_offset(article: str, text: str) -> Optional[int]:
    """Return the only occurrence offset of *text*, or ``None`` when ambiguous."""
    if not text:
        return None
    first = article.find(text)
    if first < 0:
        return None
    if article.find(text, first + 1) >= 0:
        return None
    return first


def map_chunk(article: str, metadata: Mapping[str, Any]):
    """Map one saved chunk record onto the article.

    Returns ``(s, e, article_start, status)`` or ``None``.
    """
    s = metadata.get("char_start")
    e = metadata.get("char_end")
    text = metadata.get("snapshot_text")
    if not (isinstance(s, int) and isinstance(e, int) and isinstance(text, str)):
        return None
    if not text or e - s != len(text):
        return None
    if 0 <= s <= e <= len(article) and article[s:e] == text:
        return s, e, s, "direct"
    offset = unique_offset(article, text)
    if offset is None:
        return None
    return s, e, offset, "relocated"


def project(mapping, start: int, end: int) -> Optional[Tuple[int, int]]:
    """Project a passage-local range through a chunk mapping."""
    s, e, article_start = mapping[0], mapping[1], mapping[2]
    lo, hi = max(s, start), min(e, end)
    if lo >= hi:
        return None
    return (article_start + lo - s, article_start + hi - s)


def merge_ranges(ranges: Iterable[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """Merge overlapping or immediately adjacent half-open ranges."""
    ordered = sorted((int(a), int(b)) for a, b in ranges if int(a) < int(b))
    merged: List[List[int]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(int(a), int(b)) for a, b in merged]


def subtract_ranges(covered: Sequence[Tuple[int, int]],
                    delivered: Sequence[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """Remove delivered intervals from covered intervals; return the rest."""
    result: List[List[int]] = [[int(a), int(b)] for a, b in covered]
    for da, db in delivered:
        updated: List[List[int]] = []
        for a, b in result:
            if db <= a or da >= b:
                updated.append([a, b])
                continue
            if da > a:
                updated.append([a, da])
            if db < b:
                updated.append([db, b])
        result = updated
    return merge_ranges([(a, b) for a, b in result])


def _fully_covered(span: Tuple[int, int],
                   delivered: Sequence[Tuple[int, int]]) -> bool:
    return not subtract_ranges([list(span)], delivered)


# ──────────────────────────────────────────────────────────────
#  Source map and documents
# ──────────────────────────────────────────────────────────────


def _read_context(state: Dict[str, Any]) -> Dict[str, Any]:
    context = state.setdefault("read_context", {})
    if context.get("bundle_view") is None and state.get("bundle_view") is not None:
        context["bundle_view"] = state["bundle_view"]
    return context


def _decode_text(bundle_view: Any, path: str) -> Optional[str]:
    try:
        raw = bundle_view.read_bytes(path)
    except Exception:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def build_source_map(bundle_view: Any) -> Dict[str, str]:
    """Map ``digest.source_id`` → article path without guessing file names."""
    source_map: Dict[str, str] = {}
    for entry in bundle_view.list_entries("sources/digests/"):
        if not entry.endswith(".json"):
            continue
        raw = bundle_view.read_bytes(entry)
        try:
            digest = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        source_id = digest.get("source_id") if isinstance(digest, dict) else None
        if not isinstance(source_id, str) or not source_id:
            continue
        source_map[source_id] = "sources/articles/{}.md".format(source_id)
    return source_map


def load_document(context: Dict[str, Any], source_id: str) -> Optional[Document]:
    """Read and cache one article per source per query."""
    documents = context.setdefault("documents", {})
    if source_id in documents:
        return documents[source_id]
    bundle_view = context.get("bundle_view")
    if bundle_view is None:
        return None
    source_map = context.get("source_map") or build_source_map(bundle_view)
    context["source_map"] = source_map
    article_path = source_map.get(source_id)
    if article_path is None:
        documents[source_id] = None
        return None
    text = _decode_text(bundle_view, article_path)
    if text is None:
        documents[source_id] = None
        return None
    revision_id = getattr(bundle_view, "revision_id", "") or ""
    document = Document(
        source_id=source_id,
        article_path=article_path,
        revision_id=revision_id,
        text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        text=text,
    )
    documents[source_id] = document
    return document


def load_digest(context: Dict[str, Any], source_id: str) -> Optional[Dict[str, Any]]:
    digests = context.setdefault("digests", {})
    if source_id in digests:
        return digests[source_id]
    bundle_view = context.get("bundle_view")
    if bundle_view is None:
        return None
    raw = _decode_text(bundle_view, "sources/digests/{}.json".format(source_id))
    if raw is None:
        digests[source_id] = None
        return None
    try:
        digest = json.loads(raw)
    except ValueError:
        digests[source_id] = None
        return None
    digests[source_id] = digest
    return digest


def load_reading_materials(context: Dict[str, Any], source_id: str) -> Optional[Dict[str, Any]]:
    """Load the published reading-material catalog for one source, if present."""
    materials = context.setdefault("reading_materials", {})
    if source_id in materials:
        return materials[source_id]
    digest = load_digest(context, source_id)
    if digest is None:
        materials[source_id] = None
        return None
    value = digest.get("reading_materials")
    if not isinstance(value, dict):
        materials[source_id] = None
        return None
    materials[source_id] = value
    return value


def _material_unit(catalog: Optional[Dict[str, Any]], *, kind: str,
                   unit_id: str) -> Optional[Dict[str, Any]]:
    if not catalog:
        return None
    for unit in catalog.get("units") or []:
        if not isinstance(unit, dict):
            continue
        if unit.get("kind") == kind and str(unit.get("unit_id") or "") == str(unit_id):
            return unit
    return None


def _workbook_unit(catalog: Optional[Dict[str, Any]], source_id: str) -> Optional[Dict[str, Any]]:
    """Return the canonical workbook, upgrading an old spreadsheet catalog."""
    existing = _material_unit(catalog, kind="workbook", unit_id=source_id)
    if existing is not None:
        return existing
    if not catalog or catalog.get("route") != "spreadsheet_native":
        return None
    sheets = [unit for unit in catalog.get("units") or []
              if isinstance(unit, dict) and unit.get("kind") == "sheet"]
    if not sheets:
        return None
    first_location = sheets[0].get("content_location") or {}
    return {
        "unit_id": source_id,
        "kind": "workbook",
        "name": ((catalog.get("source_identity") or {}).get("source_name")
                 or source_id),
        "content_kind": "spreadsheet_workbook",
        "content_location": {"tables_path": first_location.get("tables_path")},
        "sheets": [{
            "sheet_id": unit.get("sheet_id"),
            "name": unit.get("name"),
            "order": unit.get("sheet_order"),
            "visibility": unit.get("visibility"),
            "table_ids": list(unit.get("table_ids") or []),
        } for unit in sheets],
        "table_ids": [table_id for unit in sheets
                      for table_id in unit.get("table_ids") or []],
    }


def render_reading_material_body(
    state: Dict[str, Any], material: Dict[str, Any],
) -> Optional[Tuple[str, str]]:
    """Return ``(body, selected_level)`` for one resolved material unit."""
    unit = material.get("unit") if isinstance(material, dict) else None
    source_id = str(material.get("source_id") or "") if isinstance(material, dict) else ""
    if not isinstance(unit, dict):
        return None
    bundle_view = state.get("bundle_view")
    if bundle_view is None:
        return None
    location = unit.get("content_location") or {}
    kind = unit.get("kind")
    if kind == "document":
        path = location.get("article_path")
        if not path:
            return None
        text = _decode_text(bundle_view, path)
        if text is None:
            return None
        return text, "full_document"
    if kind in ("workbook", "sheet", "table"):
        path = location.get("tables_path")
        if not path:
            return None
        try:
            connection = bundle_view.open_sqlite(path)
        except Exception:
            return None
        connection.row_factory = sqlite3.Row
        try:
            from jonex_wiki.domain.engine.query.table_reader import (
                full_sheet_body, full_table_body,
            )
            if kind == "workbook":
                parts = []
                for sheet in unit.get("sheets") or []:
                    sheet_id = str(sheet.get("sheet_id") or "")
                    if not sheet_id:
                        continue
                    parts.append(full_sheet_body(connection, source_id, sheet_id))
                body = "\n\n".join(part for part in parts if part.strip())
                return (body, "full_workbook") if body.strip() else None
            if kind == "sheet":
                body = full_sheet_body(connection, source_id, str(unit.get("sheet_id") or ""))
                return body, "full_sheet"
            body = full_table_body(connection, source_id, str(unit.get("table_id") or ""))
            return body, "full_table"
        except Exception:
            return None
        finally:
            connection.close()
    return None



def _registered_table_unit(state: Mapping[str, Any], source_id: str,
                           table_id: str) -> Optional[Dict[str, Any]]:
    """Older catalogs omit native tables already saved in the registry."""
    info = (state.get("table_registry") or {}).get(table_id) or {}
    if info.get("source_id") == source_id and info.get("path"):
        return {"kind": "table", "unit_id": table_id, "table_id": table_id,
                "content_location": {"tables_path": info["path"]}}
    return None


def resolve_reading_material(
    state: Dict[str, Any], corpus: Any, requested_path: str,
) -> Optional[Dict[str, Any]]:
    """Resolve one requested wiki path to a published reading-material unit."""
    context = _read_context(state)
    unit: Optional[Dict[str, Any]] = None
    source_id: Optional[str] = None
    page_entry = corpus.page_index.get(requested_path)

    if requested_path.startswith("tables/"):
        table_id = None
        if page_entry is not None:
            table_id = getattr(page_entry, "object_id", None)
        if not table_id:
            table_id = requested_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        source_id = None
        registry = state.get("table_registry") or {}
        info = registry.get(str(table_id))
        if info:
            source_id = str(info.get("source_id") or "")
        if not source_id:
            for candidate_source in list(context.get("source_map") or {}):
                catalog = load_reading_materials(context, candidate_source)
                found = _material_unit(catalog, kind="table", unit_id=str(table_id))
                if found is not None:
                    source_id = candidate_source
                    unit = found
                    break
        if source_id:
            catalog = load_reading_materials(context, source_id)
            unit = (_material_unit(catalog, kind="table", unit_id=str(table_id))
                    or _registered_table_unit(state, source_id, str(table_id)))
        if unit is None:
            return None
        return {"catalog": load_reading_materials(context, source_id), "unit": unit,
                "source_id": source_id}

    if requested_path.startswith((
            "raw-tables/", "source-articles/", "source-evidence/")):
        scope = (getattr(page_entry, "scope", None) or {}) if page_entry is not None else {}
        source_id = str(scope.get("source_id") or "")
        if not source_id and requested_path.startswith("source-evidence/"):
            records = corpus.source_records.get(requested_path) or []
            if records:
                source_id = str(records[0].get("source_id")
                                or records[0].get("document_id")
                                or "")
        if not source_id:
            return None
        catalog = load_reading_materials(context, source_id)
        if requested_path.startswith(("raw-tables/", "source-evidence/")):
            if requested_path.startswith("raw-tables/"):
                table_id = str(scope.get("table_id") or getattr(page_entry, "table_id", "") or "")
                unit = (_material_unit(catalog, kind="table", unit_id=table_id)
                        or _registered_table_unit(state, source_id, table_id))
            else:
                unit = (_material_unit(catalog, kind="document", unit_id=source_id)
                        or _workbook_unit(catalog, source_id))
        else:
            unit = (_material_unit(catalog, kind="document", unit_id=source_id)
                    or _workbook_unit(catalog, source_id))
        if unit is None:
            return None
        return {"catalog": catalog, "unit": unit, "source_id": source_id}

    if requested_path.startswith("sources/"):
        source_id = None
        if page_entry is not None:
            source_id = (getattr(page_entry, "object_id", None)
                         or (getattr(page_entry, "scope", None) or {}).get("source_id"))
        if not source_id:
            records = corpus.source_records.get(requested_path) or []
            if records:
                source_id = records[0].get("source_id") or records[0].get("document_id")
        if not source_id:
            return None
        catalog = load_reading_materials(context, source_id)
        unit = (_material_unit(catalog, kind="document", unit_id=source_id)
                or _workbook_unit(catalog, source_id))
        if unit is None:
            return None
        return {"catalog": catalog, "unit": unit, "source_id": source_id}

    return None


# ──────────────────────────────────────────────────────────────
#  Saved structure (minimal section tree used by material views)
# ──────────────────────────────────────────────────────────────

_ATX_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*$")
_SETEXT_RE = re.compile(r"^[ \t]*(=+|-+)[ \t]*$")


def _markdown_headings(text: str) -> Iterator[Tuple[int, int, str]]:
    in_fence = False
    fence = ""
    lines = text.splitlines(keepends=True)
    offset = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            marker = stripped[:3]
            if not in_fence:
                in_fence, fence = True, marker
            elif stripped.startswith(fence):
                in_fence = False
            offset += len(line)
            continue
        if not in_fence:
            match = _ATX_RE.match(stripped)
            if match:
                title = match.group(2).strip()
                if title:
                    yield offset, len(match.group(1)), title
        offset += len(line)
    non_empty = [(i, line) for i, line in enumerate(lines) if line.strip()]
    for pos, (index, line) in enumerate(non_empty):
        if pos + 1 >= len(non_empty):
            continue
        next_line = non_empty[pos + 1][1]
        if not _SETEXT_RE.match(next_line.strip()):
            continue
        title = line.strip()
        if not title or _ATX_RE.match(title):
            continue
        level = 1 if next_line.strip().startswith("=") else 2
        start = sum(len(lines[j]) for j in range(index))
        yield start, level, title


def _line_at(text: str, start: int) -> str:
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", start)
    if line_end < 0:
        line_end = len(text)
    stripped = text[line_start:line_end].strip()
    match = _ATX_RE.match(stripped)
    if match:
        return match.group(2).strip()
    return stripped


def load_saved_structure(
    context: Dict[str, Any], document: Document, digest: Optional[Dict[str, Any]],
) -> List[Section]:
    """Build the heading tree from compiled sections or Markdown fallback."""
    structures = context.setdefault("structures", {})
    if document.source_id in structures:
        return structures[document.source_id]
    article = document.text

    catalog = load_reading_materials(context, document.source_id)
    unit = _material_unit(catalog, kind="document", unit_id=document.source_id)
    compiled_sections = (unit or {}).get("sections")
    if isinstance(compiled_sections, list) and compiled_sections:
        root = Section(id=-1, parent_id=None, level=0, title="",
                       start=0, end=len(article))
        sections: List[Section] = [root]
        for item in compiled_sections:
            if not isinstance(item, dict):
                continue
            start = item.get("start")
            level = item.get("level")
            title = item.get("title")
            if not (isinstance(start, int) and isinstance(level, int)
                    and isinstance(title, str) and title):
                continue
            if not 0 <= start < len(article) or not 1 <= level <= 6:
                continue
            parent = item.get("parent")
            parent_id = parent if isinstance(parent, int) else -1
            end = item.get("end")
            sections.append(Section(
                id=start, parent_id=parent_id, level=level, title=title,
                start=start,
                end=end if isinstance(end, int) and end > start else len(article),
                origin="reading_materials",
            ))
        sections.sort(key=lambda section: section.start)
        structures[document.source_id] = sections
        return sections

    headings: List[Dict[str, Any]] = []
    for start, level, title in _markdown_headings(article):
        headings.append({"start": start, "level": level, "title": title,
                         "origin": "markdown_fallback"})
    headings.sort(key=lambda item: item["start"])

    root = Section(id=-1, parent_id=None, level=0, title="", start=0,
                   end=len(article))
    sections: List[Section] = [root]
    stack: List[Section] = [root]
    for heading in headings:
        new_level = heading["level"]
        while stack and stack[-1].level >= new_level:
            stack[-1].end = heading["start"]
            stack.pop()
        if not stack:
            stack = [root]
        parent = stack[-1]
        node = Section(id=heading["start"], parent_id=parent.id,
                       level=new_level, title=heading["title"],
                       start=heading["start"], end=len(article),
                       origin=heading["origin"])
        sections.append(node)
        stack.append(node)
    structures[document.source_id] = sections
    return sections


def _section_for(sections: Sequence[Section], start: int, end: int) -> Section:
    best: Optional[Section] = None
    for section in sections:
        if section.start <= start and end <= section.end:
            if best is None or section.start >= best.start:
                best = section
    if best is not None:
        return best
    return Section(id=-1, parent_id=None, level=0, title="",
                   start=min(s.start for s in sections),
                   end=max(s.end for s in sections))


def _ancestors(sections: Sequence[Section], section: Section) -> List[Section]:
    by_id = {section.id: section for section in sections}
    chain: List[Section] = []
    node: Optional[Section] = section
    while node is not None:
        chain.append(node)
        parent = by_id.get(node.parent_id) if node.parent_id is not None else None
        if parent is None or parent.id == -1:
            break
        node = parent
    chain.reverse()
    return chain


def document_blocks(document: Document,
                    focuses: Sequence[Focus]) -> List[Tuple[int, int]]:
    """Complete blocks covering each focus, from saved locations or units."""
    article = document.text
    unit_spans: Optional[List[Tuple[int, int]]] = None
    for focus in focuses:
        for ref in focus.passage_refs:
            metadata = ref.get("metadata") or {}
            locations = metadata.get("source_locations")
            if not isinstance(locations, list) or not locations:
                continue
            unit_spans = []
            for loc in locations:
                s = loc.get("char_start")
                e = loc.get("char_end")
                if isinstance(s, int) and isinstance(e, int) and s < e:
                    unit_spans.append((int(s), int(e)))
            break
        if unit_spans:
            break

    if unit_spans is None:
        units = identify_units(article) if identify_units is not None else []
        unit_spans = [(int(unit.start), int(unit.end)) for unit in units]

    blocks: List[Tuple[int, int]] = []
    for focus in focuses:
        for span in focus.spans:
            f0, f1 = span
            covering = [b for b in unit_spans if b[0] <= f0 and f1 <= b[1]]
            if covering:
                blocks.append(min(covering, key=lambda item: item[1] - item[0]))
                continue
            crossing = [b for b in unit_spans if b[0] < f1 and b[1] > f0]
            blocks.extend(crossing)
    return merge_ranges(blocks)


def _blocks_view(document: Document, spans: Sequence[Tuple[int, int]], *,
                 coverage: str, selected_level: str, origin: str,
                 covered: Sequence[Tuple[int, int]],
                 citation_refs: Sequence[Dict[str, str]]) -> ReadingView:
    spans = merge_ranges(spans)
    return ReadingView(
        object_key=document.object_key,
        source_id=document.source_id,
        article_path=document.article_path,
        spans=spans,
        blocks=[{"start": s, "end": e, "body": document.text[s:e]}
                for s, e in spans],
        citation_refs=list(citation_refs),
        coverage=coverage,
        structure_origin=origin,
        selected_level=selected_level,
        covered_focuses=[tuple(s) for s in covered],
    )


def _full_candidate(document: Document, focuses: Sequence[Focus]) -> ReadingView:
    refs: List[Dict[str, str]] = []
    for focus in focuses:
        refs.extend(focus.citation_refs)
    return _blocks_view(
        document, [(0, len(document.text))],
        coverage="document_complete", selected_level="article",
        origin="plain_range",
        covered=[span for focus in focuses for span in focus.spans],
        citation_refs=refs,
    )


def reading_view_payload(view: "ReadingView") -> Dict[str, Any]:
    """Serialize one ``ReadingView`` for the synthesis ``source_reading_context``."""
    return {
        "revision_id": view.object_key[0] if view.object_key else "",
        "source_id": view.source_id,
        "article_path": view.article_path,
        "text_sha256": view.object_key[3] if len(view.object_key) > 3 else "",
        "spans": [[int(start), int(end)] for start, end in view.spans],
        "coverage": view.coverage,
        "structure_origin": view.structure_origin,
        "selected_level": view.selected_level,
        "blocks": [{"start": int(block["start"]), "end": int(block["end"]),
                    "body": block.get("body", "")} for block in view.blocks],
        "citation_refs": [dict(ref) for ref in view.citation_refs],
        "title_path": [dict(item) for item in getattr(view, "title_path", [])],
    }


__all__ = [
    "Document",
    "Focus",
    "ReadingView",
    "ReadPlan",
    "Section",
    "_fully_covered",
    "_material_unit",
    "_read_context",
    "_workbook_unit",
    "build_source_map",
    "document_blocks",
    "load_digest",
    "load_document",
    "load_reading_materials",
    "load_saved_structure",
    "map_chunk",
    "merge_ranges",
    "project",
    "reading_view_payload",
    "render_reading_material_body",
    "resolve_reading_material",
    "subtract_ranges",
    "unique_offset",
]


# ──────────────────────────────────────────────────────────────
#  Ported planning functions (integration read_views.py tail)
# ──────────────────────────────────────────────────────────────

def _focus_from_source_evidence(
    context: Dict[str, Any], corpus: Any, requested_path: str,
) -> Optional[Focus]:
    """Resolve a ``source-evidence/...`` path to an article focus."""
    records = corpus.source_records.get(requested_path) or []
    location = corpus.source_locations.get(requested_path) or {}
    if not records or not location:
        return None
    record = records[0]
    source_id = record.get("source_id") or record.get("document_id") or ""
    if not source_id:
        return None
    document = load_document(context, source_id)
    if document is None:
        return None
    metadata = location.get("metadata") or {}
    focus = Focus(
        requested_path=requested_path,
        source_id=source_id,
        object_key=document.object_key,
        citation_refs=[dict(record)],
        passage_refs=[dict(location)],
    )

    mapping = map_chunk(document.text, metadata)
    if mapping is not None:
        local_start = location.get("start_char")
        local_end = location.get("end_char")
        if (isinstance(local_start, int) and isinstance(local_end, int)
                and local_start < local_end):
            # Publication stores intervals relative to the passage snapshot;
            # map_chunk/project use article coordinates.  Mixing those axes
            # silently upgraded a short hit to its entire ingestion chunk.
            chunk_start, chunk_end = mapping[:2]
            projected = None
            if 0 <= local_start < local_end <= chunk_end - chunk_start:
                projected = project(mapping, chunk_start + local_start,
                                    chunk_start + local_end)
            # Older callers may already supply article coordinates. Accept
            # that form only when the actual source excerpt identifies it,
            # rather than inferring the coordinate system from numeric size.
            absolute = project(mapping, local_start, local_end)
            excerpt = record.get("excerpt") or ""
            if (absolute is not None and excerpt
                    and document.text[absolute[0]:absolute[1]] == excerpt
                    and (projected is None or
                         document.text[projected[0]:projected[1]] != excerpt)):
                projected = absolute
            if projected is not None:
                focus.spans = [list(projected)]
                return focus
        focus.spans = [[mapping[2], mapping[2] + (mapping[1] - mapping[0])]]
        return focus

    # No saved interval: unique full-passage relocation inside the same article.
    excerpt = record.get("excerpt") or ""
    if excerpt:
        offset = unique_offset(document.text, excerpt)
        if offset is not None:
            focus.spans = [[offset, offset + len(excerpt)]]
            return focus
    return focus



def _focus_from_source_page(
    context: Dict[str, Any], corpus: Any, requested_path: str,
) -> Optional[Focus]:
    """Resolve a ``sources/source-<hash>.md`` navigation page to its article."""
    page_entry = corpus.page_index.get(requested_path)
    if page_entry is None:
        return None
    scope = getattr(page_entry, "scope", None) or {}
    source_id = (getattr(page_entry, "object_id", None)
                 or (scope.get("source_id") if isinstance(scope, dict) else None))
    if not isinstance(source_id, str) or not source_id:
        return None
    document = load_document(context, source_id)
    if document is None:
        return None
    catalog = load_reading_materials(context, source_id)
    if catalog is not None and not any(
        isinstance(unit, dict) and unit.get("kind") == "document"
        for unit in catalog.get("units") or []
    ):
        # Native spreadsheet source: its navigation page is the complete first
        # worksheet via the reading-material path, never the summary article.
        return None
    focus = Focus(requested_path=requested_path, source_id=source_id,
                  object_key=document.object_key, citation_refs=[],
                  open_request=True)
    return focus



def _focus_from_article_page(
    context: Dict[str, Any], corpus: Any, requested_path: str,
) -> Optional[Focus]:
    """Resolve a published article unit, retaining the legacy open fallback."""
    page_entry = corpus.page_index.get(requested_path)
    scope = (getattr(page_entry, "scope", None) or {}) if page_entry is not None else {}
    source_id = str(scope.get("source_id") or "")
    if not source_id:
        return None
    document = load_document(context, source_id)
    if document is None:
        return None
    focus = Focus(requested_path=requested_path, source_id=source_id,
                  object_key=document.object_key, citation_refs=[],
                  open_request=True)
    span = _source_article_units(context, corpus, document).get(requested_path)
    if span is not None:
        focus.spans = [span]
        focus.open_request = False
    return focus


def _source_article_units(
    context: Dict[str, Any], corpus: Any, document: Document,
) -> Dict[str, Tuple[int, int]]:
    """Locate literal published units once per source object in this query."""
    cached = context.setdefault("article_units", {})
    if document.object_key in cached:
        return cached[document.object_key]
    units: Dict[str, Tuple[int, int]] = {}
    cached[document.object_key] = units
    if _workbook_unit(load_reading_materials(context, document.source_id),
                      document.source_id) is not None:
        return units
    files = getattr(corpus, "files", {})
    for path, entry in getattr(corpus, "page_index", {}).items():
        if (not path.startswith("source-articles/")
                or (getattr(entry, "scope", None) or {}).get("source_id") != document.source_id):
            continue
        rendered = files.get(path) or ""
        # _literal_source_block publishes one literal text fence. Only that
        # body, never a title/snippet/generated paragraph, may locate a unit.
        fence = re.search(r"(?m)^(`{3,})text\n", rendered)
        if fence is None:
            continue
        end = rendered.find("\n" + fence.group(1) + "\n", fence.end())
        if end < 0:
            continue
        text = rendered[fence.end():end]
        start = unique_offset(document.text, text)
        if start is not None:
            units[path] = (start, start + len(text))
    return units


def _expand_source_unit(context: Dict[str, Any], corpus: Any, focus: Focus) -> Focus:
    document = load_document(context, focus.source_id)
    if document is None or not focus.spans:
        return focus
    candidates = [span for span in _source_article_units(context, corpus, document).values()
                  if _fully_covered(focus.spans[0], [span])
                  and all(_fully_covered(other, [span]) for other in focus.spans)]
    if not candidates:
        return focus
    span = min(candidates, key=lambda item: item[1] - item[0])
    if [tuple(item) for item in focus.spans] == [span]:
        return focus
    # Keep the short span's old identity in the ledger. The larger delivery
    # receives its own source_read ref, not that short excerpt's citation.
    return Focus(requested_path=focus.requested_path, source_id=focus.source_id,
                 object_key=focus.object_key, spans=[span])



def resolve_focus(context: Dict[str, Any], corpus: Any,
                  requested_path: str) -> Optional[Focus]:
    """Resolve one requested path to a focus, or ``None`` when non-source."""
    if requested_path.startswith("source-evidence/"):
        return _focus_from_source_evidence(context, corpus, requested_path)
    if requested_path.startswith("source-articles/"):
        return _focus_from_article_page(context, corpus, requested_path)
    if requested_path.startswith("sources/"):
        return _focus_from_source_page(context, corpus, requested_path)
    return None



def _focuses_from_page_sources(
    context: Dict[str, Any], corpus: Any, requested_path: str,
) -> List[Focus]:
    """Bridge a published content page to all of its archived source passages."""
    if (requested_path not in getattr(corpus, "files", {})
            or corpus.source_records.get(requested_path)):
        return []
    claims = getattr(corpus, "claims", {}).get(requested_path) or []
    paths = getattr(corpus, "evidence_fallback", {}).get(requested_path, ())
    if not claims and not paths:
        return []
    from jonex_wiki.domain.engine.query.retrieval import (
        build_evidence_fallback, page_source_ids_from_markdown,
    )
    evidence_passage_ids = (
        [ref.passage_id for claim in claims for ref in claim.evidence] +
        [record["passage_id"] for path in paths
         for record in corpus.source_records.get(path, ())])
    known_passage_ids = {
        record.get("passage_id") for records in corpus.source_records.values()
        for record in records
    } | set(evidence_passage_ids)
    known_source_ids = set(context.get("source_map") or {}) | {
        record[key] for records in corpus.source_records.values()
        for record in records for key in ("source_id", "document_id")
        if record.get(key)
    }
    # Older pages declare document IDs in source_ids, while claim evidence
    # carries passage IDs. A known document is neither a missing passage nor
    # permission to read its whole article; only the actual passages bridge.
    page_passage_ids = [source_id for source_id in
        page_source_ids_from_markdown(corpus.files[requested_path])
        if source_id not in known_source_ids or source_id in known_passage_ids]
    passage_ids = list(dict.fromkeys(page_passage_ids + evidence_passage_ids))
    if passage_ids:
        paths = build_evidence_fallback({requested_path: passage_ids},
                                       corpus.source_records).get(requested_path, ())
        if len(paths) != len(passage_ids):
            return []
    # A generated fact page requests its supported facts, not every fact in
    # their ingestion passages. Use the already validated source intervals;
    # old/partial claims retain the existing whole-passage fallback.
    claim_spans: Dict[str, List[Any]] = {}
    incomplete_spans = set()
    for claim in claims:
        for ref in claim.evidence:
            spans = [span for span in getattr(claim, "source_spans", ())
                     if span.passage_id == ref.passage_id]
            if spans:
                claim_spans.setdefault(ref.passage_id, []).extend(spans)
            else:
                incomplete_spans.add(ref.passage_id)
    focuses: List[Focus] = []
    seen = set()
    for path in paths:
        location = corpus.source_locations.get(path) or {}
        records = corpus.source_records.get(path) or []
        if not records:
            return []
        for record in records:
            source_id = record.get("source_id") or record.get("document_id") or ""
            document = load_document(context, source_id) if source_id else None
            if document is None:
                return []
            if _workbook_unit(load_reading_materials(context, source_id), source_id) is not None:
                return []
            mapping = map_chunk(document.text, location.get("metadata") or {})
            if mapping is None:
                return []
            targets = (claim_spans.get(record.get("passage_id"), [])
                       if record.get("passage_id") not in incomplete_spans else [])
            mapped = []
            snapshot = (location.get("metadata") or {}).get("snapshot_text", "")
            for target in targets:
                projected = project(mapping, mapping[0] + target.start_char,
                                    mapping[0] + target.end_char)
                if (target.source_id == source_id
                        and target.document_id == record.get("document_id")
                        and target.chunk_id == record.get("chunk_id")
                        and target.passage_text_sha256 == hashlib.sha256(snapshot.encode()).hexdigest()
                        and snapshot[target.start_char:target.end_char] == target.text
                        and projected is not None
                        and document.text[projected[0]:projected[1]] == target.text):
                    mapped.append(projected)
            if targets and len(mapped) == len(targets):
                # Keep complete original blocks and the existing title path;
                # do not manufacture a citation for a generated summary.
                focus = Focus(requested_path=requested_path, source_id=source_id,
                              object_key=document.object_key, spans=mapped)
                spans = document_blocks(document, [focus]) or mapped
            else:
                span = project(mapping, mapping[0], mapping[1])
                spans = [span] if span is not None else []
            for span in spans:
                key = (source_id, span)
                if key in seen:
                    continue
                seen.add(key)
                focuses.append(Focus(
                    requested_path=requested_path, source_id=source_id,
                    object_key=document.object_key, spans=[span],
                ))
    return focuses



def related_table_paths(corpus: Any, evidence: Sequence[Mapping[str, Any]],
                        requested_paths: Sequence[str]) -> List[str]:
    """Recover native tables interleaved with the requested parser passages.

    Packed prose omits table blocks. Original parser positions, rather than
    relocated article offsets or a guessed heading, preserve their association.
    A directly requested PDF table also brings its same-page header fragments.
    """
    passage_ids = {str(item.get("passage_id") or "") for item in evidence}
    excerpts = {}
    for item in evidence:
        text = re.sub(r"\s+", "", str(item.get("excerpt") or "").replace("\\$", "$"))
        if text:
            excerpts.setdefault(str(item.get("passage_id") or ""), []).append(text)
    evidence_pages = set()
    ranges: Dict[str, List[Tuple[int, int]]] = {}
    tables: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for path, records in corpus.source_records.items():
        metadata = (corpus.source_locations.get(path) or {}).get("metadata") or {}
        for record in records:
            source_id = str(record.get("source_id") or record.get("document_id") or "")
            if record.get("passage_id") in passage_ids:
                locations = metadata.get("source_locations") or []
                matched = []
                for location in locations:
                    text = re.sub(r"\s+", "", str((location.get("raw_item") or {}).get("text") or ""))
                    if text and any(excerpt in text or text in excerpt
                                    for excerpt in excerpts.get(str(record.get("passage_id")), [])):
                        matched.append(location)
                if matched:
                    evidence_pages.update((source_id, item["page_number"]) for item in matched
                                          if isinstance(item.get("page_number"), int))
                else:
                    positions = [item["position"] for item in locations
                                 if isinstance(item.get("position"), int)]
                    if positions:
                        ranges.setdefault(source_id, []).append((min(positions), max(positions)))
            table_id = metadata.get("table_id")
            if table_id and isinstance(metadata.get("page_number"), int):
                tables[(source_id, str(table_id))] = metadata
    raw_paths: Dict[Tuple[str, str], str] = {}
    requested_pages = set()
    for path, entry in corpus.page_index.items():
        scope = getattr(entry, "scope", None) or {}
        key = (str(scope.get("source_id") or ""), str(scope.get("table_id") or ""))
        if path.startswith("raw-tables/"):
            raw_paths.setdefault(key, path)
        if path in requested_paths and key in tables:
            requested_pages.add((key[0], tables[key]["page_number"]))
    return [raw_paths[key] for key, metadata in sorted(
                tables.items(), key=lambda item: (item[0][0], item[1].get("source_position", -1)))
            if key in raw_paths and (
                (key[0], metadata["page_number"]) in requested_pages | evidence_pages
                or any(start < metadata.get("source_position", -1) < end
                       for start, end in ranges.get(key[0], [])))]


def resolve_requested_sources(
    context: Dict[str, Any], corpus: Any, requested_paths: Sequence[str],
    *, expand_units: bool = True,
) -> Tuple[Dict[str, Document], List[Focus]]:
    """Group requested paths into documents and focuses, preserving order."""
    documents: Dict[str, Document] = {}
    focuses: List[Focus] = []
    seen_paths = set()
    for path in requested_paths:
        if path in seen_paths:
            continue
        seen_paths.add(path)
        focus = resolve_focus(context, corpus, path)
        if focus is not None and expand_units and path.startswith("source-evidence/"):
            focus = _expand_source_unit(context, corpus, focus)
        resolved = ([focus] if focus is not None else
                    _focuses_from_page_sources(context, corpus, path))
        for focus in resolved:
            if not focus.spans and not focus.open_request:
                # Failed mapping must not become a whole-article request.
                continue
            document = load_document(context, focus.source_id)
            if document is None:
                continue
            documents[focus.source_id] = document
            focuses.append(focus)
    return documents, focuses


# ──────────────────────────────────────────────────────────────
#  Saved structure (§5.2 heading tree)
# ──────────────────────────────────────────────────────────────

_ATX_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*$")
_SETEXT_RE = re.compile(r"^[ \t]*(=+|-+)[ \t]*$")



def _lowest_common_ancestor(sections: Sequence[Section],
                            focus_sections: Sequence[Section]) -> Section:
    by_id = {section.id: section for section in sections}

    def path(section: Section) -> List[int]:
        ids: List[int] = [section.id]
        node: Optional[Section] = section
        while node is not None:
            parent = by_id.get(node.parent_id) if node.parent_id is not None else None
            if parent is None or parent.id == -1:
                break
            ids.append(parent.id)
            node = parent
        return list(reversed(ids))

    def virtual_root() -> Section:
        starts = [section.start for section in sections]
        ends = [section.end for section in sections]
        return Section(id=-1, parent_id=None, level=0, title="",
                       start=min(starts), end=max(ends))

    common = path(focus_sections[0])
    for section in focus_sections[1:]:
        other = path(section)
        bound = min(len(common), len(other))
        i = 0
        while i < bound and common[i] == other[i]:
            i += 1
        common = common[:i]
        if not common:
            return virtual_root()
    return by_id.get(common[-1], virtual_root())


# ──────────────────────────────────────────────────────────────
#  Complete blocks (layer 3)
# ──────────────────────────────────────────────────────────────



def _block_of(blocks: Sequence[Tuple[int, int]], span: Tuple[int, int]) -> Optional[Tuple[int, int]]:
    f0, f1 = span
    covering = [b for b in blocks if b[0] <= f0 and f1 <= b[1]]
    if covering:
        return min(covering, key=lambda item: item[1] - item[0])
    crossing = [b for b in blocks if b[0] < f1 and b[1] > f0]
    if not crossing:
        return None
    merged = merge_ranges(crossing)
    return merged[0]


# ──────────────────────────────────────────────────────────────
#  Candidate construction (layers 1-3)
# ──────────────────────────────────────────────────────────────



def build_full_then_structured_candidates(
    context: Dict[str, Any],
    document: Document,
    focuses: Sequence[Focus],
    blocks: Sequence[Tuple[int, int]],
) -> List[ReadingView]:
    """Layers 1-3 as one descending candidate list for a single document."""
    refs: List[Dict[str, str]] = []
    for focus in focuses:
        refs.extend(focus.citation_refs)

    candidates: List[ReadingView] = [_full_candidate(document, focuses)]

    digest = load_digest(context, document.source_id)
    sections = load_saved_structure(context, document, digest)

    # Layer 2: enclosing sections, lowest common ancestor, independent sections.
    focus_sections = [_section_for(sections, s, e) for focus in focuses
                      for s, e in focus.spans]
    lca = (_lowest_common_ancestor(sections, focus_sections)
           if focus_sections else sections[0])
    chain = _ancestors(sections, lca)
    seen_spans: set = set()
    for section in chain:  # outermost first (largest first)
        key = (section.start, section.end)
        if key in seen_spans:
            continue
        seen_spans.add(key)
        candidates.append(_blocks_view(
            document, [(section.start, section.end)],
            coverage="section_complete", selected_level="section",
            origin=section.origin,
            covered=[span for focus in focuses for span in focus.spans
                     if section.start <= span[0] and span[1] <= section.end],
            citation_refs=refs,
        ))

    if lca.id == -1 or len(chain) == 0:
        independent = _independent_sections(sections, focuses)
        for spans in independent:
            key = tuple(spans)
            if key in seen_spans:
                continue
            seen_spans.add(key)
            candidates.append(_blocks_view(
                document, spans,
                coverage="section_complete", selected_level="section",
                origin="saved_metadata",
                covered=[span for focus in focuses for span in focus.spans
                         if any(s <= span[0] and span[1] <= e for s, e in spans)],
                citation_refs=refs,
            ))

    # Layer 3: complete blocks.
    if blocks:
        candidates.append(_blocks_view(
            document, blocks,
            coverage="ranges_partial", selected_level="blocks",
            origin="saved_metadata",
            covered=[span for focus in focuses for span in focus.spans],
            citation_refs=refs,
        ))
    return candidates



def _independent_sections(sections: Sequence[Section],
                          focuses: Sequence[Focus]) -> List[List[Tuple[int, int]]]:
    """Independent-section candidates, refined one level at a time (§6.1 L2)."""
    by_id = {section.id: section for section in sections}

    def first_level(section: Section) -> Section:
        node = section
        while node.parent_id not in (-1, None):
            node = by_id.get(node.parent_id, node)
        return node

    current: Dict[int, Section] = {}
    for focus in focuses:
        for span in focus.spans:
            section = _section_for(sections, span[0], span[1])
            top = first_level(section)
            if top.id not in current:
                current[top.id] = top
    if not current:
        return []

    candidates: List[List[Tuple[int, int]]] = []
    seen: set = set()
    while True:
        spans = merge_ranges([(section.start, section.end)
                              for section in current.values()])
        key = tuple(spans)
        if key not in seen:
            seen.add(key)
            candidates.append(spans)
        refined: Dict[int, Section] = {}
        changed = False
        for section_id, section in current.items():
            children = [s for s in sections if s.parent_id == section.id]
            if not children:
                refined[section_id] = section
                continue
            covering = _covering_children(sections, section, focuses)
            if covering == [(section.start, section.end)]:
                refined[section_id] = section
                continue
            for child in children:
                if any(child.start <= c[0] and c[1] <= child.end for c in covering):
                    refined[child.id] = child
                    changed = True
        if not changed:
            break
        current = refined
    return candidates



def _covering_children(sections: Sequence[Section], section: Section,
                       focuses: Sequence[Focus]) -> List[Tuple[int, int]]:
    children = [s for s in sections if s.parent_id == section.id]
    if not children:
        return [(section.start, section.end)]
    needed: List[Tuple[int, int]] = []
    for focus in focuses:
        for span in focus.spans:
            if not (section.start <= span[0] and span[1] <= section.end):
                continue
            child = next((c for c in children
                          if c.start <= span[0] and span[1] <= c.end), None)
            needed.append((child.start, child.end) if child else
                          (section.start, section.end))
    return merge_ranges(needed)


# ──────────────────────────────────────────────────────────────
#  Selection
# ──────────────────────────────────────────────────────────────



def _combine_views(views: Sequence[ReadingView]) -> ReadingView:
    """Merge views into one previewable candidate (same object key)."""
    spans = merge_ranges([span for view in views for span in view.spans])
    blocks: List[Dict[str, Any]] = []
    seen: set = set()
    refs: List[Dict[str, str]] = []
    for view in views:
        for block in view.blocks:
            key = (block["start"], block["end"])
            if key in seen:
                continue
            seen.add(key)
            blocks.append(dict(block))
        for ref in view.citation_refs:
            key = json.dumps(ref, sort_keys=True, ensure_ascii=False)
            if key in seen:
                continue
            seen.add(key)
            refs.append(ref)
    first = views[0]
    return ReadingView(
        object_key=first.object_key, source_id=first.source_id,
        article_path=first.article_path, spans=spans, blocks=blocks,
        citation_refs=refs, coverage=first.coverage,
        structure_origin=first.structure_origin,
        selected_level=first.selected_level,
        covered_focuses=[tuple(s) for view in views for s in view.covered_focuses],
    )



def select_combination(
    per_document_candidates: Sequence[Tuple[str, List[ReadingView]]],
    preview: Preview,
) -> Optional[List[ReadingView]]:
    """Layer 1-3 multi-document degradation (§6.1 multi-document selection)."""
    order = [(source_id, list(candidates), 0)
             for source_id, candidates in per_document_candidates]
    order = [item for item in order if item[1]]
    if not order:
        return None
    while True:
        current = [candidates[index] for _, candidates, index in order]
        if preview(_combine_views(current)):
            return current
        target = None
        for position, (_, candidates, index) in enumerate(order):
            if index + 1 >= len(candidates):
                continue
            if (target is None
                    or candidates[index].char_size
                    > order[target][1][order[target][2]].char_size):
                target = position
        if target is None:
            return None
        order[target] = (order[target][0], order[target][1], order[target][2] + 1)



def _focus_window(block: Tuple[int, int], focus: Tuple[int, int],
                  fraction: float) -> Tuple[int, int]:
    b0, b1 = block
    f0, f1 = focus
    return (f0 - int((f0 - b0) * fraction),
            f1 + int((b1 - f1) * fraction))



def select_focus_windows(
    documents: Dict[str, Document],
    focuses: Sequence[Focus],
    blocks_by_source: Mapping[str, Sequence[Tuple[int, int]]],
    preview: Preview,
) -> ReadPlan:
    """Layer 4: shrink around hits, preserving every focus (§6.1 layer 4)."""
    block_of: Dict[Tuple[str, Tuple[int, int]], Tuple[int, int]] = {}
    for focus in focuses:
        blocks = blocks_by_source.get(focus.source_id) or []
        for span in focus.spans:
            block = _block_of(blocks, span)
            if block is None:
                block = (0, len(documents[focus.source_id].text))
            block_of[(focus.source_id, tuple(span))] = block

    delivered: List[Tuple[int, int]] = []
    for fraction in (0.5, 0.25, 0.125, 0.0):
        ranges: List[Tuple[int, int]] = []
        for focus in focuses:
            for span in focus.spans:
                block = block_of[(focus.source_id, tuple(span))]
                ranges.append(_focus_window(block, span, fraction))
        delivered = merge_ranges(ranges)
        views = _views_for_ranges(documents, delivered, focuses)
        if preview(_combine_views(views)):
            return _window_plan(documents, focuses, views, block_of, delivered)

    # Whole-focus fallback in requested-path then character order.
    return _select_minimal_focuses(documents, focuses, preview, block_of)



def _views_for_ranges(documents: Dict[str, Document],
                      ranges: Sequence[Tuple[int, int]],
                      focuses: Sequence[Focus]) -> List[ReadingView]:
    views: List[ReadingView] = []
    for source_id, document in documents.items():
        spans = [(max(0, s), min(len(document.text), e)) for s, e in ranges
                 if e > 0 and s < len(document.text)]
        spans = merge_ranges([(s, e) for s, e in spans if s < e])
        if not spans:
            continue
        refs: List[Dict[str, str]] = []
        for focus in focuses:
            if focus.source_id == source_id:
                refs.extend(focus.citation_refs)
        views.append(_blocks_view(
            document, spans, coverage="ranges_partial",
            selected_level="range", origin="plain_range",
            covered=[span for focus in focuses if focus.source_id == source_id
                     for span in focus.spans],
            citation_refs=refs,
        ))
    return views



def _dedupe_ordered(ranges: Iterable[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """Drop exact duplicates without reordering or merging."""
    seen: set = set()
    out: List[Tuple[int, int]] = []
    for start, end in ranges:
        key = (int(start), int(end))
        if key in seen or key[0] >= key[1]:
            continue
        seen.add(key)
        out.append(key)
    return out



def _pending_by_source(
    focuses: Sequence[Focus],
    views: Sequence[ReadingView],
    block_of: Mapping[Tuple[str, Tuple[int, int]], Tuple[int, int]],
    delivered: Sequence[Tuple[int, int]],
) -> Dict[str, List[Tuple[int, int]]]:
    """Unread targets by source: uncovered focuses, then tails, then heads."""
    result: Dict[str, List[Tuple[int, int]]] = {}
    for focus in focuses:
        for span in focus.spans:
            if not _fully_covered(span, delivered):
                result.setdefault(focus.source_id, []).append(tuple(span))
    # Tails/heads are relative to each selected block, not to the delivered
    # window itself, so the unread context around a window is recovered.
    blocks_by_source: Dict[str, List[Tuple[int, int]]] = {}
    for (source_id, _), block in block_of.items():
        if block not in blocks_by_source.get(source_id, []):
            blocks_by_source.setdefault(source_id, []).append(tuple(block))
    tails: Dict[str, List[Tuple[int, int]]] = {}
    heads: Dict[str, List[Tuple[int, int]]] = {}
    for source_id, blocks in blocks_by_source.items():
        for block in blocks:
            for da, db in delivered:
                if da >= block[1] or db <= block[0]:
                    continue
                da, db = max(da, block[0]), min(db, block[1])
                if db < block[1]:
                    tails.setdefault(source_id, []).append((db, block[1]))
                if da > block[0]:
                    heads.setdefault(source_id, []).append((block[0], da))
    for source_id in tails:
        result.setdefault(source_id, []).extend(tails[source_id])
    for source_id in heads:
        result.setdefault(source_id, []).extend(heads[source_id])
    return {key: _dedupe_ordered(value) for key, value in result.items()}



def _attach_cursor(plan: ReadPlan, documents: Mapping[str, Document],
                   views: Sequence[ReadingView],
                   ranges_by_source: Mapping[str, Sequence[Tuple[int, int]]]) -> ReadPlan:
    plan.pending_ranges = _dedupe_ordered(
        [span for ranges in ranges_by_source.values() for span in ranges])
    if plan.pending_ranges:
        token = _new_cursor_token()
        plan.next_cursor = token
        plan.cursor_token = token
        plan.cursor_queue = build_cursor_queue(documents, views, ranges_by_source)
    return plan



def _window_plan(documents: Dict[str, Document], focuses: Sequence[Focus],
                 views: List[ReadingView],
                 block_of: Mapping[Tuple[str, Tuple[int, int]], Tuple[int, int]],
                 delivered: Sequence[Tuple[int, int]]) -> ReadPlan:
    plan = ReadPlan(views=views)
    plan.covered_focuses = [tuple(s) for focus in focuses for s in focus.spans]
    plan.pending_focuses = [
        focus for focus in focuses
        if any(not _fully_covered(span, delivered) for span in focus.spans)
    ]
    ranges_by_source = _pending_by_source(focuses, views, block_of, delivered)
    return _attach_cursor(plan, documents, views, ranges_by_source)



def _select_minimal_focuses(
    documents: Dict[str, Document],
    focuses: Sequence[Focus],
    preview: Preview,
    block_of: Mapping[Tuple[str, Tuple[int, int]], Tuple[int, int]],
) -> ReadPlan:
    plan = ReadPlan()
    kept: List[Tuple[ReadingView, Tuple[int, int]]] = []
    for focus in focuses:
        for span in focus.spans:
            document = documents[focus.source_id]
            view = _blocks_view(
                document, [span], coverage="ranges_partial",
                selected_level="range", origin="plain_range",
                covered=[span], citation_refs=focus.citation_refs,
            )
            candidate = _combine_views([v for v, _ in kept] + [view])
            if preview(candidate):
                kept.append((view, tuple(span)))
            else:
                plan.pending_focuses.append(focus)
                break
    if kept:
        plan.views = _merge_views_by_source([v for v, _ in kept], documents)
        plan.covered_focuses = [tuple(s) for _, s in kept]
        delivered = [tuple(s) for _, s in kept]
        ranges_by_source = _pending_by_source(focuses, plan.views, block_of, delivered)
        return _attach_cursor(plan, documents, plan.views, ranges_by_source)

    # Even a full focus does not fit: take a prefix of the first focus.
    first_focus = focuses[0]
    first_span = first_focus.spans[0]
    document = documents[first_focus.source_id]
    length = first_span[1] - first_span[0]
    prefix = length // 2
    while prefix > 0:
        span = (first_span[0], first_span[0] + prefix)
        view = _blocks_view(document, [span], coverage="ranges_partial",
                            selected_level="range", origin="plain_range",
                            covered=[span], citation_refs=first_focus.citation_refs)
        if preview(view):
            plan.views = [view]
            plan.covered_focuses = [tuple(span)]
            plan.pending_focuses = [first_focus]
            ranges_by_source = {first_focus.source_id: [(span[1], first_span[1])]}
            return _attach_cursor(plan, documents, [view], ranges_by_source)
        prefix //= 2
    plan.is_budget_limited = True
    plan.reason = "budget_limited"
    plan.pending_focuses = list(focuses)
    return plan



def _merge_views_by_source(views: Sequence[ReadingView],
                           documents: Dict[str, Document]) -> List[ReadingView]:
    by_source: Dict[str, List[ReadingView]] = {}
    for view in views:
        by_source.setdefault(view.source_id, []).append(view)
    result: List[ReadingView] = []
    for source_id, group in by_source.items():
        document = documents[source_id]
        spans = merge_ranges([span for view in group for span in view.spans])
        refs: List[Dict[str, str]] = []
        for view in group:
            refs.extend(view.citation_refs)
        result.append(_blocks_view(
            document, spans, coverage="ranges_partial", selected_level="range",
            origin="plain_range",
            covered=[tuple(s) for view in group for s in view.covered_focuses],
            citation_refs=refs,
        ))
    return result


# ──────────────────────────────────────────────────────────────
#  Cursor continuation (layer 5)
# ──────────────────────────────────────────────────────────────



def _is_reading_cursor(cursor: Optional[str]) -> bool:
    return isinstance(cursor, str) and cursor.startswith("reading-v1:")



def _new_cursor_token() -> str:
    return "reading-v1:" + secrets.token_hex(16)



def build_cursor_queue(
    documents: Mapping[str, Document],
    views: Sequence[ReadingView],
    ranges_by_source: Mapping[str, Sequence[Tuple[int, int]]],
) -> List[Dict[str, Any]]:
    """Build query-internal cursor queue entries (object key + full text)."""
    refs_by_source: Dict[str, List[Dict[str, str]]] = {}
    for view in views:
        refs_by_source.setdefault(view.source_id, []).extend(view.citation_refs)
    queue: List[Dict[str, Any]] = []
    for source_id, ranges in ranges_by_source.items():
        document = documents.get(source_id)
        if document is None:
            continue
        for start, end in ranges:
            if start >= end:
                continue
            queue.append({
                "object_key": list(document.object_key),
                "source_id": source_id,
                "article_path": document.article_path,
                "start": start,
                "end": end,
                "text": document.text,
                "citation_refs": list(refs_by_source.get(source_id) or []),
            })
    return queue



def plan_next_ranges(queue: Sequence[Dict[str, Any]], preview: Preview) -> ReadPlan:
    """Layer 5: consume the head of the pending queue (§6.1 layer 5).

    One call delivers the whole head range or a halving prefix of it, then
    returns a fresh cursor for the remaining queue.  It never walks the whole
    queue in one shot, so a later continuation resumes exactly where this one
    stopped.
    """
    plan = ReadPlan()
    if not queue:
        return plan
    item = dict(queue[0])
    start, end = int(item["start"]), int(item["end"])
    if start >= end:
        remaining = [dict(other) for other in queue[1:]]
        plan.pending_ranges = [(int(i["start"]), int(i["end"])) for i in remaining]
        if remaining:
            token = _new_cursor_token()
            plan.next_cursor = token
            plan.cursor_token = token
            plan.cursor_queue = remaining
        return plan

    length = end - start
    half = length
    chosen: Optional[Tuple[int, int]] = None
    while half > 0:
        span = (start, start + half)
        probe = _range_probe_view(item, span)
        if preview(probe):
            chosen = span
            break
        half //= 2
    if chosen is None:
        plan.is_budget_limited = True
        plan.reason = "budget_limited"
        plan.pending_ranges = [(int(i["start"]), int(i["end"])) for i in queue]
        return plan

    plan.views = [_range_probe_view(item, chosen)]
    plan.covered_focuses = [tuple(chosen)]
    remaining: List[Dict[str, Any]] = []
    if chosen[1] < end:
        remaining.append({**item, "start": chosen[1]})
    remaining.extend(dict(other) for other in queue[1:])
    plan.pending_ranges = [(int(i["start"]), int(i["end"])) for i in remaining]
    if remaining:
        token = _new_cursor_token()
        plan.next_cursor = token
        plan.cursor_token = token
        plan.cursor_queue = [dict(i) for i in remaining]
    return plan



def _range_probe_view(item: Dict[str, Any], span: Tuple[int, int]) -> ReadingView:
    text = item.get("text") or ""
    return ReadingView(
        object_key=tuple(item.get("object_key") or ()),
        source_id=item.get("source_id") or "",
        article_path=item.get("article_path") or "",
        spans=[tuple(span)],
        blocks=[{"start": span[0], "end": span[1], "body": text[span[0]:span[1]]}],
        citation_refs=list(item.get("citation_refs") or []),
        coverage="ranges_partial", structure_origin="plain_range",
        selected_level="range",
    )



def _heading_path(sections: Sequence[Section],
                  spans: Sequence[Tuple[int, int]]) -> List[Dict[str, Any]]:
    """Outermost-first title path for the selected spans.

    Each span keeps the full ancestor chain of its smallest enclosing section,
    so the reader can include the necessary heading context without expanding
    the selected range into the whole parent section.
    """
    path: List[Dict[str, Any]] = []
    seen: set = set()
    for span in spans:
        if span[1] <= span[0]:
            continue
        section = _section_for(sections, span[0], span[1])
        for node in _ancestors(sections, section):
            key = (node.id, node.level, node.title, node.start, node.end)
            if key in seen:
                continue
            seen.add(key)
            path.append({
                "level": node.level,
                "title": node.title,
                "start": node.start,
                "end": node.end,
            })
    return path



def _navigation_payload(document: Document) -> List[Dict[str, Any]]:
    """Return locatable headings/blocks for a source that is too large to open."""
    units = identify_units(document.text) if identify_units is not None else []
    return [{
        "source_id": document.source_id,
        "article_path": document.article_path,
        "blocks": [{"start": int(unit.start), "end": int(unit.end)}
                   for unit in units],
    }]



def _open_source_plan(context: Dict[str, Any], document: Document,
                      focus: Focus, preview: Preview) -> ReadPlan:
    """Plan an "open this source/article" request.

    A whole small unit is delivered complete.  A source that does not fit the
    actual next request is *not* silently shrunk: it returns its locatable
    blocks/headings so the query model can choose a range.
    """
    full = _full_candidate(document, [focus])
    # A source index is navigation, not an implicit request for every page.
    # Explicit ranges and published article units still deliver their bodies.
    if not focus.requested_path.startswith("sources/source-") and preview(full):
        return ReadPlan(views=[full], reason="")

    digest = load_digest(context, document.source_id)
    sections = load_saved_structure(context, document, digest)
    navigation = _navigation_payload(document)
    for item in navigation:
        item["requested_path"] = focus.requested_path
        item["headings"] = [
            {"level": section.level, "title": section.title,
             "start": section.start, "end": section.end}
            for section in sections if section.id != -1
        ]
    return ReadPlan(navigation=navigation, reason="navigation_required")



def _located_plan(document: Document, focuses: Sequence[Focus],
                  blocks: Sequence[Tuple[int, int]],
                  sections: Sequence[Section], preview: Preview) -> ReadPlan:
    """Plan one or more located fragments without upgrading to the whole unit.

    The selected range is the complete block(s) that contain the located spans,
    plus the title path.  If that batch is too large, trailing complete blocks
    are left for cursor continuation; a single block that still does not fit is
    reported as too large instead of being cut mid-sentence.
    """
    refs: List[Dict[str, str]] = []
    requested_spans: List[Tuple[int, int]] = []
    for focus in focuses:
        refs.extend(focus.citation_refs)
        requested_spans.extend(tuple(span) for span in focus.spans)
    requested_spans = merge_ranges(requested_spans)

    if blocks:
        selected: List[Tuple[int, int]] = []
        for span in requested_spans:
            block = _block_of(blocks, span)
            selected.append(tuple(block) if block is not None else tuple(span))
        selected = merge_ranges(selected)
    else:
        selected = list(requested_spans)

    def _view(spans: Sequence[Tuple[int, int]]) -> ReadingView:
        view = _blocks_view(
            document, spans, coverage="ranges_partial",
            selected_level="blocks" if blocks else "range",
            origin="saved_metadata" if blocks else "plain_range",
            covered=requested_spans, citation_refs=refs,
        )
        view.title_path = _heading_path(sections, view.spans)
        return view

    candidate = _view(selected)
    if preview(candidate):
        plan = ReadPlan(views=[candidate])
        plan.covered_focuses = [tuple(span) for span in requested_spans]
        plan.pending_focuses = [
            focus for focus in focuses
            if any(not _fully_covered(span, candidate.spans) for span in focus.spans)
        ]
        return plan

    accepted: List[Tuple[int, int]] = []
    remaining = list(selected)
    pending_blocks: List[Tuple[int, int]] = []
    while remaining:
        candidate = _view(accepted + remaining)
        if preview(candidate):
            accepted = list(candidate.spans)
            break
        pending_blocks.insert(0, remaining.pop())
    if accepted:
        view = _view(accepted)
        plan = ReadPlan(views=[view])
        plan.covered_focuses = [
            tuple(span) for span in requested_spans
            if _fully_covered(span, accepted)
        ]
        plan.pending_focuses = [
            focus for focus in focuses
            if any(not _fully_covered(span, accepted) for span in focus.spans)
        ]
        return _attach_cursor(plan, {document.source_id: document}, [view],
                              {document.source_id: pending_blocks})

    return ReadPlan(
        is_budget_limited=True, reason="selected_range_too_large",
        pending_focuses=list(focuses), pending_ranges=list(selected),
    )



def _merge_read_plans(plans: Sequence[ReadPlan],
                      documents: Mapping[str, Document]) -> ReadPlan:
    """Merge per-document plans into one delivery.

    Independent source ranges stay independent; overlapping ranges of the same
    object are deduplicated by the same merge used for view spans.
    """
    views: List[ReadingView] = []
    navigation: List[Dict[str, Any]] = []
    covered: List[Tuple[int, int]] = []
    pending_focuses: List[Focus] = []
    pending_ranges: List[Tuple[int, int]] = []
    cursor_queue: List[Dict[str, Any]] = []
    reasons: List[str] = []
    is_budget_limited = False
    for plan in plans:
        views.extend(plan.views)
        navigation.extend(plan.navigation)
        covered.extend(tuple(span) for span in plan.covered_focuses)
        pending_focuses.extend(plan.pending_focuses)
        pending_ranges.extend(tuple(span) for span in plan.pending_ranges)
        cursor_queue.extend(plan.cursor_queue)
        if plan.reason:
            reasons.append(plan.reason)
        is_budget_limited = is_budget_limited or plan.is_budget_limited

    merged_views: List[ReadingView] = []
    if views:
        by_source: Dict[str, List[ReadingView]] = {}
        for view in views:
            by_source.setdefault(view.source_id, []).append(view)
        for source_id, group in by_source.items():
            for merged in _merge_views_by_source(group, documents):
                title_path: List[Dict[str, Any]] = []
                seen: set = set()
                for view in group:
                    for item in view.title_path:
                        key = tuple(sorted(item.items()))
                        if key not in seen:
                            seen.add(key)
                            title_path.append(item)
                merged.title_path = title_path
                merged_views.append(merged)

    # Deduplicate pending focuses by (requested_path, span).
    focus_seen: set = set()
    deduped_focuses: List[Focus] = []
    for focus in pending_focuses:
        for span in focus.spans:
            key = (focus.requested_path, tuple(span))
            if key in focus_seen:
                continue
            focus_seen.add(key)
            deduped_focuses.append(focus)
            break

    plan = ReadPlan(
        views=merged_views,
        covered_focuses=_dedupe_ordered(covered),
        pending_focuses=deduped_focuses,
        pending_ranges=_dedupe_ordered(pending_ranges),
        navigation=navigation,
        is_budget_limited=is_budget_limited,
        reason="; ".join(dict.fromkeys(reasons)),
    )
    if cursor_queue:
        token = _new_cursor_token()
        plan.next_cursor = token
        plan.cursor_token = token
        plan.cursor_queue = cursor_queue
    return plan



def plan_read(
    state: Dict[str, Any],
    corpus: Any,
    requested_paths: Sequence[str],
    cursor: Optional[str],
    preview: Preview,
    *,
    explicit_ranges: Optional[Mapping[str, Tuple[int, int]]] = None,
) -> ReadPlan:
    """Plan one ``wiki_read`` delivery; never mutates query state.

    ``plan_read`` is the only place that chooses or shrinks the delivered
    range.  Downstream readers, body builders and citation registration never
    expand the selected range on their own.
    """
    if preview is None:
        # A legacy model without a budget meter has no budget voice, so every
        # candidate is admissible and no continuation is budget-limited.
        preview = lambda view: True
    context = _read_context(state)
    if _is_reading_cursor(cursor):
        cursors = state.setdefault("read_cursors", {})
        queue = cursors.get(cursor)
        if not queue:
            return ReadPlan(is_budget_limited=True, reason="invalid_read_cursor")
        return plan_next_ranges(queue, preview)

    documents, focuses = resolve_requested_sources(
        context, corpus, requested_paths, expand_units=not bool(explicit_ranges))
    if not documents or not focuses:
        return ReadPlan(reason="no_source_focus")

    page_paths = {f.requested_path for f in focuses if not f.requested_path.startswith(
        ("sources/", "source-articles/", "source-evidence/"))}
    if explicit_ranges:
        for focus in focuses:
            if focus.requested_path in explicit_ranges:
                start, end = explicit_ranges[focus.requested_path]
                if (focus.requested_path in page_paths
                        and not _fully_covered((start, end), focus.spans)):
                    continue
                if 0 <= start < end <= len(documents[focus.source_id].text):
                    if not _fully_covered((start, end), focus.spans):
                        focus.citation_refs = []
                    focus.spans = [(start, end)]
                    focus.open_request = False

    by_source: Dict[str, List[Focus]] = {}
    for focus in focuses:
        by_source.setdefault(focus.source_id, []).append(focus)

    plans: List[ReadPlan] = []
    for source_id, doc_focuses in by_source.items():
        document = documents[source_id]
        open_focuses = [f for f in doc_focuses if f.open_request]
        located_focuses = [f for f in doc_focuses if not f.open_request]
        for focus in open_focuses:
            plans.append(_open_source_plan(context, document, focus, preview))
        if located_focuses:
            exact = [f for f in located_focuses if not f.citation_refs
                     or f.requested_path in (explicit_ranges or {})]
            ordinary = [f for f in located_focuses if f not in exact]
            # Exact units, mapped passages and explicit ranges do not expand
            # again through a surrounding structural block.
            blocks = merge_ranges(document_blocks(document, ordinary) +
                                  [span for f in exact for span in f.spans])
            digest = load_digest(context, document.source_id)
            sections = load_saved_structure(context, document, digest)
            plans.append(_located_plan(document, located_focuses, blocks,
                                       sections, preview))
    return _merge_read_plans(plans, documents)


__all__ = [
    "Document", "Focus", "Section", "ReadingView", "ReadPlan", "Preview",
    "build_source_map", "load_document", "load_digest",
    "resolve_focus", "resolve_requested_sources", "load_saved_structure",
    "map_chunk", "project", "merge_ranges", "unique_offset", "subtract_ranges",
    "document_blocks", "build_full_then_structured_candidates",
    "select_combination", "select_focus_windows", "plan_next_ranges",
    "plan_read", "build_cursor_queue", "reading_view_payload",
]


def referenced_unread_workbook(state: Dict[str, Any], corpus: Any,
                               draft: str) -> Optional[str]:
    """Select only a searched workbook explicitly named in a refusal draft.

    A saved table's ambiguous interpretation does not make its original source
    unreadable. Do not infer a new source, revisit delivered workbooks, or pick
    an arbitrary old frontier after a refined search.
    """
    for path in state.get('frontier_paths') or []:
        if path in (state.get('read_pages') or set()):
            continue
        material = resolve_reading_material(state, corpus, str(path))
        if material is None or material.get('unit', {}).get('kind') != 'workbook':
            continue
        if str(material.get('source_id') or '') in (state.get('delivered_workbooks') or set()):
            continue
        name = str(material.get('unit', {}).get('name') or '')
        aliases = [name] if name else []
        aliases.extend(str(sheet.get('name') or '') for sheet in material.get('unit', {}).get('sheets') or [] if sheet.get('name'))
        normalized_draft = re.sub(r'\s+', '', draft)
        if any(alias and re.sub(r'\s+', '', alias) in normalized_draft for alias in aliases):
            return str(path)
    return None
