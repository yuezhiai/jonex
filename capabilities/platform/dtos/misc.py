from datetime import datetime
from typing import Any, Optional

try:
    from pydantic.v1 import BaseModel, Field
except ImportError:
    from pydantic import BaseModel, Field


# ============ 租户管理 ============

class TenantCreateRequest(BaseModel):
    id: str = Field(..., min_length=1, max_length=64, regex=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    plan_type: str = Field(default="free")
    expire_time: datetime | None = None


class TenantUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    plan_type: str | None = None
    status: int | None = None
    expire_time: datetime | None = None


class TenantResponse(BaseModel):
    id: str
    name: str
    description: str | None = None
    status: int
    plan_type: str
    expire_time: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    class Config:
        orm_mode = True


class TenantListResponse(BaseModel):
    items: list[TenantResponse]
    total: int


# ============ 系统配置 ============

class SystemConfigUpdateRequest(BaseModel):
    config_value: Optional[str] = None
    description: Optional[str] = None


class SystemConfigResponse(BaseModel):
    id: int
    config_group: str
    config_key: str
    config_value: Optional[str] = None
    value_type: str
    description: Optional[str] = None

    class Config:
        orm_mode = True


class SystemConfigListResponse(BaseModel):
    items: list[SystemConfigResponse]


# ============ 审计日志 ============

class AuditLogResponse(BaseModel):
    id: int
    tenant_id: Optional[str] = None  # 系统事件时为 None
    user_id: Optional[int] = None
    username: Optional[str] = None
    ip: Optional[str] = None
    action: str
    resource: Optional[str] = None
    resource_label: Optional[str] = None
    resource_name: Optional[str] = None
    resource_id: Optional[str] = None
    status_code: Optional[int] = None
    duration_ms: Optional[int] = None
    request_params: Optional[Any] = None
    trace_id: Optional[str] = None
    log_type: Optional[str] = None
    service_name: Optional[str] = None
    outcome: Optional[str] = None
    log_level: Optional[str] = None
    error_message: Optional[str] = None
    method: Optional[str] = None
    path: Optional[str] = None
    created_at: Optional[datetime] = None

    class Config:
        orm_mode = True


class AuditLogDetailResponse(AuditLogResponse):
    """详情视图：包含 response_body 和 error_stack"""
    response_body: Optional[Any] = None
    error_stack: Optional[str] = None


class AuditLogListResponse(BaseModel):
    total: int
    items: list[AuditLogResponse]


# ============ 任务调度 ============

class TaskScheduleCreateRequest(BaseModel):
    name: str = Field(..., max_length=255)
    task_type: str = Field(..., max_length=64)
    cron_expr: Optional[str] = None
    config_json: Optional[dict] = None


class TaskScheduleUpdateRequest(BaseModel):
    name: Optional[str] = None
    cron_expr: Optional[str] = None
    status: Optional[int] = None
    config_json: Optional[dict] = None


class TaskScheduleResponse(BaseModel):
    id: int
    tenant_id: str
    name: str
    task_type: str
    cron_expr: Optional[str] = None
    status: int
    last_run_at: Optional[datetime] = None
    next_run_at: Optional[datetime] = None
    config_json: Optional[Any] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        orm_mode = True


class TaskScheduleListResponse(BaseModel):
    total: int
    items: list[TaskScheduleResponse]
