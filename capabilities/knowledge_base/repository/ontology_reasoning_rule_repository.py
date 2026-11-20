#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""推理规则仓储。

规则是 KB 级业务实体；查询一律带 tenant + is_deleted = 0。
"""
from typing import Optional

from sqlalchemy import select

from jonex_core.common.repository import BaseRepository
from jonex_core.common.tenant import require_tenant

from ..models.ontology_reasoning_rule import OntologyReasoningRule


class OntologyReasoningRuleRepository(BaseRepository[OntologyReasoningRule]):
    model = OntologyReasoningRule

    async def get_by_key(
        self, tenant_id: str, knowledge_base_id: str, rule_key: str,
    ) -> Optional[OntologyReasoningRule]:
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(select(OntologyReasoningRule).where(
            OntologyReasoningRule.tenant_id == tenant_id,
            OntologyReasoningRule.knowledge_base_id == knowledge_base_id,
            OntologyReasoningRule.rule_key == rule_key,
            OntologyReasoningRule.is_deleted == 0,
        ))
        return result.scalar_one_or_none()

    async def get_by_name(
        self, tenant_id: str, knowledge_base_id: str, name: str,
    ) -> Optional[OntologyReasoningRule]:
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(select(OntologyReasoningRule).where(
            OntologyReasoningRule.tenant_id == tenant_id,
            OntologyReasoningRule.knowledge_base_id == knowledge_base_id,
            OntologyReasoningRule.name == name,
            OntologyReasoningRule.is_deleted == 0,
        ))
        return result.scalar_one_or_none()

    async def list_by_kb(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        *,
        status: Optional[str] = None,
        enabled: Optional[bool] = None,
        offset: int = 0,
        limit: int = 500,
    ) -> list[OntologyReasoningRule]:
        extra = [OntologyReasoningRule.knowledge_base_id == knowledge_base_id]
        if status:
            extra.append(OntologyReasoningRule.status == status)
        if enabled is not None:
            extra.append(OntologyReasoningRule.enabled.is_(enabled))
        return await self.list_all(
            tenant_id=tenant_id, offset=offset, limit=limit, extra_conditions=extra,
        )

    async def list_active_enabled(
        self, tenant_id: str, knowledge_base_id: str,
    ) -> list[OntologyReasoningRule]:
        return await self.list_by_kb(
            tenant_id, knowledge_base_id, status="active", enabled=True, limit=10_000,
        )

    async def count_by_kb(
        self, tenant_id: str, knowledge_base_id: str,
        status: Optional[str] = None,
    ) -> int:
        extra = [OntologyReasoningRule.knowledge_base_id == knowledge_base_id]
        if status:
            extra.append(OntologyReasoningRule.status == status)
        return await self.count(tenant_id, extra_conditions=extra)


__all__ = ["OntologyReasoningRuleRepository"]
