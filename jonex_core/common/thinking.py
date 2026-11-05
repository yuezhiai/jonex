"""思考字段注入规则（平台与各引擎共用）。

把「怎么关思考」从代码里解耦成配置，写法：

    <字段路径>.<类型>[.<取值>][,...]

类型与取值：
    string  → enabled / disabled（省略取值 = disabled）
    bool    → true / false      （省略取值 = false）

示例：
    thinking.type.string                     → {"thinking": {"type": "disabled"}}
    thinking.type.string.enabled             → {"thinking": {"type": "enabled"}}
    chat_template_kwargs.enable_thinking.bool → {"chat_template_kwargs": {"enable_thinking": false}}
    extra_body.thinking.type.string.disabled → {"extra_body": {"thinking": {"type": "disabled"}}}

注意：本模块只负责「怎么写」，不保证上游认这个字段。字段被上游忽略时请求不会失败，
但思考也不会被关掉——新增/更换上游时请先用 curl 实测。
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("jonex.common.thinking")

#: 未配置时的默认写法（与历史 TokenHub 行为一致）
DEFAULT_FIELDS = "thinking.type.string"

_TYPES = {"string", "bool"}
_VALUES = {"string": ("enabled", "disabled"), "bool": ("true", "false")}
_DEFAULT_VALUE = {"string": "disabled", "bool": False}
_PATH_RE = re.compile(r"^[A-Za-z0-9_]+$")
_MAX_DEPTH = 5


def _parse_entry(item: str):
    """解析单条配置 → (path_parts, value)；非法返回 None。"""
    parts = item.split(".")
    if len(parts) < 2:
        return None

    if parts[-1].lower() in _TYPES:
        vtype = parts[-1].lower()
        path = parts[:-1]
        raw_value = None
    elif len(parts) >= 3 and parts[-2].lower() in _TYPES:
        vtype = parts[-2].lower()
        path = parts[:-2]
        raw_value = parts[-1].lower()
    else:
        return None

    if not path or len(path) > _MAX_DEPTH:
        return None
    if not all(_PATH_RE.match(p) for p in path):
        return None
    if raw_value is not None and raw_value not in _VALUES[vtype]:
        return None

    value = _DEFAULT_VALUE[vtype]
    if raw_value == "enabled":
        value = "enabled"
    elif raw_value == "true":
        value = True
    return path, value


def apply_thinking_fields(body: dict, spec: str | None, *, log_prefix: str = "") -> list[str]:
    """按配置向请求体注入字段，返回实际注入的字段路径列表。

    - ``spec`` 为空 → 使用 :data:`DEFAULT_FIELDS`
    - 调用方已显式设置的叶子字段不覆盖
    - 单条非法只告警跳过，不影响其余条目与请求本身
    """
    spec = (spec or "").strip() or DEFAULT_FIELDS
    injected: list[str] = []

    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        parsed = _parse_entry(item)
        if parsed is None:
            logger.warning("%s 思考字段配置非法，已跳过: %r", log_prefix, item)
            continue
        path, value = parsed

        node = body
        conflict = False
        for key in path[:-1]:
            nxt = node.get(key)
            if nxt is None:
                nxt = {}
                node[key] = nxt
            elif not isinstance(nxt, dict):
                logger.warning(
                    "%s 思考字段路径冲突（%s 不是对象），已跳过: %r",
                    log_prefix, key, item,
                )
                conflict = True
                break
            node = nxt
        if conflict:
            continue

        leaf = path[-1]
        if leaf in node:
            # 调用方显式声明优先
            continue
        node[leaf] = value
        injected.append(".".join(path))

    return injected
