#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""
Sidecar 代理主应用

统一入口，处理所有能力调用，提供：
- API Key 认证
- 调用计量
- 限流熔断
- 日志追踪
- 能力服务反向代理
"""

import json
import logging
import time
from typing import Any, Optional

import httpx
from fastapi import FastAPI, Depends, Request, Header
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

from jonex_core.sidecar.proxy import get_capability_proxy
from jonex_core.common.i18n import translate
from jonex_core.sidecar.hooks import (
    get_rate_limiter,
    get_metering,
    get_circuit_breaker,
    get_audit_forwarder,
)
from jonex_core.common.audit_enums import _ACTION_TO_RESOURCE, _RESOURCE_TO_ID_FIELD
from jonex_core.common import (
    error_response,
    register_exception_handlers,
    install_locale_middleware,
    MissingApiKeyError,
    CapabilityInvokeError,
    InvalidParameterError,
    TenantIsolationError,
    TokenExpiredError,
    require_tenant,
    setup_logging,
)
from jonex_core.common.config import get_config
from jonex_core.security.user_auth import get_user_auth

logger = logging.getLogger(__name__)


class CapabilityInvokeRequest(BaseModel):
    """能力调用请求"""
    capability_id: str
    payload: dict
    tenant_id: Optional[str] = None
    user_id: Optional[str] = None
    username: Optional[str] = None
    ip: Optional[str] = None
    context: Optional[dict] = None
    impersonated: bool = False
    perms: list[str] = []
    original_tenant_id: Optional[str] = None


class InvokeResult(BaseModel):
    """调用结果"""
    request_id: str
    success: bool
    code: int
    message: str
    data: Optional[dict] = None
    details: Optional[dict] = None
    params: Optional[dict] = None
    latency_ms: float


class UserInfo(BaseModel):
    """用户信息"""
    user_id: int
    username: str
    display_name: Optional[str] = None
    tenant_id: str
    role: str


TENANT_REQUIRED_AUTH_PATHS = {
    "auth/login",
    "auth/refresh",
    "auth/login-ticket",
}

TENANT_REQUIRED_PLATFORM_PREFIXES = (
    "platform/users",
    "platform/roles",
    "platform/audit-logs",
    "platform/task-schedules",
)


def _normalize_proxy_path(path: str) -> str:
    return path.strip("/")


async def _read_json_body(request: Request) -> dict[str, Any] | None:
    """读取代理请求体。空 body 允许通过，非对象 JSON 直接拒绝。"""
    if request.method not in ("POST", "PUT", "PATCH"):
        return None

    raw = await request.body()
    if not raw:
        return None

    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidParameterError(cause=exc)

    if body is None:
        return None
    if not isinstance(body, dict):
        raise InvalidParameterError()
    return body


def _tenant_from_authorization(auth_header: str | None) -> str | None:
    if not auth_header:
        return None

    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None

    if token.startswith("jonex_test_"):
        return require_tenant(token.removeprefix("jonex_test_"))

    user_auth = get_user_auth()
    payload = user_auth.decode_token(token)
    return require_tenant(payload.get("tenant_id"))


def _candidate_tenant(value: Any, source: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidParameterError(
            details={"source": source},
        )
    if not value.strip():
        return None
    return require_tenant(value)


def _assert_tenant_matches(
    candidate: Any,
    canonical: str | None,
    source: str,
) -> str | None:
    tenant_id = _candidate_tenant(candidate, source)
    if tenant_id is None:
        return canonical
    if canonical is not None and tenant_id != canonical:
        raise TenantIsolationError(
            message=translate("err.tenant.mismatch", fallback="请求租户与认证租户不一致"),
            details={
                "source": source,
                "request_tenant_id": tenant_id,
                "auth_tenant_id": canonical,
            },
        )
    return tenant_id


def _platform_path_requires_tenant(path: str) -> bool:
    normalized = _normalize_proxy_path(path)
    if normalized in TENANT_REQUIRED_AUTH_PATHS:
        return True
    return any(
        normalized == prefix or normalized.startswith(f"{prefix}/")
        for prefix in TENANT_REQUIRED_PLATFORM_PREFIXES
    )


def _build_anon_rate_limit_key(request) -> str:
    """为未认证请求构建限流 key（基于客户端 IP）。

    未认证路径（如 /auth/login、/auth/exchange-ticket）没有租户上下文，
    不能使用共享的 "system" key（会被单一攻击者耗尽 300/min 额度）。
    改用客户端 IP 作为限流维度，每个 IP 独立计数。

    使用固定前缀 "anon:ip:" 以便在 Redis keys 中区别于普通租户 key。

    ═══════════════════════════════════════════════════════════════════════
    SECURITY NOTE: X-Forwarded-For 信任假设
    ───────────────────────────────────────────────────────────────────────
    本函数取 X-Forwarded-For 第一段作为客户端 IP 用于限流 key。
    安全前提是前置反向代理（Nginx/Traefik/Caddy）已正确设置该头，
    且 Sidecar 不接受来自公网的直连请求。

    若攻击者可直连 Sidecar 端口，则可通过伪造 X-Forwarded-For 头
    任意切换限流 key，绕过匿名 IP 限流。

    加固方向（未实施，供后续参考）：
      - 在反向代理层设 X-Real-IP 并 strip 入站 X-Forwarded-For
      - 或在 Nginx 侧配置 trusted proxies CIDR，仅信任上游代理 IP
      - 或在 jonex_core/common/config.py 增加 TRUSTED_PROXIES 配置，
        类似 uvicorn --proxy-headers --forwarded-allow-ips 机制
    ═══════════════════════════════════════════════════════════════════════
    """
    client_ip = (
        request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )
    return f"anon:ip:{client_ip}"


def _resolve_platform_tenant(
    request: Request,
    body: dict[str, Any] | None,
    auth: dict | None = None,
    require: bool = False,
) -> str | None:
    canonical = _candidate_tenant(auth.get("tenant_id"), "auth") if auth else None
    if auth is not None:
        canonical = _assert_tenant_matches(
            _tenant_from_authorization(request.headers.get("Authorization")),
            canonical,
            "authorization",
        )
    canonical = _assert_tenant_matches(
        request.headers.get("X-Tenant-ID"),
        canonical,
        "X-Tenant-ID",
    )

    if require and canonical is None:
        raise TenantIsolationError(message=translate("err.tenant.required", fallback="该请求必须携带明确租户"))
    return canonical


def _resolve_invoke_tenant(
    invoke_request: CapabilityInvokeRequest,
    auth: dict,
    x_tenant_id: str | None = None,
) -> str:
    canonical = _candidate_tenant(auth.get("tenant_id"), "auth")
    canonical = _assert_tenant_matches(x_tenant_id, canonical, "X-Tenant-ID")
    if canonical is None:
        raise TenantIsolationError(message=translate("err.tenant.required_for_invoke", fallback="能力调用必须携带明确租户"))
    _assert_tenant_matches(invoke_request.tenant_id, canonical, "invoke.tenant_id")
    return canonical


# 系统级 invoke action：租户由能力侧（数据源记录）推导，允许无租户调用。
# 这些 action 自身做强校验（如 ingest_push 校验 per-数据源 ingest key）。
_SYSTEM_INVOKE_ACTIONS = {"ingest_push"}


def _resolve_invoke_tenant_optional(
    invoke_request: CapabilityInvokeRequest,
    auth: dict,
    x_tenant_id: str | None = None,
) -> str | None:
    """系统 action 用：尽力解析租户，缺失则返回 None（不抛异常）。"""
    try:
        canonical = _candidate_tenant(auth.get("tenant_id"), "auth")
        canonical = _assert_tenant_matches(x_tenant_id, canonical, "X-Tenant-ID")
        return canonical
    except Exception:
        return None


async def _record_platform_metrics(
    metering_tenant_id: str,
    path: str,
    request: Request,
    status_code: int,
    start: float,
    audit_user_id: str,
    audit_username: str,
    client_ip: str,
    impersonated: bool = False,
    original_tenant_id: Optional[str] = None,
):
    """记录 platform 代理的计量与审计"""
    latency = (time.time() - start) * 1000
    metering = get_metering()
    await metering.record(
        metering_tenant_id,
        path,
        latency,
        status_code,
    )
    # TODO: REST proxy 路径暂不从 URL 路径参数提取 resource_id。
    # 后续可从 path 中解析 ID 字段（如 "platform/spaces/{space_id}"）。
    # 当前 resource_id 仅在 /invoke 路径中填充。
    await get_audit_forwarder().collect(
        tenant_id=metering_tenant_id,
        method=request.method,
        path=path,
        status_code=status_code,
        latency_ms=latency,
        trace_id=getattr(request.state, "request_id", ""),
        user_id=audit_user_id,
        username=audit_username,
        ip=client_ip,
        service_name="sidecar",
        impersonated=impersonated,
        original_tenant_id=original_tenant_id,
    )


async def _proxy_to_platform(request: Request, path: str, auth: dict | None = None):
    """转发请求到 platform 容器"""
    config = get_config()
    platform_url = config.PLATFORM_URL
    limiter = get_rate_limiter()
    metering = get_metering()

    auth_header = request.headers.get("Authorization", "")
    body = await _read_json_body(request)
    tenant_id = _resolve_platform_tenant(
        request,
        body,
        auth=auth,
        require=_platform_path_requires_tenant(path),
    )
    metering_tenant_id = tenant_id or _build_anon_rate_limit_key(request)

    allowed, retry_after = await limiter.check(metering_tenant_id, path)
    if not allowed:
        resp = error_response(
            code=429,
            message=f"Rate limit exceeded. Retry after {retry_after} seconds.",
            status_code=429,
        )
        resp.headers["Retry-After"] = str(retry_after)
        return resp

    headers = {
        "X-Request-ID": getattr(request.state, "request_id", ""),
    }
    if auth_header:
        headers["Authorization"] = auth_header
    if tenant_id:
        headers["X-Tenant-ID"] = tenant_id

    from jonex_core.common import transmit_locale_header
    transmit_locale_header(headers)

    target = f"{platform_url}/api/v1/{path}"

    start = time.time()
    status_code = 200
    # 提取客户端 IP（优先 X-Forwarded-For）
    client_ip = request.headers.get(
        "X-Forwarded-For",
        request.client.host if request.client else "",
    ).split(",")[0].strip()
    audit_user_id = str(auth.get("user_id")) if auth and auth.get("user_id") else ""
    audit_username = auth.get("username", "") if auth else ""
    impersonated = bool(auth.get("impersonated")) if auth else False
    original_tenant_id = auth.get("original_tenant_id") if auth else None
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            kwargs = {"headers": headers, "params": request.query_params}
            if body is not None:
                kwargs["json"] = body
            resp = await client.request(request.method, target, **kwargs)
            status_code = resp.status_code
            resp.raise_for_status()
            result = resp.json()
            await _record_platform_metrics(
                metering_tenant_id, path, request, status_code, start,
                audit_user_id, audit_username, client_ip,
                impersonated, original_tenant_id,
            )
            return result
        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code
            await _record_platform_metrics(
                metering_tenant_id, path, request, status_code, start,
                audit_user_id, audit_username, client_ip,
                impersonated, original_tenant_id,
            )
            try:
                detail = e.response.json()
            except Exception:
                detail = {"message": e.response.text}
            if isinstance(detail, dict) and "success" in detail and "message" in detail:
                return JSONResponse(content=detail, status_code=e.response.status_code)
            raise CapabilityInvokeError(
                message=translate("err.capability.upstream_error", params={"status": str(e.response.status_code)}, fallback=detail.get("message", f"平台服务错误: HTTP {e.response.status_code}")),
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
            await _record_platform_metrics(
                metering_tenant_id, path, request, 502, start,
                audit_user_id, audit_username, client_ip,
                impersonated, original_tenant_id,
            )
            raise CapabilityInvokeError(
                message=translate("err.capability.unreachable", fallback=f"平台服务不可达: {str(e)}"),
            )


class SidecarApp:
    """Sidecar 应用类"""

    def __init__(self):
        self.app = FastAPI(
            title="Jonex Platform Sidecar",
            description="Jonex 平台能力代理 - 统一入口（内部服务）",
            version="1.0.0"
        )
        self.proxy = get_capability_proxy()
        self._setup_routes()
        self._setup_lifecycle()
        # 注册统一异常处理器
        register_exception_handlers(self.app)
        # 国际化 locale 中间件（X-Lang 解析 + contextvars 上下文）
        install_locale_middleware(self.app)

    def _setup_lifecycle(self):
        """启动/关闭钩子：启停 AuditForwarder 定时 flush。

        若不启动定时 flush，缓冲只在攒满批量大小时才推送，
        低流量审计会长期滞留内存且重启丢失。
        """

        @self.app.on_event("startup")
        async def _start_audit_forwarder():
            try:
                setup_logging(enable_file=True)
                logger.info("[Sidecar] 本地文件日志已启用")
                await get_audit_forwarder().start_periodic_flush()
                logger.info("[Sidecar] AuditForwarder 定时 flush 已启动")
            except Exception:
                logger.exception("[Sidecar] AuditForwarder 启动失败")

        @self.app.on_event("shutdown")
        async def _stop_audit_forwarder():
            try:
                await get_audit_forwarder().stop()
                logger.info("[Sidecar] AuditForwarder 已停止并 flush")
            except Exception:
                logger.exception("[Sidecar] AuditForwarder 停止失败")

        @self.app.on_event("shutdown")
        async def _stop_rate_limiter():
            try:
                await get_rate_limiter().close()
                logger.info("[Sidecar] RateLimiter Redis 连接池已关闭")
            except Exception:
                logger.exception("[Sidecar] RateLimiter 关闭失败")

    def _setup_routes(self):
        """设置路由"""

        api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

        async def verify_any_auth(
            api_key: Optional[str] = Depends(api_key_header),
            authorization: Optional[str] = Header(None),
            x_tenant_id: Optional[str] = Header(None),
        ) -> dict:
            """二选一认证：优先 Authorization，其次 X-API-Key。

            API Key 只负责认证调用方，不从 key 文本推导业务租户。
            """
            if authorization and authorization.startswith("Bearer "):
                token = authorization[7:]
                if token.startswith("jonex_test_"):
                    return {
                        "auth_type": "test",
                        "tenant_id": require_tenant(token.removeprefix("jonex_test_")),
                    }
                payload = get_user_auth().decode_token(token)
                return {
                    "auth_type": "user",
                    "tenant_id": require_tenant(payload.get("tenant_id")),
                    "user_id": int(payload["sub"]),
                    "username": payload.get("username", ""),
                    "impersonated": bool(payload.get("impersonated")),
                    "original_tenant_id": payload.get("original_tenant_id"),
                    "perms": payload.get("perms") or [],
                }
            if api_key and api_key.startswith("jonex_"):
                tenant_id = require_tenant(x_tenant_id) if x_tenant_id else None
                return {"auth_type": "apikey", "tenant_id": tenant_id}
            raise MissingApiKeyError()

        # ==================== 系统端点 ====================

        @self.app.get("/health")
        async def health_check():
            return {
                "status": "healthy",
                "service": "sidecar",
                "endpoints_configured": len(self.proxy.capability_endpoints),
            }

        @self.app.get("/capabilities")
        async def list_capabilities(auth: dict = Depends(verify_any_auth)):
            capabilities = [
                {
                    "capability_id": f"business.{name}.v1",
                    "name": f"{name} 能力服务",
                    "type": "business",
                    "version": "v1",
                    "endpoint": endpoint,
                }
                for name, endpoint in self.proxy.capability_endpoints.items()
                if endpoint
            ]
            return {"capabilities": capabilities}

        # ==================== 认证端点（代理到 platform 容器） ====================

        @self.app.post("/auth/login")
        async def auth_login(request: Request):
            return await _proxy_to_platform(request, "auth/login")

        @self.app.get("/auth/me")
        async def auth_me(request: Request):
            return await _proxy_to_platform(request, "auth/me")

        @self.app.post("/auth/refresh")
        async def auth_refresh(request: Request, auth: dict = Depends(verify_any_auth)):
            return await _proxy_to_platform(request, "auth/refresh", auth)

        @self.app.post("/auth/login-ticket")
        async def auth_login_ticket(request: Request, auth: dict = Depends(verify_any_auth)):
            return await _proxy_to_platform(request, "auth/login-ticket", auth)

        @self.app.post("/auth/exchange-ticket")
        async def auth_exchange_ticket(request: Request):
            return await _proxy_to_platform(request, "auth/exchange-ticket")

        @self.app.post("/auth/logout")
        async def auth_logout(request: Request):
            return await _proxy_to_platform(request, "auth/logout")

        @self.app.post("/auth/impersonate")
        async def auth_impersonate(request: Request, auth: dict = Depends(verify_any_auth)):
            return await _proxy_to_platform(request, "auth/impersonate", auth)

        @self.app.post("/auth/impersonate/end")
        async def auth_impersonate_end(request: Request, auth: dict = Depends(verify_any_auth)):
            return await _proxy_to_platform(request, "auth/impersonate/end", auth)

        # ==================== 平台管理代理 ====================

        @self.app.api_route("/platform/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
        async def platform_proxy(
            request: Request,
            path: str,
            auth: dict = Depends(verify_any_auth),
        ):
            return await _proxy_to_platform(request, f"platform/{path}", auth)

        # ==================== 能力调用端点 ====================

        @self.app.post("/invoke", response_model=InvokeResult)
        async def invoke_capability(
            request: Request,
            invoke_request: CapabilityInvokeRequest,
            auth: dict = Depends(verify_any_auth),
            x_tenant_id: Optional[str] = Header(None),
        ):
            start_time = time.time()
            # 系统级 invoke action（如入站推送 ingest_push）：租户由 Gateway 从 ingest key 解析传入
            _action = (invoke_request.payload or {}).get("action")
            # 从 invoke payload 的 data 中提取 resource_id
            _resource = _ACTION_TO_RESOURCE.get(_action) if _ACTION_TO_RESOURCE else None
            _resource_id = None
            if _resource:
                _id_field = _RESOURCE_TO_ID_FIELD.get(_resource)
                if _id_field:
                    _payload_data = (invoke_request.payload or {}).get("data", {})
                    _resource_id = _payload_data.get(_id_field)
            if _action in _SYSTEM_INVOKE_ACTIONS:
                tenant_id = invoke_request.tenant_id or _resolve_invoke_tenant_optional(invoke_request, auth, x_tenant_id)
                if not tenant_id:
                    raise CapabilityInvokeError(message=translate("err.ingest.cannot_resolve_tenant", fallback="ingest key 无效，无法解析租户信息，请重新生成 API Key"))
            else:
                tenant_id = _resolve_invoke_tenant(invoke_request, auth, x_tenant_id)
            user_id = invoke_request.user_id or str(auth.get("user_id", ""))
            username = invoke_request.username or auth.get("username", "")
            impersonated = bool(invoke_request.impersonated or auth.get("impersonated", False))
            original_tenant_id = invoke_request.original_tenant_id or auth.get("original_tenant_id")
            perms = invoke_request.perms or auth.get("perms") or []
            # 提取客户端 IP（优先 X-Forwarded-For）
            client_ip = request.headers.get(
                "X-Forwarded-For",
                request.client.host if request.client else "",
            ).split(",")[0].strip()
            ip = invoke_request.ip or client_ip
            # 链路追踪 ID：透传入站 X-Request-ID，缺失则生成，贯穿整条能力调用链路
            import uuid as _uuid
            trace_id = request.headers.get("X-Request-ID") or _uuid.uuid4().hex
            limiter = get_rate_limiter()
            breaker = get_circuit_breaker()
            metering = get_metering()

            # 从 capability_id 提取服务名用于熔断
            service_name = invoke_request.capability_id.split(".")[1] if "." in invoke_request.capability_id else invoke_request.capability_id

            allowed, retry_after = await limiter.check(tenant_id, invoke_request.capability_id, user_id)
            if not allowed:
                resp = error_response(
                    code=429,
                    message=f"Rate limit exceeded. Retry after {retry_after} seconds.",
                    status_code=429,
                )
                resp.headers["Retry-After"] = str(retry_after)
                return resp

            if not await breaker.before_call(service_name):
                raise CapabilityInvokeError(message=translate("err.capability.circuit_open", params={"service_name": service_name}, fallback=f"服务 {service_name} 已熔断，请稍后重试"))

            try:
                logger.info(
                    f"[Sidecar] 转发能力调用: {invoke_request.capability_id}, "
                    f"tenant: {tenant_id}, auth_type: {auth['auth_type']}"
                )

                result = await self.proxy.invoke_capability(
                    capability_id=invoke_request.capability_id,
                    payload=invoke_request.payload,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    username=username,
                    ip=ip,
                    request_id=trace_id,
                    impersonated=impersonated,
                    perms=perms,
                    original_tenant_id=original_tenant_id,
                )

                latency = (time.time() - start_time) * 1000
                await breaker.on_success(service_name)
                await metering.record(tenant_id, invoke_request.capability_id, latency, 200, user_id)

                await get_audit_forwarder().collect(
                    tenant_id=tenant_id,
                    method="POST",
                    path=f"invoke/{invoke_request.capability_id}",
                    status_code=200,
                    latency_ms=latency,
                    trace_id=trace_id,
                    user_id=user_id,
                    username=username,
                    ip=ip,
                    service_name="sidecar",
                    invoke_action=_action,
                    is_invoke=True,
                    resource_id=_resource_id,
                    impersonated=impersonated,
                    original_tenant_id=original_tenant_id,
                )

                logger.info(
                    f"[Sidecar] 转发完成: {invoke_request.capability_id}, "
                    f"success: {result.get('success', True)}, latency: {latency:.2f}ms"
                )

                return InvokeResult(
                    request_id=result.get("request_id", ""),
                    success=result.get("success", True),
                    code=result.get("code", 0),
                    message=result.get("message", "success"),
                    data=result.get("data"),
                    details=result.get("details"),
                    params=result.get("params"),
                    latency_ms=latency,
                )

            except Exception as e:
                latency = (time.time() - start_time) * 1000
                await breaker.on_failure(service_name)
                await metering.record(tenant_id, invoke_request.capability_id, latency, 500, user_id)
                await get_audit_forwarder().collect(
                    tenant_id=tenant_id,
                    method="POST",
                    path=f"invoke/{invoke_request.capability_id}",
                    status_code=500,
                    latency_ms=latency,
                    trace_id=trace_id,
                    user_id=user_id,
                    username=username,
                    ip=ip,
                    service_name="sidecar",
                    invoke_action=_action,
                    is_invoke=True,
                    resource_id=_resource_id,
                    impersonated=impersonated,
                    original_tenant_id=original_tenant_id,
                )
                msg = str(e)
                cid = invoke_request.capability_id
                if "Name or service not known" in msg:
                    hint = f"依赖的服务容器未启动或 DNS 无法解析"
                elif "Connection refused" in msg:
                    hint = "依赖的服务端口未就绪"
                else:
                    hint = msg
                logger.error(f"[Sidecar] 转发异常: {cid}, {hint}, latency={latency:.0f}ms")
                raise CapabilityInvokeError(
                    message=translate("err.capability.invoke_failed", params={"hint": hint}, fallback=f"能力调用失败: {hint}"),
                    cause=e,
                )

        @self.app.post("/invoke/stream")
        async def invoke_capability_stream(
            request: Request,
            invoke_request: CapabilityInvokeRequest,
            auth: dict = Depends(verify_any_auth),
            x_tenant_id: Optional[str] = Header(None),
        ):
            from fastapi.responses import StreamingResponse

            tenant_id = _resolve_invoke_tenant(invoke_request, auth, x_tenant_id)
            user_id = invoke_request.user_id or str(auth.get("user_id", ""))
            impersonated = bool(invoke_request.impersonated or auth.get("impersonated", False))
            original_tenant_id = invoke_request.original_tenant_id or auth.get("original_tenant_id")
            perms = invoke_request.perms or auth.get("perms") or []
            import uuid as _uuid
            trace_id = request.headers.get("X-Request-ID") or _uuid.uuid4().hex

            async def _generate():
                async for line in self.proxy.stream_invoke(
                    capability_id=invoke_request.capability_id,
                    payload=invoke_request.payload,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    request_id=trace_id,
                    impersonated=impersonated,
                    perms=perms,
                    original_tenant_id=original_tenant_id,
                ):
                    yield line + "\n"

            return StreamingResponse(_generate(), media_type="application/x-ndjson")

        @self.app.get("/invoke/stream/rag")
        async def invoke_rag_stream(
            request: Request,
            query: str = "",
            mode: str = "hybrid",
            top_k: int = 5,
            knowledge_base_id: str = "",
            auth: dict = Depends(verify_any_auth),
            x_tenant_id: Optional[str] = Header(None),
        ):
            from fastapi.responses import StreamingResponse

            tenant_id = _assert_tenant_matches(
                x_tenant_id,
                _candidate_tenant(auth.get("tenant_id"), "auth"),
                "X-Tenant-ID",
            )
            if tenant_id is None:
                raise TenantIsolationError(message=translate("err.tenant.required_for_rag", fallback="流式 RAG 查询必须携带明确租户"))

            import uuid as _uuid2
            _trace_id = request.headers.get("X-Request-ID") or _uuid2.uuid4().hex
            async def _generate():
                async for line in self.proxy.stream_rag_query(
                    query=query, tenant_id=tenant_id, mode=mode, top_k=top_k,
                    knowledge_base_id=knowledge_base_id, request_id=_trace_id,
                ):
                    yield line + "\n"

            return StreamingResponse(_generate(), media_type="application/x-ndjson")

    def get_app(self) -> FastAPI:
        """获取 FastAPI 应用"""
        return self.app


# 全局单例
_sidecar_app: Optional[SidecarApp] = None


def get_sidecar_app() -> SidecarApp:
    """获取全局 Sidecar 应用实例"""
    global _sidecar_app
    if _sidecar_app is None:
        _sidecar_app = SidecarApp()
    return _sidecar_app
