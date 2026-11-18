"""Retrieval / tokenizer core for the wiki query engine (lwp-arch-001 QA-1).

Extracted verbatim from ``engines/llm_wiki_plus/query_algorithm.py``: the
FTS5 index over knowledge pages, BM25 lexical search, snippet and
passage machinery and the
query-corpus builder. No provider, network,
filesystem, storage or database access happens here beyond the in-memory
SQLite FTS index.  The old import path stays available through the re-export
shim.
"""

from __future__ import annotations

import json
import hashlib
from threading import RLock
import logging
import re
import sqlite3
import sys
import time
from collections import OrderedDict, defaultdict
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from jonex_wiki.domain.engine.compile.rendering import render_wiki_tree
from jonex_wiki.domain.engine.compile.table_views import (
    build_view_pages,
    human_table_title,
    parse_grid,
    view_link_line,
)
from jonex_wiki.kernel.runtime_env import current_env


# ──────────────────────────────────────────────
#  Legacy-patchable surface (read-through seam)
# ──────────────────────────────────────────────
# A few knobs stay owned by the legacy ``query_algorithm`` module namespace so
# that pre-split tests that ``monkeypatch.setattr(qa, <name>, ...)`` keep
# working unchanged.  Each accessor resolves the value through the shim at
# call time; production behaviour is identical to reading the constant
# directly.  These are the only read-backs into the shim from this module.

# A table navigation page carries a structural preview only.  The bound keeps one
# page read affordable for the next model request and forces the reader tools.
TABLE_PREVIEW_LIMIT = 8 * 1024


def _bounded_preview(text: str, limit: int = TABLE_PREVIEW_LIMIT) -> tuple[str, bool]:
    """Return at most ``limit`` UTF-8 bytes of text and whether it was cut."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, False
    return encoded[:limit].decode("utf-8", errors="ignore"), True


def _segmenter_available() -> bool:
    """Delegate to the shim so ``query_algorithm._segmenter_available`` patches
    (e.g. tests simulating a jieba-less environment) stay effective."""

    from jonex_wiki.domain.engine.query.algorithm import (
        _segmenter_available as _impl,
    )

    return _impl()


def _segment_for_search(text: str) -> str:
    """Delegate to the shim so ``query_algorithm._segment_for_search`` patches
    stay effective for the moved retrieval machinery."""

    from jonex_wiki.domain.engine.query.algorithm import (
        _segment_for_search as _impl,
    )

    return _impl(text)


logger = logging.getLogger(__name__)

from jonex_wiki.domain.engine.models.query_models import (
    QueryCorpus,
    RawWikiPage,
    SearchHit,
)
from jonex_wiki.kernel.telemetry import (
    current_runtime_telemetry,
    telemetry_stage,
)
from jonex_wiki.domain.engine.models.compiler_state import (
    ArchivedPassage,
    CompilerState,
    PageIndexEntry,
    WikiPageSnapshot,
    normalize_wiki_path,
)
from jonex_wiki.domain.engine.compile.content_validation import (
    ClaimDraft,
    ValidatedClaim,
    validate_claim_evidence,
)
from jonex_wiki.domain.engine.compile.rendering import render_wiki_tree

MAX_SEARCH_HITS = 10
# Wave 4 Q6: configurable hit cap.  Offline eval showed musique q_hit@10=0.818
# vs q_hit@20=0.868 (+0.05): raising the cap recovers bridge pages that BM25
# pushes below the old top-10 on large corpora.  Default stays at 10; set
# QUERY_MAX_SEARCH_HITS=20 to widen retrieval.  More hits cost a little more
# model-facing text but can save wiki_read calls.
MAX_SEARCH_HITS_ENV = "QUERY_MAX_SEARCH_HITS"

# Wave 4 Q1: query-anchored body snippet on each search hit.  Default on; off
# via ``QUERY_SNIPPET=off`` for a strict A/B against the no-snippet baseline.
QUERY_SNIPPET_ENV = "QUERY_SNIPPET"


# Wave 3 P2: passage-level candidates.  Each page body is split into bounded,
# paragraph-aligned passages that are indexed separately, so a bridge page can
# be surfaced by the passage that actually carries the query terms.  Search
# snippets anchor on the best matching passage instead of the first occurrence
# anywhere in the page.
PASSAGE_MAX_CHARS = 600

def _env_int(name: str, default: int) -> int:
    """Read a positive integer env override, falling back to ``default``."""
    raw = current_env().get(name, "")
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return default


def _max_search_hits() -> int:
    """Read the search-hit cap override (default ``MAX_SEARCH_HITS``)."""
    return _env_int(MAX_SEARCH_HITS_ENV, MAX_SEARCH_HITS)


def _snippet_enabled() -> bool:
    """Whether search hits carry a query-anchored body snippet (default on).

    ``QUERY_SNIPPET=off`` restores the no-snippet baseline for a strict A/B.
    """
    raw = current_env().get(QUERY_SNIPPET_ENV, "on").strip().casefold()
    return raw not in ("0", "off", "false", "no")


FTS5_UNAVAILABLE = "FTS5_UNAVAILABLE"


# ──────────────────────────────────────────────
#  FTS5 index over knowledge pages only
# ──────────────────────────────────────────────

_TOKEN_RE = re.compile(r"[a-z0-9]+|[一-鿿]")


# Wave 3 P1.2: soft question phrasing and filler tokens dropped by the
# deterministic empty-search rewrite so the re-search targets the content
# bearing key entities.
_QUERY_STOPWORDS = frozenset({
    "what", "who", "when", "where", "why", "how", "which", "whose", "whom",
    "is", "are", "was", "were", "be", "been", "being", "do", "does", "did",
    "the", "a", "an", "of", "to", "in", "on", "at", "for", "with", "from",
    "by", "as", "and", "or", "but", "it", "its", "this", "that", "these",
    "those", "tell", "me", "about", "please", "give", "explain", "describe",
    "there", "their", "的", "是", "和", "与", "以及", "关于", "什么", "怎么",
    "如何", "为什么", "谁", "哪些", "哪个", "请问", "一下", "这个", "那个",
    "这些", "那些",
})


def _rewrite_query_for_search(question: str) -> str:
    """Deterministically rewrite a search query that returned no hits.

    Drops soft question phrasing and stopwords, keeping the content-bearing
    tokens (typically the key entities) so a re-search targets the actual
    entities.  With jieba available the rewrite runs at word granularity so a
    Chinese entity stays a whole term instead of degrading to single chars.
    Pure and deterministic; never calls a model.  Returns the input unchanged
    when there is no meaningful rewrite.
    """
    terms = _query_terms(question)
    kept = [t for t in terms if t.casefold() not in _QUERY_STOPWORDS]
    if not kept:
        return question
    rewritten = " ".join(kept)
    if rewritten == " ".join(terms):
        return question
    return rewritten


_SEARCH_LITERAL_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    r"[A-Za-z][A-Za-z0-9_]*(?:[./:-][A-Za-z0-9_]+)+|"
    r"[+-]?\d+(?:[.,/:-]\d+)*(?:[eE][+-]?\d+)?[%％]?"
    r")(?![\d.])"
)


def _query_terms(query: str) -> List[str]:
    """Keep literal values intact without deciding their semantic type.

    FTS still tokenizes punctuation inside a quoted phrase, but it must receive
    the whole phrase: dropping 4.7 merely because it contains a dot loses the
    only discriminating value. Original literals also anchor ranking and
    snippets. This does not equate dates, decimals, versions or identifiers.
    """
    literals = [match.group() for match in _SEARCH_LITERAL_RE.finditer(query)]
    remainder = _SEARCH_LITERAL_RE.sub(" ", query)
    terms = ([t for t in _segment_for_search(remainder).split(" ") if t and t.isalnum()]
             if _segmenter_available() else _TOKEN_RE.findall(remainder.casefold()))
    return list(dict.fromkeys([*literals, *terms]))


def _fts_query(query: str) -> str:
    """Build a safely quoted FTS query from a raw user/tool query.

    With jieba available the query is segmented into words (Chinese entities
    stay whole terms), each wrapped in a double-quoted FTS string literal and
    joined by ``OR`` — matching the word-level index built by
    ``_build_fts_index``.  Without jieba the legacy fallback keeps only
    alphanumeric runs and single CJK characters.  In both cases punctuation can
    therefore never become FTS syntax or SQL.
    """
    terms = _query_terms(query)
    if not terms:
        return ""
    return " OR ".join('"{}"'.format(term.replace('"', '""')) for term in terms)


def _fts_rows(corpus: QueryCorpus, paths: Iterable[str]):
    """Prepare identical FTS text while sharing repeated segmentation work.

    jieba already treats whitespace as independent block boundaries. Segment
    each non-whitespace run once in this build, then join exactly as the
    original segmenter does. Preserve dictionary-specific splits (e.g. 5B).
    Whitespace also separates jieba's CJK blocks. Keep large runs uncached so
    a source with no whitespace cannot fill the bounded cache with long text.
    """
    from functools import lru_cache
    from jonex_wiki.kernel.telemetry import count_work, current_runtime_telemetry, telemetry_stage

    @lru_cache(maxsize=8192)
    def segment_run(text):
        return _segment_for_search(text)

    def segment(text):
        count_work("fts_input_chars", len(text))
        if text.isascii():
            count_work("fts_ascii_chars", len(text))
        return " ".join(
            segment_run(run) if len(run) <= 8192 else _segment_for_search(run)
            for run in re.findall(r"\S+", text)
        )

    rows, passages, claims = [], [], []
    with telemetry_stage(current_runtime_telemetry(), "fts_tokenize"):
        for path in paths:
            if path not in corpus.page_index:
                continue
            entry = corpus.page_index[path]
            body = corpus.page_text.get(path, "")
            rows.append((segment(entry.title), segment(" ".join(entry.aliases)),
                         segment(" ".join(entry.tags)), segment(entry.description),
                         segment(body), path))
            passages.extend((segment(text), path, i) for i, text in enumerate(_split_passages(body)))
            claims.extend((segment(c.text), path, c.claim_id) for c in corpus.claims.get(path, []))
    count_work("fts_page_rows", len(rows))
    count_work("fts_passage_rows", len(passages))
    count_work("fts_claim_rows", len(claims))
    count_work("fts_token_cache_hits", segment_run.cache_info().hits)
    count_work("fts_segment_calls", segment_run.cache_info().misses)
    return rows, passages, claims


def _build_fts_index(corpus: QueryCorpus) -> "sqlite3.Connection":
    """Build the in-memory FTS5 index over knowledge pages only.

    Every indexed text column is run through ``_segment_for_search`` so CJK
    content is word-segmented before the unicode61 tokenizer — the query side
    (``_fts_query``) uses the same segmenter, which is what makes Chinese
    queries match (see ``_segment_for_search``).  English text passes through
    unchanged.  Raises ``ValueError(FTS5_UNAVAILABLE)`` when table creation is
    unavailable.  There is deliberately no ``LIKE``, substring or other search
    fallback.
    """
    try:
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA encoding='UTF-8'")
        conn.execute(
            "CREATE VIRTUAL TABLE pages USING fts5("
            "title, alias, tag, description, body, path UNINDEXED, "
            "tokenize='unicode61 remove_diacritics 0')"
        )
        conn.execute(
            "CREATE VIRTUAL TABLE passages USING fts5("
            "text, page_path UNINDEXED, idx UNINDEXED, "
            "tokenize='unicode61 remove_diacritics 0')"
        )
        conn.execute(
            "CREATE VIRTUAL TABLE claims USING fts5("
            "text, page_path UNINDEXED, claim_id UNINDEXED, "
            "tokenize='unicode61 remove_diacritics 0')"
        )
        rows, passage_rows, claim_rows = _fts_rows(corpus, sorted(corpus.page_index))
        from jonex_wiki.kernel.telemetry import current_runtime_telemetry, telemetry_stage
        with telemetry_stage(current_runtime_telemetry(), "fts_insert"):
            conn.executemany("INSERT INTO pages VALUES (?,?,?,?,?,?)", rows)
            conn.executemany("INSERT INTO passages VALUES (?,?,?)", passage_rows)
            conn.executemany("INSERT INTO claims VALUES (?,?,?)", claim_rows)
            _ensure_fts_paths(conn)
            conn.commit()
    except Exception:
        raise ValueError(FTS5_UNAVAILABLE)
    return conn


def _ensure_fts_paths(conn):
    """One indexed path-to-row map; old published databases migrate privately."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='fts_paths'").fetchone():
        return
    from jonex_wiki.kernel.telemetry import count_work
    count_work("fts_path_map_build")
    conn.execute("CREATE TABLE fts_paths (path TEXT NOT NULL, kind TEXT NOT NULL, row_id INTEGER NOT NULL, PRIMARY KEY(kind,row_id))")
    conn.execute("CREATE INDEX fts_paths_path ON fts_paths(path)")
    for table, column in (("pages", "path"), ("passages", "page_path"), ("claims", "page_path")):
        conn.execute(f"INSERT INTO fts_paths SELECT {column}, ?, rowid FROM {table}", (table,))


def _insert_fts_delta(conn, rows, passages, claims):
    for table, data, path_index in (("pages", rows, 5), ("passages", passages, 1), ("claims", claims, 1)):
        if not data:
            continue
        last = conn.execute(f"SELECT rowid FROM {table} ORDER BY rowid DESC LIMIT 1").fetchone()
        start = (last[0] if last else 0) + 1
        placeholders = ",".join("?" for _ in range(len(data[0]) + 1))
        columns = {"pages": "title,alias,tag,description,body,path",
                   "passages": "text,page_path,idx", "claims": "text,page_path,claim_id"}[table]
        conn.executemany(f"INSERT INTO {table}(rowid,{columns}) VALUES ({placeholders})",
                         ((start + i, *row) for i, row in enumerate(data)))
        conn.executemany("INSERT INTO fts_paths VALUES (?,?,?)",
                         ((row[path_index], table, start + i) for i, row in enumerate(data)))


def lexical_preprocessing_identity():
    if _segmenter_available():
        import jieba
        return "jieba:" + str(getattr(jieba, "__version__", "unknown")) + ":unicode61-v2"
    return "cjk-character:unicode61-v2"


