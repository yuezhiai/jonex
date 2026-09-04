"""
认证 API 路由（platform 容器内部）

由 Sidecar 代理调用，不直接对外暴露。
"""
from fastapi import APIRouter, Depends, Header, Request

from jonex_core.common.database import get_db
from jonex_core.common.response import success_response
from jonex_core.common.exceptions import InvalidParameterError
from jonex_core.common.i18n import translate
from jonex_core.security.permission import require_permission
from jonex_core.security.user_auth import get_current_user
from capabilities.platform.auth.auth_service import AuthService
from capabilities.platform.dtos.auth import (
    LoginRequest,
    LoginTicketRequest,
    ExchangeTicketRequest,
    ImpersonateRequest,
)

router = APIRouter()


@router.post("/login")
async def login(
    req: LoginRequest,
    request: Request,
    tenant_id: str | None = Header(default=None, alias="X-Tenant-ID"),
    db=Depends(get_db),
):
    if not tenant_id:
        raise InvalidParameterError(message=translate("err.tenant.required", fallback="该请求必须携带明确租户"))  # 原消息: 该请求必须携带明确租户
    svc = AuthService(db)
    # 提取真实客户端 IP（Sidecar 转发的 X-Forwarded-For），fallback 到直连 IP
    client_ip = (
        request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or (request.client.host if request.client else None)
    )
    result = await svc.login(tenant_id, req, client_ip=client_ip)
    return success_response(data=result.dict(), message="success")


@router.get("/me")
async def me(authorization: str = Header(...), db=Depends(get_db)):
    if not authorization.startswith("Bearer "):
        raise InvalidParameterError(message=translate("err.auth.missing_bearer_token", fallback="缺少 Bearer token"))  # 原消息: 缺少 Bearer token
    token = authorization[7:]
    svc = AuthService(db)
    result = await svc.me(token)
    return success_response(data=result.dict())


@router.post("/refresh")
async def refresh(authorization: str = Header(...), db=Depends(get_db)):
    if not authorization.startswith("Bearer "):
        raise InvalidParameterError(message=translate("err.auth.missing_bearer_token", fallback="缺少 Bearer token"))  # 原消息: 缺少 Bearer token
    token = authorization[7:]
    svc = AuthService(db)
    result = await svc.refresh(token)
    return success_response(data=result.dict())


@router.post("/login-ticket")
async def login_ticket(
    req: LoginTicketRequest,
    request: Request,
    authorization: str = Header(...),
    db=Depends(get_db),
):
    if not authorization.startswith("Bearer "):
        raise InvalidParameterError(message=translate("err.auth.missing_bearer_token", fallback="缺少 Bearer token"))  # 原消息: 缺少 Bearer token
    token = authorization[7:]
    client_ip = request.client.host if request and request.client else None
    user_agent = request.headers.get("User-Agent", "")[:1024] if request else None
    svc = AuthService(db)
    result = await svc.create_login_ticket(req, token, client_ip, user_agent)
    return success_response(data=result.dict())


@router.post("/exchange-ticket")
async def exchange_ticket(req: ExchangeTicketRequest, db=Depends(get_db)):
    svc = AuthService(db)
    result = await svc.exchange_ticket(req)
    return success_response(data=result.dict())


@router.post("/logout")
async def logout():
    return success_response(message="已登出")


@router.post("/impersonate")
async def impersonate(
    req: ImpersonateRequest,
    db=Depends(get_db),
    current: dict = Depends(require_permission("platform:admin")),
):
    svc = AuthService(db)
    result = await svc.impersonate(current, req.target_tenant_id)
    return success_response(data=result.dict())


@router.post("/impersonate/end")
async def impersonate_end(
    db=Depends(get_db),
    current: dict = Depends(get_current_user),
):
    svc = AuthService(db)
    await svc.end_impersonation(current)
    return success_response(message="已退出模拟")
