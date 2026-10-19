"""
Auth 路由 — 纯反代到 Sidecar，零业务逻辑。
"""
import json
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from jonex_core.common.config import get_config
from jonex_core.common import transmit_locale_header, passthrough_upstream_response
from api_gateway.deps import raise_from_capability_result

router = APIRouter()


async def _proxy_auth(request: Request, path: str):
    """转发认证请求到 Sidecar"""
    config = get_config()
    sidecar_url = config.SIDECAR_URL

    content_type = request.headers.get("content-type", "")
    is_form = "application/x-www-form-urlencoded" in content_type
    body = None
    raw_body = None
    if request.method in ("POST", "PUT", "PATCH"):
        raw_body = await request.body()
        if raw_body and not is_form:
            try:
                body = json.loads(raw_body)
            except json.JSONDecodeError:
                raise_from_capability_result(
                    {"code": 422, "message": "请求体不是有效的 JSON 格式"}
                )

    headers = {
        "X-API-Key": config.GATEWAY_API_KEY,
        "X-Request-ID": getattr(request.state, "request_id", ""),
        "X-Forwarded-For": request.client.host if request.client else "",
    }
    # 透传用户 Authorization header（如已认证）
    auth_header = request.headers.get("Authorization")
    if auth_header:
        # 同 platform.py：尾部空白会让 httpx 拒发（Illegal header value）。
        headers["Authorization"] = auth_header.strip()
    tenant_id = request.headers.get("X-Tenant-ID") or request.headers.get("x-tenant-id")
    if tenant_id:
        headers["X-Tenant-ID"] = tenant_id
    # 透传 Cookie：refresh/logout 的 httpOnly refresh token 经 cookie 携带，不落 header。
    cookie_header = request.headers.get("cookie")
    if cookie_header:
        headers["cookie"] = cookie_header

    transmit_locale_header(headers)

    # 拼接 query string（OIDC 授权码回调的 code/state 走 query 回传，必须透传）
    target = f"{sidecar_url}/auth/{path}"
    if request.query_params:
        target += f"?{request.query_params}"

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            if request.method == "GET":
                resp = await client.get(target, headers=headers)
            elif is_form and raw_body:
                headers["Content-Type"] = content_type
                resp = await client.request(request.method, target, content=raw_body, headers=headers)
            else:
                resp = await client.request(request.method, target, json=body, headers=headers)
            # 3xx 重定向（OIDC 授权码回调 302 + Set-Cookie）必须透传而非报错：
            # httpx raise_for_status() 对 3xx 也抛 HTTPStatusError，空 body 的
            # resp.json() 会抛 JSONDecodeError。先于 raise_for_status 判断并透传。
            if 300 <= resp.status_code < 400:
                return passthrough_upstream_response(resp)
            resp.raise_for_status()
            # 2xx 且带 Set-Cookie（refresh 旋转 / logout 删 cookie）→ 透传（含 content-type + body），
            # 否则解析 JSON；resp.json() 会丢弃 Set-Cookie。
            if resp.headers.get_list("set-cookie"):
                return passthrough_upstream_response(resp)
            try:
                return resp.json()
            except Exception:
                raise_from_capability_result(
                    {"code": 502, "message": "认证服务返回了非预期的响应格式"}
                )
        except httpx.HTTPStatusError as e:
            try:
                detail = e.response.json()
            except Exception:
                detail = {"message": e.response.text}
            if isinstance(detail, dict) and "success" in detail and "message" in detail:
                return JSONResponse(content=detail, status_code=e.response.status_code)
            raise_from_capability_result(
                {"code": e.response.status_code,
                 "message": detail.get("message", f"认证服务错误: HTTP {e.response.status_code}")}
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
            raise_from_capability_result(
                {"code": 502, "message": f"认证服务不可达: {str(e)}"}
            )


@router.post("/login")
async def login(request: Request):
    """代理到 Sidecar POST /auth/login"""
    return await _proxy_auth(request, "login")


@router.get("/login-config", summary="登录页启动配置（默认租户 / 隐藏租户框 / SSO 入口显隐）")
async def login_config(request: Request):
    """代理到 Sidecar GET /auth/login-config。

    登录页在未登录状态调用，因此本路由不需要 Authorization，也不带租户头；
    值由 platform 侧解析后下发（DEFAULT_TENANT_ID / HIDE_TENANT_INPUT，以及
    SSO 入口显隐 enabled / provider）。
    """
    return await _proxy_auth(request, "login-config")


@router.get("/me")
async def me(request: Request):
    """代理到 Sidecar GET /auth/me，透传 Authorization"""
    return await _proxy_auth(request, "me")


@router.post("/refresh")
async def refresh(request: Request):
    """代理到 Sidecar POST /auth/refresh"""
    return await _proxy_auth(request, "refresh")


@router.post("/login-ticket")
async def login_ticket(request: Request):
    """代理到 Sidecar POST /auth/login-ticket"""
    return await _proxy_auth(request, "login-ticket")


@router.post("/exchange-ticket")
async def exchange_ticket(request: Request):
    """代理到 Sidecar POST /auth/exchange-ticket"""
    return await _proxy_auth(request, "exchange-ticket")


@router.post("/logout")
async def logout(request: Request):
    """代理到 Sidecar POST /auth/logout"""
    return await _proxy_auth(request, "logout")


@router.get("/oidc/authorize")
async def oidc_authorize(request: Request):
    """代理到 Sidecar GET /auth/oidc/authorize（tenant_id/redirect_uri 走 query，302 透传）"""
    return await _proxy_auth(request, "oidc/authorize")


@router.get("/oidc/callback")
async def oidc_callback_get(request: Request):
    """代理到 Sidecar GET /auth/oidc/callback（授权码 code/state 走 query）"""
    return await _proxy_auth(request, "oidc/callback")


@router.post("/oidc/callback")
async def oidc_callback_post(request: Request):
    """代理到 Sidecar POST /auth/oidc/callback（授权码 code/state 走 form body）"""
    return await _proxy_auth(request, "oidc/callback")


@router.post("/oidc/backchannel-logout")
async def oidc_backchannel_logout(request: Request):
    """代理到 Sidecar POST /auth/oidc/backchannel-logout（logout_token 走 form body）"""
    return await _proxy_auth(request, "oidc/backchannel-logout")


@router.post("/impersonate")
async def impersonate(request: Request):
    """代理到 Sidecar POST /auth/impersonate"""
    return await _proxy_auth(request, "impersonate")


@router.post("/impersonate/end")
async def impersonate_end(request: Request):
    """代理到 Sidecar POST /auth/impersonate/end"""
    return await _proxy_auth(request, "impersonate/end")
