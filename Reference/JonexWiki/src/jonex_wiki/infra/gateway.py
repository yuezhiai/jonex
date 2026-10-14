import asyncio
import json
import logging

import httpx
from openai import AsyncOpenAI

from jonex_wiki.infra.settings import current_llm_model

log = logging.getLogger(__name__)


class GwModelNotConfigured(RuntimeError):
    """LLM 模型名解析为空 —— 请求根本发不出去。

    模型名的唯一来源是环境变量（``LLM_MODEL`` 或兜底 ``DEFAULT_LLM_MODEL``）。
    这里**响亮失败**而不是拿一个空名字去请求上游：空模型名换回来的只会是上游
    一句语焉不详的 400/404，看不出是配置漏了。历史教训见
    ``infra/settings.py`` 的 ``_default_llm_model``。
    """


class GwOutputTooLong(RuntimeError):
    """输出撞上 max_tokens 被截断。**不可重试**——同样的请求只会再撞一次上限。

    带上 usage 与 content_chars 两个理由:
    1. 被截断的调用**确实烧掉了**整个 max_tokens 预算,不记进遥测会让成本汇总
       系统性低估(实测一轮编译里截断占了约 28% 的 output token)
    2. `completion_tokens` 顶到上限而 `content_chars` 接近 0,正是"预算花在了
       不产出内容的地方"的直接证据——这是判断该不该做章节化输出的关键判据
    """

    def __init__(self, message: str, *, usage: dict | None = None, content_chars: int = 0):
        super().__init__(message)
        self.usage = usage
        self.content_chars = content_chars


# 值得重发的 HTTP 状态：限流、超时、以及一切 5xx。其余 4xx 都是"重发同一个请求
# 只会拿到同一个错"，重试纯属空转（实测把一次 402 刷成 3 条一样的日志）。
_RETRYABLE_STATUS = frozenset({408, 409, 425, 429})


def _status_of(exc: BaseException) -> int | None:
    """取异常的 HTTP 状态码（openai 的 APIStatusError 与 httpx 的响应都挂在这里）。"""
    for candidate in (getattr(exc, "status_code", None),
                      getattr(getattr(exc, "response", None), "status_code", None)):
        if isinstance(candidate, int):
            return candidate
    return None


def _is_retryable(exc: BaseException) -> bool:
    """无状态码（连接失败/超时/未知）→ 保守重试；有状态码 → 只认瞬时那一档。"""
    status = _status_of(exc)
    if status is None:
        return True
    return status in _RETRYABLE_STATUS or status >= 500


# 只有这些状态才可能是"某个参数不被支持"，值得去掉参数重发；402/401 之类与参数无关。
_PARAMETER_STATUS = frozenset({400, 404, 415, 422})


class GwUpstreamRejected(RuntimeError):
    """上游**明确拒绝**：重发同一个请求只会拿到同一个错，因此不消耗重试预算。

    与"重试耗尽"分开成两种错误，是因为它们指向完全不同的排查方向：重试耗尽说明
    链路在抖，明确拒绝说明**这次请求本身不被接受**（欠费/鉴权/参数）。

    ``model`` 带上失败时实际使用的模型名 —— 排查时"哪个模型被拒"是第一个要看的东西，
    而失败路径此前在计量里记的是 ``model: null``（2026-10-08：日志里 3 条一样的
    402，看不出用的是哪一个模型，把排查引向了"配置没生效"这个错误方向）。
    """

    def __init__(self, message: str, *, model: str = "", status: int | None = None,
                 reason: str = "") -> None:
        super().__init__(message)
        self.model = model
        self.status_code = status
        self.reason = reason


def _upstream_reason(exc: BaseException, limit: int = 300) -> str:
    """从 SDK 异常里抠出上游返回的错因正文（code/type/message 串起来，截断）。"""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        for source in (error, body):
            if not isinstance(source, dict):
                continue
            parts = [str(source[key]) for key in ("code", "type", "message")
                     if source.get(key)]
            if parts:
                return " ".join(parts)[:limit]
    return ""


