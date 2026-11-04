"""standard_assets 仓储：列表聚合、版本、状态机。

一行 = 一版；同一 ``(tenant_id, kind, code)`` 下最多一条 published（部分唯一
索引 ``uq_std_asset_published`` 兜底）。列表按 code 聚合：

- 有 published 取 published（优先），否则 draft，否则 retired；
- ``include_drafts=False`` 时 SQL 直接排除 draft，只回 published/retired，
  因此「只有草稿、尚未发布」的 code 不会出现在列表里；
- ``draft_version`` / ``published_version`` 用 ``LEFT JOIN LATERAL`` 求各状态
  最大版本，供列表标记 ``has_draft``。

草稿可见性还要由服务层按管理员身份再兜一道，这里的 SQL 只保证语义正确。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import text

from jonex_core.common.exceptions import InvalidParameterError
from jonex_core.common.tenant import require_tenant

from ..models_standard import StandardAsset

#: 列表与 count 共用的 WHERE（count 必须与列表口径逐字一致）。
#: 空值参数必须显式 CAST：PostgreSQL 无法从 `$n IS NULL OR col = $n` 推断
#: 参数类型（真机报 `could not determine data type of parameter $2`，2026-10-19）。
_LIST_WHERE = """
    WHERE tenant_id = :tenant_id AND is_deleted = 0
      AND (CAST(:kind AS VARCHAR) IS NULL OR kind = CAST(:kind AS VARCHAR))
      AND (CAST(:status AS VARCHAR) IS NULL OR status = CAST(:status AS VARCHAR))
      AND (CAST(:keyword AS VARCHAR) IS NULL
           OR code ILIKE CAST(:keyword AS VARCHAR) ESCAPE '\\'
           OR name ILIKE CAST(:keyword AS VARCHAR) ESCAPE '\\')
      AND (CAST(:include_drafts AS BOOLEAN) OR status <> 'draft')
"""

_LIST_CTE = (
    "WITH candidates AS (\n"
    "    SELECT DISTINCT ON (kind, code) *\n"
    "    FROM data_governance.standard_assets\n"
    + _LIST_WHERE
    + "    ORDER BY kind, code,\n"
    "      CASE status WHEN 'published' THEN 0 WHEN 'draft' THEN 1 ELSE 2 END,\n"
    "      version DESC\n"
    ")\n"
)

#: 列表不取 payload 大字段：只 SELECT 列表映射需要的列。
_LIST_COLUMNS = (
    "c.id, c.tenant_id, c.kind, c.code, c.version, c.name, c.status, "
    "c.revision, c.created_by, c.updated_by, c.published_by, c.published_at, "
    "c.is_deleted, c.created_at, c.updated_at"
)

_LIST_SQL = _LIST_CTE + """
SELECT """ + _LIST_COLUMNS + """,
       pub.version AS published_version,
       draft.version AS draft_version
