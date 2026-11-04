"""Source-first artwork catalog and immutable image assets.

Wiki pages are reading views, not entity identities. Model names are discovery
hints only: admission requires a literal catalog heading and material/size
metadata in the source layout. Uncertain image associations remain unresolved.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path

from jonex_wiki.domain.engine.json_artifacts import load_json_artifact

PATH = "query/media_catalog.json"
RULE = "source-artwork-catalog-v1"
_MEDIUM = re.compile(r"纸本|绢本|布面|木板|水墨|设色|油彩|油画|厘米|\bcm\b", re.I)
_AUTHOR_KIND = {"作者-有-作品", "作者", "创作", "有", "绘", "绘制", "作", "画"}


def _id(kind, *values):
    return (
        kind
        + "_"
        + hashlib.sha256(
            json.dumps(values, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()[:32]
    )


def _compact(text):
    return "".join(
        ch for ch in str(text) if not ch.isspace() and ch not in "《》〈〉「」『』"
    )


def raw_items(metadata):
    """Recover native items once, including images without recognized text."""
    items = {}

    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            item = value.get("raw_item")
            if isinstance(item, dict) and isinstance(value.get("position"), int):
                items.setdefault(value["position"], item)
            for key, item in value.items():
                if key != "raw_item" and isinstance(item, (list, dict)):
                    visit(item)

    visit(metadata)
    return [{"position": p, **item} for p, item in sorted(items.items())]


def _locate(body, text):
    """Locate one literal source span, allowing parser whitespace/quote styles."""
    positions = [
        i
        for i, ch in enumerate(body)
        if not ch.isspace() and ch not in "《》〈〉「」『』"
    ]
    compact = "".join(body[i] for i in positions)
    needle = _compact(text)
    start = compact.find(needle) if needle else -1
    if start < 0 or compact.find(needle, start + 1) >= 0:
        return None
    return [positions[start], positions[start + len(needle) - 1] + 1]


def _evidence(doc, path, body, text, page, bbox, kind):
    span = _locate(body, text)
    if span is None:
        return None
    return {
        "document_id": doc,
        "path": path,
        "start_char": span[0],
        "end_char": span[1],
        "read_path": "sources/source-"
        + hashlib.sha256(doc.encode()).hexdigest()[:16]
        + ".md",
        "excerpt": body[span[0] : span[1]],
        "pdf_page": page,
        "bbox": bbox,
        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "kind": kind,
    }


def _author_hints(pub):
    file = "source_contributions.json"
    if not (pub / file).is_file():
        return set()
    data = load_json_artifact(lambda key: (pub / key).read_bytes(), file)
    return {
        str(p.get("subject_mention") or "").strip()
        for record in data.get("records", [])
        if record.get("support_status") == "SUPPORTED"
        for p in record.get("link_proposals", [])
        if p.get("relation") in _AUTHOR_KIND
        and re.fullmatch(
            r"[\u3400-\u9fff]{2,6}", str(p.get("subject_mention") or "").strip()
        )
    }


def _header(item, next_item, names):
    if item.get("type") != "text":
        return None
    text = str(item.get("text") or "").strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    first = lines[0] if lines else ""
    candidates = sorted(
        (name for name in names if _compact(first).startswith(name)),
        key=lambda name: -len(name),
    )
    if not candidates:
        return None
    author = candidates[0]
    if len(candidates) > 1 and len(candidates[1]) == len(author):
        return None
    if _compact(first) == author and len(lines) >= 3:
        title = lines[1].strip("《》〈〉 ")
        metadata = "\n".join(lines[2:])
        source = text
    else:
        title = _compact(first)[len(author) :].strip("《》〈〉 ")
        if len(lines) > 1:
            metadata, source = "\n".join(lines[1:]), text
        elif (
            next_item
            and next_item.get("type") == "text"
            and next_item.get("page_idx") == item.get("page_idx")
        ):
            a, b = item.get("bbox", []), next_item.get("bbox", [])
            if (
                len(a) != 4
                or len(b) != 4
                or abs(a[0] - b[0]) > 30
                or not -5 <= b[1] - a[3] <= 40
            ):
                return None
            metadata = str(next_item.get("text") or "")
            source = first + "\n" + metadata
        else:
            return None
    if (
        not 1 <= len(title) <= 40
        or not _MEDIUM.search(metadata)
        or re.search(r"[。；：:]|主编|编著|出版社", title)
        or not re.search(r"\d|藏|年代", metadata)
    ):
        return None
    return author, title, metadata, source


def build_catalog(pub: Path, resolve_asset) -> dict:
    """Build from preserved source metadata, copying only identified assets.

    resolve_asset(doc, generation, relative_path) returns a local source file;
    it must enforce containment in that document's retained parse generation.
    """
    names = _author_hints(pub)
    catalog = {
        "schema": RULE,
        "entities": [],
        "relations": [],
        "assets": [],
        "bindings": [],
        "unresolved": [],
    }
    entities = {}
    for file in sorted((pub / "sources/digests").glob("*.json")):
        digest = load_json_artifact(
            lambda key: (pub / key).read_bytes(), file.relative_to(pub).as_posix()
        )
        doc, generation = str(digest["document_id"]), int(digest["content_generation"])
        source_path = f"sources/articles/{doc}.md"
        source = pub / source_path
        if not source.is_file():
            continue
        body = source.read_text("utf-8")
        items = raw_items(digest.get("source_metadata") or {})
        by_page = defaultdict(list)
        for item in items:
            if isinstance(item.get("page_idx"), int):
                by_page[item["page_idx"]].append(item)
        for page_idx, page in sorted(by_page.items()):
            images = []
            for item in page:
                if item.get("type") != "image":
                    continue
                relative = str(item.get("img_path") or item.get("image_path") or "")
                original = resolve_asset(doc, generation, relative)
                if original is None or not original.is_file():
                    catalog["unresolved"].append(
                        {
                            "document_id": doc,
                            "position": item["position"],
                            "reason": "asset_missing",
                        }
                    )
                    continue
                suffix = original.suffix.lower()
                if (
                    suffix not in (".jpg", ".jpeg", ".png", ".webp")
                    or original.stat().st_size > 8 * 1024 * 1024
                ):
                    continue
                data = original.read_bytes()
                if not (
                    (suffix in (".jpg", ".jpeg") and data.startswith(b"\xff\xd8\xff"))
                    or (suffix == ".png" and data.startswith(b"\x89PNG\r\n\x1a\n"))
                    or (
                        suffix == ".webp"
                        and data[:4] == b"RIFF"
                        and data[8:12] == b"WEBP"
                    )
                ):
                    catalog["unresolved"].append(
                        {
                            "document_id": doc,
                            "position": item["position"],
                            "reason": "invalid_image",
                        }
                    )
                    continue
                sha = hashlib.sha256(data).hexdigest()
                target = f"media/assets/{sha}{suffix}"
                (pub / target).parent.mkdir(parents=True, exist_ok=True)
                if not (pub / target).exists():
                    (pub / target).write_bytes(data)
                elif hashlib.sha256((pub / target).read_bytes()).hexdigest() != sha:
                    raise ValueError("MEDIA_ASSET_CONTENT_MISMATCH")
                asset = {
                    "asset_id": _id("image", doc, generation, sha, item["position"]),
                    "document_id": doc,
                    "content_generation": generation,
                    "sha256": sha,
                    "path": target,
                    "pdf_page": page_idx + 1,
                    "bbox": item.get("bbox"),
                    "image_idx": item["position"],
                    "mime_type": "image/jpeg"
                    if suffix in (".jpg", ".jpeg")
                    else "image/" + suffix[1:],
                    "source_relative_path": relative,
                }
                images.append((item, asset))
                catalog["assets"].append(asset)
            headers = []
            for index, item in enumerate(page):
                header = _header(
                    item, page[index + 1] if index + 1 < len(page) else None, names
                )
                if header:
                    headers.append((item, header))
            for item, (author, title, metadata, literal) in headers:
                ev = _evidence(
                    doc,
                    source_path,
                    body,
                    literal,
                    page_idx + 1,
                    item.get("bbox"),
                    "catalog_heading",
                )
                if ev is None:
                    catalog["unresolved"].append(
                        {
                            "document_id": doc,
                            "pdf_page": page_idx + 1,
                            "author": author,
                            "title": title,
                            "reason": "source_span_not_unique",
                        }
                    )
                    continue
                person = _id("person", doc, author)
                work = _id("artwork", doc, author, title, ev["start_char"])
                entities.setdefault(
                    person,
                    {
                        "entity_id": person,
                        "entity_type": "person",
                        "label": author,
                        "document_id": doc,
                    },
                )
                entities[work] = {
                    "entity_id": work,
                    "entity_type": "artwork",
                    "label": title,
                    "author": author,
                    "document_id": doc,
                    "description": metadata,
                }
                relation = {
                    "relation_id": _id("relation", person, work, "创作"),
                    "source_entity_id": person,
                    "target_entity_id": work,
                    "predicate": "创作",
                    "evidence": [ev],
                }
                catalog["relations"].append(relation)
                representative = []
                for text_item in page:
                    text = str(text_item.get("text") or "")
                    if (
                        author in text
                        and re.search(
                            r"《"
                            + re.escape(title)
                            + r"》.{0,8}(?:是|为)(?:其|他|该作者的)?代表作",
                            text,
                        )
                        and not re.search(r"并非|不是|不属|是否|可能|假设", text)
                    ):
                        proof = _evidence(
                            doc,
                            source_path,
                            body,
                            text,
                            page_idx + 1,
                            text_item.get("bbox"),
                            "explicit_representative_statement",
                        )
                        if proof:
                            representative.append(proof)
                if representative:
                    catalog["relations"].append(
                        {
                            "relation_id": _id("relation", person, work, "代表作"),
                            "source_entity_id": person,
                            "target_entity_id": work,
                            "predicate": "代表作",
                            "evidence": [ev, *representative],
                        }
                    )
                chosen, method = [], ""
                # Native image captions give a stronger direct association.
                for image_item, asset in images:
                    caption = (
                        image_item.get("image_caption")
                        or image_item.get("img_caption")
                        or []
                    )
                    caption = (
                        caption
                        if isinstance(caption, str)
                        else " ".join(map(str, caption))
                    )
                    if _compact(author) in _compact(caption) and _compact(
                        title
                    ) in _compact(caption):
                        chosen.append(asset)
                        method = "native_image_caption"
                # Catalog spread layout: one unambiguous work heading and one
                # dominant image. Do not resolve a multi-work page by proximity.
                if not chosen and len(headers) == 1 and images:
                    ranked = sorted(
                        images,
                        key=lambda pair: _area(pair[1].get("bbox")),
                        reverse=True,
                    )
                    largest = _area(ranked[0][1].get("bbox"))
                    second = _area(ranked[1][1].get("bbox")) if len(ranked) > 1 else 0
                    if largest >= 20000 and largest > 2.5 * second:
                        chosen, method = [ranked[0][1]], "unique_catalog_dominant_image"
                if not chosen:
                    catalog["unresolved"].append(
                        {
                            "document_id": doc,
                            "pdf_page": page_idx + 1,
                            "author": author,
                            "title": title,
                            "reason": "image_association_ambiguous",
                        }
                    )
                for asset in chosen:
                    catalog["bindings"].append(
                        {
                            "binding_id": _id("binding", work, asset["asset_id"]),
                            "work_entity_id": work,
                            "asset_id": asset["asset_id"],
                            "author": author,
                            "title": title,
                            "method": method,
                            "evidence": [ev],
                            "representative_evidence": representative,
                        }
                    )
    catalog["entities"] = [entities[key] for key in sorted(entities)]
    for key in ("relations", "assets", "bindings", "unresolved"):
        catalog[key].sort(
            key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False)
        )
    (pub / PATH).parent.mkdir(parents=True, exist_ok=True)
    (pub / PATH).write_text(
        json.dumps(catalog, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        "utf-8",
    )
    return catalog


def _area(bbox):
    return (
        max(0, bbox[2] - bbox[0]) * max(0, bbox[3] - bbox[1])
        if isinstance(bbox, list) and len(bbox) == 4
        else 0
    )