def build_fts_index_bytes(corpus: QueryCorpus, *, previous=None, output_directory=None) -> bytes:
    """Update a private SQLite copy; the prior publication remains immutable."""
    from jonex_wiki.kernel.telemetry import count_work
    from jonex_wiki.kernel.ports import RevisionFileReference,RevisionFileArtifact
    from pathlib import Path
    import os, tempfile, shutil
    private_path=None
    def private_name():
        descriptor,path=tempfile.mkstemp(prefix='fts-',suffix='.sqlite',dir=output_directory)
        os.close(descriptor)
        return path
    if previous is None or previous.corpus is None:
        count_work("fts_full_build")
        conn = _build_fts_index(corpus)
    else:
        old = previous.corpus
        def signature(c, path):
            entry = c.page_index.get(path)
            return ((entry.title, tuple(entry.aliases), tuple(entry.tags), entry.description)
                    if entry else None, c.page_text.get(path, ""),
                    tuple((v.text, v.claim_id) for v in c.claims.get(path, [])))
        from jonex_wiki.kernel.telemetry import current_runtime_telemetry, telemetry_stage
        with telemetry_stage(current_runtime_telemetry(), "fts_diff"):
            paths = set(old.page_index) | set(corpus.page_index)
            changed = {p for p in paths if signature(old, p) != signature(corpus, p)}
        count_work("fts_records_updated", len(changed))
        if not changed:
            count_work("fts_index_reused")
            index=previous.lexical_index
            return index.read_bytes() if output_directory is None and isinstance(index,RevisionFileReference) else index
        if output_directory is not None and isinstance(previous.lexical_index,RevisionFileReference) and previous.lexical_index.verify_file:
            from jonex_wiki.kernel.telemetry import current_runtime_telemetry,telemetry_stage
            private_path=private_name()
            try:
                with telemetry_stage(current_runtime_telemetry(),'fts_private_copy'):
                    shutil.copyfile(previous.lexical_index.verify_file(),private_path)
                conn=sqlite3.connect(private_path)
            except BaseException:
                Path(private_path).unlink(missing_ok=True);raise
        else:
            conn = _open_fts_index(previous.lexical_index, writable=True)
        try:
            with conn:
                _ensure_fts_paths(conn)
                with telemetry_stage(current_runtime_telemetry(), "fts_delete"):
                    deleted = defaultdict(list)
                    for path in sorted(changed):
                        for table, rowid in conn.execute("SELECT kind,row_id FROM fts_paths WHERE path=?", (path,)):
                            deleted[table].append((rowid,))
                    for table, rowids in deleted.items():
                        conn.executemany(f"DELETE FROM {table} WHERE rowid=?", rowids)
                    conn.executemany("DELETE FROM fts_paths WHERE path=?", ((path,) for path in sorted(changed)))
                rows, passages, claims = _fts_rows(corpus, sorted(changed))
                with telemetry_stage(current_runtime_telemetry(), "fts_insert"):
                    _insert_fts_delta(conn, rows, passages, claims)
        except Exception:
            conn.close()
            if private_path:Path(private_path).unlink(missing_ok=True)
            raise
    try:
        from jonex_wiki.kernel.telemetry import current_runtime_telemetry, telemetry_stage
        with telemetry_stage(current_runtime_telemetry(), "fts_serialize"):
            if output_directory is not None:
                if private_path is None:
                    private_path=private_name();target=sqlite3.connect(private_path)
                    try:conn.backup(target)
                    finally:target.close()
                conn.close()
                artifact=RevisionFileArtifact.from_file('indexes/lexical.sqlite',private_path)
                private_path=None
                count_work('fts_private_file_published')
                return artifact
            if hasattr(conn, "serialize"):
                return bytes(conn.serialize())
            # Older SQLite builds use a private backup so published bytes stay
            # portable; include that I/O in the same serialization measurement.
            import os
            import tempfile
            descriptor, path = tempfile.mkstemp(prefix="jonex-fts-", suffix=".sqlite")
            os.close(descriptor)
            try:
                target = sqlite3.connect(path)
                try:
                    conn.backup(target)
                finally:
                    target.close()
                with open(path, "rb") as handle:
                    return handle.read()
            finally:
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
    finally:
        conn.close()
        if private_path:Path(private_path).unlink(missing_ok=True)


def _open_fts_index(data: bytes, *, writable: bool = False) -> "sqlite3.Connection":
    """Open and minimally validate a lexical index from published bytes."""
    from jonex_wiki.kernel.ports import RevisionFileReference,RevisionFileArtifact
    if isinstance(data,RevisionFileReference):
        if not writable and data.verify_file:
            from pathlib import Path
            conn=None
            try:
                conn=sqlite3.connect(Path(data.verify_file()).resolve().as_uri()+'?mode=ro&immutable=1',uri=True)
                if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pages'").fetchone():
                    raise ValueError(FTS5_UNAVAILABLE)
                return conn
            except Exception as exc:
                if conn is not None:conn.close()
                raise ValueError(FTS5_UNAVAILABLE) from exc
        data=data.read_bytes()
    elif isinstance(data,RevisionFileArtifact):data=data.read_bytes()
    if not isinstance(data, bytes) or not data:
        raise ValueError(FTS5_UNAVAILABLE)
    try:
        conn = sqlite3.connect(":memory:")
        if hasattr(conn, "deserialize"):
            conn.deserialize(data)
        else:
            import os
            import tempfile
            descriptor, path = tempfile.mkstemp(prefix="jonex-fts-open-", suffix=".sqlite")
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(data)
                source = sqlite3.connect("file:{}?mode=ro".format(path), uri=True)
                try:
                    source.backup(conn)
                finally:
                    source.close()
            finally:
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "pages" not in tables:
            raise ValueError(FTS5_UNAVAILABLE)
        if writable and hasattr(conn, "deserialize"):
            # Deserialized databases default to a 1 GiB growth limit. Backup
            # into an ordinary private database before incremental writes;
            # the published bytes and query-only opening stay unchanged.
            target = sqlite3.connect(":memory:")
            try:
                conn.backup(target)
            except Exception:
                target.close()
                raise
            conn.close()
            conn = target
        return conn
    except Exception as exc:
        try:
            conn.close()
        except Exception:
            pass
        raise ValueError(FTS5_UNAVAILABLE) from exc


def _search_terms(query: str) -> List[str]:
    """Content terms used to anchor body snippets on a hit page.

    Same token logic as ``_fts_query`` but drops stopwords so the snippet
    anchors on a content-bearing term (e.g. an entity) instead of a generic
    function word that appears everywhere.
    """
    terms = _query_terms(query)
    kept = [t for t in terms if t.casefold() not in _QUERY_STOPWORDS]
    return kept or terms


def _split_passages(body: str, max_chars: int = PASSAGE_MAX_CHARS) -> List[str]:
    """Split a page body into bounded, paragraph-aligned passages.

    Blank lines delimit paragraphs; a paragraph longer than ``max_chars`` is
    cut at the last sentence or space boundary inside the cap.  Empty passages
    are dropped.  Pure and deterministic, so the FTS index and the snippet
    anchoring use exactly the same units.
    """
    if not body:
        return []
    passages: List[str] = []
    for raw_para in body.split("\n\n"):
        para = raw_para.strip()
        if not para:
            continue
        while len(para) > max_chars:
            cut = para.rfind("。", 0, max_chars + 1)
            if cut < max_chars * 0.5:
                cut = para.rfind(". ", 0, max_chars + 1)
            if cut < max_chars * 0.5:
                cut = para.rfind(" ", 0, max_chars + 1)
            if cut <= 0:
                cut = max_chars
            else:
                cut += 1
            passages.append(para[:cut].strip())
            para = para[cut:].strip()
        if para:
            passages.append(para)
    return passages


def _passage_hits(
    conn: "sqlite3.Connection",
    query: str,
    limit: int = 40,
) -> List[tuple]:
    """Return the top ``limit`` passages ranked by BM25.

    Each result is ``(page_path, passage_idx, passage_text, bm25_score)``.
    Fails open to ``[]`` so a passage-level miss never breaks page search.
    """
    fts = _fts_query(query)
    if not fts:
        return []
    try:
        cur = conn.execute(
            "SELECT page_path, idx, text, bm25(passages, 1.0) AS rank "
            "FROM passages WHERE passages MATCH ? "
            "ORDER BY bm25(passages, 1.0) ASC, page_path ASC, idx ASC "
            "LIMIT ?",
            (fts, limit),
        )
        return [(r[0], int(r[1]), r[2], float(r[3])) for r in cur.fetchall()]
    except Exception:
        return []


def _claim_hits(
    conn: "sqlite3.Connection",
    query: str,
    limit: int = 20,
) -> List[tuple]:
    """Return the top ``limit`` validated claims ranked by BM25.

    Each result is ``(page_path, claim_id, claim_text, bm25_score)``.  Claims
    are the compiler's validated fact statements, so a claim that matches the
    query is a more precise snippet anchor than the raw passage that generated
    it.  Fails open to ``[]`` on a miss.
    """
    fts = _fts_query(query)
    if not fts:
        return []
    try:
        cur = conn.execute(
            "SELECT page_path, claim_id, text, bm25(claims, 1.0) AS rank "
            "FROM claims WHERE claims MATCH ? "
            "ORDER BY bm25(claims, 1.0) ASC, page_path ASC, claim_id ASC "
            "LIMIT ?",
            (fts, limit),
        )
        return [(r[0], r[1], r[2], float(r[3])) for r in cur.fetchall()]
    except Exception:
        return []


def _make_snippet(body: str, terms: List[str], limit: int = 250) -> str:
    """A query-anchored excerpt from a page body.

    Prefer a bounded window containing several query terms over an entity-only
    prefix so the model can choose the relevant passage before reading it.
    Falls back to the body prefix when no term occurs (e.g. alias-only hit).
    """
    terms = sorted({re.sub(r"^fy(?=\d{4}$)", "", term.casefold())
                    for term in terms if term})
    starts = {max(0, match.start() - 80) for term in terms
              for match in re.finditer(re.escape(term), body, re.IGNORECASE)}
    if not starts:
        return body[:limit]
    def relevance(start: int) -> tuple:
        window = body[start:start + limit].casefold()
        counts = [window.count(term) for term in terms]
        return sum(count > 0 for count in counts), sum(counts), -start
    start = max(starts, key=relevance)
    return body[start:start + limit]


_TOKEN_CACHE = OrderedDict()
_TOKEN_CACHE_LOCK = RLock()
_TOKEN_CACHE_BYTES = 0


def _material_tokens(text: str) -> frozenset[str]:
    """Share exact, content-addressed token sets; retain neither sources nor queries."""
    global _TOKEN_CACHE_BYTES
    from jonex_wiki.domain.engine.query.algorithm import _segment_for_search as segmenter
    limit = max(0, int(current_env().get("JONEX_WIKI_QUERY_TOKEN_CACHE_BYTES", 16 * 1024 * 1024)))
    key = (_segment_for_search, segmenter, hashlib.sha256(text.encode("utf-8")).digest())
    with _TOKEN_CACHE_LOCK:
        while _TOKEN_CACHE and _TOKEN_CACHE_BYTES > limit:
            _, (_, weight) = _TOKEN_CACHE.popitem(last=False)
            _TOKEN_CACHE_BYTES -= weight
        found = _TOKEN_CACHE.get(key) if limit else None
        if found is not None:
            _TOKEN_CACHE.move_to_end(key)
            return found[0]
    tokens = frozenset(_segment_for_search(text).split())
    weight = sys.getsizeof(tokens) + sum(sys.getsizeof(t) for t in tokens) + 256
    if not limit or weight > limit:
        return tokens
    with _TOKEN_CACHE_LOCK:
        # Another concurrent query may already have computed these same tokens.
        if key not in _TOKEN_CACHE:
            while _TOKEN_CACHE and _TOKEN_CACHE_BYTES + weight > limit:
                _, (_, removed) = _TOKEN_CACHE.popitem(last=False)
                _TOKEN_CACHE_BYTES -= removed
            _TOKEN_CACHE[key] = (tokens, weight)
            _TOKEN_CACHE_BYTES += weight
    return tokens


def _search_material(corpus: QueryCorpus, path: str) -> tuple[str, str, frozenset[str]]:
    entry = corpus.page_index.get(path)
    body = corpus.page_text.get(path, "")
    haystack = " ".join((
        entry.title if entry else "",
        " ".join(entry.aliases) if entry else "",
        body,
    )).casefold()
    return body, haystack, _material_tokens(haystack)


class _SearchMaterialCache:
    """LRU of complete page material, owned by one query and pinned corpus.

    Charge Python strings, token sets and a conservative entry overhead. An
    oversized page is still searched in full, but is never retained here.
    """

    def __init__(self, corpus: QueryCorpus, max_bytes: int | None = None):
        self.corpus = corpus
        self.max_bytes = max(0, int(
            current_env().get("JONEX_WIKI_QUERY_MATERIAL_CACHE_BYTES", 16 * 1024 * 1024)
            if max_bytes is None else max_bytes
        ))
        self.entries = OrderedDict()
        self.bytes = 0
        self.hits = self.misses = self.evictions = self.oversized = 0

    def get(self, path: str) -> tuple[str, str, frozenset[str]]:
        cached = self.entries.get(path)
        if cached is not None:
            self.hits += 1
            self.entries.move_to_end(path)
            return cached[0]
        self.misses += 1
        material = _search_material(self.corpus, path)
        body, haystack, tokens = material
        weight = (sys.getsizeof(path) + sys.getsizeof(material)
                  + sys.getsizeof(body) + sys.getsizeof(haystack)
                  + sys.getsizeof(tokens) + sum(sys.getsizeof(t) for t in tokens)
                  + 256)
        if weight > self.max_bytes:
            self.oversized += 1
            return material
        while self.entries and self.bytes + weight > self.max_bytes:
            _, (_, removed_weight) = self.entries.popitem(last=False)
            self.bytes -= removed_weight
            self.evictions += 1
        self.entries[path] = (material, weight)
        self.bytes += weight
        return material

    def body(self, path: str) -> str:
        cached = self.entries.get(path)
        if cached is not None:
            self.entries.move_to_end(path)
            return cached[0][0]
        return self.corpus.page_text.get(path, "")


