#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""MCP Server - MCP Key 鉴权中间件。

单一文件包含 5 个逻辑块：
  1. McpAuthContext dataclass + contextvar
  2. McpAuthError 异常类
  3. 函数式 Repository：find_by_key_hash / find_service_permissions / update_last_used
  4. ASGI 中间件 McpAuthMiddleware
  5. 守卫函数：require_mcp_auth / require_kb_scope / require_write_scope

纯 ASGI 中间件（不使用 BaseHTTPMiddleware——与 Streamable HTTP 不兼容）。

统一 Key 体系：鉴权单查 platform.mcp_keys（含 write_grants 列），服务级权限由
platform.mcp_key_service_mappings 预载进 ctx.service_permissions。
"""
import asyncio
import logging
import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Optional

from starlette.types import ASGIApp, Receive, Scope, Send

from crypto import candidate_hashes
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
    allowed_kb_ids: list[str] = field(default_factory=list)
    # 统一 Key 知识写入授权（三态语义：None/[]/非空，此处已归一化为 [] 表示无写入）
    write_grants: list[dict] = field(default_factory=list)
    # service_id → permission_level（call|view），由中间件预载自 mcp_key_service_mappings
    service_permissions: dict[str, str] = field(default_factory=dict)
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
    """查找有效的统一 MCP Key 记录（Key 与所属租户均处于 active 状态）。

    「Key 有效」现在包含两层含义，均由 SQL 隐式判定：
      1. Key 自身 active：未撤销/未停用/未过期/未删除
         （revoked_at / disabled_at / expires_at / is_deleted）；
      2. Key 所属租户 active：``platform.tenants`` 满足 ``is_deleted = 0 AND status = 1``。
         与平台登录链路（TenantRepository.list_active_by_ids）口径一致——租户被软删
         或停用后，其存量 Key 不能再访问残留 KB 数据。

    租户状态不做额外区分：任何一条不满足都返回 None，由调用方统一抛 401
    「认证失败」，不泄露租户是删除了还是停用了。

    使用参数化查询 $1；allowed_kb_ids / write_grants 为 JSONB，asyncpg 自动
    反序列化为 list。

    调用方应遍历 ``crypto.candidate_hashes()`` 的结果逐个查询，以放行盐轮换
    之前签发的存量 Key（legacy 兜底）；只查当前盐会让存量 Key 全部 401。

    Args:
        key_hash: MCP Key 的 HMAC-SHA256 hash（64 字符 hex）

    Returns:
        dict: {'id', 'tenant_id', 'allowed_kb_ids', 'space_id', 'write_grants'} 或 None
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT k.id, k.tenant_id, k.allowed_kb_ids, k.space_id, k.write_grants
               FROM platform.mcp_keys k
               JOIN platform.tenants t
                 ON t.id = k.tenant_id
                AND t.is_deleted = 0
                AND t.status = 1
               WHERE k.key_hash = $1
                 AND k.revoked_at IS NULL
                 AND k.disabled_at IS NULL
                 AND k.is_deleted = 0
                 AND (k.expires_at IS NULL OR k.expires_at > NOW())""",
            key_hash,
        )
    if row is None:
        return None
    return dict(row)


async def find_service_permissions(key_id: str) -> dict[str, str]:
    """预载 Key 的服务级权限映射（service_id → permission_level）。

    统一 Key 体系下服务级权限唯一真源是 mcp_key_service_mappings 中间表。
    鉴权中间件一次查出全部映射，供守卫函数按 service_id 判定 call/view。

    Args:
        key_id: MCP Key 的 ID

    Returns:
        dict[str, str]: {service_id: permission_level}，无映射返回空 dict
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT service_id, permission_level
               FROM platform.mcp_key_service_mappings
               WHERE mcp_key_id = $1""",
            key_id,
        )
    return {row["service_id"]: row["permission_level"] for row in rows}


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
    5. 计算候选哈希（当前盐优先，MCP_KEY_SECRET_LEGACY 兜底）
    6. 按候选哈希逐个单查 mcp_keys（统一 Key），任一命中即可
    7. 状态过滤（Key 的 revoked/disabled/expired/deleted + 租户的 is_deleted/status，
       SQL WHERE/JOIN 隐式完成）
    8. tenant 格式校验（空值/默认租户；租户 active 已在 Step 6 的 JOIN 中过滤）
    9. 预载 service_permissions + 组装 ctx
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

            # Step 5-7: 逐个候选哈希查 mcp_keys（当前盐优先，LEGACY 兜底），任一命中即可。
            # 鉴权是「按 key_hash 查表」而非「取出哈希再比对」，双盐兜底必须落在查询侧——
            # 只调 verify_mcp_key() 兜不住存量 Key（它没有生产调用点）。
            record = None
            for key_hash in candidate_hashes(raw_key):     # Step 5
                record = await find_by_key_hash(key_hash)  # Step 6
                if record is not None:
                    break
            if record is None:
                raise McpAuthError(401, "认证失败")

            self._validate_tenant(record["tenant_id"])     # Step 8

            # Step 9: 预载服务级权限 + 组装 ctx
            service_permissions = await find_service_permissions(record["id"])
            ctx = McpAuthContext(
                key_id=record["id"],
                tenant_id=record["tenant_id"],
                allowed_kb_ids=record["allowed_kb_ids"] or [],
                write_grants=record.get("write_grants") or [],
                service_permissions=service_permissions,
                space_id=record.get("space_id"),
            )

            # Step 10: fire-and-forget last_used 更新（统一 Key 均落在 mcp_keys）
            ip = self._extract_client_ip(scope)
            asyncio.create_task(_update_last_used_fire(ctx.key_id, ip))

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
            # McpToolError 正常不会冒到中间件：工具入口装饰器 ``_inject_mcp_tool``
            # 已在 tool 调用内把它翻译成 ``CallToolResult(isError=True)``，码值放在
            # ``structuredContent = {code, message, data}``，**不产生 JSON-RPC error
            # 对象**（MCP 规范里 tool 执行错误走 isError，JSON-RPC error 留给协议层）。
            # 这里保留 re-raise 只是防御性透传（未来若有路径绕过装饰器），不要据此
            # 认为 FastMCP 会把码值还原——它只会包成 ``ToolError`` 文本。
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
        """租户格式校验：非空且非默认租户（纵深防御）。

        真正的租户 active 判定（is_deleted=0 AND status=1）已在
        ``find_by_key_hash`` 的 JOIN 中完成；此处只兜住 Key 记录本身
        携带空值/默认租户的异常数据。
        """
        if not tenant_id or not tenant_id.strip():
            raise McpAuthError(401, "认证失败")
        if tenant_id.strip() in _DEFAULT_TENANT_IDS:
            raise McpAuthError(401, "认证失败")

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

    统一 Key 体系下的判定语义：
      - "write"：看 write_grants 是否非空（写授权唯一真源）；
      - "view"/"call"：看 service_permissions 是否有任一服务授权（每服务 call/view
        粒度由 _check_service_scope 按 service_id 判定）。

    Args:
        permission: 所需权限（默认 "view"）。合法值 view/call/write。

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
        # 写授权只看 write_grants 非空——统一后无独立写 Key 类型
        if not ctx.write_grants:
            raise McpAuthError(401, "权限不足")
        return ctx
    # view/call：有任一服务级授权即放行（无 service_id 工具如 list_domain_services）
    # 纯写 Key（write_grants 非空但 service_permissions 空）在此被拒
    if not ctx.service_permissions:
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
    """校验写入范围（write_grants 级）——仅供写授权 Key 使用。

    写入范围由 write_grants 表达：
      - mode=="all"：该 kb 全目录放行；
      - mode=="specified"：directory_id 命中 directories 放行；
      - kb 不在 write_grants 中：拒绝。

    Args:
        auth: 当前请求的鉴权上下文（调用方 require_mcp_auth("write") 已保证 write_grants 非空）
        knowledge_base_id: 目标知识库 ID
        directory_id: 目标目录 ID（specified 模式必须提供）

    Raises:
        McpAuthError(401): 不在写入授权范围内
    """
    # Key 级 KB 白名单（allowed_kb_ids）是总上界：白名单非空时，即使
    # write_grants 里有该库，未列进白名单也一律拒绝。空列表 = 不限制，
    # 与读路径 ``_apply_kb_whitelist`` 的口径一致。
    if auth.allowed_kb_ids and knowledge_base_id not in auth.allowed_kb_ids:
        raise McpAuthError(401, "不在写入授权范围内")
    for g in auth.write_grants:
        if g.get("kb") == knowledge_base_id:
            if g.get("mode") == "all":
                return
            if directory_id is not None and directory_id in (g.get("directories") or []):
                return
    raise McpAuthError(401, "不在写入授权范围内")


# ============================================================================
# Fire-and-forget helper
# ============================================================================

# 系统写服务固定 service_id / tool 名（与 capabilities/platform/repository
# /mcp_service_repository.py 的 SYSTEM_SERVICE_ID / SYSTEM_SERVICE_TOOL 保持一致）
SYSTEM_SERVICE_ID = "system.knowledge_document_write"
SYSTEM_SERVICE_TOOL = "knowledge_document_write"


async def touch_service_call(tenant_id: str, service_id: str) -> None:
    """更新服务目录「最近调用时间」（服务级）。

    领域服务只 UPDATE、不 INSERT：调用能走到这里说明 ``_check_service_scope``
    已放行，发布行必然存在。若此前用 UPSERT 并插入 ``is_published=0``，而该列
    现在也参与可用性判定，就会把「首次调用成功 → 插入未发布行 → 第二次被拒」
    锁死自己。

    系统写服务仍 UPSERT：它的发布行由 platform 侧懒 seed，目录页若从未打开过
    就还没有行；插入时固定 ``is_published=1`` / ``service_type='system'``。
    冲突时一律只刷新 last_call_at，不覆盖发布状态与 tool 配置。
    """
    if not service_id:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        if service_id == SYSTEM_SERVICE_ID:
            await conn.execute(
                """
                INSERT INTO platform.mcp_service_publish
                    (id, tenant_id, service_id, is_published, service_type, tool,
                     last_call_at, created_at)
                VALUES ($1, $2, $3, 1, 'system', $4, NOW(), NOW())
                ON CONFLICT (tenant_id, service_id)
                DO UPDATE SET last_call_at = NOW()
                """,
                uuid.uuid4().hex,
                tenant_id,
                service_id,
                SYSTEM_SERVICE_TOOL,
            )
            return
        result = await conn.execute(
            """
            UPDATE platform.mcp_service_publish
               SET last_call_at = NOW()
             WHERE tenant_id = $1
               AND service_id = $2
            """,
            tenant_id,
            service_id,
        )
        # 影响 0 行说明「服务被成功调用却没有发布记录」——数据面异常（发布行
        # 丢失、迁移遗漏、绕过发布直连调用）。打点不补假数据，只把它暴露出来。
        if result == "UPDATE 0":
            logger.warning(
                "Touch skipped: no publish row for service_id=%s tenant=%s",
                service_id,
                tenant_id,
            )


# 服务不可调用原因（``service_block_reason`` 的返回值；None 表示可调用）
SERVICE_BLOCK_STOPPED = "stopped"
SERVICE_BLOCK_UNPUBLISHED = "unpublished"
# 领域服务自身停用（``knowledge_base.services`` 的 status/enabled）
SERVICE_BLOCK_DISABLED = "disabled"


def _service_row_disabled(row) -> bool:
    """``knowledge_base.services`` 行是否处于停用态。

    停用口径：``status != 'active'`` 或 ``COALESCE(enabled, 1) = 0``。
    ``enabled`` 可空，NULL 按默认启用处理（``enabled is not None and == 0``
    才算停用）；``status`` 为 NULL 不等于 ``'active'``，按异常数据判停用。
    """
    if row["status"] != "active":
        return True
    enabled = row["enabled"]
    return enabled is not None and enabled == 0


async def service_block_reason(tenant_id: str, service_id: str) -> str | None:
    """返回服务不可调用的原因；可调用时返回 ``None``。

    判定分两层，**先查领域服务自身状态、再查 MCP 发布表**：

    ① ``knowledge_base.services``（领域服务是否还活着）
      - 查不到行（服务被删/租户不符/软删）→ ``"disabled"``。服务被删不该
        退化成「无主可查」：授权表里残留的映射不能让已删除服务继续响应。
      - ``status != 'active'`` 或 ``COALESCE(enabled, 1) = 0`` → ``"disabled"``。
        领域服务停用/禁用不会联动发布表（实测：``status=inactive``
        后发布行 ``stopped_at`` 仍为空，已授权 Key 仍能检索），所以必须在这里
        独立判定。

    ② ``platform.mcp_service_publish``（可调用条件与平台侧
      ``_derive_publish_status`` 的 published 一致：``is_published=1`` 且
      ``stopped_at`` 为空）
      - ``stopped_at`` 非空 → ``"stopped"``（运营点「停用」）；
      - 有发布行且 ``is_published=0`` → ``"unpublished"``（运营点「取消发布」，
        该动作会把服务从 MCP 目录下架，已授权 Key 也必须同步失效）；
      - 无发布行 → ``None``。从未发布过的服务不会有 Key 授权（授权时校验
        已发布），无记录更可能是目录页从未打开、stub 未补，属数据面问题；
        此处放行避免异常数据把全部工具打成不可用。

    放行范围：系统写服务 → ``None``（发布表里可能没有行，语义上恒为 published）。

    失败口径（软治理，不 fail-closed）：
      - 查询异常（连接池未初始化、表缺失、瞬时抖动）→ 记 warning 并**跳过该层
        的判定**，继续走下一层；两层都异常时最终 ``None``（放行）。鉴权本身已
        依赖同一数据库，DB 不可用时请求根本走不到这里；若在此 fail-closed，
        一次抖动会让全部工具不可用。
      - 唯一的 fail-closed 是「``services`` 表能查通但查不到该服务行」——那是
        确定性结论（服务不存在），不是查询失败。
    """
    if not service_id or service_id == SYSTEM_SERVICE_ID:
        return None
    try:
        pool = await get_pool()
    except Exception:
        logger.warning(
            "Failed to check availability for service_id=%s, allow by default",
            service_id,
            exc_info=True,
        )
        return None

    # ① 领域服务自身状态。查询异常 → 跳过本层，继续查发布表（fail-open）。
    try:
        async with pool.acquire() as conn:
            service_row = await conn.fetchrow(
                """
                SELECT status, enabled
                  FROM knowledge_base.services
                 WHERE tenant_id = $1
                   AND id = $2
                   AND is_deleted = 0
                 LIMIT 1
                """,
                tenant_id,
                service_id,
            )
    except Exception:
        logger.warning(
            "Failed to check domain service status for service_id=%s, "
            "fall back to publish table",
            service_id,
            exc_info=True,
        )
    else:
        if service_row is None or _service_row_disabled(service_row):
            return SERVICE_BLOCK_DISABLED

    # ② MCP 发布表。查询异常 → 放行（与既有口径一致）。
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT is_published, stopped_at
                  FROM platform.mcp_service_publish
                 WHERE tenant_id = $1
                   AND service_id = $2
                 LIMIT 1
                """,
                tenant_id,
                service_id,
            )
    except Exception:
        logger.warning(
            "Failed to check publish status for service_id=%s, allow by default",
            service_id,
            exc_info=True,
        )
        return None
    if row is None:
        return None
    if row["stopped_at"] is not None:
        return SERVICE_BLOCK_STOPPED
    if not row["is_published"]:
        return SERVICE_BLOCK_UNPUBLISHED
    return None


