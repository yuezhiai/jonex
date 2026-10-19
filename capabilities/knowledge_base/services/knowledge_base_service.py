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
from .wiki_outline_service import WikiOutlineService
from .reconciliation_service import ReconciliationService
from .search_feedback_service import SearchFeedbackService
from .search_history_service import SearchHistoryService
from .search_service import SearchService
from .image_query_service import ImageQueryService
from .query_image_service import QueryImageService
from .task_runner_service import TaskRunnerService
from .task_watch_service import TaskWatchService
from .execution_registry import PostgresExecutionRegistry


class KnowledgeBaseService:
    def __init__(self):
        self.documents = DocumentService()
        self.folders = FolderService()
        self.tags = TagService()
        self.data_sources = DataSourceService()
        self.knowledge_infos = KnowledgeInfoService()
        self.search = SearchService()
        # [jonex] 以图问知识（两步式的第一步 + 图片生命周期）：
        #   `image_query` = 图片 → 查询文本（识别完只在 Redis 暂存，不落盘）
        #   `query_image` = 落盘 / 查看定位 / 释放清理 / 每小时兜底扫描
        # 两者共用同一份 `kb:imgq:*` 键规则（`image_query_service` 里那三个函数），
        # 所以历史服务必须拿到**同一个** query_image 实例，不能各建一个。
        self.query_image = QueryImageService()
        self.image_query = ImageQueryService()
        self.history = SearchHistoryService(query_image=self.query_image)
        self.feedback = SearchFeedbackService()
        self.answer_feedback = AnswerFeedbackService()
        self.parse_results = ParseResultService()
        self.parser_settings = ParserSettingService()
        self.ontology_query = OntologyQueryService()
        self.ontology = OntologyService()
        self.reconciliation = ReconciliationService()
        self.compiler = OntologyCompiler()
        self.llm_wiki_schemas = LlmWikiSchemaService()
        # [jonex] Wiki 阅读目录（LLM 起草 + LLM 调整，纯展示投影，不参与检索）
        self.wiki_outlines = WikiOutlineService()
        self.synonyms = OntologySynonymService()
        # [jonex] 任务中心接入（只服务 Ontology 库 —— kb_type == "lightrag"）。
        # `task_runner` 是**运行方**：任务中心派活时进它（经 `_build_dispatch` 里那句
        # `dispatch.update(...bind())`）；`task_watch` 是 30 秒看门狗，盯每一步的下游
        # 跑完没有、替它上报终态（挂在既有对账循环尾部，不新起线程）。
        #
        # ⚠️ 构造 `task_runner` 会**当场**取任务运行时（`RunnerBase.__init__` 的「早失败」
        # 设计），所以这里得是进程里第一个碰它的地方 —— 见 `task_runner_service.
        # _ensure_task_runtime()` 为什么是「只补缺、不覆盖」。
        #
        # ⚠️ **`registry` 必须显式换成持久实现**：`RunnerBase.__init__` 默认给的是
        # `InMemoryExecutionRegistry`，漏传**不报错**、只会静默降级 —— 它的失效表现是
        # 「重启 / 多副本时认不出重派 → 重复建下游任务、重复扣配额、产出重复向量」。
        # 真正的兜底是 `capability.initialize()` 里那句 `registry.persistent` 自检。
        self.task_runner = TaskRunnerService(registry=PostgresExecutionRegistry())
        self.task_watch = TaskWatchService()


__all__ = ["KnowledgeBaseService"]
