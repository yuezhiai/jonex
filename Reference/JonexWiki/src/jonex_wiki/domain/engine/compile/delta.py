"""One exact archive comparison shared by validation and source projection."""
from collections import defaultdict
from dataclasses import dataclass


@dataclass(frozen=True)
class CompileDelta:
    unchanged_documents: frozenset[str]
    changed_sources: frozenset[str]
    removed_sources: frozenset[str]

    @classmethod
    def between(cls, current, previous=None, versions=None, old_versions=None):
        old, new = defaultdict(dict), defaultdict(dict)
        if previous is not None:
            for pid, passage in previous.source_archive.items():
                old[passage.document_id][pid] = passage
        for pid, passage in current.source_archive.items():
            new[passage.document_id][pid] = passage
        versions, old_versions = versions or {}, old_versions or {}
        return cls(frozenset(doc for doc, rows in new.items() if rows == old.get(doc)),
                   frozenset(s for s in versions if versions[s] != old_versions.get(s)),
                   frozenset(set(old_versions) - set(versions)))
