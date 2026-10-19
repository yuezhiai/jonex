"""
认证服务 — 独立运行在 platform 容器中，通过 Sidecar 代理调用。
"""
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse

from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.config import get_config
from jonex_core.common.exceptions import (
    AccountDisabledError,
    AccountLockedError,
    ImpersonationForbiddenError,
    ImpersonationTargetError,
    InternalError,
    InvalidApiKeyError,
    InvalidCredentialsError,
    InvalidParameterError,
    JonexException,
    NotImpersonatedError,
    OidcAccessDeniedError,
    OidcAuthorizationCodeError,
    OidcNonceMismatchError,
    OidcPkceVerifierError,
    OidcShadowUserConflictError,
    OidcStateMismatchError,
    OidcSyncRequiredError,
    PermissionDeniedError,
    RealmTenantMismatchError,
    RefreshTokenRevokedError,
    TokenExpiredError,
    UpstreamServiceError,
)
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant
from capabilities.platform.repository.user_repository import UserRepository
from capabilities.platform.repository.tenant_repository import TenantRepository
from capabilities.platform.repository.system_config_repository import SystemConfigRepository
from capabilities.platform.models.user import User
from capabilities.platform.models.login_ticket import LoginTicket
from capabilities.platform.models.identity_integration import (
    IdentityIntegrationConfig,
    LoginPolicy,
)
from capabilities.platform.services.user_service import UserService
from capabilities.platform.services.role_binding import ensure_default_role_binding
from capabilities.platform.services.security_config_service import SecurityConfigService
from jonex_core.security.user_auth import get_user_auth
from capabilities.platform.dtos.auth import (
    LoginRequest,
    LoginResponse,
    LoginTicketRequest,
    LoginTicketResponse,
    ExchangeTicketRequest,
    ImpersonateResponse,
    UserInfo,
)

logger = logging.getLogger(__name__)

# 管理员标识码（详见 docs/permissions/PERMISSIONS_REDESIGN.md）
_PLATFORM_ADMIN_CODE = "platform:admin"
_TENANT_ADMIN_CODE = "tenant:admin"


def _is_platform_admin(perms: set[str]) -> bool:
    """平台管理员布尔（下发给前端 shellContext）。"""
    return _PLATFORM_ADMIN_CODE in perms


def _is_tenant_admin(perms: set[str]) -> bool:
    """租户管理员布尔（下发给前端 shellContext）。

    权限重构变更：判据从 `user:write` 换为 `tenant:admin`（platform:admin 为上位）。

    为什么这是个修复而非改名：现状前端看 `user:write`、后端
    `space_permission_service.is_tenant_admin` 看 `tenant:write`，两套口径。
    自定义角色只配 `user:write` 时，前端把人当租户管理员渲染（显示全部管理入口），
    后端却拒绝 → 用户看得到点不动。现在两边看同一组码，必须保持同步：
    改这里就要同步改 space_permission_service._TENANT_ADMIN，反之亦然。
    回归护栏见 tests/unit/test_tenant_admin_code.py::TestFrontBackAgreement。

    提取成模块级纯函数的原因：原先这个表达式在 login() 与 me() 两处内联，
    改一处漏一处（现状就是这么来的）。
    """
    return _TENANT_ADMIN_CODE in perms or _is_platform_admin(perms)


def _pkce_s256_challenge(code_verifier: str) -> str:
    """PKCE S256：``code_challenge = base64url(sha256(ASCII(code_verifier)))``。"""
    import base64
    import hashlib

    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _generate_state() -> str:
    """生成 OIDC 授权请求的随机 ``state``（防 CSRF，一次性票据键）。"""
    return secrets.token_urlsafe(32)


def _generate_nonce() -> str:
    """生成 OIDC 授权请求的随机 ``nonce``（id_token 防重放）。"""
    return secrets.token_urlsafe(16)


def _generate_code_verifier() -> str:
    """生成 PKCE ``code_verifier``。

    ``token_urlsafe(64)`` 产出约 86 个 base64url 字符，落在 RFC 7636 的 43-128
    区间内；base64url 仅含 ``-``/``_`` 等 unreserved 字符，ASCII 安全。
    """
    return secrets.token_urlsafe(64)


def _redirect_uri_allowed(redirect_uri: str, allowed_uris: list[str]) -> bool:
    """redirect 白名单校验：scheme + host + port 精确相等，path 前缀匹配。

    用 urlparse 而非 startswith，杜绝 ``https://app.example.com.evil.com``、
    ``https://app.example.com@evil.com`` 等前缀绕过（前者 host 解析为
    ``app.example.com.evil.com``，后者 userinfo 前段不被当作 host）。
    """
    from urllib.parse import urlparse

    try:
        target = urlparse(redirect_uri)
    except ValueError:
        return False
    if target.scheme not in ("http", "https") or not target.hostname:
        return False
    try:
        target_port = target.port
    except ValueError:
        return False

    for u in allowed_uris:
        try:
            allowed = urlparse(u)
        except ValueError:
            continue
        if allowed.scheme != target.scheme or not allowed.hostname:
            continue
        try:
            allowed_port = allowed.port
        except ValueError:
            continue
        if (
            allowed.hostname == target.hostname
            and allowed_port == target_port
            and _path_prefix_match(allowed.path, target.path)
        ):
            return True
    return False


def _path_prefix_match(prefix: str, path: str) -> bool:
    """path 前缀匹配（边界对齐，防 /apps/core 误匹配 /apps/coreevil）。

    空前缀或 "/" 视为任意 path 放行（白名单只配 origin 时的语义）。
    """
    if not prefix or prefix == "/":
        return True
    if not path.startswith(prefix):
        return False
    if len(path) == len(prefix) or prefix.endswith("/"):
        return True
    return path[len(prefix)] == "/"


# ==================== SSO 无访问权限错误页 ====================

# OidcAccessDeniedError.reason 的三个取值，与前端 ssoErrorModel.ts 的 SSO_ERROR_REASONS 一一对应。
# 改这里必须同步改前端常量，否则页面会回落到通用兜底文案。
SSO_ERROR_REASON_SYNC_REQUIRED = "sync_required"
SSO_ERROR_REASON_ACCOUNT_DISABLED = "account_disabled"
SSO_ERROR_REASON_IDENTITY_CONFLICT = "identity_conflict"

# 三档业务拒绝异常 → reason。用**异常类本身**做键、而不取 ``exc.code``：
# ``except`` 元组就是按类型匹配的，键也该是类型，两者对齐才不会出现「捕获了 A、却按 B 的码
# 查到 C 的 reason」这种静默错配（两个类共用同一个 code 时就会这样）；也不需要写死数字，
# 码值调整时这里不受影响。三个类本就在本模块顶部已导入。
_ACCESS_DENIED_REASON_BY_TYPE = {
    OidcSyncRequiredError: SSO_ERROR_REASON_SYNC_REQUIRED,
    AccountDisabledError: SSO_ERROR_REASON_ACCOUNT_DISABLED,
    OidcShadowUserConflictError: SSO_ERROR_REASON_IDENTITY_CONFLICT,
}


def build_sso_error_url(
    post_login_redirect_uri: str,
    reason: str,
    tenant_id: Optional[str] = None,
) -> Optional[str]:
    """把 OIDC 登录落点页折算成 Shell 的 SSO 错误页地址。

    **丢弃 path 而不是拼接**：``post_login_redirect_uri`` 是「登录成功后的落点页」，
    可能带路径（如 ``/apps/core-business``）；错误页是 Shell 的顶级路由，只依赖 origin。

    该地址在 ``oidc_callback`` 第 2 步已过 ``AUTH_ALLOWED_REDIRECT_URIS`` 白名单校验，
    是可信来源，故此处仅做「能否解析出 origin」的形态校验。

    返回 ``None`` 表示解析不出可信 origin（空串 / 相对路径 / 非 http(s) scheme / 无 host）——
    调用方据此退回 JSON 响应，**不要**拿它去拼一个畸形 302。
    """
    from urllib.parse import urlencode, urlsplit

    try:
        parts = urlsplit(post_login_redirect_uri)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None

    query = {"reason": reason}
    if tenant_id:
        query["tenant"] = tenant_id
    return f"{parts.scheme}://{parts.netloc}/sso-error?{urlencode(query)}"


def _issuer_hosts_from_whitelist(raw: str) -> set[str]:
    """从 KEYCLOAK_ISSUER_WHITELIST（JSON dict，key 为 issuer URL）解析合法 host 集合。

    用于 refresh 未验签链路反查租户前的 host 白名单校验（防任意 host 伪造
    ``/realms/jonex-{租户}`` 映射到合法租户）。解析失败 / 空 / 非 dict 返回空集，
    fail-closed 由调用方处理（空集即拒绝反查）。
    """
    if not raw:
        return set()
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return set()
    if not isinstance(data, dict):
        return set()
    hosts: set[str] = set()
    for issuer in data:
        try:
            host = urlparse(issuer).hostname
        except ValueError:
            continue
        if host:
            hosts.add(host)
    return hosts