def _source_filename_hints(corpus, query):
    """Generic filename tokens supply a candidate pool, never a source policy."""
    names={}
    for path,entry in corpus.page_index.items():
        metadata=(corpus.source_locations.get(path) or {}).get("metadata") or {}
        values=[metadata.get("source_name", ""),*entry.aliases]
        values+=re.findall(r"[^:/\\]+?\.(?:pdf|docx|xlsx|xlsm|md|txt|csv)",entry.title,re.I)
        for name in values:
            name=str(name).strip()
            if not re.search(r"\.(pdf|docx|xlsx|xlsm|md|txt|csv)$",name,re.I):continue
            tokens=set(re.findall(r"[a-z]{3,}|[0-9]+|[\u4e00-\u9fff]{2,}",name.rsplit(".",1)[0].casefold()))
            names.setdefault(name,tokens)
    counts=defaultdict(int)
    for tokens in names.values():
        for token in tokens:counts[token]+=1
    terms=set(re.findall(r"[a-z]{3,}|[0-9]+|[\u4e00-\u9fff]{2,}",query.casefold()))
    scored=[]
    for name,tokens in names.items():
        overlap=terms&tokens
        rare=[t for t in overlap if not t.isdigit() and counts[t]<=max(2,len(names)//4)]
        if rare:scored.append((len(rare)*3+len(overlap),name))
    best=max((score for score,_ in scored),default=0)
    return [name for score,name in scored if score==best][:4]


def source_scoped_paths(corpus, source_ids):
    """Published ownership only; never infer scope from question words."""
    wanted=set(source_ids)
    known=set()
    owned={}
    for path,entry in corpus.page_index.items():
        scope=entry.scope or {}
        owners=set(scope.get('source_ids') or [])
        if scope.get('source_id'):owners.add(scope['source_id'])
        owners.update(record.get('source_id') for record in corpus.source_records.get(path,[]) if record.get('source_id'))
        for claim in corpus.claims.get(path,[]):
            owners.update(ref.source_id for ref in claim.evidence)
        known.update(owners)
        if owners and owners <= wanted:owned[path]=True
    if wanted-known:
        raise ValueError('QUERY_SOURCE_SCOPE_UNKNOWN')
    return set(owned)


def _fts_search(
    conn: "sqlite3.Connection",
    corpus: QueryCorpus,
    query: str,
    *,
    material_cache: _SearchMaterialCache | None = None,
    source_ids: Sequence[str] = (),
) -> List[SearchHit]:
    """Run a ranked FTS search, ordered by BM25 then canonical path.

    Column weights are 8, 6, 4, 3, 1 over title, alias, tag, description and
    body; ``path`` is unindexed.  Returns at most ``MAX_SEARCH_HITS`` hits.
    Raises ``ValueError(FTS5_UNAVAILABLE)`` when querying is unavailable.

    """
    if material_cache is not None and material_cache.max_bytes == 0:
        material_cache = None
    bm25_ranked: List[tuple[str, float]] = []
    hint_paths=set()
    allowed=source_scoped_paths(corpus,source_ids) if source_ids else None
    if allowed is not None and not allowed:return []
    # Filter before candidate truncation, including on immutable old indexes.
    # Bound source_ids at the tool boundary. JSON carries paths as data; it
    # neither interpolates SQL nor changes the persisted FTS schema.
    scope_sql=' AND path IN (SELECT value FROM json_each(?))' if allowed is not None else ''
    scope_params=[json.dumps(sorted(allowed))] if allowed is not None else []
    try:
        with telemetry_stage(current_runtime_telemetry(), "bm25"):
            fts = _fts_query(query)
            if fts:
                cur = conn.execute(
                    "SELECT path, bm25(pages, 8.0, 6.0, 4.0, 3.0, 1.0) AS rank "
                    "FROM pages WHERE pages MATCH ? " + scope_sql + " "
                    "ORDER BY bm25(pages, 8.0, 6.0, 4.0, 3.0, 1.0) ASC, path ASC "
                    "LIMIT ?",
                    (fts, *scope_params, _max_search_hits() * 8),
                )
                bm25_ranked = [(r[0], float(r[1])) for r in cur.fetchall()]
                # Numeric OR terms can fill the whole pool with unrelated
                # tables. Add an independent bounded topic pool before ranking.
                topic_terms = [t for t in _search_terms(query)
                               if len(t) > 1 and not any(c.isdigit() for c in t)]
                if topic_terms:
                    topic_query = " OR ".join('"' + t.replace('"', '""') + '"' for t in topic_terms)
                    extra = conn.execute(
                        "SELECT path, bm25(pages, 8.0, 6.0, 4.0, 3.0, 1.0) FROM pages WHERE pages MATCH ?"+scope_sql+" ORDER BY bm25(pages, 8.0, 6.0, 4.0, 3.0, 1.0), path LIMIT ?",
                        (topic_query, *scope_params, _max_search_hits() * 8)).fetchall()
                    seen = {p for p, _ in bm25_ranked}
                    bm25_ranked.extend((p, float(rank)) for p, rank in extra if p not in seen)

                names=_source_filename_hints(corpus,query)
                if names:
                    names_query=" OR ".join('(title:"'+name.replace('"','""')+'" OR alias:"'+name.replace('"','""')+'")' for name in names)
                    rows=conn.execute("SELECT path,bm25(pages,8.0,6.0,4.0,3.0,1.0) FROM pages WHERE pages MATCH ?"+scope_sql+" ORDER BY bm25(pages,8.0,6.0,4.0,3.0,1.0),path LIMIT ?",
                        ('('+names_query+') AND ('+fts+')',*scope_params,_max_search_hits()*8)).fetchall()
                    hint_paths={row[0] for row in rows}
                    seen={path for path,_ in bm25_ranked}
                    bm25_ranked.extend((path,float(rank)) for path,rank in rows if path not in seen)
    except Exception:
        raise ValueError(FTS5_UNAVAILABLE)

    # FTS uses OR so a short page matching only a common word can outrank
    # a page matching several distinct literal query terms.
    # Fetch a bounded wider pool and prefer distinct query-term coverage,
    # retaining BM25 as the tie-breaker.  This is deterministic and applies
    # to every lexical query, including numeric/date lookups.
    terms = _search_terms(query)
    def lexical_coverage(path: str) -> int:
        _body, haystack, segmented = (
            material_cache.get(path) if material_cache is not None
            else _search_material(corpus, path)
        )
        score = sum(1 for term in set(terms) if (
            bool(re.search(r"(?<![A-Za-z0-9_.])" + re.escape(term.casefold())
                           + r"(?![A-Za-z0-9_.])", haystack))
            if _SEARCH_LITERAL_RE.fullmatch(term) else term.casefold() in segmented))
        return score
    phrase_terms=[t.casefold() for t in terms if len(t)>2 and not t.isdigit()]
    phrases={re.sub(r"\W+","",a+b) for a,b in zip(phrase_terms,phrase_terms[1:]) if len(a+b)>=7}
    def phrase_coverage(path):
        compact=re.sub(r"\W+","",corpus.page_text.get(path,"").casefold())
        return sum(phrase in compact for phrase in phrases)
    rank=lambda item:(-phrase_coverage(item[0]),-lexical_coverage(item[0]),item[1],item[0])
    if hint_paths:
        preferred=sorted((item for item in bm25_ranked if item[0] in hint_paths),key=rank)[:max(1,_max_search_hits()-2)]
        chosen={p for p,_ in preferred}
        ordered=preferred+sorted((item for item in bm25_ranked if item[0] not in chosen),key=rank)[:_max_search_hits()-len(preferred)]
    else:
        ordered=sorted(bm25_ranked,key=rank)[:_max_search_hits()]

    # New publications expose continuous articles as the read target of a
    # fine-grained fact hit. Preserve its discovery identity, but do not ask
    # the model to read several tiny pages from the same article.
    discovery_indexes={}
    if any(corpus.page_index[path].scope.get('reading_targets') for path,_ in ordered if path in corpus.page_index):
        pool=sorted(bm25_ranked,key=rank)
        if hint_paths:
            pool=[item for item in pool if item[0] in hint_paths]+[item for item in pool if item[0] not in hint_paths]
        projected=[];seen=set()
        for original,score in pool:
            if original not in corpus.page_index:continue
            entry=corpus.page_index[original]
            targets=entry.scope.get('reading_targets') or [original]
            for target in targets:
                if target not in corpus.page_index or target in seen or (allowed is not None and target not in allowed):continue
                seen.add(target);projected.append((target,score))
                if target!=original:discovery_indexes[target]=original
                if len(projected)>=_max_search_hits():break
            if len(projected)>=_max_search_hits():break
        ordered=projected

    bm25_score_map = {path: score for path, score in bm25_ranked}

    # Wave 4 Q1: precompute content terms once for snippet anchoring.
    enabled = _snippet_enabled()
    terms = _search_terms(query) if enabled else []
    claim_by_page: Dict[str, str] = {}
    passage_by_page: Dict[str, tuple] = {}
    claim_units: Dict[str, List[str]] = {}
    passage_units: Dict[str, List[str]] = {}
    if enabled:
        for c_path, _c_id, c_text, _c_score in _claim_hits(conn, query, 20):
            if c_path not in claim_by_page:
                claim_by_page[c_path] = c_text
            if c_path not in claim_units:
                claim_units[c_path] = []
            if len(claim_units[c_path]) < 3:
                claim_units[c_path].append(c_text)
        for p_path, p_idx, p_text, _p_score in _passage_hits(conn, query, 40):
            if p_path not in passage_by_page:
                passage_by_page[p_path] = (p_idx, p_text)
            if p_path not in passage_units:
                passage_units[p_path] = []
            if len(passage_units[p_path]) < 3:
                passage_units[p_path].append(p_text)
    hits: List[SearchHit] = []
    for path, rank in ordered:
        entry = corpus.page_index.get(path)
        if entry is None:
            continue
        body = (material_cache.body(path) if material_cache is not None
                else corpus.page_text.get(path, ""))
        discovery = discovery_indexes.get(path, path)
        best_claim = claim_by_page.get(discovery) or claim_by_page.get(path)
        evidence_units = claim_units.get(discovery) or passage_units.get(discovery, [])[:3]
        if best_claim is not None:
            snippet = _make_snippet(best_claim, terms)
        else:
            best_passage = passage_by_page.get(discovery) or passage_by_page.get(path)
            if best_passage is not None:
                snippet = _make_snippet(best_passage[1], terms)
            else:
                snippet = _make_snippet(body, terms) if enabled else ""
        scope = dict(entry.scope)
        if scope.get('reading_role') == 'article':
            # Full integrity/reference metadata stays in the Bundle. Search
            # returns a bounded locator, never a second copy of its outline.
            references = scope.pop('reference_targets', [])
            scope.pop('article_sha256', None)
            scope.pop('body_sha256', None)
            scope['reference_count'] = len(references)
            scope['heading_path'] = scope.get('heading_path', [])[-8:]
            scope['section_titles'] = scope.get('section_titles', [])[:8]
        hits.append(
            SearchHit(
                path=path,
                rank=float(rank) if rank is not None else 0.0,
                title=entry.title,
                aliases=list(entry.aliases),
                tags=list(entry.tags),
                description=entry.description,
                bm25_score=bm25_score_map.get(path),
                snippet=snippet,
                evidence_units=[_make_snippet(u, terms) for u in evidence_units],
                scope={**scope, **({'discovery_index_path':discovery_indexes[path]} if path in discovery_indexes else {}), 'source_context': [{
                    key: corpus.source_scopes[sid].get(key) for key in (
                        'source_name', 'source_sha256', 'source_version',
                        'source_period_labels', 'period_basis', 'subject_mentions')}
                    for sid in entry.scope.get('source_ids', []) if sid in corpus.source_scopes]},
            )
        )
    return hits


def with_table_navigation_context(corpus: QueryCorpus, hits: Sequence[SearchHit],
                                  cache: Dict[str, Any]) -> List[SearchHit]:
    """Expose same-source worksheet regions and their source overview as clues.

    This uses saved scope metadata, never a date/meal vocabulary or an inferred
    entity identity. A tiny region can match a heading while the requested cells
    live elsewhere in the worksheet. Context paths remain candidates only.
    The metadata index is built once per query; no grid or body is scanned.
    """
    def single_row(scope):
        bounds = scope.get("bounds") or scope.get("row_bounds") or scope
        return (isinstance(bounds, dict) and bounds.get("row_start") is not None
                and bounds.get("row_start") == bounds.get("row_end"))

    # Raw fragments name their table but may omit its worksheet. Resolve only
    # through the published table identity, with matching source ownership.
    resolved_hits = []
    for hit in hits:
        table_id = hit.scope.get("table_id")
        parent = corpus.page_index.get(f"tables/{table_id}.md") if table_id else None
        if (not hit.scope.get("sheet") and parent is not None
                and set(hit.scope.get("source_ids") or [])
                & set(parent.scope.get("source_ids") or [])):
            hit = hit.model_copy(update={"scope": {
                **hit.scope, "sheet": parent.scope.get("sheet")}})
        resolved_hits.append(hit)
    hits = resolved_hits
    anchors = [hit for hit in hits if hit.scope.get("sheet")
               and hit.scope.get("source_ids") and single_row(hit.scope)]
    if not anchors:
        return list(hits)
    if "tables" not in cache:
        tables, overviews = defaultdict(list), {}
        for path, entry in corpus.page_index.items():
            ids = entry.scope.get("source_ids") or []
            for source in ids:
                if path.startswith("tables/") and entry.scope.get("sheet"):
                    tables[(source, entry.scope["sheet"])].append((path, entry))
                elif path.startswith("workbooks/"):
                    overviews[source] = path
        cache.update(tables=tables, overviews=overviews)
    enriched = []
    for hit in hits:
        if hit not in anchors:
            enriched.append(hit)
            continue
        related, seen = [], {hit.path}
        for source in hit.scope.get("source_ids") or []:
            candidates = cache["tables"].get((source, hit.scope.get("sheet")), [])
            for path, entry in sorted(candidates, key=lambda item: item[0]):
                if (path in seen or entry.object_id == hit.scope.get("table_id")
                        or len(related) >= 3):
                    continue
                seen.add(path)
                related.append({"path": path, "table_id": entry.object_id,
                    "title": entry.title, "sheet": entry.scope.get("sheet"),
                    "bounds": {key: entry.scope.get(key) for key in (
                        "row_start", "row_end", "column_start", "column_end")},
                    "description": entry.description[:350]})
            overview = cache["overviews"].get(source)
            if overview and len(related) < 4:
                related.append({"path": overview, "kind": "source_overview"})
        enriched.append(hit.model_copy(update={"scope": {
            **hit.scope, **({"table_context": related[:4]} if related else {})}}))
    return enriched


def derive_page_number_map(
    state: CompilerState, claims: List[ValidatedClaim]
) -> Dict[str, int]:
    """LWP-RET-002 D-5: derive ``page_path -> page_number`` query-side.

    PageIndexEntry is frozen and gains no field; the page number is read from
    the archived passage ``source_metadata`` at query time and surfaced as an
    independent map on the QueryCorpus.  The first supported claim's evidence
    passage that carries an integer ``page_number`` wins for each page.

    Pure and deterministic; never mutates ``state``.
    """
    page_number_map: Dict[str, int] = {}
    for claim in claims:
        if claim.unresolved_evidence or claim.support_status != "SUPPORTED":
            continue
        if claim.page_path in page_number_map:
            continue
        for ref in claim.evidence:
            passage = state.source_archive.get(ref.passage_id)
            if passage is None:
                continue
            page_number = passage.source_metadata.get("page_number")
            if isinstance(page_number, int) and not isinstance(page_number, bool):
                page_number_map[claim.page_path] = page_number
                break
    return page_number_map


def _chapter_tree_hits(
    corpus: QueryCorpus, question: str, limit: int = 5
) -> List[SearchHit]:
    """LWP-RET-002 chapter_tree route: navigation hits over the render tree.

    Surfaces knowledge pages (chapter pages) derived from the wiki render tree
    plus the query-side page-number map: pages whose title / description /
    path overlap the question terms, ordered by page number when available so
    a numbered chapter question surfaces the chapter pages in document order.
    A pure-navigation question with no FTS terms returns the chapter pages in
    page-number order (bounded by ``limit``).

    Navigation files (``index.md`` / ``dir/index.md``) are intentionally not
    returned here — they are not canonical knowledge paths and cannot be
    SearchHits; the controller force-navigates them on read instead.
    Pure and deterministic.
    """
    terms = {t.casefold() for t in _search_terms(question)}
    selected: List[str] = []
    for path in sorted(corpus.page_index):
        # Navigation files are not canonical knowledge paths (SearchHit.path
        # rejects them); the controller force-navigates them on read instead.
        if path == "index.md" or path.endswith("/index.md"):
            continue
        if terms:
            entry = corpus.page_index[path]
            haystack = set(_search_terms(entry.title))
            haystack |= set(_search_terms(entry.description))
            haystack |= set(_search_terms(path))
            if not (terms & haystack):
                continue
        selected.append(path)
    selected.sort(key=lambda p: (corpus.page_number_map.get(p, 1 << 30), p))
    hits: List[SearchHit] = []
    for index, path in enumerate(selected[:limit]):
        entry = corpus.page_index[path]
        hits.append(
            SearchHit(
                path=path,
                rank=float(len(selected) - index),
                title=entry.title,
                aliases=list(entry.aliases),
                tags=list(entry.tags),
                description=entry.description,
                bm25_score=None,
                snippet="",
                evidence_units=[],
            )
        )
    return hits


def _support_rule_is_current(version: str | None) -> bool:
    """Whether stored support labels must match the current deterministic rule.

    ``None`` means the caller did not load a published Bundle (compile-time
    corpus builds), so the current rule applies and mismatches stay fatal.
    """
    from jonex_wiki.domain.engine.models.claim import SUPPORT_RULE_VERSION
    if version not in (None, SUPPORT_RULE_VERSION, "claim-support-v1", "claim-support-v2"):
        raise ValueError("unsupported claim support rule: " + str(version))
    return version is None or version == SUPPORT_RULE_VERSION


def validate_query_claims(
    state: CompilerState,
    claims: List[ValidatedClaim],
    *,
    tolerate_support_drift: bool = False,
    previous_state: CompilerState | None = None,
    previous_claims: List[ValidatedClaim] | None = None,
) -> None:
    """Validate accepted claims once, independently of rendering/indexing.

    ``tolerate_support_drift`` is set only while opening a Bundle whose support
    labels were published under an older deterministic support rule: the stored
    labels stay authoritative for that Bundle (it is read, never rewritten),
    while evidence and span identity are still re-checked exactly.
    """
    from jonex_wiki.kernel.telemetry import count_work
    count_work("claim_validation", len(claims))
    if not isinstance(state, CompilerState):
        raise ValueError("QUERY_CORPUS_INVALID_STATE")

    old_claims = {claim.claim_id: claim for claim in previous_claims or []}
    unchanged_documents: set[str] = set()
    delta = getattr(state, '_compile_delta', None)
    if previous_state is not None and delta is not None:
        unchanged_documents = set(delta.unchanged_documents)
    elif previous_state is not None:
        old_sources, current_sources = defaultdict(dict), defaultdict(dict)
        for pid, passage in previous_state.source_archive.items():
            old_sources[passage.document_id][pid] = passage
        for pid, passage in state.source_archive.items():
            current_sources[passage.document_id][pid] = passage
        unchanged_documents = {sid for sid, passages in current_sources.items()
                             if passages == old_sources.get(sid)}
    seen_ids = set()
    for claim in claims:
        if claim.claim_id in seen_ids:
            raise ValueError("DUPLICATE_CLAIM_ID: " + claim.claim_id)
        seen_ids.add(claim.claim_id)

        if claim.page_path not in state.pages:
            raise ValueError("CLAIM_PAGE_MISSING: " + claim.page_path)
        # The previous Bundle has already passed the decoder's full trust
        # boundary. Reuse its proof only for the identical claim and archived
        # inputs; changed/missing evidence always takes the ordinary gate.
        if previous_state is not None and old_claims.get(claim.claim_id) == claim:
            refs = list(claim.evidence) + list(claim.unresolved_evidence)
            # The gate also reads neighbouring passages for table/unit scope.
            # Reuse therefore requires the complete document archive to match.
            if refs and all(ref.document_id in unchanged_documents for ref in refs):
                count_work("claim_validation_reused")
                continue
        # Claim text is not required to be a substring of the (LLM-rewritten)
        # page content; claim correctness is enforced by validate_claim_evidence
        # against the archived source passage.

        # Wave 3 P0.3: re-validate over the FULL evidence (resolved + unresolved)
        # so the stable id still matches, then exclude unresolved-evidence claims
        # from the query corpus below instead of silently exposing bad evidence.
        full_evidence = list(claim.evidence) + list(claim.unresolved_evidence)
        if not full_evidence:
            raise ValueError("CLAIM_WITHOUT_EVIDENCE: " + claim.claim_id)
        draft = ClaimDraft(
            page_path=claim.page_path,
            text=claim.text,
            evidence=full_evidence,
        )
        report = validate_claim_evidence(state, [draft])
        if not report.validated_claims or report.validated_claims[0].claim_id != claim.claim_id:
            raise ValueError("STALE_CLAIM: " + claim.claim_id)
        revalidated = report.validated_claims[0]
        support_changed = (
            claim.support_status != revalidated.support_status
            or claim.support_reason != revalidated.support_reason
        )
        if (
            claim.evidence != revalidated.evidence
            or claim.unresolved_evidence != revalidated.unresolved_evidence
            or claim.source_spans != revalidated.source_spans
            or (support_changed and not tolerate_support_drift)
        ):
            raise ValueError("STALE_CLAIM_SUPPORT: " + claim.claim_id)


_TABLE_GUIDANCE_SEARCH = (
    "表格身份不明时，可在 `articles/tables/index.md` 和 `full-tables/` 中搜索标题、表头或相关文字；"
    "零命中不证明业务不存在。"
)


TABLE_CANDIDATE_LIMIT = 12


def _table_identity_lines(*, table_id: str, document_id: str, source_id: str,
                          source_sha256: str, scope: Mapping[str, Any], source_path: str,
                          table_path: str) -> List[str]:
    """State which table this page is, by the identity the artifacts carry.

    Everything here is read from the table's own record: the ids, the sheet, the
    cell bounds and the coordinates of its preview.  A page number or a
    neighbouring table is never rendered as identity, and a field the artifacts
    do not carry is stated as unknown instead of guessed.
    """
    lines = [
        "- table_id `{}`".format(table_id),
        "- document_id `{}`；source_id `{}`；source_sha256 `{}`".format(
            document_id, source_id, source_sha256),
    ]
    sheet_id = str(scope.get("sheet_id") or "")
    if sheet_id:
        lines.append("- sheet_id `{}`".format(sheet_id))
    bounds = scope.get("bounds")
    if isinstance(bounds, Mapping) and bounds:
        rows = "{}–{}".format(bounds.get("row_start"), bounds.get("row_end"))
        columns = "{}–{}".format(bounds.get("column_start"), bounds.get("column_end"))
        lines.append("- 数据范围：行 {}，列 {}（该表自身网格坐标）".format(rows, columns))
    elif isinstance(bounds, (list, tuple)) and bounds:
        lines.append("- 数据范围：`{}`（该表自身网格坐标）".format(bounds))
    elif bounds == [] or bounds is None:
        lines.append("- 数据范围：未记录（不以页面位置推断）")
    lines.append(
        "- 本页与来源导航页 `{}`、表格原文页 `{}` 由上述标识连接；"
        "同一来源、同一文档或同一页上的其他表都不是本表。".format(source_path, table_path))
    return lines


def _table_candidate_lines(siblings: Sequence[Mapping[str, Any]],
                           *, table_id: str) -> List[str]:
    """List other tables of the same source as scoped candidates.

    A candidate is offered so a reader can *check* it, never so a run can answer
    with it: each line carries the scope that candidate covers and says that it
    must be confirmed through its own identity before any value is taken from it.
    """
    candidates = [item for item in siblings
                  if str(item.get("table_id") or "") and str(item.get("table_id")) != table_id]
    if not candidates:
        return []
    lines = []
    for item in candidates[:TABLE_CANDIDATE_LIMIT]:
        scope = item.get("scope") if isinstance(item.get("scope"), Mapping) else {}
        bounds = (scope or {}).get("bounds")
        if isinstance(bounds, Mapping) and bounds:
            scope_note = "范围：行 {}–{}，列 {}–{}".format(
                bounds.get("row_start"), bounds.get("row_end"),
                bounds.get("column_start"), bounds.get("column_end"))
        elif isinstance(bounds, (list, tuple)) and bounds:
            scope_note = "范围：`{}`".format(bounds)
        else:
            scope_note = "范围：未记录"
        title = str(item.get("title") or "")
        title_note = "（标题 `{}`）".format(title) if title else ""
        lines.append(
            "- 候选 table_id `{}`{} — status `{}`；{}。候选不是本表：先按来源、工作表和范围确认身份，"
            "确认前不得据此取值，也不得用它替代本页的表。".format(
                item.get("table_id"), title_note, item.get("status"), scope_note))
    omitted = len(candidates) - TABLE_CANDIDATE_LIMIT
    if omitted > 0:
        lines.append("- 另有 {} 张同来源表未列出（本页只列前 {} 张候选）。".format(
            omitted, TABLE_CANDIDATE_LIMIT))
    return lines


def build_table_navigation_page(
    *,
    source_id: str,
    source_name: str,
    document_id: str,
    source_sha256: str,
    table_id: str,
    title: str,
    interpretation_status: str,
    fields: List[str],
    scope: Dict[str, Any],
    raw_text: str,
    source_path: str,
    table_path: str,
    title_aliases: Sequence[str] = (),
    siblings: Sequence[Mapping[str, Any]] = (),
    complete_table: Optional[str] = None,
) -> tuple[PageIndexEntry, WikiPageSnapshot, str]:
    """One deterministic navigation page for a logical table.

    ``raw_text`` is a bounded locator. The complete saved logical table is a
    separate ordinary file; unconfirmed structures stay explicitly marked.

    ``title_aliases`` keeps the flattened header line this page used to be
    titled with, so loosening the title for readers does not lose the column
    labels retrieval matches on.

    The page states its own identity first (document, source, table, sheet and
    cell bounds), because that identity - not a page number or a neighbour in
    the same document - is what connects an article's table reference to the
    original table.  ``siblings`` are the source's other tables: they are listed
    as *candidates* with the scope each one covers, and the page says in code
    that a candidate is not this table, so no reader and no run may slide onto a
    neighbouring table when the requested one cannot answer.
    """
    status = str(interpretation_status or "unknown")
    grid = parse_grid(raw_text)
    bounds = scope.get("bounds") or scope.get("row_bounds") or scope
    rows = (bounds["row_end"] - bounds["row_start"] + 1
            if isinstance(bounds, dict) and isinstance(bounds.get("row_start"), int)
            and isinstance(bounds.get("row_end"), int) else None)
    columns = (bounds["column_end"] - bounds["column_start"] + 1
               if isinstance(bounds, dict) and isinstance(bounds.get("column_start"), int)
               and isinstance(bounds.get("column_end"), int) else None)
    from jonex_wiki.domain.engine.compile.table_navigation import full_table_path
    full_path=full_table_path(source_id,table_id)
    prefix = "../" * (table_path.count("/") + 1)
    full_link = "[读取完整逻辑表](" + prefix + complete_table + ")\n\n" if complete_table else ""
    size = "保存范围 {} 行 × {} 列（含表头）".format(rows, columns) if rows and columns else "行列范围见完整逻辑表"
    card = "来源：{}；工作表/位置：{}；{}。\n字段：{}。\n\n".format(
        source_name, scope.get("sheet") or scope.get("page_number") or title,
        size, "、".join(fields[:12]) or "尚未确认")
    preview_text, preview_truncated = _bounded_preview(raw_text)
    preview = "```markdown\n" + preview_text.rstrip("\n") + "\n```\n"
    # A reader lands on this page before any agent does: open with one plain
    # sentence about the table's shape instead of a bare code fence.
    shape = ""
    if grid is not None:
        shape = "下面是结构预览（{} 行 × {} 列）。\n\n".format(len(grid[1]), len(grid[0]))
    if preview_truncated:
        preview += (
            "\n本页只是结构预览，已按 {} 字节截断；请直接读取完整逻辑表文件。\n"
        ).format(TABLE_PREVIEW_LIMIT)
    if status == "accepted":
        table_body = (
            full_link + shape
            + preview
            + ("" if complete_table else "\n完整逻辑表候选路径：`{}`。若文件存在，请直接读取整表，核对表头、单位、期间与附注；若不存在，请在 `articles/tables/index.md` 按来源和表格 ID 查找，或回查原文。".format(full_path))
            + "本页预览仅供定位。{}\n".format(_TABLE_GUIDANCE_SEARCH)
        )
        table_capabilities = ["read_file"]
        table_limitations = [
            "navigation_only",
            "preview_is_not_complete_table",
            "no_aggregation",
            "no_sort",
            "no_join",
            "no_formula_recalculation",
        ]
        if preview_truncated:
            table_limitations.append("preview_truncated")
    else:
        article_path = "sources/articles/{}.md".format(source_id)
        table_body = (
            full_link + preview
            + "\n编译时结构未确认（interpretation_status=`{}`）。preview 是定位线索。"
              .format(status)
            + ("" if complete_table else "若完整逻辑表文件 `{}` 存在，请直接读取并核对其结构；否则在 `articles/tables/index.md` 查找或回查原文。".format(full_path))
            + "未确认的字段不可用于统计。来源全文为 `{}`，来源导航为 `{}`。\n".format(article_path, source_path)
        )
        table_capabilities = ["read_file"]
        table_limitations = [
            "navigation_only",
            "preview_is_not_complete_table",
            "structure_unconfirmed",
            "raw_observation_only",
        ]
        if preview_truncated:
            table_limitations.append("preview_truncated")
    identity = _table_identity_lines(
        table_id=str(table_id), document_id=document_id, source_id=source_id,
        source_sha256=source_sha256, scope=scope, source_path=source_path,
        table_path=table_path)
    candidates = _table_candidate_lines(siblings, table_id=str(table_id))
    if identity:
        table_body = card + "\n".join(identity) + "\n\n" + table_body
    if candidates:
        table_body += ("\n## 同来源候选表（候选，不是本表，不得替代）\n"
                       + "\n".join(candidates) + "\n")
    entry = PageIndexEntry(
        path=table_path,
        title=title[:200],
        aliases=[str(table_id)] + [value for value in title_aliases if value],
        tags=["table", "navigation-only"],
        description="{}；{}；字段：{}；编译解释 {}。预览仅用于定位，直接读取本表可获得完整交付或明确缺口。".format(
            source_name, size, "、".join(fields[:8]), status),
        object_id=str(table_id),
        scope=dict(scope),
        fields=list(fields),
        why_visit="Inspect this logical table, locate exact values, then read the complete necessary range.",
        limitations=table_limitations,
        capabilities=table_capabilities,
        interpretation_status=status,
    )
    snapshot = WikiPageSnapshot(
        index=entry,
        content=table_body,
        source_ids=[source_id],
        related_pages=[source_path],
        links=[{
            "target_id": source_path,
            "why_visit": "Verify the table against its source identity and original artifact.",
            "evidence_refs": [{"source_id": source_id, "source_sha256": source_sha256}],
            "kind": "navigation",
        }],
    )
    return entry, snapshot, table_body


def _column_letter(index: int) -> str:
    letter = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letter = chr(ord("A") + remainder) + letter
    return letter


def workbook_concept_path(source_id: str) -> str:
    """Canonical Wiki path of a source's workbook skeleton page."""
    return "workbooks/workbook-{}.md".format(
        hashlib.sha256(str(source_id).encode("utf-8")).hexdigest()[:16])


def build_workbook_concept_page(
    *,
    outline: Any,
    source_id: str,
    source_name: str,
    document_id: str,
    source_sha256: str,
    source_path: str,
    page_path: str,
    table_paths: Dict[str, str],
) -> tuple[PageIndexEntry, WikiPageSnapshot, str]:
    """One deterministic concept page orienting a whole native workbook.

    A spreadsheet contributes no prose, so without this page a reader gets a
    grid with no orientation: nothing says which sheet covers which dates, which
    column a date lives in, or how a sheet bands its rows.  The page states that
    skeleton and links every table it describes.

    Two properties are deliberate.  It is built from the **full** grid, never
    from the bounded parser preview, which can stop mid-table and drop whole
    sections while still looking complete.  And
    it carries **structure only** — never a cell value — so it cannot become a
    second and staler source of truth beside the grid it describes.
    """
    tables = list(getattr(outline, "tables", ()) or ())
    from jonex_wiki.domain.engine.compile.table_navigation import full_table_path
    described = [table for table in tables if table.table_id in table_paths]
    linked_paths = [table_paths[table.table_id] for table in described]

    lines: List[str] = ["# " + (source_name or source_id), ""]
    lines += [
        "## Summary",
        "",
        "{} 个工作表、{} 张逻辑表。**本页只给骨架与取数入口，不含单元格取值**；"
        "逐格取值请读取下列完整逻辑表文件。".format(
            getattr(outline, "sheet_count", 0) or len({
                table.sheet for table in described}),
            len(described),
        ),
        "",
    ]

    dated = [table for table in described if table.dates]
    if dated:
        lines += [
            "## 日期 → 星期 → 列",
            "",
            "星期由日期推得，不取自表头——部分工作表的表头存的是 Excel 序列号。",
            "",
            "| 工作表 | 日期 | 星期 | 列 | 表头原值 |",
            "|---|---|---|---|---|",
        ]
        for table in dated:
            for item in table.dates:
                lines.append("| {} | {} | {} | {} | {} |".format(
                    table.sheet, item.date, item.weekday or "—",
                    _column_letter(int(item.column_index)),
                    item.raw_value or "—",
                ))
        lines.append("")

    banded = [table for table in described if table.groups]
    if banded:
        lines += [
            "## 行分组结构",
            "",
            "行范围来自表格自身的合并单元格，因此各工作表可以不同。",
            "",
            "| 工作表 | 分组 | 行范围 | 段内第二列取值 |",
            "|---|---|---|---|",
        ]
        for table in banded:
            for group in table.groups:
                if not group.label:
                    continue
                lines.append("| {} | {} | R{}:R{} | {} |".format(
                    table.sheet, group.label, group.row_start, group.row_end,
                    "、".join(group.members) or "—",
                ))
        lines.append("")

    if described:
        lines += ["## 表格清单", ""]
        for table in described:
            path = table_paths[table.table_id]
            rows = table.row_end - table.row_start + 1
            columns = table.column_end - table.column_start + 1
            lines.append(
                "- table_id `{}` — 工作表 `{}`，{}×{}，status `{}`，"
                "导航页 `{}`；完整逻辑表候选路径 `{}`，文件不存在时请查 `articles/tables/index.md` 或回查原文。".format(
                    table.table_id, table.sheet, rows, columns, table.status, path,
                    full_table_path(source_id,table.table_id)))
        lines.append("")

    lines += [
        "## 怎么取数",
        "",
        "| 需要什么 | 用什么 |",
        "|---|---|",
        "| 指定字段或位置的原始取值 | 打开对应表格页，核对表头路径、行列坐标与原始值；存在日期或行分组时再使用相应导航 |",
        "| 完整逻辑表 | 按表格清单直接读取对应文件，保留表头、单位和附注 |",
        "| 精确计数或求和 | 先读取相关完整表，依据明确的范围和条件计算 |",
        "",
    ]
    body = "\n".join(lines) + "\n"

    accepted = [table for table in described if table.status == "accepted"]
    capabilities = ["read_file", "search_files"]
    limitations = ["navigation_only", "skeleton_only", "no_cell_values", "no_aggregation"]

    entry = PageIndexEntry(
        path=page_path,
        title=(source_name or source_id)[:200],
        aliases=[value for value in [document_id] if value],
        tags=["workbook", "spreadsheet", "navigation-only"],
        description=("本来源含 {} 个工作表或原文分区、{} 张逻辑表。分区：{}。{}"
                     "进入后按来源区域、表头和坐标选择原表。").format(
            getattr(outline, "sheet_count", 0) or len({table.sheet for table in described}), len(described),
            "、".join(list(dict.fromkeys(table.sheet for table in described))[:6]) or "未记录",
            ("原表分组：" + "、".join(list(dict.fromkeys(
                group.label for table in described for group in table.groups if group.label))[:8]) + "。")
            if any(table.groups for table in described) else ""),
        object_id="workbook-" + source_id,
        scope={
            "document_id": document_id,
            "source_sha256": source_sha256,
            "sheet_count": int(getattr(outline, "sheet_count", 0) or 0),
            "table_count": len(described),
        },
        fields=[],
        why_visit="Inspect the saved sheet/table inventory, header paths and source "
                  "coordinates. Then read linked table pages for values and notes.",
        limitations=limitations,
        capabilities=capabilities,
        interpretation_status="accepted" if accepted else "partial",
    )
    links = [{
        "target_id": source_path,
        "why_visit": "Verify this skeleton against its source identity and original "
                     "artifact.",
        "evidence_refs": [{"source_id": source_id, "source_sha256": source_sha256}],
        "kind": "navigation",
    }]
    for table in described:
        links.append({
            "target_id": table_paths[table.table_id],
            "why_visit": "Read the exact cells this skeleton only outlines.",
            "evidence_refs": [{"source_id": source_id, "source_sha256": source_sha256}],
            "kind": "navigation",
        })
    snapshot = WikiPageSnapshot(
        index=entry,
        content=body,
        # Declared here so the page is resolvable to its own source evidence:
        # a page that cannot name the intervals it was built from yields no
        # citable evidence at read time, however authoritative it looks.
        source_ids=[source_id],
        related_pages=[source_path] + linked_paths,
        links=links,
    )
    return entry, snapshot, body


def build_source_navigation_page(
    *,
    source_id: str,
    source_name: str,
    document_id: str,
    source_sha256: str,
    parser_id: str,
    content_generation: str,
    source_path: str,
    table_paths: List[str],
    table_status: Dict[str, str],
    table_titles: Optional[Dict[str, str]] = None,
) -> tuple[PageIndexEntry, WikiPageSnapshot, str]:
    """One deterministic navigation page for a source and its table edges."""
    from jonex_wiki.domain.engine.compile.table_navigation import full_table_path
    article_path = "sources/articles/{}.md".format(source_id)
    body = (
        "# {}\n\nSource ID: `{}`\n\nDocument: `{}`\n\n"
        "Source SHA256: `{}`\n\nParser: `{}`\n\nContent generation: `{}`\n\n"
        "来源全文：`{}`。继续读取对应原文片段；来源摘要和目录本身不是答案证据。"
        "涉及表格时请直接读取下面列出的完整逻辑表文件。\n\n"
    ).format(
        source_name, source_id, document_id, source_sha256, parser_id,
        content_generation, article_path,
    )
    if table_paths:
        rows = []
        for value, path in zip(sorted(table_status), table_paths):
            status = table_status[value]
            title = str((table_titles or {}).get(value, "") or "")
            title_note = "（标题 `{}`）".format(title) if title else ""
            if status == "accepted":
                rows.append(
                    "- table_id `{}`{} — 编译状态 `accepted`；完整逻辑表候选路径 `{}`（存在时直接读，不存在时查 `articles/tables/index.md` 或回查原文）；"
                    "表格导航页 `{}`。".format(
                        value, title_note, full_table_path(source_id,value), path
                    )
                )
            else:
                rows.append(
                    "- table_id `{}`{} — 编译状态 `{}`；完整逻辑表候选路径 `{}`（存在时直接读，不存在时查 `articles/tables/index.md` 或回查原文）；"
                    "导航页 `{}`。字段未确认时不按字段名统计。".format(
                        value, title_note, status, full_table_path(source_id,value), path
                    )
                )
        body += "## Tables\n" + "\n".join(rows) + "\n"
        capabilities = ["read_file", "open_original"]
    else:
        body += (
            "## Tables\n\n_本来源没有已发布的逻辑表。_ 请直接读取来源文件；"
            "原件附件在 `sources/registry.json`。\n"
        )
        capabilities = ["read_file", "open_original"]
    entry = PageIndexEntry(
        path=source_path,
        title=source_name[:200],
        tags=["source", "navigation-only"],
        description="Source navigation page; read linked evidence before answering.",
        object_id=source_id,
        scope={"document_id": document_id, "source_sha256": source_sha256},
        why_visit="Browse every published object and exact evidence interval from this source.",
        limitations=["navigation_only", "read_original_evidence_before_answering"],
        capabilities=capabilities,
    )
    snapshot = WikiPageSnapshot(
        index=entry,
        content=body,
        source_ids=[source_id],
        related_pages=table_paths,
        links=[{
            "target_id": target,
            "why_visit": "Inspect a logical table published from this source.",
            "evidence_refs": [{"source_id": source_id, "source_sha256": source_sha256}],
            "kind": "navigation",
        } for target in table_paths],
    )
    return entry, snapshot, body


def _literal_source_block(text: str) -> str:
    """Keep source Markdown literal; only our added links are navigation."""
    runs = [len(value) for value in re.findall(r"`+", text)]
    fence = "`" * max(3, max(runs, default=0) + 1)
    return fence + "text\n" + text + "\n" + fence + "\n"


_PAGE_SOURCE_IDS_RE = re.compile(r"^source_ids:\s*(\[.*\])\s*$", re.MULTILINE)


def page_source_ids_from_markdown(markdown: str) -> List[str]:
    """Passage ids a rendered page declares in its own frontmatter.

    Only the leading YAML block is inspected, and only its machine-written
    ``source_ids`` line (a single-line JSON array).  Used when a corpus is
    rebuilt from an already-published bundle, where the page snapshots are no
    longer in memory and the rendered frontmatter is the only surviving record
    of which intervals the page was compiled from.  Unparseable frontmatter
    yields no ids rather than an error: the fallback is best-effort.
    """
    if not markdown.startswith("---"):
        return []
    fence = markdown.find("\n---", 3)
    if fence < 0:
        return []
    match = _PAGE_SOURCE_IDS_RE.search(markdown, 0, fence)
    if match is None:
        return []
    try:
        values = json.loads(match.group(1))
    except ValueError:
        return []
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, str) and value.strip()]


