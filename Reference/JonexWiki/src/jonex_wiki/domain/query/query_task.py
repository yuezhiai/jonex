"""core 引擎 query 分支接线（§6.3）。

旧 dev 的 BundleReader/search/synthesize 同步查询替换为后台异步：
resolve_published 锁 bundle_sha → ArtifactStoreAdapter.open_revision →
LLMWikiV1Engine.query → query_tasks.result。
"""
import logging
import time
from typing import Any

from jonex_wiki.adapters.artifact_store import ArtifactStoreAdapter
from jonex_wiki.adapters.bundle_view import read_revision_header
from jonex_wiki.adapters.cancel import CancelPortAdapter
from jonex_wiki.adapters.model_port import ModelPortAdapter
from jonex_wiki.domain.engine.compile.engine import LLMWikiV1Engine
from jonex_wiki.kernel.errors import NotFoundError
from jonex_wiki.kernel.runtime_env import runtime_environment
from jonex_wiki.storage.layout import Layout
from jonex_wiki.storage.models import QueryTaskRow

log = logging.getLogger(__name__)


def _dump_citations(citations) -> list:
    out = []
    for c in citations:
        if hasattr(c, "model_dump"):
            out.append(c.model_dump(mode="json"))
        elif isinstance(c, dict):
            out.append(c)
        else:
            out.append(str(c))
    return out


# [jonex] 平台侧「检索链路」公开投影（Jonex /search/jonex_wiki 的 reasoning 数据源）。
#
# 原始 QueryRunResult.trace 是内部审计事件流（query_budget_exhausted /
# model_stopped_incomplete ...），detail 可能携带模型中间产物；而平台会把推理链落库
# 并回给前端，所以出网必须经白名单：只放行「检索/阅读轨迹」事件，且只保留公开字段。
# 新增事件必须在此显式登记，未登记的一律丢弃（默认关闭，而不是默认放行）。
_PUBLIC_TRACE_FIELDS = {
    # wiki_search: query + 候选前沿；wiki_read: 实读路径；表格读: 结果计数
    "tool_result": (
        "tool", "query", "paths", "candidate_paths", "frontier_paths",
        "count", "success", "cached", "table_id", "status", "cells", "has_more",
    ),
    "search_rewritten": ("from", "to"),
    "search_constraints_preserved": ("terms", "query"),
    "source_evidence_read": ("paths", "kind"),
    # keep-and-search-v1：claim 未准入的生成页读取时，回挂其原始来源区间
    "evidence_fallback_read": ("paths", "kind", "generated_claim", "spans"),
    "read_repeated": ("paths", "of_step"),
    "chapter_forced_navigation": ("paths", "from", "page_numbers"),
}
_PUBLIC_TRACE_MAX_ENTRIES = 240
_PUBLIC_TRACE_MAX_STRING = 300
_PUBLIC_TRACE_MAX_LIST = 40


def _public_trace_value(value: Any) -> Any:
    """递归截断一个公开字段值，保证出网体积有界。"""
    if isinstance(value, str):
        return value[:_PUBLIC_TRACE_MAX_STRING]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return [_public_trace_value(item) for item in list(value)[:_PUBLIC_TRACE_MAX_LIST]]
    if isinstance(value, dict):
        return {str(key): _public_trace_value(item)
                for key, item in list(value.items())[:20]}
    return str(value)[:_PUBLIC_TRACE_MAX_STRING]


def _dump_public_trace(trace) -> list:
    """把引擎内部 trace 投影成平台可落库的「检索链路」（白名单 + 有界）。"""
    out: list = []
    for entry in trace or []:
        event = str(getattr(entry, "event", "") or "")
        allowed = _PUBLIC_TRACE_FIELDS.get(event)
        if not allowed:
            continue
        detail = getattr(entry, "detail", None)
        if not isinstance(detail, dict):
            detail = {}
        out.append({
            "event": event,
            "detail": {key: _public_trace_value(detail[key])
                       for key in allowed if key in detail},
        })
        if len(out) >= _PUBLIC_TRACE_MAX_ENTRIES:
            break
    return out


