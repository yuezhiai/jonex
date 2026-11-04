"""Version-bound functional reads and review overlays over immutable source resources."""
from __future__ import annotations
import asyncio
import base64
import hashlib
import json
import re
import sqlite3
from collections import OrderedDict
from threading import RLock
from datetime import datetime, timezone

from jonex_wiki.adapters.bundle_view import DirectoryBundleView
from jonex_wiki.domain.engine.compile.resource_catalog import PATH
from jonex_wiki.kernel.errors import NotFoundError, ValidationError, ConflictError

OPERATIONS = {"find_resources", "get_question", "get_evidence", "get_media_segment", "lookup_records", "review_resources"}
_TABLE_STATS = OrderedDict()
_TABLE_STATS_LOCK = RLock()


def _cursor(value):
    return base64.urlsafe_b64encode(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).decode()


def _position(cursor, identity):
    if not cursor:
        return 0
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor))
        if data["identity"] != identity or not isinstance(data["position"], int) or data["position"] < 0:
            raise ValueError()
        return data["position"]
    except Exception as exc:
        raise ValidationError("RESOURCE_CURSOR_SCOPE_MISMATCH") from exc


def _text(view, evidence, max_chars=32768, offset=0):
    if not str(evidence["path"]).startswith("sources/"):
        raise ValidationError("RESOURCE_EVIDENCE_PATH_INVALID")
    data = view.read_bytes(evidence["path"])
    plain = (json.loads(data)["pages"][evidence["json_page"]]["text"] if "json_page" in evidence else data.decode("utf-8"))
    if hashlib.sha256(plain.encode()).hexdigest() != evidence["body_sha256"]:
        raise ValidationError("RESOURCE_EVIDENCE_HASH_MISMATCH")
    body = plain[evidence["start_char"]:evidence["end_char"]]
    return {**evidence, "text": body[offset:offset + max_chars], "text_offset": offset,
        "has_more": offset + max_chars < len(body), "total_chars": len(body)}


