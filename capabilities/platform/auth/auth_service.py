"""
认证服务 — 独立运行在 platform 容器中，通过 Sidecar 代理调用。
"""
import json
import logging
from datetime import datetime, timedelta

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
    PermissionDeniedError,
    TokenExpiredError,
)
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant
from capabilities.platform.repository.user_repository import UserRepository
from capabilities.platform.repository.tenant_repository import TenantRepository
from capabilities.platform.repository.system_config_repository import SystemConfigRepository
from capabilities.platform.models.user import User
from capabilities.platform.models.login_ticket import LoginTicket
from capabilities.platform.services.user_service import UserService
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

# [jonex] 权限重构 B1：管理员标识码（方案 docs/permissions/PERMISSIONS_REDESIGN.md §3.2-A）
_PLATFORM_ADMIN_CODE = "platform:admin"
_TENANT_ADMIN_CODE = "tenant:admin"


def _is_platform_admin(perms: set[str]) -> bool:
    """平台管理员布尔（下发给前端 shellContext）。"""
    return _PLATFORM_ADMIN_CODE in perms


def _is_tenant_admin(perms: set[str]) -> bool:
    """租户管理员布尔（下发给前端 shellContext）。

    [jonex] B1 变更：判据从 `user:write` 换为 `tenant:admin`（platform:admin 为上位）。

    为什么这是个修复而非改名：现状前端看 `user:write`、后端
    `space_permission_service.is_tenant_admin` 看 `tenant:write`，两套口径。
    自定义角色只配 `user:write` 时，前端把人当租户管理员渲染（显示全部管理入口），
    后端却拒绝 → 用户看得到点不动。现在两边看同一组码，必须保持同步：
    改这里就要同步改 space_permission_service._TENANT_ADMIN，反之亦然。
    回归护栏见 tests/unit/test_tenant_admin_code.py::TestFrontBackAgreement。

    提取成模块级纯函数的原因：原先这个表达式在 login() 与 me() 两处内联，
    改一处漏一处（现状 §10.1 就是这么来的）。
    """
    return _TENANT_ADMIN_CODE in perms or _is_platform_admin(perms)


class AuthService:
    """平台认证服务"""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.user_repo = UserRepository(session)
        self.tenant_repo = TenantRepository(session)
        self.system_config_repo = SystemConfigRepository(session)
        self.user_auth = get_user_auth()
        self.config = get_config()

    async def login(self, tenant_id: str, req: LoginRequest, client_ip: str | None = None) -> LoginResponse:
        """登录：必须显式指定租户（Sidecar 已在入口拦截缺失租户，这里兜底再校验）。

        安全配置真实消费：
        - login_lock_threshold / lock_duration：连续失败锁定（Redis 计数，降级不锁定）
        - password_min_length：登录时校验密码最小长度
        - session_timeout：访问令牌有效期（分钟）
        """
        tenant_id = require_tenant(tenant_id)
        security = await self._get_security_config()
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

    async def _get_security_config(self) -> dict:
        """读取安全配置（DB 缺失/异常时回退默认值，不阻断登录）。"""
        defaults = {
            "session_timeout": 480,      # 访问令牌有效期（分钟，8 小时）
            "password_min_length": 8,    # 密码最小长度
            "login_lock_threshold": 5,   # 连续失败锁定阈值（0=不锁定）
            "lock_duration": 15,         # 锁定时长（分钟）
        }
        result = dict(defaults)
        try:
            for key in defaults:
                raw = await self.system_config_repo.get_value(key)
                if raw is None:
                    continue
                raw = str(raw).strip()
                if not raw:
                    continue
                result[key] = int(float(raw))
        except Exception:
            logger.warning("读取安全配置失败，使用默认值", exc_info=True)
        return result

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
        security = await self._get_security_config()
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

    async def refresh(self, token: str) -> LoginResponse:
        payload = self.user_auth.decode_token(token)
        if payload.get("type") != "refresh":
            raise TokenExpiredError()  # refresh 接口传入了非 refresh 类型的 Token
        tenant_id = require_tenant(payload.get("tenant_id"))

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

        # 主路径由 _build_login_response 重查 DB 拿最新角色；仅 DB 查询异常时兜底旧 token roles。
        fallback_roles = payload.get("roles") or ([payload.get("role")] if payload.get("role") else [])
        return await self._build_login_response(user, fallback_roles=fallback_roles)

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
        if not any(req.redirectUri.startswith(u) for u in allowed_uris):
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
