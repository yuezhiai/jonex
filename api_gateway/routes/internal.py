#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""MCP Server 集成的内部 API 路由。

这些端点由 MCP Server（内部服务）调用，不面向终端浏览器。认证使用
``X-Internal-API-Key`` 而非用户 JWT；Gateway 负责把 MCP 调用语义翻译成
Sidecar invoke 契约。
"""

import asyncio
import json
import os
import random
import re
import uuid
from typing import Annotated, Any, Optional

import httpx
import redis.asyncio as aioredis
from fastapi import APIRouter, Body, Header, Request
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
from jonex_core.common.exceptions import JonexException, ResourceNotFoundError
from jonex_core.common.monitor_collector import attempt_span
from jonex_core.common.tenant import require_tenant
from jonex_core.common.tenant_status import is_mcp_key_active

logger = get_logger("api_internal")

router = APIRouter()

# ── Action 白名单 ─────────────────────────────────────────────────
_ALLOWED_ACTIONS = frozenset({"deep_query", "expand_ontology_neighbors", "generate_upload_url", "get_document_status", "get_raw_url", "get_service", "list_documents", "list_ontology_entity_types", "list_ontology_instances", "list_services", "query_with_ontology", "search_jonex_wiki", "search_llmwiki", "search_mix", "submit_mcp_feedback", "upload_document"})
_ALLOWED_ACTIONS = _ALLOWED_ACTIONS | frozenset({
    "get_wiki_graph_types", "search_wiki_nodes", "get_wiki_node",
    "get_wiki_neighbors", "get_wiki_graph_evidence", "get_wiki_media",
})

# MCP 工具名：仅作监控展示元数据，不参与租户/授权判定。
# 为空或非法一律拒绝，避免脏值写入监控表；长度上限与 `monitor.call_log.mcp_tool`
# 的 VARCHAR(128) 对齐。
_MCP_TOOL_MAX_LENGTH = 128
_MCP_TOOL_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")

# MCP 检索 action：携带查询文本。MCP 固定不写搜索历史 —— 由本端点兜底强制
# `save_history=false`（MCP 层也显式传参，双保险）；Web 链路不经此端点，不受影响。
_MCP_SEARCH_ACTIONS = frozenset({
    "deep_query",
    "query_with_ontology",
    "search_jonex_wiki",
    "search_llmwiki",
    "search_mix",
})

# 内部前置校验 action：MCP 工具 handler 为取 kb_ids / 校验归属而发起的辅助调用，
# 不对应任何对外暴露的 MCP 工具（当前只有 ``get_service``，三段调用点都在
# mcp_server/tools.py：list_documents / _check_ontology_kb_scope / read_source）。
# 这类调用不参与 MCP 监控口径：转发 Sidecar 时**省略** ``context.source="mcp"``，
# 采集侧因此记成普通 invoke 记录（protocol=invoke、mcp_tool=NULL），避免与真实
# 工具调用落两条同名记录、把调用次数算成两倍。
# ⚠ 新增内部校验 action 时必须登记到这里；真实工具 action 不得加入。
_INTERNAL_CHECK_ACTIONS = frozenset({"get_service"})


# ── 错误脱敏 ──────────────────────────────────────────────────────

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
# 带 scheme 的单标签主机（如 ``http://sidecar:8001/invoke``）：上一个正则要求
# 主机带点或是 localhost/IP，``http://internal:8787/x`` 这类内部服务名会漏网。
# 负向前瞻排除后面仍接主机字符的情况（否则 ``http://example.com`` 会被截成
# ``[internal-url].com``）；FQDN 已由上一个正则先处理。
_SCHEME_INTERNAL_URL_RE = re.compile(
    r"""\bhttps?://[A-Za-z0-9_-]+(?![A-Za-z0-9_.-])(?::\d{1,5})?(?:/[^"\s,]*)?""",
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

# ── Redis 客户端（download token 存储，懒初始化）───────────────────
_redis_client: aioredis.Redis | None = None


async def _get_redis() -> aioredis.Redis:
    """懒初始化 Redis 客户端，用于 download token 存储。"""
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


