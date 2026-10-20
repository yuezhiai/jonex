"""
用户认证模块

密码哈希 + 用户 JWT 生成/验签 + 一次性登录票据。仅在 Sidecar 中使用。
"""
import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
from fastapi import Depends, Header

from jonex_core.common.config import get_config
from jonex_core.common.exceptions import TokenExpiredError, PermissionDeniedError
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant, require_test_token_allowed

logger = logging.getLogger(__name__)


def extract_bearer_token(authorization: Optional[str]) -> Optional[str]:
    """从 ``Authorization`` 头取出 token，并去掉首尾空白。

    ``Bearer  <jwt>``（双空格）会让直接切片 ``authorization[7:]`` 得到
    ``" <jwt>"``，JWT 验签失败 —— 这与网关 ``require_auth_header`` /
    ``identity._decode`` / ``tenant._tenant_from_bearer_token`` 的 ``.strip()``
    口径不一致：同一个头在 invoke 路径可用、在 REST 透传却 401。

    非 ``Bearer`` 方案、空 token、缺头一律返回 ``None``，由调用方按既有
    认证失败路径处理（不在这里抛异常，避免改变各入口的错误码）。
    """
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization[7:].strip()
    return token or None


class UserAuth:
    """用户认证 — 密码哈希 + 用户 JWT"""

    def __init__(self):
        config = get_config()
        self.secret = config.JWT_SECRET
        self.algorithm = config.JWT_ALGORITHM
        self.access_expire_hours = config.USER_JWT_EXPIRE_HOURS
        self.refresh_expire_days = config.USER_JWT_REFRESH_DAYS
        self.impersonate_expire_minutes = config.IMPERSONATE_EXPIRE_MINUTES
        self.bcrypt_rounds = config.BCRYPT_ROUNDS

    def hash_password(self, password: str) -> str:
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=self.bcrypt_rounds)).decode()

    def verify_password(self, password: str, password_hash: str) -> bool:
        # 影子用户 password_hash 可为 None：本地登录统一返回 False → InvalidCredentialsError，
        # 不抛 AttributeError 冒泡 500，也不泄露账号存在性。
        if not password_hash:
            return False
        return bcrypt.checkpw(password.encode(), password_hash.encode())

    def _create_token(
        self,
        user,
        roles: list[str] | None,
        expires_delta: timedelta,
        token_type: str,
        extra_claims: dict | None = None,
    ) -> str:
        now = datetime.now(timezone.utc)
        payload = {
            "sub": str(user.id),
            "tenant_id": user.tenant_id,
            "username": user.username,
            "role": user.role,
            "roles": roles or [],
            "type": token_type,
            "exp": now + expires_delta,
            "iat": now,
        }
        if extra_claims:
            payload.update(extra_claims)
        return jwt.encode(payload, self.secret, algorithm=self.algorithm)

    def create_access_token(self, user, roles: list[str] | None = None, expires_minutes: int | None = None) -> str:
        minutes = expires_minutes if expires_minutes is not None else self.access_expire_hours * 60
        return self._create_token(user, roles, timedelta(minutes=minutes), "user")

    def create_refresh_token(self, user, roles: list[str] | None = None) -> str:
        return self._create_token(user, roles, timedelta(days=self.refresh_expire_days), "refresh")

    def create_impersonation_token(
        self,
        user,
        roles: list[str] | None,
        perms: list[str] | None,
        target_tenant_id: str,
    ) -> str:
        """签发模拟态 access token（type=user，含 impersonated/original_tenant_id/perms）。

        仅签发 access token，不签发 refresh；refresh 端点天然拒绝 type=user。
        """
        expires_delta = timedelta(minutes=self.impersonate_expire_minutes)
        extra_claims = {
            "tenant_id": target_tenant_id,
            "perms": perms or [],
            "impersonated": True,
            "original_tenant_id": user.tenant_id,
        }
        return self._create_token(user, roles, expires_delta, "user", extra_claims=extra_claims)

    def decode_token(self, token: str) -> dict:
        try:
            payload = jwt.decode(token, self.secret, algorithms=[self.algorithm])
            if payload.get("type") not in ("user", "refresh"):
                raise TokenExpiredError()  # Token 类型不合法（非 user/refresh）
            self._validate_claims(payload)
            return payload
        except jwt.ExpiredSignatureError:
            raise TokenExpiredError()  # Token 签名已过期
        except jwt.PyJWTError as e:
            logger.warning(f"JWT 验签失败: {e}")
            raise TokenExpiredError()  # Token 格式无效或签名校验失败

    @staticmethod
    def _validate_claims(payload: dict) -> None:
        """校验本地 HS256 token 必需 claim 的存在性与类型。

        缺 ``sub`` / ``tenant_id`` / ``username`` / ``role``，或 ``sub`` 非整数 →
        抛 :class:`TokenExpiredError`(401)，禁止裸 ``KeyError``/``ValueError`` 泄漏为 500。
        调用方（get_current_user / me / sidecar 租户解析）据此安全地 ``int(payload["sub"])``。
        """
        try:
            int(payload["sub"])
        except (KeyError, TypeError, ValueError):
            raise TokenExpiredError(message="token 缺少合法的 sub claim")
        for field in ("tenant_id", "username", "role"):
            if payload.get(field) is None:
                raise TokenExpiredError(message=f"token 缺少 {field} claim")


    def create_login_ticket_plaintext(self) -> str:
        """生成一次性登录票据明文（32 字节 urlsafe base64）"""
        return secrets.token_urlsafe(32)

    def hash_login_ticket(self, ticket: str) -> str:
        """对 ticket 明文做 HMAC-SHA256，返回 hex digest。使用 JWT_SECRET 作为 key"""
        return hmac.new(
            self.secret.encode(),
            ticket.encode(),
            hashlib.sha256,
        ).hexdigest()


