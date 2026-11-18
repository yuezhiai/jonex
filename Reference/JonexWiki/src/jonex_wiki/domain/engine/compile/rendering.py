"""Deterministic in-memory Markdown Wiki renderer.

Pure serialization boundary for an accepted ``CompilerState``.  It owns
artifact paths, YAML-compatible frontmatter, global/directory indices and
reciprocal backlinks.  It is deliberately free of any I/O, model call,
clock, random source or network access; later tasks validate and publish
the produced tree.

Paper-required behaviour fixed here:

- a self-contained Markdown Wiki has a global index, directory indices and
  knowledge pages;
- knowledge pages expose metadata, related pages and sources;
- a page-to-page relation is navigable in both directions.

The exact Markdown layout, JSON-style YAML scalars, nested directory indices
and in-memory byte mapping are Jonex engineering adaptations.
"""

from __future__ import annotations

import json
import hashlib
import re
from typing import Dict, List, Mapping, Optional

from jonex_wiki.domain.engine.models.compiler_state import CompilerState
from jonex_wiki.domain.engine.compile.incremental import (
    MutationImpact,
    WikiRenderIndex,
    WikiRenderIndexOverlay,
    WikiRenderDelta,
)

__all__ = [
    "MutationImpact",
    "WikiRenderDelta",
    "WikiRenderIndex",
    "render_wiki_tree",
    "render_wiki_tree_incremental",
    "render_wiki_delta",
    "stabilize_directory_shards",
]

# Artifact paths (renderer-owned).  Index artifacts are not knowledge pages
# and must never be passed to ``canonical_wiki_path``.
_GLOBAL_INDEX_ARTIFACT = "wiki/index.md"
_DIR_INDEX_SUFFIX = "/index.md"
_DIRECTORY_PAGE_LIMIT = 200
READING_RENDER_VERSION = 'agent-file-reading-v1'
_DIRECTORY_READING_HINTS = {
    "pages": "事实与主题索引；连续阅读优先进入原文章节，再核对精确证据。",
    "workbooks": "按来源查看表格导读、工作表或原文分区、日期列与行分组。",
    "tables": "逻辑表的字段、区域与读取入口；先核对来源及工作表。",
    "raw-tables": "按来源组织的表格原文；保留行列与分片位置。",
    "table-views": "同一张表的分组阅读视图；完整范围和计算以原表为准。",
    "source-articles": "连续阅读来源正文，查看章节、上下文与原表。",
    "articles": "按原文章标题阅读；段落保持来源、版本和位置，细事实仍可检索。",
    "source-evidence": "精确来源片段，可回查其原件与位置。",
    "sources": "来源清单、版本及该来源的原文和表格入口。",
    "topics": "主题总览与导航；主题说明不代替原文证据。",
}

# JSON encoding used as deterministic YAML 1.2-compatible scalar/list syntax.
def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _page_id(path: str) -> str:
    return "page-" + hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]


def _page_hash(path: str) -> str:
    """Return the stable 16-hex suffix of a page's durable ID."""
    return _page_id(path)[5:]


def _shard_artifact(dir_path: str, prefix: str) -> str:
    return dir_path + "/by-id/" + "/".join(prefix) + "/index.md"


