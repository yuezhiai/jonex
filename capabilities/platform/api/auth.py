"""
认证 API 路由（platform 容器内部）

由 Sidecar 代理调用，不直接对外暴露。
"""
from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import RedirectResponse

from jonex_core.common import get_logger
from jonex_core.common.config import get_config
from jonex_core.common.database import get_db
from jonex_core.common.response import success_response
from jonex_core.common.exceptions import (
    ImpersonationRefreshForbiddenError,
    InvalidParameterError,
    OidcAccessDeniedError,
    OidcAuthorizationCodeError,
    OidcStateMismatchError,
    RefreshTokenRevokedError,
)
from jonex_core.common.i18n import translate
from jonex_core.integrations.keycloak_oidc import (
    build_authorization_error,
    sanitize_oidc_error_value,
)
from jonex_core.security.permission import require_permission
from jonex_core.security.user_auth import get_current_user
from capabilities.platform.auth.auth_service import AuthService, build_sso_error_url
from capabilities.platform.dtos.auth import (
    LoginRequest,
    LoginTicketRequest,
    ExchangeTicketRequest,
    ImpersonateRequest,
)

logger = get_logger("platform.api.auth")

router = APIRouter()

# 登录页配置冲突只告警一次（进程级）：配了「隐藏租户框」却没配默认租户时，
# 隐藏会让登录页无法提交（租户是登录必填，见本模块 login 的 X-Tenant-ID 校验），
# 解析时会强制回落成「显示」。
_login_conflict_logged = False


@router.get("/login-config")
async def get_login_config():
    """登录页启动配置（**公开接口**：登录前调用，不校验身份与租户）。

    下发四项：默认租户 ID、是否隐藏租户输入框、企业 SSO 入口是否渲染
    （``enabled`` + ``provider``）。值全部由 platform 侧**解析后**给出——前端不做
    判断，照值渲染即可：

    * ``default_tenant_id`` / ``hide_tenant_input`` 来自环境变量
      ``DEFAULT_TENANT_ID`` / ``HIDE_TENANT_INPUT``（deploy/.env 第 7 节）：
      ``hide_tenant_input`` 只在「确实配了默认租户」时为 true。配了隐藏却没有
      默认租户属于自相矛盾的配置（页面会无法登录），此处强制回落为 false 并
      告警一次，而不是把一个用不了的登录页发出去。
    * ``enabled`` 取值为 ``KeycloakSettings.sso_entry_visible``
      （= 部署级 ``sso_enabled`` **且** 未被 ``HIDE_SSO_LOGIN`` 强制隐藏）；``provider``
      为常量 ``"keycloak"``。⚠️ 键名含义不变 —— ``enabled`` 是**入口可见性**，
      **不是** ``sso_enabled``：未配 Keycloak 时恒为 false，配了 Keycloak 时仍可被
      ``HIDE_SSO_LOGIN`` 单独隐藏（默认隐藏，只有显式 ``=false`` 才展示）。

    ⛔ 不得回传 authorize URL / client_id / secret：本端点在登录页未认证时即可访问。
    """
    global _login_conflict_logged
    config = get_config()
    if config.login_page_config_conflict and not _login_conflict_logged:
        _login_conflict_logged = True
        logger.warning(
            "HIDE_TENANT_INPUT 为 true 但 DEFAULT_TENANT_ID 为空：隐藏租户输入框会"
            "导致登录无法提交（租户是登录必填），已强制回落为显示输入框。"
            "请设置 DEFAULT_TENANT_ID，或显式设 HIDE_TENANT_INPUT=false。"
        )
    data = dict(config.resolved_login_page_config)
    data["enabled"] = bool(config.sso_entry_visible)
    data["provider"] = "keycloak"
    return success_response(data=data)
# 模拟态标记 cookie 值（契约 §2.1 固定为 "1"）：只有字面量 "1" 才算命中，
# 避免残留的 "0" 之类脏值误触发拒绝。
_IMPERSONATION_MARKER_VALUE = "1"
# 与 `KeycloakSettings.IMPERSONATION_MARKER_COOKIE_NAME` 的默认值保持一致（见 config.py）。
_DEFAULT_MARKER_COOKIE_NAME = "jonex_impersonating"