async def block_reasons_for_services(
    tenant_id: str, service_ids: list[str]
) -> dict[str, str]:
    """批量版 ``service_block_reason``：一次查出多个服务的不可用原因。

    返回 ``{service_id: "disabled" | "stopped" | "unpublished"}``，**只含不可用
    服务**；可用服务、无发布行、系统写服务都不出现在结果里。投放目录时据此
    隐藏，避免「列得出来但调不通」——领域服务停用/禁用、运营停用与取消发布
    都隐藏。

    判定顺序与单条实现一致：先批量查 ``knowledge_base.services``（无行或
    status/enabled 停用 → ``"disabled"``），再叠加发布表结果（``"disabled"``
    优先，不被他层覆盖）。

    失败口径同样按层 fail-open：某层查询异常只跳过该层，已确定的另一层结论
    保留；两层都异常时返回空 dict（全部可用）。
    """
    ids = [sid for sid in service_ids if sid and sid != SYSTEM_SERVICE_ID]
    if not ids:
        return {}
    try:
        pool = await get_pool()
    except Exception:
        logger.warning(
            "Failed to batch check availability for tenant=%s, allow by default",
            tenant_id,
            exc_info=True,
        )
        return {}

    blocked: dict[str, str] = {}

    # ① 领域服务自身状态。查询异常 → 跳过本层。
    try:
        async with pool.acquire() as conn:
            service_rows = await conn.fetch(
                """
                SELECT id, status, enabled
                  FROM knowledge_base.services
                 WHERE tenant_id = $1
                   AND id = ANY($2::text[])
                   AND is_deleted = 0
                """,
                tenant_id,
                ids,
            )
    except Exception:
        logger.warning(
            "Failed to batch check domain service status for tenant=%s, "
            "fall back to publish table",
            tenant_id,
            exc_info=True,
        )
    else:
        found = {row["id"]: row for row in service_rows}
        for sid in ids:
            row = found.get(sid)
            if row is None or _service_row_disabled(row):
                blocked[sid] = SERVICE_BLOCK_DISABLED

    # ② MCP 发布表。查询异常 → 跳过本层（已确定的服务停用结论仍保留）。
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT service_id, is_published, stopped_at
                  FROM platform.mcp_service_publish
                 WHERE tenant_id = $1
                   AND service_id = ANY($2::text[])
                """,
                tenant_id,
                ids,
            )
    except Exception:
        logger.warning(
            "Failed to batch check publish status for tenant=%s, "
            "keep domain-service verdicts only",
            tenant_id,
            exc_info=True,
        )
        return blocked
    for row in rows:
        if row["stopped_at"] is not None:
            blocked.setdefault(row["service_id"], SERVICE_BLOCK_STOPPED)
        elif not row["is_published"]:
            blocked.setdefault(row["service_id"], SERVICE_BLOCK_UNPUBLISHED)
    return blocked


def touch_service_call_async(tenant_id: str, service_id: str | None) -> None:
    """不阻塞工具主路径地记录服务调用时间（无 service_id 时静默跳过）。"""
    if not service_id:
        return
    asyncio.create_task(_touch_service_call_fire(tenant_id, service_id))


async def _touch_service_call_fire(tenant_id: str, service_id: str) -> None:
    """Fire-and-forget 写 last_call_at；失败静默，不影响工具调用结果。"""
    try:
        await touch_service_call(tenant_id, service_id)
    except Exception:
        logging.getLogger("mcp.auth").debug(
            "Failed to touch last_call_at for service_id=%s", service_id, exc_info=True
        )


async def _update_last_used_fire(key_id: str, ip: str) -> None:
    """Fire-and-forget 更新 last_used。

    统一 Key 全部落在 mcp_keys（含 last_used_at/last_used_ip 列），无需按来源表分流。
    写 DB 失败静默丢弃，不阻塞鉴权主路径。
    """
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
    "find_service_permissions",
    "service_block_reason",
    "block_reasons_for_services",
    "SERVICE_BLOCK_STOPPED",
    "SERVICE_BLOCK_UNPUBLISHED",
    "SERVICE_BLOCK_DISABLED",
    "update_last_used",
    "touch_service_call",
    "touch_service_call_async",
    "SYSTEM_SERVICE_ID",
    "SYSTEM_SERVICE_TOOL",
    "require_mcp_auth",
    "require_kb_scope",
    "require_write_scope",
]
