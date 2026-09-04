from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

from capabilities.platform.models.mcp_key import McpKey, McpKeyServiceMapping
from capabilities.platform.repository.base import BaseRepository
from jonex_core.common.exceptions import InvalidParameterError
from jonex_core.common.tenant import require_tenant


class McpKeyRepository(BaseRepository[McpKey]):
    model = McpKey

    async def list_by_tenant(self, tenant_id: str) -> list[McpKey]:
        """全量返回租户下所有 Key（含已撤销），按创建时间降序排列。

        不使用 limit/offset —— D-10 决策全量返回每租户至多几十条。
        不过滤 revoked_at —— 含已撤销 Key。
        过滤 is_deleted == 0 —— 已软删除 Key 不可见。
        """
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(
            select(McpKey)
            .where(
                McpKey.tenant_id == tenant_id,
                McpKey.is_deleted == 0,
            )
            .order_by(McpKey.created_at.desc())
        )
        return list(result.scalars().all())

    async def revoke(self, key_id: str, tenant_id: str, revoked_by: str | None = None) -> bool:
        """设置 revoked_at + revoked_by（审计归属）。返回 True 表示更新成功，False 表示 Key 不存在。

        幂等——对已撤销 Key（revoked_at 已有值）再次调用，SET 同值无副作用，rowcount 仍 > 0。
        时间用 Python datetime（async ORM 禁 func.now()，MissingGreenlet 陷阱）。
        """
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(
            update(McpKey)
            .where(McpKey.id == key_id, McpKey.tenant_id == tenant_id)
            .values(revoked_at=datetime.now(timezone.utc), revoked_by=revoked_by)
        )
        await self.session.flush()
        return result.rowcount > 0

    async def get_by_id_for_update(
        self, key_id: str, tenant_id: str
    ) -> Optional[McpKey]:
        """使用 SELECT ... FOR UPDATE 行锁查询 Key，返回 None 表示不存在或不属于该租户。

        必须在活跃事务内调用（session.begin() / session.begin_nested() 内部），
        否则 PostgreSQL 报错。Service 层 reset() 在事务内使用此方法获取行锁，
        防止并发 reset 读到同一旧 Key 产生重复行。
        """
        tenant_id = require_tenant(tenant_id)
        stmt = (
            select(McpKey)
            .where(McpKey.id == key_id, McpKey.tenant_id == tenant_id)
            .with_for_update()
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id_include_deleted(
        self, key_id: str, tenant_id: str
    ) -> Optional[McpKey]:
        """查询 Key（含已软删除记录），用于 delete 幂等检查。

        不过滤 is_deleted —— 已软删除 Key 仍可查回。
        用于 delete() 区分"从未存在"和"已删除"两种场景。
        """
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(
            select(McpKey).where(
                McpKey.id == key_id,
                McpKey.tenant_id == tenant_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_client_request_id(
        self, tenant_id: str, client_request_id: str
    ) -> Optional[McpKey]:
        """幂等命中预查：同 (tenant_id, client_request_id) 已存在则返回 McpKey，否则 None。

        不过滤 is_deleted / revoked_at —— 编号终身占用（软删/撤销后不复用），
        与部分唯一索引 uq_mcp_keys_tenant_reqid 语义一致。
        """
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(
            select(McpKey).where(
                McpKey.tenant_id == tenant_id,
                McpKey.client_request_id == client_request_id,
            )
        )
        return result.scalar_one_or_none()


class McpKeyServiceMappingRepository:
    """MCP Key ↔ 领域服务映射中间表仓储。

    无 BaseRepository 父类——该表无 tenant_id、无软删除，
    关联由应用层全量替换管理。
    """

    _VALID_PERMISSION_LEVELS = frozenset({"call", "view"})

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_mappings(self, key_id: str) -> list[tuple[str, str]]:
        """查询某个 MCP Key 关联的所有 (service_id, permission_level) 对。"""
        result = await self.session.execute(
            select(
                McpKeyServiceMapping.service_id,
                McpKeyServiceMapping.permission_level,
            ).where(
                McpKeyServiceMapping.mcp_key_id == key_id,
            )
        )
        return [(row[0], row[1]) for row in result.all()]

    async def get_service_ids(self, key_id: str) -> list[str]:
        """查询某个 MCP Key 关联的所有 service_id（向后兼容）。"""
        result = await self.session.execute(
            select(McpKeyServiceMapping.service_id).where(
                McpKeyServiceMapping.mcp_key_id == key_id,
            )
        )
        return [row[0] for row in result.all()]

    async def get_mcp_key_ids(self, service_id: str) -> list[str]:
        """按 Service 查询关联的所有 mcp_key_id（给 B2 前端筛选用）。"""
        result = await self.session.execute(
            select(McpKeyServiceMapping.mcp_key_id).where(
                McpKeyServiceMapping.service_id == service_id,
            )
        )
        return [row[0] for row in result.all()]

    async def get_mappings_batch(
        self, key_ids: list[str]
    ) -> dict[str, list[tuple[str, str]]]:
        """批量查询：一次 SQL 获取所有 key 的 (service_id, permission_level)。"""
        if not key_ids:
            return {}
        result = await self.session.execute(
            select(
                McpKeyServiceMapping.mcp_key_id,
                McpKeyServiceMapping.service_id,
                McpKeyServiceMapping.permission_level,
            ).where(McpKeyServiceMapping.mcp_key_id.in_(key_ids))
        )
        mapping: dict[str, list[tuple[str, str]]] = {k: [] for k in key_ids}
        for row in result.all():
            mapping.setdefault(row[0], []).append((row[1], row[2]))
        return mapping

    async def get_service_ids_batch(self, key_ids: list[str]) -> dict[str, list[str]]:
        """批量查询 service_ids（向后兼容）。"""
        if not key_ids:
            return {}
        result = await self.session.execute(
            select(
                McpKeyServiceMapping.mcp_key_id,
                McpKeyServiceMapping.service_id,
            ).where(McpKeyServiceMapping.mcp_key_id.in_(key_ids))
        )
        mapping: dict[str, list[str]] = {k: [] for k in key_ids}
        for row in result.all():
            mapping.setdefault(row[0], []).append(row[1])
        return mapping

    async def set_mappings(
        self, key_id: str, mappings: list[str] | list[tuple[str, str]]
    ) -> None:
        """全量替换映射行：先删旧 → 再写新。在事务内调用。

        支持两种入参格式：
        - list[str]: 每条 permission_level 默认 'call'（向后兼容）
        - list[tuple[str, str]]: (service_id, permission_level) 对
        """
        await self.session.execute(
            delete(McpKeyServiceMapping).where(
                McpKeyServiceMapping.mcp_key_id == key_id,
            )
        )
        for item in mappings:
            if isinstance(item, str):
                sid = item
                pl = "call"
            else:
                sid, pl = item
            self.session.add(
                McpKeyServiceMapping(
                    mcp_key_id=key_id, service_id=sid, permission_level=pl
                )
            )
        await self.session.flush()

    async def add_mapping(
        self, key_id: str, service_id: str, permission_level: str = "call"
    ) -> None:
        """添加单条 Key↔Service 映射。存在冲突（同 key_id+service_id）则更新 permission_level。"""
        if permission_level not in self._VALID_PERMISSION_LEVELS:
            raise InvalidParameterError(
                f"无效的权限级别: {permission_level!r}，"
                f"必须为 {sorted(self._VALID_PERMISSION_LEVELS)} 之一"
            )
        from sqlalchemy import insert as sa_insert
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = (
            pg_insert(McpKeyServiceMapping)
            .values(
                mcp_key_id=key_id,
                service_id=service_id,
                permission_level=permission_level,
            )
            .on_conflict_do_update(
                constraint="mcp_key_service_mappings_mcp_key_id_service_id_key",
                set_={"permission_level": permission_level},
            )
        )
        await self.session.execute(stmt)
        await self.session.flush()

    async def remove_mapping(self, key_id: str, service_id: str) -> bool:
        """删除单条映射，返回 True 表示删除成功。"""
        from sqlalchemy import delete as sa_delete

        result = await self.session.execute(
            sa_delete(McpKeyServiceMapping).where(
                McpKeyServiceMapping.mcp_key_id == key_id,
                McpKeyServiceMapping.service_id == service_id,
            )
        )
        await self.session.flush()
        return result.rowcount > 0

    async def update_permission(
        self, key_id: str, service_id: str, permission_level: str
    ) -> bool:
        """更新单条映射的 permission_level。返回 True 表示更新成功。"""
        if permission_level not in self._VALID_PERMISSION_LEVELS:
            raise InvalidParameterError(
                f"无效的权限级别: {permission_level!r}，"
                f"必须为 {sorted(self._VALID_PERMISSION_LEVELS)} 之一"
            )
        from sqlalchemy import update as sa_update

        result = await self.session.execute(
            sa_update(McpKeyServiceMapping)
            .where(
                McpKeyServiceMapping.mcp_key_id == key_id,
                McpKeyServiceMapping.service_id == service_id,
            )
            .values(permission_level=permission_level)
        )
        await self.session.flush()
        return result.rowcount > 0

    async def get_permission_level(
        self, key_id: str, service_id: str
    ) -> str | None:
        """查询单条映射的 permission_level。返回 None 表示映射不存在。"""
        result = await self.session.execute(
            select(McpKeyServiceMapping.permission_level).where(
                McpKeyServiceMapping.mcp_key_id == key_id,
                McpKeyServiceMapping.service_id == service_id,
            )
        )
        row = result.first()
        return row[0] if row else None
