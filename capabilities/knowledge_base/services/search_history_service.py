"""Search history service for Knowledge Base."""

from typing import Any

from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import ResourceNotFoundError
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant

from ..repository import KnowledgeSearchHistoryRepository
from ..dtos import (
    SearchHistoryCreateRequest,
    SearchHistoryDeleteRequest,
    SearchHistoryListRequest,
    SearchOverviewRequest,
)
from .document_service import _payload


class SearchHistoryService:
    async def list_history(
        self,
        tenant_id: str,
        user_id: str,
        request: SearchHistoryListRequest | dict,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        req = SearchHistoryListRequest(**_payload(request))
        offset = (req.page - 1) * req.page_size
        async with get_db_session() as session:
            repo = KnowledgeSearchHistoryRepository(session)
            items = await repo.list_by_user(
                tenant_id,
                user_id,
                knowledge_base_id=req.knowledge_base_id,
                offset=offset,
                limit=req.page_size,
                domain_space_id=req.domain_space_id,
            )
            total = await repo.count_by_user(
                tenant_id,
                user_id,
                knowledge_base_id=req.knowledge_base_id,
                domain_space_id=req.domain_space_id,
            )
        return {
            "items": [item.to_dict() for item in items],
            "total": total,
            "page": req.page,
            "page_size": req.page_size,
        }

    async def save_history(
        self,
        tenant_id: str,
        user_id: str,
        request: SearchHistoryCreateRequest | dict,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        req = SearchHistoryCreateRequest(**_payload(request))
        data: dict[str, Any] = _payload(req)
        metadata = dict(data.pop("metadata", {}) or {})
        domain = data.pop("domain", None)
        domain_id = data.pop("domain_id", None)
        domain_space_id = data.pop("domain_space_id", None)
        strict_config = data.pop("strict_config", None)
        if domain:
            metadata["domain"] = domain
        if domain_id:
            metadata["domain_id"] = domain_id
        if strict_config:
            metadata["strict_config"] = strict_config
        if domain_space_id:
            data["domain_space_id"] = domain_space_id
        data["extra_metadata"] = metadata
        async with get_db_session() as session:
            repo = KnowledgeSearchHistoryRepository(session)
            history = await repo.upsert_for_user(tenant_id, user_id, data)
            # [jonex] 超过 MAX_HISTORY_PER_USER 自动清理最早记录
            await repo.trim_for_user(tenant_id, user_id)
            return history.to_dict()

    async def delete_history(
        self,
        tenant_id: str,
        user_id: str,
        history_id: str,
        request: SearchHistoryDeleteRequest | dict,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        req = SearchHistoryDeleteRequest(**_payload(request))
        async with get_db_session() as session:
            repo = KnowledgeSearchHistoryRepository(session)
            deleted = await repo.delete_for_user(
                tenant_id,
                user_id,
                req.knowledge_base_id,
                history_id,
                domain_space_id=req.domain_space_id,
            )
            if not deleted:
                raise ResourceNotFoundError(message=translate("err.search_history.not_found", fallback="搜索历史不存在")  )  # 原消息)
        return {"id": history_id, "deleted": True}

    async def clear_history(
        self, tenant_id: str, user_id: str, request: SearchOverviewRequest | dict,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        req = SearchOverviewRequest(**_payload(request))
        async with get_db_session() as session:
            repo = KnowledgeSearchHistoryRepository(session)
            deleted_count = await repo.clear_for_user(
                tenant_id, user_id, req.knowledge_base_id,
                domain_space_id=req.domain_space_id,
            )
        return {"deleted_count": deleted_count}

    async def get_overview(
        self, tenant_id: str, user_id: str, request: SearchOverviewRequest | dict,
    ) -> dict:
        req = SearchOverviewRequest(**_payload(request))
        history = await self.list_history(
            tenant_id,
            user_id,
            SearchHistoryListRequest(
                knowledge_base_id=req.knowledge_base_id or "",
                domain_space_id=req.domain_space_id,
                page=1,
                page_size=10,
            ),
        )
        return {
            "total_history": history["total"],
            "recent_items": history["items"],
        }


__all__ = ["SearchHistoryService"]
