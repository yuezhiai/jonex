"""classification_level / classification_category / classification_default 读写。"""
from __future__ import annotations

import re
import uuid

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.exceptions import InvalidParameterError
from jonex_core.common.tenant import require_tenant

from ..models import ClassificationCategory, ClassificationDefault, ClassificationLevel

DEFAULT_LEVELS = (
    ("public", "公开", 1),
    ("internal", "内部", 2),
    ("confidential", "机密", 3),
)
_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
MAX_CATEGORIES = 20

# 读侧筛选哨兵（`sensitivity=unclassified` 表示「未分级」）。等级/分类 code
# 是同一命名空间，保留给筛选语义，不允许真实配置项占用（等级与分类同规则）。
RESERVED_CODES = {"unclassified"}

_LEVEL_FIELDS = ("name", "rank", "is_active")
_CATEGORY_FIELDS = ("name", "is_active")
_DEFAULT_FIELDS = ("level_code", "categories")


def _filter_fields(fields: dict, allowed: tuple[str, ...]) -> dict:
    """只保留白名单字段；非 dict 直接拒绝。"""
    if not isinstance(fields, dict):
        raise InvalidParameterError(message="字段必须是对象")
    return {key: value for key, value in fields.items() if key in allowed}


def normalize_code(value) -> str:
    if not isinstance(value, str):
        raise InvalidParameterError(
            message="code 必须是字符串",
            details={"value": value},
        )
    code = value.strip().lower()
    if not _CODE_RE.match(code):
        raise InvalidParameterError(
            message="分级 code 必须以小写字母开头，只含小写字母/数字/下划线，长度 2..32",
            details={"value": value},
        )
    if code in RESERVED_CODES:
        raise InvalidParameterError(
            message="code 是保留字，不能用作等级或分类",
            details={"value": value, "reserved": sorted(RESERVED_CODES)},
        )
    return code


def normalize_name(value) -> str:
    name = str(value or "").strip()
    if not 1 <= len(name) <= 64:
        raise InvalidParameterError(message="名称长度必须为 1..64")
    return name


