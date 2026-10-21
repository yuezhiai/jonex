"""data_governance.user_clearance 读写。

无记录 = 使用默认最低启用等级（由 service 层解析），因此清除密级是删除行，
不写 ``max_level_code=NULL``。

``get_clearance`` / ``upsert_clearance`` / ``delete_clearance`` 的 ``user_id``
必须是规范的数字字符串（服务层负责把 int 归一为 str）；``user_exists`` 会
自行校验并安全拒绝非数字输入。
"""
from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.tenant import require_tenant

from ..models import UserClearance

_LIST_SQL = text("""
    SELECT u.id::text AS user_id,
           u.username,
           u.display_name,
           c.max_level_code,
           COALESCE(u.status, 1) = 1 AS is_active
      FROM platform.users u
      LEFT JOIN data_governance.user_clearance c
             ON c.tenant_id = u.tenant_id
            AND c.user_id = u.id::text
     WHERE u.tenant_id = :t
       AND u.is_deleted = 0
     ORDER BY u.username, u.id
""")


async def get_clearance(
    session: AsyncSession, tenant_id: str, user_id: str,
) -> UserClearance | None:
    tid = require_tenant(tenant_id)
    result = await session.execute(
        select(UserClearance).where(
            UserClearance.tenant_id == tid,
            UserClearance.user_id == str(user_id),
        )
    )
    return result.scalars().first()


async def upsert_clearance(
    session: AsyncSession,
    *,
    tenant_id: str,
    user_id: str,
    max_level_code: str,
    updated_by: str | None,
) -> UserClearance:
    tid = require_tenant(tenant_id)
    stmt = (
        pg_insert(UserClearance)
        .values(
            id=uuid.uuid4().hex,
            tenant_id=tid,
            user_id=str(user_id),
            max_level_code=max_level_code,
            updated_by=updated_by,
            # 显式给时间戳：DB 默认 now() 与 ORM 取回值在时区处理上可能不一致。
            created_at=func.now(),
            updated_at=func.now(),
        )
        .on_conflict_do_update(
            index_elements=["tenant_id", "user_id"],
            set_={
                "max_level_code": max_level_code,
                "updated_by": updated_by,
                "updated_at": func.now(),
            },
        )
        .returning(UserClearance)
        # 同一 session 里已加载过该身份时，用 RETURNING 的新值覆盖旧属性。
        .execution_options(populate_existing=True)
    )
    row = (await session.execute(stmt)).scalars().one()
    await session.flush()
    return row


async def delete_clearance(
    session: AsyncSession, tenant_id: str, user_id: str,
) -> None:
    tid = require_tenant(tenant_id)
    await session.execute(
        delete(UserClearance).where(
            UserClearance.tenant_id == tid,
            UserClearance.user_id == str(user_id),
        )
    )


async def list_user_clearances(
    session: AsyncSession, tenant_id: str,
) -> list[dict]:
    tid = require_tenant(tenant_id)
    rows = (await session.execute(_LIST_SQL, {"t": tid})).mappings().all()
    return [
        {
            "user_id": row["user_id"],
            "username": row["username"],
            "display_name": row["display_name"] or row["username"],
            "max_level_code": row["max_level_code"],
            "is_active": row["is_active"],
        }
        for row in rows
    ]


async def user_exists(
    session: AsyncSession, tenant_id: str, user_id: str,
) -> bool:
    tid = require_tenant(tenant_id)
    uid = str(user_id).strip()
    # 仅接受可安全转成 BIGINT 的 ASCII 十进制串：全角/上标数字或超长值
    # 若直接 CAST 会抛数据库异常并把 session 带入 aborted transaction。
    if not (uid.isascii() and uid.isdecimal()) or len(uid) > 19:
        return False
    if int(uid) > 9223372036854775807:
        return False
    # 参数以 TEXT 绑定再转 BIGINT：避免 asyncpg 按 BIGINT 推断参数类型后
    # 拒绝字符串实参。
    row = (
        await session.execute(
            text(
                "SELECT 1 FROM platform.users "
                "WHERE tenant_id = :t AND is_deleted = 0 "
                "AND id = CAST(CAST(:u AS TEXT) AS BIGINT) LIMIT 1"
            ),
            {"t": tid, "u": uid},
        )
    ).first()
    return row is not None
