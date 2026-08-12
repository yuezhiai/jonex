"""
Auth 路由 — 纯反代到 Sidecar，零业务逻辑。
"""
import json
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from jonex_core.common.config import get_config
from jonex_core.common import transmit_locale_header
from api_gateway.deps import raise_from_capability_result

router = APIRouter()


async def _proxy_auth(request: Request, path: str):
    """转发认证请求到 Sidecar"""
    config = get_config()
    sidecar_url = config.SIDECAR_URL

    body = None
    if request.method in ("POST", "PUT", "PATCH"):
        raw = await request.body()
        if raw:
            try:
                body = json.loads(raw)
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
        headers["Authorization"] = auth_header
    tenant_id = request.headers.get("X-Tenant-ID") or request.headers.get("x-tenant-id")
    if tenant_id:
        headers["X-Tenant-ID"] = tenant_id

    transmit_locale_header(headers)

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            if request.method == "GET":
                resp = await client.get(f"{sidecar_url}/auth/{path}", headers=headers)
            else:
                resp = await client.request(request.method, f"{sidecar_url}/auth/{path}", json=body, headers=headers)
            resp.raise_for_status()
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
