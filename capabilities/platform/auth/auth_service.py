"""
认证服务 — 独立运行在 platform 容器中，通过 Sidecar 代理调用。
"""
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.config import get_config
from jonex_core.common.exceptions import (
    InternalError,
    InvalidApiKeyError,
    InvalidCredentialsError,
    JonexException,
    PermissionDeniedError,
    TokenExpiredError,
)
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant
from capabilities.platform.repository.user_repository import UserRepository
from capabilities.platform.repository.tenant_repository import TenantRepository
from capabilities.platform.models.user import User
from capabilities.platform.models.login_ticket import LoginTicket
from capabilities.platform.services.user_service import UserService
from jonex_core.security.user_auth import get_user_auth
from capabilities.platform.dtos.auth import (
    LoginFlowResponse,
    LoginRequest,
    LoginResponse,
    LoginTicketRequest,
    LoginTicketResponse,
    TenantOption,
    TenantSelectionRequiredResponse,
    ExchangeTicketRequest,
    UserInfo,
)

logger = logging.getLogger(__name__)


class AuthService:
    """平台认证服务"""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.user_repo = UserRepository(session)
        self.tenant_repo = TenantRepository(session)
        self.user_auth = get_user_auth()
        self.config = get_config()

    async def login(self, tenant_id: str | None, req: LoginRequest, client_ip: str | None = None) -> LoginFlowResponse:
        try:
            if tenant_id:
                user = await self.user_repo.get_by_username(require_tenant(tenant_id), req.username)
                if not user:
                    raise InvalidCredentialsError()
                if not self.user_auth.verify_password(req.password, user.password_hash):
                    raise InvalidCredentialsError()
                result = await self._build_login_response(
                    user,
                    update_last_login=True,
                    inactive_tenant_message=translate(3008, fallback="用户名或密码错误"),  # 原消息: 用户名或密码错误
                )
                await self._record_login_audit(user.tenant_id, user, client_ip, "SUCCESS")
                return result

            users = list(await self.user_repo.list_active_by_username(req.username))
            matched_users = [
                user for user in users
                if self.user_auth.verify_password(req.password, user.password_hash)
            ]
            if not matched_users:
                raise InvalidCredentialsError()

            tenant_ids = [user.tenant_id for user in matched_users]
            tenants = await self.tenant_repo.list_active_by_ids(tenant_ids)
            matched_users = [user for user in matched_users if user.tenant_id in tenants]
            if not matched_users:
                raise InvalidCredentialsError()
            if len(matched_users) == 1:
                user = matched_users[0]
                result = await self._build_login_response(
                    user,
                    update_last_login=True,
                    inactive_tenant_message=translate(3008, fallback="用户名或密码错误"),  # 原消息: 用户名或密码错误
                )
                await self._record_login_audit(user.tenant_id, user, client_ip, "SUCCESS")
                return result

            tenant_options = [
                TenantOption(
                    tenant_id=user.tenant_id,
                    tenant_name=tenants[user.tenant_id].name,
                )
                for user in matched_users
            ]
            return TenantSelectionRequiredResponse(tenant_options=tenant_options)
        except InvalidApiKeyError:
            # 登录失败：记录失败日志
            await self._record_login_audit(
                tenant_id or "unknown",
                None,
                client_ip,
                "FAILED",
                error_message="用户名或密码错误",
                username=req.username,
            )
            raise
        except JonexException:
            # JonexException 子类直接向上传播（已有 i18n message）
            raise
        except Exception as e:
            # 将未预期的异常（如数据库连接失败）包装为 JonexException
            raise InternalError(message=str(e))

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
        access_token = self.user_auth.create_access_token(user, role_names)
        refresh_token = self.user_auth.create_refresh_token(user, role_names)

        if update_last_login:
            user.last_login_at = datetime.utcnow()
            await self.session.flush()

        logger.info(f"用户登录成功: {user.username} (id={user.id})")

        return LoginResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=self.user_auth.access_expire_hours * 3600,
            user=UserInfo(
                user_id=user.id,
                username=user.username,
                display_name=user.display_name,
                tenant_id=user.tenant_id,
                tenant_name=tenant.name,
                role=user.role,
                roles=role_names,
                is_platform_admin=("platform:admin" in perms),
                is_tenant_admin=("user:write" in perms),
                permissions=sorted(perms),
            ),
        )

    async def me(self, token: str) -> UserInfo:
        payload = self.user_auth.decode_token(token)
        tenant_id = require_tenant(payload.get("tenant_id"))

        from sqlalchemy import select

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

        tenants = await self.tenant_repo.list_active_by_ids([user.tenant_id])
        tenant = tenants.get(user.tenant_id)
        if not tenant:
            raise InvalidApiKeyError(message=translate("err.auth.user_not_found_or_disabled", fallback="用户不存在或已禁用"))  # 原消息: 用户不存在或已禁用

        role_names = await UserService(self.session).get_role_names(user.tenant_id, user.id)

        from jonex_core.security.permission import get_user_permissions

        perms = await get_user_permissions(user.tenant_id, user.id)

        return UserInfo(
            user_id=user.id,
            username=user.username,
            display_name=user.display_name,
            tenant_id=user.tenant_id,
            tenant_name=tenant.name,
            role=user.role,
            roles=role_names,
            is_platform_admin=("platform:admin" in perms),
            is_tenant_admin=("user:write" in perms),
            permissions=sorted(perms),
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

    async def create_login_ticket(
        self, req: LoginTicketRequest, token: str, client_ip: str = None, user_agent: str = None
    ) -> LoginTicketResponse:
        payload = self.user_auth.decode_token(token)
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