class AuthService:
    """平台认证服务"""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.user_repo = UserRepository(session)
        self.user_service = UserService(session)
        self.tenant_repo = TenantRepository(session)
        self.system_config_repo = SystemConfigRepository(session)
        # 安全配置（租户覆盖 → 平台默认 → 代码默认）：默认值定义在 SecurityConfigService，
        # 本类不再自带默认值 —— 改造前「这里 30 分钟 / USER_JWT_EXPIRE_HOURS 24 小时」
        # 两处默认值互相矛盾，正是「改了配置不生效」的成因。
        self.security_config_service = SecurityConfigService(session)
        self.user_auth = get_user_auth()
        self.config = get_config()

    async def get_user_by_external_id(self, tenant_id: str, external_id: str) -> Optional[User]:
        """按 Keycloak sub（external_id）映射本地影子用户，供 refresh / Sidecar 复用。"""
        return await self.user_repo.get_by_external_id(tenant_id, external_id)

    async def _get_login_policy(self, tenant_id: str) -> str:
        """读取租户 SSO 首登账号策略（默认 auto_create）。

        查询 platform.identity_integration_config.login_policy；未配置 / 空值兜底
        auto_create。synced_only 仅允许已同步（存量影子）用户登录，禁止自动建号。
        """
        from sqlalchemy import select

        result = await self.session.execute(
            select(IdentityIntegrationConfig.login_policy).where(
                IdentityIntegrationConfig.tenant_id == tenant_id
            )
        )
        policy = result.scalar_one_or_none()
        return policy or LoginPolicy.AUTO_CREATE.value

    async def provision_shadow_user(
        self,
        tenant_id: str,
        preferred_username: str,
        external_id: str,
        allow_create: bool = True,
        display_name: Optional[str] = None,
        email: Optional[str] = None,
        upstream_subject: Optional[str] = None,
    ) -> User:
        """首登影子用户供给。

        顺序（J 侧入口只有 Keycloak 的 ``sub`` ⇒ ``external_id``，拿不到上游主体就无从
        先查，故这里没有「① upstream_subject 命中」这一档；出站只用于**补写**，不用于匹配）：
        1. 先按 external_id 查（幂等 / 并发兜底），命中即补写上游主体标识后返回；
        2. 再按 preferred_username 同租户匹配存量用户：命中过本地密码账号（password_hash
           非空）⇒ 拒绝；命中影子账号 ⇒ 解析上游主体标识后决定放行 / 拒绝 / 补写 + 换 sub；
        3. 都未命中：allow_create=False（synced_only 策略）抛 OidcSyncRequiredError，
           否则新建影子用户（password_hash=None、role=user、status=1）并一并写入上游主体标识。

        上游主体标识（``upstream_subject``，跨 realm 稳定）与下游账号标识
        （``external_id`` = KC ``sub``，跨 realm 漂移）是**双锚点**：
        - 形参显式传入时**优先采用、不出站**；为空才走 :meth:`_resolve_upstream_subject` 解析。
        - ⛔ 出站只在「确实要写 ``upstream_subject`` 的那一刻」发生：已有锚点的存量账号
          每次登录**一次都不查**（否则每次登录都卡在 KEYCLOAK_TIMEOUT 上）。
        - 解析失败一律留空、**不阻断登录**（上游不可用不能变成平台登录不可用）。

        IdP 字段映射：display_name/email 来自 id_token 的 name/email claim，仅在其非空时
        写入（「IdP 为准的 refresh，空不覆盖」），用于补齐存量影子用户缺失的展示名/邮箱。

        并发兜底：新建 flush 若触发 (tenant_id, external_id) 唯一约束冲突（IntegrityError），
        回滚后重查 external_id 返回已存在用户，不裸抛数据库异常。
        """
        from sqlalchemy.exc import IntegrityError

        tenant_id = require_tenant(tenant_id)

        existing = await self.user_repo.get_by_external_id(tenant_id, external_id)
        if existing is not None:
            # 存量影子用户（已建号）补字段：仅非空覆盖，空不抹掉已有值。
            if display_name:
                existing.display_name = display_name
            if email:
                existing.email = email
            # 锚点为空的存量账号顺手补写；非空 ⇒ 一次都不出站（绝大多数登录走这条）。
            if existing.upstream_subject is None:
                resolved = await self._resolve_for_write(
                    tenant_id, external_id, upstream_subject
                )
                if resolved:
                    existing.upstream_subject = resolved
            return existing

        by_username = await self.user_repo.get_by_username(tenant_id, preferred_username)
        if by_username is not None:
            # 仅绑定影子用户：命中本地密码账号（password_hash 非空）→ 拒绝复用，防账号接管。
            if by_username.password_hash is not None:
                raise OidcShadowUserConflictError(
                    message=translate(
                        "err.auth.shadow_user_conflict",
                        fallback="登录失败，账号身份冲突，请联系管理员",
                    )
                )
            # 走到这里 ⇒ get_by_external_id 未命中而过 username 命中 ⇒ 必然是 KC sub 变了
            # （换 realm / 账号删后重建）。新 sub 的上游主体标识要与存量锚点比对后才能决定
            # 放行还是拒绝 —— 判据是 upstream_subject，⛔ 不是 external_id（它必然对不上）。
            resolved = await self._resolve_for_write(
                tenant_id, external_id, upstream_subject
            )
            if resolved is None:
                # 解析不到 ⇒ 无法确认是同一个人，严判不猜（宽进会让换 sub 变成接管同名账号）。
                raise OidcShadowUserConflictError(
                    message=translate(
                        "err.auth.shadow_user_conflict",
                        fallback="登录失败，账号身份冲突，请联系管理员",
                    )
                )
            if by_username.upstream_subject is None:
                # 本地锚点为空 ⇒ 接受，但先做唯一性护栏：该上游主体若已被**别人**占用，
                # 放过它等于让两个账号指向同一个人。
                owner = await self.user_repo.get_by_upstream_subject(
                    tenant_id, resolved
                )
                if owner is not None and owner.id != by_username.id:
                    raise OidcShadowUserConflictError(
                        message=translate(
                            "err.auth.shadow_user_conflict",
                            fallback="登录失败，账号身份冲突，请联系管理员",
                        )
                    )
            elif resolved != by_username.upstream_subject:
                # 锚点明确不符 ⇒ 真冲突（同名不同人）。
                raise OidcShadowUserConflictError(
                    message=translate(
                        "err.auth.shadow_user_conflict",
                        fallback="登录失败，账号身份冲突，请联系管理员",
                    )
                )
            by_username.upstream_subject = by_username.upstream_subject or resolved
            by_username.external_id = external_id  # 换 sub
            if display_name:
                by_username.display_name = display_name
            if email:
                by_username.email = email
            await self.session.flush()
            return by_username

        # 3) 都未命中：synced_only（allow_create=False）拒绝未同步用户，否则新建影子用户。
        if not allow_create:
            raise OidcSyncRequiredError(
                message=translate(
                    "err.auth.sync_required",
                    fallback="当前租户仅允许已同步用户登录，请联系管理员",
                )
            )

        user = User(
            tenant_id=tenant_id,
            username=preferred_username,
            external_id=external_id,
            upstream_subject=await self._resolve_for_write(
                tenant_id, external_id, upstream_subject
            ),
            password_hash=None,
            role="user",
            status=1,
            display_name=display_name,
            email=email,
        )
        self.session.add(user)
        try:
            await self.session.flush()
        except IntegrityError:
            await self.session.rollback()
            existing = await self.user_repo.get_by_external_id(tenant_id, external_id)
            if existing is not None:
                return existing
            raise InternalError(message="影子用户供给触发唯一约束冲突且重查未命中")

        # 建号后补默认角色绑定（否则新用户零角色、权限集为空、侧边栏全空，
        # 且全程 HTTP 200）。必须放在 flush 成功之后 —— 上面的 except 分支会 rollback，
        # 同 session 里已 add 的绑定会一起被抹掉。上面两个命中分支是复用既有用户，
        # 按「不改写已有绑定」的既定口径不碰角色。
        await ensure_default_role_binding(
            self.user_service, tenant_id, user.id, source="oidc_jit"
        )
        return user

    async def login(self, tenant_id: str, req: LoginRequest, client_ip: str | None = None) -> LoginResponse:
        """登录：必须显式指定租户（Sidecar 已在入口拦截缺失租户，这里兜底再校验）。

        安全配置真实消费：
        - login_lock_threshold / lock_duration：连续失败锁定（Redis 计数，降级不锁定）
        - password_min_length：登录时校验密码最小长度
        - session_timeout：访问令牌有效期（分钟）
        """
        tenant_id = require_tenant(tenant_id)
        security = await self._get_security_config(tenant_id)
        username = (req.username or "").strip()

        # 1) 账户锁定检查
        lock_minutes = await self._account_lock_remaining(tenant_id, username, security["lock_duration"])
        if lock_minutes is not None:
            await self._record_login_audit(
                tenant_id, None, client_ip, "FAILED",
                error_message="账户已锁定", username=username,
            )
            raise AccountLockedError(
                message=translate(
                    "err.auth.account_locked",
                    params={"minutes": lock_minutes},
                    fallback=f"账户已锁定，请在 {lock_minutes} 分钟后重试",
                )
            )

        # 2) 密码最小长度校验
        if len(req.password or "") < security["password_min_length"]:
            await self._record_login_audit(
                tenant_id, None, client_ip, "FAILED",
                error_message="密码长度不足", username=username,
            )
            raise InvalidParameterError(
                message=translate(
                    "err.auth.password_too_short",
                    params={"min": security["password_min_length"]},
                    fallback=f"密码长度不能少于 {security['password_min_length']} 位",
                )
            )

        try:
            user = await self.user_repo.get_by_username(tenant_id, username)
            if not user or not self.user_auth.verify_password(req.password, user.password_hash):
                if security["login_lock_threshold"] > 0:
                    triggered = await self._register_login_failure(
                        tenant_id, username, security["login_lock_threshold"], security["lock_duration"]
                    )
                    if triggered:
                        raise AccountLockedError(
                            message=translate(
                                "err.auth.account_locked",
                                params={"minutes": security["lock_duration"]},
                                fallback=f"账户已锁定，请在 {security['lock_duration']} 分钟后重试",
                            )
                        )
                raise InvalidCredentialsError()

            # [jonex] 停用账号拒绝登录。此前只有 me / refresh / ticket 交换校验
            # User.status == 1，登录路径漏了（user_repo.get_by_username 只过滤
            # is_deleted），导致 status=0 的用户能拿到 token、之后 /me 才失败。
            #
            # 沿用本方法对「租户停用」的既有口径：对外一律报「用户名或密码错误」，
            # 不暴露账号是否存在或是否被停用（见下方 inactive_tenant_message）。
            # 返回明确原因而不是「用户名或密码错误」：本检查在密码校验通过之后，
            # 调用方已证明知道正确凭据，告知「账号已停用」不泄漏其未知信息，
            # 不构成账号枚举风险；密码错误的请求仍统一返回 3008。
            # 校验放在密码验证之后还有一层用意 —— 停用不是凭据错误，
            # 不该累计登录失败锁定计数。
            if user.status != 1:
                logger.warning(
                    "登录被拒：账号已停用 tenant=%s username=%s user_id=%s",
                    tenant_id, username, user.id,
                )
                raise AccountDisabledError(
                    message=translate(
                        "err.auth.account_disabled",
                        fallback="账号已停用，请联系管理员",
                    )
                )

            await self._clear_login_failures(tenant_id, username)
            result = await self._build_login_response(
                user,
                update_last_login=True,
                inactive_tenant_message=translate(3008, fallback="用户名或密码错误"),  # 原消息: 用户名或密码错误
            )
            await self._record_login_audit(user.tenant_id, user, client_ip, "SUCCESS")
            return result
        except AccountLockedError:
            await self._record_login_audit(
                tenant_id, None, client_ip, "FAILED",
                error_message="账户已锁定", username=username,
            )
            raise
        except AccountDisabledError:
            # 必须显式捕获：否则落到下方 `except JonexException: raise`，不写审计记录。
            # 审计里记真实原因，便于事后追查「为什么这个人登不进来」。
            await self._record_login_audit(
                tenant_id, None, client_ip, "FAILED",
                error_message="账号已停用", username=username,
            )
            raise
        except (InvalidCredentialsError, InvalidApiKeyError):
            # 登录失败：记录失败日志
            await self._record_login_audit(
                tenant_id, None, client_ip, "FAILED",
                error_message="用户名或密码错误", username=username,
            )
            raise
        except JonexException:
            # JonexException 子类直接向上传播（已有 i18n message）
            raise
        except Exception as e:
            # 将未预期的异常（如数据库连接失败）包装为 JonexException
            raise InternalError(message=str(e))

    async def _get_security_config(self, tenant_id: str) -> dict:
        """读取**该租户**的安全配置，三层回落：租户覆盖 → 平台默认 → 代码默认。

        - `tenant_id` 是必填：不按租户读就等于还用全局值，租户级的 session_timeout 完全不生效；
        - 默认值与解析语义统一定义在 `SecurityConfigService`（本类不再自带一份默认值）；
        - DB 读不到/读异常只告警、回落默认，**不阻断登录**（与改造前口径一致）。
        """
        return await self.security_config_service.resolve(tenant_id)

    @staticmethod
    def _login_lock_keys(tenant_id: str, username: str) -> tuple[str, str]:
        return (
            f"login_fail:{tenant_id}:{username}",
            f"login_lock:{tenant_id}:{username}",
        )

    async def _account_lock_remaining(self, tenant_id: str, username: str, lock_minutes: int) -> int | None:
        """返回剩余锁定分钟数（向上取整）；未锁定/Redis 不可用返回 None。"""
        if lock_minutes <= 0:
            return None
        try:
            from jonex_core.common.cache import CacheUtil
            _, lock_key = self._login_lock_keys(tenant_id, username)
            ttl = await CacheUtil.ttl(lock_key)
            if ttl and ttl > 0:
                return max(1, (ttl + 59) // 60)
        except Exception:
            logger.warning("读取登录锁定状态失败，降级为不锁定", exc_info=True)
        return None

    async def _register_login_failure(self, tenant_id: str, username: str, threshold: int, lock_minutes: int) -> bool:
        """登记一次失败；返回是否触发锁定。Redis 不可用降级为不锁定。"""
        if threshold <= 0:
            return False
        try:
            from jonex_core.common.cache import CacheUtil
            fail_key, lock_key = self._login_lock_keys(tenant_id, username)
            count = await CacheUtil.incr(fail_key)
            if count == 1:
                await CacheUtil.expire(fail_key, lock_minutes * 60)
            if count >= threshold:
                await CacheUtil.set(lock_key, "1", expire=lock_minutes * 60)
                await CacheUtil.delete(fail_key)
                return True
        except Exception:
            logger.warning("登记登录失败失败，降级为不锁定", exc_info=True)
        return False

    async def _clear_login_failures(self, tenant_id: str, username: str) -> None:
        try:
            from jonex_core.common.cache import CacheUtil
            fail_key, lock_key = self._login_lock_keys(tenant_id, username)
            await CacheUtil.delete(fail_key)
            await CacheUtil.delete(lock_key)
        except Exception:
            logger.warning("清理登录失败计数失败", exc_info=True)

    async def _record_login_audit(
        self,
        tenant_id: str,
        user,
        client_ip: str | None,
        outcome: str,
        *,
        error_message: str | None = None,
        username: str | None = None,
    ):
        """记录登录审计日志（同步直写，不阻塞登录流程）"""
        try:
            from capabilities.platform.services.audit_log_service import AuditLogService
            svc = AuditLogService(self.session)
            await svc.record(
                tenant_id=tenant_id,
                log_type="LOGIN",
                action="auth.login",
                outcome=outcome,
                service_name="platform",
                user_id=user.id if user else None,
                username=user.username if user else username,
                ip=client_ip,
                error_message=error_message,
                sync=True,
            )
        except Exception:
            logger.exception("记录登录审计日志失败（不影响登录流程）")

    async def _build_login_response(
        self,
        user: User,
        update_last_login: bool = False,
        inactive_tenant_message: str | None = None,
        fallback_roles: list[str] | None = None,
    ) -> LoginResponse:
        if inactive_tenant_message is None:
            inactive_tenant_message = translate("err.auth.user_not_found_or_disabled", fallback="用户不存在或已禁用")  # 原消息: 用户不存在或已禁用
        tenants = await self.tenant_repo.list_active_by_ids([user.tenant_id])
        tenant = tenants.get(user.tenant_id)
        if not tenant:
            raise InvalidApiKeyError(message=inactive_tenant_message)

        # 优先重查 DB 拿最新角色名（防旧 token 滞留绕过权限撤销）；仅 DB 查询异常时兜底。
        try:
            role_names = await UserService(self.session).get_role_names(user.tenant_id, user.id)
        except Exception:
            logger.warning(f"查询用户角色失败，回退兜底角色: user_id={user.id}", exc_info=True)
            role_names = fallback_roles if fallback_roles is not None else ([user.role] if user.role else [])

        from jonex_core.security.permission import get_user_permissions

        perms = await get_user_permissions(user.tenant_id, user.id)
        # ⚠ 这里是 `user.tenant_id`，不是 `tenant_id` —— 本方法没有 `tenant_id` 这个局部名
        # （全程用 user.tenant_id）。写成 tenant_id 的后果：参数个数、语法、单测全都正常，
        # 但线上**每次登录都 500 `name 'tenant_id' is not defined`**，只有真实登录才暴露。
        security = await self._get_security_config(user.tenant_id)
        session_minutes = max(1, security["session_timeout"])
        access_token = self.user_auth.create_access_token(user, role_names, expires_minutes=session_minutes)
        refresh_token = self.user_auth.create_refresh_token(user, role_names)

        if update_last_login:
            user.last_login_at = datetime.utcnow()
            await self.session.flush()

        logger.info(f"用户登录成功: {user.username} (id={user.id})")

        return LoginResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=session_minutes * 60,
            user=UserInfo(
                user_id=user.id,
                username=user.username,
                display_name=user.display_name,
                tenant_id=user.tenant_id,
                tenant_name=tenant.name,
                role=user.role,
                roles=role_names,
                is_platform_admin=_is_platform_admin(perms),
                is_tenant_admin=_is_tenant_admin(perms),
                permissions=sorted(perms),
            ),
        )

    async def me(self, token: str) -> UserInfo:
        payload = self.user_auth.decode_token(token)
        impersonated = bool(payload.get("impersonated"))
        tenant_id = require_tenant(payload.get("tenant_id"))
        # 模拟态：用户查询按原租户，展示租户按目标租户
        lookup_tenant_id = require_tenant(payload.get("original_tenant_id") or tenant_id)

        from sqlalchemy import select

        result = await self.session.execute(
            select(User).where(
                User.id == int(payload["sub"]),
                User.tenant_id == lookup_tenant_id,
                User.status == 1,
                User.is_deleted == 0,
            )
        )
        user = result.scalar_one_or_none()
        if not user:
            raise InvalidApiKeyError(message=translate("err.auth.user_not_found_or_disabled", fallback="用户不存在或已禁用"))  # 原消息: 用户不存在或已禁用

        # 展示租户：模拟态取目标租户，否则取用户租户
        display_tenant_id = tenant_id if impersonated else user.tenant_id
        tenants = await self.tenant_repo.list_active_by_ids([display_tenant_id])
        tenant = tenants.get(display_tenant_id)
        if not tenant:
            raise InvalidApiKeyError(message=translate("err.auth.user_not_found_or_disabled", fallback="用户不存在或已禁用"))  # 原消息: 用户不存在或已禁用

        role_names = await UserService(self.session).get_role_names(user.tenant_id, user.id)

        from jonex_core.security.permission import get_user_permissions

        perms = await get_user_permissions(user.tenant_id, user.id)

        return UserInfo(
            user_id=user.id,
            username=user.username,
            display_name=user.display_name,
            tenant_id=display_tenant_id,
            tenant_name=tenant.name,
            role=user.role,
            roles=role_names,
            is_platform_admin=True if impersonated else _is_platform_admin(perms),
            is_tenant_admin=True if impersonated else _is_tenant_admin(perms),
            permissions=sorted(perms),
            impersonated=impersonated,
            original_tenant_id=lookup_tenant_id if impersonated else None,
        )

    async def _get_oidc_client(self):
        """懒加载 Keycloak OIDC token 客户端（测试注入 ``_oidc_client`` 覆盖）。"""
        if getattr(self, "_oidc_client", None) is None:
            from jonex_core.integrations.keycloak_oidc import KeycloakOidcClient

            self._oidc_client = KeycloakOidcClient()
        return self._oidc_client

    async def _get_admin_client(self):
        """懒加载 Keycloak Admin 客户端（测试注入 ``_admin_client`` 覆盖）。"""
        if getattr(self, "_admin_client", None) is None:
            from jonex_core.integrations.keycloak_admin import (
                get_keycloak_admin_client,
            )

            self._admin_client = get_keycloak_admin_client()
        return self._admin_client

    async def _resolve_upstream_subject(
        self, tenant_id: str, kc_user_id: str
    ) -> Optional[str]:
        """出站解析上游主体标识（客户系统主体 id，经 Keycloak ``federated-identity`` 中转）。

        ``kc_user_id`` 就是本次登录者的 Keycloak ``sub``（= ``external_id`` 形参），
        故一次 ``GET /admin/realms/{realm}/users/{sub}/federated-identity`` 即可拿到
        「当前登录者的上游主体」—— **不需要按 username 反查 Keycloak**。

        ⛔ 失败一律返回 ``None``、不抛、不阻断登录：Keycloak 不可达 / 404 / 200 但非 JSON /
        空列表 / 元素缺 ``userId`` 全部归为「取不到」，由调用方降级（留空照常放行，
        或严判拒绝）。⛔ 日志不打主体标识取值、不打上游原始返回。
        """
        from jonex_core.common.realm_util import realm_name_for_tenant

        realm = realm_name_for_tenant(tenant_id)
        try:
            from capabilities.platform.auth.account_sync_service import (
                AccountSyncService,
            )

            # 复用停用路径同一套 realm-admin 凭据解析（口径只有一处）；未配置 ⇒ None ⇒ 降级。
            credentials = await AccountSyncService(
                self.session
            )._resolve_realm_admin_credentials(realm, tenant_id)
            if credentials is None:
                logger.warning(
                    "未解析到 realm-admin 凭据，上游主体标识按「取不到」降级（realm: %s）",
                    realm,
                )
                return None
            admin = await self._get_admin_client()
            return await admin.get_federated_identity(credentials, kc_user_id)
        except Exception:  # noqa: BLE001 —— 上游不可用绝不阻断登录
            logger.warning(
                "读取上游主体标识失败，按「取不到」降级（realm: %s）", realm
            )
            return None

    async def _resolve_for_write(
        self, tenant_id: str, kc_user_id: str, explicit: Optional[str] = None
    ) -> Optional[str]:
        """取「即将写进 ``upstream_subject`` 的值」：显式传入优先（不出站），否则出站解析。

        解析不到 ⇒ 返回 ``None`` 并记 warning（⛔ 不打取值），由调用方决定是「留空照常」
        还是「严判拒绝」—— 本方法自身不抛、不阻断。所有出站都收敛到这里，
        以保证「已有锚点 ⇒ 一次都不查」这条约束在四处调用点上口径一致。
        """
        if explicit:
            return explicit
        resolved = await self._resolve_upstream_subject(tenant_id, kc_user_id)
        if not resolved:
            logger.warning(
                "上游主体标识（subject）解析失败，本次按未设置处理、不阻断登录"
            )
            return None
        return resolved

    async def _get_session_repo(self):
        """懒加载会话聚合根 Redis 仓储（测试注入 ``_session_repo`` 覆盖）。"""
        if getattr(self, "_session_repo", None) is None:
            from capabilities.platform.auth.session_aggregate_repository import (
                SessionAggregateRepository,
            )

            self._session_repo = SessionAggregateRepository()
        return self._session_repo

    async def _get_oidc_state_repo(self):
        """懒加载 OIDC state 一次性票据 Redis 仓储（测试注入 ``_oidc_state_repo`` 覆盖）。"""
        if getattr(self, "_oidc_state_repo", None) is None:
            from capabilities.platform.auth.oidc_state_repository import (
                OidcStateRepository,
            )

            self._oidc_state_repo = OidcStateRepository()
        return self._oidc_state_repo

    async def refresh(self, refresh_token: str) -> LoginResponse:
        """Keycloak SSO refresh 链路。

        refresh token 从 httpOnly cookie 携带（本方法只收 token 字符串，cookie 解析在路由层）；
        调 Keycloak token endpoint 换新（rotation）→ ``sub``(UUID) 映射 ``external_id``→本地
        ``user_id`` → 重签本地 HS256 access（内存态）+ 下发新 refresh cookie（路由层）。

        会话聚合根 + refresh 家族状态落 Redis（setex TTL 对齐 Keycloak 在线会话
        ``ssoSessionIdleTimeout`` 7 天，滑动续期，吊销 = del）。``sub``(UUID) 映射仅在
        边界端点发生一次，业务主链不接触 UUID。
        """
        import jwt as _jwt

        from capabilities.platform.auth.realm_resolver import (
            resolve_tenant_id_from_issuer,
        )
        from jonex_core.common.realm_util import realm_name_for_tenant

        # 1) 解码 refresh token（未验签，仅取 iss/sid/sub/jti 做路由；有效性由 Keycloak token 端点裁决）
        try:
            claims = _jwt.decode(
                refresh_token,
                options={"verify_signature": False, "verify_exp": False},
            )
        except _jwt.PyJWTError as exc:
            raise RefreshTokenRevokedError() from exc

        issuer = claims.get("iss")
        if not issuer:
            # 本地 HS256 refresh 分支（本地密码登录，token 无 iss claim），与 Keycloak 链路并行。
            return await self._refresh_local(refresh_token)

        sub = claims.get("sub")
        sid = claims.get("sid")
        jti = claims.get("jti")

        # 2) issuer URL → realm → tenant_id（Realm 名反查，非 token claim）
        # refresh 是唯一「未验签就反查」的链路：必须校验 issuer host 命中白名单，
        # 否则任意 https://evil.com/realms/jonex-{租户} 可映射到合法租户。
        allowed_hosts = _issuer_hosts_from_whitelist(self.config.KEYCLOAK_ISSUER_WHITELIST)
        if not allowed_hosts:
            # 白名单为空（未启用 SSO）→ fail-closed，不允许反查
            raise RefreshTokenRevokedError()
        tenant_id = await resolve_tenant_id_from_issuer(self.session, issuer, allowed_hosts=allowed_hosts)

        # 3) 定位 user_id：优先 sub（含 sub 的 refresh token 兼容路径），否则回退
        #    sid → 会话聚合根 → user_id（Keycloak 26 opaque refresh token 无 sub）。
        #    聚合根是本地写入的，sid 仅作路由、不产生信任（信任由下方家族 jti + Keycloak 换新裁决）。
        session_repo = await self._get_session_repo()
        aggregate = await session_repo.get_session(sid) if sid else None

        user = None
        if sub:
            user = await self.get_user_by_external_id(tenant_id, sub)
        if user is None and aggregate is not None and aggregate.tenant_id == tenant_id:
            user = await self.user_repo.get_by_id(aggregate.user_id, tenant_id)
        if user is None or user.status != 1:
            raise RefreshTokenRevokedError()

        # 4) refresh 家族 jti 校验（撤销 / rotation 重放检测）
        # jti 必须存在且匹配已存家族 jti；缺失/为空一律拒绝（未验签 token 的 jti
        # 不可信，不得因 jti 为空短路放行，否则伪造 token 可借 Keycloak invalid_grant
        # 触发 delete_family 造成会话 DoS）。
        family_jti = await session_repo.get_family_jti(tenant_id, user.id, sid or jti)
        if family_jti is None or not jti or family_jti != jti:
            raise RefreshTokenRevokedError()

        # 5) 会话聚合根吊销校验
        if aggregate is not None and aggregate.revoked_at is not None:
            raise RefreshTokenRevokedError()

        # 6) 调 Keycloak token endpoint 换新（rotation）
        realm = realm_name_for_tenant(tenant_id)
        oidc_client = await self._get_oidc_client()
        try:
            token_resp = await oidc_client.exchange_refresh_token(realm, refresh_token)
        except RefreshTokenRevokedError:
            # Keycloak 已拒绝（invalid_grant：rotation 旧 token 复用 / 撤销 / 过期）→ 清本地家族
            await session_repo.delete_family(tenant_id, user.id)
            raise

        new_refresh_token = token_resp.refresh_token or refresh_token
        new_jti = jti
        if token_resp.refresh_token:
            try:
                new_claims = _jwt.decode(
                    token_resp.refresh_token,
                    options={"verify_signature": False, "verify_exp": False},
                )
                new_jti = new_claims.get("jti") or jti
            except _jwt.PyJWTError:
                new_jti = jti

        # 7) 重签本地 HS256 access（内存态）+ 用户信息
        role_names = await UserService(self.session).get_role_names(tenant_id, user.id)

        from jonex_core.security.permission import get_user_permissions

        perms = await get_user_permissions(tenant_id, user.id)
        security = await self._get_security_config(tenant_id)
        session_minutes = max(1, security["session_timeout"])
        access_token = self.user_auth.create_access_token(
            user, role_names, expires_minutes=session_minutes
        )

        tenants = await self.tenant_repo.list_active_by_ids([tenant_id])
        tenant = tenants.get(tenant_id)
        if not tenant:
            raise InvalidApiKeyError(message=translate("err.auth.user_not_found_or_disabled", fallback="用户不存在或已禁用"))  # 原消息: 用户不存在或已禁用

        # 8) 会话聚合根滑动续期 + refresh 家族轮换（TTL 对齐 ssoSessionIdleTimeout 7 天）
        from capabilities.platform.dtos.session import (
            SessionAggregate,
            refresh_family_key,
        )

        ttl = self.config.USER_JWT_REFRESH_DAYS * 86400
        await session_repo.set_family_jti(tenant_id, user.id, sid or jti, new_jti or jti, ttl)
        if sid:
            now = datetime.now(timezone.utc)
            created_at = aggregate.created_at if aggregate is not None else now
            await session_repo.save_session(
                SessionAggregate(
                    sid=sid,
                    user_id=user.id,
                    tenant_id=tenant_id,
                    refresh_token_ref=refresh_family_key(tenant_id, user.id),
                    created_at=created_at,
                    expires_at=now + timedelta(seconds=ttl),
                ),
                ttl,
            )

        logger.info(
            f"Keycloak refresh 成功: user={user.username} (id={user.id}) tenant={tenant_id}"
        )

        return LoginResponse(
            access_token=access_token,
            refresh_token=new_refresh_token,
            expires_in=session_minutes * 60,
            user=UserInfo(
                user_id=user.id,
                username=user.username,
                display_name=user.display_name,
                tenant_id=tenant_id,
                tenant_name=tenant.name,
                role=user.role,
                roles=role_names,
                is_platform_admin=_is_platform_admin(perms),
                is_tenant_admin=_is_tenant_admin(perms),
                permissions=sorted(perms),
            ),
        )

    async def _refresh_local(self, refresh_token: str) -> LoginResponse:
        """本地 HS256 refresh 续期分支（本地密码登录，refresh token 无 iss claim）。

        与 Keycloak 链路（上方 refresh 主体）并行，互斥路由：refresh 未验签解码后
        无 ``iss`` 即进入本分支。本地 token 由 user_auth 直接验签（HS256 + JWT_SECRET），
        不依赖 issuer 反查 / sid 聚合根 / Keycloak exchange；``sub`` 为整数 user_id，
        ``tenant_id`` 直接取自 token。滑动续期重签 access + refresh。

        安全语义：
        - 真正验签（伪造 token 签名不符 → decode_token 抛 TokenExpiredError → 转 3019）；
        - 只接受 ``type=refresh``（access / 模拟态 token 的 type=user 不得充当 refresh）；
        - 用户必须存在且未停用，且 token 租户与用户实属租户一致（跨租户拒绝）。
        """
        try:
            payload = self.user_auth.decode_token(refresh_token)
        except JonexException:
            raise RefreshTokenRevokedError()

        if payload.get("type") != "refresh":
            raise RefreshTokenRevokedError()

        user_id = int(payload["sub"])
        tenant_id = require_tenant(payload.get("tenant_id"))
        user = await self.user_repo.get_by_id(user_id, tenant_id)
        if user is None or user.status != 1:
            raise RefreshTokenRevokedError()

        # 重查最新角色/权限（防旧 token 滞留绕过权限撤销），对齐 Keycloak 链路口径
        role_names = await UserService(self.session).get_role_names(tenant_id, user.id)
        from jonex_core.security.permission import get_user_permissions

        perms = await get_user_permissions(tenant_id, user.id)
        security = await self._get_security_config(tenant_id)
        session_minutes = max(1, security["session_timeout"])
        access_token = self.user_auth.create_access_token(
            user, role_names, expires_minutes=session_minutes
        )
        new_refresh_token = self.user_auth.create_refresh_token(user, role_names)

        tenants = await self.tenant_repo.list_active_by_ids([tenant_id])
        tenant = tenants.get(tenant_id)
        if not tenant:
            raise RefreshTokenRevokedError()

        logger.info(
            "本地 refresh 成功: user=%s (id=%s) tenant=%s", user.username, user.id, tenant_id
        )

        return LoginResponse(
            access_token=access_token,
            refresh_token=new_refresh_token,
            expires_in=session_minutes * 60,
            user=UserInfo(
                user_id=user.id,
                username=user.username,
                display_name=user.display_name,
                tenant_id=tenant_id,
                tenant_name=tenant.name,
                role=user.role,
                roles=role_names,
                is_platform_admin=_is_platform_admin(perms),
                is_tenant_admin=_is_tenant_admin(perms),
                permissions=sorted(perms),
            ),
        )

    async def oidc_authorize(
        self,
        tenant_id: str,
        post_login_redirect_uri: str,
        redirect_uri: Optional[str] = None,
    ) -> str:
        """Keycloak OIDC 授权码登录发起（authorize 端点编排）。

        生成 state / nonce / code_verifier → 算 PKCE S256 challenge → 拼 authorize URL
        → 把 OidcState（一次性票据）写入 Redis → 返回 authorize URL，由路由层 302 跳转。

        安全语义（fail-closed）：
        - ``post_login_redirect_uri`` 必须先命中 ``AUTH_ALLOWED_REDIRECT_URIS`` 白名单，
          否则抛 :class:`PermissionDeniedError`（防开放重定向，与回调侧校验一致）；
        - OIDC 回调地址 ``redirect_uri`` 缺省取 ``KEYCLOAK_REDIRECT_URI``，仍为空则抛
          :class:`UpstreamServiceError`（禁止半成品 URL 泄露到授权请求）。

        返回：Keycloak authorize URL 字符串；state 等上下文已落 Redis，回调时原子消费。
        """
        # 1) 租户合法性校验（防空 / 默认租户）
        tenant_id = require_tenant(tenant_id)

        # 2) redirect 白名单校验（防开放重定向；post_login_redirect_uri 须命中 AUTH_ALLOWED_REDIRECT_URIS）
        try:
            allowed = (
                json.loads(self.config.AUTH_ALLOWED_REDIRECT_URIS)
                if self.config.AUTH_ALLOWED_REDIRECT_URIS
                else {}
            )
        except json.JSONDecodeError:
            logger.error(
                "AUTH_ALLOWED_REDIRECT_URIS 解析失败: %s", self.config.AUTH_ALLOWED_REDIRECT_URIS
            )
            raise PermissionDeniedError(
                message=translate("err.auth.redirect_whitelist_error", fallback="redirect 白名单配置错误")
            )  # 原消息: redirect 白名单配置错误
        allowed_uris = [u for uris in allowed.values() if isinstance(uris, list) for u in uris]
        if not _redirect_uri_allowed(post_login_redirect_uri, allowed_uris):
            raise PermissionDeniedError(
                message=translate(
                    "err.auth.redirect_uri_not_whitelisted",
                    params={"uri": post_login_redirect_uri},
                    fallback=f"redirectUri 不在白名单中: {post_login_redirect_uri}",
                )
            )  # 原消息: redirectUri 不在白名单中: {uri}

        # 3) OIDC 回调地址 fail-closed：缺省取 KEYCLOAK_REDIRECT_URI，仍为空拒绝
        redirect_uri = redirect_uri or getattr(self.config, "KEYCLOAK_REDIRECT_URI", "")
        if not redirect_uri:
            raise UpstreamServiceError(
                message="OIDC authorize 发起失败：缺少回调地址 KEYCLOAK_REDIRECT_URI"
            )

        # 4) 生成 state / nonce / code_verifier，并算 PKCE S256 challenge
        state = _generate_state()
        nonce = _generate_nonce()
        code_verifier = _generate_code_verifier()
        code_challenge = _pkce_s256_challenge(code_verifier)

        # 5) tenant_id → realm
        from jonex_core.common.realm_util import realm_name_for_tenant

        realm = realm_name_for_tenant(tenant_id)

        # 5.5) IdP 提示（kc_idp_hint）：让 Keycloak 直接跳到指定企业 IdP，跳过它自己的
        # 中间登录表单。开关关闭 / 解析失败一律得到 None = 不注入，不阻断登录。
        idp_hint = await self._resolve_idp_hint(tenant_id)

        # 6) 拼 authorize URL（client 层纯函数）
        from jonex_core.integrations.keycloak_oidc import build_authorize_url

        url = build_authorize_url(
            realm,
            redirect_uri=redirect_uri,
            state=state,
            nonce=nonce,
            code_challenge=code_challenge,
            idp_hint=idp_hint,
        )

        # 7) 构造 OidcState 一次性票据
        from capabilities.platform.dtos.session import OidcState

        oidc_state = OidcState(
            state=state,
            tenant_id=tenant_id,
            nonce=nonce,
            code_verifier=code_verifier,
            code_challenge=code_challenge,
            code_challenge_method="S256",
            redirect_uri=redirect_uri,
            post_login_redirect_uri=post_login_redirect_uri,
            created_at=datetime.now(timezone.utc),
        )

        # 8) 写入 Redis（一次性票据，TTL 对齐 KEYCLOAK_STATE_TTL_SECONDS）
        state_repo = await self._get_oidc_state_repo()
        await state_repo.save(
            oidc_state,
            ttl=getattr(self.config, "KEYCLOAK_STATE_TTL_SECONDS", 300),
        )

        return url

    async def _resolve_idp_hint(self, tenant_id: str) -> Optional[str]:
        """解析生效的 ``kc_idp_hint`` alias；返回 ``None`` 表示「不注入」。

        口径（jonex-rnyg）：
        - 平台级开关 ``KEYCLOAK_IDP_HINT_ENABLED`` 关闭时直接返回 ``None``，**不查库** ——
          授权行为与改造前逐字一致，也不产生任何额外的 DB 读取（唯一的回退开关，
          线上出问题关掉即完全回退）；
        - 打开时取该租户生效 alias（租户级覆盖值 ``or`` 平台默认值，规则归属
          :meth:`IdentityIntegrationConfigService.get_idp_hint_alias`），并把空串 /
          纯空白归一为 ``None``，不把空值塞进 authorize URL；
        - 查询异常**不得阻断登录**：降级为「不注入」并记 warning，与
          ``_get_security_config`` 的「缺失或异常则回退」同风格。

        本方法是纯编排：hint 由这里解析好再传给纯函数 ``build_authorize_url``，
        不让纯函数去读库（保持其无 IO 契约）。
        """
        if not getattr(self.config, "KEYCLOAK_IDP_HINT_ENABLED", False):
            return None
        try:
            from capabilities.platform.services.identity_integration_config import (
                IdentityIntegrationConfigService,
            )

            alias = await IdentityIntegrationConfigService(self.session).get_idp_hint_alias(
                tenant_id
            )
        except Exception:
            logger.warning(
                "读取 kc_idp_hint alias 失败，降级为不注入 hint（不阻断登录）",
                exc_info=True,
            )
            return None
        return (alias or "").strip() or None

    async def oidc_callback(self, code: str, state: str) -> dict:
        """Keycloak OIDC 授权码回调。

        校验 state（一次性消费）/ PKCE code_verifier（local S256 预校验 + Keycloak 强制）/
        nonce（id_token claim）→ 兑换 code → RS256/JWKS 验签 id_token → ``sub``(UUID) →
        external_id → 影子用户供给 → 建会话聚合根 + refresh 家族（Redis）→ 记审计
        ``auth.login``。access 不下发（SPA 持 cookie 走 /auth/refresh 启动）。

        返回 ``{refresh_token, post_login_redirect_uri}``；路由层据此设 cookie + 302。
        """
        import jwt as _jwt

        from capabilities.platform.auth.realm_resolver import (
            resolve_tenant_id_from_issuer,
        )
        from jonex_core.common.realm_util import realm_name_for_tenant
        from jonex_core.security.oidc import verify_rs256_token

        # 1) 消费 state（一次性：重放 / 缺失 / 过期 → 拒绝）
        state_repo = await self._get_oidc_state_repo()
        st = await state_repo.consume(state)
        if st is None:
            # state 已被消费（回跳 URL 重放）/ 已过期：与「IdP 回带 error 被当成兑换失败」
            # 是两类现场，日志须能区分。只记 state 前 8 位（一次性 CSRF 票据，不回填原值）。
            logger.warning(
                "OIDC 回调 state 无效或已被消费（回跳 URL 重放 / 过期）：state_prefix=%s",
                (state or "")[:8],
            )
            raise OidcStateMismatchError()
        tenant_id = require_tenant(st.tenant_id)

        # 2) redirect 白名单（防开放重定向；post_login_redirect_uri 须命中 AUTH_ALLOWED_REDIRECT_URIS）
        try:
            allowed = (
                json.loads(self.config.AUTH_ALLOWED_REDIRECT_URIS)
                if self.config.AUTH_ALLOWED_REDIRECT_URIS
                else {}
            )
        except json.JSONDecodeError:
            logger.error(
                "AUTH_ALLOWED_REDIRECT_URIS 解析失败: %s", self.config.AUTH_ALLOWED_REDIRECT_URIS
            )
            raise PermissionDeniedError(
                message=translate("err.auth.redirect_whitelist_error", fallback="redirect 白名单配置错误")
            )  # 原消息: redirect 白名单配置错误
        allowed_uris = [u for uris in allowed.values() if isinstance(uris, list) for u in uris]
        if not _redirect_uri_allowed(st.post_login_redirect_uri, allowed_uris):
            raise PermissionDeniedError(
                message=translate(
                    "err.auth.redirect_uri_not_whitelisted",
                    params={"uri": st.post_login_redirect_uri},
                    fallback=f"redirectUri 不在白名单中: {st.post_login_redirect_uri}",
                )
            )  # 原消息: redirectUri 不在白名单中: {uri}

        # 3) 本地 PKCE S256 预校验（防御性；Keycloak token 端点也会强制校验）
        if not st.code_verifier:
            raise OidcPkceVerifierError()
        if st.code_challenge and _pkce_s256_challenge(st.code_verifier) != st.code_challenge:
            raise OidcPkceVerifierError()

        # 4) 兑换授权码（invalid_grant → OidcAuthorizationCodeError）
        realm = realm_name_for_tenant(tenant_id)
        oidc_client = await self._get_oidc_client()
        token_resp = await oidc_client.exchange_authorization_code(
            realm, code, redirect_uri=st.redirect_uri, code_verifier=st.code_verifier
        )

        id_token = token_resp.id_token
        refresh_token = token_resp.refresh_token
        if not id_token or not refresh_token:
            raise OidcAuthorizationCodeError()

        # 5) 验签 id_token（RS256，iss 白名单）+ 租户 ↔ issuer 一致性 + nonce
        try:
            unverified = _jwt.decode(
                id_token, options={"verify_signature": False, "verify_exp": False}
            )
        except _jwt.PyJWTError as exc:
            raise OidcAuthorizationCodeError() from exc
        issuer = unverified.get("iss")
        if not issuer:
            raise OidcAuthorizationCodeError()

        payload = verify_rs256_token(id_token, expected_issuer=issuer)

        if await resolve_tenant_id_from_issuer(self.session, issuer) != tenant_id:
            raise RealmTenantMismatchError()

        if (payload.get("nonce") or "") != (st.nonce or ""):
            raise OidcNonceMismatchError()

        sub = payload.get("sub")
        if not sub:
            raise OidcAuthorizationCodeError()
        preferred_username = payload.get("preferred_username") or sub
        # id_token 的 name/email claim 映射到影子用户 display_name/email 字段。
        name = payload.get("name")
        email = payload.get("email")

        # 6) 影子用户供给（sub(UUID) → external_id → 本地 user_id，映射仅此一次）
        #    按租户 SSO 首登策略决定是否允许自动建号：synced_only 未同步用户拒登。
        #
        #    三档「IdP 已认证、平台侧不放行」的业务拒绝在这里被归成 OidcAccessDeniedError，
        #    由路由层转成 SSO 错误页。**try 只包住这一段**：三档异常全部在此抛出
        #    （identity_conflict 由 provision_shadow_user 内部抛，account_disabled 紧随其后
        #    判定）；前置的 state/白名单/PKCE/兑换/验签与后续的会话/审计异常一律原样穿透，
        #    它们不是权限问题，不能被兜成「无访问权限」。
        login_policy = await self._get_login_policy(tenant_id)
        try:
            user = await self.provision_shadow_user(
                tenant_id,
                preferred_username,
                sub,
                allow_create=(login_policy != LoginPolicy.SYNCED_ONLY.value),
                display_name=name,
                email=email,
            )
            if user.status != 1:
                raise AccountDisabledError(
                    message=translate("err.auth.account_disabled", fallback="账号已停用，请联系管理员")
                )
        except (
            OidcSyncRequiredError,
            OidcShadowUserConflictError,
            AccountDisabledError,
        ) as exc:
            # 「拒绝 = 无副作用」必须在这里写实：本段抛点之前 provision_shadow_user 可能已经
            # 改过存量用户行（display_name / email、必要时补 upstream_subject，走 username 分支
            # 还会 flush 换 external_id）。路由层接走本异常后**正常返回 302**，
            # get_db_session 只在异常路径回滚、正常路径会 commit —— 不在这里先回滚，
            # 这些写入就会随一次「被拒绝的登录」落库。
            await self.session.rollback()
            raise OidcAccessDeniedError(
                reason=_ACCESS_DENIED_REASON_BY_TYPE[type(exc)],
                post_login_redirect_uri=st.post_login_redirect_uri,
                tenant_id=tenant_id,
            ) from exc

        # 首登写 last_login_at（对齐 _build_login_response 口径）；SSO 首登入口此前未写，
        # 导致 SSO 用户 last_login_at 长期为空。commit 由路由层统一处理。
        user.last_login_at = datetime.utcnow()
        await self.session.flush()

        # 7) refresh token 的 sid/jti（会话聚合根 + 家族键）
        sid = None
        jti = None
        try:
            rt_claims = _jwt.decode(
                refresh_token, options={"verify_signature": False, "verify_exp": False}
            )
            sid = rt_claims.get("sid")
            jti = rt_claims.get("jti")
        except _jwt.PyJWTError:
            pass

        # 8) 建会话聚合根 + refresh 家族（TTL 对齐 ssoSessionIdleTimeout 7 天）
        from capabilities.platform.dtos.session import (
            SessionAggregate,
            refresh_family_key,
        )

        session_repo = await self._get_session_repo()
        ttl = self.config.USER_JWT_REFRESH_DAYS * 86400
        now = datetime.now(timezone.utc)
        family_jti = jti or sid
        if family_jti:
            await session_repo.set_family_jti(tenant_id, user.id, sid or jti, family_jti, ttl)
        if sid:
            await session_repo.save_session(
                SessionAggregate(
                    sid=sid,
                    user_id=user.id,
                    tenant_id=tenant_id,
                    refresh_token_ref=refresh_family_key(tenant_id, user.id),
                    created_at=now,
                    expires_at=now + timedelta(seconds=ttl),
                ),
                ttl,
            )

        # 9) 审计 auth.login
        await self._record_login_audit(tenant_id, user, None, "SUCCESS")

        logger.info(
            f"Keycloak OIDC 回调成功: user={user.username} (id={user.id}) tenant={tenant_id}"
        )

        return {
            "refresh_token": refresh_token,
            # id_token 只作登出时的 id_token_hint（RP-initiated logout）载体：
            # 验签已在第 5 步完成，此处原样回传给路由层落 httpOnly cookie。
            "id_token": id_token,
            "post_login_redirect_uri": st.post_login_redirect_uri,
        }

    async def logout(
        self, refresh_token: str, id_token_hint: Optional[str] = None
    ) -> Optional[str]:
        """登出：吊销本地 refresh 家族 + 产出 Keycloak RP-initiated logout URL。

        两段语义，顺序不可交换：

        1. **本地吊销（安全底线，必做）**：解析 refresh token 的 iss/sub 反查租户+用户，
           吊销该用户所有 refresh 家族。幂等降级 —— 解码失败 / 缺 iss/sub / issuer 无法
           映射 / 用户不存在时静默返回（logout 不抛 500，只 log warning）。
        2. **结束 Keycloak 侧 SSO 会话**：产出 ``end_session_endpoint`` URL，由路由层
           交给浏览器跳转。只做本地吊销不清 Keycloak cookie 是「假登出」——浏览器残留的
           ``KEYCLOAK_IDENTITY`` / ``KEYCLOAK_SESSION`` 会让下一次 authorize 在用户无感
           的情况下重新签发 code，静默登回原用户（无法切换账号）。

        参数 ``id_token_hint``：登录时保存的 id_token（路由层从 httpOnly cookie 取）。
        带上它 Keycloak 直接终结该 ``sid`` 的会话；不带则 Keycloak 26 先弹登出确认页。

        返回：Keycloak end_session URL；以下情况返回 ``None``（本地吊销已完成，不抛异常）：
        - 本地无 SSO 会话：解码失败 / 缺 iss / issuer 无法映射 / 用户不存在（含本地密码
          登录的 HS256 refresh —— 它无 iss，本就没有 Keycloak 侧会话可结束）；
        - Keycloak 配置缺失（``KEYCLOAK_AUTHORIZE_BASE_URL`` 与 ``KEYCLOAK_SERVER_URL``
          均为空 / ``KEYCLOAK_CLIENT_ID`` 为空）—— 降级为「仅本地登出」，不因跳转失败
          而回滚已完成的吊销。
        """
        import jwt as _jwt

        from capabilities.platform.auth.realm_resolver import (
            resolve_tenant_id_from_issuer,
        )

        try:
            claims = _jwt.decode(
                refresh_token,
                options={"verify_signature": False, "verify_exp": False},
            )
        except _jwt.PyJWTError as exc:
            logger.warning("logout 解码 refresh token 失败，静默返回: %s", exc)
            return None

        issuer = claims.get("iss")
        sub = claims.get("sub")
        sid = claims.get("sid")
        if not issuer:
            logger.warning("logout refresh token 缺 iss，静默返回")
            return None

        try:
            tenant_id = await resolve_tenant_id_from_issuer(self.session, issuer)
        except RealmTenantMismatchError:
            logger.warning("logout issuer 无法映射租户，静默返回: %s", issuer)
            return None

        # 定位 user_id：优先 sub，否则回退 sid → 会话聚合根（refresh token 无 sub 场景）
        user = None
        if sub:
            user = await self.get_user_by_external_id(tenant_id, sub)
        if user is None and sid:
            session_repo = await self._get_session_repo()
            aggregate = await session_repo.get_session(sid)
            if aggregate is not None and aggregate.tenant_id == tenant_id:
                user = await self.user_repo.get_by_id(aggregate.user_id, tenant_id)
        if user is None:
            logger.warning("logout 用户不存在，静默返回: tenant=%s", tenant_id)
            return None

        # 1) 本地吊销先行：无论后续能否产出 SSO 登出 URL，本地会话都必须已失效。
        await self.revoke_user_sessions(tenant_id, user.id)

        # 2) 构造 Keycloak 侧登出 URL（纯函数）。失败只降级，不影响已完成的本地吊销。
        from jonex_core.common.realm_util import realm_name_for_tenant
        from jonex_core.integrations.keycloak_oidc import build_end_session_url

        try:
            url = build_end_session_url(
                realm_name_for_tenant(tenant_id),
                id_token_hint=id_token_hint,
                post_logout_redirect_uri=(
                    getattr(self.config, "KEYCLOAK_POST_LOGOUT_REDIRECT_URI", "") or None
                ),
            )
        except UpstreamServiceError as exc:
            logger.warning(
                "构造 Keycloak 登出 URL 失败，降级为仅本地登出: tenant=%s err=%s",
                tenant_id,
                exc,
            )
            return None

        logger.info("logout 完成本地吊销并产出 SSO 登出 URL: tenant=%s", tenant_id)
        return url

    async def revoke_user_sessions(self, tenant_id: str, user_id: int) -> None:
        """吊销用户所有已登录会话（refresh 家族），账号停用 / SLO 共用。

        session 聚合根按 sid 存储、无 user→sid 反向索引，此处只吊销 refresh 家族；
        家族吊销后 refresh 立即失效，access 短效自然过期，满足「强制登出」语义。
        """
        session_repo = await self._get_session_repo()
        await session_repo.delete_family(tenant_id, user_id)

    async def backchannel_logout(self, logout_token: str) -> None:
        """Keycloak Back-Channel Logout 回调。

        校验 logout_token（RS256 验签 + iss/aud/azp/exp + events/sid/sub/jti 防重放/iat）
        → 原子吊销本地 session + refresh 家族（impersonation 无服务端状态，随家族吊销失效）
        → 记审计 auth.backchannel_logout。
        """
        import time

        import jwt as _jwt

        from capabilities.platform.auth.realm_resolver import (
            resolve_tenant_id_from_issuer,
        )
        from jonex_core.security.oidc import TokenValidationError, verify_rs256_token

        # 1) 未验签读 iss（路由用），再验签（aud/azp 默认取白名单 client_id）
        try:
            unverified = _jwt.decode(
                logout_token,
                options={"verify_signature": False, "verify_exp": False},
            )
        except _jwt.PyJWTError as exc:
            raise TokenValidationError() from exc
        issuer = unverified.get("iss")
        if not issuer:
            raise TokenValidationError()

        # logout_token（OIDC BCL）不含 azp，仅靠 aud（= client_id）校验身份，跳过 azp。
        payload = verify_rs256_token(
            logout_token, expected_issuer=issuer, require_azp=False
        )

        # 2) logout_token 特有校验：events / sid / sub / jti / iat
        events = payload.get("events") or {}
        if "http://schemas.openid.net/event/backchannel-logout" not in events:
            raise TokenValidationError()
        sid = payload.get("sid")
        sub = payload.get("sub")
        jti = payload.get("jti")
        if not sid or not sub or not jti:
            raise TokenValidationError()
        iat = payload.get("iat")
        now = int(time.time())
        if not isinstance(iat, int) or (now - iat) > self.config.KEYCLOAK_BACKCHANNEL_IAT_MAX_AGE_SECONDS:
            raise TokenValidationError()

        # 3) jti 防重放
        session_repo = await self._get_session_repo()
        if not await session_repo.check_and_record_logout_jti(
            jti, self.config.KEYCLOAK_BACKCHANNEL_JTI_TTL_SECONDS
        ):
            raise TokenValidationError()  # 重放

        # 4) issuer → tenant_id；sub(UUID) → external_id → 本地 user（边界唯一映射点）
        tenant_id = await resolve_tenant_id_from_issuer(self.session, issuer)
        user = await self.get_user_by_external_id(tenant_id, sub)

        # 5) 吊销会话聚合根 + refresh 家族（会话聚合根缺失时仍按 sub 兜底吊销家族）
        await session_repo.delete_session(sid)
        if user is not None:
            await self.revoke_user_sessions(tenant_id, user.id)

        # 6) 审计
        await self._record_backchannel_logout_audit(
            tenant_id,
            user_id=user.id if user else None,
            username=user.username if user else None,
            sid=sid,
        )

    async def _record_backchannel_logout_audit(
        self, tenant_id: str, *, user_id, username, sid: str
    ) -> None:
        """记录 SLO backchannel logout 审计（同步直写，失败降级不阻塞）。"""
        try:
            from capabilities.platform.services.audit_log_service import AuditLogService

            svc = AuditLogService(self.session)
            await svc.record(
                tenant_id=tenant_id,
                log_type="LOGIN",
                action="auth.backchannel_logout",
                outcome="SUCCESS",
                service_name="platform",
                user_id=user_id,
                username=username,
                request_params={"sid": sid},
                sync=True,
            )
        except Exception:
            logger.exception("记录 backchannel logout 审计失败（不影响主流程）")

    async def impersonate(self, current_user: dict, target_tenant_id: str) -> ImpersonateResponse:
        """签发模拟态 access token，切换到目标租户视角（沿用平台管理员权限码）。"""
        from sqlalchemy import select

        from jonex_core.security.permission import get_user_permissions

        # 嵌套模拟拒绝
        if current_user.get("impersonated"):
            raise ImpersonationForbiddenError()

        original_tenant_id = require_tenant(current_user["tenant_id"])
        target = require_tenant(target_tenant_id)
        if target == original_tenant_id:
            raise ImpersonationTargetError()

        # 目标租户存在校验
        tenants = await self.tenant_repo.list_active_by_ids([target])
        if target not in tenants:
            raise ImpersonationTargetError()

        # 加载管理员用户（原租户）
        result = await self.session.execute(
            select(User).where(
                User.id == int(current_user["user_id"]),
                User.tenant_id == original_tenant_id,
                User.status == 1,
                User.is_deleted == 0,
            )
        )
        user = result.scalar_one_or_none()
        if not user:
            raise InvalidApiKeyError(message=translate("err.auth.user_not_found_or_disabled", fallback="用户不存在或已禁用"))

        # 管理员权限码全集（原租户）
        perms = await get_user_permissions(original_tenant_id, user.id)

        token = self.user_auth.create_impersonation_token(
            user,
            roles=current_user.get("roles") or [],
            perms=sorted(perms),
            target_tenant_id=target,
        )

        await self._record_impersonation_audit(
            "impersonate_start",
            original_tenant_id,
            original_tenant_id,
            target,
            user_id=user.id,
            username=user.username,
        )

        return ImpersonateResponse(
            token=token,
            target_tenant_id=target,
            target_tenant_name=tenants[target].name,
        )

    async def end_impersonation(self, current_user: dict) -> None:
        """退出模拟态：仅记审计，无服务端状态。"""
        if not current_user.get("impersonated"):
            raise NotImpersonatedError()

        original_tenant_id = require_tenant(current_user.get("original_tenant_id"))
        target_tenant_id = require_tenant(current_user["tenant_id"])

        await self._record_impersonation_audit(
            "impersonate_end",
            original_tenant_id,
            target_tenant_id,
            original_tenant_id,
            user_id=current_user.get("user_id"),
            username=current_user.get("username"),
        )

    async def _record_impersonation_audit(
        self,
        action: str,
        audit_tenant_id: str,
        from_tenant: str,
        to_tenant: str,
        *,
        user_id: int | None,
        username: str | None,
    ) -> None:
        """记录租户模拟审计（同步直写，不阻塞主流程）。"""
        try:
            from capabilities.platform.services.audit_log_service import AuditLogService

            svc = AuditLogService(self.session)
            await svc.record(
                tenant_id=audit_tenant_id,
                log_type="OPERATION",
                action=action,
                outcome="SUCCESS",
                service_name="platform",
                user_id=user_id,
                username=username,
                request_params={"from_tenant": from_tenant, "to_tenant": to_tenant},
                sync=True,
            )
        except Exception:
            logger.exception("记录租户模拟审计日志失败（不影响主流程）")

    async def create_login_ticket(
        self, req: LoginTicketRequest, token: str, client_ip: str = None, user_agent: str = None
    ) -> LoginTicketResponse:
        payload = self.user_auth.decode_token(token)
        tenant_id = require_tenant(payload.get("original_tenant_id") or payload.get("tenant_id"))

        from sqlalchemy import select
        from capabilities.platform.models.user import User

        result = await self.session.execute(
            select(User).where(
                User.id == int(payload["sub"]),
                User.tenant_id == tenant_id,
                User.status == 1,
                User.is_deleted == 0,
            )
        )
        user = result.scalar_one_or_none()
        if not user:
            raise InvalidApiKeyError(message=translate("err.auth.user_not_found_or_disabled", fallback="用户不存在或已禁用"))  # 原消息: 用户不存在或已禁用

        try:
            allowed = json.loads(self.config.AUTH_ALLOWED_REDIRECT_URIS) if self.config.AUTH_ALLOWED_REDIRECT_URIS else {}
        except json.JSONDecodeError:
            logger.error(f"AUTH_ALLOWED_REDIRECT_URIS 解析失败: {self.config.AUTH_ALLOWED_REDIRECT_URIS}")
            raise PermissionDeniedError(message=translate("err.auth.redirect_whitelist_error", fallback="redirect 白名单配置错误"))  # 原消息: redirect 白名单配置错误

        allowed_uris = allowed.get(req.appId, [])
        if not allowed_uris:
            raise PermissionDeniedError(
                message=translate("err.auth.appid_whitelist_missing", params={"app_id": req.appId}, fallback=f"未找到 appId={req.appId} 的白名单配置")
            )  # 原消息: 未找到 appId={app_id} 的白名单配置
        if not _redirect_uri_allowed(req.redirectUri, allowed_uris):
            raise PermissionDeniedError(
                message=translate("err.auth.redirect_uri_not_whitelisted", params={"uri": req.redirectUri}, fallback=f"redirectUri 不在白名单中: {req.redirectUri}")
            )  # 原消息: redirectUri 不在白名单中: {uri}

        ticket_plain = self.user_auth.create_login_ticket_plaintext()
        ticket_hash = self.user_auth.hash_login_ticket(ticket_plain)
        expire_seconds = self.config.LOGIN_TICKET_EXPIRE_SECONDS
        expires_at = datetime.utcnow() + timedelta(seconds=expire_seconds)

        ticket_record = LoginTicket(
            ticket_hash=ticket_hash,
            tenant_id=user.tenant_id,
            user_id=user.id,
            app_id=req.appId,
            redirect_uri=req.redirectUri,
            state=req.state,
            expires_at=expires_at,
            client_ip=client_ip,
            user_agent=user_agent,
        )
        self.session.add(ticket_record)
        await self.session.flush()
        # login ticket 会被浏览器下一次跳转立即兑换，必须在返回明文 ticket 前持久化。
        await self.session.commit()

        logger.info(
            f"创建 login ticket: hash={ticket_hash[:8]}... user={user.username} "
            f"app={req.appId} redirect={req.redirectUri}"
        )

        return LoginTicketResponse(ticket=ticket_plain, expires_in=expire_seconds)

    async def exchange_ticket(self, req: ExchangeTicketRequest) -> LoginResponse:
        from sqlalchemy import select
        from capabilities.platform.models.user import User

        ticket_hash = self.user_auth.hash_login_ticket(req.ticket)

        result = await self.session.execute(
            select(LoginTicket)
            .where(LoginTicket.ticket_hash == ticket_hash)
            .with_for_update()
        )
        ticket_record = result.scalar_one_or_none()

        if not ticket_record:
            raise InvalidApiKeyError(message=translate("err.auth.invalid_ticket", fallback="ticket 不存在或格式不合法"))  # 原消息: ticket 不存在或格式不合法

        now = datetime.utcnow()
        if ticket_record.expires_at < now:
            raise TokenExpiredError()  # 登录 ticket 已过期
        if ticket_record.used_at is not None:
            raise TokenExpiredError()  # 登录 ticket 已被使用

        if ticket_record.app_id != req.appId:
            raise PermissionDeniedError(message=translate("err.auth.appid_ticket_mismatch", fallback="appId 与 ticket 绑定不一致"))  # 原消息: appId 与 ticket 绑定不一致
        if ticket_record.redirect_uri != req.redirectUri:
            raise PermissionDeniedError(message=translate("err.auth.redirect_uri_ticket_mismatch", fallback="redirectUri 与 ticket 绑定不一致"))  # 原消息: redirectUri 与 ticket 绑定不一致
        if ticket_record.state != req.state:
            raise PermissionDeniedError(message=translate("err.auth.state_ticket_mismatch", fallback="state 与 ticket 绑定不一致"))  # 原消息: state 与 ticket 绑定不一致
        tenant_id = require_tenant(ticket_record.tenant_id)

        result = await self.session.execute(
            select(User).where(
                User.id == ticket_record.user_id,
                User.tenant_id == tenant_id,
                User.status == 1,
                User.is_deleted == 0,
            )
        )
        user = result.scalar_one_or_none()
        if not user:
            raise InvalidApiKeyError(message=translate("err.auth.user_not_found_or_disabled", fallback="用户不存在或已禁用"))  # 原消息: 用户不存在或已禁用

        login_response = await self._build_login_response(user)
        ticket_record.used_at = now
        await self.session.flush()
        await self.session.commit()

        logger.info(
            f"兑换 login ticket 成功: hash={ticket_hash[:8]}... user={user.username} "
            f"app={req.appId}"
        )

        try:
            from capabilities.platform.services.audit_log_service import AuditLogService
            svc = AuditLogService(self.session)
            await svc.record(
                tenant_id=tenant_id,
                log_type="LOGIN",
                action="auth.exchange_ticket",
                outcome="SUCCESS",
                service_name="platform",
                user_id=user.id,
                username=user.username,
                sync=True,
            )
        except Exception:
            logger.exception("记录票据兑换审计日志失败")

        return login_response
