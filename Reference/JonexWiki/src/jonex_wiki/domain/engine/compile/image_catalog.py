"""Keep source pictures, source order and existing text without visual inference."""

import hashlib
import json
import os
import shutil
from pathlib import Path

from jonex_wiki.domain.engine.json_artifacts import load_json_artifact
from jonex_wiki.domain.image_identity import image_anchor, plain
from jonex_wiki.domain.image_identity import identity as _id

PATH = "query/media_catalog.json"
RULE = "source-media-assets-v4"
MAX_BYTES = 32 * 1024 * 1024


def raw_items(metadata):
    items = {}

    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            item = value.get("raw_item")
            position = value.get("source_position", value.get("position"))
            if isinstance(item, dict) and isinstance(position, int):
                items.setdefault(position, item)
            for key, item in value.items():
                if key != "raw_item" and isinstance(item, (list, dict)):
                    visit(item)

    visit(metadata)
    return [{**item, "position": p} for p, item in sorted(items.items())]


def _image_info(path):
    from PIL import Image

    size = path.stat().st_size
    if size > MAX_BYTES:
        raise ValueError("image_byte_budget")
    with path.open("rb") as stream:
        sha = hashlib.file_digest(stream, "sha256").hexdigest()
    try:
        with Image.open(path) as img:
            if img.format not in {"PNG", "JPEG", "WEBP"}:
                raise ValueError("image_format_unsupported")
            width, height = img.size
            if width <= 0 or height <= 0 or width * height > 32_000_000:
                raise ValueError("image_pixel_budget")
            ext, mime = {
                "PNG": (".png", "image/png"),
                "JPEG": (".jpg", "image/jpeg"),
                "WEBP": (".webp", "image/webp"),
            }[img.format]
    except Image.DecompressionBombError as exc:
        raise ValueError("image_pixel_budget") from exc
    return sha, size, width, height, ext, mime