def build_evidence_fallback(
    page_source_ids: Mapping[str, Sequence[str]],
    source_records: Mapping[str, List[Dict[str, str]]],
    *,
    skip_paths: Iterable[str] = (),
) -> Dict[str, List[str]]:
    """Map a claim-less generated page to the source pages behind it.

    A page whose claims were never admitted (``support_status != SUPPORTED``)
    contributes no claim evidence, so ``_execute_read`` of that page alone
    leaves the mandatory-read gate closed — even though the exact intervals it
    was compiled from are archived and stay raw-readable under
    ``keep-and-search-v1``.  Indexing the source-unit page carrying each of the
    page's own passages lets the read path deliver those records instead.

    Pages that already have admitted claims, source pages themselves, and pages
    that declare no readable passage are omitted, so corpora built from healthy
    bundles keep byte-identical behaviour.
    """
    skip = set(skip_paths)
    passage_spans: Dict[str, str] = {}
    for span_path, records in source_records.items():
        for record in records:
            passage_id = str(record.get("passage_id") or "")
            if passage_id:
                passage_spans.setdefault(passage_id, span_path)
    fallback: Dict[str, List[str]] = {}
    for path, source_ids in page_source_ids.items():
        if path in skip or path in source_records:
            continue
        spans: List[str] = []
        for source_id in source_ids:
            span_path = passage_spans.get(str(source_id))
            if span_path and span_path not in spans:
                spans.append(span_path)
        if spans:
            fallback[path] = spans
    return fallback


