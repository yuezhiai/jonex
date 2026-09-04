"""
Sidecar 横切关注点 hook — 限流、计量、熔断、审计采集（占位实现）。

默认均为 no-op，通过配置开启：
- RATE_LIMIT_ENABLED / METERING_ENABLED / CIRCUIT_BREAKER_ENABLED / AUDIT_LOG_ENABLED
"""

import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Optional

import redis.asyncio as aioredis

import httpx

from jonex_core.common.config import get_config
from jonex_core.security.internal_auth import get_internal_auth

from jonex_core.common.audit_enums import ResourceType, _ACTION_TO_RESOURCE

# 审计 action 枚举 & 标签词典，用于验证 collect() 写入的 action 值
try:
    from capabilities.platform.models.audit_enums import AuditAction, _LABEL_ZH

    # 缓存 AuditAction 已知值集合（枚举成员不变化，无需重复构建列表）
    _AUDIT_ACTION_VALUES: frozenset = frozenset(m.value for m in AuditAction)
except ImportError:
    AuditAction = None
    _LABEL_ZH = None
    _AUDIT_ACTION_VALUES = frozenset()
logger = logging.getLogger(__name__)


class RateLimiter:
    """限流器 — 基于 redis.asyncio 的租户级 Fixed Window 实现"""

    KEY_PREFIX = "rate_limit:tenant"

    def __init__(self):
        self.config = get_config()
        self._redis: aioredis.Redis | None = None
        self._pool: aioredis.ConnectionPool | None = None

    async def _get_redis(self) -> aioredis.Redis:
        """Lazy init Redis 连接池与客户端"""
        if self._redis is None:
            redis_url = self.config.REDIS_URL or "redis://redis:6379/0"
            self._pool = aioredis.ConnectionPool.from_url(
                redis_url,
                max_connections=self.config.REDIS_MAX_CONNECTIONS,
                decode_responses=True,
                socket_connect_timeout=self.config.REDIS_CONNECT_TIMEOUT,
                socket_keepalive=True,
            )
            self._redis = aioredis.Redis.from_pool(self._pool)
            logger.info("RateLimiter Redis 连接池已初始化: %s", redis_url)
        return self._redis

    async def check(self, tenant_id: str, api_path: str, user_id: Optional[str] = None) -> tuple[bool, int]:
        """
        检查是否允许请求。返回 (allowed, retry_after_seconds)。

        Fixed Window 算法：同一分钟窗口内统计 tenant_id 维度请求数，
        超过 RATE_LIMIT_TENANT_PER_MINUTE 阈值时拒绝。

        api_path 和 user_id 保留签名兼容性，不在限流 key 中使用。

        Redis 故障时降级放行（fail-open）：记录 ERROR 日志并返回 (True, 0)，
        避免 Redis 不可达导致全站 500。
        """
        if not self.config.RATE_LIMIT_ENABLED:
            return (True, 0)

        limit = self.config.RATE_LIMIT_TENANT_PER_MINUTE
        window = int(time.time() / 60)
        key = f"{self.KEY_PREFIX}:{tenant_id}:invoke:{window}"

        try:
            redis = await self._get_redis()
            current = await redis.incr(key)
            if current == 1:
                await redis.expire(key, 120)
        except Exception as exc:
            logger.error(
                "RateLimiter Redis 操作失败，降级放行: tenant=%s key=%s error=%s",
                tenant_id, key, exc,
            )
            return (True, 0)

        retry_after = 60 - int(time.time()) % 60
        allowed = current <= limit

        if not allowed:
            logger.warning(
                "RateLimiter 触发: tenant=%s window=%s count=%d limit=%d",
                tenant_id, window, current, limit,
            )

        return (allowed, retry_after)

    async def close(self) -> None:
        """释放 Redis 连接池与客户端"""
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None
            self._pool = None
            logger.info("RateLimiter Redis 连接池已关闭")


class MeteringCollector:
    """计量收集器 — 记录请求耗时、状态码，默认 no-op"""

    def __init__(self):
        self.config = get_config()

    async def record(
        self,
        tenant_id: str,
        api_path: str,
        latency_ms: float,
        status_code: int,
        user_id: Optional[str] = None,
    ):
        """
        记录一次请求的计量数据。

        占位实现：仅 DEBUG 日志。
        TODO: 写入时序数据库或消息队列。
        """
        if not self.config.METERING_ENABLED:
            return
        logger.debug(
            f"[Metering] tenant={tenant_id} api={api_path} "
            f"latency={latency_ms:.0f}ms status={status_code}"
        )


