"""Compile-time page graph + link/mention index for the reading API.

The platform used to build "what links here" at request time by scanning the
published ``wiki/*.md`` tree and re-parsing every frontmatter.  This module
moves that work into the compile step so the result becomes part of the
published bundle (``indexes/links.json`` and the ``page_graph`` block of
``query/navigation.json``) and is shared by the standalone service and the
platform integration.

The rendered frontmatter is the engine's own machine-written record (see
``rendering._render_page``): ``links``/``related``/``source_ids`` are
single-line JSON arrays and ``version`` is a bare integer, so they are parsed
with ``json.loads`` exactly like ``page_source_ids_from_markdown`` already
does.  Titles and aliases come from the structured ``page_index`` instead.

Two edge kinds, matching the platform frontend blue-link contract
(``wikiMarkdown.autoLinkMentions``):

- ``link``: a page's ``links[].target_id`` (compile-time navigation edges),
  reversed into in-links.
- ``mention``: the first occurrence of another page's title/alias inside a
  page body (>= 3 chars; ASCII names require word boundaries).

Raw content pages (keep-and-search) never enter the reading tree, so they are
excluded from the backlink index — the same prefixes the platform adapter used
to filter at runtime, now centralized for compile + read reuse.
"""

from __future__ import annotations

import json
import re
from zlib import crc32
from collections import Counter, defaultdict
from typing import Any, Dict, List, Tuple

from jonex_wiki.domain.engine.models.query_models import PageIndexEntry

# Raw content pages produced by the parser/compiler (full-article splits,
# complete-table cells, source-evidence spans).  They are searchable but do not
# appear in the Wiki reading tree, so they are excluded from backlinks.
RAW_PAGE_PREFIXES: Tuple[str, ...] = (
    "source-articles/",
    "raw-tables/",
    "source-evidence/",
)

_MENTION_MIN_LEN = 3

_FIELD_RE = re.compile(r"^(links|related|source_ids|version):\s*(.*?)\s*$", re.MULTILINE)
_ASCII_WORD = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")


def _mention_filter(text: str) -> int:
    """Stable 2048-bit trigram filter: false positives only cause extra reads.

    Every literal match contains every trigram of its name. Store their bits
    so a new title/alias can find possible old pages without opening every
    Markdown file. Exact matching below remains the authority.
    """
    value = 0
    for gram in {text[i:i + 3] for i in range(len(text) - 2)}:
        value |= 1 << (crc32(gram.encode("utf-8")) & 2047)
    return value


def _literal_mention_matches(text: str, name: str, ascii_name: bool) -> bool:
    """Match the old escaped literal with its exact ASCII boundary rule."""
    position = text.find(name)
    if not ascii_name:
        return position >= 0
    while position >= 0:
        end = position + len(name)
        if ((position == 0 or text[position - 1] not in _ASCII_WORD)
                and (end == len(text) or text[end] not in _ASCII_WORD)):
            return True
        position = text.find(name, position + 1)
    return False


def is_raw_page(path: str) -> bool:
    """True for directory pages and raw keep-and-search content pages."""
    return (
        path == "index.md"
        or path.endswith("/index.md")
        or path.startswith(RAW_PAGE_PREFIXES)
    )


def _parse_frontmatter_fields(md: str) -> Dict[str, Any]:
    """Extract ``links``/``related``/``source_ids``/``version`` from the
    rendered frontmatter.  Unparseable fields fall back to empty values."""
    result: Dict[str, Any] = {
        "links": [],
        "related": [],
        "source_ids": [],
        "version": 0,
    }
    if not md.startswith("---"):
        return result
    fence = md.find("\n---", 3)
    if fence < 0:
        return result
    for key, raw_value in _FIELD_RE.findall(md, 0, fence):
        try:
            result[key] = json.loads(raw_value)
        except (ValueError, TypeError):
            continue
    return result


def _strip_frontmatter(md: str) -> str:
    """Return the body after the first YAML frontmatter block (``---``)."""
    if not md.startswith("---"):
        return md
    lines = md.splitlines()
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[i + 1 :])
    return md