def stabilize_directory_shards(
    state: CompilerState,
    index: Optional[WikiRenderIndex | WikiRenderIndexOverlay] = None,
) -> CompilerState:
    """Persist split decisions for complete source/table type directories.

    A directory starts sharding only after it exceeds 200 direct objects.
    Once split, prior prefix nodes remain present (possibly empty), while new
    prefixes are added deterministically. This implements the no-auto-merge
    rule without depending on publication order or filesystem state.
    """
    render_index = index or WikiRenderIndex.from_state(state)
    shards = {key: list(value) for key, value in state.directory_shards.items()}
    changed = False
    # ``table-views``（一张表几个阅读角度，见 compile/table_views.py）与
    # ``raw-tables`` 的**扁平**目录同样可能超过 200 个直接对象：前者一张表就有
    # ≤3 页，后者每个 ``<doc>/<table>/`` 目录只有 1 页、不会在这里触发，但把名字
    # 登记进来可以让将来压平或增量的形态自动获得分片。
    for directory in ("sources", "tables", "table-views", "raw-tables", "pages", "source-evidence", "articles"):
        pages = sorted(render_index.direct_pages(directory))
        leaves = set(shards.get(directory, ()))
        if not leaves and len(pages) > _DIRECTORY_PAGE_LIMIT:
            leaves = {_page_hash(path)[:1] for path in pages}
        elif leaves:
            # A new hash may belong to a top-level branch that did not exist
            # at the time of the original split.
            for path in pages:
                digest = _page_hash(path)
                if not any(digest.startswith(prefix) for prefix in leaves):
                    leaves.add(digest[:1])

        while leaves:
            overloaded = next((
                prefix for prefix in sorted(leaves)
                if sum(_page_hash(path).startswith(prefix) for path in pages)
                > _DIRECTORY_PAGE_LIMIT
            ), None)
            if overloaded is None:
                break
            matching = [_page_hash(path) for path in pages if _page_hash(path).startswith(overloaded)]
            if len(overloaded) >= 16:
                raise ValueError("directory shard hash collision exceeds 200 objects")
            children = {digest[:len(overloaded) + 1] for digest in matching}
            leaves.remove(overloaded)
            leaves.update(children)

        prefixes = sorted(leaves)
        if prefixes != shards.get(directory, []):
            if prefixes:
                shards[directory] = prefixes
            changed = True
    return state.model_copy(update={"directory_shards": shards}) if changed else state


def _safe_display(text: str) -> str:
    """Collapse whitespace, then strip WikiLink-breaking characters."""
    collapsed = " ".join(text.split())
    return collapsed.replace("[", "(").replace("]", ")").replace("|", "-")


def _normalize_block(text: str) -> str:
    """Normalize CRLF/CR to LF and drop only boundary LF characters."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.strip("\n")


def _directory_display(segment: str) -> str:
    """Derive a directory display name from its final kebab-case segment."""
    return segment.replace("-", " ").title()


# ``raw-tables/<doc-slug>-<source16>/<table-slug>-<table_id>/`` —— 目录名是引擎
# 生成的身份（可读前缀 + 可回查后缀），直接显示给读者仍然是乱码。这两个正则从
# 段尾把身份抠出来，回查它对应的知识页标题。
_SOURCE_SLUG_TAIL_RE = re.compile(r"(?:^|-)([0-9a-f]{16})$")
_TABLE_ID_TAIL_RE = re.compile(r"(table-[0-9a-f]{8,})$")


def _directory_title(
    state: CompilerState,
    dir_path: str,
    index: WikiRenderIndex | WikiRenderIndexOverlay,
) -> str:
    """Display title for one directory, resolved through its identity suffix.

    ``raw-tables/`` 下面两级目录分别是「来源」与「表」，段尾带着可回查的身份：
    来源段尾部 16 位十六进制 == ``sources/source-<16 hex>.md`` 的同一哈希；表段
    尾部 ``table-…`` 就是 table_id。于是目录可以按**页面标题**显示——这正是
    「父索引按文档列标题」要的效果，而路径本身保持全 ASCII 的 canonical 形式
    （整棵发布树里没有任何非 ASCII 路径，链接解析器也只认 canonical 路径）。
    """
    del index
    segment = dir_path.rsplit("/", 1)[-1]
    derived = _directory_display(segment)
    if dir_path.startswith('source-articles/'):
        source_page = state.pages.get('sources/source-' + segment + '.md')
        if source_page is not None:
            return _safe_display(source_page.index.title) or derived
    if not dir_path.startswith("raw-tables/"):
        return derived
    table_match = _TABLE_ID_TAIL_RE.search(segment)
    if table_match:
        candidate = "tables/{}.md".format(table_match.group(1))
        page = state.pages.get(candidate)
        title = _safe_display(page.index.title) if page is not None else ""
        if title:
            return title
    source_match = _SOURCE_SLUG_TAIL_RE.search(segment)
    if source_match:
        candidate = "sources/source-{}.md".format(source_match.group(1))
        page = state.pages.get(candidate)
        title = _safe_display(page.index.title) if page is not None else ""
        if title:
            return title
    return derived


def _all_directories(
    state: CompilerState,
    index: WikiRenderIndex | WikiRenderIndexOverlay,
) -> set[str]:
    """Return directories from the sidecar index, without scanning pages."""
    del state
    return index.directories()


def _wiki_link(state: CompilerState, target: str) -> str:
    """Build a WikiLink bullet, using the target's title or its path."""
    existing = state.pages.get(target)
    if existing is not None:
        title = _safe_display(existing.index.title)
    else:
        title = _safe_display(target)
    return "- [[" + target + "|" + title + "]]"


