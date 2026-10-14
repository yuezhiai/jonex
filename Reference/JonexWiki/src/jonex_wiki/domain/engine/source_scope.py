"""Source-bound reading hints. Labels never become a fact's effective period."""
import re
from collections import defaultdict

_PERIOD = re.compile(r"(?<!\d)(?:19|20)\d{2}(?:[-_/ ]?Q[1-4]|[-/]\d{1,2}[-/]\d{1,2})?", re.I)
_SUBJECT = re.compile(r"(?:^|[.!?]\s+)([A-Z][A-Za-z0-9 ,&().'\-]{1,100}?)\s+(?:is|was)\s+(?:a|an|the)\s")
_OVERVIEW = re.compile(r"\b(?:overview|summary|introduction)\b|概述|简介|概况|总览|摘要", re.I)


def is_overview(title):
    return bool(_OVERVIEW.search(title))


def build_source_scopes(archive, claims):
    """One linear pass over sources and supported text; no new semantic assertions."""
    scopes = {}
    subjects, periods = defaultdict(set), defaultdict(set)
    for passage in archive.values():
        sid = passage.source_id
        metadata = passage.source_metadata or {}
        if sid not in scopes:
            name = str(metadata.get('source_name') or passage.document_id)
            scopes[sid] = {
                'source_id': sid, 'document_id': passage.document_id, 'source_name': name,
                'source_version': {key: metadata.get(key) for key in (
                    'content_generation', 'parser_id', 'parser_version')},
                'source_sha256': passage.sha256,
                'source_period_labels': sorted(set(_PERIOD.findall(name))),
                'period_basis': 'source_label_only; not a fact effective period',
            }
        for key in ('subject', 'entity_name', 'organization', 'company'):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                subjects[sid].add((value.strip(), 'source_metadata'))
    for claim in claims:
        if claim.support_status != 'SUPPORTED' or claim.unresolved_evidence:
            continue
        text = re.sub(r'^\s*[#*\-]+\s*', '', claim.text)
        names = _SUBJECT.findall(text[:1500])
        mentions = set(_PERIOD.findall(text))
        for sid in {ref.source_id for ref in claim.evidence}:
            periods[sid].update(mentions)
            subjects[sid].update((name.strip(), 'supported_clause_mention') for name in names)
    for sid, scope in scopes.items():
        scope['subject_mentions'] = [{'name': name, 'basis': basis}
                                     for name, basis in sorted(subjects[sid])[:16]]
        scope['period_mentions'] = sorted(periods[sid])
    return scopes


def attach_source_scopes(pages, scopes):
    for path, page in pages.items():
        sources = [scopes[sid] for sid in sorted(set(page.source_ids)) if sid in scopes]
        if not sources:
            continue
        scope = dict(page.index.scope)
        # Keep common context once in navigation, instead of once per page.
        scope['source_ids'] = [item['source_id'] for item in sources]
        if path.startswith('pages/'):
            scope['page_role'] = 'overview' if is_overview(page.index.title) else 'detail'
        pages[path] = page.model_copy(update={'index': page.index.model_copy(update={'scope': scope})})
