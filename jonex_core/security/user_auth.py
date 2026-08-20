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
from jonex_core.common.tenant import require_tenant

logger = logging.getLogger(__name__)


class UserAuth:
    """用户认证 — 密码哈希 + 用户 JWT"""

    def __init__(self):
        config = get_config()
        self.secret = config.JWT_SECRET
        self.algorithm = config.JWT_ALGORITHM
        self.access_expire_hours = config.USER_JWT_EXPIRE_HOURS
        self.refresh_expire_days = config.USER_JWT_REFRESH_DAYS
        self.bcrypt_rounds = config.BCRYPT_ROUNDS

    def hash_password(self, password: str) -> str:
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=self.bcrypt_rounds)).decode()

    def verify_password(self, password: str, password_hash: str) -> bool:
        return bcrypt.checkpw(password.encode(), password_hash.encode())

    def _create_token(self, user, roles: list[str] | None, expires_delta: timedelta, token_type: str) -> str:
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
        return jwt.encode(payload, self.secret, algorithm=self.algorithm)

    def create_access_token(self, user, roles: list[str] | None = None) -> str:
        return self._create_token(user, roles, timedelta(hours=self.access_expire_hours), "user")

    def create_refresh_token(self, user, roles: list[str] | None = None) -> str:
        return self._create_token(user, roles, timedelta(days=self.refresh_expire_days), "refresh")

    def decode_token(self, token: str) -> dict:
        try:
            payload = jwt.decode(token, self.secret, algorithms=[self.algorithm])
            if payload.get("type") not in ("user", "refresh"):
                raise TokenExpiredError()  # Token 类型不合法（非 user/refresh）
            return payload
        except jwt.ExpiredSignatureError:
            raise TokenExpiredError()  # Token 签名已过期
        except jwt.PyJWTError as e:
            logger.warning(f"JWT 验签失败: {e}")
            raise TokenExpiredError()  # Token 格式无效或签名校验失败


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


async def get_current_user(authorization: str = Header(...)) -> dict:
    """FastAPI 依赖：从 Bearer token 解析当前用户（Sidecar / platform 共用）。

    platform 侧 require_admin 收严后，mcp_* 30 端点经此依赖做 JWT 校验。
    本地调试/演示用测试 token ``jonex_test_{tenant_id}`` 无法 JWT 解码，
    此处特判映射为 admin 用户兜底（与 Sidecar ``_tenant_from_authorization`` 一致），
    否则本地调试所有 admin 端点一律 401。
    """
    if not authorization.startswith("Bearer "):
        raise TokenExpiredError(message=translate("err.auth.missing_bearer_token", fallback="缺少 Bearer token"))
    token = authorization[7:]
    if token.startswith("jonex_test_"):
        return {
            "user_id": 0,
            "tenant_id": require_tenant(token.removeprefix("jonex_test_")),
            "username": "test_token",
            "role": "admin",
            "roles": ["admin"],
        }
    auth = get_user_auth()
    payload = auth.decode_token(token)
    return {
        "user_id": int(payload["sub"]),
        "tenant_id": payload["tenant_id"],
        "username": payload["username"],
        "role": payload["role"],
        "roles": payload.get("roles") or ([payload.get("role")] if payload.get("role") else []),
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
    """依赖本身（非工厂）：roles 数组含 'admin' 或「系统管理员」角色名放行。

    用法::

        @router.post("/mcp-keys")
        async def create_key(..., _admin: dict = Depends(require_admin)):
            ...

    非 admin 用户调用返回 403 PermissionDeniedError。
    """
    user_roles = set(current_user.get("roles") or [])
    if not (user_roles & {"admin", "系统管理员"}):
        raise PermissionDeniedError(
            message=translate(
                "err.auth.insufficient_role",
                params={"required": "admin/系统管理员", "current": ", ".join(user_roles)},
                fallback="需要角色: admin/系统管理员",
            )
        )
    return current_user
