#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""MCP Server - Tool 处理器 + Gateway HTTP 客户端。

单一文件包含 4 个逻辑块：
  1. HTTP 客户端 getter（参照 db._pool 模式）
  2. call_gateway() — POST Gateway /internal/kb/invoke
  3. tool handler 函数（由 app.py 通过 @mcp.tool() 注册）
  4. __all__ 导出

约束：
  - 零 import jonex_core——所有 KB 交互通过 HTTP 调 Gateway
  - Tool handler 签名不含 request、tenant_id、mcp_key_id——从 contextvar 获取
"""
import logging
import os
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from auth import (
    McpAuthContext,
    McpAuthError,
    require_kb_scope,
    require_mcp_auth,
    require_write_scope,
)
from config import settings

logger = logging.getLogger("mcp.tools")


# ============================================================================
# Block 0: 异常类
# ============================================================================


class McpToolError(Exception):
    """MCP Tool 执行异常——参数校验、上游服务、限流等非鉴权错误。

    与 McpAuthError 语义严格分离：
      - McpAuthError: 仅鉴权/授权失败（auth.py 内部，被中间件捕获返回 401）
      - McpToolError: tool 执行层面的所有非鉴权错误（参数校验、Gateway 故障、限流等）

    code 使用 JSON-RPC error code（MCP 协议标准）。
    data 用于传递结构化错误上下文（如 retry_after 秒数）。
    """

    def __init__(self, code: int, message: str, data: dict | None = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(message)


# ============================================================================
# Block 1: HTTP 客户端 getter（参照 db._pool 模式）
# ============================================================================

_http_client: httpx.AsyncClient | None = None


async def get_http_client() -> httpx.AsyncClient:
    """获取已初始化的 httpx 客户端（lifespan 中由 app.py 注入）。

    参照 db.get_pool() 模式：模块级变量 + None 校验守卫。

    Returns:
        httpx.AsyncClient: 共享的异步 HTTP 客户端

    Raises:
        RuntimeError: 客户端未初始化（lifespan 尚未执行或已关闭）
    """
    if _http_client is None:
        raise RuntimeError("HTTP client not initialized")
    return _http_client


def set_http_client(client: httpx.AsyncClient | None) -> None:
    """设置模块级 HTTP 客户端（由 app.py lifespan 调用）。"""
    global _http_client
    _http_client = client


# ============================================================================
# Block 2: call_gateway() — POST Gateway /internal/kb/invoke
# ============================================================================

async def call_gateway(
    action: str,
    tenant_id: str,
    mcp_key_id: str,
    data: dict | None = None,
) -> dict:
    """向 Gateway /internal/kb/invoke 发送请求，返回 capability 响应 data 部分。

    异常统一转换为 McpToolError(code=-32000, message="知识库服务暂不可用")，
    消息脱敏，不暴露 Gateway URL、堆栈、内部错误详情。

    Args:
        action: capability action 名称（list_knowledge_info/search/get_raw_url）
        tenant_id: 租户 ID（从 McpAuthContext 获取）
        mcp_key_id: MCP Key ID（从 McpAuthContext 获取，用于审计）
        data: action 所需参数（可选）

    Returns:
        dict: capability 响应数据

    Raises:
        McpToolError(code=-32000): 知识库服务不可用（HTTP 错误 / 超时 / 传输错误 / 业务失败）
    """
    client = await get_http_client()
    try:
        resp = await client.post(
            "/internal/kb/invoke",
            json={
                "action": action,
                "tenant_id": tenant_id,
                "mcp_key_id": mcp_key_id,
                "data": data or {},
            },
            headers={
                "X-Internal-API-Key": settings.INTERNAL_API_KEY,
                "X-Tenant-ID": tenant_id,
            },
        )
        resp.raise_for_status()
        result = resp.json()
    except httpx.HTTPStatusError as e:
        # SEC-03: Sidecar 429 限流 → MCP JSON-RPC error（含 retry_after）
        if e.response.status_code == 429:
            retry_after_raw = e.response.headers.get("Retry-After", "60")
            try:
                retry_after = int(retry_after_raw)
            except (ValueError, TypeError):
                # HTTP-date 格式: "Thu, 01 Jan 2026 00:00:00 GMT"
                try:
                    retry_dt = parsedate_to_datetime(retry_after_raw).replace(
                        tzinfo=timezone.utc
                    )
                    retry_after = max(
                        0, int((retry_dt - datetime.now(timezone.utc)).total_seconds())
                    )
                except (ValueError, TypeError):
                    retry_after = 60
            raise McpToolError(
                code=_ERROR_RATE_LIMITED,
                message=f"Rate limit exceeded. Retry after {retry_after} seconds.",
                data={"retry_after": retry_after},
            )
        if 400 <= e.response.status_code < 500:
            # 认证/授权失败（401/403）→ 不应触发管线 fallback
            if e.response.status_code in (401, 403):
                raise McpToolError(
                    code=_ERROR_AUTH,
                    message="认证失败或权限不足",
                )
            # 其他客户端错误（400/404/422）→ 参数或管线问题，允许上游 safe-fallback
            raise McpToolError(
                code=_ERROR_INVALID_PARAMS,
                message="知识库服务请求参数有误",
            )
        # 500+ → 服务端错误
        logger.error(
            "Gateway HTTP error: action=%s tenant_id=%s status=%d",
            action,
            tenant_id,
            e.response.status_code,
        )
        raise McpToolError(code=_ERROR_INTERNAL, message="知识库服务暂不可用")
    except (httpx.TimeoutException, httpx.TransportError) as e:
        logger.error(
            "Gateway transport error: action=%s tenant_id=%s type=%s",
            action,
            tenant_id,
            type(e).__name__,
        )
        raise McpToolError(code=_ERROR_INTERNAL, message="知识库服务暂不可用")
    except httpx.RequestError as e:
        # Catch-all for any httpx errors not covered above
        logger.error(
            "Gateway request error: action=%s tenant_id=%s type=%s",
            action,
            tenant_id,
            type(e).__name__,
        )
        raise McpToolError(code=_ERROR_INTERNAL, message="知识库服务暂不可用")

    # 业务失败：响应 code 非 0
    if isinstance(result, dict) and result.get("code", 0) != 0:
        biz_code = result.get("code")
        logger.error(
            "Gateway business error: action=%s tenant_id=%s code=%s",
            action,
            tenant_id,
            biz_code,
        )
        # 1xxx = 通用参数/管线不匹配错误，映射为 INVALID_PARAMS 以便上游安全 fallback
        mcp_code = _ERROR_INVALID_PARAMS if isinstance(biz_code, int) and 1000 <= biz_code < 2000 else _ERROR_INTERNAL
        raise McpToolError(code=mcp_code, message="知识库服务暂不可用")

    # 提取 data 字段（标准 success_response 格式），否则返回原始结果
    if isinstance(result, dict) and "data" in result:
        return result["data"]
    return result


# ============================================================================
# Block 3: 3 个 tool handler 函数 + C3 交集校验
# ============================================================================


# ── Service 级权限层级: call(1) > view(0)（write 已移交写 Key）──
_PERMISSION_LEVEL_RANK = {"view": 0, "call": 1}

# 遗留 service 映射权限值降级（write/*/read → call/view，
# 与 capabilities/platform/services/mcp_key_service.py::_normalize_key_permissions 一致）
_LEGACY_PERMISSION_LEVEL_MAP = {"write": "call", "read": "view", "*": "call"}


async def _check_service_scope(
    auth: McpAuthContext, service_id: str, required_permission: str = "call"
) -> list[str]:
    """C3: 校验 service 下的服务级权限级别。

    权限级别层级（向下兼容，收敛为 call/view 两级）：
      - call:  需 "call"（含降级后的遗留 write/*）
      - view:  "view" 或 "call" 可通过
      - 映射不存在时 raise McpAuthError(401, "不在授权范围内")

    遗留值降级迁移：write→call、read→view、*→call（写权限已移交写 Key，
    * 不再全通配）。未知值落到 rank -1 → 403（安全默认拒绝）。

    统一 Key 重构后，KB 范围不再由 allowed_kb_ids 表达——空间隔离已在授权期
    由 mcp_key_service 强制（create/update 只允许把 Key 空间内的服务写进映射），
    故此处只做服务级权限检查，恒返回 []（空 = 全部 KB 允许），调用方自行获取
    service 的 kb_ids。

    Returns:
        list[str]: 恒为 []（表示「全部 KB 允许」，调用方自行获取 service 的 kb_ids）
    """
    perm_level = auth.service_permissions.get(service_id)

    if perm_level is None:
        raise McpAuthError(401, "不在授权范围内")

    # 遗留值降级迁移：write→call、read→view、*→call（与 key 级归一化规则一致）
    perm_level = _LEGACY_PERMISSION_LEVEL_MAP.get(perm_level, perm_level)

    required_rank = _PERMISSION_LEVEL_RANK.get(required_permission, 0)
    actual_rank = _PERMISSION_LEVEL_RANK.get(perm_level, -1)
    if actual_rank < required_rank:
        _LABELS: dict[str, str] = {"call": "可调用", "view": "仅查看"}
        raise McpAuthError(
            403,
            f"需要'{_LABELS.get(required_permission, required_permission)}'权限，"
            f"当前为'{_LABELS.get(perm_level, perm_level)}'权限",
        )

    return []


def _validate_file_name(file_name: str) -> str:
    """Validate file_name for safety: non-empty, no path traversal characters.

    Args:
        file_name: The uploaded file name from MCP client (untrusted).

    Returns:
        str: Trimmed safe file name.

    Raises:
        McpToolError(code=-32602): file_name empty or contains path traversal chars.
    """
    name = file_name.strip()
    if not name:
        raise McpToolError(code=-32602, message="file_name 为必填参数，不能为空")
    if _PATH_TRAVERSAL_RE.search(name) or "/" in name or "\\" in name:
        raise McpToolError(code=-32602, message="file_name 包含非法字符（路径穿越拒绝）")
    if not _VALID_FILENAME_RE.match(name):
        raise McpToolError(code=-32602, message="file_name 包含非法字符")
    return name


def _grant_mode_for_kb(auth: McpAuthContext, kb_id: str) -> str | None:
    """返回写 Key 对指定 kb 的授权模式："all" | "specified" | None（未授权）。

    方案 1：specified 目录级授权暂不支持 MCP 上传/查状态，
    handler 据此显式拒绝，避免「能建不能用」的静默失效与目录级越权。
    """
    for g in auth.write_grants:
        if g.get("kb") == kb_id:
            return g.get("mode")
    return None


async def confirm_upload(
    service_id: str,
    file_name: str,
    knowledge_base_id: str,
    storage_key: str,
    doc_id: str = "",
    mime_type: str = "",
) -> dict:
    """Confirm a COS direct upload and trigger parsing/indexing via MCP.

    直传闭环的确认步骤（真实文件上传推荐走这条链路）：
      1) 先调 generate_upload_url 拿预签名 PUT URL + storage_key；
      2) 客户端把字节直传对象存储；
      3) 再调本工具传 storage_key 确认入库并触发解析/索引。

    原 base64 内联路径已移除——它受模型输出墙限制仅支持几 KB 级文本，
    对真实文档不适用。

    Args:
        service_id: Domain service ID（写 Key 场景保留签名兼容，不再用于定位 KB）。
        file_name: Original file name (untrusted, validated for traversal).
        knowledge_base_id: Target knowledge base ID.
        storage_key: COS storage key（generate_upload_url 返回），直传后确认入库。
        doc_id: generate_upload_url 返回的文档 ID（可选，确认入库元数据）。
        mime_type: Optional MIME type (default: "").

    Returns:
        dict: Document metadata (doc_id, file_name, knowledge_base_id, status).

    Raises:
        McpToolError(code=-32602): Parameter validation failure.
        McpAuthError(401): Not authenticated / missing "write" permission / KB not in scope.
        McpToolError(code=-32000): Gateway error / timeout / transport failure.
    """
    # Step 1 — Parameter non-empty validation
    if not service_id or not service_id.strip():
        raise McpToolError(code=-32602, message="service_id 为必填参数，不能为空")
    if not knowledge_base_id or not knowledge_base_id.strip():
        raise McpToolError(code=-32602, message="knowledge_base_id 为必填参数，不能为空")
    if not storage_key or not storage_key.strip():
        raise McpToolError(code=-32602, message="storage_key 为必填参数，不能为空")

    # Step 2 — file_name security validation
    file_name = _validate_file_name(file_name)

    # Step 3 — Auth + write scope（写 Key：grants 级校验，替代 service scope）
    # 写 Key 无 mcp_key_service_mappings，_check_service_scope 恒 401；写入范围由 grants 表达。
    auth = require_mcp_auth("write")  # 写授权看 write_grants
    kb_id = knowledge_base_id.strip()
    # 方案 1：specified 目录级写入暂不支持 MCP 上传，显式拒绝而非静默 401
    if _grant_mode_for_kb(auth, kb_id) == "specified":
        raise McpToolError(
            code=_ERROR_INVALID_PARAMS,
            message="目录级写入（specified 模式）暂不支持 MCP 上传，请改用 all 模式写 Key",
        )
    require_write_scope(auth, kb_id)  # grants 级：kb 在 grants 且 mode=all，否则 401

    # Step 4 — 确认入库（直传闭环）
    # [jonex] S3 兼容：storage_backend 跟随平台全局配置，不硬编码 cos——
    # 否则 S3 部署下上传的文档被标成 cos，后续取原文会用错客户端。
    _backend = (os.getenv("OBJECT_STORAGE_BACKEND", "local") or "local").strip().lower()
    data: dict = {
        "knowledge_base_id": kb_id,
        "storage_key": storage_key,
        "file_name": file_name,
        "storage_backend": _backend if _backend in ("cos", "s3") else "cos",
    }
    if doc_id:
        data["doc_id"] = doc_id
    if mime_type:
        data["mime_type"] = mime_type
    return await call_gateway(
        "upload_document",
        auth.tenant_id,
        auth.key_id,
        data,
    )


async def generate_upload_url(
    service_id: str,
    knowledge_base_id: str,
    file_name: str,
    content_type: str = "",
    file_size: int = 0,
) -> dict:
    """Generate a COS presigned upload URL for direct-to-storage upload.

    Returns a presigned PUT URL so MCP clients can upload large files directly
    to object storage (e.g. ``curl -X PUT --upload-file <file> <upload_url>``).
    After the bytes are uploaded, call confirm_upload to confirm and trigger
    parsing/indexing.

    Args:
        service_id: Domain service ID（写 Key 场景保留签名兼容，不再用于定位 KB）。
        knowledge_base_id: Target knowledge base ID.
        file_name: Original file name (untrusted, validated for traversal).
        content_type: Optional MIME type (default: "").
        file_size: Optional byte size to upload (default: 0 = unknown). When > 0,
            the size is validated against the server cap and signed into the
            Content-Length header, locking the exact byte count of the PUT.

    Returns:
        dict: {doc_id, storage_key, upload_url, storage_backend}.

    Raises:
        McpToolError(code=-32602): Parameter validation failure.
        McpAuthError(401): Not authenticated / missing "write" permission / KB not in scope.
        McpToolError(code=-32000): Gateway error / timeout / transport failure.
    """
    # Step 1 — Parameter non-empty validation
    if not service_id or not service_id.strip():
        raise McpToolError(code=-32602, message="service_id 为必填参数，不能为空")
    if not knowledge_base_id or not knowledge_base_id.strip():
        raise McpToolError(code=-32602, message="knowledge_base_id 为必填参数，不能为空")
    if not file_name or not file_name.strip():
        raise McpToolError(code=-32602, message="file_name 为必填参数，不能为空")
    if file_size < 0:
        raise McpToolError(code=-32602, message="file_size 不能为负数")

    # Step 2 — file_name security validation（路径穿越 / 非法字符）
    file_name = _validate_file_name(file_name)

    # Step 3 — Auth + write scope（写 Key：grants 级校验，替代 service scope）
    # 写 Key 无 mcp_key_service_mappings，_check_service_scope 恒 401；写入范围由 grants 表达。
    auth = require_mcp_auth("write")  # 写授权看 write_grants
    kb_id = knowledge_base_id.strip()
    # 方案 1：specified 目录级写入暂不支持 MCP 直传，显式拒绝而非静默 401
    if _grant_mode_for_kb(auth, kb_id) == "specified":
        raise McpToolError(
            code=_ERROR_INVALID_PARAMS,
            message="目录级写入（specified 模式）暂不支持 MCP 直传，请改用 all 模式写 Key",
        )
    require_write_scope(auth, kb_id)  # grants 级：kb 在 grants 且 mode=all，否则 401

    # Step 4 — Invoke capability generate_upload_url via Gateway
    data: dict = {
        "knowledge_base_id": kb_id,
        "file_name": file_name,
    }
    if content_type:
        data["content_type"] = content_type.strip()
    if file_size and file_size > 0:
        data["file_size"] = file_size

    return await call_gateway(
        "generate_upload_url",
        auth.tenant_id,
        auth.key_id,
        data,
    )


async def get_upload_status(service_id: str, document_id: str) -> dict:
    """Query document processing status after upload via MCP.

    Returns status, ontology_status, error_message, and other metadata
    to let MCP clients track upload progress through the async pipeline
    (PENDING → PARSING → INGESTING → READY / FAILED).

    Args:
        service_id: Domain service ID（写 Key 场景保留签名兼容，不再用于定位 KB）。
        document_id: Document ID (returned by confirm_upload).

    Returns:
        dict: Document status fields (doc_id, status, ontology_status,
              error_message, file_name, file_size, knowledge_base_id,
              created_at, updated_at).

    Raises:
        McpAuthError(401): Not authenticated / not a write key.
        McpToolError(code=-32602): Parameter validation failure.
        McpToolError(code=-32000): Gateway error / write key grants empty.
    """
    # Step 1 — Parameter non-empty validation
    if not service_id or not service_id.strip():
        raise McpToolError(code=-32602, message="service_id 为必填参数，不能为空")
    if not document_id or not document_id.strip():
        raise McpToolError(code=-32602, message="document_id 为必填参数，不能为空")

    # Step 2 — Auth + scope（写 Key 专属：上传生命周期，service_id 保留签名但不再用于定位）
    auth = require_mcp_auth("write")  # 写授权看 write_grants
    # 方案 1：specified 目录级不支持 MCP，仅 all 模式 kb 可查上传状态，
    # 剔除 specified 的 kb，避免平铺 grants[].kb 导致目录级隔离被突破（越权读他人文档状态）。
    kb_ids_to_check = [
        g["kb"] for g in auth.write_grants if g.get("mode") == "all" and g.get("kb")
    ]

    if not kb_ids_to_check:
        raise McpToolError(
            code=_ERROR_INTERNAL,
            message="写 Key 未授权可查询的知识库（specified 目录级暂不支持 MCP）",
        )

    # Step 3 — Call Gateway (returns document status or raises ResourceNotFoundError)
    return await call_gateway(
        "get_document_status",
        auth.tenant_id,
        auth.key_id,
        {
            "document_id": document_id.strip(),
            "kb_ids": kb_ids_to_check,
        },
    )


async def list_domain_services() -> dict:
    """列出 MCP Key 对应租户下所有领域服务。

    Action: list_services
    Guard: require_mcp_auth("view") + 按 Key 授权 service_permissions 过滤目录

    只返回该 Key 可访问（service_permissions 授权范围内）的服务及其库，
    避免目录枚举平铺未授权库、agent 一问即 403。

    Returns:
        dict: 含 items（领域服务列表，已按授权过滤）、total（过滤后总数）字段
    """
    auth = require_mcp_auth("view")
    data = {}
    if auth.space_id:
        data["space_id"] = auth.space_id
    result = await call_gateway(
        "list_services",
        auth.tenant_id,
        auth.key_id,
        data,
    )
    # 上游标准返回 {items: [...], total: N}
    if isinstance(result, dict) and "items" in result:
        authorized_ids = set(auth.service_permissions.keys())
        items = [
            item
            for item in result["items"]
            if isinstance(item, dict) and item.get("id") in authorized_ids
        ]
        return {"items": items, "total": len(items)}
    # 格式意外 → 记录并报错
    logger.error("list_services 返回意外格式: %s", type(result).__name__)
    raise McpToolError(code=-32000, message="领域服务列表返回格式异常")


# ── JSON-RPC 错误码常量（per JSON-RPC 2.0 spec + MCP extension） ──
_ERROR_INVALID_PARAMS = -32602  # 参数校验失败（JSON-RPC standard）
_ERROR_INTERNAL = -32000  # 内部服务错误
_ERROR_RATE_LIMITED = -32001  # 限流（自定义 MCP extension）
_ERROR_AUTH = -32002  # 认证/授权失败（401/403 → 禁止管线 fallback）

# NOTE: SEARCH_LLMWIKI_ENABLED 是进程级状态。多 worker 场景下各 worker 独立进行
# 健康检查，可能在不同时刻得出不同结论（部分 worker 启用、部分禁用）。
# 当前影响较小（仅控制一个可选 search tool）；若需严格一致性，可考虑 Redis 等共享存储。
# LLM-Wiki 检索开关（Feature Flag，默认开启）
SEARCH_LLMWIKI_ENABLED = os.getenv("SEARCH_LLMWIKI_ENABLED", "true").lower() in ("1", "true", "yes", "on")


def is_search_llmwiki_enabled() -> bool:
    """返回 search_llmwiki 是否可用（线程安全读取模块级状态）。

    该标志由 lifespan 健康检查设置，多 worker 下各 worker 独立。
    """
    return SEARCH_LLMWIKI_ENABLED


_VALID_SEARCH_MODES = frozenset({"naive", "local", "global", "hybrid", "mix"})
# mix → hybrid 别名兼容（与 capabilities/knowledge_base/dtos/search.py 保持一致）
_SEARCH_MODE_ALIASES = {"mix": "hybrid"}

# Upload document validation constants
# 合法文件名字符：字母数字下划线、中日韩汉字、点、连字符，以及括号/空格/方括号
# （半角 () [] 与全角（）均允许，路径穿越仍由 _PATH_TRAVERSAL_RE 与斜杠检查拦截）
_VALID_FILENAME_RE = re.compile(r'^[\w一-鿿.()\[\]（） -]+$')  # Safe filename characters
_PATH_TRAVERSAL_RE = re.compile(r'\.\./|\.\.\\')  # Path traversal detection


async def list_documents(
    service_id: str,
    file_name: str = "",
) -> dict:
    """列出领域服务下所有知识库的文档。以领域服务为入口，自动解析所属知识库。

    Action: list_documents
    Guard: require_mcp_auth("view") + _check_service_scope(auth, service_id, required_permission="view")

    解决「文件名 → document_id」桥接缺口：MCP 客户端无需先到 REST API 查文档列表再回来调用 read_source。
    支持可选的 file_name 关键词过滤（模糊匹配文件名和文件路径）。

    Args:
        service_id: 领域服务 ID
        file_name: 可选的文件名关键词过滤（最多 255 字符）

    Returns:
        dict: 含 items（文档列表，每项含 id、file_name、knowledge_base_id、status 等）、total 字段

    Raises:
        McpAuthError(401): 未认证 / service 不在授权范围
        McpToolError(code=-32602): 参数校验失败（空 service_id / file_name 超长）
        McpToolError(code=-32000): 知识库服务不可用 / 服务下无可用知识库
    """
    if not service_id or not service_id.strip():
        raise McpToolError(code=-32602, message="service_id 为必填参数，不能为空")
    file_name = file_name.strip()
    if len(file_name) > 255:
        raise McpToolError(code=-32602, message="file_name 长度不能超过 255 字符")
    auth = require_mcp_auth("view")
    intersection = await _check_service_scope(auth, service_id, required_permission="view")
    if intersection:
        kb_ids_to_query = intersection
    else:
        service_detail = await call_gateway(
            "get_service", auth.tenant_id, auth.key_id, {"service_id": service_id},
        )
        kb_ids_to_query = service_detail.get("kb_ids", [])
    if not kb_ids_to_query:
        raise McpToolError(code=-32000, message="服务下无可用知识库")
    all_items: list[dict] = []
    for kb_id in kb_ids_to_query:
        try:
            data: dict[str, str] = {"knowledge_base_id": kb_id}
            if file_name:
                data["keyword"] = file_name
            result = await call_gateway(
                "list_documents", auth.tenant_id, auth.key_id, data,
            )
            all_items.extend(result.get("items", []))
        except McpToolError:
            continue
    return {"items": all_items, "total": len(all_items)}


async def _validate_search_params(
    service_id: str,
    query: str,
    mode: str,
    top_k: int,
) -> tuple[str, str, "McpAuthContext", list[str]]:
    """统一的搜索参数校验 + 鉴权。

    校验顺序：mode 规范化 → service_id/query 非空 → query 长度 → mode 白名单 → top_k 范围 → 鉴权。

    Returns:
        (normalized_query, normalized_mode, auth, kb_ids): 规范化后的 query、mode、鉴权上下文和授权的 kb_ids 交集（空列表 = 全部允许）

    Raises:
        McpToolError(code=-32602): 参数校验失败
        McpAuthError(401): 未认证 / service 不在授权范围
    """
    mode = mode.lower()
    mode = _SEARCH_MODE_ALIASES.get(mode, mode)

    if not service_id or not service_id.strip():
        raise McpToolError(code=-32602, message="service_id 为必填参数，不能为空")
    if not query or not query.strip():
        raise McpToolError(code=-32602, message="query 为必填参数，不能为空")
    query = query.strip()
    if len(query) > 2000:
        raise McpToolError(code=-32602, message="query 长度不能超过 2000 字符")
    if mode not in _VALID_SEARCH_MODES:
        raise McpToolError(
            code=-32602,
            message=f"mode 必须为 {sorted(_VALID_SEARCH_MODES)} 之一，当前值: {mode}",
        )
    if not 1 <= top_k <= 100:
        raise McpToolError(code=-32602, message="top_k 必须在 1-100 之间")

    auth = require_mcp_auth("view")
    kb_ids = await _check_service_scope(auth, service_id, required_permission="call")

    return query, mode, auth, kb_ids


async def search_ontology(
    service_id: str,
    query: str,
    mode: str = "hybrid",
    top_k: int = 5,
    strict_mode: bool = False,
) -> dict:
    """本体优先检索——实体匹配→1-hop 邻域→RAG fallback。

    Action: query_with_ontology
    Guard: require_mcp_auth("view") + _check_service_scope(auth, service_id, required_permission="call")

    三步检索管线：1) 实体匹配 2) 1-hop 邻居遍历 3) RAG 融合。
    支持严格模式（strict_mode=True），多次验证循环确保可靠性。

    Args:
        service_id: 领域服务 ID
        query: 搜索查询（最多 2000 字符）
        mode: 搜索模式（naive / local / global / hybrid，默认 "hybrid"）
        top_k: 返回结果数（默认 5，1-100）
        strict_mode: 是否启用严格模式多次验证（默认 False）

    Returns:
        dict: 搜索结果，含 answer、references、reasoning 字段

    Raises:
        McpAuthError(401): 未认证 / service 不在授权范围
        McpToolError(code=-32602): 参数校验失败（空值 / 长度超限 / mode 不合法 / top_k 超范围）
        McpToolError(code=-32000): 知识库服务不可用
    """
    query_norm, mode_norm, auth, kb_ids = await _validate_search_params(service_id, query, mode, top_k)
    payload: dict = {
        "service_id": service_id,
        "query": query_norm,
        "mode": mode_norm,
        "top_k": top_k,
        "strict_mode": strict_mode,
    }
    if kb_ids:
        payload["knowledge_base_ids"] = kb_ids
    return await call_gateway("query_with_ontology", auth.tenant_id, auth.key_id, payload)


async def search_service(
    service_id: str,
    query: str,
    mode: str = "hybrid",
    top_k: int = 5,
) -> dict:
    """[已废弃] 语义搜索领域服务下的所有知识库。

    自 2026-08-10 起废弃，请使用 search_ontology 替代。
    此别名仅用于向后兼容，将在后续版本中移除。
    """
    logger.warning("search_service is deprecated, use search_ontology instead")
    return await search_ontology(service_id, query, mode, top_k, strict_mode=False)


async def search_deep(
    service_id: str,
    query: str,
    mode: str = "hybrid",
    top_k: int = 5,
    strict_mode: bool = False,
) -> dict:
    """深度查询——多轮分解→子问题查询→归并答案。

    Action: deep_query
    Guard: require_mcp_auth("view") + _check_service_scope(auth, service_id, required_permission="call")

    将复杂问题分解为多个子问题，依次查询后归并生成综合答案。
    支持严格模式（strict_mode=True），多次验证循环确保可靠性。
    常规查询请优先使用 search_ontology（本体检索）或 search_llmwiki（OpenKB 检索）。

    Args:
        service_id: 领域服务 ID
        query: 搜索查询（最多 2000 字符）
        mode: 搜索模式（naive / local / global / hybrid，默认 "hybrid"）
        top_k: 返回结果数（默认 5，1-100）
        strict_mode: 是否启用严格模式多次验证（默认 False）

    Returns:
        dict: 搜索结果，含 answer、references、reasoning 字段

    Raises:
        McpToolError(code=-32000): 知识库服务不可用
        McpAuthError(401): 未认证 / service 不在授权范围
        McpToolError(code=-32602): 参数校验失败（空值 / 长度超限 / mode 不合法 / top_k 超范围）
    """
    query_norm, mode_norm, auth, kb_ids = await _validate_search_params(service_id, query, mode, top_k)
    payload: dict = {
        "service_id": service_id,
        "query": query_norm,
        "mode": mode_norm,
        "top_k": top_k,
        "strict_mode": strict_mode,
    }
    if kb_ids:
        payload["knowledge_base_ids"] = kb_ids
    return await call_gateway("deep_query", auth.tenant_id, auth.key_id, payload)


async def search_llmwiki(
    service_id: str,
    query: str,
    mode: str = "hybrid",
    top_k: int = 5,
) -> dict:
    """OpenKB Wiki 检索——仅适用于 OpenKB 管线知识库，不适用于 LightRAG 管线。

    Action: search_llmwiki (fallback: search_mix)
    Guard: SEARCH_LLMWIKI_ENABLED flag + require_mcp_auth("view") + _check_service_scope(auth, service_id, required_permission="call")

    通过 OpenKB 编译产物进行 Wiki 风格检索。
    若管线不匹配（code=-32602），自动 fallback 到 search_mix。
    不支持 strict_mode（OpenKB 编译产物无严格模式语义）。

    Args:
        service_id: 领域服务 ID
        query: 搜索查询（最多 2000 字符）
        mode: 搜索模式（naive / local / global / hybrid，默认 "hybrid"）
        top_k: 返回结果数（默认 5，1-100）

    Returns:
        dict: 搜索结果，含 answer、references、reasoning 字段

    Raises:
        McpToolError(code=-32000): LLM-Wiki 检索未开放（feature flag 关闭） / 知识库服务不可用
        McpAuthError(401): 未认证 / service 不在授权范围
        McpToolError(code=-32602): 参数校验失败（空值 / 长度超限 / mode 不合法 / top_k 超范围）
    """
    if not SEARCH_LLMWIKI_ENABLED:
        raise McpToolError(code=-32000, message="LLM-Wiki 检索暂未开放")
    query_norm, mode_norm, auth, kb_ids = await _validate_search_params(service_id, query, mode, top_k)
    payload: dict = {
        "service_id": service_id,
        "query": query_norm,
        "mode": mode_norm,
        "top_k": top_k,
    }
    if kb_ids:
        payload["knowledge_base_ids"] = kb_ids
    try:
        return await call_gateway("search_llmwiki", auth.tenant_id, auth.key_id, payload)
    except McpToolError as e:
        # 管线不匹配 (-32602，服务含 lightrag 库) 或 OpenKB 运行时不可用 (-32000)
        # → fallback 到 search_mix 统一入口（按 kb_type 分组扇出，正确处理混合库）
        if e.code in (-32602, _ERROR_INTERNAL):
            logger.info(
                "search_llmwiki 不可用（code=%s），自动回退到 search_mix: service_id=%s",
                e.code,
                service_id,
            )
            return await call_gateway("search_mix", auth.tenant_id, auth.key_id, payload)
        raise


async def search_mix(
    service_id: str,
    query: str,
    mode: str = "hybrid",
    top_k: int = 5,
) -> dict:
    """混合管线检索统一入口——同时查询 LightRAG 和 OpenKB 管线知识库。

    Action: search_mix
    Guard: require_mcp_auth("view") + _check_service_scope(auth, service_id, required_permission="call")

    自动按 pipeline_type 分组扇出到 LightRAG / OpenKB，支持混合选库。
    无论 OpenKB 是否可用，均正常返回（纯 LightRAG 知识库不受影响）。

    Args:
        service_id: 领域服务 ID
        query: 搜索查询（最多 2000 字符）
        mode: 搜索模式（naive / local / global / hybrid，默认 "hybrid"）
        top_k: 返回结果数（默认 5，1-100）

    Returns:
        dict: 搜索结果，含 answer、references、reasoning 字段

    Raises:
        McpAuthError(401): 未认证 / service 不在授权范围
        McpToolError(code=-32602): 参数校验失败（空值 / 长度超限 / mode 不合法 / top_k 超范围）
    """
    query_norm, mode_norm, auth, kb_ids = await _validate_search_params(service_id, query, mode, top_k)
    payload: dict = {
        "service_id": service_id,
        "query": query_norm,
        "mode": mode_norm,
        "top_k": top_k,
    }
    if kb_ids:
        payload["knowledge_base_ids"] = kb_ids
    return await call_gateway("search_mix", auth.tenant_id, auth.key_id, payload)


async def read_source(
    service_id: str,
    document_id: str,
) -> dict:
    """获取文档源文件的预签名 URL。以领域服务为入口，自动解析文档所属知识库。

    Action: get_raw_url
    Guard: require_mcp_auth("view") + _check_service_scope(auth, service_id)

    不经过 MCP 传输大文件内容——仅返回预签名 URL。

    遍历 service 下的所有 KB，依次尝试查找文档，找到后返回预签名 URL。
    若文档不在任何 KB 中，raise McpToolError。

    Args:
        service_id: 领域服务 ID
        document_id: 文档 ID

    Returns:
        dict: 含 url 字段（文档预签名下载 URL）

    Raises:
        McpAuthError(401): 未认证 / service 不在授权范围
        McpToolError(code=-32000): 文档不属于该服务下的任何知识库
    """
    if not service_id or not service_id.strip():
        raise McpToolError(code=-32602, message="service_id 为必填参数，不能为空")
    if not document_id or not document_id.strip():
        raise McpToolError(code=-32602, message="document_id 为必填参数，不能为空")
    auth = require_mcp_auth("view")
    intersection = await _check_service_scope(auth, service_id, required_permission="call")

    if intersection:
        kb_ids_to_try = intersection
    else:
        # allowed_kb_ids 为空 = 全部允许，需要获取 service 的所有 kb_ids
        service_detail = await call_gateway(
            "get_service",
            auth.tenant_id,
            auth.key_id,
            {"service_id": service_id},
        )
        kb_ids_to_try = service_detail.get("kb_ids", [])

    for kb_id in kb_ids_to_try:
        try:
            return await call_gateway(
                "get_raw_url",
                auth.tenant_id,
                auth.key_id,
                {
                    "knowledge_base_id": kb_id,
                    "document_id": document_id,
                },
            )
        except McpToolError:
            continue

    raise McpToolError(code=-32000, message="文档不属于该服务下的任何知识库")


# ============================================================================
# Block 4: __all__ 导出
# ============================================================================

__all__ = [
    "call_gateway",
    "get_http_client",
    "_check_service_scope",
    "_validate_file_name",
    "confirm_upload",
    "generate_upload_url",
    "get_upload_status",
    "list_documents",
    "list_domain_services",
    "read_source",
    "search_deep",
    "search_llmwiki",
    "search_ontology",
    "search_service",   # deprecated, 向后兼容别名
]
