"""ModelPort 适配器：engine ModelPort → GwClient。

信封 → 对象转换集中在这里：GwClient 返回 ``{"content": str, ...}`` 信封，
engine 只认解析后的 JSON 对象（``ModelPort.complete -> Dict``）。scene 与调用点
一对一（§8.3/§14）：引擎在每个逻辑调用点显式传 ``scene``；构造时注入的
``self._scene`` 只作兜底（``scene or self._scene``），调用点 scene 优先。

三个契约在这里落地，缺一个都会让真实网关下的查询直接 500：

1. **异常类型按引擎的接住方式给**。查询遍历轮与合成轮用
   ``except RuntimeError`` 接协议错误（``MODEL_INVALID_JSON``）与截断
   （``model output truncated``）；编译轮用 ``except (ValueError, RuntimeError)``。
   所以两者都抛 ``RuntimeError``，编译侧照旧接得住。
2. **遍历轮的端口级结构校验 + 反馈重试**。网关侧结构化输出已从 json_schema
   降级为 json_object（见 `0096a94`），模型因此会偶发多字段/坏 tool_calls 信封/
   不可解析 JSON；而引擎的响应模型是 ``extra="forbid"``。这里按传入 schema 做顶层
   校验，失败就把原因回喂给模型重试（最多 3 次），耗尽才抛
   ``MODEL_INVALID_JSON`` 交给引擎的协议重试/优雅降级。
3. **usage 走属性通道**。引擎用 ``getattr(raw, "usage", None)`` 聚合用量，
   裸 dict 会让 ``QueryRunResult.token_usage`` 恒空，所以解析结果包一层
   ``_UsageDict``。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List

from jonex_wiki.infra.gateway import GwOutputTooLong, parse_json_content
from jonex_wiki.kernel.ports import ModelPort

log = logging.getLogger(__name__)

# 遍历轮（tools 非空）的反馈重试次数。编译轮不重复放大调用量：引擎自身有
# per-passage 的 validation-failure 重试兜底。
TRAVERSAL_STRUCTURE_ATTEMPTS = 3


class _UsageDict(dict):
    """解析后的 JSON 对象 + provider usage（属性通道）。

    引擎按 ``getattr(raw, "usage", None)`` 读用量，而不是按键读——返回裸 dict 会
    让查询结果的 token_usage 恒为空。
    """

    def __init__(self, *args: Any, usage: Any = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.usage = usage


def _truncation_error(exc: GwOutputTooLong) -> RuntimeError:
    """把网关的截断信号转成 engine 认识的 ``output_truncated`` 异常。

    保留 ``usage``/``content_chars`` 以便 MeteredGw 之外的上层仍然能读截断用量；
    message 含 "model output truncated" 让 query 侧按子串判别的降级路径命中。
    类型是 ``RuntimeError``：查询侧的截断降级只接 RuntimeError，编译侧接
    ``(ValueError, RuntimeError)``，两边都成立。
    """
    err = RuntimeError("model output truncated")
    err.output_truncated = True  # type: ignore[attr-defined]
    err.usage = exc.usage        # type: ignore[attr-defined]
    err.content_chars = exc.content_chars  # type: ignore[attr-defined]
    return err


def _check_schema_structure(parsed: Any, schema: Dict[str, Any]) -> None:
    """按引擎传入的 JSON Schema 做顶层结构校验（字段/必填/信封）。

    上游无 json_schema 约束时模型自由输出会偶发多字段/缺字段，而引擎的响应模型是
    ``extra="forbid"``。只查顶层：嵌套 ``$defs`` 的完整校验属于引擎职责，这里拦下
    已观测到的失败形态即可。
    """
    if not isinstance(schema, dict):
        return
    props = schema.get("properties")
    if schema.get("type") != "object" or not isinstance(props, dict):
        return
    if not isinstance(parsed, dict):
        raise ValueError("输出必须是 JSON 对象")
    for req in schema.get("required") or []:
        if req not in parsed:
            raise ValueError(f"缺少必填字段 {req!r}")
    if schema.get("additionalProperties") is False:
        for key in parsed:
            if key not in props:
                raise ValueError(f"多余字段 {key!r} 不允许")
    # tool_calls 信封：引擎的 ToolCall 解析器只认
    # {id, type, function:{name, arguments(str)}}。网关侧结构化输出已降级为
    # json_object，模型常见两种"近似正确"形态：
    #   1) 扁平形态 {name, arguments(obj)}（缺 function 包装）；
    #   2) 完整包装但 arguments 是对象而非字符串。
    # 这两种都能无损规整为引擎要的信封，无需重试让模型再犯一次同样的错。
    if "tool_calls" in props and parsed.get("tool_calls"):
        normalized = []
        for index, item in enumerate(parsed["tool_calls"]):
            if not isinstance(item, dict):
                raise ValueError("tool_calls 必须是对象列表")
            call_id = item.get("id") or "call_{}".format(index + 1)
            if isinstance(item.get("function"), dict):
                fn = item["function"]
                if not isinstance(fn.get("name"), str):
                    raise ValueError("tool_calls 信封缺少 name")
                args = fn.get("arguments")
                if not isinstance(args, str):
                    args = json.dumps(
                        args if args is not None else {},
                        ensure_ascii=False, separators=(",", ":"))
                normalized.append({
                    "id": call_id,
                    "type": item.get("type") or "function",
                    "function": {"name": fn["name"], "arguments": args},
                })
            elif isinstance(item.get("name"), str):
                args = item.get("arguments")
                if not isinstance(args, str):
                    args = json.dumps(
                        args if args is not None else {},
                        ensure_ascii=False, separators=(",", ":"))
                normalized.append({
                    "id": call_id,
                    "type": "function",
                    "function": {"name": item["name"], "arguments": args},
                })
            else:
                raise ValueError("tool_calls 信封缺少 function 或 name")
        parsed["tool_calls"] = normalized


def _append_feedback(messages: List[Dict[str, Any]], exc: Exception) -> List[Dict[str, Any]]:
    """把校验失败原因追加到最后一条用户消息，请求模型修正后重发。

    重发同一份 prompt 只会让模型再犯一次同样的错——必须把被拒的原因带回去。
    """
    out = [dict(m) for m in messages]
    note = ("\n\nYour previous response was REJECTED by schema validation: "
            + str(exc)
            + "\nReply again. Output exactly one JSON object matching the "
              "structured-output contract above (or call the tools natively), "
              "without explanations or markdown fences.")
    if out and out[-1].get("role") == "user":
        out[-1] = {**out[-1], "content": (out[-1].get("content") or "") + note}
    else:
        out.append({"role": "user", "content": note})
    return out


def _parse_traversal_tool_content(content: str, *, allow_tool_recovery: bool = True) -> dict:
    """Recover known traversal formatting errors without inventing values.

    A complete answer may contain unescaped quotes; only those literal quotes
    are escaped. For a missing root brace the decoder must reach EOF after a
    complete value. Both recoveries retain response validation.
    """
    try:
        return parse_json_content(content)
    except json.JSONDecodeError as exc:
        # Some providers leave literal quotation marks inside an otherwise
        # complete answer string. Escape only that root string, preserving its
        # text and the complete citations/tail; never fill in a missing value.
        answer = re.fullmatch(r'(\s*\{\s*"answer"\s*:\s*")(.*)("\s*,\s*"citations"\s*:.*\}\s*)',
                              exc.doc, re.S)
        if answer is None:
            bare = re.fullmatch(r'(\s*\{\s*"answer"\s*:\s*")(.*)("\s*\}\s*)', exc.doc, re.S)
            # A root answer-only response already supports inline E citations.
            # Refuse ambiguous additional fields; do not invent an array.
            if bare and not re.search(r'"\s*,\s*"[^"\n]+"\s*:', bare[2]):
                answer = bare
        if (exc.msg == "Expecting ',' delimiter" and answer
                and answer.start(2) <= exc.pos <= answer.end(2)):
            body, slashes = [], 0
            for char in answer[2]:
                if char == '"' and slashes % 2 == 0:
                    body.append("\\")
                body.append(char)
                slashes = slashes + 1 if char == "\\" else 0
            try:
                candidate = json.loads(answer[1] + "".join(body) + answer[3], strict=False)
                from jonex_wiki.domain.engine.query.algorithm import TraversalResponse
                if set(candidate) - set(TraversalResponse.model_fields):
                    raise ValueError("unknown answer field")
                response = TraversalResponse.model_validate(candidate)
                if response.tool_calls:
                    raise ValueError("answer recovery is tool-free")
            except ValueError:
                raise exc
            return candidate
        if (not allow_tool_recovery or exc.pos != len(exc.doc)
                or exc.msg != "Expecting ',' delimiter"):
            raise
        try:
            candidate = json.loads(exc.doc + "}", strict=False)
            # Only this rare recovery needs the query response type; ordinary
            # parsing and all compilation responses keep their current path.
            from jonex_wiki.domain.engine.query.algorithm import TraversalResponse
            response = TraversalResponse.model_validate(candidate)
            if not response.tool_calls:
                raise ValueError("no complete traversal tool calls")
        except ValueError:
            raise exc
        log.info("Recovered a traversal tool response missing its final root brace")
        return candidate


class ModelPortAdapter(ModelPort):
    """把 GwClient（信封）包成 engine ModelPort（JSON 对象）。

    包装顺序（已定）：GwClient → MeteredGw → ModelPortAdapter。截断在 MeteredGw
    已记过账，本适配器只做信号转换不再计量。
    """

    def __init__(self, gw, *, scene: str | None = None, tenant_id: str = "", kb_id: str = "",
                 before_call=None) -> None:
        self._gw = gw
        self._scene = scene
        self._tenant_id = tenant_id
        self._kb_id = kb_id
        self._before_call = before_call

    async def complete(
        self,
        messages: List[Dict[str, Any]],
        schema: Dict[str, Any] | None = None,
        *,
        max_tokens: int = 4096,
        tools: List[Dict[str, Any]] | None = None,
        model: str | None = None,
        tenant_id: str = "",
        context_id: str = "",
        trace_id: str = "",
        thinking: Dict[str, Any] | None = None,
        scene: str | None = None,
    ) -> Dict[str, Any]:
        if self._before_call is not None:
            await self._before_call()
        from jonex_wiki.kernel.query_progress import publish_query_progress
        publish_query_progress({"type": "synthesis" if "synth" in (scene or self._scene or "") else "model",
                                "title": "正在生成回答" if "synth" in (scene or self._scene or "") else "正在分析资料"})
        gw_kwargs: Dict[str, Any] = {
            "scene": scene or self._scene,
            "tenant_id": tenant_id or self._tenant_id,
            "kb_id": context_id or self._kb_id,
            "trace_id": trace_id,
            "schema": schema,
            "max_tokens": max_tokens,
            "model": model,
        }
        if tools is not None:
            gw_kwargs["tools"] = tools
        if thinking is not None:
            gw_kwargs["thinking"] = thinking

        # The query loop owns one shared repair allowance. Retrying here as
        # well would hide extra provider calls from that allowance/accounting.
        is_traversal = bool(schema and schema.get("title") == "TraversalResponse")
        attempts = 1 if is_traversal else (TRAVERSAL_STRUCTURE_ATTEMPTS if tools is not None else 1)
        turn_messages = messages
        for attempt in range(attempts):
            try:
                out = await self._gw.complete(turn_messages, **gw_kwargs)
            except GwOutputTooLong as exc:
                raise _truncation_error(exc) from exc

            tool_calls = out.get("tool_calls")
            if tool_calls:
                # 原生 tool_calls 通道（finish_reason=tool_calls）：交给 engine 的
                # 工具解析。
                return _UsageDict(
                    {"tool_calls": tool_calls, "answer": "", "citations": []},
                    usage=out.get("usage"),
                )
            try:
                parsed = (_parse_traversal_tool_content(out["content"], allow_tool_recovery=tools is not None)
                          if is_traversal
                          else parse_json_content(out["content"]))
                if tools is not None and schema is not None and not is_traversal:
                    _check_schema_structure(parsed, schema)
                return _UsageDict(parsed, usage=out.get("usage"))
            except ValueError as exc:
                content = out.get("content", "") or ""
                if attempt < attempts - 1:
                    turn_messages = _append_feedback(turn_messages, exc)
                    log.warning(
                        "模型输出结构偏差，反馈重试 %d/%d: %s | content[:200]=%r",
                        attempt + 1, attempts - 1, exc, content[:200],
                    )
                    continue
                log.warning(
                    "模型输出不可解析/不符合 schema（重试耗尽）: %s | content[:500]=%r",
                    exc, content[:500],
                )
                # 遍历轮：抛引擎认识的协议错误，让 query 的协议重试/优雅降级接住，
                # 而不是漏成 500。编译轮保持可解析失败语义，交给引擎自身重试。
                if tools is not None or is_traversal:
                    err: Exception = RuntimeError(
                        f"MODEL_INVALID_JSON: 模型输出不可解析/不符合 schema: {exc}"
                    )
                else:
                    err = ValueError(f"model response is not a JSON object: {exc}")
                err.usage = out.get("usage")  # type: ignore[attr-defined]
                err.raw_content = content  # type: ignore[attr-defined]
                raise err from exc
        raise AssertionError("unreachable: complete() retry loop must return or raise")