FROM candidates c
LEFT JOIN LATERAL (
    SELECT max(version) AS version
    FROM data_governance.standard_assets p
    WHERE p.tenant_id = c.tenant_id AND p.kind = c.kind AND p.code = c.code
      AND p.status = 'published' AND p.is_deleted = 0
) pub ON TRUE
LEFT JOIN LATERAL (
    SELECT max(version) AS version
    FROM data_governance.standard_assets d
    WHERE d.tenant_id = c.tenant_id AND d.kind = c.kind AND d.code = c.code
      AND d.status = 'draft' AND d.is_deleted = 0
) draft ON TRUE
ORDER BY updated_at DESC, code
LIMIT :limit OFFSET :offset
"""

_COUNT_SQL = _LIST_CTE + "SELECT count(*) FROM candidates"

#: 发布 = 先退役旧的 published，再把目标草稿置为 published。顺序不能反：
#: ``uq_std_asset_published`` 是部分唯一索引，旧版本未退役时新版本必然冲突。
PUBLISH_STEPS = (
    "UPDATE data_governance.standard_assets SET status = 'retired', "
    "revision = revision + 1, updated_by = :user_id, updated_at = now() "
    "WHERE tenant_id = :tenant_id AND kind = :kind AND code = :code "
    "AND status = 'published' AND is_deleted = 0 AND id <> :asset_id",
    "UPDATE data_governance.standard_assets SET status = 'published', published_by = :user_id, "
    "published_at = now(), revision = revision + 1, updated_at = now(), updated_by = :user_id "
    "WHERE tenant_id = :tenant_id AND id = :asset_id AND status = 'draft' "
    "AND revision = :revision AND is_deleted = 0",
)

#: 发布前锁行：状态与 revision 必须在这一刻校验，否则「读到 draft 之后、UPDATE
#: 之前被别人发布/修改」会重复退役或写到错误版本。
_PUBLISH_LOCK_SQL = (
    "SELECT target.kind, target.code, target.status, target.revision, "
    "target.version, "
    "(SELECT max(p.version) FROM data_governance.standard_assets p "
    " WHERE p.tenant_id = target.tenant_id AND p.kind = target.kind "
    " AND p.code = target.code AND p.status = 'published' "
    " AND p.is_deleted = 0) AS published_version "
    "FROM data_governance.standard_assets target "
    "WHERE target.tenant_id = :tenant_id AND target.id = :asset_id "
    "AND target.is_deleted = 0 "
    "FOR UPDATE"
)

#: ``publish`` 的哨兵返回值：目标草稿版本低于当前已发布版本。
PUBLISH_STALE_DRAFT = -1

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
        # 版本列表的行没有 lateral 列，用行自身的 version 兜底。
        if draft_version is None and status == "draft":
            draft_version = version
        if published_version is None and status == "published":
            published_version = version
    else:
        draft_version = None
    return {
        "asset_id": _value(row, "id"),
        "kind": _value(row, "kind"),
        "code": _value(row, "code"),
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


async def next_version(session, *, tenant_id, kind, code) -> int:
    """下一个版本号；无历史行时 SQL 的 COALESCE 直接回 1。"""
    value = (await session.execute(
        text(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM data_governance.standard_assets "
            "WHERE tenant_id=:tenant_id AND kind=:kind AND code=:code"
        ),
        {"tenant_id": require_tenant(tenant_id), "kind": kind, "code": code},
    )).scalar()
    return int(value or 1)


async def create_draft(session, *, tenant_id, kind, code, version, name, payload,
                       created_by) -> StandardAsset:
    now = datetime.now(timezone.utc)
    row = StandardAsset(
        id=uuid.uuid4().hex, tenant_id=require_tenant(tenant_id), kind=kind,
        code=code, version=version, name=name,
        payload=payload if payload is not None else {},
        status="draft", revision=1, created_by=created_by,
        updated_by=created_by, is_deleted=0,
        created_at=now, updated_at=now,
    )
    session.add(row)
    await session.flush()
    return row


async def get_by_id(session, *, tenant_id, asset_id, include_deleted=False):
    stmt = (
        "SELECT * FROM data_governance.standard_assets "
        "WHERE tenant_id=:tenant_id AND id=:asset_id"
    )
    if not include_deleted:
        stmt += " AND is_deleted=0"
    return (await session.execute(text(stmt), {
        "tenant_id": require_tenant(tenant_id), "asset_id": asset_id,
    })).mappings().first()


async def get_version(session, *, tenant_id, kind, code, version):
    return (await session.execute(
        text(
            "SELECT * FROM data_governance.standard_assets "
            "WHERE tenant_id=:tenant_id AND kind=:kind AND code=:code "
            "AND version=:version AND is_deleted=0"
        ),
        {
            "tenant_id": require_tenant(tenant_id), "kind": kind,
            "code": code, "version": version,
        },
    )).mappings().first()


async def list_page(session, *, tenant_id, kind=None, status=None, keyword=None,
                    include_drafts: bool = False, limit: int = 20,
                    offset: int = 0) -> list[dict]:
    """分页列表（每个 `(kind, code)` 一行）。

    `status` 过滤发生在 candidates 聚合**内**，属于版本级筛选：例如同一 code
    同时有 published 与 draft 时，传 `status='draft'` 会让代表行变成草稿；不传
    `status` 且 `include_drafts=True` 时，代表行仍按 published 优先，草稿只体现
    在 `draft_version` / `has_draft` 上。
    """
    rows = (await session.execute(text(_LIST_SQL), {
        "tenant_id": require_tenant(tenant_id),
        "kind": kind,
        "status": status,
        "keyword": _like_pattern(keyword),
        "include_drafts": bool(include_drafts),
        "limit": limit,
        "offset": offset,
    })).mappings()
    return [dict(row) for row in rows]


async def count(session, *, tenant_id, kind=None, status=None, keyword=None,
                include_drafts: bool = False) -> int:
    """与 ``list_page`` 同一 WHERE 的计数。

    `status` 同样是版本级过滤（见 ``list_page``）；`include_drafts` 默认与
    ``list_page`` 对齐为 ``False``，调用方必须传与列表相同的值，否则 total
    与 items 口径不一致。
    """
    value = (await session.execute(text(_COUNT_SQL), {
        "tenant_id": require_tenant(tenant_id),
        "kind": kind,
        "status": status,
        "keyword": _like_pattern(keyword),
        "include_drafts": bool(include_drafts),
    })).scalar()
    return int(value or 0)


async def update_draft(session, *, tenant_id, asset_id, revision, fields) -> int:
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
        "asset_id": asset_id,
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
            "UPDATE data_governance.standard_assets SET "
            + (sets + ", " if sets else "")
            + "revision = revision + 1, updated_at = now(), updated_by = :updated_by "
            "WHERE tenant_id=:tenant_id AND id=:asset_id AND status='draft' "
            "AND is_deleted=0 AND revision=:revision"
        ),
        params,
    )
    return result.rowcount


async def publish(session, *, tenant_id, asset_id, revision, user_id) -> int:
    """发布草稿。

    先 `FOR UPDATE` 锁行并在锁内校验 `status='draft' AND revision=:revision`；
    任一不满足直接返回 0，**不执行**退役/发布两步，避免 revision 失配时把旧
    published 误退役。

    若存在版本号更高的 published（旧草稿试图覆盖新发布版），返回
    ``PUBLISH_STALE_DRAFT``（-1），同样不执行任何写操作。
    """
    tid = require_tenant(tenant_id)
    row = (await session.execute(text(_PUBLISH_LOCK_SQL), {
        "tenant_id": tid, "asset_id": asset_id,
    })).mappings().first()
    if row is None or revision is None:
        return 0
    if row["status"] != "draft" or int(row["revision"]) != int(revision):
        return 0
    # 旧草稿不得覆盖更新的发布版（例如先克隆了 v3、随后 v4 已发布）。
    published_version = _value(row, "published_version")
    draft_version = _value(row, "version")
    if (
        published_version is not None
        and draft_version is not None
        and int(published_version) > int(draft_version)
    ):
        return PUBLISH_STALE_DRAFT
    params = {
        "tenant_id": tid,
        "kind": row["kind"], "code": row["code"],
        "asset_id": asset_id, "revision": revision, "user_id": user_id,
    }
    await session.execute(text(PUBLISH_STEPS[0]), params)
    result = await session.execute(text(PUBLISH_STEPS[1]), params)
    return result.rowcount


async def retire(session, *, tenant_id, asset_id, user_id) -> int:
    result = await session.execute(
        text(
            "UPDATE data_governance.standard_assets SET status='retired', "
            "revision=revision+1, updated_at=now(), updated_by=:user_id "
            "WHERE tenant_id=:tenant_id AND id=:asset_id "
            "AND status='published' AND is_deleted=0"
        ),
        {
            "tenant_id": require_tenant(tenant_id),
            "asset_id": asset_id, "user_id": user_id,
        },
    )
    return result.rowcount


async def delete_draft(session, *, tenant_id, asset_id, user_id) -> int:
    result = await session.execute(
        text(
            "UPDATE data_governance.standard_assets SET is_deleted=1, "
            "revision=revision+1, updated_at=now(), updated_by=:user_id "
            "WHERE tenant_id=:tenant_id AND id=:asset_id "
            "AND status='draft' AND is_deleted=0"
        ),
        {
            "tenant_id": require_tenant(tenant_id),
            "asset_id": asset_id, "user_id": user_id,
        },
    )
    return result.rowcount


#: 指标口径卡的引用作用域：每个 code 取**最新一版**（草稿优先）。
#: 跨草稿引用是正常写作过程，不能要求对方先发布；但同一 code 有草稿时以草稿为准，
#: 否则作者刚改的输入指标名在保存时会被旧发布版误判为不存在。
_METRIC_SCOPE_SQL = """
SELECT DISTINCT ON (code) code, payload
  FROM data_governance.standard_assets
 WHERE tenant_id = :tenant_id AND kind = 'metric_card' AND is_deleted = 0
 ORDER BY code,
          CASE status WHEN 'draft' THEN 0 WHEN 'published' THEN 1 ELSE 2 END,
          version DESC
