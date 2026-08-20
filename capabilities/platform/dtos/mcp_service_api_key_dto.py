"""领域服务 API Key DTO（v1.4 Phase 15 DS-04）。

契约要点（SDD）：
- ApiKeyCreateResponse 含一次性明文 plaintext（仅创建响应返回一次）；
- ApiKeyResponse（列表/查询）脱敏——仅 key_prefix，不含 key_hash / plaintext；
- expires_at = None 表示永久有效。
"""
from datetime import datetime

try:
    from pydantic.v1 import BaseModel, Field, validator
except ImportError:
    from pydantic import BaseModel, Field, validator


class ApiKeyCreateRequest(BaseModel):
    """创建领域服务 API Key 请求体。"""

    name: str = Field(..., max_length=255)
    expires_at: datetime | None = Field(default=None)

    @validator("name")
    def validate_name(cls, v):
        """strip 后空串拒绝。"""
        if isinstance(v, str):
            stripped = v.strip()
            if not stripped:
                raise ValueError("name 不能为空")
            return stripped
        return v


class ApiKeyResponse(BaseModel):
    """领域服务 API Key 响应（脱敏）。

    不含 key_hash / plaintext；status 为派生字段（revoked / expired / active），
    非 DB 列，故由 service 层手动构造（orm_mode = False）。
    """

    id: str
    service_id: str
    name: str
    key_prefix: str
    status: str
    created_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None

    class Config:
        orm_mode = False


class ApiKeyCreateResponse(BaseModel):
    """创建领域服务 API Key 响应——明文仅在此 DTO 一次性返回。"""

    id: str
    plaintext: str
    service_id: str
    name: str
    key_prefix: str
    created_at: datetime
    expires_at: datetime | None = None

    class Config:
        orm_mode = False
