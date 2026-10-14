"""Published-bundle reading API (engine vocabulary).

Reads the published bundle files (``query/navigation.json``,
``indexes/links.json``, ``claims.jsonl``, ``wiki/*.md``) and returns
engine-domain structures: canonical paths, ``PageIndexEntry`` fields,
``PageLink`` dicts, and raw claim fields.

No platform display cleaning lives here — no HTML stripping, no truncation,
no title rewriting.  That projection belongs to the platform adapter, which
keeps the OpenKB vocabulary, stem compatibility and display formatting.

The bundle layout may be older than the reading indexes (a bundle compiled
before the sinkdown has no ``page_graph`` / ``indexes/links.json``); those
fields degrade to empty values instead of failing.

**同名归并（展示侧）**：本模块是*展示链路*（standalone service 与 platform
共用），因此同名页面在这里被投影成「一个实体一页」——发布 bundle 一字不动，
检索侧（``domain/engine/query`` 的 traversal / corpus）完全不受影响。
归并规则见 ``page_merge``；所有读取入口都经过 ``MergeMap``：
``list_pages`` 只列 winner，``read_page`` 把 loser 解析到 winner 并取全体成员的
references，``get_backlinks`` 合并簇内链入，``get_claims`` /
``list_page_documents`` 按簇取并集。

**claims 口径**：references / claims 一律来自 ``claims.jsonl``（编译期写出的
**全量** claim，实测 2743 条），**不做 ``support_status`` 过滤**。SUPPORTED
过滤只存在于检索侧（``claim_allowed``）与 ``navigation.json.claim_ids``
（无消费方），展示侧要用「编译时真正产出的全部 claim」。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Tuple

from jonex_wiki.domain.engine.compile.mention_index import is_raw_page
from jonex_wiki.domain.reading.page_merge import (
    MergeMap,
    build_merge_map,
    is_merge_candidate,
)
from jonex_wiki.kernel.errors import NotFoundError
from jonex_wiki.domain.engine.json_artifacts import load_json_artifact

_MAX_REFS_PER_PAGE = 12
_MAX_EXCERPTS_PER_DOC = 2
_MAX_CLAIMS_PER_QUERY = 500
_BACKLINK_LIMIT = 30
# [jonex] navigation.json 解析缓存槽位：单份解析后约 24MB，只在重新编译期间
# 新旧两个 bundle 会短暂共存，4 个槽位足以覆盖且不会失控。
_NAVIGATION_CACHE_SIZE = 4


def _page_type(path: str) -> str:
    if path.startswith("sources/"):
        return "source"
    if path.startswith("tables/"):
        return "table"
    return "concept"


def _strip_frontmatter(md: str) -> str:
    """Return the body after the first YAML frontmatter block (``---``)."""
    if not md.startswith("---"):
        return md
    lines = md.splitlines()
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[i + 1 :])
    return md


def _page_bytes(pub: Path, path: str) -> int:
    """正文体量：选主用的「谁更厚」。只需 stat，不必解析 claims.jsonl。"""
    try:
        return (pub / "wiki" / path).stat().st_size
    except OSError:
        return 0


@lru_cache(maxsize=_NAVIGATION_CACHE_SIZE)
def _claim_counts_cached(pub_str: str, stamp: tuple) -> Dict[str, int]:
    """``page_path → claim 数``（全量 claims，不做 support_status 过滤）。

    选主的第一判据。按 (mtime_ns, size) 缓存，同一 revision 只解析一次
    claims.jsonl。
    """
    _, grouped = _claims_by_page_cached(pub_str, stamp)
    return {page: len(indices) for page, indices in grouped.items() if page}


def _claim_counts(pub: Path) -> Mapping[str, int]:
    claims_file = pub / "claims.jsonl"
    try:
        stat = claims_file.stat()
    except OSError:
        return {}
    return _claim_counts_cached(str(pub.resolve()), (stat.st_mtime_ns, stat.st_size))


def _merge_map(pub: Path, nav: Mapping[str, Any]) -> MergeMap:
    """当前 bundle 的展示侧归并映射（纯函数结果；claims 计数按 revision 缓存）。

    ``weight_of`` = claim 数（证据最多的一份是实体正页），``size_of`` = 正文体量
    （只 stat，用于 tie-break）。
    """
    index = nav.get("page_index", {}) or {}
    candidates = {
        str(path): entry
        for path, entry in index.items()
        if not is_raw_page(str(path)) and is_merge_candidate(str(path))
    }
    counts = _claim_counts(pub)
    return build_merge_map(
        candidates,
        weight_of=lambda p: counts.get(p, 0),
        size_of=lambda p: _page_bytes(pub, p),
    )


def merge_map(pub: Path) -> MergeMap:
    """展示侧同名归并映射（供目录投影复用：目录在 DB 里，不在 bundle 内）。"""
    return _merge_map(pub, load_navigation(pub))


def merge_redirects(pub: Path) -> Dict[str, str]:
    """loser→winner 映射（``merge_map`` 的薄封装）。"""
    return dict(merge_map(pub).redirects)


def _remap_graph(graph: Mapping[str, Any], merge: MergeMap, owner: str) -> Dict[str, Any]:
    """page_graph 的 related_pages / links 指向 loser 时改指 winner。

    归并会把「winner → loser」这类边重定向成**自指**（winner → winner），
    `owner` 用于把它们丢掉。无归并时原样返回，行为与归并前逐字节一致。
    """
    if merge.is_identity():
        return dict(graph)
    related: List[str] = []
    for target in graph.get("related_pages", []) or []:
        resolved = merge.resolve(str(target))
        if resolved and resolved != owner and resolved not in related:
            related.append(resolved)
    links: List[Dict[str, Any]] = []
    seen = set()
    for link in graph.get("links", []) or []:
        if not isinstance(link, dict):
            continue
        resolved = merge.resolve(str(link.get("target_id") or ""))
        if not resolved or resolved == owner or resolved in seen:
            continue
        seen.add(resolved)
        links.append({**link, "target_id": resolved})
    return {**graph, "related_pages": related, "links": links}


def _merged_source_ids(page_graph: Mapping[str, Any], cluster: Iterable[str]) -> List[str]:
    """合并后页面的证据指纹 = 簇内全体成员的并集（否则会丢证据）。"""
    out: List[str] = []
    for member in cluster:
        for source_id in (page_graph.get(member, {}) or {}).get("source_ids") or []:
            if source_id not in out:
                out.append(source_id)
    return out


@lru_cache(maxsize=_NAVIGATION_CACHE_SIZE)
def _navigation_cached(pub_str: str, stamp: Tuple[int, int]) -> Dict[str, Any]:
    """[jonex] 按文件指纹缓存 ``navigation.json`` 的解析结果。

    发布 bundle 是不可变目录（按内容 sha 命名），重新编译会换目录，所以
    ``(mtime_ns, size)`` 一变就自然落到新缓存键上，不存在「读到旧内容」。

    动机：该文件实测 4.7MB、解析后约占 24MB 内存、单次解析约 110ms，而阅读
    链路（``list_pages`` / ``read_page`` / ``get_outline`` /
    ``list_page_documents``）每次请求都要读它一遍。缓存后这些路径都免掉重复
    解析，「冷态首击慢」与「重新编译后集体变慢」两个现象同时缓解。

    调用方一律只读——本模块内没有任何对 ``nav`` / ``page_index`` 的写操作，
    故可安全共享同一份对象。``maxsize`` 取小值：单份约 24MB，多 bundle 只在
    重新编译期间短暂共存。
    """
    return load_json_artifact(lambda path:(Path(pub_str)/path).read_bytes(),'query/navigation.json')


def load_navigation(pub: Path) -> Dict[str, Any]:
    nav_file = pub / "query" / "navigation.json"
    try:
        stat = nav_file.stat()
    except OSError:
        # 文件缺失时保持原有行为：交给 read_text 抛 FileNotFoundError
        return json.loads(nav_file.read_text("utf-8"))
    return _navigation_cached(str(pub), (stat.st_mtime_ns, stat.st_size))


def load_links(pub: Path) -> Dict[str, Any]:
    """Read the backlink index; degrade to empty for pre-sinkdown bundles."""
    path = pub / "indexes" / "links.json"
    if not path.is_file():
        return {"in_links": {}, "mentions": {}}
    data = load_json_artifact(lambda relative: (pub / relative).read_bytes(), "indexes/links.json")
    return {
        "in_links": data.get("in_links", {}),
        "mentions": data.get("mentions", {}),
    }


@lru_cache(maxsize=_NAVIGATION_CACHE_SIZE)
def _claims_by_page_cached(pub_str: str, stamp: tuple) -> tuple:
    """One JSONL scan per immutable revision; retain all statuses and evidence."""
    records = []
    grouped: Dict[str, list] = {}
    with (Path(pub_str) / "claims.jsonl").open("r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                claim = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(claim, dict):
                grouped.setdefault(str(claim.get("page_path") or ""), []).append(len(records))
                records.append(claim)
    return tuple(records), {path: tuple(indices) for path, indices in grouped.items()}


def iter_claims(pub: Path, pages: Iterable[str] | None = None) -> Iterator[Dict[str, Any]]:
    """Read all claims, or only the requested page clusters, without rescanning."""
    path = pub / "claims.jsonl"
    try:
        stat = path.stat()
    except FileNotFoundError:
        return
    records, grouped = _claims_by_page_cached(str(pub.resolve()), (stat.st_mtime_ns, stat.st_size))
    indices = range(len(records)) if pages is None else sorted({i for page in pages for i in grouped.get(page, ())})
    for i in indices:
        yield records[i]


def list_pages(pub: Path) -> List[Dict[str, Any]]:
    """Reading-tree pages with full ``PageIndexEntry`` metadata + type + snapshot.

    The nested ``page`` block carries the snapshot fields (``source_ids``/
    ``links``/``related_pages``/``version``) so list projections (e.g. the
    graph view) do not need a per-page read.

    Same-title pages are merged: only the winner appears, and it carries
    ``merged_paths`` / ``merged_count`` so display surfaces can say how many
    pages collapsed into it.
    """
    nav = load_navigation(pub)
    page_index = nav.get("page_index", {})
    page_graph = nav.get("page_graph", {})
    merge = _merge_map(pub, nav)
    pages: List[Dict[str, Any]] = []
    for path in sorted(page_index):
        if is_raw_page(path):
            continue
        if merge.resolve(path) != path:
            # 已并入同名 winner：展示列表里不再单独出现
            continue
        entry = page_index[path]
        item = dict(entry) if isinstance(entry, dict) else {}
        item["path"] = path
        item["type"] = _page_type(path)
        cluster = merge.cluster(path)
        graph = _remap_graph(page_graph.get(path, {}), merge, path)
        item["page"] = {
            "version": graph.get("version", 0),
            "related_pages": graph.get("related_pages", []),
            "links": graph.get("links", []),
            "source_ids": _merged_source_ids(page_graph, cluster),
        }
        if len(cluster) > 1:
            item["merged_paths"] = list(cluster)
            item["merged_count"] = len(cluster)
        pages.append(item)
    return pages


def count_pages(pub: Path) -> Dict[str, int]:
    """[jonex] 只统计展示侧页面数量，按 ``_page_type`` 口径分桶。

    与 ``list_pages`` 用同一套过滤规则（同样的 ``is_raw_page`` 剔除与
    ``merge.resolve`` 同名归并），但跳过逐页的 ``_remap_graph`` /
    ``_merged_source_ids`` 投影、也不构造页面对象：调用方（编译资产面板的
    「三类页数」卡片与 tab 计数）只需要数字，没必要为此组装上千个对象。

    返回 ``{source: n, table: n, concept: n}``；**不做**平台词汇映射——
    section 词汇属于展示侧 adapter。
    """
    nav = load_navigation(pub)
    page_index = nav.get("page_index", {}) or {}
    merge = _merge_map(pub, nav)
    counts: Dict[str, int] = {}
    for path in sorted(page_index):
        if is_raw_page(path):
            continue
        if merge.resolve(path) != path:
            # 已并入同名 winner：与 list_pages 口径一致，不单独计数
            continue
        page_type = _page_type(path)
        counts[page_type] = counts.get(page_type, 0) + 1
    return counts


def read_page(pub: Path, page_id: str) -> Dict[str, Any]:
    """Structured page: index metadata + snapshot fields + body + references.

    ``page_id`` 允许是已被归并的 loser：直接返回 winner 的内容，并附带
    ``requested_path`` / ``redirected_from``，让旧链接与收藏继续可用。
    """
    nav = load_navigation(pub)
    page_index = nav.get("page_index", {})
    if page_id not in page_index:
        raise NotFoundError(f"页面 {page_id} 不存在")
    merge = _merge_map(pub, nav)
    target = merge.resolve(page_id)
    wiki_file = pub / "wiki" / target
    if not wiki_file.is_file():
        raise NotFoundError(f"页面 {page_id} 不存在")
    raw = wiki_file.read_text("utf-8")
    body = _strip_frontmatter(raw)
    page_graph = nav.get("page_graph", {})
    cluster = merge.cluster(page_id)
    graph = _remap_graph(page_graph.get(target, {}), merge, target)
    result: Dict[str, Any] = {
        "path": target,
        "type": _page_type(target),
        "index": page_index[target],
        "page": {
            "version": graph.get("version", 0),
            "related_pages": graph.get("related_pages", []),
            "links": graph.get("links", []),
            "source_ids": _merged_source_ids(page_graph, cluster),
        },
        "body": body,
        "content": body,  # deprecated alias, kept for standalone HTTP compat
        "references": _references_for(pub, cluster),
    }
    if target != page_id:
        result["requested_path"] = page_id
        result["redirected_from"] = page_id
    if len(cluster) > 1:
        result["merged_paths"] = list(cluster)
        result["merged_count"] = len(cluster)
    return result


def get_backlinks(pub: Path, path: str) -> Dict[str, Any]:
    """"What links here": reversed navigation links + body mentions.

    簇内所有成员的链入合并到 winner 上；来源路径同样重定向到 winner，
    并丢掉「归并后指向自己」的自环。
    """
    merge = _merge_map(pub, load_navigation(pub))
    cluster = merge.cluster(path)
    target = merge.resolve(path)
    links = load_links(pub)
    combined: Dict[str, Dict[str, Any]] = {}
    for member in cluster:
        for entry in links["in_links"].get(member, []):
            source = merge.resolve(str(entry.get("path") or ""))
            if not source or source == target:
                continue
            item = combined.setdefault(source, {"path": source, "kind": "link"})
            if entry.get("why_visit") and not item.get("why_visit"):
                item["why_visit"] = entry["why_visit"]
        for source in links["mentions"].get(member, []):
            source = merge.resolve(str(source))
            if not source or source == target:
                continue
            item = combined.get(source)
            if item is None:
                combined[source] = {"path": source, "kind": "mention"}
            elif item["kind"] == "link":
                item["kind"] = "link+mention"
    items = sorted(
        combined.values(),
        key=lambda i: (0 if "link" in i["kind"] else 1, i["path"]),
    )
    mention_count = sum(1 for i in items if "mention" in i["kind"])
    return {
        "path": target,
        "total": len(items),
        "mention_count": mention_count,
        "items": items[:_BACKLINK_LIMIT],
    }


def get_claims(
    pub: Path,
    *,
    page_path: str = "",
    document_id: str = "",
    limit: int = _MAX_CLAIMS_PER_QUERY,
) -> Dict[str, Any]:
    """Claim-level provenance detail, filtered by page and/or document.

    ``page_path`` 命中整个同名簇（全量 claims，不做 support_status 过滤）。
    """
    if not page_path and not document_id:
        return {"items": [], "total": 0}
    wanted = None
    if page_path:
        wanted = set(_merge_map(pub, load_navigation(pub)).cluster(page_path))
    items: List[Dict[str, Any]] = []
    total = 0
    cap = max(1, min(limit, _MAX_CLAIMS_PER_QUERY))
    for claim in iter_claims(pub, wanted):
        row_page = str(claim.get("page_path") or "")
        if wanted is not None and row_page not in wanted:
            continue
        evidence = [e for e in (claim.get("evidence") or []) if isinstance(e, dict)]
        spans = [s for s in (claim.get("source_spans") or []) if isinstance(s, dict)]
        docs = {str(e.get("document_id") or "") for e in evidence}
        docs |= {str(s.get("document_id") or "") for s in spans}
        docs.discard("")
        if document_id and document_id not in docs:
            continue
        total += 1
        if len(items) >= cap:
            continue
        span = spans[0] if spans else {}
        excerpt = ""
        for e in evidence:
            excerpt = e.get("excerpt") or ""
            if excerpt:
                break
        items.append({
            "claim_id": str(claim.get("claim_id") or ""),
            "page_path": row_page,
            "text": claim.get("text") or "",
            "document_ids": sorted(docs),
            "excerpt": excerpt,
            "chunk_id": str(span.get("chunk_id") or ""),
            "start_char": span.get("start_char"),
            "end_char": span.get("end_char"),
            "support_status": str(claim.get("support_status") or ""),
        })
    return {"items": items, "total": total}


def list_page_documents(pub: Path) -> Dict[str, List[str]]:
    """``page_path → 去重 document_id 列表`` (batch claim attribution).

    ``frontmatter`` 的 ``source_ids`` 是 passage 指纹而非 document_id,只有
    ``claims.jsonl`` 的 ``evidence``/``source_spans`` 带 document_id,是
    page→document 的权威归属,供列表按文档过滤与「来源文档数」计数。

    归并后归属取簇内并集并记在 winner 上：否则被并入的页面会把文档归属带走,
    「按文档过滤页面清单」会漏页。
    """
    merge = _merge_map(pub, load_navigation(pub))
    documents: Dict[str, set] = {}
    for claim in iter_claims(pub):
        page_path = str(claim.get("page_path") or "")
        if not page_path:
            continue
        page_path = merge.resolve(page_path)
        docs = documents.setdefault(page_path, set())
        for ref in (claim.get("evidence") or []):
            if isinstance(ref, dict) and ref.get("document_id"):
                docs.add(str(ref["document_id"]))
        for span in (claim.get("source_spans") or []):
            if isinstance(span, dict) and span.get("document_id"):
                docs.add(str(span["document_id"]))
    return {p: sorted(d) for p, d in documents.items()}


def _references_for(pub: Path, cluster: Iterable[str]) -> List[Dict[str, Any]]:
    """Page-level references: source documents behind this page's claims.

    One entry per source document, ranked by supporting claim count, capped at
    ``_MAX_REFS_PER_PAGE``; excerpts are raw (uncleaned), up to
    ``_MAX_EXCERPTS_PER_DOC`` per document.

    扫描 ``claims.jsonl`` 的**全量** claim（实测 2743 条，含 unsupported /
    not_enough_info）——展示侧不做 ``support_status`` 过滤，否则 References 会
    只剩 792 条 supported 支撑的那一部分。``cluster`` 是归并簇，单页时退化为
    单个 path，行为与归并前一致。
    """
    wanted = set(cluster)
    refs: Dict[str, Dict[str, Any]] = {}
    for claim in iter_claims(pub, wanted):
        if str(claim.get("page_path") or "") not in wanted:
            continue
        claim_text = str(claim.get("text") or "")
        for ref in (claim.get("evidence") or []):
            if not isinstance(ref, dict):
                continue
            doc_id = str(ref.get("document_id") or "")
            if not doc_id:
                continue
            entry = refs.setdefault(doc_id, {
                "document_id": doc_id,
                "excerpts": [],
                "claim_count": 0,
                "chunk_id": str(ref.get("chunk_id") or ""),
                "start_char": None,
                "end_char": None,
            })
            entry["claim_count"] += 1
            excerpt = ref.get("excerpt") or claim_text or ""
            if excerpt and len(entry["excerpts"]) < _MAX_EXCERPTS_PER_DOC:
                if excerpt not in entry["excerpts"]:
                    entry["excerpts"].append(excerpt)
        for span in (claim.get("source_spans") or []):
            if not isinstance(span, dict):
                continue
            doc_id = str(span.get("document_id") or "")
            if not doc_id:
                continue
            entry = refs.setdefault(doc_id, {
                "document_id": doc_id,
                "excerpts": [],
                "claim_count": 0,
                "chunk_id": str(span.get("chunk_id") or ""),
                "start_char": None,
                "end_char": None,
            })
            if entry.get("start_char") is None and span.get("start_char") is not None:
                entry["start_char"] = span["start_char"]
            if entry.get("end_char") is None and span.get("end_char") is not None:
                entry["end_char"] = span["end_char"]
    references = sorted(
        refs.values(),
        key=lambda r: (-int(r.get("claim_count") or 0), str(r.get("document_id"))),
    )
    return references[:_MAX_REFS_PER_PAGE]


def get_references(pub: Path, path: str) -> List[Dict[str, Any]]:
    """References for ``path``, unioned over its same-title cluster."""
    return _references_for(pub, _merge_map(pub, load_navigation(pub)).cluster(path))
