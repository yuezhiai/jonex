#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""KB 级模型覆盖模型。

只存**覆盖值**：某 KB 某 scope 没配就没有行（没有行 = 继承引擎默认）。
清空配置即硬删本行，与 ``platform.tenant_security_configs`` 的硬删语义一致
（那张表也没有 ``is_deleted``）——软删会让"清空"留下幽灵行，
唯一约束还得带 ``WHERE is_deleted=0`` 的部分索引才能成立。

设计见 ``docs/knowledge-base/kb-model-settings-design.md`` §4.1。
"""

import uuid

from sqlalchemy import Column, String, UniqueConstraint

from jonex_core.common.database import Base
from jonex_core.common.entity import AuditMixin, TenantMixin, TimestampMixin


class KnowledgeModelSetting(TenantMixin, TimestampMixin, AuditMixin, Base):
    """知识库级模型覆盖，一行对应 (KB, scope)。"""

    __tablename__ = "knowledge_model_settings"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "knowledge_base_id",
            "scope",
            name="uq_kb_model_settings_scope",
        ),
        {"schema": "knowledge_base"},
    )

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    knowledge_base_id = Column(String(128), nullable=False, index=True)
    #: parse_multimodal | compile_ontology | compile_wiki（见 services/kb_model_config.py）
    scope = Column(String(32), nullable=False)
    #: 用户自由填写的模型名；连接信息（host/key/binding）仍由容器 env 决定
    model_name = Column(String(255), nullable=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "knowledge_base_id": self.knowledge_base_id,
            "scope": self.scope,
            "model_name": self.model_name,
            "created_by": self.created_by,
            "updated_by": self.updated_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


__all__ = ["KnowledgeModelSetting"]
