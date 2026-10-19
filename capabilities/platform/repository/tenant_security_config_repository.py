"""租户级安全配置仓储（只存覆盖值，缺失即回落）。"""

from typing import Optional, Sequence

from sqlalchemy import select

from capabilities.platform.models.tenant_security_config import TenantSecurityConfig
from capabilities.platform.repository.base import BaseRepository


class TenantSecurityConfigRepository(BaseRepository[TenantSecurityConfig]):
    """``platform.tenant_security_configs`` 的数据访问。

    全部方法都**显式带 tenant_id**（不依赖 BaseRepository 的租户条件推断）：
    这张表的每次读写都必须落到某一个租户，漏了就是一个租户改到别人的配置。
    """

    model = TenantSecurityConfig

    async def get_values(self, tenant_id: str, config_keys: Sequence[str]) -> dict:
        """取该租户对这批键的覆盖值（一次 `IN` 查询），返回 `{config_key: config_value}`。

        **只返回真正有行的键** —— 「这里没有这个键」代表该租户未覆盖（回落平台默认），
        与「配成了和默认一样的值」不是一回事：前者删行即恢复默认。
        一次取全 4 个键而不是循环单查：登录路径上每个键一次往返 = 4 倍往返。
        """
        keys = list(config_keys)
        if not keys:
            return {}
        result = await self.session.execute(
            select(TenantSecurityConfig.config_key, TenantSecurityConfig.config_value).where(
                TenantSecurityConfig.tenant_id == tenant_id,
                TenantSecurityConfig.config_key.in_(keys),
            )
        )
        return {row.config_key: row.config_value for row in result}

    async def upsert(
        self,
        tenant_id: str,
        config_key: str,
        config_value: str,
        description: Optional[str] = None,
    ) -> TenantSecurityConfig:
        """按 (tenant_id, config_key) 新增或更新。

        走「先查后写」而不是 PostgreSQL 的 ``ON CONFLICT``：并发下理论上可能撞唯一键，
        但配置页是低频人工操作，撞了的后果是**一个 500 而不是脏数据**（唯一约束兜底），
        换取的是不依赖方言、且能被 ORM 正常 flush。
        """
        result = await self.session.execute(
            select(TenantSecurityConfig).where(
                TenantSecurityConfig.tenant_id == tenant_id,
                TenantSecurityConfig.config_key == config_key,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            row = TenantSecurityConfig(
                tenant_id=tenant_id,
                config_key=config_key,
                config_value=config_value,
                value_type="int",
                description=description,
            )
            self.session.add(row)
        else:
            row.config_value = config_value
            if description is not None:
                row.description = description
        await self.session.flush()
        return row

    async def delete_key(self, tenant_id: str, config_key: str) -> bool:
        """删除覆盖行（= 恢复默认）。返回是否真的存在过。

        硬删除而非软删除：这张表里「没有行」本身就是业务语义（未覆盖 → 回落），
        留一行 is_deleted=1 只会让「是否被自定义」的判定多一个容易写错的过滤条件。
        """
        result = await self.session.execute(
            select(TenantSecurityConfig).where(
                TenantSecurityConfig.tenant_id == tenant_id,
                TenantSecurityConfig.config_key == config_key,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            return False
        await self.session.delete(row)
        await self.session.flush()
        return True
