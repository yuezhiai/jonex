"""领域服务 API Key 实体（v1.4 Phase 15 DS-04）。

service_id 对应 knowledge_base.services.id；明文仅创建时一次性返回，绝不落库。
数据库仅存 key_hash（HMAC-SHA256 单向哈希），不存 plaintext。
"""
from sqlalchemy import Column, Integer, String, TIMESTAMP

from jonex_core.common.database import Base


class McpServiceApiKey(Base):
    """领域服务 API Key。

    字段与 DDL（deploy/postgres/migrations/002_platform.sql）一一对应。
    created_at 不设 Python default，由 service 层显式写 datetime.now(timezone.utc)。
    """

    __tablename__ = "mcp_service_api_keys"
    __table_args__ = {"schema": "platform"}

    id = Column(String(64), primary_key=True)
    tenant_id = Column(String(64), nullable=False)
    service_id = Column(String(64), nullable=False)
    name = Column(String(255), nullable=False, default="")
    key_prefix = Column(String(32), nullable=False, default="")
    key_hash = Column(String(64), nullable=False)
    expires_at = Column(TIMESTAMP(timezone=True))
    revoked_at = Column(TIMESTAMP(timezone=True))
    created_at = Column(TIMESTAMP(timezone=True), nullable=False)
    is_deleted = Column(Integer, nullable=False, default=0)
