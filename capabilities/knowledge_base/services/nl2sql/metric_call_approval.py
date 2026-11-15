#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""W4.8 指标级调用审批（发布/下线/调用）。

设计：docs/superpowers/specs/2026-11-01-w4.8-metric-call-approval-design.md（工作区文档，不入库）。

- 存储：`knowledge_base.metric_call_approvals`（迁移 035 / update 087）；
- 状态：`pending`（已申请）→ `approved`（可调用）/ `suspended`（已下线）；
  `approved` 可带 `expires_at`（到期后**读时**判定为 `expired`，不写回库）；
- 开关：`METRIC_CALL_APPROVAL_ENABLED`（默认关；关时 `ask_metric` 不查表、逐字节同旧行为）；
- 有效期策略：批准时 `expires_at`（ISO）/ `expires_in_days` 优先，都没给时用
  `METRIC_APPROVAL_TTL_DAYS`（默认 0 = 不过期，与引入前一致）；
- **申请**任何有读权限的用户可发；**审批/下线**仅租户管理员（服务内校验）。
"""
from __future__ import annotations

import datetime as _dt
import logging
import uuid

from jonex_core.common.config import config
from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import (
    InvalidParameterError,
    PermissionDeniedError,
    ResourceNotFoundError,
)
from jonex_core.common.tenant import require_tenant

from ...repository.metric_call_approval_repository import (
    MetricCallApprovalRepository,
)

logger = logging.getLogger(__name__)

STATUSES = ("pending", "approved", "suspended")
#: 运行时可能出现的「生效状态」：到期是**读时计算**，不写回库（保留批准历史）。
EFFECTIVE_STATUSES = STATUSES + ("expired",)

_UTC = _dt.timezone.utc


def _now() -> _dt.datetime:
    return _dt.datetime.now(_UTC)


def _aware(value: _dt.datetime) -> _dt.datetime:
    """库里取回的 naive 时间按 UTC 解释（本列以 UTC 写入）。"""
    return value if value.tzinfo is not None else value.replace(tzinfo=_UTC)


def parse_expires_at(value) -> _dt.datetime | None:
    """ISO 8601 字符串 → aware datetime；空值 None；非法值 1001。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise InvalidParameterError(
            message="expires_at 必须是 ISO 8601 时间") from None
    return _aware(parsed)


def resolve_expiry(expires_at=None, expires_in_days=None, *,
                   now: _dt.datetime | None = None) -> _dt.datetime | None:
    """批准时的有效期：显式时间 > 显式天数 > 配置默认 TTL > 不过期。

    只接受**未来**时间与**正**天数；配错不该悄悄变成「不过期」（那是更宽的权限）。
    """
    current = now or _now()
    explicit = parse_expires_at(expires_at)
    if explicit is not None:
        if explicit <= current:
            raise InvalidParameterError(message="expires_at 必须晚于当前时间")
        return explicit
    if expires_in_days is not None:
        try:
            days = int(expires_in_days)
        except (TypeError, ValueError):
            raise InvalidParameterError(
                message="expires_in_days 必须是正整数") from None
        if days <= 0:
            raise InvalidParameterError(message="expires_in_days 必须是正整数")
        return current + _dt.timedelta(days=days)
    ttl = int(config.METRIC_APPROVAL_TTL_DAYS or 0)
    if ttl > 0:
        return current + _dt.timedelta(days=ttl)
    return None


def effective_status(row, *, now: _dt.datetime | None = None) -> str:
    """存储状态 + 到期计算。只有 `approved` 会变成 `expired`。"""
    status = str(getattr(row, "status", "") or "")
    expires_at = getattr(row, "expires_at", None)
    if status != "approved" or expires_at is None:
        return status
    return "expired" if _aware(expires_at) <= (now or _now()) else status


async def status_for(tenant_id: str, kb_id: str, metric_name: str) -> str:
    """当前调用状态；没有申请行 → `not_requested`。"""
    tenant_id = require_tenant(tenant_id)
    async with get_db_session() as session:
        row = await MetricCallApprovalRepository(session).get_for_metric(
            tenant_id, kb_id, metric_name)
    return effective_status(row) if row else "not_requested"


