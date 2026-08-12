# [jonex] 悦溪新增文件 — 计量上下文透传模块
"""
OpenKB 计量上下文模块。

提供 contextvar 存取 + build_metering_headers() 函数，
使 OpenKB 内部发起的 LLM 调用带上 X-Jonex-* metering headers。
"""

import contextvars
from typing import Optional

# ── contextvar 定义 ──
_jonex_tenant_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "jonex_tenant_id", default=None
)
_jonex_kb_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "jonex_kb_id", default=None
)
_jonex_scene: contextvars.ContextVar[str] = contextvars.ContextVar(
    "jonex_scene", default="openkb_compile"
)
_jonex_trace_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "jonex_trace_id", default=None
)


# ── 便捷存取 ──
def set_jonex_context(tenant_id=None, kb_id=None, scene=None, trace_id=None):
    """批量设置计量上下文（在 adapter 层 action 入口调用）。"""
    if tenant_id:
        _jonex_tenant_id.set(tenant_id)
    if kb_id:
        _jonex_kb_id.set(kb_id)
    if scene:
        _jonex_scene.set(scene)
    if trace_id:
        _jonex_trace_id.set(trace_id)


def clear_jonex_context():
    """清除计量上下文（仅在测试/上下文切换时使用）。"""
    _jonex_tenant_id.set(None)
    _jonex_kb_id.set(None)
    _jonex_scene.set("openkb_compile")
    _jonex_trace_id.set(None)


def build_metering_headers(scene: str = None) -> dict:
    """构造 X-Jonex-* metering headers，供 litellm extra_headers 使用。"""
    headers = {}
    tenant = _jonex_tenant_id.get()
    kb = _jonex_kb_id.get()
    scene_val = scene or _jonex_scene.get()
    trace = _jonex_trace_id.get()

    if tenant:
        headers["X-Jonex-Tenant-Id"] = tenant
    if kb:
        headers["X-Jonex-Kb-Id"] = kb
    headers["X-Jonex-Scene"] = scene_val
    if trace:
        headers["X-Jonex-Trace-Id"] = trace

    return headers