# ── Sidecar 重试 / 超时配置（导入时读取一次）───────────────────────
_SIDECAR_MAX_RETRIES = int(os.getenv("GATEWAY_SIDECAR_RETRIES", "2"))
_SIDECAR_BASE_DELAY = float(os.getenv("GATEWAY_SIDECAR_BASE_DELAY", "1.0"))
_SIDECAR_MAX_DELAY = float(os.getenv("GATEWAY_SIDECAR_MAX_DELAY", "8.0"))
_SIDECAR_TIMEOUT = float(os.getenv("GATEWAY_SIDECAR_TIMEOUT", "180"))


def _sanitize_error_message(raw_message: str) -> str:
    """从错误消息中移除 SQL 语句、内部 URL 与堆栈帧。

    在把能力错误返回给 MCP Server 之前调用，确保内部基础设施细节
    不会被暴露（SEC-05）。
    """
    if not raw_message:
        return raw_message

    sanitized = raw_message
    sanitized = _SQL_KEYWORDS_RE.sub("[SQL]", sanitized)
    sanitized = _INTERNAL_URL_RE.sub("[internal-url]", sanitized)
    sanitized = _SCHEME_INTERNAL_URL_RE.sub("[internal-url]", sanitized)
    sanitized = _PYTHON_STACK_RE.sub("[stack-frame]", sanitized)
    sanitized = _JAVA_STACK_RE.sub("[stack-frame]", sanitized)
    sanitized = _JS_STACK_RE.sub("[stack-frame]", sanitized)

    if len(sanitized) > _MAX_SANITIZED_LENGTH:
        sanitized = sanitized[:_MAX_SANITIZED_LENGTH]

    return sanitized


