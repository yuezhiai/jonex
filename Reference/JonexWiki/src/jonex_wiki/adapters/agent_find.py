"""Optional ranked discovery in the pinned publication, using its existing catalog.

The Agent runs this file as an ordinary local command inside its read-only sandbox.
Paths and previews are navigation. The original files remain the reading surface.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from itertools import zip_longest
import json
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import sys
import unicodedata
import zlib


CATALOG_KINDS = {"text": "unit", "relation": "relation", "table": "table"}


def install_command(package, bundle):
    """Stage the standalone query command in the Agent's read-only package."""
    script = package / "agent_find.py"
    shutil.copyfile(__file__, script)
    command = package / "wiki-find"
    arguments = shlex.join([sys.executable, "-E", "-s", "-B", str(script), str(bundle)])
    command.write_text('#!/bin/sh\nexec ' + arguments + ' "$@"\n', encoding="utf-8")
    command.chmod(0o500)
    return command


def _phrase(text):
    return '"' + text.replace('"', '""') + '"'


def _expressions(terms, match_any):
    expressions = []
    relaxed = []
    for term in terms:
        term = unicodedata.normalize("NFKC", term).strip()
        if not any(char.isalnum() for char in term):
            continue
        expression = _phrase(term)
        fallback = expression
        if re.search(r"[\u3400-\u9fff]", term):
            try:
                import jieba
            except ImportError:
                pass
            else:
                jieba.setLogLevel(40)
                words = [word for word in jieba.cut(term.casefold()) if word.isalnum()]
                if len(words) > 1:
                    exact = " AND ".join(map(_phrase, words))
                    expression = f"({expression} OR ({exact}))"
                    fallback = expression
                    # A query can end inside a word that the index saw in a
                    # longer string. Retry only the final Chinese segment as a
                    # prefix so the rest of the phrase still constrains recall.
                    if re.fullmatch(r"[\u3400-\u9fff]+", words[-1]):
                        fallback = f"({expression} OR ({exact}*))"
        expressions.append(expression)
        relaxed.append(fallback)
    operator = " OR " if match_any else " AND "
    return operator.join(expressions), operator.join(relaxed)


def _registered_path(root, entries, name):
    if not isinstance(name, str) or name not in entries:
        return None
    path = (root / name).resolve()
    return path if path.is_relative_to(root) and path.is_file() else None


def _claims(db, root, entries, expression, source, limit):
    rows = db.execute(
        """SELECT r.id,r.payload,l.span_no,l.start_char,l.end_char,s.title,s.path,o.path AS context
           FROM search
           JOIN records r ON r.ordinal=search.rowid
           JOIN record_locations l ON l.record=r.id AND l.status='located'
           JOIN objects s ON s.kind='source' AND s.id=l.source_id
           LEFT JOIN record_units u ON u.record=l.record AND u.span_no=l.span_no
           LEFT JOIN objects o ON o.kind='unit' AND o.id=u.unit
           WHERE search MATCH ? AND (? IS NULL OR s.id=? OR instr(lower(s.title),lower(?))>0)
           ORDER BY bm25(search,3,1),r.id,l.span_no,o.ordinal""",
        (expression, source, source, source),
    )
    hits, seen = [], set()
    for row in rows:
        identity = (row["id"], row["span_no"])
        if identity in seen or _registered_path(root, entries, row["path"]) is None:
            continue
        if len(hits) == limit:
            return {"available": True, "hits": hits, "more": True}
        record = json.loads(zlib.decompress(row["payload"]))
        span = record["source_spans"][row["span_no"]]
        lead = (span.get("text") or span.get("source_excerpt") or "")[:400]
        proposals = [
            [proposal[key][:160] for key in ("subject_mention", "relation", "target_mention")]
            for proposal in record.get("link_proposals", [])
            if all(isinstance(proposal.get(key), str) and proposal[key].strip()
                   for key in ("subject_mention", "relation", "target_mention"))
            and any(ref.get("source_id") == span.get("source_id")
                    and ref.get("passage_id") == span.get("passage_id")
                    and ref.get("excerpt") == (span.get("text") or span.get("source_excerpt"))
                    for ref in proposal.get("evidence_refs", []))
        ]
        context = row["context"] if _registered_path(root, entries, row["context"]) else None
        hits.append({"path": row["path"], "source": row["title"], "title": str(record.get("topic_hint") or "")[:160],
                     "origin_span": [row["start_char"], row["end_char"]], "lead": lead,
                     "context_path": context, "unverified_relation_leads": proposals[:6]})
        seen.add(identity)
    return {"available": True, "hits": hits, "more": False}


