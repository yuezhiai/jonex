#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""工具调用：一次调用的公共收尾（平台工具 / 知识库工具、执行 / 试运行 / 问答共用）。

流程：没有运行时 → no_runtime；按入参 schema 校验必填 → 解密凭据 → 调用运行时
（`jonex_core/common/tool_runtime.py`）→ 输出截断 → 结果状态 → 结构化日志。
所有异常都转成结果状态，不抛给调用方 —— 调用方据状态决定怎么回给用户 / 模型。

不在这里做的事
    「绑定是否存在、工具是否可用（已上架 / 已启用）」依赖各自的库表，由调用方判定；
    本体契约判定（D15）在 knowledge_base 的 `decide_execution`。

审计
    `tool_audit_entry` 生成统一形状的审计条目，入参只记键名不记值（可能含卡号等敏感值），
    凭据与输出都不进审计。调用方用 `jonex_core.common.audit.schedule_emit` 发送。

观测
    每次调用打一条 `[tool-invoke] tool=… source=… status=… http=… ms=…`，按工具统计
    调用数、延迟、失败率时直接检索这一行。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Awaitable, Callable, Optional

import httpx

from jonex_core.common.crypto import decrypt_secret
from jonex_core.common.exceptions import JonexException
from jonex_core.common.tool_runtime import MissingToolArgument, missing_required, run_runtime

logger = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 20000

STATUS_EXECUTED = "executed"
STATUS_FAILED = "failed"
STATUS_NO_RUNTIME = "no_runtime"
STATUS_INVALID_INPUT = "invalid_input"
STATUS_TOOL_UNAVAILABLE = "tool_unavailable"

# (runtime, arguments, secret, context) -> {"ok", "output", "http_status"?}
Runner = Callable[[dict, dict, str, dict], Awaitable[dict]]


def truncate_output(value: Any, max_chars: int = MAX_OUTPUT_CHARS) -> tuple[Any, bool]:
    """按 JSON 序列化长度截断工具输出，返回 `(值, 是否截断)`。"""
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) <= max_chars:
        return value, False
    return text[:max_chars], True


def _default_runner(timeout_cap: Optional[float]) -> Runner:
    async def _run(runtime: dict, arguments: dict, secret: str, context: dict) -> dict:
        return await run_runtime(
            runtime, arguments, secret=secret, context=context, timeout_cap=timeout_cap,
        )
    return _run


async def invoke_tool(
    runtime: Optional[dict],
    secret_encrypted: Optional[str],
    input_schema: Optional[dict],
    arguments: dict,
    context: dict,
    *,
    timeout_cap: Optional[float] = None,
    runner: Optional[Runner] = None,
    max_output_chars: int = MAX_OUTPUT_CHARS,
) -> dict:
    """调用一个工具定义，返回 `{"status", "ok", "output"?, "http_status"?, "error"?,
    "missing_inputs"?, "output_truncated"?, "duration_ms"}`。

    `context`：`tenant_id / knowledge_base_id / tool_name / tool_source / action_name / request_id`，
    `request_id` 会作为 `X-Request-ID` 发给对方接口。`input_schema` 为 None 表示调用方已校验过必填。
    """
    t0 = time.perf_counter()

    def _done(status: str, **extra) -> dict:
        out = {"status": status, "ok": status == STATUS_EXECUTED}
        out.update(extra)
        out["duration_ms"] = int((time.perf_counter() - t0) * 1000)
        logger.info(
            "[tool-invoke] tool=%s source=%s status=%s http=%s ms=%d error=%s",
            context.get("tool_name"), context.get("tool_source"), status,
            out.get("http_status"), out["duration_ms"], out.get("error") or "",
        )
        return out

    if not runtime:
        return _done(STATUS_NO_RUNTIME)
    if input_schema is not None:
        missing = missing_required(input_schema, arguments)
        if missing:
            return _done(STATUS_INVALID_INPUT, missing_inputs=missing)
    try:
        secret = decrypt_secret(secret_encrypted or "")
    except Exception:  # noqa: BLE001 — 密钥轮换后旧密文解不开：按失败回传，不泄露细节
        return _done(STATUS_FAILED, error="secret_unreadable")
    run = runner or _default_runner(timeout_cap)
    try:
        outcome = await run(runtime, arguments, secret, context)
    except MissingToolArgument as exc:
        return _done(STATUS_INVALID_INPUT, missing_inputs=exc.missing)
    except JonexException as exc:
        # 地址未通过安全校验、缺凭据等配置类错误：不调用，按失败回传
        return _done(STATUS_FAILED, error=exc.message or type(exc).__name__)
    except httpx.TimeoutException:
        return _done(STATUS_FAILED, error="timeout")
    except httpx.HTTPError as exc:
        return _done(STATUS_FAILED, error=type(exc).__name__)
    output, truncated = truncate_output(outcome.get("output"), max_output_chars)
    extra: dict = {"output": output, "output_truncated": truncated}
    if outcome.get("http_status") is not None:
        extra["http_status"] = outcome["http_status"]
    return _done(STATUS_EXECUTED if outcome.get("ok") else STATUS_FAILED, **extra)


def tool_audit_entry(
    *,
    tenant_id: str,
    action: str,
    service_name: str,
    status: str,
    tool_name: Optional[str],
    tool_source: Optional[str],
    user_id: Optional[str] = None,
    username: Optional[str] = None,
    request_id: Optional[str] = None,
    duration_ms: Optional[int] = None,
    resource: str = "skill",
    request_params: Optional[dict] = None,
    response_body: Optional[dict] = None,
    success_statuses: tuple[str, ...] = (STATUS_EXECUTED,),
) -> dict:
    """统一形状的工具调用审计条目（入参只记键名，凭据与输出不进审计）。"""
    try:
        uid = int(user_id) if user_id else None
    except (TypeError, ValueError):
        uid = None
    return {
        "tenant_id": tenant_id,
        "log_type": "OPERATION",
        "action": action,
        "outcome": "SUCCESS" if status in success_statuses else "FAILED",
        "service_name": service_name,
        "user_id": uid,
        "username": username,
        "resource": resource,
        "resource_id": tool_name,
        "trace_id": request_id,
        "duration_ms": duration_ms,
        "request_params": {"tool_source": tool_source, **(request_params or {})},
        "response_body": {"status": status, **(response_body or {})},
    }


__all__ = [
    "MAX_OUTPUT_CHARS",
    "Runner",
    "STATUS_EXECUTED",
    "STATUS_FAILED",
    "STATUS_INVALID_INPUT",
    "STATUS_NO_RUNTIME",
    "STATUS_TOOL_UNAVAILABLE",
    "invoke_tool",
    "tool_audit_entry",
    "truncate_output",
]
