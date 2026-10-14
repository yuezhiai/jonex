"""网关调用计量(§10 结构化日志遥测)。

分工与 compile_task 里的既有约定一致:**逐调用明细进结构化日志,任务级汇总进
tasks.summary**。前者用来做"延迟 vs 输出 token"的散点(判断延迟是被输出字数主导
还是被固定开销主导),后者用来在 `GET /tasks/{id}` 里直接看这次编译烧了多少。
"""

import logging
import time

log = logging.getLogger(__name__)


#: 调用结果三态。`degraded` 单独一档而不并入 `failed`——rerank 返回 None 是
#: §9 明文规定的降级(BM25-only / 保留原序),把它记成失败会让告警噪音盖掉真正的故障。
OK, DEGRADED, FAILED = "ok", "degraded", "failed"


def _model_for_failure(exc: BaseException, kwargs: dict, fallback: str | None) -> str | None:
    """失败路径也要记下模型名：**异常携带的 → 本次请求的 → 网关配置的**，逐级回落。

    失败时拿不到响应里的 ``model``（原实现因此一律记 ``None``），但"哪个模型被拒"恰恰
    是排查第一个要看的东西 —— 2026-10-08：日志里 3 条一模一样的 402 全是
    ``model: null``，看不出请求发的其实是旧模型名，把排查引向了"配置没生效"的错误方向。
    """
    for candidate in (getattr(exc, "model", None), kwargs.get("model"), fallback):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _blank() -> dict:
    return {"attempts": 0, OK: 0, DEGRADED: 0, FAILED: 0, "prompt_tokens": 0,
            "completion_tokens": 0,
            # 定位"output token 花在哪"的两个字段:
            # reasoning_tokens 靠上游透传;content_chars 是本地测量,不依赖网关配合。
            # completion_tokens 远大于 content_chars 对应的 token 量 → 预算花在了
            # 不产出内容的地方(推理/丢弃),此时压缩正文输出的收益有限。
            "reasoning_tokens": 0, "content_chars": 0,
            "latency_ms_sum": 0, "latency_ms_max": 0}


def _reasoning_tokens(usage: dict) -> int:
    """从 usage 里取推理 token(OpenAI 兼容字段);上游不给就是 0。"""
    details = usage.get("completion_tokens_details") or {}
    if not isinstance(details, dict):
        return 0
    return int(details.get("reasoning_tokens") or 0)


