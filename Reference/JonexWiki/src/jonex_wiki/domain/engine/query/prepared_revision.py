"""Bounded, tenant-bound cache of verified immutable publication snapshots."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, is_dataclass
import sys
import json
from threading import RLock

from pydantic import BaseModel

from jonex_wiki.kernel.runtime_env import current_env
from jonex_wiki.kernel.telemetry import count_work
from jonex_wiki.domain.engine.compile.bundle import sha256_hex
from jonex_wiki.domain.engine.compile.publication import RULE_VERSION


@dataclass(frozen=True)
class PreparedRevision:
    bundle_sha256: str
    previous: object


def _retained_size(root: object) -> int:
    """Count reachable Python objects once, including shared corpus references."""
    seen, pending, size = set(), [root], 0
    from jonex_wiki.domain.engine.json_artifacts import PartitionedMapping
    from jonex_wiki.domain.engine.source_index import SourceLocations
    while pending:
        value = pending.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        size += sys.getsizeof(value)
        if isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, (list, tuple, set, frozenset)):
            pending.extend(value)
        elif isinstance(value, BaseModel):
            pending.extend((value.__dict__, value.__pydantic_fields_set__,
                            value.__pydantic_extra__, value.__pydantic_private__))
        elif isinstance(value, (PartitionedMapping,SourceLocations)):
            pending.append(vars(value))
            if isinstance(value,PartitionedMapping):size+=value.cache_budget*8
        elif hasattr(value, "_manifest_raw"):
            pending.append(vars(value))
        elif is_dataclass(value) and not isinstance(value, type):
            pending.append(vars(value))
    return size


class PreparedRevisionCache:
    """No live SQLite connections; each query owns and closes its connection.

    Weight conservatively includes decoded Python objects and original index
    bytes. Oversized revisions are verified but not retained.
    """
    def __init__(self, max_entries=1, max_bytes=768 * 1024 * 1024):
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._entries = OrderedDict()
        self._bytes = 0
        self._lock = RLock()

    def find(self, tenant_id, context_id, revision_id, checksum):
        """Use a trusted immutable Revision checksum without refetching bytes."""
        key = (tenant_id, context_id, revision_id, checksum, RULE_VERSION)
        with self._lock:
            cached = self._entries.get(key)
            if cached:
                self._entries.move_to_end(key)
                count_work("prepared_revision_hit")
                return cached[0]
        return None

    def load(self, data: bytes, tenant_id: str, context_id: str, revision_id=None):
        from jonex_wiki.domain.engine.compile.bundle_codec import decode_previous_bundle
        checksum = sha256_hex(data)
        key = (tenant_id, context_id, revision_id, checksum, RULE_VERSION)
        with self._lock:
            cached = self._entries.get(key)
            if cached:
                self._entries.move_to_end(key)
                count_work("prepared_revision_hit")
                return cached[0]
            count_work("prepared_revision_miss")
            previous = decode_previous_bundle(data)
            if previous.tenant_id != tenant_id or previous.context_id != context_id:
                raise ValueError("bundle owner does not match the published Revision")
            if revision_id is not None and previous.revision_id != revision_id:
                raise ValueError("bundle revision does not match the published Revision")
            prepared = PreparedRevision(checksum, previous)
            weight = len(data) * 4
            if self.max_entries > 0 and weight <= self.max_bytes:
                while self._entries and (len(self._entries) >= self.max_entries or self._bytes + weight > self.max_bytes):
                    _, (_, removed_weight) = self._entries.popitem(last=False)
                    self._bytes -= removed_weight
                self._entries[key] = (prepared, weight)
                self._bytes += weight
            else:
                count_work("prepared_revision_not_retained")
            return prepared

    def load_view(self, view, tenant_id: str, context_id: str, revision_id=None):
        """Verify/cache one pinned directory view without a giant JSON projection."""
        from jonex_wiki.domain.engine.compile.bundle_codec import decode_previous_files
        from jonex_wiki.adapters.bundle_view import DirectoryBundleView
        from jonex_wiki.domain.engine.compile.error_book import ErrorBook
        from jonex_wiki.domain.engine.models.compiler_state import CompilerState
        checksum = view.manifest_hash
        key = (tenant_id, context_id, revision_id, checksum, RULE_VERSION)
        with self._lock:
            cached = self._entries.get(key)
            if cached:
                self._entries.move_to_end(key)
                count_work("prepared_revision_hit")
                return cached[0]
        from jonex_wiki.domain.engine.query.published_projection import load_query_projection
        from jonex_wiki.domain.engine.json_artifacts import load_json_artifact
        navigation = (load_json_artifact(view.read_bytes,'query/navigation.json',
                        lazy_fields={'page_graph','page_links','fact_mentions','page_source_ids'})
                      if isinstance(view, DirectoryBundleView)
                      and "query/navigation.json" in view.list_entries() else {})
        files = {}
        if navigation.get("query_projection_version") == 1:
            previous = load_query_projection(view, navigation)
        else:
            # Query preparation needs compiler/query/index metadata, not original
            # attachments or source-level table databases. Table tools keep the
            # pinned BundleView and open the selected SQLite file on demand.
            paths = [path for path in view.list_entries()
                     if not (path.startswith("sources/") and (
                         "/original." in path or path.endswith("/tables.sqlite")))]
            files = {path: view.read_bytes(path) for path in paths}
            previous = decode_previous_files(files, revision_id=view.revision_id)
        if previous.tenant_id != tenant_id or previous.context_id != context_id:
            raise ValueError("bundle owner does not match the published Revision")
        if revision_id is not None and previous.revision_id != revision_id:
            raise ValueError("bundle revision does not match the published Revision")
        if isinstance(view, DirectoryBundleView) and previous.corpus is not None:
            # The verified corpus already contains query evidence. Compilation
            # never consumes this cache; its state and duplicate index are cold.
            previous.state = CompilerState()
            previous.claims = []
            previous.error_book = ErrorBook()
            previous.reading_index = None
            previous.render_signatures = None
            previous.lexical_index = None
        prepared = PreparedRevision(checksum, previous)
        weight = _retained_size(prepared) if isinstance(view, DirectoryBundleView) else sum(
            len(value) * (1 if path == "indexes/lexical.sqlite" else 3)
            for path, value in files.items()
        )
        with self._lock:
            count_work("prepared_revision_miss")
            if self.max_entries > 0 and weight <= self.max_bytes:
                while self._entries and (len(self._entries) >= self.max_entries or self._bytes + weight > self.max_bytes):
                    _, (_, removed_weight) = self._entries.popitem(last=False)
                    self._bytes -= removed_weight
                self._entries[key] = (prepared, weight)
                self._bytes += weight
            else:
                count_work("prepared_revision_not_retained")
        return prepared

    def clear(self):
        with self._lock:
            self._entries.clear()
            self._bytes = 0


def _default_cache_bytes() -> int:
    raw = current_env().get("JONEX_WIKI_PREPARED_CACHE_BYTES", str(768 * 1024 * 1024))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 768 * 1024 * 1024
    return max(0, value)


# One verified PoC revision expands to roughly four times its compressed Bundle
# size.  The former 128 MiB default could not retain a 165.8 MiB Bundle and paid
# the full decode/validation cost on every query process.
DEFAULT_PREPARED_REVISIONS = PreparedRevisionCache(
    max_entries=1,
    max_bytes=_default_cache_bytes(),
)
