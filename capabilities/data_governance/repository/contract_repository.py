"""data_contracts 仓储：列表聚合、版本与发布状态机。

一行 = 一版；同一 ``(tenant_id, data_source_id)`` 下最多一条 published、
最多一条 draft（部分唯一索引 ``uq_dc_published`` / ``uq_dc_draft`` 兜底）。
列表按 ``data_source_id`` 聚合：

- 有 published 取 published（优先），否则 draft，否则 retired；
- ``include_drafts=False`` 时 SQL 直接排除 draft，只回 published/retired，
  因此「只有草稿、尚未发布」的数据源不会出现在列表里；
- ``published_version`` / ``draft_version`` 用 ``LEFT JOIN LATERAL`` 求各状态
  最大版本，供列表标记 ``has_draft``；
- 数据源名称走 ``LEFT JOIN``，数据源已删除时回 NULL（合同本身仍可读）。

草稿可见性还要由服务层按管理员身份再兜一道，这里的 SQL 只保证语义正确。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import text

from jonex_core.common.exceptions import InvalidParameterError
from jonex_core.common.tenant import require_tenant

from ..models_contract import DataContract

#: 列表与 count 共用的 WHERE（count 必须与列表口径逐字一致）。
#: 空值参数必须显式 CAST：PostgreSQL 无法从 `$n IS NULL OR col = $n` 推断
#: 参数类型（真机报 `could not determine data type of parameter $2`，2026-10-19）。
_LIST_WHERE = """
    WHERE tenant_id = :tenant_id AND is_deleted = 0
      AND (CAST(:status AS VARCHAR) IS NULL OR status = CAST(:status AS VARCHAR))
      AND (CAST(:keyword AS VARCHAR) IS NULL
           OR name ILIKE CAST(:keyword AS VARCHAR) ESCAPE '\\'
           OR data_source_id ILIKE CAST(:keyword AS VARCHAR) ESCAPE '\\')
      AND (CAST(:include_drafts AS BOOLEAN) OR status <> 'draft')
"""

_LIST_CTE = (
    "WITH candidates AS (\n"
    "    SELECT DISTINCT ON (data_source_id) *\n"
    "    FROM data_governance.data_contracts\n"
    + _LIST_WHERE
    + "    ORDER BY data_source_id,\n"
    "      CASE status WHEN 'published' THEN 0 WHEN 'draft' THEN 1 ELSE 2 END,\n"
    "      version DESC\n"
    ")\n"
)

#: 列表不取 payload 大字段：只 SELECT 列表映射与聚合标记需要的列。
_LIST_SQL = _LIST_CTE + """
SELECT c.id AS contract_id, c.data_source_id, ds.name AS data_source_name,
       c.name, c.version, c.status, c.revision, c.updated_at, c.updated_by,
       pub.version AS published_version, draft.version AS draft_version
FROM candidates c
LEFT JOIN knowledge_base.knowledge_data_sources ds
  ON ds.id = c.data_source_id AND ds.tenant_id = c.tenant_id AND ds.is_deleted = 0