"""


async def load_metric_card_scope(session, *, tenant_id) -> dict[str, dict]:
    """该租户全部指标口径卡的 `{code: {name, executable, additivity}}`。

    只搬作用域校验需要的键；payload 里的业务字段（definition/formula/unit 等）
    不参与指标引用解析。
    """
    rows = (await session.execute(
        text(_METRIC_SCOPE_SQL),
        {"tenant_id": require_tenant(tenant_id)},
    )).mappings()
    scope: dict[str, dict] = {}
    for row in rows:
        payload = row["payload"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                # 坏行不能冒充一个「空 payload 的指标」进作用域：静默补 {} 会让
                # 它看起来存在但没有 executable，错误归因会指向引用方。
                continue
        if not isinstance(payload, dict):
            continue
        code = str(row["code"])
        scope[code] = {
            "name": str(payload.get("definition") or code),
            "executable": payload.get("executable"),
            "additivity": payload.get("additivity"),
        }
    return scope


async def list_versions(session, *, tenant_id, kind, code) -> list[dict]:
    return [dict(r) for r in (await session.execute(
        text(
            "SELECT * FROM data_governance.standard_assets "
            "WHERE tenant_id=:tenant_id AND kind=:kind AND code=:code "
            "AND is_deleted=0 ORDER BY version DESC"
        ),
        {
            "tenant_id": require_tenant(tenant_id),
            "kind": kind, "code": code,
        },
    )).mappings()]
