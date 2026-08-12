#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Internal API routes for MCP Server integration.

These endpoints are invoked by the MCP Server (an internal service), not by
end-user browsers. Authentication uses ``X-Internal-API-Key`` rather than user
JWTs.  The Gateway translates MCP-call semantics into Sidecar invoke contracts.
"""

import asyncio
import json
import os
import random
import re
from typing import Any
from uuid import uuid4

import httpx
import redis.asyncio as aioredis
from fastapi import APIRouter, Body, File, Form, Header, Request, UploadFile
from fastapi.responses import RedirectResponse

from jonex_core.common import (
    CapabilityInvokeError,
    InvalidApiKeyError,
    InvalidParameterError,
    MissingApiKeyError,
    get_config,
    get_logger,
    success_response,
    transmit_locale_header,
)
from jonex_core.common.audit import schedule_emit
from jonex_core.common.exceptions import ResourceNotFoundError
from jonex_core.common.object_storage import build_object_key, get_object_storage
from jonex_core.common.tenant import require_tenant

logger = get_logger("api_internal")

router = APIRouter()

# ── Action whitelist ──────────────────────────────────────────────
_ALLOWED_ACTIONS = frozenset({"deep_query", "get_document_status", "get_raw_url", "get_service", "list_documents", "list_services", "query_with_ontology", "search_llmwiki"})


# ── Error sanitization ────────────────────────────────────────────

_SQL_KEYWORDS_RE = re.compile(
    r"""\b(?:SELECT|INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|REPLACE|
    MERGE|GRANT|REVOKE|EXEC|EXECUTE|UNION|DECLARE|FETCH|OPEN|CLOSE)\b""",
    re.IGNORECASE | re.VERBOSE,
)
_INTERNAL_URL_RE = re.compile(
    r"""(?:https?://)?
    (?:
        (?:[a-zA-Z0-9_-]+\.)+[a-zA-Z]{2,}    # FQDN
        | localhost                            # localhost
        | \d{1,3}(?:\.\d{1,3}){3}            # IPv4
        | \[[0-9a-fA-F:]+\]                   # IPv6
    )
    (?::\d{1,5})?
    [/][^"\s,]*""",
    re.VERBOSE,
)
_PYTHON_STACK_RE = re.compile(
    r"""File\s+"[^"]*",\s*line\s*\d+,\s*in\s+\S+""",
)
_JAVA_STACK_RE = re.compile(
    r"""\bat\s+[\w.$]+\([\w.]+:\d+\)""",
)
_JS_STACK_RE = re.compile(
    r"""\bat\s+\S+\s+\([^)]+:\d+:\d+\)""",
)

_MAX_SANITIZED_LENGTH = 1024

# ── Redis client (lazy init for download token storage) ──────────
_redis_client: aioredis.Redis | None = None


async def _get_redis() -> aioredis.Redis:
    """Lazy init Redis 客户端用于 download token 存储。"""
    global _redis_client
    if _redis_client is None:
        config = get_config()
        redis_url = config.REDIS_URL or "redis://redis:6379/0"
        _redis_client = aioredis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=5,
        )
        logger.info("Gateway download-token Redis 客户端已初始化: %s", redis_url)
    return _redis_client


# ── Sidecar retry / timeout config (read once at import) ──────────
_SIDECAR_MAX_RETRIES = int(os.getenv("GATEWAY_SIDECAR_RETRIES", "2"))
_SIDECAR_BASE_DELAY = float(os.getenv("GATEWAY_SIDECAR_BASE_DELAY", "1.0"))
_SIDECAR_MAX_DELAY = float(os.getenv("GATEWAY_SIDECAR_MAX_DELAY", "8.0"))
_SIDECAR_TIMEOUT = float(os.getenv("GATEWAY_SIDECAR_TIMEOUT", "120"))


def _sanitize_error_message(raw_message: str) -> str:
    """Remove SQL statements, internal URLs, and stack traces from error messages.

    Called before returning capability errors to the MCP Server so that
    internal infrastructure details are never exposed (SEC-05).
    """
    if not raw_message:
        return raw_message

    sanitized = raw_message
    sanitized = _SQL_KEYWORDS_RE.sub("[SQL]", sanitized)
    sanitized = _INTERNAL_URL_RE.sub("[internal-url]", sanitized)
    sanitized = _PYTHON_STACK_RE.sub("[stack-frame]", sanitized)
    sanitized = _JAVA_STACK_RE.sub("[stack-frame]", sanitized)
    sanitized = _JS_STACK_RE.sub("[stack-frame]", sanitized)

    if len(sanitized) > _MAX_SANITIZED_LENGTH:
        sanitized = sanitized[:_MAX_SANITIZED_LENGTH]

    return sanitized


# ── MCP → Sidecar proxy ──────────────────────────────────────────


