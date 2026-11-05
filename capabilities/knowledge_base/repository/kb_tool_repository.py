#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Repository for knowledge-base tools."""

from sqlalchemy import func, or_, select

from jonex_core.common.repository import BaseRepository
from jonex_core.common.tenant import require_tenant

from ..models.kb_tool import KbTool


class KbToolRepository(BaseRepository[KbTool]):
    model = KbTool

    async def list_by_kb(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        offset: int = 0,
        limit: int = 50,
        keyword: str | None = None,
        enabled_only: bool = False,
    ) -> tuple[list[KbTool], int]:
        tid = require_tenant(tenant_id)
        conditions = [
            KbTool.tenant_id == tid,
            KbTool.knowledge_base_id == knowledge_base_id,
            KbTool.is_deleted == 0,
        ]
        if enabled_only:
            conditions.append(KbTool.enabled.is_(True))
        if keyword:
            pattern = f"%{keyword.strip()}%"
            conditions.append(or_(KbTool.name.ilike(pattern), KbTool.tool_name.ilike(pattern)))
        items = (
            await self.session.execute(
                select(KbTool).where(*conditions)
                .order_by(KbTool.created_at.desc()).offset(offset).limit(limit)
            )
        ).scalars().all()
        total = (
            await self.session.execute(select(func.count()).select_from(KbTool).where(*conditions))
        ).scalar_one()
        return list(items), total

    async def get_by_name(
        self, tenant_id: str, knowledge_base_id: str, tool_name: str,
    ) -> KbTool | None:
        """库内未删除的同名工具（启停不在此过滤）。"""
        tid = require_tenant(tenant_id)
        result = await self.session.execute(
            select(KbTool).where(
                KbTool.tenant_id == tid,
                KbTool.knowledge_base_id == knowledge_base_id,
                KbTool.tool_name == tool_name,
                KbTool.is_deleted == 0,
            )
        )
        return result.scalar_one_or_none()

    async def list_by_names(
        self, tenant_id: str, knowledge_base_id: str, names: list[str], enabled_only: bool = True,
    ) -> list[KbTool]:
        tid = require_tenant(tenant_id)
        if not names:
            return []
        conditions = [
            KbTool.tenant_id == tid,
            KbTool.knowledge_base_id == knowledge_base_id,
            KbTool.tool_name.in_(sorted(set(names))),
            KbTool.is_deleted == 0,
        ]
        if enabled_only:
            conditions.append(KbTool.enabled.is_(True))
        return list((await self.session.execute(select(KbTool).where(*conditions))).scalars().all())


__all__ = ["KbToolRepository"]
