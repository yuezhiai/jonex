"""用户密级配置服务：列出用户密级、设置/清除「可读最高等级」。"""
from __future__ import annotations

from jonex_core.common.audit import schedule_emit
from jonex_core.common.audit_enums import ResourceType
from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import (
    InvalidParameterError,
    PermissionDeniedError,
    ResourceNotFoundError,
)
from jonex_core.common.tenant import require_tenant

from capabilities.knowledge_base.services.space_permission_service import is_tenant_admin

from ..repository import classification_repository
from ..repository import clearance_repository as repo
from .classification_service import _audit_user_id

_MAX_BIGINT_DIGITS = 19


async def _require_admin(tenant_id: str, user_id: str | None) -> None:
    if not user_id or not await is_tenant_admin(tenant_id, user_id):
        raise PermissionDeniedError(message="仅租户管理员可维护用户密级")


def _normalize_user_id(value) -> str:
    """把 user_id 归一成与 ``platform.users.id::text`` 一致的形式。

    ``list_user_clearances`` 用 ``u.id::text`` 连接，若把 ``"000123"`` 原样写入
    clearance 行，列表里永远匹配不到该用户；ASCII 十进制串去前导零后即规范形式。
    先折前导零再判长度：``"000...123"``（20 位）折完是合法 BIGINT，不能按原长
    误拒。非十进制/归一后仍超长值保持原样，交给 ``user_exists`` 安全拒绝（404）。
    """
    uid = str(value).strip()
    if uid.isascii() and uid.isdecimal():
        canonical = uid.lstrip("0") or "0"
        if len(canonical) <= _MAX_BIGINT_DIGITS:
            return canonical
    return uid


async def list_clearances(tenant_id: str, user_id: str | None) -> dict:
    """本租户未删除用户 + 当前密级；仅租户管理员。"""
    tenant_id = require_tenant(tenant_id)
    await _require_admin(tenant_id, user_id)
    async with get_db_session() as session:
        items = await repo.list_user_clearances(session, tenant_id)
    return {"items": items}


async def set_clearance(
    tenant_id: str,
    user_id: str | None,
    *,
    target_user_id,
    max_level_code,
    username: str | None = None,
    ip: str | None = None,
) -> dict:
    """设置/清除某用户的最高可读等级。

    ``max_level_code=None`` 表示清除（删除行，而不是写 NULL）；字符串按等级
    词表校验存在且启用。清除不校验等级。
    """
    tenant_id = require_tenant(tenant_id)
    await _require_admin(tenant_id, user_id)
    uid = _normalize_user_id(target_user_id)

    async with get_db_session() as session:
        if not await repo.user_exists(session, tenant_id, uid):
            raise ResourceNotFoundError(message="用户不存在")
        before_row = await repo.get_clearance(session, tenant_id, uid)
        before = before_row.max_level_code if before_row else None

        if max_level_code is None:
            await repo.delete_clearance(session, tenant_id, uid)
            after = None
        else:
            if not isinstance(max_level_code, str):
                raise InvalidParameterError(message="等级不存在或已停用")
            code = max_level_code.strip().lower()
            # 懒播种放在同一次事务内：全新租户直接设置内置等级时，get_level
            # 才不会因为词表尚未建立而误报 400。
            await classification_repository.ensure_default_levels(session, tenant_id)
            level = await classification_repository.get_level(session, tenant_id, code)
            if level is None or not level.is_active:
                raise InvalidParameterError(message="等级不存在或已停用")
            await repo.upsert_clearance(
                session,
                tenant_id=tenant_id,
                user_id=uid,
                max_level_code=code,
                updated_by=user_id,
            )
            after = code
        await session.commit()

    schedule_emit({
        "tenant_id": tenant_id, "user_id": _audit_user_id(user_id),
        "username": username, "ip": ip, "log_type": "OPERATION",
        "action": "classification_clearance_update", "outcome": "SUCCESS",
        "service_name": "data_governance",
        "resource": ResourceType.SYSTEM_CONFIG.value,
        "resource_id": f"user:{uid}",
        "request_params": {"user_id": uid, "before": before, "after": after},
    })
    return {"user_id": uid, "max_level_code": after}
