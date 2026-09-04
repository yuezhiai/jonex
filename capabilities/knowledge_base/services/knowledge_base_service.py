"""Knowledge Base service facade."""

from .answer_feedback_service import AnswerFeedbackService
from .document_service import DocumentService
from .data_source_service import DataSourceService
from .folder_service import FolderService
from .tag_service import TagService
from .knowledge_info_service import KnowledgeInfoService
from .ontology_compiler import OntologyCompiler
from .ontology_query_service import OntologyQueryService
from .ontology_service import OntologyService
from .ontology_synonym_service import OntologySynonymService
from .parse_result_service import ParseResultService
from .parser_setting_service import ParserSettingService
from .llm_wiki_schema_service import LlmWikiSchemaService
from .reconciliation_service import ReconciliationService
from .search_feedback_service import SearchFeedbackService
from .search_history_service import SearchHistoryService
from .search_service import SearchService


class KnowledgeBaseService:
    def __init__(self):
        self.documents = DocumentService()
        self.folders = FolderService()
        self.tags = TagService()
        self.data_sources = DataSourceService()
        self.knowledge_infos = KnowledgeInfoService()
        self.search = SearchService()
        self.history = SearchHistoryService()
        self.feedback = SearchFeedbackService()
        self.answer_feedback = AnswerFeedbackService()
        self.parse_results = ParseResultService()
        self.parser_settings = ParserSettingService()
        self.ontology_query = OntologyQueryService()
        self.ontology = OntologyService()
        self.reconciliation = ReconciliationService()
        self.compiler = OntologyCompiler()
        self.llm_wiki_schemas = LlmWikiSchemaService()
        self.synonyms = OntologySynonymService()


__all__ = ["KnowledgeBaseService"]