class CircuitBreaker:
    """熔断器 — 按能力服务跟踪失败计数，默认 no-op"""

    def __init__(self):
        self.config = get_config()
        self._failure_counts: dict[str, int] = {}
        self._state: dict[str, str] = {}  # "closed" | "open" | "half_open"

    async def before_call(self, service_name: str) -> bool:
        """
        调用前检查熔断状态。返回 True 表示允许调用。

        占位实现：始终放行。
        TODO: Redis 计数器 + 半开探测。
        """
        if not self._enabled:
            return True
        state = self._state.get(service_name, "closed")
        if state == "open":
            logger.warning(f"[CircuitBreaker] 熔断器开路，拒绝调用: {service_name}")
            return False
        return True

    async def on_success(self, service_name: str):
        """调用成功时重置失败计数"""
        if not self._enabled:
            return
        self._failure_counts[service_name] = 0
        self._state[service_name] = "closed"

    async def on_failure(self, service_name: str):
        """调用失败时累加计数，达到阈值时熔断"""
        if not self._enabled:
            return
        count = self._failure_counts.get(service_name, 0) + 1
        self._failure_counts[service_name] = count
        if count >= self.config.CIRCUIT_BREAKER_THRESHOLD:
            self._state[service_name] = "open"
            logger.warning(
                f"[CircuitBreaker] 熔断器触发: {service_name} "
                f"(连续失败 {count} 次)"
            )

    @property
    def _enabled(self) -> bool:
        return getattr(self.config, "CIRCUIT_BREAKER_ENABLED", False)


