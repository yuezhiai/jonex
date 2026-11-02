# [jonex] 新增文件：raganything 入库计量上下文透传
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

# [jonex] KB 级模型覆盖 P2a：VLM 降级计数。
#
# 用 **可变 list** 而不是 int：解析任务用 asyncio.gather 开子 task 并发跑多模态项，
# contextvar 在 create_task 时按引用复制 —— 子 task 与父 task 共享同一个 list 对象，
# 子 task 里的 append 父 task 直接可见（换成 int 就必须在父 task 汇总子 task 的返回值，
# 而多模态项分散在 stages.py / task_manager.py 两处，改不动）。
_vlm_degraded: ContextVar[Optional[list]] = ContextVar("jonex_vlm_degraded", default=None)


def set_ingest_ctx(
    tenant_id: str = "",
    kb_id: str = "",
    doc_id: str = "",
    trace_id: str = "",
    vlm_override: str = "",
    vlm_strict_mode: str = "",
) -> Token:
    """在任务入口设置当前入库任务的维度上下文。

    调用方应在 finally 中用返回的 Token 调用 reset_ingest_ctx。

    Args:
        vlm_override: [jonex] KB 级 VLM 覆盖（模型名）。空 = 不下发覆盖，引擎用
            自己的默认链（注册表 → ``VLM_MODEL_NAME``）—— 这与"没有配置"完全等价。
        vlm_strict_mode: [jonex] 降级处理策略：
            ``"fail"`` = reparse_strict（旧数据已删，运行期降级只留痕，靠预检兜底）；
            ``"record"`` = full / parse_only（降级比例超阈值即失败）；
            ``""`` = 不做严格判定（无覆盖时的默认，行为与从前逐字一致）。
    """
    # 新任务清零降级计数（同一事件循环里上一个任务的残留不能带进来）
    _vlm_degraded.set([])
    return _ingest_ctx.set({
        "tenant_id": tenant_id or "",
        "kb_id": kb_id or "",
        "doc_id": doc_id or "",
        "trace_id": trace_id or "",
        "vlm_override": vlm_override or "",
        "vlm_strict_mode": vlm_strict_mode or "",
    })


def get_ingest_ctx() -> Optional[dict]:
    """读取当前任务的计量维度上下文；无上下文返回 None。"""
    return _ingest_ctx.get()


def get_vlm_override() -> str:
    """[jonex] 当前任务的 KB 级 VLM 覆盖模型名；无覆盖返回空字符串。"""
    ctx = _ingest_ctx.get()
    if not ctx:
        return ""
    return str(ctx.get("vlm_override") or "")


def get_vlm_strict_mode() -> str:
    """[jonex] 当前任务的 VLM 降级策略：``fail`` | ``record`` | ``""``。"""
    ctx = _ingest_ctx.get()
    if not ctx:
        return ""
    return str(ctx.get("vlm_strict_mode") or "")


def record_vlm_degraded(reason: str = "") -> None:
    """[jonex] 记录一次 VLM 降级（占位/失败/空描述）。

    跨线程/子 task 共享同一个 list；计数只在任务内有效，由
    ``set_ingest_ctx`` 清零、``reset_vlm_degraded`` 收尾。
    """
    counter = _vlm_degraded.get()
    if counter is None:
        _vlm_degraded.set([reason or "unknown"])
        return
    counter.append(reason or "unknown")


def get_vlm_degraded_count() -> int:
    """[jonex] 当前任务累计的 VLM 降级次数。"""
    counter = _vlm_degraded.get()
    return len(counter) if counter else 0


def get_vlm_degraded_reasons() -> list:
    """[jonex] 降级原因列表（写任务结果/日志用，不含敏感信息）。"""
    counter = _vlm_degraded.get()
    return list(counter) if counter else []


def reset_vlm_degraded() -> None:
    """[jonex] 清空降级计数（任务收尾，避免污染下一任务）。"""
    _vlm_degraded.set([])


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
        dict: 非空维度（tenant/kb/doc/trace）对应的 X-Jonex-* 头；无上下文返回空 dict。

    ⚠️ 不输出 X-Jonex-Scene：scene 是**模型级**身份（VLM → raganything_vlm，
    文本 LLM/embedding → raganything_ingest），由 ModelSpec.extra_headers 提供。
    driver 中本函数的叠加顺序在 spec.extra_headers 之后，若此处也给 scene，
    会把模型级 scene 覆盖回单一值，导致 VLM 在 gateway 侧无法与文本调用区分。
    """
    ctx = _ingest_ctx.get()
    if not ctx:
        return {}
    headers: dict[str, str] = {}
    if ctx.get("tenant_id"):
        headers["X-Jonex-Tenant-Id"] = ctx["tenant_id"]
    if ctx.get("kb_id"):
        headers["X-Jonex-Kb-Id"] = ctx["kb_id"]
    if ctx.get("doc_id"):
        headers["X-Jonex-Doc-Id"] = ctx["doc_id"]
    if ctx.get("trace_id"):
        headers["X-Jonex-Trace-Id"] = ctx["trace_id"]
    return headers
