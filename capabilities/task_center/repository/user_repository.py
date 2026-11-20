#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""用户展示名探针（跨 schema 只读）。

任务中心的定义/任务行只存 `user_id` 字符串；前端要展示「创建人 / 发起人名字」，
所以这里用一条原生 SQL 去 `platform.users` 取 `display_name`（为空回落 `username`）。

⚠️ **任务中心不 import 平台服务**（跨 capability 依赖会把平台整条 import 链拖进来，
同 `SpaceRepository` 的理由）；这里只读一张表、只取一个字段。
⚠️ 一律带 `tenant_id`，不允许跨租户查名字。
"""

from typing import Iterable

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

_STMT = text(
    "SELECT id::text AS id, "
    "       COALESCE(NULLIF(display_name, ''), username) AS name "
    "  FROM platform.users "
    " WHERE tenant_id = :tenant_id "
    "   AND id::text IN :user_ids"
).bindparams(bindparam("user_ids", expanding=True))


async def user_names_by_ids(
    session: AsyncSession, tenant_id: str, user_ids: Iterable[str]
) -> dict[str, str]:
    """`{user_id: 展示名}`；查不到的 id 不出现在结果里（调用方回落「系统」）。"""
    ids = sorted({str(uid) for uid in user_ids if uid})
    if not ids:
        return {}
    rows = await session.execute(_STMT, {"tenant_id": tenant_id, "user_ids": ids})
    return {row[0]: row[1] for row in rows}


__all__ = ["user_names_by_ids"]
