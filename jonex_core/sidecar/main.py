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

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from functools import lru_cache
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
from jonex_core.common.monitor_collector import get_monitor_collector
from jonex_core.common.monitor_models import (
    CallDirection,
    CallFinalStatus,
    CallTrigger,
    Protocol,
)
from jonex_core.common import (
    error_response,
    register_exception_handlers,
    install_locale_middleware,
    passthrough_upstream_response,
    MissingApiKeyError,
    CapabilityInvokeError,
    InvalidParameterError,
    TenantIsolationError,
    TokenExpiredError,
    require_tenant,
    setup_logging,
)
from jonex_core.common.tenant import require_test_token_allowed
from jonex_core.common.exceptions import RateLimitExceededError
from jonex_core.common.config import get_config
from jonex_core.common.security_guards import (
    assert_secure_jwt_secret,
    is_insecure_gateway_api_key,
    is_insecure_keycloak_client_secret,
)
from jonex_core.security.user_auth import extract_bearer_token, get_user_auth

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
    """调用结果。

    `data` 故意是 `Any`、不是 `dict`：Sidecar 是**转发层**，不该对下游能力的
    业务数据做形状裁决。设计文档 §4.3 明确 `data` 可为**对象或列表**（`runner.list` /
    `def.catalog_list` / `def.catalog_steps` / `task.subtask_list` 这四个不设分页的接口
    就是直接回裸数组，doc §4.0「不设分页的接口」）。写死成 `dict` 会让这几个 action
    经 `/invoke` 反代时被 FastAPI 的 `response_model` 校验拦下、冒出一个 500 ——
    数据本身没问题，纯粹是这张模型写窄了。形状由能力自己的契约负责。
    """

    request_id: str
    success: bool
    code: int
    message: str
    data: Optional[Any] = None
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
    # auth/refresh 不在其中：refresh token 自包含租户（issuer 反查），首登后仅持 httpOnly cookie、
    # 无 access token / X-Tenant-ID，强制 require 会误拦为 TenantIsolationError。
    "auth/login-ticket",
}

TENANT_REQUIRED_PLATFORM_PREFIXES = (
    "platform/users",
    "platform/roles",
    "platform/audit-logs",
    "platform/task-schedules",
    "platform/identity-integration",
)


def _normalize_proxy_path(path: str) -> str:
    return path.strip("/")


async def _read_json_body(request: Request) -> dict[str, Any] | None:
    """读取代理请求体。空 body 允许通过，非对象 JSON 直接拒绝。form 编码返回 None（由调用方透传原始字节）。"""
    if request.method not in ("POST", "PUT", "PATCH"):
        return None

    content_type = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" in content_type:
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
    # `Bearer  <jwt>` 双空格会把前导空格带进 token，与 verify_any_auth /
    # gateway / tenant 的解析口径不一致 —— 统一 strip。
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None

    if token.startswith("jonex_test_"):
        require_test_token_allowed()
        return require_tenant(token.removeprefix("jonex_test_"))

    user_auth = get_user_auth()
    payload = user_auth.decode_token(token)
    return require_tenant(payload.get("tenant_id"))


def _gateway_api_key_matches(api_key: Optional[str]) -> bool:
    """精确比对网关内部 X-API-Key（脱钩 startswith("jonex_")）。

    与 `jonex_test_` 租户测试 token（Bearer 兜底）脱钩：前者是网关内部
    X-API-Key，后者是 Bearer 兜底，字符串相似纯属巧合，二者独立处理。
    只有与配置值完全一致才通过，前缀匹配已被废除。
    """
    if not api_key:
        return False
    expected = get_config().GATEWAY_API_KEY
    if not expected:
        return False
    return api_key == expected