class AuditForwarder:
    """审计日志采集转发器

    在 Sidecar 采集 HTTP 关键动作的审计信息，内存批量缓冲后通过
    内部鉴权 POST 到 platform 的 ingest 接口。
    """

    # 默认不采集的 HTTP 方法（只采写操作）
    _AUDIT_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

    # 排除路径前缀
    _EXCLUDED_PREFIXES = ("auth/", "health")

    _BASE_BACKOFF = 1.0       # 初始退避 1 秒
    _MAX_BACKOFF = 60.0       # 最大退避 60 秒

    def __init__(self):
        self.config = get_config()
        self._buffer: List[Dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self._bg_task: Optional[asyncio.Task] = None
        self._running = False
        self._consecutive_failures = 0
        self._next_retry_at: float = 0.0
        # 关键动作关键字（小写），用于过滤 invoke 能力调用
        self._key_action_keywords = [
            kw.strip().lower()
            for kw in getattr(self.config, "AUDIT_KEY_ACTION_KEYWORDS", "").split(",")
            if kw.strip()
        ]

    def _is_key_action(self, action: Optional[str]) -> bool:
        """判断 invoke 的 payload.action 是否为关键行为（创建/修改/删除等）。

        按分隔符（_ - . 空格）切分为 token 后匹配关键字（精确或前缀），
        兼容 batch_delete / update_status / create_xxx 等命名；
        同时避免 preview 误命中 review 这类子串假阳性。
        读类动作（list/get/search/query/preview/detail/export/stats）不命中即被过滤。
        """
        if not action:
            return False
        tokens = re.split(r"[_\-.\s]+", action.lower())
        return any(
            tok == kw or tok.startswith(kw)
            for tok in tokens
            for kw in self._key_action_keywords
        )

    def _is_collectable(
        self,
        method: str,
        path: str,
        invoke_action: Optional[str] = None,
        is_invoke: bool = False,
    ) -> bool:
        """判断是否应采集该请求"""
        if not self.config.AUDIT_LOG_ENABLED:
            return False
        normalized = path.lstrip("/")
        for prefix in self._EXCLUDED_PREFIXES:
            if normalized.startswith(prefix):
                return False
        # invoke 能力调用：按关键动作过滤（只记录创建/修改/删除等；无 action 不采）
        if is_invoke:
            return self._is_key_action(invoke_action)
        # REST 代理（平台 CRUD 等）：按写方法过滤
        if method not in self._AUDIT_METHODS:
            return False
        return True

    async def collect(
        self,
        tenant_id: str,
        method: str,
        path: str,
        status_code: int,
        latency_ms: float,
        trace_id: str = "",
        user_id: Optional[str] = None,
        username: Optional[str] = None,
        ip: Optional[str] = None,
        service_name: str = "sidecar",
        request_params: Optional[Dict] = None,
        response_body: Optional[Dict] = None,
        invoke_action: Optional[str] = None,
        is_invoke: bool = False,
        resource_id: Optional[str] = None,
        impersonated: bool = False,
        original_tenant_id: Optional[str] = None,
    ):
        """采集一条审计条目

        内存缓冲，满足以下任一条件时触发批量推送：
        - 缓冲达到 AUDIT_FLUSH_BATCH_SIZE
        - 距上次推送超过 AUDIT_FLUSH_INTERVAL_MS

        Args:
            invoke_action: 能力调用的 payload.action（仅 /invoke 采集点传入），
                用于过滤非关键行为。
            is_invoke: 标记该采集来自 /invoke 能力调用（按关键动作过滤），
                REST 代理不传（按写方法过滤）。
            resource_id: 被操作资源的唯一标识（仅 /invoke 采集点传入），
                REST 代理不传。None 表示不关联具体资源实例。
        """
        if not self._is_collectable(method, path, invoke_action, is_invoke):
            return

        # invoke 调用以业务 action 作为审计动作，REST 以 http.{method}
        audit_action = invoke_action if is_invoke and invoke_action else f"http.{method.lower()}"

        # 模拟态元数据并入 request_params（JSONB），免改表结构
        if impersonated:
            _params = dict(request_params or {})
            _params["impersonated"] = True
            if original_tenant_id:
                _params["original_tenant_id"] = original_tenant_id
            request_params = _params

        entry = {
            "tenant_id": tenant_id,
            "log_type": "OPERATION",
            "action": audit_action,
            "outcome": "SUCCESS" if status_code < 400 else "FAILED",
            "service_name": service_name,
            "user_id": int(user_id) if user_id and user_id.isdigit() else None,
            "username": username,
            "ip": ip,
            "resource": _ACTION_TO_RESOURCE.get(audit_action) if _ACTION_TO_RESOURCE else None,
            "resource_id": resource_id,
            "method": method,
            "path": path,
            "status_code": status_code,
            "duration_ms": int(latency_ms),
            "trace_id": trace_id,
            "request_params": request_params,
            "response_body": response_body,
        }

        async with self._lock:
            self._buffer.append(entry)
            if len(self._buffer) >= self.config.AUDIT_FLUSH_BATCH_SIZE:
                asyncio.ensure_future(self._flush())

    async def _flush(self):
        """批量推送缓冲条目到 platform ingest

        先 POST 再清除缓冲区；失败时保留数据并使用指数退避重试，
        避免静默丢弃审计数据。
        """
        # 指数退避窗口内跳过本次 flush
        now = time.monotonic()
        if self._next_retry_at > 0 and now < self._next_retry_at:
            return

        async with self._lock:
            if not self._buffer:
                return
            batch = list(self._buffer)
            self._buffer.clear()

        if not batch:
            return

        success = False
        try:
            internal_auth = get_internal_auth()
            token = internal_auth.generate_token("sidecar")
            platform_url = self.config.PLATFORM_URL
            url = f"{platform_url}/api/v1/platform/audit-logs:ingest"
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    url,
                    json={"entries": batch},
                    headers={"Authorization": f"Bearer {token}"},
                )
                if resp.status_code >= 400:
                    logger.warning(
                        "[AuditForwarder] ingest 返回 %s: %s",
                        resp.status_code, resp.text,
                    )
                else:
                    success = True
        except Exception:
            logger.exception(
                "[AuditForwarder] 推送审计日志失败，保留 %d 条待下次重试",
                len(batch),
            )

        if success:
            self._consecutive_failures = 0
            self._next_retry_at = 0.0
        else:
            # 失败：将 batch 恢复到缓冲区前端，优先重试旧数据
            async with self._lock:
                self._buffer[:0] = batch
            self._consecutive_failures += 1
            backoff = min(
                self._BASE_BACKOFF * (2 ** (self._consecutive_failures - 1)),
                self._MAX_BACKOFF,
            )
            self._next_retry_at = now + backoff
            logger.warning(
                "[AuditForwarder] 连续失败 %d 次，下次重试在 %.1fs 后",
                self._consecutive_failures, backoff,
            )

    async def start_periodic_flush(self):
        """启动定时 flush 后台任务"""
        if self._running:
            return
        self._running = True
        interval = self.config.AUDIT_FLUSH_INTERVAL_MS / 1000.0

        async def _loop():
            while self._running:
                await asyncio.sleep(interval)
                await self._flush()
        self._bg_task = asyncio.create_task(_loop())

    async def stop(self):
        """停止并执行最终 flush"""
        self._running = False
        if self._bg_task:
            self._bg_task.cancel()
            try:
                await self._bg_task
            except asyncio.CancelledError:
                pass
        await self._flush()


# 全局单例
_audit_forwarder: Optional["AuditForwarder"] = None
_rate_limiter: Optional[RateLimiter] = None
_metering: Optional[MeteringCollector] = None
_circuit_breaker: Optional[CircuitBreaker] = None


def get_audit_forwarder() -> "AuditForwarder":
    global _audit_forwarder
    if _audit_forwarder is None:
        _audit_forwarder = AuditForwarder()
    return _audit_forwarder


def get_rate_limiter() -> RateLimiter:
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter()
    return _rate_limiter


def get_metering() -> MeteringCollector:
    global _metering
    if _metering is None:
        _metering = MeteringCollector()
    return _metering


def get_circuit_breaker() -> CircuitBreaker:
    global _circuit_breaker
    if _circuit_breaker is None:
        _circuit_breaker = CircuitBreaker()
    return _circuit_breaker