# 402 最容易被误判成"本地配置没生效"（2026-10-08 现场就在这条路上绕了很久），
# 所以按状态码给一句指向性提示，直接写在错误里。
_REJECTION_HINTS = {
    400: "上游认为请求不合法：检查模型名形态与请求参数",
    401: "鉴权失败：检查 LLM_BINDING_API_KEY / 网关 token 是否有效",
    402: "上游返回 402：通常是账户额度或余额用尽，**不是**本地配置错误；"
         "请到上游控制台确认该模型可用性与付费状态",
    403: "上游拒绝访问：确认该 API Key 是否有此模型的权限",
    404: "上游找不到该模型：模型名可能已改名或下架",
}


def _rejection(exc: BaseException, *, status: int | None, model: str) -> GwUpstreamRejected:
    """把"明确拒绝"整理成一条自带模型名、上游错因与处置提示的错误。"""
    reason = _upstream_reason(exc) or type(exc).__name__
    hint = _REJECTION_HINTS.get(status or 0, "")
    message = "上游明确拒绝(model={} status={}): {}".format(
        model or "<未配置>", status, reason)
    if hint:
        message += "；" + hint
    message += "。重发同一个请求只会拿到同一个错，故不重试。"
    return GwUpstreamRejected(message, model=model or "", status=status, reason=reason)


def _is_truncated(choice, usage: dict | None, max_tokens: int) -> bool:
    """判定响应是否被 max_tokens 截断。

    优先信 `finish_reason == "length"`。网关不透传该字段时(实测有此情况)退回
    token 计数:输出贴着上限基本只可能是被截断。留 2 个 token 余量是因为不同网关
    的计数口径差 1(实测出现过 completion_tokens=8191 而上限为 8192)。
    """
    finish_reason = getattr(choice, "finish_reason", None)
    if finish_reason == "length":
        return True
    if finish_reason is not None:
        return False
    completion_tokens = int((usage or {}).get("completion_tokens") or 0)
    return bool(max_tokens) and completion_tokens >= max_tokens - 2


def thinking_payload(value: str | None) -> dict | None:
    """``off``/``on``/``scene_default`` → ``{"type": "disabled"|"enabled"}`` / ``None``。

    ``None`` 表示**不发** thinking 字段,交回 llm-gateway 按 ``X-Jonex-Scene``
    白名单(``LLMGW_DISABLE_THINKING_SCENES``)决定。空值按 ``scene_default`` 处理。
    """
    normalized = str(value or "").strip().casefold()
    if normalized in ("on", "enabled"):
        return {"type": "enabled"}
    if normalized in ("off", "disabled"):
        return {"type": "disabled"}
    if normalized in ("", "scene_default"):
        return None
    raise ValueError(f"thinking 取值非法: {value!r}(仅支持 on/off/scene_default)")


