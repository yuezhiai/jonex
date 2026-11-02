"""OpenAIDriver — OpenAI-compatible API (vLLM, Ollama, LM Studio, OpenAI)."""

from __future__ import annotations

import os
from typing import Any

from raganything.models.driver import BaseModelDriver
from raganything.models.types import (
    ModelCapability,
    ModelContentFilterError,
    ModelResponse,
    ModelSpec,
    Usage,
)


class OpenAIDriver(BaseModelDriver):
    """Driver for OpenAI-compatible APIs.

    Handles vLLM, Ollama, LM Studio, and official OpenAI — all share
    the same chat completions format.
    """

    bindings = ("openai", "vllm", "ollama", "lmstudio")

    async def complete(
        self,
        messages: list[dict],
        spec: ModelSpec,
        **kwargs,
    ) -> ModelResponse:
        """Send chat completion request → parse into ModelResponse.

        [jonex] Uses httpx directly (not openai_complete_if_cache) so metering
        headers (X-Jonex-*) from spec.extra_headers are injected into every
        LLM call — required for llm-gateway token accounting.
        """
        import httpx

        # [jonex] Gap A: lazy-import contextvar overlay to avoid circular imports
        try:
            from raganything.service.jonex_metering_ctx import (
                build_ingest_headers as _build_ingest_h,
            )
        except ImportError:
            _build_ingest_h = lambda: {}  # noqa: E731

        headers: dict[str, str] = {
            "Content-Type": "application/json",
            **spec.extra_headers,  # [jonex] X-Jonex-Tenant-Id, X-Jonex-Kb-Id, etc.
            # [jonex] Gap A: overlay per-task contextvar (doc_id/trace_id)
            # overrides build-time static tenant/kb with per-task values.
            **_build_ingest_h(),
        }
        if spec.api_key:
            headers["Authorization"] = f"Bearer {spec.api_key}"

        # [jonex] KB 级 VLM 覆盖（P2a）：只有**视觉请求**改用覆盖模型。
        # 文本 LLM / embedding 调用不带图片块，仍用 spec.model_id —— 否则同一个
        # 解析任务里的摘要/标签模型会被一起改掉。无覆盖时返回 spec.model_id，
        # 与改造前逐字节一致。
        try:
            from raganything.models.vlm_override import resolve_vision_model as _resolve_vlm
        except ImportError:  # pragma: no cover
            _resolve_vlm = lambda default, _messages: default  # noqa: E731
        model_id = _resolve_vlm(spec.model_id, messages)

        # Merge lightrag extras (temperature, max_tokens, top_p, etc.) into payload
        payload: dict = {
            "model": model_id,
            "messages": messages,
        }
        # [jonex] timeout 是 httpx 控制参数，不是上游请求字段——pop 后再合并，
        # 否则会把 {"timeout": 300} 发进请求体（llama.cpp 可能拒绝未知字段）。
        timeout = httpx.Timeout(float(kwargs.pop("timeout", 120.0)))
        for k, v in kwargs.items():
            if v is not None:
                payload[k] = v

        # [jonex] 思考控制：**仅对含图片的请求**注入（文本抽取保持原行为，
        # 避免误伤解析链）。写法由 VLM_THINKING_FIELDS 决定，缺省即历史
        # TokenHub 的 {"thinking": {"type": "disabled"}}。
        try:
            from raganything.models.vlm_override import has_image_part as _has_image
        except ImportError:  # pragma: no cover
            _has_image = None
        if _has_image is not None and _has_image(messages):
            try:
                from jonex_core.common.thinking import (
                    DEFAULT_FIELDS as _THINK_DEFAULT,
                    apply_thinking_fields as _apply_think,
                )
            except ImportError:  # 独立运行（无 jonex_core）时不注入
                _apply_think = None
            if _apply_think is not None:
                _think_spec = (
                    os.getenv("VLM_THINKING_FIELDS")
                    or ""
                ).strip()
                _apply_think(
                    payload,
                    _think_spec or _THINK_DEFAULT,
                    log_prefix="raganything.openai",
                )

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{spec.host}/chat/completions",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            raw = resp.json()

        return self._parse(raw, spec)

    # ── Response parsing (owned by this driver, no global parser) ─────

    def _parse(self, raw: Any, spec: ModelSpec) -> ModelResponse:
        """Parse raw API response → normalized ModelResponse.

        Strategy derived from capability.supports_thinking (SSOT).
        No independent ``response_parser`` config field.
        """
        cap = spec.capability

        if isinstance(raw, str):
            return ModelResponse(text=raw)

        if isinstance(raw, dict):
            return self._parse_dict(raw, cap, spec)

        return ModelResponse(text=str(raw))

    def _parse_dict(
        self, raw: dict, cap: ModelCapability, spec: ModelSpec
    ) -> ModelResponse:
        """Parse OpenAI-format dict response."""
        choice = (raw.get("choices") or [{}])[0]
        msg = choice.get("message") or {}  # [jonex] vLLM 错误响应可能 message: null，同 NoneType 防护
        text = msg.get("content", "") or ""
        # [jonex] Some backends (ollama heretic, llama.cpp) emit reasoning
        # under ``reasoning`` (no underscore) instead of the OpenAI-standard
        # ``reasoning_content``.  Always extract when available so downstream
        # adapters (base64_caption_adapter) can fall back when content=="".
        reasoning = msg.get("reasoning_content") or msg.get("reasoning") or None
        finish_reason = choice.get("finish_reason", "")

        # Content filter: empty text + content_filter → error, not silent
        if not text and finish_reason == "content_filter":
            raise ModelContentFilterError(
                f"Content filtered by model '{spec.model_id}'"
            )

        # Usage
        usage_raw = raw.get("usage", {})
        usage: Usage | None = None
        if usage_raw:
            # [jonex] vLLM 对非思考请求返回 completion_tokens_details: null，
            # dict.get(key, {}) 对「存在但为 null」返回 None 而非默认值，
            # 直接链式 .get() 会崩（NoneType has no attribute 'get'）。
            completion_details = usage_raw.get("completion_tokens_details") or {}
            reasoning_tokens = completion_details.get("reasoning_tokens", 0)
            usage = Usage(
                prompt_tokens=usage_raw.get("prompt_tokens", 0),
                completion_tokens=usage_raw.get("completion_tokens", 0),
                reasoning_tokens=reasoning_tokens,
            )

        # Metadata
        metadata: dict[str, Any] = {"model": raw.get("model", "")}
        if finish_reason:
            metadata["finish_reason"] = finish_reason
        if os.getenv("MODEL_DEBUG_RAW", "").lower() in ("1", "true"):
            metadata["raw"] = raw

        return ModelResponse(
            text=text,
            reasoning=reasoning,
            usage=usage,
            metadata=metadata,
        )
