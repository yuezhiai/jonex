"""W2.3 数据合同（每版一行）。"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, SmallInteger, String
from sqlalchemy.dialects.postgresql import JSONB

from jonex_core.common.database import Base
from jonex_core.common.entity import TenantMixin, TimestampMixin


class DataContract(TenantMixin, TimestampMixin, Base):
    __tablename__ = "data_contracts"
    __table_args__ = {"schema": "data_governance"}

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    data_source_id = Column(String(64), nullable=False)
    name = Column(String(255), nullable=False)
    version = Column(Integer, nullable=False)
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

    # 覆盖 mixin 的 naive 列（迁移是 TIMESTAMPTZ）。
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
        """行字典（snake_case）；服务层再按需组装响应。"""
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "data_source_id": self.data_source_id,
            "name": self.name,
            "version": self.version,
            "payload": self.payload or {},
            "status": self.status,
            "revision": self.revision,
            "created_by": self.created_by,
            "updated_by": self.updated_by,
            "published_by": self.published_by,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "is_deleted": self.is_deleted,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
