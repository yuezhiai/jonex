#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""KnowledgeDataSource 仓储。"""

from sqlalchemy import text

from jonex_core.common.repository import BaseRepository
from jonex_core.common.tenant import require_tenant

from ..models.data_source import KnowledgeDataSource


class KnowledgeDataSourceRepository(BaseRepository[KnowledgeDataSource]):
    model = KnowledgeDataSource

    async def list_by_kb(
        self, tenant_id: str, knowledge_base_id: str, offset: int = 0, limit: int = 100,
    ) -> list[KnowledgeDataSource]:
        return await self.list_all(
            tenant_id=tenant_id, offset=offset, limit=limit,
            extra_conditions=[KnowledgeDataSource.knowledge_base_id == knowledge_base_id],
        )

    async def count_by_kb(self, tenant_id: str, knowledge_base_id: str) -> int:
        return await self.count(
            tenant_id,
            extra_conditions=[KnowledgeDataSource.knowledge_base_id == knowledge_base_id],
        )

    async def list_referencing_kbs(self, tenant_id: str, connection_id: str) -> list[str]:
        """引用了该连接的**知识库名**（去重、按名排序）。

        「删一个仍被引用的连接」要返回 400 并列出库名（spec §3.4）——
        静默级联删会让人在某天发现某个库的数据源凭空消失。

        只算**活跃**引用，所以两道 is_deleted 都要查：
        - `ds.is_deleted = 0` —— 已删的引用行不该挡着删连接；
        - `ki.is_deleted = 0` —— 已删的知识库也不是活跃引用。`KnowledgeInfoService.delete`
          只软删 KB 自己那一行、不级联 `knowledge_data_sources`，而且没有恢复入口，
          所以漏掉这一条就等于让一个界面上根本不存在的库把连接**永久锁死**，
          400 里报出的名字用户还无处可寻。
        """
        # 仓储层统一租户校验：即便调用方漏传，也不能让非法租户走到「查不到 = 没人引用」
        # 这条**放行**的路上（那是删连接门禁的反面）。
        tenant_id = require_tenant(tenant_id)
        rows = (await self.session.execute(text(
            """
            SELECT DISTINCT ki.name
              FROM knowledge_base.knowledge_data_sources ds
              JOIN knowledge_base.knowledge_info ki
                ON ki.id = ds.knowledge_base_id
               AND ki.tenant_id = ds.tenant_id
             WHERE ds.tenant_id = :t
               AND ds.connection_id = :c
               AND ds.is_deleted = 0
               AND ki.is_deleted = 0
             ORDER BY ki.name
            """
        ), {"t": tenant_id, "c": connection_id})).scalars().all()
        # 不做「名字为空就跳过」的过滤：`ki.name` 是 NOT NULL（004 迁移），真库里不会
        # 出现无名行，那种过滤只可能滤掉空串——而空串仍是一个活着的引用方，
        # 滤掉它就是把这个门禁倒过来（漏一个引用方 = 它的连接被删掉）。
        return list(rows)


__all__ = ["KnowledgeDataSourceRepository"]
