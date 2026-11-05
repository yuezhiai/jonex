#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Knowledge Base answer feedback entities.

回答级反馈：按问答记录（history_id = knowledge_search_history.id）维度存储，
主记录保存最新反馈，事件表追加每次变更。
"""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, Column, DateTime, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from jonex_core.common.database import Base
from jonex_core.common.entity import SoftDeleteMixin, TenantMixin, TimestampMixin


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class KnowledgeAnswerFeedback(Base, TenantMixin, TimestampMixin, SoftDeleteMixin):
    """回答反馈主记录（Web 按 history_id 唯一，MCP 按 request_id 唯一）

    MCP 链路关闭检索历史后没有 history_id，改以 `request_id`
    （= search_request_event.request_id）为锚点；此时 user_id / history_id /
    query / answer 均为 NULL（数据最小化），只保留 mcp_key_id / mcp_key_name 快照。
    """

    __tablename__ = "knowledge_answer_feedback"
    __table_args__ = (
        Index("idx_kb_answer_feedback_tenant_user", "tenant_id", "user_id"),
        Index("idx_kb_answer_feedback_tenant_time", "tenant_id", "created_at"),
        Index("idx_kb_answer_feedback_mcp_key", "mcp_key_id"),
        # MCP 幂等冲突键：同一 request_id 只允许一行；NULL 行不参与唯一性。
        Index(
            "uq_kb_answer_feedback_request",
            "request_id",
            unique=True,
            postgresql_where=text("request_id IS NOT NULL"),
        ),
        {"schema": "knowledge_base"},
    )

    id = Column(String(64), primary_key=True, default=lambda: str(uuid4()))
    user_id = Column(String(128), nullable=True, index=True, comment="Web 反馈用户；MCP 反馈为 NULL")
    history_id = Column(String(64), nullable=True, unique=True, comment="问答记录 ID（检索历史 id）；MCP 反馈为 NULL")
    request_id = Column(String(64), nullable=True, comment="MCP 检索请求 ID（反馈锚点）；Web 反馈为 NULL")
    query = Column(Text, nullable=True, comment="原始问题快照；MCP 反馈不保存（NULL）")
    answer = Column(Text, nullable=True, comment="AI 回答快照；MCP 反馈不保存（NULL）")
    feedback_type = Column(String(16), nullable=False, comment="like / dislike")
    feedback_reason = Column(String(32), nullable=True, comment="点踩原因 code，like 时为空")
    feedback_comment = Column(String(300), nullable=True, comment="补充说明，like 时为空")
    domain_space_id = Column(String(64), nullable=True, comment="领域空间")
    knowledge_base_ids = Column(JSONB, nullable=False, default=list, comment="知识来源（可空/多值）")
    source = Column(String(64), nullable=True, comment="检索链路来源")
    mode = Column(String(32), nullable=True, comment="检索模式")
    source_missing_reason = Column(String(64), nullable=True, comment="来源缺失原因")
    mcp_key_id = Column(String(64), nullable=True, comment="MCP Key ID 快照；Web 反馈为 NULL")
    mcp_key_name = Column(String(255), nullable=True, comment="提交时的 MCP Key 名称快照")
    version = Column(Integer, nullable=False, default=0, comment="客户端操作序号（乱序保护）")
    adopted = Column(Boolean, nullable=False, default=False, comment="是否已被管理员采纳")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "history_id": self.history_id,
            "request_id": self.request_id,
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
            "mcp_key_id": self.mcp_key_id,
            "mcp_key_name": self.mcp_key_name,
            "version": self.version,
            "adopted": self.adopted,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


class KnowledgeAnswerFeedbackEvent(Base, TenantMixin):
    """回答反馈变更事件（只增不改，operation_id 幂等）

    MCP 反馈没有 history_id / user_id，改以 request_id + mcp_key_id 追溯。
    """

    __tablename__ = "knowledge_answer_feedback_event"
    __table_args__ = (
        Index("idx_kb_answer_feedback_event_history", "history_id", "version"),
        Index("idx_kb_answer_feedback_event_request", "request_id"),
        {"schema": "knowledge_base"},
    )

    id = Column(String(64), primary_key=True, default=lambda: str(uuid4()))
    history_id = Column(String(64), nullable=True, comment="问答记录 ID（冗余）；MCP 反馈为 NULL")
    request_id = Column(String(64), nullable=True, comment="MCP 检索请求 ID；Web 反馈为 NULL")
    feedback_id = Column(String(64), nullable=True, comment="主记录 id")
    user_id = Column(String(128), nullable=True, comment="操作人；MCP 反馈为 NULL")
    operation_id = Column(String(64), nullable=False, unique=True, comment="幂等键")
    version = Column(Integer, nullable=False, comment="操作序号")
    feedback_type = Column(String(16), nullable=True)
    feedback_reason = Column(String(32), nullable=True)
    feedback_comment = Column(String(300), nullable=True)
    mcp_key_id = Column(String(64), nullable=True, comment="MCP Key ID 快照；Web 反馈为 NULL")
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), comment="事件时间")


__all__ = ["KnowledgeAnswerFeedback", "KnowledgeAnswerFeedbackEvent"]
