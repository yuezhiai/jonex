# -*- coding:utf-8 -*-
"""
上游路由解析 + httpx 转发（流式/非流式）。
"""

import time
import logging
import asyncio
import math
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import AsyncGenerator

import httpx
from fastapi import Request

from jonex_core.common.config import get_config
from jonex_core.common.monitor_collector import attempt_span
from jonex_core.llm_gateway.context import MeteringContext, derive_request_id
from jonex_core.llm_gateway.rerank_profiles import get_profile

logger = logging.getLogger("llm_gateway")


class UpstreamError(RuntimeError):
    """上游在**首字节前**返回 >=400（流式路径）。

    旧行为是 ``proxy_stream`` 直接 ``return`` —— 流**安静结束**，客户端当正常答完，
    计量侧也照常记成功（失真）。改为抛本异常：调用方据此记 ``failed`` 并让流中断，
    客户端能看到失败 —— 与「网络断线」路径早已在跑的行为一致。

    ``status_code`` 供采集侧填 ``error_code``；``message`` 是截断后的上游响应文本。
    """

    def __init__(self, status_code: int, message: str = ""):
        self.status_code = int(status_code)
        self.message = message or ""
        super().__init__(f"upstream returned {self.status_code}: {self.message}")


def _seal_final(att) -> None:
    """把当前 Attempt 标为「本次逻辑调用的最后一次尝试」。

    ``attempt_span`` 只在**进入**时接收 ``is_final``，而「是否最后一次」必须等上游
    响应到手才知道（成功即终止；失败还要看是否还有重试额度 / 预算），
    故由冻结接口的 ``mark_final()`` 承担（采集是旁路，兜一层异常）。

    """
    try:
        att.mark_final()
    except Exception:  # pragma: no cover - 采集是旁路，绝不冒到主链路
        logger.exception("N2 Attempt is_final 标记失败（已吞异常）")


# 预留：按 model 名细化到多上游的映射表
MODEL_ROUTE_OVERRIDES: dict[str, str] = {}

# [jonex] 这里原先有一张 MODEL_ALIAS_MAP（`{"gpt-5.4": <环境变量名>}`）+
# `_apply_model_alias()`，作用是把入站的 `gpt-5.4` 静默改写成部署里的模型名
# ——垫的是「OpenKB 用 LiteLLM，会把模型名转成 gpt-5.4」那段历史。
#
# 2026-10-08 按决定**整体删除**：模型名应当在 KB 自己的 config.yaml 里显式可见、
# 可审计，而不是由网关在转发前偷偷换掉。隐藏改写还有个副作用——排查「模型名对不上」
# 得同时想到 openkb 层、网关别名表、上游渠道表三层，而日志只显示"映射成功"。
#
# 现在改由 openkb 侧在初始化 KB 时就把**环境变量解析出的模型名**写进 config.yaml
# （见 jonex_core/capability/atomic/openkb/openkb_adapter.py 的三处 initialize_kb）。
# 网关只做一件事：把入站 model 原样转发，认识不认识由上游渠道表决定。
#
# ⚠️ 存量 KB 的 config.yaml 里若仍写着 gpt-5.4，去掉别名后会直接撞上游
# "No available channel" —— 需要把这些 KB 的 model 改写成当前部署的模型名。


def _upstream_path(request_path: str) -> str:
    """将网关请求路径映射到上游 API 路径（不含 /v1，host 基地址已包含）。"""
    if request_path.endswith("/embeddings"):
        return "/embeddings"
    return "/chat/completions"


def resolve_upstream(path: str, body: dict | None = None) -> tuple[str, str]:
    """按路径 + body.model 解析上游 host 和 API key"""
    cfg = get_config()

    if path.endswith("/embeddings"):
        return cfg.LLMGW_UPSTREAM_EMBED_HOST, cfg.LLMGW_UPSTREAM_EMBED_API_KEY

    # chat/completions：优先按 model 路由
    if body and body.get("model"):
        model = body["model"]
        if model in MODEL_ROUTE_OVERRIDES:
            return MODEL_ROUTE_OVERRIDES[model], cfg.LLMGW_UPSTREAM_LLM_API_KEY

    return cfg.LLMGW_UPSTREAM_LLM_HOST, cfg.LLMGW_UPSTREAM_LLM_API_KEY


