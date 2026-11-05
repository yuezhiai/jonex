#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Knowledge Base ORM exports.

加载该包时，下列模型会在各自模块的类定义处注册到 SQLAlchemy 元数据。
任一模型导入或注册失败时，模块级 import 抛出的异常会直接向上传播，
终止整个 models 包的加载并指示失败的模型，从而不会注册部分模型。
"""

from .document import (
    DocStatus,
    KnowledgeDocument,
    KnowledgeDocumentVersion,
    OntologyStatus,
)
from .llm_wiki_schema import (
    LlmWikiSchema,
    STATUS_ACTIVE,
    STATUS_ARCHIVED,
    SYNC_APPLY_FAILED,
    SYNC_SYNCED,
)
from .data_source import KnowledgeDataSource
from .data_source_connection import DataSourceConnection
from .folder import Folder
from .tag import DocumentTag, Tag
from .domain_service import (
    DomainService,
    ServiceApiKey,
    ServiceConfig,
    ServiceKnowledgeBase,
)
from .knowledge_info import KnowledgeInfo
from .kb_permission import KbPermission
from .ontology_abstract_schema import AbstractSchemaSaveHistory
from .ontology_data_alignment import OntologyDataAlignment
from .ontology_synonym import OntologySynonym
from .metric_call_approval import MetricCallApproval
from .nl2sql_metric_shadow import Nl2SqlMetricShadow
from .nl2sql_observation import Nl2SqlObservation
from .lineage_edge import LineageEdge
from .ontology_tool_binding import OntologyToolBinding
from .parser_setting import KnowledgeParserSetting
from .model_setting import KnowledgeModelSetting
from .answer_feedback import KnowledgeAnswerFeedback, KnowledgeAnswerFeedbackEvent
from .search_feedback import KnowledgeSearchFeedback
from .search_history import KnowledgeSearchHistory, build_query_hash, normalize_query
from .query_image import KnowledgeQueryImage, KnowledgeSearchHistoryImage
from .search_event import (
    AssetStatusEvent,
    ProcessingTaskEvent,
    SearchAdoptionEvent,
    SearchHitEvent,
    SearchRequestEvent,
    SearchStepEvent,
)
from .similarity import (
    STATUS_DISMISSED,
    STATUS_INVALID,
    STATUS_MERGED,
    STATUS_PENDING,
    STATUS_SUPERSEDED,
    KnowledgeDocumentSignature,
    KnowledgeDocumentSimilaritySuggestion,
)
from .space import Space, SpacePermission
# [jonex] 任务中心接入表（接管行 + 执行登记）。按本模块 docstring 的契约，模型只有
# 在这里被 import 才算注册进元数据 —— 别只靠「某个 repository 恰好 import 了它」。
from .task_center import (
    ACTIVE_STATES,
    MANAGED_STATES,
    TaskCenterDocState,
    TaskCenterDocument,
    TaskExecutionRegistry,
)

__all__ = [
    "ACTIVE_STATES",
    "MANAGED_STATES",
    "AbstractSchemaSaveHistory",
    "AssetStatusEvent",
    "DataSourceConnection",
    "DocStatus",
    "DocumentTag",
    "DomainService",
    "Folder",
    "Tag",
    "KnowledgeAnswerFeedback",
    "KnowledgeAnswerFeedbackEvent",
    "KnowledgeDataSource",
    "KnowledgeDocument",
    "KnowledgeDocumentSignature",
    "KnowledgeDocumentSimilaritySuggestion",
    "KnowledgeInfo",
    "KnowledgeParserSetting",
    "KnowledgeModelSetting",
    "KnowledgeSearchFeedback",
    "KnowledgeSearchHistory",
    "KnowledgeQueryImage",
    "KnowledgeDocumentVersion",
    "KnowledgeSearchHistoryImage",
    "KbPermission",
    "LineageEdge",
    "MetricCallApproval",
    "Nl2SqlMetricShadow",
    "Nl2SqlObservation",
    "OntologyDataAlignment",
    "OntologyStatus",
    "OntologySynonym",
    "OntologyToolBinding",
    "ProcessingTaskEvent",
    "SearchAdoptionEvent",
    "SearchHitEvent",
    "SearchRequestEvent",
    "SearchStepEvent",
    "ServiceApiKey",
    "ServiceConfig",
    "ServiceKnowledgeBase",
    "Space",
    "SpacePermission",
    "STATUS_DISMISSED",
    "STATUS_INVALID",
    "STATUS_MERGED",
    "STATUS_PENDING",
    "STATUS_SUPERSEDED",
    "TaskCenterDocState",
    "TaskCenterDocument",
    "TaskExecutionRegistry",
    "build_query_hash",
    "normalize_query",
]
