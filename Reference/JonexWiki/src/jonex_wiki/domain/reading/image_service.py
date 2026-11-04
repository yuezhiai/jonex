"""Version-bound picture metadata, lexical discovery and lazy display renditions."""

import base64
import hashlib
import json
import os
import re
import tempfile
from contextlib import closing
from pathlib import Path

from jonex_wiki.domain.engine.compile.image_index import INDEX, SCHEMA, search_tokens
from jonex_wiki.kernel.errors import NotFoundError, ValidationError

CATALOG = "query/media_catalog.json"
ASSET_ID = re.compile(r"image_[0-9a-f]{32}")


def _stamp(view):
    return {
        "revision": view.manifest_hash,
        "tenant": view.tenant_id,
        "kb": view.context_id,
    }


def _validate_index(db):
    row = db.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()
    if row is None or row[0] != SCHEMA:
        raise ValidationError("IMAGE_INDEX_SCHEMA_MISMATCH")


def open_index(view):
    # Check once per immutable file identity, not once per row or page request.
    view.verified_file_path(INDEX)
    return view.open_sqlite(INDEX)


def get_image(view, asset_id, document_id=""):
    if not ASSET_ID.fullmatch(asset_id or ""):
        raise ValidationError("IMAGE_ID_INVALID")
    if INDEX in view.list_entries():
        with closing(open_index(view)) as db:
            _validate_index(db)
            row = db.execute(
                "SELECT payload FROM images WHERE asset_id=?", (asset_id,)
            ).fetchone()
            asset = json.loads(row[0]) if row else None
    elif CATALOG in view.list_entries():
        asset = next(
            (
                a
                for a in json.loads(view.read_bytes(CATALOG)).get("assets", [])
                if a["asset_id"] == asset_id
            ),
            None,
        )
    else:
        asset = None
    if asset is None or document_id and asset["document_id"] != document_id:
        raise NotFoundError("IMAGE_NOT_IN_DOCUMENT_OR_VERSION")
    return {**asset, "bundle_sha": view.manifest_hash, "occurrence_id": asset_id}


