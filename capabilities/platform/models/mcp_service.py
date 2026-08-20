import uuid

from sqlalchemy import Column, Integer, String, Text, TIMESTAMP, UniqueConstraint
from sqlalchemy.sql import func

from jonex_core.common.database import Base


class McpServicePublish(Base):
    """MCP 服务发布状态表（v1.4 Phase 2 E6-E8）

    独立存储 MCP 发布状态，避免跨 capability 修改 knowledge_base schema。
    service_id 对应 knowledge_base.services.id，UNIQUE(tenant_id, service_id)
    保证同租户下同一服务只能有一条发布记录。
    """

    __tablename__ = "mcp_service_publish"
    __table_args__ = (
        UniqueConstraint("tenant_id", "service_id"),
        {"schema": "platform"},
    )

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    tenant_id = Column(String(64), nullable=False)
    service_id = Column(String(64), nullable=False)
    is_published = Column(Integer, nullable=False, default=0)
    published_at = Column(TIMESTAMP(timezone=True))
    published_by = Column(String(64))
    tool = Column(String(128))
    tool_description = Column(Text)
    service_type = Column(String(32), nullable=False, default="domain")
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), onupdate=func.now())