def build_page_graph_and_links(
    page_index: Dict[str, PageIndexEntry],
    rendered: Dict[str, str],
    *,
    previous: Dict[str, Any] | None = None,
    previous_page_index: Dict[str, PageIndexEntry] | None = None,
    changed_paths: set[str] | None = None,
) -> Dict[str, Any]:
    """Build ``{"page_graph", "in_links", "mentions"}`` from the corpus.

    ``page_index`` is the full published page index (raw pages included).
    ``rendered`` is ``path -> rendered markdown (with frontmatter)``.
    """
    reading: Dict[str, PageIndexEntry] = {
        path: entry for path, entry in page_index.items() if not is_raw_page(path)
    }
    old_reading = {
        path: entry for path, entry in (previous_page_index or {}).items()
        if not is_raw_page(path)
    }
    old_filters = (previous or {}).get("mention_filters") or {}
    incremental = (previous is not None and changed_paths is not None
                   and all(path in old_filters for path in old_reading))
    changed_targets = {path for path, entry in reading.items()
                       if path not in old_reading or (entry.title, tuple(entry.aliases))
                       != (old_reading[path].title, tuple(old_reading[path].aliases))}
    scan_paths = set(reading)
    if incremental:
        scan_paths &= changed_paths | (set(reading) - set(old_reading))
        masks = {_mention_filter(name.strip())
                 for path in changed_targets
                 for name in [reading[path].title, *reading[path].aliases,
                              path.removesuffix(".md")]
                 if name and len(name.strip()) >= _MENTION_MIN_LEN}
        has_short_target = any(len(target.removesuffix(".md")) < _MENTION_MIN_LEN
                               for target in changed_targets)
        filter_matches = {}
        for path in set(reading) - scan_paths:
            value = int(old_filters[path], 16)
            if value not in filter_matches:
                filter_matches[value] = has_short_target or any(value & mask == mask for mask in masks)
            if filter_matches[value]:
                scan_paths.add(path)
    from jonex_wiki.kernel.telemetry import count_work
    count_work("reading_pages_reused", len(reading) - len(scan_paths))
    count_work("reading_pages_scanned", len(scan_paths))
    # An unchanged body already has all edges to unchanged names. Only new or
    # renamed targets need matching there, even when a broad Bloom filter hits.
    full_scan_paths = (scan_paths & (changed_paths | (set(reading) - set(old_reading)))
                       if incremental else scan_paths)
    count_work("reading_pages_full_scan", len(full_scan_paths))
    count_work("reading_pages_target_scan", len(scan_paths - full_scan_paths))

    # 1. page_graph covers every page (raw pages remain readable), rebuilt from
    #    the engine's own rendered frontmatter.
    page_graph: Dict[str, Dict[str, Any]] = {}
    for path in page_index:
        if (incremental and path not in changed_paths
                and path in previous.get("page_graph", {})):
            page_graph[path] = previous["page_graph"][path]
            continue
        fields = _parse_frontmatter_fields(rendered.get(path, ""))
        page_graph[path] = {
            "version": fields["version"],
            "related_pages": fields["related"],
            "links": fields["links"],
            "source_ids": fields["source_ids"],
        }

    # 2. in_links: reverse compile-time navigation edges, reading tree only.
    # Edges to a newly added target may already exist in an old page. Reverse
    # the small cached graph; body text is still read only for affected pages.
    in_links: Dict[str, List[Dict[str, str]]] = {}
    for path in reading:
        for link in page_graph[path]["links"]:
            if not isinstance(link, dict):
                continue
            target = link.get("target_id")
            if not target or target == path or target not in reading:
                continue
            entry = {"path": path}
            why_visit = link.get("why_visit")
            if why_visit:
                entry["why_visit"] = why_visit
            in_links.setdefault(target, []).append(entry)

    # 3. Raw-only changes do not rescan the reading tree. In that common case
    # avoid building candidates for every title/alias in the entire KB.
    anchored = defaultdict(list)
    changed_anchored = defaultdict(list)
    if scan_paths:
        names = defaultdict(set)
        for path, entry in reading.items():
            for name in [entry.title, *entry.aliases]:
                name = (name or "").strip()
                if len(name) >= _MENTION_MIN_LEN:
                    names[name].add(path)
        # Every literal match contains its anchor. A rare three-character
        # substring narrows candidates without changing boundary verification.
        grams = {name: {name[i:i + 3] for i in range(len(name) - 2)} for name in names}
        counts = Counter(gram for keys in grams.values() for gram in keys)
        for name, targets in names.items():
            anchor = min(grams[name], key=lambda gram: (counts[gram], gram))
            anchored[anchor].append((name, targets, name.isascii()))
            changed = targets & changed_targets
            if changed:
                changed_anchored[anchor].append((name, changed, name.isascii()))
        del names, grams, counts

    protected_re = re.compile(r"\[[^\]]*\]\([^)]*\)|`[^`]*`")
    fence_re = re.compile(r"^\s*```")
    heading_re = re.compile(r"^\s*#{1,6}\s")
    wikilink_re = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")

    mentions: Dict[str, List[str]] = {}
    if incremental:
        mentions = {
            target: [source for source in sources
                     if source in reading and source not in full_scan_paths]
            for target, sources in previous.get("mentions", {}).items()
            if target in reading and target not in changed_targets
        }
    mention_filters = {path: old_filters[path] for path in reading
                       if path not in full_scan_paths and path in old_filters}
    for path in reading:
        if path not in scan_paths:
            continue
        body = _strip_frontmatter(rendered.get(path, ""))
        if path in full_scan_paths:
            mention_filters[path] = format(_mention_filter(body), "x")
        candidates = anchored if path in full_scan_paths else changed_anchored
        used: set = set()
        in_fence = False
        for line in body.splitlines():
            if fence_re.match(line):
                in_fence = not in_fence
                continue
            if in_fence or heading_re.match(line):
                continue
            for segment in protected_re.split(line):
                if not segment:
                    continue
                for gram in {segment[i:i + 3] for i in range(len(segment) - 2)}:
                    for name, targets, ascii_name in candidates.get(gram, ()):
                        available = targets - used - {path}
                        if available and _literal_mention_matches(segment, name, ascii_name):
                            used.update(available)
                            for target in available:
                                mentions.setdefault(target, []).append(path)
        # wikilinks (e.g. "## Related pages") also count as mentions, matching
        # the frontend contract.
        for m in wikilink_re.finditer(body):
            target = m.group(1).strip().split("#")[0]
            if target and not target.endswith(".md"):
                target = f"{target}.md"
            if target in reading and target not in used:
                used.add(target)
                mentions.setdefault(target, []).append(path)

    # Replace one target at a time instead of duplicating the whole reverse graph.
    mentions = {target: sorted(set(paths)) for target, paths in mentions.items() if paths}
    in_links = {
        target: sorted(entries, key=lambda e: e["path"])
        for target, entries in in_links.items() if entries
    }
    return {"page_graph": page_graph, "in_links": in_links, "mentions": mentions,
            "mention_filters": mention_filters}
