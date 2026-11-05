#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""② NL→SQL 运行时观测行 — Nl2SqlObservation（运行时证据回写）。

迁移：`deploy/postgres/migrations/009_structured_data.sql`（原 `009_nl2sql_observations.sql` 已并入；列与本模型一一对应）。
设计：docs/superpowers/specs/2026-10-12-nl2sql-runtime-observation-design.md（工作区文档，不入库）。

⚠️ 纯追加观测，**不参与业务 CRUD**：**不继承** `SoftDeleteMixin` / `TimestampMixin`
—— 本表没有 `is_deleted` / `updated_at`（`created_at` 是「观测发生时刻」，由数据库
`DEFAULT CURRENT_TIMESTAMP` 落，不是「行修改时刻」）。将来清理 = 按窗口硬删（009 注释）。
"""
import uuid

from sqlalchemy import Column, DateTime, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB

from jonex_core.common.database import Base
from jonex_core.common.entity import TenantMixin


class Nl2SqlObservation(TenantMixin, Base):
    """一条 ② 运行时观测：失败现场（走 `_fail`）或成功行（带轨迹）。"""

    __tablename__ = "nl2sql_observations"
    __table_args__ = {"schema": "knowledge_base"}

    #: 应用侧生成（同 `KnowledgeDataSource.id` 口径）；DB 侧无默认。
    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    knowledge_base_id = Column(String(64), nullable=False)
    #: 候选失败时无人选源 → 空串（与审计 `resource_id` 同口径，保住诊断分组键）。
    data_source_id = Column(String(64), nullable=False, default="", server_default="")
    question = Column(String(500), nullable=False)
    #: `success` / `failed`（CHECK 约束在 009）。
    outcome = Column(String(16), nullable=False)
    reason_code = Column(String(64))
    reason = Column(String(500))
    entity_name = Column(String(255))
    sql = Column(Text)
    rejected_sql = Column(Text)
    row_count = Column(Integer)
    elapsed_ms = Column(Integer)
    #: 现场（形状见设计 §4.1）：全部键可选、按档出现；无现场时 `{}`。
    scene = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    #: 观测发生时刻。**不是** TimestampMixin 的语义（本表刻意不继承它）——
    #: 只在 DB 侧落默认值，应用侧从不赋值。
    created_at = Column(DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP"))


__all__ = ["Nl2SqlObservation"]