def upstream_headers(upstream_key: str, request: Request) -> dict[str, str]:
    """构造上游请求头

    - 剥离入站 Authorization / X-API-Key（由 upstream_key 代替）
    - 剥离 X-Jonex-* 上下文头（仅网关消费）
    - 透传 User-Agent / Content-Type 等业务头
    """
    headers = {
        "Authorization": f"Bearer {upstream_key}",
        "Content-Type": "application/json",
    }

    # 透传 User-Agent（部分上游对 User-Agent 有要求）
    ua = request.headers.get("User-Agent")
    if ua:
        headers["User-Agent"] = ua

    return headers


def _maybe_disable_thinking(path: str, body: dict, ctx: MeteringContext) -> None:
    """按场景为指定模型注入 body.thinking={"type":"disabled"}，加速抽取类任务。

    - 仅作用于 chat/completions（embedding 无 thinking 概念）；
    - 仅当 model 命中 LLMGW_DISABLE_THINKING_MODELS 且 scene 命中
      LLMGW_DISABLE_THINKING_SCENES 时注入（空集合表示不限制该维度）；
    - 不覆盖调用方已显式设置的 thinking 字段；
    - ⚠️ 仅适配腾讯 tokenhub（tencentmaas）：顶层 {"thinking": {"type": "disabled"}}
      是其专有关思考格式。换其他上游（OpenAI/DeepSeek 官方/vLLM 等）须在此适配对应
      参数，否则上游可能报错或忽略；不确定时把 LLMGW_DISABLE_THINKING_ENABLED 关掉。
    """
    if path.endswith("/embeddings"):
        return
    cfg = get_config()
    if not getattr(cfg, "LLMGW_DISABLE_THINKING_ENABLED", False):
        return
    if not isinstance(body, dict) or "thinking" in body:  # 调用方显式声明优先
        return
    model = body.get("model") or ""
    models = {m.strip() for m in (cfg.LLMGW_DISABLE_THINKING_MODELS or "").split(",") if m.strip()}
    if models and model not in models:
        return
    scenes = {s.strip() for s in (cfg.LLMGW_DISABLE_THINKING_SCENES or "").split(",") if s.strip()}
    if scenes and ctx.scene not in scenes:
        return
    body["thinking"] = {"type": "disabled"}
    logger.info(
        "注入 thinking.disabled | req_id=%s model=%s scene=%s",
        ctx.request_id, model, ctx.scene,
    )


# ============================================================
# [jonex] 上游 429 有界重试 + 退避（§12 B 方案）
# 把上游限流从"硬失败透传"改为"网关内部重试吸收"，使 LightRAG 抽取
# 看不到 429、chunk 不失败、strict 不回滚。
# ============================================================


def _retry_after_seconds(header: str | None) -> float | None:
    """解析 Retry-After 头，返回等待秒数；解析失败返回 None。

    支持两种格式：
    - 纯数字秒：``Retry-After: 60``
    - HTTP-date：``Retry-After: Wed, 21 Oct 2015 07:28:00 GMT``
    """
    if not header:
        return None
    header = header.strip()
    # 数字秒
    try:
        seconds = int(header)
        if seconds >= 0:
            return float(seconds)
    except ValueError:
        pass
    # HTTP-date（rfc 7231）
    try:
        date = parsedate_to_datetime(header)
        if date:
            delta = (date - datetime.now(timezone.utc)).total_seconds()
            if delta > 0:
                return delta
    except Exception:
        pass
    return None


def _decide_wait(resp, attempt: int, cfg) -> float:
    """计算本次重试的等待秒数。

    优先使用上游 Retry-After，否则指数退避 + 随机抖动，
    最后用 RETRY_AFTER_CAP 裁剪病态值。
    """
    wait = _retry_after_seconds(resp.headers.get("Retry-After"))
    if wait is None:
        wait = min(
            cfg.LLMGW_UPSTREAM_RETRY_BACKOFF_BASE * (2 ** attempt),
            cfg.LLMGW_UPSTREAM_RETRY_BACKOFF_MAX,
        )
    wait = min(wait, cfg.LLMGW_UPSTREAM_RETRY_AFTER_CAP)
    wait += random.uniform(0, cfg.LLMGW_UPSTREAM_RETRY_JITTER)
    return wait


