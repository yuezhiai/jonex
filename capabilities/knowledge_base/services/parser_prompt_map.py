#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""解析器类目 → atomic-rag 主解析提示词 code 映射 + 轻量模板预校验。

见 docs/parser-prompt-integration-plan.md §2/§4.6。

本期 KB 只创建并生效 4 个 prompt_code：
  generic_prompt / vision_prompt / audio_global_prompt / video_global_prompt。
table/equation 等文档内模态一律用 atomic-rag 内置 prompt，KB 不维护。

权威占位符白名单校验在 atomic-rag 侧 create_prompt/update_prompt handler
（从 PROMPTS 派生）；KB 侧只做括号成对的轻量预检，提前拦明显错误。

[jonex] auto-escape：用户常直接在 prompt 中嵌入 JSON 示例（如
``{"key": "value"}``），Python string.Formatter 将 ``{``/``}`` 视为非法占位符。
本模块在预检失败时尝试自动转义——将非占位符的 ``{``/``}`` 转为
``{{``/``}}``，保留形如 ``{ident}`` 的合法占位符不动。
"""
import re
import string
from typing import Optional

from jonex_core.common.exceptions import InvalidParameterError
from jonex_core.common.i18n import translate


# parser_type -> (preset_name（元数据）, prompt_code（生效键）, category)
PARSER_PROMPT_MAP: dict[str, tuple[str, str, str]] = {
    "document": ("document", "generic_prompt", "analysis"),
    "txt": ("text_parse", "generic_prompt", "analysis"),
    "image": ("image", "vision_prompt", "analysis"),
    "audio": ("audio_transcribe", "audio_global_prompt", "analysis"),
    "video": ("video_full_pipeline", "video_global_prompt", "analysis"),
    "web": ("document", "generic_prompt", "analysis"),
    "cad": ("document", "generic_prompt", "analysis"),
}

# Regex matching a Python format placeholder: {name} or {name.attr} or {name[idx]}
_PLACEHOLDER_RE = re.compile(
    r"\{([a-zA-Z_][a-zA-Z0-9_]*)(\.[a-zA-Z_][a-zA-Z0-9_]*|\[[0-9]+\])*\}",
)


def prompt_target(parser_type: str) -> Optional[tuple[str, str, str]]:
    """返回 (preset_name, prompt_code, category)；未映射类目返回 None（不下发 prompt）。"""
    return PARSER_PROMPT_MAP.get((parser_type or "").strip().lower())


def _needs_auto_escape(content: str) -> bool:
    """Check whether content has {{ }} that look like JSON, not Python placeholders.

    ``string.Formatter().parse()`` treats *any* balanced ``{ ... }`` as a format
    field, so ``{"key": "value"}`` is parsed as a field named ``"key"`` (which is
    not a valid Python identifier).  Return True when such fields are detected.
    """
    try:
        for _lit, field, _spec, _conv in string.Formatter().parse(content):
            if field is not None and field != "" and not field.isidentifier():
                return True
    except ValueError:
        return True  # unbalanced braces
    return False


def _auto_escape_braces(content: str) -> str:
    """Auto-escape literal { } to {{ }} while preserving {ident}-style placeholders.

    Strategy: temporarily replace all valid identifier fields with sentinel
    tokens, escape the remaining braces, then restore the placeholders.  This lets
    users write prompts containing JSON examples without manual double-bracing.
    """
    sentinel_map: dict[str, str] = {}
    counter = 0

    def _save(m: re.Match) -> str:
        nonlocal counter
        key = f"\x00PH\x00{counter}\x00"
        counter += 1
        sentinel_map[key] = m.group(0)
        return key

    # Step 1: save {ident}, {ident.attr}, {ident[idx]} patterns
    content = _PLACEHOLDER_RE.sub(_save, content)
    # Step 2: escape all remaining braces
    content = content.replace("{", "{{").replace("}", "}}")
    # Step 3: restore saved placeholders
    for key, original in sentinel_map.items():
        content = content.replace(key, original)
    return content


def precheck_prompt_template(content: str) -> str:
    """KB 侧轻量预检 + 自动转义。

    若原样是干净的 format 模板 → 原样返回。
    若检测到 JSON 字面大括号（字段名非 Python 合法标识符）→ 自动转义 {{ }}，
    保留 {ident} 占位符。仍失败则抛 InvalidParameterError。

    返回最终应落库的内容（已转义，如需要）。
    """
    if content is None:
        return ""
    # No JSON-like braces → return as-is
    if not _needs_auto_escape(content):
        return content
    # Try auto-escape
    escaped = _auto_escape_braces(content)
    if not _needs_auto_escape(escaped):
        return escaped
    # Still invalid after escaping → reject
    raise InvalidParameterError(
        message=translate(
            "err.prompt.template_brace_invalid",
            params={"error": "提示词中大括号语法无法自动纠正"},
            fallback="提示词模板大括号非法，字面大括号请写成 {{ }}",
        ),
        details={"error": "auto-escape failed"},
    )


__all__ = ["PARSER_PROMPT_MAP", "prompt_target", "precheck_prompt_template"]