_user_auth_instance: Optional[UserAuth] = None


def get_user_auth() -> UserAuth:
    global _user_auth_instance
    if _user_auth_instance is None:
        _user_auth_instance = UserAuth()
    return _user_auth_instance


async def get_current_user(authorization: Optional[str] = Header(None)) -> dict:
    """FastAPI 依赖：从 Bearer token 解析当前用户（Sidecar / platform 共用）。

    ``Authorization`` 声明为可选、缺失时由本函数抛 ``TokenExpiredError``
    （401 / 3003），而不是交给 FastAPI 的必填 Header 校验（会变成 422 / 1001）：
    前端与网关都把「未认证」统一当 401 处理，422 会让「该跳登录页」的判断漏掉。

    platform 侧 require_admin 收严后，mcp_* 30 端点经此依赖做 JWT 校验。
    本地调试/演示用测试 token ``jonex_test_{tenant_id}`` 无法 JWT 解码，
    此处特判映射为 admin 用户兜底（与 Sidecar ``_tenant_from_authorization`` 一致），
    否则本地调试所有 admin 端点一律 401。

    安全守卫：测试 token 后门的门控已收敛到公共守卫 ``require_test_token_allowed()``
    （tenant 模块），仅开发环境（ENV=dev）放行，test/uat/prod 一律拒绝，
    杜绝无验签 admin 绕过（对齐 api_gateway GATEWAY_API_KEY 生产哨兵守卫模式）。
    """
    if not authorization:
        raise TokenExpiredError(message=translate("err.auth.missing_auth_header", fallback="缺少 Authorization 请求头"))
    if not authorization.startswith("Bearer "):
        raise TokenExpiredError(message=translate("err.auth.missing_bearer_token", fallback="缺少 Bearer token"))
    token = extract_bearer_token(authorization)
    if not token:
        raise TokenExpiredError(message=translate("err.auth.missing_bearer_token", fallback="缺少 Bearer token"))
    if token.startswith("jonex_test_"):
        require_test_token_allowed()
        return {
            "user_id": 0,
            "tenant_id": require_tenant(token.removeprefix("jonex_test_")),
            "username": "test_token",
            "role": "admin",
            "roles": ["admin"],
            "perms": [],
            "impersonated": False,
        }
    auth = get_user_auth()
    payload = auth.decode_token(token)
    # `decode_token` 同时接受 user / refresh（续期端点要靠它解 refresh），
    # 访问令牌的判别必须由调用方做：refresh token 有效期 30 天，当成 access
    # 用等于给了一条长效通行证（B6 终审只堵了网关 invoke 路径）。
    if payload.get("type") != "user":
        raise TokenExpiredError(message=translate("err.auth.user_token_required", fallback="需要用户访问令牌"))
    return {
        "user_id": int(payload["sub"]),
        "tenant_id": payload["tenant_id"],
        "username": payload["username"],
        "role": payload["role"],
        "roles": payload.get("roles") or ([payload.get("role")] if payload.get("role") else []),
        "perms": payload.get("perms") or [],
        "impersonated": bool(payload.get("impersonated")),
        "original_tenant_id": payload.get("original_tenant_id"),
    }


def require_role(*roles: str):
    """FastAPI 依赖工厂（sync）：校验用户角色（roles 数组口径）。"""

    async def _check_role(current_user: dict = Depends(get_current_user)):
        user_roles = set(current_user.get("roles") or [])
        if not (user_roles & set(roles)):
            raise PermissionDeniedError(
                message=translate(
                    "err.auth.insufficient_role",
                    params={"required": ", ".join(roles), "current": ", ".join(user_roles)},
                    fallback=f"需要角色: {', '.join(roles)}",
                )
            )
        return current_user

    return _check_role


async def require_admin(current_user: dict = Depends(get_current_user)):
    """依赖本身（非工厂）：roles 数组含 'admin' 或「租户管理员」角色名放行。

    用法::

        @router.post("/mcp-keys")
        async def create_key(..., _admin: dict = Depends(require_admin)):
            ...

    非 admin 用户调用返回 403 PermissionDeniedError。
    """
    user_roles = set(current_user.get("roles") or [])
    if not (user_roles & {"admin", "租户管理员"}):
        raise PermissionDeniedError(
            message=translate(
                "err.auth.insufficient_role",
                params={"required": "admin/租户管理员", "current": ", ".join(user_roles)},
                fallback="需要角色: admin/租户管理员",
            )
        )
    return current_user