async def proxy_nonstream(
    request: Request,
    body: dict,
    ctx: MeteringContext,
) -> tuple[dict, int, float]:
    """非流式转发：返回 (response_json, status_code, latency_ms)

    [jonex] B 方案：上游 429 时网关内部有界重试 + 退避，耗尽后透传最后一次 429，
    让 LightRAG/对账保持既有兜底路径。
    """
    host, key = resolve_upstream(request.url.path, body)
    cfg = get_config()

    _maybe_disable_thinking(request.url.path, body, ctx)
    url = f"{host}{_upstream_path(request.url.path)}"
    logger.info("转发上游(非流式) | req_id=%s url=%s model=%s", ctx.request_id, url, body.get("model"))

    retry_enabled = cfg.LLMGW_UPSTREAM_RETRY_ENABLED
    retry_statuses: set[int] = set()
    if retry_enabled:
        retry_statuses = {int(s.strip()) for s in cfg.LLMGW_UPSTREAM_RETRY_STATUS.split(",") if s.strip()}
    max_retries = cfg.LLMGW_UPSTREAM_RETRY_MAX
    total_budget = cfg.LLMGW_UPSTREAM_RETRY_TOTAL_BUDGET

    t0 = time.monotonic()
    attempt = 0
    total_waited = 0.0
    resp = None
    t_attempt = t0  # 每次 attempt 的真实请求计时起点（不含重试 sleep）
    # N2（monitor.call_attempt）埋点：与计量同一幂等键，同一逻辑调用的重试稳定
    monitor_request_id = derive_request_id(ctx, body)
    wait_before_ms = None  # 进入本次 attempt 前的等待毫秒数；首次无等待

    while True:
        # 每次 attempt 独立 try/except；网络异常不重试直接返回 502
        t_attempt = time.monotonic()
        with attempt_span(
            request_id=monitor_request_id,
            tenant_id=ctx.tenant_id,
            attempt_no=attempt + 1,  # 物理尝试从 1 起（attempt 变量从 0 起）
            is_final=attempt >= max_retries,  # 无重试额度时本次必为末次；成功时下面补正
            wait_before_ms=wait_before_ms,
        ) as att:
            try:
                async with httpx.AsyncClient(timeout=cfg.LLMGW_REQUEST_TIMEOUT) as cli:
                    resp = await cli.post(
                        url,
                        json=body,
                        headers=upstream_headers(key, request),
                    )
            except Exception as e:
                latency_ms = int((time.monotonic() - t_attempt) * 1000)
                att.fail(error_code=502, error_message=e)
                _seal_final(att)
                logger.exception(
                    "上游请求异常 | req_id=%s url=%s latency_ms=%s err=%s",
                    ctx.request_id, url, latency_ms, e,
                )
                return (
                    {"error": {"message": f"upstream request failed: {e}", "type": "upstream_error"}},
                    502,
                    latency_ms,
                )

            # 限流且未达重试上限 → 计算退避并重试
            if retry_enabled and resp.status_code in retry_statuses and attempt < max_retries:
                wait = _decide_wait(resp, attempt, cfg)
                if total_waited + wait <= total_budget:
                    att.fail(error_code=resp.status_code)  # 本次尝试失败，还要重试
                    logger.warning(
                        "upstream 429 retry | req_id=%s attempt=%d/%d wait=%.1fs "
                        "total_waited=%.1fs retry_after=%s",
                        ctx.request_id, attempt + 1, max_retries, wait, total_waited,
                        resp.headers.get("Retry-After"),
                    )
                    await asyncio.sleep(wait)
                    total_waited += wait
                    attempt += 1
                    wait_before_ms = int(wait * 1000)
                    continue
                logger.warning(
                    "upstream 429 giving_up=budget_exceeded | req_id=%s attempt=%d/%d "
                    "total_waited=%.1fs budget=%.1fs wait_needed=%.1fs retry_after=%s",
                    ctx.request_id, attempt + 1, max_retries, total_waited, total_budget,
                    wait, resp.headers.get("Retry-After"),
                )
            if resp.status_code >= 400:
                att.fail(error_code=resp.status_code)  # 走到这里 = 不再重试的末次失败
            _seal_final(att)
            break

    # latency_ms = 最后一次 attempt 的真实请求耗时（不含重试 sleep），
    # 避免把 60s 等待污染延迟看板；total_wall_ms 保留全貌供排障。
    latency_ms = int((time.monotonic() - t_attempt) * 1000)
    total_wall_ms = int((time.monotonic() - t0) * 1000)

    try:
        data = resp.json()
    except Exception:
        data = {"error": {"message": f"upstream returned {resp.status_code}", "type": "upstream_error"}}

    if resp.status_code >= 400:
        # 任何上游错误（429 限流 / 5xx / 各平台自定义错误码等）都打全错误内容，
        # 便于压测/调并发时定位。不同上游错误码/结构不一，故不区分状态码，统一透传 body。
        # 查看：docker logs jonex-llm-gateway | findstr upstream_error
        logger.warning(
            "upstream_error 上游返回错误 | req_id=%s scene=%s model=%s url=%s "
            "status=%s latency_ms=%s total_wall_ms=%s retry_after=%s body=%s",
            ctx.request_id, getattr(ctx, "scene", ""), body.get("model"), url,
            resp.status_code, latency_ms, total_wall_ms,
            resp.headers.get("Retry-After"), resp.text[:500],
            extra={
                "event": "upstream_error",
                "req_id": ctx.request_id,
                "scene": getattr(ctx, "scene", ""),
                "model": body.get("model"),
                "status": resp.status_code,
                "latency_ms": latency_ms,
                "total_wall_ms": total_wall_ms,
                "retry_after": resp.headers.get("Retry-After"),
            },
        )
    return data, resp.status_code, latency_ms