class MeteredGw:
    """给任意 gw 实现套上计量的转发包装器。

    鸭子类型转发,所以真实 `GwClient` 与测试里的 fakes 都能被包。

    **聚合键就是 `scene`**:六个调用点各有独立 scene(见 ARCHITECTURE §8.2),
    一对一,不需要再叠一层本地标签。曾经有过一个 `stage` 参数,那是为了在
    分配与页面编辑共用 `jonex_compile` 时把两者分开;scene 拆开之后它就只剩
    重复(`…page_edit/page_edit`)和契约负担(每个 gw 实现都得接受该 kwarg),已移除。

    聚合按 `trace_id` 归集,且只对**显式注册**过的 trace 累加。编译任务用 task_id
    注册、结束时取走并释放;查询请求的 trace_id 不注册,只留逐调用日志,
    避免字典无界增长。
    """

    def __init__(self, inner):
        self._inner = inner
        self._traces: dict[str, dict[str, dict]] = {}

    def __getattr__(self, name):        # 未显式包装的属性/方法原样透传
        return getattr(self._inner, name)

    # --- trace 生命周期 ---------------------------------------------------------------

    def begin_trace(self, trace_id: str) -> None:
        self._traces[trace_id] = {}

    def pop_trace(self, trace_id: str) -> dict[str, dict]:
        """取走并释放某个 trace 的聚合。**必须在任务结束路径上无条件调用**,
        否则失败的任务会把聚合永久留在内存里。"""
        return self._traces.pop(trace_id, {})

    def _record(self, *, scene, trace_id, kb_id, model, outcome,
                latency_ms, usage, content_chars=0, error=None) -> None:
        usage = usage or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        reasoning_tokens = _reasoning_tokens(usage)
        metric = {"scene": scene, "model": model, "outcome": outcome,
                  "latency_ms": latency_ms,
                  "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                  "reasoning_tokens": reasoning_tokens, "content_chars": content_chars}
        if error:
            metric["error"] = error
        log.info("gw_call", extra={"metric": metric, "trace_id": trace_id, "kb_id": kb_id})
        agg = self._traces.get(trace_id)
        if agg is None:
            return
        # 事件循环单线程:以下累加块内无 await,并发页面任务之间不会交错,无需加锁
        row = agg.setdefault(scene, _blank())
        row["attempts"] += 1
        row[outcome] += 1
        row["prompt_tokens"] += prompt_tokens
        row["completion_tokens"] += completion_tokens
        row["reasoning_tokens"] += reasoning_tokens
        row["content_chars"] += content_chars
        row["latency_ms_sum"] += latency_ms
        row["latency_ms_max"] = max(row["latency_ms_max"], latency_ms)

    async def _call(self, fn, *, scene, trace_id, kb_id, args, kwargs,
                    fallback_model: str | None = None):
        """统一计时 + 记录。**失败尝试同样记录**——尾延迟几乎全在重试与挂起里,
        只统计成功调用会系统性低估延迟(complete 内部重试 2 次,外层页面编辑再重试 1 次)。

        ``fallback_model`` 是该通道（chat）在"请求没带模型名"时实际会用的
        模型，供失败路径记名使用；必须是**本通道**的模型，避免失败路径记录错误的模型。
        """
        start = time.monotonic()
        try:
            out = await fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 —— 只为记录,随后原样抛出
            # 异常也可能带用量(截断:整个 max_tokens 预算已经烧掉了),取到就记,
            # 否则成本汇总会漏掉这部分
            self._record(scene=scene, trace_id=trace_id, kb_id=kb_id,
                         model=_model_for_failure(e, kwargs, fallback_model),
                         outcome=FAILED,
                         latency_ms=int((time.monotonic() - start) * 1000),
                         usage=getattr(e, "usage", None),
                         content_chars=int(getattr(e, "content_chars", 0) or 0),
                         error=type(e).__name__)
            raise
        latency_ms = int((time.monotonic() - start) * 1000)
        usage = out.get("usage") if isinstance(out, dict) else None
        model = out.get("model") if isinstance(out, dict) else None
        content = out.get("content") if isinstance(out, dict) else None
        self._record(scene=scene, trace_id=trace_id, kb_id=kb_id, model=model,
                     outcome=OK if out is not None else DEGRADED,
                     latency_ms=latency_ms, usage=usage,
                     content_chars=len(content) if isinstance(content, str) else 0)
        return out

    # --- 转发的三个网关方法 ------------------------------------------------------------

    async def complete(self, messages, *, scene, tenant_id, kb_id, trace_id, **kwargs):
        return await self._call(self._inner.complete, scene=scene,
                                trace_id=trace_id, kb_id=kb_id,
                                args=(messages,),
                                kwargs=dict(scene=scene, tenant_id=tenant_id, kb_id=kb_id,
                                            trace_id=trace_id, **kwargs),
                                fallback_model=getattr(self._inner, "llm_model", None))


    async def rerank(self, query, documents, *, scene, tenant_id, kb_id, trace_id, **kwargs):
        return await self._call(self._inner.rerank, scene=scene,
                                trace_id=trace_id, kb_id=kb_id,
                                args=(query, documents),
                                kwargs=dict(scene=scene, tenant_id=tenant_id, kb_id=kb_id,
                                            trace_id=trace_id, **kwargs))

    async def aclose(self) -> None:
        inner_close = getattr(self._inner, "aclose", None)
        if inner_close is not None:
            await inner_close()


def metered(gw) -> MeteredGw:
    """幂等包装:已经是 MeteredGw 就原样返回,避免嵌套导致每次调用记两条日志。"""
    return gw if isinstance(gw, MeteredGw) else MeteredGw(gw)
