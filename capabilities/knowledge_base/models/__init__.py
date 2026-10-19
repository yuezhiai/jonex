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
from .ontology_synonym import OntologySynonym
from .parser_setting import KnowledgeParserSetting
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
    "AssetStatusEvent",
    "DocStatus",
    "DocumentTag",
    "DomainService",
    "Folder",
    "Tag",
    "KnowledgeAnswerFeedback",
    "KnowledgeAnswerFeedbackEvent",
    "KnowledgeDataSource",
    "KnowledgeDocument",
    "KnowledgeInfo",
    "KnowledgeParserSetting",
    "KnowledgeSearchFeedback",
    "KnowledgeSearchHistory",
    "KnowledgeQueryImage",
    "KnowledgeDocumentVersion",
    "KnowledgeSearchHistoryImage",
    "KbPermission",
    "OntologyStatus",
    "OntologySynonym",
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
    "TaskCenterDocState",
    "TaskCenterDocument",
    "TaskExecutionRegistry",
    "build_query_hash",
    "normalize_query",
]
