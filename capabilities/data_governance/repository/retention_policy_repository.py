"""retention_policy 读写。

语义：``retention_days``/``grace_days`` 为 NULL 表示继承上级策略；
``retention_days=0`` 表示不自动到期，``grace_days=0`` 表示无宽限。

并发：``upsert`` 依赖唯一约束 ``uq_retention_scope`` 的 ``ON CONFLICT``，
不做 SELECT-then-INSERT，避免并发下重复插入。
"""
from __future__ import annotations

import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.exceptions import InvalidParameterError
from jonex_core.common.tenant import require_tenant

from ..models import RetentionPolicy

_SCOPE_TYPES = frozenset({"tenant", "kb", "document"})
#: 策略字段的唯一来源；service/capability 必须引用这里，不要各自复制一份。
RETENTION_FIELDS = ("retention_days", "grace_days")
_POLICY_FIELDS = RETENTION_FIELDS
MAX_RETENTION_DAYS = 36500
_MAX_SCOPE_ID_LEN = 64
_MAX_UPDATED_BY_LEN = 128


def normalize_scope(scope_type: str, scope_id: str | None) -> tuple[str, str]:
    """归一作用域：tenant 的 scope_id 恒为空串，其余必须非空且不超过 64 字符。"""
    kind = scope_type.strip() if isinstance(scope_type, str) else ""
    if kind not in _SCOPE_TYPES:
        raise InvalidParameterError(
            message=f"不支持的策略作用域: {scope_type}",
            details={"allowed": sorted(_SCOPE_TYPES)},
        )
    normalized_id = "" if scope_id is None else str(scope_id).strip()
    if kind == "tenant":
        return kind, ""
    if not normalized_id:
        raise InvalidParameterError(message=f"{kind} 作用域必须提供 scope_id")
    if len(normalized_id) > _MAX_SCOPE_ID_LEN:
        raise InvalidParameterError(
            message=f"scope_id 超长（>{_MAX_SCOPE_ID_LEN}）",
        )
    return kind, normalized_id


def normalize_policy_fields(fields: dict) -> dict:
    """只保留 retention_days/grace_days；None 透传，值必须是非负整数且 ≤36500。"""
    if not isinstance(fields, dict):
        raise InvalidParameterError(message="策略字段必须是对象")
    out: dict = {}
    for key in _POLICY_FIELDS:
        if key not in fields:
            continue
        value = fields[key]
        if value is None:
            out[key] = None
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidParameterError(
                message=f"{key} 必须是整数", details={"field": key},
            )
        if value < 0 or value > MAX_RETENTION_DAYS:
            raise InvalidParameterError(
                message=f"{key} 必须在 0..{MAX_RETENTION_DAYS} 之间",
                details={"field": key},
            )
        out[key] = value
    return out


async def upsert(
    session: AsyncSession, *, tenant_id: str, scope_type: str,
    scope_id: str | None, fields: dict, updated_by: str | None,
) -> RetentionPolicy:
    """按 (tenant_id, scope_type, scope_id) 幂等写入，并发安全。

    ``fields={}`` 合法：创建空行（全部继承）或仅更新 ``updated_by``。
    非空 ``fields`` 归一后为空说明全是未知键（拼写错误），直接拒绝。
    ``updated_by=None`` 会覆盖为 NULL（记录“本次未知操作人”）。
    """
    tid = require_tenant(tenant_id)
    kind, normalized_id = normalize_scope(scope_type, scope_id)
    normalized = normalize_policy_fields(fields)
    if fields and not normalized:
        raise InvalidParameterError(
            message="没有可更新的策略字段",
            details={"allowed": list(_POLICY_FIELDS)},
        )
    if updated_by is not None:
        updated_by = str(updated_by).strip() or None
        if updated_by is not None and len(updated_by) > _MAX_UPDATED_BY_LEN:
            raise InvalidParameterError(
                message=f"updated_by 超长（>{_MAX_UPDATED_BY_LEN}）",
            )

    values = {
        "id": uuid.uuid4().hex,
        "tenant_id": tid,
        "scope_type": kind,
        "scope_id": normalized_id,
        "updated_by": updated_by,
    }
    values.update(normalized)

    statement = pg_insert(RetentionPolicy).values(**values)
    set_ = {
        key: getattr(statement.excluded, key) for key in normalized
    }
    set_["updated_by"] = statement.excluded.updated_by
    set_["updated_at"] = func.now()
    statement = statement.on_conflict_do_update(
        constraint="uq_retention_scope", set_=set_,
    ).returning(RetentionPolicy)

    result = await session.execute(statement)
    return result.scalars().first()


async def list_for_tenant(
    session: AsyncSession, tenant_id: str,
) -> list[RetentionPolicy]:
    tid = require_tenant(tenant_id)
    rows = (
        await session.execute(
            select(RetentionPolicy)
            .where(RetentionPolicy.tenant_id == tid)
            .order_by(RetentionPolicy.scope_type, RetentionPolicy.scope_id)
        )
    ).scalars().all()
    return list(rows)


async def list_for_scope(
    session: AsyncSession, tenant_id: str, *,
    kb_id: str, document_id: str = "",
) -> list[RetentionPolicy]:
    """只取解析所需的三级行：tenant + 指定 kb + 指定 document。

    策略解析在目录详情读路径上，`list_for_tenant` 的全租户扫描会随策略行数
    线性变慢；这里把过滤下推到 SQL，最多返回 3 行。空 `kb_id`/`document_id`
    表示该级不参与解析（不生成对应条件）。
    """
    tid = require_tenant(tenant_id)
    scope_filters = [RetentionPolicy.scope_type == "tenant"]
    if kb_id:
        scope_filters.append(
            (RetentionPolicy.scope_type == "kb")
            & (RetentionPolicy.scope_id == kb_id)
        )
    if document_id:
        scope_filters.append(
            (RetentionPolicy.scope_type == "document")
            & (RetentionPolicy.scope_id == document_id)
        )
    rows = (
        await session.execute(
            select(RetentionPolicy)
            .where(RetentionPolicy.tenant_id == tid, or_(*scope_filters))
            .order_by(RetentionPolicy.scope_type, RetentionPolicy.scope_id)
        )
    ).scalars().all()
    return list(rows)
