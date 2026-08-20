"""
菜单管理服务。
"""
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.exceptions import ResourceNotFoundError
from jonex_core.common.i18n import translate
from capabilities.platform.models.menu import Menu
from capabilities.platform.repository.menu_repository import MenuRepository
from capabilities.platform.dtos.platform import (
    MenuCreateRequest,
    MenuUpdateRequest,
    MenuResponse,
    MenuListResponse,
)

logger = logging.getLogger(__name__)


class MenuService:
    """菜单管理服务"""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.repo = MenuRepository(session)

    @staticmethod
    def _build_tree(menus: list[Menu], parent_id: int = 0) -> list[MenuResponse]:
        result = []
        for m in menus:
            if m.parent_id == parent_id:
                node = MenuResponse.from_orm(m)
                node.children = MenuService._build_tree(menus, m.id)
                result.append(node)
        return result

    async def get_tree(self) -> MenuListResponse:
        menus = await self.repo.list_tree()
        tree = self._build_tree(menus)
        return MenuListResponse(items=tree)

    async def create(self, req: MenuCreateRequest) -> MenuResponse:
        menu = Menu(
            parent_id=req.parent_id,
            name=req.name,
            path=req.path,
            icon=req.icon,
            app_id=req.app_id,
            sort_order=req.sort_order,
            visible=req.visible,
            permission_code=req.permission_code,
        )
        self.session.add(menu)
        await self.session.flush()
        logger.info(f"创建菜单: {menu.name} (id={menu.id})")
        return MenuResponse.from_orm(menu)

    async def update(self, menu_id: int, req: MenuUpdateRequest) -> MenuResponse:
        menu = await self.repo.get_by_id_shared(menu_id)
        if not menu or menu.is_deleted:
            raise ResourceNotFoundError(
            message=translate("err.menu.not_found", params={"menu_id": str(menu_id)}, fallback=f"菜单不存在: {menu_id}")
        )  # 原消息: 菜单不存在: {menu_id}

        update_data = req.dict(exclude_unset=True)
        for key, val in update_data.items():
            setattr(menu, key, val)
        await self.session.flush()
        return MenuResponse.from_orm(menu)

    async def delete(self, menu_id: int) -> None:
        menu = await self.repo.get_by_id_shared(menu_id)
        if not menu or menu.is_deleted:
            raise ResourceNotFoundError(
            message=translate("err.menu.not_found", params={"menu_id": str(menu_id)}, fallback=f"菜单不存在: {menu_id}")
        )  # 原消息: 菜单不存在: {menu_id}
        await self.repo.delete_soft_shared(menu)
        logger.info(f"删除菜单: {menu.name} (id={menu.id})")
