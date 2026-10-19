"""[bbox-port] 引用 bbox 定位的 KB 侧纯函数。

两件事，都不碰 IO / 数据库：

- `normalize_spans`：把引擎投影里读回的 span 明细收敛成前端可用的最小形状
  （过滤坏 bbox、排序、截断）。脏 bbox 只应导致「这段没框」，**绝不允许抛错**
  —— 调用方是「打开原文」点击路径，降级就该静默跳到页码。
- `anchors_match`：引用里的位置锚点（page/table/row/image）与引擎当前 chunk 的
  `file_source` 锚点串比对，用来挡住「引用是旧的、chunk 已重切」的错位框选。

`expect` 里为空的项表示前端没带该锚点（旧引用 / 非表格 / 非图片 chunk）——
**双方都有值才比**，任一方缺失就不判为不一致（保守放行）。
"""
from __future__ import annotations

import logging
import math
from typing import Any

from jonex_core.common.file_source_util import parse_file_source

logger = logging.getLogger(__name__)

# 单次定位最多回多少个 span。超过就是「这一 chunk 的版式碎片极多」，
# 多到再画也读不出重点，截断即可（引擎侧投影本身按 span 存，不设上限）。
SOURCE_SPANS_MAX = 200

# bbox 坐标值域（MinerU 版面坐标，0–1000 归一化）
_BBOX_MIN = 0.0
_BBOX_MAX = 1000.0

# 需要「双方都有值才比」的直接相等锚点
_EQUALITY_ANCHORS = ("table_idx", "row_start", "row_end", "image_idx")


def _finite_float(value: Any) -> float | None:
    """把 bbox 分量转成有限 float；非数值 / NaN / inf 一律 None（= 丢弃）。"""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _as_int(value: Any) -> int | None:
    """把锚点值收敛成 int；不可转换（None / bool / 非数字字符串等）返回 None。

    `expect` 来自引用 location，正常是 int；但直接打 Sidecar `/invoke` 的调用方
    可以传 JSON 字符串（FastAPI 的 query 解析被绕过）。这里统一在比较前收敛，
    既让 `"2"` == 2，也让 `"x"` 这种无法判定的值退化成「没提供该锚点」——
    `anchors_match` 处在「打开原文」的点击路径上，**绝不能因为锚点形状抛错**。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        # 只为兼容 JSON 数字（2.0）；非整数值不当作页码/序号。
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except (TypeError, ValueError):
            return None
    return None


def _bbox_of(span: dict) -> list[float] | None:
    """校验并归一化 span 的 bbox；非法返回 None。

    合法 = 恰好 4 个有限数、每项 ∈[0,1000]、x0<=x1 且 y0<=y1。
    """
    raw = span.get("bbox")
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    box = [_finite_float(v) for v in raw]
    if any(v is None for v in box):
        return None
    x0, y0, x1, y1 = box  # type: ignore[misc]
    if not (_BBOX_MIN <= x0 <= _BBOX_MAX and _BBOX_MIN <= y0 <= _BBOX_MAX
            and _BBOX_MIN <= x1 <= _BBOX_MAX and _BBOX_MIN <= y1 <= _BBOX_MAX):
        return None
    if x0 > x1 or y0 > y1:
        return None
    return [x0, y0, x1, y1]


def normalize_spans(raw: list[dict] | None) -> list[dict]:
    """过滤非法 span、按 `ordinal` 升序、截断到 `SOURCE_SPANS_MAX`。

    只保留 `page_idx is not None` 的项：没有页码的框不知道该画在哪一页。
    输出字段固定为 `{page_idx, bbox, origin, span_type, ordinal}` —— 其余引擎
    明细（span_id / 文本偏移 / 时间轴）不在定位页消费，不透出。
    """
    out: list[dict] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        if item.get("page_idx") is None:
            continue
        bbox = _bbox_of(item)
        if bbox is None:
            continue
        out.append({
            "page_idx": item["page_idx"],
            "bbox": bbox,
            "origin": item.get("origin") or "top-left",
            "span_type": item.get("span_type") or "text",
            "ordinal": item.get("ordinal") if item.get("ordinal") is not None else 0,
        })
    out.sort(key=lambda s: s["ordinal"])
    if len(out) > SOURCE_SPANS_MAX:
        logger.warning(
            "source_spans_truncated total=%d max=%d", len(out), SOURCE_SPANS_MAX
        )
        out = out[:SOURCE_SPANS_MAX]
    return out


def anchors_match(expect: dict, file_source: str) -> bool:
    """引用锚点与当前 chunk 的 `file_source` 是否仍指向同一处。

    - `page_no`：按引擎 chunk 的 `page..page_end` 区间判定（跨页打包 chunk 的
      末页见 `page_end`；旧 chunk 无该键时区间退化为单页）；
    - `table_idx` / `row_start` / `row_end` / `image_idx`：直接相等；
    - 任一项任一方缺值 ⇒ 该项跳过（无法判定就不判不一致）。

    `expect` 为空（或全为 None）返回 True：没有可比的锚点，就当一致。
    """
    if not isinstance(expect, dict) or not expect:
        return True
    parsed = parse_file_source(file_source or "")

    # 预期锚点与引擎锚点都先收敛成 int：预期侧可能带 JSON 字符串（直接
    # `/invoke` 绕过 FastAPI query 的 int 解析），实际侧也按同一口径比较。
    expected_page = _as_int(expect.get("page_no"))
    actual_page = _as_int(parsed.get("page_no"))
    if expected_page is not None and actual_page is not None:
        last_page = _as_int(parsed.get("page_end"))
        if last_page is None:
            last_page = actual_page
        if not (actual_page <= expected_page <= last_page):
            return False

    for key in _EQUALITY_ANCHORS:
        expected = _as_int(expect.get(key))
        actual = _as_int(parsed.get(key))
        if expected is not None and actual is not None and expected != actual:
            return False

    return True