def _render_related(state: CompilerState, snapshot, page_path: str) -> str:
    bullets = [_wiki_link(state, target) for target in snapshot.related_pages]
    return "\n".join(bullets) if bullets else "_None._"


def _render_backlinks(
    state: CompilerState,
    page_path: str,
    index: WikiRenderIndex | WikiRenderIndexOverlay,
) -> str:
    inbound = sorted(index.inbound(page_path))
    if not inbound:
        return "_None._"
    return "\n".join(_wiki_link(state, src) for src in inbound)


def _render_sources(source_ids: List[str]) -> str:
    if not source_ids:
        return "_None._"
    return "\n".join("- " + _json(sid) for sid in source_ids)


def _parent_target(dir_path: str):
    """Return (target, display title) of a directory's immediate parent."""
    if "/" not in dir_path:
        return "index.md", "Wiki Index"
    parent = dir_path.rsplit("/", 1)[0]
    return parent + _DIR_INDEX_SUFFIX, _directory_display(parent.rsplit("/", 1)[-1])


def _render_global_index(
    state: CompilerState,
    index: WikiRenderIndex | WikiRenderIndexOverlay,
) -> str:
    del state
    top_level = sorted(
        index.top_level_dirs
        if isinstance(index, WikiRenderIndex)
        else index.top_level_dirs()
    )
    if 'source-articles' in top_level:
        order={'articles':0,'source-articles':1,'sources':2,'workbooks':3,'tables':4,'topics':5,'pages':8,'source-evidence':9}
        top_level.sort(key=lambda path:(order.get(path,5),path))
    lines = [
        "---",
        "kind: " + _json("index"),
        "type: " + _json("index"),
        "page_id: " + _json(_page_id("index.md")),
        "path: " + _json("index.md"),
        "title: " + _json("Wiki Index"),
        "aliases: []",
        "source_ids: []",
        "evidence_refs: []",
        "related: []",
        "---",
        "",
        "# Wiki Index",
        "",
        "## Categories",
        "",
    ]
    if top_level:
        lines.append(
            "\n".join(
                "- [[" + d + "/index.md|" + _directory_display(d) + "]]"
                + (" — " + _DIRECTORY_READING_HINTS[d] if d in _DIRECTORY_READING_HINTS else "")
                for d in top_level
            )
        )
    else:
        lines.append("_None._")
    return "\n".join(lines) + "\n"


def _render_directory_index(
    state: CompilerState,
    dir_path: str,
    index: WikiRenderIndex | WikiRenderIndexOverlay,
) -> str:
    segment = dir_path.rsplit("/", 1)[-1]
    title = _directory_title(state, dir_path, index)
    parent_target, parent_title = _parent_target(dir_path)

    child_dirs_sorted = sorted(index.child_dirs(dir_path))
    child_pages_sorted = sorted(index.direct_pages(dir_path))
    shard_prefixes = sorted({
        prefix[:1] for prefix in state.directory_shards.get(dir_path, ())
    })

    lines = [
        "---",
        "kind: " + _json("index"),
        "type: " + _json("index"),
        "page_id: " + _json(_page_id(dir_path + _DIR_INDEX_SUFFIX)),
        "path: " + _json(dir_path + _DIR_INDEX_SUFFIX),
        "title: " + _json(title),
        "aliases: []",
        "source_ids: []",
        "evidence_refs: []",
        "related: []",
        "---",
        "",
        "# " + title,
        "",
        "Parent: [[" + parent_target + "|" + parent_title + "]]",
        "",
        "## Subcategories",
        "",
    ]
    if child_dirs_sorted:
        lines.append(
            "\n".join(
                "- [["
                + d
                + "/index.md|"
                + _directory_title(state, d, index)
                + "]]"
                for d in child_dirs_sorted
            )
        )
    else:
        lines.append("_None._")

    lines.extend(["", "## Pages", ""])
    if shard_prefixes:
        lines.append("\n".join(
            "- [[" + _shard_artifact(dir_path, prefix) + "|ID "
            + prefix + " objects]]"
            for prefix in shard_prefixes
        ))
    elif child_pages_sorted:
        page_bullets = []
        for page_path in child_pages_sorted:
            entry = state.pages[page_path].index
            safe_title = _safe_display(entry.title)
            safe_desc = _safe_display(entry.description)
            bullet = "- [[" + page_path + "|" + safe_title + "]]"
            if safe_desc:
                bullet += " — " + safe_desc
            page_bullets.append(bullet)
        lines.append("\n".join(page_bullets))
    else:
        lines.append("_None._")

    return "\n".join(lines) + "\n"


