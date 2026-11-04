"""Location hints from sealed resources for evidence that this run actually read."""
import json


def media_span_handles(state, reading_view):
    """Create time-bound citations only for segments wholly inside a delivered range."""
    view = state.get("bundle_view")
    if view is None or "query/resources.json" not in getattr(view, "list_entries", lambda: ())():return [], {}
    catalog = state.get("resource_citation_catalog")
    if catalog is None:
        catalog = json.loads(view.read_bytes("query/resources.json"));state["resource_citation_catalog"] = catalog
    evidence = {e["evidence_id"]: e for e in catalog["evidence"]}
    handles, refs = [], {}
    for obj in catalog["objects"]:
        if obj["kind"] != "media_segment" or obj["document_id"] != reading_view.source_id:continue
        ev = evidence[obj["evidence_ids"][0]]
        start, end = ev["start_char"], ev["end_char"]
        if ev["path"] != reading_view.article_path or end <= start:continue
        if not any(a <= start and end <= b for a, b in reading_view.spans):continue
        texts = state.setdefault("resource_article_texts", {})
        if ev["path"] not in texts:texts[ev["path"]] = view.read_bytes(ev["path"]).decode()
        text = texts[ev["path"]][start:end]
        claim = "source_media_" + obj["object_id"]
        handles.append({"claim_id": claim, "passage_id": claim, "source_id": obj["document_id"],
            "document_id": obj["document_id"], "chunk_id": obj["chunk_id"], "excerpt": text[:300],
            "page_path": ev["path"]})
        refs[claim] = {"kind": "media_segment", "revision": view.manifest_hash,
            "document_id": obj["document_id"], "object_id": obj["object_id"],
            "original_sha256": obj["source_sha256"], "locator": obj["locator"],
            "evidence_kind": obj.get("evidence_kind"), "time_precision": "segment",
            "path": ev["path"], "spans": [[start, end]], "body_sha256": ev["body_sha256"]}
    return handles, refs


def media_matches(state, record):
    view = state.get("bundle_view")
    path = "query/resources.json"
    if view is None or path not in getattr(view, "list_entries", lambda: ())():return []
    catalog = state.get("resource_citation_catalog")
    if catalog is None:
        catalog = json.loads(view.read_bytes(path));state["resource_citation_catalog"] = catalog
    candidates = [o for o in catalog.get("objects", []) if o["kind"] == "media_segment" and o["document_id"] == record.get("document_id")]
    exact = [o for o in candidates if o.get("chunk_id") and o["chunk_id"] == record.get("chunk_id")]
    if exact:return exact
    excerpt = " ".join(str(record.get("excerpt") or "").split())
    if len(excerpt) < 10:return []
    evidence = {e["evidence_id"]: e for e in catalog.get("evidence", [])}
    texts = state.setdefault("resource_article_texts", {})
    matches = []
    for obj in candidates:
        ev = evidence[obj["evidence_ids"][0]]
        if ev["path"] not in texts:
            texts[ev["path"]] = view.read_bytes(ev["path"]).decode()
        text = texts[ev["path"]][ev["start_char"]:ev["end_char"]]
        if excerpt in " ".join(text.split()):matches.append(obj)
    return matches


def attach_media_locations(state, payload):
    query_state = state["query_state"]
    locations = []
    for evidence_id in query_state.active_evidence_ids:
        record = query_state.seen_evidence_by_id[evidence_id]
        matches = media_matches(state, record.model_dump())
        if len(matches) == 1:
            obj = matches[0]
            locations.append({"handle": query_state.evidence_handle(evidence_id),
                "document_id": obj["document_id"], "locator": obj["locator"],
                "evidence_kind": obj.get("evidence_kind"), "time_precision": "segment",
                "note": "Segment interval, not word-level timing; generated descriptions are not verbatim speech."})
    if locations:payload["evidence_locations"] = locations[:32]
