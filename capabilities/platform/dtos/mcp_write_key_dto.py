"""知识写入 Key DTO（v1.4 Phase 17 WRITE-01）。

契约要点（SDD）：
- WriteKeyCreateResponse 含一次性明文 plaintext（仅创建响应返回一次）；
- WriteKeyResponse（列表/查询）脱敏——仅 key_prefix + status（派生），不含 key_hash / plaintext；
- grants[] 存写入范围 [{kb, mode(all|specified), directories[]}]；
- expires_at = None 表示永久有效。
"""
from datetime import datetime
from typing import Optional

try:
    from pydantic.v1 import BaseModel, Field, validator
except ImportError:
    from pydantic import BaseModel, Field, validator


class WriteGrant(BaseModel):
    """单个知识库写入授权范围。

    mode ∈ {all, specified}；specified 时 directories 为平铺 folder_id 列表。
    """

    kb: str
    mode: str
    directories: list[str] = []

    @validator("mode")
    def validate_mode(cls, v):
        if v not in {"all", "specified"}:
            raise ValueError("mode 必须是 all 或 specified")
        return v


class WriteKeyCreateRequest(BaseModel):
    """创建知识写入 Key 请求体。"""

    name: str = Field(..., max_length=255)
    expires_at: Optional[datetime] = Field(default=None)
    grants: list[WriteGrant] = Field(default_factory=list)

    @validator("name")
    def validate_name(cls, v):
        """strip 后空串拒绝。"""
        if isinstance(v, str):
            stripped = v.strip()
            if not stripped:
                raise ValueError("name 不能为空")
            return stripped
        return v


class WriteKeyUpdateRequest(BaseModel):
    """更新知识写入 Key 请求体（PUT 局部更新，全 Optional）。

    None 表示不改动该字段。
    """

    name: Optional[str] = Field(default=None, max_length=255)
    expires_at: Optional[datetime] = Field(default=None)
    grants: Optional[list[WriteGrant]] = Field(default=None)

    @validator("name")
    def validate_name(cls, v):
        """仅当 name 非 None 时校验（strip 后空串拒绝）。"""
        if v is not None and isinstance(v, str):
            stripped = v.strip()
            if not stripped:
                raise ValueError("name 不能为空")
            return stripped
        return v


class WriteKeyResponse(BaseModel):
    """知识写入 Key 响应（脱敏）。

    不含 key_hash / plaintext；status 为派生字段（expired/disabled/revoked/active），
    非 DB 列，故由 service 层手动构造（orm_mode = False）。
    """

    id: str
    name: str
    key_prefix: str
    status: str
    grants: list[WriteGrant] = Field(default_factory=list)
    space_id: Optional[str] = None
    kb_id: Optional[str] = None
    created_at: Optional[datetime] = None
    created_by: Optional[str] = None
    updated_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    disabled_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    revoked_by: Optional[str] = None

    class Config:
        orm_mode = False


class WriteKeyCreateResponse(BaseModel):
    """创建知识写入 Key 响应——明文仅在此 DTO 一次性返回。"""

    id: str
    plaintext: str
    name: str
    key_prefix: str
    grants: list[WriteGrant] = Field(default_factory=list)
    space_id: Optional[str] = None
    kb_id: Optional[str] = None
    created_at: datetime
    created_by: Optional[str] = None
    expires_at: Optional[datetime] = None

    class Config:
        orm_mode = False
