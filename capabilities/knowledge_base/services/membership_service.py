"""[jonex] 权限重构 B5 —— 成员池约束（方案 §3.5 / 执行文档 §6.3 步骤 1）。

不变式：**被授权到子资源（KB）的人，必须已在父层（空间）的成员池内。**

为什么是「校验对象合法」而不是「校验操作者有权」：
  操作者有没有权改这个 KB 的授权，由 knowledge_info_service.set_permissions 前段的
  get_write_space_ids / kb_manager 判定负责（不满足抛 403）。
  本函数管的是另一件事 —— 就算你有权改，你也不能把 KB 授给一个「连空间都没进」的人。
  所以失败码是 **400（InvalidParameterError）**：授权请求本身不合法，不是权限不足。

parent_type 目前只支持 'space'。目录层（folder）接入时在此加一个分支即可，
**调用方签名不变** —— 这正是把它抽成通用函数而非写死在 KB service 里的原因。
"""
from __future__ import annotations

from sqlalchemy import select

from jonex_core.common import get_db_session
from jonex_core.common.exceptions import InvalidParameterError
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant

from ..models.space import SpacePermission


async def require_parent_membership(
    tenant_id: str,
    parent_type: str,
    parent_id: str,
    user_ids: list[str],
) -> None:
    """被授权人必须已在父层成员池内，否则 400（InvalidParameterError）。

    - 空 user_ids（全量撤销授权）直接放行 —— 没有「新对象」需要校验。
    - 一次 SQL 查出父层现有成员，再做集合差，避免逐人查。
    - 判定看 `is_deleted = 0` 的**当前**成员池，不依赖「谁是创建人」这类会过期的事实。
    """
    tenant_id = require_tenant(tenant_id)
    if parent_type != "space":
        # 目录层等未接入类型：显式拒绝而非静默放行，避免将来加了 parent_type
        # 却忘了在这里补分支，导致约束被绕过。
        raise InvalidParameterError(
            message=translate(
                "err.membership.unsupported_parent_type",
                params={"parent_type": str(parent_type)},
                fallback=f"不支持的父层类型: {parent_type}",
            )
        )

    wanted = {str(u) for u in user_ids}
    if not wanted:
        return

    async with get_db_session() as session:
        result = await session.execute(
            select(SpacePermission.user_id).where(
                SpacePermission.tenant_id == tenant_id,
                SpacePermission.space_id == parent_id,
                SpacePermission.user_id.in_(list(wanted)),
                SpacePermission.is_deleted == 0,
            )
        )
        members = {str(r[0]) for r in result.all()}

    missing = sorted(wanted - members)
    if missing:
        raise InvalidParameterError(
            message=translate(
                "err.membership.not_in_parent_pool",
                params={"user_ids": ", ".join(missing), "parent_id": parent_id},
                fallback=(
                    f"以下用户不在空间成员池内，需先加入空间再授权知识库: "
                    f"{', '.join(missing)}"
                ),
            )
        )
