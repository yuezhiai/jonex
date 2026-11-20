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

# Jonex Wiki 列式浏览的整数参数同样用 Any + WithJsonSchema 接收原始值：
# FastMCP lax 模式会把 "2" / true / 2.0 静默转成 int，契约要求这些一律 -32602。
_WIKI_DEPTH_SCHEMA = {"type": "integer", "default": 1, "title": "Depth"}
_WIKI_LINKS_LIMIT_SCHEMA = {"type": "integer", "default": 50, "title": "Limit"}
_WIKI_PAGE_SCHEMA = {"type": "integer", "default": 1, "title": "Page"}
_WIKI_PAGE_SIZE_SCHEMA = {"type": "integer", "default": 20, "title": "Page size"}
_WIKI_CLAIMS_LIMIT_SCHEMA = {"type": "integer", "default": 200, "title": "Limit"}


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

    何时不用：已知具体实例、想沿关系探索时用 expand_ontology_neighbors；想检索文档内容时用 search_ontology。只有本体类（lightrag）知识库有数据，其他类型知识库返回空列表；想浏览 Jonex Wiki 知识库请用 list_wiki_topics / list_wiki_pages，OpenKB 暂无浏览工具。

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

    何时不用：还不知道有哪些类型时先用 list_ontology_entity_types；想沿关系展开某个实例的邻居时用 expand_ontology_neighbors；Jonex Wiki 知识库请用 list_wiki_pages（本体工具对非 lightrag 库返回空列表）。

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
    何时不用：还没有具体实例名时，先用 list_ontology_instances；想看类型全貌时用 list_ontology_entity_types；Jonex Wiki 知识库请用 expand_wiki_links（本体工具对非 lightrag 库返回空列表）。depth>1 会按分支因子放大返回体量，仅在确有多跳需要时使用（局部属性查询、hub 节点、上下文紧张时不要用）。

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
async def list_wiki_topics(
    service_id: str,
    knowledge_base_id: str = "",
    knowledge_base_ids: Any = None,
) -> dict:
    """浏览 Jonex Wiki 知识库的目录/主题树，是 Jonex Wiki 列式浏览的第一列。

    仅支持 Jonex Wiki（jonex_wiki 管线）知识库；LightRAG / OpenKB 库会返回 -32602。
    想浏览本体（lightrag）知识库请用 list_ontology_entity_types / list_ontology_instances。
    知识库选择（二选一）：
    - knowledge_base_id：指定单个知识库。
    - knowledge_base_ids：字符串数组，指定多个知识库，单次聚合并按库分组。
    - 两者都不传：查询该服务下全部 Jonex Wiki 知识库（超过 20 个时要求显式指定）。
    knowledge_base_id 与 knowledge_base_ids 不能同时提供；显式列表（即使 1 个元素）返回 groups。

    返回：topics（语义主题树）、page_count、outline_available、outline_version、stale、wiki_degraded。
    未生成目录时 outline_available=false 且 topics=[]；stale=true 表示目录可能过期。

    何时用：想先了解这个 Wiki 有哪些语义主题、再按主题浏览页面时。
    何时不用：已知页面路径想看链接关系时用 expand_wiki_links；想直接检索内容时用 search_jonex_wiki / search_mix。

    注意：不提供跨知识库连边，也不做同名合并；source/table/concept 仅作为页面过滤条件，不是第一列。"""
    return await tools.list_wiki_topics(
        service_id, knowledge_base_id, knowledge_base_ids
    )