def build_query_corpus(
    state: CompilerState,
    claims: List[ValidatedClaim],
    *,
    error_book=None,
    include_sources: bool = False,
    validated: bool = False,
    source_unit_rule_version: str | None = None,
    support_rule_version: str | None = None,
    extra_table_navigation: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    raw_pages: Any = (),
    workbook_outlines: Optional[Dict[str, Any]] = None,
    previous_corpus: QueryCorpus | None = None,
    previous_source_unit_rule_version: str | None = None,
    previous_render_signatures: Dict[str, str] | None = None,
    render_signatures_out: Dict[str, str] | None = None,
    rendered_pages_out: set[str] | None = None,
) -> QueryCorpus:
    """Materialize generated pages, original text and raw tables for search.

    The new default (``keep-and-search-v1``) keeps every generated Wiki body
    verbatim, adds every decodable source interval and every supplied raw page,
    and links generated pages to the origins that already exist.  Nothing is
    deleted because a Claim is missing, unsupported or quarantined: semantic
    findings are recorded in the Error Book, never turned into a text gate.

    ``source_unit_rule_version`` in the legacy evidence-disposition versions
    routes to ``_build_query_corpus_legacy`` so old bundles cold-open unchanged.
    ``raw_pages`` carries prepared full-article and complete-table text pages;
    it must be collected before this call, not appended after the FTS index is
    built.  ``workbook_outlines`` carries one pre-collected skeleton per native
    spreadsheet, from which this call renders a concept page per workbook.
    """
    from jonex_wiki.kernel.telemetry import count_work
    from jonex_wiki.domain.engine.compile.publication import (
        KEEP_AND_SEARCH_RULE_VERSION,
        claim_allowed,
        is_old_rule,
        quarantine_scope,
        source_units,
    )

    if is_old_rule(source_unit_rule_version):
        return _build_query_corpus_legacy(
            state,
            claims,
            error_book=error_book,
            include_sources=include_sources,
            validated=validated,
            source_unit_rule_version=source_unit_rule_version,
            support_rule_version=support_rule_version,
            extra_table_navigation=extra_table_navigation,
        )

    count_work("query_corpus_build")
    raw_pages = [raw if isinstance(raw, RawWikiPage) else RawWikiPage.model_validate(raw) for raw in raw_pages]
    if not validated:
        validate_query_claims(
            state,
            claims,
            tolerate_support_drift=not _support_rule_is_current(support_rule_version),
        )
    scope = quarantine_scope(error_book)

    # Claims keep their existing validation state for legacy citation readers.
    # A missing/unsupported claim never removes a page or source.
    grouped: Dict[str, List[ValidatedClaim]] = defaultdict(list)
    for claim in claims:
        if claim_allowed(claim, scope):
            grouped[claim.page_path].append(claim)

    pages: Dict[str, WikiPageSnapshot] = {}
    index: Dict[str, PageIndexEntry] = {}
    from jonex_wiki.domain.engine.source_scope import build_source_scopes, attach_source_scopes, is_overview
    source_scopes = build_source_scopes(state.source_archive,
        (claim for items in grouped.values() for claim in items))
    overview_paths = defaultdict(list)
    for path, snapshot in state.pages.items():
        entry = snapshot.index.model_copy(update={
            "scope": {**snapshot.index.scope, "content_kind": "generated"},
        })
        pages[path] = snapshot.model_copy(update={"index": entry})
        index[path] = entry
        if is_overview(entry.title):
            for sid in snapshot.source_ids:
                overview_paths[sid].append((path, entry.title))

    source_records: Dict[str, List[Dict[str, str]]] = {}
    source_locations: Dict[str, Dict[str, Any]] = {}
    raw_paths_by_table: Dict[tuple[str, str], List[str]] = defaultdict(list)
    full_paths_by_table: Dict[tuple[str, str], str] = {}

    if include_sources:
        delta = getattr(state, '_compile_delta', None)
        reusable_documents = (delta.unchanged_documents if delta is not None
            and previous_corpus is not None
            and previous_source_unit_rule_version == KEEP_AND_SEARCH_RULE_VERSION else frozenset())
        units = []
        if reusable_documents:
            for path, records in previous_corpus.source_records.items():
                if len(records) != 1:
                    continue
                record = records[0]
                if record.get('document_id') in reusable_documents:
                    passage = state.source_archive[record['passage_id']]
                    units.append((path, passage, record, previous_corpus.source_locations[path]))
            count_work('query_source_units_reused',len(units))
        changed_state = state.model_copy(update={'source_archive':{
            pid:passage for pid,passage in state.source_archive.items()
            if passage.document_id not in reusable_documents}}) if reusable_documents else state
        units.extend(source_units(changed_state,error_book,unit_rule_version=KEEP_AND_SEARCH_RULE_VERSION))
        # Keep the same passage/interval order as a fresh publication, including
        # origin-link order. Only a fully verified identical document is reused.
        units.sort(key=lambda unit:(unit[1].passage_id,unit[3]['start_char']))
        for path, passage, record, location in units:
            if path in pages:
                raise ValueError("SOURCE_EVIDENCE_PATH_COLLISION")
            source_records[path] = [record]
            source_locations[path] = location
            name = str(passage.source_metadata.get("source_name") or passage.document_id)
            entry = PageIndexEntry(
                path=path,
                title=("Source: " + name)[:200],
                description="Exact source interval; not a generated claim.",
                scope={"content_kind": "source"},
            )
            body = _literal_source_block(record["excerpt"])
            index[path] = entry
            pages[path] = WikiPageSnapshot(index=entry, content=body,
                                           source_ids=[passage.source_id])

        for raw in raw_pages:
            raw = raw if isinstance(raw, RawWikiPage) else RawWikiPage.model_validate(raw)
            if raw.path in pages:
                raise ValueError("RAW_PAGE_PATH_COLLISION: " + raw.path)
            scope_payload: Dict[str, Any] = {
                **raw.scope,
                "content_kind": raw.content_kind,
                "source_id": raw.source_id,
            }
            if raw.table_id is not None:
                scope_payload["table_id"] = raw.table_id
            if raw.row_bounds is not None:
                scope_payload["row_bounds"] = raw.row_bounds
            entry = PageIndexEntry(
                path=raw.path,
                title=raw.title or ("Raw table " + str(raw.table_id) if raw.table_id else raw.source_id),
                aliases=list(raw.aliases),
                tags=list(raw.tags),
                description=(' / '.join(raw.scope.get('section_titles',[]))[:200]
                             if raw.scope.get('reading_role')=='article' else ''),
                object_id=raw.table_id or raw.source_id,
                scope=scope_payload,
                limitations=["raw_text_only", "not_a_validated_claim"],
                capabilities=["read_file"],
                interpretation_status=raw.structure_status,
            )
            index[raw.path] = entry
            pages[raw.path] = WikiPageSnapshot(
                index=entry,
                content=(raw.body if raw.scope.get('reading_role')=='article' else _literal_source_block(raw.body)),
                source_ids=[raw.source_id],
                related_pages=list(raw.related_pages),
            )
            if raw.content_kind == "table_raw" and raw.table_id is not None:
                identity = (raw.source_id, raw.table_id)
                raw_paths_by_table[identity].append(raw.path)
                if raw.scope.get("full_table_path"):
                    full_paths_by_table[identity] = raw.scope["full_table_path"]

        # Deterministic navigation pages are still built from parser identity.
        by_source: Dict[str, List[ArchivedPassage]] = defaultdict(list)
        for passage in state.source_archive.values():
            by_source[passage.source_id].append(passage)
        table_navigation_paths: Dict[tuple[str, str], str] = {}
        used_table_paths: set[str] = set(pages)

        def table_navigation_path(source_id: str, table_id: str) -> str:
            identity = (source_id, table_id)
            if identity in table_navigation_paths:
                return table_navigation_paths[identity]
            try:
                preferred = normalize_wiki_path("tables/{}.md".format(table_id))
            except ValueError:
                preferred = "tables/table-{}.md".format(
                    hashlib.sha256((source_id + "\0" + table_id).encode()).hexdigest()[:20]
                )
            if preferred in used_table_paths:
                preferred = "tables/table-{}.md".format(
                    hashlib.sha256((source_id + "\0" + table_id).encode()).hexdigest()[:20]
                )
            used_table_paths.add(preferred)
            table_navigation_paths[identity] = preferred
            return preferred

        extra_tables = extra_table_navigation or {}
        all_source_ids = sorted(set(by_source) | set(extra_tables))

        for source_id in all_source_ids:
            passages = sorted(
                by_source.get(source_id, []),
                key=lambda item: (item.position or 0, item.passage_id),
            )
            specs = list(extra_tables.get(source_id, []))
            short = hashlib.sha256(source_id.encode()).hexdigest()[:16]
            source_path = "sources/source-{}.md".format(short)
            table_status: Dict[str, str] = {}
            if passages:
                first = passages[0]
                name = str(first.source_metadata.get("source_name") or first.document_id)
                document_id = first.document_id
                source_sha256 = first.sha256
                parser_id = str(first.source_metadata.get("parser_id", "unknown"))
                content_generation = str(first.source_metadata.get("content_generation", "unknown"))
                for item in passages:
                    value = item.source_metadata.get("table_id")
                    if value:
                        table_status.setdefault(
                            str(value),
                            str(item.source_metadata.get("interpretation_status") or "unknown"),
                        )
            else:
                first_spec = specs[0]
                name = str(first_spec.get("source_name") or first_spec.get("document_id"))
                document_id = str(first_spec.get("document_id") or "")
                source_sha256 = str(first_spec.get("source_sha256") or "")
                parser_id = str(first_spec.get("parser_id", "unknown"))
                content_generation = str(first_spec.get("content_generation", "unknown"))
            for spec in specs:
                table_status.setdefault(
                    str(spec["table_id"]),
                    str(spec.get("interpretation_status") or "unknown"),
                )
            table_titles: Dict[str, str] = {}
            for item in passages:
                tid = item.source_metadata.get("table_id")
                if tid:
                    table_titles.setdefault(
                        str(tid),
                        item.text.splitlines()[0].lstrip("# ").strip(),
                    )
            for spec in specs:
                table_titles.setdefault(
                    str(spec["table_id"]),
                    str(spec.get("title") or "").splitlines()[0].lstrip("# ").strip(),
                )
            table_ids = sorted(table_status)
            table_paths = [table_navigation_path(source_id, value) for value in table_ids]
            source_entry, source_snapshot, source_body = build_source_navigation_page(
                source_id=source_id, source_name=name, document_id=document_id,
                source_sha256=source_sha256, parser_id=parser_id,
                content_generation=content_generation, source_path=source_path,
                table_paths=table_paths, table_status=table_status,
                table_titles=table_titles)
            if overview_paths[source_id]:
                links = '\n'.join('- [[{}|{}]]'.format(path, title)
                                  for path, title in sorted(overview_paths[source_id]))
                source_snapshot = source_snapshot.model_copy(update={
                    'content': source_body + '\n\n## Overview entry points\n\n'
                    + 'Read the linked supported facts for the requested scope; these links are not a complete summary.\n'
                    + links + '\n'})
            index[source_path] = source_entry
            scope_info = source_scopes.get(source_id, {})
            source_snapshot = source_snapshot.model_copy(update={'content':
                source_snapshot.content + '\n\n## Source scope and version\n\n'
                + json.dumps(scope_info, ensure_ascii=False, sort_keys=True) + '\n'})
            pages[source_path] = source_snapshot
            # Each table page lists the source's other tables as scoped
            # candidates, so an article's table reference can be checked by
            # identity instead of assumed from the surrounding document.
            specs_by_table = {str(spec["table_id"]): spec for spec in specs}

            def _siblings(exclude: str) -> List[Dict[str, Any]]:
                listed = []
                for candidate_id in table_ids:
                    if candidate_id == exclude:
                        continue
                    spec = specs_by_table.get(candidate_id) or {}
                    listed.append({
                        "table_id": candidate_id,
                        "title": table_titles.get(candidate_id, ""),
                        "status": table_status.get(candidate_id),
                        "scope": dict(spec.get("scope") or
                                      {"bounds": spec.get("bounds") or {}}),
                    })
                return listed
            # Inputs for the reading views built after both table passes: the
            # view needs the grid the page previews, and the specs pass wins
            # over the passage pass exactly as it does for the page itself.
            view_inputs: Dict[str, Dict[str, Any]] = {}
            for passage in passages:
                table_id = passage.source_metadata.get("table_id")
                if not table_id:
                    continue
                table_path = table_navigation_path(source_id, str(table_id))
                raw_title = passage.text.splitlines()[0].lstrip("# ").strip()
                fields = list(passage.source_metadata.get("fields") or [])
                title = human_table_title(raw_title, fields, fallback=str(table_id))
                status = str(passage.source_metadata.get("interpretation_status") or "unknown")
                table_entry, table_snapshot, table_body = build_table_navigation_page(
                    source_id=source_id, source_name=name, document_id=document_id,
                    source_sha256=passage.sha256, table_id=str(table_id), title=title,
                    interpretation_status=status,
                    fields=fields,
                    scope=dict(passage.source_metadata.get("scope") or {
                        "bounds": passage.source_metadata.get("bounds", [])}),
                    raw_text=passage.text, source_path=source_path, table_path=table_path,
                    title_aliases=[raw_title] if raw_title else [],
                    siblings=_siblings(str(table_id)),
                    complete_table=full_paths_by_table.get((source_id, str(table_id))))
                index[table_path] = table_entry
                pages[table_path] = table_snapshot
                view_inputs[table_path] = {
                    "title": title, "raw_text": passage.text, "status": status,
                    "sha256": passage.sha256,
                    "partial": _bounded_preview(passage.text)[1],
                }
            for spec in specs:
                table_id = str(spec["table_id"])
                table_path = table_navigation_path(source_id, table_id)
                raw_title = (str(spec.get("title") or "").splitlines()[0]
                             .lstrip("# ").strip())
                fields = [str(value) for value in (spec.get("fields") or [])]
                title = human_table_title(raw_title, fields, fallback=table_id)
                status = str(spec.get("interpretation_status") or "unknown")
                table_entry, table_snapshot, table_body = build_table_navigation_page(
                    source_id=source_id, source_name=name, document_id=document_id,
                    source_sha256=str(spec.get("source_sha256") or source_sha256),
                    table_id=table_id, title=title,
                    interpretation_status=status,
                    fields=fields,
                    scope=dict(spec.get("scope") or {"bounds": dict(spec.get("bounds") or {})}),
                    raw_text=str(spec.get("raw_text") or ""),
                    source_path=source_path, table_path=table_path,
                    title_aliases=[raw_title] if raw_title else [],
                    siblings=_siblings(table_id),
                    complete_table=full_paths_by_table.get((source_id, table_id)))
                index[table_path] = table_entry
                pages[table_path] = table_snapshot
                view_inputs[table_path] = {
                    "title": title, "raw_text": str(spec.get("raw_text") or ""),
                    "status": status,
                    "sha256": str(spec.get("source_sha256") or source_sha256),
                    "partial": _bounded_preview(str(spec.get("raw_text") or ""))[1],
                }

            # Reading views: one small page per organising logic the table
            # supports ("按交易所看", "按品种看"), linked both ways with the table
            # page.  Deterministic and cheap — structure only, no model call.
            for table_path, view_input in view_inputs.items():
                if table_path not in pages:
                    continue
                built_views = build_view_pages(
                    table_path=table_path, table_title=view_input["title"],
                    source_id=source_id, source_sha256=view_input["sha256"],
                    source_path=source_path, source_name=name,
                    raw_text=view_input["raw_text"],
                    interpretation_status=view_input["status"],
                    already_taken=tuple(pages),
                    preview_truncated=bool(view_input.get("partial")))
                if not built_views:
                    continue
                view_links = [(view_entry.path, view_entry.title)
                              for view_entry, _snapshot, _content in built_views]
                for view_entry, view_snapshot, _content in built_views:
                    index[view_entry.path] = view_entry
                    pages[view_entry.path] = view_snapshot
                table_snapshot = pages[table_path]
                pages[table_path] = table_snapshot.model_copy(update={
                    "related_pages": list(table_snapshot.related_pages)
                                    + [path for path, _title in view_links],
                    "content": view_link_line(view_links) + "\n\n" + table_snapshot.content,
                })

            # One concept page per native workbook, so the source contributes
            # orientation and not only a grid.  It is rendered from the
            # pre-collected full-grid outline, never from the bounded parser
            # preview, and links every table page both ways.
            outline = (workbook_outlines or {}).get(source_id) if workbook_outlines else None
            if outline is not None:
                # Keyed by table_id, never zipped by position: ``table_paths``
                # follows sorted table ids while an outline follows sheet and
                # row order, so a positional pairing silently mis-links every
                # table whose two orders disagree.
                path_by_table = dict(zip(table_ids, table_paths))
                described = {
                    str(table.table_id): path_by_table[str(table.table_id)]
                    for table in (getattr(outline, "tables", ()) or ())
                    if str(getattr(table, "table_id", "")) in path_by_table
                }
                concept_path = workbook_concept_path(source_id)
                if described and concept_path not in pages:
                    concept_entry, concept_snapshot, _body = build_workbook_concept_page(
                        outline=outline, source_id=source_id, source_name=name,
                        document_id=document_id, source_sha256=source_sha256,
                        source_path=source_path, page_path=concept_path,
                        table_paths=described)
                    index[concept_path] = concept_entry
                    pages[concept_path] = concept_snapshot
                    for table_path in sorted(set(described.values())):
                        if table_path not in pages:
                            continue
                        snapshot = pages[table_path]
                        if concept_path in snapshot.related_pages:
                            continue
                        pages[table_path] = snapshot.model_copy(update={
                            "related_pages": list(snapshot.related_pages) + [concept_path]})
                    if concept_path not in pages[source_path].related_pages:
                        pages[source_path] = pages[source_path].model_copy(update={
                            "related_pages": list(pages[source_path].related_pages)
                                            + [concept_path]})

        # Raw table pages link to each other and to their table navigation page.
        for (source_id, table_id), raw_paths in raw_paths_by_table.items():
            ordered = sorted(raw_paths)
            nav_path = table_navigation_paths.get((source_id, table_id))
            for position, path in enumerate(ordered):
                related = list(pages[path].related_pages)
                if position > 0:
                    related.append(ordered[position - 1])
                if position + 1 < len(ordered):
                    related.append(ordered[position + 1])
                if nav_path:
                    related.append(nav_path)
                pages[path] = pages[path].model_copy(update={"related_pages": related})
            if nav_path and nav_path in pages:
                import posixpath
                links = [
                    "- [part {}]({})".format(
                        position,
                        posixpath.relpath(path, posixpath.dirname(nav_path)),
                    )
                    for position, path in enumerate(ordered)
                ]
                nav_snapshot = pages[nav_path]
                pages[nav_path] = nav_snapshot.model_copy(update={
                    "content": nav_snapshot.content + "\n\n## Raw table pages\n\n"
                                + "\n".join(links) + "\n",
                })

        # Unclassified topic page mirrors the legacy navigation registry.
        if by_source:
            topic_path = "topics/unclassified.md"
            topic_entry = PageIndexEntry(
                path=topic_path,
                title="Unclassified",
                tags=["topic", "navigation-only"],
                description="Published objects without an accepted semantic topic.",
                object_id="topic-unclassified",
                scope={
                    "source_count": len(by_source),
                    "table_count": sum(
                        1 for path in pages if path.startswith("tables/")
                    ),
                },
                why_visit="Use the complete Sources and Tables registries when semantic classification is unavailable.",
                limitations=["navigation_only", "semantic_classification_unavailable"],
                capabilities=["read_file", "search_files"],
                interpretation_status="unclassified",
            )
            registries = ["[[sources/index.md|All Sources]]"]
            if any(path.startswith("tables/") for path in pages):
                registries.append("[[tables/index.md|All Tables]]")
            topic_body = (
                "Objects in this topic have not received an accepted semantic "
                "classification. Nothing is omitted. Browse " + " or ".join(registries)
                + ", then verify original evidence before answering."
            )
            index[topic_path] = topic_entry
            pages[topic_path] = WikiPageSnapshot(index=topic_entry, content=topic_body)

    # Origin links are best-effort and only use associations already present.
    attach_source_scopes(pages, source_scopes)
    if include_sources and source_scopes:
        path = 'topics/overview.md'
        links = '\n'.join('- [[sources/source-{}.md|{}]]'.format(
            hashlib.sha256(sid.encode()).hexdigest()[:16], scope['source_name'])
            for sid, scope in sorted(source_scopes.items()))
        entry = PageIndexEntry(path=path, title='Source overviews and versions',
            tags=['overview', 'navigation-only'], scope={'content_kind': 'navigation'},
            limitations=['navigation_only', 'source_period_is_not_fact_period'])
        pages[path] = WikiPageSnapshot(index=entry, content=(
            'Browse source-bound overview entry points and their versions. Read linked facts before answering.\n\n' + links))
    attach_origin_links(pages, state, claims, source_records, source_locations)
    from jonex_wiki.domain.engine.compile.chapter_pages import attach_reading_targets, attach_article_catalog
    attach_reading_targets(pages, claims, raw_pages, state.source_archive, source_records, source_locations)
    attach_article_catalog(pages,raw_pages)
    index = {path: pages[path].index for path in pages}

    # Rendering a detached state keeps navigation/backlinks intact while
    # ensuring every prose surface visible to wiki_search/wiki_read was rebuilt
    # from the full pages, not from supported claims only.
    grounded_state = CompilerState(
        batch_number=state.batch_number,
        pages=pages,
        index=index,
        source_archive={},
        processed_passage_ids=[],
        active_constraints=[],
        directory_shards=dict(state.directory_shards),
    )
    if render_signatures_out is not None:
        from jonex_wiki.domain.engine.compile.rendering import render_wiki_tree_incremental
        tree, signatures, changed_pages = render_wiki_tree_incremental(
            grounded_state,
            previous_corpus.files if previous_corpus is not None else None,
            previous_render_signatures,
        )
        render_signatures_out.update(signatures)
        if rendered_pages_out is not None:
            rendered_pages_out.update(changed_pages)
    else:
        tree = render_wiki_tree(grounded_state)
    files: Dict[str, str] = {}
    for key, blob in tree.items():
        if not key.startswith("wiki/"):
            raise ValueError("QUERY_CORPUS_UNEXPECTED_ARTIFACT")
        files[key[len("wiki/"):]] = blob.decode("utf-8")

    if include_sources and source_scopes and 'index.md' in files:
        # Incremental rendering may reuse the previous index body. Replace our
        # own suffix instead of appending another copy on every publication.
        marker = '\n\n## Source overviews and scope\n'
        files['index.md'] = files['index.md'].split(marker, 1)[0] + marker + '\n[[topics/overview.md|Source overviews and versions]]\n'
    page_text = {path: files[path] for path in index}
    page_number_map = derive_page_number_map(state, claims)
    evidence_fallback = build_evidence_fallback(
        {path: snapshot.source_ids for path, snapshot in pages.items()},
        source_records,
        skip_paths=set(grouped),
    )

    return QueryCorpus(
        files=files,
        page_index=index,
        page_text=page_text,
        claims=dict(grouped),
        page_number_map=page_number_map,
        source_records=source_records,
        source_locations=source_locations,
        source_scopes=source_scopes,
        evidence_fallback=evidence_fallback,
    )