async def proxy_stream(
    request: Request,
    body: dict,
    ctx: MeteringContext,
) -> AsyncGenerator[bytes, None]:
    """流式转发：合并注入 stream_options.include_usage=true 后透传 SSE chunks

    [jonex] B 方案：仅在首字节前允许整请求重试（手动 send/aclose 管理响应生命周期），
    一旦开始 yield chunk 不再重试以避免重复输出。
    """
    host, key = resolve_upstream(request.url.path, body)
    cfg = get_config()

    _maybe_disable_thinking(request.url.path, body, ctx)
    # dict 合并 stream_options，仅补 include_usage 不覆盖（循环外只做一次）
    body["stream_options"] = {
        **body.get("stream_options", {}),
        "include_usage": True,
    }

    t0 = time.monotonic()
    url = f"{host}{_upstream_path(request.url.path)}"
    logger.info("转发上游(流式) | req_id=%s url=%s model=%s", ctx.request_id, url, body.get("model"))

    retry_enabled = cfg.LLMGW_UPSTREAM_RETRY_ENABLED
    retry_statuses: set[int] = set()
    if retry_enabled:
        retry_statuses = {int(s.strip()) for s in cfg.LLMGW_UPSTREAM_RETRY_STATUS.split(",") if s.strip()}
    max_retries = cfg.LLMGW_UPSTREAM_RETRY_MAX
    total_budget = cfg.LLMGW_UPSTREAM_RETRY_TOTAL_BUDGET

    attempt = 0
    total_waited = 0.0
    # N2（monitor.call_attempt）埋点：与计量同一幂等键，同一逻辑调用的重试稳定
    monitor_request_id = derive_request_id(ctx, body)
    wait_before_ms = None  # 进入本次 attempt 前的等待毫秒数；首次无等待

    async with httpx.AsyncClient(timeout=cfg.LLMGW_REQUEST_TIMEOUT) as cli:
        while True:
            with attempt_span(
                request_id=monitor_request_id,
                tenant_id=ctx.tenant_id,
                attempt_no=attempt + 1,  # 物理尝试从 1 起（attempt 变量从 0 起）
                is_final=attempt >= max_retries,  # 无重试额度时本次必为末次；成功时下面补正
                wait_before_ms=wait_before_ms,
            ) as att:
                # 手动 build_request + send（不用 async with cli.stream，否则无法 continue 外层 while）
                try:
                    req = cli.build_request(
                        "POST", url,
                        json=body,
                        headers=upstream_headers(key, request),
                    )
                    resp = await cli.send(req, stream=True)
                except Exception as e:
                    att.fail(error_code=502, error_message=e)
                    _seal_final(att)
                    logger.exception(
                        "上游流式请求异常 | req_id=%s url=%s err=%s", ctx.request_id, url, e,
                    )
                    raise

                # 首字节前：429 且未达上限 → 关掉本次响应并重试
                if retry_enabled and resp.status_code in retry_statuses and attempt < max_retries:
                    wait = _decide_wait(resp, attempt, cfg)
                    if total_waited + wait <= total_budget:
                        att.fail(error_code=resp.status_code)  # 本次尝试失败，还要重试
                        logger.warning(
                            "upstream 429 retry(stream) | req_id=%s attempt=%d/%d wait=%.1fs "
                            "total_waited=%.1fs retry_after=%s",
                            ctx.request_id, attempt + 1, max_retries, wait, total_waited,
                            resp.headers.get("Retry-After"),
                        )
                        await resp.aclose()
                        await asyncio.sleep(wait)
                        total_waited += wait
                        attempt += 1
                        wait_before_ms = int(wait * 1000)
                        continue
                    logger.warning(
                        "upstream 429 giving_up=budget_exceeded(stream) | req_id=%s attempt=%d/%d "
                        "total_waited=%.1fs budget=%.1fs wait_needed=%.1fs retry_after=%s",
                        ctx.request_id, attempt + 1, max_retries, total_waited, total_budget,
                        wait, resp.headers.get("Retry-After"),
                    )
                if resp.status_code >= 400:
                    att.fail(error_code=resp.status_code)  # 走到这里 = 不再重试的末次失败
                _seal_final(att)
                break

        # 最终响应（2xx 或耗尽后的错误）
        if resp.status_code >= 400:
            # 任何上游错误统一打全内容（各平台错误码/结构不一，不区分状态码）。
            # 查看：docker logs jonex-llm-gateway | findstr upstream_error
            err_body = (await resp.aread())[:500]
            logger.warning(
                "upstream_error 上游流式返回错误 | req_id=%s scene=%s model=%s "
                "url=%s status=%s retry_after=%s body=%s",
                ctx.request_id, getattr(ctx, "scene", ""), body.get("model"), url,
                resp.status_code, resp.headers.get("Retry-After"), err_body,
                extra={
                    "event": "upstream_error",
                    "req_id": ctx.request_id,
                    "scene": getattr(ctx, "scene", ""),
                    "model": body.get("model"),
                    "status": resp.status_code,
                    "retry_after": resp.headers.get("Retry-After"),
                },
            )
            # aread() 已消费 body，不向客户端 yield 任何 chunk；改为**抛异常**而非
            # 安静 return —— 否则流就此结束，客户端当正常答完、采集侧也照记成功。
            raise UpstreamError(
                status_code=resp.status_code,
                message=err_body.decode("utf-8", "replace"),
            )

        async for chunk in resp.aiter_bytes():
            yield chunk


