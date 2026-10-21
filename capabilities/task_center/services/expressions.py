#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""编排里的「来源引用」解析引擎（§6.2）。

`bind_inputs` 的值只认三种写法：

| 引用对象   | 写法                            | 例子                         |
|-----------|--------------------------------|------------------------------|
| 常量      | 直接写个值                       | `"openkb"` / `300` / `true`  |
| 任务级入参 | `@task.inputs.<key>`            | `@task.inputs.kb_type`       |
| 上游产出   | `@<node_id>.outputs.<字段名>`    | `@parse_1.outputs.doc_ids`   |

引用上游写的是 **`node_id`**（编排里的节点，如 `parse_1`），不是 `step_code`
（内置步骤名，如 `parse`）——同一个 `step_code` 可以出现在多个节点上，拿它做引用
指不到唯一一步（§3.5、§6.2）。

`bind_inputs` **不能引用本步骤入参**（自己填自己，循环），所以 `@inputs.x` 这种写法
在这里不合法，发版校验（§6.5 第 3 条）会直接打回草稿。

**为什么自己手写解析而不用 `eval`**：引用写法由租户写在编排里，属于外部输入。本模块
只认上面这份白名单文法，逐段校验名字，任何不认识的写法在**发版时**就打回草稿；运行期
没有任何可注入的执行面。