def _sanitize_details(value: Any) -> Any:
    """递归脱敏错误 ``details`` / ``params`` 载荷（SEC-05）。

    ``details`` 与 ``message`` 同属外部可见字段，其中每个字符串都必须过
    ``_sanitize_error_message``。字典的 key 原样保留；非字符串标量
    （int / bool / None 等）原样返回。

    每个字符串单独脱敏（并各自截断）——不对整个载荷序列化后统一截断，
    否则会截掉尾部字段、破坏 MCP 客户端依赖的结构。
    """
    if isinstance(value, str):
        return _sanitize_error_message(value)
    if isinstance(value, dict):
        return {key: _sanitize_details(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_details(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_details(item) for item in value)
    return value


# ── MCP → Sidecar 代理 ────────────────────────────────────────────


async def _call_kb_capability_mcp(
    tenant_id: str,
    mcp_key_id: str,
    action: str,
    data: dict[str, Any] | None = None,
    request_id: str = "",
    mcp_tool: Optional[str] = None,
) -> tuple[dict[str, Any], str]:
    """以 MCP 请求身份经 Sidecar 调用知识库能力。

    Returns ``(data, capability_request_id)``：``capability_request_id`` 取自
    Sidecar 回传的能力信封 ``request_id``，与 ``search_request_event.request_id``
    **同源**，是 MCP 反馈的唯一锚点。Gateway 侧采集用的 ``request_id`` 现随
    ``X-Request-ID`` 发给 Sidecar，故**成功路径**两者同值（能力侧回显入站编号）；
    错误路径拿不到能力信封 id（由全局处理器带 Gateway id），属契约允许的偏差。

    与 ``_call_kb_capability()`` 不同，本函数不接收用户 ``Request``：调用方是
    MCP Server（内部服务），不是已认证的终端用户。租户身份来自 MCP Key，
    并同时通过 ``X-Tenant-ID`` 头与请求体传给 Sidecar（MCT-03 双租户校验）。

    常规 action 的 Sidecar payload 带 ``context.source = "mcp"`` 与
    ``context.mcp_key_id`` 用于审计追踪（SEC-06）。**内部前置校验 action**
    （见 ``_INTERNAL_CHECK_ACTIONS``，如 ``get_service``）不是 MCP 工具调用，
    省略 ``context.source`` 使其按普通 invoke 采集，但仍保留 ``mcp_key_id``。
    ``context.mcp_tool`` 仅在调用方上报了工具名时加入；缺省时保持旧的 payload
    形状不变。

    ``X-Request-ID`` 同时发往 Sidecar，供监控侧把 N1 逻辑调用与 N2 物理尝试
    按同一编号关联（重试次数、降级视图依赖它）。
    """
    config = get_config()

    # 采集用 request_id：MCP 路没有用户 `Request` 对象，调用方拿不到时按本次调用
    # **新生成**；正常由 `mcp_kb_invoke` 从 `request.state.request_id` 透传。
    # 只在此处解析一次 —— 下面的 X-Request-ID 与 attempt_span 必须共用同一个值。
    request_id = request_id or uuid.uuid4().hex

    # 内部前置校验（get_service）不是 MCP 工具调用：省略 ``source`` 让 Sidecar
    # 按普通 invoke 采集；``mcp_key_id`` 必须保留（审计 / 归属校验需要）。
    # 不写 "internal" 等自造枚举 —— 下游只认识 "mcp"，未知值反而会让人误读。
    context: dict[str, Any] = {"mcp_key_id": mcp_key_id}
    if action not in _INTERNAL_CHECK_ACTIONS:
        context["source"] = "mcp"
        if mcp_tool is not None:
            context["mcp_tool"] = mcp_tool

    sidecar_payload: dict[str, Any] = {
        "capability_id": "business.knowledge_base.v1",
        "payload": {"action": action, "data": data or {}},
        "tenant_id": tenant_id,
        "context": context,
    }

    headers = {
        "X-API-Key": config.GATEWAY_API_KEY,
        "X-Request-ID": request_id,
        "X-Tenant-ID": tenant_id,
        "X-Forwarded-For": "",
    }
    transmit_locale_header(headers)

    last_exc: BaseException | None = None
    wait_before_ms: Optional[int] = None
    async with httpx.AsyncClient(timeout=_SIDECAR_TIMEOUT) as client:
        for attempt in range(_SIDECAR_MAX_RETRIES + 1):
            # N2 物理 Attempt 埋点（监控旁路，采不到不影响主链路）。
            with attempt_span(
                request_id=request_id,
                tenant_id=tenant_id,
                attempt_no=attempt + 1,
                # 先按「最后一次尝试」记；只有下面真会重试的 transport 分支才降级为 False。
                is_final=True,
                wait_before_ms=wait_before_ms,
            ) as att:
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
                    # `or {}` 保留原语义：能力返回 [] / 0 / "" 时仍折叠为 {}。
                    return result.get("data") or {}, result.get("request_id") or ""
                except httpx.TimeoutException as e:
                    att.fail(error_code=504, error_message=e)
                    raise CapabilityInvokeError(
                        message="知识库能力调用超时",
                        details={"action": action},
                    )
                except httpx.TransportError as e:
                    att.fail(error_code=502, error_message=e)
                    last_exc = e
                    if attempt < _SIDECAR_MAX_RETRIES:
                        # 还会重试 ⇒ 本次不是最后一次尝试。is_final 无 setter，就地降级快照。
                        att.mark_final(False)
                        delay = min(_SIDECAR_BASE_DELAY * (2 ** attempt), _SIDECAR_MAX_DELAY)
                        jitter = random.uniform(0, delay)
                        wait_before_ms = int(jitter * 1000)
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
                except JonexException as exc:
                    # 下游业务异常（含 ResourceConflictError/ResourceNotFoundError 等，
                    # 由 raise_from_capability_result 按 code 还原）必须穿透：它们的
                    # status_code/code 已是正确业务语义，重包成 500 会把「内容重复」
                    # 「未找到」一律掩盖成「服务不可用」。
                    # message 仍须脱敏——放行异常类型不等于放行原文。
                    # details / params 同属外部可见字段（后者会渲染进模板消息），
                    # 必须一并脱敏，否则只是把泄漏点从 message 挪到别处。
                    exc.message = _sanitize_error_message(exc.message)
                    exc.details = _sanitize_details(exc.details)
                    exc.params = _sanitize_details(exc.params)
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


# ── 端点 ──────────────────────────────────────────────────────────


async def _require_active_mcp_key(tenant_id: str, mcp_key_id: str) -> None:
    """校验 MCP Key 归属与租户可用性。

    与 ``mcp_server/auth.py::find_by_key_hash`` 同口径：Key 必须存在、属于
    请求声明的租户、未撤销/停用/软删/过期，且所属租户 active。失败统一 401、
    不区分失败原因，避免持有内部 Key 的调用方枚举租户与 Key。
    """
    if not await is_mcp_key_active(tenant_id=tenant_id, mcp_key_id=mcp_key_id):
        raise InvalidApiKeyError(message="认证失败")


@router.post("/kb/invoke", summary="MCP Server 知识库能力调用")
async def mcp_kb_invoke(
    request: Request,
    action: str = Body(..., description="知识库能力 action"),
    data: dict[str, Any] | None = Body(None, description="action 参数"),
    tenant_id: str = Body(..., min_length=1, max_length=128, description="租户 ID"),
    mcp_key_id: str = Body(..., min_length=1, max_length=128, description="MCP Key ID"),
    # 用 Annotated 声明 Body 元数据、Python 默认值保持纯 ``None``：本文件既有
    # 单测会直接按关键字调用端点函数，若默认值是 ``Body(None)`` 对象会炸 len()。
    mcp_tool: Annotated[
        Optional[str],
        Body(description="MCP 工具名（可选，仅作监控展示；≤128，字符集 [A-Za-z0-9_.-]）"),
    ] = None,
    x_internal_api_key: str = Header("", alias="X-Internal-API-Key"),
):
    """MCP Server 调用的内部知识库能力端点。

    验证 ``X-Internal-API-Key``（401），校验必填字段、可选工具名和 action
    白名单，然后通过 Sidecar 转发到 knowledge-base capability 并返回结果。
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

    # ── 可选 MCP 工具名校验 ──
    # 仅作监控展示元数据；不参与租户/授权判定。显式校验而不依赖 Body 约束，
    # 是为了让错误定位统一为 details.field（空串也必须拒绝，不能当「未上报」）。
    if mcp_tool is not None and (
        not 1 <= len(mcp_tool) <= _MCP_TOOL_MAX_LENGTH
        or not _MCP_TOOL_PATTERN.fullmatch(mcp_tool)
    ):
        raise InvalidParameterError(
            message="mcp_tool 格式无效",
            details={
                "field": "mcp_tool",
                "max_length": _MCP_TOOL_MAX_LENGTH,
                "pattern": _MCP_TOOL_PATTERN.pattern,
            },
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

    # ── Key 归属 + 租户 active 校验 ──
    # 必须在转发 Sidecar 之前：这条链路只认共享内部 Key，若不校验，持有内部
    # Key 的调用方可对任意租户（含已软删/停用）发起 KB 调用。
    await _require_active_mcp_key(tenant_id, mcp_key_id)

    # ── MCP 兜底：检索 action 固定关闭历史 ──
    # 覆盖调用方传入值（含 True），不只是缺省填充。本端点仅 MCP 使用，
    # 不会影响 Web 检索链路。
    if action in _MCP_SEARCH_ACTIONS:
        data = {**(data or {}), "save_history": False}

    # ── 调用 Sidecar ──
    try:
        result, capability_request_id = await _call_kb_capability_mcp(
            tenant_id=tenant_id,
            mcp_key_id=mcp_key_id,
            action=action,
            data=data or {},
            request_id=getattr(request.state, "request_id", ""),
            mcp_tool=mcp_tool,
        )
    except CapabilityInvokeError as exc:
        # 错误路径拿不到能力信封 request_id（raise_from_capability_result 丢弃），
        # 错误响应由全局处理器带 Gateway id，属契约允许的偏差。
        raise CapabilityInvokeError(
            message=_sanitize_error_message(exc.message),
            details=_sanitize_details(exc.details),
        )

    # 顶层 request_id = 能力信封 id（反馈锚点），不是 Gateway 的采集 id。
    return success_response(data=result, request_id=capability_request_id)


# ── Download token 端点 ──────────────────────────────────────────────


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
