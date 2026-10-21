"""分级访问策略：用户可读最高等级与文档 / 知识库生效等级。

只读模块：不写库、不缓存，每请求一次批量查询。所有 SQL 都绑定
``tenant_id``，调用方负责先把 ``is_tenant_admin`` 算好传进来。

等级口径（与资产目录一致）：

* 生效 code = ``COALESCE(asset_metadata.sensitivity, KB 默认, 租户默认)``；
* 空 code -> rank 0（未分级，所有人可读）；
* 有 code -> ``classification_level.rank``；**该 code 无等级行时**（例如等级表
  尚未懒初始化）按内置 ``public=1 / internal=2 / confidential=3`` 回落；
  其余未知 code 按 ``1000000`` fail closed；
* 用户 clearance 的 code 无法解析时按 ``0`` fail closed：只放行未分级
  （rank 0）资产，任何已分级资产（rank ≥ 1）都拒绝；
* 用户无 clearance 行 -> ``MIN(active rank)``，无启用等级时回 1；
* 租户管理员 -> ``None``（不限）。
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.exceptions import ResourceNotFoundError
from jonex_core.common.tenant import require_tenant

# 内置 code 回落（与第 4 期懒初始化种子一致）。列名来自本模块常量，非用户输入。
def _builtin_rank(column: str) -> str:
    return (
        "CASE " + column + "\n"
        "        WHEN 'public' THEN 1\n"
        "        WHEN 'internal' THEN 2\n"
        "        WHEN 'confidential' THEN 3\n"
        "        ELSE 1000000\n"
        "    END"
    )


_USER_CLEARANCE_SQL = text("""
    SELECT c.max_level_code AS code,
           lvl.rank AS level_rank
      FROM data_governance.user_clearance c
      LEFT JOIN data_governance.classification_level lvl
        ON lvl.tenant_id = c.tenant_id
       AND lvl.code = c.max_level_code
     WHERE c.tenant_id = :tenant_id
       AND c.user_id = :user_id
     LIMIT 1
""")

_DEFAULT_RANK_SQL = text("""
    SELECT COALESCE(MIN(rank) FILTER (WHERE is_active), 1) AS default_rank
      FROM data_governance.classification_level
     WHERE tenant_id = :tenant_id
""")

_DOCUMENT_RANKS_SQL = text(f"""
    WITH effective AS (
        SELECT d.id AS doc_id,
               COALESCE(am.sensitivity, kbd.level_code, td.level_code) AS code
          FROM knowledge_base.knowledge_documents d
          LEFT JOIN data_governance.asset_metadata am
            ON am.tenant_id = d.tenant_id
           AND am.asset_type = 'document'
           AND am.asset_id = d.id
          LEFT JOIN data_governance.classification_default kbd
            ON kbd.tenant_id = d.tenant_id
           AND kbd.scope_type = 'kb'
           AND kbd.scope_id = d.knowledge_base_id
          LEFT JOIN data_governance.classification_default td
            ON td.tenant_id = d.tenant_id
           AND td.scope_type = 'tenant'
           AND td.scope_id = ''
         WHERE d.tenant_id = :tenant_id
           AND d.id = ANY(:doc_ids)
    )
    SELECT e.doc_id,
           CASE
               WHEN e.code IS NULL THEN 0
               ELSE COALESCE(lvl.rank, {_builtin_rank("e.code")})
           END AS level_rank
      FROM effective e
      LEFT JOIN data_governance.classification_level lvl
        ON lvl.tenant_id = :tenant_id
       AND lvl.code = e.code
""")

_KB_MAX_RANK_SQL = text(f"""
    WITH effective AS (
        SELECT d.knowledge_base_id AS kb_id,
               COALESCE(am.sensitivity, kbd.level_code, td.level_code) AS code
          FROM knowledge_base.knowledge_documents d
          LEFT JOIN data_governance.asset_metadata am
            ON am.tenant_id = d.tenant_id
           AND am.asset_type = 'document'
           AND am.asset_id = d.id
          LEFT JOIN data_governance.classification_default kbd
            ON kbd.tenant_id = d.tenant_id
           AND kbd.scope_type = 'kb'
           AND kbd.scope_id = d.knowledge_base_id
          LEFT JOIN data_governance.classification_default td
            ON td.tenant_id = d.tenant_id
           AND td.scope_type = 'tenant'
           AND td.scope_id = ''
         WHERE d.tenant_id = :tenant_id
           AND d.knowledge_base_id = ANY(:kb_ids)
           AND d.is_deleted = 0
    )
    SELECT e.kb_id,
           MAX(
               CASE
                   WHEN e.code IS NULL THEN 0
                   ELSE COALESCE(lvl.rank, {_builtin_rank("e.code")})
               END
           ) AS max_rank
      FROM effective e
      LEFT JOIN data_governance.classification_level lvl
        ON lvl.tenant_id = :tenant_id
       AND lvl.code = e.code
     GROUP BY e.kb_id