def _phrase_words(text):
    folded = unicodedata.normalize("NFKD", text.casefold())
    folded = "".join(char for char in folded if not unicodedata.combining(char))
    folded = re.sub(r"([\u3400-\u9fff])", r" \1 ", folded)
    return " " + " ".join(re.findall(r"[^\W_]+", folded)) + " "


def _matches_relation(fields, phrases, match_any):
    parts = tuple(_phrase_words(part) for part in fields if part)
    satisfied = any if match_any else all
    return bool(phrases) and satisfied(any(phrase in part for part in parts) for phrase in phrases)


def _contextual_claims(hits, phrases, match_any, limit):
    """Match a single scoped proposal before offering its original context."""
    qualified = []
    for hit in hits:
        matched = [proposal for proposal in hit["unverified_relation_leads"]
                   if _matches_relation(proposal, phrases, match_any)]
        if matched:
            qualified.append({**hit, "unverified_relation_leads": matched})
            if len(qualified) == limit:
                break
    return qualified


def _preview(path, patterns):
    """Return a small window on an actual line of the file, not an FTS token stream."""
    best = None
    frontmatter = False
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            line = line.strip()
            if number == 1 and line == "---":
                frontmatter = True
                continue
            if frontmatter:
                frontmatter = line != "---"
                continue
            if not line or line.startswith(("#", "> 来源：")):
                continue
            matches = [match for pattern in patterns if (match := pattern.search(line))]
            score = len(matches)
            if best is None or score > best[0]:
                start = max(0, min((match.start() for match in matches), default=0) - 80)
                excerpt = line[start : start + 400]
                best = (score, {"line": number, "preview": excerpt,
                                "preview_truncated": start > 0 or start + len(excerpt) < len(line)})
            if score == len(patterns):
                break
    return best[1] if best else {}


def _catalog_rows(db, expression, kind, source, preferred_title=None, *, exact_source=False, limit=-1):
    return db.execute(
        """SELECT o.title,o.path,o.payload,s.title AS source_title
               FROM catalog_search
               JOIN catalog_search_objects i ON i.id=catalog_search.rowid
               JOIN objects o ON o.ordinal=i.object
               LEFT JOIN objects s ON s.kind='source' AND s.id=COALESCE(
                   json_extract(o.payload,'$.source_id'),json_extract(o.source_ids,'$[0]'))
               WHERE catalog_search MATCH ? AND o.kind=?
                 AND (? IS NULL OR s.id=? OR (NOT ? AND instr(lower(s.title),lower(?))>0))
               ORDER BY CASE WHEN o.title COLLATE NOCASE = ? THEN 0 ELSE 1 END,
                        bm25(catalog_search,3,1),o.ordinal,i.id LIMIT ?""",
        (expression, CATALOG_KINDS[kind], source, source, exact_source, source,
         preferred_title, limit),
    )


def _table_rows(db, expressions, source):
    """Intersect phrase matches on the logical table, across its stored FTS parts."""
    values = ",".join("(?,?)" for _ in expressions)
    parameters = [value for no, expression in enumerate(expressions) for value in (no, expression)]
    return db.execute(
        f"""WITH queries(phrase_no, expression) AS (VALUES {values})
            SELECT o.title,o.path,o.payload,s.title AS source_title
              FROM queries q
              CROSS JOIN catalog_search
              JOIN catalog_search_objects i ON i.id=catalog_search.rowid
              JOIN objects o ON o.ordinal=i.object AND o.kind='table'
              LEFT JOIN objects s ON s.kind='source' AND s.id=COALESCE(
                   json_extract(o.payload,'$.source_id'),json_extract(o.source_ids,'$[0]'))
             WHERE catalog_search MATCH q.expression
               AND (? IS NULL OR s.id=? OR instr(lower(s.title),lower(?))>0)
             GROUP BY o.ordinal
            HAVING count(DISTINCT q.phrase_no)=?
             ORDER BY o.ordinal""",
        (*parameters, source, source, source, len(expressions)),
    )


