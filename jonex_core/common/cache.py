#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""
Jonex 平台 - 缓存连接模块

基于 Redis 异步客户端，支持：
- 异步 Redis 操作
- 分布式锁
- 连接池管理
- 重试机制
- 租户级缓存隔离
"""

import logging
import uuid
from functools import wraps
from typing import Any, Optional, Union

import redis
from redis import ConnectionError, TimeoutError, RedisError
from redis.asyncio import ConnectionPool, Redis

from jonex_core.common.config import get_config

logger = logging.getLogger(__name__)

config = get_config()


# ==================== 连接池管理 ====================
class RedisPoolManager:
    """Redis 连接池管理器"""
    _pool: Optional[ConnectionPool] = None

    @classmethod
    def get_pool(cls) -> ConnectionPool:
        """获取异步连接池（懒加载）"""
        if cls._pool is None:
            cls._pool = ConnectionPool(
                host=config.REDIS_HOST,
                port=config.REDIS_PORT,
                db=config.REDIS_DB,
                password=config.REDIS_PASSWORD,
                max_connections=config.REDIS_MAX_CONNECTIONS,
                socket_timeout=config.REDIS_SOCKET_TIMEOUT,
                socket_connect_timeout=config.REDIS_CONNECT_TIMEOUT,
                retry_on_timeout=True,
                health_check_interval=config.REDIS_HEALTH_CHECK_INTERVAL,
                decode_responses=config.REDIS_DECODE_RESPONSES,
            )
            logger.info(f"✅ Redis 连接池已初始化 (host={config.REDIS_HOST}:{config.REDIS_PORT})")
        return cls._pool

    @classmethod
    async def close_pool(cls):
        """关闭连接池"""
        if cls._pool is not None:
            await cls._pool.disconnect()
            cls._pool = None
            logger.info("✅ Redis 连接池已关闭")


def get_redis_client() -> Redis:
    """获取异步 Redis 客户端"""
    return Redis(connection_pool=RedisPoolManager.get_pool())


# ==================== 重试装饰器 ====================
def redis_retry(max_retries: int = 3, delay: float = 0.1):
    """
    Redis 操作重试装饰器

    Args:
        max_retries: 最大重试次数
        delay: 重试延迟基数（秒）
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except (ConnectionError, TimeoutError, RedisError) as e:
                    last_exception = e
                    logger.warning(
                        f"Redis 操作失败，尝试重试 ({attempt + 1}/{max_retries}): {e}"
                    )
                    if attempt < max_retries - 1:
                        import asyncio
                        await asyncio.sleep(delay * (attempt + 1))
            logger.error(f"Redis 操作最终失败: {last_exception}")
            raise last_exception
        return wrapper
    return decorator