@mcp.tool()
async def list_wiki_pages(
    service_id: str,
    knowledge_base_id: str = "",
    knowledge_base_ids: Any = None,
    topic: str = "",
    type: str = "",
    page: Annotated[Any, WithJsonSchema(_WIKI_PAGE_SCHEMA)] = 1,
    page_size: Annotated[Any, WithJsonSchema(_WIKI_PAGE_SIZE_SCHEMA)] = 20,
) -> dict:
    """分页浏览 Jonex Wiki 知识库的页面列表，是 Jonex Wiki 列式浏览的第二列。

    仅支持 Jonex Wiki 知识库。可按 list_wiki_topics 返回的主题 id 过滤（含其子节点），
    也可用 type 做内部过滤（source / table / concept / article，仅作过滤、不作为第一列暴露）。
    想浏览本体（lightrag）知识库请用 list_ontology_entity_types / list_ontology_instances。
    知识库选择（二选一）：
    - knowledge_base_id：指定单个知识库。
    - knowledge_base_ids：字符串数组，指定多个知识库，单次聚合并按库分组。
    - 两者都不传：查询该服务下全部 Jonex Wiki 知识库。
    knowledge_base_id 与 knowledge_base_ids 不能同时提供；显式列表（即使 1 个元素）返回 groups。

    返回：单库 items/total/page/page_size/outline_available/stale/wiki_degraded；
    多库只返回 {total, groups}，没有顶层 items/page/page_size。topic 不存在或该库未生成目录 → -32602。

    何时用：已确定主题，想列出该主题下的页面时。
    何时不用：还不知道主题时先用 list_wiki_topics；已知具体页面想沿链接展开时用 expand_wiki_links。

    注意：多库时 page/page_size 对每个库独立生效；不跨库合并同名页面。"""
    return await tools.list_wiki_pages(
        service_id, knowledge_base_id, knowledge_base_ids,
        topic, type, page, page_size,
    )


@mcp.tool()
async def read_wiki_page(service_id: str, knowledge_base_id: str, path: str,
                         offset: int = 0, max_chars: int = 12000) -> dict:
    """按页读取已授权的 Wiki 文章正文；长正文通过 next_offset 继续读取。

    先用 list_wiki_pages 取得 canonical path；返回的引用只表示文章链接的来源，
    不表示每句话都已核验。每次最多 12000 个字符。
    """
    return await tools.read_wiki_page(service_id, knowledge_base_id, path, offset, max_chars)


@mcp.tool()
async def expand_wiki_links(
    service_id: str,
    knowledge_base_id: str = "",
    knowledge_base_ids: Any = None,
    path: str = "",
    depth: Annotated[Any, WithJsonSchema(_WIKI_DEPTH_SCHEMA)] = 1,
    limit: Annotated[Any, WithJsonSchema(_WIKI_LINKS_LIMIT_SCHEMA)] = 50,
) -> dict:
    """沿 Jonex Wiki 页面的出链做多跳展开，并附一跳链入，是 Jonex Wiki 列式浏览的第三列。

    仅支持 Jonex Wiki 知识库。path 是起点页面 canonical path（兼容旧口径
    summaries/、entities/、concepts/ 前缀，会自动补 .md）。
    想浏览本体（lightrag）知识库的邻域请用 expand_ontology_neighbors。
    知识库选择（二选一）：
    - knowledge_base_id：指定单个知识库。
    - knowledge_base_ids：字符串数组，指定多个知识库，单次聚合并按库分组。
    - 两者都不传：查询该服务下全部 Jonex Wiki 知识库。
    knowledge_base_id 与 knowledge_base_ids 不能同时提供；显式列表（即使 1 个元素）返回 groups。

    返回：pages（含 hop，起点 hop=0）、links（含 hop）、start_found、depth、limit、returned、
    truncated、truncated_reason、wiki_degraded。每个返回页附 backlinks / backlink_total /
    backlinks_truncated。depth=1 不含起点，只返回一跳目标页。
    链入只来自同一张语义图的反向边，**可能被截断**（最多 30 条），不能当成「全部链入」；
    正文提及型链入请用既有 get_wiki_backlinks 能力。source 页也在这张语义图里。

    何时用：已知一个页面，想看清它的出链 1–3 跳范围，或需要引用它的链入页面时。
    何时不用：还没有具体页面路径时先用 list_wiki_pages；只想检索内容时用 search_jonex_wiki。

    注意：BFS 只沿出链；每节点每层最多 20 条、总边数受 limit 限制，被截断时 truncated=true
    （truncated_reason 含 limit / fanout / source_pages / source_links）。多库时每组独立展开，
    不提供跨库连边，也没有顶层 pages/links。"""
    return await tools.expand_wiki_links(
        service_id, knowledge_base_id, knowledge_base_ids, path, depth, limit
    )


