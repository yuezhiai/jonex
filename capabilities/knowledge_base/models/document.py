#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Knowledge Base document entities."""

from datetime import datetime
from enum import Enum
from uuid import uuid4

from sqlalchemy import BigInteger, Column, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB

from jonex_core.common.database import Base
from jonex_core.common.entity import SoftDeleteMixin, TenantMixin, TimestampMixin


class DocStatus(str, Enum):
    """Document lifecycle managed by the Knowledge Base service."""

    PENDING = "pending"
    PARSING = "parsing"
    INGESTING = "ingesting"  # [jonex] P3 推送入图阶段（LightRAG LLM 抽取+embedding+写图）
    READY = "ready"
    FAILED = "failed"
    DELETING = "deleting"
    DELETED = "deleted"


class OntologyStatus(str, Enum):
    """Ontology extraction lifecycle for a parsed document."""

    PENDING = "pending"
    EXTRACTING = "extracting"
    READY = "ready"
    FAILED = "failed"


# 服务器本地时区，与 TimestampMixin 的 datetime.now()（naive 本地时间）落库口径一致。
# 序列化出口据此补齐真实时区偏移（如 +08:00），避免 naive 值被硬标成 UTC 导致前端 +8h 偏差。
_LOCAL_TZ = datetime.now().astimezone().tzinfo


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=_LOCAL_TZ)
    return value.isoformat()


class KnowledgeDocument(Base, TenantMixin, TimestampMixin, SoftDeleteMixin):
    """Tenant-scoped Knowledge Base document metadata."""

    __tablename__ = "knowledge_documents"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(String(64), primary_key=True, default=lambda: str(uuid4()))
    file_name = Column(String(512), nullable=False)
    file_path = Column(String(1024), nullable=False)
    file_size = Column(BigInteger, nullable=False, default=0)
    mime_type = Column(String(128), nullable=True)
    # 源文件内容 md5（上传去重 + reparse 跳过），非安全用途
    content_hash = Column(String(32), nullable=True, index=True)
    knowledge_base_id = Column(String(128), nullable=False, index=True)

    storage_backend = Column(String(16), nullable=False, default="local")
    storage_key = Column(String(1024), nullable=True)

    status = Column(String(32), nullable=False, default=DocStatus.PENDING.value, index=True)
    rag_task_id = Column(String(128), nullable=True, index=True)
    rag_doc_ids = Column(JSONB, nullable=False, default=list)
    error_message = Column(Text, nullable=True)

    ontology_status = Column(
        String(32),
        nullable=False,
        default=OntologyStatus.PENDING.value,
        index=True,
    )
    ontology_error = Column(Text, nullable=True)
    ontology_retry_count = Column(Integer, nullable=False, default=0)

    # reparse 代次（P0-I）：每次 reparse 提交原子递增，旧代次任务结果作废
    content_generation = Column(Integer, nullable=False, default=0)
    # 文档级本体 schema 版本记账（P1-E）
    ontology_target_schema_version = Column(Integer, nullable=True)
    ontology_applied_schema_version = Column(Integer, nullable=True)
    ontology_applied_schema_hash = Column(String(32), nullable=True)

    extra_metadata = Column(JSONB, nullable=False, default=dict)

    # [jonex] LLM-Wiki 编译状态列化（原 extra_metadata JSONB → 真实列，migration 008）
    # 时区口径：llm_wiki_compile_requested_at 用 datetime.utcnow()（naive UTC），
    # 与 TimestampMixin 的 created_at（naive 本地时间）口径不同是有意的——
    # 编译超时判定按 UTC 单调时间（P0-2 同坑，见 reconciliation_service.py:723）
    llm_wiki_compile_status = Column(String(32), nullable=True, index=True)   # NULL/compiling/compiled/stale/failed
    llm_wiki_compile_error = Column(Text, nullable=True)
    llm_wiki_compile_warnings = Column(JSONB, nullable=True)                  # 编译警告列表
    llm_wiki_compile_requested_at = Column(DateTime, nullable=True)
    llm_wiki_compiled_at = Column(DateTime, nullable=True)
    llm_wiki_task_id = Column(String(128), nullable=True, index=True)
    # [jonex] LLM-Wiki Schema 版本 fencing（方案 llmwiki-schema-settings-execution-plan §6）
    llm_wiki_target_schema_version = Column(Integer, nullable=True)
    llm_wiki_applied_schema_version = Column(Integer, nullable=True)

    # 文档来源方式（冗余真实列）：api / api_push / storage / file，统计按此列分组
    folder_id = Column(String(64), nullable=True, index=True)

    data_source_type = Column(String(32), nullable=True, index=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "file_name": self.file_name,
            "file_path": self.file_path,
            "file_size": self.file_size,
            "mime_type": self.mime_type,
            "knowledge_base_id": self.knowledge_base_id,
            "status": self.status,
            "storage_backend": self.storage_backend,
            "storage_key": self.storage_key,
            "rag_task_id": self.rag_task_id,
            "rag_doc_ids": self.rag_doc_ids or [],
            "error_message": self.error_message,
            "ontology_status": self.ontology_status,
            "ontology_error": self.ontology_error,
            "ontology_retry_count": self.ontology_retry_count or 0,
            "content_generation": self.content_generation or 0,
            "ontology_target_schema_version": self.ontology_target_schema_version,
            "ontology_applied_schema_version": self.ontology_applied_schema_version,
            "ontology_applied_schema_hash": self.ontology_applied_schema_hash,
            "folder_id": self.folder_id,
            "data_source_type": self.data_source_type,
            # [jonex] LLM-Wiki 编译状态（顶层字段，对齐 status/ontology_status）
            "llm_wiki_compile_status": self.llm_wiki_compile_status,
            "llm_wiki_compile_error": self.llm_wiki_compile_error,
            # [jonex] LLM-Wiki Schema 版本 fencing（applied < target = 过期，方案 §6）
            "llm_wiki_target_schema_version": self.llm_wiki_target_schema_version,
            "llm_wiki_applied_schema_version": self.llm_wiki_applied_schema_version,
            "metadata": self.extra_metadata or {},
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


__all__ = ["DocStatus", "KnowledgeDocument", "OntologyStatus"]
