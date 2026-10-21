"""分级词表、默认值与资产校验。"""
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

from ..repository import classification_repository as repo
from .permissions import assert_can_write, visible_kb_ids

_SCOPES = {"tenant", "kb"}


def _audit_user_id(user_id) -> int | None:
    if user_id and str(user_id).isdigit():
        return int(user_id)
    return None


async def ensure_default_levels(tenant_id: str) -> None:
    """幂等懒播种：每次读/校验都会发一次 ON CONFLICT DO NOTHING + commit。"""
    async with get_db_session() as session:
        await repo.ensure_default_levels(session, tenant_id)
        await session.commit()


def _is_authenticated(user_id) -> bool:
    """与保留策略读路径同口径：空值与 `anonymous` 视为未登录。"""
    return bool(user_id) and user_id != "anonymous"


async def _require_admin(tenant_id: str, user_id: str | None) -> None:
    if not user_id or not await is_tenant_admin(tenant_id, user_id):
        raise PermissionDeniedError(message="仅租户管理员可维护分级配置")


async def list_config(tenant_id: str, user_id: str | None) -> dict:
    """读词表：任意已登录租户用户可读（spec §5.3）。"""
    tenant_id = require_tenant(tenant_id)
    if not _is_authenticated(user_id):
        raise PermissionDeniedError(message="需要登录后查看分级配置")
    await ensure_default_levels(tenant_id)
    async with get_db_session() as session:
        levels = await repo.list_levels(session, tenant_id)
        categories = await repo.list_categories(session, tenant_id)
        # 在 session 内转 dict：会话关闭后 ORM 行可能已过期。
        return {
            "levels": [row.to_dict() for row in levels],
            "categories": [row.to_dict() for row in categories],
        }


async def upsert_level(tenant_id, user_id, *, code, fields, username=None, ip=None):
    tenant_id = require_tenant(tenant_id)
    await _require_admin(tenant_id, user_id)
    before = None
    async with get_db_session() as session:
        existing = await repo.get_level(session, tenant_id, code)
        before = existing.to_dict() if existing else None
        row = await repo.upsert_level(session, tenant_id=tenant_id, code=code, fields=fields)
        after = row.to_dict()
        await session.commit()
    schedule_emit({
        "tenant_id": tenant_id, "user_id": _audit_user_id(user_id),
        "username": username, "ip": ip, "log_type": "OPERATION",
        "action": "classification_config_update", "outcome": "SUCCESS",
        "service_name": "data_governance",
        "resource": ResourceType.SYSTEM_CONFIG.value,
        "resource_id": f"level:{after['code']}",
        "request_params": {"kind": "level", "code": after["code"],
                           "before": before, "after": after},
    })
    return after


async def upsert_category(tenant_id, user_id, *, code, fields, username=None, ip=None):
    tenant_id = require_tenant(tenant_id)
    await _require_admin(tenant_id, user_id)
    before = None
    async with get_db_session() as session:
        existing = await repo.get_category(session, tenant_id, code)
        before = existing.to_dict() if existing else None
        row = await repo.upsert_category(session, tenant_id=tenant_id, code=code, fields=fields)
        after = row.to_dict()
        await session.commit()
    schedule_emit({
        "tenant_id": tenant_id, "user_id": _audit_user_id(user_id),
        "username": username, "ip": ip, "log_type": "OPERATION",
        "action": "classification_config_update", "outcome": "SUCCESS",
        "service_name": "data_governance",
        "resource": ResourceType.SYSTEM_CONFIG.value,
        "resource_id": f"category:{after['code']}",
        "request_params": {"kind": "category", "code": after["code"],
                           "before": before, "after": after},
    })
    return after


async def validate_assignment(tenant_id: str, fields: dict) -> None:
    """校验资产/默认值里的等级与分类；fields 只含调用方显式给的键。"""
    if not isinstance(fields, dict):
        raise InvalidParameterError(message="字段必须是对象")
    tenant_id = require_tenant(tenant_id)
    await ensure_default_levels(tenant_id)
    async with get_db_session() as session:
        if "sensitivity" in fields and fields["sensitivity"] is not None:
            row = await repo.get_level(session, tenant_id, fields["sensitivity"])
            if row is None:
                raise InvalidParameterError(message=f"等级不存在：{fields['sensitivity']}")
            if not row.is_active:
                raise InvalidParameterError(message=f"等级已停用：{fields['sensitivity']}")
        if "categories" in fields and fields["categories"] is not None:
            categories = fields["categories"]
            if not isinstance(categories, list):
                raise InvalidParameterError(message="分类必须是字符串数组")
            for code in categories:
                if not isinstance(code, str):
                    raise InvalidParameterError(message="分类必须是字符串数组")
                row = await repo.get_category(session, tenant_id, code)
                if row is None:
                    raise InvalidParameterError(message=f"分类不存在：{code}")
                if not row.is_active:
                    raise InvalidParameterError(message=f"分类已停用：{code}")