def _render_shard_index(
    state: CompilerState,
    dir_path: str,
    prefix: str,
    index: WikiRenderIndex | WikiRenderIndexOverlay,
) -> str:
    artifact_path = _shard_artifact(dir_path, prefix)
    leaves = list(state.directory_shards.get(dir_path, ()))
    child_prefixes = sorted({
        leaf[:len(prefix) + 1]
        for leaf in leaves
        if len(leaf) > len(prefix) and leaf.startswith(prefix)
    })
    pages = sorted(
        path for path in index.direct_pages(dir_path) if _page_hash(path).startswith(prefix)
    )
    lines = [
        "---",
        "kind: " + _json("index"),
        "type: " + _json("index"),
        "page_id: " + _json(_page_id(artifact_path)),
        "path: " + _json(artifact_path),
        "title: " + _json(_directory_display(dir_path) + " ID " + prefix),
        "aliases: []",
        "source_ids: []",
        "evidence_refs: []",
        "related: []",
        "---",
        "",
        "# " + _directory_display(dir_path) + " ID " + prefix,
        "",
        "Parent: [[" + dir_path + "/index.md|" + _directory_display(dir_path) + "]]",
        "",
        "## Pages",
        "",
    ]
    if child_prefixes:
        for child in child_prefixes:
            lines.append(
                "- [[" + _shard_artifact(dir_path, child) + "|ID " + child + " objects]]"
            )
    elif pages:
        for page_path in pages:
            entry = state.pages[page_path].index
            bullet = "- [[" + page_path + "|" + _safe_display(entry.title) + "]]"
            description = _safe_display(entry.description)
            if description:
                bullet += " — " + description
            lines.append(bullet)
    else:
        lines.append("_No current objects in this stable shard._")
    return "\n".join(lines) + "\n"


def _render_page(
    state: CompilerState,
    page_path: str,
    index: WikiRenderIndex | WikiRenderIndexOverlay,
) -> str:
    snapshot = state.pages[page_path]
    entry = snapshot.index
    safe_title = _safe_display(entry.title)
    summary = _normalize_block(entry.description) or "_No summary._"
    content = _normalize_block(snapshot.content) or "_No content._"
    article = entry.scope.get('reading_role') == 'article'
    page_type = "source" if article or page_path.startswith("sources/") else (
        "table" if page_path.startswith("tables/") else "concept"
    )

    lines = [
        "---",
        "kind: " + _json("page"),
        "type: " + _json(page_type),
        "page_id: " + _json(_page_id(page_path)),
        "path: " + _json(page_path),
        "title: " + _json(entry.title),
        "aliases: " + _json(entry.aliases),
        "tags: " + _json(entry.tags),
        "description: " + _json(entry.description),
        "object_id: " + _json(entry.object_id),
        "scope: " + _json(entry.scope),
        "fields: " + _json(entry.fields),
        "why_visit: " + _json(entry.why_visit),
        "limitations: " + _json(entry.limitations),
        "capabilities: " + _json(entry.capabilities),
        "interpretation_status: " + _json(entry.interpretation_status),
        "version: " + str(snapshot.version),
        "source_ids: " + _json(snapshot.source_ids),
        "evidence_refs: " + _json([{"source_id": value} for value in snapshot.source_ids]),
        "related: " + _json(snapshot.related_pages),
        "links: " + _json([link.model_dump() for link in snapshot.links]),
        "---",
        "",
        "# " + safe_title,
        "",
        "## Summary",
        "",
        summary,
        "",
        "## Content",
        "",
        content,
        "",
        "## Related pages",
        "",
        _render_related(state, snapshot, page_path),
        "",
        "## Backlinks",
        "",
        _render_backlinks(state, page_path, index),
        "",
        "## Sources",
        "",
        _render_sources(snapshot.source_ids),
    ]
    if article:
        # A reading projection opens on original content. Repeating a long
        # synthetic title, a list of headings as Summary, and a Content heading
        # would bury the useful text. Identity remains in frontmatter and the
        # reader's source panel; navigation follows the original body.
        header_end = lines.index('---', 1)
        tables = entry.scope.get("preview_table_links") or []
        prefix = "../" * (page_path.count("/") + 1)
        table_links = [
            "- [" + _safe_display(table["title"]) + "](" + prefix + table["page_path"] + ")"
            for table in tables
        ]
        return '\n'.join([
            *lines[:header_end+2],
            *(['# '+_safe_display(entry.scope['original_article_title']), '']
              if entry.scope.get('original_article_title') else []),
            '> 来源：' + _safe_display(str(entry.scope.get('source_name') or entry.title)),
            '', *(['## 完整逻辑表', '', *table_links, ''] if table_links else []),
            content, '', '## Related pages', '',
            _render_related(state, snapshot, page_path), '', '## Sources', '',
            _render_sources(snapshot.source_ids), '',
        ])
    if entry.scope.get('reading_role') == 'fact_index':
        # Fine facts remain addressable/indexable. Their reading projection
        # carries the statement once; continuous source targets precede it.
        # Full index metadata, claims and provenance are still published.
        header_end=lines.index('---',1)
        required={'kind','type','page_id','path','title','version','source_ids'}
        header=[line for line in lines[1:header_end]
                if line.partition(':')[0] in required or (
                    line.partition(':')[0] not in {'description','why_visit'}
                    and line.partition(':')[2].strip() not in {'[]','{}','null','""'})]
        targets=[p for p in entry.scope.get('reading_targets',[]) if p in state.pages]
        links='\n'.join('- [['+p+'|'+_safe_display(state.pages[p].index.title)+']]' for p in targets)
        return '\n'.join(['---',*header,'---','', '# '+safe_title,'',
            *(['## Read source context','',links,''] if links else []),
            content,'','## Related pages','',_render_related(state,snapshot,page_path),
            '','## Backlinks','',_render_backlinks(state,page_path,index),
            *(['','## Sources','',_render_sources(snapshot.source_ids)] if '## Sources' not in content else []),''])
    return "\n".join(lines) + "\n"


