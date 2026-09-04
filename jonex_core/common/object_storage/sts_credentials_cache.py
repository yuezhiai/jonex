#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""STS 临时密钥缓存（STS-02）。

按租户缓存 :class:`StsClient` 换取的临时密钥，过期前留提前量刷新，
异步安全 + 并发去重（多协程同时 miss 时只发一次 STS 请求）。

单进程内存缓存即可覆盖当前部署（多 worker 各自缓存，只多几次换证、不影响正确性）；
跨进程共享需 Redis，留到需要时再议。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from jonex_core.common import get_logger
from jonex_core.common.object_storage.sts_client import StsClient, StsCredentials

logger = get_logger("object_storage.sts_cache")

# 默认提前 30min 刷新（临时密钥有效期默认 2h）。
_DEFAULT_REFRESH_BEFORE_SECONDS = 1800


class StsCredentialsCache:
    """按租户缓存的临时密钥提供方（异步接口）。"""

    def __init__(
        self,
        sts_client: StsClient | None = None,
        *,
        refresh_before_seconds: int = _DEFAULT_REFRESH_BEFORE_SECONDS,
    ) -> None:
        self._sts = sts_client or StsClient()
        self._refresh_before = timedelta(seconds=refresh_before_seconds)
        self._cache: dict[str, StsCredentials] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _is_fresh(self, creds: StsCredentials, now: datetime) -> bool:
        """凭证剩余有效期是否超过提前量（即无需刷新）。"""
        return creds.expires_at - now > self._refresh_before

    async def get_credentials(self, tenant_id: str) -> StsCredentials:
        """获取租户的临时密钥：命中缓存直接返回，否则换证并缓存。

        并发去重：同一租户多协程同时 miss 时，通过 per-tenant 锁 + 双重检查
        保证只发一次 STS 请求。换证失败 fail-closed 抛 ``ServiceUnavailableError``。
        """
        now = datetime.now(timezone.utc)
        cached = self._cache.get(tenant_id)
        if cached is not None and self._is_fresh(cached, now):
            return cached

        lock = self._locks.setdefault(tenant_id, asyncio.Lock())
        async with lock:
            # 双重检查：等待锁期间可能有别的协程已完成换证。
            now = datetime.now(timezone.utc)
            cached = self._cache.get(tenant_id)
            if cached is not None and self._is_fresh(cached, now):
                return cached

            creds = await asyncio.to_thread(self._sts.get_federation_token, tenant_id)
            self._cache[tenant_id] = creds
            logger.info("STS 临时密钥已换取并缓存（租户: %s）", tenant_id)
            return creds

    def invalidate(self, tenant_id: str) -> None:
        """主动失效某租户的缓存（供密钥轮换 / 测试用）。

        同时清掉该租户的锁，避免 ``_locks`` 随历史租户无界增长。``_cache`` 与
        ``_locks`` 本身未加容量上限：单个锁对象开销极小，且租户集通常有界；
        若未来租户基数暴涨可再补一个 prune 上限逻辑。
        """
        self._cache.pop(tenant_id, None)
        self._locks.pop(tenant_id, None)


__all__ = ["StsCredentialsCache"]
