# [jonex] Jonex新增文件：raganything 入库计量上下文透传
#
# 作用：让 raganything 直连 llm-gateway 的多模态/summary LLM 与 embedding 调用
#       带上 X-Jonex-* 维度头（tenant/kb/doc/trace），使 gateway 能按文档/任务
#       归因计量。
#
# 设计：参照 LightRAG jonex_metering.py 的 contextvar 模式。
#   - 任务入口 _execute_pipeline_http 调用 set_ingest_ctx() 存入维度；
#   - driver（OpenAI/Anthropic）的 HTTP 调用处通过 build_ingest_headers()
#     读取 contextvar 并叠加到请求头；
#   - _metered_llm/_metered_embedding fallback 闭包通过 _build_metering_headers
#     （model_factory.py）间接读取 contextvar；
#   - 任务结束 finally 中 reset，避免污染下一任务。
#
# contextvar 在 asyncio.create_task 时自动复制进子 task，因此 raganything
# 后台并发子任务能读到各自任务维度；每任务独立 context，并发入库不串租户。
#
# 注意：所有改动以 # [jonex] 标记，便于升级 raganything 时定位。

from contextvars import ContextVar, Token
from typing import Optional

_ingest_ctx: ContextVar[Optional[dict]] = ContextVar("jonex_ingest_ctx", default=None)


def set_ingest_ctx(
    tenant_id: str = "",
    kb_id: str = "",
    doc_id: str = "",
    trace_id: str = "",
) -> Token:
    """在任务入口设置当前入库任务的计量维度上下文。

    调用方应在 finally 中用返回的 Token 调用 reset_ingest_ctx。
    """
    return _ingest_ctx.set({
        "tenant_id": tenant_id or "",
        "kb_id": kb_id or "",
        "doc_id": doc_id or "",
        "trace_id": trace_id or "",
    })


def get_ingest_ctx() -> Optional[dict]:
    """读取当前任务的计量维度上下文；无上下文返回 None。"""
    return _ingest_ctx.get()


def reset_ingest_ctx(token: Token) -> None:
    """还原 contextvar，避免污染同一事件循环的其他任务。"""
    try:
        _ingest_ctx.reset(token)
    except (ValueError, LookupError):
        # 跨 task reset 可能失败，忽略
        pass


def build_ingest_headers() -> dict:
    """从 ingest contextvar 构造 X-Jonex-* 头，供 driver HTTP 调用叠加。

    Returns:
        dict: 非空维度对应的 X-Jonex-* 头字典；无上下文返回空 dict。
        scene 固定为 raganything_ingest。
    """
    ctx = _ingest_ctx.get()
    if not ctx:
        return {}
    headers: dict[str, str] = {"X-Jonex-Scene": "raganything_ingest"}
    if ctx.get("tenant_id"):
        headers["X-Jonex-Tenant-Id"] = ctx["tenant_id"]
    if ctx.get("kb_id"):
        headers["X-Jonex-Kb-Id"] = ctx["kb_id"]
    if ctx.get("doc_id"):
        headers["X-Jonex-Doc-Id"] = ctx["doc_id"]
    if ctx.get("trace_id"):
        headers["X-Jonex-Trace-Id"] = ctx["trace_id"]
    return headers
