"""W2.5 数据质量闭环：规则 / 巡检 / 结果 / 问题工单 / 工单事件。

迁移见 ``deploy/postgres/migrations/019_data_governance_assets.sql``（2026-11-04 起
原 ``022_data_quality.sql`` 已并入；``update/`` 下为逐字节同名镜像）。
时间列必须显式 ``DateTime(timezone=True)``：迁移落的是 ``TIMESTAMPTZ``，沿用
``TimestampMixin`` 的 naive 列会让 asyncpg 按 ``TIMESTAMP WITHOUT TIME ZONE``
发参数（见 ``models_contract.py`` / ``models_standard.py`` 的说明）。

``quality_results`` 与 ``quality_issue_events`` 的迁移里没有 ``created_at`` /
``updated_at`` 两列。模型仍按任务约定继承 ``TenantMixin, TimestampMixin``，
但把不存在的 mixin 列显式置为 ``None`` 排除，避免 ORM 生成数据库里没有的列。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import JSONB

from jonex_core.common.database import Base
from jonex_core.common.entity import TenantMixin, TimestampMixin


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value) -> str | None:
    return value.isoformat() if value else None


class QualityRule(TenantMixin, TimestampMixin, Base):
    """质量规则（绑定 W2.1 资产 ID）。"""

    __tablename__ = "quality_rules"
    __table_args__ = {"schema": "data_governance"}

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    name = Column(String(255), nullable=False)
    asset_type = Column(String(16), nullable=False)
    asset_id = Column(String(255), nullable=False)
    data_source_id = Column(String(64), nullable=False)
    kb_id = Column(String(128), nullable=True)
    dimension = Column(String(16), nullable=False)
    severity = Column(String(16), nullable=False, default="warning")
    check_type = Column(String(16), nullable=False, default="library")
    # none_as_null=True：None 必须绑定为 SQL NULL（触发 NOT NULL 约束），
    # 而不是被写成 JSON 'null'。
    definition = Column(JSONB(none_as_null=True), nullable=False, default=dict)
    threshold = Column(JSONB(none_as_null=True), nullable=False, default=dict)
    enabled = Column(SmallInteger, nullable=False, default=0)
    contract_id = Column(String(64), nullable=True)
    created_by = Column(String(64), nullable=True)
    updated_by = Column(String(64), nullable=True)
    revision = Column(Integer, nullable=False, default=1)
    is_deleted = Column(SmallInteger, nullable=False, default=0)
    # 覆盖 mixin 的 naive 列（迁移是 TIMESTAMPTZ）。
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now,
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "asset_type": self.asset_type,
            "asset_id": self.asset_id,
            "data_source_id": self.data_source_id,
            "kb_id": self.kb_id,
            "dimension": self.dimension,
            "severity": self.severity,
            "check_type": self.check_type,
            "definition": self.definition or {},
            "threshold": self.threshold or {},
            "enabled": self.enabled,
            "contract_id": self.contract_id,
            "created_by": self.created_by,
            "updated_by": self.updated_by,
            "revision": self.revision,
            "is_deleted": self.is_deleted,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


class QualityRun(TenantMixin, TimestampMixin, Base):
    """一次巡检运行（规则快照由外部执行）。"""

    __tablename__ = "quality_runs"
    __table_args__ = {"schema": "data_governance"}

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    data_source_id = Column(String(64), nullable=False)
    trigger = Column(String(16), nullable=False, default="manual")
    status = Column(String(16), nullable=False, default="running")
    rule_ids = Column(JSONB(none_as_null=True), nullable=False, default=list)
    # 创建运行时的规则快照：结果回传只按它落资产与开单，不再读当前规则。
    rule_snapshot = Column(JSONB(none_as_null=True), nullable=False, default=list)
    executor = Column(String(128), nullable=True)
    dialect = Column(String(16), nullable=False, default="tsql")
    total = Column(Integer, nullable=False, default=0)
    passed = Column(Integer, nullable=False, default=0)
    failed = Column(Integer, nullable=False, default=0)
    error = Column(Integer, nullable=False, default=0)
    message = Column(Text, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    created_by = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now,
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "data_source_id": self.data_source_id,
            "trigger": self.trigger,
            "status": self.status,
            "rule_ids": self.rule_ids or [],
            "rule_snapshot": self.rule_snapshot or [],
            "executor": self.executor,
            "dialect": self.dialect,
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "error": self.error,
            "message": self.message,
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "created_by": self.created_by,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


class QualityResult(TenantMixin, TimestampMixin, Base):
    """单条规则在某次运行中的结果。"""

    __tablename__ = "quality_results"
    __table_args__ = {"schema": "data_governance"}

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    run_id = Column(String(64), nullable=False)
    rule_id = Column(String(64), nullable=False)
    asset_type = Column(String(16), nullable=False)
    asset_id = Column(String(255), nullable=False)
    status = Column(String(16), nullable=False)
    metric_value = Column(JSONB(none_as_null=True), nullable=True)
    message = Column(Text, nullable=True)
    sample = Column(JSONB(none_as_null=True), nullable=True)
    checked_at = Column(DateTime(timezone=True), nullable=False, default=_now)

    # quality_results 只有 checked_at；排除 mixin 的 created_at / updated_at，
    # 否则 ORM 会 SELECT 不存在的列。
    created_at = None
    updated_at = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "run_id": self.run_id,
            "rule_id": self.rule_id,
            "asset_type": self.asset_type,
            "asset_id": self.asset_id,
            "status": self.status,
            "metric_value": self.metric_value,
            "message": self.message,
            "sample": self.sample,
            "checked_at": _iso(self.checked_at),
        }


class QualityIssue(TenantMixin, TimestampMixin, Base):
    """质量问题工单（同一规则+资产至多一条未关闭，见部分唯一索引）。"""

    __tablename__ = "quality_issues"
    __table_args__ = {"schema": "data_governance"}

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    rule_id = Column(String(64), nullable=False)
    asset_type = Column(String(16), nullable=False)
    asset_id = Column(String(255), nullable=False)
    data_source_id = Column(String(64), nullable=False)
    severity = Column(String(16), nullable=False, default="warning")
    status = Column(String(16), nullable=False, default="open")
    assignee_id = Column(String(64), nullable=True)
    title = Column(String(255), nullable=False)
    detail = Column(Text, nullable=True)
    first_seen_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    last_seen_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    revision = Column(Integer, nullable=False, default=1)
    created_by = Column(String(64), nullable=True)
    updated_by = Column(String(64), nullable=True)
    is_deleted = Column(SmallInteger, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now,
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "rule_id": self.rule_id,
            "asset_type": self.asset_type,
            "asset_id": self.asset_id,
            "data_source_id": self.data_source_id,
            "severity": self.severity,
            "status": self.status,
            "assignee_id": self.assignee_id,
            "title": self.title,
            "detail": self.detail,
            "first_seen_at": _iso(self.first_seen_at),
            "last_seen_at": _iso(self.last_seen_at),
            "resolved_at": _iso(self.resolved_at),
            "revision": self.revision,
            "created_by": self.created_by,
            "updated_by": self.updated_by,
            "is_deleted": self.is_deleted,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


class QualityIssueEvent(TenantMixin, TimestampMixin, Base):
    """工单事件（assign / 流转 / 评论 / 复验）。"""

    __tablename__ = "quality_issue_events"
    __table_args__ = {"schema": "data_governance"}

    id = Column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    issue_id = Column(String(64), nullable=False)
    event_type = Column(String(24), nullable=False)
    from_status = Column(String(16), nullable=True)
    to_status = Column(String(16), nullable=True)
    actor_id = Column(String(64), nullable=True)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)

    # quality_issue_events 只有 created_at；排除 mixin 的 updated_at。
    updated_at = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "issue_id": self.issue_id,
            "event_type": self.event_type,
            "from_status": self.from_status,
            "to_status": self.to_status,
            "actor_id": self.actor_id,
            "note": self.note,
            "created_at": _iso(self.created_at),
        }