# ============================================================
# Rerank 上游（方案 B2）
# 对内暴露 cohere 风格契约：{model, query, documents[], top_n}
#                       → {results:[{index, relevance_score}]}
# ============================================================


def _aggregate_score(profile, top_logprobs: list[dict]) -> float:
    """按 profile 的 yes/no 词表聚合首 token 概率，返回 P(yes)/(P(yes)+P(no))。"""
    yes_p = no_p = 0.0
    for t in top_logprobs or []:
        tok = str(t.get("token", "")).strip().lower()
        try:
            p = math.exp(t.get("logprob", -99))
        except (TypeError, ValueError, OverflowError):
            continue
        if tok in profile.yes_set:
            yes_p += p
        elif tok in profile.no_set:
            no_p += p
    return yes_p / (yes_p + no_p) if (yes_p + no_p) > 0 else 0.0


async def _score_one(cli, host, key, model, query, doc, profile) -> float:
    """单文档打分：/api/generate raw 模板，读首 token yes/no 概率。"""
    payload = {
        "model": model,
        "prompt": profile.render(query, doc),
        "raw": True,
        "stream": False,
        "options": {"temperature": 0, "num_predict": 1},
        "logprobs": True,
        "top_logprobs": 20,
    }
    resp = await cli.post(
        f"{host}/api/generate", json=payload,
        headers={"Authorization": f"Bearer {key}"},
    )
    resp.raise_for_status()
    data = resp.json()
    lp = data.get("logprobs") or []
    if lp:  # 概率算分（首选）
        first = lp[0] if isinstance(lp, list) else lp
        return _aggregate_score(profile, first.get("top_logprobs") or [])
    # 降级二值兜底：无 logprobs 时按生成 token 判定
    tok = (data.get("response") or "").strip().lower()
    return 1.0 if tok in profile.yes_set else (0.0 if tok in profile.no_set else 0.5)


