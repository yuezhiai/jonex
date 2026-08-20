#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""MCP Server - MCP Key 鉴权中间件。

单一文件包含 5 个逻辑块：
  1. McpAuthContext dataclass + contextvar
  2. McpAuthError 异常类
  3. 函数式 Repository：find_by_key_hash / update_last_used
  4. ASGI 中间件 McpAuthMiddleware
  5. 守卫函数：require_mcp_auth / require_kb_scope

纯 ASGI 中间件（不使用 BaseHTTPMiddleware——与 Streamable HTTP 不兼容）。
"""
import asyncio
import logging
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Optional

from starlette.types import ASGIApp, Receive, Scope, Send

from crypto import hash_mcp_key
from db import get_pool

logger = logging.getLogger(__name__)

# ============================================================================
# Block 1: McpAuthContext dataclass + contextvar
# ============================================================================

@dataclass
class McpAuthContext:
    """MCP Key 鉴权上下文——单次请求的有效鉴权信息。

    通过 contextvar 注入，请求结束后 finally 块中 reset() 清理。
    """
    key_id: str
    tenant_id: str
    key_type: str = "service"            # "service" | "write"（来源表）
    permissions: list[str] = field(default_factory=lambda: ["view"])
    allowed_kb_ids: list[str] = field(default_factory=list)
    grants: list[dict] = field(default_factory=list)   # 写 Key 原始 grants（目录级范围）
    space_id: str | None = None


_auth_ctx: ContextVar[Optional["McpAuthContext"]] = ContextVar(
    "jonex_mcp_auth", default=None
)

# Re-open McpAuthContext to add class methods (decoupled from dataclass definition)
McpAuthContext.set = classmethod(  # type: ignore[attr-defined]
    lambda cls, ctx: _auth_ctx.set(ctx)
)
McpAuthContext.get = classmethod(  # type: ignore[attr-defined]
    lambda cls: _auth_ctx.get()
)
McpAuthContext.reset = classmethod(  # type: ignore[attr-defined]
    lambda cls, token: _auth_ctx.reset(token)
)
McpAuthContext.clear = classmethod(  # type: ignore[attr-defined]
    lambda cls: _auth_ctx.set(None)
)


# ============================================================================
# Block 2: McpAuthError 异常类
# ============================================================================

class McpAuthError(Exception):
    """MCP Key 鉴权异常。

    错误消息脱敏——不在 message 中暴露 SQL、stack trace、内部详情。
    Key 不存在和 Key 已撤销返回相同错误消息（防枚举攻击）。
    """

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(message)

    def to_dict(self) -> dict:
        return {"code": self.status_code, "message": self.message}


# ============================================================================
# Block 3: 函数式 Repository
# ============================================================================

async def find_by_key_hash(key_hash: str) -> dict | None:
    """查找有效的 MCP Key 记录（未撤销/未停用/未过期/未删除）。

    使用参数化查询 $1，状态过滤（revoked_at/disabled_at/expires_at/is_deleted）
    通过 WHERE 子句隐式完成，只放行 active 状态的 Key。
    permissions 列为逗号分隔 VARCHAR，读取后 split 为 list。
    allowed_kb_ids 为 JSONB，asyncpg 自动反序列化为 list。

    Args:
        key_hash: MCP Key 的 HMAC-SHA256 hash（64 字符 hex）

    Returns:
        dict: {'id', 'tenant_id', 'permissions', 'allowed_kb_ids'} 或 None
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, tenant_id, permissions, allowed_kb_ids, space_id
               FROM platform.mcp_keys
               WHERE key_hash = $1
                 AND revoked_at IS NULL
                 AND disabled_at IS NULL
                 AND is_deleted = 0
                 AND (expires_at IS NULL OR expires_at > NOW())""",
            key_hash,
        )
    if row is None:
        return None
    return dict(row)


async def find_write_key_by_hash(key_hash: str) -> dict | None:
    """查找有效的知识写入 Key 记录（platform.mcp_write_keys，未撤销/未停用/未过期/未删除）。

    查表 hash 判别：写 Key 的鉴权依据是 key_hash 查表，
    不依赖明文前缀。状态过滤（revoked_at/disabled_at/expires_at/is_deleted）
    通过 WHERE 子句隐式完成。grants 为 JSONB，asyncpg 自动反序列化为 list[dict]。

    Args:
        key_hash: 写 Key 的 HMAC-SHA256 hash（64 字符 hex）

    Returns:
        dict: {'id', 'tenant_id', 'grants', 'space_id', 'kb_id'} 或 None
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, tenant_id, grants, space_id, kb_id
               FROM platform.mcp_write_keys
               WHERE key_hash = $1
                 AND revoked_at IS NULL
                 AND disabled_at IS NULL
                 AND is_deleted = 0
                 AND (expires_at IS NULL OR expires_at > NOW())""",
            key_hash,
        )
    if row is None:
        return None
    return dict(row)


async def update_last_used(key_id: str, ip: str) -> None:
    """更新 MCP Key 最近使用时间和 IP（fire-and-forget）。

    使用 PostgreSQL NOW() 函数和参数化查询 $1/$2。

    Args:
        key_id: MCP Key 的 ID
        ip: 客户端 IP 地址（来自 scope['client'][0]）
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE platform.mcp_keys SET last_used_at = NOW(), last_used_ip = $1 WHERE id = $2",
            ip,
            key_id,
        )


