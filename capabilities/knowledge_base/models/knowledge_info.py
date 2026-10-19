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
    #                  / openkb / jonex_wiki（Wiki 编译型：LLM 编译为结构化 Wiki）
    # 创建时选定，之后不可变（改类型意味着旧数据全部失效）。
    # 原名 pipeline_type，位于 service_knowledge_bases。
    kb_type = Column(String(16), nullable=False, default="lightrag")
    # [jonex] 库级派单值（2026-10-09）：这个库走哪条入库链。当前仅 `rag` 一条链；
    # **NULL 是合法值** = 没表态、跟随服务端开关（`KB_INGEST_BACKEND`）—— 存量库全是
    # NULL，行为与从前逐字一致。
    # 建列脚本：`deploy/postgres/update/045_knowledge_info_ingest_backend.sql`。
    ingest_backend = Column(String(32), nullable=True)
    # [jonex] P2 库级派单值：这个库的 **compile 步**（本体编译）走哪个后端，
    # `rag`（atomic-rag 现状）/ `ontology_compile`（独立编译服务）。
    # **NULL 是合法值** = 没表态、跟随服务端开关（`ONTOLOGY_COMPILE_BACKEND`）—— 存量库全是
    # NULL ⇒ 未设库值即跟随开关（P4a 2026-10-17 起开关缺省已翻为 `ontology_compile`）。
    # ⚠ 与上一列是**两把钥匙、两个语义**：`ingest_backend` 管解析链，本列管 compile 步；
    # 前者写进 `extra_metadata["ingest_backend"]`，后者写进 `extra_metadata["ontology_compile_backend"]`。
    # 建列脚本：`deploy/postgres/update/057_knowledge_info_ontology_compile_backend.sql`。
    # ⚠ 刻意**不进 `to_dict`**：库级 API 暴露（前端/接口层）是设计 §5.5 的独立项，本期不做
    # （本期只有列 + env）；读取走 `kb_type_service.get_kb_ontology_compile_backend` 直查列。
    ontology_compile_backend = Column(String(32), nullable=True)

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
            "ingest_backend": self.ingest_backend,  # [jonex] NULL = 跟随服务端开关
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if space_name:
            result["space_name"] = space_name
        return result