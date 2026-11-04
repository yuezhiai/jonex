"""Digest identity — stable content fingerprint for incremental compilation."""

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

# Change when deterministic source/reading projections change. This is
# independent of the Bundle format version so historical Bundles stay readable.
SOURCE_PROJECTION_VERSION = "source-projection-2"


@dataclass
class SourceDigest:
    """Deterministic identity for a single source/artifact."""

    source_id: str
    document_id: str
    content_sha256: str
    content_generation: int
    parser_id: str
    parser_version: str = "1.0.0"
    prompt_checksum: str = ""
    manifest_checksum: str = ""
    engine_id: str = "llm_wiki"
    engine_version: str = "1.0.0"
    meta: dict[str, Any] = field(default_factory=dict)

    def fingerprint(self) -> str:
        """Return a single deterministic hash over all digest fields.

        If this fingerprint matches the previous revision's digest, the
        source can be replayed instead of recompiled.
        """
        payload = {
            "source_projection_version": SOURCE_PROJECTION_VERSION,
            "source_id": self.source_id,
            "document_id": self.document_id,
            "content_sha256": self.content_sha256,
            "content_generation": self.content_generation,
            "parser_id": self.parser_id,
            "parser_version": self.parser_version,
            "prompt_checksum": self.prompt_checksum,
            "manifest_checksum": self.manifest_checksum,
            "engine_id": self.engine_id,
            "engine_version": self.engine_version,
            "meta": self.meta,
        }
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    @classmethod
    def from_artifact(
        cls,
        artifact: Any,  # SourceArtifact
        *,
        parser_version: str = "1.0.0",
        prompt_checksum: str = "",
        manifest_checksum: str = "",
        engine_id: str = "llm_wiki",
        engine_version: str = "1.0.0",
    ) -> "SourceDigest":
        return cls(
            source_id=artifact.source_id,
            document_id=artifact.document_id,
            content_sha256=artifact.sha256,
            content_generation=artifact.content_generation,
            parser_id=artifact.parser_id,
            parser_version=parser_version,
            prompt_checksum=prompt_checksum,
            manifest_checksum=manifest_checksum,
            engine_id=engine_id,
            engine_version=engine_version,
            meta=semantic_metadata(artifact.source_metadata or {}),
        )


def source_fingerprint(
    source_id: str,
    artifacts: list[Any],
    *,
    prompt_checksum: str = "",
    manifest_checksum: str = "",
    engine_id: str = "llm_wiki",
    engine_version: str = "1.0.0",
    parser_version: str = "1.0.0",
) -> str:
    """Return one deterministic digest for a logical ``source_id``.

    The digest covers every ordered artifact of the source: the production
    ``PROMPT_CHECKSUM``, the manifest checksum, the engine identity and, per
    chunk, the stable identity (``chunk_id``), position, text and evidence
    metadata.  Chunks are sorted by a stable key so digest equality is
    deterministic regardless of artifact input order; the input order may be
    preserved for compilation.
    """
    ordered = sorted(
        artifacts,
        key=lambda a: (
            a.position if a.position is not None else -1,
            a.chunk_id,
            a.document_id,
        ),
    )
    from jonex_wiki.domain.engine.compile.repair.planner import compile_output_tokens
    payload = {
        "source_projection_version": SOURCE_PROJECTION_VERSION,
        "compile_output_tokens": compile_output_tokens(),
        "source_id": source_id,
        "prompt_checksum": prompt_checksum,
        "manifest_checksum": manifest_checksum,
        "engine_id": engine_id,
        "engine_version": engine_version,
        "parser_version": parser_version,
        "chunks": [
            {
                "document_id": a.document_id,
                "content_generation": a.content_generation,
                "parser_id": a.parser_id,
                "chunk_id": a.chunk_id,
                "position": a.position,
                "text_sha256": hashlib.sha256(a.text.encode("utf-8")).hexdigest(),
                "sha256": a.sha256,
                "evidence": semantic_metadata(a.source_metadata or {}),
            }
            for a in ordered
        ],
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def classify_changes(
    prev_digests: dict[str, str],  # source_id → fingerprint
    curr_digests: dict[str, str],
    curr_artifacts: list[Any],
) -> dict[str, list[Any]]:
    """Classify each source as new, replay, changed, or removed.

    ``new``, ``replay`` and ``changed`` contain *every* ordered artifact for
    the sources they classify, so a multi-chunk source is never collapsed to a
    single passage.  ``removed_ids`` lists source ids that disappeared.
    """
    new: list[Any] = []
    replay: list[Any] = []
    changed: list[Any] = []
    removed_ids: list[str] = [
        aid for aid in prev_digests if aid not in curr_digests
    ]

    by_source: dict[str, list[Any]] = {}
    for artifact in curr_artifacts:
        by_source.setdefault(artifact.source_id, []).append(artifact)

    for source_id, artifacts in by_source.items():
        if source_id not in prev_digests:
            new.extend(artifacts)
        elif prev_digests[source_id] == curr_digests[source_id]:
            replay.extend(artifacts)
        else:
            changed.extend(artifacts)

    return {
        "new": new,
        "replay": replay,
        "changed": changed,
        "removed_ids": removed_ids,
    }


def semantic_metadata(value):
    if isinstance(value, dict):
        return {key: semantic_metadata(item) for key, item in value.items()
                if key not in {"raw_output_dir", "run_id", "attempt_id", "input_copy", "temporary_path", "logs"}}
    if isinstance(value, list):
        return [semantic_metadata(item) for item in value]
    return value