# ============================================================================
# Block 4: ASGI 中间件 McpAuthMiddleware
# ============================================================================

# 禁止的默认租户（与 jonex_core/common/tenant.py 保持一致）
_DEFAULT_TENANT_IDS = frozenset({"", "default", "default_tenant", "system"})


class McpAuthMiddleware:
    """MCP Key 鉴权中间件 — 纯 ASGI（不使用 BaseHTTPMiddleware）。

    10 步校验流程：
    1. 放行非 HTTP scope（lifespan）
    2. 放行 /health 白名单
    3. 提取 Bearer token（兼容 X-MCP-Key header）
    4. 格式校验（yxm_ 前缀 + 长度检查）
    5. hash
    6. DB 查表
    7. revoked_at 检查（SQL WHERE 隐式完成）
    8. tenant 状态校验
    9. permissions 解析 + allowed_kb_ids
    10. contextvar 注入 + fire-and-forget last_used 更新
    11. try/finally 清理 contextvar
    12. 异常处理：catch McpAuthError → 401 JSON 响应
    """

    HEALTH_PATH = "/health"
    BEARER_PREFIX = "Bearer "
    KEY_PREFIX = "yxm_"
    MIN_KEY_LENGTH = 40
    MAX_KEY_LENGTH = 80

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Step 1: 放行非 HTTP scope（lifespan 等）
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Step 2: 放行 /health 白名单
        if scope["path"] == self.HEALTH_PATH:
            await self.app(scope, receive, send)
            return

        try:
            # Steps 3-10: 鉴权主流程
            raw_key = self._extract_bearer_token(scope)  # Step 3
            self._validate_key_format(raw_key)            # Step 4
            key_hash = hash_mcp_key(raw_key)               # Step 5

            # Step 6-7: 查表 hash 判别（先写 Key 表 → 再普通 Key 表）
            write_record = await find_write_key_by_hash(key_hash)
            if write_record is not None:
                self._validate_tenant(write_record["tenant_id"])
                grants = write_record["grants"] or []
                ctx = McpAuthContext(
                    key_id=write_record["id"],
                    tenant_id=write_record["tenant_id"],
                    key_type="write",
                    permissions=["write"],
                    allowed_kb_ids=[g["kb"] for g in grants if g.get("kb")],
                    grants=grants,
                    space_id=write_record.get("space_id"),
                )
            else:
                record = await find_by_key_hash(key_hash)
                if record is None:
                    raise McpAuthError(401, "认证失败")

                self._validate_tenant(record["tenant_id"])     # Step 8
                permissions = self._parse_permissions(record["permissions"])  # Step 9
                allowed_kb_ids = record["allowed_kb_ids"] or []  # NULL defense

                ctx = McpAuthContext(
                    key_id=record["id"],
                    tenant_id=record["tenant_id"],
                    key_type="service",
                    permissions=permissions,
                    allowed_kb_ids=allowed_kb_ids,
                    space_id=record.get("space_id"),
                )

            # Step 10: fire-and-forget last_used 更新（写 Key 无 last_used 列 → 跳过）
            ip = self._extract_client_ip(scope)
            asyncio.create_task(_update_last_used_fire(ctx.key_id, ctx.key_type, ip))

            # Step 11: contextvar 注入 → try/finally 清理（set/reset 严格配对）
            token = McpAuthContext.set(ctx)
            try:
                await self.app(scope, receive, send)
            finally:
                McpAuthContext.reset(token)

        except McpAuthError as e:
            # Step 12: 异常处理 → 401 JSON 响应
            await self._send_error_response(send, e)
        except Exception as e:
            # McpToolError（参数校验、Gateway 故障等）→ re-raise 让 FastMCP 处理为 MCP JSON-RPC 错误
            from tools import McpToolError
            if isinstance(e, McpToolError):
                raise
            # 非鉴权异常（DB 连接失败、pool 未初始化等）→ 503
            # 确保 contextvar 已清理（防御性编程，set/reset 在 try/finally 中已配对）
            try:
                McpAuthContext.clear()
            except Exception:
                pass  # best-effort 清理，不掩盖原始异常
            import json
            logger.exception("Auth middleware unexpected error")
            body = json.dumps({"code": 503, "message": "服务暂时不可用"}).encode("utf-8")
            await send({
                "type": "http.response.start",
                "status": 503,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            })
            await send({"type": "http.response.body", "body": body})

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _extract_bearer_token(self, scope: Scope) -> str:
        """从 scope['headers'] 提取 Bearer token。

        优先读 Authorization: Bearer ...，兼容 X-MCP-Key header。
        """
        headers = dict(scope.get("headers", []))

        # 优先 Authorization header
        auth_bytes = headers.get(b"authorization") or headers.get(b"Authorization")
        if auth_bytes:
            auth_str = auth_bytes.decode("utf-8", errors="replace")
            if auth_str.startswith(self.BEARER_PREFIX):
                return auth_str[len(self.BEARER_PREFIX):]

        # 备选 x-mcp-key header
        mcp_key_bytes = headers.get(b"x-mcp-key") or headers.get(b"X-MCP-Key")
        if mcp_key_bytes:
            return mcp_key_bytes.decode("utf-8", errors="replace")

        raise McpAuthError(401, "认证失败")

    def _validate_key_format(self, raw_key: str) -> None:
        """格式校验：yxm_ 前缀 + 长度范围检查。"""
        if not raw_key.startswith(self.KEY_PREFIX):
            raise McpAuthError(401, "认证失败")
        if len(raw_key) < self.MIN_KEY_LENGTH or len(raw_key) > self.MAX_KEY_LENGTH:
            raise McpAuthError(401, "认证失败")

    def _validate_tenant(self, tenant_id: str) -> None:
        """租户状态校验：非空且非默认租户。"""
        if not tenant_id or not tenant_id.strip():
            raise McpAuthError(401, "认证失败")
        if tenant_id.strip() in _DEFAULT_TENANT_IDS:
            raise McpAuthError(401, "认证失败")

    def _parse_permissions(self, permissions_str: str) -> list[str]:
        """解析逗号分隔的 permissions 字符串为 list，并归一化遗留值。

        permissions 列是 VARCHAR(512)，如 'read' 或 'read,write'。
        遗留值归一化（与 platform 侧 _normalize_key_permissions 一致）：
          - read → view（旧 read 语义 = 仅查看）
          - write / * → call（含调用能力，降级不丢失）
        去重后返回；未知值原样保留（由 require_mcp_auth 层级判定安全默认拒绝）。
        """
        parts = [p.strip() for p in permissions_str.split(",") if p.strip()]
        normalized: list[str] = []
        for p in parts:
            if p == "read":
                p = "view"
            elif p in ("write", "*"):
                p = "call"
            if p not in normalized:
                normalized.append(p)
        return normalized

    def _extract_client_ip(self, scope: Scope) -> str:
        """从 scope['client'] 提取客户端 IP 地址。"""
        client = scope.get("client")
        if client and isinstance(client, tuple) and len(client) >= 1:
            return str(client[0])
        return "unknown"

    @staticmethod
    async def _send_error_response(send: Send, error: McpAuthError) -> None:
        """发送 401 JSON 响应。

        所有鉴权失败返回同一消息（防枚举）。
        不返回 WWW-Authenticate header（API Key 非标准 HTTP auth scheme）。
        """
        import json
        body = json.dumps(error.to_dict()).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        })
        await send({
            "type": "http.response.body",
            "body": body,
        })


