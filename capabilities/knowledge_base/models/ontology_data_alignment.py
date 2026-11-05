#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Knowledge Base — 跨模态对齐（文档实体类型 ↔ 数据源实体，人审）。

对应表：`knowledge_base.ontology_data_alignment`（迁移 011）。

每一行是一条**人审对齐**：
- `doc_type`：文档侧实体类型（`OntologyEntity.canonical_name`）；
- `data_source_id` + `entity_name`：数据源侧实体（物理表名经该源编译 schema 解析）；
- `status`：只许 `proposed|active|rejected` —— **只有 active 投影 `MAPS_TO`**。

`proposed` 是建议（同名/别名命中），不自动生效；`rejected` 保留行，
避免下次生成建议时把同一条重复提出来。
"""
from sqlalchemy import Column, String

from jonex_core.common.database import Base
from jonex_core.common.entity import TenantMixin, TimestampMixin
from jonex_core.common.exceptions import InvalidParameterError

#: 对齐状态白名单（与迁移 011 的 `ck_oda_status` 一一对应）。
VALID_ALIGNMENT_STATUSES = ("proposed", "active", "rejected")


def validate_status(status: str) -> str:
    """校验对齐状态；非法值抛 `InvalidParameterError`（1001）。

    返回原值，便于调用方链式使用（`row.status = validate_status(status)`）。
    """
    if status not in VALID_ALIGNMENT_STATUSES:
        raise InvalidParameterError(
            message=(
                f"非法对齐状态：{status!r}"
                f"（允许：{'/'.join(VALID_ALIGNMENT_STATUSES)}）"
            ),
            details={"status": status},
        )
    return status


class OntologyDataAlignment(TenantMixin, TimestampMixin, Base):
    """一条跨模态对齐记录（tenant/kb/doc_type/源/实体 唯一）。"""

    __tablename__ = "ontology_data_alignment"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(String(64), primary_key=True)
    knowledge_base_id = Column(String(128), nullable=False, index=True)
    doc_type = Column(String(255), nullable=False)
    data_source_id = Column(String(64), nullable=False)
    entity_name = Column(String(255), nullable=False)
    status = Column(String(16), nullable=False, default="proposed")
    created_by = Column(String(64), nullable=True)

    def to_dict(self) -> dict:
        # 对外不泄漏 tenant_id（同 LineageEdge / OntologySynonym 口径）。
        return {
            "id": self.id,
            "knowledge_base_id": self.knowledge_base_id,
            "doc_type": self.doc_type,
            "data_source_id": self.data_source_id,
            "entity_name": self.entity_name,
            "status": self.status or "proposed",
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


__all__ = [
    "OntologyDataAlignment",
    "VALID_ALIGNMENT_STATUSES",
    "validate_status",
]
