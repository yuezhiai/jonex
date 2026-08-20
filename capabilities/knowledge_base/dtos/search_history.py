"""Knowledge Base search history DTOs."""

from datetime import datetime
from typing import Any, Optional

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

    class Config:
        allow_population_by_field_name = True


class SearchHistoryListRequest(BaseModel):
    knowledge_base_id: str = Field(default="", min_length=0, max_length=128)
    domain_space_id: Optional[str] = Field(default=None, max_length=64)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


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