def _assert_gateway_api_key_configured(config) -> None:
    """生产环境守卫：GATEWAY_API_KEY 必须已从**仓库内公开值**修改。

    生产漏配时 Sidecar 会接受公开可知的字符串（``change-me-in-production``，
    或 ``deploy/.env.example`` 发出的 ``jonex_test_gateway``）为合法内部
    X-API-Key；而 ``_gateway_api_key_matches`` **不校验租户**，攻击者直连
    Sidecar 发送该值 + 任意 ``X-Tenant-ID`` 即可任意租户身份调用能力，
    造成租户隔离旁路。

    > ⚠️ 2026-10-14：原先只比对哨兵串 ``change-me-in-production``，**漏掉实际
    > 发出的 ``jonex_test_gateway``**（实测生产正在用）。现改用共享判定
    > :func:`is_insecure_gateway_api_key`，覆盖仓库内全部曾公开的值。

    fail-closed：生产未正确配置则抛 RuntimeError，让 uvicorn 启动失败拒绝服务。
    """
    if not config.is_production:
        return
    if is_insecure_gateway_api_key(config.GATEWAY_API_KEY):
        raise RuntimeError(
            "GATEWAY_API_KEY 未配置或仍为仓库内公开的默认值"
            "（含 change-me-in-production、jonex_test_gateway）。"
            "生产环境必须在 .env 或环境变量中设置 GATEWAY_API_KEY 为真实随机 key，"
            "否则 Gateway→Sidecar 内部认证可被伪造（且该 key 不绑定租户）。"
        )


def _assert_keycloak_client_secret_configured(config) -> None:
    """生产环境守卫：KEYCLOAK_CLIENT_SECRET 必须已从**仓库内公开值**覆盖。

    生产漏配时服务会静默使用公开可知的字符串作为 Keycloak confidential client
    secret，攻击者可据此伪造 client 身份（Admin API / backchannel 调用）。

    > ⚠️ 2026-10-14：原先只比对哨兵串 ``change-me-in-production``，**漏掉公开的
    > ``jonex-platform-public-pkce-client``**（实测生产正在用，那是 PKCE **公开**
    > client id）。现改用共享判定 :func:`is_insecure_keycloak_client_secret`。

    fail-closed：生产未正确配置则抛 RuntimeError，让 uvicorn 启动失败拒绝服务。
    """
    if not config.is_production:
        return
    # SSO 未启用的部署不消费该密钥（全仓运行时唯一读取者即本守卫），
    # 不得让与 SSO 无关的部署因哨兵值而启动失败。
    if not config.sso_enabled:
        return
    if is_insecure_keycloak_client_secret(config.KEYCLOAK_CLIENT_SECRET):
        raise RuntimeError(
            "KEYCLOAK_CLIENT_SECRET 未配置或仍为仓库内公开的默认值"
            "（含 change-me-in-production、jonex-platform-public-pkce-client）。"
            "生产环境必须在 .env 或环境变量中设置 KEYCLOAK_CLIENT_SECRET 为真实随机 secret，"
            "否则 Keycloak confidential client secret 可被攻击者猜到。"
        )


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


@lru_cache(maxsize=1)
def _error_code_to_http_status() -> dict[int, int]:
    """错误码 → HTTP 状态的映射，取自异常类自身的 `status_code`。"""
    from jonex_core.common import exceptions as _exceptions

    mapping: dict[int, int] = {}
    for _obj in vars(_exceptions).values():
        if isinstance(_obj, type) and issubclass(_obj, _exceptions.JonexException):
            mapping.setdefault(_obj.code, _obj.status_code)
    return mapping


def _derive_invoke_status_code(result: dict) -> int:
    """由能力返回体推导「记录用」状态码（审计 / 计量侧）。

    能力侧 `/invoke` 端点（`deploy/start_capability.py:177`）恒以 **HTTP 200** 返回，
    业务成败只体现在响应体的 `success` / `code` 上 —— `CapabilityRegistry.invoke`
    会把 `JonexException` 与未捕获异常**都转成** `CapabilityResponse.error` 返回
    （`jonex_core/capability/registry.py:86-101`），因此这里 `raise_for_status()`
    对业务失败**不触发**。若沿用写死的 200，审计与调用监控会把每一次失败调用都记成
    成功（监控 N1 的 `final_status` 恒为成功）。

    只用于审计 / 计量记录，**不改回给调用方的 HTTP 响应**（仍由 FastAPI 按
    `InvokeResult` 返回 200，成败由 body 的 `success` / `code` 承载）。

    判定顺序：
    1. `success` 为真 ⇒ 200；
    2. `code` 落在 HTTP **失败**区间（4xx/5xx）⇒ 原样使用（能力侧对「能力未找到」
       填 404、对未捕获异常填 500，见 `registry.py:77`/`:99`）；
    3. 否则查异常类的 `status_code`；查不到保守记 500。

    ⚠️ 失败态下的 2xx/3xx `code` 属矛盾数据，**不**按 HTTP 语义原样返回，
    一律落到第 3 条（查不到即 500），避免把失败记成成功。
    """
    if result.get("success", True):
        return 200
    code = result.get("code")
    if isinstance(code, int):
        if 400 <= code < 600:
            return code
        mapped = _error_code_to_http_status().get(code)
        if mapped is not None:
            return mapped
    return 500


