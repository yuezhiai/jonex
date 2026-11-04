from typing import Optional, Sequence

from sqlalchemy import select, func, or_

from capabilities.platform.models.application import Application
from capabilities.platform.models.menu import Menu
from capabilities.platform.repository.base import BaseRepository


class MenuRepository(BaseRepository[Menu]):
    model = Menu

    async def list_tree(self) -> Sequence[Menu]:
        result = await self.session.execute(
            select(Menu)
            .outerjoin(Application, Application.id == Menu.app_id)
            .where(
                Menu.is_deleted == 0,
                or_(Menu.app_id.is_(None), Application.is_deleted == 0),
            )
            .order_by(Menu.sort_order, Menu.id)
        )
        return result.scalars().all()

    async def list_by_parent(self, parent_id: int = 0) -> Sequence[Menu]:
        result = await self.session.execute(
            select(Menu)
            .outerjoin(Application, Application.id == Menu.app_id)
            .where(
                Menu.parent_id == parent_id,
                Menu.is_deleted == 0,
                or_(Menu.app_id.is_(None), Application.is_deleted == 0),
            )
            .order_by(Menu.sort_order)
        )
        return result.scalars().all()