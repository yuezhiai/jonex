"""文档级双向血缘图装配。

方向语义（以文档为中心）：
- ``up``   数据源 → 文档（``ingested_from``，边方向与数据流一致）
- ``down`` 文档 → 本体实体/关系（``produces_entity`` / ``relation``）
           文档 → 检索引用聚合（``cited_by``）
           depth=2 时再展开 文档 → chunk（``contains``）

图只做只读投影：节点按 ``id`` 去重保序，返回前不混入目录详情的质量档位等字段。
所有来源调用并发执行，单个来源失败只让该段为空，整体不 500。
"""
from __future__ import annotations

import asyncio
import logging

import httpx
from sqlalchemy.exc import SQLAlchemyError

from jonex_core.common.exceptions import InvalidParameterError, JonexException

from .lineage_sources import CITATION_WINDOW_DAYS

logger = logging.getLogger(__name__)

ENTITY_LIMIT = 100
CHUNK_LIMIT = 50

_DIRECTIONS = frozenset({"up", "down", "both"})

# 只允许「上游不可用」类异常被降级；本侧编程错误继续上抛。
# httpx 异常（连不上 / 非 2xx）同样属于上游不可用。
_DEGRADE_EXCEPTIONS = (
    JonexException,
    asyncio.TimeoutError,
    OSError,
    SQLAlchemyError,
    httpx.HTTPError,
)

_EMPTY_PROVENANCE = {
    "entities": [],
    "relations": [],
    "entities_truncated": False,
    "relations_truncated": False,
}
_EMPTY_CHUNKS = {"total": 0, "chunks": []}
_EMPTY_CITATIONS = {"count": 0, "last_seen_at": None}


def _normalize_direction(value) -> str:
    if value is None:
        return "both"
    if not isinstance(value, str):
        raise InvalidParameterError(
            message="direction 必须是 up/down/both",
            details={"direction": value},
        )
    direction = value.strip().lower()
    if direction not in _DIRECTIONS:
        raise InvalidParameterError(
            message="direction 必须是 up/down/both",
            details={"direction": value},
        )
    return direction


def _normalize_depth(value) -> int:
    if value is None:
        return 1
    # bool 是 int 的子类，True/False 必须显式拒绝。
    if isinstance(value, bool) or not isinstance(value, int) or value not in (1, 2):
        raise InvalidParameterError(
            message="depth 必须是 1 或 2",
            details={"depth": value},
        )
    return value


async def _degrade(
    coro, default, what: str, *, tenant_id=None, kb_id=None, doc_id=None,
):
    """等待来源协程；上游不可用 → default + warning。"""
    try:
        return await coro
    except _DEGRADE_EXCEPTIONS:
        logger.warning(
            "lineage source degraded: %s tenant=%s kb=%s doc=%s",
            what, tenant_id, kb_id, doc_id,
            exc_info=True,
        )
        return default


def _text(value) -> str:
    return str(value or "").strip()


def _chunk_key(chunk: dict, index: int):
    for key in ("chunk_id", "id", "chunk_index"):
        value = chunk.get(key)
        if value is not None and value != "":
            return value
    return index


