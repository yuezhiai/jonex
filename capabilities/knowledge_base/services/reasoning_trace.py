"""编排推理链采集器"""
import time
from typing import Any, Optional

from .reasoning_io import (
    REASONING_IO_ENABLED,
    ReasoningContext,
    build_step_io,
    sanitize_step_io,
)


class ReasoningCollector:
    """编排推理链采集器。enabled=False 时全部方法为 no-op，零开销。

    `context` 是本请求的展示上下文（检索问题 / 知识库范围 / mode / top_k），
    用于在调用点没有显式传 I/O 时自动派生「输入参数」。
    """

    def __init__(
        self,
        enabled: bool,
        *,
        context: Optional[ReasoningContext] = None,
    ):
        self.enabled = enabled
        self._ctx = context or ReasoningContext()
        self._steps: list[dict] = []
        self._t0 = time.perf_counter()
        self._io_truncated = False

    def step(
        self,
        stage: str,
        title: str,
        *,
        status: str = "done",
        summary: Optional[str] = None,
        detail: Optional[dict[str, Any]] = None,
        input: Optional[list[dict]] = None,
        output: Optional[list[dict]] = None,
        t_start: Optional[float] = None,
        duration_ms: Optional[int] = None,
    ) -> None:
        if not self.enabled:
            return
        now = time.perf_counter()
        # 显式 duration_ms 优先：mix 合并子链步骤时原耗时已经算好，
        # 不能再按「合并时刻 - 缺失的 t_start」重算（会变成 None）。
        if duration_ms is None and t_start is not None:
            duration_ms = int((now - t_start) * 1000)
        step: dict[str, Any] = {
            "stage": stage,
            "title": title,
            "status": status,
            "summary": summary,
            "detail": detail,
            "duration_ms": duration_ms,
        }
        if REASONING_IO_ENABLED:
            auto_in: list[dict] = []
            auto_out: list[dict] = []
            auto_cut = False
            if input is None or output is None:
                auto_in, auto_out, auto_cut = build_step_io(
                    stage, summary=summary, detail=detail, ctx=self._ctx,
                )
            final_in = auto_in if input is None else input
            final_out = auto_out if output is None else output
            # 显式传入的 I/O 也必须过脱敏/裁剪，调用点无法绕过。
            final_in, final_out, cut = sanitize_step_io(final_in, final_out)
            step["input"] = final_in
            step["output"] = final_out
            self._io_truncated = self._io_truncated or auto_cut or cut
        self._steps.append(step)

    def build(
        self, final_source: str, *, strategy: Optional[str] = None,
    ) -> Optional[dict]:
        if not self.enabled:
            return None
        trace: dict[str, Any] = {
            "steps": self._steps,
            "final_source": final_source,
            "total_ms": int((time.perf_counter() - self._t0) * 1000),
        }
        # strategy 是「实际走了哪条检索策略」，与 final_source（答题来源）
        # 可能不同：/search/mix 全 lightrag 时 strategy=ontology、source=ontology；
        # 混合多路时 strategy=mixed。不传时前端回退到 final_source。
        if strategy:
            trace["strategy"] = strategy
        if self._ctx.kb_names:
            trace["kb_names"] = dict(self._ctx.kb_names)
        if REASONING_IO_ENABLED and self._io_truncated:
            trace["io_truncated"] = True
        return trace


__all__ = ["ReasoningCollector"]
