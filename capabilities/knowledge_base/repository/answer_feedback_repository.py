#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Repository for Knowledge Base answer feedback（主记录 + 变更事件）."""

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.repository import BaseRepository

from ..models import KnowledgeAnswerFeedback, KnowledgeAnswerFeedbackEvent


class KnowledgeAnswerFeedbackRepository(BaseRepository[KnowledgeAnswerFeedback]):
    """回答反馈主记录仓储：按问答记录 history_id 唯一保存最新反馈。"""

    model = KnowledgeAnswerFeedback

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def get_by_history(
        self,
        tenant_id: str,
        history_id: str,
    ) -> KnowledgeAnswerFeedback | None:
        conditions = [
            KnowledgeAnswerFeedback.tenant_id == self._tenant_id(tenant_id),
            KnowledgeAnswerFeedback.history_id == history_id,
            KnowledgeAnswerFeedback.is_deleted == 0,
        ]
        result = await self.session.execute(
            select(KnowledgeAnswerFeedback).where(*conditions)
        )
        return result.scalar_one_or_none()

    async def upsert_main(
        self,
        tenant_id: str,
        data: dict,
    ) -> KnowledgeAnswerFeedback:
        """插入或按 history_id 更新主记录（反馈状态 + version，快照字段首次写入不变）。"""
        values = {
            "tenant_id": self._tenant_id(tenant_id),
            "user_id": data["user_id"],
            "history_id": data["history_id"],
            "query": data["query"],
            "answer": data.get("answer"),
            "feedback_type": data["feedback_type"],
            "feedback_reason": data.get("feedback_reason"),
            "feedback_comment": data.get("feedback_comment"),
            "domain_space_id": data.get("domain_space_id"),
            "knowledge_base_ids": data.get("knowledge_base_ids") or [],
            "source": data.get("source"),
            "mode": data.get("mode"),
            "source_missing_reason": data.get("source_missing_reason"),
            "version": data["version"],
            "is_deleted": 0,
        }
        stmt = (
            pg_insert(KnowledgeAnswerFeedback)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["history_id"],
                set_={
                    "user_id": values["user_id"],
                    "feedback_type": values["feedback_type"],
                    "feedback_reason": values["feedback_reason"],
                    "feedback_comment": values["feedback_comment"],
                    "version": values["version"],
                    "updated_at": func.now(),
                },
            )
            .returning(KnowledgeAnswerFeedback)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one()

    def _kb_contains_conditions(self, tenant_id: str, knowledge_base_id: str) -> list:
        """按知识库聚合的公共过滤条件（tenant + 未删除 + knowledge_base_ids 包含该 kb）。"""
        return [
            KnowledgeAnswerFeedback.tenant_id == self._tenant_id(tenant_id),
            KnowledgeAnswerFeedback.is_deleted == 0,
            KnowledgeAnswerFeedback.knowledge_base_ids.contains([knowledge_base_id]),
        ]

    async def list_by_knowledge_base(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        feedback_type: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> list[KnowledgeAnswerFeedback]:
        """按知识库聚合查询回答反馈（knowledge_base_ids JSONB 包含该 kb）。"""
        conditions = self._kb_contains_conditions(tenant_id, knowledge_base_id)
        if feedback_type:
            conditions.append(KnowledgeAnswerFeedback.feedback_type == feedback_type)

        stmt = (
            select(KnowledgeAnswerFeedback)
            .where(*conditions)
            .order_by(KnowledgeAnswerFeedback.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def count_by_knowledge_base(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        feedback_type: str | None = None,
    ) -> int:
        conditions = self._kb_contains_conditions(tenant_id, knowledge_base_id)
        if feedback_type:
            conditions.append(KnowledgeAnswerFeedback.feedback_type == feedback_type)

        result = await self.session.execute(
            select(func.count()).select_from(KnowledgeAnswerFeedback).where(*conditions)
        )
        return result.scalar_one()

    async def get_stats(
        self,
        tenant_id: str,
        knowledge_base_id: str,
    ) -> dict:
        """按知识库聚合统计（总数 / 点赞 / 点踩）。"""
        base = self._kb_contains_conditions(tenant_id, knowledge_base_id)

        async def _count(extra: list) -> int:
            return (
                await self.session.execute(
                    select(func.count()).select_from(KnowledgeAnswerFeedback).where(*base, *extra)
                )
            ).scalar_one()

        return {
            "total": await _count([]),
            "like_count": await _count([KnowledgeAnswerFeedback.feedback_type == "like"]),
            "dislike_count": await _count([KnowledgeAnswerFeedback.feedback_type == "dislike"]),
        }

    async def toggle_adopted(
        self,
        tenant_id: str,
        feedback_id: str,
    ) -> KnowledgeAnswerFeedback | None:
        """按 id 切换采纳状态（未找到返回 None）。"""
        conditions = [
            KnowledgeAnswerFeedback.id == feedback_id,
            KnowledgeAnswerFeedback.tenant_id == self._tenant_id(tenant_id),
            KnowledgeAnswerFeedback.is_deleted == 0,
        ]
        result = await self.session.execute(select(KnowledgeAnswerFeedback).where(*conditions))
        record = result.scalar_one_or_none()
        if not record:
            return None
        record.adopted = not record.adopted
        await self.session.flush()
        return record


class KnowledgeAnswerFeedbackEventRepository(BaseRepository[KnowledgeAnswerFeedbackEvent]):
    """回答反馈变更事件仓储：只增不改，operation_id 幂等。"""

    model = KnowledgeAnswerFeedbackEvent

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def get_by_operation(
        self,
        tenant_id: str,
        operation_id: str,
    ) -> KnowledgeAnswerFeedbackEvent | None:
        conditions = [
            KnowledgeAnswerFeedbackEvent.tenant_id == self._tenant_id(tenant_id),
            KnowledgeAnswerFeedbackEvent.operation_id == operation_id,
        ]
        result = await self.session.execute(
            select(KnowledgeAnswerFeedbackEvent).where(*conditions)
        )
        return result.scalar_one_or_none()

    async def insert_event(
        self,
        tenant_id: str,
        data: dict,
    ) -> KnowledgeAnswerFeedbackEvent:
        record = KnowledgeAnswerFeedbackEvent(
            tenant_id=self._tenant_id(tenant_id),
            history_id=data["history_id"],
            feedback_id=data.get("feedback_id"),
            user_id=data["user_id"],
            operation_id=data["operation_id"],
            version=data["version"],
            feedback_type=data.get("feedback_type"),
            feedback_reason=data.get("feedback_reason"),
            feedback_comment=data.get("feedback_comment"),
        )
        self.session.add(record)
        await self.session.flush()
        return record


__all__ = ["KnowledgeAnswerFeedbackRepository", "KnowledgeAnswerFeedbackEventRepository"]
