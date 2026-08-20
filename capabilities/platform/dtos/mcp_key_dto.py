from datetime import datetime
from typing import Optional

try:
    from pydantic.v1 import BaseModel, Field, validator
except ImportError:
    from pydantic import BaseModel, Field, validator


VALID_PERMISSIONS = {"call", "view"}
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


class McpKeyCreateRequest(BaseModel):
    """创建 MCP Key 请求——permissions 白名单仅允许 call / view"""

    name: str = Field(default="", max_length=255)
    space_id: str = Field(..., max_length=64)
    permissions: list[str] = Field(default=["view"])
    allowed_kb_ids: list[str] = Field(default=[])
    service_ids: list[str] = Field(default=[])
    service_permissions: list[ServicePermissionItem] = Field(default=[])
    expires_at: datetime | None = Field(default=None)

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

    @validator("permissions")
    def validate_permissions(cls, v):
        """白名单校验：仅接受 call / view"""
        for perm in v:
            if perm not in VALID_PERMISSIONS:
                raise ValueError(
                    f"无效权限: {perm}，仅支持 {', '.join(sorted(VALID_PERMISSIONS))}"
                )
        return v

    @validator("allowed_kb_ids")
    def validate_allowed_kb_ids(cls, v):
        """元素级校验：拒绝空字符串、超长元素、超过 100 个元素"""
        if len(v) > 100:
            raise ValueError("allowed_kb_ids 最多 100 个")
        for element in v:
            if element == "":
                raise ValueError("allowed_kb_ids 元素不能为空字符串")
            if len(element) > 64:
                raise ValueError(
                    f"allowed_kb_ids 元素过长: {element[:20]}..., 最大 64 字符"
                )
        return v

    @validator("service_ids")
    def validate_service_ids(cls, v):
        """元素级校验：拒绝空字符串、超长元素、超过 100 个元素"""
        if len(v) > 100:
            raise ValueError("service_ids 最多 100 个")
        for element in v:
            if element == "":
                raise ValueError("service_ids 元素不能为空字符串")
            if len(element) > 64:
                raise ValueError(
                    f"service_ids 元素过长: {element[:20]}..., 最大 64 字符"
                )
        return v


class McpKeyResponse(BaseModel):
    """单条 Key 响应——不含 key_hash 和 tenant_id，permissions 从逗号字符串 split 为 list"""

    id: str
    name: str
    note: str | None = None
    key_prefix: str
    space_id: str | None = None
    permissions: list[str]
    allowed_kb_ids: list[str]
    service_ids: list[str] = []
    service_permissions: list[dict] = []
    created_by: str | None = None
    created_at: datetime | None = None
    revoked_at: datetime | None = None
    expires_at: datetime | None = None
    disabled_at: datetime | None = None
    status: str = "active"
    last_used_at: datetime | None = None

    class Config:
        orm_mode = True

    @validator("permissions", pre=True)
    def split_permissions(cls, v):
        """DB 逗号分隔字符串 → list[str]，空字符串/None → []"""
        if v is None:
            return []
        if isinstance(v, str):
            if v.strip() == "":
                return []
            return v.split(",")
        return v


class McpKeyCreateResponse(BaseModel):
    """创建/重置响应——明文 Key 仅在此 DTO 一次性返回"""

    id: str
    plaintext: str
    name: str
    key_prefix: str
    space_id: str | None = None
    permissions: list[str]
    allowed_kb_ids: list[str]
    service_ids: list[str] = []
    service_permissions: list[dict] = []
    created_at: datetime
    expires_at: datetime | None = None

    class Config:
        orm_mode = False


class _McpKeyUpsertFields(BaseModel):
    """MCP Key 重置/编辑请求共享字段与校验器。

    为 McpKeyResetRequest 和 McpKeyUpdateRequest 提供统一的 Optional 字段
    定义与校验逻辑，消除重复代码。所有字段默认为 None，由 service 层按
    "传入则更新，未传入则保留原值"的语义处理。

    注意：使用 typing.Optional[X] 而非 X | None，因为项目依赖
    pydantic>=1.10.0,<2.0.0，原生 Pydantic v1 不支持 Python 3.10+
    union 语法（尤其是 list[str] | None 等参数化泛型会解析失败，
    导致 code 1001 参数校验错误）。
    """

    name: Optional[str] = None
    space_id: Optional[str] = None
    permissions: Optional[list[str]] = None
    allowed_kb_ids: Optional[list[str]] = None
    service_ids: Optional[list[str]] = None
    service_permissions: Optional[list[ServicePermissionItem]] = Field(default=None)
    expires_at: Optional[datetime] = None

    @validator("space_id")
    def validate_space_id_upsert(cls, v):
        """space_id 非空校验——v 为 None（未传入）时跳过校验"""
        if v is None:
            return v
        if not v.strip():
            raise ValueError("space_id 不能为空")
        return v.strip()

    @validator("permissions")
    def validate_permissions(cls, v):
        """白名单校验——v 为 None（未传入）时跳过校验"""
        if v is None:
            return v
        for perm in v:
            if perm not in VALID_PERMISSIONS:
                raise ValueError(
                    f"无效权限: {perm}，仅支持 {', '.join(sorted(VALID_PERMISSIONS))}"
                )
        return v

    @validator("allowed_kb_ids")
    def validate_allowed_kb_ids(cls, v):
        """元素级校验——v 为 None（未传入）时跳过校验"""
        if v is None:
            return v
        if len(v) > 100:
            raise ValueError("allowed_kb_ids 最多 100 个")
        for element in v:
            if element == "":
                raise ValueError("allowed_kb_ids 元素不能为空字符串")
            if len(element) > 64:
                raise ValueError(
                    f"allowed_kb_ids 元素过长: {element[:20]}..., 最大 64 字符"
                )
        return v

    @validator("service_ids")
    def validate_service_ids(cls, v):
        """元素级校验——v 为 None（未传入）时跳过校验"""
        if v is None:
            return v
        if len(v) > 100:
            raise ValueError("service_ids 最多 100 个")
        for element in v:
            if element == "":
                raise ValueError("service_ids 元素不能为空字符串")
            if len(element) > 64:
                raise ValueError(
                    f"service_ids 元素过长: {element[:20]}..., 最大 64 字符"
                )
        return v


class McpKeyResetRequest(_McpKeyUpsertFields):
    """重置 MCP Key 请求体——所有字段可选，不传则继承旧 Key 对应值"""


class McpKeyRecreateRequest(BaseModel):
    """重新创建 MCP Key 请求——可选覆盖新 Key 的 expires_at（不传默认 12 个月）。

    继承原 Key 的 service_permissions / service_ids 授权，生成全新明文 Key，
    旧 Key 保留历史（不撤销）。区别于「启用」（toggle 恢复原 Key）。
    """

    expires_at: Optional[datetime] = None


class McpKeyUpdateRequest(_McpKeyUpsertFields):
    """编辑 MCP Key 请求——所有字段可选，不传则保持原值"""


class McpKeyListResponse(BaseModel):
    """列表响应——含 total 计数"""
    items: list[McpKeyResponse]
    total: int