# ============================================================================
# Block 5: 守卫函数
# ============================================================================

def require_mcp_auth(permission: str = "view") -> McpAuthContext:
    """从 contextvar 取鉴权上下文，为空或权限不足则抛出 McpAuthError。

    供 tool handler 调用。

    Args:
        permission: 所需权限（默认 "view"）。合法值 view/call/write，其中 call 蕴含 view；支持通配符 "*"。

    Returns:
        McpAuthContext: 当前请求的鉴权上下文

    Raises:
        McpAuthError(401, "需要认证"): contextvar 为空，未提供有效认证
        McpAuthError(401, "权限不足"): 当前 Key 不具备所需权限
    """
    ctx = McpAuthContext.get()
    if ctx is None:
        raise McpAuthError(401, "需要认证")
    if permission == "write":
        # 写授权只看 key_type——普通 Key 即使残留 "write" 字符串也不放行
        if ctx.key_type != "write":
            raise McpAuthError(401, "权限不足")
        return ctx
    # call 蕴含 view：permissions=["call"] 的 Key 可查看
    if permission == "view" and "call" in ctx.permissions:
        return ctx
    if permission not in ctx.permissions and "*" not in ctx.permissions:
        raise McpAuthError(401, "权限不足")
    return ctx


def require_kb_scope(auth: McpAuthContext, knowledge_base_id: str) -> None:
    """校验 knowledge_base_id 在授权范围内。

    allowed_kb_ids 非空时校验 knowledge_base_id 在列表中。
    allowed_kb_ids 为空列表 = 该租户下全部知识库——不阻断。

    Args:
        auth: 当前请求的鉴权上下文
        knowledge_base_id: 用户请求访问的知识库 ID

    Raises:
        McpAuthError(401): 不在授权范围内
    """
    if auth.allowed_kb_ids and knowledge_base_id not in auth.allowed_kb_ids:
        raise McpAuthError(401, "不在授权范围内")


