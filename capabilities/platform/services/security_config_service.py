"""租户级安全配置：默认值的**唯一定义处** + 三层回落（租户覆盖 → 平台默认 → 代码默认）。

为什么要抽成独立模块
--------------------
改造前默认值写在 ``AuthService._get_security_config()`` 里（硬编码 30），而
``USER_JWT_EXPIRE_HOURS`` 又声明了一个 24 小时 —— **同一件事两处默认值**，
谁看代码都会先信后者，但实际生效的是前者。这类「改了配置没反应」的问题最难查。

现在默认值只有这里的 ``SECURITY_CONFIG_DEFAULTS``：登录读取链（``AuthService``）
与租户自助配置页（``SecurityConfigService``）都从它取，不可能再各说各话。

三层回落的语义
--------------
1. **租户覆盖**（``platform.tenant_security_configs`` 有行）→ 用它
2. **平台默认**（``platform.system_configs`` 有行）→ 用它
3. **代码默认**（``SECURITY_CONFIG_DEFAULTS``）→ 用它

⛔ 「没有行」与「配成和默认一样的值」必须可区分：前者删行即恢复默认。
"""

import logging
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from capabilities.platform.repository.system_config_repository import SystemConfigRepository
from capabilities.platform.repository.tenant_security_config_repository import (
    TenantSecurityConfigRepository,
)
from jonex_core.common.exceptions import InvalidParameterError
from jonex_core.common.i18n import translate

logger = logging.getLogger(__name__)

#: 租户可自助设置的 4 个安全键。键名与 ``platform.system_configs`` 完全同名 ——
#: 改名会让「平台默认」那一层读不到值，静默变成代码默认。
SECURITY_CONFIG_DEFAULTS: dict[str, dict[str, Any]] = {
    "session_timeout": {
        "default": 30,
        "min": 1,
        "max": 10080,  # 7 天：再长也没有意义，refresh token 本身就是 7 天
        "description": "访问令牌有效期（分钟）",
    },
    "password_min_length": {
        "default": 8,
        "min": 1,
        # bcrypt 只取前 72 字节，放开到 128 会让「设了 128 位」成为错觉
        "max": 72,
        "description": "密码最小长度",
    },
    "login_lock_threshold": {
        "default": 5,
        "min": 0,  # 0 = 不锁定，是既有语义，校验不能一刀切 ge=1
        "max": 100,
        "description": "连续登录失败锁定阈值（0=不锁定）",
    },
    "lock_duration": {
        "default": 15,
        "min": 1,
        "max": 1440,
        "description": "锁定时长（分钟）",
    },
}

SECURITY_CONFIG_KEYS = tuple(SECURITY_CONFIG_DEFAULTS)


def parse_security_int(raw: Any, fallback: int) -> int:
    """把库里的字符串按**既有语义**解析成 int。

    改造前是 ``int(float(raw))``：``"30.0"`` 能解析、空串/空白在调用前就被跳过。
    换实现不能改语义 —— 存量库里可能真躺着 ``"30.0"``。解析不了就用 ``fallback``
    （**不抛异常**：脏数据不能让登录挂掉）。
    """
    if raw is None:
        return fallback
    text = str(raw).strip()
    if not text:
        return fallback
    try:
        return int(float(text))
    except (TypeError, ValueError):
        logger.warning("安全配置值无法解析为整数，回落默认值: value=%r", raw)
        return fallback


