#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Repository for KB 级模型覆盖（硬删语义）。"""

from sqlalchemy import delete, select

from jonex_core.common.repository import BaseRepository

from ..models.model_setting import KnowledgeModelSetting


class KnowledgeModelSettingRepository(BaseRepository[KnowledgeModelSetting]):
    model = KnowledgeModelSetting

    async def list_by_kb(self, tenant_id: str, knowledge_base_id: str) -> list[KnowledgeModelSetting]:
        result = await self.session.execute(
            select(KnowledgeModelSetting).where(
                KnowledgeModelSetting.tenant_id == tenant_id,
                KnowledgeModelSetting.knowledge_base_id == knowledge_base_id,
            )
        )
        return list(result.scalars())

    async def get_by_scope(
        self, tenant_id: str, knowledge_base_id: str, scope: str
    ) -> KnowledgeModelSetting | None:
        result = await self.session.execute(
            select(KnowledgeModelSetting).where(
                KnowledgeModelSetting.tenant_id == tenant_id,
                KnowledgeModelSetting.knowledge_base_id == knowledge_base_id,
                KnowledgeModelSetting.scope == scope,
            )
        )
        return result.scalars().first()

    async def upsert(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        scope: str,
        model_name: str,
        actor_user_id: str | None = None,
    ) -> KnowledgeModelSetting:
        """写入覆盖值：有行则更新，无行则新建。"""
        existing = await self.get_by_scope(tenant_id, knowledge_base_id, scope)
        if existing is None:
            return await self.create(
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                scope=scope,
                model_name=model_name,
                created_by=actor_user_id,
                updated_by=actor_user_id,
            )
        existing.model_name = model_name
        existing.updated_by = actor_user_id
        return existing

    async def delete_by_scope(
        self, tenant_id: str, knowledge_base_id: str, scope: str
    ) -> int:
        """硬删一条覆盖（"清空"即删行）。返回受影响行数。"""
        result = await self.session.execute(
            delete(KnowledgeModelSetting).where(
                KnowledgeModelSetting.tenant_id == tenant_id,
                KnowledgeModelSetting.knowledge_base_id == knowledge_base_id,
                KnowledgeModelSetting.scope == scope,
            )
        )
        return int(result.rowcount or 0)

    async def delete_by_kb(self, tenant_id: str, knowledge_base_id: str) -> int:
        """硬删该 KB 的全部覆盖（删除 KB 时级联）。"""
        result = await self.session.execute(
            delete(KnowledgeModelSetting).where(
                KnowledgeModelSetting.tenant_id == tenant_id,
                KnowledgeModelSetting.knowledge_base_id == knowledge_base_id,
            )
        )
        return int(result.rowcount or 0)


__all__ = ["KnowledgeModelSettingRepository"]
