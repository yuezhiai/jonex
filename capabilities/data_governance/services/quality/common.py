"""W2.5 质量闭环共用：权限、分页、审计与序列化。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from capabilities.knowledge_base.services.space_permission_service import (
    is_tenant_admin,
)
from jonex_core.common.audit import schedule_emit
from jonex_core.common.exceptions import InvalidParameterError, PermissionDeniedError

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


def is_authenticated(user_id) -> bool:
    """空值与 `anonymous` 视为未登录（与 W2.2/W2.3 同口径）。"""
    return bool(user_id) and user_id != "anonymous"


async def is_admin(tenant_id: str, user_id) -> bool:
    return is_authenticated(user_id) and await is_tenant_admin(tenant_id, user_id)


async def require_admin(tenant_id: str, user_id, message: str) -> None:
    if not await is_admin(tenant_id, user_id):
        raise PermissionDeniedError(message=message)


def require_login(user_id, message: str) -> None:
    if not is_authenticated(user_id):
        raise PermissionDeniedError(message=message)


def paging(data: dict | None) -> tuple[int, int]:
    """`page/page_size` → `(page, page_size)`；非法值 1001，越界截断。"""
    data = data or {}

    def _int(value, *, default: int, minimum: int, maximum: int | None, field: str) -> int:
        if value is None or value == "":
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidParameterError(
                message=f"{field} 必须是整数", details={"field": field},
            )
        number = max(minimum, value)
        if maximum is not None:
            number = min(maximum, number)
        return number

    page = _int(data.get("page"), default=1, minimum=1, maximum=None, field="page")
    page_size = _int(
        data.get("page_size"), default=DEFAULT_PAGE_SIZE,
        minimum=1, maximum=MAX_PAGE_SIZE, field="page_size",
    )
    return page, page_size


def audit_user_id(user_id) -> int | None:
    """invoke 链路的 user_id 是字符串；审计表只收 int，非法值落 None。"""
    return int(user_id) if user_id and str(user_id).isdigit() else None


def audit(
    *, tenant_id: str, user_id, action: str, resource: str,
    resource_id: str, params: dict[str, Any] | None = None,
) -> None:
    """非阻塞审计：只记标识与状态摘要，不落规则全文/结果样本。"""
    schedule_emit({
        "tenant_id": tenant_id,
        "user_id": audit_user_id(user_id),
        "log_type": "OPERATION",
        "action": action,
        "outcome": "SUCCESS",
        "service_name": "data_governance",
        "resource": resource,
        "resource_id": str(resource_id),
        "request_params": params or {},
    })


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
