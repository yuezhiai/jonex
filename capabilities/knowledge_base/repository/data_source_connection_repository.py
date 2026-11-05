#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""DataSourceConnection 仓储。"""

from sqlalchemy import func, select

from jonex_core.common.repository import BaseRepository
from jonex_core.common.tenant import require_tenant

from ..models.data_source_connection import DataSourceConnection


class DataSourceConnectionRepository(BaseRepository[DataSourceConnection]):
    model = DataSourceConnection

    async def list_by_space(self, tenant_id: str, space_id: str,
                            offset: int = 0, limit: int = 200):
        """按领域空间列连接（连同总数）。空间为空时由调用方拒绝，
        本方法不做「返回全租户」的兜底 —— 那正是切换领域后串数据的那条路。"""
        tenant_id = require_tenant(tenant_id)
        conditions = [
            DataSourceConnection.tenant_id == tenant_id,
            DataSourceConnection.space_id == space_id,
            DataSourceConnection.is_deleted == 0,
        ]
        items = list((await self.session.execute(
            select(DataSourceConnection).where(*conditions)
            .order_by(DataSourceConnection.created_at.desc())
            .offset(offset).limit(limit))).scalars().all())
        total = (await self.session.execute(
            select(func.count()).select_from(DataSourceConnection).where(*conditions)
        )).scalar_one()
        return items, int(total or 0)

    async def list_by_ids(self, tenant_id: str, ids: list[str]) -> list[DataSourceConnection]:
        """批量取连接（列表回显用，避免按行 N+1），返回 ORM 对象。

        顺序不保证（`IN` 不排序）：调用方要按 id 对齐得自己建 map。
        """
        # 仓储层统一租户校验（与 list_by_kb/count_by_kb 走 BaseRepository 时同口径）：
        # 非法租户必须在这里炸，而不是静静返回空列表——空列表在调用方眼里等于
        # 「这些连接都不存在」，是**放行**那一侧的答案。
        tenant_id = require_tenant(tenant_id)
        if not ids:
            return []
        stmt = (
            select(DataSourceConnection)
            .where(
                DataSourceConnection.tenant_id == tenant_id,
                DataSourceConnection.id.in_(ids),
                DataSourceConnection.is_deleted == 0,
            )
        )
        return list((await self.session.execute(stmt)).scalars().all())


__all__ = ["DataSourceConnectionRepository"]
