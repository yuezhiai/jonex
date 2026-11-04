"""W2.5 质量问题工单：列表/详情、指派、状态流转、复验、评论、摘要。

权限：
- 指派 / 复验：仅租户管理员；
- 状态流转 / 评论：管理员或当前 assignee；
- 读：任意登录用户。
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import text

from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import (
    InvalidParameterError,
    PermissionDeniedError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from jonex_core.common.tenant import require_tenant

from ..repository import quality_issue_repository as repo
from .quality.common import (
    audit,
    is_admin,
    iso,
    paging,
    require_admin,
    require_login,
)

logger = logging.getLogger(__name__)

_RESOURCE = "quality_issue"
STATUSES = frozenset({"open", "assigned", "fixing", "pending_verify", "closed"})
SEVERITIES = frozenset({"info", "warning", "error", "critical"})
TRANSITIONS = {
    "open": frozenset({"assigned", "fixing"}),
    "assigned": frozenset({"fixing"}),
    "fixing": frozenset({"pending_verify"}),
    "pending_verify": frozenset({"fixing"}),
}
NOTE_MAX = 2000


def _invalid(message: str, **details) -> None:
    raise InvalidParameterError(message=message, details=details or None)


def _require_revision(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _invalid("expected_revision 必须是整数")
    return value


def _require_note(note, *, required: bool = False) -> str | None:
    if note is None:
        if required:
            _invalid("note 必填")
        return None
    if not isinstance(note, str):
        _invalid("note 必须是字符串")
    text_value = note.strip()
    if required and not text_value:
        _invalid("note 不能为空")
    if len(text_value) > NOTE_MAX:
        _invalid("note 不能超过 2000 字符")
    return text_value or None


def _filters(data: dict) -> dict:
    status = data.get("status")
    if status not in (None, "") and status not in STATUSES:
        _invalid("status 非法", status=status)
    severity = data.get("severity")
    if severity not in (None, "") and severity not in SEVERITIES:
        _invalid("severity 非法", severity=severity)
    asset_type = data.get("asset_type")
    if asset_type not in (None, "", "table", "column"):
        _invalid("asset_type 非法", asset_type=asset_type)
    return {
        "status": status or None,
        "severity": severity or None,
        "assignee_id": data.get("assignee_id") or None,
        "data_source_id": data.get("data_source_id") or None,
        "asset_type": asset_type or None,
        "asset_id": data.get("asset_id") or None,
        "keyword": (data.get("keyword") or "").strip() or None,
    }


async def _ensure_assignee(session, tenant_id: str, assignee_id) -> str:
    if not isinstance(assignee_id, str) or not assignee_id.strip():
        _invalid("assignee_id 必填")
    row = (
        await session.execute(
            text(
                "SELECT 1 FROM platform.users "
                "WHERE tenant_id=:tenant_id AND id::text=:assignee_id "
                "AND status=1 LIMIT 1"
            ),
            {"tenant_id": tenant_id, "assignee_id": assignee_id.strip()},
        )
    ).scalar()
    if row is None:
        _invalid("指派人不存在或已停用", assignee_id=assignee_id)
    return assignee_id.strip()


class QualityIssueService:
    """问题工单读取与状态机。"""

    async def list_issues(self, tenant_id: str, user_id, data: dict | None) -> dict:
        tenant_id = require_tenant(tenant_id)
        require_login(user_id, "需要登录后查看质量问题")
        data = data or {}
        page, page_size = paging(data)
        filters = _filters(data)
        async with get_db_session() as session:
            items = await repo.list_page(
                session, tenant_id=tenant_id, page=page, page_size=page_size,
                filters=filters,
            )
            total = await repo.count(session, tenant_id=tenant_id, filters=filters)
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    async def get_issue(self, tenant_id: str, user_id, issue_id: str) -> dict:
        tenant_id = require_tenant(tenant_id)
        require_login(user_id, "需要登录后查看质量问题")
        async with get_db_session() as session:
            issue = await repo.get_by_id(
                session, tenant_id=tenant_id, issue_id=issue_id,
            )
            if issue is None:
                raise ResourceNotFoundError(message="质量问题不存在")
            events = await repo.list_events(
                session, tenant_id=tenant_id, issue_id=issue_id,
            )
        return {**issue, "events": events}

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------

    async def assign(
        self, tenant_id: str, user_id, issue_id: str, assignee_id: str,
        expected_revision,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        await require_admin(tenant_id, user_id, "仅租户管理员可指派质量问题")
        revision = _require_revision(expected_revision)
        async with get_db_session() as session:
            issue = await repo.get_by_id(
                session, tenant_id=tenant_id, issue_id=issue_id,
            )
            if issue is None:
                raise ResourceNotFoundError(message="质量问题不存在")
            if issue["status"] not in ("open", "assigned", "fixing"):
                raise ResourceConflictError(message="当前状态不支持指派")
            assignee = await _ensure_assignee(session, tenant_id, assignee_id)
            to_status = "assigned" if issue["status"] == "open" else issue["status"]
            rowcount = await repo.update_issue(
                session, tenant_id=tenant_id, issue_id=issue_id,
                expected_revision=revision,
                fields={
                    "assignee_id": assignee,
                    "status": to_status,
                    "resolved_at": None,
                    "updated_by": user_id or None,
                },
            )
            if rowcount == 0:
                raise ResourceConflictError(message="工单已被修改，请刷新后重试")
            await repo.append_event(
                session, tenant_id=tenant_id, issue_id=issue_id,
                event_type="assigned", from_status=issue["status"],
                to_status=to_status, actor_id=user_id or None,
                note=f"指派给 {assignee}",
            )
            fresh = await repo.get_by_id(
                session, tenant_id=tenant_id, issue_id=issue_id,
            )
        audit(
            tenant_id=tenant_id, user_id=user_id, action="quality_issue_assign",
            resource=_RESOURCE, resource_id=issue_id,
            params={"assignee_id": assignee, "status": to_status},
        )
        return fresh

    async def update_status(
        self, tenant_id: str, user_id, issue_id: str, to_status: str,
        expected_revision, note=None,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        require_login(user_id, "需要登录后操作质量问题")
        revision = _require_revision(expected_revision)
        if to_status not in STATUSES:
            _invalid("to_status 非法", to_status=to_status)
        note_value = _require_note(note)
        async with get_db_session() as session:
            issue = await repo.get_by_id(
                session, tenant_id=tenant_id, issue_id=issue_id,
            )
            if issue is None:
                raise ResourceNotFoundError(message="质量问题不存在")
            await self._require_issue_actor(tenant_id, user_id, issue)
            from_status = issue["status"]
            if to_status not in TRANSITIONS.get(from_status, frozenset()):
                raise ResourceConflictError(
                    message=f"不允许从 {from_status} 流转到 {to_status}",
                )
            if from_status == "fixing" and to_status == "pending_verify" and not note_value:
                _invalid("提交整改（pending_verify）必须填写整改说明")
            fields = {
                "status": to_status,
                "resolved_at": None,
                "updated_by": user_id or None,
            }
            rowcount = await repo.update_issue(
                session, tenant_id=tenant_id, issue_id=issue_id,
                expected_revision=revision, fields=fields,
            )
            if rowcount == 0:
                raise ResourceConflictError(message="工单已被修改，请刷新后重试")
            await repo.append_event(
                session, tenant_id=tenant_id, issue_id=issue_id,
                event_type="status_change", from_status=from_status,
                to_status=to_status, actor_id=user_id or None,
                note=note_value,
            )
            fresh = await repo.get_by_id(
                session, tenant_id=tenant_id, issue_id=issue_id,
            )
        audit(
            tenant_id=tenant_id, user_id=user_id, action="quality_issue_status",
            resource=_RESOURCE, resource_id=issue_id,
            params={"from": from_status, "to": to_status},
        )
        return fresh

    async def verify(
        self, tenant_id: str, user_id, issue_id: str, passed: bool,
        note, expected_revision,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        await require_admin(tenant_id, user_id, "仅租户管理员可复验质量问题")
        revision = _require_revision(expected_revision)
        if not isinstance(passed, bool):
            _invalid("passed 必须是布尔值")
        note_value = _require_note(note, required=not passed)
        async with get_db_session() as session:
            issue = await repo.get_by_id(
                session, tenant_id=tenant_id, issue_id=issue_id,
            )
            if issue is None:
                raise ResourceNotFoundError(message="质量问题不存在")
            if issue["status"] != "pending_verify":
                raise ResourceConflictError(message="只有待复验的工单可以复验")
            to_status = "closed" if passed else "fixing"
            fields = {
                "status": to_status,
                "updated_by": user_id or None,
            }
            if passed:
                fields["resolved_at"] = datetime.now().astimezone()
            rowcount = await repo.update_issue(
                session, tenant_id=tenant_id, issue_id=issue_id,
                expected_revision=revision, fields=fields,
            )
            if rowcount == 0:
                raise ResourceConflictError(message="工单已被修改，请刷新后重试")
            await repo.append_event(
                session, tenant_id=tenant_id, issue_id=issue_id,
                event_type="verified" if passed else "reopened",
                from_status="pending_verify", to_status=to_status,
                actor_id=user_id or None, note=note_value,
            )
            fresh = await repo.get_by_id(
                session, tenant_id=tenant_id, issue_id=issue_id,
            )
        audit(
            tenant_id=tenant_id, user_id=user_id, action="quality_issue_verify",
            resource=_RESOURCE, resource_id=issue_id,
            params={"passed": passed, "status": to_status},
        )
        return fresh

    async def comment(
        self, tenant_id: str, user_id, issue_id: str, note,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        require_login(user_id, "需要登录后评论质量问题")
        note_value = _require_note(note, required=True)
        async with get_db_session() as session:
            issue = await repo.get_by_id(
                session, tenant_id=tenant_id, issue_id=issue_id,
            )
            if issue is None:
                raise ResourceNotFoundError(message="质量问题不存在")
            await self._require_issue_actor(tenant_id, user_id, issue)
            if issue["status"] == "closed":
                raise ResourceConflictError(message="已关闭的工单不能评论")
            event = await repo.append_event(
                session, tenant_id=tenant_id, issue_id=issue_id,
                event_type="comment", from_status=issue["status"],
                to_status=issue["status"], actor_id=user_id or None,
                note=note_value,
            )
        audit(
            tenant_id=tenant_id, user_id=user_id, action="quality_issue_comment",
            resource=_RESOURCE, resource_id=issue_id, params={},
        )
        return event

    # ------------------------------------------------------------------
    # 摘要
    # ------------------------------------------------------------------

    async def summary(
        self, tenant_id: str, user_id, data_source_id: str | None = None,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        require_login(user_id, "需要登录后查看质量问题")
        async with get_db_session() as session:
            rows = await repo.summary_rows(
                session, tenant_id=tenant_id, data_source_id=data_source_id,
            )
        items = []
        total_rules = 0
        total_open = 0
        last_run_at = None
        for row in rows:
            open_by_severity = {
                "critical": int(row.get("critical") or 0),
                "error": int(row.get("error_count") or 0),
                "warning": int(row.get("warning") or 0),
                "info": int(row.get("info") or 0),
            }
            open_count = sum(open_by_severity.values())
            total_rules += int(row.get("total_rules") or 0)
            total_open += open_count
            run_at = row.get("latest_run_at")
            if run_at is not None and (last_run_at is None or run_at > last_run_at):
                last_run_at = run_at
            latest = None
            if row.get("latest_run_status"):
                latest = {
                    "status": row["latest_run_status"],
                    "finished_at": iso(run_at) if isinstance(run_at, datetime) else run_at,
                    "passed": int(row.get("passed") or 0),
                    "failed": int(row.get("failed") or 0),
                    "error": int(row.get("error") or 0),
                }
            items.append({
                "data_source_id": row["data_source_id"],
                "name": row.get("name"),
                "total_rules": int(row.get("total_rules") or 0),
                "enabled_rules": int(row.get("enabled_rules") or 0),
                "latest_run": latest,
                "open_issues": open_by_severity,
            })
        return {
            "data_sources": items,
            "totals": {
                "rules": total_rules,
                "open_issues": total_open,
                "last_run_at": (
                    iso(last_run_at) if isinstance(last_run_at, datetime) else last_run_at
                ),
            },
        }

    async def _require_issue_actor(self, tenant_id: str, user_id, issue: dict) -> None:
        if await is_admin(tenant_id, user_id):
            return
        if issue.get("assignee_id") and str(issue["assignee_id"]) == str(user_id):
            return
        raise PermissionDeniedError(message="仅管理员或当前指派人可操作该工单")
