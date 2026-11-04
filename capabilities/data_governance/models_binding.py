"""W2.6 标准资产 ↔ 数据资产落标绑定。

迁移见 ``deploy/postgres/migrations/019_data_governance_assets.sql``
（2026-11-04 起原 ``023_standard_asset_bindings.sql`` 已并入；``update/`` 下为逐字节同名镜像）。时间列必须显式 ``DateTime(timezone=True)``：
迁移落的是 ``TIMESTAMPTZ``，沿用 ``TimestampMixin`` 的 naive 列会让
asyncpg 按 ``TIMESTAMP WITHOUT TIME ZONE`` 发参数（见 ``models_contract.py``
/ ``models_quality.py`` 的说明）。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, Numeric, SmallInteger, String

from jonex_core.common.database import Base
from jonex_core.common.entity import TenantMixin, TimestampMixin


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value) -> str | None:
    return value.isoformat() if value else None


class StandardAssetBinding(TenantMixin, TimestampMixin, Base):
    """一条落标绑定：某个标准资产（术语 / 数据元 / 指标口径卡）落到某个数据资产。"""

    __tablename__ = "standard_asset_bindings"
    __table_args__ = {"schema": "data_governance"}

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    standard_kind = Column(String(32), nullable=False)
    standard_code = Column(String(128), nullable=False)
    standard_asset_id = Column(String(64), nullable=True)
    asset_type = Column(String(16), nullable=False)
    asset_id = Column(String(255), nullable=False)
    source = Column(String(16), nullable=False, default="manual")
    confidence = Column(Numeric(5, 4), nullable=False, default=1.0)
    note = Column(String(512), nullable=True)
    status = Column(String(16), nullable=False, default="active")
    created_by = Column(String(64), nullable=True)
    updated_by = Column(String(64), nullable=True)
    revision = Column(Integer, nullable=False, default=1)
    is_deleted = Column(SmallInteger, nullable=False, default=0)
    # 覆盖 mixin 的 naive 列（迁移是 TIMESTAMPTZ）。
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now,
    )

    def to_dict(self) -> dict:
        """行字典（snake_case，覆盖全部列）。"""
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "standard_kind": self.standard_kind,
            "standard_code": self.standard_code,
            "standard_asset_id": self.standard_asset_id,
            "asset_type": self.asset_type,
            "asset_id": self.asset_id,
            "source": self.source,
            "confidence": (
                float(self.confidence) if self.confidence is not None else None
            ),
            "note": self.note,
            "status": self.status,
            "created_by": self.created_by,
            "updated_by": self.updated_by,
            "revision": self.revision,
            "is_deleted": self.is_deleted,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }
