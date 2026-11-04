"""Deterministic "view pages" derived from one logical table.

A table is a set of ``(dimension…, fact…)`` tuples, so the same grid can be
*read* as several short articles: a menu can be organised as "what do we eat
each day" or "what is in each dish".  This module derives those angles from
structure alone — no model call, no aggregation, no inference — and renders
each one as an ordinary wiki page, so the reader's existing furniture
(in-page table of contents, related pages, backlinks, references, blue links)
applies unchanged.

Structural signals used to pick an angle (deliberately table-agnostic):

* a column whose values repeat and stay few is a **dimension**
  (交易所 / 日期 / 菜品 / 器件类型) → group the rows by it;
* a column whose values are nearly one-per-row is an **item** column
  (品种 / 产品型号 / 技术指标) → one entry per value;
* several columns sharing one header name are a **family of entities**
  (a merged header flattened into repeated labels) → read the table
  transposed, one section per entity.

Everything here is compile-time and cheap: the output is a handful of small
markdown pages.  Pages are derived views, never the data source, and they say
so on the page.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from jonex_wiki.domain.engine.models.compiler_state import (
    PageIndexEntry,
    WikiPageSnapshot,
)

# ──────────────────────────────────────────────────────────────
#  Limits — "keep it light" in numbers
# ──────────────────────────────────────────────────────────────

VIEW_DIR = "table-views"
MAX_VIEWS_PER_TABLE = 3
MAX_DIMENSION_KEYS = 30       # 维度列的分组数上限
MAX_ITEM_KEYS = 200           # 条目列的上限（一行一个条目）
MIN_ROWS = 5                  # 少于这么多行不值得拆角度
TRANSPOSE_MAX_ROWS = 12       # 转置只对小表成立
MAX_SECTIONS = 40             # 每页最多铺多少节（维度轴）
MAX_ENTITY_SECTIONS = 160     # 条目轴（一条一项）可以长一些，本身就是清单
MAX_ITEMS_PER_SECTION = 60    # 每节最多列多少行
MAX_ROWS_PER_VIEW = 400       # 每页最多搬运多少行
NESTING_MIN_ROWS = 8          # 组内至少这么多行才再分一层
NESTING_MAX_KEYS = 8

_GRID_LINE = re.compile(r"^\s*\|")
_SEPARATOR_CELL = re.compile(r"^:?-{2,}:?$")
_ASCII_IDENTIFIER = re.compile(r"^[A-Za-z0-9\-_. /]+$")
_LABEL_SPLIT = re.compile(r"\s*(?:\||\u2013|\u2014|\s-\s|-(?=\s))\s*")


# ──────────────────────────────────────────────────────────────
#  Grid: parse the markdown preview into header + rows
# ──────────────────────────────────────────────────────────────

def parse_grid(text: str) -> Optional[Tuple[List[str], List[List[str]]]]:
    """Parse a markdown pipe grid into ``(header, rows)``.

    Returns ``None`` when the text holds no usable grid (fewer than two columns
    or no data row).  A ``---`` separator row is skipped rather than treated as
    the header.
    """
    header: Optional[List[str]] = None
    rows: List[List[str]] = []
    for line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not _GRID_LINE.match(line):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if any(cells) and all(_SEPARATOR_CELL.match(cell) for cell in cells if cell):
            continue
        if header is None:
            header = cells
            continue
        rows.append(cells)
    if header is None or len(header) < 2 or not rows:
        return None
    width = len(header)
    header = [cell or "第 {} 列".format(index + 1) for index, cell in enumerate(header)]
    normalized = [list(row[:width]) + [""] * max(0, width - len(row)) for row in rows]
    if not any(any(cell for cell in row) for row in normalized):
        return None
    return header, normalized


def fill_down(rows: Sequence[Sequence[str]]) -> List[List[str]]:
    """Normalize text without inventing values for ordinary blank cells.

    Merge inheritance belongs to the source-grid projection, where explicit
    merge bounds and anchors are available. A Markdown blank alone is not
    evidence that the preceding value applies to this row.
    """
    return [[str(value or "").strip() for value in row] for row in rows]


# ──────────────────────────────────────────────────────────────
#  Angles
# ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TableAxis:
    """One organising logic: how this table is being read."""

    kind: str                              # "group" | "columns"
    label: str                             # 列名（转置轴则是一族列名）
    keys: Tuple[str, ...] = ()             # 分组键 / 实体列名，按出现顺序
    column: Optional[int] = None           # kind == "group"
    families: Tuple[Tuple[str, Tuple[int, ...]], ...] = ()   # kind == "columns"
    entity: bool = False                   # 键是一行一个的条目，而不是类别
    score: int = 0


@dataclass(frozen=True)
class ViewItem:
    """One row rendered as a bullet: bold label plus the remaining ``列名：值``."""

    facts: str = ""
    label: str = ""
    code: str = ""                          # 紧跟其后的短标识（如品种代码）
    separator: str = " —— "


@dataclass(frozen=True)
class ViewGroup:
    """An optional second level inside a section (交易所 → 期货 / 期权)."""

    title: str
    items: Tuple[ViewItem, ...]


@dataclass(frozen=True)
class ViewSection:
    title: str
    count: int
    groups: Tuple[ViewGroup, ...]
    omitted: int = 0


@dataclass(frozen=True)
class TableView:
    """One page: an axis plus the prose derived from it."""

    axis: TableAxis
    ordinal: int
    title: str
    lead: str
    sections: Tuple[ViewSection, ...] = ()
    total_rows: int = 0
    omitted_sections: int = 0

    @property
    def slug(self) -> str:
        return "view-{}".format(self.ordinal)


def _family_map(header: Sequence[str]) -> Dict[str, List[int]]:
    families: Dict[str, List[int]] = {}
    for index, name in enumerate(header):
        key = (name or "").strip()
        if key:
            families.setdefault(key, []).append(index)
    return {name: indexes for name, indexes in families.items() if len(indexes) >= 2}


def derive_axes(
    header: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    max_axes: int = MAX_VIEWS_PER_TABLE,
) -> List[TableAxis]:
    """Pick the organising logics this grid supports, best first."""
    if len(rows) < MIN_ROWS or len(header) < 2:
        return []
    filled = fill_down(rows)
    axes: List[TableAxis] = []

    # ① Transpose: on a small table whose first column is one item per row
    #    (技术指标 / 项目 / 字段), the remaining columns are entities.
    first_values = [row[0] for row in filled if row[0]]
    if (len(filled) <= TRANSPOSE_MAX_ROWS and len(header) > 2
            and len(set(first_values)) == len(filled)):
        names = tuple(dict.fromkeys(name for name in header[1:] if name))
        if names:
            axes.append(TableAxis(
                kind="columns",
                label="、".join(names),
                keys=names,
                families=tuple(sorted(_family_map(header).items())),
                score=3,
            ))

    # ② Dimensions (values repeat, few of them) then ③ items (one per row).
    #    A column that shares its header with another column is a *sub-column*
    #    of a merged header (COG / X7R under one 瓷介电容器 label): its values
    #    read as attributes, not as a way to classify the table.
    families = _family_map(header)
    duplicated = {index for indexes in families.values() for index in indexes}
    for index, name in enumerate(header):
        if index in duplicated:
            continue
        values = [row[index] for row in filled if row[index]]
        if len(values) < 2:
            continue
        distinct = list(dict.fromkeys(values))
        repeats = len(values) - len(distinct)
        label = name or "第 {} 列".format(index + 1)
        if repeats > 0 and len(distinct) <= MAX_DIMENSION_KEYS:
            axes.append(TableAxis(kind="group", label=label, keys=tuple(distinct),
                                  column=index, entity=False, score=2))
        elif len(distinct) / len(values) >= 0.8 and len(distinct) <= MAX_ITEM_KEYS:
            axes.append(TableAxis(kind="group", label=label, keys=tuple(distinct),
                                  column=index, entity=True, score=1))

    # A transposed table already declares its entities in the header, so its
    # remaining columns are values, not alternative groupings: keep the
    # transpose and the row-label column only ("每种器件有什么" / "每项指标是多少").
    if any(axis.kind == "columns" for axis in axes):
        axes = [axis for axis in axes
                if axis.kind == "columns" or axis.column == 0]

    axes.sort(key=lambda axis: -axis.score)
    picked: List[TableAxis] = []
    seen: set = set()
    for axis in axes:
        if axis.label in seen:
            continue
        seen.add(axis.label)
        picked.append(axis)
    return picked[:max_axes]


# ──────────────────────────────────────────────────────────────
#  Rendering one angle into prose
# ──────────────────────────────────────────────────────────────

def _nesting_column(header: Sequence[str], axis: TableAxis,
                    group_rows: Sequence[Sequence[str]]) -> Optional[int]:
    """A second level inside a big group (交易所 → 期货 / 期权).

    Only worth it for a group that is long enough and whose candidate column
    actually repeats *inside* the group: otherwise the "second level" is just
    the row identifier (AG / AG6 / AG2604) and nesting it hides the value.
    """
    if len(group_rows) < NESTING_MIN_ROWS:
        return None
    best: Optional[Tuple[int, int]] = None
    for index in range(len(header)):
        if index == axis.column:
            continue
        values = [row[index] for row in group_rows if row[index]]
        distinct = len(set(values))
        if 1 < distinct <= NESTING_MAX_KEYS and distinct < len(values):
            if best is None or distinct < best[1]:
                best = (index, distinct)
    return best[0] if best else None


def _label_column(header: Sequence[str], axis: TableAxis,
                  group_rows: Sequence[Sequence[str]], nest: Optional[int],
                  avoid: frozenset) -> Optional[int]:
    """Leftmost column that names the row inside this group.

    Columns that classify rows elsewhere in this table (the dimension columns:
    交易所 / 品种类型) are skipped, as is the identifier column that only codes
    the name (AG / CU): "**白银** —— 代码及合约：AG" reads better than
    "**期货** —— 品种：白银".
    """
    fallback: Optional[int] = None
    for index, name in enumerate(header):
        if index == axis.column or index == nest or not name or index in avoid:
            continue
        values = {row[index] for row in group_rows if row[index]}
        if len(values) < 2:
            continue
        if fallback is None:
            fallback = index
        if not _is_short_code([row[index] for row in group_rows if row[index]]):
            return index
    return fallback


def _facts(header: Sequence[str], row: Sequence[str], skip: set,
           families: Dict[str, List[int]]) -> str:
    """Render the remaining cells as ``列名：值``; same-named columns merge."""
    parts: List[str] = []
    used: set = set()
    for name, indexes in families.items():
        if all(index in skip for index in indexes):
            continue
        values = [row[index] for index in indexes if row[index] and index not in skip]
        if values:
            parts.append("{}：{}".format(name, " / ".join(dict.fromkeys(values))))
            used.update(indexes)
    for index, name in enumerate(header):
        if index in skip or index in used or not row[index]:
            continue
        parts.append("{}：{}".format(name or "第 {} 列".format(index + 1), row[index]))
    return "；".join(parts)


def _is_short_code(values: Sequence[str]) -> bool:
    return bool(values) and all(
        _ASCII_IDENTIFIER.match(value) and len(value) <= 16 for value in values)


def _items_for(header: Sequence[str], axis: TableAxis, rows: Sequence[Sequence[str]],
               families: Dict[str, List[int]], avoid: frozenset,
               nest: Optional[int] = None) -> Tuple[ViewItem, ...]:
    """Render the rows of one section (or one nested group) as bullets."""
    assert axis.column is not None
    label_index = _label_column(header, axis, rows, nest, avoid)
    items: List[ViewItem] = []
    for row in rows[:MAX_ITEMS_PER_SECTION]:
        skip = {axis.column} | ({nest} if nest is not None else set())
        if label_index is None or not row[label_index]:
            items.append(ViewItem(facts=_facts(header, row, skip, families)))
            continue
        skip.add(label_index)
        code = ""
        next_index = label_index + 1
        if next_index < len(header) and next_index not in skip:
            column_values = [other[next_index] for other in rows if other[next_index]]
            if _is_short_code(column_values):
                code = row[next_index]
                skip.add(next_index)
        items.append(ViewItem(label=row[label_index], code=code,
                              facts=_facts(header, row, skip, families)))
    return tuple(items)


def _group_sections(header: Sequence[str], axis: TableAxis,
                    rows: Sequence[Sequence[str]], families: Dict[str, List[int]],
                    avoid: frozenset) -> List[ViewSection]:
    assert axis.column is not None
    sections: List[ViewSection] = []
    budget = MAX_ROWS_PER_VIEW
    for key in axis.keys:
        if budget <= 0:
            break
        bucket = [row for row in rows if row[axis.column] == key]
        if not bucket:
            continue
        nest = _nesting_column(header, axis, bucket)
        groups: List[ViewGroup] = []
        if nest is None:
            groups.append(ViewGroup(title="", items=_items_for(header, axis, bucket,
                                                               families, avoid)))
        else:
            for sub in dict.fromkeys(row[nest] for row in bucket if row[nest]):
                subset = [row for row in bucket if row[nest] == sub]
                groups.append(ViewGroup(title=sub, items=_items_for(
                    header, axis, subset, families, avoid, nest)))
        shown = sum(len(group.items) for group in groups)
        budget -= shown
        sections.append(ViewSection(title=key, count=len(bucket), groups=tuple(groups),
                                    omitted=max(0, len(bucket) - shown)))
    return sections


def _entity_sections(header: Sequence[str], axis: TableAxis,
                     rows: Sequence[Sequence[str]]) -> List[ViewSection]:
    """Transposed reading: one section per entity column, families merged."""
    assert axis.kind == "columns"
    family_of = dict(axis.families)
    sections: List[ViewSection] = []
    emitted: set = set()
    for column in range(1, len(header)):
        name = header[column] or "第 {} 列".format(column + 1)
        if name in emitted:
            continue
        emitted.add(name)
        mates = family_of.get(name, (column,))
        items: List[ViewItem] = []
        for row in rows[:MAX_ITEMS_PER_SECTION]:
            values = [row[index] for index in mates if row[index]]
            if not values:
                continue
            items.append(ViewItem(label=row[0], facts=" / ".join(dict.fromkeys(values)),
                                  separator="："))
        if items:
            sections.append(ViewSection(title=name, count=len(items),
                                        groups=(ViewGroup(title="", items=tuple(items)),)))
    return sections


def build_view(axis: TableAxis, ordinal: int, *, header: Sequence[str],
               rows: Sequence[Sequence[str]], table_title: str,
               source_name: str, dimension_columns: Sequence[int] = ()) -> TableView:
    """Render one angle into a page model."""
    filled = fill_down(rows)
    families = _family_map(header)
    avoid = frozenset(dimension_columns)
    if axis.kind == "columns":
        sections = _entity_sections(header, axis, filled)
    else:
        sections = _group_sections(header, axis, filled, families, avoid)
    limit = MAX_ENTITY_SECTIONS if axis.entity else MAX_SECTIONS
    kept = sections[:limit]
    book = short_book_name(source_name, table_title)
    if axis.kind == "columns":
        lead = "把《{}》换一种读法：按「{}」看，每一列是一个对象，列出它的各项取值。".format(book, axis.label)
    elif axis.entity:
        lead = "把《{}》按「{}」重排：一条一项，列出这一项自己的取值。".format(book, axis.label)
    else:
        lead = "把《{}》按「{}」归类：同一类的行放在一起，便于通读和比较。".format(book, axis.label)
    return TableView(
        axis=axis, ordinal=ordinal,
        title="按{}看：{}".format(axis.label, book)[:200],
        lead=lead, sections=tuple(kept),
        total_rows=sum(section.count for section in sections),
        omitted_sections=max(0, len(sections) - len(kept)),
    )


def render_view_content(view: TableView, *, table_path: str, table_title: str,
                        sibling_views: Sequence[Tuple[str, str]] = (),
                        source_path: str = "", source_name: str = "",
                        complete: bool = True) -> str:
    """Render the page body (what the reader shows under ``## Content``)."""
    lines: List[str] = [view.lead, ""]
    if view.sections:
        lines.append("共 {} 组，覆盖 {} 行取值。".format(len(view.sections), view.total_rows))
        lines.append("")
    for section in view.sections:
        heading = "## " + section.title
        if section.count:
            heading += "（{} 项）".format(section.count)
        lines.append(heading)
        lines.append("")
        for group in section.groups:
            if group.title:
                lines.append("### " + group.title)
                lines.append("")
            for item in group.items:
                if item.label:
                    bullet = "- **{}**".format(item.label)
                    if item.code:
                        bullet += "（{}）".format(item.code)
                    if item.facts:
                        bullet += item.separator + item.facts
                else:
                    bullet = "- " + item.facts
                lines.append(bullet.rstrip())
            lines.append("")
        if section.omitted:
            lines.append("（本节还有 {} 项，见原表。）".format(section.omitted))
            lines.append("")
    if view.omitted_sections:
        lines.append("（另有 {} 组未在本页展开，见原表。）".format(view.omitted_sections))
        lines.append("")

    footer: List[str] = []
    others = ["[[{}|{}]]".format(path, title) for path, title in sibling_views if path]
    if others:
        footer.append("换个角度看：" + " · ".join(others) + "。")
    footer.append("[[{}|原表]]".format(table_path))
    if source_path:
        footer.append("[[{}|来源：{}]]".format(source_path, source_name or "source"))
    lines.append("---")
    lines.append("")
    lines.append("本页是《{}》的一个阅读视角，由表结构与取值整理而成，**不是原始数据**；"
                 "引用数值前请回到原表核对。".format(table_title))
    if not complete:
        lines.append("")
        lines.append("（本页只整理了预览范围内的行列，完整表格见原表。）")
    lines.append("")
    lines.append(" ".join(footer))
    return "\n".join(lines).rstrip() + "\n"


# ──────────────────────────────────────────────────────────────
#  Pages
# ──────────────────────────────────────────────────────────────

_EXTENSION = re.compile(r"\.(pdf|xlsx?|xlsm|csv|docx?|pptx?|txt|md|json)$", re.I)
_TRAILING_DATE = re.compile(r"[（(]\s*(?:19|20)\d{2}[-./年]?\d{0,2}[-./月]?\d{0,2}\s*[)）]\s*$")


def short_book_name(source_name: str, fallback: str = "") -> str:
    """A short, human name for the document a table came from.

    Used where a full table title would be unreadable — above all in the view
    page titles ("按交易所看：代理手续费标准").  The reference to the exact
    source stays in the page body and in the frontmatter.
    """
    name = str(source_name or "").strip()
    name = _EXTENSION.sub("", name).strip()
    previous = None
    while previous != name:
        previous = name
        name = _TRAILING_DATE.sub("", name).strip()
    name = name.replace("_", " ").strip(" -—–")
    if not name:
        name = str(fallback or "").strip()
    if len(name) > 40:
        name = name[:39].rstrip("、,， ") + "…"
    return name or "表格"


def view_page_path(table_path: str, ordinal: int) -> str:
    """Canonical wiki path of one view page (derived from the table page path)."""
    stem = str(table_path).rstrip("/").rsplit("/", 1)[-1]
    if stem.endswith(".md"):
        stem = stem[:-3]
    return "{}/{}-view-{}.md".format(VIEW_DIR, stem, ordinal)


def _split_labels(text: str) -> List[str]:
    labels: List[str] = []
    for piece in _LABEL_SPLIT.split(str(text or "")):
        label = piece.strip().strip("|-\u2013\u2014 ").strip()
        if not label or _SEPARATOR_CELL.match(label):
            continue
        if label not in labels:
            labels.append(label)
    return labels


def human_table_title(raw_title: str, fields: Sequence[str] = (),
                      fallback: str = "") -> str:
    """Turn a passage's first line into a readable page title.

    A grid passage usually starts with its header row, which the page used to
    be titled with verbatim (``- A - B - C -``).  Keep the labels — retrieval
    matches on them — but drop the separator soup and the repeats.  A passage
    that already starts with a real title keeps it; the column names are only a
    fallback for an empty or purely structural first line.
    """
    labels = _split_labels(raw_title)
    if not labels:
        for name in fields:
            for label in _split_labels(name):
                if label not in labels:
                    labels.append(label)
    if not labels:
        return (fallback or "表格").strip()[:200]
    title = "、".join(labels)
    if len(title) > 120:
        title = title[:117].rstrip("、") + "…"
    return title[:200]


def build_view_pages(
    *,
    table_path: str,
    table_title: str,
    source_id: str,
    source_sha256: str,
    source_path: str,
    source_name: str = "",
    raw_text: str,
    interpretation_status: str,
    already_taken: Sequence[str] = (),
    preview_truncated: bool = False,
) -> List[Tuple[PageIndexEntry, WikiPageSnapshot, str]]:
    """Build the view pages of one logical table (0..``MAX_VIEWS_PER_TABLE``).

    Only a *confirmed* structure gets views: grouping an unconfirmed grid would
    publish a reading built on a shape nobody verified.
    """
    if str(interpretation_status or "") != "accepted":
        return []
    grid = parse_grid(raw_text)
    if grid is None:
        return []
    header, rows = grid
    axes = derive_axes(header, rows)
    if not axes:
        return []
    dimensions = tuple(sorted({axis.column for axis in axes
                               if axis.column is not None and not axis.entity}))
    views = [build_view(axis, position + 1, header=header, rows=rows,
                        table_title=table_title, source_name=source_name,
                        dimension_columns=dimensions)
             for position, axis in enumerate(axes)]
    paths = {view.ordinal: view_page_path(table_path, view.ordinal) for view in views}
    bounded = preview_truncated or len(rows) >= MAX_ITEMS_PER_SECTION * MAX_SECTIONS
    built: List[Tuple[PageIndexEntry, WikiPageSnapshot, str]] = []
    for view in views:
        page_path = paths[view.ordinal]
        if page_path in already_taken:
            continue
        siblings = [(paths[other.ordinal], other.title) for other in views
                    if other.ordinal != view.ordinal]
        content = render_view_content(
            view, table_path=table_path, table_title=table_title,
            sibling_views=siblings, source_path=source_path, source_name=source_name,
            complete=not bounded)
        entry = PageIndexEntry(
            path=page_path,
            title=view.title,
            aliases=[view.axis.label],
            tags=["table-view", "derived"],
            description=view.lead[:500],
            object_id=None,
            scope={"table_path": table_path, "axis": view.axis.label,
                   "sections": len(view.sections), "rows": view.total_rows},
            fields=[view.axis.label],
            why_visit=("Read this page for a human-readable grouping of {} by 「{}」; it is "
                       "derived from that table, never the table itself."
                       .format(table_path, view.axis.label)),
            limitations=["derived_view", "not_the_data_source"],
            capabilities=[],
            interpretation_status="accepted",
        )
        related = [table_path]
        if source_path:
            related.append(source_path)
        related.extend(path for path, _title in siblings)
        snapshot = WikiPageSnapshot(
            index=entry,
            content=content,
            source_ids=[source_id] if source_id else [],
            related_pages=related,
            links=[{
                "target_id": table_path,
                "why_visit": "Read the complete table and verify any value before citing it.",
                "evidence_refs": ([{"source_id": source_id, "source_sha256": source_sha256}]
                                  if source_id else []),
                "kind": "navigation",
            }],
        )
        built.append((entry, snapshot, content))
    return built


def view_link_line(views: Sequence[Tuple[str, str]]) -> str:
    """The one-line entry point shown on the table page itself."""
    links = ["[[{}|{}]]".format(path, title) for path, title in views if path]
    if not links:
        return ""
    return "换个角度看这张表：" + " · ".join(links) + "。"
