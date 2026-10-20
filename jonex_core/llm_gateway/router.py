# -*- coding:utf-8 -*-
"""
LLM 网关路由：OpenAI 兼容端点 + 计量查询。
"""

import asyncio
import time
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from jonex_core.common.config import get_config
from jonex_core.common.monitor_collector import (
    classify_trigger_by_scene,
    get_monitor_collector,
)
from jonex_core.common.monitor_models import (
    CallDirection,
    CallFinalStatus,
    Protocol,
)
from jonex_core.llm_gateway.context import MeteringContext, derive_request_id, parse_ctx
from jonex_core.llm_gateway.metering import (
    extract_usage_chat,
    extract_usage_embedding,
    extract_usage_rerank,
    extract_usage_stream,
)
from jonex_core.llm_gateway.recorder import UsageRecord, get_recorder
from jonex_core.llm_gateway.rerank_profiles import get_profile
from jonex_core.llm_gateway.upstream import (
    UpstreamError,
    proxy_nonstream,
    proxy_rerank,
    proxy_stream,
)

logger = logging.getLogger("llm_gateway")

router = APIRouter()


def _check_quota(ctx: MeteringContext) -> bool:
    """配额检查预留钩子（G5.2），默认放行。"""
    cfg = get_config()
    if not cfg.LLMGW_QUOTA_ENABLED:
        return True
    # TODO(G5a): 按 tenant/scene 日 token 上限检查，超额返回 429
    return True


@router.get("/health")
async def health():
    """健康检查"""
    return {"status": "ok", "service": "llm-gateway"}


@router.post("/v1/chat/completions")
async def chat_completions(request: Request, ctx: MeteringContext = Depends(parse_ctx)):
    """OpenAI 兼容 chat completions（支持流式与非流式）"""
    if not _check_quota(ctx):
        return JSONResponse({"error": "quota_exceeded", "message": "配额超限"}, status_code=429)
    body = await request.json()
    model = body.get("model", "unknown")
    stream = body.get("stream", False)

    logger.info(
        "chat 请求 | req_id=%s tenant=%s scene=%s model=%s stream=%s",
        ctx.request_id, ctx.tenant_id, ctx.scene, model, stream,
    )

    if stream:
        return await _stream_chat(request, body, ctx, model)

    # 非流式转发
    data, status_code, latency_ms = await proxy_nonstream(request, body, ctx)
    usage = extract_usage_chat(data)

    logger.info(
        "chat 完成 | req_id=%s model=%s status=%s latency_ms=%s usage=%s",
        ctx.request_id, model, status_code, latency_ms, usage,
    )

    # 计量记录
    _record_usage(
        ctx, model, usage, stream=False, estimated=(usage is None),
        latency_ms=latency_ms, status_code=status_code, body=body, response=data,
    )

    return JSONResponse(data, status_code=status_code)


async def _stream_chat(
    request: Request,
    body: dict,
    ctx: MeteringContext,
    model: str,
):
    """流式 chat：边转发边缓存，结束后取 usage 计量"""
    t0 = time.monotonic()
    chunks: list[bytes] = []
    usage = None

    async def generate():
        nonlocal usage
        try:
            async for chunk in proxy_stream(request, body, ctx):
                chunks.append(chunk)
                yield chunk
        except UpstreamError as exc:
            # 上游首字节前报错（旧行为是安静 return ⇒ 被记成成功）。记 failed 后**重新抛出**，
            # 让流中断、客户端能看出失败 —— 与「网络断线」路径早已在跑的行为一致。
            latency_ms = int((time.monotonic() - t0) * 1000)
            usage = extract_usage_stream(chunks)  # 多半为 None ⇒ estimated=True
            logger.warning(
                "chat 流式失败 | req_id=%s model=%s status=%s chunks=%s latency_ms=%s err=%s",
                ctx.request_id, model, exc.status_code, len(chunks), latency_ms, exc.message,
            )
            _record_usage(
                ctx, model, usage, stream=True, estimated=(usage is None),
                latency_ms=latency_ms, status_code=exc.status_code,
                body=body, error_message=exc.message,
            )
            raise
        except asyncio.CancelledError:
            # 客户端断开：记 cancelled 后继续传播取消信号（中途取消也要留痕）
            latency_ms = int((time.monotonic() - t0) * 1000)
            usage = extract_usage_stream(chunks)
            logger.warning(
                "chat 流式取消 | req_id=%s model=%s chunks=%s latency_ms=%s",
                ctx.request_id, model, len(chunks), latency_ms,
            )
            _record_usage(
                ctx, model, usage, stream=True, estimated=(usage is None),
                latency_ms=latency_ms, status_code=200,
                final_status=CallFinalStatus.CANCELLED.value, body=body,
            )
            raise
        # 流结束后提取 usage
        latency_ms = int((time.monotonic() - t0) * 1000)
        usage = extract_usage_stream(chunks)
        logger.info(
            "chat 流式完成 | req_id=%s model=%s chunks=%s latency_ms=%s usage=%s",
            ctx.request_id, model, len(chunks), latency_ms, usage,
        )
        _record_usage(
            ctx, model, usage, stream=True, estimated=(usage is None),
            latency_ms=latency_ms, status_code=200, body=body,
        )

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/v1/embeddings")
async def embeddings(request: Request, ctx: MeteringContext = Depends(parse_ctx)):
    """OpenAI 兼容 embedding"""
    if not _check_quota(ctx):
        return JSONResponse({"error": "quota_exceeded", "message": "配额超限"}, status_code=429)
    body = await request.json()
    model = body.get("model", "unknown")

    logger.info(
        "embedding 请求 | req_id=%s tenant=%s scene=%s model=%s",
        ctx.request_id, ctx.tenant_id, ctx.scene, model,
    )

    data, status_code, latency_ms = await proxy_nonstream(request, body, ctx)
    usage = extract_usage_embedding(data, body)

    logger.info(
        "embedding 完成 | req_id=%s model=%s status=%s latency_ms=%s usage=%s",
        ctx.request_id, model, status_code, latency_ms, usage,
    )

    _record_usage(
        ctx, model, usage, stream=False,
        estimated=("prompt_tokens" not in data.get("usage", {})),
        latency_ms=latency_ms, status_code=status_code, body=body, response=data,
    )

    return JSONResponse(data, status_code=status_code)