class SecurityConfigService:
    """租户安全配置的读取（三层回落）与写入（校验 + upsert / 恢复默认）。"""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.tenant_repo = TenantSecurityConfigRepository(session)
        self.system_repo = SystemConfigRepository(session)

    # ── 读 ────────────────────────────────────────────────────────────

    async def _platform_layer(self) -> dict:
        """第 2 层：平台默认。**一次批量查**；读失败只告警、回落代码默认（登录不能被它阻断）。"""
        values = {key: meta["default"] for key, meta in SECURITY_CONFIG_DEFAULTS.items()}
        try:
            rows = await self.system_repo.get_values(SECURITY_CONFIG_KEYS)
            for key in SECURITY_CONFIG_DEFAULTS:
                if rows.get(key) is not None:
                    values[key] = parse_security_int(rows[key], values[key])
        except Exception:
            logger.warning("读取平台安全配置失败，回落代码默认值", exc_info=True)
        return values

    async def _tenant_layer(self, tenant_id: str) -> dict:
        """第 1 层：租户覆盖值。**一次批量查**；只返回真正有行的键（空值不算覆盖）。"""
        try:
            rows = await self.tenant_repo.get_values(tenant_id, SECURITY_CONFIG_KEYS)
        except Exception:
            # ⚠ 只让「租户层」失败，不要连带丢掉平台层 —— 平台管理员配的值仍然该生效
            logger.warning("读取租户安全配置失败，回落平台默认值: tenant=%s", tenant_id, exc_info=True)
            return {}
        overrides: dict = {}
        for key, raw in rows.items():
            # `key not in ...` 是**防御性**的：仓储按 IN(4 个键) 取，正常回不来别的键；
            # 留着是防以后有人把这句读改成「读全表」。真正会遇到的脏数据是空值/空白。
            if key not in SECURITY_CONFIG_DEFAULTS or raw is None or not str(raw).strip():
                continue
            overrides[key] = parse_security_int(raw, SECURITY_CONFIG_DEFAULTS[key]["default"])
        return overrides

    async def resolve(self, tenant_id: str) -> dict:
        """三层回落后的生效值（``{key: int}``）。登录 / refresh / 配置页共用（共 2 条查询）。"""
        effective = await self._platform_layer()
        effective.update(await self._tenant_layer(tenant_id))
        return effective

    async def list_effective(self, tenant_id: str) -> list:
        """配置页视图：生效值 + 平台默认 + 是否被本租户覆盖（共 2 条查询）。"""
        platform_values = await self._platform_layer()
        overrides = await self._tenant_layer(tenant_id)
        effective = {**platform_values, **overrides}
        return [
            self._item(key, effective, platform_values, key in overrides)
            for key in SECURITY_CONFIG_DEFAULTS
        ]

    @staticmethod
    def _item(key: str, effective: dict, platform_values: dict, is_overridden: bool) -> dict:
        meta = SECURITY_CONFIG_DEFAULTS[key]
        return {
            "config_key": key,
            "config_value": str(effective[key]),
            "value_type": "int",
            "description": meta["description"],
            "platform_value": str(platform_values[key]),
            "is_overridden": is_overridden,
            "min": meta["min"],
            "max": meta["max"],
        }

    # ── 写 ────────────────────────────────────────────────────────────

    def _validate(self, config_key: str, config_value: Any) -> int:
        meta = SECURITY_CONFIG_DEFAULTS.get(config_key)
        if meta is None:
            raise InvalidParameterError(
                message=translate(
                    "err.tenant_security.key_unknown",
                    params={"key": config_key},
                    fallback=f"不支持的安全配置项：{config_key}",
                )
            )
        text = "" if config_value is None else str(config_value).strip()
        try:
            parsed = int(float(text))
        except (TypeError, ValueError):
            raise InvalidParameterError(
                message=translate(
                    "err.tenant_security.value_not_int",
                    params={"key": config_key},
                    fallback=f"{config_key} 必须是整数",
                )
            )
        if not (meta["min"] <= parsed <= meta["max"]):
            raise InvalidParameterError(
                message=translate(
                    "err.tenant_security.value_out_of_range",
                    params={"key": config_key, "min": meta["min"], "max": meta["max"]},
                    fallback=f"{config_key} 必须在 {meta['min']}~{meta['max']} 之间",
                )
            )
        return parsed

    async def set_value(
        self,
        tenant_id: str,
        config_key: str,
        config_value: Any,
        description: Optional[str] = None,
    ) -> dict:
        """写入该租户的覆盖值（范围校验后 upsert）。返回写入后的配置页视图。"""
        parsed = self._validate(config_key, config_value)
        await self.tenant_repo.upsert(
            tenant_id,
            config_key,
            str(parsed),
            description or SECURITY_CONFIG_DEFAULTS[config_key]["description"],
        )
        return await self._single(tenant_id, config_key)

    async def reset_value(self, tenant_id: str, config_key: str) -> dict:
        """删除该租户的覆盖值 = 恢复平台默认。返回恢复后的配置页视图。"""
        if config_key not in SECURITY_CONFIG_DEFAULTS:
            raise InvalidParameterError(
                message=translate(
                    "err.tenant_security.key_unknown",
                    params={"key": config_key},
                    fallback=f"不支持的安全配置项：{config_key}",
                )
            )
        await self.tenant_repo.delete_key(tenant_id, config_key)
        return await self._single(tenant_id, config_key)

    async def _single(self, tenant_id: str, config_key: str) -> dict:
        for item in await self.list_effective(tenant_id):
            if item["config_key"] == config_key:
                return item
        # list_effective 覆盖全部键，走不到这里；留一个显式失败而不是返回 None
        raise InvalidParameterError(message=f"不支持的安全配置项：{config_key}")