def render_wiki_tree(
    state: CompilerState,
    index: Optional[WikiRenderIndex | WikiRenderIndexOverlay] = None,
) -> dict[str, bytes]:
    """Render an accepted ``CompilerState`` into a Markdown Wiki tree.

    Returns a new insertion-ordered dictionary whose keys are sorted
    lexicographically.  Every value is UTF-8, uses LF line endings and ends
    in exactly one LF byte.  Repeated calls with equal states return
    byte-identical mappings.  ``state`` and its nested values are never
    mutated.
    """
    from jonex_wiki.kernel.telemetry import count_work
    count_work("full_wiki_render")
    render_index = index or WikiRenderIndex.from_state(state)
    state = stabilize_directory_shards(state, render_index)
    files: Dict[str, str] = {}
    files[_GLOBAL_INDEX_ARTIFACT] = _render_global_index(state, render_index)

    for page_path in state.pages:
        files["wiki/" + page_path] = _render_page(state, page_path, render_index)

    for dir_path in _all_directories(state, render_index):
        files["wiki/" + dir_path + _DIR_INDEX_SUFFIX] = _render_directory_index(
            state, dir_path, render_index
        )
        shard_nodes = sorted({
            prefix[:length]
            for prefix in state.directory_shards.get(dir_path, ())
            for length in range(1, len(prefix) + 1)
        })
        for prefix in shard_nodes:
            shard_path = "wiki/" + _shard_artifact(dir_path, prefix)
            files[shard_path] = _render_shard_index(state, dir_path, prefix, render_index)

    return {key: files[key].encode("utf-8") for key in sorted(files)}


