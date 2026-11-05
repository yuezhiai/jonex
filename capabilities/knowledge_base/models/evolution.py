# -*- coding:utf-8 -*-
"""演化台服务化模型（迁移 016 / 024）。

表：`knowledge_base.evolution_runs` / `evolution_run_logs` /
`evolution_ledgers` / `evolution_corpus_cases`（016）；
`semantic_versions` / `semantic_versions_archive` / `evolution_candidates`（024）；
`evolution_corpus_versions`（030）。

⚠️ `EvolutionRunLog` 是按 `run_id` 级联的从表，**自身不带 `tenant_id`**
（租户隔离由父表 `evolution_runs` 承担），因此不继承 `TenantMixin`。
`EvolutionCandidate` 同理。
"""
from sqlalchemy import (CHAR, Column, DateTime, ForeignKey, Integer,
                        SmallInteger, String, Text, func)
from sqlalchemy.dialects.postgresql import JSONB

from jonex_core.common.database import Base
from jonex_core.common.entity import TenantMixin


class EvolutionRun(TenantMixin, Base):
    __tablename__ = "evolution_runs"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(String(64), primary_key=True)
    knowledge_base_id = Column(String(128), nullable=False)
    data_source_id = Column(String(64))
    kind = Column(String(16), nullable=False)
    status = Column(String(16), nullable=False, default="queued")
    stage = Column(String(32), nullable=False, default="created")
    request = Column(JSONB, nullable=False, default=dict)
    parent = Column(JSONB, nullable=False, default=dict)
    report = Column(JSONB, nullable=False, default=dict)
    gate = Column(JSONB, nullable=False, default=dict)
    patch = Column(JSONB, nullable=False, default=dict)
    backup = Column(JSONB, nullable=False, default=dict)
    error = Column(Text)
    request_id = Column(String(64))
    created_by = Column(String(64))
    # ---- 信任闭环（迁移 024）：版本链 / 门禁 / 采纳 / smoke / 回滚 ----
    parent_version_id = Column(String(64))
    gate_decision = Column(String(16))
    gate_adhoc = Column(JSONB)
    gate_effective = Column(String(16))
    adopted_version_id = Column(String(64))
    adopted_by = Column(String(64))
    adopted_at = Column(DateTime(timezone=True))
    adopt_request_id = Column(String(64))
    gate_override = Column(SmallInteger, nullable=False, default=0)
    override_by = Column(String(64))
    override_reason = Column(Text)
    smoke_status = Column(String(16))
    smoke_mode = Column(String(16))
    smoke = Column(JSONB)
    rolled_back_at = Column(DateTime(timezone=True))
    rolled_back_by = Column(String(64))
    rollback_reason = Column(Text)
    # ---- 回滚崩溃窗口（迁移 025）：restore 前落 pending 标记，收口/补偿清空 ----
    # `rollback_from_version_id` = 回滚开始时的 active（链推进守卫）。
    rollback_target_version_id = Column(String(64))
    rollback_from_version_id = Column(String(64))
    rollback_started_at = Column(DateTime(timezone=True))
    legacy_adopt = Column(SmallInteger, nullable=False, default=0)
    # ---- 完整语料基准（迁移 026）：异步跑分母题，不阻塞采纳/回滚 ----
    bench = Column(JSONB)
    bench_status = Column(String(16))
    bench_mode = Column(String(16))
    bench_started_at = Column(DateTime(timezone=True))
    bench_ended_at = Column(DateTime(timezone=True))
    # ---- 语料来源（迁移 031）：本次实际用的语料版本与内容 hash ----
    corpus_version_id = Column(String(64))
    corpus_hash = Column(String(64))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class EvolutionRunLog(Base):
    # 日志表按 run_id 级联，自身不带 tenant_id（父表已隔离）
    __tablename__ = "evolution_run_logs"
    __table_args__ = {"schema": "knowledge_base"}

    run_id = Column(String(64), ForeignKey("knowledge_base.evolution_runs.id", ondelete="CASCADE"),
                    primary_key=True)
    seq = Column(Integer, primary_key=True)
    line = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class EvolutionLedger(TenantMixin, Base):
    __tablename__ = "evolution_ledgers"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(String(64), primary_key=True)
    ledger_type = Column(String(16), nullable=False)
    entry = Column(JSONB, nullable=False, default=dict)
    run_id = Column(String(64))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SemanticVersion(TenantMixin, Base):
    """不可变语义版本快照（迁移 024）；只允许改 `status`。"""

    __tablename__ = "semantic_versions"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(String(64), primary_key=True)
    knowledge_base_id = Column(String(128), nullable=False)
    scope = Column(String(16), nullable=False)  # abstract / compiled
    data_source_id = Column(String(64))
    parent_version_id = Column(String(64))
    source_run_id = Column(String(64))
    source_version_id = Column(String(64))
    snapshot = Column(JSONB, nullable=False)
    snapshot_hash = Column(CHAR(64), nullable=False)
    status = Column(String(16), nullable=False)  # candidate/active/superseded/rejected
    created_by = Column(String(64))
    legacy = Column(SmallInteger, nullable=False, default=0)
    note = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    activated_at = Column(DateTime(timezone=True))


