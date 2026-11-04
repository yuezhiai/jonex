"""Render a bounded original PDF page without changing its OCR projection."""

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4
from jonex_wiki.kernel.errors import ValidationError

RULE = "original-pdf-page-v1"


def original_sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def render_page(original, expected_sha, page_number, folder):
    if type(page_number) is not int or not 1 <= page_number <= 10000:
        raise ValidationError("PDF_VISUAL_PAGE_INVALID")
    original, folder = Path(original), Path(folder)
    if original_sha(original) != expected_sha:
        raise ValidationError("PDF_VISUAL_ORIGINAL_HASH_MISMATCH")
    folder.mkdir(parents=True, exist_ok=True)
    identity = hashlib.sha256(
        json.dumps([RULE, expected_sha, page_number, 1600]).encode()
    ).hexdigest()
    pointer = folder / (identity + ".json")
    if pointer.is_file():
        meta = json.loads(pointer.read_text())
        if (
            meta.get("original_sha256"),
            meta.get("page_number"),
            meta.get("rule"),
            meta.get("max_side"),
        ) != (expected_sha, page_number, RULE, 1600):
            raise ValidationError("PDF_VISUAL_CACHE_SCOPE_MISMATCH")
        sha = meta.get("asset_sha256", "")
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise ValidationError("PDF_VISUAL_CACHE_ID_INVALID")
        asset = folder / (sha + ".png")
        if asset.stat().st_size > 4 * 1024 * 1024:
            raise ValidationError("PDF_VISUAL_IMAGE_BUDGET")
        data = asset.read_bytes()
        if hashlib.sha256(data).hexdigest() != sha:
            raise ValidationError("PDF_VISUAL_CACHE_HASH_MISMATCH")
        return data, meta
    executable = shutil.which("pdftoppm")
    if not executable:
        raise ValidationError("PDF_VISUAL_RENDERER_UNAVAILABLE")
    try:
        result = subprocess.run(
            [
                executable,
                "-f",
                str(page_number),
                "-l",
                str(page_number),
                "-singlefile",
                "-scale-to",
                "1600",
                "-png",
                str(original),
            ],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValidationError("PDF_VISUAL_RENDER_FAILED") from exc
    data = result.stdout
    if (
        result.returncode
        or not data.startswith(b"\x89PNG\r\n\x1a\n")
        or len(data) > 4 * 1024 * 1024
    ):
        raise ValidationError("PDF_VISUAL_RENDER_FAILED_OR_IMAGE_BUDGET")
    if original_sha(original) != expected_sha:
        raise ValidationError("PDF_VISUAL_ORIGINAL_CHANGED_DURING_RENDER")
    digest = hashlib.sha256(data).hexdigest()
    meta = {
        "rule": RULE,
        "asset_sha256": digest,
        "original_sha256": expected_sha,
        "page_number": page_number,
        "max_side": 1600,
        "mime_type": "image/png",
    }
    for path, content in (
        (folder / (digest + ".png"), data),
        (pointer, json.dumps(meta).encode()),
    ):
        temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
        temporary.write_bytes(content)
        temporary.replace(path)
    return data, meta