def build_catalog(pub: Path, resolve_asset, reuse_assets=None):
    catalog = {
        "schema": RULE,
        "entities": [],
        "relations": [],
        "assets": [],
        "bindings": [],
        "unresolved": [],
    }
    file_cache = {}
    for file in sorted((pub / "sources/digests").glob("*.json")):
        digest = load_json_artifact(
            lambda key: (pub / key).read_bytes(), file.relative_to(pub).as_posix()
        )
        metadata = digest.get("source_metadata") or {}
        receipt = metadata.get("consumer_receipt") or {}
        source_name = str(
            receipt.get("source_name")
            or metadata.get("source_name")
            or digest.get("reading_materials", {})
            .get("source_identity", {})
            .get("source_name")
            or digest["document_id"]
        )
        doc, generation = str(digest["document_id"]), int(digest["content_generation"])
        source_path = f"sources/articles/{doc}.md"
        body = (
            (pub / source_path).read_text("utf-8")
            if (pub / source_path).is_file()
            else ""
        )
        body_sha = hashlib.sha256(body.encode()).hexdigest()
        items = raw_items(metadata)
        neighbors = {
            item["position"]: items[max(0, n - 2) : n + 3]
            for n, item in enumerate(items)
        }
        # Admission follows the parsed content type, never the source suffix
        # or the mere presence of an img_path on a table preview.
        for item in items:
            if item.get("type") not in {"image", "chart"}:
                continue  # video stays in its existing separate path
            position = item["position"]
            relative = str(
                item.get("img_path")
                or item.get("image_path")
                or item.get("chart_path")
                or ""
            )
            original = resolve_asset(doc, generation, relative)
            try:
                if original is None or not original.is_file():
                    raise ValueError("asset_missing")
                stat = original.stat()
                stamp = (str(original.resolve()), stat.st_size, stat.st_mtime_ns)
                if stamp not in file_cache:
                    file_cache[stamp] = _image_info(original)
                sha, size, width, height, ext, mime = file_cache[stamp]
                if item.get("original_image") and any(
                    a["document_id"] == doc and a["sha256"] == sha
                    for a in catalog["assets"]
                ):
                    continue
                target = f"media/assets/{sha}{ext}"
                destination = pub / target
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not destination.exists():
                    previous = (
                        Path(reuse_assets) / destination.name if reuse_assets else None
                    )
                    if previous and previous.is_file():
                        with previous.open("rb") as stream:
                            verified = (
                                hashlib.file_digest(stream, "sha256").hexdigest() == sha
                            )
                        if not verified:
                            raise ValueError("published_asset_hash_mismatch")
                        try:
                            os.link(previous, destination)
                        except OSError:
                            shutil.copyfile(original, destination)
                    else:
                        shutil.copyfile(original, destination)
                caption = plain(
                    item.get("source_caption")
                    or item.get("image_caption")
                    or item.get("chart_caption")
                    or item.get("caption")
                )
                if item.get("evidence_kind") == "native_image_unread":
                    caption = plain(item.get("source_caption"))
                marker = image_anchor(source_name, position, relative)
                context_parts = [
                    plain(near.get("text"), 300)
                    for near in neighbors.get(position, [])
                    if near.get("type") in {"text", "paragraph"}
                ]
                page_index = item.get("page_idx")
                if not isinstance(page_index, int) or isinstance(page_index, bool):
                    page_index = None
                locator = dict(item.get("native_locator") or {})
                locator.update(
                    kind="original_image"
                    if item.get("original_image")
                    else locator.get("kind", "source_image"),
                    source_position=position,
                )
                if source_name.lower().endswith(".pdf") and page_index is not None:
                    locator.update(
                        kind="pdf_image",
                        page_number=page_index + 1,
                        bbox=item.get("bbox"),
                    )
                asset_id = _id("image", doc, generation, sha, position)
                at = body.find(marker)
                catalog["assets"].append(
                    {
                        "asset_id": asset_id,
                        "occurrence_id": asset_id,
                        "document_id": doc,
                        "source_name": source_name,
                        "content_generation": generation,
                        "sha256": sha,
                        "path": target,
                        "bytes": size,
                        "width": width,
                        "height": height,
                        "mime_type": mime,
                        "pdf_page": page_index + 1
                        if source_name.lower().endswith(".pdf")
                        and page_index is not None
                        else None,
                        "parser_page_index": page_index,
                        "bbox": item.get("bbox"),
                        "image_idx": position,
                        "source_relative_path": relative,
                        "source_locator": locator,
                        "source_path": source_path,
                        "source_marker": marker,
                        "source_span": [at, at + len(marker)]
                        if at >= 0 and body.count(marker) == 1
                        else None,
                        "source_body_sha256": body_sha,
                        "caption": caption,
                        "ocr_text": plain(
                            item.get("ocr_text")
                            or item.get("text_content")
                            or item.get("text"),
                            4000,
                        ),
                        "description": plain(item.get("description"), 4000),
                        "context": "\n".join(context_parts)[:600],
                        "context_status": "nearby_candidate",
                        "label": caption or plain(item.get("alt_text")) or source_name,
                        "content_indexed": bool(
                            caption
                            or item.get("ocr_text")
                            or item.get("description")
                            or context_parts
                        ),
                        "usage": "illustration",
                        "association_method": "source_item",
                    }
                )
            except (OSError, ValueError, SyntaxError, TypeError) as exc:
                catalog["unresolved"].append(
                    {"document_id": doc, "position": position, "reason": str(exc)[:200]}
                )
    catalog["assets"].sort(
        key=lambda a: (a["document_id"], a["image_idx"], a["asset_id"])
    )
    catalog["unresolved"].sort(key=lambda a: (a["document_id"], a["position"]))
    (pub / PATH).parent.mkdir(parents=True, exist_ok=True)
    (pub / PATH).write_text(
        json.dumps(catalog, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        "utf-8",
    )
    if catalog["assets"]:
        from .image_index import build_image_index

        build_image_index(pub, catalog)
    else:
        # Staging may already contain a derived index from an earlier build.
        (pub / "indexes/images.sqlite").unlink(missing_ok=True)
    return catalog
