"""Small typed resource projection; source articles remain the only full-text store.

Educational extraction produces candidates. It never infers prerequisite facts,
learner mastery, or equivalence across editions from co-occurrence.
"""
from __future__ import annotations
import hashlib
import json
import re
import sqlite3
from pathlib import Path

PATH = "query/resources.json"
SCHEMA = "wiki.resources.v1"


def identity(prefix, *parts):
    return prefix + "_" + hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:32]


def build_resources(pub: Path, resolve_asset=None) -> dict:
    result = {"schema": SCHEMA, "objects": [], "relations": [], "evidence": [], "gaps": [], "auxiliary_files": []}
    for file in sorted((pub / "sources/digests").glob("*.json")):
        digest = json.loads(file.read_text("utf-8"))
        doc = str(digest["document_id"])
        generation = int(digest["content_generation"])
        metadata = digest.get("source_metadata") or {}
        receipt = metadata.get("consumer_receipt") or {}
        original_sha = receipt.get("original_sha256") or digest.get("content_sha256")
        source_path = f"sources/articles/{doc}.md"
        article = pub / source_path
        if not article.is_file():
            continue
        body = article.read_text("utf-8")
        body_sha = hashlib.sha256(body.encode()).hexdigest()
        version = {"document_id": doc, "source_sha256": original_sha,
                   "content_generation": generation, "platform_content_generation": receipt.get("content_generation"),
                   "edition": metadata.get("edition"), "curriculum": metadata.get("curriculum")}
        source_id = identity("obj", doc, original_sha, "document")

        def add(kind, label, start=0, end=None, locator=None, parent=None, **extra):
            end = min(len(body), end if end is not None else len(body))
            start = max(0, start)
            location = locator or {"kind": "article_span", "start_char": start, "end_char": end}
            oid = identity("obj", doc, original_sha, kind, location)
            evidence_id = identity("ev", oid, source_path, start, end)
            obj = {"object_id": oid, "kind": kind, "label": label, **version,
                "locator": location, "evidence_ids": [evidence_id], "parent_id": parent,
                "review_status": "source" if kind in {"document", "table", "media_segment", "image", "video_frame"} else "candidate", **extra}
            result["objects"].append(obj)
            result["evidence"].append({"evidence_id": evidence_id, "object_id": oid,
                "document_id": doc, "path": source_path, "start_char": start, "end_char": end,
                "body_sha256": body_sha, "locator": location, "evidence_kind": extra.get("evidence_kind", "source_text")})
            if parent:
                result["relations"].append({"relation_id": identity("rel", parent, "contains", oid),
                    "source": parent, "type": "contains", "target": oid, "family": "structure",
                    "evidence_ids": [evidence_id], "review_status": "source"})
            return obj

        source = add("document", metadata.get("source_name") or doc,
            original={"path": receipt.get("original_path"), "sha256": original_sha},
            quality=receipt.get("quality") or {"gaps": ["legacy_input_receipt_unavailable"]})
        source_id = source["object_id"]
        if resolve_asset:
            native = resolve_asset(doc, generation, "pdf-text-layer.json")
            if native and native.is_file():
                layer = json.loads(native.read_text("utf-8"))
                if layer["original_sha256"] != original_sha:
                    raise ValueError("PDF_TEXT_LAYER_ORIGINAL_IDENTITY_MISMATCH")
                relative = f"sources/{doc}/pdf-text-layer.json"
                target = pub / relative;target.parent.mkdir(parents=True, exist_ok=True);target.write_bytes(native.read_bytes())
                result["auxiliary_files"].append(relative);source["native_text_path"] = relative
                for index, page in enumerate(layer["pages"]):
                    obj = add("page", f'PDF 原生文本 · 第 {page["page_number"]} 页', end=0,
                        locator={"kind": "pdf_native_text", "page_number": page["page_number"]}, parent=source_id,
                        evidence_kind="native_pdf_text")
                    evidence = result["evidence"][-1]
                    evidence.update(path=relative, json_page=index, start_char=0, end_char=len(page["text"]),
                                    body_sha256=page["text_sha256"])
                result["gaps"].extend({"document_id": doc, "reason": gap} for gap in layer["gaps"])
        for table_id in digest.get("table_ids", []):
            label = table_id
            database = pub / "sources" / doc / "tables.sqlite"
            if database.is_file():
                with sqlite3.connect(database.resolve().as_uri() + "?mode=ro&immutable=1", uri=True) as connection:
                    row = connection.execute("SELECT title FROM logical_tables WHERE table_id=?", (table_id,)).fetchone()
                    if row:label = row[0] or table_id
            add("table", label, locator={"kind": "logical_table", "table_id": table_id}, parent=source_id,
                table_id=table_id, native_locator=metadata.get("native_locator") if metadata.get("table_id") == table_id else None)
        chunks = metadata.get("chunks") or [{"chunk_id": "source", "metadata": metadata}]
        seen_media = set()
        for number, chunk in enumerate(chunks):
            meta = chunk.get("metadata") or {}
            raw = meta.get("raw_item") or {}
            kind = meta.get("block_type") or raw.get("type")
            if kind not in {"audio", "video", "image", "chart"}:
                continue
            start, end = meta.get("time_start"), meta.get("time_end")
            marker = (kind, start, end, json.dumps(raw.get("native_locator"), sort_keys=True), raw.get("img_path"))
            if marker in seen_media:
                continue
            seen_media.add(marker)
            media_kind = "media_segment" if kind in {"audio", "video"} else "image"
            location = {"kind": "time_range", "start_seconds": start, "end_seconds": end} if media_kind == "media_segment" else {"kind": "image_region", "page": meta.get("page_number"), "bbox": meta.get("bbox"), "ordinal": number}
            snippet = raw.get("text") or raw.get("description") or raw.get("ocr_text") or ""
            at = body.find(snippet) if isinstance(snippet, str) and snippet else -1
            obj = add(media_kind, f"{kind} {start or 0}–{end if end is not None else '?'}" if media_kind == "media_segment" else f"{kind} {number + 1}",
                start=max(at, 0), end=at + len(snippet) if at >= 0 else 0,
                locator=location, parent=source_id, media_kind=kind,
                evidence_kind=meta.get("evidence_kind", "generated_description"),
                alignment="literal_text" if at >= 0 else "metadata_only", upstream_spans=meta.get("spans") or [],
                asset_path=raw.get("img_path"))
            obj["chunk_id"] = chunk.get("chunk_id")
        # Explicit headings define local question identity. Number 9.25 alone is
        # never a global question/entity ID and does not join different editions.
        pattern = re.compile(r"(?im)^\s*(?:#{1,6}\s*)?(例题|练习|习题|题目|思考题|Example|Try It|Be Prepared|Question|Exercise)[ \t\n]*(\d+(?:[.\-]\d+)*)[^\n]*")
        matches = list(pattern.finditer(body))
        for n, match in enumerate(matches):
            end = matches[n + 1].start() if n + 1 < len(matches) else len(body)
            # A section heading also terminates the object; its scope must not
            # absorb the remainder of an unrelated chapter or end-of-book answers.
            boundary = re.search(r"(?m)^#{1,3}\s+\S", body[match.end():end])
            if boundary:
                end = match.end() + boundary.start()
            question = add("question", (match.group(1) + " " + match.group(2)).strip(),
                start=match.start(), end=end, parent=source_id, local_number=match.group(2),
                question_role="worked_example" if match.group(1).casefold() in {"example", "例题"} else "practice")
            text = body[match.end():end]
            solution = re.search(r"(?im)^\s*(?:#{1,6}\s*)?(?:Solution|Answer|解答|答案|解析)\b", text)
            if solution:
                answer = add("answer", question["label"] + " · answer", start=match.end() + solution.start(), end=end, parent=question["object_id"])
                result["relations"].append({"relation_id": identity("rel", answer["object_id"], "answers", question["object_id"]),
                    "source": answer["object_id"], "type": "answers", "target": question["object_id"],
                    "family": "education", "review_status": "candidate", "evidence_ids": answer["evidence_ids"]})
            for sub in list(re.finditer(r"(?m)^\s*(?:[ⓐⓑⓒⓓⓔⓕ]|[（(][a-fA-F1-9][)）])", text))[:50]:
                add("subquestion", question["label"] + " " + sub.group().strip(),
                    start=match.end() + sub.start(), end=min(end, match.end() + sub.end() + 500), parent=question["object_id"])
        for heading in re.finditer(r"(?m)^#{1,4}\s+([^\n]{3,200})", body):
            label = heading.group(1).strip()
            if re.search(r"性质|定义|定理|概念|Understand|Properties of", label, re.I):
                add("concept", label, start=heading.start(), end=heading.end(), parent=source_id)
            elif not re.search(r"^(Example|Try It|Be Prepared|Solution|Answer|Figure|例题|解答|答案)\b", label, re.I):
                next_heading = re.search(r"(?m)^#{1,4}\s+", body[heading.end():])
                end = heading.end() + next_heading.start() if next_heading else len(body)
                add("section", label, start=heading.start(), end=end, parent=source_id)
        from jonex_wiki.domain.engine.compile.media_catalog import raw_items
        labels = {o["label"] for o in result["objects"] if o["document_id"] == doc and o["kind"] == "concept"}
        for item in raw_items(metadata):
            label = str(item.get("text") or "").strip()
            if any(o["document_id"] == doc and o["label"] == label and o["kind"] in {"concept", "section"} for o in result["objects"]):
                continue
            if (item.get("text_level") and re.search(r"性质|定义|定理|概念|Understand|Properties of", label, re.I)
                    and label not in labels and 3 <= len(label) <= 200):
                at = body.find(label)
                if at >= 0 and body.find(label, at + 1) < 0:
                    add("concept", label, start=at, end=at + len(label), parent=source_id);labels.add(label)
            elif (item.get("text_level") and label and len(label) <= 200
                    and not re.search(r"^(Example|Try It|Be Prepared|Solution|Answer|Figure)\b", label, re.I)):
                at = body.find(label)
                if at >= 0 and body.find(label, at + 1) < 0:
                    add("section", label, start=at, end=min(len(body), at + len(label)), parent=source_id)
        for gap in (receipt.get("quality") or {}).get("gaps", []):
            result["gaps"].append({"document_id": doc, "reason": gap})
        inventory = metadata.get("native_inventory") or {}
        for gap in inventory.get("gaps", []):
            result["gaps"].append({"document_id": doc, "reason": "native_" + str(gap) + "_needs_review"})
        if resolve_asset:
            index = resolve_asset(doc, generation, "media-timeline.json")
            if index and index.is_file():
                timeline = json.loads(index.read_text("utf-8"))
                for frame in timeline.get("frames", []):
                    target = resolve_asset(doc, generation, frame["path"])
                    if not target or not target.is_file():
                        continue
                    data = target.read_bytes();digest_sha = hashlib.sha256(data).hexdigest()
                    asset_path = "media/assets/" + digest_sha + ".jpg"
                    destination = pub / asset_path;destination.parent.mkdir(parents=True, exist_ok=True)
                    if not destination.exists():destination.write_bytes(data)
                    add("video_frame", f'Frame at {frame["start_seconds"]:.3f}s', end=0,
                        locator={"kind": "time_range", "start_seconds": frame["start_seconds"], "end_seconds": frame["end_seconds"]},
                        parent=source_id, asset_path=asset_path, asset_sha256=digest_sha,
                        evidence_kind="derived_video_frame", repeated_occurrences=frame.get("occurrences", []))
                result["gaps"].extend({"document_id": doc, "reason": reason} for reason in timeline.get("gaps", []))
    # Literal, same-edition concept mentions are discovery relations. They do not
    # establish prerequisite, mastery, causality, or equivalence across sources.
    concepts = [o for o in result["objects"] if o["kind"] == "concept"]
    evidence = {e["evidence_id"]: e for e in result["evidence"]}
    for question in [o for o in result["objects"] if o["kind"] == "question"]:
        ev = evidence[question["evidence_ids"][0]]
        text = (pub / ev["path"]).read_text("utf-8")[ev["start_char"]:ev["end_char"]]
        for concept in concepts:
            if question["document_id"] == concept["document_id"] and concept["label"] in text:
                result["relations"].append({"relation_id": identity("rel", question["object_id"], "mentions", concept["object_id"]),
                    "source": question["object_id"], "type": "mentions", "target": concept["object_id"],
                    "family": "navigation", "evidence_ids": question["evidence_ids"], "review_status": "candidate"})
    target = pub / PATH;target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")), "utf-8")
    return result
