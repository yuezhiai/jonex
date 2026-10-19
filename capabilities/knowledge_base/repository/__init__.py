#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Knowledge Base repository exports."""

from .answer_feedback_repository import (
    KnowledgeAnswerFeedbackEventRepository,
    KnowledgeAnswerFeedbackRepository,
)
from .document_repository import KnowledgeDocumentRepository, UNCLASSIFIED_SENTINEL
from .document_version_repository import KnowledgeDocumentVersionRepository
from .data_source_repository import KnowledgeDataSourceRepository
from .document_tag_repository import DocumentTagRepository
from .domain_service_repository import (
    DomainServiceRepository,
    ServiceApiKeyRepository,
    ServiceKnowledgeBaseRepository,
)
from .folder_repository import FolderRepository
from .tag_repository import TagRepository
from .ontology_graph_repository import OntologyGraphRepository
from .ontology_schema_repository import OntologySchemaRepository
from .ontology_synonym_repository import OntologySynonymRepository
from .parser_setting_repository import KnowledgeParserSettingRepository
from .query_image_repository import KnowledgeQueryImageRepository
from .processing_task_repository import (
    COMPILE_STATUS_MAP,
    EXTRACT_STATUS_MAP,
    PARSE_STATUS_MAP,
    TASK_STATUS_FAILED,
    TASK_STATUS_RUNNING,
    TASK_STATUS_SUCCESS,
    TASK_TYPE_COMPILE,
    TASK_TYPE_EXTRACT,
    TASK_TYPE_PARSE,
    build_processing_task_id,
    record_compile_task,
    record_processing_task,
)
from .search_event_repository import (
    SearchEventRepository,
    safe_record_asset_status_event,
)
from .search_feedback_repository import KnowledgeSearchFeedbackRepository
from .search_history_repository import KnowledgeSearchHistoryRepository
from .space_repository import SpacePermissionRepository, SpaceRepository

__all__ = [
    "COMPILE_STATUS_MAP",
    "EXTRACT_STATUS_MAP",
    "PARSE_STATUS_MAP",
    "TASK_STATUS_FAILED",
    "TASK_STATUS_RUNNING",
    "TASK_STATUS_SUCCESS",
    "TASK_TYPE_COMPILE",
    "TASK_TYPE_EXTRACT",
    "TASK_TYPE_PARSE",
    "DocumentTagRepository",
    "DomainServiceRepository",
    "FolderRepository",
    "SearchEventRepository",
    "TagRepository",
    "KnowledgeAnswerFeedbackEventRepository",
    "KnowledgeAnswerFeedbackRepository",
    "KnowledgeDataSourceRepository",
    "KnowledgeDocumentRepository",
    "KnowledgeParserSettingRepository",
    "KnowledgeQueryImageRepository",
    "KnowledgeSearchFeedbackRepository",
    "OntologyGraphRepository",
    "OntologySchemaRepository",
    "OntologySynonymRepository",
    "ServiceApiKeyRepository",
    "ServiceKnowledgeBaseRepository",
    "SpacePermissionRepository",
    "SpaceRepository",
    "UNCLASSIFIED_SENTINEL",
    "build_processing_task_id",
    "record_compile_task",
    "record_processing_task",
    "safe_record_asset_status_event",
]
