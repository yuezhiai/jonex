"""Bounded Wiki graph reads, shared by web routes and MCP capability actions."""
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class WikiGraphRequest(BaseModel):
    knowledge_base_id: str = Field(..., min_length=1, max_length=128)
    graph_revision: str = Field(default="", max_length=64)
    document_id: str = Field(default="", max_length=128)
    query: str = Field(default="", max_length=500)
    node_type: str = Field(default="", max_length=32)
    node_id: str = Field(default="", max_length=64)
    asset_id: str = Field(default="", max_length=64)
    include_content: bool = False
    edge_id: str = Field(default="", max_length=64)
    evidence_id: str = Field(default="", max_length=64)
    direction: Literal["in", "out", "both"] = "both"
    families: Optional[List[Literal["semantic", "navigation", "provenance"]]] = None
    limit: int = Field(default=20, ge=1, le=100)
    cursor: str = Field(default="", max_length=2048)


GRAPH_FIELDS = {
    "media": ("graph_revision", "document_id", "asset_id", "include_content"),
    "types": ("graph_revision", "document_id"),
    "search": ("graph_revision", "document_id", "query", "node_type", "limit", "cursor"),
    "node": ("graph_revision", "document_id", "node_id", "include_content"),
    "neighbors": ("graph_revision", "document_id", "node_id", "direction", "families", "limit", "cursor"),
    "evidence": ("graph_revision", "document_id", "node_id", "edge_id", "evidence_id", "limit", "cursor"),
}

GRAPH_ACTIONS = {
    "get_wiki_media": "media",
    "get_wiki_graph_types": "types", "search_wiki_nodes": "search",
    "get_wiki_node": "node", "get_wiki_neighbors": "neighbors",
    "get_wiki_graph_evidence": "evidence",
}