> 早先设计里的 `skip_when`（布尔表达式求值器）与 `fail_policy` 都已随设计变更整体移除：
> **一期没有「跳过」这个子任务状态**，上游失败 / 超时后下游一律 **hold**（原地不动、
> 一直 `pending`，不派活），编排里的表达式和那版编排的处置策略都不参与决定（§6.3）。
"""

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from jonex_core.common.exceptions import InvalidParameterError

# 引用没取到值时用它占位，跟「取到的值是 None」区分开：
# 前者是「上游压根没这个产出」，后者是「产出就是 null」。
MISSING = object()


class ExpressionError(InvalidParameterError):
    """引用写法不合法（语法错 / 用了不在白名单里的写法）。"""

    default_message = "编排里的引用写法不合法"


# ============================================================
# 引用
# ============================================================


@dataclass(frozen=True)
class Ref:
    """一个来源引用：任务级入参（`task`）或上游产出（`step`）。"""

    kind: str  # "task" | "step"
    path: Tuple[str, ...]
    step: Optional[str] = None  # kind == "step" 时是上游**节点** id（node_id，如 parse_1）

    def text(self) -> str:
        """还原成编排里的写法，报错信息里用它指出「哪一条不对」。"""
        if self.kind == "task":
            return "@task.inputs." + ".".join(self.path)
        return "@" + (self.step or "") + ".outputs." + ".".join(self.path)


_SEGMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]*$")


def _parse_ref(raw: str) -> Ref:
    """把 `task.inputs.x` / `<node_id>.outputs.x` 解析成一个 `Ref`（不含前导 `@`）。"""
    segments = [seg for seg in raw.split(".") if seg != ""]
    if len(segments) < 2:
        raise ExpressionError(
            message=(
                f"引用写法不完整: @{raw}——"
                "要用 @task.inputs.x / @<node_id>.outputs.x 这两种写法之一"
            )
        )
    for seg in segments:
        if not _SEGMENT_RE.match(seg):
            raise ExpressionError(message=f"引用里的名字不合法: @{raw}")
    if segments[0] == "task":
        if len(segments) < 3 or segments[1] != "inputs":
            raise ExpressionError(
                message=f"任务级入参的写法是 @task.inputs.<字段名>，收到的是 @{raw}"
            )
        return Ref(kind="task", path=tuple(segments[2:]))
    if segments[0] == "inputs":
        raise ExpressionError(
            message=(
                f"bind_inputs 不能引用本步骤入参 @{raw}（自己填自己）——"
                "只能用常量 / @task.inputs.x / @<node_id>.outputs.x"
            )
        )
    if len(segments) >= 3 and segments[1] == "outputs":
        return Ref(kind="step", step=segments[0], path=tuple(segments[2:]))
    raise ExpressionError(
        message=(
            f"引用写法不认识: @{raw}——"
            "上游产出要写成 @<node_id>.outputs.<字段名>，任务级入参写成 @task.inputs.<字段名>"
        )
    )


# ============================================================
# bind_inputs 的来源
# ============================================================


@dataclass(frozen=True)
class Binding:
    """一个 `bind_inputs` 项的来源：常量、单个引用、或多个引用（多来源）。

    `bind_inputs` 值的三种形态：常量（`"openkb"` / `300` / `true` / 对象 / 数组）、
    单个引用字符串（`"@task.inputs.file_ref"`）、引用字符串数组（多个来源）。
    """

    field_name: str
    constant: Any = None
    sources: Tuple[Ref, ...] = field(default=())
    is_constant: bool = True
    is_merge: bool = False

    def resolve(
        self,
        resolver: Callable[[Ref], Any],
        *,
        sort_key: Callable[[Ref], int],
        merge_as_list: bool = False,
    ) -> Any:
        """把来源解析成实值。

        - 常量：原样返回（已经去过转义）；
        - 单个引用：直接取那个值（取不到就是 `MISSING`）；
        - 多个引用（多来源）：先按上游 `sort` 稳定排序，再**按 `sort` 顺序拼成一个
          数组**（`["d1"]` + `["d2"]` → `["d1", "d2"]`，**不是嵌套**）。

        多来源只有一个合法去处 —— **目标字段声明成 `array`**；配给单值字段**发版
        校验 N12 就把它打回了**（合并语义跟单值字段的 `type` 对不上，§6.2）。
        这里的 `merge_as_list=False` 分支只是**兜底**（万一漏网），按 `sort` 顺序
        取第一个有值的整体填。
        """
        if self.is_constant:
            return self.constant
        if not self.is_merge:
            return resolver(self.sources[0])

        ordered = sorted(self.sources, key=sort_key)
        values = [resolver(ref) for ref in ordered]

        if not merge_as_list:
            for value in values:
                if value is not MISSING and value is not None:
                    return value
            return MISSING

        merged: list = []
        for value in values:
            if value is MISSING or value is None:
                continue
            if isinstance(value, (list, tuple)):
                merged.extend(value)
            else:
                merged.append(value)
        return merged


_ESCAPE_PREFIX = "\\@"


def _unescape_constant(value: Any) -> Any:
    """把常量开头的 `\\@` 还原成 `@`（§6.2「常量本身要以 `@` 开头怎么办」）。

    **作用层级写清**：`bind_inputs` 的载体是 JSON，而 JSON 里反斜杠自己也要转义 ——
    原文写的是 `"\\\\@all"`；**先做 JSON 解码**得到字符串 `\\@all`，解析器再在这个
    「解码后的字符串」上扫一层，见到开头的 `\\@` 就当常量 `@`、不再当引用。
    **解析顺序固定：先看转义、再看是不是引用。**
    """
    if isinstance(value, str) and value.startswith(_ESCAPE_PREFIX):
        return value[1:]
    return value


def parse_binding(field_name: str, raw: Any) -> Binding:
    """解析一个 `bind_inputs` 项（写法不对抛 `ExpressionError`）。

    三种形态的分流判据（§6.2）：

    - 标量字符串以 `@` 开头 → 单个引用；
    - **数组里每个元素都以 `@` 开头** → 多来源合并；
    - **其余一律按常量** —— 包括「有一个元素不以 `@` 开头」的数组（**整体降级**：
      里面那个看着像引用的元素**也不再解析**，当字面字符串填下去），以及被转义过的
      `"\\\\@all"`（JSON 解码后是 `\\@all`，首字符是 `\\` 不是 `@`）。
      常量数组的每个元素**仍然要去转义** —— 「原样填」≠「什么都不做」。
    """
    if isinstance(raw, str) and raw.startswith("@"):
        ref = _parse_ref(raw[1:])
        return Binding(field_name=field_name, sources=(ref,), is_constant=False)
    if isinstance(raw, list) and raw and all(
        isinstance(item, str) and item.startswith("@") for item in raw
    ):
        refs = tuple(_parse_ref(item[1:]) for item in raw)
        return Binding(
            field_name=field_name,
            sources=refs,
            is_constant=False,
            is_merge=True,
        )
    if isinstance(raw, list):
        return Binding(
            field_name=field_name,
            constant=[_unescape_constant(item) for item in raw],
            is_constant=True,
        )
    return Binding(field_name=field_name, constant=_unescape_constant(raw), is_constant=True)


def resolver_from_sources(
    task_inputs: Dict[str, Any],
    upstream_outputs: Dict[str, Dict[str, Any]],
) -> Callable[[Ref], Any]:
    """给引用求值用的取值回调。

    `upstream_outputs` 是「**节点 id（node_id）** → 那个节点的产出」；取不到就返回 `MISSING`。
    """

    def resolver(ref: Ref) -> Any:
        if ref.kind == "task":
            return _dig(task_inputs, ref.path)
        if ref.kind == "step":
            outputs = upstream_outputs.get(ref.step or "", {})
            return _dig(outputs, ref.path)
        return MISSING

    return resolver


def _dig(container: Any, path: Sequence[str]) -> Any:
    """按路径逐段取值；中间任一段取不到就返回 `MISSING`。"""
    current = container
    for segment in path:
        if isinstance(current, dict):
            if segment not in current:
                return MISSING
            current = current[segment]
        elif isinstance(current, (list, tuple)) and segment.isdigit():
            # ⚠️ **这一段当前不可达**（2026-10-13 标注）：`path` 只来自 `_parse_ref`，
            # 而它对每段强制 `_SEGMENT_RE = ^[A-Za-z_][A-Za-z0-9_\-]*$`（**段首必须字母 /
            # 下划线**）—— 纯数字段（按下标取项，如 `@node.outputs.items.0`）**过不了解析、
            # 在发版校验就被打回**。留着它是为「将来放开数字段、支持多来源合并出数组后按
            # 位置取项」预留；真要放开，得**同时**改 `_SEGMENT_RE` / `_parse_ref` 的语义，
            # 别只删这半段。
            index = int(segment)
            if index >= len(current):
                return MISSING
            current = current[index]
        else:
            return MISSING
    return current


__all__ = [
    "Binding",
    "ExpressionError",
    "MISSING",
    "Ref",
    "parse_binding",
    "resolver_from_sources",
]