@router.post("/v1/rerank")
async def rerank(request: Request, ctx: MeteringContext = Depends(parse_ctx)):
    """Cohere 风格重排：{model, query, documents[], top_n} → {results:[{index, relevance_score}]}"""
    if not _check_quota(ctx):
        return JSONResponse({"error": "quota_exceeded", "message": "配额超限"}, status_code=429)
    cfg = get_config()
    body = await request.json()
    model = body.get("model") or cfg.LLMGW_RERANK_MODEL
    binding = cfg.LLMGW_RERANK_BINDING

    logger.info(
        "rerank 请求 | req_id=%s tenant=%s scene=%s model=%s binding=%s docs=%d",
        ctx.request_id, ctx.tenant_id, ctx.scene, model, binding,
        len(body.get("documents") or []),
    )

    data, status_code, latency_ms = await proxy_rerank(request, body, ctx)

    # [jonex] 2026-09-22：原「检索期 rerank 命中」的 Redis 标记已删除——它的唯一读者
    # 是 kb-service 旧链路的命中检测函数，二者随本次清理一并移除。留着只是让每次
    # 非平台 rerank 白写一次 Redis（TTL 120s）。

    # 计量：ollama-generate 需按 N 次带模板调用估算，传入模板固定开销字符数
    template_overhead = 0
    if binding == "ollama-generate":
        prof = get_profile(cfg.LLMGW_RERANK_PROMPT_PROFILE)
        template_overhead = len(prof.prefix) + len(prof.instruct) + len(prof.suffix)
    usage = extract_usage_rerank(data, body, binding, template_overhead)

    logger.info(
        "rerank 完成 | req_id=%s model=%s status=%s latency_ms=%s usage=%s",
        ctx.request_id, model, status_code, latency_ms, usage,
    )

    _record_usage(ctx, model, usage, stream=False, estimated=True,
                  latency_ms=latency_ms, status_code=status_code, body=body, response=data)

    return JSONResponse(data, status_code=status_code)


@router.get("/metering/usage")
async def metering_usage(
    request: Request,
    tenant_id: str = Query(..., description="租户 ID"),
    start: str = Query(..., description="开始时间 ISO 格式"),
    end: str = Query(..., description="结束时间 ISO 格式"),
    scene: Optional[str] = Query(None, description="按 scene 过滤"),
    ctx: MeteringContext = Depends(parse_ctx),
):
    """计量查询：按 tenant_id + 时间范围聚合 PG 明细"""
    # 按 tenant 过滤防止越权
    if ctx.tenant_id != tenant_id:
        return JSONResponse(
            {"error": "forbidden", "message": "只能查询本租户用量"},
            status_code=403,
        )

    # 查询 PG
    try:
        from jonex_core.common.database import get_db_session
        from sqlalchemy import text

        async with get_db_session() as session:
            filters = [
                "tenant_id = :tenant_id",
                "created_at >= :start",
                "created_at <= :end",
            ]
            params = {"tenant_id": tenant_id, "start": start, "end": end}
            if scene:
                filters.append("scene = :scene")
                params["scene"] = scene

            where = " AND ".join(filters)
            stmt = text(f"""
                SELECT
                    scene, model, kb_id,
                    SUM(call_count) AS call_count,
                    SUM(prompt_tokens) AS total_prompt,
                    SUM(completion_tokens) AS total_completion,
                    SUM(total_tokens) AS total_tokens,
                    AVG(latency_ms) AS avg_latency_ms
                FROM metering.llm_usage_log
                WHERE {where}
                GROUP BY scene, model, kb_id
                ORDER BY total_tokens DESC
            """)
            result = await session.execute(stmt, params)
            rows = result.fetchall()
            return JSONResponse({
                "tenant_id": tenant_id,
                "start": start,
                "end": end,
                "rows": [dict(r._mapping) for r in rows],
            })
    except Exception as e:
        return JSONResponse(
            {"error": "query_failed", "message": str(e)},
            status_code=500,
        )


