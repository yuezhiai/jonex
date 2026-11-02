# [jonex] 新增文件：KB 级 VLM 覆盖的**视觉请求判定**
#
# 为什么需要单独判定"这是不是一次视觉请求"
# ----------------------------------------
# KB 级覆盖只该管**多模态理解**（图片/视频关键帧）。同一个解析任务里还会发生
# 文本 LLM 调用（摘要、标签）和 embedding —— 它们各有自己的模型来源，绝不能因为
# 这个 contextvar 被一起改掉。
#
# driver 拿到的 messages 是唯一可靠的判据：视觉调用必然带 image_url（或 anthropic
# 的 image source / gemini 的 inline_data）内容块，文本调用没有。因此这里按
# **消息内容**判定，而不是按 spec 判定 —— spec 是进程级绑定产物，同一次任务里
# VLM 与 LLM 用的是不同 spec，但 preset 链路上也可能共用，只有内容不会骗人。
#
# 设计见 docs/knowledge-base/kb-model-settings-design.md §5.1.1。

from __future__ import annotations

from typing import Any, Optional

#: OpenAI 兼容 / vLLM 的图片内容类型
_OPENAI_IMAGE_TYPES = frozenset({"image_url", "input_image"})
#: anthropic 风格的图片块
_ANTHROPIC_IMAGE_TYPES = frozenset({"image"})
#: gemini 风格的内联数据块
_GEMINI_IMAGE_TYPES = frozenset({"inline_data", "inlineData"})


def has_image_part(messages: Optional[list]) -> bool:
    """messages 里是否含图片内容块（= 这是一次视觉调用）。"""
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            continue
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                part_type = part.get("type")
                if part_type in _OPENAI_IMAGE_TYPES:
                    return True
                if part_type in _ANTHROPIC_IMAGE_TYPES:
                    return True
                if part_type in _GEMINI_IMAGE_TYPES:
                    return True
    return False


def resolve_vision_model(default_model_id: str, messages: Optional[list]) -> str:
    """视觉请求用 KB 覆盖模型，其余（文本/embedding）一律用默认绑定模型。

    **无覆盖时必须返回 default_model_id** —— 这是"不配置就与今天完全一致"的落点：
    这里不做任何 env 兜底，引擎侧仍走注册表 → ``VLM_MODEL_NAME`` 的既有链路。
    """
    try:
        from raganything.service.jonex_metering_ctx import get_vlm_override
    except Exception:  # pragma: no cover - 极端导入失败时退回默认
        return default_model_id
    override = (get_vlm_override() or "").strip()
    if override and has_image_part(messages):
        return override
    return default_model_id


def effective_vlm_model(default_model_id: str) -> str:
    """当前任务实际会用的 VLM 模型名（日志/结果留痕用，不做请求判定）。"""
    try:
        from raganything.service.jonex_metering_ctx import get_vlm_override
    except Exception:  # pragma: no cover
        return default_model_id
    return (get_vlm_override() or "").strip() or default_model_id


__all__ = ["effective_vlm_model", "has_image_part", "resolve_vision_model"]
