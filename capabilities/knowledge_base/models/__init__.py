#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Knowledge Base ORM exports.

加载该包时，下列模型会在各自模块的类定义处注册到 SQLAlchemy 元数据。
任一模型导入或注册失败时，模块级 import 抛出的异常会直接向上传播，
终止整个 models 包的加载并指示失败的模型，从而不会注册部分模型。
"""

from .document import DocStatus, KnowledgeDocument, OntologyStatus
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
from .space import Space, SpacePermission

__all__ = [
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
    "KbPermission",
    "OntologyStatus",
    "OntologySynonym",
    "ServiceApiKey",
    "ServiceConfig",
    "ServiceKnowledgeBase",
    "Space",
    "SpacePermission",
    "build_query_hash",
    "normalize_query",
]
