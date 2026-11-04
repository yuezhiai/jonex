"""Compatibility boundary for the retired natural-language lookup shortcut.

A full scan proves execution coverage for supplied predicates, never that a
keyword parser understood every condition in a question. Use table_compute or
lookup_records with explicit fields, operators and Boolean branches instead.
"""
from __future__ import annotations


def exact_identifier_lookup(connection, table_id: str, question: str) -> None:
    """Decline free-text execution; never return an inferred complete answer."""
    return None


def render_identifier_lookup(result: dict) -> str:
    """Reject stale inferred results rather than presenting them as answers."""
    raise ValueError("UNVERIFIED_NATURAL_LANGUAGE_LOOKUP_RETIRED")
