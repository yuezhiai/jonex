#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""
统一实体 mixin。
"""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, SmallInteger, String
from sqlalchemy.orm import declared_attr


class TenantMixin:
    """租户隔离实体基础字段。"""

    @declared_attr
    def tenant_id(cls):
        return Column(String(64), nullable=False, index=True)


class TimestampMixin:
    """创建/更新时间字段。"""

    created_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class SoftDeleteMixin:
    """软删除字段：0-正常，1-删除。"""

    is_deleted = Column(SmallInteger, default=0, nullable=False, index=True)


class AuditMixin:
    """审计字段。"""

    created_by = Column(String(64), nullable=True)
    updated_by = Column(String(64), nullable=True)


__all__ = [
    "AuditMixin",
    "SoftDeleteMixin",
    "TenantMixin",
    "TimestampMixin",
]
