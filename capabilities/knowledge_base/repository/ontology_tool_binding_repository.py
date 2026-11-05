#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Repository for ontology instance → tool bindings."""

from sqlalchemy import func, select

from jonex_core.common.repository import BaseRepository
from jonex_core.common.tenant import require_tenant

from ..models.ontology_tool_binding import OntologyToolBinding


class OntologyToolBindingRepository(BaseRepository[OntologyToolBinding]):
    model = OntologyToolBinding

    async def list_by_kb(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        offset: int = 0,
        limit: int = 20,
        entity_type: str | None = None,
    ) -> tuple[list[OntologyToolBinding], int]:
        tid = require_tenant(tenant_id)
        conditions = [
            OntologyToolBinding.tenant_id == tid,
            OntologyToolBinding.knowledge_base_id == knowledge_base_id,
            OntologyToolBinding.is_deleted == 0,
        ]
        if entity_type:
            conditions.append(OntologyToolBinding.entity_type == entity_type)
        items = (
            await self.session.execute(
                select(OntologyToolBinding)
                .where(*conditions)
                .order_by(OntologyToolBinding.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
        ).scalars().all()
        total = (
            await self.session.execute(
                select(func.count()).select_from(OntologyToolBinding).where(*conditions)
            )
        ).scalar_one()
        return list(items), total

    async def get_active_for_entity(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        entity_type: str,
        canonical_name: str,
    ) -> OntologyToolBinding | None:
        """该实例当前的有效绑定（未软删；启停不在此过滤，由调用方判断）。"""
        tid = require_tenant(tenant_id)
        result = await self.session.execute(
            select(OntologyToolBinding).where(
                OntologyToolBinding.tenant_id == tid,
                OntologyToolBinding.knowledge_base_id == knowledge_base_id,
                OntologyToolBinding.entity_type == entity_type,
                OntologyToolBinding.canonical_name == canonical_name,
                OntologyToolBinding.is_deleted == 0,
            )
        )
        return result.scalar_one_or_none()

    async def list_for_entities(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        pairs: list[tuple[str, str]],
    ) -> list[OntologyToolBinding]:
        """按 (entity_type, canonical_name) 批量取有效绑定。"""
        tid = require_tenant(tenant_id)
        names = sorted({n for _, n in pairs if n})
        if not names:
            return []
        rows = (
            await self.session.execute(
                select(OntologyToolBinding).where(
                    OntologyToolBinding.tenant_id == tid,
                    OntologyToolBinding.knowledge_base_id == knowledge_base_id,
                    OntologyToolBinding.canonical_name.in_(names),
                    OntologyToolBinding.is_deleted == 0,
                )
            )
        ).scalars().all()
        wanted = set(pairs)
        return [b for b in rows if (b.entity_type, b.canonical_name) in wanted]


__all__ = ["OntologyToolBindingRepository"]
