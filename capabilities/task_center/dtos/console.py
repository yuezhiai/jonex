#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""管理端只读接口 DTO（§4.4）：任务分页、子任务列表。

跟 `execution.py` 那套的分工：那些是**内部契约**（`task.list` / `task.subtask_list`
的返回，字段给全、接入方按它写代码）；这几个是**给前端页面看的**，字段按页面要的
挑，另外带上「耗时」这种算出来的展示字段。

新增展示字段往这里加、**别往 `execution.py` 加**：内部契约一动，照文档写的接入方
跟着动。
"""

from typing import Any, Dict, List, Optional

try:  # pydantic>=1.10 的 v1 兼容命名空间优先
    from pydantic.v1 import BaseModel, Field
except ImportError:  # pragma: no cover
    from pydantic import BaseModel, Field


class TaskConsoleItem(BaseModel):
    """任务列表里的一行。"""

    id: str = Field(description="任务 ID")
    task_name: Optional[str] = Field(default=None, description="任务名；发起时没给就为空")
    space_id: str = Field(
        description=(
            "这条任务落在哪个空间（任务行的 `space_id`，**NOT NULL**）。"
            "与列表的 `space_id` 筛选参数同名同值 —— 前端按空间筛完可以拿它回显 / 分组。"
            "⚠️ 它是**逐个列出来的**：本 DTO 的 `extra=\"ignore\"` 会把 `tasks.to_dict()` "
            "里没列在这里的键静默丢掉（`node_id` 当初就这么丢过一次）"
        )
    )
    task_source: Optional[str] = Field(
        default=None,
        description=(
            "任务来源（∈ 协议层 `TASK_SOURCES`，如 `knowledge_base.file.manual_upload`）。"
            "**空 = 不是运行方提交的**（平台侧自己建的任务：定时触发 / 人工立即触发），"
            "或这列诞生之前的存量行。⚠️ **存的是码、不是文案** —— 中文名由前端按码映射。"
            "与列表的 `task_source` 筛选参数同名同值。"
            "⚠️ 它是**逐个列出来的**：本 DTO 的 `extra=\"ignore\"` 会把 `tasks.to_dict()` "
            "里没列在这里的键静默丢掉"
        ),
    )
    status: str = Field(
        default="pending", description="pending / running / completed / failed"
    )
    error_message: Optional[str] = Field(
        default=None,
        description=(
            "**失败原因**：任务收口成 `failed` 时写在这里，形如 `<节点名>: <原因>`"
            "（多个失败节点按 `; ` 连起来）；成功完成、或还在跑就是空。"
            "⚠️ 取自 `tasks.error_message` —— 这张表里它本来就是给人看的原因，"
            "不是内部枚举"
        ),
    )
    started_at: Optional[str] = Field(default=None, description="首次开始时间（ISO 8601）")
    finished_at: Optional[str] = Field(default=None, description="最新一次运行结束时间（ISO 8601）")
    duration_seconds: Optional[int] = Field(
        default=None,
        description="耗时整秒数（finished_at - started_at）；**还没跑完就是空**",
    )

    class Config:
        extra = "ignore"


class TaskConsolePage(BaseModel):
    """任务分页（§4.0 的 `{items, page, page_size, total}`，外加各状态数量）。"""

    items: List[TaskConsoleItem] = Field(default_factory=list)
    page: int = 1
    page_size: int = 20
    total: int = 0
    status_counts: Dict[str, int] = Field(
        default_factory=dict,
        description=(
            "四个任务状态各多少条，键恒在（没有的为 0）。"
            "**只受「除状态外」的筛选条件影响** —— 前端切状态页签时这个数字不变（§4.4）"
        ),
    )

    class Config:
        extra = "ignore"


class SubtaskConsoleItem(BaseModel):
    """子任务列表里的一行。"""

    id: str = Field(description="子任务 ID")
    node_id: str = Field(
        description=(
            "**节点名**（编排里 `config.steps[].node_id`，如 `parse_1` / `compile_1`）。"
            "⚠️ 前端调「停止子任务 / 重试子任务」那两条写接口时**寻址用的就是它**"
            "（`.../subtasks/{node_id}/stop` / `.../retry`），不是上面那个 `id` —— "
            "`id` 是子任务行自己的主键，写接口不认它。**必填**：它是非空列，永远在"
        ),
    )
    subtask_name: Optional[str] = Field(default=None, description="子任务名")
    output_refs: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description=(
            "产出「**去哪儿看**」的引用（**展示用**，由运行方在上报里带上来）：一个数组，"
            "元素**只有两个字段** —— `kind`（写死枚举 `knowledge_base.chunks / knowledge_base.ontology / "
            "knowledge_base.wiki` —— **值必须带「运行方前缀」** `{运行方 code}.{产物类型}`，"
            "任务中心是多运行方的，裸 `chunks` 迟早和别人撞；前端**按它知道"
            "自己该调哪个接口**、并据此挑渲染方式与文案）与 `params`（那个接口要的字段；"
            "每个 kind 要哪些写死在协议层 `OUTPUT_REF_PARAMS`，判据也共用 `check_output_refs`）。"
            "**引用里不带地址** —— 运行方碰不到 url，也就填不出外站地址 / `javascript:` / "
            "目录穿越那一类东西；**未知键原样透传**。"
            "⚠️ **只给入口、不给内容** —— 正文要走运行方那侧（**权限也在那边判**）："
            "这是**设计选择**（内容与它的权限都留在运行方那侧）。\n"
            "⚠️ 这条早先的理由写的是「这个读接口**不判权**」—— **那已经过时**：同一批改动"
            "（2026-10-13）给两个控制台读口子补上了 `require_space_visible`（租户 + 空间可见性）。"
            "「不放内容」仍然成立，但**别再拿「接口不判权」当依据**。"
            "⚠️ **空（null）或空数组 = 没东西可看**：老运行方不上报、或这一步还没终态"
            "（列可空，所以这里原样是 `None` —— 前端按 `?? []` 处理即可）。"
        ),
    )
    input_refs: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description=(
            "输入「**从哪儿来**」的引用（**展示用**，不参与下游取参，2026-10-16 加）："
            "由运行方在上报里带上来，与 `output_refs` 同款 —— 元素只有 `kind` + `params`，"
            "`kind` 写死枚举（协议层 `INPUT_REF_PARAMS`：`knowledge_base.document` / "
            "`knowledge_base.chunks`，**值必须带运行方前缀**），判据共用 `check_input_refs`。"
            "只给入口、不给内容：正文走运行方那侧的接口。"
            "⚠️ **空（null）= 没东西可看**：老运行方不上报、或这一步还没终态 —— 前端按 "
            "`?? []` 处理即可。\n"
            "⚠️ 它**只有终态上报那一刻才有值**（跟 `output_refs` 一样）：`running` 期间为空 "
            "—— 「输入是什么」只有运行方知道该指向哪个页面，所以只能等它上报。"
        ),
    )
    status: str = Field(
        default="pending",
        description="pending / running / success / failed / timeout",
    )
    error_message: Optional[str] = Field(
        default=None,
        description=(
            "**这一步自己的失败原因**（判死 / 运行方回失败时写）；成功、或还在跑就是空。"
            "任务列表那一行的 `error_message` 就是拿失败节点的这条拼出来的"
            "（`<节点名>: <原因>`）"
        ),
    )
    started_at: Optional[str] = Field(default=None, description="开始时间（ISO 8601）")
    finished_at: Optional[str] = Field(default=None, description="结束时间（ISO 8601）")
    duration_seconds: Optional[int] = Field(
        default=None, description="耗时整秒数；**还没结束就是空**"
    )

    class Config:
        extra = "ignore"


__all__ = ["SubtaskConsoleItem", "TaskConsoleItem", "TaskConsolePage"]