def _is_impersonated_request(request: Request) -> bool:
    """判定请求是否来自模拟态（单一载体：标记 cookie）。

    载体 = 标记 cookie（``IMPERSONATION_MARKER_COOKIE_NAME``）：由
    ``/auth/impersonate`` 种下、``/auth/impersonate/end`` 清除，带
    ``IMPERSONATE_EXPIRE_MINUTES`` TTL ⇒ ``/end`` 失败时自动过期，不永久锁死续期。

    ⚠️ fail-closed：命中即判为模拟态（判不准就不续期）。
    """
    config = get_config()
    # 兜底取键：与 `KeycloakSettings.IMPERSONATION_MARKER_COOKIE_NAME` 的默认值一致。
    # 真实 `Settings` 恒有该键，兜底只为容忍「手写的窄配置替身」（既有单测里大量
    # 用 SimpleNamespace 拼部分配置），避免为新增一个键去改动非本单范围的既有用例。
    marker_name = getattr(
        config, "IMPERSONATION_MARKER_COOKIE_NAME", _DEFAULT_MARKER_COOKIE_NAME
    )
    return request.cookies.get(marker_name) == _IMPERSONATION_MARKER_VALUE


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

    # 本地密码登录也下发 refresh httpOnly cookie（与 SSO oidc_callback 同口径），
    # 使本地登录用户的 access 过期后能走 /auth/refresh 续期。
    # 必须在返回的 JSONResponse 上 set_cookie：FastAPI 返回 Response 对象时丢弃注入的
    # response 参数，注入式 set_cookie 会静默失效（refresh cookie 到不了浏览器）。
    config = get_config()
    resp = success_response(data=result.dict(), message="success")
    resp.set_cookie(
        key=config.KEYCLOAK_REFRESH_COOKIE_NAME,
        value=result.refresh_token,
        max_age=config.USER_JWT_REFRESH_DAYS * 86400,
        path=config.KEYCLOAK_REFRESH_COOKIE_PATH,
        httponly=True,
        secure=config.AUTH_COOKIE_SECURE,
        samesite=config.AUTH_COOKIE_SAMESITE.lower(),
    )
    return resp


@router.get("/me")
async def me(authorization: str = Header(...), db=Depends(get_db)):
    if not authorization.startswith("Bearer "):
        raise InvalidParameterError(message=translate("err.auth.missing_bearer_token", fallback="缺少 Bearer token"))  # 原消息: 缺少 Bearer token
    token = authorization[7:].strip()
    svc = AuthService(db)
    result = await svc.me(token)
    return success_response(data=result.dict())


@router.post("/refresh")
async def refresh(
    request: Request,
    db=Depends(get_db),
):
    config = get_config()
    # ── 模拟态禁止续期（纵深防御层）──────────────────────────────────────────
    # 模拟态没有任何 refresh 凭证，放行就是用「原租户 refresh cookie」换回原租户
    # access token ⇒ 界面显示目标租户、请求以原租户身份落地（跨租户静默读写）。
    # 主逻辑在前端短路（模拟态根本不发本请求），本闸覆盖旧前端 / SPA 缓存旧 dist。
    # 判据只有标记 cookie 一个载体（请求头载体已废弃删除：网关/侧车 header 白名单
    # 不转发它，经 浏览器→网关→侧车→platform 永远到不了，是文档债）。
    # ⛔ 必须 403（ImpersonationRefreshForbiddenError, 3026）而**不是 401**：401 会命中
    # 前端 request.ts 的登出分支 → clearAuthStorage() → 删掉原租户 token 唯一副本
    # （JONEX_IMPERSONATION_BACKUP_KEY），管理员被登出且回不到原租户。
    # fail-closed：拒绝时**不续期、不改动任何 cookie**，且先于「缺 refresh token」判定
    # （模拟态下语义更准确，不误报 3019）。
    if _is_impersonated_request(request):
        raise ImpersonationRefreshForbiddenError()
    refresh_token = request.cookies.get(config.KEYCLOAK_REFRESH_COOKIE_NAME)
    if not refresh_token:
        raise RefreshTokenRevokedError(
            message=translate("err.auth.missing_refresh_token", fallback="缺少 refresh token")
        )
    svc = AuthService(db)
    result = await svc.refresh(refresh_token)

    # 下发新 refresh cookie（httpOnly + Secure + SameSite）；access 走内存态返回 body，不回传 refresh。
    # 必须在返回的 JSONResponse 上 set_cookie：FastAPI 返回 Response 对象时丢弃注入的 response 参数，
    # 注入式 set_cookie 会静默失效（refresh cookie 到不了浏览器）。
    resp = success_response(
        data={
            "status": result.status,
            "access_token": result.access_token,
            "token_type": result.token_type,
            "expires_in": result.expires_in,
            "user": result.user.dict(),
        }
    )
    resp.set_cookie(
        key=config.KEYCLOAK_REFRESH_COOKIE_NAME,
        value=result.refresh_token,
        max_age=config.USER_JWT_REFRESH_DAYS * 86400,
        path=config.KEYCLOAK_REFRESH_COOKIE_PATH,
        httponly=True,
        secure=config.AUTH_COOKIE_SECURE,
        samesite=config.AUTH_COOKIE_SAMESITE.lower(),
    )
    return resp


