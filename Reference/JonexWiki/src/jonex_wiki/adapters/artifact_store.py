"""ArtifactStore 适配器：engine ArtifactStore → dev Layout 目录布局。

引擎产物内容换成 core（revision.json/manifest.json/provenance.json/wiki/*/query/*/
sources/*/indexes/{lexical.sqlite}），发布/读取走 dev Layout（staging →
versions/{bundle_sha} + published symlink）。这里完成三件 dev 独有的事：

1. ``revision.json`` 增补 dev 字段（kb_id/page_count/claim_count/doc_fingerprints/
   fingerprints/prompt_shas），core 字段（compiler_state/digest_map/source_digests）
   原样保留供 ``decode_previous_files`` 增量还原；
2. ``indexes/manifest.json`` 用 dev BundleManifest（bundle_sha/files/pages/segmenter）
   覆盖，并把 core 的 index_manifest（lexical 身份）合并进去，两套读者都能读；
3. bundle_sha = 除 indexes/manifest.json 外全部文件的聚合哈希（dev 口径）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Tuple

from jonex_wiki.adapters.bundle_view import DirectoryBundleView, read_revision_header, remember_verified_file
from jonex_wiki.kernel.ports import ArtifactStore, BundleView, RevisionFileReference,RevisionFileArtifact
from jonex_wiki.storage.layout import Layout

_MANIFEST_EXCLUDED = "indexes/manifest.json"
_REVISION_SCHEMA = "jonex_wiki.bundle_revision.v1"
# Cache keys are sha256 hex digests produced by the engine, never user text.
_CACHE_KEY_RE = re.compile(r"^[0-9a-f]{64}$")


def _bundle_sha_of(staging: Path) -> str:
    """dev 口径：除 indexes/manifest.json 外全部文件（相对路径排序）的聚合哈希。"""
    h = hashlib.sha256()
    rels = sorted(
        p.relative_to(staging).as_posix()
        for p in staging.rglob("*")
        if p.is_file() and p.relative_to(staging).as_posix() != _MANIFEST_EXCLUDED
    )
    for rel in rels:
        h.update(rel.encode())
        h.update(b"\0")
        with (staging / rel).open("rb") as stream:
            h.update(hashlib.file_digest(stream, "sha256").digest())
    return h.hexdigest()


def _bundle_sha_from_files(files: Mapping[str, str]) -> str:
    digest = hashlib.sha256()
    for path, checksum in sorted(files.items()):
        digest.update(path.encode())
        digest.update(b"\0")
        digest.update(bytes.fromhex(checksum))
    return digest.hexdigest()


class ArtifactStoreAdapter(ArtifactStore):
    supports_file_references = True
    supports_json_shards = True
    def __init__(self, layout: Layout, *, tenant_id: str, kb_id: str) -> None:
        self._layout = layout
        self._tenant_id = tenant_id
        self._kb_id = kb_id
        # One operation's pinned view, not a cache of decoded compiler objects.
        self._last_view: DirectoryBundleView | None = None

    # ── legacy blob hooks（dev 目录布局不用，保留显式 NotImplementedError）──

    async def put_bundle(self, context_id: str, revision_id: str, bundle_bytes: bytes) -> str:
        raise NotImplementedError("dev layout stores directory revisions, not blob bundles")

    async def get_bundle(self, key: str) -> bytes:
        raise NotImplementedError("dev layout stores directory revisions, not blob bundles")

    # ── write path ─────────────────────────────────────────────────────

    def private_index_directory(self):
        directory=self._layout.kb_dir(self._tenant_id,self._kb_id)/'compile'/'.index-work'
        directory.mkdir(parents=True,exist_ok=True)
        return directory

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
        return await asyncio.to_thread(
            self._stage_revision, tenant_id, context_id, revision_id, files,
            attempt_id, parent_revision, fingerprints,
        )

    def _stage_revision(self, tenant_id, context_id, revision_id, files,
                        attempt_id, parent_revision, fingerprints) -> str:
        staging_key = attempt_id or revision_id
        staging = self._layout.staging_dir(tenant_id, context_id, staging_key)
        # A restarted compile has already decoded its completed prior stage.
        # Replace the private tree so removed files cannot survive the delta.
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True, exist_ok=True)

        parent = self._layout.resolve_published(tenant_id, context_id)
        old_files = {}
        if parent is not None:
            try:
                old_files = json.loads((parent / _MANIFEST_EXCLUDED).read_text("utf-8")).get("files", {})
            except (OSError, ValueError):
                pass  # A damaged old manifest cannot prevent a fresh publication.
        file_digests = {}
        verified_parent = self._last_view
        if verified_parent is not None and verified_parent._dir != parent:
            verified_parent = None
        directories = {staging}
        def ensure_directory(directory):
            if directory not in directories:
                directory.mkdir(parents=True, exist_ok=True)
                directories.add(directory)
        def checked_parent_file(path, checksum, old_path):
            stat = old_path.stat()
            if verified_parent is not None and verified_parent.has_verified_file(path, checksum, stat):
                count_work("bundle_file_checks_reused")
                return True, stat.st_size
            with old_path.open("rb") as stream:
                valid = hashlib.file_digest(stream, "sha256").hexdigest() == checksum
            count_work("bundle_file_checks_read")
            return valid, stat.st_size
        from jonex_wiki.kernel.telemetry import count_work

        core_index_manifest: bytes | None = None
        for path, data in (files.items() if isinstance(files, Mapping) else files):
            if path == _MANIFEST_EXCLUDED:
                core_index_manifest = data
                continue
            target = staging / path
            ensure_directory(target.parent)
            if isinstance(data,RevisionFileArtifact):
                if data.path!=path:raise ValueError('private artifact path mismatch')
                source=Path(data.source_file);stat=source.stat()
                identity=(stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)
                if identity!=data.verified_identity:
                    with source.open('rb') as stream:actual=hashlib.file_digest(stream,'sha256').hexdigest()
                    after=source.stat()
                    if actual!=data.sha256 or (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns)!=identity:
                        raise ValueError('private artifact checksum mismatch')
                try:os.replace(source,target)
                except OSError:
                    shutil.copyfile(source,target);data.cleanup()
                file_digests[path]=data.sha256
                remember_verified_file(DirectoryBundleView._file_identity(target.stat()),data.sha256)
                count_work('bundle_files_moved_private');count_work('bundle_bytes_written',stat.st_size)
                continue
            if isinstance(data, RevisionFileReference):
                # References are offered only for byte-identical rendered files.
                # Pin the parent and verify disk bytes before sharing an inode.
                if data.path != path or parent is None or old_files.get(path) != data.sha256:
                    raise ValueError("immutable file reference does not match parent: " + path)
                old_path = parent / path
                valid, size = checked_parent_file(path, data.sha256, old_path)
                if not valid:
                    raise ValueError("bundle artifact checksum mismatch: " + path)
                try:
                    os.link(old_path, target)
                    count_work("bundle_files_reused")
                    count_work("bundle_files_reused_early")
                    count_work("bundle_bytes_reused", size)
                except OSError:
                    content = data.read_bytes()
                    if hashlib.sha256(content).hexdigest() != data.sha256:
                        raise ValueError("bundle artifact checksum mismatch: " + path)
                    target.write_bytes(content)
                    count_work("bundle_files_written")
                    count_work("bundle_bytes_written", len(content))
                file_digests[path] = data.sha256
                remember_verified_file(DirectoryBundleView._file_identity(target.stat()),data.sha256)
                continue
            checksum = hashlib.sha256(data).hexdigest()
            file_digests[path] = checksum
            reused = False
            if path != "revision.json" and old_files.get(path) == checksum:
                old_path = parent / path
                # Validate the actual bytes before sharing an immutable inode;
                # a damaged old publication must never taint a new version.
                if old_path.is_file():
                    valid, _ = checked_parent_file(path, checksum, old_path)
                    if valid:
                        try:
                            os.link(old_path, target)
                            reused = True
                        except OSError:
                            pass  # Cross-filesystem stores keep the ordinary write.
            if reused:
                count_work("bundle_files_reused")
                count_work("bundle_bytes_reused", len(data))
            else:
                target.write_bytes(data)
                count_work("bundle_files_written")
                count_work("bundle_bytes_written", len(data))
            remember_verified_file(DirectoryBundleView._file_identity(target.stat()),checksum)
            del data

        from jonex_wiki.kernel.telemetry import current_runtime_telemetry, telemetry_stage
        with telemetry_stage(current_runtime_telemetry(), "bundle_revision_enrich"):
            self._enrich_revision(
                staging, tenant_id, context_id, parent_revision, fingerprints
            )
        # bundle_sha 必须在 indexes/manifest.json 写入前计算（它排除自身）。
        file_digests["revision.json"] = hashlib.sha256((staging / "revision.json").read_bytes()).hexdigest()
        sha = _bundle_sha_from_files(file_digests)
        with telemetry_stage(current_runtime_telemetry(), "bundle_manifest_write"):
            self._write_index_manifest(staging, core_index_manifest, sha, file_digests=file_digests)
        return staging_key

    async def publish_revision(
        self,
        staging_key: str,
        *,
        tenant_id: str,
        context_id: str,
        revision_id: str,
    ) -> str:
        staging = self._layout.staging_dir(tenant_id, context_id, staging_key)
        manifest = json.loads((staging / _MANIFEST_EXCLUDED).read_text("utf-8"))
        sha = manifest["bundle_sha"]
        existing = self._layout.versions_dir(tenant_id, context_id, sha)
        if existing.is_dir():
            # A rebuilt candidate can have the same content address as a
            # damaged publication. Never report success by reactivating the
            # damaged directory while silently discarding the healthy stage.
            DirectoryBundleView(tenant_id=tenant_id, context_id=context_id,
                                revision_dir=existing).verify_integrity()
        await asyncio.to_thread(
            self._layout.publish_bundle, tenant_id, context_id, staging_key, sha
        )
        return sha

    # ── read path ──────────────────────────────────────────────────────

    def open_revision(self, revision_ref: str) -> BundleView:
        d = self._layout.versions_dir(self._tenant_id, self._kb_id, revision_ref)
        if not d.is_dir():
            d = self._layout.staging_dir(self._tenant_id, self._kb_id, revision_ref)
        view = DirectoryBundleView(
            tenant_id=self._tenant_id, context_id=self._kb_id, revision_dir=d
        )
        self._last_view = view
        return view

    async def published_revision_id(self, tenant_id: str, context_id: str) -> str:
        """The revision id the published pointer resolves to (S7-B).

        Read from the published revision's own ``revision.json``: the directory
        name is the bundle content address, not the revision identity, so the
        parent recheck has to compare the identity the engine published.
        """
        published = await asyncio.to_thread(
            self._layout.resolve_published, tenant_id, context_id)
        if published is None:
            return ""
        revision = await asyncio.to_thread(read_revision_header, published)
        return str(revision.get("revision_id") or "")

    async def get_revision_files(self, header: Mapping[str, Any]) -> Dict[str, bytes]:
        revision_id = str(header.get("revision_id") or header.get("bundle_sha"))
        d = self._layout.versions_dir(self._tenant_id, self._kb_id, revision_id)
        return {
            p.relative_to(d).as_posix(): p.read_bytes()
            for p in d.rglob("*")
            if p.is_file()
        }

    # ── source-understanding checkpoints (S7.1) ────────────────────────
    # One small JSON document per source understanding, under this KB's own
    # directory, so tenant/kb isolation is structural rather than a convention.
    # The write is atomic: a temp file in the same directory is fsynced and
    # then renamed, so a crash or cancellation can leave the old bytes or the
    # new bytes but never a half-written checkpoint.

    def _checkpoint_path(self, namespace: str, cache_key: str) -> Path:
        if not _CACHE_KEY_RE.match(cache_key or ""):
            raise ValueError(f"unsafe checkpoint cache key: {cache_key!r}")
        directory = self._layout.checkpoint_dir(self._tenant_id, self._kb_id, namespace)
        return directory / f"{cache_key}.json"

    async def put_checkpoint(self, namespace: str, cache_key: str, payload: bytes) -> str:
        import os
        import tempfile

        path = self._checkpoint_path(namespace, cache_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return path.as_posix()

    async def get_checkpoint(self, namespace: str, cache_key: str) -> bytes:
        path = self._checkpoint_path(namespace, cache_key)
        data = path.read_bytes()
        # A truncated or half-written file must never look like a complete
        # understanding: JSON validity is the completeness check here.
        json.loads(data.decode("utf-8"))
        return data

    # ── internals ──────────────────────────────────────────────────────

    def _enrich_revision(
        self,
        staging: Path,
        tenant_id: str,
        context_id: str,
        parent_revision: str | None,
        fingerprints: Mapping[str, str] | None,
    ) -> None:
        rev_path = staging / "revision.json"
        revision = json.loads(rev_path.read_text("utf-8"))

        manifest_payload: Dict[str, Any] = {}
        mpath = staging / "manifest.json"
        if mpath.is_file():
            manifest_payload = json.loads(mpath.read_text("utf-8"))
        counts = manifest_payload.get("counts") or {}
        input_manifest = manifest_payload.get("manifest") or {}

        revision.setdefault("tenant_id", tenant_id)
        revision.setdefault("context_id", context_id)
        revision["schema"] = _REVISION_SCHEMA
        revision["kb_id"] = context_id
        revision["page_count"] = int(counts.get("pages", 0))
        revision["claim_count"] = int(counts.get("claims", 0))
        revision["source_count"] = int(counts.get("sources", 0))
        if fingerprints:
            revision["fingerprints"] = dict(fingerprints)
        if revision.get("digest_map"):
            revision["doc_fingerprints"] = dict(revision["digest_map"])
        prompt_shas = input_manifest.get("prompt_shas")
        if prompt_shas:
            revision["prompt_shas"] = dict(prompt_shas)
        elif revision.get("prompt_checksum"):
            revision["prompt_shas"] = {"default": revision["prompt_checksum"]}
        if parent_revision:
            revision["parent_revision"] = parent_revision

        with rev_path.open("w", encoding="utf-8") as stream:
            # A compact dump uses the C encoder and one write, rather than
            # millions of pretty-print fragments for large compiler histories.
            stream.write(json.dumps(revision, ensure_ascii=False, separators=(",", ":")))

    def _write_index_manifest(
        self, staging: Path, core_index_manifest: bytes | None, sha: str,
        *, file_digests: Mapping[str, str] | None = None,
    ) -> None:
        idx_dir = staging / "indexes"
        idx_dir.mkdir(parents=True, exist_ok=True)

        merged: Dict[str, Any] = {}
        if core_index_manifest:
            try:
                merged.update(json.loads(core_index_manifest.decode("utf-8")))
            except (ValueError, UnicodeDecodeError):
                merged = {}

        files = dict(file_digests) if file_digests is not None else {}
        if file_digests is None:
            for p in sorted(staging.rglob("*")):
                if p.is_file() and p.relative_to(staging).as_posix() != _MANIFEST_EXCLUDED:
                    with p.open("rb") as stream:
                        files[p.relative_to(staging).as_posix()] = hashlib.file_digest(stream, "sha256").hexdigest()
        import jieba

        merged.update({
            "bundle_sha": sha,
            "files": files,
            "pages": sorted(
                path[len("wiki/"):-3] for path in files
                if path.startswith("wiki/") and path.endswith(".md")
            ),
            "segmenter": {"lib": "jieba", "version": jieba.__version__},
        })
        (idx_dir / "manifest.json").write_text(
            json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
        )