def find_images(view, options):
    if set(options) - {
        "revision",
        "query",
        "document_id",
        "page_path",
        "limit",
        "cursor",
    }:
        raise ValidationError("IMAGE_SEARCH_FIELDS_INVALID")
    query, doc, page = (
        options.get(k, "") for k in ("query", "document_id", "page_path")
    )
    limit, cursor = options.get("limit", 20), options.get("cursor", "")
    if (
        any(not isinstance(v, str) for v in (query, doc, page, cursor))
        or len(query) > 512
    ):
        raise ValidationError("IMAGE_SEARCH_VALUE_INVALID")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValidationError("IMAGE_SEARCH_LIMIT_INVALID")
    scope = hashlib.sha256(
        json.dumps([_stamp(view), query, doc, page], sort_keys=True).encode()
    ).hexdigest()
    offset = 0
    if cursor:
        try:
            token = json.loads(base64.urlsafe_b64decode(cursor))
            offset = token["offset"]
            if token["scope"] != scope or type(offset) is not int or offset < 0:
                raise ValueError()
        except Exception as exc:
            raise ValidationError("IMAGE_CURSOR_SCOPE_MISMATCH") from exc
    if INDEX not in view.list_entries():
        # Legacy bundles remain readable; they need a projection rebuild for
        # the indexed search, rather than an unbounded request-time scan.
        coverage = "image_index_unavailable"
        if CATALOG in view.list_entries():
            catalog = json.loads(view.read_bytes(CATALOG))
            if not catalog.get("assets") and not catalog.get("unresolved"):
                coverage = "no_images"
        return {
            "items": [],
            "total": 0,
            "revision": view.manifest_hash,
            "has_more": False,
            "cursor": None,
            "coverage": coverage,
        }
    tokens = search_tokens(query)[:32]
    if query.strip() and not tokens:
        return {
            "items": [],
            "total": 0,
            "revision": view.manifest_hash,
            "has_more": False,
            "cursor": None,
        }
    with closing(open_index(view)) as db:
        _validate_index(db)
        where, params = [], []
        join = " JOIN image_text f ON f.rowid=i.id" if tokens else ""
        if tokens:
            where.append("image_text MATCH ?")
            params.append(" OR ".join('"' + t.replace('"', '""') + '"' for t in tokens))
        if doc:
            where.append("i.document_id=?")
            params.append(doc)
        if page:
            where.append(
                "EXISTS(SELECT 1 FROM page_images p WHERE p.asset_id=i.asset_id AND p.page=?)"
            )
            params.append(page)
        clause = " WHERE " + " AND ".join(where) if where else ""
        source = " FROM images i" + join + clause
        total = db.execute("SELECT count(*)" + source, params).fetchone()[0]
        rank = "bm25(image_text,3,4,2,1,0.5)" if tokens else "0"
        rows = db.execute(
            "SELECT i.payload," + rank + source + " ORDER BY 2,i.id LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        count = db.execute("SELECT count(*) FROM images").fetchone()[0]
        items = [
            {
                **json.loads(row[0]),
                "bundle_sha": view.manifest_hash,
                "score": -row[1],
                "score_kind": "lexical_rank",
                "usage": "illustration",
            }
            for row in rows
        ]
    more = offset + len(items) < total
    next_cursor = (
        base64.urlsafe_b64encode(
            json.dumps({"scope": scope, "offset": offset + len(items)}).encode()
        ).decode()
        if more
        else None
    )
    return {
        "items": items,
        "total": total,
        "revision": view.manifest_hash,
        "has_more": more,
        "cursor": next_cursor,
        "coverage": "existing_source_text_only",
        "indexed_images": count,
        "limitations": [
            "Pictures without usable text signals may not be retrieved. Nearby text is a candidate association."
        ],
    }


def page_images(view, paths, body, limit=24):
    if INDEX not in view.list_entries() or not paths:
        return body, [], 0
    with closing(open_index(view)) as db:
        _validate_index(db)
        query = (
            " FROM page_images p JOIN images i ON i.asset_id=p.asset_id WHERE p.page IN ("
            + ",".join("?" for _ in paths)
            + ")"
        )
        total = db.execute(
            "SELECT count(DISTINCT i.asset_id)" + query, tuple(paths)
        ).fetchone()[0]
        rows = db.execute(
            "SELECT i.payload,p.anchor,p.method,p.page"
            + query
            + " ORDER BY i.id,p.page LIMIT ?",
            (*paths, limit * 2),
        ).fetchall()
    out, seen = [], set()
    for raw, anchor, method, source_page in rows:
        asset = json.loads(raw)
        if asset["asset_id"] in seen or len(out) >= limit:
            continue
        seen.add(asset["asset_id"])
        token = "#wiki-image/" + asset["asset_id"]
        placement = "gallery"
        if anchor.startswith("#wiki-source-image/") and body.count(anchor) == 1:
            body = body.replace(anchor, token, 1)
            placement = "inline"
        elif anchor and body.count(anchor) == 1:
            end = body.find("\n\n", body.index(anchor) + len(anchor))
            end = end if end >= 0 else len(body)
            body = body[:end] + "\n\n![](" + token + ")" + body[end:]
            placement = "inline"
        out.append(
            {
                **asset,
                "bundle_sha": view.manifest_hash,
                "placement": placement,
                "association_method": method,
                "source_page": source_page,
                "usage": "illustration",
            }
        )
    return body, out, total


def image_file(view, asset, *, variant="original", cache_dir=None, region=None):
    if variant not in {"original", "display", "preview"}:
        raise ValidationError("IMAGE_VARIANT_INVALID")
    path = asset.get("path", "")
    if not re.fullmatch(r"media/assets/[0-9a-f]{64}\.(jpg|jpeg|png|webp)", path):
        raise ValidationError("IMAGE_ASSET_PATH_INVALID")
    original = Path(view.verified_file_path(path))
    # The Bundle and the image occurrence must agree, even after cold reopen.
    if original.stem != asset["sha256"]:
        raise ValidationError("IMAGE_ASSET_CONTENT_MISMATCH")
    transform = (asset.get("source_locator") or {}).get("transform") or {}
    if region is not None and (
        not isinstance(region, list)
        or len(region) != 4
        or any(type(v) not in (int, float) or not 0 <= v <= 1 for v in region)
        or region[0] >= region[2]
        or region[1] >= region[3]
    ):
        raise ValidationError("IMAGE_REGION_INVALID")
    if variant == "original" and region is not None:
        raise ValidationError("IMAGE_REGION_NEEDS_RENDITION")
    if (
        variant == "original"
        or not region
        and not transform
        and (
            variant == "display"
            or max(asset.get("width", 100000), asset.get("height", 100000)) <= 1600
            and asset.get("bytes", 10000000) <= 1024 * 1024
        )
    ):
        return original, {
            "sha256": asset["sha256"],
            "mime_type": asset["mime_type"],
            "variant": "original",
            "width": asset.get("width"),
            "height": asset.get("height"),
        }
    if cache_dir is None:
        raise ValidationError("IMAGE_RENDER_CACHE_REQUIRED")
    key = hashlib.sha256(
        json.dumps(
            ["image-display-v1", asset["sha256"], transform, variant, region],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    folder = Path(cache_dir)
    folder.mkdir(parents=True, exist_ok=True)
    target, meta = folder / (key + ".png"), folder / (key + ".json")
    if target.is_file() and meta.is_file():
        return target, json.loads(meta.read_text())
    from PIL import Image, ImageOps

    with Image.open(original) as raw:
        if raw.width * raw.height > 32_000_000:
            raise ValidationError("IMAGE_PIXEL_BUDGET")
        img = ImageOps.exif_transpose(raw)
        crop = transform.get("crop") or {}
        l, t, r, b = (crop.get(k, 0) for k in ("l", "t", "r", "b"))
        if (
            any(type(v) not in (int, float) or not 0 <= v < 1 for v in (l, t, r, b))
            or l + r >= 1
            or t + b >= 1
        ):
            raise ValidationError("IMAGE_CROP_INVALID")
        if crop:
            img = img.crop(
                (
                    round(l * img.width),
                    round(t * img.height),
                    round((1 - r) * img.width),
                    round((1 - b) * img.height),
                )
            )
        if transform.get("flip_h"):
            img = ImageOps.mirror(img)
        if transform.get("flip_v"):
            img = ImageOps.flip(img)
        rotation = transform.get("rotation", 0)
        if not isinstance(rotation, (int, float)) or abs(rotation) > 360:
            raise ValidationError("IMAGE_ROTATION_INVALID")
        if rotation:
            img = img.rotate(-rotation, expand=True)
        if region:
            left, top, right, bottom = region
            box = (
                round(left * img.width),
                round(top * img.height),
                round(right * img.width),
                round(bottom * img.height),
            )
            if box[0] >= box[2] or box[1] >= box[3]:
                raise ValidationError("IMAGE_REGION_EMPTY")
            img = img.crop(box)
        img.thumbnail((1600, 1600) if variant == "preview" else (4096, 4096))
        with tempfile.NamedTemporaryFile(
            dir=folder, suffix=".png", delete=False
        ) as tmp:
            tmp_path = Path(tmp.name)
        try:
            img.save(tmp_path, format="PNG")
            if tmp_path.stat().st_size > 8 * 1024 * 1024:
                raise ValidationError("IMAGE_RENDER_BYTE_BUDGET")
            with tmp_path.open("rb") as stream:
                sha = hashlib.file_digest(stream, "sha256").hexdigest()
            os.replace(tmp_path, target)
            info = {
                "sha256": sha,
                "source_sha256": asset["sha256"],
                "mime_type": "image/png",
                "variant": variant,
                "width": img.width,
                "height": img.height,
                "render_key": key,
                "transform": transform,
                "region": region,
            }
            tmp_meta = meta.with_suffix(".json." + os.urandom(6).hex())
            tmp_meta.write_text(json.dumps(info, ensure_ascii=False))
            os.replace(tmp_meta, meta)
            trim_renditions(folder, keep=key)
            return target, info
        finally:
            tmp_path.unlink(missing_ok=True)


def trim_renditions(folder, *, keep, max_files=128, max_bytes=128 * 1024 * 1024):
    """Bound disposable render cache; source assets are never evicted here."""
    entries = []
    for path in folder.glob("*.png"):
        try:
            stat = path.stat()
            entries.append((stat.st_mtime_ns, path, stat.st_size))
        except FileNotFoundError:
            continue
    count, size = len(entries), sum(row[2] for row in entries)
    for _, path, amount in sorted(entries):
        if count <= max_files and size <= max_bytes:
            break
        if path.stem == keep:
            continue
        path.unlink(missing_ok=True)
        path.with_suffix(".json").unlink(missing_ok=True)
        count -= 1
        size -= amount
