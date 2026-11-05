"""
Knowledge Base — 本体绑定与编译结果模型。

对应表：
- knowledge_base.ontology_template_bindings
- knowledge_base.ontology_compiled_schemas
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB

from jonex_core.common.database import Base
from jonex_core.common.entity import TenantMixin, TimestampMixin


class OntologyTemplateBinding(TenantMixin, TimestampMixin, Base):
    """知识库-模板绑定表，记录每个 KB 绑定的模板领域和场景。"""

    __tablename__ = "ontology_template_bindings"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(Integer, primary_key=True, autoincrement=True)
    knowledge_base_id = Column(String(128), nullable=False, index=True)
    template_domain_id = Column(String(64), nullable=True)
    template_scenario_id = Column(String(64), nullable=True)
    source_type = Column(String(32), nullable=False, default="business_template")
    status = Column(String(32), nullable=False, default="active")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "knowledge_base_id": self.knowledge_base_id,
            "template_domain_id": self.template_domain_id,
            "template_scenario_id": self.template_scenario_id,
            "source_type": self.source_type,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class OntologyCompiledSchema(TenantMixin, TimestampMixin, Base):
    """本体编译快照。

    粒度是**知识库 + 数据源**：一个知识库可以挂多个数据源（含不同引擎的数据库），
    每个数据源有自己的一份 schema。`data_source_id` 为 NULL 表示**知识库级**
    ——不绑定具体数据源，是改造前的形态，也用于模板种子出来、不属于任何数据源的那些。

    唯一性由两个**部分唯一索引**保证（普通 UNIQUE 不约束 NULL，见
    `deploy/postgres/update/037_data_source_scoped_schema.sql`）：
      uq_ocs_kb_level       (tenant, kb)         WHERE data_source_id IS NULL
      uq_ocs_per_data_source(tenant, kb, ds_id)  WHERE data_source_id IS NOT NULL
    """

    __tablename__ = "ontology_compiled_schemas"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(Integer, primary_key=True, autoincrement=True)
    knowledge_base_id = Column(String(128), nullable=False, index=True)
    # NULL = 知识库级（不绑定具体数据源）
    data_source_id = Column(String(64), nullable=True, index=True)
    template_domain_id = Column(String(64), nullable=True)
    template_scenario_id = Column(String(64), nullable=True)
    source_type = Column(String(32), nullable=False, default="business_template")
    source_version = Column(Integer, nullable=False, default=1)
    source_hash = Column(String(64), nullable=True)
    schema_version = Column(Integer, nullable=False, default=1)
    entity_types = Column(JSONB, nullable=False, default=list)
    relation_types = Column(JSONB, nullable=False, default=list)
    constraints = Column(JSONB, nullable=False, default=list)
    disambiguation = Column(
        JSONB,
        nullable=False,
        default={"case_insensitive": True, "alias_merge": True},
    )
    # P1-1：生成期有界探测的派生证据（`{probed_at, sample_rows, tables:{表:{columns:{列:{…}}}}, errors}`）。
    # **不是语义的一部分**：`upsert_compiled_schema` 不写它（画布保存不会洗掉），
    # 它也**不进 `to_dict()`**（API 契约不变）。
    evidence = Column(JSONB, nullable=False, default=dict)
    prompt_schema = Column(JSONB, nullable=False, default=dict)
    status = Column(String(32), nullable=False, default="active")
    schema_mode = Column(String(32), nullable=False, default="template_seeded")
    sync_status = Column(String(32), nullable=False, default="synced")
    edited_at = Column(DateTime(timezone=True), nullable=True)
    edited_by = Column(String(128), nullable=True)
    compiled_at = Column(DateTime(timezone=True), nullable=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "knowledge_base_id": self.knowledge_base_id,
            "template_domain_id": self.template_domain_id,
            "template_scenario_id": self.template_scenario_id,
            "source_type": self.source_type,
            "source_version": self.source_version,
            "source_hash": self.source_hash,
            "schema_version": self.schema_version,
            # None = 知识库级。前端据此判断这份 schema 属于哪个数据源
            "data_source_id": self.data_source_id,
            "entity_types": self.entity_types or [],
            "relation_types": self.relation_types or [],
            "constraints": self.constraints or [],
            "disambiguation": self.disambiguation or {},
            "prompt_schema": self.prompt_schema or {},
            "status": self.status,
            "schema_mode": self.schema_mode,
            "sync_status": self.sync_status,
            "edited_at": self.edited_at.isoformat() if self.edited_at else None,
            "edited_by": self.edited_by,
            "compiled_at": self.compiled_at.isoformat() if self.compiled_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


__all__ = ["OntologyTemplateBinding", "OntologyCompiledSchema"]