async def _ollama_rerank_scores(host, key, model, query, docs, cfg) -> list[dict]:
    """ollama-generate：逐文档并发打分。失败给中性分 0.5；失败率>30% 抛错整体降级。"""
    profile = get_profile(cfg.LLMGW_RERANK_PROMPT_PROFILE)
    sem = asyncio.Semaphore(cfg.LLMGW_RERANK_CONCURRENCY)
    failures = 0

    async def _bounded(cli, i, doc):
        nonlocal failures
        async with sem:
            try:
                s = await _score_one(cli, host, key, model, query, doc, profile)
            except Exception as e:
                failures += 1
                logger.warning("[rerank] doc#%d 打分失败记中性分 0.5: %s", i, e)
                s = 0.5  # 中性分：避免把网络抖动失败的文档不公平地沉到末尾
            return {"index": i, "relevance_score": s}

    async with httpx.AsyncClient(timeout=cfg.LLMGW_RERANK_TIMEOUT) as cli:
        results = await asyncio.gather(*[_bounded(cli, i, d) for i, d in enumerate(docs)])

    # 失败率过高（>30%）说明 reranker 整体不可信，抛错让上层整体回退 len(locations)
    if docs and failures / len(docs) > 0.3:
        raise RuntimeError(f"rerank 失败率过高 {failures}/{len(docs)}，整体降级")
    return results


