"""
知识库 — 领域空间模型
"""
import uuid

from sqlalchemy import Column, Integer, String, Text

from jonex_core.common.database import Base
from jonex_core.common.entity import SoftDeleteMixin, TenantMixin, TimestampMixin


class Space(TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "spaces"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    name = Column(String(255), nullable=False)
    description = Column(Text)
    # [jonex] 权限重构 B2（D2）：**仅创建人记录，不参与权限判定**。
    # 空间管理权限一律查 space_permissions（role='space_manager'）。
    # 曾经的行为是「只要是 owner_id 就有最高权限，即使 space_permissions 无记录」，
    # 已在 space_permission_service 的三个函数与 space_service.list 里全部移除。
    # 保留本列是为了审计/展示（谁建的），**不要把它接回任何判定表达式**。
    # 回归护栏：tests/unit/test_space_role_d2.py::TestOwnerIdNotInJudgement
    owner_id = Column(String(64))
    status = Column(String(32), default="active")
    knowledge_base_count = Column(Integer, default=0)
    service_count = Column(Integer, default=0)

    def to_dict(self):
        return {
            "id": self.id, "tenant_id": self.tenant_id,
            "name": self.name, "description": self.description,
            "owner_id": self.owner_id,
            "status": self.status,
            "knowledge_base_count": self.knowledge_base_count,
            "service_count": self.service_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class SpacePermission(TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "space_permissions"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    space_id = Column(String(64), nullable=False, index=True)
    user_id = Column(String(64), nullable=False, index=True)
    # [jonex] 权限重构 B2：默认值 viewer → member（取值收为 space_manager / member）。
    # 注意 models/kb_permission.py 的同名字段默认值属 KB 层，由 B3 处理 ——
    # 两张表默认值同源但不同层，容易只改一个。
    role = Column(String(32), nullable=False, default="member")
