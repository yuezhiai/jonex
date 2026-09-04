#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Knowledge Base answer feedback entities.

回答级反馈：按问答记录（history_id = knowledge_search_history.id）维度存储，
主记录保存最新反馈，事件表追加每次变更。
"""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, Column, DateTime, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from jonex_core.common.database import Base
from jonex_core.common.entity import SoftDeleteMixin, TenantMixin, TimestampMixin


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class KnowledgeAnswerFeedback(Base, TenantMixin, TimestampMixin, SoftDeleteMixin):
    """回答反馈主记录（按问答记录唯一，保存最新反馈 + 上下文快照）"""

    __tablename__ = "knowledge_answer_feedback"
    __table_args__ = (
        Index("idx_kb_answer_feedback_tenant_user", "tenant_id", "user_id"),
        Index("idx_kb_answer_feedback_tenant_time", "tenant_id", "created_at"),
        {"schema": "knowledge_base"},
    )

    id = Column(String(64), primary_key=True, default=lambda: str(uuid4()))
    user_id = Column(String(128), nullable=False, index=True)
    history_id = Column(String(64), nullable=False, unique=True, comment="问答记录 ID（检索历史 id）")
    query = Column(Text, nullable=False, comment="原始问题快照")
    answer = Column(Text, nullable=True, comment="AI 回答快照")
    feedback_type = Column(String(16), nullable=False, comment="like / dislike")
    feedback_reason = Column(String(32), nullable=True, comment="点踩原因 code，like 时为空")
    feedback_comment = Column(String(300), nullable=True, comment="补充说明，like 时为空")
    domain_space_id = Column(String(64), nullable=True, comment="领域空间")
    knowledge_base_ids = Column(JSONB, nullable=False, default=list, comment="知识来源（可空/多值）")
    source = Column(String(64), nullable=True, comment="检索链路来源")
    mode = Column(String(32), nullable=True, comment="检索模式")
    source_missing_reason = Column(String(64), nullable=True, comment="来源缺失原因")
    version = Column(Integer, nullable=False, default=0, comment="客户端操作序号（乱序保护）")
    adopted = Column(Boolean, nullable=False, default=False, comment="是否已被管理员采纳")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "history_id": self.history_id,
            "query": self.query,
            "answer": self.answer,
            "feedback_type": self.feedback_type,
            "feedback_reason": self.feedback_reason,
            "feedback_comment": self.feedback_comment,
            "domain_space_id": self.domain_space_id,
            "knowledge_base_ids": self.knowledge_base_ids or [],
            "source": self.source,
            "mode": self.mode,
            "source_missing_reason": self.source_missing_reason,
            "version": self.version,
            "adopted": self.adopted,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


class KnowledgeAnswerFeedbackEvent(Base, TenantMixin):
    """回答反馈变更事件（只增不改，operation_id 幂等）"""

    __tablename__ = "knowledge_answer_feedback_event"
    __table_args__ = (
        Index("idx_kb_answer_feedback_event_history", "history_id", "version"),
        {"schema": "knowledge_base"},
    )

    id = Column(String(64), primary_key=True, default=lambda: str(uuid4()))
    history_id = Column(String(64), nullable=False, comment="问答记录 ID（冗余）")
    feedback_id = Column(String(64), nullable=True, comment="主记录 id")
    user_id = Column(String(128), nullable=False, comment="操作人")
    operation_id = Column(String(64), nullable=False, unique=True, comment="幂等键")
    version = Column(Integer, nullable=False, comment="操作序号")
    feedback_type = Column(String(16), nullable=True)
    feedback_reason = Column(String(32), nullable=True)
    feedback_comment = Column(String(300), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), comment="事件时间")


__all__ = ["KnowledgeAnswerFeedback", "KnowledgeAnswerFeedbackEvent"]
