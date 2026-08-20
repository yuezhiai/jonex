"""LLM-Wiki Schema 编译设置模型（kb_type=openkb 的编译配置权威源）。

对应表：knowledge_base.llm_wiki_schemas
方案：docs/llmwiki-schema-settings-execution-plan.md

留档模型：每次保存递增 schema_version，旧 active 行转 archived；
partial unique index（WHERE status='active'）保证每 KB 单 active。
sync_status 与 status 分开：status 是留档语义（active/archived），
sync_status 是 apply_schema 同步状态（synced/apply_failed）。
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB

from jonex_core.common.database import Base
from jonex_core.common.entity import TenantMixin, TimestampMixin

# 状态枚举（与方案 §3/§7 一致）
STATUS_ACTIVE = "active"
STATUS_ARCHIVED = "archived"
SYNC_SYNCED = "synced"
SYNC_APPLY_FAILED = "apply_failed"


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


class LlmWikiSchema(TenantMixin, TimestampMixin, Base):
    """KB 级 LLM-Wiki Schema（编译设置）留档行。"""

    __tablename__ = "llm_wiki_schemas"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(Integer, primary_key=True, autoincrement=True)
    knowledge_base_id = Column(String(128), nullable=False, index=True)
    schema_version = Column(Integer, nullable=False, default=1)
    status = Column(String(32), nullable=False, default=STATUS_ACTIVE)
    sync_status = Column(String(32), nullable=False, default=SYNC_SYNCED)

    schema_name = Column(String(128), nullable=False, default="default")
    language = Column(String(32), nullable=False, default="zh-CN")
    model = Column(String(128), nullable=True)

    entity_types = Column(JSONB, nullable=False, default=list)
    concept_types = Column(JSONB, nullable=False, default=list)  # 概念类型词表（AGENTS.md 引导，无硬校验）
    # [jonex] 用户自定义追加块（多行 markdown，渲染时原样拼到 AGENTS.md 末尾）
    # 页面规则/编译规则等均由此块承载（此前结构化的 page_types/compile_rules 已移除）
    agents_md_extra = Column(Text, nullable=False, default="")

    agents_md = Column(Text, nullable=False)
    config_snapshot = Column(JSONB, nullable=False, default=dict)

    edited_by = Column(String(128), nullable=True)
    edited_at = Column(DateTime, nullable=True)
    applied_at = Column(DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "knowledge_base_id": self.knowledge_base_id,
            "schema_version": self.schema_version,
            "status": self.status,
            "sync_status": self.sync_status,
            "schema_name": self.schema_name,
            "language": self.language,
            "model": self.model,
            "entity_types": self.entity_types or [],
            "concept_types": self.concept_types or [],
            "agents_md_extra": self.agents_md_extra or "",
            "agents_md": self.agents_md,
            "edited_by": self.edited_by,
            "edited_at": _iso(self.edited_at),
            "applied_at": _iso(self.applied_at),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }
