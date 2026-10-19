from typing import Optional, Sequence

from sqlalchemy import select, func

from capabilities.platform.models.system_config import SystemConfig
from capabilities.platform.repository.base import BaseRepository


class SystemConfigRepository(BaseRepository[SystemConfig]):
    model = SystemConfig

    async def get_by_key(self, config_key: str) -> Optional[SystemConfig]:
        result = await self.session.execute(
            select(SystemConfig).where(SystemConfig.config_key == config_key)
        )
        return result.scalar_one_or_none()

    async def list_by_group(self, group: str) -> Sequence[SystemConfig]:
        result = await self.session.execute(
            select(SystemConfig).where(SystemConfig.config_group == group)
        )
        return result.scalars().all()

    async def get_value(self, config_key: str, default: str = None) -> Optional[str]:
        cfg = await self.get_by_key(config_key)
        return cfg.config_value if cfg else default

    async def get_values(self, config_keys: Sequence[str]) -> dict:
        """按 key 批量取值（一次 `IN` 查询），返回 `{config_key: config_value}`。

        ⛔ 别用 `list_all_shared()` 代替：它默认 `limit=20`，配置表一旦超过 20 行就会
        **静默截断** —— 被截掉的 key 会悄悄回落默认值而不是报错，是最难查的一类问题。
        """
        keys = list(config_keys)
        if not keys:
            return {}
        result = await self.session.execute(
            select(SystemConfig.config_key, SystemConfig.config_value).where(
                SystemConfig.config_key.in_(keys)
            )
        )
        return {row.config_key: row.config_value for row in result}