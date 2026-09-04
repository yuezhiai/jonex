"""RBAC 权限内核 — 权限码查询、判定依赖与缓存失效。

判定以 DB 权限集合为准（tenant_id + user_id）；Redis 缓存 30s 兜底。
"""
import json
import logging
from contextvars import ContextVar, Token

from fastapi import Depends

from jonex_core.common.cache import get_redis_client
from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import PermissionDeniedError
from jonex_core.common.i18n import translate
from jonex_core.security.user_auth import get_current_user

logger = logging.getLogger(__name__)

# 特权租户写死点（唯一）；判定代码其余部分不散落租户名
PLATFORM_ADMIN_TENANT_IDS: frozenset[str] = frozenset({"tenant_jonex_demo"})
PERM_CACHE_TTL = 30

# 模拟态上下文（invoke 链路：Sidecar → 能力服务透传）。能力服务侧 has_permission
# 兜底短路用，避免逐层把 impersonated/perms 穿到每个 has_permission 调用点。
_impersonation_ctx: ContextVar[dict | None] = ContextVar("jonex_impersonation", default=None)


def set_impersonation_context(ctx: dict | None) -> Token:
    """在能力服务 /invoke 入口注入模拟态上下文（impersonated/perms/original_tenant_id）。"""
    return _impersonation_ctx.set(ctx)


def get_impersonation_context() -> dict | None:
    return _impersonation_ctx.get()


def reset_impersonation_context(token: Token) -> None:
    _impersonation_ctx.reset(token)


def _cache_key(tenant_id: str, user_id: int) -> str:
    return f"perm:{tenant_id}:{user_id}"


_PERMISSION_SQL = """
    SELECT DISTINCT p.code
      FROM platform.user_roles ur
      JOIN platform.role_permissions rp
        ON rp.tenant_id = ur.tenant_id AND rp.role_id = ur.role_id
      JOIN platform.permissions p ON p.id = rp.permission_id
     WHERE ur.tenant_id = :tenant_id
       AND ur.user_id = :user_id
"""


async def get_user_permissions(
    tenant_id: str,
    user_id: int,
    *,
    impersonated: bool = False,
    perms: list[str] | None = None,
) -> set[str]:
    """用户权限码集合（缓存 → DB）。

    注意：get_redis_client() 永不返回 None（懒建连接池，不实际连接），
    Redis 不可用时 get/setex 会抛异常——读写两侧都降级为直查 DB，绝不 500。

    模拟态（impersonated=True）直接返回 token 内嵌 perms，不查 DB/Redis——
    供需要完整权限集合（非单码判定）的调用点使用（如菜单过滤、权限清单 scope）。
    """
    if impersonated:
        return set(perms or [])
    redis = get_redis_client()
    key = _cache_key(tenant_id, user_id)
    try:
        cached = await redis.get(key)
    except Exception:
        logger.warning("权限缓存读取失败 %s，降级直查 DB", key, exc_info=True)
        cached = None
    if cached is not None:
        try:
            return set(json.loads(cached))
        except (ValueError, TypeError):
            pass

    from sqlalchemy import text

    async with get_db_session() as session:
        result = await session.execute(text(_PERMISSION_SQL), {"tenant_id": tenant_id, "user_id": user_id})
        codes = set(result.scalars().all())

    try:
        await redis.setex(key, PERM_CACHE_TTL, json.dumps(sorted(codes)))
    except Exception:
        logger.warning("权限缓存写入失败 %s", key, exc_info=True)
    return codes


async def has_permission(
    tenant_id: str,
    user_id: int,
    code: str,
    *,
    impersonated: bool = False,
    perms: list[str] | None = None,
) -> bool:
    """软降级端点用：布尔判定。

    模拟态（impersonated=True）时直接判 token 内嵌 perms 权限码，不查 DB/Redis。
    能力服务（knowledge_base/business_domain）经 invoke 链路透传的模拟上下文，
    通过 contextvar 兜底（无显式参数时同样短路，避免按原始 user_id 误查权限）。
    """
    if impersonated:
        return code in (perms or [])
    ctx = _impersonation_ctx.get()
    if ctx and ctx.get("impersonated"):
        return code in (ctx.get("perms") or [])
    return code in await get_user_permissions(tenant_id, user_id)


def require_permission(*codes: str):
    """FastAPI 依赖：用户权限码与所需码交集非空放行，否则 403。

    测试 token（user_id=0，get_current_user 特判映射 role='admin'）直接放行，
    与既有本地调试兜底一致。模拟态 token 直接取 token 内嵌 perms 权限码，
    不查 DB/Redis（沿用管理员权限码）。
    """

    async def _check(current_user: dict = Depends(get_current_user)) -> dict:
        if current_user.get("user_id") == 0 and current_user.get("role") == "admin":
            return current_user
        if current_user.get("impersonated"):
            if not (set(codes) & set(current_user.get("perms") or [])):
                raise PermissionDeniedError(
                    message=translate(
                        "err.auth.insufficient_permission",
                        params={"required": ", ".join(codes)},
                        fallback=f"权限不足：需要权限 {', '.join(codes)}",
                    )
                )
            return current_user
        perms = await get_user_permissions(current_user["tenant_id"], current_user["user_id"])
        if not (set(codes) & perms):
            raise PermissionDeniedError(
                message=translate(
                    "err.auth.insufficient_permission",
                    params={"required": ", ".join(codes)},
                    fallback=f"权限不足：需要权限 {', '.join(codes)}",
                )
            )
        return current_user

    return _check


def require_any_permission(*codes: str):
    """同 require_permission（多码任一）；语义别名，保持调用侧可读。"""
    return require_permission(*codes)


async def invalidate_user_permissions(tenant_id: str, user_id: int) -> None:
    """DEL 单用户缓存（get_redis_client 永不返回 None；异常吞掉，TTL 兜底）。"""
    try:
        await get_redis_client().delete(_cache_key(tenant_id, user_id))
    except Exception:
        logger.warning("权限缓存删除失败 %s", _cache_key(tenant_id, user_id), exc_info=True)


async def invalidate_users_permissions(tenant_id: str, user_ids: list[int]) -> None:
    """批量失效（纯 Redis，不碰 DB）——角色维度失效由服务层用自身 session 查
    user_roles 拿到用户 ids 后调用本函数（避免事务内另开 DB 连接）。"""
    for uid in user_ids:
        await invalidate_user_permissions(tenant_id, uid)
