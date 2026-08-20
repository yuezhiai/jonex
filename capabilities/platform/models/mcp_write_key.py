"""知识写入 Key 实体（v1.4 Phase 17 WRITE-01）。

知识写入 Key 授予对知识库的写入权限（grants JSONB）。
明文 Key 仅在创建响应一次性返回，绝不落库（无明文列，仅存 key_hash HMAC-SHA256 单向哈希）。
4 态生命周期（revoked > expired > disabled > active）由 _derive_status 派生，
status 为逻辑字段，不落独立列。
"""
from datetime import datetime, timezone

from sqlalchemy import Column, Integer, String, TIMESTAMP, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB

from jonex_core.common.database import Base


class McpWriteKey(Base):
    """知识写入 Key。

    字段与 DDL（deploy/postgres/migrations/002_platform.sql）一一对应。
    created_at 不设 Python default，由 17-02 service 层显式写 datetime.now(timezone.utc)。
    space_id 由 grants[0].kb 反推、kb_id = grants[0].kb 冗余索引（17-02 计算）。
    """

    __tablename__ = "mcp_write_keys"
    __table_args__ = (
        UniqueConstraint("key_hash", name="uq_mcp_write_keys_key_hash"),
        {"schema": "platform"},
    )

    id = Column(String(64), primary_key=True)
    tenant_id = Column(String(64), nullable=False)
    name = Column(String(255), nullable=False, default="")
    key_prefix = Column(String(32), nullable=False, default="")
    key_hash = Column(String(64), nullable=False)
    grants = Column(JSONB, nullable=False, default=[])  # [{kb, mode, directories[]}]
    space_id = Column(String(64), nullable=True)
    kb_id = Column(String(64), nullable=True)
    disabled_at = Column(TIMESTAMP(timezone=True))
    revoked_at = Column(TIMESTAMP(timezone=True))
    revoked_by = Column(String(128))
    expires_at = Column(TIMESTAMP(timezone=True))
    created_at = Column(TIMESTAMP(timezone=True), nullable=False)
    created_by = Column(String(128))
    updated_at = Column(TIMESTAMP(timezone=True))
    is_deleted = Column(Integer, nullable=False, default=0)

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
