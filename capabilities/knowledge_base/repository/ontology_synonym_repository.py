#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Repository for KB-level ontology synonyms."""

from sqlalchemy import func, select

from jonex_core.common.repository import BaseRepository
from jonex_core.common.tenant import require_tenant

from ..models.ontology_synonym import OntologySynonym


class OntologySynonymRepository(BaseRepository[OntologySynonym]):
    model = OntologySynonym

    async def list_by_kb(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[OntologySynonym], int]:
        tid = require_tenant(tenant_id)
        conditions = [
            OntologySynonym.tenant_id == tid,
            OntologySynonym.knowledge_base_id == knowledge_base_id,
            OntologySynonym.is_deleted == 0,
        ]
        items = (
            await self.session.execute(
                select(OntologySynonym)
                .where(*conditions)
                # id DESC 作 tiebreaker：created_at 同值时（批量导入同秒写入）
                # OFFSET/LIMIT 分页否则会漏行/重行。快照也对顺序敏感。
                .order_by(OntologySynonym.created_at.desc(),
                          OntologySynonym.id.desc())
                .offset(offset)
                .limit(limit)
            )
        ).scalars().all()
        total = (
            await self.session.execute(
                select(func.count()).select_from(OntologySynonym).where(*conditions)
            )
        ).scalar_one()
        return list(items), total

    async def list_all_by_kb(
        self,
        tenant_id: str,
        knowledge_base_id: str,
    ) -> list[OntologySynonym]:
        """取该 KB 全部未删组（不分页），用于跨组唯一校验。"""
        tid = require_tenant(tenant_id)
        result = await self.session.execute(
            select(OntologySynonym).where(
                OntologySynonym.tenant_id == tid,
                OntologySynonym.knowledge_base_id == knowledge_base_id,
                OntologySynonym.is_deleted == 0,
            )
        )
        return list(result.scalars())


__all__ = ["OntologySynonymRepository"]