LEFT JOIN LATERAL (
    SELECT max(version) AS version FROM data_governance.data_contracts p
    WHERE p.tenant_id = c.tenant_id AND p.data_source_id = c.data_source_id
      AND p.status = 'published' AND p.is_deleted = 0
) pub ON TRUE
LEFT JOIN LATERAL (
    SELECT max(version) AS version FROM data_governance.data_contracts d
    WHERE d.tenant_id = c.tenant_id AND d.data_source_id = c.data_source_id
      AND d.status = 'draft' AND d.is_deleted = 0
) draft ON TRUE
ORDER BY c.updated_at DESC, c.data_source_id
LIMIT :limit OFFSET :offset
"""

_COUNT_SQL = _LIST_CTE + "SELECT count(*) FROM candidates"

#: 发布前锁行：状态与 revision 必须在这一刻校验，否则「读到 draft 之后、
#: UPDATE 之前被别人发布/修改」会重复退役或写到错误版本。
PUBLISH_LOCK_SQL = (
    "SELECT status, revision, version FROM data_governance.data_contracts "
    "WHERE tenant_id=:tenant_id AND id=:contract_id AND is_deleted=0 FOR UPDATE"
)

#: 锁内查询当前已发布的最大版本。锁行 SQL 只取 status/revision/version，
#: 这里用子查询按 contract_id 找回 data_source_id，避免多一次读。
PUBLISHED_VERSION_SQL = (
    "SELECT max(version) FROM data_governance.data_contracts "
    "WHERE tenant_id=:tenant_id AND status='published' AND is_deleted=0 "
    "AND data_source_id = ("
    "SELECT data_source_id FROM data_governance.data_contracts "
    "WHERE tenant_id=:tenant_id AND id=:contract_id AND is_deleted=0)"
)

#: 发布 = 先退役旧的 published，再把目标草稿置为 published。顺序不能反：
#: ``uq_dc_published`` 是部分唯一索引，旧版本未退役时新版本必然冲突。
PUBLISH_STEPS = (
    "UPDATE data_governance.data_contracts SET status = 'retired', "
    "revision = revision + 1, updated_at = now(), updated_by = :user_id "
    "WHERE tenant_id = :tenant_id AND status = 'published' AND is_deleted = 0 "
    "AND id <> :contract_id AND data_source_id = ("
    "SELECT data_source_id FROM data_governance.data_contracts "
    "WHERE tenant_id = :tenant_id AND id = :contract_id AND is_deleted = 0)",
    "UPDATE data_governance.data_contracts SET status = 'published', "
    "published_by = :user_id, published_at = now(), revision = revision + 1, "
    "updated_at = now(), updated_by = :user_id "
    "WHERE tenant_id = :tenant_id AND id = :contract_id AND status = 'draft' "
    "AND revision = :revision AND is_deleted = 0",
)

#: ``publish`` 的返回值：0 成功，-1 行不存在/非草稿/revision 失配，
#: -2 已存在更高版本的 published（旧草稿不可发布）。
PUBLISH_OK = 0
PUBLISH_INVALID = -1
PUBLISH_STALE_DRAFT = -2

#: 版本号不过滤 ``is_deleted``：软删版本也占号，重建时版本号不回退。
_NEXT_VERSION_SQL = (
    "SELECT COALESCE(MAX(version), 0) + 1 FROM data_governance.data_contracts "
    "WHERE tenant_id=:tenant_id AND data_source_id=:data_source_id"
)

_RETIRE_SQL = (
    "UPDATE data_governance.data_contracts SET status='retired', "
    "revision=revision+1, updated_at=now(), updated_by=:user_id "
    "WHERE tenant_id=:tenant_id AND id=:contract_id "
    "AND status='published' AND is_deleted=0"
)

_DELETE_DRAFT_SQL = (
    "UPDATE data_governance.data_contracts SET is_deleted=1, "
    "revision=revision+1, updated_at=now(), updated_by=:user_id "
    "WHERE tenant_id=:tenant_id AND id=:contract_id "
    "AND status='draft' AND is_deleted=0"
)

#: ``update_draft`` 允许的列。其余列（状态/版本/租户等）只能由状态机改写，
#: 防止调用方借动态 SET 子句越权。
_UPDATABLE_COLUMNS = frozenset({"name", "payload"})


def _like_pattern(keyword) -> str | None:
    """把关键字转成 ``%...%``；转义 ``\\`` ``%`` ``_`` 以匹配字面量。"""
    if keyword is None:
        return None
    value = str(keyword).strip()
    if not value:
        return None
    escaped = (
        value.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return f"%{escaped}%"


def _value(row, key, default=None):
    """兼容 RowMapping / dict / ORM 对象取列。"""
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key, default)


def _iso(value):
    if value is None or isinstance(value, str):
        return value
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else value


def to_summary(row, include_drafts: bool = False) -> dict:
    """列表行。``include_drafts=False`` 时不泄露草稿存在性。"""
    version = _value(row, "version")
    status = _value(row, "status")
    draft_version = _value(row, "draft_version")
    published_version = _value(row, "published_version")
    if include_drafts:
        # 版本/详情行没有 lateral 列，用行自身的 version 兜底。
        if draft_version is None and status == "draft":
            draft_version = version
        if published_version is None and status == "published":
            published_version = version
    else:
        draft_version = None
    contract_id = _value(row, "contract_id")
    if contract_id is None:
        # 列表 SQL 的别名是 contract_id；raw 版本行只有 id。
        contract_id = _value(row, "id")
    return {
        "contract_id": contract_id,
        "data_source_id": _value(row, "data_source_id"),
        "data_source_name": _value(row, "data_source_name"),
        "name": _value(row, "name"),
        "version": version,
        "status": status,
        "revision": _value(row, "revision", 1),
        "published_version": published_version,
        "draft_version": draft_version,
        "has_draft": include_drafts and draft_version is not None,
        "updated_at": _iso(_value(row, "updated_at")),
        "updated_by": _value(row, "updated_by"),
    }


def to_detail(row) -> dict:
    """详情 = 列表行 + 完整 payload。"""
    detail = to_summary(row, include_drafts=True)
    detail["payload"] = _value(row, "payload") or {}
    return detail


async def next_version(session, *, tenant_id, data_source_id) -> int:
    """下一个版本号；无历史行时 SQL 的 COALESCE 直接回 1。

    不过滤 ``is_deleted``：软删版本也占号，避免删除草稿后版本号回退。
    """
    value = (await session.execute(
        text(_NEXT_VERSION_SQL),
        {
            "tenant_id": require_tenant(tenant_id),
            "data_source_id": data_source_id,
        },
    )).scalar()
    return int(value or 1)


async def create_draft(session, *, tenant_id, data_source_id, version, name, payload,
                       created_by) -> DataContract:
    now = datetime.now(timezone.utc)
    row = DataContract(
        id=uuid.uuid4().hex, tenant_id=require_tenant(tenant_id),
        data_source_id=data_source_id, version=version, name=name,
        payload=payload if payload is not None else {},
        status="draft", revision=1, created_by=created_by,
        updated_by=created_by, is_deleted=0,
        # 模型列是 TIMESTAMPTZ；显式给 UTC，不能依赖 DB 默认值。
        created_at=now, updated_at=now,
    )
    session.add(row)
    await session.flush()
    return row


async def get_by_id(session, *, tenant_id, contract_id, include_deleted=False):
    stmt = (
        "SELECT * FROM data_governance.data_contracts "
        "WHERE tenant_id=:tenant_id AND id=:contract_id"
    )
    if not include_deleted:
        stmt += " AND is_deleted=0"
    return (await session.execute(text(stmt), {
        "tenant_id": require_tenant(tenant_id), "contract_id": contract_id,
    })).mappings().first()


async def get_version(session, *, tenant_id, data_source_id, version):
    return (await session.execute(
        text(
            "SELECT * FROM data_governance.data_contracts "
            "WHERE tenant_id=:tenant_id AND data_source_id=:data_source_id "
            "AND version=:version AND is_deleted=0"
        ),
        {
            "tenant_id": require_tenant(tenant_id),
            "data_source_id": data_source_id, "version": version,
        },
    )).mappings().first()


async def list_page(session, *, tenant_id, status=None, keyword=None,
                    include_drafts: bool = False, limit: int = 20,
                    offset: int = 0) -> list[dict]:
    """分页列表（每个数据源一行）。

    ``status`` 过滤发生在 candidates 聚合**内**，属于版本级筛选：例如同一
    数据源同时有 published 与 draft 时，传 ``status='draft'`` 会让代表行变成
    草稿；不传 ``status`` 且 ``include_drafts=True`` 时，代表行仍按 published
    优先，草稿只体现在 ``draft_version`` / ``has_draft`` 上。
    """
    rows = (await session.execute(text(_LIST_SQL), {
        "tenant_id": require_tenant(tenant_id),
        "status": status,
        "keyword": _like_pattern(keyword),
        "include_drafts": bool(include_drafts),
        "limit": limit,
        "offset": offset,
    })).mappings()
    return [dict(row) for row in rows]


async def count(session, *, tenant_id, status=None, keyword=None,
                include_drafts: bool = False) -> int:
    """与 ``list_page`` 同一 WHERE 的计数。

    ``include_drafts`` 默认与 ``list_page`` 对齐为 ``False``，调用方必须传与
    列表相同的值，否则 total 与 items 口径不一致。
    """
    value = (await session.execute(text(_COUNT_SQL), {
        "tenant_id": require_tenant(tenant_id),
        "status": status,
        "keyword": _like_pattern(keyword),
        "include_drafts": bool(include_drafts),
    })).scalar()
    return int(value or 0)


async def update_draft(session, *, tenant_id, contract_id, revision, fields) -> int:
    values = dict(fields or {})
    updated_by = values.pop("updated_by", None)
    unknown = set(values) - _UPDATABLE_COLUMNS
    if unknown:
        raise InvalidParameterError(
            message="不允许更新的字段",
            details={"fields": sorted(unknown)},
        )
    assignments: list[str] = []
    params: dict = {
        "tenant_id": require_tenant(tenant_id),
        "contract_id": contract_id,
        "revision": revision,
        "updated_by": updated_by,
    }
    if "name" in values:
        assignments.append("name = :name")
        params["name"] = values["name"]
    if "payload" in values:
        # 显式经 text::jsonb：asyncpg 的 jsonb codec 只接受字符串，直接绑 dict 会炸。
        assignments.append("payload = CAST(:payload AS text)::jsonb")
        params["payload"] = json.dumps(
            values["payload"], ensure_ascii=False, separators=(",", ":"),
        )
    sets = ", ".join(assignments)
    result = await session.execute(
        text(
            "UPDATE data_governance.data_contracts SET "
            + (sets + ", " if sets else "")
            + "revision = revision + 1, updated_at = now(), updated_by = :updated_by "
            "WHERE tenant_id=:tenant_id AND id=:contract_id AND status='draft' "
            "AND is_deleted=0 AND revision=:revision"
        ),
        params,
    )
    return result.rowcount


async def publish(session, *, tenant_id, contract_id, revision, user_id) -> int:
    """发布草稿。

    先 ``FOR UPDATE`` 锁行并在锁内校验 ``status='draft' AND revision=:revision``；
    任一不满足返回 ``PUBLISH_INVALID``（-1），**不执行**任何写操作，避免 revision
    失配时把旧 published 误退役。

    随后查当前已发布的最大版本；目标草稿版本更低时返回
    ``PUBLISH_STALE_DRAFT``（-2），同样不写。否则依次退役旧 published、把目标
    置为 published，成功返回 ``PUBLISH_OK``（0）。
    """
    tid = require_tenant(tenant_id)
    row = (await session.execute(text(PUBLISH_LOCK_SQL), {
        "tenant_id": tid, "contract_id": contract_id,
    })).mappings().first()
    if row is None or revision is None:
        return PUBLISH_INVALID
    if row["status"] != "draft" or int(row["revision"]) != int(revision):
        return PUBLISH_INVALID
    published_version = (await session.execute(text(PUBLISHED_VERSION_SQL), {
        "tenant_id": tid, "contract_id": contract_id,
    })).scalar()
    draft_version = _value(row, "version")
    # 旧草稿不得覆盖更新的发布版（例如先克隆了 v3、随后 v4 已发布）。
    if (
        published_version is not None
        and draft_version is not None
        and int(published_version) > int(draft_version)
    ):
        return PUBLISH_STALE_DRAFT
    params = {
        "tenant_id": tid, "contract_id": contract_id,
        "revision": revision, "user_id": user_id,
    }
    await session.execute(text(PUBLISH_STEPS[0]), params)
    result = await session.execute(text(PUBLISH_STEPS[1]), params)
    # 锁内目标行仍可能因并发删除/改写导致更新 0 行：如实回 -1。
    return PUBLISH_OK if result.rowcount == 1 else PUBLISH_INVALID


async def retire(session, *, tenant_id, contract_id, user_id) -> int:
    result = await session.execute(
        text(_RETIRE_SQL),
        {
            "tenant_id": require_tenant(tenant_id),
            "contract_id": contract_id, "user_id": user_id,
        },
    )
    return result.rowcount


async def delete_draft(session, *, tenant_id, contract_id, user_id) -> int:
    result = await session.execute(
        text(_DELETE_DRAFT_SQL),
        {
            "tenant_id": require_tenant(tenant_id),
            "contract_id": contract_id, "user_id": user_id,
        },
    )
    return result.rowcount


async def list_versions(session, *, tenant_id, data_source_id) -> list[dict]:
    return [dict(r) for r in (await session.execute(
        text(
            "SELECT * FROM data_governance.data_contracts "
            "WHERE tenant_id=:tenant_id AND data_source_id=:data_source_id "
            "AND is_deleted=0 ORDER BY version DESC"
        ),
        {
            "tenant_id": require_tenant(tenant_id),
            "data_source_id": data_source_id,
        },
    )).mappings()]