def _relation_hits(db, root, entries, expression, source, limit, phrases, match_any):
    """Keep the predicate and its literal endpoints distinct from the supporting quote."""
    # Bound quote matches before looking up the registered endpoints. This also
    # works with catalogs built before relationship labels were indexed together.
    scan_limit = 4 * limit + 1
    rows = list(_catalog_rows(db, expression, "relation", source, limit=scan_limit))
    relations = [json.loads(row["payload"]) for row in rows]
    ids = sorted({rel[key] for rel in relations for key in ("source", "target") if rel.get(key)})
    labels = dict(db.execute(
        "SELECT id,title FROM objects WHERE kind='entity' AND id IN (" +
        ",".join("?" for _ in ids) + ")", ids)) if ids else {}
    direct, context, seen = [], [], set()
    for row, relation in zip(rows, relations):
        evidence = relation.get("evidence") or {}
        name = evidence.get("path")
        location = (evidence.get("start_char"), evidence.get("end_char"))
        key = (name, location, row["title"], relation.get("source"), relation.get("target"))
        if key in seen or _registered_path(root, entries, name) is None:
            continue
        seen.add(key)
        subject = labels.get(relation.get("source"))
        target = labels.get(relation.get("target"))
        matches = _matches_relation((subject, row["title"], target), phrases, match_any)
        hit = {"kind": "relation", "path": name, "title": row["title"], "source": row["source_title"],
               "origin_span": location, "lead": (evidence.get("excerpt") or "")[:400],
               "subject": subject, "target": target, "matched_in": "relation" if matches else "source_excerpt"}
        qualifiers = [
            {"kind": note["kind"], "value": note["value"][:240]}
            for note in ((relation.get("qualifiers") or {}).get("record_qualifiers") or [])
            if isinstance(note, dict) and isinstance(note.get("kind"), str)
            and isinstance(note.get("value"), str) and note["value"]
        ]
        if qualifiers:
            hit["qualifiers"] = qualifiers[:8]
        (direct if matches else context).append((hit, relation.get("source_id")))
    ranked = direct + context
    hits = []
    for hit, source_id in ranked[:limit]:
        chapter = db.execute(
            """SELECT o.path FROM units u JOIN objects o ON o.kind='unit' AND o.id=u.id
               WHERE u.source_id=? AND u.start_char<=? AND u.end_char>=?
               ORDER BY u.start_char DESC,u.end_char,u.id LIMIT 1""",
            (source_id, *hit["origin_span"]),
        ).fetchone()
        if chapter and _registered_path(root, entries, chapter["path"]):
            hit["context_path"] = chapter["path"]
        hits.append(hit)
    return {"available": True, "hits": hits, "more": len(ranked) > limit or len(rows) == scan_limit}


def _catalog_hits(db, root, entries, expression, kind, source, limit, patterns, preferred_title,
                  *, exact_source=False, table_parts=()):
    if table_parts:
        rows = _table_rows(db, table_parts, source)
    else:
        rows = _catalog_rows(db, expression, kind, source, preferred_title if kind == "text" else None,
                             exact_source=exact_source)
    hits, seen = [], set()
    for row in rows:
        if kind == "table":
            payload = json.loads(row["payload"])
            name = payload.get("page_path")
            details = {"page": payload.get("page_number"), "sheet": payload.get("sheet_name")}
            if table_parts:
                details["match"] = "same_table"
            key = name
        else:
            name = row["path"]
            details = {}
            key = name
        path = _registered_path(root, entries, name)
        if path is None or key in seen:
            continue
        if len(hits) == limit:
            return {"available": True, "hits": hits, "more": True}
        if kind == "text":
            details = _preview(path, patterns)
        seen.add(key)
        hits.append({"kind": kind, "path": name, "title": row["title"], "source": row["source_title"], **details})
    return {"available": True, "hits": hits, "more": False}


def _article_hits(db, root, entries, expression, source, limit, patterns, preferred_title):
    rows = db.execute(
        """SELECT a.path,a.title
           FROM authored_search JOIN authored_articles a ON a.ordinal=authored_search.rowid
           WHERE authored_search MATCH ? AND (? IS NULL OR EXISTS (
               SELECT 1 FROM json_each(a.source_ids) owner
               JOIN objects s ON s.kind='source' AND s.id=owner.value
               WHERE s.id=? OR instr(lower(s.title),lower(?))>0
           ))
           ORDER BY CASE WHEN a.title COLLATE NOCASE=? THEN 0 ELSE 1 END,
                    bm25(authored_search,3,1),a.ordinal""",
        (expression, source, source, source, preferred_title),
    )
    hits = []
    for row in rows:
        path = _registered_path(root, entries, row["path"])
        if path is None:
            continue
        if len(hits) == limit:
            return {"available": True, "hits": hits, "more": True}
        hits.append({"kind": "article", "path": row["path"], "title": row["title"],
                     **_preview(path, patterns)})
    return {"available": True, "hits": hits, "more": False}


