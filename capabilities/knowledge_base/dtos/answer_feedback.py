"""Knowledge Base answer feedback DTOs."""

from typing import Optional

try:
    from pydantic.v1 import BaseModel, Field
except ImportError:
    from pydantic import BaseModel, Field


class SubmitAnswerFeedbackRequest(BaseModel):
    """提交/修改回答反馈（客户端只传反馈本体 + 锚点，上下文服务端反查）。"""

    history_id: str = Field(..., min_length=1, max_length=64, description="问答记录 ID")
    operation_id: str = Field(..., min_length=1, max_length=64, description="客户端操作幂等键（重试复用）")
    version: int = Field(..., ge=1, description="客户端操作序号（单调递增）")
    feedback_type: str = Field(..., regex="^(like|dislike)$", description="反馈类型")
    feedback_reason: Optional[str] = Field(default=None, max_length=32, description="点踩原因 code")
    feedback_comment: Optional[str] = Field(default=None, max_length=300, description="补充说明")

    class Config:
        extra = "forbid"


class AnswerFeedbackQueryRequest(BaseModel):
    """按问答记录 ID 查询当前反馈状态。"""

    history_id: str = Field(..., min_length=1, max_length=64, description="问答记录 ID")

    class Config:
        extra = "forbid"


class AnswerFeedbackItemResponse(BaseModel):
    """反馈主记录响应。"""

    id: str
    history_id: str
    feedback_type: str
    feedback_reason: Optional[str] = None
    feedback_comment: Optional[str] = None
    version: int = 0
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class SubmitAnswerFeedbackResponse(BaseModel):
    """提交反馈响应。"""

    feedback: Optional[AnswerFeedbackItemResponse] = None
    superseded: bool = False
    idempotent: bool = False


class AnswerFeedbackStatusResponse(BaseModel):
    """回显反馈状态响应。"""

    feedback: Optional[AnswerFeedbackItemResponse] = None


class AnswerFeedbackListItemResponse(BaseModel):
    """反馈单条记录响应（按知识库聚合列表项）。"""

    id: str
    tenant_id: str
    user_id: str
    history_id: str
    query: str
    answer: Optional[str] = None
    feedback_type: str
    feedback_reason: Optional[str] = None
    feedback_comment: Optional[str] = None
    knowledge_base_ids: list[str] = Field(default_factory=list)
    source: Optional[str] = None
    mode: Optional[str] = None
    source_missing_reason: Optional[str] = None
    adopted: bool = False
    version: int = 0
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class AnswerFeedbackListResponse(BaseModel):
    """反馈列表响应。"""

    items: list[AnswerFeedbackListItemResponse] = Field(default_factory=list)
    total: int = 0
    like_count: int = 0
    dislike_count: int = 0
    page: int = 1
    page_size: int = 50


class AnswerFeedbackStatsResponse(BaseModel):
    """反馈统计响应。"""

    total: int = 0
    like_count: int = 0
    dislike_count: int = 0


class AnswerFeedbackToggleAdoptRequest(BaseModel):
    """切换反馈记录的采纳状态。"""

    feedback_id: str = Field(..., min_length=1, max_length=64, description="反馈记录 ID")

    class Config:
        extra = "forbid"
