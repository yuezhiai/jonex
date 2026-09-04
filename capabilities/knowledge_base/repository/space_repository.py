#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Repositories for Space and related models."""

from sqlalchemy import select

from jonex_core.common.repository import BaseRepository
from jonex_core.common.tenant import require_tenant

from ..models.space import Space, SpacePermission


class SpaceRepository(BaseRepository[Space]):
    model = Space

    async def name_exists(
        self,
        tenant_id: str,
        name: str,
        exclude_id: str | None = None,
    ) -> bool:
        """同租户内是否已存在同名空间（排除软删；编辑时用 exclude_id 排除自身）。"""
        tid = require_tenant(tenant_id)
        conditions = [
            Space.tenant_id == tid,
            Space.name == name,
            Space.is_deleted == 0,
        ]
        if exclude_id is not None:
            conditions.append(Space.id != exclude_id)
        result = await self.session.execute(select(Space.id).where(*conditions))
        return result.first() is not None


class SpacePermissionRepository(BaseRepository[SpacePermission]):
    model = SpacePermission
