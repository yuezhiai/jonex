"""LIVE-CORE-001 — Deterministic portable Wiki bundle codec.

Encodes a published Wiki revision as one immutable, portable byte blob that can
be persisted through ``ArtifactStore``.  The bundle is a canonical JSON object
whose keys are sorted and whose values are compact::

    {
      "format": "llm-wiki-plus",
      "schema_version": 3,
      "engine_id": "llm_wiki",
      "engine_version": "1.0.0",
      "revision_id": "rev_...",
      "provenance": {
        "tenant_id": "tenant_...",
        "context_id": "context_...",
        "source_inventory_digest": "sha256 hex",
        "runtime_snapshot_checksum": "sha256 hex"
      },
      "checksums": {"relative/path": "sha256 hex", ...},
      "files": {"relative/path": "base64", ...}
    }

Decode is the trust boundary.  It re-validates every relative path (no
absolute path, no backslash, no ``..``/empty/dot segments, no duplicates),
rejects a wrong format/schema, requires every top-level artifact, and verifies
every checksum against the actual decoded file bytes.  Traversal, duplicate
paths, checksum drift and malformed content fail closed with ``ValueError``.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Sequence

from jonex_wiki.domain.engine.compile.build_identity import implementation_checksum
from jonex_wiki.kernel.contracts import (
    BundleProvenance,
    CodeIdentity,
    EffectiveRuntimeSnapshot,
    EmbeddingIdentity,
    SourceArtifact,
    SourceInventoryEntry,
    SourceInventorySnapshot,
)
from jonex_wiki.kernel.runtime_env import current_env

BUNDLE_FORMAT = "llm-wiki-plus"
SCHEMA_VERSION = 3

# Every top-level artifact a verified production bundle must contain.
REQUIRED_FILES: List[str] = [
    "revision.json",
    "manifest.json",
    "claims.jsonl",
    "claim_audit.json",
    "ledger.json",
    "error_book.yaml",
    "provenance.json",
    "indexes/manifest.json",
    "indexes/lexical.sqlite",
]

# Only compilation and published-index inputs belong to the Bundle identity.
# Query-only knobs are read live by query_task and must not invalidate a Wiki.
# Never widen this to a prefix scan: unknown variables may contain credentials.
#
# 这些变量名的**值**来自 Settings.runtime_env()(dev runner 注入),测试路径默认回退
# 到进程环境(monkeypatch.setenv 仍生效);每个名字对应 Settings 里的一个已校验字段
# (见 infra/settings.py)。
SAFE_RUNTIME_ENV = (
    "JONEX_WIKI_COMPILE_OUTPUT_TOKENS",
    "JONEX_WIKI_SEMANTIC_REVIEW_OUTPUT_TOKENS",
    "LLM_WIKI_SEMANTIC_REVIEW",
)


class BundleSchemaIncompatibleError(ValueError):
    """The bundle container uses a ``schema_version`` this codec cannot serve.

    Raised only for a mismatched container ``schema_version`` — a schema
    evolution condition, not byte-level corruption.  ``schema_version`` carries
    the actual version found in the container so callers can record it.  Still
    a ``ValueError``, so existing fail-closed catch sites keep working.
    """

    def __init__(self, message: str, *, schema_version: Any) -> None:
        super().__init__(message)
        self.schema_version = schema_version


def sha256_hex(data: bytes) -> str:
    """Return the lowercase hex SHA-256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize a JSON value into deterministic UTF-8 bytes."""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def snapshot_checksum(value: Any) -> str:
    """Return the checksum of a Pydantic model or plain JSON value."""
    if hasattr(value, "dict"):
        value = value.model_dump()
    return sha256_hex(canonical_json_bytes(value))


def build_source_inventory(
    artifacts: Sequence[SourceArtifact],
    compiler_fingerprints: Mapping[str, str],
) -> SourceInventorySnapshot:
    """Freeze the exact logical Source set and ordered parser artifacts.

    The digest changes when source membership, generation, parser, original
    document bytes, chunk identity/text/position/metadata, or compiler
    fingerprint changes.  Caller order cannot change it.
    """
    grouped: Dict[str, List[SourceArtifact]] = {}
    for artifact in artifacts:
        grouped.setdefault(artifact.source_id, []).append(artifact)
    if set(grouped) != set(compiler_fingerprints):
        raise ValueError("source inventory and compiler fingerprints must match")

    entries: List[SourceInventoryEntry] = []
    for source_id in sorted(grouped):
        items = sorted(
            grouped[source_id],
            key=lambda item: (
                item.position if item.position is not None else 2**31,
                item.page_number if item.page_number is not None else 2**31,
                item.chunk_id,
            ),
        )
        first = items[0]
        identity = (
            first.document_id,
            first.content_generation,
            first.parser_id,
            first.sha256,
        )
        if any(
            (
                item.document_id,
                item.content_generation,
                item.parser_id,
                item.sha256,
            )
            != identity
            for item in items
        ):
            raise ValueError("one logical source has inconsistent artifact identity")
        chunk_payload = [
            {
                "chunk_id": item.chunk_id,
                "page_number": item.page_number,
                "position": item.position,
                "source_metadata": item.source_metadata,
                "text_sha256": sha256_hex(item.text.encode("utf-8")),
            }
            for item in items
        ]
        entries.append(
            SourceInventoryEntry(
                source_id=source_id,
                document_id=first.document_id,
                content_generation=first.content_generation,
                parser_id=first.parser_id,
                document_sha256=first.sha256,
                artifact_count=len(items),
                artifact_digest=sha256_hex(canonical_json_bytes(chunk_payload)),
                compiler_fingerprint=compiler_fingerprints[source_id],
            )
        )
    material = {
        "schema_version": 1,
        "entries": [entry.model_dump() for entry in entries],
    }
    return SourceInventorySnapshot(
        entries=entries,
        digest=sha256_hex(canonical_json_bytes(material)),
    )


def capture_effective_runtime(
    manifest: Mapping[str, Any],
    *,
    prompt_checksum: str,
    tool_checksum: str,
    environment: Mapping[str, str] | None = None,
    commit_sha: str | None = None,
    dirty: bool | None = None,
) -> EffectiveRuntimeSnapshot:
    """Capture safe effective runtime values without shelling out or secrets."""
    env = environment if environment is not None else current_env()
    overrides = {
        name: str(env[name])
        for name in SAFE_RUNTIME_ENV
        if name in env and str(env[name]) != ""
    }
    if overrides.get("LLM_WIKI_SEMANTIC_REVIEW", "off").strip().casefold() in {
        "0", "off", "false", "no", ""
    }:
        overrides.pop("JONEX_WIKI_SEMANTIC_REVIEW_OUTPUT_TOKENS", None)
    if commit_sha is None:
        commit_sha = str(
            env.get("APP_COMMIT_SHA")
            or env.get("GIT_COMMIT")
            or env.get("SOURCE_COMMIT")
            or "unknown"
        )
    if dirty is None:
        raw_dirty = env.get("APP_GIT_DIRTY") or env.get("GIT_DIRTY")
        dirty = _parse_optional_bool(raw_dirty)
    raw_models = manifest.get("models", {})
    models = (
        {str(key): str(value) for key, value in raw_models.items()}
        if isinstance(raw_models, Mapping)
        else {}
    )
    return EffectiveRuntimeSnapshot(
        prompt_checksum=prompt_checksum,
        tool_checksum=tool_checksum,
        models=models,
        embedding=EmbeddingIdentity(status="disabled"),
        environment_overrides=overrides,
        code=CodeIdentity(commit=commit_sha, dirty=dirty,
                          source_tree_sha256=implementation_checksum(),
                          compiler_sha256=implementation_checksum(compilation=True)),
    )


def _parse_optional_bool(value: Any) -> bool | None:
    if value in (None, ""):
        return None
    normalized = str(value).strip().casefold()
    if normalized in {"1", "true", "yes", "on", "dirty"}:
        return True
    if normalized in {"0", "false", "no", "off", "clean"}:
        return False
    raise ValueError("dirty build override must be boolean")


def is_safe_relative(rel: str) -> bool:
    """Return True only for a safe relative path with no traversal risk."""
    if not isinstance(rel, str) or not rel:
        return False
    if rel.startswith("/") or rel.startswith("\\"):
        return False
    if "\\" in rel:
        return False
    parts = rel.split("/")
    if any(p == "" or p in (".", "..") for p in parts):
        return False
    return True


def _reject_duplicate_keys(pairs: Any) -> Dict[str, Any]:
    """``object_pairs_hook`` that fails closed on duplicate JSON keys."""
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate bundle path: " + str(key))
        result[key] = value
    return result


@dataclass
class DecodedBundle:
    """The result of reopening and verifying a saved production bundle."""

    files: Dict[str, bytes]
    checksums: Dict[str, str]
    revision_id: str
    engine_id: str
    engine_version: str
    provenance: BundleProvenance


def encode_bundle(
    *,
    revision_id: str,
    engine_id: str,
    engine_version: str,
    files: Dict[str, bytes],
    provenance: BundleProvenance | Mapping[str, Any] | None = None,
) -> bytes:
    """Encode a validated revision into canonical portable bundle bytes.

    ``files`` must not contain ``checksums.json`` (there is no separate
    manifest file); the checksum map is embedded in the container.  Every
    relative key must be safe, duplicate keys are rejected, and every required
    top-level artifact must be present.  Returns deterministic bytes.
    """
    if not isinstance(revision_id, str) or not revision_id:
        raise ValueError("revision_id must be non-blank")
    if not isinstance(engine_id, str) or not engine_id:
        raise ValueError("engine_id must be non-blank")
    if not isinstance(engine_version, str) or not engine_version:
        raise ValueError("engine_version must be non-blank")
    if not isinstance(files, dict) or not files:
        raise ValueError("bundle files must be a non-empty mapping")

    for rel in files:
        if not is_safe_relative(rel):
            raise ValueError("unsafe bundle path: " + repr(rel))
    if len(files) != len(set(files)):
        raise ValueError("duplicate bundle path")
    for required in REQUIRED_FILES:
        if required not in files:
            raise ValueError("missing required file: " + required)

    if provenance is None:
        try:
            raw_provenance = json.loads(files["provenance.json"].decode("utf-8"))
            provenance = {
                "tenant_id": raw_provenance["tenant_id"],
                "context_id": raw_provenance["context_id"],
                "source_inventory_digest": raw_provenance[
                    "source_inventory_digest"
                ],
                "runtime_snapshot_checksum": raw_provenance[
                    "runtime_snapshot_checksum"
                ],
            }
        except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
            raise ValueError("bundle provenance is missing or malformed") from exc
    provenance_model = BundleProvenance.model_validate(provenance)

    checksums = {rel: sha256_hex(data) for rel, data in files.items()}
    payload = {
        "format": BUNDLE_FORMAT,
        "schema_version": SCHEMA_VERSION,
        "engine_id": engine_id,
        "engine_version": engine_version,
        "revision_id": revision_id,
        "provenance": provenance_model.model_dump(),
        "checksums": checksums,
        "files": {
            rel: base64.b64encode(data).decode("ascii")
            for rel, data in files.items()
        },
    }
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def decode_bundle(data: bytes) -> DecodedBundle:
    """Reopen and verify a saved production bundle, returning its files.

    Fails closed on: unparseable/duplicate-key JSON, a wrong format or schema,
    a missing/blank revision id, unsafe paths, checksum/files key mismatch,
    missing required top-level artifacts, malformed base64 and any checksum
    drift.  Only the bytes passed in are ever read.
    """
    if not isinstance(data, bytes) or not data:
        raise ValueError("bundle bytes must be non-empty")
    try:
        raw = data.decode("utf-8")
        payload = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("unparseable bundle") from exc
    if not isinstance(payload, dict):
        raise ValueError("bundle must be a JSON object")
    if payload.get("format") != BUNDLE_FORMAT:
        raise ValueError("unsupported bundle format")
    schema_version = payload.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        # LWP-CURATE-RECOVERY (P2-2): a missing, boolean or non-integer
        # ``schema_version`` is byte-level corruption, not schema evolution.
        # ``bool`` is an ``int`` subclass so it must be excluded explicitly;
        # only a real integer that differs from SCHEMA_VERSION is the
        # distinguishable BundleSchemaIncompatibleError; anything else fails
        # closed as a bare ValueError so a corrupt bundle can never masquerade
        # as a clean schema-upgrade skip path in the curation worker.
        raise ValueError("bundle schema_version must be an integer")
    if schema_version != SCHEMA_VERSION:
        raise BundleSchemaIncompatibleError(
            f"unsupported bundle schema_version: "
            f"{schema_version!r} (expected {SCHEMA_VERSION})",
            schema_version=schema_version,
        )

    revision_id = payload.get("revision_id")
    if not isinstance(revision_id, str) or not revision_id:
        raise ValueError("bundle revision_id must be non-blank")
    engine_id = payload.get("engine_id")
    engine_version = payload.get("engine_version")
    if not isinstance(engine_id, str) or not engine_id:
        raise ValueError("bundle engine_id must be non-blank")
    if not isinstance(engine_version, str) or not engine_version:
        raise ValueError("bundle engine_version must be non-blank")
    try:
        provenance = BundleProvenance.model_validate(payload.get("provenance"))
    except (TypeError, ValueError) as exc:
        raise ValueError("bundle provenance must be valid") from exc

    checksums = payload.get("checksums")
    files_map = payload.get("files")
    if not isinstance(checksums, dict) or not isinstance(files_map, dict):
        raise ValueError("bundle checksums/files must be objects")
    if set(checksums) != set(files_map):
        raise ValueError("bundle checksums and files must match exactly")

    for rel in checksums:
        if not is_safe_relative(rel):
            raise ValueError("unsafe bundle path: " + repr(rel))
    for required in REQUIRED_FILES:
        if required not in checksums:
            raise ValueError("missing required file: " + required)

    out_files: Dict[str, bytes] = {}
    for rel, digest in checksums.items():
        if not isinstance(digest, str):
            raise ValueError("checksum must be a string: " + rel)
        encoded = files_map[rel]
        if not isinstance(encoded, str):
            raise ValueError("bundle file must be base64 text: " + rel)
        try:
            blob = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("malformed base64: " + rel) from exc
        if sha256_hex(blob) != digest:
            raise ValueError("checksum mismatch: " + rel)
        out_files[rel] = blob

    return DecodedBundle(
        files=out_files,
        checksums=dict(checksums),
        revision_id=revision_id,
        engine_id=engine_id,
        engine_version=engine_version,
        provenance=provenance,
    )


__all__ = [
    "BUNDLE_FORMAT",
    "SCHEMA_VERSION",
    "REQUIRED_FILES",
    "DecodedBundle",
    "SAFE_RUNTIME_ENV",
    "BundleSchemaIncompatibleError",
    "build_source_inventory",
    "canonical_json_bytes",
    "capture_effective_runtime",
    "sha256_hex",
    "snapshot_checksum",
    "is_safe_relative",
    "encode_bundle",
    "decode_bundle",
]