def require_write_scope(
    auth: McpAuthContext,
    knowledge_base_id: str,
    directory_id: str | None = None,
) -> None:
    """校验写入范围（grants 级）——仅供写 Key 使用。

    写 Key 无 mcp_key_service_mappings，写入范围由 grants 表达：
      - mode=="all"：该 kb 全目录放行；
      - mode=="specified"：directory_id 命中 directories 放行；
      - kb 不在 grants 中：拒绝。

    Args:
        auth: 当前请求的鉴权上下文（调用方 require_mcp_auth("write") 已保证 key_type=="write"）
        knowledge_base_id: 目标知识库 ID
        directory_id: 目标目录 ID（specified 模式必须提供）

    Raises:
        McpAuthError(401): 不在写入授权范围内
    """
    for g in auth.grants:
        if g.get("kb") == knowledge_base_id:
            if g.get("mode") == "all":
                return
            if directory_id is not None and directory_id in (g.get("directories") or []):
                return
    raise McpAuthError(401, "不在写入授权范围内")


# ============================================================================
# Fire-and-forget helper
# ============================================================================

async def _update_last_used_fire(key_id: str, key_type: str, ip: str) -> None:
    """Fire-and-forget 更新 last_used。

    按 key_type 分流：写 Key 无 last_used 列，直接跳过。
    写 DB 失败静默丢弃，不阻塞鉴权主路径。
    """
    if key_type == "write":
        return
    try:
        await update_last_used(key_id, ip)
    except Exception:
        logging.getLogger("mcp.auth").debug(
            "Failed to update last_used for key_id=%s", key_id, exc_info=True
        )


__all__ = [
    "McpAuthContext",
    "McpAuthError",
    "McpAuthMiddleware",
    "find_by_key_hash",
    "find_write_key_by_hash",
    "update_last_used",
    "require_mcp_auth",
    "require_kb_scope",
    "require_write_scope",
]
