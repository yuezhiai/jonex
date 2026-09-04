#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""
Jonex 平台 - MCP Server

知识库 MCP Tool 服务，提供：
- MCP Streamable HTTP 传输
- MCP Key 鉴权中间件
- knowledge_base 搜索/读取 tool handler
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
    """语义搜索——检索领域服务下知识库的相关内容，作为通用搜索入口。
    适用于常规问答、文档检索、知识查询。
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
    """[实验性，需配置启用] 深度多轮查询——将复杂问题拆解为子问题逐一检索后汇总归并。
    仅适用于需要多步推理的复杂分析场景，常规搜索请优先使用 search_ontology。
    当前可能因 DEEP_QUERY_ENABLED 未启用而不可用。
    搜索模式：naive / local / global / hybrid（默认 hybrid）。
    支持严格模式（strict_mode=True）多次验证循环确保可靠性。"""
    return await tools.search_deep(service_id, query, mode, top_k, strict_mode)


@mcp.tool()
async def search_llmwiki(
    service_id: str, query: str, mode: str = "hybrid", top_k: int = 5,
) -> dict:
    """Wiki 知识检索——从 OpenKB 编译的 Wiki 知识库中检索。
    仅适用于 OpenKB 管线知识库，通用搜索请优先使用 search_ontology。
    搜索模式：naive / local / global / hybrid（默认 hybrid）。
    不支持严格模式（OpenKB 编译产物无严格模式语义）。"""
    return await tools.search_llmwiki(service_id, query, mode, top_k)


@mcp.tool()
async def search_mix(
    service_id: str, query: str, mode: str = "hybrid", top_k: int = 5,
) -> dict:
    """混合检索——自动识别知识库管线类型（LightRAG / OpenKB）并扇出并行检索。
    适用于包含多种管线知识库的领域服务，通用搜索请优先使用 search_ontology。
    搜索模式：naive / local / global / hybrid（默认 hybrid）。"""
    return await tools.search_mix(service_id, query, mode, top_k)


@mcp.tool()
async def read_source(service_id: str, document_id: str) -> dict:
    """获取文档源文件的预签名 URL。以领域服务为入口，自动解析文档所属知识库。"""
    return await tools.read_source(service_id, document_id)


@mcp.tool()
async def confirm_upload(
    service_id: str,
    file_name: str,
    knowledge_base_id: str,
    storage_key: str,
    doc_id: str = "",
    mime_type: str = "",
) -> dict:
    """确认 COS 直传结果并触发解析索引（直传闭环最后一步）。

    先调 generate_upload_url 拿预签名 PUT URL + storage_key，客户端直传对象存储后，
    再调本工具传 storage_key 确认入库并触发解析，否则文档不会进入检索。
    上传后异步解析，用 get_upload_status 查询处理进度。"""
    return await tools.confirm_upload(
        service_id, file_name, knowledge_base_id, storage_key, doc_id, mime_type
    )


@mcp.tool()
async def get_upload_status(service_id: str, document_id: str) -> dict:
    """查询已上传文档的处理状态。返回 status（pending/parsing/ingesting/ready/failed）、
    ontology_status（pending/extracting/ready/failed）、error_message 等字段。
    用于追踪 confirm_upload 后的异步解析进度。"""
    return await tools.get_upload_status(service_id, document_id)


@mcp.tool()
async def generate_upload_url(
    service_id: str,
    knowledge_base_id: str,
    file_name: str,
    content_type: str = "",
    file_size: int = 0,
) -> dict:
    """生成 COS 预签名上传 URL，用于文件直传（替代 base64 内联上传）。
    返回 {doc_id, storage_key, upload_url, storage_backend}。
    客户端 curl -X PUT --upload-file <file> <upload_url> 直传 COS 后，
    必须调用 confirm_upload 确认入库并触发解析索引，否则文档不会进入检索。
    处理进度用 get_upload_status 查询。
    file_size 为待上传字节数（可选，默认 0 表示未知）：传 >0 时服务端校验上限并
    将 Content-Length 签入预签名，锁死精确大小。"""
    return await tools.generate_upload_url(
        service_id, knowledge_base_id, file_name, content_type, file_size
    )


async def health(request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


async def lifespan(app: Starlette):
    await create_pool()
    _client = httpx.AsyncClient(
        base_url=settings.GATEWAY_URL,
        timeout=120,
    )
    tools.set_http_client(_client)
    # OpenKB 健康检查——控制 SEARCH_LLMWIKI_ENABLED 运行时开关
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
#   - 置于鉴权外层：防止攻击者无限制暴力尝试无效 key
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
