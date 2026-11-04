"""Conservative occurrence matching and revalidation for changed documents."""
from collections import defaultdict, deque

from jonex_wiki.domain.engine.compile.content_validation import ClaimDraft, validate_claim_evidence
from jonex_wiki.domain.engine.compile.digest import semantic_metadata
from jonex_wiki.kernel.telemetry import count_work
from jonex_wiki.domain.engine.compile.pipeline import artifact_to_passage, canonical_json

# These describe the container or physical location, not the block's meaning.
_LOCATION_KEYS = {'content_generation', 'position', 'page_number', 'source_location',
                  'source_locations', 'snapshot_text', 'file_name', 'source_name',
                  'char_start', 'char_end', 'sha256', 'original_blob', 'original_sha256',
                  'parse_input_signature', 'parse_input_components'}


def dependency_key(p):
    metadata = semantic_metadata(p.source_metadata)
    metadata = {k: v for k, v in metadata.items() if k not in _LOCATION_KEYS}
    # Source display names are carried in the reading catalog as well. A
    # display-only rename changes publication labels, not the passage meaning.
    catalog = metadata.get('source_reading_materials')
    if isinstance(catalog, dict):
        catalog = {**catalog}
        identity = catalog.get('source_identity')
        if isinstance(identity, dict):
            catalog['source_identity'] = {k: v for k, v in identity.items()
                                          if k != 'source_name'}
        catalog['units'] = [
            {k: v for k, v in unit.items() if k != 'name'}
            if isinstance(unit, dict) and unit.get('kind') in {'document', 'workbook'}
            else unit
            for unit in catalog.get('units', [])
        ]
        metadata['source_reading_materials'] = catalog
    return (p.source_id, p.document_id, p.text, canonical_json(metadata), p.context_text)


def replay_blocks(state, claims, artifacts):
    """Match duplicate occurrences one-to-one; never match on title alone.

    Retained claims are freshly bound and validated against the new archive.
    Draft page prose is discarded on a source change: published facts are the
    source-grounded claims, not an old model-written paragraph.
    """
    pools = defaultdict(deque)
    processed = set(state.processed_passage_ids)
    old_archive = state.source_archive
    for pid, p in old_archive.items():
        if pid in processed:
            pools[dependency_key(p)].append(pid)
    archive, remap, order = {}, {}, []
    for artifact in artifacts:
        p = artifact_to_passage(artifact)
        if p.passage_id in archive:
            raise ValueError('DUPLICATE_PASSAGE_ID: parser occurrences must be distinct')
        archive[p.passage_id] = p
        pool = pools[dependency_key(p)]
        if pool:
            remap[pool.popleft()] = p.passage_id
        else:
            order.append(p.passage_id)
    state.recovered_retries = {remap[pid]: value for pid, value in state.recovered_retries.items() if pid in remap and old_archive[pid].sha256 == archive[remap[pid]].sha256}
    state.source_archive = archive
    state.processed_passage_ids = list(remap.values())
    drafts = []
    for claim in claims:
        refs = []
        for ref in claim.evidence:
            pid = remap.get(ref.passage_id)
            if pid is not None:
                p = archive[pid]
                refs.append(ref.model_copy(update=dict(passage_id=pid, source_id=p.source_id,
                    document_id=p.document_id, chunk_id=p.chunk_id)))
        if refs:
            drafts.append(ClaimDraft(page_path=claim.page_path, text=claim.text, evidence=refs))
    validated = validate_claim_evidence(state, drafts).validated_claims
    # A shared-source claim survives deletion only if remaining evidence still
    # supports it. New source spans are produced by validation, never copied.
    retained = [c for c in validated if c.support_status == 'SUPPORTED' and not c.unresolved_evidence]
    by_page = defaultdict(list)
    for c in retained:
        by_page[c.page_path].append(c)
    for path in list(state.pages):
        page_claims = by_page.get(path, [])
        if not page_claims:
            state.pages.pop(path)
            state.index.pop(path, None)
        else:
            page = state.pages[path]
            state.pages[path] = page.model_copy(update={
                'content': '\n\n'.join(c.text for c in page_claims),
                'source_ids': sorted({r.passage_id for c in page_claims for r in c.evidence}),
                'related_pages': [], 'links': []})
    state._publication = None
    state._rendered_wiki_files = None
    claims[:] = retained
    count_work('compiled_blocks_reused', len(remap))
    count_work('compiled_blocks_invalidated', len(processed) - len(remap))
    return order
