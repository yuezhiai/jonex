"""KB 授权判定服务 — viewer/editor 叠加授权（设计 2026-08-20-kb-permission-design §1）。

叠加语义：空间成员对 KB 的权限不变；本模块只回答「该用户是否被单独授权」。
`get_granted_kb_ids` 刻意不返回 None——无 user/anonymous 返回 []（与
get_visible_space_ids 的 None=不过滤语义相反，避免调用侧把「全给」写成「不给」）。
"""
from __future__ import annotations

from sqlalchemy import select

from jonex_core.common import get_db_session
from jonex_core.common.exceptions import PermissionDeniedError
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant

from ..models.kb_permission import KbPermission


async def get_kb_grant_role(tenant_id: str, kb_id: str, user_id: str | None) -> str | None:
    """'editor'|'viewer'|None——单查询 kb_permissions；多行取最高 editor > viewer。"""
    tenant_id = require_tenant(tenant_id)
    if not user_id or user_id == "anonymous":
        return None
    async with get_db_session() as session:
        result = await session.execute(
            select(KbPermission.role).where(
                KbPermission.tenant_id == tenant_id,
                KbPermission.kb_id == kb_id,
                KbPermission.user_id == user_id,
                KbPermission.is_deleted == 0,
            )
        )
        roles = {r[0] for r in result.all() if r[0] in ("editor", "viewer")}
    if not roles:
        return None
    return "editor" if "editor" in roles else "viewer"


async def require_kb_role(tenant_id: str, kb_id: str, user_id: str | None, *roles: str) -> None:
    """不满足任一授权角色抛 403。"""
    actual = await get_kb_grant_role(tenant_id, kb_id, user_id)
    if actual not in roles:
        raise PermissionDeniedError(
            message=translate(
                "err.kb_permission.insufficient_role",
                params={"kb_id": kb_id},
                fallback=f"无权操作该知识库: {kb_id}",
            )
        )


async def get_granted_kb_ids(tenant_id: str, user_id: str | None) -> list[str]:
    """授权 KB id 集合；无 user/anonymous → 空列表 []（不承担内部链路信号）。"""
    tenant_id = require_tenant(tenant_id)
    if not user_id or user_id == "anonymous":
        return []
    async with get_db_session() as session:
        result = await session.execute(
            select(KbPermission.kb_id).where(
                KbPermission.tenant_id == tenant_id,
                KbPermission.user_id == user_id,
                KbPermission.is_deleted == 0,
            )
        )
        return [r[0] for r in result.all()]


async def get_granted_editor_kb_ids(tenant_id: str, user_id: str | None) -> list[str]:
    """editor 授权的 KB id 集合（KB 写权限判定用）；无 user/anonymous → 空列表 []。"""
    tenant_id = require_tenant(tenant_id)
    if not user_id or user_id == "anonymous":
        return []
    async with get_db_session() as session:
        result = await session.execute(
            select(KbPermission.kb_id).where(
                KbPermission.tenant_id == tenant_id,
                KbPermission.user_id == user_id,
                KbPermission.role == "editor",
                KbPermission.is_deleted == 0,
            )
        )
        return [r[0] for r in result.all()]
