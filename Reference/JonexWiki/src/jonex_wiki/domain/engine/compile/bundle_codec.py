"""Immutable Wiki bundle codec (lwp-arch-001 PROD).

Extracted verbatim from ``engines/llm_wiki_plus/production.py``: source
removal/slugs, source snapshots, the bundle file builder
(``build_bundle_files`` / ``build_empty_bundle_files``) and the fail-closed
previous-bundle decoder (``PreviousBundle`` / ``decode_previous_bundle``).
Pure: reads/writes only in-memory bytes; no network, clock, database or
object storage.  The old import path stays available through the re-export
shim.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from collections.abc import Mapping

from jonex_wiki.kernel.contracts import (
    EffectiveRuntimeSnapshot,
    SourceArtifact,
    SourceInventorySnapshot,
)
from jonex_wiki.kernel.ports import RevisionFileReference,RevisionFileArtifact
from jonex_wiki.domain.engine.json_artifacts import iter_json_artifact, load_json_artifact
from jonex_wiki.domain.engine.source_index import compact_source_index, restore_source_index
from jonex_wiki.domain.engine.compile.pipeline import (
    ENGINE_ID,
    ENGINE_VERSION,
    SCHEMA_VERSION,
    _prompt_checksum,
    _tool_checksum,
)
from jonex_wiki.domain.engine.models.compiler_state import (
    ArchivedPassage,
    CompilerState,
)
from jonex_wiki.domain.engine.models.claim import SUPPORT_RULE_VERSION
from jonex_wiki.domain.engine.compile.content_validation import ValidatedClaim
from jonex_wiki.domain.engine.compile.digest import classify_changes
from jonex_wiki.domain.engine.compile.bundle import DecodedBundle, decode_bundle, sha256_hex
from jonex_wiki.domain.engine.compile.error_book import ErrorBook
from jonex_wiki.domain.engine.compile.rendering import render_wiki_tree
from jonex_wiki.domain.engine.compile import bundle
from jonex_wiki.domain.engine.compile.pipeline import (
    build_claim_audit,
    canonical_json,
    derive_passage_id,
    enforce_claim_policy,
    resolve_claim_policy,
)
from jonex_wiki.domain.engine.query.retrieval import (
    build_fts_index_bytes,
    lexical_preprocessing_identity,
    build_query_corpus,
)

logger = logging.getLogger(__name__)


def _canonical_fragments(value, depth=0):
    """Bound Unicode intermediates by records, using the fast JSON encoder."""
    if (isinstance(value, Mapping) and depth < 2
            and all(isinstance(key, str) for key in value)):
        yield "{"
        for index, key in enumerate(sorted(value)):
            if index:
                yield ","
            yield json.dumps(key, ensure_ascii=False) + ":"
            yield from _canonical_fragments(value[key], depth + 1)
        yield "}"
    elif isinstance(value, (list, tuple)) and depth < 2:
        yield "["
        for index, item in enumerate(value):
            if index:
                yield ","
            yield from _canonical_fragments(item, depth + 1)
        yield "]"
    else:
        yield json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=lambda item: dict(item)
                         if isinstance(item, Mapping) else item.model_dump())


def _corpus_sha256(corpus, **kwargs) -> str:
    from jonex_wiki.kernel.telemetry import current_runtime_telemetry, telemetry_stage
    with telemetry_stage(current_runtime_telemetry(), "corpus_snapshot_hash"):
        return _corpus_sha256_impl(corpus, **kwargs)


def canonical_checksum(material):
    """The original canonical SHA256 without a second whole model dump."""
    digest = hashlib.sha256()
    for fragment in _canonical_fragments(material):
        digest.update(fragment.encode('utf-8'))
    return digest.hexdigest()


def _corpus_sha256_impl(corpus, *, legacy: bool = False, derived_placeholders: tuple = (), retired_vector_placeholder: bool = False) -> str:
    """Stream the unchanged canonical snapshot without materializing its JSON."""
    material = {
        key: value for key, value in corpus
        if not type(corpus).model_fields[key].exclude
        and not (legacy and key == "revision_id")
    }
    material.update({key: {} for key in derived_placeholders})
    if retired_vector_placeholder:
        material["page_vectors"] = None
    digest = hashlib.sha256()
    # Repeated source intervals share a passage's metadata. Encode that object
    # once while retaining the exact original canonical byte sequence/hash.
    locations = material.get('source_locations', {})
    from collections import Counter
    counts = Counter(id(loc.get('metadata')) for loc in locations.values())
    encoded_metadata = {}
    digest.update(b'{')
    for index, key in enumerate(sorted(material)):
        if index:digest.update(b',')
        digest.update((json.dumps(key, ensure_ascii=False)+':').encode())
        if key != 'source_locations':
            for fragment in _canonical_fragments(material[key], 1):digest.update(fragment.encode())
            continue
        digest.update(b'{')
        for n, path in enumerate(sorted(locations)):
            if n:digest.update(b',')
            digest.update((json.dumps(path,ensure_ascii=False)+':{').encode())
            location = locations[path]
            for k, name in enumerate(sorted(location)):
                if k:digest.update(b',')
                digest.update((json.dumps(name,ensure_ascii=False)+':').encode())
                value = location[name]
                if name == 'metadata' and counts[id(value)] > 1:
                    if id(value) not in encoded_metadata:encoded_metadata[id(value)] = _canonical_bytes(value)
                    digest.update(encoded_metadata[id(value)])
                else:digest.update(_canonical_bytes(value))
            digest.update(b'}')
        digest.update(b'}')
    digest.update(b'}')
    return digest.hexdigest()


def remove_sources(
    state: CompilerState,
    claims: List[ValidatedClaim],
    source_ids: List[str],
) -> None:
    """Prune deleted/changed sources and anything that depends on them.

    Mutates ``state`` and ``claims`` in place.  Removed passages are dropped
    from the archive and processed list; every claim whose evidence references
    a removed passage is dropped; every page whose ``source_ids`` are a subset
    of the removed passages (i.e. depends exclusively on deleted evidence) is
    dropped together with its index entry.
    """
    if not source_ids:
        return
    source_set = set(source_ids)
    deleted_passage_ids = {
        pid
        for pid, passage in state.source_archive.items()
        if passage.source_id in source_set
    }
    if not deleted_passage_ids:
        return

    for pid in deleted_passage_ids:
        state.source_archive.pop(pid, None)
        if pid in state.processed_passage_ids:
            state.processed_passage_ids.remove(pid)

    removed_pages = []
    for path, snapshot in state.pages.items():
        if snapshot.source_ids and set(snapshot.source_ids).issubset(
            deleted_passage_ids
        ):
            removed_pages.append(path)
    for path in removed_pages:
        state.pages.pop(path, None)
        state.index.pop(path, None)
    removed_pages_set = set(removed_pages)

    retained: List[ValidatedClaim] = []
    for claim in claims:
        if claim.page_path in removed_pages_set:
            continue
        if any(ref.passage_id in deleted_passage_ids for ref in claim.evidence):
            continue
        retained.append(claim)
    claims[:] = retained


# ──────────────────────────────────────────────
#  Source snapshots and digests
# ──────────────────────────────────────────────


def safe_slug(source_id: str) -> str:
    """Return a filesystem-safe slug for a source id."""
    slug = "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in source_id)
    slug = slug.strip("._")
    return slug or sha256_hex(source_id.encode("utf-8"))[:16]


def _finalize_reading_materials(materials: Any, slug: str) -> Any:
    """Fill the bundle-relative content paths into a transport-agnostic catalog."""
    if not isinstance(materials, dict):
        return materials
    finalized = copy.deepcopy(materials)
    article_path = "sources/articles/{}.md".format(slug)
    tables_path = "sources/{}/tables.sqlite".format(slug)
    for unit in finalized.get("units") or []:
        if not isinstance(unit, dict):
            continue
        location = unit.get("content_location")
        if not isinstance(location, dict):
            location = {}
            unit["content_location"] = location
        if unit.get("kind") == "document" or unit.get("content_kind") == "article":
            location["article_path"] = article_path
        else:
            location["tables_path"] = tables_path
    return finalized


def build_source_snapshots(
    artifacts: List[SourceArtifact],
) -> Dict[str, Dict[str, Any]]:
    """Group artifacts by source id into snapshots with concatenated text."""
    by_source: Dict[str, List[SourceArtifact]] = {}
    for artifact in artifacts:
        by_source.setdefault(artifact.source_id, []).append(artifact)

    snapshots: Dict[str, Dict[str, Any]] = {}
    for source_id, items in by_source.items():
        ordered = sorted(
            items, key=lambda a: (a.position if a.position is not None else 0)
        )
        reading_materials = None
        for item in ordered:
            candidate = (item.source_metadata or {}).get("source_reading_materials")
            if candidate is not None:
                reading_materials = candidate
                break
        chunks = []
        for item in ordered:
            chunk_metadata = dict(item.source_metadata or {})
            chunk_metadata.pop("source_reading_materials", None)
            chunks.append({
                "chunk_id": item.chunk_id,
                "position": item.position,
                "page_number": item.page_number,
                "metadata": chunk_metadata,
            })
        base_metadata = dict(ordered[0].source_metadata or {})
        base_metadata.pop("source_reading_materials", None)
        snapshots[source_id] = {
            "source_id": source_id,
            "document_id": ordered[0].document_id,
            "content_sha256": ordered[0].sha256,
            "passage_ids": [derive_passage_id(a) for a in ordered],
            "parser_id": ordered[0].parser_id,
            "content_generation": ordered[0].content_generation,
            "source_metadata": {
                **base_metadata,
                "chunk_count": len(ordered),
                "chunks": chunks,
            },
            "text": "".join(a.source_metadata.get("snapshot_text", a.text) for a in ordered),
            "reading_materials": reading_materials,
        }
    return snapshots


def _registered_table_ids(path: Path) -> List[str]:
    """Return the sorted logical table ids stored in one source grid."""
    import sqlite3

    connection = sqlite3.connect("file:{}?mode=ro".format(path.as_posix()), uri=True)
    try:
        rows = connection.execute("SELECT table_id FROM logical_tables").fetchall()
    finally:
        connection.close()
    return sorted(str(row[0]) for row in rows)


def _validate_skipped_table_claims(
    artifacts: List[SourceArtifact],
    table_ids_by_source: Dict[str, List[str]],
) -> None:
    """Fail the publication when a skipped-table passage cannot be verified.

    Passages excluded from value compilation are still published as raw
    evidence.  Each one must name a table that is registered in its own source
    grid and must carry a readable raw snapshot; an unknown structure is
    allowed, an unverifiable one is not.
    """
    problems: List[str] = []
    for artifact in artifacts:
        metadata = artifact.source_metadata or {}
        if metadata.get("compile_table_values", True) is not False:
            continue
        table_id = metadata.get("table_id")
        registered = table_ids_by_source.get(artifact.source_id, [])
        # A raw article without a declared table id is still a valid asset;
        # only a *declared* native table that does not resolve is an asset error.
        if table_id and str(table_id) not in registered:
            problems.append("{}:TABLE_CLAIM_NOT_REGISTERED:{}".format(artifact.chunk_id, table_id))
    if problems:
        raise ValueError("TABLE_CLAIM_VALIDATION_FAILED: " + "; ".join(sorted(problems)))


# ──────────────────────────────────────────────
#  Bundle composition
# ──────────────────────────────────────────────


def _canonical_bytes(value: Any) -> bytes:
    """Canonical JSON with bounded Unicode intermediates (unchanged wire bytes)."""
    from io import BytesIO
    output = BytesIO()
    for chunk in _canonical_fragments(value):
        output.write(chunk.encode("utf-8"))
    return output.getvalue()


def build_bundle_files(**kwargs: Any) -> Dict[str, bytes]:
    """Compatibility API for callers that need a materialized byte mapping."""
    return dict(iter_bundle_files(**kwargs))


def iter_bundle_files(
    *,
    revision_id: str,
    artifacts: List[SourceArtifact],
    state: CompilerState,
    error_book: ErrorBook,
    claims: List[ValidatedClaim],
    manifest: Dict[str, Any],
    counts: Dict[str, int],
    digest_map: Dict[str, str],
    tenant_id: str = "",
    context_id: str = "",
    source_inventory: SourceInventorySnapshot | None = None,
    effective_runtime: EffectiveRuntimeSnapshot | None = None,
    lexical_index: bytes | None = None,
    rendered_wiki_files: Dict[str, bytes] | None = None,
    prepared_corpus=None,
    prepared_audit=None,
    render_signatures: Dict[str, str] | None = None,
    previous_reading_index: Dict[str, Any] | None = None,
    previous_page_index: Dict[str, Any] | None = None,
    rendered_pages: set[str] | None = None,
    source_contributions: Any = None,
    previous_contributions: Any = None,
    link_increment: Any = None,
    previous_link_increment: Any = None,
    previous_wiki_files: Dict[str, str] | None = None,
    file_references: Mapping[str, RevisionFileReference] | None = None,
    shard_artifacts: bool = False,
    previous: Any = None,
    prepared_contribution_payload: Any = None,
) -> Iterator[Tuple[str, bytes | RevisionFileReference | RevisionFileArtifact]]:
    """Yield identical bundle artifacts one at a time, without retaining their bytes."""
    claim_audit = prepared_audit if prepared_audit is not None else enforce_claim_policy(
        state,
        claims,
        policy=resolve_claim_policy(manifest),
    )
    effective_counts = {**counts, **claim_audit["summary"]}
    claim_audit_mirror = {
        key: claim_audit[key]
        for key in (
            "contract",
            "schema_version",
            "policy",
            "degraded",
            "summary",
        )
    }
    # S5.1: the page-independent source contributions, derived from the very same
    # claims.  Computed before the manifest so the mirror below can describe it.
    from jonex_wiki.domain.engine.compile import source_contribution
    if source_contributions is not None:
        contributions = source_contributions
    else:
        incoming = source_contribution.from_claims(claims, source_versions=digest_map, pages=state.pages)
        contributions = incoming
    contribution_payload = (prepared_contribution_payload if prepared_contribution_payload is not None
                            else source_contribution.payload(contributions))
    snapshots = build_source_snapshots(artifacts)
    source_inventory = source_inventory or bundle.build_source_inventory(
        artifacts, digest_map
    )
    effective_runtime = effective_runtime or bundle.capture_effective_runtime(
        manifest,
        prompt_checksum=_prompt_checksum(),
        tool_checksum=_tool_checksum(),
        environment={},
    )
    runtime_snapshot_checksum = bundle.snapshot_checksum(
        effective_runtime
    )

    shard_specs = getattr(previous, "json_shards", None) or {}
    def partitioned(path, data, fields, old_data=None, owners=None, bucket_count=256):
        return iter_json_artifact(path, data, fields=fields, encode=_canonical_bytes,
            previous=old_data, previous_specs=shard_specs.get(path),
            file_references=file_references, owners=owners, bucket_count=bucket_count)

    revision = {
        "revision_id": revision_id,
        "tenant_id": tenant_id,
        "context_id": context_id,
        "engine_id": ENGINE_ID,
        "engine_version": ENGINE_VERSION,
        "schema_version": SCHEMA_VERSION,
        "prompt_checksum": effective_runtime.prompt_checksum,
        "tool_checksum": effective_runtime.tool_checksum,
        "source_inventory_digest": source_inventory.digest,
        "runtime_snapshot_checksum": runtime_snapshot_checksum,
        "digest_map": digest_map,
        "source_digests": {
            source_id: snap["content_sha256"]
            for source_id, snap in snapshots.items()
        },
    }
    if shard_artifacts:
        large_state_fields = {key: value for key, value in state
                              if isinstance(value, dict) and key in type(state).model_fields}
        state_payload = {**state.model_dump(exclude=set(large_state_fields)), **large_state_fields}
        old_state = ({key: getattr(previous.state, key) for key in large_state_fields}
                     if previous is not None else None)
        archive_owners = {key: value.document_id for key, value in state.source_archive.items()}
        source_documents = {}
        for passage in state.source_archive.values():
            source_documents.setdefault(passage.source_id, set()).add(passage.document_id)
        page_owners = {key: "|".join(sorted({document
            for source in value.source_ids
            for document in ({state.source_archive[source].document_id} if source in state.source_archive
                             else source_documents.get(source, ())) }))
            for key, value in state.pages.items()}
        yield from partitioned("state/compiler.json", state_payload,
            {key: None for key in large_state_fields}, old_state,
            {"source_archive": archive_owners, "pages": page_owners, "index": page_owners})
        revision["compiler_state_file"] = "state/compiler.json"
    else:
        revision["compiler_state"] = state.model_dump()
    yield ("revision.json", _canonical_bytes(revision))
    del revision

    manifest_payload = {
        "revision_id": revision_id,
        "tenant_id": tenant_id,
        "context_id": context_id,
        "engine_id": ENGINE_ID,
        "engine_version": ENGINE_VERSION,
        "schema_version": SCHEMA_VERSION,
        "manifest_checksum": manifest.get("checksum", ""),
        "manifest": manifest,
        "models": manifest.get("models", {}),
        "counts": effective_counts,
        "claim_audit": claim_audit_mirror,
        "source_contributions": {
            "version": contribution_payload["version"],
            "record_count": len(contribution_payload["records"]),
            "digest": contribution_payload["digest"],
            "rebuilt_from_legacy": contribution_payload["rebuilt_from_legacy"],
        },
        "source_inventory_digest": source_inventory.digest,
        "runtime_snapshot_checksum": runtime_snapshot_checksum,
    }
    yield ("manifest.json", _canonical_bytes(manifest_payload))

    from jonex_wiki.domain.engine.compile.publication import KEEP_AND_SEARCH_RULE_VERSION
    from jonex_wiki.domain.engine.compile.raw_collection import (
        collect_article_and_table_pages,
        collect_workbook_outlines,
    )
    corpus = prepared_corpus if prepared_corpus is not None else build_query_corpus(
        state, claims, error_book=error_book, include_sources=True,
        source_unit_rule_version=KEEP_AND_SEARCH_RULE_VERSION,
        raw_pages=collect_article_and_table_pages(artifacts),
        workbook_outlines=collect_workbook_outlines(artifacts))
    # Cursor identity is part of the published query snapshot and is bound to
    # the immutable revision, not to the pre-publication in-memory corpus.
    corpus = corpus.model_copy(update={"revision_id": revision_id})
    # S6.3: the links the compile model already published, audited here.  This
    # layer never invents a link and never upgrades one: a published
    # supported_relation keeps its kind only if its evidence is really in the
    # stored contributions, and every downgrade is recorded in the payload.
    from jonex_wiki.domain.engine.compile import relation_increment
    if link_increment is not None:
        relations = link_increment
        relation_downgraded: List[Dict[str, Any]] = []
    else:
        published_links = relation_increment.proposals_from_pages(corpus.files)
        incoming_relations = relation_increment.build_link_increment(
            contributions,
            proposals=published_links["proposals"] + [relation_increment.LinkProposal.model_validate(value)
                for record in contributions.records for value in record.link_proposals],
            page_index=corpus.page_index,
            legacy_related=published_links["related"],
        )
        relations = incoming_relations
        relations = relation_increment.resolve_pending(
            relations, page_index=corpus.page_index, contributions=contributions)
        relation_downgraded = relation_increment.audit_declared_relations(
            relation_increment.page_declared_relations(corpus.files),
            relations, contributions)
    relation_payload = relation_increment.payload(
        relations, page_index=corpus.page_index, downgraded=relation_downgraded)
    for key, data in corpus.files.items():
        path = "wiki/" + key
        if (file_references is not None and path in file_references
                and previous_wiki_files is not None and previous_wiki_files.get(key) == data):
            yield (path, file_references[path])
        else:
            yield (path, data.encode("utf-8"))
    if render_signatures is not None:
        yield ("indexes/render_signatures.json", _canonical_bytes({
            "schema_version": 1, "signatures": render_signatures,
        }))
    # Draft prose remains auditable in revision.compiler_state, never the
    # public Wiki. Snapshot has no per-query route or live readers.
    from jonex_wiki.domain.engine.compile.publication import disposition_ledger
    snapshot = {"schema_version": 2, "rule_version": KEEP_AND_SEARCH_RULE_VERSION,
                "revision_id": revision_id,
                "corpus_sha256": _corpus_sha256(corpus)}
    yield ("query/corpus.json", _canonical_bytes(snapshot))
    from jonex_wiki.domain.engine.compile.mention_index import (
        build_page_graph_and_links,
    )
    from jonex_wiki.kernel.telemetry import current_runtime_telemetry, telemetry_stage
    with telemetry_stage(current_runtime_telemetry(), "reading_index"):
        reading_index = build_page_graph_and_links(
            corpus.page_index, corpus.files,
            previous=previous_reading_index,
            previous_page_index=previous_page_index,
            changed_paths=rendered_pages,
        )
    # [jonex] source_records/source_locations 单独落 source_index.json：这俩是 query
    # 检索溯源用的重数据（大库实测 source_locations 达 258MB），而 reading 页面清单
    # （list_pages/outline）只读 page_index/page_graph。拆开后 reading 侧
    # load_navigation 只读 ~10MB 的 navigation.json，避免 266MB 全量读拖慢
    # wiki-contents / wiki/outline。
    from jonex_wiki.domain.engine.query.retrieval import page_source_ids_from_markdown
    # Query projection: publication already validates the compiler snapshot.
    # Readers restore navigation directly instead of decoding compiler state.
    from jonex_wiki.domain.engine.compile.identity import build_identity_index
    identity = build_identity_index(contributions, source_versions=contributions.source_versions)
    published_query_links = {}
    for record in relations.links:
        published_query_links.setdefault(record.page_path, []).append({
            "target_id": record.target_id, "why_visit": record.why_visit, "kind": record.kind})
    navigation_payload = {
        "schema_version": 2,
        "query_projection_version": 1,
        "source_scopes": corpus.source_scopes,
        "revision_id": revision_id,
        "page_source_ids": {path: page_source_ids_from_markdown(corpus.files[path])
                            for path in corpus.page_index},
        "fact_mentions": {name: identity.pages_for(name) for name in sorted(
            set(identity.entity_to_pages) | set(identity.topic_to_pages))},
        "page_links": published_query_links,
        "page_index": {key: value.model_dump() for key, value in corpus.page_index.items()},
        "claim_ids": {
            path: [claim.claim_id for claim in page_claims]
            for path, page_claims in corpus.claims.items()
        },
        "page_number_map": corpus.page_number_map,
        "page_graph": reading_index["page_graph"],
        "relations": {"version": relation_payload["version"],
                      "link_count": len(relation_payload["links"]),
                      "pending_count": len(relation_payload["pending"]),
                      "digest": relation_payload["digest"]},
    }
    from jonex_wiki.domain.engine.query.retrieval import build_evidence_fallback
    navigation_payload['evidence_fallback'] = build_evidence_fallback(
        navigation_payload['page_source_ids'], corpus.source_records, skip_paths=set(corpus.claims))
    if shard_artifacts:
        source_documents = {}
        for passage in state.source_archive.values():
            source_documents.setdefault(passage.source_id,set()).add(passage.document_id)
        page_owners = {path:'|'.join(sorted({doc for sid in ids for doc in source_documents.get(sid,())}))
                       for path,ids in navigation_payload['page_source_ids'].items()}
        fields = {key:None for key,value in navigation_payload.items() if isinstance(value,dict) and key != 'relations'}
        yield from partitioned('query/navigation.json',navigation_payload,fields,
            getattr(previous,'navigation',None), {key:page_owners for key in fields if key != 'fact_mentions'},32)
    else:
        yield ('query/navigation.json',_canonical_bytes(navigation_payload))
    source_payload = {
        "schema_version": 2,
        "source_records": corpus.source_records,
        "source_locations": corpus.source_locations,
    }
    if shard_artifacts:
        source_payload, source_owners = compact_source_index(corpus.source_records, corpus.source_locations)
        old_sources = (compact_source_index(previous.corpus.source_records, previous.corpus.source_locations)[0]
                       if previous is not None and previous.corpus is not None else None)
        yield from partitioned("query/source_index.json", source_payload,
            {"source_records": None, "source_locations": None, "passage_metadata": None}, old_sources,
            source_owners)
    else:
        yield ("query/source_index.json", _canonical_bytes(source_payload))
    links_payload = {
        "schema_version": 1,
        "in_links": reading_index["in_links"],
        "mentions": reading_index["mentions"],
        "mention_filters": reading_index["mention_filters"],
    }
    if shard_artifacts:
        yield from partitioned("indexes/links.json", links_payload,
            {"in_links": None, "mentions": None, "mention_filters": None}, previous_reading_index)
    else:
        yield ("indexes/links.json", _canonical_bytes(links_payload))
    del reading_index
    yield ("evidence_disposition.json", _canonical_bytes(disposition_ledger(state, claims, error_book, rule_version=KEEP_AND_SEARCH_RULE_VERSION)))

    # Slug collisions (e.g. "a/b" and "a?b" both slug to "a_b") must never
    # overwrite each other; each colliding source gets a deterministic suffix.
    slug_owners: Dict[str, List[str]] = {}
    for source_id in snapshots:
        slug_owners.setdefault(safe_slug(source_id), []).append(source_id)

    def source_slug(source_id: str) -> str:
        slug = safe_slug(source_id)
        if len(slug_owners[slug]) > 1:
            return "{}_{}".format(slug, sha256_hex(source_id.encode("utf-8"))[:12])
        return slug

    digests: Dict[str, Dict[str, Any]] = {}
    for source_id, snap in snapshots.items():
        slug = source_slug(source_id)
        yield ("sources/articles/{}.md".format(slug), snap["text"].encode("utf-8"))
        digest = {
            "source_id": source_id,
            "document_id": snap["document_id"],
            "content_sha256": snap["content_sha256"],
            "passage_ids": snap["passage_ids"],
            "parser_id": snap["parser_id"],
            "content_generation": snap["content_generation"],
            "source_metadata": snap["source_metadata"],
        }
        if snap.get("reading_materials") is not None:
            digest["reading_materials"] = _finalize_reading_materials(
                snap["reading_materials"], slug
            )
        digests[source_id] = digest
        yield ("sources/digests/{}.json".format(slug), _canonical_bytes(digest))

    # Attach the fixed original and source-level table grid once.  Local paths
    # are transport-only and were removed from compiler_state by
    # artifact_to_passage, so a moved Bundle never depends on the build host.
    attachments_seen = set()
    attachment_registry: Dict[str, Dict[str, str]] = {}
    table_ids_by_source: Dict[str, List[str]] = {}
    for artifact in artifacts:
        slug = source_slug(artifact.source_id)
        metadata = artifact.source_metadata or {}
        for key, relative in (("original_local_path", "original" + Path(str(metadata.get("source_name") or "source")).suffix.lower()),
                              ("tables_sqlite_local_path", "tables.sqlite")):
            local = metadata.get(key)
            target = "sources/{}/{}".format(slug, relative)
            if not local or target in attachments_seen:
                continue
            path = Path(str(local))
            if not path.is_file():
                raise ValueError("bundle attachment is missing: " + str(path))
            data = path.read_bytes()
            if key == "original_local_path" and sha256_hex(data) != artifact.sha256:
                raise ValueError("original attachment checksum changed during compile")
            if key == "tables_sqlite_local_path":
                # The grid is the authoritative list of published tables: a
                # source may declare fewer handles than its artifact holds.
                table_ids_by_source[artifact.source_id] = _registered_table_ids(path)
            yield (target, data)
            attachments_seen.add(target)
            attachment_registry.setdefault(artifact.source_id, {})[
                "original" if key == "original_local_path" else "tables"
            ] = target
    _validate_skipped_table_claims(artifacts, table_ids_by_source)
    for source_id, digest in digests.items():
        published = table_ids_by_source.get(source_id)
        if published is None:
            continue
        digest["table_ids"] = published
        yield ("sources/digests/{}.json".format(source_slug(source_id)), _canonical_bytes(digest))
    yield ("sources/registry.json", _canonical_bytes({
        "schema_version": 1,
        "revision_id": revision_id,
        "sources": [{
            "source_id": source_id,
            "document_id": snapshots[source_id]["document_id"],
            "source_page": "wiki/sources/source-{}.md".format(
                sha256_hex(source_id.encode())[:16]
            ),
            "table_ids": table_ids_by_source.get(source_id, []),
            **attachment_registry.get(source_id, {}),
        } for source_id in sorted(snapshots)],
    }))

    claims_lines = "".join(
        json.dumps(c.model_dump(), ensure_ascii=False, sort_keys=True) + "\n" for c in claims
    )
    yield ("claims.jsonl", claims_lines.encode("utf-8"))
    yield ("claim_audit.json", _canonical_bytes(claim_audit))
    # S5.1: writing the record set here puts it under the container's checksum
    # map automatically (see ``encode_bundle``).
    if shard_artifacts:
        contribution_owners = {record.record_id: "|".join(sorted({span.document_id
            for span in record.source_spans})) for record in contributions.records}
        old_contributions = ({key: value for key, value in previous.source_contributions}
            if previous is not None and previous.source_contributions is not None else None)
        fields = {key: None for key, value in contribution_payload.items() if isinstance(value, dict)}
        fields["records"] = "record_id"
        yield from partitioned(source_contribution.FILE_NAME, contribution_payload,
            fields, old_contributions, {"records": contribution_owners})
    else:
        yield (source_contribution.FILE_NAME, _canonical_bytes(contribution_payload))
    # S6.3: same container checksum map, and the navigation snapshot points at
    # it so the default query tool reads the audited links, not the raw draft.
    if shard_artifacts:
        yield from partitioned(relation_increment.FILE_NAME,relation_payload,
            {'links':lambda row:hashlib.sha256(_canonical_bytes(row)).hexdigest(),
             'pending':lambda row:hashlib.sha256(_canonical_bytes(row)).hexdigest()},
            getattr(previous,'relation_payload',None), bucket_count=32)
    else:
        yield (relation_increment.FILE_NAME, _canonical_bytes(relation_payload))

    ledger = [
        {
            "phase": "compile",
            "passages": effective_counts["passages"],
            "pages": effective_counts["pages"],
            "claims": effective_counts["claims"],
        },
        {
            "phase": "claim_gate",
            "policy": claim_audit["policy"],
            "degraded": claim_audit["degraded"],
            "summary": claim_audit["summary"],
        },
        {"phase": "finalize", "publishable": True},
    ]
    yield ("ledger.json", _canonical_bytes(ledger))

    # JSON is valid YAML; the Error Book round-trips without type loss.
    yield ("error_book.yaml", error_book.model_dump_json(indent=2).encode("utf-8"))

    provenance = {
        "schema_version": 1,
        "revision_id": revision_id,
        "tenant_id": tenant_id,
        "context_id": context_id,
        "source_inventory_digest": source_inventory.digest,
        "source_inventory": source_inventory.model_dump(),
        "runtime_snapshot_checksum": runtime_snapshot_checksum,
        "effective_runtime": effective_runtime.model_dump(),
    }
    yield ("provenance.json", _canonical_bytes(provenance))

    lexical_bytes = lexical_index or build_fts_index_bytes(corpus)
    if not isinstance(lexical_bytes,(bytes,RevisionFileReference,RevisionFileArtifact)) or not lexical_bytes:
        raise ValueError("lexical index must be non-empty SQLite bytes")
    yield ("indexes/lexical.sqlite", lexical_bytes)

    index_manifest = {
        "schema_version": 1,
        "revision_id": revision_id,
        "source_inventory_digest": source_inventory.digest,
        "runtime_snapshot_checksum": runtime_snapshot_checksum,
        "lexical": {
            "preprocessing": lexical_preprocessing_identity(),
            "format": "sqlite-fts5",
            "sha256": lexical_bytes.sha256 if isinstance(lexical_bytes,(RevisionFileReference,RevisionFileArtifact)) else sha256_hex(lexical_bytes),
            "page_count": len(corpus.page_text),
        },
    }
    yield ("indexes/manifest.json", _canonical_bytes(index_manifest))


def build_empty_bundle_files(
    *,
    revision_id: str,
    manifest: Dict[str, Any],
    tenant_id: str = "",
    context_id: str = "",
    source_inventory: SourceInventorySnapshot | None = None,
    effective_runtime: EffectiveRuntimeSnapshot | None = None,
) -> Dict[str, bytes]:
    """Compose the minimal bundle for a context with no sources."""
    counts = {
        "sources": 0,
        "passages": 0,
        "pages": 0,
        "claims": 0,
        "open_errors": 0,
    }
    files = build_bundle_files(
        revision_id=revision_id,
        artifacts=[],
        state=CompilerState(),
        error_book=ErrorBook(),
        claims=[],
        manifest=manifest,
        counts=counts,
        digest_map={},
        tenant_id=tenant_id,
        context_id=context_id,
        source_inventory=source_inventory,
        effective_runtime=effective_runtime,
    )
    claim_audit = json.loads(files["claim_audit.json"].decode("utf-8"))
    files["ledger.json"] = canonical_json(
        [
            {"phase": "no_sources"},
            {
                "phase": "claim_gate",
                "policy": claim_audit["policy"],
                "degraded": claim_audit["degraded"],
                "summary": claim_audit["summary"],
            },
            {"phase": "finalize", "publishable": True},
        ]
    ).encode("utf-8")
    return files


@dataclass
class PreviousBundle:
    """Compiler state/claims/manifest restored from a verified previous bundle."""

    state: CompilerState
    claims: List[ValidatedClaim]
    error_book: ErrorBook
    digest_map: Dict[str, str]
    source_digests: Dict[str, str]
    manifest: Dict[str, Any]
    revision_id: str
    tenant_id: str
    context_id: str
    source_inventory: SourceInventorySnapshot
    effective_runtime: EffectiveRuntimeSnapshot
    runtime_snapshot_checksum: str
    lexical_index: bytes | RevisionFileReference
    index_manifest: Dict[str, Any]
    corpus: Any = None
    render_signatures: Dict[str, str] | None = None
    reading_index: Dict[str, Any] | None = None
    source_contributions: Any = None
    link_increment: Any = None
    json_shards: Dict[str, Any] | None = None
    navigation: Dict[str, Any] | None = None
    relation_payload: Dict[str, Any] | None = None
    source_unit_rule_version: str | None = None


def decode_previous_bundle(bundle_bytes: bytes) -> PreviousBundle:
    """Decode and validate a previous bundle, restoring state/claims/manifest.

    Every relative path and checksum is validated by the bundle codec; then the
    embedded compiler state, claims, Error Book and complete Manifest snapshot
    are re-parsed so malformed content fails closed.
    """
    from jonex_wiki.kernel.telemetry import count_work
    count_work("bundle_decode")
    decoded = decode_bundle(bundle_bytes)
    return _decode_previous_decoded(decoded)


def decode_previous_files(files: Dict[str, bytes], *, revision_id: str,
                          engine_id: str = ENGINE_ID, engine_version: str = ENGINE_VERSION) -> PreviousBundle:
    """Restore a directory Bundle without constructing a base64 container."""
    from jonex_wiki.kernel.telemetry import count_work
    from jonex_wiki.kernel.contracts import BundleProvenance
    count_work("directory_bundle_open")
    files = dict(files)
    if "indexes/catalog.sqlite" in files and "indexes/lexical.sqlite" not in files:
        files["indexes/lexical.sqlite"] = files.pop("indexes/catalog.sqlite")
    try:
        provenance_payload = json.loads(files["provenance.json"].decode("utf-8"))
        provenance = BundleProvenance.model_validate({key: provenance_payload[key] for key in (
            "tenant_id", "context_id", "source_inventory_digest", "runtime_snapshot_checksum")})
    except (KeyError, ValueError, TypeError, UnicodeDecodeError) as exc:
        raise ValueError("directory bundle provenance is missing or malformed") from exc
    decoded = DecodedBundle(files=dict(files), checksums={path: sha256_hex(data) for path, data in files.items()},
                            revision_id=revision_id, engine_id=engine_id,
                            engine_version=engine_version, provenance=provenance)
    return _decode_previous_decoded(decoded)


def decode_previous_view(view) -> PreviousBundle:
    """Verify a directory, then read only artifacts needed to restore it.

    Do not retain the complete byte tree alongside decoded compiler/query
    objects. Every read still goes through the view's checksum validation.
    """
    from jonex_wiki.kernel.contracts import BundleProvenance
    from jonex_wiki.kernel.telemetry import count_work
    entries = view.list_entries()
    # Legacy split compiler-state/catalog layouts retain their existing decoder.
    if ("indexes/catalog.sqlite" in entries
            or not getattr(view, "is_published_revision", True)):
        return decode_previous_files({path: view.read_bytes(path) for path in entries},
                                     revision_id=view.revision_id)

    class Files(Mapping):
        def __iter__(self):
            return iter(entries)

        def __len__(self):
            return len(entries)

        def __contains__(self, path):
            return path in entry_set

        def __getitem__(self, path):
            if path not in entry_set:
                raise KeyError(path)
            return view.read_bytes(path)

        def file_reference(self,path):return view.file_reference(path)

    entry_set = set(entries)
    files = Files()
    provenance_payload = json.loads(files["provenance.json"])
    provenance = BundleProvenance.model_validate({key: provenance_payload[key] for key in (
        "tenant_id", "context_id", "source_inventory_digest", "runtime_snapshot_checksum")})
    count_work("directory_bundle_open")
    decoded = DecodedBundle(files=files, checksums={entry["path"]: entry["sha256"]
        for entry in view.manifest["files"]}, revision_id=view.revision_id,
        engine_id=ENGINE_ID, engine_version=ENGINE_VERSION, provenance=provenance)
    previous = _decode_previous_decoded(decoded)
    # Reads above have verified their bytes already. The view checks the
    # remaining files, unchanged file identities and directory manifest here.
    view.verify_integrity()
    return previous


def _decode_previous_decoded(decoded: DecodedBundle) -> PreviousBundle:
    json_shards = {}
    def read_json(path):
        return load_json_artifact(decoded.files.__getitem__, path, manifests=json_shards)
    try:
        revision = json.loads(decoded.files["revision.json"].decode("utf-8"))
        if not isinstance(revision, dict):
            raise ValueError("previous bundle revision must be an object")
        inner_revision_id = revision.get("revision_id")
        if inner_revision_id != decoded.revision_id:
            raise ValueError("inner revision id does not match bundle revision id")
        if len(decoded.revision_id) > 64:
            raise ValueError("revision id exceeds the DB column limit")
        if revision.get("engine_id") != ENGINE_ID:
            raise ValueError("inner revision engine id is invalid")
        if revision.get("engine_version") != ENGINE_VERSION:
            raise ValueError("inner revision engine version is invalid")
        if decoded.engine_id != revision.get("engine_id"):
            raise ValueError("outer and inner engine ids do not match")
        if decoded.engine_version != revision.get("engine_version"):
            raise ValueError("outer and inner engine versions do not match")
        if revision.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("inner revision schema version is invalid")
        if not isinstance(revision.get("prompt_checksum"), str):
            raise ValueError("inner revision prompt checksum is invalid")
        if not isinstance(revision.get("tool_checksum"), str):
            raise ValueError("inner revision tool checksum is invalid")
        if "compiler_state" in revision:
            state_payload = revision["compiler_state"]
        else:
            if revision.get("compiler_state_file", "state/compiler.json") != "state/compiler.json":
                raise ValueError("invalid compiler state reference")
            state_payload = read_json("state/compiler.json")
            state_payload = state_payload.get("compiler_state", state_payload)
        state = CompilerState.model_validate(state_payload)
        digest_map = _string_map(revision["digest_map"], "digest_map")
        source_digests = _string_map(revision["source_digests"], "source_digests")
        if set(digest_map) != set(source_digests):
            raise ValueError("digest maps must cover the same source ids")

        claims: List[ValidatedClaim] = []
        for line in decoded.files["claims.jsonl"].decode("utf-8").splitlines():
            line = line.strip()
            if line:
                claims.append(ValidatedClaim.model_validate(json.loads(line)))

        error_book = ErrorBook.model_validate(
            json.loads(decoded.files["error_book.yaml"].decode("utf-8"))
        )

        # S5.1/S5.2: a Bundle written before these records existed still opens -
        # the set is rebuilt from the claims it already stores, with every field
        # that material cannot supply left empty and marked as rebuilt.  Nothing
        # is inferred, and no model is called.
        from jonex_wiki.domain.engine.compile import source_contribution
        raw_contributions = None
        if source_contribution.FILE_NAME in decoded.files:
            raw_contributions = read_json(source_contribution.FILE_NAME)
        source_contributions = source_contribution.load(
            raw_contributions, claims=claims
        )
        from jonex_wiki.domain.engine.compile import relation_increment
        raw_relations = None
        if relation_increment.FILE_NAME in decoded.files:
            raw_relations = read_json(relation_increment.FILE_NAME)
        link_increment = relation_increment.load(raw_relations)

        manifest_payload = json.loads(
            decoded.files["manifest.json"].decode("utf-8")
        )
        if not isinstance(manifest_payload, dict):
            raise ValueError("previous bundle manifest payload must be an object")
        if manifest_payload.get("revision_id") != decoded.revision_id:
            raise ValueError("manifest revision id does not match bundle revision id")
        if manifest_payload.get("engine_id") != ENGINE_ID:
            raise ValueError("manifest engine id is invalid")
        if manifest_payload.get("engine_version") != ENGINE_VERSION:
            raise ValueError("manifest engine version is invalid")
        if manifest_payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("manifest schema version is invalid")
        manifest = manifest_payload.get("manifest")
        if not isinstance(manifest, dict):
            raise ValueError("previous bundle manifest must be an object")
        if not isinstance(manifest.get("models", {}), dict):
            raise ValueError("previous bundle manifest models must be an object")
        claim_audit = json.loads(
            decoded.files["claim_audit.json"].decode("utf-8")
        )
        if not isinstance(claim_audit, dict):
            raise ValueError("bundle claim audit must be an object")
        expected_claim_audit = build_claim_audit(
            state,
            claims,
            policy=resolve_claim_policy(manifest),
        )
        if (
            expected_claim_audit["policy"] == "benchmark"
            and expected_claim_audit["failed_passages"]
        ):
            raise ValueError("benchmark bundle violates the claim publication gate")
        # S1.1: support labels are frozen in a published Bundle.  A Bundle
        # compiled before the protected-atom fix carries ``support_rule``
        # ``claim-support-v1`` (or no marker at all); re-deriving its coverage
        # with the current rule legitimately changes ``degraded``/``summary``/
        # ``failed_passages``.  Such a Bundle is read with the labels it was
        # published with and never rewritten, while a Bundle that claims the
        # *current* rule must still match exactly.
        trusted_claim_audit = expected_claim_audit
        stored_support_rule = claim_audit.get("support_rule")
        if stored_support_rule not in (None, "claim-support-v1", "claim-support-v2", SUPPORT_RULE_VERSION):
            raise ValueError("unsupported claim support rule: " + str(stored_support_rule))
        if claim_audit != expected_claim_audit:
            if stored_support_rule == SUPPORT_RULE_VERSION:
                raise ValueError("bundle claim audit does not match source evidence")
            if not isinstance(claim_audit.get("summary"), dict):
                raise ValueError("bundle claim audit summary is invalid")
            logger.warning(
                "legacy claim support rule accepted | revision=%s stored=%s current=%s",
                decoded.revision_id,
                stored_support_rule or "claim-support-v1",
                SUPPORT_RULE_VERSION,
            )
            trusted_claim_audit = claim_audit
        expected_claim_audit_mirror = {
            key: trusted_claim_audit.get(key)
            for key in (
                "contract",
                "schema_version",
                "policy",
                "degraded",
                "summary",
            )
        }
        if manifest_payload.get("claim_audit") != expected_claim_audit_mirror:
            raise ValueError("manifest claim audit mirror is inconsistent")
        manifest_counts = manifest_payload.get("counts")
        if not isinstance(manifest_counts, dict):
            raise ValueError("manifest counts must be an object")
        if any(
            manifest_counts.get(key) != value
            for key, value in (trusted_claim_audit.get("summary") or {}).items()
        ):
            raise ValueError("manifest claim audit counts are inconsistent")

        provenance_payload = json.loads(
            decoded.files["provenance.json"].decode("utf-8")
        )
        if not isinstance(provenance_payload, dict):
            raise ValueError("bundle provenance payload must be an object")
        tenant_id = provenance_payload.get("tenant_id")
        context_id = provenance_payload.get("context_id")
        if not isinstance(tenant_id, str) or not tenant_id:
            raise ValueError("bundle tenant_id is invalid")
        if not isinstance(context_id, str) or not context_id:
            raise ValueError("bundle context_id is invalid")
        if provenance_payload.get("revision_id") != decoded.revision_id:
            raise ValueError("provenance revision id does not match bundle")
        source_inventory = SourceInventorySnapshot.model_validate(
            provenance_payload["source_inventory"]
        )
        inventory_material = {
            "schema_version": source_inventory.schema_version,
            "entries": [entry.model_dump() for entry in source_inventory.entries],
        }
        expected_inventory_digest = sha256_hex(
            bundle.canonical_json_bytes(inventory_material)
        )
        if source_inventory.digest != expected_inventory_digest:
            raise ValueError("source inventory digest is invalid")
        effective_runtime = EffectiveRuntimeSnapshot.model_validate(
            provenance_payload["effective_runtime"]
        )
        if revision.get("prompt_checksum") != effective_runtime.prompt_checksum:
            raise ValueError("revision prompt checksum does not match runtime snapshot")
        if revision.get("tool_checksum") != effective_runtime.tool_checksum:
            raise ValueError("revision tool checksum does not match runtime snapshot")
        manifest_models = manifest.get("models", {})
        if manifest_payload.get("models") != manifest_models:
            raise ValueError("manifest model mirror is inconsistent")
        if effective_runtime.models != manifest_models:
            raise ValueError("runtime model identity does not match manifest")
        # Hash the validated original envelope so additive optional identity
        # fields do not silently change an older publication's checksum.
        runtime_snapshot_checksum = bundle.snapshot_checksum(
            provenance_payload["effective_runtime"]
        )
        if (
            provenance_payload.get("runtime_snapshot_checksum")
            != runtime_snapshot_checksum
        ):
            raise ValueError("effective runtime snapshot checksum is invalid")
        if (
            provenance_payload.get("source_inventory_digest")
            != source_inventory.digest
        ):
            raise ValueError("provenance source inventory digest is inconsistent")
        outer_provenance = decoded.provenance
        if (
            outer_provenance.tenant_id != tenant_id
            or outer_provenance.context_id != context_id
            or outer_provenance.source_inventory_digest != source_inventory.digest
            or outer_provenance.runtime_snapshot_checksum
            != runtime_snapshot_checksum
        ):
            raise ValueError("outer bundle provenance does not match provenance.json")
        for owner_payload, label in (
            (revision, "revision"),
            (manifest_payload, "manifest"),
        ):
            if owner_payload.get("tenant_id") != tenant_id:
                raise ValueError(f"{label} tenant does not match provenance")
            if owner_payload.get("context_id") != context_id:
                raise ValueError(f"{label} context does not match provenance")
            if owner_payload.get("source_inventory_digest") != source_inventory.digest:
                raise ValueError(f"{label} source inventory does not match provenance")
            if (
                owner_payload.get("runtime_snapshot_checksum")
                != runtime_snapshot_checksum
            ):
                raise ValueError(f"{label} runtime snapshot does not match provenance")

        seen_source_digests: Dict[str, str] = {}
        for rel in decoded.files:
            if not rel.startswith("sources/digests/"):
                continue
            raw = decoded.files[rel]
            source_digest = json.loads(raw.decode("utf-8"))
            if not isinstance(source_digest, dict):
                raise ValueError("source digest must be an object")
            source_id = source_digest.get("source_id")
            content_sha256 = source_digest.get("content_sha256")
            if not isinstance(source_id, str) or not source_id:
                raise ValueError("source digest source_id is invalid")
            if source_id in seen_source_digests:
                raise ValueError("duplicate source digest: " + source_id)
            if not isinstance(content_sha256, str) or not content_sha256:
                raise ValueError("source digest content_sha256 is invalid")
            if not isinstance(source_digest.get("document_id"), str):
                raise ValueError("source digest document_id is invalid")
            if not isinstance(source_digest.get("parser_id"), str):
                raise ValueError("source digest parser_id is invalid")
            if not isinstance(source_digest.get("content_generation"), int):
                raise ValueError("source digest content_generation is invalid")
            if not isinstance(source_digest.get("source_metadata"), dict):
                raise ValueError("source digest source_metadata is invalid")
            passage_ids = source_digest.get("passage_ids")
            if not isinstance(passage_ids, list) or any(
                not isinstance(item, str) for item in passage_ids
            ):
                raise ValueError("source digest passage_ids is invalid")
            seen_source_digests[source_id] = content_sha256
        if seen_source_digests != source_digests:
            raise ValueError("source digest files do not match revision source_digests")

        inventory_sources = {entry.source_id for entry in source_inventory.entries}
        if inventory_sources != set(source_digests):
            raise ValueError("source inventory does not cover source digest files")

        index_manifest = json.loads(
            decoded.files["indexes/manifest.json"].decode("utf-8")
        )
        if not isinstance(index_manifest, dict):
            raise ValueError("index metadata must be a JSON object")
        if index_manifest.get("schema_version") != 1:
            raise ValueError("index manifest schema version is invalid")
        if index_manifest.get("revision_id") != decoded.revision_id:
            raise ValueError("index manifest revision does not match bundle")
        if index_manifest.get("source_inventory_digest") != source_inventory.digest:
            raise ValueError("index manifest source inventory does not match bundle")
        if index_manifest.get("runtime_snapshot_checksum") != runtime_snapshot_checksum:
            raise ValueError("index manifest runtime snapshot does not match bundle")
        lexical_meta = index_manifest.get("lexical")
        if not isinstance(lexical_meta, dict):
            raise ValueError("lexical index manifest must be an object")
        lexical_index = (decoded.files.file_reference('indexes/lexical.sqlite')
            if hasattr(decoded.files,'file_reference') else decoded.files['indexes/lexical.sqlite'])
        lexical_sha = lexical_index.sha256 if isinstance(lexical_index,RevisionFileReference) else sha256_hex(lexical_index)
        if lexical_meta.get("sha256") != lexical_sha:
            raise ValueError("lexical index checksum does not match index manifest")
        has_snapshot = "query/corpus.json" in decoded.files
        snapshot = json.loads(decoded.files["query/corpus.json"]) if has_snapshot else None
        from jonex_wiki.domain.engine.compile.publication import (
            KEEP_AND_SEARCH_RULE_VERSION,
            RULE_VERSION,
            LEGACY_RULE_VERSION,
            disposition_ledger,
        )
        snapshot_rule = snapshot.get("rule_version") if snapshot else None
        navigation_raw = 'query/navigation.json' in decoded.files
        if navigation_raw:
            navigation = read_json('query/navigation.json')
            if navigation.get("schema_version") not in (1, 2, 3):
                raise ValueError("unsupported query navigation version")
            from jonex_wiki.domain.engine.models.query_models import QueryCorpus, PageIndexEntry
            from jonex_wiki.domain.engine.query.retrieval import (
                build_evidence_fallback,
                page_source_ids_from_markdown,
            )
            wiki_files = {
                path[len("wiki/"):]: decoded.files[path].decode("utf-8")
                for path in decoded.files if path.startswith("wiki/")
            }
            page_index = {
                path: PageIndexEntry.model_validate(value)
                for path, value in navigation.get("page_index", {}).items()
            }
            admitted_claim_ids = {
                claim_id
                for values in navigation.get("claim_ids", {}).values()
                for claim_id in values
            }
            grouped_claims: Dict[str, List[ValidatedClaim]] = {}
            for claim in claims:
                if claim.page_path in page_index and (
                    "claim_ids" not in navigation or claim.claim_id in admitted_claim_ids
                ):
                    grouped_claims.setdefault(claim.page_path, []).append(claim)
            # [jonex] source_records/source_locations 新产物下沉 query/source_index.json；
            # 旧产物仍内嵌在 navigation.json，这里优先读独立文件、回退内嵌字段兼容。
            if "query/source_index.json" in decoded.files:
                source_index = restore_source_index(read_json("query/source_index.json"))
                source_records = source_index["source_records"]
                source_locations = source_index["source_locations"]
            elif "query/evidence.json" in decoded.files:
                # Compatibility with previously published split-evidence bundles.
                evidence_raw = decoded.files["query/evidence.json"]
                evidence = json.loads(evidence_raw.decode("utf-8"))
                metadata = evidence["passage_metadata"]
                source_records = evidence["source_records"]
                source_locations = {
                    path: {
                        **{key: value for key, value in location.items()
                           if key != "metadata_ref"},
                        "metadata": metadata[location["metadata_ref"]],
                    }
                    for path, location in evidence["source_locations"].items()
                }
            else:
                source_records = navigation.get("source_records", {})
                source_locations = navigation.get("source_locations", {})
            corpus = QueryCorpus(
                files=wiki_files,
                revision_id=decoded.revision_id,
                page_index=page_index,
                page_text={path: wiki_files[path] for path in page_index},
                claims=grouped_claims,
                source_records=source_records,
                source_locations={} if not isinstance(source_locations, dict) else source_locations,
                page_number_map=navigation.get("page_number_map", {}),
                evidence_fallback=build_evidence_fallback(
                    {
                        path: page_source_ids_from_markdown(wiki_files[path])
                        for path in page_index
                    },
                    source_records,
                    skip_paths=set(grouped_claims),
                ),
            )
            if not isinstance(source_locations, dict):
                corpus = corpus.model_copy(update={'source_locations':source_locations})
        else:
            # Compatibility adapter for historical single-JSON bundles only.
            corpus = build_query_corpus(state, claims, error_book=error_book if has_snapshot else None,
                                        include_sources=has_snapshot,
                                        source_unit_rule_version=snapshot_rule,
                                        support_rule_version=(
                                            claim_audit.get("support_rule") or "claim-support-v1"
                                        ))
        if has_snapshot:
            if lexical_meta.get("preprocessing") != lexical_preprocessing_identity():
                raise ValueError("LEXICAL_PREPROCESSING_CHANGED: rebuild the published index")
            expected_wiki = {"wiki/" + p for p in corpus.files}
            if {p for p in decoded.files if p.startswith("wiki/")} != expected_wiki:
                raise ValueError("published Wiki file set differs from query snapshot")
            if (snapshot.get("schema_version") not in (1, 2) or snapshot_rule not in (RULE_VERSION, LEGACY_RULE_VERSION, KEEP_AND_SEARCH_RULE_VERSION)
                    or snapshot.get("revision_id") != decoded.revision_id):
                raise ValueError("unsupported query snapshot version")
            if snapshot["schema_version"] == 1:
                corpus_material = corpus.model_dump()
                legacy_corpus_material = {
                    key: value for key, value in corpus_material.items()
                    if key != "revision_id"
                }
                valid_snapshot = snapshot.get("corpus") in (
                    corpus_material, legacy_corpus_material,
                    {**corpus_material, "page_vectors": None},
                    {**legacy_corpus_material, "page_vectors": None},
                )
            else:
                expected_digest = snapshot.get("corpus_sha256")
                # These two projections are loaded only after verification and
                # have their own checksummed artifacts. Pre-review S4-S6 builds
                # included empty placeholders in the corpus hash; BASE_SHA did
                # not. Accept only these enumerated historical empty shapes,
                # without dropping evidence or changing navigation versions.
                valid_snapshot = any(expected_digest == _corpus_sha256(
                    corpus, legacy=legacy, derived_placeholders=fields,
                    retired_vector_placeholder=retired)
                    for legacy in (False, True)
                    for fields in ((), ("page_links",), ("page_links", "fact_mentions"))
                    for retired in (False, True))
            if not valid_snapshot:
                raise ValueError("query snapshot does not match admitted evidence")
            if json.loads(decoded.files["evidence_disposition.json"]) != disposition_ledger(
                state, claims, error_book, rule_version=snapshot_rule
            ):
                raise ValueError("evidence disposition mismatch")
            for path, content in corpus.files.items():
                if decoded.checksums.get("wiki/" + path) != sha256_hex(content.encode("utf-8")):
                    raise ValueError("published Wiki differs from query snapshot")
        if lexical_meta.get("page_count") != len(corpus.page_text):
            raise ValueError("lexical manifest page count does not match Wiki pages")
        # Opening the frozen SQLite bytes proves the index is readable now,
        # before any Revision can point at this bundle.
        from jonex_wiki.domain.engine.query.algorithm import _open_fts_index

        lexical_connection = _open_fts_index(lexical_index)
        try:
            lexical_tables = {
                row[0]
                for row in lexical_connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            indexed_paths = {
                row[0]
                for row in lexical_connection.execute(
                    "SELECT path FROM pages"
                ).fetchall()
            }
            indexed_claims = (
                sorted(
                    (row[0], row[1])
                    for row in lexical_connection.execute(
                        "SELECT page_path, claim_id FROM claims"
                    ).fetchall()
                )
                if "claims" in lexical_tables
                else None
            )
        finally:
            lexical_connection.close()
        expected_claims = sorted(
            (path, claim.claim_id)
            for path, page_claims in corpus.claims.items()
            for claim in page_claims
        )
        if indexed_paths != set(corpus.page_text):
            raise ValueError("lexical index paths do not match published Wiki pages")
        if indexed_claims is not None and indexed_claims != expected_claims:
            raise ValueError("lexical index claims do not match published claims")
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError("malformed previous bundle content") from exc
    # S6.3: attach the audited links last - after every snapshot/digest check
    # has validated the corpus as it was published, so the delivered links never
    # alter what was verified.  Navigation only; no link is evidence.
    from jonex_wiki.domain.engine.compile.identity import build_identity_index
    identity_index = build_identity_index(source_contributions, source_versions=source_contributions.source_versions)
    corpus = corpus.model_copy(update={"fact_mentions": {
        name: identity_index.pages_for(name) for name in sorted(set(identity_index.entity_to_pages) | set(identity_index.topic_to_pages))}})
    if link_increment is not None:
        from jonex_wiki.domain.engine.compile import relation_increment
        published_links: Dict[str, List[Dict[str, Any]]] = {}
        for record in link_increment.links:
            published_links.setdefault(record.page_path, []).append({
                "target_id": record.target_id,
                "why_visit": record.why_visit,
                "kind": record.kind,
            })
        # R2.1 counters at the publication boundary (one pass per compile), so
        # "links grew" is attributable to navigation, supported relations or
        # unresolved targets instead of counting internal decision passes.
        from jonex_wiki.kernel.telemetry import count_work
        count_work("compile_links_navigation", sum(
            1 for record in link_increment.links
            if record.kind == relation_increment.KIND_NAVIGATION))
        count_work("compile_links_supported", sum(
            1 for record in link_increment.links
            if record.kind == relation_increment.KIND_SUPPORTED))
        count_work("compile_links_pending", len(link_increment.pending))
        if link_increment.links:
            count_work("compile_links_serialized_characters", len(json.dumps(
                sorted(published_links.items()), ensure_ascii=False,
                sort_keys=True, separators=(",", ":"))))
        corpus = corpus.model_copy(update={
            "page_links": {path: items for path, items in sorted(published_links.items())}})
    reading_links = (read_json("indexes/links.json")
                     if "indexes/links.json" in decoded.files and navigation_raw else None)
    return PreviousBundle(
        state=state,
        claims=claims,
        error_book=error_book,
        digest_map=digest_map,
        source_digests=source_digests,
        manifest=manifest,
        revision_id=decoded.revision_id,
        tenant_id=tenant_id,
        context_id=context_id,
        source_inventory=source_inventory,
        effective_runtime=effective_runtime,
        runtime_snapshot_checksum=runtime_snapshot_checksum,
        lexical_index=lexical_index,
        index_manifest=index_manifest,
        corpus=corpus,
        source_contributions=source_contributions,
        link_increment=link_increment,
        json_shards=json_shards,
        navigation=navigation if navigation_raw else None,
        relation_payload=raw_relations,
        source_unit_rule_version=snapshot_rule if lexical_index is not None else None,
        render_signatures=(
            _string_map(json.loads(decoded.files["indexes/render_signatures.json"])["signatures"],
                        "render signatures")
            if "indexes/render_signatures.json" in decoded.files else None
        ),
        reading_index=(
            {"page_graph": navigation.get("page_graph", {}),
             "in_links": reading_links.get("in_links", {}),
             "mentions": reading_links.get("mentions", {}),
             "mention_filters": reading_links.get("mention_filters", {})}
            if reading_links is not None else None
        ),
    )


def _string_map(value: Any, name: str) -> Dict[str, str]:
    """Validate a JSON object whose keys and values are all strings."""
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in value.items()
    ):
        raise ValueError(f"previous bundle {name} must be a string map")
    return dict(value)
