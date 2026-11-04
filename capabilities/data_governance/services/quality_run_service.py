"""W2.5 巡检运行：SQL 预览、创建运行、结果回传与问题联动。

平台**不执行**检查 SQL：created run 只给外部执行器提供 SQL 快照，结果由
`submit_results` 回传。问题联动与结果写入在同一会话内完成。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import (
    InvalidParameterError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from jonex_core.common.tenant import require_tenant

from ..repository import (
    quality_issue_repository as issue_repo,
    quality_rule_repository as rule_repo,
    quality_run_repository as repo,
)
from .quality.common import audit, paging, require_admin, require_login
from .quality.sqlgen import DIALECTS, build_rule_sql, build_run_sql

logger = logging.getLogger(__name__)

_RESOURCE = "quality_run"
_RESULT_STATUSES = frozenset({"passed", "failed", "error"})
_RUN_STATUSES = frozenset({"running", "completed", "failed"})
_TRIGGERS = frozenset({"manual", "external"})
_MAX_RESULTS = 500
_MAX_RULE_IDS = 200
_SAMPLE_MAX = 50
_MESSAGE_MAX = 2000


def _invalid(message: str, **details) -> None:
    raise InvalidParameterError(message=message, details=details or None)


def _text(value, *, field: str, max_len: int | None = None, required=False):
    if value is None:
        if required:
            _invalid(f"{field} 必填")
        return None
    if not isinstance(value, str):
        _invalid(f"{field} 必须是字符串", field=field)
    text_value = value.strip()
    if required and not text_value:
        _invalid(f"{field} 不能为空", field=field)
    if max_len is not None and len(text_value) > max_len:
        _invalid(f"{field} 不能超过 {max_len} 字符", field=field)
    return text_value or None


async def _require_data_source(session, tenant_id: str, data_source_id: str) -> None:
    row = (
        await session.execute(
            text(
                "SELECT 1 FROM knowledge_base.knowledge_data_sources "
                "WHERE tenant_id=:tenant_id AND id=:data_source_id "
                "AND is_deleted=0 LIMIT 1"
            ),
            {"tenant_id": tenant_id, "data_source_id": data_source_id},
        )
    ).scalar()
    if row is None:
        raise ResourceNotFoundError(message="数据源不存在或已删除")


def _dialect(data: dict) -> str:
    dialect = data.get("dialect") or "tsql"
    if dialect not in DIALECTS:
        _invalid("dialect 必须是 tsql 或 ansi", dialect=dialect)
    return dialect


def _filters(data: dict) -> dict:
    status = data.get("status")
    if status not in (None, "") and status not in _RUN_STATUSES:
        _invalid("status 非法", status=status)
    return {
        "data_source_id": data.get("data_source_id") or None,
        "status": status or None,
    }


def _normalize_rule_ids(data: dict) -> list[str] | None:
    rule_ids = data.get("rule_ids")
    if rule_ids is None:
        return None
    if not isinstance(rule_ids, list) or not all(
        isinstance(item, str) and item.strip() for item in rule_ids
    ):
        _invalid("rule_ids 必须是字符串数组")
    if len(rule_ids) > _MAX_RULE_IDS:
        _invalid(f"rule_ids 不能超过 {_MAX_RULE_IDS} 个")
    return [item.strip() for item in rule_ids]


def _reject_unknown_rule_ids(requested: list[str], rules: list[dict]) -> None:
    """请求里点名但仓储没返回的 rule_id：跨源或拼错，静默丢弃会让调用方误判。"""
    found = {str(rule.get("id")) for rule in rules}
    missing = [rule_id for rule_id in dict.fromkeys(requested) if rule_id not in found]
    if missing:
        _invalid(
            "无效的 rule_id：" + ", ".join(missing),
            rule_ids=missing,
        )


def _snapshot_rule_sql(rule: dict, dialect: str) -> str | None:
    check_type = rule.get("check_type") or "library"
    if check_type == "library":
        return build_rule_sql(rule, dialect)
    if check_type == "sql":
        definition = rule.get("definition")
        query = definition.get("query") if isinstance(definition, dict) else None
        return query if isinstance(query, str) and query.strip() else None
    return None


def _build_rule_snapshot(rules: list[dict], dialect: str) -> list[dict]:
    return [
        {
            "rule_id": rule["id"],
            "name": rule.get("name"),
            "asset_type": rule.get("asset_type"),
            "asset_id": rule.get("asset_id"),
            "severity": rule.get("severity") or "warning",
            "sql": _snapshot_rule_sql(rule, dialect),
        }
        for rule in rules
    ]


def _snapshot_as_rule(entry: dict) -> dict:
    """快照条目 → 问题联动需要的规则形状（不再读当前规则表）。"""
    return {
        "id": entry.get("rule_id"),
        "name": entry.get("name"),
        "asset_type": entry.get("asset_type"),
        "asset_id": entry.get("asset_id"),
        "severity": entry.get("severity") or "warning",
    }


def _validate_results(results) -> list[dict]:
    if not isinstance(results, list) or not 1 <= len(results) <= _MAX_RESULTS:
        _invalid(f"results 数量必须在 1..{_MAX_RESULTS} 之间")
    seen: set[str] = set()
    output: list[dict] = []
    for item in results:
        if not isinstance(item, dict):
            _invalid("results 的每一项必须是对象")
        rule_id = item.get("rule_id")
        if not isinstance(rule_id, str) or not rule_id.strip():
            _invalid("rule_id 必填")
        if rule_id in seen:
            raise ResourceConflictError(
                message="results 中 rule_id 不能重复",
                details={"rule_id": rule_id},
            )
        seen.add(rule_id)
        status = item.get("status")
        if status not in _RESULT_STATUSES:
            _invalid("结果 status 非法", status=status)
        message = _text(item.get("message"), field="message", max_len=_MESSAGE_MAX)
        sample = item.get("sample")
        if sample is not None:
            if not isinstance(sample, list) or len(sample) > _SAMPLE_MAX:
                _invalid(f"sample 必须是最多 {_SAMPLE_MAX} 条的数组")
        metric_value = item.get("metric_value")
        try:
            # allow_nan=False：NaN/Infinity 不是合法 JSON（PostgreSQL JSONB 会拒收）。
            json.dumps(metric_value, allow_nan=False)
            json.dumps(sample, allow_nan=False)
        except (TypeError, ValueError):
            _invalid("metric_value / sample 必须是 JSON 可序列化值")
        output.append({
            "rule_id": rule_id.strip(),
            "status": status,
            "metric_value": metric_value,
            "message": message,
            "sample": sample,
        })
    return output


def _issue_detail(result: dict) -> str:
    parts = []
    if result.get("metric_value") is not None:
        try:
            parts.append("metric=" + json.dumps(result["metric_value"], ensure_ascii=False))
        except (TypeError, ValueError):
            parts.append("metric=<unserializable>")
    if result.get("message"):
        parts.append("message=" + str(result["message"]))
    return "; ".join(parts)[:2000]


class QualityRunService:
    """巡检预览 / 创建 / 回传 / 失败收口。"""

    async def preview_sql(self, tenant_id: str, user_id, data: dict | None) -> dict:
        tenant_id = require_tenant(tenant_id)
        require_login(user_id, "需要登录后预览质量检查 SQL")
        data = data or {}
        data_source_id = _text(
            data.get("data_source_id"), field="data_source_id", required=True,
        )
        dialect = _dialect(data)
        rule_ids = _normalize_rule_ids(data)
        async with get_db_session() as session:
            await _require_data_source(session, tenant_id, data_source_id)
            rules = await rule_repo.list_for_run(
                session, tenant_id=tenant_id,
                data_source_id=data_source_id, rule_ids=rule_ids,
            )
        if rule_ids is not None:
            _reject_unknown_rule_ids(rule_ids, rules)
        if not rules:
            raise InvalidParameterError(message="没有可执行的规则")
        items = build_run_sql(rules, dialect)
        warnings = [
            f"规则「{item['name']}」不是 library 类型，SQL 需由外部执行器提供"
            for item in items if item["sql"] is None
        ]
        return {"rules": items, "warnings": warnings}

    async def list_runs(self, tenant_id: str, user_id, data: dict | None) -> dict:
        tenant_id = require_tenant(tenant_id)
        require_login(user_id, "需要登录后查看巡检记录")
        data = data or {}
        page, page_size = paging(data)
        filters = _filters(data)
        async with get_db_session() as session:
            items = await repo.list_runs(
                session, tenant_id=tenant_id, page=page, page_size=page_size,
                filters=filters,
            )
            total = await repo.count_runs(
                session, tenant_id=tenant_id, filters=filters,
            )
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    async def get_run(self, tenant_id: str, user_id, run_id: str) -> dict:
        tenant_id = require_tenant(tenant_id)
        require_login(user_id, "需要登录后查看巡检记录")
        async with get_db_session() as session:
            run = await repo.get_run(
                session, tenant_id=tenant_id, run_id=run_id,
            )
            if run is None:
                raise ResourceNotFoundError(message="巡检运行不存在")
            results = await repo.list_results(
                session, tenant_id=tenant_id, run_id=run_id,
            )
        return {**run, "results": results}

    async def create_run(self, tenant_id: str, user_id, data: dict) -> dict:
        tenant_id = require_tenant(tenant_id)
        await require_admin(tenant_id, user_id, "仅租户管理员可发起质量巡检")
        data = data or {}
        data_source_id = _text(
            data.get("data_source_id"), field="data_source_id", required=True,
        )
        dialect = _dialect(data)
        executor = _text(data.get("executor"), field="executor", max_len=128)
        trigger = _text(data.get("trigger"), field="trigger", max_len=16) or "manual"
        if trigger not in _TRIGGERS:
            _invalid("trigger 必须是 manual 或 external", trigger=trigger)
        rule_ids = _normalize_rule_ids(data)

        async with get_db_session() as session:
            await _require_data_source(session, tenant_id, data_source_id)
            rules = await rule_repo.list_for_run(
                session, tenant_id=tenant_id,
                data_source_id=data_source_id, rule_ids=rule_ids,
            )
            if rule_ids is not None:
                _reject_unknown_rule_ids(rule_ids, rules)
            if not rules:
                raise InvalidParameterError(message="没有可执行的规则")
            items = build_run_sql(rules, dialect)
            run = await repo.create_run(
                session, tenant_id=tenant_id,
                data_source_id=data_source_id,
                rule_ids=[rule["id"] for rule in rules],
                rule_snapshot=_build_rule_snapshot(rules, dialect),
                trigger=trigger, executor=executor, dialect=dialect,
                created_by=user_id or None,
            )
        warnings = [
            f"规则「{item['name']}」不是 library 类型，SQL 需由外部执行器提供"
            for item in items if item["sql"] is None
        ]
        audit(
            tenant_id=tenant_id, user_id=user_id, action="quality_run_create",
            resource=_RESOURCE, resource_id=run["id"],
            params={"data_source_id": data_source_id, "rules": len(rules)},
        )
        return {"run": run, "rules": items, "warnings": warnings}

    async def submit_results(
        self, tenant_id: str, user_id, run_id: str, results,
        executor=None, message=None,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        await require_admin(tenant_id, user_id, "仅租户管理员可回传质量巡检结果")
        validated = _validate_results(results)
        executor_text = _text(executor, field="executor", max_len=128)
        message_text = _text(message, field="message", max_len=_MESSAGE_MAX)

        async with get_db_session() as session:
            run = await repo.get_run(
                session, tenant_id=tenant_id, run_id=run_id, for_update=True,
            )
            if run is None:
                raise ResourceNotFoundError(message="巡检运行不存在")
            if run["status"] != "running":
                raise ResourceConflictError(message="巡检运行已结束，不能重复回传结果")

            # 只认创建时的规则快照：规则被删除/改资产后，结果仍落到旧资产。
            rules_by_id = {
                str(entry["rule_id"]): entry
                for entry in run.get("rule_snapshot") or []
                if isinstance(entry, dict) and entry.get("rule_id")
            }
            for item in validated:
                if item["rule_id"] not in rules_by_id:
                    _invalid(
                        "结果里的 rule_id 不在本次运行快照内",
                        rule_id=item["rule_id"],
                    )

            rows = []
            for item in validated:
                rule = rules_by_id[item["rule_id"]]
                rows.append({
                    **item,
                    "asset_type": rule.get("asset_type"),
                    "asset_id": rule.get("asset_id"),
                })
            await repo.insert_results(
                session, tenant_id=tenant_id, run_id=run_id, results=rows,
            )

            counts = {"total": len(rows), "passed": 0, "failed": 0, "error": 0}
            now = datetime.now(timezone.utc)
            for item in rows:
                counts[item["status"]] += 1
                if item["status"] == "error":
                    continue
                rule = _snapshot_as_rule(rules_by_id[item["rule_id"]])
                if item["status"] == "failed":
                    await self._on_failed(session, tenant_id, run, rule, item, user_id, now)
                else:
                    await self._on_passed(session, tenant_id, run, rule, item, user_id)

            updated = await repo.mark_completed(
                session, tenant_id=tenant_id, run_id=run_id,
                counts=counts, message=message_text, executor=executor_text,
            )
            if updated == 0:
                raise ResourceConflictError(message="巡检运行已被其他请求收口")
            fresh = await repo.get_run(
                session, tenant_id=tenant_id, run_id=run_id,
            )

        audit(
            tenant_id=tenant_id, user_id=user_id, action="quality_run_submit",
            resource=_RESOURCE, resource_id=run_id,
            params={"counts": counts, "executor": executor_text},
        )
        return {"run": fresh, "counts": counts}

    async def fail_run(
        self, tenant_id: str, user_id, run_id: str, message=None,
    ) -> dict:
        tenant_id = require_tenant(tenant_id)
        await require_admin(tenant_id, user_id, "仅租户管理员可收口质量巡检")
        message_text = _text(message, field="message", max_len=_MESSAGE_MAX, required=True)
        async with get_db_session() as session:
            run = await repo.get_run(
                session, tenant_id=tenant_id, run_id=run_id, for_update=True,
            )
            if run is None:
                raise ResourceNotFoundError(message="巡检运行不存在")
            if run["status"] != "running":
                raise ResourceConflictError(message="巡检运行已结束")
            updated = await repo.mark_failed(
                session, tenant_id=tenant_id, run_id=run_id, message=message_text,
            )
            if updated == 0:
                raise ResourceConflictError(message="巡检运行已被其他请求收口")
            fresh = await repo.get_run(
                session, tenant_id=tenant_id, run_id=run_id,
            )
        audit(
            tenant_id=tenant_id, user_id=user_id, action="quality_run_fail",
            resource=_RESOURCE, resource_id=run_id, params={},
        )
        return fresh

    # ------------------------------------------------------------------
    # 结果 → 问题联动
    # ------------------------------------------------------------------

    async def _on_failed(
        self, session, tenant_id: str, run: dict, rule: dict, item: dict,
        user_id, now: datetime,
    ) -> None:
        detail = _issue_detail(item)
        issue = await issue_repo.find_open_for_update(
            session, tenant_id=tenant_id,
            rule_id=rule["id"], asset_id=rule["asset_id"],
        )
        if issue is not None:
            await issue_repo.update_issue(
                session, tenant_id=tenant_id, issue_id=issue["id"],
                expected_revision=issue["revision"],
                fields={
                    "severity": rule["severity"], "detail": detail,
                    "last_seen_at": now, "updated_by": user_id or None,
                },
            )
            return
        title = f"{rule['name']} 校验失败"[:255]
        try:
            # 嵌套事务：并发开单撞部分唯一索引时只回滚这一条 INSERT，
            # 外层事务仍可继续重查并更新已存在的工单。
            async with session.begin_nested():
                created = await issue_repo.create_issue(
                    session, tenant_id=tenant_id,
                    rule_id=rule["id"], asset_type=rule["asset_type"],
                    asset_id=rule["asset_id"], data_source_id=run["data_source_id"],
                    severity=rule["severity"], status="open", title=title,
                    detail=detail, created_by=user_id or None,
                    updated_by=user_id or None,
                )
        except IntegrityError:
            # 并发巡检同时开单：部分唯一索引兜底，转为更新已存在的那条。
            issue = await issue_repo.find_open_for_update(
                session, tenant_id=tenant_id,
                rule_id=rule["id"], asset_id=rule["asset_id"],
            )
            if issue is not None:
                await issue_repo.update_issue(
                    session, tenant_id=tenant_id, issue_id=issue["id"],
                    expected_revision=issue["revision"],
                    fields={
                        "severity": rule["severity"], "detail": detail,
                        "last_seen_at": now, "updated_by": user_id or None,
                    },
                )
            return
        await issue_repo.append_event(
            session, tenant_id=tenant_id, issue_id=created["id"],
            event_type="created", from_status=None, to_status="open",
            actor_id=user_id or None, note=title,
        )

    async def _on_passed(
        self, session, tenant_id: str, run: dict, rule: dict, item: dict, user_id,
    ) -> None:
        issue = await issue_repo.find_open_for_update(
            session, tenant_id=tenant_id,
            rule_id=rule["id"], asset_id=rule["asset_id"],
        )
        if issue is None or issue["status"] == "pending_verify":
            return
        updated = await issue_repo.update_issue(
            session, tenant_id=tenant_id, issue_id=issue["id"],
            expected_revision=issue["revision"],
            fields={
                "status": "pending_verify", "resolved_at": None,
                "updated_by": user_id or None,
            },
        )
        if updated:
            await issue_repo.append_event(
                session, tenant_id=tenant_id, issue_id=issue["id"],
                event_type="status_change", from_status=issue["status"],
                to_status="pending_verify", actor_id=user_id or None,
                note="巡检通过，等待复验",
            )
