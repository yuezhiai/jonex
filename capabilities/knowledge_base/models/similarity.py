"""解析后相似检测：文本签名 + 合并建议。"""
from uuid import uuid4

from sqlalchemy import (
    BigInteger, Column, DateTime, Integer, Numeric, String, UniqueConstraint,
)

from jonex_core.common.database import Base
from jonex_core.common.entity import TenantMixin, TimestampMixin

STATUS_PENDING = "pending"
STATUS_MERGED = "merged"
STATUS_DISMISSED = "dismissed"
STATUS_SUPERSEDED = "superseded"
STATUS_INVALID = "invalid"


class KnowledgeDocumentSignature(Base, TenantMixin, TimestampMixin):
    __tablename__ = "knowledge_document_signatures"
    __table_args__ = {"schema": "knowledge_base"}

    document_id = Column(String(64), primary_key=True)
    knowledge_base_id = Column(String(128), nullable=False)
    # NULL = 已尝试但无可用文本（回填哨兵，避免每轮重复扫描）。
    simhash = Column(BigInteger, nullable=True)
    text_chars = Column(Integer, nullable=False, default=0)
    text_source = Column(String(32), nullable=False)


class KnowledgeDocumentSimilaritySuggestion(Base, TenantMixin, TimestampMixin):
    __tablename__ = "knowledge_document_similarity_suggestions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "source_document_id", "target_document_id",
            name="uq_kb_doc_sim_pair",
        ),
        {"schema": "knowledge_base"},
    )

    id = Column(String(64), primary_key=True, default=lambda: uuid4().hex)
    knowledge_base_id = Column(String(128), nullable=False)
    source_document_id = Column(String(64), nullable=False)
    target_document_id = Column(String(64), nullable=False)
    similarity = Column(Numeric(5, 4), nullable=False)
    method = Column(String(32), nullable=False, default="text_lines")
    status = Column(String(16), nullable=False, default=STATUS_PENDING)
    dismissed_by = Column(String(64))
    dismissed_at = Column(DateTime)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source_document_id": self.source_document_id,
            "target_document_id": self.target_document_id,
            "similarity": (
                float(self.similarity) if self.similarity is not None else 0.0
            ),
            "method": self.method,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
