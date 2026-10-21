"""保留策略解析与写入（第 3 期 · 任务 2）。

解析口径（**字段级继承**）：document > kb > tenant > system 默认。

- 每个字段独立继承：上级设置了 `retention_days` 不影响下级 `grace_days` 的取值。
- `None` = 该级没有覆盖，继续向上找；`0` 是合法覆盖
  （`retention_days=0` 表示不自动到期，`grace_days=0` 表示无宽限）。
- `scope` 返回真正设置了任一字段的最高一级作用域，供 UI 概览。

写入口径：租户级仅租户管理员；kb/document 级复用资产目录的三档写权限
（租户管理员 / 空间管理者 / KB 管理者）。
"""
from __future__ import annotations

from sqlalchemy import text

from jonex_core.common.audit import schedule_emit
from jonex_core.common.audit_enums import ResourceType
from jonex_core.common.config import get_config
from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import (
    InvalidParameterError,
    PermissionDeniedError,
    ResourceNotFoundError,
)
from jonex_core.common.tenant import require_tenant

from capabilities.knowledge_base.services.space_permission_service import (
    is_tenant_admin,
)

from ..repository import retention_policy_repository
from ..repository.retention_policy_repository import (
    RETENTION_FIELDS,
    normalize_policy_fields,
    normalize_scope,
)
from .catalog_service import CatalogService
from .permissions import assert_can_write, visible_kb_ids

_POLICY_FIELDS = RETENTION_FIELDS
_SCOPE_TYPES = frozenset({"tenant", "kb", "document"})

_KB_SPACE_SQL = """
SELECT space_id
FROM knowledge_base.knowledge_info
WHERE id = :kb_id AND tenant_id = :tenant_id AND is_deleted = 0
"""


def _audit_user_id(user_id) -> int | None:
    """invoke 链路的 user_id 是字符串；审计表只收 int，非法值落 None。"""
    if user_id and str(user_id).isdigit():
        return int(user_id)
    return None


def _system_defaults() -> dict:
    """系统默认值**每次调用时**从 `get_config()` 读取。

    不用模块级快照：`reload_config()` 之后必须立即生效。
    """
    current = get_config()
    return {
        "retention_days": current.RETENTION_DEFAULT_DAYS,
        "grace_days": current.RETENTION_GRACE_DAYS,
    }


def _is_authenticated(user_id: str | None) -> bool:
    return bool(user_id) and user_id != "anonymous"


def _resolve_rows(rows, *, kb_id: str, document_id: str) -> dict:
    """按 document → kb → tenant 顺序做字段级覆盖（先到先得）。"""
    by_scope = {(row.scope_type, row.scope_id): row for row in rows}
    resolved = _system_defaults()
    sources = {key: "system" for key in _POLICY_FIELDS}
    locked = {key: False for key in _POLICY_FIELDS}
    top_scope = "system"

    for scope, sid in (("document", document_id), ("kb", kb_id), ("tenant", "")):
        if scope != "tenant" and not sid:
            continue
        row = by_scope.get((scope, sid))
        if row is None:
            continue
        hit = False
        for key in _POLICY_FIELDS:
            value = getattr(row, key, None)
            if not locked[key] and value is not None:
                resolved[key] = value
                sources[key] = scope
                locked[key] = True
                hit = True
        if hit and top_scope == "system":
            top_scope = scope

    return {
        "scope": top_scope,
        **resolved,
        "sources": sources,
        # spec §5.2：`effective_from` 是 retention_days 的生效层级。
        "effective_from": sources["retention_days"],
    }


async def resolve_policy_in_session(
    session, tenant_id: str, *, kb_id: str, document_id: str = "",
) -> dict:
    """在调用方 session 内解析（详情投影复用同一事务，免开新连接）。"""
    tenant_id = require_tenant(tenant_id)
    rows = await retention_policy_repository.list_for_scope(
        session, tenant_id, kb_id=kb_id, document_id=document_id,
    )
    return _resolve_rows(rows, kb_id=kb_id, document_id=document_id)


async def resolve_policy(
    tenant_id: str, *, kb_id: str, document_id: str = "", session=None,
) -> dict:
    """解析给定 KB / 文档的生效策略。

    `session` 仅供详情投影传入（复用调用方事务）；默认自己开一个 session。
    """
    tenant_id = require_tenant(tenant_id)
    if session is not None:
        return await resolve_policy_in_session(
            session, tenant_id, kb_id=kb_id, document_id=document_id,
        )
    async with get_db_session() as session:
        return await resolve_policy_in_session(
            session, tenant_id, kb_id=kb_id, document_id=document_id,
        )


async def _fetch_kb_space_id(tenant_id: str, kb_id: str) -> str:
    """kb scope 的权限需要 space_id：查 knowledge_info（不存在即 404）。"""
    async with get_db_session() as session:
        row = (
            await session.execute(
                text(_KB_SPACE_SQL),
                {"kb_id": kb_id, "tenant_id": tenant_id},
            )
        ).mappings().first()
    if row is None:
        raise ResourceNotFoundError(message="知识库不存在")
    return row.get("space_id") or ""


