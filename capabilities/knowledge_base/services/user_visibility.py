"""软删用户可见性辅助。

权限绑定表（space_permissions / kb_permissions）在用户软删时**不删行**，用于复职后
恢复授权。因此每一处「展示成员」的查询都必须自己过滤 `platform.users.is_deleted`。

注意：soft-delete 谓词必须放在 WHERE（或 INNER JOIN），不能放在 LEFT JOIN 的 ON 里 ——
写在 ON 里只让 join 列变 NULL，不会排除行。
"""

from typing import Iterable

from sqlalchemy import text


async def get_soft_deleted_user_ids(
    session,
    tenant_id: str,
    user_ids: Iterable[str],
) -> set[str]:
    """返回 `user_ids` 中在 platform.users 已软删的 id 集合。

    user_ids 统一按字符串比较（`id::text`），与绑定表 `user_id` 的存量口径一致，
    避免非数字历史 id 触发 CAST 报错。
    """
    ids = {str(uid) for uid in user_ids if uid}
    if not ids:
        return set()
    rows = await session.execute(
        text(
            "SELECT id::text FROM platform.users "
            "WHERE tenant_id = :tenant_id "
            "  AND is_deleted <> 0 "
            "  AND id::text = ANY(:uids)"
        ),
        {"tenant_id": tenant_id, "uids": sorted(ids)},
    )
    return {row[0] for row in rows}
