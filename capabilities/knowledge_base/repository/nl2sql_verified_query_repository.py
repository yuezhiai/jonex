#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Nl2SqlVerifiedQuery 仓储（N1-1）。

所有查询都显式带 `tenant_id` + `knowledge_base_id`（+ 可选 `data_source_id`）：
「按 id 取」也带 KB 条件，避免拿别的 KB 的 id 越权读到行。写操作一律走
`version` 乐观锁 CAS；冲突统一映射成 `ResourceConflictError(1003)`，与抽象 schema
仓储同口径。
"""
from typing import Iterable, Optional

from sqlalchemy import select, update

from jonex_core.common.exceptions import ResourceConflictError
from jonex_core.common.repository import BaseRepository
from jonex_core.common.tenant import require_tenant

from ..models.nl2sql_verified_query import Nl2SqlVerifiedQuery


def _stale_version_error(expected: int, actual: Optional[int]) -> ResourceConflictError:
    actual_text = actual if actual is not None else "?"
    return ResourceConflictError(
        message=f"已验证查询已被他人修改（当前 v{actual_text}），请刷新后重试",
        details={"expected_version": expected, "actual_version": actual},
    )


class Nl2SqlVerifiedQueryRepository(BaseRepository[Nl2SqlVerifiedQuery]):
    model = Nl2SqlVerifiedQuery

    async def list_for_kb(self, tenant_id: str, knowledge_base_id: str,
                          data_source_id: str = "") -> list[Nl2SqlVerifiedQuery]:
        tid = require_tenant(tenant_id)
        stmt = select(Nl2SqlVerifiedQuery).where(
            Nl2SqlVerifiedQuery.tenant_id == tid,
            Nl2SqlVerifiedQuery.knowledge_base_id == knowledge_base_id,
            Nl2SqlVerifiedQuery.is_deleted == 0,
        )
        if data_source_id:
            stmt = stmt.where(Nl2SqlVerifiedQuery.data_source_id == data_source_id)
        stmt = stmt.order_by(Nl2SqlVerifiedQuery.updated_at.desc(),
                             Nl2SqlVerifiedQuery.name)
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_active(self, tenant_id: str, knowledge_base_id: str,
                          data_source_id: str) -> list[Nl2SqlVerifiedQuery]:
        tid = require_tenant(tenant_id)
        stmt = select(Nl2SqlVerifiedQuery).where(
            Nl2SqlVerifiedQuery.tenant_id == tid,
            Nl2SqlVerifiedQuery.knowledge_base_id == knowledge_base_id,
            Nl2SqlVerifiedQuery.data_source_id == data_source_id,
            Nl2SqlVerifiedQuery.status == "active",
            Nl2SqlVerifiedQuery.is_deleted == 0,
        ).order_by(Nl2SqlVerifiedQuery.name)
        return list((await self.session.execute(stmt)).scalars().all())

    async def get(self, tenant_id: str, knowledge_base_id: str,
                  verified_query_id: str) -> Optional[Nl2SqlVerifiedQuery]:
        tid = require_tenant(tenant_id)
        stmt = select(Nl2SqlVerifiedQuery).where(
            Nl2SqlVerifiedQuery.tenant_id == tid,
            Nl2SqlVerifiedQuery.knowledge_base_id == knowledge_base_id,
            Nl2SqlVerifiedQuery.id == verified_query_id,
            Nl2SqlVerifiedQuery.is_deleted == 0,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_name(self, tenant_id: str, knowledge_base_id: str,
                          data_source_id: str,
                          name: str) -> Optional[Nl2SqlVerifiedQuery]:
        tid = require_tenant(tenant_id)
        stmt = select(Nl2SqlVerifiedQuery).where(
            Nl2SqlVerifiedQuery.tenant_id == tid,
            Nl2SqlVerifiedQuery.knowledge_base_id == knowledge_base_id,
            Nl2SqlVerifiedQuery.data_source_id == data_source_id,
            Nl2SqlVerifiedQuery.name == name,
            Nl2SqlVerifiedQuery.is_deleted == 0,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def add_row(self, row: Nl2SqlVerifiedQuery) -> Nl2SqlVerifiedQuery:
        self.session.add(row)
        await self.session.flush()
        return row

    async def _cas_update(self, tenant_id: str, knowledge_base_id: str,
                          verified_query_id: str, expected_version: int,
                          values: dict) -> Optional[Nl2SqlVerifiedQuery]:
        """`version = expected` 的 CAS 更新；命中返回刷新后的行，冲突抛 1003。"""
        tid = require_tenant(tenant_id)
        result = await self.session.execute(
            update(Nl2SqlVerifiedQuery)
            .where(
                Nl2SqlVerifiedQuery.tenant_id == tid,
                Nl2SqlVerifiedQuery.knowledge_base_id == knowledge_base_id,
                Nl2SqlVerifiedQuery.id == verified_query_id,
                Nl2SqlVerifiedQuery.is_deleted == 0,
                Nl2SqlVerifiedQuery.version == expected_version,
            )
            .values(**values, version=expected_version + 1)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            current = await self.get(tenant_id, knowledge_base_id, verified_query_id)
            raise _stale_version_error(
                expected_version, current.version if current else None)
        # ⚠️ 必须 `populate_existing`：同会话里先前 select 过的 ORM 对象还在 identity map，
        # 普通 `select` 会把**旧值**（如 status=draft）原样返回 —— 真机冒烟抓到过一次
        # （DB 已是 active，接口却回 draft）。
        return await self._get_fresh(tenant_id, knowledge_base_id, verified_query_id)

    async def _get_fresh(self, tenant_id: str, knowledge_base_id: str,
                         verified_query_id: str) -> Optional[Nl2SqlVerifiedQuery]:
        tid = require_tenant(tenant_id)
        stmt = select(Nl2SqlVerifiedQuery).where(
            Nl2SqlVerifiedQuery.tenant_id == tid,
            Nl2SqlVerifiedQuery.knowledge_base_id == knowledge_base_id,
            Nl2SqlVerifiedQuery.id == verified_query_id,
            Nl2SqlVerifiedQuery.is_deleted == 0,
        ).execution_options(populate_existing=True)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def update_fields(self, tenant_id: str, knowledge_base_id: str,
                            verified_query_id: str, expected_version: int,
                            fields: dict) -> Optional[Nl2SqlVerifiedQuery]:
        return await self._cas_update(
            tenant_id, knowledge_base_id, verified_query_id, expected_version,
            dict(fields))

    async def set_status(self, tenant_id: str, knowledge_base_id: str,
                         verified_query_id: str, expected_version: int,
                         status: str, *, actor: str = "",
                         activated_at=None) -> Optional[Nl2SqlVerifiedQuery]:
        values = {"status": status, "updated_by": actor}
        if status == "active":
            values["activated_by"] = actor
            values["activated_at"] = activated_at
        return await self._cas_update(
            tenant_id, knowledge_base_id, verified_query_id, expected_version, values)

    async def soft_delete(self, tenant_id: str, knowledge_base_id: str,
                          verified_query_id: str,
                          expected_version: int) -> None:
        await self._cas_update(
            tenant_id, knowledge_base_id, verified_query_id, expected_version,
            {"is_deleted": 1})


__all__ = ["Nl2SqlVerifiedQueryRepository"]
