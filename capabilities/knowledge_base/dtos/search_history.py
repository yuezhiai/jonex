"""Knowledge Base search history DTOs."""

from datetime import datetime
from typing import Any, List, Optional

try:
    from pydantic.v1 import BaseModel, Field
except ImportError:
    from pydantic import BaseModel, Field


class SearchHistoryCreateRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    knowledge_base_id: str = Field(default="", min_length=0, max_length=128)
    domain_space_id: Optional[str] = Field(default=None, max_length=64, alias="domainSpaceId")
    mode: str = Field(default="hybrid", min_length=1, max_length=32)
    top_k: int = Field(default=5, ge=1, le=50, alias="topK")
    status: str = Field(default="done", max_length=32)
    domain: Optional[str] = Field(default=None, max_length=128)
    domain_id: Optional[str] = Field(default=None, max_length=128, alias="domainId")
    answer_preview: Optional[str] = Field(default=None, alias="answerPreview")
    reference_count: int = Field(default=0, ge=0, alias="referenceCount")
    result_count: int = Field(default=0, ge=0, alias="resultCount")
    duration_ms: Optional[int] = Field(default=None, ge=0, alias="durationMs")
    # [jonex] 检索历史快照：完整答案 + 引用快照 + 推理链快照（点击历史直接展示，不重新检索）
    answer: Optional[str] = None
    references: Optional[list] = Field(default_factory=list)
    reasoning: Optional[dict] = None
    # [jonex] 检索条件快照：严格模式配置（再次搜索时复用当时条件）
    strict_config: Optional[dict] = Field(default=None, alias="strictConfig")
    metadata: dict[str, Any] = Field(default_factory=dict)
    # [jonex] 以图问知识：本次检索附带的查询图片 id，**顺序即展示顺序**（D6 / B9）。
    # 上限 8 只是报文层面的粗挡：真正的上限是 IMAGE_QUERY_MAX_IMAGES（默认 4），
    # 由 `QueryImageService.bind_for_history` 去重后截断。
    # ⚠️ 未知字段会被静默忽略（本 DTO 没设 extra="forbid"），但**服务层不显式处理
    # 一样会被丢掉** —— `save_history` 是手动 pop 已知字段后组 data 的（F17）。
    query_image_ids: List[str] = Field(default_factory=list, max_items=8, alias="queryImageIds")
    # 用户和图片一起输入的原话（可为空）。存进 `extra_metadata.image_question`，
    # **不加列**；前端从 `metadata.image_question` 读。
    # 它的用途是历史条目上显示「当时问的是什么」—— 历史行的 `query` 字段存的是
    # **实际用于检索的文本**（识别产出），不是用户原话。
    image_question: Optional[str] = Field(default=None, max_length=500, alias="imageQuestion")

    class Config:
        allow_population_by_field_name = True


class SearchHistoryListRequest(BaseModel):
    knowledge_base_id: str = Field(default="", min_length=0, max_length=128)
    domain_space_id: Optional[str] = Field(default=None, max_length=64)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
    # [jonex] 传了 history_id 就只查这一条并带上「快照」（answer / references /
    # reasoning），忽略分页；不传则是列表，列表**不带**快照 —— 实测 18 条历史
    # 222 KB 里 83% 是 `references[].locations[].text`（整段 chunk 正文），
    # 而侧栏一个字都不渲染，白传。
    history_id: Optional[str] = Field(default=None, max_length=64)


class SearchHistoryDeleteRequest(BaseModel):
    knowledge_base_id: str = Field(default="", min_length=0, max_length=128)
    domain_space_id: Optional[str] = Field(default=None, max_length=64)


class SearchOverviewRequest(BaseModel):
    knowledge_base_id: str = Field(default="", min_length=0, max_length=128)
    domain_space_id: Optional[str] = Field(default=None, max_length=64)


class SearchHistoryResponse(BaseModel):
    id: str
    tenant_id: str
    user_id: str
    domain_space_id: Optional[str] = None
    query: str
    query_hash: str
    knowledge_base_id: str
    mode: str
    top_k: int
    status: str
    answer_preview: Optional[str] = None
    reference_count: int = 0
    result_count: int = 0
    duration_ms: Optional[int] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    answer: Optional[str] = None
    references: list = Field(default_factory=list)
    reasoning: Optional[dict] = None
    searched_at: Optional[datetime | str] = None
    created_at: Optional[datetime | str] = None
    updated_at: Optional[datetime | str] = None


class SearchHistoryListResponse(BaseModel):
    items: list[SearchHistoryResponse] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20


class SearchOverviewResponse(BaseModel):
    total_history: int = 0
    recent_items: list[SearchHistoryResponse] = Field(default_factory=list)