def normalize_rank(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1000:
        raise InvalidParameterError(message="rank 必须是 1..1000 的整数")
    return value


def normalize_is_active(value) -> bool:
    if not isinstance(value, bool):
        raise InvalidParameterError(message="is_active 必须是布尔值")
    return value


def normalize_categories(value) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise InvalidParameterError(message="分类必须是字符串数组")
    out: list[str] = []
    for item in value:
        code = normalize_code(item)
        if code not in out:
            out.append(code)
    if len(out) > MAX_CATEGORIES:
        raise InvalidParameterError(message=f"分类最多 {MAX_CATEGORIES} 个")
    return out


async def ensure_default_levels(session: AsyncSession, tenant_id: str) -> None:
    tid = require_tenant(tenant_id)
    stmt = pg_insert(ClassificationLevel).values([
        {"id": uuid.uuid4().hex, "tenant_id": tid, "code": code,
         "name": name, "rank": rank, "is_active": True}
        for code, name, rank in DEFAULT_LEVELS
    ]).on_conflict_do_nothing(index_elements=["tenant_id", "code"])
    await session.execute(stmt)


async def list_levels(session: AsyncSession, tenant_id: str) -> list[ClassificationLevel]:
    tid = require_tenant(tenant_id)
    rows = await session.execute(
        select(ClassificationLevel)
        .where(ClassificationLevel.tenant_id == tid)
        .order_by(ClassificationLevel.rank.asc(), ClassificationLevel.code.asc())
    )
    return list(rows.scalars().all())


async def list_categories(session: AsyncSession, tenant_id: str) -> list[ClassificationCategory]:
    tid = require_tenant(tenant_id)
    rows = await session.execute(
        select(ClassificationCategory)
        .where(ClassificationCategory.tenant_id == tid)
        .order_by(ClassificationCategory.code.asc())
    )
    return list(rows.scalars().all())


async def get_level(session, tenant_id: str, code: str) -> ClassificationLevel | None:
    tid = require_tenant(tenant_id)
    result = await session.execute(
        select(ClassificationLevel).where(
            ClassificationLevel.tenant_id == tid,
            ClassificationLevel.code == normalize_code(code),
        )
    )
    return result.scalars().first()


async def get_category(session, tenant_id: str, code: str) -> ClassificationCategory | None:
    tid = require_tenant(tenant_id)
    result = await session.execute(
        select(ClassificationCategory).where(
            ClassificationCategory.tenant_id == tid,
            ClassificationCategory.code == normalize_code(code),
        )
    )
    return result.scalars().first()


async def upsert_level(session, *, tenant_id: str, code: str, fields: dict) -> ClassificationLevel:
    """创建或更新分级。

    并发：SELECT-then-INSERT 在并发创建同一 code 时可能撞唯一约束；
    当前由单管理员配置场景接受该限制（不改为 ON CONFLICT）。
    """
    tid = require_tenant(tenant_id)
    code = normalize_code(code)
    raw_fields = fields
    fields = _filter_fields(raw_fields, _LEVEL_FIELDS)
    if raw_fields and not fields:
        raise InvalidParameterError(message="没有可更新的字段")
    row = await get_level(session, tid, code)
    if row is not None and not fields:
        raise InvalidParameterError(message="没有可更新的字段")
    if row is None:
        if "name" not in fields or "rank" not in fields:
            raise InvalidParameterError(message="创建等级必须提供 name 与 rank")
        row = ClassificationLevel(
            id=uuid.uuid4().hex, tenant_id=tid, code=code,
            name=normalize_name(fields["name"]),
            rank=normalize_rank(fields["rank"]),
        )
        session.add(row)
    if "name" in fields:
        row.name = normalize_name(fields["name"])
    if "rank" in fields:
        row.rank = normalize_rank(fields["rank"])
    if "is_active" in fields:
        row.is_active = normalize_is_active(fields["is_active"])
    await session.flush()
    return row


async def upsert_category(session, *, tenant_id: str, code: str, fields: dict) -> ClassificationCategory:
    """创建或更新分类。

    并发：SELECT-then-INSERT 在并发创建同一 code 时可能撞唯一约束；
    当前由单管理员配置场景接受该限制（不改为 ON CONFLICT）。
    """
    tid = require_tenant(tenant_id)
    code = normalize_code(code)
    raw_fields = fields
    fields = _filter_fields(raw_fields, _CATEGORY_FIELDS)
    if raw_fields and not fields:
        raise InvalidParameterError(message="没有可更新的字段")
    row = await get_category(session, tid, code)
    if row is not None and not fields:
        raise InvalidParameterError(message="没有可更新的字段")
    if row is None:
        if "name" not in fields:
            raise InvalidParameterError(message="创建分类必须提供 name")
        row = ClassificationCategory(
            id=uuid.uuid4().hex, tenant_id=tid, code=code,
            name=normalize_name(fields["name"]),
        )
        session.add(row)
    if "name" in fields:
        row.name = normalize_name(fields["name"])
    if "is_active" in fields:
        row.is_active = normalize_is_active(fields["is_active"])
    await session.flush()
    return row


async def get_default(session, tenant_id: str, scope_type: str, scope_id: str):
    tid = require_tenant(tenant_id)
    result = await session.execute(
        select(ClassificationDefault).where(
            ClassificationDefault.tenant_id == tid,
            ClassificationDefault.scope_type == scope_type,
            ClassificationDefault.scope_id == scope_id,
        )
    )
    return result.scalars().first()


async def upsert_default(session, *, tenant_id: str, scope_type: str,
                         scope_id: str, fields: dict, updated_by: str | None):
    """创建或更新默认分级分类；NULL 表示清空该字段。

    并发：SELECT-then-INSERT 在并发创建同一作用域时可能撞唯一约束；
    当前由单管理员配置场景接受该限制（不改为 ON CONFLICT）。
    """
    tid = require_tenant(tenant_id)
    raw_fields = fields
    fields = _filter_fields(raw_fields, _DEFAULT_FIELDS)
    if raw_fields and not fields:
        raise InvalidParameterError(message="没有可更新的字段")
    row = await get_default(session, tid, scope_type, scope_id)
    if row is None:
        row = ClassificationDefault(
            id=uuid.uuid4().hex, tenant_id=tid,
            scope_type=scope_type, scope_id=scope_id,
        )
        session.add(row)
    if "level_code" in fields:
        value = fields["level_code"]
        row.level_code = None if value is None else normalize_code(value)
    if "categories" in fields:
        row.categories = normalize_categories(fields["categories"])
    row.updated_by = updated_by
    await session.flush()
    return row


async def get_kb_space_id(session, tenant_id: str, kb_id: str) -> str | None:
    tid = require_tenant(tenant_id)
    row = (await session.execute(
        text(
            "SELECT space_id FROM knowledge_base.knowledge_info "
            "WHERE id=:kb AND tenant_id=:t AND is_deleted=0"
        ),
        {"kb": kb_id, "t": tid},
    )).first()
    return row[0] if row else None
