"""Legacy adapters — wrap BoundModel for old-style callable signatures.

These are EXPLICIT adapters (not implicit inference). Each adapter handles
one old calling convention.  Phase 3+ should migrate callers to BoundModel
directly and then delete these adapters.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from raganything.models.transformers import LLMResponseTransformer, NoOpTransformer

if TYPE_CHECKING:
    from raganything.models.types import BoundModel


def legacy_llm_adapter(
    bound: "BoundModel",
    transformer: LLMResponseTransformer | None = None,
):
    """Wrap BoundModel as old-style ``(prompt, system_prompt, **kw) -> str``.

    Used by: modalprocessors.py, video_processor.py (MapReduce).
    Signature: modal_caption_func(prompt, system_prompt="", max_tokens=128)

    Args:
        bound: The bound model to wrap.
        transformer: Optional response transformer for LLM output cleanup.
                     Defaults to NoOpTransformer (passthrough).
    """
    _transformer = transformer or NoOpTransformer()

    async def wrapper(prompt: str, system_prompt: str = "", **kwargs) -> str:
        messages: list[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        response = await bound.complete(messages, **kwargs)
        return _transformer.transform(response.text)

    # Attach capability info for callers that need it (video_processor VLM check)
    wrapper.model_capability = bound.capability  # type: ignore[attr-defined]
    wrapper.__name__ = f"legacy_llm({bound.spec.model_id})"  # type: ignore[attr-defined]
    return wrapper


def legacy_vlm_adapter(bound: "BoundModel"):
    """Wrap BoundModel as old-style ``(image_path, prompt) -> str``.

    Used by: video_processor.py (VLM keyframe description).
    Signature: vlm_model_func(image_path, prompt)

    Uses ``file://`` URI scheme for local image paths.
    ``normalize_images()`` in the driver handles base64 encoding.
    """

    async def wrapper(image_path: str, prompt: str, **kwargs) -> str:
        messages = [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"file://{image_path}"}},
        ]}]
        response = await bound.complete(messages, **kwargs)
        return response.text

    wrapper.model_capability = bound.capability  # type: ignore[attr-defined]
    wrapper.vlm_model = bound.spec.model_id  # type: ignore[attr-defined]
    wrapper.__name__ = f"legacy_vlm({bound.spec.model_id})"  # type: ignore[attr-defined]
    return wrapper


def base64_caption_adapter(bound: "BoundModel"):
    """Wrap BoundModel for base64 image caption calling convention.

    [jonex] 批次 2-C：匹配 modalprocessors.py ImageModalProcessor 的调用约定：
    ``(prompt, image_data=base64, system_prompt=...)`` → ``data:`` image_url。

    Used by: processor_builder.py image factory (modal_caption_func).
    Does NOT write temporary files — encodes base64 directly as a data URI.
    """

    async def wrapper(prompt: str, image_data: str = "", system_prompt: str = "", **kwargs) -> str:
        messages: list[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        content_parts: list[dict] = [{"type": "text", "text": prompt}]
        if image_data:
            # Auto-detect MIME type from base64 magic bytes, default to PNG
            if image_data.startswith("data:"):
                image_url = image_data
            elif image_data.startswith("/9j/"):
                image_url = f"data:image/jpeg;base64,{image_data}"
            elif image_data.startswith("iVBOR"):
                image_url = f"data:image/png;base64,{image_data}"
            elif image_data.startswith("UklGR"):
                image_url = f"data:image/webp;base64,{image_data}"
            elif image_data.startswith("R0lGOD"):
                image_url = f"data:image/gif;base64,{image_data}"
            else:
                image_url = f"data:image/png;base64,{image_data}"
            content_parts.append({"type": "image_url", "image_url": {"url": image_url}})

        messages.append({"role": "user", "content": content_parts})
        response = await bound.complete(messages, **kwargs)
        # [jonex] Some reasoning models (e.g. Qwen3.6 heretic) emit the
        # generated text in ``reasoning`` but leave ``content`` as an empty
        # string.  Fall back to reasoning when content is blank so downstream
        # JSON parsers get a non-empty payload.
        return response.text.strip() or response.reasoning or ""

    wrapper.model_capability = bound.capability  # type: ignore[attr-defined]
    wrapper.vlm_model = bound.spec.model_id  # type: ignore[attr-defined]
    wrapper.__name__ = f"base64_caption({bound.spec.model_id})"  # type: ignore[attr-defined]
    return wrapper
