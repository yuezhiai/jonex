"""Schedule related source topics together using locatable discovery leads."""

from __future__ import annotations

from collections import defaultdict
from heapq import heapify, heappop, heappush
import json
import re
import unicodedata


def _name_tokens(value):
    letters = unicodedata.normalize("NFKD", value.casefold())
    letters = "".join(c for c in letters if not unicodedata.combining(c))
    return tuple(re.findall(r"[^\W_]+", letters))


def _related_topics(topics, source_topics, claim_index):
    names = defaultdict(set)
    shorter = defaultdict(set)
    for number, title in enumerate(topics):
        label = title.partition(" — ")[0].partition(" · ")[0]
        words = _name_tokens(label)
        names[words].add(number)
        for length in range(2, len(words)):
            shorter[words[:length]].add(number)
            shorter[words[-length:]].add(number)

    def topic_for_name(name):
        if not isinstance(name, str) or not name.strip():
            return None
        for variant in dict.fromkeys((name, name.partition(",")[0])):
            words = _name_tokens(variant)
            matches = names.get(words) or shorter.get(words)
            if matches:
                return next(iter(matches)) if len(matches) == 1 else None
        return None

    neighbors = defaultdict(set)
    with claim_index.open() as stream:
        for line in stream:
            record = json.loads(line)
            leads = record.get("relation_leads")
            if not leads or record.get("support_status") != "SUPPORTED":
                continue
            owners = set()
            for span in record.get("source_locations", []):
                if span.get("status") == "located":
                    owners.update(
                        source_topics.get(span.get("unit_path"))
                        or source_topics.get(span.get("source_path"))
                        or ()
                    )
            if len(owners) != 1:
                continue
            owner = next(iter(owners))
            for lead in leads:
                for field in ("subject", "target"):
                    peer = topic_for_name(lead.get(field))
                    if peer is not None and peer != owner:
                        neighbors[owner].add(peer)
                        neighbors[peer].add(owner)
    return neighbors


def _connected_topics(count, neighbors):
    seen = set()
    for number in range(count):
        if number in seen:
            continue
        group = [number]
        seen.add(number)
        for current in group:
            for peer in sorted(neighbors.get(current, ())):
                if peer not in seen:
                    seen.add(peer)
                    group.append(peer)
        yield group


def group_related_topics(topics, source_topics, claim_index, size):
    """Keep connected topics close while retaining every seed and batch slot."""
    sequential = [topics[start : start + size] for start in range(0, len(topics), size)]
    if len(sequential) < 2 or not claim_index.is_file():
        return sequential
    try:
        neighbors = _related_topics(topics, source_topics, claim_index)
    except (OSError, ValueError):
        return sequential
    if not neighbors:
        return sequential

    pieces = [
        group[start : start + size]
        for group in _connected_topics(len(topics), neighbors)
        for start in range(0, len(group), size)
    ]
    batches = [[] for _ in sequential]
    available = [(0, index) for index in range(len(batches))]
    heapify(available)
    for group in sorted(pieces, key=lambda part: -len(part)):
        while group:
            used, index = heappop(available)
            n = size - used
            batches[index].extend(topics[number] for number in group[:n])
            group = group[n:]
            filled = len(batches[index])
            if filled < size:
                heappush(available, (filled, index))
    return batches
