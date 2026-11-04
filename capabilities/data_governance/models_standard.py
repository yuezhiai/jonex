"""W2.2 标准资产（每版一行）。"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, SmallInteger, String
from sqlalchemy.dialects.postgresql import JSONB

from jonex_core.common.database import Base
from jonex_core.common.entity import TenantMixin, TimestampMixin


class StandardAsset(TenantMixin, TimestampMixin, Base):
    __tablename__ = "standard_assets"
    __table_args__ = {"schema": "data_governance"}

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    kind = Column(String(32), nullable=False)
    code = Column(String(128), nullable=False)
    version = Column(Integer, nullable=False)
    name = Column(String(255), nullable=False)
    # none_as_null=True：None 必须绑定为 SQL NULL（触发 NOT NULL 约束），
    # 而不是被写成 JSON 'null'。
    payload = Column(JSONB(none_as_null=True), nullable=False, default=dict)
    status = Column(String(16), nullable=False, default="draft")
    revision = Column(Integer, nullable=False, default=1)
    created_by = Column(String(64), nullable=True)
    updated_by = Column(String(64), nullable=True)
    published_by = Column(String(64), nullable=True)
    published_at = Column(DateTime(timezone=True), nullable=True)
    is_deleted = Column(SmallInteger, nullable=False, default=0)
    # 覆盖 TimestampMixin 的 naive 列：迁移 019 的 created_at/updated_at 是
    # TIMESTAMPTZ，而 create_draft 显式绑定 aware UTC 时间。若沿用 mixin 的
    # naive DateTime，asyncpg 会按 TIMESTAMP WITHOUT TIME ZONE 发参数并报
    # 「can't subtract offset-naive and offset-aware datetimes」（2026-10-19 真机）。
    created_at = Column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self) -> dict:
        return {
            "asset_id": self.id,
            "kind": self.kind,
            "code": self.code,
            "version": self.version,
            "name": self.name,
            "payload": self.payload or {},
            "status": self.status,
            "revision": self.revision,
            "updated_by": self.updated_by,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