def lookup_records(view, *, table_id, filters=None, filter_groups=None, limit=100, cursor="", include_hidden=False):
    """Restricted SQL predicates over stored strings. No model-generated SQL or numeric coercion."""
    registry = json.loads(view.read_bytes("sources/registry.json"))
    source = next((s for s in registry["sources"] if table_id in s.get("table_ids", [])), None)
    if not source or not source.get("tables"):
        raise NotFoundError("RESOURCE_TABLE_NOT_FOUND")
    if ((filters is not None and not isinstance(filters, list))
            or (filter_groups is not None and not isinstance(filter_groups, list))
            or not isinstance(include_hidden, bool)):
        raise ValidationError("RESOURCE_FILTER_INVALID")
    filters, groups = filters or [], filter_groups or []
    if any(not isinstance(g, list) for g in groups):raise ValidationError("RESOURCE_FILTER_INVALID")
    if len(filters) > 32 or len(groups) > 32 or any(not g or len(g) > 32 for g in groups):
        raise ValidationError("RESOURCE_FILTER_LIMIT")
    from jonex_wiki.domain.engine.query.table_query import TableFilter
    try:
        filters = [TableFilter.model_validate(f) for f in filters]
        groups = [[TableFilter.model_validate(f) for f in group] for group in groups]
    except ValueError as exc:
        raise ValidationError("RESOURCE_FILTER_INVALID") from exc
    identity = hashlib.sha256(json.dumps([view.manifest_hash, table_id,
        [f.model_dump() for f in filters], [[f.model_dump() for f in g] for g in groups], include_hidden], sort_keys=True).encode()).hexdigest()
    position = _position(cursor, identity)
    with view.open_sqlite(source["tables"]) as connection:
        connection.row_factory = sqlite3.Row
        table = connection.execute("SELECT * FROM logical_tables WHERE table_id=?", (table_id,)).fetchone()
        interpretation = connection.execute("SELECT * FROM interpretations WHERE table_id=? ORDER BY version DESC LIMIT 1", (table_id,)).fetchone()
        if not table or not interpretation or interpretation["status"] != "accepted":
            raise ValidationError("RESOURCE_TABLE_STRUCTURE_UNCONFIRMED")
        fields = [dict(r) for r in connection.execute("SELECT * FROM field_map WHERE interpretation_id=? ORDER BY column_index", (interpretation["interpretation_id"],))]
        known = {}
        for field in fields:
            known[field["field_id"]] = field
            labels = [f for f in fields if f["label"] == field["label"]]
            if len(labels) == 1:
                known[field["label"]] = field

        def predicate(item):
            field = known.get(item.column)
            if field is None:
                raise ValidationError("RESOURCE_FIELD_UNKNOWN_OR_AMBIGUOUS")
            # Only explicit merges carry an anchor's value across a blank.
            actual = "CASE WHEN c.formula IS NOT NULL THEN c.cached_value WHEN c.empty_kind='merged_covered' THEN a.raw_value ELSE c.raw_value END"
            expression = "EXISTS (SELECT 1 FROM cells c LEFT JOIN merges m ON m.sheet_id=c.sheet_id AND c.row_index BETWEEN m.row_start AND m.row_end AND c.column_index BETWEEN m.column_start AND m.column_end LEFT JOIN cells a ON a.sheet_id=m.sheet_id AND a.row_index=m.row_start AND a.column_index=m.column_start WHERE c.sheet_id=r.sheet_id AND c.row_index=r.row_index AND c.column_index=? AND "
            values = [field["column_index"]]
            if item.operator == "not_empty":
                expression += f"{actual} IS NOT NULL AND {actual}!='')"
            elif item.operator == "in":
                expression += f"{actual} IN (" + ",".join("?" for _ in item.value) + "))"
                values += item.value
            elif item.operator == "contains":
                expression += f"instr({actual},?)>0)";values.append(item.value)
            else:
                expression += f"{actual}=?)";values.append(item.value)
            return expression, values

        header_end = max(f["header_row"] for f in fields)
        stats_key = (view.manifest_hash, table_id)
        with _TABLE_STATS_LOCK:
            stats = _TABLE_STATS.get(stats_key)
        if stats is None:
            formula_columns = {r[0] for r in connection.execute("SELECT DISTINCT column_index FROM cells WHERE sheet_id=? AND formula IS NOT NULL", (table["sheet_id"],))}
            merge_columns = set()
            for merge in connection.execute("SELECT column_start,column_end FROM merges WHERE sheet_id=?", (table["sheet_id"],)):
                merge_columns.update(range(merge[0], merge[1] + 1))
            unresolved = connection.execute("SELECT COUNT(*) FROM cells WHERE sheet_id=? AND row_index>? AND row_index<=? AND column_index BETWEEN ? AND ? AND formula IS NOT NULL AND cached_value IS NULL", (table["sheet_id"], header_end, table["row_end"], table["column_start"], table["column_end"])).fetchone()[0]
            stats = formula_columns, merge_columns, unresolved
            with _TABLE_STATS_LOCK:
                _TABLE_STATS[stats_key] = stats
                while len(_TABLE_STATS) > 64:_TABLE_STATS.popitem(last=False)
        formula_columns, merge_columns, unresolved = stats

        def fast_candidate(item):
            field = known.get(item.column)
            values = item.value if isinstance(item.value, list) else [item.value]
            if (field is None or item.operator not in {"equals", "in"} or not all(values)
                    or field["column_index"] in formula_columns | merge_columns):
                return None
            return ("SELECT row_index FROM table_terms WHERE table_id=? AND term IN (" + ",".join("?" for _ in values) + ") AND column_index=?",
                    [table_id, *values, field["column_index"]])

        where = ["r.sheet_id=?", "r.row_index>?", "r.row_index<=?", "r.column_index BETWEEN ? AND ?"]
        params = [table["sheet_id"], header_end, table["row_end"], table["column_start"], table["column_end"]]
        if not include_hidden:
            where.append("NOT EXISTS (SELECT 1 FROM cells h WHERE h.sheet_id=r.sheet_id AND h.row_index=r.row_index AND h.hidden_row=1)")
        for item in filters:
            expression, values = predicate(item);where.append(expression);params += values
        if groups:
            branches = []
            for group in groups:
                clauses = []
                for item in group:
                    expression, values = predicate(item);clauses.append(expression);params += values
                branches.append("(" + " AND ".join(clauses) + ")")
            where.append("(" + " OR ".join(branches) + ")")
        # The existing term index narrows rare exact literals before joining
        # complete rows. It never prunes a branch lacking a proven exact index.
        common = next((candidate for f in filters if (candidate := fast_candidate(f)) is not None), None)
        narrowed = [common] if common else [next((candidate for f in group if (candidate := fast_candidate(f)) is not None), None) for group in groups]
        if narrowed and all(narrowed):
            where.append("r.row_index IN (" + " UNION ".join(c[0] for c in narrowed) + ")")
            for _, values in narrowed:params += values
        base = " FROM cells r WHERE " + " AND ".join(where)
        total = connection.execute("SELECT COUNT(DISTINCT r.row_index)" + base, params).fetchone()[0]
        selected = connection.execute("SELECT DISTINCT r.row_index" + base + " AND r.row_index>? ORDER BY r.row_index LIMIT ?", [*params, position, limit + 1]).fetchall()
        has_more = len(selected) > limit
        ids = [r[0] for r in selected[:limit]]
        records = []
        for row in ids:
            cells = [dict(c) for c in connection.execute("SELECT * FROM cells WHERE sheet_id=? AND row_index=? AND column_index BETWEEN ? AND ? ORDER BY column_index", (table["sheet_id"], row, table["column_start"], table["column_end"]))]
            records.append({"row_id": row, "document_id": source["document_id"], "table_id": table_id, "cells": cells})
        context = json.loads(interpretation["payload_json"])
        observations = None
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='table_observations'").fetchone():
            obs = connection.execute("SELECT caption_json,footnote_json,raw_item_json,page_number,bbox_json FROM table_observations WHERE table_id=?", (table_id,)).fetchone()
            if obs:
                raw = json.loads(obs["raw_item_json"])
                observations = {"captions": json.loads(obs["caption_json"]), "footnotes": json.loads(obs["footnote_json"]),
                    "native_locator": (raw.get("raw_item") or {}).get("native_locator"),
                    "page_number": obs["page_number"], "bbox": json.loads(obs["bbox_json"] or "null")}
    return {"records": records, "fields": fields, "context": context, "observations": observations,
        "total": total, "has_more": has_more, "next_cursor": _cursor({"identity": identity, "position": ids[-1]}) if has_more and ids else None,
        "coverage": {"scope": "selected_table_and_predicates", "returned_rows": ids,
            "total_matches": total, "page_complete": not has_more, "started_at_beginning": not cursor,
            "unresolved_formulas": unresolved, "exact_values_available": not unresolved,
            "numeric_coercion": False, "branches": len(groups),
            "applied_filters": [f.model_dump() for f in filters],
            "applied_filter_groups": [[f.model_dump() for f in group] for group in groups],
            "predicate_coverage": "supplied_predicates_only",
            "natural_language_conditions": "not_verified"}}


