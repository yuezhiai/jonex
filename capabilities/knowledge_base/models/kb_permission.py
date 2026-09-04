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
    # [jonex] 权限重构 B3（D1）：默认值 viewer → member（取值收为 kb_manager / member）。
    # 注意 models/space.py 的同名字段属**空间层**，由 B2 处理 ——
    # 两张表默认值同源但不同层，容易只改一个。
    role = Column(String(32), nullable=False, default="member")
