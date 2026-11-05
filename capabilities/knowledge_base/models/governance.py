#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Knowledge Base — 数据治理控制面 P1（迁移 014）。

五张表：标签字典 / 资源标签 / 用户密级 / 策略 / 判定审计。
"""
from sqlalchemy import Column, DateTime, Integer, SmallInteger, String
from sqlalchemy.dialects.postgresql import JSONB

from jonex_core.common.database import Base
from jonex_core.common.entity import SoftDeleteMixin, TenantMixin, TimestampMixin


class GovernanceTagDef(TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "governance_tag_defs"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(String(64), primary_key=True)
    knowledge_base_id = Column(String(128), nullable=False, index=True)
    name = Column(String(128), nullable=False)
    color = Column(String(32), nullable=True)
    description = Column(String(500), nullable=True)
    created_by = Column(String(64), nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "knowledge_base_id": self.knowledge_base_id,
            "name": self.name, "color": self.color,
            "description": self.description, "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class GovernanceLabel(TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "governance_labels"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(String(64), primary_key=True)
    knowledge_base_id = Column(String(128), nullable=False, index=True)
    data_source_id = Column(String(64), nullable=False)
    table_name = Column(String(255), nullable=False)
    column_name = Column(String(255), nullable=True)
    level = Column(String(16), nullable=False, default="public")
    tags = Column(JSONB, nullable=False, default=list)
    origin = Column(String(16), nullable=False, default="manual")
    note = Column(String(500), nullable=True)
    created_by = Column(String(64), nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "knowledge_base_id": self.knowledge_base_id,
            "data_source_id": self.data_source_id,
            "table_name": self.table_name, "column_name": self.column_name,
            "level": self.level, "tags": list(self.tags or []),
            "origin": self.origin, "note": self.note,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class GovernanceUserClearance(TenantMixin, TimestampMixin, Base):
    __tablename__ = "governance_user_clearance"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(String(64), primary_key=True)
    user_id = Column(String(64), nullable=False)
    level = Column(String(16), nullable=False, default="public")
    granted_by = Column(String(64), nullable=True)
    note = Column(String(500), nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "user_id": self.user_id, "level": self.level,
            "granted_by": self.granted_by, "note": self.note,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class GovernancePolicy(TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "governance_policies"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(String(64), primary_key=True)
    knowledge_base_id = Column(String(128), nullable=False, index=True)
    name = Column(String(128), nullable=False)
    description = Column(String(500), nullable=True)
    effect = Column(String(16), nullable=False)
    priority = Column(Integer, nullable=False, default=100)
    resource_type = Column(String(16), nullable=False)
    table_pattern = Column(String(255), nullable=True)
    column_pattern = Column(String(255), nullable=True)
    tag_name = Column(String(128), nullable=True)
    min_clearance = Column(String(16), nullable=False, default="public")
    mask_type = Column(String(16), nullable=True)
    row_filter = Column(String(1000), nullable=True)
    valid_from = Column(DateTime(timezone=True), nullable=True)
    valid_to = Column(DateTime(timezone=True), nullable=True)
    enabled = Column(SmallInteger, nullable=False, default=1)
    created_by = Column(String(64), nullable=True)
    note = Column(String(500), nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "knowledge_base_id": self.knowledge_base_id,
            "name": self.name, "description": self.description,
            "effect": self.effect, "priority": self.priority,
            "resource_type": self.resource_type,
            "table_pattern": self.table_pattern, "column_pattern": self.column_pattern,
            "tag_name": self.tag_name, "min_clearance": self.min_clearance,
            "mask_type": self.mask_type, "row_filter": self.row_filter,
            "valid_from": self.valid_from.isoformat() if self.valid_from else None,
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
            "enabled": bool(self.enabled), "created_by": self.created_by,
            "note": self.note,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class GovernanceDecisionAudit(TenantMixin, Base):
    __tablename__ = "governance_decision_audit"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(String(64), primary_key=True)
    request_id = Column(String(64), nullable=True)
    user_id = Column(String(64), nullable=True)
    knowledge_base_id = Column(String(128), nullable=False, index=True)
    data_source_id = Column(String(64), nullable=True)
    action = Column(String(16), nullable=False)
    resource_table = Column(String(255), nullable=True)
    resource_column = Column(String(255), nullable=True)
    level = Column(String(16), nullable=True)
    decision = Column(String(16), nullable=False)
    policy_id = Column(String(64), nullable=True)
    reason = Column(String(500), nullable=True)
    sql_hash = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "request_id": self.request_id, "user_id": self.user_id,
            "knowledge_base_id": self.knowledge_base_id,
            "data_source_id": self.data_source_id, "action": self.action,
            "resource_table": self.resource_table,
            "resource_column": self.resource_column, "level": self.level,
            "decision": self.decision, "policy_id": self.policy_id,
            "reason": self.reason, "sql_hash": self.sql_hash,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


__all__ = [
    "GovernanceTagDef", "GovernanceLabel", "GovernanceUserClearance",
    "GovernancePolicy", "GovernanceDecisionAudit",
]
