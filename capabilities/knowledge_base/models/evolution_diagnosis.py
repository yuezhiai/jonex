# -*- coding:utf-8 -*-
"""演化台服务化 ②b 诊断报告模型（迁移 017）。

表：`knowledge_base.evolution_diagnosis_reports` —— 一次诊断 run 一份报告，
纯追加（无 updated_at / is_deleted），取最新走 (tenant_id, created_at DESC) 索引。
"""
import uuid

from sqlalchemy import Column, DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB

from jonex_core.common.database import Base
from jonex_core.common.entity import TenantMixin


class EvolutionDiagnosisReport(TenantMixin, Base):
    __tablename__ = "evolution_diagnosis_reports"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    params = Column(JSONB, nullable=False, default=dict)
    report = Column(JSONB, nullable=False, default=dict)
    created_by = Column(String(64))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


__all__ = ["EvolutionDiagnosisReport"]
