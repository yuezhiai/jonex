"""Source-contribution discovery, independent of page size and QA inputs.

Names are recall keys, never global entity identities. Every occurrence keeps
its source record, qualifiers and old claim displays. A relation is an existing
source-backed proposal, not a new assertion inferred from co-occurrence.
"""
from __future__ import annotations

from collections import Counter, deque
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import re
import unicodedata
import zlib

RULE = "source-record-aggregation-v5"
SUPPORT_LEGACY_RULE = "source-record-aggregation-v4"
LEGACY_RULE = "source-record-aggregation-v2"
SCOPED_LEGACY_RULE = "source-record-aggregation-v3"


def published_records(access):
    """Stream checksummed contribution partitions from an operator-pinned Bundle."""
    from jonex_wiki.domain.engine.json_artifacts import MARKER, _part_values
    path = "source_contributions.json"
    data = json.loads(access.read_text(path))
    if MARKER not in data:
        yield from data.get("records", [])
        return
    spec = data[MARKER]["fields"]["records"]
    if spec.get("kind") != "list":
        raise ValueError("CONTRIBUTION_PARTITION_KIND")
    for part in spec["parts"]:
        name = part["path"]
        if not name.startswith("json-shards/source_contributions/records/") or ".." in Path(name).parts:
            raise ValueError("CONTRIBUTION_PARTITION_PATH")
        yield from _part_values(lambda p: access.path(p).read_bytes(), part).values()


def normalized(value):
    return " ".join(unicodedata.normalize("NFKC", str(value)).casefold().split()).strip(' \"“”')


def fts_text(value):
    """Keep original text and index the same mixed CJK tokens used by find.

    unicode61 otherwise treats a continuous Chinese sentence as one token.
    This changes discovery only; stored text and evidence coordinates stay exact.
    """
    value=str(value)
    if not re.search(r'[\u3400-\u9fff]',value):return value
    try:
        import jieba
        jieba.setLogLevel(40)
        terms=jieba.cut_for_search(unicodedata.normalize('NFKC',value).casefold())
        return value+'\n'+' '.join(t for t in terms if t.isalnum())
    except ImportError:
        return value+'\n'+' '.join(value[i:i+2] for i in range(len(value)-1)
            if all('\u3400'<=c<='\u9fff' for c in value[i:i+2]))


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def record_labels(record):
    """Reuse model names and arbitrary relation/subject expressions, including Chinese."""
    names = record.get("original_names", {})
    labels = [(n, "name") for n in (names.values() if isinstance(names, dict) else names)]
    labels += [(n, "mention") for n in record.get("entity_mentions", [])]
    if record.get("topic_hint"):
        labels.append((record["topic_hint"], "topic"))
    for relation in record.get("link_proposals", []):
        labels += [(relation.get("subject_mention", ""), "relation_subject"),
                   (relation.get("target_mention", ""), "relation_target")]
    result = {}
    for raw, role in labels:
        key = normalized(raw)
        if key:
            result.setdefault(key, (str(raw), role))
    return result


