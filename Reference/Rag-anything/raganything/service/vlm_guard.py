# [jonex] 新增文件：KB 级 VLM 覆盖的**预检与降级护栏**
#
# 为什么需要预检（而不是"跑挂了再说"）
# ------------------------------------
# 1. ``modalprocessors`` 对 VLM 失败是**宽容**的：重试 3 次仍失败会写
#    ``"Image content: ..."`` 占位并让任务成功（:1253-1266）。所以"模型名填错"今天
#    表现为"任务成功但图片全空"，用户看不出问题。
# 2. ``reparse_strict`` 会在多模态**之前**就删光旧 doc（task_manager.py 的
#    _reparse_delete_all_old）。等跑到多模态才判定失败时，用户数据已经没了。
#
# 因此：有覆盖时先打一发极小的真实 VLM 请求（1x1 PNG + max_tokens=1）。
# 模型名错 / 无权限 / 端点不通 ⇒ 在**删数据之前**失败，且错误码归类为
# MODEL_NOT_FOUND，前端可读。
#
# 设计见 docs/knowledge-base/kb-model-settings-design.md §5.1.3。

from __future__ import annotations

import asyncio
import base64
import logging
import os
from typing import Optional

from raganything.service.jonex_metering_ctx import (
    get_vlm_degraded_count,
    get_vlm_override,
    get_vlm_strict_mode,
)

logger = logging.getLogger(__name__)

#: 降级比例阈值（仅 full / parse_only 生效）。默认 50%：超过一半多模态项降级，
#: 基本可以断定模型不可用，而不是"个别图片有问题"。
DEFAULT_STRICT_FAIL_RATIO = 0.5
STRICT_RATIO_ENV = "VLM_STRICT_FAIL_RATIO"

#: 预检单次超时（秒）。预检是"能不能用"的探测，不该等满常规 VLM 超时。
DEFAULT_PRECHECK_TIMEOUT = 20.0
PRECHECK_TIMEOUT_ENV = "VLM_PRECHECK_TIMEOUT"

#: 1x1 透明 PNG（67 字节）——最小合法图片，用来触发一次真实视觉调用。
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)


class VlmOverridePrecheckError(RuntimeError):
    """KB 级 VLM 覆盖预检失败（模型不可用）。归类为 MODEL_NOT_FOUND。"""

    code = "MODEL_NOT_FOUND"


class VlmStrictModeError(RuntimeError):
    """多模态降级比例超过阈值，任务判失败。归类为 VLM_STRICT_MODE。"""

    code = "VLM_STRICT_MODE"


def strict_fail_ratio() -> float:
    """降级阈值（0~1）。非法配置回落默认值，不让一个坏 env 把任务全判失败。"""
    raw = os.getenv(STRICT_RATIO_ENV, "")
    if not raw:
        return DEFAULT_STRICT_FAIL_RATIO
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("[jonex] %s=%r 不是数字，回落 %.2f", STRICT_RATIO_ENV, raw,
                       DEFAULT_STRICT_FAIL_RATIO)
        return DEFAULT_STRICT_FAIL_RATIO
    if value < 0 or value > 1:
        logger.warning("[jonex] %s=%r 超出 0~1，回落 %.2f", STRICT_RATIO_ENV, raw,
                       DEFAULT_STRICT_FAIL_RATIO)
        return DEFAULT_STRICT_FAIL_RATIO
    return value


def _precheck_timeout() -> float:
    raw = os.getenv(PRECHECK_TIMEOUT_ENV, "")
    try:
        value = float(raw) if raw else DEFAULT_PRECHECK_TIMEOUT
    except (TypeError, ValueError):
        value = DEFAULT_PRECHECK_TIMEOUT
    return value if value > 0 else DEFAULT_PRECHECK_TIMEOUT


async def precheck_vlm_override(bound, *, stage: str = "") -> None:
    """在删除旧数据/开始解析之前，用覆盖模型打一发极小视觉请求。

    Args:
        bound: ``BoundModel``（process 级 VLM 绑定）。None 表示进程没绑定 VLM ——
            此时**不能**静默放过：有覆盖却用不了，用户会以为配置生效了。
        stage: 日志里的阶段名（``reparse_strict`` / ``full`` / ``parse_only``）。

    Raises:
        VlmOverridePrecheckError: 覆盖模型不可用（模型名错、鉴权失败、端点不通、超时）。
    """
    override = (get_vlm_override() or "").strip()
    if not override:
        return
    if bound is None:
        raise VlmOverridePrecheckError(
            f"VLM 覆盖 '{override}' 无法生效：本进程未绑定 VLM（检查 VLM 绑定配置）"
        )

    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "ping"},
            {"type": "image_url", "image_url": {
                "url": f"data:image/png;base64,{_TINY_PNG_B64}",
            }},
        ],
    }]
    timeout = _precheck_timeout()
    try:
        await asyncio.wait_for(
            bound.complete(messages, max_tokens=1, timeout=timeout),
            timeout=timeout + 5,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 任何失败都意味着覆盖不可用
        raise VlmOverridePrecheckError(
            f"VLM 覆盖 '{override}' 预检失败（stage={stage or '-'}）："
            f"{type(exc).__name__}: {str(exc)[:200]}"
        ) from exc
    logger.info("[jonex] VLM 覆盖预检通过：%s（stage=%s）", override, stage or "-")


def guard_vlm_degraded(total: int, degraded: Optional[int] = None, *, stage: str = "") -> None:
    """降级比例护栏：仅在有覆盖且 strict_mode == ``record`` 时判定失败。

    - 无覆盖（未配置 KB 模型）⇒ 完全不介入，行为与改造前一致；
    - ``fail``（reparse_strict）⇒ 旧数据已删，运行期降级只留痕，不判失败；
    - ``record``（full / parse_only）⇒ 比例 > 阈值即失败，避免"任务成功但内容全空"。

    Raises:
        VlmStrictModeError: 降级比例超过阈值。
    """
    override = (get_vlm_override() or "").strip()
    mode = get_vlm_strict_mode()
    if not override or mode != "record":
        return

    total = max(0, int(total or 0))
    counted = get_vlm_degraded_count() if degraded is None else int(degraded)
    if total <= 0 or counted <= 0:
        return

    ratio = counted / total
    threshold = strict_fail_ratio()
    logger.info(
        "[jonex] VLM 降级比例：%d/%d = %.2f（阈值 %.2f，模型=%s，stage=%s）",
        counted, total, ratio, threshold, override, stage or "-",
    )
    if ratio > threshold:
        raise VlmStrictModeError(
            f"VLM_STRICT_MODE: 模型 '{override}' 降级 {counted}/{total}（{ratio:.0%}）"
            f"超过阈值 {threshold:.0%}，任务判失败（stage={stage or '-'}）"
        )


__all__ = [
    "DEFAULT_PRECHECK_TIMEOUT",
    "DEFAULT_STRICT_FAIL_RATIO",
    "PRECHECK_TIMEOUT_ENV",
    "STRICT_RATIO_ENV",
    "VlmOverridePrecheckError",
    "VlmStrictModeError",
    "guard_vlm_degraded",
    "precheck_vlm_override",
    "strict_fail_ratio",
]