def _normalize_scope(scope_type: str, scope_id: str | None) -> tuple[str, str]:
    """归一作用域：tenant 的 scope_id 恒为空串（对应 DB CHECK 约束），kb 必须非空。"""
    # 先类型后成员：list/dict 不可哈希，直接 `in set` 会抛 TypeError（500）。
    if not isinstance(scope_type, str) or scope_type not in _SCOPES:
        raise InvalidParameterError(message="scope_type 只能是 tenant/kb")
    if scope_id is not None and not isinstance(scope_id, str):
        raise InvalidParameterError(message="scope_id 必须是字符串")
    sid = (scope_id or "").strip()
    if scope_type == "tenant":
        # 刻意忽略：tenant 级没有下级 id，多余 scope_id 归一为空串（不是 400）。
        sid = ""
    elif not sid:
        raise InvalidParameterError(message="kb 作用域必须提供 scope_id")
    if len(sid) > 64:
        raise InvalidParameterError(message="scope_id 超长（>64）")
    return scope_type, sid


def _empty_default(scope_type: str, scope_id: str) -> dict:
    """未设置时的空壳：`None` 表示该级无覆盖（对应 DB 列可空 + categories JSON 数组 CHECK）。"""
    return {"scope_type": scope_type, "scope_id": scope_id,
            "level_code": None, "categories": None,
            "updated_by": None, "updated_at": None}


async def get_default(tenant_id, user_id, *, scope_type, scope_id=None) -> dict:
    """读某作用域的默认分级分类；explicit `None` 与 `[]` 原样返回，不做继承解析。"""
    tenant_id = require_tenant(tenant_id)
    scope_type, sid = _normalize_scope(scope_type, scope_id)
    if scope_type == "tenant":
        # 与 retention `_authorize_read_scope` 同口径：默认值也需要登录态。
        if not user_id or user_id == "anonymous":
            raise PermissionDeniedError(message="需要登录后查看分级分类默认值")
    else:
        # 租户管理员（visible=None）也必须先确认 KB 存在，不能凭越权 id 拿空壳。
        async with get_db_session() as session:
            space_id = await repo.get_kb_space_id(session, tenant_id, sid)
        if space_id is None:
            raise ResourceNotFoundError(message="知识库不存在")
        visible = await visible_kb_ids(tenant_id, user_id)
        if visible is not None and sid not in visible:
            raise ResourceNotFoundError(message="知识库不存在")
    async with get_db_session() as session:
        row = await repo.get_default(session, tenant_id, scope_type, sid)
        if row is None:
            return _empty_default(scope_type, sid)
        return row.to_dict() | {
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }


async def set_default(tenant_id, user_id, *, scope_type, scope_id=None,
                      fields, username=None, ip=None) -> dict:
    """写某级默认值：`level_code=None`/`categories=None` 清除该级覆盖，`[]` 是显式无分类。

    NULL 与 JSONB 数组的区分由 DB `ck_classification_default_categories` 兜底；
    tenant 的 scope_id 恒为空串由 `ck_classification_default_tenant_scope` 兜底。
    存在性检查与写入分两次事务：单管理员配置场景可接受（`repo.upsert_default`
    的 SELECT-then-INSERT 并发限制见其 docstring）。
    """
    tenant_id = require_tenant(tenant_id)
    scope_type, sid = _normalize_scope(scope_type, scope_id)
    # 权限先于值域：未授权用户拿不到「空字段/类型错误」这类信号。
    if scope_type == "tenant":
        await _require_admin(tenant_id, user_id)
    else:
        async with get_db_session() as session:
            space_id = await repo.get_kb_space_id(session, tenant_id, sid)
        if space_id is None:
            raise ResourceNotFoundError(message="知识库不存在")
        await assert_can_write(tenant_id, user_id, kb_id=sid, space_id=space_id)

    if not isinstance(fields, dict):
        raise InvalidParameterError(message="字段必须是对象")
    if not fields:
        raise InvalidParameterError(message="没有可更新的字段")

    validation_fields = {}
    if "level_code" in fields:
        validation_fields["sensitivity"] = fields["level_code"]
    if "categories" in fields:
        validation_fields["categories"] = fields["categories"]
    await validate_assignment(tenant_id, validation_fields)

    async with get_db_session() as session:
        before_row = await repo.get_default(session, tenant_id, scope_type, sid)
        before = before_row.to_dict() | {
            "updated_at": (
                before_row.updated_at.isoformat() if before_row.updated_at else None
            ),
        } if before_row else None
        row = await repo.upsert_default(
            session, tenant_id=tenant_id, scope_type=scope_type, scope_id=sid,
            fields=fields, updated_by=user_id,
        )
        after = row.to_dict() | {
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
        await session.commit()
    schedule_emit({
        "tenant_id": tenant_id, "user_id": _audit_user_id(user_id),
        "username": username, "ip": ip, "log_type": "OPERATION",
        "action": "classification_default_update", "outcome": "SUCCESS",
        "service_name": "data_governance",
        "resource": ResourceType.SYSTEM_CONFIG.value,
        "resource_id": f"{scope_type}:{sid}",
        "request_params": {"scope_type": scope_type, "scope_id": sid,
                           "before": before, "after": after},
    })
    return after


async def assert_filter_values(tenant_id: str, *, level: str | None, category: str | None) -> None:
    """只校验存在性，停用项允许用于历史筛选。"""
    tenant_id = require_tenant(tenant_id)
    await ensure_default_levels(tenant_id)
    async with get_db_session() as session:
        if level and level != "unclassified":
            row = await repo.get_level(session, tenant_id, level)
            if row is None:
                raise InvalidParameterError(message=f"未知等级：{level}")
        if category:
            row = await repo.get_category(session, tenant_id, category)
            if row is None:
                raise InvalidParameterError(message=f"未知分类：{category}")