def _error_text_from_response(response: Any) -> Optional[str]:
    """从上游返回体里取错误文本（脱敏由采集侧 ``_sanitize_error_message`` 做）。"""
    if not isinstance(response, dict):
        return None
    err = response.get("error")
    if isinstance(err, dict):
        return err.get("message") or err.get("type") or None
    if err is not None:
        return str(err)
    return None


def _collect_monitor_n1_outbound(
    *,
    ctx: MeteringContext,
    model: str,
    request_id: str,
    latency_ms: int,
    final_status: str,
    error_code: Optional[str] = None,
    error_message: Optional[Any] = None,
) -> None:
    """投递一条 llm-gateway **出站** N1（``monitor.call_log``），**吞掉一切异常**。

    拼行范式照抄 ``sidecar/main.py`` 的 ``_monitor_n1_fields`` / ``_collect_monitor_n1``。
    两处刻意的取值：

    - ``direction=outbound`` —— 记录的是「网关**发出**的调用」，与 Sidecar 的
      ``inbound``（进入能力）相对。
    - ``source`` = 用户身份，拿不到时记 ``platform``（表示平台自身发起，不是某个用户）。

    ``tenant_id`` 直接取 ``ctx.tenant_id``：非法（空 / ``unknown`` 等）时整条会被
    采集侧丢弃 —— **既定行为，不造假租户兜底**。
    """
    try:
        ended = datetime.now(timezone.utc)
        failed = final_status == CallFinalStatus.FAILED.value
        get_monitor_collector().put_call_log({
            "request_id": request_id,
            "trace_id": ctx.trace_id,
            "tenant_id": ctx.tenant_id,
            "direction": CallDirection.OUTBOUND.value,
            "source": ctx.user_id or "platform",
            "target": model,
            "protocol": Protocol.OPENAI.value,
            "trigger_type": classify_trigger_by_scene(ctx.scene),
            # DDL 是 TIMESTAMPTZ ⇒ 必须 tz-aware（朴素值会踩 asyncpg 编解码坑）
            "logical_started_at": ended - timedelta(milliseconds=latency_ms),
            "final_ended_at": ended,
            "final_status": final_status,
            "error_code": str(error_code) if (failed and error_code is not None) else None,
            "error_message": error_message if failed else None,
        })
    except Exception:
        logger.exception("[llm-gateway] 监控采集投递失败（已吞异常，不影响调用）")


def _record_usage(
    ctx: MeteringContext,
    model: str,
    usage: dict | None,
    stream: bool,
    estimated: bool,
    latency_ms: int,
    status_code: int = 200,
    body: dict | None = None,
    response: Any = None,
    final_status: Optional[str] = None,
    error_message: Optional[Any] = None,
):
    """统一计量记录入口（同时投递一条出站 N1 调用明细）。"""
    prompt_tokens = (usage or {}).get("prompt_tokens", 0) or 0
    completion_tokens = (usage or {}).get("completion_tokens", 0) or 0
    total_tokens = (usage or {}).get("total_tokens", 0) or (prompt_tokens + completion_tokens)

    # 幂等键：显式透传优先，否则按 trace_id + 请求体哈希确定性派生
    request_id = derive_request_id(ctx, body or {})

    rec = UsageRecord(
        ts=time.time(),
        tenant_id=ctx.tenant_id,
        user_id=ctx.user_id,
        scene=ctx.scene,
        model=model,
        kb_id=ctx.kb_id,
        doc_id=ctx.doc_id,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        stream=stream,
        estimated=estimated,
        request_id=request_id,
        latency_ms=latency_ms,
        trace_id=ctx.trace_id,
    )
    recorder = get_recorder()
    asyncio.create_task(recorder.record(rec))

    # 出站 N1 调用明细（旁路；投递同步非阻塞、吞异常，不影响计量与业务）
    if final_status is None:
        final_status = (
            CallFinalStatus.SUCCESS.value if status_code < 400 else CallFinalStatus.FAILED.value
        )
    failed = final_status == CallFinalStatus.FAILED.value
    if failed and error_message is None:
        error_message = _error_text_from_response(response)
    _collect_monitor_n1_outbound(
        ctx=ctx,
        model=model,
        request_id=request_id,
        latency_ms=latency_ms,
        final_status=final_status,
        error_code=str(status_code) if failed else None,
        error_message=error_message,
    )