""")


async def get_user_max_rank(
    session: AsyncSession,
    tenant_id: str,
    user_id: str,
    *,
    is_tenant_admin: bool,
) -> int | None:
    """用户可读最高 rank；``None`` = 不限（租户管理员）。"""
    tid = require_tenant(tenant_id)
    if is_tenant_admin:
        return None

    row = (
        await session.execute(
            _USER_CLEARANCE_SQL,
            {"tenant_id": tid, "user_id": str(user_id)},
        )
    ).mappings().first()

    if row is not None:
        if row["level_rank"] is not None:
            return int(row["level_rank"])
        code = row["code"]
        return {
            "public": 1,
            "internal": 2,
            "confidential": 3,
        }.get(code, 0)

    default_rank = (
        await session.execute(_DEFAULT_RANK_SQL, {"tenant_id": tid})
    ).scalar_one()
    return int(default_rank)


async def document_level_ranks(
    session: AsyncSession,
    tenant_id: str,
    doc_ids: list[str],
) -> dict[str, int]:
    """``doc_id -> 生效 rank``；0 = 未分级；缺失文档不出现在 dict 中。

    不过滤 ``is_deleted``：软删文档仍返回其生效 rank（存在性由下游校验）。
    """
    tid = require_tenant(tenant_id)
    doc_ids = [str(x) for x in doc_ids]
    if not doc_ids:
        return {}
    rows = (
        await session.execute(
            _DOCUMENT_RANKS_SQL,
            {"tenant_id": tid, "doc_ids": doc_ids},
        )
    ).mappings().all()
    return {row["doc_id"]: int(row["level_rank"]) for row in rows}


async def kb_max_level_rank(
    session: AsyncSession,
    tenant_id: str,
    kb_ids: list[str],
) -> dict[str, int]:
    """``kb_id -> KB 内文档最高生效 rank``；空 KB / 缺失 KB = 0。

    返回结果覆盖输入的全部 ``kb_id``；只统计 ``is_deleted=0`` 的文档。
    """
    tid = require_tenant(tenant_id)
    kb_ids = [str(x) for x in kb_ids]
    if not kb_ids:
        return {}
    result = {kb_id: 0 for kb_id in kb_ids}
    rows = (
        await session.execute(
            _KB_MAX_RANK_SQL,
            {"tenant_id": tid, "kb_ids": kb_ids},
        )
    ).mappings().all()
    for row in rows:
        result[row["kb_id"]] = int(row["max_rank"])
    return result


async def filter_allowed_documents(
    session: AsyncSession,
    tenant_id: str,
    user_id: str,
    doc_ids: list[str],
    *,
    is_tenant_admin: bool,
) -> list[str]:
    """返回允许访问的 doc_id（保持输入顺序；重复项原样保留）。

    缺失文档按 rank 0 放行，存在性由下游 service 校验（随后 404）。
    """
    tid = require_tenant(tenant_id)
    doc_ids = [str(x) for x in doc_ids]
    if not doc_ids:
        return []
    if is_tenant_admin:
        return list(doc_ids)

    user_rank = await get_user_max_rank(
        session, tid, user_id, is_tenant_admin=False,
    )
    assert user_rank is not None
    ranks = await document_level_ranks(session, tid, doc_ids)
    return [
        doc_id for doc_id in doc_ids
        if ranks.get(doc_id, 0) <= user_rank
    ]


async def assert_documents_allowed(
    session: AsyncSession,
    tenant_id: str,
    user_id: str,
    doc_ids: list[str],
    *,
    is_tenant_admin: bool,
) -> None:
    """任一文档超密级 -> ``ResourceNotFoundError``（404，不泄露存在性）。

    缺失文档按 rank 0 放行，存在性由下游 service 校验（随后 404）。
    """
    tid = require_tenant(tenant_id)
    doc_ids = [str(x) for x in doc_ids]
    if not doc_ids or is_tenant_admin:
        return

    user_rank = await get_user_max_rank(
        session, tid, user_id, is_tenant_admin=False,
    )
    assert user_rank is not None
    ranks = await document_level_ranks(session, tid, doc_ids)
    if any(ranks.get(doc_id, 0) > user_rank for doc_id in doc_ids):
        raise ResourceNotFoundError(message="文档不存在或无权访问")