async def _authorize_scope(
    tenant_id: str, user_id: str | None, scope_type: str, scope_id: str,
) -> tuple[str, str]:
    """校验作用域读写权限，返回解析所需的 `(kb_id, document_id)`。

    - tenant：仅租户管理员；
    - kb：查 `knowledge_info.space_id` 后走资产目录三档写权限；
    - document：读资产详情拿 `kb_id/space_id` 后走同一写权限。
    """
    if scope_type == "tenant":
        if not user_id or not await is_tenant_admin(tenant_id, user_id):
            raise PermissionDeniedError(message="仅租户管理员可管理租户级保留策略")
        return "", ""

    if scope_type == "kb":
        space_id = await _fetch_kb_space_id(tenant_id, scope_id)
        await assert_can_write(
            tenant_id, user_id, kb_id=scope_id, space_id=space_id,
        )
        return scope_id, ""

    asset = await CatalogService().get_asset(
        tenant_id, user_id, "document", scope_id, include_quality=False,
    )
    await assert_can_write(
        tenant_id, user_id,
        kb_id=asset.get("kb_id") or "",
        space_id=asset.get("space_id") or "",
    )
    return asset.get("kb_id") or "", scope_id


async def _authorize_read_scope(
    tenant_id: str, user_id: str | None, scope_type: str, scope_id: str,
) -> tuple[str, str]:
    """读策略授权（spec §9.1，**不要求写权限**）。

    - tenant：任意已认证用户可读（默认值不敏感）；
    - kb：`visible_kb_ids` 可见集合内（None = 租户管理员不限）；
    - document：`get_asset` 的读可见性（不可见/不存在一律 404）。
    """
    if scope_type == "tenant":
        if not _is_authenticated(user_id):
            raise PermissionDeniedError(message="需要登录后查看保留策略")
        return "", ""

    if scope_type == "kb":
        if not _is_authenticated(user_id):
            raise PermissionDeniedError(message="无权查看该知识库的保留策略")
        visible = await visible_kb_ids(tenant_id, user_id)
        if visible is not None and scope_id not in visible:
            raise PermissionDeniedError(message="无权查看该知识库的保留策略")
        return scope_id, ""

    asset = await CatalogService().get_asset(
        tenant_id, user_id, "document", scope_id, include_quality=False,
    )
    return asset.get("kb_id") or "", scope_id


async def get_policy(
    tenant_id: str, *, scope_type: str, scope_id: str | None,
    user_id: str | None,
) -> dict:
    """查询某作用域的**生效**策略（读授权，见 spec §9.1）。"""
    tenant_id = require_tenant(tenant_id)
    kind, normalized_id = normalize_scope(scope_type, scope_id)
    kb_id, document_id = await _authorize_read_scope(
        tenant_id, user_id, kind, normalized_id,
    )
    return await resolve_policy(
        tenant_id, kb_id=kb_id, document_id=document_id,
    )


async def set_policy(
    tenant_id: str, *, scope_type: str, scope_id: str | None, fields: dict,
    user_id: str | None, username: str | None = None, ip: str | None = None,
) -> dict:
    """写入（或清除）某级覆盖，落审计并返回更新后的生效策略。

    `fields` 中的 `None` 表示清除该级覆盖；未出现的字段保持原值。
    """
    tenant_id = require_tenant(tenant_id)
    kind, normalized_id = normalize_scope(scope_type, scope_id)
    if kind not in _SCOPE_TYPES:  # 防御：normalize_scope 已挡，保持显式
        raise InvalidParameterError(message=f"不支持的策略作用域: {scope_type}")

    # 权限先于值域归一：未授权用户拿不到「负值/超上限」这类字段错误。
    kb_id, document_id = await _authorize_scope(
        tenant_id, user_id, kind, normalized_id,
    )
    normalized = normalize_policy_fields(fields)
    if not normalized:
        raise InvalidParameterError(
            message="没有可更新的策略字段",
            details={"allowed": list(_POLICY_FIELDS)},
        )

    before_scope = {
        "tenant": ("", ""),
        "kb": (normalized_id, ""),
        "document": (kb_id, normalized_id),
    }[kind]

    async with get_db_session() as session:
        rows = await retention_policy_repository.list_for_scope(
            session, tenant_id,
            kb_id=before_scope[0], document_id=before_scope[1],
        )
        current = next(
            (
                row for row in rows
                if (row.scope_type, row.scope_id) == (kind, normalized_id)
            ),
            None,
        )
        before = {
            key: (getattr(current, key, None) if current is not None else None)
            for key in _POLICY_FIELDS
        }
        after = {**before, **normalized}
        await retention_policy_repository.upsert(
            session,
            tenant_id=tenant_id,
            scope_type=kind,
            scope_id=normalized_id,
            fields=normalized,
            updated_by=(username or user_id),
        )

    schedule_emit({
        "tenant_id": tenant_id,
        "user_id": _audit_user_id(user_id),
        "username": username,
        "ip": ip,
        "log_type": "OPERATION",
        "action": "retention_policy_update",
        "outcome": "SUCCESS",
        "service_name": "data_governance",
        "resource": (
            ResourceType.DOCUMENT.value
            if kind == "document"
            else (
                ResourceType.KNOWLEDGE_INFO.value
                if kind == "kb"
                else ResourceType.SYSTEM_CONFIG.value
            )
        ),
        "resource_id": normalized_id if kind != "tenant" else tenant_id,
        "request_params": {
            "scope_type": kind,
            "scope_id": normalized_id,
            "before": before,
            "after": after,
        },
    })

    return await resolve_policy(
        tenant_id, kb_id=kb_id, document_id=document_id,
    )
