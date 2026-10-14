"""Read one verified citation from its immutable query Bundle, without decoding the library."""
import asyncio
import hashlib
import json
import sqlite3

from jonex_wiki.kernel.errors import ConflictError, NotFoundError, ValidationError
from jonex_wiki.storage.repos import DocumentRepo, QueryTaskRepo

_MAX_TEXT = 20000


def _path(base, name):
    target = (base / name).resolve()
    if not target.is_relative_to(base.resolve()) or not target.is_file():
        raise NotFoundError("引用对应的版本文件不可用")
    return target


def _segments(text, spans):
    # Python code-point offsets stay on the server; the UI receives already split text.
    spans = sorted((int(a), int(b)) for a, b in spans)
    if not spans or any(a < 0 or a >= b or b > len(text) for a, b in spans):
        raise ValidationError("引用的原文范围无效")
    merged = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    result, remaining, requested = [], _MAX_TEXT, 0
    for a, b in merged:
        for left, right, highlighted in ((max(0, a-400), a, False), (a, b, True),
                                         (b, min(len(text), b+400), False)):
            requested += right-left
            if remaining <= 0:
                continue
            count = min(right-left, remaining)
            result.append({"text": text[left:left+count], "highlight": highlighted})
            remaining -= count
    return result, requested > _MAX_TEXT


def _table(base, citation, ref):
    path = _path(base, ref["path"])
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        table = db.execute("SELECT * FROM logical_tables WHERE table_id=?", (ref["table_id"],)).fetchone()
        if table is None:
            raise NotFoundError("引用的表格不存在")
        sheet_id = table["sheet_id"]
        if ref.get("sheet_id") and ref["sheet_id"] != sheet_id:
            raise ConflictError("引用的工作表不一致")
        sheet = db.execute("SELECT * FROM sheets WHERE sheet_id=?", (sheet_id,)).fetchone()
        row, column = ref.get("row"), ref.get("column")
        r0, r1 = table["row_start"], table["row_end"]
        c0, c1 = table["column_start"], table["column_end"]
        first = max(r0, (int(row)-3) if row is not None else int(ref.get("row_start") or r0))
        last = min(r1, first + 24)
        left = max(c0, int(column)-4) if column is not None else max(c0, int(ref.get("column_start") or c0))
        right = min(c1, left+19)
        # Header rows and explicitly cited context cells remain visible alongside the target.
        rows = sorted(set(range(first, last+1)) | set(range(r0, min(r0+2, r1)+1)) |
                      {int(v[0]) for v in ref.get("context_cells", [])})[:32]
        cols = sorted(set(range(left, right+1)) | {int(v[1]) for v in ref.get("context_cells", [])})[:24]
        placeholders = ",".join("?" for _ in rows)
        cells = db.execute(f"SELECT * FROM cells WHERE sheet_id=? AND row_index IN ({placeholders}) "
                           "AND column_index BETWEEN ? AND ? ORDER BY row_index,column_index",
                           (sheet_id, *rows, min(cols), max(cols))).fetchall()
        grid = []
        found = row is None
        for cell in cells:
            if cell["column_index"] not in cols:
                continue
            highlight = row == cell["row_index"] and column == cell["column_index"]
            if ref["kind"] == "table_range":
                highlight = (int(ref.get("row_start") or r0) <= cell["row_index"] <= int(ref.get("row_end") or r1)
                             and int(ref.get("column_start") or c0) <= cell["column_index"] <= int(ref.get("column_end") or c1))
            found = found or highlight
            value = cell["cached_value"] if cell["formula"] and cell["cached_value"] is not None else cell["raw_value"]
            grid.append({"row": cell["row_index"], "column": cell["column_index"],
                         "coordinate": cell["coordinate"], "value": value,
                         "formula": cell["formula"], "number_format": cell["number_format"],
                         "highlight": highlight})
        if not found:
            raise NotFoundError("引用的单元格不存在")
        page, bbox = None, None
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='table_observations'").fetchone():
            observation = db.execute("SELECT page_number,bbox_json FROM table_observations WHERE table_id=?",
                                     (ref["table_id"],)).fetchone()
            if observation:
                page, bbox = observation[0], json.loads(observation[1]) if observation[1] else None
        return {"kind": ref["kind"], "exact_location": True, "table_title": table["title"],
                "sheet_name": sheet["name"] if sheet else None, "rows": rows, "columns": cols,
                "cells": grid, "page_number": page, "bbox": bbox,
                "truncated": first > r0 or last < r1 or left > c0 or right < c1 or len(citation.get("excerpt", "")) > _MAX_TEXT,
                "segments": [{"text": citation.get("excerpt", "")[:_MAX_TEXT], "highlight": True}]}


