#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""`hints`（BIRD evidence / 业务口径）的归一化与边界。

调用方可能给 `str` 或 `list[str]`；这里统一成 `list[str]`，并 fail-closed：
- ≤10 条；
- 单条 ≤500 字符；
- 合计 ≤2000 字符；
- 只接受字符串，空白丢弃、重复折叠（保序）。

非法输入抛 `InvalidParameterError`（调用方用错，不是「这个源答不了」）。
"""
from __future__ import annotations

from jonex_core.common.exceptions import InvalidParameterError

MAX_HINTS = 10
MAX_HINT_CHARS = 500
MAX_HINTS_TOTAL_CHARS = 2000


def normalize_hints(value) -> list[str]:
    """`None` / `str` / `list[str]` → 去空白、去重（保序）后的列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise InvalidParameterError("hints 必须是字符串或字符串数组")

    out: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise InvalidParameterError("hints 的每一项都必须是字符串")
        text = item.strip()
        if not text:
            continue
        if len(text) > MAX_HINT_CHARS:
            raise InvalidParameterError(
                f"单条 hints 超过 {MAX_HINT_CHARS} 字符（本次 {len(text)}）")
        if text not in out:
            out.append(text)

    if len(out) > MAX_HINTS:
        raise InvalidParameterError(f"hints 最多 {MAX_HINTS} 条（本次 {len(out)}）")
    total = sum(len(item) for item in out)
    if total > MAX_HINTS_TOTAL_CHARS:
        raise InvalidParameterError(
            f"hints 合计超过 {MAX_HINTS_TOTAL_CHARS} 字符（本次 {total}）")
    return out


def merge_hints(base: list[str], extra: list[str]) -> list[str]:
    """在已归一化的 hints 上追加（去重、按条数/单条/总长预算**丢弃**，不抛）。

    用于 P1-2 的 evidence 改写结果：改写是增能，超预算时宁可少一条也不能让提问 1001。
    """
    out: list[str] = list(base or [])
    total = sum(len(item) for item in out)
    for item in extra or []:
        text = str(item or "").strip()
        if not text or text in out:
            continue
        text = text[:MAX_HINT_CHARS]
        if len(out) >= MAX_HINTS or total + len(text) > MAX_HINTS_TOTAL_CHARS:
            continue
        out.append(text)
        total += len(text)
    return out


#: 一条指标 hint 的单条长度 / 公式预算 / 展示别名上限。
#: 生产自动 hints 与演化台验真卡**共用一个构造器**：两边口径必须逐字相同，
#: 否则「验真看到的」和「生产注入的」不是同一条提示。
METRIC_HINT_MAX_CHARS = MAX_HINT_CHARS
METRIC_HINT_FORMULA_MAX = 200
METRIC_HINT_MAX_ALIASES = 5


def build_metric_hint_text(*, name, definition: str = "",
                           formula: str = "", aliases=None) -> str:
    """构造一条指标 hint：`指标 {name}（aliases）：{definition}；公式：{formula}`。

    单条必须 ≤ `MAX_HINT_CHARS`：先按固定部分（名称/别名/标点）算 overhead，
    再 `formula[:200]` **优先保完整**，definition 用剩余预算 —— 长定义不能把
    公式挤出通道上限（那会让模型只看到半条公式，与验真口径不一致）。
    返回空串表示条目不参与（name 为空）。
    """
    name = str(name or "").strip()
    if not name:
        return ""
    alias_list: list[str] = []
    for raw in aliases or []:
        alias = str(raw or "").strip()
        if alias:
            alias_list.append(alias)
        if len(alias_list) >= METRIC_HINT_MAX_ALIASES:
            break
    alias_part = f"（{'、'.join(alias_list)}）" if alias_list else ""
    formula = str(formula or "").strip()
    prefix = f"指标 {name}{alias_part}："
    suffix = "；公式：" if formula else ""
    overhead = len(prefix) + len(suffix)
    budget = max(0, METRIC_HINT_MAX_CHARS - overhead)
    formula_capped = formula[:METRIC_HINT_FORMULA_MAX]
    definition = str(definition or "").strip()
    definition_capped = definition[:max(0, budget - len(formula_capped))]
    return f"{prefix}{definition_capped}{suffix}{formula_capped}"


__all__ = ["MAX_HINTS", "MAX_HINT_CHARS", "MAX_HINTS_TOTAL_CHARS",
           "normalize_hints", "merge_hints", "build_metric_hint_text",
           "METRIC_HINT_MAX_CHARS", "METRIC_HINT_FORMULA_MAX",
           "METRIC_HINT_MAX_ALIASES"]
