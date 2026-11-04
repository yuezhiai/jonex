import asyncio
import json
import os
import re
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

KB_ID_RE_RAW = r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$"
# Checkpoint namespaces and cache keys become path segments; both are
# program-generated (never model- or caller-supplied text).
_SAFE_NAMESPACE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


class Layout:
    def __init__(self, data_root: str | Path):
        self.root = Path(data_root).expanduser()
        self._version_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._compile_locks: dict[tuple[str, str], asyncio.Lock] = {}

    def compile_lock(self, tenant_id: str, kb_id: str) -> asyncio.Lock:
        """Run at most one compilation pipeline for a KB at a time."""
        return self._compile_locks.setdefault((tenant_id, kb_id), asyncio.Lock())

    def version_lock(self, tenant_id: str, kb_id: str) -> asyncio.Lock:
        """Serialize publication, query pinning, rollback and retention per KB.

        The deployed service is single-instance; this lock protects its
        coroutine interleavings, including the query-commit/GC window.
        """
        return self._version_locks.setdefault((tenant_id, kb_id), asyncio.Lock())

    @asynccontextmanager
    async def publication_lock(self, tenant_id: str, kb_id: str, wait_resume):
        lock = self.version_lock(tenant_id, kb_id)
        while True:
            await wait_resume()
            await lock.acquire()
            if not self.compilation_paused(tenant_id, kb_id):
                break
            lock.release()
        try:
            yield
        finally:
            lock.release()

    def kb_dir(self, tenant_id: str, kb_id: str) -> Path:
        return self.root / tenant_id / kb_id

    def compilation_paused(self, tenant_id: str, kb_id: str) -> bool:
        return (self.kb_dir(tenant_id, kb_id) / "compile" / "PAUSED").is_file()

    def set_compilation_paused(self, tenant_id: str, kb_id: str, paused: bool) -> None:
        path = self.kb_dir(tenant_id, kb_id) / "compile" / "PAUSED"
        if paused:
            self.atomic_write(path, b"paused\n")
        else:
            path.unlink(missing_ok=True)

    def checkpoint_dir(self, tenant_id: str, kb_id: str, namespace: str) -> Path:
        """Per-KB checkpoint root (S7.1): tenant/kb are part of the path, so
        one KB can never read another's source understanding."""
        if not _SAFE_NAMESPACE.match(namespace or ""):
            raise ValueError(f"unsafe checkpoint namespace: {namespace!r}")
        return self.kb_dir(tenant_id, kb_id) / "checkpoints" / namespace

    def upload_path(self, tenant_id: str, kb_id: str, document_id: str, ext: str) -> Path:
        return self.kb_dir(tenant_id, kb_id) / "uploads" / document_id / f"{document_id}{ext}"

    def gen_dir(self, tenant_id: str, kb_id: str, document_id: str, generation: int) -> Path:
        return self.kb_dir(tenant_id, kb_id) / "parse" / document_id / f"gen-{generation}"

    def raw_dir(self, tenant_id: str, kb_id: str, document_id: str, generation: int) -> Path:
        """raw-only parse 落盘目录:原始采集结果(content_list.json / source.txt)+ manifest.json。

        归一化/打包/投影全部推迟到 compile 的 source_prep,parse 只负责「原样拿到」。
        """
        return self.gen_dir(tenant_id, kb_id, document_id, generation) / "raw"

    def artifacts_path(self, tenant_id: str, kb_id: str, document_id: str, generation: int) -> Path:
        return self.gen_dir(tenant_id, kb_id, document_id, generation) / "source_artifacts.jsonl"

    def gen_manifest_path(self, tenant_id: str, kb_id: str, document_id: str, generation: int) -> Path:
        """代次清单:这一代次是**怎么切出来的**。

        `source_artifacts.jsonl` 里虽然每行都带 `chunk_policy`/`merged_from`,但那是隐式的——
        要知道 `mineru:chunk:N` 与 `mineru:block:N` 的命名约定才看得出产物是否经过重组。
        清单把它写成显式声明,不必读几十行 JSONL 就能判断这一代次的形态。
        """
        return self.gen_dir(tenant_id, kb_id, document_id, generation) / "manifest.json"

    def staging_dir(self, tenant_id: str, kb_id: str, task_id: str) -> Path:
        return self.kb_dir(tenant_id, kb_id) / "compile" / "staging" / task_id

    def versions_dir(self, tenant_id: str, kb_id: str, bundle_sha: str) -> Path:
        return self.kb_dir(tenant_id, kb_id) / "compile" / "versions" / bundle_sha

    def published_symlink(self, tenant_id: str, kb_id: str) -> Path:
        return self.kb_dir(tenant_id, kb_id) / "compile" / "published"

    def resolve_published(self, tenant_id: str, kb_id: str) -> Path | None:
        link = self.published_symlink(tenant_id, kb_id)
        if not link.is_symlink():
            return None
        return Path(os.path.realpath(link))

    def atomic_write(self, path: Path, content: bytes) -> None:
        """临时文件 + os.replace 原子写(spec §3.2)。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(content)
            os.replace(tmp, path)
        except BaseException:
            os.unlink(tmp)
            raise

    def publish_bundle(self, tenant_id: str, kb_id: str, task_id: str, bundle_sha: str) -> None:
        """spec §5.5 步骤 4-5:staging→versions(os.replace)+ symlink 切换(单步原子)。"""
        compile_dir = self.kb_dir(tenant_id, kb_id) / "compile"
        versions = compile_dir / "versions"
        versions.mkdir(parents=True, exist_ok=True)
        target = versions / bundle_sha
        if not target.exists():
            os.replace(self.staging_dir(tenant_id, kb_id, task_id), target)
        self.activate_version(tenant_id, kb_id, bundle_sha)

    def activate_version(self, tenant_id: str, kb_id: str, bundle_sha: str) -> None:
        """Atomically select an existing immutable bundle for new readers."""
        compile_dir = self.kb_dir(tenant_id, kb_id) / "compile"
        target = self.versions_dir(tenant_id, kb_id, bundle_sha)
        if not target.is_dir():
            raise FileNotFoundError(target)
        link = self.published_symlink(tenant_id, kb_id)
        tmp_link = compile_dir / "published.tmp"
        if tmp_link.exists() or tmp_link.is_symlink():
            os.unlink(tmp_link)
        os.symlink(f"versions/{bundle_sha}", tmp_link)  # 相对路径目标
        os.replace(tmp_link, link)
        os.utime(target, None)

    def prune_versions(self, tenant_id: str, kb_id: str, keep_history: int,
                       protected_refs: set[str] | None = None) -> list[str]:
        """Keep the active version, newest history, and pinned query versions."""
        versions = self.kb_dir(tenant_id, kb_id) / "compile" / "versions"
        if not versions.is_dir():
            return []
        active = self.resolve_published(tenant_id, kb_id)
        if active is None or not active.is_dir() or active.parent != versions:
            return []  # an invalid pointer must never authorize deletion
        history = sorted((path for path in versions.iterdir()
                          if path.is_dir() and path != active),
                         key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True)
        # A corrupt reference ledger cannot authorize deletion. Scenario jobs,
        # retained business selections and workbench runs share this ledger.
        try:
            durable_pins = self.version_pins(tenant_id, kb_id)
        except (ValueError, OSError):
            return []
        keep = {active.name, *(protected_refs or set()), *durable_pins.values()}
        keep.update(path.name for path in history[:max(0, keep_history)])
        removed = []
        for path in history:
            if path.name not in keep:
                shutil.rmtree(path)
                removed.append(path.name)
        return removed

    def version_pins(self, tenant_id: str, kb_id: str) -> dict[str, str]:
        path = self.kb_dir(tenant_id, kb_id) / "compile" / "retention.json"
        if not path.is_file():
            return {}
        value = json.loads(path.read_text())
        if (not isinstance(value,dict) or value.get("schema") != "wiki.retention.v1" or not isinstance(value.get("pins"), dict)
                or any(not isinstance(owner, str) or not isinstance(sha,str) or not re.fullmatch(r"[0-9a-f]{64}", sha)
                       for owner, sha in value["pins"].items())):
            raise ValueError("VERSION_PINS_INVALID")
        return value["pins"]

    def set_version_pin(self, tenant_id: str, kb_id: str, owner: str, revision: str | None) -> None:
        """Caller holds version_lock. Releasing a pin never deletes an artifact."""
        if not owner or len(owner) > 240:
            raise ValueError("VERSION_PIN_OWNER_INVALID")
        pins = self.version_pins(tenant_id, kb_id)
        if revision is None:
            pins.pop(owner, None)
        else:
            if not isinstance(revision,str) or not re.fullmatch(r"[0-9a-f]{64}", revision) or not self.versions_dir(tenant_id, kb_id, revision).is_dir():
                raise ValueError("VERSION_PIN_REVISION_INVALID")
            pins[owner] = revision
        self.atomic_write(self.kb_dir(tenant_id, kb_id) / "compile" / "retention.json",
                          json.dumps({"schema": "wiki.retention.v1", "pins": pins}, sort_keys=True).encode())
