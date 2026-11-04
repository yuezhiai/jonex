"""Page text corroboration for a selected PDF observation, with independent citations."""
import hashlib
import json
import re


def native_page_views(state, source_id, info, revision_id, refs):
    from jonex_wiki.domain.engine.query.read_views import ReadingView
    view = state.get("bundle_view")
    path = f"sources/{source_id}/pdf-text-layer.json"
    if view is None or path not in getattr(view, "list_entries", lambda: ())():return [], []
    layer = json.loads(view.read_bytes(path))
    pages = [p for p in layer["pages"] if p["text"].strip()]
    if not pages:return [], []
    question = str(state.get("original_question") or "")
    if "query/resources.json" in view.list_entries():
        resources = json.loads(view.read_bytes("query/resources.json"))
        source = next((o for o in resources["objects"] if o["kind"] == "document" and o["document_id"] == info["document_id"]), None)
        if source:question = question.replace(source["label"], "")
    identifiers = re.findall(r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9_.-]{1,31}", question)
    matching = [p for p in pages if any(re.search(r"(?<![A-Za-z0-9])" + re.escape(value) + r"(?![A-Za-z0-9])", p["text"]) for value in identifiers)]
    pages = (matching or pages)[:4]
    views, groups = [], []
    for page in pages:
        number, text = page["page_number"], page["text"]
        digest = hashlib.sha256(text.encode()).hexdigest()
        if digest != page["text_sha256"]:raise ValueError("PDF_TEXT_LAYER_PAGE_HASH_MISMATCH")
        claim_id = "pdf_native_" + hashlib.sha256((source_id + layer["original_sha256"] + str(number) + digest).encode()).hexdigest()[:24]
        ref = {"kind": "pdf_native_text", "source_id": source_id, "document_id": info["document_id"],
            "path": path, "page_path": info.get("page_path", ""), "page_number": number,
            "text_sha256": digest, "original_sha256": layer["original_sha256"], "spans": [[0, len(text)]]}
        refs[claim_id] = ref
        handles = [{"claim_id": claim_id, "passage_id": claim_id, "source_id": source_id,
            "document_id": info["document_id"], "chunk_id": f"pdf-native-page-{number}", "excerpt": text,
            "page_path": info.get("page_path", "")}]
        views.append(ReadingView(object_key=(revision_id, source_id, path + f"#page={number}", digest),
            source_id=source_id, article_path=path, spans=[(0, len(text))],
            blocks=[{"start": 0, "end": len(text), "body": f"原 PDF 第 {number} 页原生文本（独立于 OCR；保留版面空白，不代表已识别单元格）：\n" + text}],
            citation_refs=[{"claim_id": claim_id, "source_id": source_id}], coverage="native_page_text_complete",
            structure_origin="pdf_native_text", selected_level="native_pdf_page"))
        groups.append(handles)
    return views, groups