class GwClient:
    """spec §12:所有模型调用走 llm-gateway;计量头强制注入;rerank 失败降级。"""

    def __init__(self, settings, transport: httpx.AsyncBaseTransport | None = None,
                 backoff_schedule=(1.0, 2.0)):
        self.settings = settings
        self._backoff = backoff_schedule
        # chat 用 per-request timeout 收紧到 settings.llm_timeout
        self.llm = AsyncOpenAI(base_url=settings.llm_binding_host,
                               api_key=settings.llm_binding_api_key,
                               max_retries=0,  # 重试预算由 GwClient 统一控制,禁用 SDK 自带重试
                               timeout=max(settings.llm_timeout, 300.0),
                               http_client=httpx.AsyncClient(transport=transport) if transport else None)
        self.llm_model = settings.llm_model
        self._httpx = httpx.AsyncClient(transport=transport, timeout=30.0)

    def _headers(self, *, scene, tenant_id, kb_id, trace_id, doc_id=None) -> dict:
        h = {"X-Jonex-Tenant-Id": tenant_id, "X-Jonex-Kb-Id": kb_id,
             "X-Jonex-Trace-Id": trace_id, "X-Jonex-Scene": scene}
        if doc_id:
            h["X-Jonex-Doc-Id"] = doc_id
        return h

    async def complete(self, messages, *, scene, tenant_id, kb_id, trace_id,
                       schema=None, json_mode=False, max_tokens=4096, model=None,
                       doc_id=None, tools=None, thinking=None) -> dict:
        """契约:返回 dict 且 content 恒为 str(JSON 文本或原始文本);调用方经 parse_json_content 解析。

        json_mode=True 时请求 response_format=json_object(强制模型输出 JSON);
        上游/网关不支持(400)时自动去掉该参数重试,容错解析兜底。
        ``tools`` 原样透传(OpenAI tools 数组),响应里的 tool_calls 回填进返回 dict。
        ``thinking`` 原样透传(``{"type": "enabled"|"disabled"}``),网关不支持时安全忽略。

        ``thinking=None`` 表示**调用方未声明** → 用配置总开关
        ``Settings.thinking_default``(默认 ``off``,见 JONEX_WIKI_THINKING_DEFAULT)。
        于是"未声明"的调用(查询遍历/最终合成)全部默认关思考,而调用点写死
        ``{"type": "disabled"}`` 的辅助步骤依旧恒关——两者一致,不存在漏网的开思考调用。
        ``thinking_default=scene_default`` 时不发该字段,交回网关按 scene 决定。
        """
        if thinking is None:
            thinking = thinking_payload(getattr(self.settings, "thinking_default", "off"))
        # 模型名一律来自环境变量；调用点可显式覆盖，否则用配置里的。
        # [jonex] 兜底也读**当下**的 env：self.llm_model 是 GwClient 构造时的快照，
        # 进程启动后改 env 不重启时会拿到旧值（`current_llm_model` 每次调用重读）。
        resolved_model = (model or current_llm_model() or self.llm_model or "").strip()
        if not resolved_model:
            raise GwModelNotConfigured(
                "LLM 模型名未配置：请设置环境变量 LLM_MODEL 或 DEFAULT_LLM_MODEL"
            )
        kwargs = dict(model=resolved_model, messages=messages,
                      max_tokens=max_tokens, temperature=0,
                      timeout=self.settings.llm_timeout,   # 挂起要快失败,交给下面的重试预算
                      extra_headers=self._headers(scene=scene, tenant_id=tenant_id,
                                                  kb_id=kb_id, trace_id=trace_id, doc_id=doc_id))
        if schema is not None:
            # 网关不支持 json_schema 结构化输出(实测 400 "type unavailable now")。
            # 按 core 的 evaluate_poc 做法:schema 内联进 system 消息 + json_object
            # 强制 JSON,结构由客户端 pydantic 校验兜底。
            instruction = (
                "Return only one JSON object valid against this JSON Schema: "
                + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
            )
            kwargs["messages"] = list(messages) + [{"role": "system", "content": instruction}]
            kwargs["response_format"] = {"type": "json_object"}
        elif json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if tools is not None:
            kwargs["tools"] = tools
        if thinking is not None:
            kwargs["extra_body"] = {"thinking": thinking}
        last: Exception | None = None
        for attempt in range(3):  # 重试 2 次,共 3 尝试
            try:
                resp = await self.llm.chat.completions.create(**kwargs)
                choice = resp.choices[0]
                content = choice.message.content or ""
                usage = (resp.usage.model_dump() if getattr(resp, "usage", None) else None)
                if _is_truncated(choice, usage, max_tokens):
                    # 输出撞上 max_tokens 被截断。**必须与"json_object 不兼容"区分开**:
                    # 原实现把"空输出"一律当成格式不兼容,去掉 response_format 后用
                    # 同样超长的请求再打一次——真实日志里因此每次白烧两轮 8192 token、
                    # 单次 complete 耗时 110~145 秒。截断是内容规模问题,重试同样的请求
                    # 只会再撞一次,所以这里直接上抛且不参与任何降级。
                    raise GwOutputTooLong(
                        f"输出被 max_tokens={max_tokens} 截断"
                        f"(completion_tokens={(usage or {}).get('completion_tokens')},"
                        f" content_chars={len(content)});"
                        f"需要缩小单次输出规模,而不是重试",
                        usage=usage, content_chars=len(content))
                if not content and json_mode and "response_format" in kwargs:
                    # 未截断的空输出才可能是 json_object 模式不兼容 → 去掉参数重试
                    log.warning("json_object 下空输出(未截断),降级自由输出重试")
                    kwargs.pop("response_format", None)
                    continue
                tool_calls = getattr(choice.message, "tool_calls", None) or None
                if tool_calls is not None:
                    tool_calls = [tc.model_dump() for tc in tool_calls]
                return {"content": content, "raw": content, "usage": usage,
                        "model": resp.model or kwargs["model"],
                        "tool_calls": tool_calls,
                        "finish_reason": getattr(choice, "finish_reason", None)}
            except GwOutputTooLong:
                raise            # 不可重试:绕过下面的 json_mode 降级与网络重试
            except Exception as e:  # noqa: BLE001 —— 网络/5xx 重试
                status = _status_of(e)
                # 「参数不被支持」才值得去掉参数重发。402/401/403 与参数无关，
                # 放它们进降级分支只会白多打一次上游（实测 402 因此多发一次请求）。
                parameter_error = status is None or status in _PARAMETER_STATUS
                if parameter_error and thinking is not None and "extra_body" in kwargs \
                        and "thinking" in str(e).lower():
                    # 仅当错误确实指向 thinking 字段时才去掉重试(网关支持 body 里的
                    # thinking;其它 400 不能误判成 thinking 问题、浪费一次重试)
                    log.warning("thinking 不被支持,安全忽略重试: %s", e)
                    kwargs.pop("extra_body", None)
                    continue
                if parameter_error and json_mode and "response_format" in kwargs:
                    # 上游不支持 json_object(通常 400)→ 去掉参数继续(容错解析兜底)
                    log.warning("json_object 不被支持,降级自由输出: %s", e)
                    kwargs.pop("response_format", None)
                    continue
                if not _is_retryable(e):
                    # 明确拒绝：重发同一个请求只会拿到同一个错。立刻上抛并把模型名
                    # 与上游错因带出去，而不是空转 2 次再报一句"重试耗尽"。
                    raise _rejection(e, status=status, model=resolved_model) from e
                last = e
                log.warning("llm complete attempt %d failed: %s", attempt + 1, e)
                if attempt < 2:
                    await asyncio.sleep(self._backoff[attempt])
        raise RuntimeError(
            "llm complete 重试耗尽(model={}, 尝试 3 次): {}".format(resolved_model, last)
        ) from last


    async def rerank(self, query, documents, *, scene, tenant_id, kb_id, trace_id,
                     top_n=10) -> list[dict] | None:
        try:
            base = self.settings.llm_binding_host.rstrip("/")
            base = base.removesuffix("/v1")
            r = await self._httpx.post(
                f"{base}/v1/rerank",
                headers={"Authorization": f"Bearer {self.settings.llm_binding_api_key}",
                         **self._headers(scene=scene, tenant_id=tenant_id, kb_id=kb_id, trace_id=trace_id)},
                json={"model": "awenleven/Qwen3-Reranker-4B:Q4_K_M",
                      "query": query, "documents": documents, "top_n": top_n},
                timeout=15.0)
            if r.status_code >= 400:
                log.warning("rerank 失败 HTTP %d,保留原序", r.status_code)
                return None
            results = r.json().get("results") or []
            return [{"index": item.get("index"), "relevance_score": item.get("relevance_score")}
                    for item in results]
        except Exception as e:  # noqa: BLE001
            log.warning("rerank 异常,保留原序: %s", e)
            return None

    async def aclose(self) -> None:
        await self.llm.close()
        await self._httpx.aclose()


def parse_json_content(content: str) -> dict:
    """结构化输出统一解析入口:失败抛 ValueError,调用方走重试/失败路径。

    容错:模型常把 JSON 包进 markdown 代码块(```json ...```)或在 JSON 前后
    附加解释文字——先剥离代码块,再提取首个平衡花括号块。
    """
    import re
    text = content.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    start = text.find("{")
    if start < 0:
        raise ValueError("响应中没有 JSON 对象")
    # Read one complete object, ignoring surrounding prose or a repeated
    # transport rendering. The decoder handles braces inside quoted strings;
    # malformed/truncated objects still raise without another provider call.
    value, _ = json.JSONDecoder(strict=False).raw_decode(text[start:])
    return value