@router.post("/login-ticket")
async def login_ticket(
    req: LoginTicketRequest,
    request: Request,
    authorization: str = Header(...),
    db=Depends(get_db),
):
    if not authorization.startswith("Bearer "):
        raise InvalidParameterError(message=translate("err.auth.missing_bearer_token", fallback="缺少 Bearer token"))  # 原消息: 缺少 Bearer token
    token = authorization[7:].strip()
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
async def logout(request: Request, db=Depends(get_db)):
    """登出：吊销本地 refresh 家族 + 清 httpOnly cookie + 交还 Keycloak SSO 登出 URL。

    为什么不是 302 而是 JSON：本端点由前端 fetch 调用（跨域跳转会被浏览器当 fetch
    重定向处理，Keycloak 的 Set-Cookie 到不了浏览器，SSO 会话不会结束）。要真正结束
    Keycloak 会话必须由**浏览器顶层导航**发起，故这里把 ``sso_logout_url`` 放在
    ``data`` 里由前端 ``window.location.assign`` 跳转 —— 端点契约（POST + JSON）不变，
    老调用方忽略该字段即退化为原行为。

    ``sso_logout_url`` 为 None 的两种情况：本地密码登录（本就无 Keycloak 会话）/
    Keycloak 配置缺失（降级为仅本地登出）。**本地吊销与清 cookie 不受其影响**。
    """
    config = get_config()
    refresh_token = request.cookies.get(config.KEYCLOAK_REFRESH_COOKIE_NAME)
    id_token = request.cookies.get(config.KEYCLOAK_ID_TOKEN_COOKIE_NAME)
    sso_logout_url = None
    if refresh_token:
        svc = AuthService(db)
        sso_logout_url = await svc.logout(refresh_token, id_token_hint=id_token)
    # 无论是否携带 refresh token，都清掉 httpOnly cookie，杜绝登出后凭旧 cookie 续期。
    # 必须在返回的 JSONResponse 上 delete_cookie：FastAPI 返回 Response 对象时丢弃注入的 response 参数。
    resp = success_response(data={"sso_logout_url": sso_logout_url}, message="已登出")
    resp.delete_cookie(
        key=config.KEYCLOAK_REFRESH_COOKIE_NAME,
        path=config.KEYCLOAK_REFRESH_COOKIE_PATH,
    )
    # id_token 同属本次会话的凭据：登出后一并清掉，避免下次登录前被误用。
    resp.delete_cookie(
        key=config.KEYCLOAK_ID_TOKEN_COOKIE_NAME,
        path=config.KEYCLOAK_REFRESH_COOKIE_PATH,
    )
    return resp


@router.post("/oidc/backchannel-logout")
async def backchannel_logout(request: Request, db=Depends(get_db)):
    """Keycloak Back-Channel Logout 回调：验签 logout_token → 吊销本地会话。

    端点不挂用户 JWT/API-Key，唯一鉴权是 logout_token 验签（service 内完成）。
    body 为 form 编码 ``logout_token=<JWT>``，用 parse_qs 直接解析（不依赖 python-multipart）。
    """
    from urllib.parse import parse_qs

    from jonex_core.security.oidc import TokenValidationError

    raw = await request.body()
    form = parse_qs(raw.decode("utf-8"))
    logout_token = (form.get("logout_token") or [None])[0]
    if not logout_token:
        raise TokenValidationError()

    svc = AuthService(db)
    await svc.backchannel_logout(logout_token)
    return success_response(message="success")


