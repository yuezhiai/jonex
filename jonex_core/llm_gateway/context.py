# -*- coding:utf-8 -*-
"""
计量上下文：从 X-Jonex-* 请求头解析调用方上下文。

两个 ID 的职责拆分（方案 A）：
- trace_id（链路追踪 ID）：一次用户业务请求一个，全链路透传不变，
  用于把该请求下的多次 LLM 调用归组统计。来源 X-Jonex-Trace-Id，
  兼容回落到 X-Request-ID。
- request_id（计量幂等键）：每次逻辑 LLM 调用一个、重试稳定，写入
  metering.llm_usage_log 的 UNIQUE 列去重。调用方可显式透传
  X-Jonex-Request-Id；缺失时由网关按 trace_id + 请求体哈希确定性派生
  （见 derive_request_id），保证重试稳定、不同调用不冲突。
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Optional

from fastapi import Request


# 用户名头（``X-Jonex-Username``）校验上限，与 ``monitor.call_log.source``
# 列宽（VARCHAR(128)）对齐。
_USERNAME_MAX_LEN = 128

# 这些字面量在监控里各有既定语义（平台自身 / 匿名 / 未知），不能当人名用。
_USERNAME_RESERVED = frozenset({"platform", "anonymous", "unknown"})


def sanitize_username(raw: Optional[str]) -> Optional[str]:
    """校验并规范化 ``X-Jonex-Username``。

    规则：trim 后 1..128 字符；仅 ASCII 可打印字符（拒绝 CR/LF/
    其它控制字符与非 ASCII）；保留字 ``platform`` / ``anonymous`` / ``unknown``
    （大小写不敏感）视为无效。

    **无效时返回 None（整个头忽略），绝不截断或部分采用** —— 截断后的名字会与
    另一个真实用户撞名，``source`` 展示就错了。
    """
    if raw is None:
        return None
    value = raw.strip()
    if not (1 <= len(value) <= _USERNAME_MAX_LEN):
        return None
    # ASCII 可打印：0x20(空格) .. 0x7E(~)；CR/LF/控制字符与中文等一律拒绝
    if any(ord(ch) < 0x20 or ord(ch) > 0x7E for ch in value):
        return None
    if value.lower() in _USERNAME_RESERVED:
        return None
    return value


@dataclass
class MeteringContext:
    """调用方计量上下文，由 X-Jonex-* 头携带"""
    tenant_id: str = "unknown"
    user_id: Optional[str] = None
    # 调用方显式透传的用户名（可选，前向兼容）；仅用于展示，不参与 source 生成
    username: Optional[str] = None
    scene: str = "unknown"
    kb_id: Optional[str] = None
    doc_id: Optional[str] = None
    trace_id: Optional[str] = None
    request_id: Optional[str] = None


def parse_ctx(request: Request) -> MeteringContext:
    """FastAPI 依赖：从请求头解析 MeteringContext

    request_id 此处可能为 None（调用方未透传）；真正的幂等键在 router 拿到
    请求体后通过 derive_request_id 补齐，避免空字符串撞 UNIQUE 约束被丢弃。

    ``username`` 仅在存在合法非空 ``X-Jonex-User-Id`` 时才采纳 —— 与现有
    「无 user_id = platform」语义一致；没有 user_id 的名字头不可信（拿不到人，
    只留下一个无法回查的名字）。
    """
    user_id = request.headers.get("X-Jonex-User-Id")
    has_user = bool((user_id or "").strip())
    return MeteringContext(
        tenant_id=request.headers.get("X-Jonex-Tenant-Id", "unknown"),
        user_id=user_id,
        username=(
            sanitize_username(request.headers.get("X-Jonex-Username"))
            if has_user
            else None
        ),
        scene=request.headers.get("X-Jonex-Scene", "unknown"),
        kb_id=request.headers.get("X-Jonex-Kb-Id"),
        doc_id=request.headers.get("X-Jonex-Doc-Id"),
        trace_id=(
            request.headers.get("X-Jonex-Trace-Id")
            or request.headers.get("X-Request-ID")
        ),
        request_id=request.headers.get("X-Jonex-Request-Id"),
    )


def derive_request_id(ctx: MeteringContext, body: dict) -> str:
    """确定性派生计量幂等键。

    解析顺序：
    1) 调用方显式透传 X-Jonex-Request-Id → 直接采用（调用方自控幂等）；
    2) 否则按 trace_id + 规范化请求体哈希派生：
       - 同一逻辑调用的重试：trace_id 与 body 完全一致 → 哈希一致 → 同一 key → 去重；
       - 不同调用（不同 chunk/不同问题）：body 不同 → key 不同 → 如实记录。

    trace_id 缺失时退化为纯 body 哈希（按内容去重，作用域更宽但仍正确）。
    """
    explicit = (ctx.request_id or "").strip()
    if explicit:
        return explicit[:64]

    try:
        canonical_body = json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        canonical_body = str(body)

    model = (body or {}).get("model", "") if isinstance(body, dict) else ""
    trace = ctx.trace_id or ""
    digest = hashlib.sha1(
        f"{trace}|{ctx.scene}|{model}|{canonical_body}".encode("utf-8")
    ).hexdigest()[:32]

    # 前缀带上 trace 便于排查；总长度受 VARCHAR(64) 约束
    prefix = trace[:24] if trace else "auto"
    return f"{prefix}:{digest}"[:64]
