import re
from datetime import datetime
from typing import Optional

try:
    from pydantic.v1 import BaseModel, Field, validator
except ImportError:
    from pydantic import BaseModel, Field, validator


class McpServiceListRequest(BaseModel):
    """MCP 服务目录列表查询参数（E6 列表）"""

    search: str | None = Field(default=None, max_length=255)
    space_id: str | None = Field(default=None, max_length=64)
    status: str | None = Field(default=None)

    @validator("search")
    def strip_search(cls, v):
        if isinstance(v, str):
            stripped = v.strip()
            return stripped if stripped else None
        return v

    @validator("status")
    def validate_status(cls, v):
        if v is not None and v not in ("published", "unpublished"):
            raise ValueError("status 仅接受 'published' / 'unpublished' / None")
        return v


class McpServiceResponse(BaseModel):
    """单条 MCP 服务响应（E6 列表项）

    跨 schema 数据聚合：knowledge_base.services + knowledge_base.spaces
    + platform.mcp_service_publish。不由单一 ORM 直接映射，Service 层手动构造。
    """

    id: str
    name: str
    description: str | None = None
    domain_type: str | None = None
    space_id: str
    space_name: str = ""
    status: str
    enabled: int = 0
    kb_count: int = 0
    kb_names: list[str] = []
    enabled: int = 1
    is_published: bool = False
    published_at: datetime | None = None
    published_by: str | None = None
    last_call_at: datetime | None = None
    created_at: datetime | None = None
    tool: str | None = None
    tool_description: str | None = None
    service_type: str = "domain"


class McpServiceListResponse(BaseModel):
    """MCP 服务目录列表响应（E6）"""

    items: list[McpServiceResponse]
    total: int


class McpServiceDetailResponse(McpServiceResponse):
    """单个 MCP 服务详情

    在列表项基础上补充：
    - enabled：服务本体启用状态（1=启用，0=停用），与 is_published 独立
    - kb_ids：关联知识库 ID 列表
    - updated_at：服务最近更新时间
    """

    enabled: int = 1
    kb_ids: list[str] = []
    updated_at: datetime | None = None


class ToolConfigRequest(BaseModel):
    """保存领域服务 MCP Tool 配置请求（DS-03）

    tool 命名仅支持英文字母、数字、下划线（^[A-Za-z0-9_]+$）；
    同租户内唯一性在 Service 层校验（DTO 层只做格式校验）。
    """

    tool: str = Field(..., max_length=128)
    tool_description: Optional[str] = Field(default=None)

    @validator("tool")
    def validate_tool(cls, v):
        if not re.fullmatch(r"^[A-Za-z0-9_]+$", v):
            raise ValueError("tool 仅支持英文字母、数字、下划线")
        return v


class PublishRequest(BaseModel):
    """发布/取消发布请求（E7/E8 共用）

    操作人从 Authorization header 提取，body 为空。
    """

    pass


class TestCallRequest(BaseModel):
    """测试调用请求（E9）"""

    query: str = Field(..., min_length=1, max_length=2000)


class TestCallResponse(BaseModel):
    """测试调用响应（E9）

    成功时 answer 包含检索结果；失败时 success=False 且 error_code/error_message 说明原因。
    """

    success: bool = True
    answer: str
    kb_names: list[str] = []
    relevance: float = 0.0
    latency_ms: int = 0
    request_id: str
    error_code: Optional[str] = None
    error_message: Optional[str] = None


class AuthorizedKeyResponse(BaseModel):
    """已授权 Key 响应（E10 Key 列表项）

    来自 platform.mcp_keys JOIN mcp_key_service_mappings。
    """

    key_id: str
    key_name: str
    key_prefix: str
    permission_level: str
    key_status: str


class AuthorizedKeyListResponse(BaseModel):
    """授权 Key 列表响应（E10）"""

    items: list[AuthorizedKeyResponse]
    total: int


class ServiceKeyAddRequest(BaseModel):
    """从服务视角添加已有 MCP Key 的请求体"""

    key_id: str = Field(..., max_length=64)
    permission_level: str = Field(default="call")

    @validator("permission_level")
    def validate_pl(cls, v):
        if v not in ("call", "view"):
            raise ValueError("permission_level 仅支持 'call' / 'view'")
        return v

    @validator("key_id")
    def validate_key_id(cls, v):
        if not v or not v.strip():
            raise ValueError("key_id 不能为空")
        return v.strip()


class ServiceKeyCreateRequest(BaseModel):
    """从服务视角创建新 MCP Key 并关联的请求体"""

    name: str = Field(..., max_length=255)
    permission_level: str = Field(default="call")
    description: str | None = Field(default=None, max_length=512)
    expires_at: datetime | None = Field(default=None)

    @validator("name")
    def strip_name(cls, v):
        if isinstance(v, str):
            stripped = v.strip()
            if not stripped:
                raise ValueError("Key 名称不能为空")
            return stripped
        return v

    @validator("permission_level")
    def validate_pl(cls, v):
        if v not in ("call", "view"):
            raise ValueError("permission_level 仅支持 'call' / 'view'")
        return v


class ServiceKeyPermissionUpdateRequest(BaseModel):
    """从服务视角切换权限的请求体"""

    permission_level: str = Field(...)

    @validator("permission_level")
    def validate_pl(cls, v):
        if v not in ("call", "view"):
            raise ValueError("permission_level 仅支持 'call' / 'view'")
        return v


class ServiceKeyCreateResponse(BaseModel):
    """创建并关联新 Key 的响应——包含一次性明文 Key"""

    id: str
    plaintext: str
    name: str
    key_prefix: str
    permission_level: str
    created_at: datetime
    expires_at: datetime | None = None
