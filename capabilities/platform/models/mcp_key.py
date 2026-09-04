from datetime import datetime, timezone

from sqlalchemy import Column, Integer, String, TIMESTAMP
from sqlalchemy.dialects.postgresql import JSONB

from jonex_core.common.database import Base


class McpKey(Base):
    __tablename__ = "mcp_keys"
    __table_args__ = {"schema": "platform"}

    @staticmethod
    def _derive_status(key) -> str:
        """派生 4 态状态（逻辑字段，不落库）。

        优先级：revoked（revoked_at 非空，撤销不可逆终态最优先）
        > expired（expires_at 到期）> disabled（disabled_at 非空）> active。
        时间用 datetime.now(timezone.utc)——async ORM 禁 func.now()（MissingGreenlet 陷阱）。
        """
        if key.revoked_at:
            return "revoked"
        if key.expires_at and key.expires_at <= datetime.now(timezone.utc):
            return "expired"
        if key.disabled_at:
            return "disabled"
        return "active"

    id = Column(String(64), primary_key=True)
    tenant_id = Column(String(64), nullable=False)
    name = Column(String(255), nullable=False, default="")
    note = Column(String(512))
    key_prefix = Column(String(32), nullable=False, default="")
    key_hash = Column(String(64), nullable=False)
    allowed_kb_ids = Column(JSONB, nullable=False, default=[])
    space_id = Column(String(64), nullable=True)
    write_grants = Column(JSONB, nullable=True)
    client_request_id = Column(String(64), nullable=True)
    created_by = Column(String(128))
    created_at = Column(TIMESTAMP(timezone=True), nullable=False)
    revoked_at = Column(TIMESTAMP(timezone=True))
    revoked_by = Column(String(128))
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