@router.get("/oidc/authorize")
async def oidc_authorize(
    tenant_id: str = Query(...),
    redirect_uri: str = Query(...),
    db=Depends(get_db),
):
    """Keycloak OIDC 授权码发起：生成 state/nonce/PKCE → 302 到 Keycloak authorize。

    端点不挂用户 JWT/API-Key；tenant_id/redirect_uri 走 query（非秘密）。redirect_uri
    是登录成功后跳转的 SPA 地址（post_login_redirect_uri），OIDC 回调地址由 service 读
    KEYCLOAK_REDIRECT_URI。返回 302 到 Keycloak authorize URL。
    """
    svc = AuthService(db)
    url = await svc.oidc_authorize(tenant_id, post_login_redirect_uri=redirect_uri)
    return RedirectResponse(url=url, status_code=302)


@router.get("/oidc/callback")
@router.post("/oidc/callback")
async def oidc_callback(request: Request, db=Depends(get_db)):
    """Keycloak OIDC 授权码回调：校验 state/nonce/PKCE → 兑换 → 设 refresh cookie → 302。

    端点不挂用户 JWT/API-Key，唯一鉴权链是 state 一次性票据 + id_token 验签（service 内完成）。
    成功后只下发 refresh httpOnly cookie，access 由 SPA 持 cookie 走 /auth/refresh 启动。
    """
    from urllib.parse import parse_qs

    # code/state（+ IdP 拒绝时的 error/error_description）从 query（GET）或 form body（POST）
    # 提取；回跳 URL 上的 code 用后即弃。
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    oidc_error = request.query_params.get("error")
    oidc_error_description = request.query_params.get("error_description")
    if request.method == "POST":
        raw = await request.body()
        form = parse_qs(raw.decode("utf-8"))
        code = code or (form.get("code") or [None])[0]
        state = state or (form.get("state") or [None])[0]
        oidc_error = oidc_error or (form.get("error") or [None])[0]
        oidc_error_description = oidc_error_description or (
            form.get("error_description") or [None]
        )[0]

    # IdP 拒绝登录时不回 code，只回 error/error_description（Keycloak「浏览器已有会话」=
    # already_logged_in → error=temporarily_unavailable&error_description=authentication_expired）。
    # 先无条件落日志再分支：否则现场只剩「授权码兑换失败」，加权限流/会话冲突无从区分。
    # 日志只记 state 前 8 位（state 是一次性 CSRF 票据，不回填敏感值）。
    if oidc_error:
        logger.warning(
            "OIDC 回调返回错误：error=%s error_description=%s state_prefix=%s",
            sanitize_oidc_error_value(oidc_error),
            sanitize_oidc_error_value(oidc_error_description),
            (state or "")[:8],
        )

    if not state:
        raise OidcStateMismatchError()
    if oidc_error:
        # 会话冲突 / 授权码过期重放 / 未知 error 各归到独立错误码与文案（见 keycloak_oidc 映射表）
        raise build_authorization_error(oidc_error, oidc_error_description)
    if not code:
        raise OidcAuthorizationCodeError()

    svc = AuthService(db)
    try:
        result = await svc.oidc_callback(code, state)
    except OidcAccessDeniedError as exc:
        # IdP 认证已通过、平台侧不放行：这是用户能看懂、也能自己解决的一档，
        # 转成一张说人话的错误页而不是裸 JSON。
        #
        # 拒绝事件必须在这里留痕：改前它经全局异常处理器打「业务异常: code=3022/3013/3021」，
        # 现在被本块整段接走（异常不再外泄），而 _record_login_audit 只在 SUCCESS 路径落审计，
        # 不补这条日志，认证路径上的拒绝就在日志里彻底静默。
        # 只记 reason + tenant：路由层拿不到用户名，且没必要把 PII 带进日志。
        logger.warning("SSO 无访问权限: reason=%s tenant=%s", exc.reason, exc.tenant_id)
        error_url = build_sso_error_url(
            exc.post_login_redirect_uri, exc.reason, exc.tenant_id
        )
        if error_url is None:
            # 落点页解析不出可信 origin —— 宁可不跳转（退化到 JSON），
            # 也不能拿它拼一个畸形 302 把用户送到未知地址。
            raise
        return RedirectResponse(url=error_url, status_code=302)

    config = get_config()
    # 必须在返回的 RedirectResponse 上 set_cookie：FastAPI 返回 Response 对象时丢弃注入的 response 参数。
    resp = RedirectResponse(url=result["post_login_redirect_uri"], status_code=302)
    resp.set_cookie(
        key=config.KEYCLOAK_REFRESH_COOKIE_NAME,
        value=result["refresh_token"],
        max_age=config.USER_JWT_REFRESH_DAYS * 86400,
        path=config.KEYCLOAK_REFRESH_COOKIE_PATH,
        httponly=True,
        secure=config.AUTH_COOKIE_SECURE,
        samesite=config.AUTH_COOKIE_SAMESITE.lower(),
    )
    # id_token 只服务登出：作为 RP-initiated logout 的 id_token_hint 证明「我是刚登录的
    # 那个 Keycloak 会话」，否则 Keycloak 26 会先弹登出确认页。httpOnly，JS 读不到；
    # 生命周期对齐 refresh cookie（logout 时一并删除）。
    resp.set_cookie(
        key=config.KEYCLOAK_ID_TOKEN_COOKIE_NAME,
        value=result["id_token"],
        max_age=config.USER_JWT_REFRESH_DAYS * 86400,
        path=config.KEYCLOAK_REFRESH_COOKIE_PATH,
        httponly=True,
        secure=config.AUTH_COOKIE_SECURE,
        samesite=config.AUTH_COOKIE_SAMESITE.lower(),
    )
    return resp


