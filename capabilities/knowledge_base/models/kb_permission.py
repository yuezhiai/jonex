"""知识库授权成员模型（kb_permissions 表，设计 2026-08-20-kb-permission-design）。"""
import uuid

from sqlalchemy import Column, String

from jonex_core.common.database import Base
from jonex_core.common.entity import SoftDeleteMixin, TenantMixin, TimestampMixin


class KbPermission(TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "kb_permissions"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    kb_id = Column(String(64), nullable=False, index=True)
    user_id = Column(String(64), nullable=False, index=True)
    role = Column(String(32), nullable=False, default="viewer")