async def _call_kb_capability_mcp(
    tenant_id: str,
    mcp_key_id: str,
    action: str,
    data: dict[str, Any] | None = None,
) -> dict:
    """Invoke the knowledge-base capability via Sidecar for an MCP request.

    Unlike ``_call_kb_capability()`` this does not receive a user ``Request``
    because the caller is the MCP Server (an internal service), not an
    authenticated end user.  Tenant identity comes from the MCP key and is
    forwarded to Sidecar in both the ``X-Tenant-ID`` header and the request
    body (MCT-03 dual-tenant validation).

    The Sidecar payload includes ``context.source = "mcp"`` and
    ``context.mcp_key_id`` for audit tracking (SEC-06).
    """
    config = get_config()

    sidecar_payload: dict[str, Any] = {
        "capability_id": "business.knowledge_base.v1",
        "payload": {"action": action, "data": data or {}},
        "tenant_id": tenant_id,
        "context": {
            "source": "mcp",
            "mcp_key_id": mcp_key_id,
        },
    }

    headers = {
        "X-API-Key": config.GATEWAY_API_KEY,
        "X-Tenant-ID": tenant_id,
        "X-Forwarded-For": "",
    }
    transmit_locale_header(headers)

    last_exc: BaseException | None = None
    async with httpx.AsyncClient(timeout=_SIDECAR_TIMEOUT) as client:
        for attempt in range(_SIDECAR_MAX_RETRIES + 1):
            try:
                response = await client.post(
                    f"{config.SIDECAR_URL}/invoke",
                    json=sidecar_payload,
                    headers=headers,
                )
                result = response.json()
                if response.status_code != 200 or not result.get("success", False):
                    from api_gateway.deps import raise_from_capability_result

                    raise_from_capability_result(result)
                return result.get("data") or {}
            except httpx.TimeoutException:
                raise CapabilityInvokeError(
                    message="知识库能力调用超时",
                    details={"action": action},
                )
            except httpx.TransportError as e:
                last_exc = e
                if attempt < _SIDECAR_MAX_RETRIES:
                    delay = min(_SIDECAR_BASE_DELAY * (2 ** attempt), _SIDECAR_MAX_DELAY)
                    jitter = random.uniform(0, delay)
                    logger.warning(
                        "MCP KB Sidecar 调用 %s 瞬断 (attempt %s/%s)，"
                        "%.1fs 后重试: %s",
                        action, attempt + 1, _SIDECAR_MAX_RETRIES + 1, jitter, e,
                    )
                    await asyncio.sleep(jitter)
                else:
                    logger.error(
                        "MCP KB Sidecar 调用 %s 失败，已重试 %s 次: %s",
                        action, _SIDECAR_MAX_RETRIES, e,
                    )
                    raise CapabilityInvokeError(
                        message=_sanitize_error_message(
                            getattr(e, "message", "") or str(e)
                        ),
                        details={"action": action},
                    )
            except CapabilityInvokeError:
                raise
            except Exception as exc:
                logger.error(
                    "MCP KB capability call failed: action=%s error=%s type=%s",
                    action, exc, type(exc).__name__,
                    exc_info=True,
                )
                raise CapabilityInvokeError(
                    message=_sanitize_error_message(str(exc)),
                    details={"action": action},
                )


# ── Endpoints ────────────────────────────────────────────────────


@router.post("/kb/invoke", summary="MCP Server 知识库能力调用")
async def mcp_kb_invoke(
    request: Request,
    action: str = Body(..., description="知识库能力 action"),
    data: dict[str, Any] | None = Body(None, description="action 参数"),
    tenant_id: str = Body(..., min_length=1, max_length=128, description="租户 ID"),
    mcp_key_id: str = Body(..., min_length=1, max_length=128, description="MCP Key ID"),
    x_internal_api_key: str = Header("", alias="X-Internal-API-Key"),
):
    """MCP Server 调用的内部知识库能力端点。

    验证 ``X-Internal-API-Key``（401），校验必填字段和 action 白名单（422），
    然后通过 Sidecar 转发到 knowledge-base capability 并返回结果。
    """
    config = get_config()

    # ── 认证：X-Internal-API-Key ──
    # 不设条件守卫：startup guard 已确保 INTERNAL_API_KEY 非空且非哨兵值，
    # 此处始终强制校验 header，防止配置变更或热重载导致认证被意外跳过。
    if not x_internal_api_key:
        raise MissingApiKeyError(message="缺少 X-Internal-API-Key 请求头")
    if x_internal_api_key != config.INTERNAL_API_KEY:
        raise InvalidApiKeyError(message="X-Internal-API-Key 无效")

    # ── 必填字段校验 ──
    if not action:
        raise InvalidParameterError(
            message="action 为必填字段",
            details={"field": "action"},
        )
    if not tenant_id:
        raise InvalidParameterError(
            message="tenant_id 为必填字段",
            details={"field": "tenant_id"},
        )
    require_tenant(tenant_id)
    if not mcp_key_id:
        raise InvalidParameterError(
            message="mcp_key_id 为必填字段",
            details={"field": "mcp_key_id"},
        )

    # ── Action 白名单校验 ──
    if action not in _ALLOWED_ACTIONS:
        raise InvalidParameterError(
            message=f"不支持的操作: {action}",
            details={
                "field": "action",
                "value": action,
                "allowed": sorted(_ALLOWED_ACTIONS),
            },
        )

    # ── 调用 Sidecar ──
    try:
        result = await _call_kb_capability_mcp(
            tenant_id=tenant_id,
            mcp_key_id=mcp_key_id,
            action=action,
            data=data or {},
        )
    except CapabilityInvokeError as exc:
        raise CapabilityInvokeError(
            message=_sanitize_error_message(exc.message),
            details=exc.details,
        )

    return success_response(data=result)


