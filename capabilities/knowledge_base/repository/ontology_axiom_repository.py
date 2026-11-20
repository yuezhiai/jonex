#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""本体公理仓储。

公理是 KB 级数据；查询一律带 tenant + is_deleted = 0。引用解析用合并视图，
本仓储只负责表内读写。
"""
from typing import Optional

from sqlalchemy import select

from jonex_core.common.repository import BaseRepository
from jonex_core.common.tenant import require_tenant

from ..models.ontology_axiom import OntologyAxiom


class OntologyAxiomRepository(BaseRepository[OntologyAxiom]):
    model = OntologyAxiom

    async def get_by_key(
        self, tenant_id: str, knowledge_base_id: str, axiom_key: str,
    ) -> Optional[OntologyAxiom]:
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(select(OntologyAxiom).where(
            OntologyAxiom.tenant_id == tenant_id,
            OntologyAxiom.knowledge_base_id == knowledge_base_id,
            OntologyAxiom.axiom_key == axiom_key,
            OntologyAxiom.is_deleted == 0,
        ))
        return result.scalar_one_or_none()

    async def list_by_kb(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        *,
        status: Optional[str] = None,
        offset: int = 0,
        limit: int = 500,
    ) -> list[OntologyAxiom]:
        extra = [OntologyAxiom.knowledge_base_id == knowledge_base_id]
        if status:
            extra.append(OntologyAxiom.status == status)
        return await self.list_all(
            tenant_id=tenant_id, offset=offset, limit=limit, extra_conditions=extra,
        )

    async def list_active(
        self, tenant_id: str, knowledge_base_id: str,
    ) -> list[OntologyAxiom]:
        return await self.list_by_kb(
            tenant_id, knowledge_base_id, status="active", limit=10_000,
        )

    async def count_by_kb(
        self, tenant_id: str, knowledge_base_id: str,
        status: Optional[str] = None,
    ) -> int:
        extra = [OntologyAxiom.knowledge_base_id == knowledge_base_id]
        if status:
            extra.append(OntologyAxiom.status == status)
        return await self.count(tenant_id, extra_conditions=extra)


__all__ = ["OntologyAxiomRepository"]
