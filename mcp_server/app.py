#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""
悦溪平台 - MCP Server

知识库 MCP Tool 服务，提供：
- MCP Streamable HTTP 传输
- MCP Key 鉴权中间件（Phase 2）
- knowledge_base 搜索/读取 tool handler（Phase 4）
"""
import contextlib
import logging
import os

from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.responses import JSONResponse
from mcp.server.fastmcp import FastMCP

import httpx

from auth import McpAuthMiddleware
from rate_limit import McpRateLimitMiddleware
from config import settings
from db import create_pool, close_pool
from health import check_openkb_health
import tools

logger = logging.getLogger("mcp")

mcp = FastMCP("jonex-knowledge-base", stateless_http=True, host="0.0.0.0")


@mcp.tool()
async def list_domain_services() -> dict:
    """列出 MCP Key 对应租户下所有领域服务。"""
    return await tools.list_domain_services()


@mcp.tool()
async def list_documents(service_id: str, file_name: str = "") -> dict:
    """列出领域服务下所有文档。以领域服务为入口自动解析所属知识库。
    支持可选 file_name 关键词过滤。返回文档 id + file_name，可直接用于 read_source 获取原文。"""
    return await tools.list_documents(service_id, file_name)


@mcp.tool()
async def search_ontology(
    service_id: str, query: str, mode: str = "hybrid", top_k: int = 5, strict_mode: bool = False,
) -> dict:
    """本体优先检索——实体匹配→1-hop 邻域→RAG fallback。
    搜索模式：naive / local / global / hybrid（默认 hybrid）。
    支持严格模式（strict_mode=True）多次验证循环确保可靠性。"""
    return await tools.search_ontology(service_id, query, mode, top_k, strict_mode)


@mcp.tool()
async def search_service(
    service_id: str, query: str, mode: str = "hybrid", top_k: int = 5,
) -> dict:
    """[已废弃] 语义搜索领域服务下的所有知识库。请使用 search_ontology 替代。"""
    return await tools.search_service(service_id, query, mode, top_k)


@mcp.tool()
async def search_deep(
    service_id: str, query: str, mode: str = "hybrid", top_k: int = 5, strict_mode: bool = False,
) -> dict:
    """深度查询——多轮分解→子问题查询→归并答案。
    搜索模式：naive / local / global / hybrid（默认 hybrid）。
    支持严格模式（strict_mode=True）多次验证循环确保可靠性。"""
    return await tools.search_deep(service_id, query, mode, top_k, strict_mode)


@mcp.tool()
async def search_llmwiki(
    service_id: str, query: str, mode: str = "hybrid", top_k: int = 5,
) -> dict:
    """OpenKB Wiki 检索——仅适用于 OpenKB 管线知识库，不适用于 LightRAG 管线。
    搜索模式：naive / local / global / hybrid（默认 hybrid）。
    不支持严格模式（OpenKB 编译产物无严格模式语义）。"""
    return await tools.search_llmwiki(service_id, query, mode, top_k)


@mcp.tool()
async def read_source(service_id: str, document_id: str) -> dict:
    """获取文档源文件的预签名 URL。以领域服务为入口，自动解析文档所属知识库。"""
    return await tools.read_source(service_id, document_id)


@mcp.tool()
async def upload_document(
    service_id: str,
    file_name: str,
    file_content_base64: str,
    knowledge_base_id: str,
    mime_type: str = "",
) -> dict:
    """上传文档到指定知识库。文档将经过解析和索引后支持语义搜索。"""
    return await tools.upload_document(
        service_id, file_name, file_content_base64, knowledge_base_id, mime_type
    )


@mcp.tool()
async def get_upload_status(service_id: str, document_id: str) -> dict:
    """查询已上传文档的处理状态。返回 status（pending/parsing/ingesting/ready/failed）、
    ontology_status（pending/extracting/ready/failed）、error_message 等字段。
    用于追踪 upload_document 后的异步解析进度。"""
    return await tools.get_upload_status(service_id, document_id)


async def health(request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


async def lifespan(app: Starlette):
    await create_pool()
    _client = httpx.AsyncClient(
        base_url=settings.GATEWAY_URL,
        timeout=30,
    )
    tools.set_http_client(_client)
    # Phase 11: OpenKB 健康检查——控制 SEARCH_LLMWIKI_ENABLED 运行时开关 (D-08)
    health_ok = await check_openkb_health(settings.OPENKB_HEALTH_URL, client=_client)
    if not health_ok:
        tools.SEARCH_LLMWIKI_ENABLED = False
    try:
        async with mcp.session_manager.run():
            yield
    finally:
        try:
            _client_ref = await tools.get_http_client()
        except RuntimeError:
            _client_ref = None
        if _client_ref is not None:
            await _client_ref.aclose()
            tools.set_http_client(None)
        await close_pool()


app = Starlette(
    routes=[
        Route("/health", health, methods=["GET"]),
        Mount("/", app=mcp.streamable_http_app()),
    ],
    lifespan=lifespan,
)
# ═══════════════════════════════════════════════════════════════════════════════
# 中间件注册顺序说明（ASGI 中间件 = 洋葱模型）
# ──────────────────────────────────────────────────────────────────────────────
# Starlette 中间件按注册顺序逐层包裹——最后注册的在外层，最先执行。
#
#   请求 → [限流 McpRateLimitMiddleware] → [鉴权 McpAuthMiddleware] → MCP App
#         ← [限流 McpRateLimitMiddleware] ← [鉴权 McpAuthMiddleware] ← 响应
#
# 外层：限流中间件（后注册，最先执行）
#   - 对所有请求生效（含未认证请求），按 key_id 或 "unknown" 限流
#   - _get_key_id() 优先从 contextvar 读取；若 contextvar 为 None（鉴权未到达），
#     降级为 "unknown" key（同源限流，避免 key_id=None 导致所有请求合并计数）
#   - 置于鉴权外层：防止攻击者无限制暴力尝试无效 key（per P0 jonex-0o3）
app = McpRateLimitMiddleware(
    app,
    storage_uri=settings.REDIS_URL.replace("redis://", "async+redis://"),
    limit_str="60/minute",
)
# 内层：鉴权中间件（先注册，后执行）
#   解析 MCP Authorization header → 验证签名 → 注入 McpAuthContext
app = McpAuthMiddleware(app)

# Startup guard: JWT_SECRET must be set and non-empty
_jwt_secret = os.getenv("JWT_SECRET", "").strip()
if not _jwt_secret:
    raise RuntimeError(
        "JWT_SECRET 环境变量未配置或为空。"
        "MCP Key 鉴权依赖 JWT_SECRET 作为 HMAC key，"
        "未配置将导致所有鉴权可被绕过。"
        "请在 .env.mcp 或 deploy/.env.mcp 中设置 JWT_SECRET。"
    )
