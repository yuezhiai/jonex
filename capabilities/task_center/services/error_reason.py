#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""失败原因的多语言：**写侧收成一组、读侧按请求语言现场渲染**。

## 为什么要这样

失败原因原来是**写入那一刻**就 `translate()` 成一句话存进 `error_message` 的。写它的
多半是**后台循环**（超时巡检 / 防僵尸 / 派活判死 / 停止任务），那些帧里**没有请求上下文**，
`translate()` 只能拿到 `DEFAULT_LOCALE` —— 于是**语言在写的那一刻就被冻住**，读的人换语言
也变不了（实测库里 34 条英文、12 条硬编码中文）。

现在三件一起存：

| 列 | 是什么 | 谁用 |
| --- | --- | --- |
| `error_code` | 消息键（如 `err.task_center.subtask.timed_out`） | **读侧**按它渲染 |
| `error_params` | 渲染占位符的实参（JSONB） | 同上 |
| `error_message` | 写入时的渲染快照 | 排障直查库；**老数据 / 老运行方 / 引擎原文**的兜底 |

⚠️ **`error_message` 不是"备用"，它是唯一能装"翻不了的东西"的列**：引擎与下下游的自由
文本（`LibreOffice conversion failed for …`、`'list' object has no attribute 'get'`）
**永远没有代号**，只能以原文形式存与显示。所以外层包装走代号、内嵌原文放 `params`。

## 两个入口

- `build_fail_reason()` —— **写侧**（任务中心自己产生的原因）：`(code, params, fallback)`
  一次收成落库那三个键；
- `render_error_reason()` —— **读侧**：按**当前请求的 locale**（`X-Lang` …）渲染。

判据/形状校验不在这里 —— 那是协议层的 `check_error_reason`（SDK 与服务端共用同一份）。
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from jonex_core.common.i18n import translate

#: 任务级收口时那条「**零件拼接**」的外壳代号。任务级的原因天生不是一句原子文案，而是
#: 「哪几个节点挂了、各自为什么」—— 所以外壳是固定代号，零件在 `params["parts"]` 里。
TASK_AGGREGATE_FAILED = "err.task_center.task.aggregate_failed"


def build_fail_reason(
    code: str, params: Optional[Dict[str, Any]], fallback: str
) -> Dict[str, Any]:
    """**写侧**：把一条「任务中心自己产生」的失败原因收成落库那三个键。

    用法就一句 —— `await self._fail_subtask(ctx, subtask, **build_fail_reason(...))`，
    或者直接 `transition_cas(..., **build_fail_reason(...))`（键名就是列名，能直接展开）。

    三件必须**一起、由这一处**算出来：分几处各算一遍必然漂（改了代号忘改文案，或反过来），
    而漂的方向恰恰是「库里存的文案和读侧渲染出来的不是同一件事」。

    快照跟着**写它那一刻**的语言走是**可以接受**的 —— 它只给排障直查库用，不是给人看的
    那份（那份由 `render_error_reason` 按读的人的语言现渲）。
    """
    return {
        "error_message": translate(code, params=params, fallback=fallback),
        "error_code": code,
        "error_params": dict(params or {}),
    }


def render_error_reason(
    error_message: Optional[str],
    error_code: Optional[str] = None,
    error_params: Optional[Mapping[str, Any]] = None,
    *,
    fallback_message: Optional[str] = None,
) -> Optional[str]:
    """**读侧**：按当前请求的语言渲染失败原因（没有原因就回 `None`）。

    三种来路、一条回落链：

    1. **有代号** → `translate(code, params, fallback=存的快照)` —— 翻不出来（运行方自己的
       键、或词条还没加）就回落到快照，**不报错**。这是「未知代号放行」那条协议约定的
       兜底（正常到不了：写入侧已经卡过枚举了 —— 只有「枚举后来删了某个代号」或
       「有人直接改库」才会遇到，那时显示空白比显示一个键名强）；
    2. **代号是 `TASK_AGGREGATE_FAILED`** → 任务级那条**零件拼接**：把
       `params["parts"]` 里每个零件按它自己的代号渲染（翻不出就回落它自己的 `message`），
       再拼成 `<节点名>: <原因>; <节点名>: <原因>` —— 形态与写侧那份快照一致，但**每个
       零件的语言都跟着读的人走**；
    3. **没有代号**（引擎原文 / 老数据 / 老运行方）→ 原样返回快照。

    ⚠️ 第 2 种里零件自己的 `code` 可能就是空的（引擎原文）—— 那时用它的 `message`，
    原样带出。**翻不了的不假装能翻**，这是这套设计的一部分。
    """
    if error_code == TASK_AGGREGATE_FAILED:
        parts = (error_params or {}).get("parts") or []
        rendered = []
        for part in parts:
            if not isinstance(part, Mapping):
                continue
            node_id = str(part.get("node_id") or "")
            message = part.get("message")
            part_code = part.get("code")
            text = (
                translate(part_code, params=part.get("params") or {}, fallback=message)
                if part_code
                else message
            )
            if text is None:
                continue
            rendered.append(f"{node_id}: {text}" if node_id else str(text))
        if rendered:
            return "; ".join(rendered)
        # 零件数组空的（理论上不该出现）：回落到快照，别把原因显示成空白。
        return error_message or fallback_message

    if error_code:
        return translate(error_code, params=dict(error_params or {}), fallback=error_message)
    return error_message or fallback_message


__all__ = ["TASK_AGGREGATE_FAILED", "build_fail_reason", "render_error_reason"]
