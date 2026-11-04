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
from typing import Annotated, Any

from pydantic import WithJsonSchema
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.responses import JSONResponse
from mcp.server.fastmcp import FastMCP

import httpx

from auth import McpAuthMiddleware
from rate_limit import McpRateLimitMiddleware
from config import settings
from crypto import assert_secure_jwt_secret
from db import create_pool, close_pool
from health import check_openkb_health, check_jonex_wiki_health
import tools

logger = logging.getLogger("mcp")

mcp = FastMCP("jonex-knowledge-base", stateless_http=True, host="0.0.0.0")

# FastMCP 会用 pydantic lax 模式先解析函数签名里的 int 注解：JSON `"2"`、
# `2.0`、`true` 会在进入 handler 前被静默转成 int，handler 看不到原始值。
# depth 因此用 Any 接收原始值，由 tools._ontology_int_param 严格校验并返回
# 结构化 -32602；WithJsonSchema 保证 tools/list 中仍是 integer。
_DEPTH_SCHEMA = {"type": "integer", "default": 1, "title": "Depth"}


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
async def list_ontology_entity_types(
    service_id: str,
    knowledge_base_id: str = "",
    knowledge_base_ids: Any = None,
) -> dict:
    """浏览本体知识库的「实体类型目录」：返回实体类型（如设备、部件）及其实例数量、状态和说明，是本体列式浏览的第一列。

    知识库选择（二选一）：
    - knowledge_base_id：指定单个知识库（旧用法）。
    - knowledge_base_ids：字符串数组，指定多个知识库，单次聚合并按库分组（形状由服务端校验，非法返回 -32602）。
    - 两者都不传：查询该服务下全部可读知识库（最多 20 个，默认上限，部署可调）。
    knowledge_base_id 与 knowledge_base_ids 不能同时提供。

    返回：单个知识库保持原结构；多个知识库返回 groups，每项含 knowledge_base_id。某个库不可访问时该组带 errors，其余库照常返回。

    何时用：需要先了解知识库抽取出了哪些实体类型、每类有多少实例时，作为后续 list_ontology_instances 的入口。

    何时不用：已知具体实例、想沿关系探索时用 expand_ontology_neighbors；想检索文档内容时用 search_ontology。只有本体类（lightrag）知识库有数据，其他类型知识库返回空列表。

    注意：不同知识库中的同名类型是不同类型，请按 groups[].knowledge_base_id 区分；不提供跨知识库关系边，也不做同名合并。"""
    return await tools.list_ontology_entity_types(
        service_id, knowledge_base_id, knowledge_base_ids
    )


@mcp.tool()
async def list_ontology_instances(
    service_id: str,
    knowledge_base_id: str = "",
    entity_type: str = "",
    keyword: str = "",
    page: int = 1,
    page_size: int = 20,
    knowledge_base_ids: Any = None,
) -> dict:
    """分页浏览某个实体类型下的具体实例（如「1号变压器」），支持按名称或别名关键词模糊搜索。返回的实例行没有 `id`，定位单个实例请用 `type` + `name`。

    知识库选择（二选一）：
    - knowledge_base_id：指定单个知识库（旧用法）。
    - knowledge_base_ids：字符串数组，指定多个知识库，单次聚合并按库分组（形状由服务端校验，非法返回 -32602）。
    - 两者都不传：查询该服务下全部可读知识库（最多 20 个，默认上限，部署可调）。
    knowledge_base_id 与 knowledge_base_ids 不能同时提供。

    返回：单个知识库保持原结构；多个知识库返回 groups，每项含 knowledge_base_id。某个库不可访问时该组带 errors，其余库照常返回。

    何时用：已知实体类型，想列出或搜索该类下的具体实例时。

    何时不用：还不知道有哪些类型时先用 list_ontology_entity_types；想沿关系展开某个实例的邻居时用 expand_ontology_neighbors。

    注意：多个知识库时 page / page_size 对每个知识库独立生效（不是全局第 N 页），顶层 total 为各库 total 之和，且不返回顶层 items；不跨库合并同名实例。"""
    return await tools.list_ontology_instances(
        service_id, knowledge_base_id, entity_type, keyword, page, page_size,
        knowledge_base_ids,
    )


