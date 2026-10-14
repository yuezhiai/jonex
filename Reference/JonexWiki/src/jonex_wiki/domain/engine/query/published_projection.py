"""Query-only preparation of an immutable, already published directory."""
from collections.abc import Mapping
from dataclasses import dataclass
import json

from jonex_wiki.domain.engine.models.compiler_state import CompilerState
from jonex_wiki.domain.engine.models.query_models import QueryCorpus, PageIndexEntry
from jonex_wiki.domain.engine.compile.content_validation import ValidatedClaim
from jonex_wiki.domain.engine.query.retrieval import build_evidence_fallback
from jonex_wiki.domain.engine.json_artifacts import load_json_artifact
from jonex_wiki.domain.engine.source_index import restore_source_index


@dataclass(frozen=True)
class PublishedTexts(Mapping):
    """No retained body cache: the per-query read path owns selected material."""
    view: object
    paths: frozenset

    def __getitem__(self, path):
        if path not in self.paths:
            raise KeyError(path)
        return self.view.read_bytes("wiki/" + path).decode("utf-8")

    def __iter__(self):
        return iter(sorted(self.paths))

    def __len__(self):
        return len(self.paths)


@dataclass
class PublishedQueryRevision:
    revision_id: str
    tenant_id: str
    context_id: str
    manifest: dict
    corpus: QueryCorpus
    state: CompilerState
    claims: list
    lexical_index: object = None


def load_query_projection(view, navigation):
    """Read checksummed query artifacts; do not reconstruct compiler history.

    Full semantic validation belongs to publication. This reader retains
    owner/revision checks and each selected artifact's manifest checksum.
    Historical bundles without this projection use the existing decoder.
    """
    def read_json(path):
        return json.loads(view.read_bytes(path))

    if '_jonex_json_shards' in navigation:
        navigation=load_json_artifact(view.read_bytes,'query/navigation.json',
            lazy_fields={'page_graph','page_links','fact_mentions','page_source_ids'})

    provenance = read_json("provenance.json")
    if (provenance.get("tenant_id"), provenance.get("context_id"),
            provenance.get("revision_id")) != (view.tenant_id, view.context_id, view.revision_id):
        raise ValueError("query projection owner/revision mismatch")
    if navigation.get("revision_id") != view.revision_id:
        raise ValueError("query navigation revision mismatch")
    page_index = {path: PageIndexEntry.model_validate(item)
                  for path, item in navigation["page_index"].items()}
    files = PublishedTexts(view, frozenset(path[5:] for path in view.list_entries("wiki/")))
    if not set(page_index).issubset(files.paths):
        raise ValueError("query navigation names missing Wiki pages")
    admitted = {cid for ids in navigation["claim_ids"].values() for cid in ids}
    grouped = {}
    for line in view.read_bytes("claims.jsonl").splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        if raw.get("claim_id") in admitted:
            claim = ValidatedClaim.model_validate(raw)
            if claim.page_path not in page_index:
                raise ValueError("query claim names a missing page")
            grouped.setdefault(claim.page_path, []).append(claim)
    if admitted != {claim.claim_id for claims in grouped.values() for claim in claims}:
        raise ValueError("query navigation claim missing from publication")
    source_index = restore_source_index(load_json_artifact(view.read_bytes, "query/source_index.json",
        lazy_fields={'source_records','source_locations','passage_metadata'}))
    records = source_index["source_records"]
    # Typed metadata is validated above. model_construct preserves the lazy
    # text mappings rather than making Pydantic eagerly read every body.
    corpus = QueryCorpus.model_construct(
        files=files, page_text=PublishedTexts(view, frozenset(page_index)),
        revision_id=view.revision_id, page_index=page_index, claims=grouped,
        source_records=records, source_locations=source_index["source_locations"],
        page_number_map=navigation.get("page_number_map", {}),
        page_links=navigation.get("page_links", {}),
        source_scopes=navigation.get("source_scopes", {}),
        fact_mentions=navigation.get("fact_mentions", {}),
        evidence_fallback=(navigation['evidence_fallback'] if 'evidence_fallback' in navigation
            else build_evidence_fallback(navigation["page_source_ids"], records, skip_paths=set(grouped))),
    )
    runtime = provenance["effective_runtime"]
    return PublishedQueryRevision(
        view.revision_id, view.tenant_id, view.context_id,
        {"models": runtime["models"]}, corpus, CompilerState(), [],
    )
