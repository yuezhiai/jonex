from datetime import datetime
from typing import Optional

try:
    from pydantic.v1 import BaseModel, Field, validator
except ImportError:
    from pydantic import BaseModel, Field, validator


VALID_SERVICE_PERMISSION_LEVELS = {"call", "view"}


class ServicePermissionItem(BaseModel):
    """单条 service-permission 映射"""

    service_id: str = Field(..., max_length=64)
    permission_level: str = Field(default="call")

    @validator("permission_level")
    def validate_pl(cls, v):
        if v not in VALID_SERVICE_PERMISSION_LEVELS:
            raise ValueError(
                f"无效权限级别: {v}，仅支持 {', '.join(sorted(VALID_SERVICE_PERMISSION_LEVELS))}"
            )
        return v

    @validator("service_id")
    def validate_sid(cls, v):
        if not v or not v.strip():
            raise ValueError("service_id 不能为空")
        return v.strip()


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


class McpKeyCreateRequest(BaseModel):
    """创建统一 MCP Key 请求——多服务授权（service_permissions）+ 写入 grants（write_grants）"""

    name: str = Field(default="", max_length=255)
    note: Optional[str] = Field(default=None, max_length=512)
    space_id: str = Field(..., max_length=64)
    service_permissions: list[ServicePermissionItem] = Field(default=[])
    expires_at: datetime | None = Field(default=None)
    client_request_id: str = Field(..., max_length=64)
    write_grants: Optional[list[WriteGrant]] = Field(default=None)

    @validator("space_id")
    def validate_space_id(cls, v):
        if not v or not v.strip():
            raise ValueError("space_id 不能为空")
        return v.strip()

    @validator("name")
    def strip_name(cls, v):
        """自动 strip 空白：纯空格 → ''，非空则返回 strip 后的值"""
        if isinstance(v, str):
            stripped = v.strip()
            return stripped
        return v


class McpKeyResponse(BaseModel):
    """单条 Key 响应——不含 key_hash 和 tenant_id"""

    id: str
    name: str
    note: str | None = None
    key_prefix: str
    space_id: str | None = None
    service_permissions: list[dict] = []
    write_grants: Optional[list[WriteGrant]] = None
    auth_summary: str = ""
    write_scope_summary: str = ""
    created_by: str | None = None
    created_at: datetime | None = None
    revoked_at: datetime | None = None
    revoked_by: str | None = None
    expires_at: datetime | None = None
    disabled_at: datetime | None = None
    status: str = "active"
    last_used_at: datetime | None = None

    class Config:
        orm_mode = True


class McpKeyCreateResponse(BaseModel):
    """创建/重置响应——明文 Key 仅在此 DTO 一次性返回（幂等命中时 plaintext 为 None）"""

    id: str
    plaintext: Optional[str] = None
    name: str
    key_prefix: str
    space_id: str | None = None
    service_permissions: list[dict] = []
    write_grants: Optional[list[WriteGrant]] = None
    status: str = "active"
    auth_summary: str = ""
    write_scope_summary: str = ""
    mcp_config: Optional[dict] = None
    delivery_failed: bool = False
    dropped_service_ids: list[str] = Field(default=[])
    created_at: datetime
    expires_at: datetime | None = None

    class Config:
        orm_mode = False


class _McpKeyUpsertFields(BaseModel):
    """MCP Key 编辑请求共享字段与校验器。

    为 McpKeyUpdateRequest 提供统一的 Optional 字段
    定义与校验逻辑，消除重复代码。所有字段默认为 None，由 service 层按
    "传入则更新，未传入则保留原值"的语义处理。

    注意：使用 typing.Optional[X] 而非 X | None，因为项目依赖
    pydantic>=1.10.0,<2.0.0，原生 Pydantic v1 不支持 Python 3.10+
    union 语法（尤其是 list[str] | None 等参数化泛型会解析失败，
    导致 code 1001 参数校验错误）。
    """

    name: Optional[str] = None
    note: Optional[str] = Field(default=None, max_length=512)
    space_id: Optional[str] = None
    service_permissions: Optional[list[ServicePermissionItem]] = Field(default=None)
    expires_at: Optional[datetime] = None
    write_grants: Optional[list[WriteGrant]] = Field(default=None)

    @validator("space_id")
    def validate_space_id_upsert(cls, v):
        """space_id 非空校验——v 为 None（未传入）时跳过校验"""
        if v is None:
            return v
        if not v.strip():
            raise ValueError("space_id 不能为空")
        return v.strip()


class McpKeyRecreateRequest(BaseModel):
    """重新创建 MCP Key 请求——可选覆盖新 Key 的 expires_at（不传默认 12 个月）与 name。

    继承原 Key 的 service_permissions + write_grants 授权，生成全新明文 Key，
    旧 Key 保留历史（不撤销）。区别于「启用」（toggle 恢复原 Key）。
    """

    expires_at: Optional[datetime] = None
    name: Optional[str] = Field(default=None, max_length=255)


class McpKeyUpdateRequest(_McpKeyUpsertFields):
    """编辑 MCP Key 请求——所有字段可选，不传则保持原值"""


class McpKeyListResponse(BaseModel):
    """列表响应——含 total 计数"""
    items: list[McpKeyResponse]
    total: int
