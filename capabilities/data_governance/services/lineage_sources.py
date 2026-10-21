"""文档级血缘的真实来源：数据源绑定、本体溯源、chunk、检索引用。

每个方法都**自行降级** —— 血缘是只读增强视图，任何单一来源不可用
（Neo4j 抖动、KB 超时、DB 瞬态错误）都不应把整张图变成 500。
降级捕获范围只覆盖「上游不可用」类异常；`AttributeError / TypeError`
这类本侧编程错误继续向上抛，避免把 bug 伪装成空结果。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import httpx
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import JonexException
from jonex_core.common.tenant import require_tenant

from ..clients.knowledge_base_client import get_knowledge_base_client

logger = logging.getLogger(__name__)

_PROVENANCE_TIMEOUT_SECONDS = 10
_CHUNKS_TIMEOUT_SECONDS = 15

#: 引用聚合回看窗口（天），SQL 与血缘 meta 共用同一来源。
CITATION_WINDOW_DAYS = 30

# 文档 → 数据源绑定：extra_metadata.data_source_id 同时受租户/KB 约束，
# 避免同 id 的跨租户数据源被误连。
_DATA_SOURCE_SQL = """
SELECT s.id, s.name
FROM knowledge_base.knowledge_documents d
JOIN knowledge_base.knowledge_data_sources s
  ON s.id = d.extra_metadata->>'data_source_id'
 AND s.tenant_id = d.tenant_id
 AND s.knowledge_base_id = d.knowledge_base_id
 AND s.is_deleted = 0
WHERE d.id = :doc_id
  AND d.tenant_id = :tenant_id
  AND d.knowledge_base_id = :kb_id
  AND d.is_deleted = 0
LIMIT 1
"""

# 引用聚合：只看最近 30 天、最多 1000 条历史，展开 references JSONB 后数 doc_id。
# 只回计数与最后出现时间 —— 查询文本、用户身份一律不出库。
# ⚠️ `references` 是 PostgreSQL 保留字：CTE 内的裸列名必须加双引号，否则每次
# 都报语法错、被 SQLAlchemyError 静默降级成「引用恒为空」。外层 `r.references`
# 是限定列名，合法，不用加引号。
_CITATIONS_SQL = """
WITH recent AS (
    SELECT "references", searched_at
    FROM knowledge_base.knowledge_search_history
    WHERE tenant_id = :tenant_id
      AND knowledge_base_id = :kb_id
      AND is_deleted = 0
      AND searched_at >= now() - make_interval(days => :window_days)
    ORDER BY searched_at DESC
    LIMIT 1000
)
SELECT count(*) AS cnt, max(searched_at) AS last_seen
FROM recent r, jsonb_array_elements(coalesce(r.references, '[]'::jsonb)) ref
WHERE ref->>'doc_id' = :doc_id
"""

# httpx 异常（连接失败 / 非 2xx）不是 JonexException，不加入降级集合会把整张图打成 500。
_DEGRADE_EXCEPTIONS = (
    JonexException,
    asyncio.TimeoutError,
    OSError,
    httpx.HTTPError,
)


_DB_DEGRADE_EXCEPTIONS = (SQLAlchemyError, OSError, httpx.HTTPError)


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class LineageSources:
    """单个租户的血缘来源集合（构造即校验租户）。"""

    def __init__(self, tenant_id: str):
        self._tenant_id = require_tenant(tenant_id)

    async def data_source_for_doc(self, *, kb_id: str, doc_id: str) -> dict | None:
        """文档的入库数据源绑定；无绑定或查询失败一律 None。"""
        try:
            async with get_db_session() as session:
                row = (
                    await session.execute(
                        text(_DATA_SOURCE_SQL),
                        {
                            "tenant_id": self._tenant_id,
                            "kb_id": kb_id,
                            "doc_id": doc_id,
                        },
                    )
                ).mappings().first()
        except _DB_DEGRADE_EXCEPTIONS:
            logger.warning(
                "lineage data_source lookup failed: tenant=%s kb=%s doc=%s",
                self._tenant_id, kb_id, doc_id,
                exc_info=True,
            )
            return None
        if not row:
            return None
        return {"id": row["id"], "name": row["name"]}

    async def provenance(self, *, kb_id: str, doc_id: str, limit: int) -> dict:
        """文档产出的本体实体/关系；超时或上游失败 → 空结果。"""
        try:
            data = await asyncio.wait_for(
                get_knowledge_base_client().invoke(
                    "get_document_provenance",
                    {
                        "document_id": doc_id,
                        "knowledge_base_id": kb_id,
                        "limit": limit,
                    },
                    self._tenant_id,
                ),
                timeout=_PROVENANCE_TIMEOUT_SECONDS,
            )
        except _DEGRADE_EXCEPTIONS:
            logger.warning(
                "lineage provenance degraded: tenant=%s kb=%s doc=%s",
                self._tenant_id, kb_id, doc_id,
                exc_info=True,
            )
            return {
                "entities": [],
                "relations": [],
                "entities_truncated": False,
                "relations_truncated": False,
            }
        data = data or {}
        return {
            "entities": data.get("entities") or [],
            "relations": data.get("relations") or [],
            "entities_truncated": bool(data.get("entities_truncated")),
            "relations_truncated": bool(data.get("relations_truncated")),
        }

    async def chunks(self, *, kb_id: str, doc_id: str, limit: int) -> dict:
        """文档 chunk 列表；多取 1 条用于判断截断。"""
        try:
            data = await asyncio.wait_for(
                get_knowledge_base_client().invoke(
                    "get_document_chunks",
                    {"document_id": doc_id},
                    self._tenant_id,
                ),
                timeout=_CHUNKS_TIMEOUT_SECONDS,
            )
        except _DEGRADE_EXCEPTIONS:
            logger.warning(
                "lineage chunks degraded: tenant=%s kb=%s doc=%s",
                self._tenant_id, kb_id, doc_id,
                exc_info=True,
            )
            return {"total": 0, "chunks": []}
        data = data or {}
        chunks = data.get("chunks") or []
        total = data.get("total")
        if total is None:
            total = len(chunks)
        return {"total": int(total), "chunks": chunks[: limit + 1]}

    async def citations(self, *, kb_id: str, doc_id: str) -> dict:
        """近 30 天检索引用聚合；失败 → 0 / None。"""
        try:
            async with get_db_session() as session:
                row = (
                    await session.execute(
                        text(_CITATIONS_SQL),
                        {
                            "tenant_id": self._tenant_id,
                            "kb_id": kb_id,
                            "doc_id": doc_id,
                            "window_days": CITATION_WINDOW_DAYS,
                        },
                    )
                ).mappings().first()
        except _DB_DEGRADE_EXCEPTIONS:
            logger.warning(
                "lineage citations lookup failed: tenant=%s kb=%s doc=%s",
                self._tenant_id, kb_id, doc_id,
                exc_info=True,
            )
            return {"count": 0, "last_seen_at": None}
        if not row:
            return {"count": 0, "last_seen_at": None}
        return {
            "count": int(row.get("cnt") or 0),
            "last_seen_at": _iso(row.get("last_seen")),
        }


def get_lineage_sources(tenant_id: str) -> LineageSources:
    """工厂：能力 dispatch 每次请求构造一份（租户绑定）。"""
    return LineageSources(tenant_id)
