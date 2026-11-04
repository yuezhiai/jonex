"""Runner-selected retrieval policy; natural-language intent is not a regex.

The default is the general proposition path. Legacy explicit route names are
retained for callers that deliberately select and label another policy.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional


class Route(str, Enum):
    """The four LWP-RET-002 long-document routing intents."""

    PROPOSITION = "proposition"
    LEXICAL = "lexical"
    CHAPTER_TREE = "chapter_tree"
    SUMMARY = "summary"


def _normalize_route(value: object) -> Route:
    """Coerce a str/Route to ``Route``, failing closed on unknown values."""
    if isinstance(value, Route):
        return value
    if isinstance(value, str):
        try:
            return Route(value.strip().lower())
        except ValueError:
            raise ValueError(
                "UNKNOWN_ROUTE: {!r}; known routes: {}".format(
                    value, [r.value for r in Route]
                )
            ) from None
    raise ValueError(
        "UNKNOWN_ROUTE: {!r}; known routes: {}".format(value, [r.value for r in Route])
    )


def resolve_route(question: str, explicit: Optional[object] = None) -> Route:
    """Use a runner-selected policy; free-text keywords never choose intent.

    Auto/default keeps the general retrieval path. An explicit route remains
    available to trusted callers for separately labelled experiments.
    """
    if explicit is not None and not (isinstance(explicit, str) and explicit.strip().casefold() == "auto"):
        return _normalize_route(explicit)
    return Route.PROPOSITION


__all__ = [
    "Route",
    "resolve_route",
]
