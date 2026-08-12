"""
知识库信息模型 — KnowledgeInfo
"""
import uuid

from sqlalchemy import Column, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB

from jonex_core.common.database import Base
from jonex_core.common.entity import SoftDeleteMixin, TenantMixin, TimestampMixin


class KnowledgeInfo(TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    """知识库信息 — 领域知识管理的元数据主表"""

    __tablename__ = "knowledge_info"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    space_id = Column(String(64), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text)
    data_source_types = Column(JSONB, default=list)
    document_count = Column(Integer, default=0)
    status = Column(String(32), default="synced")
    owner_id = Column(String(64))
    # [jonex] 知识库类型：lightrag（标准检索型：向量+图谱+本体）
    #                  / openkb（Wiki 编译型：LLM 编译为结构化 Wiki）
    # 创建时选定，之后不可变（改类型意味着旧数据全部失效）。
    # 原名 pipeline_type，位于 service_knowledge_bases。
    kb_type = Column(String(16), nullable=False, default="lightrag")

    def to_dict(self, space_name: str = None):
        result = {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "space_id": self.space_id,
            "name": self.name,
            "description": self.description,
            "data_source_types": self.data_source_types or [],
            "document_count": self.document_count or 0,
            "status": self.status,
            "owner_id": self.owner_id,
            "kb_type": self.kb_type,  # [jonex]
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if space_name:
            result["space_name"] = space_name
        return result