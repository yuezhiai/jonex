#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""② 指标编译影子对比行 — Nl2SqlMetricShadow（W3.3 M4）。

迁移：`deploy/postgres/migrations/034_metric_shadow.sql`（列与本模型一一对应）。
设计：docs/superpowers/specs/2026-11-01-w3-metric-shadow-design.md（工作区文档，不入库）。

⚠️ 纯追加诊断，**不参与业务 CRUD**，也**不存原始数据值**（只记行数与比较口径）。
不继承 `SoftDeleteMixin` / `TimestampMixin`：没有 `is_deleted` / `updated_at`。
"""
import uuid

from sqlalchemy import Column, DateTime, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB

from jonex_core.common.database import Base
from jonex_core.common.entity import TenantMixin


class Nl2SqlMetricShadow(TenantMixin, Base):
    """一条影子对比：主链路结果 + 指标编译侧的执行结果与差异口径。"""

    __tablename__ = "nl2sql_metric_shadow"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    knowledge_base_id = Column(String(64), nullable=False)
    data_source_id = Column(String(64), nullable=False, default="", server_default="")
    question = Column(String(500), nullable=False)
    entity_name = Column(String(255))

    main_outcome = Column(String(16), nullable=False)
    main_reason_code = Column(String(64))
    main_row_count = Column(Integer)
    main_elapsed_ms = Column(Integer)

    status = Column(String(32), nullable=False)
    metric_name = Column(String(255))
    metric_type = Column(String(16))
    match_kind = Column(String(16))
    matched_term = Column(String(255))
    compiled_sql = Column(Text)
    shadow_row_count = Column(Integer)
    shadow_elapsed_ms = Column(Integer)
    agreement = Column(String(32), nullable=False)
    scene = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    created_at = Column(DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP"))


__all__ = ["Nl2SqlMetricShadow"]