async def list_approvals(tenant_id: str, kb_id: str, *,
                         status: str | None = None) -> dict:
    tenant_id = require_tenant(tenant_id)
    if status is not None and status not in STATUSES:
        raise InvalidParameterError(message="status 必须是 pending/approved/suspended")
    async with get_db_session() as session:
        rows = await MetricCallApprovalRepository(session).list_for_kb(
            tenant_id, kb_id, status=status)
    return {"items": [_view(row) for row in rows]}


async def request_approval(tenant_id: str, kb_id: str, metric_name: str, *,
                           user_id: str = "", reason: str = "") -> dict:
    tenant_id = require_tenant(tenant_id)
    metric_name = str(metric_name or "").strip()
    if not metric_name:
        raise InvalidParameterError(message="metric_name 不能为空")
    async with get_db_session() as session:
        repo = MetricCallApprovalRepository(session)
        existing = await repo.get_for_metric(tenant_id, kb_id, metric_name)
        if existing is not None:
            if existing.status == "suspended":
                return {"item": _view(existing), "created": False,
                        "idempotent": True}
            return {"item": _view(existing), "created": False, "idempotent": True}
        row = await repo.create(
            id=uuid.uuid4().hex, tenant_id=tenant_id,
            knowledge_base_id=kb_id, metric_name=metric_name,
            status="pending", requested_by=str(user_id or ""),
            reason=str(reason or "")[:500],
        )
        return {"item": _view(row), "created": True, "idempotent": False}


async def decide_approval(tenant_id: str, kb_id: str, metric_name: str,
                          status: str, *, user_id: str = "",
                          reason: str = "", expires_at=None,
                          expires_in_days=None) -> dict:
    """管理员审批/下线；无申请行 → 1002（先申请）。

    批准可带有效期（`expires_at` ISO 时间或 `expires_in_days` 天；都没给时用
    `METRIC_APPROVAL_TTL_DAYS`，0 = 不过期）；下线会清掉有效期。
    """
    tenant_id = require_tenant(tenant_id)
    if status not in ("approved", "suspended"):
        raise InvalidParameterError(message="status 必须是 approved/suspended")
    await _require_admin(tenant_id, user_id)
    async with get_db_session() as session:
        repo = MetricCallApprovalRepository(session)
        row = await repo.get_for_metric(tenant_id, kb_id, metric_name)
        if row is None:
            raise ResourceNotFoundError(
                message="该指标还没有调用申请",
                details={"metric_name": metric_name})
        row.status = status
        row.decided_by = str(user_id or "")
        row.reason = str(reason or row.reason or "")[:500]
        if status == "approved":
            row.expires_at = resolve_expiry(
                expires_at, expires_in_days)
        else:
            row.expires_at = None
        await session.flush()
        return {"item": _view(row), "status": status}


async def _require_admin(tenant_id: str, user_id: str) -> None:
    from ...services.space_permission_service import is_tenant_admin  # noqa: PLC0415

    if not user_id or not await is_tenant_admin(tenant_id, user_id):
        raise PermissionDeniedError(message="仅租户管理员可审批/下线指标调用")


def _view(row) -> dict:
    expires_at = getattr(row, "expires_at", None)
    return {
        "id": row.id,
        "knowledge_base_id": row.knowledge_base_id,
        "metric_name": row.metric_name,
        "status": row.status,
        "effective_status": effective_status(row),
        "requested_by": row.requested_by or "",
        "decided_by": row.decided_by or "",
        "reason": row.reason or "",
        "expires_at": expires_at.isoformat() if expires_at else None,
        "expired": effective_status(row) == "expired",
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


__all__ = [
    "EFFECTIVE_STATUSES", "STATUSES", "decide_approval", "effective_status",
    "list_approvals", "parse_expires_at", "request_approval", "resolve_expiry",
    "status_for",
]
