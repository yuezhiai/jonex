#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""推理规则 ORM。

对应表：`knowledge_base.ontology_reasoning_rules`（T2，2026-11-17）。

规则是 KB 级业务实体：AST 为权威，`text` 只作展示；`content_revision` 只随内容变化，
启停单独留痕（`enabled_changed_at / by`），避免平台只切启停就误伤 Studio 的内容发布。
"""
import uuid

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, text as sa_text
from sqlalchemy.dialects.postgresql import JSONB

from jonex_core.common.database import Base
from jonex_core.common.entity import (
    AuditMixin,
    SoftDeleteMixin,
    TenantMixin,
    TimestampMixin,
)

RULE_STATUS_PROPOSED = "proposed"
RULE_STATUS_ACTIVE = "active"
RULE_STATUS_REJECTED = "rejected"
RULE_STATUSES = (RULE_STATUS_PROPOSED, RULE_STATUS_ACTIVE, RULE_STATUS_REJECTED)

RULE_ORIGIN_STUDIO = "studio"
RULE_ORIGIN_PLATFORM = "platform"
RULE_ORIGIN_IMPORTED = "imported"
RULE_ORIGINS = (RULE_ORIGIN_STUDIO, RULE_ORIGIN_PLATFORM, RULE_ORIGIN_IMPORTED)


class OntologyReasoningRule(
    TenantMixin, TimestampMixin, SoftDeleteMixin, AuditMixin, Base,
):
    """一条 Horn 规则（KB 级）。"""

    __tablename__ = "ontology_reasoning_rules"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    knowledge_base_id = Column(String(128), nullable=False, index=True)
    #: 业务键：Studio 发布沿用其规则 id；平台新建生成 r_<uuid>。
    rule_key = Column(String(64), nullable=False, default=lambda: f"r_{uuid.uuid4().hex}")
    name = Column(String(128), nullable=False)
    #: 作者编写的规则文本（展示用）；AST 为权威。
    text = Column(Text, nullable=False)
    premises = Column(
        JSONB, nullable=False, default=list, server_default=sa_text("'[]'::jsonb"),
    )
    conclusion = Column(
        JSONB, nullable=False, default=dict, server_default=sa_text("'{}'::jsonb"),
    )
    status = Column(
        String(16), nullable=False, default=RULE_STATUS_ACTIVE,
        server_default=sa_text("'active'"),
    )
    enabled = Column(
        Boolean, nullable=False, default=True, server_default=sa_text("true"),
    )
    enabled_changed_at = Column(DateTime(timezone=True), nullable=True)
    enabled_changed_by = Column(String(128), nullable=True)
    origin = Column(
        String(16), nullable=False, default=RULE_ORIGIN_IMPORTED,
        server_default=sa_text("'imported'"),
    )
    module = Column(String(64), nullable=True)
    #: 内容版本：text / premises / conclusion / status / name / description 变化时 +1。
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
            "rule_key": self.rule_key,
            "name": self.name,
            "text": self.text,
            "premises": self.premises or [],
            "conclusion": self.conclusion or {},
            "status": self.status,
            "enabled": bool(self.enabled),
            "enabled_changed_at": (
                self.enabled_changed_at.isoformat()
                if self.enabled_changed_at else None
            ),
            "enabled_changed_by": self.enabled_changed_by,
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
    "OntologyReasoningRule",
    "RULE_ORIGINS",
    "RULE_ORIGIN_IMPORTED",
    "RULE_ORIGIN_PLATFORM",
    "RULE_ORIGIN_STUDIO",
    "RULE_STATUSES",
    "RULE_STATUS_ACTIVE",
    "RULE_STATUS_PROPOSED",
    "RULE_STATUS_REJECTED",
]
