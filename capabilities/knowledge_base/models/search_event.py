#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Knowledge Base 数据运营事件实体（完整事件体系，append-only 事实表）。

这些表是检索/处理链路的埋点事实表，不承载业务 CRUD，只由链路落库、由
AnalyticsService 聚合查询。id 沿用知识库服务统一约定：VARCHAR(64) + Python
端 uuid4（非数据库 gen_random_uuid），与 knowledge_search_history 等一致。
"""

from uuid import uuid4

from sqlalchemy import Column, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from jonex_core.common.database import Base
from jonex_core.common.entity import SoftDeleteMixin, TenantMixin, TimestampMixin


class SearchRequestEvent(Base, TenantMixin, TimestampMixin, SoftDeleteMixin):
    """检索请求接收事件：每次检索调用的入口记录，含失败/中断的请求。

    request_id 为每次检索调用的 trace id（capability 层 r.request_id），
    entry_service_id 为唯一入口服务（一次调用只记一个入口，不按内部多服务重复归属）。
    """

    __tablename__ = "search_request_event"
    __table_args__ = (
        Index("idx_sre_tenant_time", "tenant_id", "received_at"),
        Index("idx_sre_request", "request_id"),
        {"schema": "knowledge_base"},
    )

    id = Column(String(64), primary_key=True, default=lambda: str(uuid4()))
    user_id = Column(String(128), nullable=True)
    request_id = Column(String(64), nullable=False)
    entry_service_id = Column(String(64), nullable=True)
    domain_space_id = Column(String(64), nullable=True)
    query = Column(Text, nullable=True)
    query_hash = Column(String(64), nullable=True)
    # 本次检索实际覆盖的 kb 列表（capability 权限过滤后的 data 值）。
    # 领域服务形态（payload 只有 service_id）入口处拿不到 kb → 空数组，
    # 聚合期按 entry_service_id 反查 service_knowledge_bases 补齐。
    knowledge_base_ids = Column(JSONB, nullable=False, default=list)
    received_at = Column(DateTime(timezone=True), nullable=False, default=func.now())


class SearchStepEvent(Base, TenantMixin, TimestampMixin, SoftDeleteMixin):
    """检索步骤事件：一次检索内各推理/检索步骤的逐条记录。

    stage 取值对齐 dtos/reasoning.py 的 STAGE_*；status 取值 done/skipped/failed
    （进行中的中间态不落库，只记终态）。
    """

    __tablename__ = "search_step_event"
    __table_args__ = (
        Index("idx_sse_tenant_time", "tenant_id", "searched_at"),
        Index("idx_sse_request", "request_id"),
        {"schema": "knowledge_base"},
    )

    id = Column(String(64), primary_key=True, default=lambda: str(uuid4()))
    request_id = Column(String(64), nullable=False)
    stage = Column(String(32), nullable=False)
    title = Column(String(128), nullable=True)
    status = Column(String(16), nullable=False)
    summary = Column(Text, nullable=True)
    detail = Column(JSONB, nullable=True)
    duration_ms = Column(Integer, nullable=True)
    knowledge_base_ids = Column(JSONB, nullable=False, default=list)
    service_id = Column(String(64), nullable=True)
    searched_at = Column(DateTime(timezone=True), nullable=False, default=func.now())


class SearchHitEvent(Base, TenantMixin, TimestampMixin, SoftDeleteMixin):
    """检索命中事件：检索链路实际命中的来源记录（request_id + target_id 去重）。

    target_type = ontology_instance（本体实例）/ document（RAG 召回文档）。
    target_id 对本体实例取 sha256(组合键)[:32] 稳定 id，对文档取 doc_id。
    """

    __tablename__ = "search_hit_event"
    __table_args__ = (
        Index("idx_she_tenant_time", "tenant_id", "hit_at"),
        Index("idx_she_request", "request_id"),
        Index("idx_she_target", "target_type", "target_id"),
        {"schema": "knowledge_base"},
    )

    id = Column(String(64), primary_key=True, default=lambda: str(uuid4()))
    request_id = Column(String(64), nullable=False)
    # 32 而非 16：本体命中事件写 'ontology_instance'（17 字符）。
    # 声明 16 会让 INSERT 报 value too long，把同一事务里的步骤/命中/采用三类事件整批回滚
    # （「被引用数据资产数」「高频引用资产 Top10」随之不更新）。见 tests/unit/test_search_event_schema_contract.py
    target_type = Column(String(32), nullable=False)
    target_id = Column(String(128), nullable=False)
    entity_type = Column(String(64), nullable=True)
    canonical_name = Column(String(256), nullable=True)
    kb_id = Column(String(128), nullable=True)
    hit_at = Column(DateTime(timezone=True), nullable=False, default=func.now())


class SearchAdoptionEvent(Base, TenantMixin, TimestampMixin, SoftDeleteMixin):
    """回答采用事件：最终答案实际引用的来源记录。

    answer_id = knowledge_search_history.id；answer_id + target_id 去重。
    target_type/target_id 语义同 SearchHitEvent（最终答案实际采用的来源）。
    """

    __tablename__ = "search_adoption_event"
    __table_args__ = (
        Index("idx_sae_tenant_time", "tenant_id", "adopted_at"),
        Index("idx_sae_answer", "answer_id"),
        Index("idx_sae_target", "target_type", "target_id"),
        {"schema": "knowledge_base"},
    )

    id = Column(String(64), primary_key=True, default=lambda: str(uuid4()))
    answer_id = Column(String(64), nullable=False)
    request_id = Column(String(64), nullable=True)
    # 同 SearchHitEvent.target_type：32 而非 16，见该字段注释
    target_type = Column(String(32), nullable=False)
    target_id = Column(String(128), nullable=False)
    entity_type = Column(String(64), nullable=True)
    canonical_name = Column(String(256), nullable=True)
    kb_id = Column(String(128), nullable=True)
    adopted_at = Column(DateTime(timezone=True), nullable=False, default=func.now())


class AssetStatusEvent(Base, TenantMixin, TimestampMixin, SoftDeleteMixin):
    """资产状态事件：文档资产状态流转的变更记录。

    记录 knowledge_documents.status 每次流转（status + previous_status + reason），
    status 取值对齐 DocStatus：pending/parsing/ingesting/ready/failed/deleting/deleted。
    """

    __tablename__ = "asset_status_event"
    __table_args__ = (
        Index("idx_ase_tenant_time", "tenant_id", "changed_at"),
        Index("idx_ase_asset", "asset_id"),
        {"schema": "knowledge_base"},
    )

    id = Column(String(64), primary_key=True, default=lambda: str(uuid4()))
    asset_id = Column(String(64), nullable=False)
    kb_id = Column(String(128), nullable=True)
    status = Column(String(32), nullable=False)
    previous_status = Column(String(32), nullable=True)
    reason = Column(Text, nullable=True)
    changed_at = Column(DateTime(timezone=True), nullable=False, default=func.now())


class ProcessingTaskEvent(Base, TenantMixin, TimestampMixin, SoftDeleteMixin):
    """处理任务事件：解析/本体抽取/编译等处理任务的执行记录。

    processing_task_id 唯一键 upsert：同一任务状态更新覆盖 status/finished_at。
    每个任务写两次：进入处理中落 running + started_at，终态再 upsert
    success/failed + finished_at/error（只写终态则 duration_ms 永远算不出来）。

    task_type 取值 parse / extract / compile，与前端 `DoStage`
    （`pages/DataOperations/types.ts`）及 i18n `dataOperations.stage.*` 一一对应，
    也对应原型上的「解析 / 抽取 / 编译」三阶段。
    ⚠️ 本 docstring 原先写的是 rag_ingest / ontology_extract / llm_wiki_compile，
    全仓无代码依赖该写法，二期订正为上述三个值（少一层映射）。

    processing_task_id 是**确定性组合键** `{doc_id}:{task_type}:{content_generation}`，
    不是 RAG/编译任务 id —— 后者可空、reparse 会换、且解析与抽取可能共用同一个
    rag_task_id，拿它做幂等键会互相覆盖；content_generation 是 reparse 代次，
    天然区分「同一资产的第 n 轮处理」。
    """

    __tablename__ = "processing_task_event"
    __table_args__ = (
        Index("idx_pte_tenant_time", "tenant_id", "started_at"),
        Index("idx_pte_asset", "asset_id"),
        UniqueConstraint("tenant_id", "processing_task_id", name="uq_pte_task"),
        {"schema": "knowledge_base"},
    )

    id = Column(String(64), primary_key=True, default=lambda: str(uuid4()))
    processing_task_id = Column(String(128), nullable=False)
    asset_id = Column(String(64), nullable=False)
    kb_id = Column(String(128), nullable=True)
    task_type = Column(String(32), nullable=False)
    status = Column(String(32), nullable=False)
    error = Column(Text, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)


__all__ = [
    "AssetStatusEvent",
    "ProcessingTaskEvent",
    "SearchAdoptionEvent",
    "SearchHitEvent",
    "SearchRequestEvent",
    "SearchStepEvent",
]
