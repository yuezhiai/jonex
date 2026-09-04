# [jonex] Jonex新增文件：LLM/Embedding 计量上下文透传
#
# 作用：让 LightRAG 内部发起的 LLM/Embedding 调用带上 X-Jonex-* 头，
#       使 llm-gateway 能记录 tenant/kb/doc/scene/trace 维度。
#
# 两条透传链路（见 JONEX_CHANGES.md）：
#   1) 在线查询（/query、/query/stream）：同步 HTTP 请求 —— 用本模块的 contextvar，
#      由 server 中间件从请求头读 X-Jonex-* 存入，LLM/embedding 调用时读出注入。
#   2) 入库抽取（后台 pipeline）：异步 task，contextvar 跨 task 失效 ——
#      改用数据载体 file_source（kb=|doc=|tenant=|...）透传，见 parse_file_source。
#
# 注意：所有改动以 # [jonex] 标记，便于升级 LightRAG 时定位。

from contextvars import ContextVar, Token
from typing import Optional

# 当前请求的计量上下文（仅在线查询路径有效）
_jonex_ctx: ContextVar[Optional[dict]] = ContextVar("jonex_metering_ctx", default=None)


def set_jonex_context_from_headers(headers) -> Token:
    """从 HTTP 请求头提取 X-Jonex-* 存入 contextvar，返回 reset token。

    headers: 支持 .get() 的对象（FastAPI/Starlette Headers）。
    """
    ctx = {
        "tenant_id": (headers.get("X-Jonex-Tenant-Id") or "").strip(),
        "kb_id": (headers.get("X-Jonex-Kb-Id") or "").strip(),
        "doc_id": (headers.get("X-Jonex-Doc-Id") or "").strip(),
        "user_id": (headers.get("X-Jonex-User-Id") or "").strip(),
        "scene": (headers.get("X-Jonex-Scene") or "").strip(),
        "trace_id": (
            headers.get("X-Jonex-Trace-Id") or headers.get("X-Request-ID") or ""
        ).strip(),
    }
    return _jonex_ctx.set(ctx)


def set_jonex_context_from_file_source(file_source: Optional[str]) -> Optional[Token]:
    """[jonex] 入库 pipeline 专用：从 file_source 解析维度并写入 contextvar。

    背景：入库 embedding 在后台 pipeline 的独立 asyncio task 中执行，
    既拿不到请求级 contextvar，调用链也不携带 file_source，导致计量
    tenant/kb/doc 全空（JONEX_CHANGES.md 已知约束）。

    解法：在 LightRAG `process_document` 入口（已解析出携带
    kb=|doc=|tenant= 的 file_path）调用本函数写入 contextvar。由于
    contextvar 会在 `asyncio.create_task` 时被复制进子 task，之后创建的
    embedding upsert 子 task（chunks_vdb / entities_vdb / relationships_vdb）
    便能读到维度。又因每个文档跑在各自独立 task、context 互不影响，
    并发入库不会串租户。

    仅当 file_source 解析出 tenant/kb/doc 任一维度时才设置，避免用
    "unknown_source" 这类裸路径污染。无可用维度返回 None（调用方据此
    决定是否需要 reset）。
    """
    fs = parse_file_source(file_source)
    if not (fs.get("tenant_id") or fs.get("kb_id") or fs.get("doc_id")):
        return None
    ctx = {
        "tenant_id": fs.get("tenant_id", ""),
        "kb_id": fs.get("kb_id", ""),
        "doc_id": fs.get("doc_id", ""),
        "user_id": "",
        # scene 设为 lightrag_extract：入库期间走 contextvar 的 LLM 调用（如
        # merge/summary，未带 _jonex_file_source）据此正确归类为抽取场景，
        # 避免落到 LLM 路径的 default_scene=lightrag_query 而污染查询统计。
        # embedding 调用方（openai_embed）会再用 lightrag_embed 覆盖；带
        # file_source 的抽取 LLM 走 file_source 分支（同为 lightrag_extract），均不冲突。
        "scene": "lightrag_extract",
        "trace_id": fs.get("trace_id", ""),
    }
    return _jonex_ctx.set(ctx)


def clear_jonex_context(token: Token) -> None:
    """请求结束时还原 contextvar，避免污染同一事件循环的其他任务。"""
    try:
        _jonex_ctx.reset(token)
    except (ValueError, LookupError):
        # 跨 task reset 可能失败，忽略
        pass


def parse_file_source(file_source: Optional[str]) -> dict:
    """解析入库数据载体 file_source（kb=|doc=|tenant=|file=|chunk=）为维度 dict。"""
    if not file_source:
        return {}
    parts = dict(p.split("=", 1) for p in file_source.split("|") if "=" in p)
    return {
        "tenant_id": parts.get("tenant", ""),
        "kb_id": parts.get("kb", ""),
        "doc_id": parts.get("doc", ""),
        "trace_id": parts.get("trace", ""),
    }


def build_metering_headers(
    file_source: Optional[str] = None,
    default_scene: str = "lightrag",
    force_scene: Optional[str] = None,
) -> Optional[dict]:
    """构造注入下游（llm-gateway）的 X-Jonex-* 头。

    优先级：入库 file_source（若提供）> 在线查询 contextvar。
    - file_source 命中：scene=lightrag_extract（入库抽取）。
    - contextvar 命中：scene 取请求头透传值，缺省用 default_scene。
    - force_scene 非空时：无视以上所有规则，强制使用 force_scene
      （用于 rerank 等与查询 LLM 同 task 树但需要独立 scene 分类的场景）。
    - 都没有：返回 None（不注入，gateway 走 auto 兜底）。
    """
    dims = {}
    scene = ""

    fs = parse_file_source(file_source)
    if fs and (fs.get("tenant_id") or fs.get("kb_id") or fs.get("doc_id")):
        dims = fs
        scene = "lightrag_extract"
    else:
        ctx = _jonex_ctx.get()
        if ctx:
            dims = ctx
            scene = ctx.get("scene") or default_scene

    if force_scene:
        scene = force_scene

    if not dims:
        return None

    headers = {}
    if dims.get("tenant_id"):
        headers["X-Jonex-Tenant-Id"] = dims["tenant_id"]
    if dims.get("kb_id"):
        headers["X-Jonex-Kb-Id"] = dims["kb_id"]
    if dims.get("doc_id"):
        headers["X-Jonex-Doc-Id"] = dims["doc_id"]
    if dims.get("user_id"):
        headers["X-Jonex-User-Id"] = dims["user_id"]
    if dims.get("trace_id"):
        headers["X-Jonex-Trace-Id"] = dims["trace_id"]
    if scene:
        headers["X-Jonex-Scene"] = scene

    return headers or None