@mcp.tool()
async def list_wiki_claims(
    service_id: str,
    knowledge_base_id: str = "",
    knowledge_base_ids: Any = None,
    path: str = "",
    document_id: str = "",
    limit: Annotated[Any, WithJsonSchema(_WIKI_CLAIMS_LIMIT_SCHEMA)] = 200,
) -> dict:
    """读取 Jonex Wiki 页面/文档的 claim 级溯源明细，是 Jonex Wiki 列式浏览的第四列。

    仅支持 Jonex Wiki 知识库。想浏览本体（lightrag）知识库请用 list_ontology_entity_types / list_ontology_instances。
    path 与 document_id 至少提供一个：
    - path：页面 canonical path；
    - document_id：来源文档 id。
    知识库选择（二选一）：
    - knowledge_base_id：指定单个知识库。
    - knowledge_base_ids：字符串数组，指定多个知识库，单次聚合并按库分组。
    - 两者都不传：查询该服务下全部 Jonex Wiki 知识库。
    knowledge_base_id 与 knowledge_base_ids 不能同时提供；显式列表（即使 1 个元素）返回 groups。

    返回：items（每条 text 已截断 400）、total（截断前总数）、returned、limit、
    truncated（total > returned）、text_limit=400、wiki_degraded。多库只返回 groups。

    何时用：需要核对某页/某文档里的断言原文与来源 chunk 时。
    何时不用：想浏览页面结构时用 list_wiki_topics / list_wiki_pages；想沿链接探索时用 expand_wiki_links。

    注意：不提供跨知识库连边；limit 上限 500。"""
    return await tools.list_wiki_claims(
        service_id, knowledge_base_id, knowledge_base_ids,
        path, document_id, limit,
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
    service_id: str, query: str, with_reasoning: bool = False,
    thinking_mode: str = "fast",
) -> dict:
    """Jonex Wiki 知识检索——从 jonex_wiki 编译的 Wiki 知识库中检索。
    仅适用于 jonex_wiki 管线知识库；不确定管线类型时用 search_mix。
    检索模式固定为 hybrid、top_k=5，不提供 mode / top_k 参数。
    thinking_mode：fast（默认，检索决策与最终回答都不思考）/ standard（检索决策允许思考、最终回答不思考）/ deep（两阶段都允许思考）。不改变命中数、工具调用数、时间预算，且取决于上游模型能力。
    不支持严格模式（jonex_wiki 编译产物无严格模式语义）。
    支持 with_reasoning 推理链追踪（默认 False），返回 wiki 浏览轨迹。
    响应顶层含 request_id（本次检索的反馈锚点，回退时取最终成功那次）；MCP 检索不写入搜索历史。"""
    return await tools.search_jonex_wiki(service_id, query, with_reasoning, thinking_mode)


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


@mcp.tool()
async def list_data_sources(service_id: str, knowledge_base_id: str = "") -> dict:
    """列出服务下各知识库的数据源，并标出哪些能被 ask_data 查询。

    结构化数据（数据库）问数前的第一步：只有 access_type == "database"
    的源（is_queryable=true）能被 ask_data 查询，其余是文件/推送/对象存储源。
    返回 {items: [{knowledge_base_id, id, name, access_type, status,
    document_count, is_queryable}], total}；个别知识库失败时附 kb_errors，
    不整体失败。knowledge_base_id 可选（必须属于该服务）。"""
    return await tools.list_data_sources(service_id, knowledge_base_id)


@mcp.tool()
async def ask_data(
    service_id: str,
    question: str,
    knowledge_base_id: str = "",
    data_source_id: str = "",
    entity_name: str = "",
    offset: int = 0,
) -> dict:
    """按问题查结构化数据（NL→SQL）：把一个自然语言问题落到某个数据库数据源
    的某张表上，生成并执行一条只读 SQL，返回表格结果。

    适用场景：问题要的是「精确的数字/清单/统计」（有多少、有哪些、求和、
    分组计数），而不是文档里的描述性答案。先用 list_data_sources 看哪些源
    可查（is_queryable=true），必要时用 get_schema 确认表名列名。

    成功返回：{ok: true, sql, columns, rows, row_count, truncated, offset,
    next_offset, contract_version, blocks, data_source_id, source_name,
    entity_name, knowledge_base_id}。blocks 是表格块，可直接展示。

    ⚠ ok=false 是正常结果（这个源这次答不了），不是工具错误：reason_code
    取值 no_candidate / ambiguous / entity_gone / no_readonly_creds /
    not_a_database_source / source_gone / not_answerable /
    rejected_unsafe_sql / rejected_sensitive —— 这些重试没有意义；
    只有 llm_failed / exec_failed 值得重试。
    truncated=true 表示行数上限被平台加上或收紧过（不是结果被截断）。
    data_source_id / entity_name 可选，用于点名；缺省由候选匹配决定。"""
    return await tools.ask_data(
        service_id, question,
        knowledge_base_id=knowledge_base_id,
        data_source_id=data_source_id,
        entity_name=entity_name,
        offset=offset,
    )