# ==================== 缓存工具类 ====================
class CacheUtil:
    """Redis 缓存工具类（异步）"""

    # ==================== 基础操作 ====================
    @staticmethod
    @redis_retry(max_retries=3)
    async def ping() -> bool:
        """健康检查"""
        client = get_redis_client()
        try:
            return await client.ping()
        finally:
            await client.aclose()

    @staticmethod
    @redis_retry(max_retries=3)
    async def get(key: str) -> Optional[Any]:
        """获取值"""
        client = get_redis_client()
        try:
            return await client.get(key)
        finally:
            await client.aclose()

    @staticmethod
    @redis_retry(max_retries=3)
    async def set(key: str, value: Any, expire: Optional[int] = None) -> bool:
        """
        设置值

        Args:
            key: 键名
            value: 值
            expire: 过期时间（秒），None 表示不过期
        """
        client = get_redis_client()
        try:
            result = await client.set(name=key, value=value, ex=expire)
            return result is not None
        finally:
            await client.aclose()

    @staticmethod
    @redis_retry(max_retries=3)
    async def delete(key: str) -> int:
        """删除键"""
        client = get_redis_client()
        try:
            return await client.delete(key)
        finally:
            await client.aclose()

    @staticmethod
    @redis_retry(max_retries=3)
    async def exists(key: str) -> bool:
        """检查键是否存在"""
        client = get_redis_client()
        try:
            return await client.exists(key) > 0
        finally:
            await client.aclose()

    @staticmethod
    @redis_retry(max_retries=3)
    async def ttl(key: str) -> int:
        """获取剩余过期时间（秒）"""
        client = get_redis_client()
        try:
            return await client.ttl(key)
        finally:
            await client.aclose()

    @staticmethod
    @redis_retry(max_retries=3)
    async def expire(key: str, seconds: int) -> bool:
        """设置过期时间"""
        client = get_redis_client()
        try:
            return await client.expire(key, seconds)
        finally:
            await client.aclose()

    # ==================== Hash 操作 ====================
    @staticmethod
    @redis_retry(max_retries=3)
    async def hgetall(key: str) -> dict:
        """获取 Hash 所有字段"""
        client = get_redis_client()
        try:
            return await client.hgetall(key)
        finally:
            await client.aclose()

    @staticmethod
    @redis_retry(max_retries=3)
    async def hget(key: str, field: str) -> Optional[Any]:
        """获取 Hash 字段值"""
        client = get_redis_client()
        try:
            return await client.hget(key, field)
        finally:
            await client.aclose()

    @staticmethod
    @redis_retry(max_retries=3)
    async def hset(key: str, field: str, value: Any) -> int:
        """设置 Hash 字段值"""
        client = get_redis_client()
        try:
            return await client.hset(key, field, value)
        finally:
            await client.aclose()

    @staticmethod
    @redis_retry(max_retries=3)
    async def hdel(key: str, *fields: str) -> int:
        """删除 Hash 字段"""
        client = get_redis_client()
        try:
            return await client.hdel(key, *fields)
        finally:
            await client.aclose()

    @staticmethod
    @redis_retry(max_retries=3)
    async def hincrby(key: str, field: str, amount: int = 1) -> int:
        """Hash 字段原子递增"""
        client = get_redis_client()
        try:
            return await client.hincrby(key, field, amount)
        finally:
            await client.aclose()

    # ==================== Set 操作 ====================
    @staticmethod
    @redis_retry(max_retries=3)
    async def sadd(key: str, *members: Any) -> int:
        """向 Set 中添加成员"""
        client = get_redis_client()
        try:
            return await client.sadd(key, *members)
        finally:
            await client.aclose()

    @staticmethod
    @redis_retry(max_retries=3)
    async def smembers(key: str) -> set:
        """获取 Set 所有成员"""
        client = get_redis_client()
        try:
            return await client.smembers(key)
        finally:
            await client.aclose()

    @staticmethod
    @redis_retry(max_retries=3)
    async def srem(key: str, *members: Any) -> int:
        """删除 Set 成员"""
        client = get_redis_client()
        try:
            return await client.srem(key, *members)
        finally:
            await client.aclose()

    # ==================== 分布式锁 ====================
    @staticmethod
    @redis_retry(max_retries=3)
    async def acquire_lock(lock_key: str, lock_timeout: int) -> str:
        """
        获取 Redis 分布式锁

        Args:
            lock_key: 锁的键名
            lock_timeout: 锁超时时间（毫秒）

        Returns:
            锁的唯一标识（若失败返回空字符串）
        """
        lock_id = str(uuid.uuid4())
        client = get_redis_client()
        try:
            acquired = await client.set(
                lock_key,
                lock_id,
                nx=True,       # 仅当键不存在时设置
                px=lock_timeout  # 过期时间（毫秒）
            )
            return lock_id if acquired else ""
        finally:
            await client.aclose()

    @staticmethod
    @redis_retry(max_retries=3)
    async def release_lock(lock_key: str, lock_id: str) -> bool:
        """
        释放 Redis 分布式锁（使用 Lua 脚本保证原子性）

        Args:
            lock_key: 锁的键名
            lock_id: 锁的唯一标识

        Returns:
            是否释放成功
        """
        lua_script = """
           if redis.call("get", KEYS[1]) == ARGV[1] then
               return redis.call("del", KEYS[1])
           else
               return 0
           end
           """
        client = get_redis_client()
        try:
            result = await client.eval(lua_script, 1, lock_key, lock_id)
            return result == 1
        finally:
            await client.aclose()

    # ==================== 计数器 ====================
    @staticmethod
    @redis_retry(max_retries=3)
    async def incr(key: str, amount: int = 1) -> int:
        """原子递增"""
        client = get_redis_client()
        try:
            return await client.incr(key, amount)
        finally:
            await client.aclose()

    @staticmethod
    @redis_retry(max_retries=3)
    async def decr(key: str, amount: int = 1) -> int:
        """原子递减"""
        client = get_redis_client()
        try:
            return await client.decr(key, amount)
        finally:
            await client.aclose()

    # ==================== 批量操作 ====================
    @staticmethod
    @redis_retry(max_retries=3)
    async def mget(*keys: str) -> list:
        """批量获取"""
        client = get_redis_client()
        try:
            return await client.mget(*keys)
        finally:
            await client.aclose()

    @staticmethod
    @redis_retry(max_retries=3)
    async def mset(mapping: dict) -> bool:
        """批量设置"""
        client = get_redis_client()
        try:
            return await client.mset(mapping)
        finally:
            await client.aclose()


# ==================== 租户级缓存封装 ====================
class TenantCache:
    """租户级缓存工具，自动添加租户前缀"""

    def __init__(self, tenant_id: str):
        self.tenant_id = tenant_id
        self._prefix = f"tenant:{tenant_id}:"

    def _make_key(self, key: str) -> str:
        """构建带租户前缀的键"""
        return f"{self._prefix}{key}"

    async def get(self, key: str) -> Optional[Any]:
        return await CacheUtil.get(self._make_key(key))

    async def set(self, key: str, value: Any, expire: Optional[int] = None) -> bool:
        return await CacheUtil.set(self._make_key(key), value, expire)

    async def delete(self, key: str) -> int:
        return await CacheUtil.delete(self._make_key(key))

    async def exists(self, key: str) -> bool:
        return await CacheUtil.exists(self._make_key(key))

    async def acquire_lock(self, lock_key: str, lock_timeout: int) -> str:
        return await CacheUtil.acquire_lock(self._make_key(lock_key), lock_timeout)

    async def release_lock(self, lock_key: str, lock_id: str) -> bool:
        return await CacheUtil.release_lock(self._make_key(lock_key), lock_id)


# ==================== 快捷函数 ====================
async def check_redis_health() -> bool:
    """检查 Redis 连接健康状态"""
    try:
        return await CacheUtil.ping()
    except Exception as e:
        logger.error(f"Redis 健康检查失败: {e}")
        return False