def _independent_sources(db, root, entries, terms, source, limit, seen):
    """Preserve exact original-title leads that need not contain the other phrases."""
    candidates = []
    for term in terms:
        rows = db.execute(
            """SELECT json_extract(a.value,'$.page_path') AS path,s.title AS source_title
               FROM objects e
               JOIN json_each(COALESCE(json_extract(e.payload,'$.evidence_alternatives'),
                                       json_array(json(e.payload)))) a
               JOIN objects s ON s.kind='source' AND s.id=json_extract(a.value,'$.evidence.source_id')
               WHERE e.kind='entity' AND json_extract(e.payload,'$.identity_status')='original_article_identity'
                 AND e.title=? COLLATE NOCASE
                 AND (? IS NULL OR s.id=? OR instr(lower(s.title),lower(?))>0)
               ORDER BY e.ordinal,a.key
               LIMIT 3""",
            (term, source, source, source),
        )
        candidates.append((term, list(rows)))
    hits = []
    for group in zip_longest(*(rows for _, rows in candidates)):
        for (term, _), row in zip(candidates, group):
            if row is None or not isinstance(row["path"], str):
                continue
            name = row["path"]
            if name.startswith("source-articles/"):
                name = "wiki/" + name
            path = _registered_path(root, entries, name)
            if path is None or name in seen:
                continue
            hits.append({"phrase": term, "path": name, "source": row["source_title"],
                         **_preview(path, [re.compile(re.escape(term), re.IGNORECASE)])})
            seen.add(name)
            if len(hits) == limit:
                return hits
    return hits


def _source_sections(db, root, entries, terms, hits, limit):
    """Independently identify one named source, then find headings within it."""
    prefixes = {}
    for term in terms:
        if sum(char.isalpha() for char in term) < 3:
            continue
        words = re.findall(r"[^\W_]+", term.casefold())
        # Report titles often join words or insert file-name separators.
        prefixes[term] = re.compile(r"^" + r"[\W_]*".join(map(re.escape, words)) + r"(?:[\W_]|$)")
    if not prefixes:
        return []
    source_id, used_terms = None, ()
    for row in db.execute("SELECT id,title FROM objects WHERE kind='source'"):
        name = unicodedata.normalize("NFKC", row["title"] or "").casefold()
        matched = tuple(term for term, prefix in prefixes.items() if prefix.match(name))
        if not matched:
            continue
        if source_id is not None:
            return []
        source_id, used_terms = row["id"], matched
    if source_id is None:
        return []
    remaining = [term for term in terms if term not in used_terms]
    # A year may occur only in the source name or inside the chapter. The
    # heading is a reading lead. The primary search still uses every phrase.
    headings = [term for term in remaining if any(char.isalpha() for char in term)]
    if not headings:
        return []
    expression, relaxed = _expressions(headings, False)
    patterns = [re.compile(re.escape(term), re.IGNORECASE) for term in headings]
    search = _catalog_hits(db, root, entries, f"title:({expression})", "text", source_id,
                           limit, patterns, None, exact_source=True)
    if not search["hits"] and relaxed != expression:
        search = _catalog_hits(db, root, entries, f"title:({relaxed})", "text", source_id,
                               limit, patterns, None, exact_source=True)
        for hit in search["hits"]:
            hit["match"] = "prefix_fallback"
    seen = {hit["path"] for hit in hits}
    return [hit for hit in search["hits"] if hit["path"] not in seen]


