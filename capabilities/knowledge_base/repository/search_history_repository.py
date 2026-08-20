#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Repository for Knowledge Base search history."""

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.repository import BaseRepository

from ..models import KnowledgeSearchHistory, build_query_hash

# [jonex] 检索历史单用户保留上限：超过后自动软清理最早记录
MAX_HISTORY_PER_USER = 200


def _strip_raw_urls(refs: list) -> list:
    """剥离引用快照中会过期的预签名 URL（COS 约 1 小时），仅保留持久标识。

    点击历史展示时经 resolve 端点按 doc_id/locations 重新生成 raw_url。
    [jonex] §image-refs P0-6：locations[].asset_url 同为预签名，一并剥离；
    历史展示经 resolve 按 image_idx + asset_ext 重新推导生成。
    """
    out = []
    for r in refs or []:
        if isinstance(r, dict):
            item = dict(r)
            item.pop("raw_url", None)
            for loc in item.get("locations") or []:
                if isinstance(loc, dict):
                    loc.pop("asset_url", None)
            out.append(item)
        else:
            out.append(r)
    return out


class KnowledgeSearchHistoryRepository(BaseRepository[KnowledgeSearchHistory]):
    model = KnowledgeSearchHistory

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def list_by_user(
        self,
        tenant_id: str,
        user_id: str,
        knowledge_base_id: str,
        offset: int = 0,
        limit: int = 20,
        domain_space_id: str | None = None,
    ) -> list[KnowledgeSearchHistory]:
        conditions = [
            KnowledgeSearchHistory.tenant_id == self._tenant_id(tenant_id),
            KnowledgeSearchHistory.user_id == user_id,
            KnowledgeSearchHistory.is_deleted == 0,
        ]
        if knowledge_base_id:
            conditions.append(KnowledgeSearchHistory.knowledge_base_id == knowledge_base_id)
        if domain_space_id is not None:
            conditions.append(KnowledgeSearchHistory.domain_space_id == domain_space_id)
        stmt = (
            select(KnowledgeSearchHistory)
            .where(*conditions)
            .order_by(KnowledgeSearchHistory.searched_at.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def count_by_user(
        self,
        tenant_id: str,
        user_id: str,
        knowledge_base_id: str,
        domain_space_id: str | None = None,
    ) -> int:
        conditions = [
            KnowledgeSearchHistory.tenant_id == self._tenant_id(tenant_id),
            KnowledgeSearchHistory.user_id == user_id,
            KnowledgeSearchHistory.is_deleted == 0,
        ]
        if knowledge_base_id:
            conditions.append(KnowledgeSearchHistory.knowledge_base_id == knowledge_base_id)
        if domain_space_id is not None:
            conditions.append(KnowledgeSearchHistory.domain_space_id == domain_space_id)
        result = await self.session.execute(
            select(func.count())
            .select_from(KnowledgeSearchHistory)
            .where(*conditions)
        )
        return result.scalar_one()

    async def upsert_for_user(
        self,
        tenant_id: str,
        user_id: str,
        data: dict,
    ) -> KnowledgeSearchHistory:
        normalized_tenant = self._tenant_id(tenant_id)
        query = data["query"]
        values = {
            "tenant_id": normalized_tenant,
            "user_id": user_id,
            "query": query,
            "query_hash": build_query_hash(query),
            "knowledge_base_id": data["knowledge_base_id"],
            "domain_space_id": data.get("domain_space_id"),
            "mode": data.get("mode") or "hybrid",
            "top_k": data.get("top_k") or 5,
            "status": data.get("status") or "done",
            "answer_preview": data.get("answer_preview"),
            "reference_count": data.get("reference_count") or 0,
            "result_count": data.get("result_count") or 0,
            "duration_ms": data.get("duration_ms"),
            "extra_metadata": data.get("extra_metadata") or {},
            "answer": data.get("answer"),
            "references": _strip_raw_urls(data.get("references") or []),
            "reasoning": data.get("reasoning"),
            "is_deleted": 0,
        }
        # [jonex] 每次搜索插入一条新记录，不覆盖旧记录（需求：新结果形成新的历史记录）。
        # 原有唯一约束 uq_knowledge_search_history_query_kb 已由 update/012 迁移移除，
        # 同一问题可保留多条历史；超出 MAX_HISTORY_PER_USER 由 trim_for_user 清理。
        stmt = (
            insert(KnowledgeSearchHistory)
            .values(**values)
            .returning(KnowledgeSearchHistory)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def trim_for_user(
        self,
        tenant_id: str,
        user_id: str,
        keep: int = MAX_HISTORY_PER_USER,
    ) -> int:
        """该用户检索历史超过 keep 条时，软删除最早记录，返回删除条数。

        在 save 后调用：保留最新 keep 条，其余（含同一问题的更早记录）标记 is_deleted=1。
        """
        conditions = [
            KnowledgeSearchHistory.tenant_id == self._tenant_id(tenant_id),
            KnowledgeSearchHistory.user_id == user_id,
            KnowledgeSearchHistory.is_deleted == 0,
        ]
        stmt_ids = (
            select(KnowledgeSearchHistory.id)
            .where(*conditions)
            .order_by(KnowledgeSearchHistory.searched_at.desc())
            .limit(keep)
        )
        keep_ids = set((await self.session.execute(stmt_ids)).scalars())
        if not keep_ids:
            return 0
        stmt = (
            update(KnowledgeSearchHistory)
            .where(*conditions, KnowledgeSearchHistory.id.not_in(keep_ids))
            .values(is_deleted=1, updated_at=func.now())
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount or 0

    async def delete_for_user(
        self,
        tenant_id: str,
        user_id: str,
        knowledge_base_id: str,
        history_id: str,
        domain_space_id: str | None = None,
    ) -> bool:
        history = await self.get_by_id(history_id, tenant_id)
        if (
            not history
            or history.user_id != user_id
            or (knowledge_base_id and history.knowledge_base_id != knowledge_base_id)
            or (domain_space_id is not None and history.domain_space_id != domain_space_id)
        ):
            return False
        history.is_deleted = 1
        await self.session.flush()
        return True

    async def clear_for_user(
        self, tenant_id: str, user_id: str, knowledge_base_id: str,
        domain_space_id: str | None = None,
    ) -> int:
        histories = await self.list_by_user(
            tenant_id,
            user_id,
            knowledge_base_id=knowledge_base_id,
            offset=0,
            limit=1000,
            domain_space_id=domain_space_id,
        )
        for history in histories:
            history.is_deleted = 1
        await self.session.flush()
        return len(histories)


__all__ = ["KnowledgeSearchHistoryRepository"]