class SemanticVersionArchive(TenantMixin, Base):
    """版本归档：与 `semantic_versions` 同构 + `archived_at`（无 active 唯一索引）。"""

    __tablename__ = "semantic_versions_archive"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(String(64), primary_key=True)
    knowledge_base_id = Column(String(128), nullable=False)
    scope = Column(String(16), nullable=False)
    data_source_id = Column(String(64))
    parent_version_id = Column(String(64))
    source_run_id = Column(String(64))
    source_version_id = Column(String(64))
    snapshot = Column(JSONB, nullable=False)
    snapshot_hash = Column(CHAR(64), nullable=False)
    status = Column(String(16), nullable=False)
    created_by = Column(String(64))
    legacy = Column(SmallInteger, nullable=False, default=0)
    note = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    activated_at = Column(DateTime(timezone=True))
    archived_at = Column(DateTime(timezone=True), nullable=False)


class EvolutionCandidate(Base):
    """演化候选（迁移 024）：按 `run_id` 级联，自身不带 `tenant_id`。"""

    __tablename__ = "evolution_candidates"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(String(64), primary_key=True)
    run_id = Column(String(64), ForeignKey("knowledge_base.evolution_runs.id", ondelete="CASCADE"),
                    nullable=False)
    candidate_no = Column(Integer, nullable=False)
    parent_version_id = Column(String(64))
    layer = Column(String(16), nullable=False)  # content / tool / schema
    edit_type = Column(String(32), nullable=False)
    target = Column(JSONB, nullable=False, default=list)
    before = Column(JSONB, nullable=False, default=dict)
    after = Column(JSONB, nullable=False, default=dict)
    expected_changes = Column(JSONB, nullable=False, default=list)
    rationale = Column(Text)
    evidence = Column(JSONB, nullable=False, default=dict)
    gate_decision = Column(String(16))
    gate_reasons = Column(JSONB)
    status = Column(String(16), nullable=False, default="draft")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class EvolutionCorpusCase(TenantMixin, Base):
    __tablename__ = "evolution_corpus_cases"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(String(64), primary_key=True)
    name = Column(String(128), nullable=False)
    question = Column(Text, nullable=False)
    tier = Column(String(32), nullable=False)
    expect = Column(String(16), nullable=False)
    control = Column(JSONB)
    kb_id = Column(String(128), nullable=False)
    ds_id = Column(String(64), nullable=False)
    enabled = Column(SmallInteger, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class EvolutionCorpusVersion(TenantMixin, Base):
    """语料版本快照（迁移 030）：append-only，每 (tenant,kb,ds) 链一条 active。"""

    __tablename__ = "evolution_corpus_versions"
    __table_args__ = {"schema": "knowledge_base"}

    id = Column(String(64), primary_key=True)
    knowledge_base_id = Column(String(128), nullable=False)
    data_source_id = Column(String(64), nullable=False)
    snapshot = Column(JSONB, nullable=False)
    snapshot_hash = Column(CHAR(64), nullable=False)
    status = Column(String(16), nullable=False, default="active")  # active/superseded
    source = Column(String(32), nullable=False, default="manual")
    note = Column(Text)
    created_by = Column(String(64))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    activated_at = Column(DateTime(timezone=True))


__all__ = [
    "EvolutionCandidate",
    "EvolutionCorpusCase",
    "EvolutionCorpusVersion",
    "EvolutionLedger",
    "EvolutionRun",
    "EvolutionRunLog",
    "SemanticVersion",
    "SemanticVersionArchive",
]
