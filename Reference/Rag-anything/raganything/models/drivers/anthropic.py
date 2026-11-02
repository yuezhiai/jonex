"""AnthropicDriver — Anthropic Messages API protocol adapter.

TokenHub / Anthropic use a different API format from OpenAI:
  - Endpoint: POST /v1/messages (not /chat/completions)
  - Auth: x-api-key header (not Authorization: Bearer)
  - Request: {model, max_tokens, messages, system?}
  - Response: {content: [{type:"text", text:"..."}], usage: {input_tokens, output_tokens}}
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from raganything.models.driver import BaseModelDriver
from raganything.models.types import (
    ModelCapability,
    ModelContentFilterError,
    ModelDriverError,
    ModelResponse,
    ModelSpec,
    Usage,
)


class AnthropicDriver(BaseModelDriver):
    """Driver for Anthropic-compatible APIs (TokenHub, Claude, etc.).

    Handles:
      - OpenAI-format messages → Anthropic messages
      - System prompt → top-level ``system`` field (Anthropic convention)
      - Anthropic response → normalized ModelResponse
    """

    bindings = ("anthropic", "tokenhub")

    async def complete(
        self,
        messages: list[dict],
        spec: ModelSpec,
        **kwargs,
    ) -> ModelResponse:
        """Send request to Anthropic-compatible API → parse response."""
        body = self._build_request(messages, spec, **kwargs)
        url = f"{spec.host.rstrip('/')}/v1/messages"
        raw = await self._send_request(url, body, spec)
        return self._parse_response(raw, spec)

    # ── Request building ────────────────────────────────────────────

    def _build_request(
        self, messages: list[dict], spec: ModelSpec, **kwargs
    ) -> dict:
        """Convert OpenAI-format messages to Anthropic format.

        Anthropic:
          - system prompt → top-level "system" field (string or list)
          - messages must alternate user/assistant
          - image → content array with {"type":"image","source":{...}}
        """
        anthropic_messages: list[dict] = []
        system_text: str | None = None

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if role == "system":
                system_text = content if isinstance(content, str) else ""
                continue

            # Anthropic roles: "user" or "assistant"
            anthropic_role = "assistant" if role == "assistant" else "user"

            if isinstance(content, str):
                anthropic_messages.append({"role": anthropic_role, "content": content})
            elif isinstance(content, list):
                parts = self._convert_content_parts(content)
                anthropic_messages.append({"role": anthropic_role, "content": parts})

        # [jonex] KB 级 VLM 覆盖：只有视觉请求改用覆盖模型（同 openai driver）。
        try:
            from raganything.models.vlm_override import resolve_vision_model as _resolve_vlm
        except ImportError:  # pragma: no cover
            _resolve_vlm = lambda default, _messages: default  # noqa: E731

        body: dict[str, Any] = {
            "model": _resolve_vlm(spec.model_id, anthropic_messages),
            "max_tokens": kwargs.pop("max_tokens", 1024),
            "messages": anthropic_messages,
        }
        if system_text:
            body["system"] = system_text
        if "temperature" in kwargs:
            body["temperature"] = kwargs.pop("temperature")

        return body

    def _convert_content_parts(self, parts: list[dict]) -> list[dict]:
        """Convert OpenAI content parts to Anthropic content blocks."""
        result: list[dict] = []
        for part in parts:
            if part.get("type") == "text":
                result.append({"type": "text", "text": part.get("text", "")})
            elif part.get("type") == "image_url":
                url = part.get("image_url", {}).get("url", "")
                if url.startswith("data:"):
                    header, data = url.split(",", 1) if "," in url else ("", url)
                    mime = "image/jpeg"
                    if ":" in header:
                        mime = header.split(":")[1].split(";")[0]
                    result.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime,
                            "data": data,
                        },
                    })
                elif url.startswith("file://"):
                    import base64 as b64
                    path = url[7:]
                    mime = "image/jpeg"
                    for ext, mt in {
                        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                        ".png": "image/png", ".webp": "image/webp",
                    }.items():
                        if path.lower().endswith(ext):
                            mime = mt
                            break
                    try:
                        with open(path, "rb") as f:
                            img_data = b64.b64encode(f.read()).decode()
                    except FileNotFoundError:
                        raise ModelDriverError(f"Image file not found: {path}")
                    result.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": mime, "data": img_data},
                    })
                else:
                    result.append({"type": "text", "text": f"[image: {url}]"})
        return result

    # ── HTTP ────────────────────────────────────────────────────────

    async def _send_request(self, url: str, body: dict, spec: ModelSpec) -> dict:
        # [jonex] Gap A: lazy-import contextvar overlay to avoid circular imports
        try:
            from raganything.service.jonex_metering_ctx import (
                build_ingest_headers as _build_ingest_h,
            )
        except ImportError:
            _build_ingest_h = lambda: {}  # noqa: E731

        headers = {
            "Content-Type": "application/json",
            "x-api-key": spec.api_key,
            "anthropic-version": "2023-06-01",
            **spec.extra_headers,  # [jonex] X-Jonex-Tenant-Id, X-Jonex-Kb-Id static fallback
            # [jonex] Gap A: overlay per-task contextvar (doc_id/trace_id)
            **_build_ingest_h(),
        }
        async with httpx.AsyncClient(timeout=120.0) as client:
            try:
                resp = await client.post(url, json=body, headers=headers)
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                detail = e.response.text[:200]
                if status in (401, 403):
                    from raganything.models.types import ModelAuthError
                    raise ModelAuthError(f"Anthropic auth failed: {detail}")
                elif status == 429:
                    from raganything.models.types import ModelRateLimitError
                    raise ModelRateLimitError(f"Anthropic rate limited: {detail}")
                elif status >= 500:
                    from raganything.models.types import ModelServerError
                    raise ModelServerError(f"Anthropic server error {status}: {detail}")
                raise ModelDriverError(f"Anthropic HTTP {status}: {detail}") from e
            except httpx.TimeoutException:
                from raganything.models.types import ModelTimeoutError
                raise ModelTimeoutError("Anthropic request timed out")

    # ── Response parsing ─────────────────────────────────────────────

    def _parse_response(self, raw: dict, spec: ModelSpec) -> ModelResponse:
        """Parse Anthropic Messages response → ModelResponse."""
        cap = spec.capability

        # Anthropic response: {"content": [{"type":"text","text":"..."}], ...}
        content_blocks = raw.get("content", [])
        text = "".join(
            b.get("text", "") for b in content_blocks if b.get("type") == "text"
        )

        # Thinking/reasoning (if present for thinking models)
        reasoning = None
        if cap.supports_thinking:
            thoughts = [
                b.get("thinking", "") or b.get("text", "")
                for b in content_blocks
                if b.get("type") == "thinking"
            ]
            if thoughts:
                reasoning = "\n".join(thoughts)

        # Stop reason
        stop_reason = raw.get("stop_reason", "")

        # Usage
        usage_raw = raw.get("usage", {})
        usage = Usage(
            prompt_tokens=usage_raw.get("input_tokens", 0),
            completion_tokens=usage_raw.get("output_tokens", 0),
        ) if usage_raw else None

        metadata: dict[str, Any] = {"model": raw.get("model", spec.model_id)}
        if stop_reason:
            metadata["stop_reason"] = stop_reason
        if os.getenv("MODEL_DEBUG_RAW", "").lower() in ("1", "true"):
            metadata["raw"] = raw

        return ModelResponse(
            text=text, reasoning=reasoning,
            usage=usage, metadata=metadata,
        )
