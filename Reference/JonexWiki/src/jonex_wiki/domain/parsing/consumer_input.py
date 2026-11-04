"""Validate the platform consumer contract before any document/database mutation."""
import hashlib
import json
from pathlib import Path


def read_consumer_receipt(full: Path, *, tenant_id: str, kb_id: str, document_id: str) -> dict | None:
    path = full.parent / "wiki-input.json"
    if not path.is_file():
        return None  # Existing standalone/legacy parser routes remain explicit legacy inputs.
    receipt = json.loads(path.read_text("utf-8"))
    if receipt.get("schema") != "jonex.wiki.input.v1":
        raise ValueError("WIKI_INPUT_SCHEMA_UNSUPPORTED")
    for key, expected in (("tenant_id", tenant_id), ("kb_id", kb_id), ("document_id", document_id)):
        if receipt.get(key) != expected:
            raise ValueError("WIKI_INPUT_SCOPE_MISMATCH")
    if full.name != receipt.get("input_path") or not receipt.get("quality", {}).get("publishable"):
        raise ValueError("WIKI_INPUT_NOT_PUBLISHABLE")
    expected = {receipt["original_path"]: receipt["original_sha256"],
                "content_list.json": receipt["parsed_sha256"], **receipt.get("assets", {})}
    if receipt.get("upstream_sha256"):
        expected["upstream.json"] = receipt["upstream_sha256"]
    for relative, digest in expected.items():
        target = (full.parent / relative).resolve()
        if not target.is_relative_to(full.parent.resolve()) or not target.is_file():
            raise ValueError("WIKI_INPUT_ASSET_MISSING")
        with target.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise ValueError("WIKI_INPUT_HASH_MISMATCH")
    receipt["receipt_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return receipt
