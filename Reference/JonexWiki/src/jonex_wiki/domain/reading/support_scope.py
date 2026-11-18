"""Source constraints over alternative, jointly required evidence groups.

Absent an explicit alternative declaration, all stored source spans form one
conservative joint group. A name match never creates a new support group.
"""
from __future__ import annotations


def support_groups(record):
    alternatives = record.get('support_alternatives')
    if alternatives is None:
        refs = record.get('source_spans') or []
        return [refs] if refs else []
    return [group['all_of'] for group in alternatives
            if isinstance(group, dict) and isinstance(group.get('all_of'), list) and group['all_of']]


def source_of(ref):
    if isinstance(ref, dict):
        return ref.get('source_id') or (ref.get('source_locator') or {}).get('source_id')
    if isinstance(ref, str) and '/' in ref:
        return ref.split('/', 1)[0]
    return None


def legal_support(record, source_ids=None):
    """Return complete legal groups, retaining joint requirements within each."""
    allowed = set(source_ids) if source_ids is not None else None
    groups = []
    for refs in support_groups(record):
        sources = [source_of(ref) for ref in refs]
        if all(sources) and (allowed is None or set(sources).issubset(allowed)):
            groups.append(refs)
    return groups
