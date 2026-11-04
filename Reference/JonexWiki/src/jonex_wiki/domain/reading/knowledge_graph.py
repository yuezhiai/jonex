"""Versioned, evidence-bearing graph projection of an immutable Wiki bundle.

This is a rebuildable SQLite index, not another fact store. Page identity is
KB + canonical path (never the display title); identical titles are not merged.
Semantic edges require audited page relations or literal source-catalog evidence. Legacy
page links and body mentions remain navigation. Every query is bounded and its
cursor is tied to the scope, filters and publication, not a process session.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from jonex_wiki.domain.engine.json_artifacts import load_json_artifact
from jonex_wiki.domain.engine.compile.mention_index import is_raw_page
from jonex_wiki.domain.reading.bundle_reader import iter_claims, load_links, load_navigation
from jonex_wiki.kernel.errors import NotFoundError, ValidationError

INDEX_PATH = "indexes/knowledge_graph.sqlite"
SCHEMA_VERSION = "wiki-knowledge-graph-v1"
NODE_TYPES = ("image", "knowledge_topic", "table", "page", "document", "claim", "section", "media_segment", "video_frame")
_TYPE_ID=re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
FAMILIES = ("semantic", "navigation", "provenance")
_KINDS = {"sources": "page", "tables": "table"}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _id(kind: str, *parts: Any) -> str:
    return kind + "_" + hashlib.sha256(_json(parts).encode()).hexdigest()[:32]


def _fold(text: str) -> str:
    return " ".join(str(text).casefold().split())


def _clean(text: Any) -> str:
    return " ".join(re.sub(r"<[^>]*>", " ", str(text or "")).split())


def _optional_artifact(pub: Path, path: str) -> dict:
    if not (pub / path).is_file():
        return {}
    value = load_json_artifact(lambda name: (pub / name).read_bytes(), path)
    return value if isinstance(value, dict) else {}


def build_index(pub: Path, destination: Path, tenant_id: str, kb_id: str) -> None:
    """Build deterministically, then atomically replace a derived index.

    No publication checksum is written inside the index: its identity is the
    enclosing bundle's checksum, avoiding a checksum self-reference.
    """
    nav = load_navigation(pub)
    page_index = nav.get("page_index") or {}
    page_graph = nav.get("page_graph") or {}
    nodes: dict[str, dict] = {}
    aliases: set[tuple[str, str]] = set()
    node_docs: set[tuple[str, str]] = set()
    edges: dict[str, dict] = {}
    edge_docs: set[tuple[str, str]] = set()
    evidence: dict[str, dict] = {}
    edge_evidence: set[tuple[str, str]] = set()
    page_ids: dict[str, str] = {}
    evidence_lookup: dict[tuple[str, str], list[str]] = {}
    statement_evidence: dict[str, set[str]] = {}
    source_docs: dict[str, set[str]] = {}

    def node(kind: str, key: str, label: str, **data) -> str:
        nid = _id("n", tenant_id, kb_id, kind, key)
        if nid not in nodes:
            nodes[nid] = {"node_id": nid, "node_type": kind, "label": label or key,
                          "description": "", "page_path": "", "document_id": "",
                          "claim_id": "", **data}
        aliases.add((nid, _fold(label or key)))
        return nid

    def edge(source: str, target: str, family: str, predicate: str, *,
             refs=(), docs=(), status="NOT_APPLICABLE", qualifiers=None, description="") -> str:
        # Different predicates/conditions between the same endpoints survive.
        eid = _id("e", source, target, family, predicate, qualifiers or {})
        edges.setdefault(eid, {"edge_id": eid, "source_node_id": source,
            "target_node_id": target, "family": family, "predicate": predicate,
            "support_status": status, "qualifiers": qualifiers or {}, "description": description})
        for ref in refs:
            edge_evidence.add((eid, ref))
        for doc in docs:
            if doc:
                edge_docs.add((eid, doc))
                node_docs.update({(source, doc), (target, doc)})
        return eid

    for path, raw in sorted(page_index.items()):
        if is_raw_page(path) or not isinstance(raw, dict):
            continue
        kind = _KINDS.get(path.split("/", 1)[0], "knowledge_topic")
        nid = node(kind, path, _clean(raw.get("title")) or path,
                   page_path=path, description=_clean(raw.get("description")),
                   aliases=list(raw.get("aliases") or []), scope=raw.get("scope") or {},
                   fields=raw.get("fields") or [], category=path.split("/", 1)[0])
        page_ids[path] = nid
        for name in raw.get("aliases") or []:
            if isinstance(name, str) and name.strip():
                aliases.add((nid, _fold(name)))

    # Native source/table pages can have no compiled claims. Their explicit
    # source records still establish document ownership and document names.
    for path, records in (nav.get("source_records") or {}).items():
        for record in records if isinstance(records, list) else []:
            if not isinstance(record, dict) or not record.get("document_id"):
                continue
            doc = str(record["document_id"])
            for key in (record.get("source_id"), record.get("passage_id")):
                if key:
                    source_docs.setdefault(str(key), set()).add(doc)
            source_title = str((page_index.get(path) or {}).get("title") or "")
            source_name = record.get("source_name") or (source_title.removeprefix("Source: ") if source_title.startswith("Source: ") else doc)
            dn = node("document", doc, str(source_name), document_id=doc)
            node_docs.add((dn, doc))
            if path in page_ids:
                node_docs.add((page_ids[path], doc))
                edge(page_ids[path], dn, "provenance", "引用来源", docs=[doc])

    claims = iter_claims(pub) if (pub / "claims.jsonl").is_file() else []
    for claim in claims:
        path = str(claim.get("page_path") or "")
        if path not in page_ids:
            continue
        cid = str(claim.get("claim_id") or "")
        if not cid:
            continue
        status = str(claim.get("support_status") or "NOT_ENOUGH_INFO")
        cn = node("claim", cid, str(claim.get("text") or cid), claim_id=cid,
                  page_path=path, description=str(claim.get("text") or ""), support_status=status)
        spans = [s for s in claim.get("source_spans") or [] if isinstance(s, dict)]
        refs = [r for r in claim.get("evidence") or [] if isinstance(r, dict)]
        # A span is its own location. Never borrow the first excerpt/location
        # from another document on a multi-source claim.
        candidates = spans + [r for r in refs if not any(
            str(s.get("passage_id") or "") == str(r.get("passage_id") or "")
            and str(s.get("document_id") or "") == str(r.get("document_id") or "") for s in spans)]
        claim_refs: list[str] = []
        docs: set[str] = set()
        for span in candidates:
            doc = str(span.get("document_id") or "")
            passage = str(span.get("passage_id") or "")
            excerpt = str(span.get("text") or span.get("excerpt") or "")
            matching_refs = [r for r in refs if str(r.get("passage_id") or "") == passage
                             and (not r.get("document_id") or str(r["document_id"]) == doc)]
            evid = _id("ev", cid, doc, passage, span.get("start_char"), span.get("end_char"), excerpt)
            evidence[evid] = {"evidence_id": evid, "claim_id": cid, "page_path": path,
                "document_id": doc, "passage_id": passage, "chunk_id": span.get("chunk_id") or "",
                "source_sha256": span.get("source_sha256") or "",
                "start_char": span.get("start_char"), "end_char": span.get("end_char"),
                "excerpt": excerpt, "claim_text": claim.get("text") or "", "support_status": status}
            statement_evidence.setdefault(_fold(claim.get("text") or ""), set()).add(evid)
            claim_refs.append(evid)
            if status == "SUPPORTED":
                # Each relation ref must match a real supported claim's quote.
                for ref in matching_refs:
                    quote = str(ref.get("excerpt") or "")
                    if quote and _fold(quote) in _fold(excerpt):
                        evidence_lookup.setdefault((passage, _fold(quote)), []).append(evid)
            if doc:
                docs.add(doc)
                for key in (span.get("source_id"), passage):
                    if key:
                        source_docs.setdefault(str(key), set()).add(doc)
                dn = node("document", doc, doc, document_id=doc)
                node_docs.update({(cn, doc), (page_ids[path], doc), (dn, doc)})
                edge(cn, dn, "provenance", "引用来源", refs=[evid], docs=[doc])
        edge(page_ids[path], cn, "provenance", "包含断言", refs=claim_refs, docs=docs)
        for doc in docs:
            dn = _id("n", tenant_id, kb_id, "document", doc)
            edge(page_ids[path], dn, "provenance", "引用来源",
                 refs=[r for r in claim_refs if evidence[r]["document_id"] == doc], docs=[doc])

    source_page_docs = {"sources/source-" + hashlib.sha256(doc.encode()).hexdigest()[:16] + ".md": doc
                        for values in source_docs.values() for doc in values}
    for path, nid in page_ids.items():
        for source in (page_graph.get(path) or {}).get("source_ids") or []:
            for doc in source_docs.get(str(source), ()):
                dn = node("document", doc, doc, document_id=doc)
                node_docs.add((nid, doc))
                edge(nid, dn, "provenance", "引用来源", docs=[doc])
        if path in source_page_docs:
            doc = source_page_docs[path]
            dn = node("document", doc, doc, document_id=doc)
            node_docs.add((nid, doc))
            edge(nid, dn, "provenance", "引用来源", docs=[doc])

    relations = _optional_artifact(pub, "query/relations.json")
    audited = False
    if relations.get("digest"):
        from jonex_wiki.domain.engine.compile.relation_increment import load
        audited = load(relations) is not None
    for record in relations.get("links") or []:
        if not isinstance(record, dict) or record.get("resolution", "resolved") != "resolved":
            continue
        source, target = page_ids.get(record.get("page_path")), page_ids.get(record.get("target_id"))
        if not source or not target or source == target:
            continue
        refs = record.get("evidence_refs") or []
        matches = [[e for e in evidence_lookup.get(
                        (str(r.get("passage_id") or ""), _fold(r.get("excerpt") or "")), [])
                    if evidence[e]["page_path"] == record.get("page_path")
                    and (not r.get("document_id") or evidence[e]["document_id"] == str(r["document_id"]))]
                   for r in refs if isinstance(r, dict)]
        supported = (audited and record.get("kind") == "supported_relation" and bool(record.get("relation"))
                     and bool(refs) and len(matches) == len(refs) and all(matches))
        evids = sorted({e for group in matches for e in group}) if supported else []
        refutations = {refutation for evid in evids
                       for refutation in statement_evidence.get(_fold(evidence[evid]["claim_text"]), ())
                       if evidence[refutation]["support_status"] == "REFUTED"}
        evids = sorted(set(evids) | refutations)
        docs = {evidence[e]["document_id"] for e in evids} if supported else {
            doc for nid, doc in node_docs if nid == source}
        eid = edge(source, target, "semantic" if supported else "navigation",
             str(record["relation"]) if supported else "页间链接", refs=evids, docs=docs,
             status=("CONFLICTING" if refutations else "SUPPORTED") if supported else "NOT_APPLICABLE",
             qualifiers=record.get("qualifiers") or {}, description=str(record.get("why_visit") or ""))
        if refutations:
            edges[eid]["conflict"] = True

    # Independently identified source entities do not have to be Wiki pages.
    # Catalog evidence is admitted separately from model-proposed page links.
    catalog = _optional_artifact(pub, "query/media_catalog.json")
    entity_nodes = {}
    for item in catalog.get("entities", []):
        entity_nodes[item["entity_id"]] = node(item["entity_type"], item["entity_id"], item["label"],
            entity_id=item["entity_id"], document_id=item["document_id"],
            description=item.get("description", ""), author=item.get("author", ""))
    for item in catalog.get("assets", []):
        label = item.get('caption') or item.get('label') or f"{item.get('source_name', '原文')} 图片 {item.get('image_idx', '')}"
        entity_nodes[item["asset_id"]] = node("image", item["asset_id"], label,
            **{key: value for key, value in item.items() if key not in {"path", "label"}})
    def catalog_refs(refs):
        admitted = []
        for ref in refs:
            source = pub / ref["path"]
            # Revalidate exact source offsets/hash when building the index.
            body = source.read_text("utf-8")
            if (hashlib.sha256(body.encode()).hexdigest() != ref["body_sha256"]
                    or body[ref["start_char"]:ref["end_char"]] != ref["excerpt"]):
                raise ValueError("MEDIA_RELATION_SOURCE_MISMATCH")
            evid = _id("ev", "source_catalog", ref)
            evidence[evid] = {**ref, "evidence_id": evid, "support_status": "SUPPORTED", "page_path": ref["path"],
                             "support_kind": "source_catalog", "claim_text": ref["excerpt"]}
            admitted.append(evid)
        return admitted
    for item in catalog.get("relations", []):
        source, target = entity_nodes[item["source_entity_id"]], entity_nodes[item["target_entity_id"]]
        eid = edge(source, target, "semantic", item["predicate"], refs=catalog_refs(item["evidence"]),
             docs=[nodes[source]["document_id"]], status="SUPPORTED",
             qualifiers={"catalog_relation_id": item["relation_id"], "support_kind": "source_catalog"})
    for item in catalog.get("bindings", []):
        source, target = entity_nodes[item["work_entity_id"]], entity_nodes[item["asset_id"]]
        edge(source, target, "provenance", "配图", refs=catalog_refs(item["evidence"]),
             docs=[nodes[source]["document_id"]], status="SUPPORTED",
             qualifiers={"association_method": item["method"], "binding_id": item["binding_id"]})
    for nid in entity_nodes.values():
        doc = nodes[nid]["document_id"]
        node_docs.add((nid, doc))
        edge(nid, node("document", doc, doc, document_id=doc), "provenance", "来自文档", docs=[doc])

    resources = _optional_artifact(pub, "query/resources.json")
    resource_nodes = {}
    resource_refs = {}
    for obj in resources.get("objects", []):
        kind = obj["kind"]
        if not isinstance(kind,str)or not _TYPE_ID.fullmatch(kind):continue
        key = obj["document_id"] if kind == "document" else obj["object_id"]
        nid = node(kind, key, obj["label"], document_id=obj["document_id"],
            resource_object_id=obj["object_id"], source_sha256=obj["source_sha256"],
            locator=obj["locator"], review_status=obj["review_status"], edition=obj.get("edition"))
        resource_nodes[obj["object_id"]] = nid
        nodes[nid].update(resource_object_id=obj["object_id"], source_sha256=obj["source_sha256"],
                          locator=obj["locator"], review_status=obj["review_status"])
        node_docs.add((nid, obj["document_id"]))
    for ref in resources.get("evidence", []):
        eid = _id("ev", ref["evidence_id"]);resource_refs[ref["evidence_id"]] = eid
        article = pub / ref["path"]
        body = article.read_text("utf-8") if article.is_file() else ""
        if "json_page" in ref:body = json.loads(body)["pages"][ref["json_page"]]["text"]
        evidence[eid] = {**ref, "evidence_id": eid, "kind": "resource_location",
            "excerpt": body[ref["start_char"]:ref["end_char"]][:800]}
    for relation in resources.get("relations", []):
        source, target = resource_nodes.get(relation["source"]), resource_nodes.get(relation["target"])
        if source and target:
            family = "navigation" if relation["family"] in {"structure", "navigation"} else "semantic"
            edge(source, target, family, relation["type"],
                refs=[resource_refs[r] for r in relation["evidence_ids"] if r in resource_refs],
                docs=[nodes[target]["document_id"]],
                status="NOT_APPLICABLE" if family == "navigation" else "NOT_ENOUGH_INFO",
                qualifiers={"review_status": relation["review_status"], "resource_relation_id": relation["relation_id"]})

    docs_by_node: dict[str, set[str]] = {}
    for nid, doc in node_docs:
        docs_by_node.setdefault(nid, set()).add(doc)
    connected = {(e["source_node_id"], e["target_node_id"]) for e in edges.values()}
    # Legacy graph records are navigation only. No same-category recommendations
    # and no title-based presentation merges are promoted to graph relations.
    for path, graph in sorted(page_graph.items()):
        source = page_ids.get(path)
        if not source or not isinstance(graph, dict):
            continue
        targets = list(graph.get("related_pages") or []) + [
            r.get("target_id") for r in graph.get("links") or [] if isinstance(r, dict)]
        for target_path in targets:
            target = page_ids.get(target_path)
            if target and target != source and (source, target) not in connected:
                edge(source, target, "navigation", "页间链接",
                     docs=docs_by_node.get(source, ()))
                connected.add((source, target))
    reading_links = load_links(pub)
    for target_path, sources in (reading_links.get("in_links") or {}).items():
        target = page_ids.get(target_path)
        for record in sources:
            source = page_ids.get(record.get("path")) if isinstance(record, dict) else None
            if source and target and source != target and (source, target) not in connected:
                edge(source, target, "navigation", "页间链接", docs=docs_by_node.get(source, ()))
                connected.add((source, target))
    for target_path, source_paths in (reading_links.get("mentions") or {}).items():
        target = page_ids.get(target_path)
        for source_path in source_paths:
            source = page_ids.get(source_path)
            if source and target and source != target:
                edge(source, target, "navigation", "正文提及",
                     docs=docs_by_node.get(source, ()))

    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".graph-", suffix=".sqlite", dir=destination.parent)
    os.close(fd)
    db = None
    try:
        with sqlite3.connect(temporary) as db:
            db.executescript("""
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE nodes (node_id TEXT PRIMARY KEY, node_type TEXT NOT NULL, label TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX nodes_label ON nodes(label,node_type);
                CREATE TABLE aliases (node_id TEXT NOT NULL, name TEXT NOT NULL, PRIMARY KEY(node_id,name));
                CREATE TABLE node_docs (node_id TEXT NOT NULL, document_id TEXT NOT NULL, PRIMARY KEY(node_id,document_id));
                CREATE TABLE edges (edge_id TEXT PRIMARY KEY, source TEXT NOT NULL, target TEXT NOT NULL, family TEXT NOT NULL, predicate TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX edges_out ON edges(source,family,edge_id);
                CREATE INDEX edges_in ON edges(target,family,edge_id);
                CREATE TABLE edge_docs (edge_id TEXT NOT NULL, document_id TEXT NOT NULL, PRIMARY KEY(edge_id,document_id));
                CREATE TABLE evidence (evidence_id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE edge_evidence (edge_id TEXT NOT NULL, evidence_id TEXT NOT NULL, PRIMARY KEY(edge_id,evidence_id));
                CREATE VIRTUAL TABLE node_search USING fts5(node_id UNINDEXED, text, tokenize='unicode61');
            """)
            db.executemany("INSERT INTO metadata VALUES (?,?)", [("schema", SCHEMA_VERSION), ("tenant_id", tenant_id), ("kb_id", kb_id)])
            db.executemany("INSERT INTO nodes VALUES (?,?,?,?)", [(nid, n["node_type"], n["label"], _json(n)) for nid, n in sorted(nodes.items())])
            db.executemany("INSERT INTO aliases VALUES (?,?)", sorted(aliases))
            db.executemany("INSERT INTO node_docs VALUES (?,?)", sorted(node_docs))
            db.executemany("INSERT INTO edges VALUES (?,?,?,?,?,?)", [(eid, e["source_node_id"], e["target_node_id"], e["family"], e["predicate"], _json(e)) for eid, e in sorted(edges.items())])
            db.executemany("INSERT INTO edge_docs VALUES (?,?)", sorted(edge_docs))
            db.executemany("INSERT INTO evidence VALUES (?,?)", [(eid, _json(e)) for eid, e in sorted(evidence.items())])
            db.executemany("INSERT INTO edge_evidence VALUES (?,?)", sorted(edge_evidence))
            aliases_by_node: dict[str, list[str]] = {}
            for nid, name in sorted(aliases):
                aliases_by_node.setdefault(nid, []).append(name)
            for nid, n in sorted(nodes.items()):
                text = " ".join([n["label"], n["description"], *aliases_by_node.get(nid, [])])
                # Space Han characters for SQLite's ordinary tokenizer. Exact
                # name/alias matches rank first; FTS also finds Chinese prose.
                text = re.sub(r"([\u3400-\u9fff])", r" \1 ", text)
                db.execute("INSERT INTO node_search VALUES (?,?)", (nid, text))
        db.close()
        db = None
        os.replace(temporary, destination)
    finally:
        if db is not None:
            db.close()
        if os.path.exists(temporary):
            os.unlink(temporary)


class GraphIndex:
    def __init__(self, path: Path, revision: str, tenant_id: str, kb_id: str):
        self.path, self.revision = path, revision
        self.tenant_id, self.kb_id = tenant_id, kb_id

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            db.row_factory = sqlite3.Row
            meta = dict(db.execute("SELECT key,value FROM metadata"))
            if meta != {"schema": SCHEMA_VERSION, "tenant_id": self.tenant_id, "kb_id": self.kb_id}:
                raise ValidationError("图索引版本或知识库范围不匹配")
            yield db
        finally:
            db.close()

    def _envelope(self, **data):
        return {"knowledge_base_id": self.kb_id, "graph_revision": self.revision, **data}

    def _page(self, items: list, total: int, limit: int, offset: int, fingerprint: str):
        more = offset + len(items) < total
        cursor = base64.urlsafe_b64encode(_json({"v": self.revision, "f": fingerprint,
                  "o": offset + len(items)}).encode()).decode().rstrip("=") if more else None
        return self._envelope(items=items, total=total, returned=len(items), has_more=more, next_cursor=cursor)

    def _offset(self, cursor: str, filters: Any) -> tuple[int, str]:
        fingerprint = _id("q", self.tenant_id, self.kb_id, filters)
        if not cursor:
            return 0, fingerprint
        try:
            if len(cursor) > 2048:
                raise ValueError()
            data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            if data["v"] != self.revision or data["f"] != fingerprint or type(data["o"]) is not int or data["o"] < 0:
                raise ValueError()
            return data["o"], fingerprint
        except (ValueError, KeyError, TypeError):
            raise ValidationError("分页游标与版本或查询条件不匹配") from None

    @staticmethod
    def _limit(limit: int) -> int:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValidationError("limit 必须在 1-100 之间")
        return limit

    @staticmethod
    def _scope(alias: str, document_id: str, table="node_docs", key="node_id"):
        return (f" AND EXISTS (SELECT 1 FROM {table} d WHERE d.{key}={alias}.{key} AND d.document_id=?)", [document_id]) if document_id else ("", [])

    def types(self, *, document_id=""):
        with self._db() as db:
            where, args = self._scope("n", document_id)
            rows = db.execute("SELECT n.node_type,count(*) AS count FROM nodes n WHERE 1=1" + where + " GROUP BY n.node_type ORDER BY CASE n.node_type WHEN 'knowledge_topic' THEN 0 WHEN 'table' THEN 1 WHEN 'page' THEN 2 WHEN 'document' THEN 3 ELSE 4 END", args)
            return self._envelope(items=[dict(r) for r in rows])

    def search(self, *, query="", node_type="", document_id="", limit=20, cursor=""):
        limit = self._limit(limit)
        if node_type and not _TYPE_ID.fullmatch(node_type):
            raise ValidationError("未知节点类型")
        if len(query) > 500:
            raise ValidationError("query 长度不能超过 500 字符")
        offset, fingerprint = self._offset(cursor, ["search", query, node_type, document_id])
        q = _fold(query)
        with self._db() as db:
            where, args = self._scope("n", document_id)
            if node_type:
                where += " AND n.node_type=?"
                args.append(node_type)
            else:
                where += " AND n.node_type<>'claim'"
            if q:
                terms = re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]+", q)
                fts = " AND ".join('"' + (" ".join(term) if re.fullmatch(r"[\u3400-\u9fff]+", term) else term) + '"' for term in terms) or '"__empty__"'
                where += " AND (EXISTS (SELECT 1 FROM aliases a WHERE a.node_id=n.node_id AND instr(a.name,?)>0) OR n.node_id IN (SELECT node_id FROM node_search WHERE node_search MATCH ?))"
                args += [q, fts]
            total = db.execute("SELECT count(*) FROM nodes n WHERE 1=1" + where, args).fetchone()[0]
            ordering = "CASE WHEN EXISTS (SELECT 1 FROM aliases a WHERE a.node_id=n.node_id AND a.name=?) THEN 0 ELSE 1 END,n.label,n.node_id"
            rows = db.execute("SELECT n.data FROM nodes n WHERE 1=1" + where + " ORDER BY " + ordering + " LIMIT ? OFFSET ?", [*args, q, limit, offset])
            items = [json.loads(r[0]) for r in rows]
            for item in items:
                exact = db.execute("SELECT 1 FROM aliases WHERE node_id=? AND name=?", (item["node_id"], q)).fetchone() if q else None
                item["matched_by"] = ("name" if _fold(item["label"]) == q else "alias") if exact else "text" if q else "browse"
            return self._page(items, total, limit, offset, fingerprint)

    def node(self, node_id: str, *, document_id=""):
        with self._db() as db:
            where, args = self._scope("n", document_id)
            row = db.execute("SELECT n.data FROM nodes n WHERE n.node_id=?" + where, [node_id, *args]).fetchone()
            if not row:
                raise NotFoundError("节点不存在或不在当前范围")
            return self._envelope(node=json.loads(row[0]))

    def neighbors(self, node_id: str, *, direction="both", families=None, document_id="", limit=20, cursor=""):
        limit = self._limit(limit)
        families = sorted(set(FAMILIES if families is None else families))
        if direction not in ("in", "out", "both") or not families or set(families) - set(FAMILIES):
            raise ValidationError("关系方向或类型无效")
        self.node(node_id, document_id=document_id)
        offset, fingerprint = self._offset(cursor, ["neighbors", node_id, direction, families, document_id])
        with self._db() as db:
            condition = {"out": "e.source=?", "in": "e.target=?", "both": "(e.source=? OR e.target=?)"}[direction]
            args = [node_id, node_id] if direction == "both" else [node_id]
            where = condition + " AND e.family IN (" + ",".join("?" for _ in families) + ")"
            args += families
            scope, scope_args = self._scope("e", document_id, "edge_docs", "edge_id")
            where += scope
            args += scope_args
            total = db.execute("SELECT count(*) FROM edges e WHERE " + where, args).fetchone()[0]
            rows = db.execute("SELECT e.data FROM edges e WHERE " + where + " ORDER BY CASE e.family WHEN 'semantic' THEN 0 WHEN 'navigation' THEN 1 ELSE 2 END,e.predicate,e.edge_id LIMIT ? OFFSET ?", [*args, limit, offset])
            items = []
            for row in rows:
                item = json.loads(row[0])
                other = item["target_node_id"] if item["source_node_id"] == node_id else item["source_node_id"]
                item["direction"] = "out" if item["source_node_id"] == node_id else "in"
                item["node"] = json.loads(db.execute("SELECT data FROM nodes WHERE node_id=?", (other,)).fetchone()[0])
                evidence_scope = " AND json_extract(v.data,'$.document_id')=?" if document_id else ""
                evidence_args = [item["edge_id"], document_id] if document_id else [item["edge_id"]]
                item["evidence_count"] = db.execute(
                    "SELECT count(*) FROM edge_evidence ee JOIN evidence v ON v.evidence_id=ee.evidence_id WHERE ee.edge_id=?" + evidence_scope,
                    evidence_args).fetchone()[0]
                items.append(item)
            return self._page(items, total, limit, offset, fingerprint)

    def evidence(self, *, edge_id="", evidence_id="", node_id="", document_id="", limit=20, cursor=""):
        limit = self._limit(limit)
        if sum(bool(v) for v in (edge_id, evidence_id, node_id)) != 1:
            raise ValidationError("edge_id、evidence_id、node_id 必须且只能指定一个")
        offset, fingerprint = self._offset(cursor, ["evidence", edge_id, evidence_id, node_id, document_id])
        with self._db() as db:
            if edge_id:
                where = "v.evidence_id IN (SELECT evidence_id FROM edge_evidence WHERE edge_id=?)"
                args = [edge_id]
            elif evidence_id:
                where, args = "v.evidence_id=?", [evidence_id]
            else:
                self.node(node_id, document_id=document_id)
                where = "v.evidence_id IN (SELECT ee.evidence_id FROM edge_evidence ee JOIN edges e ON e.edge_id=ee.edge_id WHERE e.source=? OR e.target=?)"
                args = [node_id, node_id]
            if document_id:
                where += " AND json_extract(v.data,'$.document_id')=?"
                args.append(document_id)
            total = db.execute("SELECT count(*) FROM evidence v WHERE " + where, args).fetchone()[0]
            if not total and (edge_id or evidence_id):
                exists = db.execute("SELECT 1 FROM edges WHERE edge_id=?", (edge_id,)).fetchone() if edge_id else None
                if not exists or document_id:
                    raise NotFoundError("证据不存在或不在当前范围")
            rows = db.execute("SELECT v.data FROM evidence v WHERE " + where + " ORDER BY v.evidence_id LIMIT ? OFFSET ?", [*args, limit, offset])
            return self._page([json.loads(r[0]) for r in rows], total, limit, offset, fingerprint)
