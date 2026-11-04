"""Bounded relation discovery for the existing search/read evidence workflow.

The sealed graph and the historical audited page links are two projections of
the same publication. Neither produces evidence handles: source and target
pages must still pass the normal wiki_read and citation gates.
"""
from __future__ import annotations

import json
import re
from contextlib import closing

INDEX_PATH = "indexes/knowledge_graph.sqlite"
GRAPH_SCHEMA = "wiki-knowledge-graph-v1"
MAX_SEEDS = 4
MAX_EDGES = 4
MAX_CANDIDATES = 4


def _value(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def _fold(value):
    return " ".join(str(value or "").casefold().split())


def _seeds(corpus, question):
    names = {}
    folded = _fold(question)
    for path, entry in corpus.page_index.items():
        for name in [_value(entry, "title", ""), *(_value(entry, "aliases", []) or [])]:
            name = _fold(name)
            if len(name) >= 2:
                names.setdefault(name, set()).add(path)
    matched = [name for name, paths in names.items() if len(paths) == 1 and name in folded
               and (not name.isascii() or re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", folded))]
    # Prefer the most specific name; an ambiguous title never picks an owner.
    matched.sort(key=lambda name: (-len(name), name))
    specific = []
    for name in matched:
        if not any(name in longer for longer in specific):
            specific.append(name)
    return list(dict.fromkeys(next(iter(names[name])) for name in specific))[:MAX_SEEDS]


def _legacy_supported(corpus, source, refs):
    if not refs:
        return False
    claims = [c for c in corpus.claims.get(source, [])
              if _value(c, "support_status") == "SUPPORTED" and not _value(c, "unresolved_evidence", [])]
    return all(any(
        str(_value(span, "passage_id", "")) == str(ref.get("passage_id", ""))
        and (not ref.get("document_id") or str(_value(span, "document_id", "")) == str(ref["document_id"]))
        and bool(ref.get("excerpt")) and _fold(ref["excerpt"]) in _fold(_value(span, "text", ""))
        for claim in claims for span in _value(claim, "source_spans", [])) for ref in refs)


def _legacy_edges(corpus, seeds):
    edges = []
    for source, links in sorted(corpus.page_links.items()):
        for link in sorted(links, key=lambda item: (item.get("relation", ""), item.get("target_id", ""))):
            target = link.get("target_id", "")
            if source not in seeds and target not in seeds:
                continue
            if (link.get("kind") != "supported_relation" or link.get("conflict") or not link.get("relation")
                    or not _legacy_supported(corpus, source, link.get("evidence_refs") or [])):
                continue
            edges.append({"source_path": source, "target_path": target,
                "predicate": link["relation"], "conflict": False,
                "qualifiers": [{"source_statement": ref["excerpt"]} for ref in link["evidence_refs"]]})
            if len(edges) >= MAX_EDGES:
                return edges
    return edges


def _indexed_edges(view, seeds):
    # BundleView.open_sqlite itself need not verify bytes. Verify through the
    # existing manifest-aware reader before opening the immutable connection.
    if hasattr(view, "verified_file_path"):
        view.verified_file_path(INDEX_PATH)
    else:
        view.read_bytes(INDEX_PATH)
    with closing(view.open_sqlite(INDEX_PATH)) as db:
        metadata = dict(db.execute("SELECT key,value FROM metadata"))
        if metadata != {"schema": GRAPH_SCHEMA, "tenant_id": view.tenant_id, "kb_id": view.context_id}:
            raise ValueError("GRAPH_FRONTIER_SCOPE_MISMATCH")
        placeholders = ",".join("?" for _ in seeds)
        rows = db.execute(
            "SELECT e.data,s.data,t.data FROM edges e JOIN nodes s ON s.node_id=e.source "
            "JOIN nodes t ON t.node_id=e.target WHERE e.family='semantic' AND "
            f"(json_extract(s.data,'$.page_path') IN ({placeholders}) OR "
            f"json_extract(t.data,'$.page_path') IN ({placeholders})) "
            "ORDER BY e.predicate,e.edge_id LIMIT ?", [*seeds, *seeds, MAX_EDGES])
        edges = []
        for edge, source, target in rows:
            edge, source, target = json.loads(edge), json.loads(source), json.loads(target)
            # The normal reader does not promise to deliver the graph's
            # counterevidence set. Do not let a conflicted edge steer recall
            # toward one side; ordinary search remains available.
            if edge.get("support_status") != "SUPPORTED" or edge.get("conflict"):
                continue
            edges.append({**edge, "source_path": source.get("page_path", ""),
                          "target_path": target.get("page_path", "")})
        return edges


def graph_frontier(corpus, question, view=None):
    """Return at most four page clues and a small public diagnostic, or None.

    Unambiguous source-derived names activate bounded neighbor discovery,
    independent of a relation-word whitelist. Neighbors never imply a filter,
    an exhaustive relation list, or answer support.
    """
    seeds = _seeds(corpus, question)
    if not seeds:
        return None
    indexed = view is not None and INDEX_PATH in view.list_entries()
    edges = _indexed_edges(view, seeds) if indexed else _legacy_edges(corpus, seeds)
    candidates, seen = [], set()
    for edge in edges:
        source, target = edge["source_path"], edge["target_path"]
        if source == target or source not in corpus.page_index or target not in corpus.page_index:
            continue
        # Include the supporting source page as well as the related page.
        # Reading only an outgoing target could miss the relation assertion.
        for path in (source, target):
            if path in seen or len(candidates) >= MAX_CANDIDATES:
                continue
            seen.add(path)
            candidates.append({"path": path, "source_path": source, "target_path": target,
                "predicate": edge["predicate"], "direction": "out" if source in seeds else "in",
                "family": "semantic", "conflict": bool(edge.get("conflict")),
                "edge_id": edge.get("edge_id", ""),
                "why_visit": "Published relation clue only; read both pages and original evidence before answering."})
    return {"items": candidates, "backend": "bundle_index" if indexed else "published_links",
            "coverage": "bounded_navigation_only", "seed_paths": seeds,
            "graph_revision": getattr(view, "manifest_hash", "") if view else ""}