def _legacy_snapshot(base, citation):
    index_path = base / "query/source_index.json"
    if not index_path.is_file() or index_path.stat().st_size > 16 * 1024 * 1024:
        return None  # Never decode a legacy library-wide JSON to preview one citation.
    from jonex_wiki.domain.engine.json_artifacts import load_json_artifact
    from jonex_wiki.domain.engine.source_index import restore_source_index
    index = restore_source_index(load_json_artifact(
        lambda name: _path(base, name).read_bytes(), "query/source_index.json",
        lazy_fields=("source_records", "source_locations", "passage_metadata")))
    path = citation.get("page_path")
    location = index.get("source_locations", {}).get(path) or {}
    records = index.get("source_records", {}).get(path) or []
    if location.get("kind") != "source" or not any(
        record.get("document_id") == citation["document_id"] and
        record.get("chunk_id") == citation.get("chunk_id") for record in records
    ):
        return None
    metadata = location.get("metadata") or {}
    text = metadata.get("snapshot_text") or ""
    excerpt = citation.get("excerpt") or ""
    if not text or not excerpt:
        return None
    expected = location.get("passage_text_sha256")
    if expected and hashlib.sha256(text.encode()).hexdigest() != expected:
        raise ConflictError("引用的来源快照校验失败")
    start = text.find(excerpt)
    if start < 0 or text.find(excerpt, start+1) >= 0:
        return None
    spans = [[start, start+len(excerpt)]]
    segments, truncated = _segments(text, spans)
    return {"kind": "source_span", "exact_location": True, "segments": segments,
            "spans": spans, "truncated": truncated,
            "page_number": metadata.get("page_number"), "source_position": metadata.get("position")}


def _read(base, citation):
    ref = citation.get("evidence_ref") or {}
    if ref.get("kind") in ("table_cell", "table_range", "table_note"):
        return _table(base, citation, ref)
    if ref.get("kind") == "source_span":
        text = _path(base, ref["path"]).read_text("utf-8")
        spans = ref.get("spans") or []
        segments, truncated = _segments(text, spans)
        body = "\n".join(text[a:b] for a, b in spans)
        if ref.get("body_sha256") and hashlib.sha256(body.encode()).hexdigest() != ref["body_sha256"]:
            raise ConflictError("引用的原文校验失败")
        return {"kind": "source_span", "exact_location": True, "segments": segments,
                "spans": spans, "truncated": truncated}
    snapshot = _legacy_snapshot(base, citation)
    if snapshot:
        return snapshot
    # Older citations lack structured positions. Accept an exact, unique source match only.
    excerpt = citation.get("excerpt") or ""
    source_id = citation.get("source_id") or citation["document_id"]
    try:
        text = _path(base, f"sources/articles/{source_id}.md").read_text("utf-8")
        start = text.find(excerpt) if excerpt else -1
        if start >= 0 and text.find(excerpt, start+1) < 0:
            spans = [[start, start+len(excerpt)]]
            segments, truncated = _segments(text, spans)
            return {"kind": "source_span", "exact_location": True, "segments": segments,
                    "spans": spans, "truncated": truncated}
    except NotFoundError:
        pass
    return {"kind": "source_excerpt", "exact_location": False,
            "segments": [{"text": excerpt[:_MAX_TEXT], "highlight": True}],
            "truncated": len(excerpt) > _MAX_TEXT}


async def resolve_evidence(app, tenant_id, kb_id, query_id, handle):
    async with app.layout.version_lock(tenant_id, kb_id):
        return await _resolve_evidence(app, tenant_id, kb_id, query_id, handle)


async def _resolve_evidence(app, tenant_id, kb_id, query_id, handle):
    async with app.session_factory() as session:
        row = await QueryTaskRepo().get(session, tenant_id, query_id)
        if row is None or row.kb_id != kb_id:
            raise NotFoundError("查询不存在")
        citation = next((c for c in (row.result or {}).get("citations", []) if c.get("handle") == handle), None)
        if citation is None:
            raise NotFoundError("查询中不存在此证据编号")
        doc = await DocumentRepo().get(session, tenant_id, kb_id, citation["document_id"])
        if doc is None or doc.deleted_at is not None:
            raise NotFoundError("引用的来源已删除或不可访问")
        if not row.bundle_sha:
            raise NotFoundError("查询没有保留编译版本；请重新检索")
        base = app.layout.versions_dir(tenant_id, kb_id, row.bundle_sha)
        if not base.is_dir():
            raise NotFoundError("引用的历史版本已清理；请重新检索")
        bundle_sha = row.bundle_sha
    view = await asyncio.to_thread(_read, base, citation)
    return dict(view, document_id=citation["document_id"], handle=handle,
                query_id=query_id, bundle_sha=bundle_sha)