def _dump_spans(spans) -> list:
    """CR-TRACE-002 公开 span（tool/model/step/synthesis/evidence）。

    该契约本身保证 input_summary/result_summary/evidence 只含公开信息（不含 prompt、
    凭据、正文全文、CoT），event_type 是内部提示位、序列化前剥掉。
    """
    out: list = []
    for span in spans or []:
        try:
            out.append(span.model_dump(mode="json", exclude={"event_type"}))
        except Exception:  # noqa: BLE001 — span 形状异常不得影响查询结果
            continue
    return out


def create_query_runner(session_factory, settings, layout: Layout, gw, executor=None):
    engine = LLMWikiV1Engine()

    async def runner(row: QueryTaskRow, *, thinking_mode: str | None = None) -> dict:
        start = time.monotonic()
        tenant_id, kb_id = row.tenant_id, row.kb_id
        cancel = CancelPortAdapter(executor, row.query_id) if executor else None

        # 1) 锁 bundle_sha（resolve_published 返回已发布版本目录 realpath）
        published = (layout.versions_dir(tenant_id, kb_id, row.bundle_sha)
                     if row.bundle_sha else layout.resolve_published(tenant_id, kb_id))
        if published is None or not published.is_dir():
            raise NotFoundError(f"知识库 {kb_id} 无已发布 bundle")
        bundle_sha = published.name
        from jonex_wiki.domain.source_visibility import ensure_sources_visible
        async with session_factory() as session:
            await ensure_sources_visible(session, tenant_id, kb_id, published)

        # 2) ports
        store = ArtifactStoreAdapter(layout, tenant_id=tenant_id, kb_id=kb_id)
        model = ModelPortAdapter(gw, scene=None, tenant_id=tenant_id, kb_id=kb_id)

        # 3) revision + request（dict 形状,engine.query 兼容）。
        #    bundle_key 用目录哈希（版本目录名）；revision_id 用 core 内容派生身份
        #    （revision.json.revision_id = rev_*），与 decode_previous_files 一致。
        core_revision_id = bundle_sha
        try:
            core_revision_id = read_revision_header(published).get("revision_id") or bundle_sha
        except (OSError, ValueError):
            pass
        revision = {
            "bundle_key": bundle_sha,
            "context_id": kb_id,
            "tenant_id": tenant_id,
            "id": core_revision_id,
            "revision_id": core_revision_id,
            "status": "active",
        }
        request = {"question": row.question, "strategy": "browse-first"}

        # 4) 核心查询算法；运行时旋钮经 Settings 注入
        environment = settings.runtime_env()
        if thinking_mode is not None:
            from jonex_wiki.domain.query.thinking import thinking_overrides
            environment.update(thinking_overrides(thinking_mode))
        from jonex_wiki.adapters.query_progress import QueryProgressAdapter
        from jonex_wiki.kernel.query_progress import query_progress
        async with QueryProgressAdapter(session_factory, tenant_id, row.query_id) as progress:
            with runtime_environment(environment), query_progress(progress.record):
                result = await engine.query(
                    request, revision, {"store": store, "model": model}, cancel=cancel
                )
        if thinking_mode is not None:
            result.config["thinking_mode"] = thinking_mode

        out = {
            "answer": result.answer,
            "citations": _dump_citations(result.citations),
            "incomplete": result.incomplete,
            "search_calls": result.search_calls,
            "read_calls": result.read_calls,
            "tool_calls": result.tool_calls,
            "model_calls": result.model_calls,
            "stop_reason": result.stop_reason,
            "token_usage": dict(result.token_usage or {}),
            "config": dict(result.config or {}),
            # [jonex] 平台侧检索链路：内部 trace 经白名单投影 + 公开 span 时间线。
            # 两者都在平台侧按需消费（/search/jonex_wiki 的 reasoning / turns）。
            "trace": _dump_public_trace(result.trace),
            "spans": _dump_spans(getattr(result, "spans", None)),
            "request_id": row.query_id,
            "bundle_sha": bundle_sha,
            "latency_ms": int((time.monotonic() - start) * 1000),
        }
        log.info("query done kb=%s sha=%s latency_ms=%d", kb_id, bundle_sha, out["latency_ms"])
        return out

    return runner
