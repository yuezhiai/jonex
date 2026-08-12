from sqlalchemy import Column, Integer, String, Text, TIMESTAMP, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB

from jonex_core.common.database import Base

# permission_level 白名单 — call=可调用（搜索+阅读），view=仅查看（阅读）
# write=可写入（含 call + view），*=全能力通配（跳过所有 service 检查）
VALID_PERMISSION_LEVELS = {"call", "view", "write", "*"}


class McpKey(Base):
    __tablename__ = "mcp_keys"
    __table_args__ = {"schema": "platform"}

    id = Column(String(64), primary_key=True)
    tenant_id = Column(String(64), nullable=False)
    name = Column(String(255), nullable=False, default="")
    key_prefix = Column(String(32), nullable=False, default="")
    key_hash = Column(String(64), nullable=False)
    permissions = Column(String(512), nullable=False, default="read")
    allowed_kb_ids = Column(JSONB, nullable=False, default=[])
    created_by = Column(String(128))
    created_at = Column(TIMESTAMP(timezone=True), nullable=False)
    revoked_at = Column(TIMESTAMP(timezone=True))
    expires_at = Column(TIMESTAMP(timezone=True))
    last_used_at = Column(TIMESTAMP(timezone=True))
    last_used_ip = Column(String(64))
    org_id = Column(String(64))
    is_deleted = Column(Integer, nullable=False, default=0)


class McpKeyServiceMapping(Base):
    """MCP Key ↔ 领域服务映射中间表（v1.3 Phase 2 B2 前置）

    联合主键 (mcp_key_id, service_id)。
    无外键约束、无软删除——关联完全由应用层管理。
    """

    __tablename__ = "mcp_key_service_mappings"
    __table_args__ = {"schema": "platform"}

    mcp_key_id = Column(String(64), primary_key=True)
    service_id = Column(String(64), primary_key=True)
    permission_level = Column(String(16), nullable=False, default="call")


class McpOrganization(Base):
    """MCP Key 归属组织。

    租户级组织实体，用于将 MCP Key 分组管理。
    UNIQUE(tenant_id, name) 约束保证同租户下组织名唯一。
    """

    __tablename__ = "mcp_organizations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name"),
        {"schema": "platform"},
    )

    id = Column(String(64), primary_key=True)
    tenant_id = Column(String(64), nullable=False)
    name = Column(String(255), nullable=False)
    description = Column(Text)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False)
    is_deleted = Column(Integer, nullable=False, default=0)