def _build_query_corpus_legacy(
    state: CompilerState,
    claims: List[ValidatedClaim],
    *,
    error_book=None,
    include_sources: bool = False,
    validated: bool = False,
    source_unit_rule_version: str | None = None,
    support_rule_version: str | None = None,
    extra_table_navigation: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> QueryCorpus:
    """Legacy evidence-disposition corpus, preserved for old assets."""
    from jonex_wiki.kernel.telemetry import count_work
    from jonex_wiki.domain.engine.compile.publication import claim_allowed, quarantine_scope, source_units
    count_work("query_corpus_build")
    if not validated:
        validate_query_claims(
            state,
            claims,
            tolerate_support_drift=not _support_rule_is_current(support_rule_version),
        )
    scope = quarantine_scope(error_book)
    grouped: Dict[str, List[ValidatedClaim]] = defaultdict(list)
    for claim in claims:
        if not claim_allowed(claim, scope):
            continue
        grouped[claim.page_path].append(claim)

    grounded_pages: Dict[str, WikiPageSnapshot] = {}
    grounded_index: Dict[str, PageIndexEntry] = {}
    page_text: Dict[str, str] = {}
    for path, snapshot in state.pages.items():
        if path in scope["pages"]:
            continue
        page_claims = grouped.get(path, [])
        if include_sources and not page_claims:
            continue
        seen_text = set()
        supported_texts: List[str] = []
        for claim in page_claims:
            if claim.text not in seen_text:
                seen_text.add(claim.text)
                supported_texts.append(claim.text)
        content = "\n".join(supported_texts)
        entry = PageIndexEntry(
            path=path,
            title=snapshot.index.title,
            aliases=list(snapshot.index.aliases),
            tags=list(snapshot.index.tags),
            description=" ".join(supported_texts),
            object_id=snapshot.index.object_id,
            scope=dict(snapshot.index.scope),
            fields=list(snapshot.index.fields),
            why_visit=snapshot.index.why_visit,
            limitations=list(snapshot.index.limitations),
            capabilities=list(snapshot.index.capabilities),
            interpretation_status=snapshot.index.interpretation_status,
        )
        grounded_index[path] = entry
        grounded_pages[path] = WikiPageSnapshot(
            index=entry,
            content=content,
            related_pages=list(snapshot.related_pages),
            links=list(snapshot.links),
            source_ids=list(snapshot.source_ids),
            version=snapshot.version,
        )
        page_text[path] = content

    source_records = {}
    source_locations = {}
    if include_sources:
        for path, passage, record, location in source_units(
            state, error_book, unit_rule_version=source_unit_rule_version
        ):
            if path in grounded_pages:
                raise ValueError("SOURCE_EVIDENCE_PATH_COLLISION")
            source_records[path] = [record]
            source_locations[path] = location
            name = str(passage.source_metadata.get("source_name") or passage.document_id)
            entry = PageIndexEntry(path=path, title=("Source: " + name)[:200],
                                   description="Exact source interval; not a generated claim.")
            body = record["excerpt"]
            grounded_index[path] = entry
            grounded_pages[path] = WikiPageSnapshot(index=entry, content=body,
                                                    source_ids=[passage.source_id])
            page_text[path] = body

        # Deterministic navigation exists independently of Claim admission.
        # One source page and one page per logical table are built from parser
        # identity/structure only; they are searchable but navigation_only and
        # therefore never become citation evidence by themselves.
        by_source: Dict[str, List[ArchivedPassage]] = defaultdict(list)
        for passage in state.source_archive.values():
            if passage.source_id not in scope["sources"]:
                by_source[passage.source_id].append(passage)
        table_navigation_paths: Dict[tuple[str, str], str] = {}
        used_table_paths: set[str] = set()

        def table_navigation_path(source_id: str, table_id: str) -> str:
            """Return one stable canonical path per source/table identity."""

            identity = (source_id, table_id)
            if identity in table_navigation_paths:
                return table_navigation_paths[identity]
            try:
                preferred = normalize_wiki_path("tables/{}.md".format(table_id))
            except ValueError:
                preferred = "tables/table-{}.md".format(
                    hashlib.sha256((source_id + "\0" + table_id).encode()).hexdigest()[:20]
                )
            if preferred in used_table_paths:
                preferred = "tables/table-{}.md".format(
                    hashlib.sha256((source_id + "\0" + table_id).encode()).hexdigest()[:20]
                )
            used_table_paths.add(preferred)
            table_navigation_paths[identity] = preferred
            return preferred

        for source_id, passages in sorted(by_source.items()):
            first = sorted(passages, key=lambda item: (item.position or 0, item.passage_id))[0]
            short = hashlib.sha256(source_id.encode()).hexdigest()[:16]
            source_path = "sources/source-{}.md".format(short)
            name = str(first.source_metadata.get("source_name") or first.document_id)
            table_ids = sorted({str(item.source_metadata.get("table_id")) for item in passages
                                if item.source_metadata.get("table_id")})
            table_paths = [table_navigation_path(source_id, value) for value in table_ids]
            body = (
                "# {}\n\nSource ID: `{}`\n\nDocument: `{}`\n\n"
                "Source SHA256: `{}`\n\nParser: `{}`\n\nContent generation: `{}`\n\n"
                "Original and table artifacts: see `sources/registry.json` in this Bundle.\n\n"
            ).format(
                name, source_id, first.document_id, first.sha256,
                first.source_metadata.get("parser_id", "unknown"),
                first.source_metadata.get("content_generation", "unknown"),
            )
            if table_ids:
                body += "## Tables\n" + "\n".join("- `{}`".format(value) for value in table_ids) + "\n"
            entry = PageIndexEntry(path=source_path, title=name[:200], tags=["source", "navigation-only"],
                                   description="Source navigation page; read linked evidence before answering.",
                                   object_id=source_id,
                                   scope={"document_id": first.document_id,
                                          "source_sha256": first.sha256},
                                   why_visit="Browse every published object and exact evidence interval from this source.",
                                   limitations=["navigation_only", "read_original_evidence_before_answering"],
                                   capabilities=["read_file", "open_original"])
            grounded_index[source_path] = entry
            grounded_pages[source_path] = WikiPageSnapshot(
                index=entry, content=body, source_ids=[source_id],
                related_pages=table_paths,
                links=[{
                    "target_id": target,
                    "why_visit": "Inspect a logical table published from this source.",
                    "evidence_refs": [{"source_id": source_id, "source_sha256": first.sha256}],
                    "kind": "navigation",
                } for target in table_paths],
            )
            page_text[source_path] = body
            for passage in passages:
                table_id = passage.source_metadata.get("table_id")
                if not table_id:
                    continue
                table_path = table_navigation_path(source_id, str(table_id))
                title = passage.text.splitlines()[0].lstrip("# ").strip() or str(table_id)
                table_body = passage.text + "\n\n完整数据入口：`table_id={}`；先 inspect，再 locate/read。\n".format(table_id)
                table_entry = PageIndexEntry(path=table_path, title=title[:200], aliases=[str(table_id)],
                    tags=["table", "navigation-only"],
                    description="Logical table navigation page with source scope and read capability.",
                    object_id=str(table_id),
                    scope=dict(passage.source_metadata.get("scope") or {
                        "bounds": passage.source_metadata.get("bounds", [])}),
                    fields=list(passage.source_metadata.get("fields") or []),
                    why_visit="Inspect this logical table, locate exact values, then read the complete necessary range.",
                    limitations=["navigation_only", "preview_is_not_complete_table",
                                 "no_aggregation", "no_sort", "no_join", "no_formula_recalculation"],
                    capabilities=["read_file"],
                    interpretation_status=str(passage.source_metadata.get("interpretation_status") or "unknown"))
                grounded_index[table_path] = table_entry
                grounded_pages[table_path] = WikiPageSnapshot(
                    index=table_entry, content=table_body,
                    source_ids=[source_id], related_pages=[source_path],
                    links=[{
                        "target_id": source_path,
                        "why_visit": "Verify the table against its source identity and original artifact.",
                        "evidence_refs": [{"source_id": source_id, "source_sha256": passage.sha256}],
                        "kind": "navigation",
                    }],
                )
                page_text[table_path] = table_body

        # Source-level semantic classification is optional in Milestone 1.
        # Objects without an accepted topic are never dropped: a deterministic
        # Unclassified topic routes users to both complete type registries.
        if by_source:
            topic_path = "topics/unclassified.md"
            topic_entry = PageIndexEntry(
                path=topic_path,
                title="Unclassified",
                tags=["topic", "navigation-only"],
                description="Published objects without an accepted semantic topic.",
                object_id="topic-unclassified",
                scope={
                    "source_count": len(by_source),
                    "table_count": sum(
                        1 for path in grounded_pages if path.startswith("tables/")
                    ),
                },
                why_visit="Use the complete Sources and Tables registries when semantic classification is unavailable.",
                limitations=["navigation_only", "semantic_classification_unavailable"],
                capabilities=["read_file", "search_files"],
                interpretation_status="unclassified",
            )
            registries = ["[[sources/index.md|All Sources]]"]
            if any(path.startswith("tables/") for path in grounded_pages):
                registries.append("[[tables/index.md|All Tables]]")
            topic_body = (
                "Objects in this topic have not received an accepted semantic "
                "classification. Nothing is omitted. Browse " + " or ".join(registries)
                + ", then verify original evidence before answering."
            )
            grounded_index[topic_path] = topic_entry
            grounded_pages[topic_path] = WikiPageSnapshot(
                index=topic_entry, content=topic_body,
            )
            page_text[topic_path] = topic_body

    if include_sources:
        for path, page in list(grounded_pages.items()):
            grounded_pages[path] = page.model_copy(update={"related_pages": [p for p in page.related_pages if p in grounded_pages]})
        import posixpath
        sources_by_passage = defaultdict(list)
        for source_path, records in source_records.items():
            for record in records:
                sources_by_passage[record["passage_id"]].append(source_path)
        for page_path, page_claims in grouped.items():
            links = []
            for pid in sorted({r.passage_id for c in page_claims for r in c.evidence}):
                for source_path in sources_by_passage.get(pid, []):
                    loc = source_locations[source_path]
                    target = posixpath.relpath(source_path, posixpath.dirname(page_path))
                    links.append("- [" + pid + "](" + target + ") · SHA256 " + loc["source_sha256"]
                                 + " · characters " + str(loc["start_char"]) + ":" + str(loc["end_char"]))
            if links:
                page = grounded_pages[page_path]
                grounded_pages[page_path] = page.model_copy(update={"content": page.content + "\n\n## Evidence\n" + "\n".join(links)})

    # Rendering a detached state keeps navigation/backlinks intact while
    # ensuring every prose surface visible to wiki_search/wiki_read was rebuilt
    # from supported claims.  The immutable source archive is not needed here.
    grounded_state = CompilerState(
        batch_number=state.batch_number,
        pages=grounded_pages,
        index=grounded_index,
        source_archive={},
        processed_passage_ids=[],
        active_constraints=[],
        directory_shards=dict(state.directory_shards),
    )
    tree = render_wiki_tree(grounded_state)
    files: Dict[str, str] = {}
    for key, blob in tree.items():
        if not key.startswith("wiki/"):
            raise ValueError("QUERY_CORPUS_UNEXPECTED_ARTIFACT")
        files[key[len("wiki/"):]] = blob.decode("utf-8")

    # The published Markdown is the single authoritative query text.  Keeping
    # a second pre-render body copy made directory bundles rebuild the corpus
    # just to prove two representations were equal.
    page_text = {path: files[path] for path in grounded_index}

    # LWP-RET-002 D-5: page_number derived query-side from the original state's
    # source_metadata as an independent map (PageIndexEntry stays frozen).
    page_number_map = derive_page_number_map(state, claims)

    return QueryCorpus(
        files=files,
        page_index=grounded_index,
        page_text=page_text,
        claims=dict(grouped),
        page_number_map=page_number_map,
        source_records=source_records,
        source_locations=source_locations,
    )


def _generated_pages(pages: Dict[str, WikiPageSnapshot]) -> List[str]:
    """Generated Wiki pages, identified by their explicit content kind."""
    return [
        path for path, snapshot in pages.items()
        if snapshot.index.scope.get("content_kind") == "generated"
    ]


def _existing_origin_targets(
    page_path: str,
    claims: List[ValidatedClaim],
    pages: Dict[str, WikiPageSnapshot],
    source_records: Dict[str, List[Dict[str, str]]],
    source_locations: Dict[str, Dict[str, Any]],
    *,
    by_passage: Dict[str, List[str]] | None = None,
    page_claims: List[ValidatedClaim] | None = None,
) -> List[Dict[str, str]]:
    """Exact source-record targets already present for this page's claims."""
    targets: List[Dict[str, str]] = []
    seen: set[str] = set()
    if by_passage is None:
        by_passage = defaultdict(list)
        for path, records in source_records.items():
            for record in records:
                by_passage[record["passage_id"]].append(path)

    for claim in (claims if page_claims is None else page_claims):
        if claim.page_path != page_path:
            continue
        for ref in list(claim.evidence) + list(claim.unresolved_evidence):
            for path in by_passage.get(ref.passage_id, []):
                if path not in pages or path in seen:
                    continue
                location = source_locations.get(path, {})
                # A passage may produce dozens of source fragments. Only
                # intersections with validated claim spans are exact origins;
                # legacy/unknown intervals retain document navigation below.
                records = source_records.get(path, [])
                start, end = location.get('start_char'), location.get('end_char')
                spans = [span for span in claim.source_spans if span.passage_id==ref.passage_id]
                if (not isinstance(start,int) or not isinstance(end,int) or not any(
                    span.start_char < end and start < span.end_char
                    and any(record.get('source_id')==span.source_id
                            and record.get('document_id')==span.document_id
                            and record.get('chunk_id')==span.chunk_id for record in records)
                    for span in spans)):
                    continue
                suffix = "characters {}:{}".format(
                    location.get("start_char", "?"), location.get("end_char", "?"))
                targets.append({"path": path, "label": ref.passage_id, "suffix": suffix})
                seen.add(path)
    return targets


def _source_document_targets(
    source_ids: List[str],
    pages: Dict[str, WikiPageSnapshot],
) -> List[Dict[str, str]]:
    """Link to existing source navigation pages, never guessed by name."""
    targets: List[Dict[str, str]] = []
    seen: set[str] = set()
    for source_id in source_ids:
        short = hashlib.sha256(source_id.encode()).hexdigest()[:16]
        path = "sources/source-{}.md".format(short)
        if path in pages and path not in seen:
            targets.append({"path": path, "label": source_id, "suffix": ""})
            seen.add(path)
    return targets


def _append_markdown_links(snapshot: WikiPageSnapshot, targets: List[Dict[str, str]]) -> WikiPageSnapshot:
    import posixpath

    page_path = snapshot.index.path
    lines = []
    for target in targets:
        target_path = target["path"]
        rel = posixpath.relpath(target_path, posixpath.dirname(page_path))
        text = "- [{}]({})".format(target["label"], rel)
        if target.get("suffix"):
            text += " · " + target["suffix"]
        lines.append(text)
    if not lines:
        return snapshot
    return snapshot.model_copy(update={
        "content": snapshot.content + "\n\n## Sources\n" + "\n".join(lines) + "\n",
    })


def attach_origin_links(
    pages: Dict[str, WikiPageSnapshot],
    state: CompilerState,
    claims: List[ValidatedClaim],
    source_records: Dict[str, List[Dict[str, str]]],
    source_locations: Dict[str, Dict[str, Any]],
) -> None:
    """Append origin links to generated pages without inventing associations.

    Exact evidence intervals win; when only a source document is known the page
    links that document navigation page.  A page with no resolvable origin is
    kept and marked ``origin_unresolved`` rather than being dropped.
    """
    by_passage: Dict[str, List[str]] = defaultdict(list)
    for source_path, records in source_records.items():
        for record in records:
            by_passage[record["passage_id"]].append(source_path)
    claims_by_page: Dict[str, List[ValidatedClaim]] = defaultdict(list)
    for claim in claims:
        claims_by_page[claim.page_path].append(claim)
    for path in _generated_pages(pages):
        exact = _existing_origin_targets(
            path, claims, pages, source_records, source_locations,
            by_passage=by_passage, page_claims=claims_by_page.get(path, []))
        documents = _source_document_targets(pages[path].source_ids, pages)
        targets: List[Dict[str, str]] = []
        seen: set[str] = set()
        for target in exact + documents:
            if target["path"] in seen:
                continue
            seen.add(target["path"])
            targets.append(target)
        if targets:
            pages[path] = _append_markdown_links(pages[path], targets)
        else:
            entry = pages[path].index
            pages[path] = pages[path].model_copy(update={
                "index": entry.model_copy(update={
                    "limitations": list(entry.limitations) + ["origin_unresolved"],
                }),
            })
