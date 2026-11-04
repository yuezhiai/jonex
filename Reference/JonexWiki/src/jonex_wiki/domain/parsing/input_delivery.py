"""Versioned Wiki consumer bundles. The upstream parser and its files stay untouched."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

SCHEMA = "jonex.wiki.input.v1"
ADAPTER_VERSION = "2"
MEDIA_SUFFIXES = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".mp4", ".mov", ".mkv", ".webm"}


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _segment(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:32]


def _media_items(snapshot: dict, suffix: str) -> list[dict]:
    kind = "video" if suffix in {".mp4", ".mov", ".mkv", ".webm"} else "audio"
    items = []
    for chunk in snapshot.get("chunks", []):
        content = chunk.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        anchor = dict(part.split("=", 1) for part in str(chunk.get("file_source") or "").split("|") if "=" in part)
        spans = chunk.get("spans") or []
        starts = [s["time_start"] for s in spans if s.get("time_start") is not None]
        ends = [s["time_end"] for s in spans if s.get("time_end") is not None]
        start = min(starts) if starts else anchor.get("tstart")
        end = max(ends) if ends else anchor.get("tend")
        generated = anchor.get("ctype") == "chapter" or content.startswith("【全局摘要】")
        # Upstream segments may mix ASR with model summaries. Keep that distinction;
        # a segment-level anchor does not certify every word as literal speech.
        items.append({"type": kind, "text": content,
            "time_start": float(start) if start is not None else None,
            "time_end": float(end) if end is not None else None,
            "evidence_kind": "generated_summary" if generated else "mixed_transcript_description",
            "chunk_index": chunk["chunk_index"], "spans": spans,
            "file_source": chunk.get("file_source"), "processor": "legacy.delivery"})
    return items


def publish_input_bundle(*, root: Path, tenant_id: str, kb_id: str, document_id: str,
                         generation: int, file_name: str, original: bytes | Path,
                         snapshot: dict | None, raw: bytes | None,
                         legacy_dir: Path | None = None, asset_bytes: dict[str, bytes] | None = None,
                         source_context: dict | None = None) -> str:
    """Validate first, then atomically publish an immutable, scope-bound input directory.

    The returned path is the only ingest pointer. Legacy sources are explicit and never
    receive a fabricated parser generation. A new parse delivery must have a matching generation.
    """
    if not tenant_id or not kb_id or not document_id or generation < 0:
        raise ValueError("WIKI_INPUT_SCOPE_REQUIRED")
    suffix = Path(file_name).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
        suffix = ".bin"
    gaps = []
    context_gaps = (source_context or {}).get("quality_gaps") or []
    if not isinstance(context_gaps, list):
        raise ValueError("WIKI_INPUT_QUALITY_GAPS_INVALID")
    gaps.extend(v for v in context_gaps[:100]
                if isinstance(v, str) and len(v) <= 500)
    if snapshot is not None:
        delivery_gaps = snapshot.get("delivery_quality_gaps") or []
        if not isinstance(delivery_gaps, list):
            raise ValueError("WIKI_INPUT_QUALITY_GAPS_INVALID")
        gaps.extend(v for v in delivery_gaps[:100] if isinstance(v, str) and len(v) <= 500)
        doc = snapshot.get("doc") or {}
        if any(doc.get(k) != v for k, v in (("tenant_id", tenant_id), ("kb_id", kb_id), ("doc_id", document_id))):
            raise ValueError("WIKI_INPUT_SCOPE_MISMATCH")
        if str(doc.get("status", "")).upper() != "SUCCEEDED":
            raise ValueError("WIKI_INPUT_PARSE_NOT_READY")
        actual_gen = (snapshot.get("version") or {}).get("content_generation")
        if actual_gen is None:
            raise ValueError("WIKI_INPUT_LEGACY_TASK_REPARSE_REQUIRED")
        if actual_gen != generation:
            raise ValueError("WIKI_INPUT_GENERATION_MISMATCH")
        chunks = snapshot.get("chunks") or []
        if len(chunks) != (snapshot.get("counts") or {}).get("chunks", len(chunks)):
            raise ValueError("WIKI_INPUT_TRUNCATED_SNAPSHOT")
    if suffix in MEDIA_SUFFIXES:
        if snapshot is None:
            raise ValueError("WIKI_INPUT_MEDIA_DELIVERY_REQUIRED")
        items = _media_items(snapshot, suffix)
        gaps.append("segment_timing_only; generated and mixed descriptions are not verbatim transcripts")
        if suffix in {".mp4", ".mov", ".mkv", ".webm"}:
            gaps.append("upstream_keyframes_not_guaranteed; visual supplementation may be required")
    else:
        try:
            if snapshot is not None and suffix not in {".xlsx", ".xlsm"}:
                if "content_items" not in snapshot:
                    raise ValueError("WIKI_INPUT_VERSIONED_ITEMS_REQUIRED")
                items = snapshot["content_items"]
            else:
                items = json.loads(raw) if raw is not None else []
        except (ValueError, UnicodeError) as exc:
            raise ValueError("WIKI_INPUT_INVALID_CONTENT_LIST") from exc
        if not isinstance(items, list):
            raise ValueError("WIKI_INPUT_CONTENT_LIST_NOT_ARRAY")
    native = suffix in {".xlsx", ".xlsm"}
    if not native and not items:
        raise ValueError("WIKI_INPUT_EMPTY_DELIVERY")
    parsed = _json(items)
    if isinstance(original, Path):
        with original.open("rb") as stream:
            original_sha = hashlib.file_digest(stream, "sha256").hexdigest()
    else:
        original_sha = sha(original)
    receipt = {"schema": SCHEMA, "adapter_version": ADAPTER_VERSION,
        "tenant_id": tenant_id, "kb_id": kb_id, "document_id": document_id,
        "content_generation": generation, "source_name": file_name,
        "original_path": "original" + suffix, "original_sha256": original_sha,
        "parsed_sha256": sha(parsed), "input_path": "original" + suffix if native else "content_list.json",
        "parser_delivery": {"kind": "legacy_shared_artifact",
            "task": (snapshot or {}).get("task"), "version": (snapshot or {}).get("version")},
        "counts": {"items": len(items), "chunks": len((snapshot or {}).get("chunks") or [])},
        "source_context": {k: v for k, v in (source_context or {}).items() if k in {
            "edition", "publisher", "curriculum", "subject", "grade", "province", "year", "category", "score_type",
            "origin_uri", "parent_source_sha256", "processor_id"}
            and isinstance(v, (str, int, float, bool))},
        "quality": {"publishable": True, "missing_assets": [], "gaps": gaps}, "assets": {}}
    parent = root / "parsed" / "wiki" / _segment(tenant_id) / _segment(kb_id) / _segment(document_id) / f"gen-{generation}"
    parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=".prepare-", dir=parent))
    try:
        if isinstance(original, Path):
            shutil.copyfile(original, temp / ("original" + suffix))
            with (temp / ("original" + suffix)).open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != original_sha:
                    raise ValueError("WIKI_INPUT_ORIGINAL_CHANGED_DURING_COPY")
        else:
            (temp / ("original" + suffix)).write_bytes(original)
        (temp / "content_list.json").write_bytes(parsed)
        # Copy only assets referenced by this delivery, with traversal protection.
        refs = set()
        for item in items:
            for key in ("img_path", "image_path", "chart_path"):
                value = item.get(key)
                if isinstance(value, str) and value:
                    refs.add(value)
        for relative in sorted(refs):
            path = Path(relative)
            if path.is_absolute() or ".." in path.parts:
                receipt["quality"]["missing_assets"].append(relative)
                continue
            if relative in (asset_bytes or {}):
                target = temp / path;target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(asset_bytes[relative]);receipt["assets"][relative] = sha(asset_bytes[relative])
                continue
            source = (legacy_dir / path).resolve() if legacy_dir else None
            if source is None or not source.is_relative_to(legacy_dir.resolve()) or not source.is_file():
                receipt["quality"]["missing_assets"].append(relative)
                continue
            target = temp / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            receipt["assets"][relative] = sha(target.read_bytes())
        if receipt["quality"]["missing_assets"]:
            raise ValueError("WIKI_INPUT_MISSING_ASSETS: " + ", ".join(receipt["quality"]["missing_assets"][:5]))
        # Keep raw upstream payload for audit, including non-text AV metadata.
        if raw is not None:
            (temp / "upstream.json").write_bytes(raw)
            receipt["upstream_sha256"] = sha(raw)
        encoded = _json(receipt)
        (temp / "wiki-input.json").write_bytes(encoded)
        destination = parent / sha(encoded)
        if destination.exists():
            if (destination / "wiki-input.json").read_bytes() != encoded:
                raise ValueError("WIKI_INPUT_PUBLICATION_CONFLICT")
        else:
            try:
                os.rename(temp, destination)
            except OSError:
                if not destination.is_dir() or (destination / "wiki-input.json").read_bytes() != encoded:
                    raise
        return (destination / receipt["input_path"]).relative_to(root).as_posix()
    finally:
        if temp.exists():
            shutil.rmtree(temp)