def render_wiki_tree_incremental(
    state: CompilerState,
    previous_files: Mapping[str, str] | None = None,
    previous_signatures: Mapping[str, str] | None = None,
) -> tuple[dict[str, bytes], dict[str, str], set[str]]:
    """Reuse published bytes when every renderer input for an artifact matches.

    Signatures are persisted with the bundle. Older bundles have no signatures
    and safely render every artifact once. ``changed_pages`` is for downstream
    reading-index updates; it excludes directory indexes.
    """
    from jonex_wiki.kernel.telemetry import count_work

    index = WikiRenderIndex.from_state(state)
    state = stabilize_directory_shards(state, index)
    old_files = previous_files or {}
    old_signatures = previous_signatures or {}
    files: dict[str, bytes] = {}
    signatures: dict[str, str] = {}
    changed_pages: set[str] = set()

    def emit(path: str, material: object, render) -> None:
        signature = hashlib.sha256(json.dumps(
            {'renderer':READING_RENDER_VERSION,'material':material}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        signatures[path] = signature
        old = old_files.get(path.removeprefix("wiki/"))
        if old is not None and old_signatures.get(path) == signature:
            files[path] = old.encode("utf-8")
            count_work("wiki_artifacts_reused")
        else:
            files[path] = render().encode("utf-8")
            count_work("wiki_artifacts_rendered")
            if path.removeprefix("wiki/") in state.pages:
                changed_pages.add(path.removeprefix("wiki/"))

    emit(_GLOBAL_INDEX_ARTIFACT, sorted(index.top_level_dirs),
         lambda: _render_global_index(state, index))
    for path, page in state.pages.items():
        inbound = sorted(index.inbound(path))
        related = list(page.related_pages)
        material = {
            "page": page.model_dump(mode="json"),
            "related_titles": [(target, state.pages[target].index.title if target in state.pages else None)
                               for target in related],
            "inbound_titles": [(source, state.pages[source].index.title)
                               for source in inbound if source in state.pages],
        }
        emit("wiki/" + path, material, lambda path=path: _render_page(state, path, index))

    for directory in _all_directories(state, index):
        direct = sorted(index.direct_pages(directory))
        children = sorted(index.child_dirs(directory))
        material = {
            "title": _directory_title(state, directory, index),
            "children": [(child, _directory_title(state, child, index)) for child in children],
            "pages": [(path, state.pages[path].index.model_dump(mode="json")) for path in direct],
            "shards": list(state.directory_shards.get(directory, ())),
        }
        emit("wiki/" + directory + _DIR_INDEX_SUFFIX, material,
             lambda directory=directory: _render_directory_index(state, directory, index))
        nodes = sorted({prefix[:length]
                        for prefix in state.directory_shards.get(directory, ())
                        for length in range(1, len(prefix) + 1)})
        for prefix in nodes:
            shard_path = "wiki/" + _shard_artifact(directory, prefix)
            emit(shard_path, {"directory": directory, "prefix": prefix, **material},
                 lambda directory=directory, prefix=prefix: _render_shard_index(
                     state, directory, prefix, index))
    return ({path: files[path] for path in sorted(files)}, signatures, changed_pages)


def render_wiki_delta(
    state: CompilerState,
    impact: MutationImpact,
    index: Optional[WikiRenderIndex | WikiRenderIndexOverlay] = None,
) -> WikiRenderDelta:
    """Render only artifacts named by ``impact``.

    The returned ``WikiRenderDelta`` has ``files`` (writes/overwrites),
    ``removed_artifacts`` and ``impact`` fields.  It intentionally does not
    materialize unrelated pages.
    ``render_wiki_tree`` and this function share all artifact render helpers,
    so the full-tree renderer remains the byte oracle in tests.
    """
    render_index = index or WikiRenderIndex.from_state(state)
    state = stabilize_directory_shards(state, render_index)
    files: Dict[str, bytes] = {}

    if impact.render_global_index:
        files[_GLOBAL_INDEX_ARTIFACT] = _render_global_index(
            state, render_index
        ).encode("utf-8")

    for page_path in impact.render_pages:
        if page_path in state.pages:
            files["wiki/" + page_path] = _render_page(
                state, page_path, render_index
            ).encode("utf-8")

    for dir_path in impact.render_directories:
        if render_index.has_directory(dir_path):
            files["wiki/" + dir_path + _DIR_INDEX_SUFFIX] = _render_directory_index(
                state, dir_path, render_index
            ).encode("utf-8")
            shard_nodes = sorted({
                prefix[:length]
                for prefix in state.directory_shards.get(dir_path, ())
                for length in range(1, len(prefix) + 1)
            })
            for prefix in shard_nodes:
                shard_path = "wiki/" + _shard_artifact(dir_path, prefix)
                files[shard_path] = _render_shard_index(
                    state, dir_path, prefix, render_index
                ).encode("utf-8")

    return WikiRenderDelta(
        files={key: files[key] for key in sorted(files)},
        removed_artifacts=tuple(sorted(set(impact.remove_artifacts))),
        impact=impact,
    )
