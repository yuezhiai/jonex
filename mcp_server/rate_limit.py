#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""
MCP Server 限流中间件 — 基于 limits 库的异步 Redis 限流。

不使用 slowapi 内置中间件的原因：
  Starlette Mount 路由没有 endpoint 属性，slowapi 的 _find_route_handler
  返回 None → _should_exempt 返回 True → 所有 MCP tool 请求被豁免限流。

本中间件绕过路由内省，直接对除 /health 外的所有请求按 mcp_key_id 限流。
依赖 McpAuthMiddleware 先行鉴权并注入 McpAuthContext（key_id），
限流 key 从 contextvar 读取，不同 MCP Key 的计数独立。

纯 ASGI 协议实现（不使用 Starlette BaseHTTPMiddleware——与 Streamable HTTP 不兼容）。
"""
import json
import logging
import time

from limits.aio.storage import RedisStorage
from limits.aio.strategies import MovingWindowRateLimiter
from limits import parse as parse_limit
from starlette.types import ASGIApp, Receive, Scope, Send

from auth import McpAuthContext

logger = logging.getLogger("mcp.rate_limit")


class McpRateLimitMiddleware:
    """MCP Key 级限流 — 60 req/min/key，纯 ASGI。

    依赖 McpAuthMiddleware 先行注入 McpAuthContext（key_id），
    限流 key_func 从 contextvar 读取 key_id。
    /health 路径白名单，不消耗额度。
    """

    HEALTH_PATH = "/health"

    def __init__(
        self,
        app: ASGIApp,
        storage_uri: str,
        limit_str: str = "60/minute",
    ) -> None:
        self.app = app
        # 使用 redis.asyncio 而非 coredis（后者未安装）
        # limits.aio.storage.RedisStorage 默认 implementation="coredis"，
        # 必须显式指定 implementation="redispy" 以使用 redis.asyncio
        # limits 5.8.0 中 limits.aio.storage 没有 storage_from_string 函数，
        # 直接使用 RedisStorage 构造器（参数语义等价）

        # B4: 支持 rediss:// TLS Redis 连接
        # limits.aio.storage.RedisStorage 内部使用 redis.asyncio.from_url(storage_uri)
        # 但默认不启用 SSL——rediss:// 前缀不会被自动识别
        # 手动检测并创建 SSL Redis 客户端，注入 bridge.storage（bridge 是
        # RedispyBridge 实例，其 get_connection() 返回 self.storage，
        # 所有限流操作都经 bridge.storage 执行），并重新注册 Lua 脚本。
        if storage_uri.startswith("rediss://"):
            import redis.asyncio as aioredis

            ssl_redis = aioredis.from_url(storage_uri)
            self._storage = RedisStorage(
                "redis://" + storage_uri[9:], implementation="redispy"
            )
            self._storage.bridge.storage = ssl_redis
            self._storage.bridge.register_scripts()
        else:
            self._storage = RedisStorage(
                storage_uri, implementation="redispy"
            )

        self._limiter = MovingWindowRateLimiter(self._storage)
        self._limit = parse_limit(limit_str)

    def _get_key_id(self) -> str:
        """从 McpAuthContext contextvar 提取 key_id。

        McpAuthMiddleware 先行鉴权并注入 contextvar，
        此处读取用于限流 key。
        McpAuthContext.get() 返回 Optional[McpAuthContext]，非 None 时取 .key_id，
        为 None 时降级为 "unknown"。
        """
        ctx = McpAuthContext.get()
        if ctx is not None:
            return ctx.key_id
        # 兜底：鉴权中间件本应先执行并拒绝未认证请求，
        # 到达此处说明中间件顺序配置错误
        logger.warning("McpAuthContext 为空，限流降级为 unknown")
        return "unknown"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # 放行非 HTTP scope（lifespan 等）
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 放行 /health（docker healthcheck，不消耗限流额度）
        if scope["path"] == self.HEALTH_PATH:
            await self.app(scope, receive, send)
            return

        key_id = self._get_key_id()
        key = f"rate_limit:mcp:{key_id}"

        # MovingWindowRateLimiter.hit() returns True if allowed, False if limited
        # Redis 故障时降级放行（fail-open）并记录 error 日志，
        # 避免 Redis 不可达导致所有已认证请求返回 503
        try:
            allowed = await self._limiter.hit(self._limit, key)
        except Exception as exc:
            logger.error(
                "Redis 限流查询失败，降级放行 (key=%s): %s",
                key, exc,
            )
            await self.app(scope, receive, send)
            return
        if not allowed:
            # get_window_stats 返回 WindowStats NamedTuple (reset_time, remaining)
            # reset_time 是 Unix 时间戳，需换算为 retry_after 秒数
            try:
                stats = await self._limiter.get_window_stats(self._limit, key)
                retry_after = max(1, int(stats.reset_time - time.time())) if stats else 60
            except Exception:
                logger.warning("Redis get_window_stats 失败，使用默认 retry_after=60")
                retry_after = 60

            body = json.dumps({
                "jsonrpc": "2.0",
                "error": {
                    "code": -32000,
                    "message": f"Rate limit exceeded. Retry after {retry_after} seconds.",
                    "data": {"retry_after": retry_after},
                },
            }).encode("utf-8")

            await send({
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"retry-after", str(retry_after).encode("ascii")),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return

        # 未超限 → 放行
        await self.app(scope, receive, send)
