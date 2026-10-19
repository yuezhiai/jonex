from typing import Mapping, Optional, Sequence

from sqlalchemy import or_, select

from capabilities.platform.models.tenant import Tenant
from capabilities.platform.repository.base import BaseRepository


class TenantRepository(BaseRepository[Tenant]):
    model = Tenant

    @staticmethod
    def keyword_conditions(keyword: str) -> list:
        """关键字条件：**租户 ID / 名称 / 描述**命中其一即算（大小写不敏感子串）。

        与 task_center / skill 的关键字检索同风格（`ilike '%kw%'`，不做通配符转义）——
        用户输入的 `%`/`_` 当通配符处理，这里不额外转义，与仓库既有检索保持一致。
        带上 `description` 是为了不丢能力：改服务端检索之前，前端本地过滤就是这三列，
        只搜 ID + 名称会让「按描述找租户」静默失效。`description` 可空，NULL 走 ILIKE
        结果是 NULL，自然落到「不命中」，不用额外判空。
        空关键字返回空列表（= 不过滤），交给 `list_all_shared` / `count_shared` 直接展开。
        """
        if not keyword:
            return []
        pattern = f"%{keyword}%"
        return [
            or_(
                Tenant.id.ilike(pattern),
                Tenant.name.ilike(pattern),
                Tenant.description.ilike(pattern),
            )
        ]

    async def list_by_keyword(self, offset: int, limit: int, keyword: str = "") -> Sequence[Tenant]:
        """分页查租户，`keyword` 非空时按 ID / 名称 / 描述过滤（分页与过滤都在库里做）。

        为什么要下沉到 SQL：列表接口原先没有关键字参数，前端只能「拉前 100 条 + 本地过滤」，
        第 101 个之后的租户搜不出来，页面上还得挂一条「搜索范围受限」的提示。
        """
        return await self.list_all_shared(offset, limit, extra_conditions=self.keyword_conditions(keyword))

    async def count_by_keyword(self, keyword: str = "") -> int:
        """`keyword` 口径必须与 `list_by_keyword` 完全一致，否则分页器总数与列表对不上。"""
        return await self.count_shared(extra_conditions=self.keyword_conditions(keyword))

    async def get_including_deleted_by_id(self, id_val: str) -> Optional[Tenant]:
        """查指定 id 的租户（含软删除），供创建时复用已软删的同 id 记录。

        软删除只是把 is_deleted 置 1，主键 id 仍占用；创建前若只按未删除口径
        查重（get_by_id_shared）会漏判，insert 撞 tenants_pkey。这里补查软删记录，
        让 create 走「恢复复用」而不是报 500。
        """
        result = await self.session.execute(
            select(Tenant).where(Tenant.id == id_val)
        )
        return result.scalar_one_or_none()

    async def list_active_by_ids(self, tenant_ids: Sequence[str]) -> Mapping[str, Tenant]:
        if not tenant_ids:
            return {}

        result = await self.session.execute(
            select(Tenant).where(
                Tenant.id.in_(tenant_ids),
                Tenant.status == 1,
                Tenant.is_deleted == 0,
            )
        )
        tenants = result.scalars().all()
        return {tenant.id: tenant for tenant in tenants}
