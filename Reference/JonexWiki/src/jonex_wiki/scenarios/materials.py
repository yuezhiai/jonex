"""Version-pinned source units; no educational heading heuristics."""

from __future__ import annotations
import hashlib
import json
import mimetypes
from pathlib import Path
from jonex_wiki.kernel.errors import ValidationError


def collect(view, layout, document_ids=(), *, max_unit_chars=10000):
    catalog = json.loads(view.read_bytes("query/resources.json"))
    docs = [
        o
        for o in catalog["objects"]
        if o["kind"] == "document"
        and (not document_ids or o["document_id"] in document_ids)
    ]
    if document_ids and set(document_ids) - {o["document_id"] for o in docs}:
        raise ValidationError("SCENARIO_DOCUMENT_NOT_IN_REVISION")
    units = []
    for doc in docs:
        did = doc["document_id"]
        path = "sources/articles/" + did + ".md"
        raw = view.read_bytes(path)
        body = raw.decode()
        sha = hashlib.sha256(raw).hexdigest()
        if body.strip():
            start = 0
            while start < len(body):
                end = min(len(body), start + max_unit_chars)
                if end < len(body):
                    boundary = body.rfind("\n\n", start + max_unit_chars // 2, end)
                    if boundary > start:
                        end = boundary + 2
                unit = {
                    "document_id": did,
                    "source_name": doc["label"],
                    "path": path,
                    "start_char": start,
                    "end_char": end,
                    "source_sha256": doc["source_sha256"],
                    "body_sha256": sha,
                    "text": body[start:end],
                    "modality": "text",
                    "evidence_kind": "source_article_projection",
                    "source_quality": doc.get("quality", {}),
                    "locator": {
                        "kind": "article_span",
                        "start_char": start,
                        "end_char": end,
                    },
                }
                unit["unit_id"] = (
                    "u_"
                    + hashlib.sha256(
                        json.dumps([view.manifest_hash, did, start, end]).encode()
                    ).hexdigest()[:24]
                )
                units.append(unit)
                start = end
        original = doc.get("original") or {}
        rel = Path(original.get("path") or "missing")
        mime = mimetypes.guess_type(rel.name)[0] or ""
        if mime == "application/pdf":
            base = layout.raw_dir(
                view.tenant_id, view.context_id, did, doc["content_generation"]
            ).resolve()
            target = (base / rel).resolve()
            report = base / "pdf-text-layer.json"
            if target.is_relative_to(base) and target.is_file() and report.is_file():
                pages = json.loads(report.read_text()).get("pages", [])
                for page in pages[:128]:
                    number = page["page_number"]
                    units.append(
                        {
                            "unit_id": "u_"
                            + hashlib.sha256(
                                json.dumps(
                                    [
                                        view.manifest_hash,
                                        did,
                                        "original-pdf-page-v1",
                                        number,
                                    ]
                                ).encode()
                            ).hexdigest()[:24],
                            "document_id": did,
                            "source_name": doc["label"],
                            "modality": "image",
                            "text": "",
                            "source_sha256": doc["source_sha256"],
                            "mime_type": "image/png",
                            "pdf_source_path": str(target),
                            "pdf_cache_root": str(
                                layout.kb_dir(view.tenant_id, view.context_id)
                                / "visual/pdf-pages"
                            ),
                            "locator": {"kind": "pdf_page", "page": number},
                            "evidence_kind": "rendered_original_pdf_page",
                        }
                    )
        if mime in {"image/jpeg", "image/png", "image/webp"}:
            base = layout.raw_dir(
                view.tenant_id, view.context_id, did, doc["content_generation"]
            ).resolve()
            target = (base / rel).resolve()
            if not target.is_relative_to(base) or not target.is_file():
                raise ValidationError("SCENARIO_ORIGINAL_IMAGE_UNAVAILABLE")
            if target.stat().st_size > 8 * 1024 * 1024:
                raise ValidationError("SCENARIO_IMAGE_BYTES_LIMIT")
            with target.open("rb") as stream:
                original_sha = hashlib.file_digest(stream, "sha256").hexdigest()
            if original_sha != doc["source_sha256"]:
                raise ValidationError("SCENARIO_ORIGINAL_HASH_MISMATCH")
            image_id = hashlib.sha256(
                json.dumps(
                    [
                        view.manifest_hash,
                        did,
                        doc["content_generation"],
                        doc["source_sha256"],
                    ]
                ).encode()
            ).hexdigest()[:24]
            units.append(
                {
                    "unit_id": "u_" + image_id,
                    "document_id": did,
                    "source_name": doc["label"],
                    "modality": "image",
                    "text": "",
                    "source_sha256": doc["source_sha256"],
                    "asset_sha256": doc["source_sha256"],
                    "image_path": str(target),
                    "mime_type": mime,
                    "locator": {"kind": "original_image"},
                }
            )
        for obj in catalog["objects"]:
            if obj["document_id"] != did or obj["kind"] != "video_frame":
                continue
            units.append(
                {
                    "unit_id": "u_" + obj["object_id"],
                    "document_id": did,
                    "source_name": doc["label"],
                    "modality": "image",
                    "text": "",
                    "path": obj["asset_path"],
                    "source_sha256": doc["source_sha256"],
                    "asset_sha256": obj["asset_sha256"],
                    "mime_type": "image/jpeg",
                    "locator": obj["locator"],
                    "evidence_kind": "sampled_video_frame",
                }
            )
    media = (
        json.loads(view.read_bytes("query/media_catalog.json"))
        if "query/media_catalog.json" in view.list_entries()
        else {}
    )
    for asset in media.get("assets", []):
        if asset["document_id"] not in {d["document_id"] for d in docs}:
            continue
        units.append(
            {
                "unit_id": "u_" + asset["asset_id"],
                "document_id": asset["document_id"],
                "source_name": next(
                    d["label"] for d in docs if d["document_id"] == asset["document_id"]
                ),
                "modality": "image",
                "text": "",
                "path": asset["path"],
                "asset_sha256": asset["sha256"],
                "source_sha256": next(
                    d["source_sha256"]
                    for d in docs
                    if d["document_id"] == asset["document_id"]
                ),
                "mime_type": asset["mime_type"],
                "locator": {
                    "kind": "source_image",
                    "page": asset.get("pdf_page"),
                    "bbox": asset.get("bbox"),
                },
            }
        )
    return units


def public_unit(unit):
    return {
        k: v
        for k, v in unit.items()
        if k not in {"image_path", "pdf_source_path", "pdf_cache_root"}
    }


def unit_summary(unit):
    return {
        **{k: v for k, v in public_unit(unit).items() if k != "text"},
        "text_chars": len(unit.get("text", "")),
        "preview": unit.get("text", "")[:160],
    }


def image_bytes(view, unit):
    if unit.get("pdf_source_path"):
        from jonex_wiki.domain.reading.pdf_visual import render_page

        data, metadata = render_page(
            unit["pdf_source_path"],
            unit["source_sha256"],
            unit["locator"]["page"],
            unit["pdf_cache_root"],
        )
        unit["asset_sha256"] = metadata["asset_sha256"]
        unit["rendering"] = metadata
        return data
    data = (
        Path(unit["image_path"]).read_bytes()
        if unit.get("image_path")
        else view.read_bytes(unit["path"])
    )
    if hashlib.sha256(data).hexdigest() != unit["asset_sha256"]:
        raise ValidationError("SCENARIO_IMAGE_HASH_MISMATCH")
    return data