@mcp.tool()
async def get_schema(
    service_id: str,
    knowledge_base_id: str = "",
    data_source_id: str = "",
    detail: str = "names",
) -> dict:
    """读取编译 schema（表 / 列 / 关系 / 库口径），用于选择 ask_data 的目标。

    detail="names"（默认）只给实体名 + 物理表名 + 列名 + 关系端点，体量小；
    detail="full" 另带类型、描述、样例与 constraints（库口径，例如软删标记
    取值约定），问数结果不符合预期时可据此确认口径。

    ⚠ 两种粒度：带 data_source_id 才是 ask_data 实际使用的那份 schema；
    不带则是知识库级（若该 KB 有抽象 schema，返回的是抽象投影）。返回项用
    is_data_source_level 标明，source_type 也会一并给出，两者不要混用。
    返回 {items: [schema...], total, detail}；个别知识库失败附 kb_errors。"""
    return await tools.get_schema(
        service_id,
        knowledge_base_id=knowledge_base_id,
        data_source_id=data_source_id,
        detail=detail,
    )


@mcp.tool()
async def ask_data_multi(
    service_id: str,
    query: str,
    knowledge_base_ids: list[str] | None = None,
) -> dict:
    """跨数据源问数：一次调用把问题扇出到多个库/源，返回**并列**的数据块。

    与 ask_data 的差别：ask_data 是「一问一源出一条 SQL」，本工具是「一问多源
    各出一块」，上游按源并行（每源都有 data_source_id / source_name / text /
    table / sql / row_count）。**不做跨源汇总**——不同源口径不同，合成一个数
    会把口径差异抹成确定的错，聚合由调用方按块自行判断。

    knowledge_base_ids 可选（必须都属于该服务；缺省 = 该服务下全部知识库）。
    返回原样透传上游 data_blocks，并补 knowledge_base_ids / block_count /
    answered_count / error_count。全部块都失败也仍是成功返回。"""
    return await tools.ask_data_multi(
        service_id, query, knowledge_base_ids=knowledge_base_ids
    )


@mcp.tool()
async def ask_metric(
    service_id: str,
    question: str,
    knowledge_base_id: str = "",
    data_source_id: str = "",
    group_by: list[str] | None = None,
    time_grain: str = "",
    time_from: str = "",
    time_to: str = "",
    limit: int = 0,
    offset: int = 0,
) -> dict:
    """按**已登记指标**确定性问数（不猜意图、不调 LLM），结果可溯源到表/列。

    只认知识库里登记过、且有可执行定义与实体落点的指标；命中即用指标自带的
    确定性 SQL。group_by / time_grain 必须由调用方显式给——上游不猜，猜错比
    不说更糟。指标在多个数据源有落点时用 data_source_id 点名（否则回
    ambiguous_source）。

    成功返回 {ok: true, mode: "metric", metric, sql, columns, rows, row_count,
    truncated, offset, provenance, knowledge_base_id}。

    ⚠ ok=false 是正常业务答复，不是工具错误：reason_code 常见
    no_metric（该库没登记指标，先去登记，重试无用）/ ambiguous_metric /
    ambiguous_source / ambiguous_landing（需点名，可能带 candidates、clarifications）/
    no_landing / metric_not_approved / compile_error / rejected_unsafe_sql /
    rejected_sensitive / exec_failed（后两类值得重试）。"""
    return await tools.ask_metric(
        service_id, question,
        knowledge_base_id=knowledge_base_id,
        data_source_id=data_source_id,
        group_by=group_by,
        time_grain=time_grain,
        time_from=time_from,
        time_to=time_to,
        limit=limit,
        offset=offset,
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
