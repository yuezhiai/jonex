#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""空间归属与可见性守卫（§四）。

任务中心不拥有「空间」，但 §四 硬要求：**凡是接口里带 `space_id` 的**，都得校验
「这个空间确实属于当前租户」——不能拿别的租户的空间 ID 来建定义、建触发器、起任务。

**五处入口**共用这一份实现与这一份文案（`fire` / `trig.create` / `trig.list` /
`def.create` / **`task.list` 的 `space_id` 筛选**），免得同一个校验在几处各写一遍、
过一段时间后各自漂移。

判「不属于」时报**参数非法**，不是「找不到」：§四原文明确要的是「你给的参数不对」，
而不是「你要的东西不存在」。归属查询本身是跨 schema 只读，见 `SpaceRepository`。

── 2026-10-10 追加：**用户对空间有没有权限**（`require_space_visible` / `visible_space_ids_for`）──

归属（这个空间是不是本租户的）与权限（这个人能不能看）是**两件事**：前者防「拿别的
租户的 id 来用」，后者防「同租户里越权看别人的空间」。控制台列表接口两个都要。

权威判据在知识库 schema 的 `knowledge_base.space_permissions`（角色只有
`space_manager` / `member` 两种），**任务中心不 import 知识库的服务**，走
`SpaceRepository` 的 raw SQL 探针；只有「是不是租户管理员」这一档复用 `jonex_core`
的 `has_permission`（那是全局能力，不属某个 capability）。
"""

from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.exceptions import InvalidParameterError, PermissionDeniedError
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant
from jonex_core.common.tenant_admin import is_tenant_admin

from ..repository.space_repository import SpaceRepository


async def require_owned_space(
    session: AsyncSession, tenant_id: str, space_id: str
) -> str:
    """校验 `space_id` 是当前租户名下的空间，返回它（便于链式收口）。"""
    tenant_id = require_tenant(tenant_id)
    if not await SpaceRepository(session).exists_for_tenant(tenant_id, space_id):
        raise InvalidParameterError(
            message=translate(
                "err.task_center.space.not_owned",
                params={"space_id": space_id},
                fallback=f"空间 {space_id} 不存在，或者不属于当前租户",
            ),
            details={"space_id": space_id},
        )
    return space_id


async def _is_tenant_admin(tenant_id: str, user_id: str) -> bool:
    """是不是租户/平台管理员 —— 转发内核唯一实现（`jonex_core.common.tenant_admin`）。"""
    return await is_tenant_admin(tenant_id, user_id)


async def require_space_visible(
    session: AsyncSession, tenant_id: str, space_id: str, user_id: str | None
) -> str:
    """用户**显式指定了** `space_id` 时：他得对这个空间有角色（租户管理员除外）。

    不满足 → **403**（不是 404）。任务中心对「能力级权限码」不够用的处置本来就是 403
    （`capability._check_action_permission` 抛 `PermissionDeniedError`），这里保持一致
    —— 用户点的是「看这个空间」，告诉他「你没这个空间的权限」比伪装成「不存在」有用。
    （知识库那边的 `require_space_visible` 用的是 404 防探测，口径不同：那条路会把
    空间列表本身当探测面。）

    ⚠️ **`user_id` 为空时放行**（返回 `space_id`）：这是内部链路 / 测试 token 的既有口径，
    与 `_check_action_permission` 的「没有 `user_id` 就放行」同源。**不要**在这里收紧 ——
    那会把所有不带用户身份的调用方一起打死，而它们本来就在能力级判权那一层被放过。
    """
    if not user_id:
        return space_id
    tenant_id = require_tenant(tenant_id)
    if await _is_tenant_admin(tenant_id, user_id):
        return space_id
    roles = await SpaceRepository(session).user_space_roles(tenant_id, space_id, user_id)
    if roles:
        return space_id
    raise PermissionDeniedError(
        message=translate(
            "err.task_center.space.not_visible",
            params={"space_id": space_id},
            fallback=f"你没有空间 {space_id} 的权限",
        ),
        details={"space_id": space_id},
    )


async def require_space_manager(
    session: AsyncSession, tenant_id: str, space_id: str, user_id: str | None
) -> str:
    """用户对空间有**管理**身份：`space_manager` 或租户/平台管理员。

    任务定义的写操作（创建 / 编辑 / 编排 / 发布 / 停用 / 删除 / 复制）用这道守卫；
    它与能力入口的 `task_center:write` **两个条件都要满足** —— 码在入口判、
    空间身份在这里判（2026-11-18 定）。

    `user_id` 为空（内部链路 / 测试 token）按既有口径放行 —— 与
    `require_space_visible` / `_check_action_permission` 同源；**不要**在这里收紧。
    """
    if not user_id:
        return space_id
    tenant_id = require_tenant(tenant_id)
    if await _is_tenant_admin(tenant_id, user_id):
        return space_id
    roles = await SpaceRepository(session).user_space_roles(
        tenant_id, space_id, user_id
    )
    if "space_manager" in roles:
        return space_id
    raise PermissionDeniedError(
        message=translate(
            "err.task_center.space.not_manager",
            params={"space_id": space_id},
            fallback=f"你不是空间 {space_id} 的管理员，不能修改该空间的任务定义",
        ),
        details={"space_id": space_id},
    )


async def visible_space_ids_for(
    session: AsyncSession, tenant_id: str, user_id: str | None
) -> list[str] | None:
    """**没指定空间**时，这次查询该收敛到哪些空间 —— `None` 表示**不过滤**。

    三种 `None`：没有用户身份（内部链路）、租户/平台管理员（看全部）、
    其它一律回该用户有角色的空间 id 列表（可能是空列表 = 什么都看不到）。
    """
    if not user_id:
        return None
    tenant_id = require_tenant(tenant_id)
    if await _is_tenant_admin(tenant_id, user_id):
        return None
    return await SpaceRepository(session).visible_space_ids_for_user(tenant_id, user_id)


__all__ = [
    "require_owned_space",
    "require_space_manager",
    "require_space_visible",
    "visible_space_ids_for",
]