@mcp.tool()
async def expand_ontology_neighbors(
    service_id: str,
    knowledge_base_id: str = "",
    entity_type: str = "",
    canonical_name: str = "",
    limit: int = 50,
    knowledge_base_ids: Any = None,
    depth: Annotated[Any, WithJsonSchema(_DEPTH_SCHEMA)] = 1,
) -> dict:
    """展开一个本体实例的邻居：默认一跳；传入 depth=2 或 3 可在一次调用里展开多跳。传入实体类型 + 规范名称定位起点，返回与之相连的节点和边。

    何时用：已知一个具体实例，想看看它直接连着哪些实体；或需要在一次调用里看清 2–3 跳范围时用 depth。
    何时不用：还没有具体实例名时，先用 list_ontology_instances；想看类型全貌时用 list_ontology_entity_types。depth>1 会按分支因子放大返回体量，仅在确有多跳需要时使用（局部属性查询、hub 节点、上下文紧张时不要用）。

    知识库选择（二选一）：
    - knowledge_base_id：指定单个知识库（旧用法）。
    - knowledge_base_ids：字符串数组，指定多个知识库，单次聚合并按库分组（形状由服务端校验，非法返回 -32602）。
    - 两者都不传：查询该服务下全部可读知识库（最多 20 个，默认上限，部署可调）。
    knowledge_base_id 与 knowledge_base_ids 不能同时提供。

    返回：单个知识库（且 depth=1）保持原结构 {nodes, edges, degraded}；多个知识库只返回 groups，每项含 knowledge_base_id，不提供顶层 nodes / edges / degraded。depth>1 时 nodes/edges 每项带 hop（起点 hop=0），并返回 depth / limit / returned / truncated；多个库时每组独立 depth / limit。某个库不可访问时该组带 errors，其余库照常返回。

    注意：depth 默认 1、上限 3（部署可调）；limit 是单次返回的总边数（默认 50，上限 200），depth>1 时每个节点每层还会限制扇出。truncated 仅在 depth>1 返回：true 表示结果不完整（被 limit 或扇出上限截断）。depth>1 时工具内部已按节点 id 去重并去环，无需自行维护已访问集合；depth=1 时请自行维护已访问集合，不要重复展开同一节点。多库时同名节点不合并，也没有跨库关系边。"""
    return await tools.expand_ontology_neighbors(
        service_id, knowledge_base_id, entity_type, canonical_name, limit,
        knowledge_base_ids, depth,
    )


@mcp.tool()
async def search_ontology(
    service_id: str, query: str, mode: str = "hybrid", top_k: int = 5, strict_mode: bool = False,
    with_reasoning: bool = False,
) -> dict:
    """语义搜索——检索领域服务下 LightRAG（本体）知识库的相关内容。
    仅适用于 LightRAG 管线知识库；jonex_wiki / OpenKB 库请用 search_jonex_wiki /
    search_llmwiki，不确定管线类型或服务含多种管线时用 search_mix。
    适用于常规问答、文档检索、知识查询。
    搜索模式：naive / local / global / hybrid（默认 hybrid）。
    支持严格模式（strict_mode=True）多次验证循环确保可靠性。
    支持 with_reasoning 推理链追踪（默认 False），返回逐步推导过程与 chunk 召回信息。
    响应顶层含 request_id（本次检索的反馈锚点）；MCP 检索不写入搜索历史。"""
    return await tools.search_ontology(service_id, query, mode, top_k, strict_mode, with_reasoning)


@mcp.tool()
async def search_deep(
    service_id: str, query: str, mode: str = "hybrid", top_k: int = 5, strict_mode: bool = False,
    with_reasoning: bool = False,
) -> dict:
    """[实验性，需配置启用] 深度多轮查询——将复杂问题拆解为子问题逐一检索后汇总归并。
    仅适用于需要多步推理的复杂分析场景，常规搜索请优先使用 search_ontology 或 search_mix。
    当前可能因 DEEP_QUERY_ENABLED 未启用而不可用。
    搜索模式：naive / local / global / hybrid（默认 hybrid）。
    支持严格模式（strict_mode=True）多次验证循环确保可靠性。
    支持 with_reasoning 推理链追踪（默认 False），返回逐步推导过程与 chunk 召回信息。
    响应顶层含 request_id（本次检索的反馈锚点）；MCP 检索不写入搜索历史。"""
    return await tools.search_deep(service_id, query, mode, top_k, strict_mode, with_reasoning)


@mcp.tool()
async def search_llmwiki(
    service_id: str, query: str, mode: str = "hybrid", top_k: int = 5,
    with_reasoning: bool = False,
) -> dict:
    """Wiki 知识检索——从 OpenKB 编译的 Wiki 知识库中检索。
    仅适用于 OpenKB 管线知识库；不确定管线类型时用 search_mix。
    搜索模式：naive / local / global / hybrid（默认 hybrid）。
    不支持严格模式（OpenKB 编译产物无严格模式语义）。
    支持 with_reasoning 推理链追踪（默认 False），返回 wiki 浏览轨迹。
    响应顶层含 request_id（本次检索的反馈锚点，回退时取最终成功那次）；MCP 检索不写入搜索历史。"""
    return await tools.search_llmwiki(service_id, query, mode, top_k, with_reasoning)