def _caller_from_auth(auth: dict) -> tuple:
    """从认证结果推导「真实调用方」与「触发方」。

    返回 ``(source, trigger_type)``。

    ``source`` 记**真实发起方**，不记内部中转站：网关代用户发起的业务请求会带上
    用户 JWT，此处解析出的就是用户本人。取不到用户身份时退回认证类型
    （``apikey`` 等），表示「另一个系统在调」而非某个用户。

    ``trigger_type``：带上用户身份 ⇒ ``user``；否则视为系统/后台调用 ⇒ ``system``。
    ⚠️ **已知不精确**：外部系统经 MCP 发起的调用若无用户身份，会落在 ``system``
    一侧 —— 待调用方透传显式标识后再细分。
    """
    auth_type = (auth or {}).get("auth_type") or ""
    username = (auth or {}).get("username") or ""
    user_id = (auth or {}).get("user_id")
    if auth_type == "user" and (username or user_id):
        return (username or f"user:{user_id}", CallTrigger.USER.value)
    return (auth_type or "unknown", CallTrigger.SYSTEM.value)


def _monitor_n1_fields(
    *,
    request_id: str,
    tenant_id: str,
    capability_id: str,
    start_time: float,
    status_code: int,
    source: str = "unknown",
    trigger_type: str = CallTrigger.USER.value,
    error_code: Optional[Any] = None,
    error_message: Optional[Any] = None,
    final_status: Optional[str] = None,
) -> dict:
    """拼一次 ``/invoke`` 的 N1（``monitor.call_log``）字段。

    Sidecar 腿采 **N1 主记录**：它是唯一同时握有租户上下文 + 能力标识 + 耗时的采集点。
    N2 的 Attempt 语义（``attempt_no`` /
    ``is_final`` / ``wait_before_ms``）这一腿采不到，归发起重试的 capability 采。

    ``direction`` 恒 ``inbound`` —— 记录的是「进入能力」的调用；llm-gateway 腿记 ``outbound``。
    时间列走 tz-aware（DDL 是 TIMESTAMPTZ，朴素值会踩 asyncpg 编解码坑）。
    ``error_*`` 只判状态码，成功时留空。
    """
    success = status_code < 400
    # 流式端点无法在开流后改 HTTP 状态码，取消等三态必须由调用方显式指定。
    resolved_status = final_status or (
        CallFinalStatus.SUCCESS.value if success else CallFinalStatus.FAILED.value
    )
    return {
        "request_id": request_id,
        "trace_id": request_id,  # 现与 request_id 同源，是否分离待定
        "tenant_id": tenant_id,
        "direction": CallDirection.INBOUND.value,
        "source": source,
        "target": capability_id,
        "protocol": Protocol.INVOKE.value,
        "trigger_type": trigger_type,
        "logical_started_at": datetime.fromtimestamp(start_time, timezone.utc),
        "final_ended_at": datetime.now(timezone.utc),
        "final_status": resolved_status,
        "error_code": (
            None
            if resolved_status == CallFinalStatus.SUCCESS.value or error_code is None
            else str(error_code)
        ),
        "error_message": (
            None if resolved_status == CallFinalStatus.SUCCESS.value else error_message
        ),
    }


def _collect_monitor_n1(**kwargs) -> None:
    """投递一条 N1 到采集器，**吞掉一切异常**。

    硬红线：采集是旁路，投递失败 / 队列满**不得**影响业务请求。
    ``put_call_log`` 本身已是非阻塞（``put_nowait``），这里再兜一层，防止字段拼装
    阶段（如时间转换）的意外异常冒到主链路。
    """
    try:
        get_monitor_collector().put_call_log(_monitor_n1_fields(**kwargs))
    except Exception:
        logger.exception("[Sidecar] 监控采集投递失败（已吞异常，不影响调用）")


