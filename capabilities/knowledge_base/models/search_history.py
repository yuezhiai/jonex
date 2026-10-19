#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Knowledge Base search history entities."""

import hashlib
from datetime import datetime
from uuid import uuid4

from sqlalchemy import Column, DateTime, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from jonex_core.common.database import Base
from jonex_core.common.entity import SoftDeleteMixin, TenantMixin, TimestampMixin


def normalize_query(query: str) -> str:
    return " ".join((query or "").strip().split())


def build_query_hash(query: str) -> str:
    return hashlib.sha256(normalize_query(query).encode("utf-8")).hexdigest()


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class KnowledgeSearchHistory(Base, TenantMixin, TimestampMixin, SoftDeleteMixin):
    """Tenant/user-scoped search history."""

    __tablename__ = "knowledge_search_history"
    __table_args__ = (
        Index("idx_kb_search_history_tenant_user_time", "tenant_id", "user_id", "searched_at"),
        Index("idx_kb_search_history_tenant_user_deleted", "tenant_id", "user_id", "is_deleted"),
        Index(
            "idx_kb_search_history_tenant_user_kb_time",
            "tenant_id",
            "user_id",
            "knowledge_base_id",
            "searched_at",
        ),
        {"schema": "knowledge_base"},
    )

    id = Column(String(64), primary_key=True, default=lambda: str(uuid4()))
    user_id = Column(String(128), nullable=False, index=True)
    query = Column(Text, nullable=False)
    query_hash = Column(String(64), nullable=False)
    knowledge_base_id = Column(String(128), nullable=False, index=True)
    domain_space_id = Column(String(64), nullable=True, index=True)
    mode = Column(String(32), nullable=False, default="hybrid")
    top_k = Column(Integer, nullable=False, default=5)
    status = Column(String(32), nullable=False, default="done")
    answer_preview = Column(Text, nullable=True)
    reference_count = Column(Integer, nullable=False, default=0)
    result_count = Column(Integer, nullable=False, default=0)
    duration_ms = Column(Integer, nullable=True)
    extra_metadata = Column(JSONB, nullable=False, default=dict)
    # [jonex] 检索历史快照：点击历史直接展示历史结果（不重新检索）
    # answer      完整答案原始文本（含 <think> 标记，展示时前端 parseThink/parseReferences 解析）
    # references  引用快照（JSONB，落库前剥离过期的 raw_url，展示时经 resolve 端点重新富化）
    # reasoning   推理链快照（ReasoningTrace: steps/final_source/total_ms）
    answer = Column(Text, nullable=True)
    references = Column(JSONB, nullable=False, default=list)
    reasoning = Column(JSONB, nullable=True)
    searched_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), index=True)

    def to_dict(self, include_snapshot: bool = True) -> dict:
        """`include_snapshot=False` 产出「列表视图」——不含三个快照大字段。

        [jonex] 快照三件套（`answer` / `references` / `reasoning`）实测占
        列表响应体积的 **83%**，且只有「点开某条历史回放」时才用得上。
        列表用 `include_snapshot=False`，点击时按 `history_id` 单取（见 repository
        的 `with_snapshot`）—— ⚠️ 两者必须成对使用：列表视图**不能碰**这三个属性，
        因为 SQL 侧用 `load_only` 把它们裁掉了，在这里读取会触发懒加载
        （异步会话下表现为 `MissingGreenlet`）。
        """
        data = {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "domain_space_id": self.domain_space_id,
            "query": self.query,
            "query_hash": self.query_hash,
            "knowledge_base_id": self.knowledge_base_id,
            "mode": self.mode,
            "top_k": self.top_k,
            "status": self.status,
            "reference_count": self.reference_count,
            "result_count": self.result_count,
            "duration_ms": self.duration_ms,
            "metadata": self.extra_metadata or {},
            "searched_at": _iso(self.searched_at),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }
        if include_snapshot:
            data["answer_preview"] = self.answer_preview
            data["answer"] = self.answer
            data["references"] = self.references or []
            data["reasoning"] = self.reasoning
        return data


__all__ = ["KnowledgeSearchHistory", "build_query_hash", "normalize_query"]