async def build_graph(
    *, asset: dict, direction: str = "both", depth: int = 1, sources,
) -> dict:
    """装配文档血缘图。

    `asset` 是目录详情（`CatalogService.get_asset(..., include_quality=False)`）。
    `sources` 需提供 `data_source_for_doc / provenance / chunks / citations`。
    """
    asset = asset or {}
    asset_type = asset.get("asset_type")
    if asset_type != "document":
        raise InvalidParameterError(
            message="仅文档资产支持血缘",
            details={"asset_type": asset_type},
        )

    direction = _normalize_direction(direction)
    depth = _normalize_depth(depth)

    doc_id = _text(asset.get("asset_id"))
    kb_id = _text(asset.get("kb_id"))
    tenant_id = getattr(sources, "_tenant_id", None)

    # 先建协程再并发等待；up / down 互不依赖。
    calls: list[tuple[str, object]] = []
    if direction in ("up", "both"):
        calls.append((
            "data_source",
            _degrade(
                sources.data_source_for_doc(kb_id=kb_id, doc_id=doc_id),
                None,
                "data_source",
                tenant_id=tenant_id, kb_id=kb_id, doc_id=doc_id,
            ),
        ))
    if direction in ("down", "both"):
        calls.append((
            "provenance",
            _degrade(
                sources.provenance(kb_id=kb_id, doc_id=doc_id, limit=ENTITY_LIMIT),
                _EMPTY_PROVENANCE,
                "provenance",
                tenant_id=tenant_id, kb_id=kb_id, doc_id=doc_id,
            ),
        ))
        calls.append((
            "citations",
            _degrade(
                sources.citations(kb_id=kb_id, doc_id=doc_id),
                _EMPTY_CITATIONS,
                "citations",
                tenant_id=tenant_id, kb_id=kb_id, doc_id=doc_id,
            ),
        ))
        if depth == 2:
            calls.append((
                "chunks",
                _degrade(
                    sources.chunks(kb_id=kb_id, doc_id=doc_id, limit=CHUNK_LIMIT),
                    _EMPTY_CHUNKS,
                    "chunks",
                    tenant_id=tenant_id, kb_id=kb_id, doc_id=doc_id,
                ),
            ))

    results = await asyncio.gather(*(coro for _, coro in calls))
    by_name = {name: result for (name, _), result in zip(calls, results)}

    nodes: list[dict] = []
    edges: list[dict] = []
    seen: dict[str, dict] = {}

    def add_node(node: dict) -> dict:
        existing = seen.get(node["id"])
        if existing is None:
            seen[node["id"]] = node
            nodes.append(node)
            return node
        return existing

    def add_edge(source: str, target: str, edge_type: str, meta: dict | None = None):
        edges.append({
            "source": source,
            "target": target,
            "type": edge_type,
            "meta": meta or {},
        })

    doc_node_id = f"document:{doc_id}"
    doc_meta = {}
    if kb_id:
        doc_meta["kb_id"] = kb_id
    if asset.get("kb_name"):
        doc_meta["kb_name"] = asset["kb_name"]
    add_node({
        "id": doc_node_id,
        "type": "document",
        "label": asset.get("name") or doc_id,
        "meta": doc_meta,
    })

    # ── up：数据源 ──
    data_source = by_name.get("data_source")
    if isinstance(data_source, dict) and data_source.get("id"):
        source_id = f"data_source:{data_source['id']}"
        add_node({
            "id": source_id,
            "type": "data_source",
            "label": data_source.get("name") or data_source["id"],
            "meta": {},
        })
        add_edge(source_id, doc_node_id, "ingested_from")

    # ── down：本体实体 / 关系 ──
    provenance = by_name.get("provenance") or _EMPTY_PROVENANCE
    entities = provenance.get("entities") or []
    relations = provenance.get("relations") or []
    # counts 用实际渲染数：跳过空 canonical_name / 非法关系后的条数。
    rendered_entities = 0
    rendered_relations = 0

    for entity in entities:
        name = _text(entity.get("canonical_name"))
        if not name:
            continue
        rendered_entities += 1
        entity_id = f"entity:{name}"
        add_node({
            "id": entity_id,
            "type": "ontology_entity",
            "label": name,
            "meta": {"entity_type": entity.get("entity_type")},
        })
        add_edge(
            doc_node_id, entity_id, "produces_entity",
            {"entity_type": entity.get("entity_type")},
        )

    for relation in relations:
        source_name = _text(relation.get("source"))
        target_name = _text(relation.get("target"))
        if not source_name or not target_name:
            continue
        rendered_relations += 1
        source_id = f"entity:{source_name}"
        target_id = f"entity:{target_name}"
        # 关系端点未出现在实体列表时补最小节点（不带 entity_type）。
        if source_id not in seen:
            add_node({
                "id": source_id,
                "type": "ontology_entity",
                "label": source_name,
                "meta": {},
            })
        if target_id not in seen:
            add_node({
                "id": target_id,
                "type": "ontology_entity",
                "label": target_name,
                "meta": {},
            })
        add_edge(
            source_id, target_id, "relation",
            {"relation_type": relation.get("relation_type")},
        )

    # ── down：检索引用聚合（只回聚合值，不含查询文本/用户身份）──
    citations = by_name.get("citations") or _EMPTY_CITATIONS
    citation_count = int(citations.get("count") or 0)
    last_seen_at = citations.get("last_seen_at")
    if citation_count > 0 or last_seen_at:
        citation_id = f"citation:{doc_id}"
        add_node({
            "id": citation_id,
            "type": "search_citation",
            "label": "检索引用",
            "meta": {
                "count": citation_count,
                "last_seen_at": last_seen_at,
                "window_days": CITATION_WINDOW_DAYS,
            },
        })
        add_edge(doc_node_id, citation_id, "cited_by")

    # ── down depth=2：chunk ──
    chunks_total = None
    chunks_truncated = False
    if depth == 2:
        chunk_data = by_name.get("chunks") or _EMPTY_CHUNKS
        chunks = chunk_data.get("chunks") or []
        chunks_total = chunk_data.get("total")
        if chunks_total is None:
            chunks_total = len(chunks)
        chunks_total = int(chunks_total)
        # 截断判据只看上游 total，不依赖「多取 1 条」的隐式约定。
        chunks_truncated = chunks_total > CHUNK_LIMIT
        for index, chunk in enumerate(chunks[:CHUNK_LIMIT]):
            key = _chunk_key(chunk, index)
            chunk_id = f"chunk:{key}"
            add_node({
                "id": chunk_id,
                "type": "chunk",
                "label": str(key),
                "meta": {"chunk_index": chunk.get("chunk_index", index)},
            })
            add_edge(doc_node_id, chunk_id, "contains")

    return {
        "asset": {
            "asset_type": "document",
            "asset_id": doc_id,
            "name": asset.get("name"),
        },
        "nodes": nodes,
        "edges": edges,
        "counts": {
            "chunks": chunks_total,
            "entities": rendered_entities,
            "relations": rendered_relations,
            "citations": citation_count,
        },
        "truncated": {
            "entities": bool(provenance.get("entities_truncated")),
            "relations": bool(provenance.get("relations_truncated")),
            "chunks": chunks_truncated,
        },
    }