def find(root, terms, *, kind="auto", source=None, limit=6, match_any=False):
    root = Path(root).resolve()
    manifest = root / "indexes/manifest.json"
    if not manifest.is_file():
        return {"available": False, "hits": []}
    entries = json.loads(manifest.read_text(encoding="utf-8"))["files"]
    catalog = _registered_path(root, entries, "catalog.sqlite")
    if catalog is None:
        return {"available": False, "hits": []}
    if not 1 <= limit <= 20 or len(terms) > 12 or any(len(term) > 256 for term in terms):
        raise ValueError("Use at most 12 phrases of 256 characters and request 1–20 hits.")
    expression, relaxed = _expressions(terms, match_any)
    if not expression:
        return {"available": True, "hits": [], "more": False}
    phrases = tuple(phrase for term in terms if (phrase := _phrase_words(term)).strip()) if kind == "relation" else ()
    patterns = [re.compile(re.escape(term.strip()), re.IGNORECASE) for term in terms if term.strip()]
    preferred_title = unicodedata.normalize("NFKC", terms[0]).strip() if len(terms) == 1 else None
    modes = ("text", "table") if kind == "auto" else (kind,)
    with closing(sqlite3.connect(catalog.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        db.row_factory = sqlite3.Row
        articles_available = (
            kind in {"auto", "article"}
            and "articles/authored/index.md" in entries
            and db.execute("SELECT 1 FROM sqlite_master WHERE name='authored_search'").fetchone()
        )
        title_match = ""
        if articles_available and not match_any:
            normalized = unicodedata.normalize("NFKC", " ".join(terms)).casefold()
            words = tuple(dict.fromkeys(re.findall(r"[^\W_]+", normalized)))
            # A combined heading can contain every name while changing the
            # phrase order. Keep this relaxed match limited to the title.
            if 1 < len(words) <= 32:
                title_match = "title:(" + " AND ".join(map(_phrase, words)) + ")"

        def lookup(query, mode, count):
            if mode == "claim":
                return _claims(db, root, entries, query, source, count)
            if mode == "relation":
                return _relation_hits(db, root, entries, query, source, count, phrases, match_any)
            if mode == "article":
                if title_match:
                    query = f"({query}) OR {title_match}"
                return _article_hits(db, root, entries, query, source, count, patterns, preferred_title)
            return _catalog_hits(db, root, entries, query, mode, source, count, patterns, preferred_title)

        def search(mode, count):
            result = lookup(expression, mode, count)
            if not result["hits"] and relaxed != expression:
                result = lookup(relaxed, mode, count)
                for hit in result["hits"]:
                    hit["match"] = "prefix_fallback"
            if mode == "table" and not match_any and len(terms) > 1 and not result["hits"]:
                parts = tuple(dict.fromkeys(part for term in terms
                                            if (part := _expressions([term], False)[0])))
                if len(parts) > 1:
                    result = _catalog_hits(db, root, entries, expression, mode, source, count,
                                           patterns, preferred_title, table_parts=parts)
            return result

        if kind == "article":
            return search("article", limit) if articles_available else {"available": False, "hits": []}
        batches = [search(mode, limit) for mode in modes]
        hits = [hit for pair in zip_longest(*(r["hits"] for r in batches)) for hit in pair if hit]
        result = {"available": True, "hits": hits[:limit],
                  "more": len(hits) > limit or any(r["more"] for r in batches)}
        if articles_available:
            authored = search("article", min(limit, 3))
            if authored["hits"]:
                result["authored_articles"] = authored["hits"]
        titles = tuple(dict.fromkeys(unicodedata.normalize("NFKC", term).strip() for term in terms
                                     if any(char.isalnum() for char in term)))
        if kind == "relation" and not any(hit["matched_in"] == "relation" for hit in result["hits"]):
            # Pronouns and names outside a literal excerpt can be recorded as
            # proposals without being promoted to located relationship edges.
            candidates = search("claim", 20)["hits"]
            context = _contextual_claims(candidates, phrases, match_any, min(limit, 3))
            if context:
                result["contextual_claims"] = context
        if kind in {"auto", "text"} and not match_any and len(titles) > 1:
            leads = _independent_sources(db, root, entries, titles, source, min(limit, 6),
                                         {hit["path"] for hit in result["hits"]})
            if leads:
                result["independent_sources"] = leads
            if source is None:
                sections = _source_sections(db, root, entries, titles, result["hits"], min(limit, 3))
                if sections:
                    result["source_sections"] = sections
    return result


def main():
    parser = argparse.ArgumentParser(prog="wiki-find", description=__doc__.splitlines()[0])
    parser.add_argument("publication", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--kind", choices=("auto", *CATALOG_KINDS, "claim", "article"), default="auto")
    parser.add_argument("--source", help="Published source ID or distinctive part of its title")
    parser.add_argument("--limit", type=int, default=6)
    parser.add_argument("--any", action="store_true", help="Match any supplied phrase instead of all")
    parser.add_argument("phrases", nargs="+")
    args = parser.parse_args()
    try:
        result = find(args.publication, args.phrases, kind=args.kind, source=args.source,
                      limit=args.limit, match_any=args.any)
    except (OSError, ValueError, KeyError, IndexError, sqlite3.Error, zlib.error) as exc:
        result = {"available": False, "hits": [], "error": type(exc).__name__}
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
