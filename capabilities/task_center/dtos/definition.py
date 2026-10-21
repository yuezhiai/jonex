#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""定义与版本 DTO，含编排内容（§八、§3.2.1、§6.1）。

`FlowConfig` 是 `task_definition_versions.config` 的类型化外壳；`FieldSpec` 是
「字段定义」的统一形状（任务级 `input_schema`、内置步骤的输入输出 schema 都用它）。
"""

from typing import Any, Dict, List, Optional

try:  # pydantic>=1.10 的 v1 兼容命名空间优先
    from pydantic.v1 import (
        BaseModel,
        Field,
        StrictBool,
        StrictInt,
        StrictStr,
        root_validator,
        validator,
    )
except ImportError:  # pragma: no cover
    from pydantic import (
        BaseModel,
        Field,
        StrictBool,
        StrictInt,
        StrictStr,
        root_validator,
        validator,
    )


class FieldSpec(BaseModel):
    """字段定义：`input_schema` / `output_schema` 里每个字段都长这样（§3.2.1）。

    `object` 靠 `properties` 往下套子字段、`array` 靠 `items` 定义元素类型，
    子字段和元素又都是 FieldSpec，递归到标量为止；`file`（值=文件引用对象，固定
    `storage_key` + `file_name` 两个键）不写 `properties`、跟标量一样递归到底。
    """

    # ⚠️ `StrictStr` / `StrictBool`，**不是**裸 `str` / `bool`（2026-09-30）：
    # 裸类型会把 `type=123` 悄悄转成 `"123"`、`required="yes"` 转成 `True` ——
    # 而发版校验（`flow_validator` 的 N13 / N14）**今天拒的就是它们**。
    # 用裸类型等于把那条判据做掉（详见 `FlowStep` 上的长注释）。
    type: StrictStr = Field(description="string / number / boolean / file / object / array")
    required: StrictBool = Field(default=False, description="是否必填；不写=可填可不填")
    default: Optional[Any] = Field(default=None, description="默认值，类型须跟 type 对得上")
    enum: Optional[List[Any]] = Field(default=None, description="可选值；不配=不受限制")
    description: Optional[str] = Field(default=None, description="给人看的描述")
    properties: Optional[Dict[str, "FieldSpec"]] = Field(
        default=None, description="仅 type=object：子字段定义（递归）"
    )
    items: Optional["FieldSpec"] = Field(
        default=None, description="仅 type=array：数组元素定义（递归）"
    )

    @root_validator(pre=True)
    def _ignore_nested_keys_that_do_not_apply(cls, values):
        """`type` 对不上时，**把 `properties` / `items` 当没有** —— 与 N17 的判据对齐。

        ⚠️ 今天 `flow_validator` 的 N13~N17 **只在 `type` 对得上时才往 `properties` /
        `items` 里递归**（`type == "object"` / `type == "array"`）；也就是说
        「标量字段上挂了个 `properties`、或挂了个半填的 `items: {}`」是**被忽略的、能
        发版的**（表单式编辑器很容易无条件带上这些键）。而这里的类型声明是**无条件递归**
        的，不拦就成 400 —— **收紧也是一种行为改变**，而且只有「编辑后再发版」才撞得上。

        ⚠️ **必须是 `root_validator(pre=True)`、不能是字段级校验器**：字段级跑在**该字段
        被解析之后**，而 `items: {}` 这种半填值在解析那一步就已经炸了、轮不到它。**pre 的
        根校验器拿到的才是原始 dict** —— 这时把不适用的键**摘掉**，后面的解析自然放行。

        ⚠️ 摘键前**先浅拷贝**：`values` 就是调用方那份 config 里的子 dict（`FlowConfig(**
        config)` 只解了顶层），原地 `pop` 会把调用方的数据也改掉。
        """
        if not isinstance(values, dict):
            return values
        values = dict(values)
        if values.get("type") != "object":
            values.pop("properties", None)
        if values.get("type") != "array":
            values.pop("items", None)
        return values

    class Config:
        extra = "allow"


FieldSpec.update_forward_refs()


class FlowStep(BaseModel):
    """编排里的一个**节点**：选了哪个内置步骤 + 它跟别的节点的关系 + 怎么填值（§6.1）。

    步骤的「固有形状」（输入输出 schema、跑哪个运行方）在内置步骤清单里，这里只写
    「选了哪个 + 跟谁连 + 超时多久 + 值从哪来」。

    **`node_id` 与 `step_code` 的分工**（这是这一版最容易混的一处）：

    - `node_id` 是**编排里的节点**，同一次运行里唯一 —— `depends_on` / `next_steps`
      连线、`@<node_id>.outputs.x` 引用、上报定位、重试删除全用它；**它也进
      `execute` 报文**（`ExecutionDispatch.node_id`，随 `ExecutionEnvelope` 一起下去）：
      运行方收到后**原样存着**，`retry_subtask` 指认「哪一步」时原样带回 ——
      同一个 `step_code` 出现在多个节点上（`parse_1` / `parse_2`）时，
      能唯一指认一步的只有它；
    - `step_code` 是「这个节点用的是哪个内置步骤」，**同一个 `step_code` 可以出现在
      多个节点上**（同一个内置步骤 `parse` 被引用两次就是 `parse_1` / `parse_2`，
      见 §3.2.1）。管理方拿它判并发上限、查内置步骤的 schema 与 `runner_code`、查历史；
      **它也跟着进 `execute` 报文**（`ExecutionDispatch.step_code`）—— 运行方得知道
      这一步是什么类型才知道该干什么，而入参形状分不出这个（`ingest` 与 `compile`
      的入参形状是一样的，见 §3.5 / §4.1 ①）。

    一期不做自动重试，所以没有 `on_timeout` / `on_failure`（§十二「待规划」）。
    """

    # ⚠️ **这些字段全用 `Strict*` 类型，不是裸类型**（2026-09-30）——
    # 裸类型会**悄悄洗掉非法值**，而发版校验（N0 / N2 / N3 / N19）今天拒的就是它们：
    # `node_id=123` → `"123"`、`timeout_seconds="300"` → `300`、`300.5` → `300`（**静默
    # 改短**）、`True` → `1`、`sort="3"` → `3`、`depends_on=[1,2]` → `["1","2"]`。
    # 换成裸类型，那些判据就全被架空了 —— 等于**把发版校验放松**。
    node_id: StrictStr = Field(description="编排里的节点唯一 id（同一次运行里不重复），如 parse_1")
    step_code: StrictStr = Field(description="用的是内置步骤清单里的哪个步骤，如 parse（可重复）")
    depends_on: List[StrictStr] = Field(
        default_factory=list, description="依赖的**节点 id**（node_id），空=无依赖"
    )
    next_steps: List[StrictStr] = Field(
        default_factory=list,
        description="我成功后激活的下一批**节点 id**（node_id）；空=叶子。不参与调度判定，只供对偶校验与排查",
    )
    sort: Optional[StrictInt] = Field(
        default=None, description="稳定排序；同层唯一（合并到同一个下游的几步之间不重复）"
    )
    timeout_seconds: StrictInt = Field(description="这步的超时时间（秒），必填、正整数")
    bind_inputs: Dict[StrictStr, Any] = Field(
        default_factory=dict, description="键=该步骤 input_schema 的字段名，值=来源表达式"
    )
    # ── 下游联动的两个开关（2026-10-15 加，§6.1）──
    #
    # 语义是**出边**：字段挂在上游节点上，管的是「从我这个节点出发的那几条边」。
    # `None`（不填）= `true` = 从前的口径（重跑某步带上下游 / 停某步带上下游），
    # 所以库里那几条预置编排一个字都不用改。详细取舍见
    # `TaskService._downstream_chain` 与 `_stop_cascade_targets`。
    downstream_on_retry: Optional[StrictBool] = Field(
        default=None,
        description="重跑这个节点时，它的下游跟不跟着重跑；不填 = true（跟）",
    )
    downstream_on_stop: Optional[StrictBool] = Field(
        default=None,
        description="停止这个节点时，它的下游跟不跟着停；不填 = true（跟）",
    )

    @validator("depends_on", "next_steps", "bind_inputs", pre=True)
    def _null_counts_as_empty(cls, value, field):
        """`null` 当空处理 —— **与今天的 N 判据逐字对齐**（2026-09-30）。

        今天 `flow_validator` 的 `_string_list(None) → []`、`bind_inputs` 那边 `or {}`，
        所以 `depends_on: null` / `next_steps: null` / `bind_inputs: null` 是**放过的**。
        而 v1 的 `List[...]` / `Dict[...]` 对 `None` 是**硬拒** —— 不加这个 pre 校验器，
        就会把一个今天合法的编排变成 400（**收紧**也是一种行为改变，同样不许）。

        ⚠️ 反过来那半边由 `Strict*` 负责（把今天拒的仍然拒掉）。**两条合起来才是
        「接进来之后判定一个字不变」** —— 对照测试见
        `tests/unit/task_center/test_task_center_flow_config_dto_parity.py`。
        """
        if value is not None:
            return value
        return {} if field.name == "bind_inputs" else []

    class Config:
        extra = "allow"


class FlowConfig(BaseModel):
    """`task_definition_versions.config` 的类型化外壳（§6.1）。

    **空草稿骨架**就是全默认值：`{"input_schema": {}, "entry": None, "steps": []}`。

    **没有 `fail_policy`** —— 失败 / 超时一律 **hold**（下游原地不动、不派活、
    一直 `pending`），任务的唯一出路是 `retry_subtask`（§6.3）。
    """

    input_schema: Dict[StrictStr, FieldSpec] = Field(
        default_factory=dict, description="任务级入参声明，触发时校验 task_triggers.inputs"
    )
    entry: Optional[StrictStr] = Field(
        default=None,
        description="**唯一入口节点**的 node_id（单个字符串，不是列表）；空=还没定入口",
    )
    steps: List[FlowStep] = Field(default_factory=list, description="这版要干哪些节点、怎么排")

    @validator("input_schema", pre=True)
    def _null_counts_as_empty(cls, value):
        """`input_schema: null` 当 `{}` —— 与 `flow_validator` 那句 `or {}` 对齐（同上）。"""
        return value if value is not None else {}

    class Config:
        extra = "allow"


class VersionSummary(BaseModel):
    """版本概览（定义详情里带的那份）。"""

    version_no: int = Field(description="版本号")
    scope: Optional[str] = Field(default=None, description="system / tenant / space")
    space_id: Optional[str] = Field(default=None, description="空间 ID")
    status: str = Field(description="draft / published")
    published_at: Optional[str] = Field(default=None, description="发布时间")
    created_at: Optional[str] = Field(default=None, description="创建时间")

    class Config:
        extra = "ignore"


class VersionDetail(BaseModel):
    """某版详情：概览 + 整套编排。"""

    id: str = Field(description="主键")
    definition_id: str = Field(description="属于哪个定义")
    scope: Optional[str] = Field(default=None, description="system / tenant / space")
    space_id: Optional[str] = Field(default=None, description="空间 ID")
    version_no: int = Field(description="版本号")
    status: str = Field(description="draft / published")
    config: Dict[str, Any] = Field(default_factory=dict, description="编排内容（§6.1）")
    published_at: Optional[str] = Field(default=None, description="发布时间")
    created_at: Optional[str] = Field(default=None, description="创建时间")

    class Config:
        extra = "ignore"


class TaskDefinitionItem(BaseModel):
    """任务定义列表项。"""

    id: str = Field(description="主键")
    scope: str = Field(description="system(系统级) / tenant(租户级) / space(空间级)")
    tenant_id: Optional[str] = Field(default=None, description="租户；scope=system 时为空")
    task_catalog_code: str = Field(description="基于哪个内置任务建的")
    space_id: Optional[str] = Field(default=None, description="空间 ID；scope=space 时才有")
    name: str = Field(description="给人看的名字")
    description: Optional[str] = Field(default=None, description="描述")
    current_version_no: int = Field(default=0, description="最新已发布版本号；0=还没发过版")
    created_at: Optional[str] = Field(default=None, description="创建时间")
    updated_at: Optional[str] = Field(default=None, description="更新时间")

    class Config:
        extra = "ignore"


class TaskDefinitionDetail(BaseModel):
    """定义详情：定义本身 + 版本概览。"""

    definition: TaskDefinitionItem = Field(description="定义本身")
    space_id: Optional[str] = Field(default=None, description="空间 ID（同 definition.space_id）")
    versions: List[VersionSummary] = Field(default_factory=list, description="版本概览")
    draft_version_no: Optional[int] = Field(default=None, description="当前草稿版本号；没有就为 None")

    class Config:
        extra = "ignore"


class TaskDefinitionList(BaseModel):
    """定义分页列表（§4.0：`{items, page, page_size, total}`）。"""

    items: List[TaskDefinitionItem] = Field(default_factory=list)
    page: int = Field(default=1, description="第几页（从 1 起）")
    page_size: int = Field(default=20, description="每页几条")
    total: int = Field(default=0, description="这次的匹配总数（判断还有没有下一页）")

    class Config:
        extra = "ignore"


__all__ = [
    "FieldSpec",
    "FlowConfig",
    "FlowStep",
    "TaskDefinitionDetail",
    "TaskDefinitionItem",
    "TaskDefinitionList",
    "VersionDetail",
    "VersionSummary",
]