def build_index(records, destination, *, scope):
    """Consume source-independent records once; retain no copies of source bodies.

    ``scope`` must bind the tenant, KB and immutable source publication. A
    caller cannot select a different scope when opening or resuming the index.
    """
    if not all(scope.get(k) for k in ("tenant", "kb", "publication")):
        raise ValueError("AGGREGATION_SCOPE_REQUIRED")
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".building")
    if temporary.exists():
        raise FileExistsError(temporary)
    counts = Counter()
    digest = hashlib.sha256((RULE + ':' + encoded(scope)).encode())
    try:
        with sqlite3.connect(temporary) as db:
            db.executescript("""
                CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE records(ordinal INTEGER PRIMARY KEY,id TEXT UNIQUE,payload BLOB NOT NULL);
                CREATE TABLE occurrences(label TEXT,record INTEGER,raw TEXT,role TEXT,
                    PRIMARY KEY(label,record));
                CREATE INDEX by_record ON occurrences(record,label);
                CREATE TABLE record_sources(record INTEGER,source_id TEXT,
                    PRIMARY KEY(record,source_id));
                CREATE INDEX source_records ON record_sources(source_id,record);
                CREATE TABLE support_groups(record INTEGER,group_no INTEGER,PRIMARY KEY(record,group_no));
                CREATE TABLE support_sources(record INTEGER,group_no INTEGER,source_id TEXT,
                    PRIMARY KEY(record,group_no,source_id));
                CREATE VIRTUAL TABLE search USING fts5(names,body,content='',
                    tokenize='unicode61 remove_diacritics 2');
            """)
            for record in records:
                if hasattr(record, "model_dump"):
                    record = record.model_dump()
                if {"question", "answer", "supporting_facts", "diagnosis"} & record.keys():
                    raise ValueError("QA_FIELDS_NOT_COMPILER_INPUTS")
                rid = record["record_id"]
                labels = record_labels(record)
                # Exact spans/claims remain compatible; complete original text
                # stays in the parent publication rather than every packet.
                # Cold payloads are compressed: retain every old/extension field,
                # including qualifiers and proposal evidence, without projection loss.
                payload = deepcopy(record)
                for span in payload.get('source_spans') or []:
                    if span.get('text'):
                        span.setdefault('source_excerpt',span['text'])
                for proposal in payload.get('link_proposals') or []:
                    proposal.setdefault('evidence_locations',[{k:ref.get(k) for k in
                        ('source_id','passage_id','chunk_id')} for ref in proposal.get('evidence_refs',[])])
                content = encoded(payload)
                previous = db.execute("SELECT payload FROM records WHERE id=?", (rid,)).fetchone()
                if previous:
                    if zlib.decompress(previous[0]).decode() != content:
                        raise ValueError("CONTRIBUTION_ID_COLLISION")
                    continue
                ordinal = db.execute("INSERT INTO records(id,payload) VALUES(?,?)",
                                     (rid, zlib.compress(content.encode(), 6))).lastrowid
                db.executemany("INSERT INTO occurrences VALUES(?,?,?,?)",
                    [(key, ordinal, raw, role) for key, (raw, role) in labels.items()])
                db.executemany("INSERT INTO record_sources VALUES(?,?)",[
                    (ordinal,sid) for sid in sorted({s['source_id'] for s in payload.get('source_spans') or []})])
                from jonex_wiki.domain.reading.support_scope import legal_support, source_of
                for group_no, refs in enumerate(legal_support(payload)):
                    db.execute('INSERT INTO support_groups VALUES(?,?)',(ordinal,group_no))
                    db.executemany('INSERT INTO support_sources VALUES(?,?,?)',
                        [(ordinal,group_no,sid) for sid in sorted({source_of(ref) for ref in refs})])
                db.execute("INSERT INTO search(rowid,names,body) VALUES(?,?,?)",
                    (ordinal, fts_text(" ".join(v[0] for v in labels.values())), fts_text(record.get("proposition", ""))))
                digest.update((rid + ":" + content + "\n").encode())
                counts["records"] += 1
                counts["occurrences"] += len(labels)
                for field in ("original_names", "entity_mentions", "source_spans", "qualifiers", "link_proposals"):
                    counts["with_" + field] += bool(record.get(field))
                counts["without_recall_labels"] += not bool(labels)
                counts["relation_proposals"] += sum(bool(p.get("relation")) for p in record.get("link_proposals", []))
            identity = digest.hexdigest()
            db.executemany("INSERT INTO meta VALUES(?,?)", [
                ("rule", RULE), ("scope", encoded(scope)), ("identity", identity),
                ("coverage", encoded(dict(counts)))])
            db.execute("INSERT INTO search(search) VALUES('optimize')")
        temporary.replace(destination)
    except BaseException:
        # Preserve interrupted output for diagnosis; never replace an old index.
        raise
    return {**dict(counts), "identity": identity, "bytes": destination.stat().st_size}