async def proxy_rerank(
    request: Request,
    body: dict,
    ctx: MeteringContext,
) -> tuple[dict, int, float]:
    """rerank 转发：返回 (response_json, status_code, latency_ms)。

    binding=cohere：透传上游标准 /rerank。
    binding=ollama-generate：逐文档 /api/generate 算分后组装 cohere 风格结果。
    """
    cfg = get_config()
    binding = cfg.LLMGW_RERANK_BINDING
    host = cfg.LLMGW_UPSTREAM_RERANK_HOST
    key = cfg.LLMGW_UPSTREAM_RERANK_API_KEY
    model = body.get("model") or cfg.LLMGW_RERANK_MODEL
    query = body.get("query") or ""
    # [jonex] §image-refs E3-补: MAX_DOCS 语义是单批规模保护，不是截断上限。
    # 旧实现 docs[:MAX_DOCS] 静默丢弃超限候选（62 张图片的 doc 只重排前
    # 12 个，其余保持原分，E3 语义失效一半且调用方无感知）——改为分批
    # 评分后合并，index 全局连续，对调用方透明。
    docs = body.get("documents") or []
    max_docs = max(1, cfg.LLMGW_RERANK_MAX_DOCS)
    batches = [docs[i:i + max_docs] for i in range(0, len(docs), max_docs)]
    top_n = body.get("top_n")

    t0 = time.monotonic()

    if binding == "cohere":
        # [jonex] 分批**并发**（2026-09-24）。原先这里是串行 `for` 逐批 await，
        # 而 `LLMGW_RERANK_CONCURRENCY` 只在 ollama-generate 分支被消费 ——
        # cohere 绑定下并发配置形同虚设。实测代价随批数线性放大：单批 ≈155ms，
        # 45 条 = 4 批 = 621ms、115 条 = 10 批 ≈ 1.55s，而池/证据校验动辄上百条。
        #
        # ⚠️ **批数超过并发上限时按轮次串行**，不是「一轮出完」：默认
        # `LLMGW_RERANK_CONCURRENCY=4` 时，115 条 = 10 批要跑 3 轮（≈465ms），
        # 45 条那 4 批才恰好一轮。别拿 4 批的例子外推到所有规模。
        #
        # ⚠️ 并发**不改分数**：上游对每个 (query, document) 独立打分，本函数只做
        # 分批转发 + `index` 全局重编号，**无归一化、无跨批聚合**。故批次怎么并行，
        # 同一篇文档拿到的 `relevance_score` 都一样——这是改动安全性的全部依据。
        #
        # ⚠️ **取舍：失败不再短路**。原串行版首批异常就 return、后续批次不发；
        # 现在 `gather` 会把全部批次发出去再逐个判。在「上游过载导致超时」这个
        # 最常见的失败场景下，这会让瞬时并发更高、**加剧过载**。换来的是正常路径
        # 的墙钟从 O(批数) 降到 O(批数/并发)。若实测发现上游被打爆，把
        # `LLMGW_RERANK_CONCURRENCY` 调小即可（唯一旋钮）。
        sem = asyncio.Semaphore(max(1, cfg.LLMGW_RERANK_CONCURRENCY))
        base_headers = {"Authorization": f"Bearer {key}"}

        async def _one_batch(bi: int, batch: list) -> list[dict]:
            async with sem:
                payload: dict = {"model": model, "query": query, "documents": batch}
                if top_n is not None:
                    payload["top_n"] = top_n
                resp = await cli.post(
                    f"{host}/rerank", json=payload, headers=base_headers,
                )
                out = resp.json().get("results", [])
                for r in out:
                    r["index"] = bi * max_docs + r.get("index", 0)
                return out

        async with httpx.AsyncClient(timeout=cfg.LLMGW_RERANK_TIMEOUT) as cli:
            batch_results = await asyncio.gather(
                *[_one_batch(bi, b) for bi, b in enumerate(batches)],
                return_exceptions=True,
            )
        results = []
        for res in batch_results:
            # ⚠️ 取消信号**不是**「上游失败」——`BaseException` 会连
            # `CancelledError` 一起接住，兜成 502 会让调用方以为只是上游抖动、
            # 继续往下走，而正确行为是让取消传播出去。
            if isinstance(res, asyncio.CancelledError):
                raise res
            if isinstance(res, BaseException):
                latency_ms = int((time.monotonic() - t0) * 1000)
                logger.exception(
                    "rerank 上游异常(cohere) | req_id=%s err=%s", ctx.request_id, res)
                return ({"error": {"message": f"rerank upstream failed: {res}"}}, 502, latency_ms)
            results.extend(res)
        latency_ms = int((time.monotonic() - t0) * 1000)
        if top_n:
            results = sorted(results, key=lambda r: r["relevance_score"], reverse=True)[:top_n]
        return {"results": results}, 200, latency_ms

    # binding == "ollama-generate"
    try:
        results = []
        for bi, batch in enumerate(batches):
            batch_results = await _ollama_rerank_scores(host, key, model, query, batch, cfg)
            for r in batch_results:
                r["index"] = bi * max_docs + r.get("index", 0)
            results.extend(batch_results)
    except Exception as e:
        latency_ms = int((time.monotonic() - t0) * 1000)
        logger.warning("rerank 整体降级(ollama-generate) | req_id=%s err=%s", ctx.request_id, e)
        return ({"error": {"message": f"rerank degraded: {e}"}}, 502, latency_ms)

    latency_ms = int((time.monotonic() - t0) * 1000)
    if top_n:
        results = sorted(results, key=lambda r: r["relevance_score"], reverse=True)[:top_n]
    return {"results": results}, 200, latency_ms
