"""KB 授权判定服务 — 叠加授权（设计 2026-08-20-kb-permission-design §1）。

叠加语义：空间成员对 KB 的权限不变；本模块只回答「该用户是否被单独授权」。
`get_granted_kb_ids` 刻意不返回 None——无 user/anonymous 返回 []（与
get_visible_space_ids 的 None=不过滤语义相反，避免调用侧把「全给」写成「不给」）。

[jonex] 权限重构 B3（D1）：KB 资源身份由三级（kb_manager/editor/viewer）收为两级
`kb_manager` / `member`。**写权限与人员管理权限都只属于 kb_manager，member 只读。**

两级模型下没有「能写但不能管人」这一档 —— 这是 D1 的取舍：
存量 editor 按 E4 全量升为 kb_manager（保住写权限，代价是额外获得管人能力），
而非降为 member（那会让所有 editor 立刻失去写权限）。
方案 docs/permissions/PERMISSIONS_REDESIGN.md §3.3 / 执行文档 §0.6-E4
"""
from __future__ import annotations

from sqlalchemy import select

from jonex_core.common import get_db_session
from jonex_core.common.exceptions import PermissionDeniedError
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant

from ..models.kb_permission import KbPermission

# [jonex] B3：KB 资源身份取值与优先级（唯一定义点）。
# 用 rank 而非逐值 if 链取最高角色 —— 将来加层级只改这一处。
KB_MANAGER = "kb_manager"
KB_MEMBER = "member"
_KB_ROLE_RANK: dict[str, int] = {KB_MEMBER: 0, KB_MANAGER: 1}


async def get_kb_grant_role(tenant_id: str, kb_id: str, user_id: str | None) -> str | None:
    """`'kb_manager'` | `'member'` | None —— 单查询 kb_permissions，多行取最高。

    ⚠️ 未识别的取值（残留的 editor/viewer）一律返回 None（视为无授权），**不做静默兼容**。
    把 editor 当成 kb_manager 会让「迁移没跑」变成一次静默提权，且行为上完全看不出来；
    判为无授权则会明确表现为 403，能立刻定位到是数据没迁。
    """
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
        roles = [r[0] for r in result.all() if r[0] in _KB_ROLE_RANK]
    if not roles:
        return None
    return max(roles, key=lambda r: _KB_ROLE_RANK[r])


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
    """授权 KB id 集合；无 user/anonymous → 空列表 []（不承担内部链路信号）。
    kb_manager 天然包含（本函数无 role 过滤，勿加白名单）。"""
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


async def get_granted_manager_kb_ids(tenant_id: str, user_id: str | None) -> list[str]:
    """`kb_manager` 授权的 KB id 集合；无 user/anonymous → []。

    同时是「KB 写权限」与「KB 人员管理权限」的判据 —— 两级模型下这两件事同源。

    [jonex] B3（N4）：原先还有一个 `get_granted_editor_kb_ids`，
    查的是 `role IN ('kb_manager', 'editor')`（即「可写」）。D1 删掉 editor 之后
    它与本函数**完全等价**，留两个会让后人误以为 KB 还有「可写但非管理者」这一档，
    进而写出基于该假设的分支。已删除，调用方全部指向本函数。
    """
    tenant_id = require_tenant(tenant_id)
    if not user_id or user_id == "anonymous":
        return []
    async with get_db_session() as session:
        result = await session.execute(
            select(KbPermission.kb_id).where(
                KbPermission.tenant_id == tenant_id,
                KbPermission.user_id == user_id,
                KbPermission.role == KB_MANAGER,
                KbPermission.is_deleted == 0,
            )
        )
        return [r[0] for r in result.all()]


async def get_accessible_kb_ids(
    tenant_id: str, user_id: str | None, visible_space_ids: list[str] | None
) -> set[str] | None:
    """可访问 KB 集合 = 可见空间内 KB ∪ 授权 KB。

    visible_space_ids is None（租户管理员/平台管理员/无 user/anonymous）→ 返回 None
    表示「全可见、不过滤」——与 get_visible_space_ids 的 None 语义一致。
    """
    tenant_id = require_tenant(tenant_id)
    if visible_space_ids is None:
        return None
    from ..models.knowledge_info import KnowledgeInfo

    async with get_db_session() as session:
        result = await session.execute(
            select(KnowledgeInfo.id).where(
                KnowledgeInfo.tenant_id == tenant_id,
                KnowledgeInfo.space_id.in_(visible_space_ids),
                KnowledgeInfo.is_deleted == 0,
            )
        )
        kb_ids = {r[0] for r in result.all()}
    kb_ids.update(await get_granted_kb_ids(tenant_id, user_id))
    return kb_ids
