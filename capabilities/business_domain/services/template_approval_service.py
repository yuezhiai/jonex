"""模板场景发布审批（强制审批）。

流程：
- 普通编辑：`submit` 冻结一份 candidate snapshot 并创建 pending 申请；
- 租户管理员：`approve` 时校验「当前内容 == 申请时快照」，一致才发布
  （发布走 `TemplatePublishService.publish_scenario`，版本快照同事务落库）；
- 内容已变更 ⇒ 409，要求重新提交（不静默发布审批者没看过的内容）。

管理员自己可直接发布（capability 层另行校验），避免「自己提、自己批」死锁。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import (
    InvalidParameterError,
    PermissionDeniedError,
    ResourceConflictError,
    ResourceNotFoundError,
)

from ..repository import (
    TemplatePublishRequestRepository,
    TemplateScenarioRepository,
)
from . import _check_tenant
from .template_publish_service import TemplatePublishService

logger = logging.getLogger(__name__)

_PENDING = "pending"
_APPROVED = "approved"
_REJECTED = "rejected"
_CANCELLED = "cancelled"


async def _is_tenant_admin(tenant_id: str, user_id: str | None) -> bool:
    if not user_id:
        return False
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    from jonex_core.security.permission import has_permission

    return (
        await has_permission(tenant_id, uid, "tenant:admin")
        or await has_permission(tenant_id, uid, "platform:admin")
    )


def _actor(user_id: str | None, username: str | None) -> str:
    return (username or user_id or "").strip()


class TemplateApprovalService:
    """模板发布申请与审批。"""

    @staticmethod
    async def submit(
        tenant_id: str,
        scenario_id: str,
        user_id: str | None,
        username: str | None,
    ) -> dict:
        tenant_id = _check_tenant(tenant_id)
        if not scenario_id:
            raise InvalidParameterError(message="scenario_id 不能为空")

        async with get_db_session() as session:
            scenario_repo = TemplateScenarioRepository(session)
            await scenario_repo.get_required(scenario_id, tenant_id)

            req_repo = TemplatePublishRequestRepository(session)
            if await req_repo.get_pending(tenant_id, scenario_id) is not None:
                raise ResourceConflictError(
                    message="该场景已有待审批的发布申请",
                )

            candidate = await TemplatePublishService._build_candidate(
                session, tenant_id, scenario_id,
            )
            now = datetime.now(timezone.utc)
            row = await req_repo.create(
                id=uuid.uuid4().hex,
                tenant_id=tenant_id,
                domain_id=candidate["scenario"].domain_id,
                scenario_id=scenario_id,
                status=_PENDING,
                structure_hash=candidate["structure_hash"],
                snapshot=candidate["snapshot"],
                submitted_by=_actor(user_id, username),
                submitted_at=now,
                updated_at=now,
            )
            await session.commit()
        return row.to_dict()

    @staticmethod
    async def list_requests(
        tenant_id: str,
        scenario_id: str,
        user_id: str | None,
        username: str | None,
    ) -> dict:
        tenant_id = _check_tenant(tenant_id)
        if not scenario_id:
            raise InvalidParameterError(message="scenario_id 不能为空")
        admin = await _is_tenant_admin(tenant_id, user_id)
        submitted_by = None if admin else _actor(user_id, username)

        async with get_db_session() as session:
            scenario_repo = TemplateScenarioRepository(session)
            await scenario_repo.get_required(scenario_id, tenant_id)
            repo = TemplatePublishRequestRepository(session)
            items = await repo.list_for_scenario(
                tenant_id, scenario_id, submitted_by=submitted_by,
            )
        return {"items": [item.to_dict() for item in items], "total": len(items)}

    @staticmethod
    async def approve(
        tenant_id: str,
        request_id: str,
        user_id: str | None,
        username: str | None,
        comment: str | None = None,
    ) -> dict:
        tenant_id = _check_tenant(tenant_id)
        if not await _is_tenant_admin(tenant_id, user_id):
            raise PermissionDeniedError(message="仅租户管理员可以审批发布申请")

        async with get_db_session() as session:
            req_repo = TemplatePublishRequestRepository(session)
            request = await req_repo.get_for_tenant(request_id, tenant_id)
            if request is None:
                raise ResourceNotFoundError(message="发布申请不存在")
            if request.status != _PENDING:
                raise ResourceConflictError(
                    message=f"发布申请状态为 {request.status}，不能审批",
                )
            scenario_id = request.scenario_id
            expected_snapshot = request.snapshot

        # 发布内部会重新校验内容与 expected_snapshot 一致（内容漂移 ⇒ 409）。
        published = await TemplatePublishService.publish_scenario(
            tenant_id,
            scenario_id,
            published_by=_actor(user_id, username),
            expected_snapshot=expected_snapshot,
        )

        now = datetime.now(timezone.utc)
        async with get_db_session() as session:
            req_repo = TemplatePublishRequestRepository(session)
            request = await req_repo.get_for_tenant(request_id, tenant_id)
            if request is None:
                raise ResourceNotFoundError(message="发布申请不存在")
            await req_repo.update(
                request,
                tenant_id,
                status=_APPROVED,
                decided_by=_actor(user_id, username),
                decided_at=now,
                comment=comment,
                published_version=published["version"],
                updated_at=now,
            )
            await session.commit()
            request_dict = request.to_dict()

        return {"request": request_dict, "published": published}

    @staticmethod
    async def reject(
        tenant_id: str,
        request_id: str,
        user_id: str | None,
        username: str | None,
        comment: str | None = None,
    ) -> dict:
        tenant_id = _check_tenant(tenant_id)
        if not await _is_tenant_admin(tenant_id, user_id):
            raise PermissionDeniedError(message="仅租户管理员可以审批发布申请")
        return await _decide(
            tenant_id, request_id, _REJECTED,
            _actor(user_id, username), comment,
        )

    @staticmethod
    async def cancel(
        tenant_id: str,
        request_id: str,
        user_id: str | None,
        username: str | None,
    ) -> dict:
        tenant_id = _check_tenant(tenant_id)
        actor = _actor(user_id, username)
        admin = await _is_tenant_admin(tenant_id, user_id)

        async with get_db_session() as session:
            req_repo = TemplatePublishRequestRepository(session)
            request = await req_repo.get_for_tenant(request_id, tenant_id)
            if request is None:
                raise ResourceNotFoundError(message="发布申请不存在")
            if request.status != _PENDING:
                raise ResourceConflictError(
                    message=f"发布申请状态为 {request.status}，不能撤回",
                )
            if not admin and (request.submitted_by or "") != actor:
                raise PermissionDeniedError(message="只能撤回自己提交的申请")
            now = datetime.now(timezone.utc)
            await req_repo.update(
                request, tenant_id,
                status=_CANCELLED,
                decided_by=actor,
                decided_at=now,
                updated_at=now,
            )
            await session.commit()
            return request.to_dict()


async def _decide(
    tenant_id: str,
    request_id: str,
    status: str,
    actor: str,
    comment: str | None,
) -> dict:
    async with get_db_session() as session:
        req_repo = TemplatePublishRequestRepository(session)
        request = await req_repo.get_for_tenant(request_id, tenant_id)
        if request is None:
            raise ResourceNotFoundError(message="发布申请不存在")
        if request.status != _PENDING:
            raise ResourceConflictError(
                message=f"发布申请状态为 {request.status}，不能处理",
            )
        now = datetime.now(timezone.utc)
        await req_repo.update(
            request, tenant_id,
            status=status,
            decided_by=actor,
            decided_at=now,
            comment=comment,
            updated_at=now,
        )
        await session.commit()
        return request.to_dict()


__all__ = ["TemplateApprovalService"]