async def _monitored_stream(
    source,
    *,
    request_id: str,
    tenant_id: str,
    capability_id: str,
    start_time: float,
    source_name: str,
    trigger_type: str,
):
    """把上游流包一层 N1 采集：成功 / 失败 / 取消三态各记一条，且只记一条。

    ⚠️ 流式响应一旦开流，HTTP 状态码就改不了了，N1 的结局只能按**流内结果**判定：

    - 正常迭代完 ⇒ ``success``（``status_code=200``，仅用于走既有 success 分支）；
    - ``asyncio.CancelledError`` / ``GeneratorExit``（客户端断开）⇒ ``cancelled`` 后重抛；
    - 其他异常 ⇒ ``failed``（带 error_code/message）后重抛。

    不采 N2：流式路径没有 per-attempt 语义，避免与能力侧重试记录重复。
    ``recorded`` 标志保证「异常路径记过之后，finally / 关闭路径不会补记第二条」。
    """
    recorded = False

    def _record_once(*, status_code: int, **kwargs) -> None:
        nonlocal recorded
        if recorded:
            return
        recorded = True
        _collect_monitor_n1(
            request_id=request_id,
            tenant_id=tenant_id,
            capability_id=capability_id,
            start_time=start_time,
            status_code=status_code,
            source=source_name,
            trigger_type=trigger_type,
            **kwargs,
        )

    try:
        async for line in source:
            yield line + "\n"
    except (asyncio.CancelledError, GeneratorExit):
        _record_once(
            status_code=499,  # 仅占位；终态由 final_status 决定
            final_status=CallFinalStatus.CANCELLED.value,
        )
        raise
    except Exception as exc:
        _record_once(
            status_code=500,
            error_code=getattr(exc, "code", None),
            error_message=str(exc),
        )
        raise
    else:
        _record_once(status_code=200)


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
    content_type = request.headers.get("content-type", "")
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
            code=RateLimitExceededError.code,
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
    # 透传 Cookie：refresh/logout 的 httpOnly refresh token 经 cookie 携带，不落 header。
    cookie_header = request.headers.get("cookie")
    if cookie_header:
        headers["cookie"] = cookie_header

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
            elif "application/x-www-form-urlencoded" in content_type:
                raw = await request.body()
                if raw:
                    kwargs["content"] = raw
                    headers["Content-Type"] = content_type
            resp = await client.request(request.method, target, **kwargs)
            status_code = resp.status_code

            # 3xx 重定向（OIDC 授权码回调 302 + Set-Cookie）必须透传而非报错：
            # httpx raise_for_status() 对 3xx 也抛 HTTPStatusError，且空 body 的
            # resp.json() 会抛 JSONDecodeError。先于 raise_for_status 判断并透传。
            if 300 <= status_code < 400:
                await _record_platform_metrics(
                    metering_tenant_id, path, request, status_code, start,
                    audit_user_id, audit_username, client_ip,
                    impersonated, original_tenant_id,
                )
                return passthrough_upstream_response(resp)

            resp.raise_for_status()
            # 2xx 且带 Set-Cookie（refresh 旋转 / logout 删 cookie）→ 透传（含 content-type + body），
            # 否则解析 JSON；resp.json() 会丢弃 Set-Cookie。
            if resp.headers.get_list("set-cookie"):
                await _record_platform_metrics(
                    metering_tenant_id, path, request, status_code, start,
                    audit_user_id, audit_username, client_ip,
                    impersonated, original_tenant_id,
                )
                return passthrough_upstream_response(resp)
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
            # 安全守卫：生产环境 GATEWAY_API_KEY 必须已从默认哨兵修改。
            # 放在 try 块外，确保 RuntimeError 不被吞掉，fail-closed 拒绝启动。
            _assert_gateway_api_key_configured(get_config())
            # 安全守卫：JWT_SECRET 必须为真实随机值（**生产档 ENV=prod 硬拦**；非生产只告警，
            # 见 jonex_core/common/security_guards.py 约束 2。Sidecar 用它对用户 JWT
            # 验签，使用公开默认值即可被伪造管理员 token）。
            assert_secure_jwt_secret(get_config())
            _assert_keycloak_client_secret_configured(get_config())
            try:
                setup_logging(enable_file=True)
                logger.info("[Sidecar] 本地文件日志已启用")
                await get_audit_forwarder().start_periodic_flush()
                logger.info("[Sidecar] AuditForwarder 定时 flush 已启动")
            except Exception:
                logger.exception("[Sidecar] AuditForwarder 启动失败")
            # 调用明细采集写口（N1 主记录）。同审计的理由：采集是旁路，起不来不能拖垮 Sidecar。
            try:
                get_monitor_collector().start()
                logger.info("[Sidecar] MonitorCollector 已启动")
            except Exception:
                logger.exception("[Sidecar] MonitorCollector 启动失败")

        @self.app.on_event("shutdown")
        async def close_wiki_progress_connections():
            await self.proxy.close_wiki_progress_client()

        @self.app.on_event("shutdown")
        async def _stop_audit_forwarder():
            try:
                await get_audit_forwarder().stop()
                logger.info("[Sidecar] AuditForwarder 已停止并 flush")
            except Exception:
                logger.exception("[Sidecar] AuditForwarder 停止失败")

        @self.app.on_event("shutdown")
        async def _stop_monitor_collector():
            try:
                await get_monitor_collector().stop()
                logger.info("[Sidecar] MonitorCollector 已停止并 flush")
            except Exception:
                logger.exception("[Sidecar] MonitorCollector 停止失败")

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
                # 与网关 require_auth_header / identity._decode 同口径：
                # 《Bearer  <jwt>》（双空格）也要能解出 token。
                token = extract_bearer_token(authorization)
                if not token:
                    raise MissingApiKeyError()
                if token.startswith("jonex_test_"):
                    require_test_token_allowed()
                    return {
                        "auth_type": "test",
                        "tenant_id": require_tenant(token.removeprefix("jonex_test_")),
                    }
                payload = get_user_auth().decode_token(token)
                # decode_token 同时接受 user / refresh；访问令牌必须显式判别，
                # 否则 30 天有效的 refresh token 可当 access 用（/platform/* 透传）。
                if payload.get("type") != "user":
                    raise TokenExpiredError(
                        message=translate("err.auth.user_token_required", fallback="需要用户访问令牌")
                    )
                return {
                    "auth_type": "user",
                    "tenant_id": require_tenant(payload.get("tenant_id")),
                    "user_id": int(payload["sub"]),
                    "username": payload.get("username", ""),
                    "impersonated": bool(payload.get("impersonated")),
                    "original_tenant_id": payload.get("original_tenant_id"),
                    "perms": payload.get("perms") or [],
                }
            if _gateway_api_key_matches(api_key):
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

        @self.app.get("/auth/login-config")
        async def auth_login_config(request: Request):
            """登录页启动配置（默认租户 / 是否隐藏租户框）。

            必须免鉴权：登录页在拿到 token 之前就要用它渲染，且不带租户头
            （`TENANT_REQUIRED_AUTH_PATHS` 里只有 auth/login 与
            auth/login-ticket，本路径不在其中，故不会走租户强制）。
            未认证请求按客户端 IP 走匿名限流。
            """
            return await _proxy_to_platform(request, "auth/login-config")

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

        @self.app.get("/auth/oidc/authorize")
        async def auth_oidc_authorize(request: Request):
            return await _proxy_to_platform(request, "auth/oidc/authorize")

        @self.app.get("/auth/oidc/callback")
        async def auth_oidc_callback_get(request: Request):
            return await _proxy_to_platform(request, "auth/oidc/callback")

        @self.app.post("/auth/oidc/callback")
        async def auth_oidc_callback_post(request: Request):
            return await _proxy_to_platform(request, "auth/oidc/callback")

        @self.app.post("/auth/oidc/backchannel-logout")
        async def auth_oidc_backchannel_logout(request: Request):
            return await _proxy_to_platform(request, "auth/oidc/backchannel-logout")

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
            # 监控采集：真实调用方 + 触发方。「来源服务」列记发起方本人，
            # 来源列记真实调用方，不记内部中转站。
            _monitor_source, _monitor_trigger = _caller_from_auth(auth)
            limiter = get_rate_limiter()
            breaker = get_circuit_breaker()
            metering = get_metering()

            # 从 capability_id 提取服务名用于熔断
            service_name = invoke_request.capability_id.split(".")[1] if "." in invoke_request.capability_id else invoke_request.capability_id

            # Only authenticated service calls without an end-user identity
            # use the internal bucket. User traffic retains the tenant ingress
            # limit, including calls forwarded by the gateway's service key.
            internal_hop = (auth.get("auth_type") == "apikey" and not user_id
                            and invoke_request.capability_id.startswith("atomic."))
            bucket = f"internal:{service_name}" if internal_hop else "invoke"
            allowed, retry_after = await limiter.check(
                tenant_id, invoke_request.capability_id, user_id, bucket=bucket)
            if not allowed:
                resp = error_response(
                    code=RateLimitExceededError.code,
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
                    context=invoke_request.context,
                )

                latency = (time.time() - start_time) * 1000
                # 能力侧 /invoke 恒返回 HTTP 200，业务成败只体现在响应体上；
                # 记录用状态码必须由 result 推导，否则审计/监控把失败调用记成成功。
                record_status = _derive_invoke_status_code(result)
                await breaker.on_success(service_name)
                await metering.record(tenant_id, invoke_request.capability_id, latency, record_status, user_id)

                await get_audit_forwarder().collect(
                    tenant_id=tenant_id,
                    method="POST",
                    path=f"invoke/{invoke_request.capability_id}",
                    status_code=record_status,
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

                # 监控采集（旁路，非阻塞）：N1 主记录。失败恒不抛出。
                _collect_monitor_n1(
                    request_id=trace_id,
                    tenant_id=tenant_id,
                    capability_id=invoke_request.capability_id,
                    start_time=start_time,
                    status_code=record_status,
                    source=_monitor_source,
                    trigger_type=_monitor_trigger,
                    error_code=result.get("code"),
                    error_message=result.get("message"),
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
                # 监控采集（旁路，非阻塞）：异常路径记 failed（status_code=500）。
                # 异常可能是 JonexException（带 code）也可能是裸异常（无 code）。
                _collect_monitor_n1(
                    request_id=trace_id,
                    tenant_id=tenant_id,
                    capability_id=invoke_request.capability_id,
                    start_time=start_time,
                    status_code=500,
                    source=_monitor_source,
                    trigger_type=_monitor_trigger,
                    error_code=getattr(e, "code", None),
                    error_message=str(e),
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

            start_time = time.time()
            tenant_id = _resolve_invoke_tenant(invoke_request, auth, x_tenant_id)
            user_id = invoke_request.user_id or str(auth.get("user_id", ""))
            impersonated = bool(invoke_request.impersonated or auth.get("impersonated", False))
            original_tenant_id = invoke_request.original_tenant_id or auth.get("original_tenant_id")
            perms = invoke_request.perms or auth.get("perms") or []
            import uuid as _uuid
            trace_id = request.headers.get("X-Request-ID") or _uuid.uuid4().hex
            # 来源 / 触发方取法与 /invoke 一致：记真实调用方，不记中转站。
            _monitor_source, _monitor_trigger = _caller_from_auth(auth)

            # 直接把采集生成器交给 StreamingResponse：`body_iterator` 就是它，
            # 客户端断开时 aclose/GeneratorExit 能直达采集层，记成 cancelled。
            return StreamingResponse(
                _monitored_stream(
                    self.proxy.stream_invoke(
                        capability_id=invoke_request.capability_id,
                        payload=invoke_request.payload,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        request_id=trace_id,
                        impersonated=impersonated,
                        perms=perms,
                        original_tenant_id=original_tenant_id,
                    ),
                    request_id=trace_id,
                    tenant_id=tenant_id,
                    capability_id=invoke_request.capability_id,
                    start_time=start_time,
                    source_name=_monitor_source,
                    trigger_type=_monitor_trigger,
                ),
                media_type="application/x-ndjson",
            )

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

            start_time = time.time()
            tenant_id = _assert_tenant_matches(
                x_tenant_id,
                _candidate_tenant(auth.get("tenant_id"), "auth"),
                "X-Tenant-ID",
            )
            if tenant_id is None:
                raise TenantIsolationError(message=translate("err.tenant.required_for_rag", fallback="流式 RAG 查询必须携带明确租户"))

            import uuid as _uuid2
            _trace_id = request.headers.get("X-Request-ID") or _uuid2.uuid4().hex
            _monitor_source, _monitor_trigger = _caller_from_auth(auth)

            return StreamingResponse(
                _monitored_stream(
                    self.proxy.stream_rag_query(
                        query=query, tenant_id=tenant_id, mode=mode, top_k=top_k,
                        knowledge_base_id=knowledge_base_id, request_id=_trace_id,
                    ),
                    request_id=_trace_id,
                    tenant_id=tenant_id,
                    # 调用链终点是原子能力（proxy 侧服务名 rag.lightrag 的完整能力 ID）。
                    capability_id="atomic.rag.lightrag.v1",
                    start_time=start_time,
                    source_name=_monitor_source,
                    trigger_type=_monitor_trigger,
                ),
                media_type="application/x-ndjson",
            )

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
