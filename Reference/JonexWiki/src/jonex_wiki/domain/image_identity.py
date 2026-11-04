"""Pure source-image anchors shared by consumption and publication."""

import hashlib
import json

IMAGE_SUFFIXES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def identity(kind, *parts):
    return (
        kind
        + "_"
        + hashlib.sha256(
            json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()[:32]
    )


def image_anchor(source_name, position, relative):
    return "#wiki-source-image/" + identity("anchor", source_name, position, relative)


def plain(value, limit=2048):
    if isinstance(value, list):
        value = "\n".join(v for v in value if isinstance(v, str))
    return value.strip()[:limit] if isinstance(value, str) else ""
