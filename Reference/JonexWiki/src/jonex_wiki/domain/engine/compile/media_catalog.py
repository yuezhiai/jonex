"""Compatibility facade for the source-only image publication."""
from .image_catalog import PATH, RULE, build_catalog, raw_items
from jonex_wiki.domain.image_identity import identity as _id

__all__ = ["PATH", "RULE", "build_catalog", "raw_items"]