class AggregationIndex:
    def __init__(self, path, *, scope):
        self.db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        meta = dict(self.db.execute("SELECT key,value FROM meta"))
        if meta.get("rule") not in {RULE,SUPPORT_LEGACY_RULE,LEGACY_RULE,SCOPED_LEGACY_RULE} or meta.get("scope") != encoded(scope):
            self.db.close()
            raise ValueError("AGGREGATION_PUBLICATION_MISMATCH")
        self.identity = meta["identity"]
        self.rule = meta['rule']
        self.scope = deepcopy(scope)
        self.cache = {}

    def close(self):
        self.db.close()

    def record(self, rid):
        row = self.db.execute("SELECT payload FROM records WHERE id=?", (rid,)).fetchone()
        if row is None:
            raise ValueError("UNKNOWN_SOURCE_RECORD")
        return json.loads(zlib.decompress(row[0]))

    def find(self, terms, *, limit=8, source_ids=None):
        if not 1 <= limit <= 40:
            raise ValueError("AGGREGATION_SEARCH_LIMIT")
        terms = list(dict.fromkeys(normalized(t) for t in terms if normalized(t)))[:40]
        if not terms:
            return []
        expression = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
        condition='';args=[expression]
        if source_ids is not None:
            if not source_ids or not all(isinstance(s,str) and s for s in source_ids):
                raise ValueError('AGGREGATION_SOURCE_SCOPE_REQUIRED')
            if self.rule==LEGACY_RULE:
                raise ValueError('LEGACY_INDEX_REQUIRES_DERIVED_SOURCE_SCOPE_INDEX')
            # Apply the entire source constraint BEFORE ranking and LIMIT.
            # A mixed-source record is eligible only if all its spans fit.
            condition=''' AND EXISTS(SELECT 1 FROM record_sources s WHERE s.record=r.ordinal)
                AND NOT EXISTS(SELECT 1 FROM record_sources s WHERE s.record=r.ordinal
                    AND s.source_id NOT IN (SELECT value FROM json_each(?)))'''
            if self.rule in {RULE,SUPPORT_LEGACY_RULE}:
                condition=''' AND EXISTS(SELECT 1 FROM support_groups g WHERE g.record=r.ordinal
                    AND NOT EXISTS(SELECT 1 FROM support_sources s
                        WHERE s.record=g.record AND s.group_no=g.group_no
                        AND s.source_id NOT IN (SELECT value FROM json_each(?))))'''
            args.append(encoded(sorted(set(source_ids))))
        args.append(limit)
        return [self.record(rid) for rid, in self.db.execute(
            "SELECT r.id FROM search JOIN records r ON r.ordinal=search.rowid WHERE search MATCH ?"+condition+
            " ORDER BY bm25(search,3,1),r.id LIMIT ?",args)]

    def discover(self, seeds=(), *, max_new_records=1000, max_expansions=1000, cursor=None):
        """BFS over ALL registered occurrences; budget stops retain exact frontier.

        There is no depth/branch/article limit. The cursor retains the position
        inside a high-degree name as well as unexpanded names. It can be saved
        and resumed without rereading or silently dropping branch three.
        """
        if max_new_records < 1 or max_expansions < 1:
            raise ValueError("POSITIVE_DISCOVERY_BUDGET_REQUIRED")
        if cursor is not None and cursor.get("identity") != self.identity:
            raise ValueError("DISCOVERY_CURSOR_PUBLICATION_MISMATCH")
        key = encoded([list(seeds), max_new_records, max_expansions, cursor])
        if key in self.cache:
            return deepcopy(self.cache[key])
        state = deepcopy(cursor) if cursor is not None else {
            "identity": self.identity, "records": [], "expanded": [],
            "frontier": [{"label": k, "after": "", "via": None} for k in sorted({normalized(s) for s in seeds if normalized(s)})],
            "connections": []}
        queue = deque(state["frontier"])
        expanded, found = set(state["expanded"]), set(state["records"])
        queued = expanded | {item["label"] for item in queue}
        new_records = expansions = 0
        while queue and new_records < max_new_records and expansions < max_expansions:
            item = queue.popleft()
            label, last = item["label"], item["after"]
            # Stream occurrences rather than loading the whole hub at once.
            rows = self.db.execute("SELECT r.id,o.role,o.record FROM occurrences o JOIN records r ON r.ordinal=o.record WHERE label=? AND r.id>? ORDER BY r.id",
                                   (label, last))
            interrupted = False
            for rid, role, ordinal in rows:
                if rid not in found and new_records >= max_new_records:
                    queue.appendleft({**item, "after": last})
                    interrupted = True
                    break
                last = rid
                if rid in found:
                    continue
                found.add(rid)
                state["records"].append(rid)
                new_records += 1
                state["connections"].append({"label": label, "record": rid, "via": item["via"],
                    "status": "source_occurrence_candidate", "role": role,
                    "identity_verified": False})
                for next_label, next_role in self.db.execute(
                        "SELECT label,role FROM occurrences WHERE record=? ORDER BY label", (ordinal,)):
                    if next_label not in queued:
                        queued.add(next_label)
                        queue.append({"label": next_label, "after": "", "via": rid})
            if not interrupted:
                expanded.add(label)
                expansions += 1
        state.update(expanded=sorted(expanded), frontier=list(queue), exhausted=not queue,
                     stop_reason="registered_occurrences_exhausted" if not queue else "discovery_budget",
                     new_records=new_records, expansions_this_call=expansions,
                     scope=self.scope, identity_policy="Equal labels retrieve candidates; source/role/time/qualifiers remain distinct.")
        # Bound only the session cache; discovery results/frontiers are not cut.
        if len(self.cache) >= 16:
            self.cache.pop(next(iter(self.cache)))
        self.cache[key] = deepcopy(state)
        return state

    def sections(self, discovery, *, target_chars=12000):
        """Page capacity partitions a discovered set; never changes membership."""
        if discovery.get("identity") != self.identity or target_chars < 1:
            raise ValueError("SECTION_SCOPE_OR_CAPACITY_INVALID")
        groups = {}
        for rid in discovery["records"]:
            record = self.record(rid)
            sources = tuple(sorted({(s["source_id"], s.get("source_sha256", ""))
                                    for s in record.get("source_spans") or []}))
            group = (sources, record.get("topic_hint") or "Source material")
            groups.setdefault(group, []).append(record)
        result = []
        for (sources, heading), records in groups.items():
            current, size = [], 0
            for record in records:
                length = len(record.get("proposition") or "")
                if current and size + length > target_chars:
                    result.append({"heading": heading, "sources": sources, "records": current})
                    current, size = [], 0
                current.append(record)
                size += length
            if current:
                result.append({"heading": heading, "sources": sources, "records": current})
        return result