# ── MCP upload endpoint ──────────────────────────────────────────────


@router.post("/kb/documents/upload", summary="MCP Server 文档上传（内部端点）")
async def mcp_upload_document(
    request: Request,
    file: UploadFile = File(...),
    file_name: str = Form(..., min_length=1, max_length=512),
    knowledge_base_id: str = Form(..., min_length=1, max_length=128),
    mime_type: str = Form("", max_length=128),
    mcp_key_id: str = Form(..., min_length=1, max_length=128),
    x_internal_api_key: str = Header("", alias="X-Internal-API-Key"),
    x_tenant_id: str = Header("", alias="X-Tenant-ID"),
):
    """MCP Server 专用文档上传端点：接收 multipart 文件字节 → 写对象存储 → 调 capability。

    流程: 认证 → 租户校验 → 文件空检查 → 大小校验 → 写对象存储 → Sidecar invoke
    """
    config = get_config()
    max_size_mb = int(os.getenv("MCP_UPLOAD_MAX_SIZE_MB", "50"))

    # ── 1. 认证：X-Internal-API-Key ──
    if not x_internal_api_key:
        raise MissingApiKeyError(message="缺少 X-Internal-API-Key 请求头")
    if x_internal_api_key != config.INTERNAL_API_KEY:
        raise InvalidApiKeyError(message="X-Internal-API-Key 无效")

    # ── 2. 租户校验 ──
    require_tenant(x_tenant_id)

    # ── 3. 文件读取与空检查 ──
    content = await file.read()
    if not content:
        raise InvalidParameterError(message="上传文件不能为空")

    # ── 4. 文件大小校验 ──
    max_size_bytes = max_size_mb * 1024 * 1024
    if len(content) > max_size_bytes:
        raise InvalidParameterError(
            message=f"文件大小超过限制 ({max_size_mb}MB)",
            details={"file_size": len(content), "max_size": max_size_bytes},
        )

    # ── 5. 写对象存储 ──
    doc_id = str(uuid4())
    backend = os.getenv("OBJECT_STORAGE_BACKEND", "local").strip().lower()
    content_type = mime_type or "application/octet-stream"
    storage_key = build_object_key(x_tenant_id, knowledge_base_id, doc_id, file_name)
    await get_object_storage().put_bytes(
        storage_key, content, content_type=content_type
    )

    # ── 6. 调 capability ──
    data = {
        "doc_id": doc_id,
        "file_name": file_name,
        "file_size": len(content),
        "mime_type": content_type,
        "storage_key": storage_key,
        "storage_backend": backend,
        "knowledge_base_id": knowledge_base_id,
    }
    try:
        result = await _call_kb_capability_mcp(
            tenant_id=x_tenant_id,
            mcp_key_id=mcp_key_id,
            action="upload_document",
            data=data,
        )
    except CapabilityInvokeError as exc:
        raise CapabilityInvokeError(
            message=_sanitize_error_message(exc.message),
            details=exc.details,
        )

    return success_response(data=result)


# ── Download token endpoint ──────────────────────────────────────────


@router.get("/kb/download/{token}", summary="Token 化文档下载（一次性）")
async def kb_download_token(token: str):
    """消费一次性下载 token，302 重定向到 COS 预签名 URL。

    Token 由 mcp_kb_invoke 在处理 get_raw_url action 时生成并存储在 Redis 中，
    TTL 300s，一次性消费后立即删除。
    """
    redis = await _get_redis()
    token_key = f"download_token:{token}"
    stored = await redis.getdel(token_key)

    if not stored:
        raise ResourceNotFoundError(
            message="下载链接已过期或已使用",
            details={"token_valid": False},
        )

    data = json.loads(stored)

    # 审计日志
    schedule_emit({
        "tenant_id": data.get("tenant_id", ""),
        "log_type": "OPERATION",
        "action": "document.download",
        "outcome": "SUCCESS",
        "service_name": "gateway",
        "resource": "document",
        "resource_id": data.get("document_id", ""),
        "request_params": {
            "knowledge_base_id": data.get("knowledge_base_id", ""),
            "mcp_key_id": data.get("mcp_key_id", ""),
        },
    })

    logger.info(
        "Download token consumed: tenant=%s doc=%s",
        data.get("tenant_id"), data.get("document_id"),
    )

    return RedirectResponse(url=data["presigned_url"], status_code=302)
