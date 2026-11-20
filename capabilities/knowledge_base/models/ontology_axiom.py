#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""本体公理 ORM。

对应表：`knowledge_base.ontology_axioms`（T2，2026-11-17）。

公理表是 KB 级唯一权威（D8）；抽象 schema 里的 `axioms` 只是 Studio 往返镜像。
表只存声明，不存闭包/继承等派生内容。
"""
import uuid

from sqlalchemy import Column, Integer, String, Text, text as sa_text
from sqlalchemy.dialects.postgresql import JSONB

from jonex_core.common.database import Base
from jonex_core.common.entity import (
    AuditMixin,
    SoftDeleteMixin,
    TenantMixin,
    TimestampMixin,
)

AXIOM_STATUS_PROPOSED = "proposed"
AXIOM_STATUS_ACTIVE = "active"
AXIOM_STATUS_REJECTED = "rejected"
AXIOM_STATUSES = (
    AXIOM_STATUS_PROPOSED,
    AXIOM_STATUS_ACTIVE,
    AXIOM_STATUS_REJECTED,
)

AXIOM_SOURCE_MANUAL = "manual"
AXIOM_SOURCE_IMPORTED = "imported"
AXIOM_SOURCE_EXTRACTED = "extracted"
AXIOM_SOURCES = (
    AXIOM_SOURCE_MANUAL,
    AXIOM_SOURCE_IMPORTED,
    AXIOM_SOURCE_EXTRACTED,
)

AXIOM_ORIGIN_STUDIO = "studio"
AXIOM_ORIGIN_PLATFORM = "platform"
AXIOM_ORIGIN_IMPORTED = "imported"
AXIOM_ORIGINS = (
    AXIOM_ORIGIN_STUDIO,
    AXIOM_ORIGIN_PLATFORM,
    AXIOM_ORIGIN_IMPORTED,
)


class OntologyAxiom(
    TenantMixin, TimestampMixin, SoftDeleteMixin, AuditMixin, Base,
):
    """一条 OWL 公理声明（KB 级）。"""

    __tablename__ = "ontology_axioms"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    knowledge_base_id = Column(String(128), nullable=False, index=True)
    #: 业务键：Studio 发布沿用其公理 id；平台导入没有就生成。
    axiom_key = Column(String(64), nullable=False, default=lambda: uuid.uuid4().hex)
    kind = Column(String(32), nullable=False)
    subject = Column(
        JSONB, nullable=False, default=dict, server_default=sa_text("'{}'::jsonb"),
    )
    #: 列名是 object；属性名避开 Python 内建。
    object_ref = Column("object", JSONB, nullable=True)
    chain = Column(
        JSONB, nullable=False, default=list, server_default=sa_text("'[]'::jsonb"),
    )
    facets = Column(
        JSONB, nullable=False, default=list, server_default=sa_text("'[]'::jsonb"),
    )
    status = Column(
        String(16), nullable=False, default=AXIOM_STATUS_ACTIVE,
        server_default=sa_text("'active'"),
    )
    source = Column(
        String(16), nullable=False, default=AXIOM_SOURCE_IMPORTED,
        server_default=sa_text("'imported'"),
    )
    origin = Column(
        String(16), nullable=False, default=AXIOM_ORIGIN_IMPORTED,
        server_default=sa_text("'imported'"),
    )
    module = Column(String(64), nullable=True)
    #: 内容版本：内容每变一次 +1；启停不涉及（公理没有启停）。
    content_revision = Column(
        Integer, nullable=False, default=1, server_default=sa_text("1"),
    )
    content_hash = Column(String(64), nullable=True)
    description = Column(Text, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "knowledge_base_id": self.knowledge_base_id,
            "axiom_key": self.axiom_key,
            "kind": self.kind,
            "subject": self.subject or {},
            "object": self.object_ref,
            "chain": self.chain or [],
            "facets": self.facets or [],
            "status": self.status,
            "source": self.source,
            "origin": self.origin,
            "module": self.module,
            "content_revision": self.content_revision or 0,
            "content_hash": self.content_hash,
            "description": self.description,
            "created_by": self.created_by,
            "updated_by": self.updated_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


__all__ = [
    "OntologyAxiom",
    "AXIOM_ORIGINS",
    "AXIOM_ORIGIN_IMPORTED",
    "AXIOM_ORIGIN_PLATFORM",
    "AXIOM_ORIGIN_STUDIO",
    "AXIOM_SOURCES",
    "AXIOM_SOURCE_EXTRACTED",
    "AXIOM_SOURCE_IMPORTED",
    "AXIOM_SOURCE_MANUAL",
    "AXIOM_STATUSES",
    "AXIOM_STATUS_ACTIVE",
    "AXIOM_STATUS_PROPOSED",
    "AXIOM_STATUS_REJECTED",
]