@router.post("/impersonate")
async def impersonate(
    req: ImpersonateRequest,
    db=Depends(get_db),
    current: dict = Depends(require_permission("platform:admin")),
):
    svc = AuthService(db)
    result = await svc.impersonate(current, req.target_tenant_id)
    # 额外种「模拟态标记」cookie（body 契约不变）：供 /auth/refresh 的纵深防御层判定。
    # 属性见契约 §2.1 —— Max-Age 取 IMPERSONATE_EXPIRE_MINUTES*60（与模拟 access token
    # 同生命周期，/end 失败时自动过期兜底）；Path 与 refresh cookie 一致 ⇒ 同行自动携带。
    # ⚠️ 必须在返回的 JSONResponse 上 set_cookie：FastAPI 返回 Response 对象时会丢弃注入
    # 的 response 参数，注入式 set_cookie 会静默失效（标记到不了浏览器）。
    config = get_config()
    resp = success_response(data=result.dict())
    resp.set_cookie(
        key=config.IMPERSONATION_MARKER_COOKIE_NAME,
        value=_IMPERSONATION_MARKER_VALUE,
        max_age=config.IMPERSONATE_EXPIRE_MINUTES * 60,
        path=config.KEYCLOAK_REFRESH_COOKIE_PATH,
        httponly=True,
        secure=config.AUTH_COOKIE_SECURE,
        samesite=config.AUTH_COOKIE_SAMESITE.lower(),
    )
    return resp


@router.post("/impersonate/end")
async def impersonate_end(
    db=Depends(get_db),
    current: dict = Depends(get_current_user),
):
    svc = AuthService(db)
    await svc.end_impersonation(current)
    # 清「模拟态标记」cookie（现有行为与 body 不变）：还原后原租户续期重新合法。
    # 只清标记，**不动 jonex_refresh / jonex_id_token** —— 原租户的 refresh cookie
    # 是回到原租户的凭证，清了就掉登录页。
    # ⚠️ 同上：必须在返回的 JSONResponse 上 delete_cookie。
    config = get_config()
    resp = success_response(message="已退出模拟")
    resp.delete_cookie(
        key=config.IMPERSONATION_MARKER_COOKIE_NAME,
        path=config.KEYCLOAK_REFRESH_COOKIE_PATH,
    )
    return resp
