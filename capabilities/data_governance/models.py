"""数据治理 — 资产元数据侧表。"""
import uuid

from sqlalchemy import Boolean, Column, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB

from jonex_core.common.database import Base
from jonex_core.common.entity import TenantMixin, TimestampMixin


class AssetMetadata(TenantMixin, TimestampMixin, Base):
    __tablename__ = "asset_metadata"
    __table_args__ = (
        UniqueConstraint("tenant_id", "asset_type", "asset_id",
                         name="uq_asset_metadata"),
        {"schema": "data_governance"},
    )

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    asset_type = Column(String(32), nullable=False)
    asset_id = Column(String(64), nullable=False)
    owner = Column(String(128), nullable=True)
    domain = Column(String(64), nullable=True)
    sensitivity = Column(String(32), nullable=True)
    # none_as_null=True：None 必须绑定为 SQL NULL，否则 JSONB 会写成 JSON
    # 'null'，触发 ck_asset_metadata_classification_categories 约束。
    classification_categories = Column(JSONB(none_as_null=True), nullable=True)

    def to_dict(self) -> dict:
        return {
            "asset_type": self.asset_type,
            "asset_id": self.asset_id,
            "owner": self.owner,
            "domain": self.domain,
            "sensitivity": self.sensitivity,
            "classification_categories": self.classification_categories,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class RetentionPolicy(TenantMixin, TimestampMixin, Base):
    """三级保留策略（tenant / kb / document）；空值表示继承。"""

    __tablename__ = "retention_policy"
    __table_args__ = (
        UniqueConstraint("tenant_id", "scope_type", "scope_id",
                         name="uq_retention_scope"),
        {"schema": "data_governance"},
    )

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    scope_type = Column(String(16), nullable=False)
    scope_id = Column(String(64), nullable=False, default="")
    retention_days = Column(Integer, nullable=True)
    grace_days = Column(Integer, nullable=True)
    updated_by = Column(String(128), nullable=True)

    def to_dict(self) -> dict:
        return {
            "scope_type": self.scope_type,
            "scope_id": self.scope_id,
            "retention_days": self.retention_days,
            "grace_days": self.grace_days,
            "updated_by": self.updated_by,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ClassificationLevel(TenantMixin, TimestampMixin, Base):
    """分级词表；``rank`` 越大越敏感。"""

    __tablename__ = "classification_level"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_classification_level_code"),
        {"schema": "data_governance"},
    )

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    code = Column(String(32), nullable=False)
    name = Column(String(64), nullable=False)
    rank = Column(Integer, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)

    def to_dict(self) -> dict:
        return {"code": self.code, "name": self.name, "rank": self.rank,
                "is_active": bool(self.is_active)}


class ClassificationCategory(TenantMixin, TimestampMixin, Base):
    """分类词表。"""

    __tablename__ = "classification_category"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_classification_category_code"),
        {"schema": "data_governance"},
    )

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    code = Column(String(32), nullable=False)
    name = Column(String(64), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)

    def to_dict(self) -> dict:
        return {"code": self.code, "name": self.name,
                "is_active": bool(self.is_active)}


class ClassificationDefault(TenantMixin, TimestampMixin, Base):
    """租户/知识库级默认分级分类；NULL 表示未设置（继承或使用系统默认）。"""

    __tablename__ = "classification_default"
    __table_args__ = (
        UniqueConstraint("tenant_id", "scope_type", "scope_id",
                         name="uq_classification_default_scope"),
        {"schema": "data_governance"},
    )

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    scope_type = Column(String(16), nullable=False)
    scope_id = Column(String(64), nullable=False, default="")
    level_code = Column(String(32), nullable=True)
    # none_as_null=True：显式清空分类时必须落 SQL NULL；JSON 'null' 会撞
    # ck_classification_default_categories。
    categories = Column(JSONB(none_as_null=True), nullable=True)
    updated_by = Column(String(128), nullable=True)

    def to_dict(self) -> dict:
        return {"scope_type": self.scope_type, "scope_id": self.scope_id,
                "level_code": self.level_code, "categories": self.categories,
                "updated_by": self.updated_by}


class UserClearance(TenantMixin, TimestampMixin, Base):
    """用户可读最高等级；无记录表示默认最低启用等级。"""

    __tablename__ = "user_clearance"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="uq_user_clearance_user"),
        {"schema": "data_governance"},
    )

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    user_id = Column(String(64), nullable=False)
    max_level_code = Column(String(32), nullable=False)
    updated_by = Column(String(128), nullable=True)

    def to_dict(self) -> dict:
        return {"user_id": self.user_id,
                "max_level_code": self.max_level_code,
                "updated_by": self.updated_by,
                "updated_at": self.updated_at.isoformat() if self.updated_at else None}
