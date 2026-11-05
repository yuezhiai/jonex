#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""W4.8 指标级调用审批（发布/下线/调用）。

设计：docs/superpowers/specs/2026-11-01-w4.8-metric-call-approval-design.md（工作区文档，不入库）。

- 存储：`knowledge_base.metric_call_approvals`（迁移 035 / update 087）；
- 状态：`pending`（已申请）→ `approved`（可调用）/ `suspended`（已下线）；
- 开关：`METRIC_CALL_APPROVAL_ENABLED`（默认关；关时 `ask_metric` 不查表、逐字节同旧行为）；
- **申请**任何有读权限的用户可发；**审批/下线**仅租户管理员（服务内校验）。
"""
from __future__ import annotations

import logging
import uuid

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


async def status_for(tenant_id: str, kb_id: str, metric_name: str) -> str:
    """当前调用状态；没有申请行 → `not_requested`。"""
    tenant_id = require_tenant(tenant_id)
    async with get_db_session() as session:
        row = await MetricCallApprovalRepository(session).get_for_metric(
            tenant_id, kb_id, metric_name)
    return str(row.status) if row else "not_requested"


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
                          reason: str = "") -> dict:
    """管理员审批/下线；无申请行 → 1002（先申请）。"""
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
        await session.flush()
        return {"item": _view(row), "status": status}


async def _require_admin(tenant_id: str, user_id: str) -> None:
    from ...services.space_permission_service import is_tenant_admin  # noqa: PLC0415

    if not user_id or not await is_tenant_admin(tenant_id, user_id):
        raise PermissionDeniedError(message="仅租户管理员可审批/下线指标调用")


def _view(row) -> dict:
    return {
        "id": row.id,
        "knowledge_base_id": row.knowledge_base_id,
        "metric_name": row.metric_name,
        "status": row.status,
        "requested_by": row.requested_by or "",
        "decided_by": row.decided_by or "",
        "reason": row.reason or "",
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


__all__ = [
    "STATUSES", "decide_approval", "list_approvals", "request_approval",
    "status_for",
]
