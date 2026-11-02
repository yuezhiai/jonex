#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""内置步骤清单仓储。

平台预置、只读、不带租户，所以走 `*_shared` 系列（不参与租户隔离）。

⚠️ 2026-10-23 起没有「内置任务」这一层：步骤清单是**全局一张表**，按 `step_code` 唯一，
编排时直接从里面选节点（parse / compile）。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.repository import BaseRepository

from ..models import TaskCatalogStep


class TaskCatalogStepRepository(BaseRepository[TaskCatalogStep]):
    """内置步骤清单仓储。"""

    model = TaskCatalogStep

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def list_all(self) -> list[TaskCatalogStep]:
        stmt = select(TaskCatalogStep).order_by(TaskCatalogStep.step_code)
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def map_by_step_code(self) -> dict[str, TaskCatalogStep]:
        """按 step_code 建索引：发版校验、派活、子任务命名都按 `step_code` 查。

        一次查完全表，避免逐个节点 N+1（表里只有 parse / compile 两行）。
        """
        return {row.step_code: row for row in await self.list_all()}


__all__ = ["TaskCatalogStepRepository"]
