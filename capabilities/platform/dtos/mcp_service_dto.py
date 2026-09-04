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
    capability_type: str | None = Field(default=None)
    source: str | None = Field(default=None, max_length=255)
    status: str | None = Field(default=None)

    @validator("search")
    def strip_search(cls, v):
        if isinstance(v, str):
            stripped = v.strip()
            return stripped if stripped else None
        return v

    @validator("capability_type")
    def validate_capability_type(cls, v):
        if v is not None and v not in ("domain", "write"):
            raise ValueError("capability_type 仅接受 'domain' / 'write' / None")
        return v

    @validator("status")
    def validate_status(cls, v):
        if v is not None and v not in ("published", "unpublished", "stopped"):
            raise ValueError("status 仅接受 'published' / 'unpublished' / 'stopped' / None")
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
    kb_count: int = 0
    kb_names: list[str] = []
    enabled: int = 1
    is_published: bool = False
    published_at: datetime | None = None
    published_by: str | None = None
    stopped_at: datetime | None = None
    stopped_by: str | None = None
    last_call_at: datetime | None = None
    created_at: datetime | None = None
    tool: str | None = None
    tool_description: str | None = None
    service_type: str = "domain"
    capability_type: str | None = None
    default_scope: str | None = None
    source: str | None = None


class McpServiceListResponse(BaseModel):
    """MCP 服务目录列表响应（E6）"""

    items: list[McpServiceResponse]
    total: int


class ApiAccessInfo(BaseModel):
    """领域服务 API 接入信息。

    endpoint 为规划路径占位（/api/v1/domain-services/{id}/query），真实接口另立；
    auth=api-key 锁定 API Key 认证；sample_payload 为通用占位（query 无真实 schema）。
    """

    endpoint: str
    method: str = "POST"
    auth: str = "api-key"
    sample_payload: dict | None = None


class McpAccessInfo(BaseModel):
    """领域服务 MCP 接入信息（展示用）。

    transport=streamable-http；tool_name 复用服务 tool 字段；
    auth_scheme=bearer（MCP Key）；server_url 取自 MCP_SERVER_PUBLIC_URL 配置，
    与 WorkBuddy 下发 mcp_config.url 保持一致（未配置时为 None，前端兜底展示）。
    """

    transport: str = "streamable-http"
    tool_name: str | None = None
    auth_scheme: str = "bearer"
    server_url: str | None = None


class ServiceAccessInfo(BaseModel):
    """服务接入方式聚合（API / MCP）。

    系统写服务（system.knowledge_document_write）无 query 语义，api=None 仅 MCP 接入。
    """

    api: ApiAccessInfo | None = None
    mcp: McpAccessInfo | None = None


class McpServiceDetailResponse(McpServiceResponse):
    """单个 MCP 服务详情

    在列表项基础上补充：
    - enabled：服务本体启用状态（1=启用，0=停用），与 is_published 独立
    - kb_ids：关联知识库 ID 列表
    - updated_at：服务最近更新时间
    - access：接入方式（api/mcp），向后兼容——旧前端忽略该字段不受影响
    """

    enabled: int = 1
    kb_ids: list[str] = []
    updated_at: datetime | None = None
    access: ServiceAccessInfo | None = None


class ToolConfigRequest(BaseModel):
    """保存领域服务 MCP Tool 配置请求（DS-03）

    tool 命名仅支持英文字母、数字、下划线（^[A-Za-z0-9_]+$），长度 1–128；
    tool_description 最长 255 字符（纵深防御，防 API 直连写超长）；
    同租户内唯一性在 Service 层校验（DTO 层只做格式/长度校验）。
    """

    tool: str = Field(..., max_length=128)
    tool_description: Optional[str] = Field(default=None, max_length=255)

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


class AuthorizedKeyItem(BaseModel):
    """单个领域服务已授权 Key 条目（对齐前端 AuthorizedKeyItem 契约）。

    由 platform.mcp_keys 与 platform.mcp_key_service_mappings 聚合：
    - key_id / key_name / key_prefix 来自 mcp_keys（key_prefix 为脱敏前缀，不含 mcp_ 前缀）
    - permission_level 来自映射表（call / view）
    - key_status 由 McpKey._derive_status 派生（active/revoked/expired/disabled）
    - expires_at 透出 Key 有效期（前端据此展示「永久有效」或到期时间）
    """

    key_id: str
    key_name: str
    key_prefix: str
    permission_level: str
    key_status: str
    expires_at: datetime | None = None
    created_at: datetime | None = None


class AuthorizedKeyListResponse(BaseModel):
    """领域服务已授权 Key 列表响应"""

    items: list[AuthorizedKeyItem]
    total: int
