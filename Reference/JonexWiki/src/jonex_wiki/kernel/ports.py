"""Abstract ports that ContextEngine implementations depend on.

Every port is an ABC — engines code against these interfaces, never against
concrete infrastructure.  This is what makes it possible to swap storage,
model providers, or progress reporting without touching engine logic.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from datetime import datetime
from dataclasses import dataclass,field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Sequence, Tuple

from jonex_wiki.kernel.contracts import CurationPhase, SourceArtifact


@dataclass(frozen=True)
class RevisionFileReference:
    """Checksummed immutable file; resolve only if sharing its inode fails."""

    path: str
    sha256: str
    read_bytes: Callable[[], bytes]
    file_path: str | None = None
    verify_file: Callable[[], str] | None = None


@dataclass(frozen=True)
class RevisionFileArtifact:
    """A newly built private file, consumed by staging without a bytes copy."""
    path: str
    source_file: str
    sha256: str
    verified_identity: tuple | None = field(default=None,init=False,repr=False)

    @classmethod
    def from_file(cls, path, source_file):
        file=Path(source_file);before=file.stat()
        with file.open('rb') as stream:checksum=hashlib.file_digest(stream,'sha256').hexdigest()
        after=file.stat()
        def identity(s):return (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
        if identity(before)!=identity(after):raise ValueError('private artifact changed while hashing')
        result=cls(path,str(file),checksum)
        object.__setattr__(result,'verified_identity',identity(after))
        import weakref
        weakref.finalize(result,file.unlink,missing_ok=True)
        return result

    def read_bytes(self):return Path(self.source_file).read_bytes()
    def cleanup(self):Path(self.source_file).unlink(missing_ok=True)


class SourceArtifactPort(ABC):
    """Provide filtered, ordered document chunks from existing parse results.

    Implementations read from the parser-stage result and normalise it into
    ``SourceArtifact`` instances.
    """

    @abstractmethod
    async def get_artifacts(
        self,
        context_id: str,
        tenant_id: str,
        *,
        since_generation: int | None = None,
    ) -> List[SourceArtifact]:
        """Return all artifacts for *context_id*, ordered by document then position.

        When *since_generation* is set, only return artifacts whose
        ``content_generation`` is strictly greater than the given value (used
        for incremental curation).

        Raises:
            ValueError: if *context_id* or *tenant_id* is empty / invalid.
        """
        ...


class ModelPort(ABC):
    """Send structured-output requests to an LLM through the platform's LLM Gateway.

    Engines must not call model providers directly.
    """

    @abstractmethod
    async def complete(
        self,
        messages: List[Dict[str, Any]],
        schema: Dict[str, Any] | None = None,
        *,
        max_tokens: int = 4096,
        tools: List[Dict[str, Any]] | None = None,
        model: str | None = None,
        tenant_id: str = "",
        context_id: str = "",
        trace_id: str = "",
        thinking: Dict[str, Any] | None = None,
        scene: str | None = None,
    ) -> Dict[str, Any]:
        """Return a JSON object that conforms to *schema* (JSON Schema).

        The implementation must:
        - Route through the LLM Gateway (``llm-gateway:8787/v1``).
        - Inject per-tenant and per-context metering headers.
        - Raise a structured error on persistent parse failures after
          the configured retry budget.

        ``scene`` labels the logical call site for per-scene metering (one
        scene per engine call point, see ARCHITECTURE §8.3).  ``None`` (default)
        means "use the implementation's default scene".

        ``thinking`` optionally overrides the gateway's scene-level thinking
        control for this single call (e.g. ``{"type": "enabled"}`` to force
        reasoning on a reasoning-dense stage, ``{"type": "disabled"}`` to force
        it off on a structured-extraction stage).  ``None`` (default) leaves the
        gateway's scene default in effect.  Implementations MUST forward it
        verbatim in the request body when supported, and MUST ignore it safely
        when the provider/upstream does not accept a ``thinking`` field.
        """
        ...



class ArtifactStore(ABC):
    """Store and retrieve immutable Revision bundles.

    Production uses COS / S3-compatible object storage.  The engine only sees
    byte arrays and opaque keys.
    """

    @abstractmethod
    async def put_bundle(
        self,
        context_id: str,
        revision_id: str,
        bundle_bytes: bytes,
    ) -> str:
        """Persist *bundle_bytes* and return the object-storage key.

        Legacy compatibility hook for older test stores.  Production callers
        use ``stage_bundle`` because tenant identity cannot be inferred from
        this signature.  The only current bundle format is canonical JSON.
        """
        ...

    @abstractmethod
    async def get_bundle(self, key: str) -> bytes:
        """Retrieve the bytes stored under *key*.

        Raises:
            FileNotFoundError: if the key does not exist.
        """
        ...

    async def stage_bundle(
        self,
        tenant_id: str,
        context_id: str,
        revision_id: str,
        bundle_bytes: bytes,
        *,
        attempt_id: str | None = None,
    ) -> str:
        """Write an unpublished candidate and return its opaque staging key.

        The default delegates to ``put_bundle`` so established deterministic
        test doubles keep working.  Production storage overrides this method
        and includes tenant/context/revision/attempt identity in its key.
        """
        del tenant_id, attempt_id
        return await self.put_bundle(context_id, revision_id, bundle_bytes)

    async def publish_bundle(
        self,
        staging_key: str,
        *,
        tenant_id: str,
        context_id: str,
        revision_id: str,
        expected_sha256: str,
    ) -> str:
        """Promote one verified staging candidate to an immutable final key.

        A compatibility store already writes its final key in ``put_bundle``;
        for it, promotion is only a checksum gate.  Production storage copies
        and read-verifies the candidate before removing the staging object.
        """
        del tenant_id, context_id, revision_id
        data = await self.get_bundle(staging_key)
        if hashlib.sha256(data).hexdigest() != expected_sha256:
            raise ValueError("staging bundle checksum mismatch")
        return staging_key

    def publication_key(
        self,
        staging_key: str,
        *,
        tenant_id: str,
        context_id: str,
        revision_id: str,
        expected_sha256: str,
    ) -> str:
        """Predict the final candidate key for durable orphan bookkeeping."""
        del tenant_id, context_id, revision_id, expected_sha256
        return staging_key

    async def discard_bundle(self, key: str) -> bool:
        """Best-effort deletion hook for unpublished or orphaned objects."""
        del key
        return False

    async def list_orphans(
        self,
        *,
        candidate_keys: Sequence[str],
        referenced_keys: set[str],
    ) -> List[str]:
        """Return existing known candidates not referenced by a Revision row."""
        orphans: List[str] = []
        for key in sorted(set(candidate_keys) - set(referenced_keys)):
            try:
                await self.get_bundle(key)
            except (FileNotFoundError, KeyError):
                continue
            orphans.append(key)
        return orphans

    async def reclaim_orphans(self, orphan_keys: Sequence[str]) -> List[str]:
        """Delete known orphan keys and return the keys actually reclaimed."""
        reclaimed: List[str] = []
        for key in sorted(set(orphan_keys)):
            if await self.discard_bundle(key):
                reclaimed.append(key)
        return reclaimed

    # ── directory-layout revision API (dev 目录布局) ─────────────────
    # These four methods are the primary publication/reading contract for the
    # dev directory layout (Layout + manifest.files + revision.json + published
    # symlink).  The engine calls them directly; there is no hasattr fallback
    # back to the single-blob put_bundle/stage_bundle path.

    async def stage_revision(
        self,
        tenant_id: str,
        context_id: str,
        revision_id: str,
        files: Mapping[str, bytes] | Iterable[Tuple[str, bytes | RevisionFileReference]],
        *,
        attempt_id: str | None = None,
        parent_revision: str | None = None,
        fingerprints: Mapping[str, str] | None = None,
    ) -> str:
        """Write an unpublished directory revision and return its staging key.

        ``files`` maps paths to bytes, or yields (path, bytes) pairs; the store writes
        them under a staging directory keyed by the revision/attempt identity.
        """
        raise NotImplementedError

    async def publish_revision(
        self,
        staging_key: str,
        *,
        tenant_id: str,
        context_id: str,
        revision_id: str,
    ) -> str:
        """Promote a staged revision to the published pointer; return its key."""
        raise NotImplementedError

    def open_revision(self, revision_ref: str) -> "BundleView":
        """Open a published revision as a read-only, revision-pinned view."""
        raise NotImplementedError

    async def get_revision_files(self, header: Mapping[str, Any]) -> Dict[str, bytes]:
        """Read every file of a remote directory revision, keyed by path."""
        raise NotImplementedError

    async def published_revision_id(self, tenant_id: str, context_id: str) -> str:
        """The revision id currently published for this tenant/context (S7-B).

        Deliberately optional: the engine re-checks it immediately before
        publishing so a compile cannot overwrite a newer publication, and a
        store that cannot answer is counted (``publication_parent_recheck_
        unavailable``) instead of being assumed empty.
        """
        raise NotImplementedError

    # ── source-understanding checkpoints (S7.1) ───────────────────────
    # One immutable checkpoint per source understanding, keyed by the
    # understanding cache identity.  Deliberately optional (a store that never
    # runs layered compilation need not implement it) but never emulated by a
    # process-local dictionary: the engine treats ``NotImplementedError`` as
    # "this store cannot checkpoint", which makes layered compilation fail
    # loudly instead of pretending a restart could reuse understanding.

    async def put_checkpoint(self, namespace: str, cache_key: str, payload: bytes) -> str:
        """Atomically persist one checkpoint and return its storage key.

        The write must be all-or-nothing: a crash, cancellation or disk error
        leaves either the previous bytes or the new bytes, never a partial file
        that a later run could mistake for a complete understanding.
        """
        raise NotImplementedError

    async def get_checkpoint(self, namespace: str, cache_key: str) -> bytes:
        """Read one checkpoint's bytes.

        Raises:
            FileNotFoundError: if no checkpoint exists for the key.
            ValueError: if the stored bytes are not a complete checkpoint.
        """
        raise NotImplementedError


class BundleView(ABC):
    """One revision-pinned, read-only view over a directory Bundle.

    A view is created once at request start.  Its identity includes owner,
    revision and manifest checksum, so later reads cannot silently follow a
    newly published ``current`` pointer.
    """

    tenant_id: str
    context_id: str
    revision_id: str
    manifest_hash: str

    @abstractmethod
    def list_entries(self, prefix: str = "") -> List[str]:
        """List manifest entries under *prefix* without walking storage."""
        ...

    @abstractmethod
    def read_bytes(self, path: str) -> bytes:
        """Read and checksum-verify one artifact from this fixed revision."""
        ...

    def read_text(
        self,
        path: str,
        range: Tuple[int, int] | None = None,
    ) -> str:
        """Read UTF-8 text, optionally by half-open character offsets."""
        text = self.read_bytes(path).decode("utf-8")
        if range is None:
            return text
        start, end = range
        if start < 0 or end < start:
            raise ValueError("invalid text range")
        return text[start:end]

    @abstractmethod
    def open_sqlite(self, path: str):
        """Open one manifest-listed SQLite artifact in immutable/read-only mode."""
        ...

    @abstractmethod
    def resolve_evidence(self, ref: Mapping[str, Any]) -> Dict[str, Any]:
        """Resolve a version-bound text, page, table-cell, or PDF-region ref."""
        ...

class CancelPort(ABC):
    """Cooperative cancellation signal for compile/query tasks.

    Every method is synchronous so engine internals can poll it from inside
    tight model/loop boundaries without an event-loop round-trip.
    """

    @abstractmethod
    def raise_if_cancelled(self) -> None:
        """Raise ``asyncio.CancelledError`` if the owning task was cancelled."""
        ...

    @abstractmethod
    def is_cancelled(self) -> bool:
        """Return whether the owning task has been cancelled."""
        ...


class ProgressSink(ABC):
    """Report task progress from engine internals to the durable task row.

    Engines call this port at phase transitions and periodically within a
    phase so that the API and UI always reflect current state.
    """

    @abstractmethod
    async def update(
        self,
        task_id: str,
        phase: CurationPhase,
        progress_pct: int,
        message: str,
    ) -> None:
        """Persist the current phase / percentage / human-readable message.

        *progress_pct* must be in [0, 100].
        """
        ...


class Clock(ABC):
    """Abstract time source so tests can freeze or advance time deterministically."""

    @abstractmethod
    def now(self) -> datetime:
        """Return the current local datetime."""
        ...

    @abstractmethod
    def utcnow(self) -> datetime:
        """Return the current UTC datetime."""
        ...


class SystemClock(Clock):
    """Production clock that delegates to the real system time."""

    def now(self) -> datetime:
        return datetime.now()

    def utcnow(self) -> datetime:
        return datetime.utcnow()


__all__ = [
    "ArtifactStore",
    "BundleView",
    "CancelPort",
    "Clock",
    "ModelPort",
    "ProgressSink",
    "RevisionFileReference",
    "RevisionFileArtifact",
    "SourceArtifactPort",
    "SystemClock",
]