@mcp.tool()
async def search_jonex_wiki(
    service_id: str, query: str, mode: str = "hybrid", top_k: int = 5,
    with_reasoning: bool = False,
) -> dict:
    """Jonex Wiki 知识检索——从 jonex_wiki 编译的 Wiki 知识库中检索。
    仅适用于 jonex_wiki 管线知识库；不确定管线类型时用 search_mix。
    搜索模式：naive / local / global / hybrid（默认 hybrid）。
    不支持严格模式（jonex_wiki 编译产物无严格模式语义）。
    支持 with_reasoning 推理链追踪（默认 False），返回 wiki 浏览轨迹。
    响应顶层含 request_id（本次检索的反馈锚点，回退时取最终成功那次）；MCP 检索不写入搜索历史。"""
    return await tools.search_jonex_wiki(service_id, query, mode, top_k, with_reasoning)


@mcp.tool()
async def search_mix(
    service_id: str, query: str, mode: str = "hybrid", top_k: int = 5,
    with_reasoning: bool = False,
) -> dict:
    """混合检索——自动识别知识库管线类型（LightRAG / OpenKB / jonex_wiki）并扇出并行检索。
    不确定该用哪个检索工具时优先用它；适用于包含多种管线知识库的领域服务。
    搜索模式：naive / local / global / hybrid（默认 hybrid）。
    支持 with_reasoning 推理链追踪（默认 False），返回逐步推导过程与 chunk 召回信息。
    响应顶层含 request_id（本次检索的反馈锚点）；MCP 检索不写入搜索历史。"""
    return await tools.search_mix(service_id, query, mode, top_k, with_reasoning)


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
    ontology_status（pending/extracting/ready/failed）、error_message、file_name、
    file_size、knowledge_base_id、created_at、updated_at 等字段，以及图谱降级提示：

    - ontology_error：本体抽取失败原因；
    - ontology_retry_count / ontology_retry_limit：已重试次数 / 上限；
    - degraded / degraded_reason：status=ready 且 ontology_status=failed 时为
      true / "ontology_failed"（部分成功：正文仍可问答，但知识图谱未建成）；
    - retry_exhausted：重试已达上限（此时 status 仍为 ready）。

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


@mcp.tool()
async def submit_search_feedback(
    request_id: str,
    service_id: str,
    feedback_type: str,
    feedback_reason: str = "",
    feedback_comment: str = "",
    operation_id: str = "",
    version: int = 1,
) -> dict:
    """提交检索反馈（点赞 / 点踩），用于对检索结果做效果反馈。

    适用场景：调用任一检索工具拿到结果后，把响应顶层的 request_id 与
    对应的 service_id 回传，标记本次检索是否有帮助。
    feedback_type 取 like / dislike；点踩时 feedback_reason 必填，取值：
    inaccurate / not_answered / incomplete / wrong_reference / missing_knowledge / other；
    feedback_comment 可选补充说明（≤ 300 字符）。
    request_id 必须来自本次 MCP 检索响应——MCP 检索不写搜索历史，没有其他锚点。
    重复提交同一次检索时按 operation_id 幂等（缺省自动生成）。
    返回 {feedback, superseded, idempotent}：feedback 为反馈记录，
    superseded 表示版本号落后被丢弃，idempotent 表示该 operation_id 已处理过。"""
    return await tools.submit_search_feedback(
        request_id, service_id, feedback_type,
        feedback_reason or None,
        feedback_comment or None,
        operation_id or None,
        version,
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
    # 依赖健康检查只用于启动观测，**不翻转进程级开关**：
    # jonex-wiki / openkb 晚于 mcp-server 就绪是部署常态（前者要跑 alembic + 加载引擎），
    # 一次性探测失败就永久禁用工具，会导致「服务后来起来了，工具仍一直报暂未开放」且无法自愈。
    # 调用时上游真不可用会返回 -32000，由 handler 的 fallback 回退到 search_mix。
    health_ok = await check_openkb_health(settings.OPENKB_HEALTH_URL, client=_client)
    if not health_ok:
        logger.warning(
            "OpenKB 启动探测未通过（不影响工具可用性，调用时按实际结果降级）"
        )
    jonex_health_ok = await check_jonex_wiki_health(settings.JONEX_WIKI_HEALTH_URL, client=_client)
    if not jonex_health_ok:
        logger.warning(
            "jonex_wiki 启动探测未通过（不影响工具可用性，调用时按实际结果降级）"
        )
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

# Startup guard: JWT_SECRET 必须非空。
# 2026-09-22 起「非仓库里的公开默认值」判定已停用（原因：成员环境未配置真实值，
# 容器无法启动），黑名单常量保留在 crypto.py 以便恢复。本服务是 MCP Key 的校验侧，
# 盐是公开值即等于 Key 可被离线爆破比对，请确保各环境 MCP_KEY_SECRET 独立配置。
# 判据与逃生阀统一在 crypto.assert_secure_jwt_secret（零 jonex_core 依赖）。
assert_secure_jwt_secret()
