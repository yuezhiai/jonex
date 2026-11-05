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

    async def get_by_request(
        self,
        tenant_id: str,
        request_id: str,
    ) -> KnowledgeAnswerFeedback | None:
        """按 MCP 检索 request_id 取主记录（Web 反馈 request_id 为 NULL，不会命中）。"""
        conditions = [
            KnowledgeAnswerFeedback.tenant_id == self._tenant_id(tenant_id),
            KnowledgeAnswerFeedback.request_id == request_id,
            KnowledgeAnswerFeedback.is_deleted == 0,
        ]
        result = await self.session.execute(
            select(KnowledgeAnswerFeedback).where(*conditions)
        )
        return result.scalar_one_or_none()

    async def upsert_by_request(
        self,
        tenant_id: str,
        data: dict,
    ) -> KnowledgeAnswerFeedback:
        """插入或按 request_id 更新 MCP 反馈主记录（冲突键是 request_id，不是 history_id）。

        `values` / `set_` 必须显式列全字段：漏列不会报错，只会在更新时静默丢字段。
        """
        values = {
            "tenant_id": self._tenant_id(tenant_id),
            # MCP 无用户/问答记录：数据最小化，不存 query/answer 快照
            "user_id": None,
            "history_id": None,
            "request_id": data["request_id"],
            "query": None,
            "answer": None,
            "feedback_type": data["feedback_type"],
            "feedback_reason": data.get("feedback_reason"),
            "feedback_comment": data.get("feedback_comment"),
            "domain_space_id": data.get("domain_space_id"),
            "knowledge_base_ids": data.get("knowledge_base_ids") or [],
            "source": data.get("source"),
            "mode": data.get("mode"),
            "source_missing_reason": data.get("source_missing_reason"),
            "mcp_key_id": data.get("mcp_key_id"),
            "mcp_key_name": data.get("mcp_key_name"),
            "version": data["version"],
            "is_deleted": 0,
        }
        stmt = (
            pg_insert(KnowledgeAnswerFeedback)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[KnowledgeAnswerFeedback.request_id],
                index_where=KnowledgeAnswerFeedback.request_id.isnot(None),
                set_={
                    "request_id": values["request_id"],
                    "feedback_type": values["feedback_type"],
                    "feedback_reason": values["feedback_reason"],
                    "feedback_comment": values["feedback_comment"],
                    "domain_space_id": values["domain_space_id"],
                    "knowledge_base_ids": values["knowledge_base_ids"],
                    "source": values["source"],
                    "mode": values["mode"],
                    "source_missing_reason": values["source_missing_reason"],
                    "mcp_key_id": values["mcp_key_id"],
                    "mcp_key_name": values["mcp_key_name"],
                    "version": values["version"],
                    "updated_at": func.now(),
                },
            )
            .returning(KnowledgeAnswerFeedback)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one()

    def _kb_contains_conditions(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        source: str | None = None,
    ) -> list:
        """按知识库聚合的公共过滤条件（tenant + 未删除 + knowledge_base_ids 包含该 kb）。

        `source` 区分反馈来源：web → 无 MCP Key（mcp_key_id IS NULL）；
        mcp → 有 MCP Key 快照（mcp_key_id IS NOT NULL）；None 不过滤。
        """
        conditions = [
            KnowledgeAnswerFeedback.tenant_id == self._tenant_id(tenant_id),
            KnowledgeAnswerFeedback.is_deleted == 0,
            KnowledgeAnswerFeedback.knowledge_base_ids.contains([knowledge_base_id]),
        ]
        if source == "web":
            conditions.append(KnowledgeAnswerFeedback.mcp_key_id.is_(None))
        elif source == "mcp":
            conditions.append(KnowledgeAnswerFeedback.mcp_key_id.isnot(None))
        return conditions

    async def list_by_knowledge_base(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        feedback_type: str | None = None,
        source: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> list[KnowledgeAnswerFeedback]:
        """按知识库聚合查询回答反馈（knowledge_base_ids JSONB 包含该 kb）。"""
        conditions = self._kb_contains_conditions(tenant_id, knowledge_base_id, source)
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
        source: str | None = None,
    ) -> int:
        conditions = self._kb_contains_conditions(tenant_id, knowledge_base_id, source)
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
            # Web 用 history_id + user_id；MCP 两者为空，用 request_id + mcp_key_id 追溯
            history_id=data.get("history_id"),
            request_id=data.get("request_id"),
            feedback_id=data.get("feedback_id"),
            user_id=data.get("user_id"),
            operation_id=data["operation_id"],
            version=data["version"],
            feedback_type=data.get("feedback_type"),
            feedback_reason=data.get("feedback_reason"),
            feedback_comment=data.get("feedback_comment"),
            mcp_key_id=data.get("mcp_key_id"),
        )
        self.session.add(record)
        await self.session.flush()
        return record


__all__ = ["KnowledgeAnswerFeedbackRepository", "KnowledgeAnswerFeedbackEventRepository"]
