from sqlalchemy import Column, Integer, String, TIMESTAMP
from sqlalchemy.dialects.postgresql import JSONB

from jonex_core.common.database import Base

# permission_level 白名单 — call=可调用（搜索+阅读），view=仅查看（阅读）
# write / * 已物理删除（jonex-29l）：写入能力移交独立知识写入 Key
VALID_PERMISSION_LEVELS = {"call", "view"}


class McpKey(Base):
    __tablename__ = "mcp_keys"
    __table_args__ = {"schema": "platform"}

    id = Column(String(64), primary_key=True)
    tenant_id = Column(String(64), nullable=False)
    name = Column(String(255), nullable=False, default="")
    note = Column(String(512))
    key_prefix = Column(String(32), nullable=False, default="")
    key_hash = Column(String(64), nullable=False)
    permissions = Column(String(512), nullable=False, default="view")
    allowed_kb_ids = Column(JSONB, nullable=False, default=[])
    space_id = Column(String(64), nullable=True)
    created_by = Column(String(128))
    created_at = Column(TIMESTAMP(timezone=True), nullable=False)
    revoked_at = Column(TIMESTAMP(timezone=True))
    expires_at = Column(TIMESTAMP(timezone=True))
    disabled_at = Column(TIMESTAMP(timezone=True))
    last_used_at = Column(TIMESTAMP(timezone=True))
    last_used_ip = Column(String(64))
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