async def query_resources(layout, tenant_id, kb_id, operation, options, *, actor=""):
    if operation not in OPERATIONS:
        raise ValidationError("RESOURCE_OPERATION_UNKNOWN")
    options = dict(options)
    allowed_by_operation = {
        "find_resources": {"query", "kind", "document_id", "edition", "related_to", "reviewed_only"},
        "get_question": {"object_id"}, "get_evidence": {"evidence_id"},
        "get_media_segment": {"object_id"},
        "lookup_records": {"table_id", "filters", "filter_groups", "include_hidden"},
        "review_resources": {"object_id", "expected_version", "review_status", "note", "relations"}}
    if set(options) - allowed_by_operation[operation] - {"revision", "limit", "cursor"}:
        raise ValidationError("RESOURCE_OPTIONS_INVALID")
    revision = str(options.pop("revision", "") or "")
    if revision and not re.fullmatch(r"[0-9a-f]{64}", revision):
        raise ValidationError("RESOURCE_REVISION_INVALID")
    if operation != "find_resources" and not revision:
        raise ValidationError("RESOURCE_REVISION_REQUIRED; search resources first")
    limit = options.pop("limit", 50)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValidationError("RESOURCE_LIMIT_INVALID")
    cursor = str(options.pop("cursor", "") or "")
    if len(cursor) > 4096:
        raise ValidationError("RESOURCE_CURSOR_INVALID")
    async with layout.version_lock(tenant_id, kb_id):
        pub = layout.versions_dir(tenant_id, kb_id, revision) if revision else layout.resolve_published(tenant_id, kb_id)
        versions = (layout.kb_dir(tenant_id, kb_id) / "compile/versions").resolve()
        if not pub or not pub.is_dir() or pub.resolve().parent != versions:
            raise NotFoundError("RESOURCE_PUBLICATION_NOT_FOUND")
        view = DirectoryBundleView(tenant_id=tenant_id, context_id=kb_id, revision_dir=pub)
        if PATH not in view.list_entries():
            raise NotFoundError("RESOURCE_INDEX_UNAVAILABLE; recompile this knowledge base")
        catalog = json.loads(await asyncio.to_thread(view.read_bytes, PATH))
        objects = {o["object_id"]: o for o in catalog["objects"]}
        evidence = {e["evidence_id"]: e for e in catalog["evidence"]}
        overlays = layout.kb_dir(tenant_id, kb_id) / "reviews" / (view.manifest_hash + ".json")
        review = json.loads(overlays.read_text("utf-8")) if overlays.is_file() else {"version": 0, "objects": {}, "relations": [], "events": []}
        if operation == "review_resources":
            if not actor:
                raise ValidationError("RESOURCE_REVIEW_ACTOR_REQUIRED")
            if isinstance(options.get("expected_version"), bool) or options.get("expected_version") != review["version"]:
                raise ConflictError("RESOURCE_REVIEW_VERSION_CHANGED")
            oid = options.get("object_id")
            if oid not in objects:
                raise NotFoundError("RESOURCE_OBJECT_NOT_FOUND")
            allowed = {"object_id", "expected_version", "review_status", "note", "relations"}
            if set(options) - allowed or options.get("review_status") not in {"accepted", "rejected", "candidate"}:
                raise ValidationError("RESOURCE_REVIEW_FIELDS_INVALID")
            note = str(options.get("note") or "")
            if len(note) > 4000:
                raise ValidationError("RESOURCE_REVIEW_NOTE_LIMIT")
            if len(options.get("relations") or []) > 32:raise ValidationError("RESOURCE_REVIEW_RELATION_LIMIT")
            for relation in options.get("relations") or []:
                if (relation.get("source") not in objects or relation.get("target") not in objects
                    or relation.get("type") not in {"answers", "uses_concept", "prerequisite", "explains", "corrects", "same_edition"}
                    or not relation.get("evidence_ids") or any(e not in evidence for e in relation["evidence_ids"])):
                    raise ValidationError("RESOURCE_REVIEW_RELATION_INVALID")
                relation_id = hashlib.sha256(json.dumps([relation["source"], relation["type"], relation["target"],
                    sorted(relation["evidence_ids"])], sort_keys=True).encode()).hexdigest()
                review["relations"] = [r for r in review["relations"] if r.get("relation_id") != relation_id]
                review["relations"].append({**relation, "relation_id": relation_id,
                    "review_status": options["review_status"], "actor": actor})
            review["objects"][oid] = {"review_status": options["review_status"], "note": note, "actor": actor}
            review["version"] += 1
            review["events"].append({"at": datetime.now(timezone.utc).isoformat(), "actor": actor, "object_id": oid, "version": review["version"]})
            overlays.parent.mkdir(parents=True, exist_ok=True)
            layout.atomic_write(overlays, json.dumps(review, ensure_ascii=False).encode())
            return {"revision": view.manifest_hash, "review_version": review["version"], "object_id": oid}
        for oid, patch in review["objects"].items():
            if oid in objects:
                objects[oid] = {**objects[oid], **patch}
        common = {"revision": view.manifest_hash, "review_version": review["version"], "gaps": catalog.get("gaps", [])}
        relations = catalog["relations"] + review["relations"]
        if operation == "lookup_records":
            allowed = {"table_id", "filters", "filter_groups", "include_hidden"}
            if set(options) - allowed:
                raise ValidationError("RESOURCE_LOOKUP_FIELDS_INVALID")
            return {**common, **await asyncio.to_thread(lookup_records, view, limit=limit, cursor=cursor, **options)}
        if operation == "find_resources":
            allowed = {"query", "kind", "document_id", "edition", "related_to", "reviewed_only"}
            if set(options) - allowed:
                raise ValidationError("RESOURCE_SEARCH_FIELDS_INVALID")
            candidates = list(objects.values())
            for key in ("kind", "document_id", "edition"):
                if options.get(key):candidates = [o for o in candidates if o.get(key) == options[key]]
            if options.get("query"):
                query = str(options["query"]).casefold()
                if len(query) > 500:raise ValidationError("RESOURCE_QUERY_LIMIT")
                texts, consumed, matched, limited = {}, 0, [], False
                for obj in candidates:
                    if query in obj["label"].casefold():matched.append(obj);continue
                    for eid in obj["evidence_ids"]:
                        ev = evidence[eid]
                        if ev["path"] not in texts:
                            if consumed >= 16 * 1024 * 1024:
                                limited = True;break
                            data = view.read_bytes(ev["path"]);consumed += len(data)
                            texts[ev["path"]] = data.decode()
                        plain = texts[ev["path"]]
                        if "json_page" in ev:
                            plain = json.loads(plain)["pages"][ev["json_page"]]["text"]
                        if query in plain[ev["start_char"]:ev["end_char"]].casefold():
                            matched.append(obj);break
                candidates = matched
                common["search_coverage"] = {"mode": "literal_labels_and_source_evidence", "budget_limited": limited}
            if options.get("reviewed_only"):
                candidates = [o for o in candidates if o["review_status"] in {"accepted", "source"}]
            if options.get("related_to"):
                if options["related_to"] not in objects:raise NotFoundError("RESOURCE_OBJECT_NOT_FOUND")
                neighbors = {r["target"] if r["source"] == options["related_to"] else r["source"] for r in relations
                    if options["related_to"] in {r["source"], r["target"]}
                    and (not options.get("reviewed_only") or r["review_status"] in {"source", "accepted"})}
                candidates = [o for o in candidates if o["object_id"] in neighbors]
            identity = hashlib.sha256(json.dumps([view.manifest_hash, review["version"], options], sort_keys=True).encode()).hexdigest()
            offset = _position(cursor, identity);end = offset + limit
            return {**common, "objects": candidates[offset:end], "total": len(candidates), "has_more": end < len(candidates),
                "next_cursor": _cursor({"identity": identity, "position": end}) if end < len(candidates) else None}
        if operation == "get_evidence":
            selected = evidence.get(options.get("evidence_id"))
            if selected is None:raise NotFoundError("RESOURCE_EVIDENCE_NOT_FOUND")
            offset = _position(cursor, selected["evidence_id"] + view.manifest_hash)
            result = await asyncio.to_thread(_text, view, selected, 32768, offset)
            result["next_cursor"] = _cursor({"identity": selected["evidence_id"] + view.manifest_hash, "position": offset + len(result["text"])}) if result["has_more"] else None
            return {**common, "evidence": result}
        obj = objects.get(options.get("object_id"))
        if obj is None:raise NotFoundError("RESOURCE_OBJECT_NOT_FOUND")
        if operation == "get_question":
            if obj["kind"] != "question":raise ValidationError("RESOURCE_NOT_A_QUESTION")
            children = [o for o in objects.values() if o.get("parent_id") == obj["object_id"]]
            ev = [await asyncio.to_thread(_text, view, evidence[e]) for e in obj["evidence_ids"]]
            return {**common, "question": obj, "children": children, "evidence": ev,
                "relations": [r for r in relations if obj["object_id"] in {r["source"], r["target"]}]}
        if operation == "get_media_segment":
            if obj["kind"] not in {"media_segment", "video_frame"}:raise ValidationError("RESOURCE_NOT_MEDIA")
            source = next(o for o in objects.values() if o["kind"] == "document" and o["document_id"] == obj["document_id"])
            return {**common, "segment": obj, "source": source,
                "evidence": [await asyncio.to_thread(_text, view, evidence[e]) for e in obj["evidence_ids"]],
                "playback": {"document_id": obj["document_id"], "original_sha256": source["source_sha256"],
                    **obj["locator"], "time_precision": "segment", "word_timing_available": False}}
        raise ValidationError("RESOURCE_OPERATION_UNKNOWN")
