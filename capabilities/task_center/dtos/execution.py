#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""执行层 DTO（§八）：任务 / 任务运行历史 / 子任务 / 子任务尝试历史 / 上报事件。

`TaskItemResponse` 的定义已经搬到 `jonex_core/task_protocol/models.py`（它跨进程：
运行方 `TaskRuntime.submit` 拿到的就是这个），这里改成 re-export，**不是第二份定义**。
"""

from typing import Any, Dict, List, Optional

from jonex_core.task_protocol.models import TaskItemResponse

try:  # pydantic>=1.10 的 v1 兼容命名空间优先
    from pydantic.v1 import BaseModel, Field
except ImportError:  # pragma: no cover
    from pydantic import BaseModel, Field


class ReportStatusRequest(BaseModel):
    """运行方上报执行进展 / 结果（§4.2 ① `report_status` 的入参，**全部平铺、没有嵌套**）。

    **运行方改子任务状态的唯一入口**。`execution_id` 是定位符 —— 管理方按
    `tenant_id` + `space_id` + `execution_id`（三者缺一不可）定位到唯一那一行
    `subtasks`，再往下核「是不是当前运行」「是不是已经终态」；任何一道不过就
    **丢弃这条上报、仍回 `success=true`**（幂等吸收，让运行方别重试）。

    字段顺序与
    `jonex_core/capability/business/task_center/client.py::report_status` 的签名逐位对齐
    （`task_id` 在 `status` 之前）—— 两边是同一份契约的两处表述，改一处必须改另一处。
    """

    tenant_id: str = Field(description="租户 ID（按租户隔离时用）")
    space_id: str = Field(description="空间 ID（按空间落数据时用）")
    execution_id: str = Field(description="管理方生成的受理凭证，原样带回（定位符）")
    task_id: str = Field(
        description=(
            "**不参与定位**（定位只用得上上面三个），是**同步阶段的交叉校验**："
            "管理方受理之前拿它核对「这张凭证是不是真属于这条任务」—— 按 "
            "`(tenant_id, space_id, execution_id)` 定位 `subtasks`，命中行的 `task_id` "
            "与它不符就当场拒（1002，这条上报连收件箱都不进）。"
            "另一道「任务 ↔ 空间」的核对则用 `space_id` 去比 `tasks.space_id`"
        )
    )
    status: str = Field(
        description=(
            "success(成功) / failed(失败) —— **运行方只报这 2 种**，判据是 "
            "`RUNTIME_REPORTABLE_STATUSES`（`models/enums.py`）。"
            "**没有 `running`**：`execution_id` 是派活抢占时跟「`pending → running`」"
            "同一条 UPDATE 写下的，运行方拿到凭证时子任务必然已经是 `running` —— "
            "再报一句改变不了任何东西，所以这条路直接关掉了（报了当场拒）。"
            "`pending` / `timeout` 同样由管理方自己置（诞生或重试、超时巡检、防僵尸）"
        )
    )
    outputs: Optional[Dict[str, Any]] = Field(
        default=None, description="status=success 时必带"
    )
    error_message: Optional[str] = Field(
        default=None, description="status=failed 时必带"
    )
    error_code: Optional[str] = Field(
        default=None,
        description=(
            "失败原因的**消息键**（可选，2026-10-10 加）：管理方存下来，**读的时候**按请求的 "
            "`X-Lang` 现场渲染 —— 否则「哪国语言」由运行方上报的那一刻决定，而写它的多半是后台"
            "循环，那会儿只有默认语言。**代号必须 ∈ 协议层的 `ERROR_REASON_CODES`**"
            "（写死枚举，如 `err.kb.task_center.runner_abandoned`）—— 不在枚举里**当场拒**"
            "（1001），好让运行方只能填登记过的那些。"
            "⚠️ 与 `error_message` 的分工：**可翻译的外层包装走代号，内嵌的原文（引擎报错那种）"
            "放 `error_params.detail`、并原样写进 `error_message`** —— 翻不了的不假装能翻。"
        ),
    )
    error_params: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "`error_code` 的渲染实参（可选）：`{占位符名: 值}`。上限与形状判据在协议层 "
            "`check_error_reason`（SDK 与服务端共用同一份），超了当场拒（1001）。"
        ),
    )
    ts: Optional[int] = Field(
        default=None,
        description="运行方侧的事件时间（13 位毫秒时间戳）—— 管理方**只落库留痕**（`subtask_reports.ts` 一列）、**不参与任何裁决**（运行方的钟不可信）",
    )
    output_refs: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description=(
            "产出「**去哪儿看**」的引用（可选，**展示用**）：一个数组，元素**只有两个字段**，例如 "
            "`[{\"kind\":\"knowledge_base.chunks\",\"params\":{\"document_id\":\"…\",\"knowledge_base_id\":\"…\"}}]`"
            "（⚠️ `kind` **必须带运行方前缀**，写法是 `{运行方 code}.{产物类型}` —— 裸 `chunks` "
            "不是合法取值，会被 `check_output_refs` 判 `kind_invalid` 整条拒掉）。"
            "（判据在 `jonex_core/task_protocol/models.py` 的 `check_output_refs`，SDK 与服务端共用"
            "同一份；**每个 kind 要哪些 params 也写死在那张表里** `OUTPUT_REF_PARAMS`。）"
            "⚠️ **不带地址**（`url` / `method` 都没有）：「这个 kind 该调哪个接口」是**前端**那张映射的"
            "事 —— 运行方碰不到 url，**注入面归零**（填不出外站地址 / `javascript:` / `..` / `DELETE`）。"
            "管理方**只搬运**（落到 `subtasks.output_refs`、控制台读接口原样回给前端）、"
            "未知键原样透传；前端据 `kind` 查自己的映射、带上 `params` 去看详情，"
            "**权限归运行方那侧判**。⚠️ 与 `outputs` 分工不同：`outputs` 是下游取入参的"
            "**契约字段**，这里只给前端做入口，**绝不参与 `@节点.outputs.x` 取值**。"
        ),
    )

    input_refs: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description=(
            "输入「**从哪儿来**」的引用（可选，2026-10-16 加）：与 `output_refs` 对称的那一份 "
            "—— 那边说「这一步的成果去哪儿看」，这边说「这一步吃的是什么」。同款数组，元素"
            "只有 `kind` + `params`，`kind` 必须 ∈ 协议层 `INPUT_REF_PARAMS`（写死枚举，"
            "**值要带运行方前缀**，如 `knowledge_base.document` / `knowledge_base.chunks`），"
            "判据共用 `check_input_refs`。\n"
            "**协议上可选**（老运行方不带照常受理），知识库那条运行方当前**总是带**（成功、"
            "失败都带）—— 但将来会出现真没有输入引用的任务。"
        ),
    )

    class Config:
        extra = "ignore"


class TaskList(BaseModel):
    """任务分页列表（§4.0：`{items, page, page_size, total}`）。"""

    items: List[TaskItemResponse] = Field(default_factory=list)
    page: int = Field(default=1, description="第几页（从 1 起）")
    page_size: int = Field(default=20, description="每页几条")
    total: int = Field(default=0, description="这次的匹配总数（判断还有没有下一页）")

    class Config:
        extra = "ignore"


class TaskAttemptItem(BaseModel):
    """任务尝试：某任务每一次运行的一条记录（只追加不覆盖）。"""

    id: str = Field(description="主键：一次运行的 ID")
    tenant_id: Optional[str] = Field(default=None, description="租户")
    space_id: Optional[str] = Field(default=None, description="空间 ID")
    task_id: str = Field(description="属于哪个任务")
    attempt: int = Field(description="第几次运行（首次=1，重跑 +1）")
    status: str = Field(
        default="pending", description="pending / running / completed / failed"
    )
    error_message: Optional[str] = Field(default=None, description="这次运行的失败原因")
    started_at: Optional[str] = Field(default=None, description="这次运行开始")
    finished_at: Optional[str] = Field(default=None, description="这次运行结束")
    created_at: Optional[str] = Field(default=None, description="创建时间")

    class Config:
        extra = "ignore"


class SubtaskItemResponse(BaseModel):
    """子任务（一行存「该子任务最新一次尝试」）。"""

    id: str = Field(description="主键")
    tenant_id: Optional[str] = Field(default=None, description="租户")
    space_id: Optional[str] = Field(default=None, description="空间 ID")
    task_id: str = Field(description="属于哪个任务")
    task_attempt_id: str = Field(description="属于哪一次运行")
    node_id: str = Field(description="是编排里的哪个节点（node_id，如 parse_1）")
    step_code: str = Field(
        description="这个节点用的是哪个内置步骤（如 parse / ingest / compile）；管理方用它判并发上限、查 schema 与 runner，也填进 execute 报文供运行方分流"
    )
    subtask_name: Optional[str] = Field(default=None, description="给人看的名字")
    status: str = Field(
        default="pending",
        description="pending / running / success / failed / timeout（一期没有 skipped：失败一律 hold）",
    )
    depends_on: List[str] = Field(default_factory=list, description="它依赖哪些节点（node_id）")
    next_steps: List[str] = Field(default_factory=list, description="成功后激活哪些节点（node_id）")
    sort: Optional[int] = Field(default=None, description="稳定排序")
    timeout_seconds: int = Field(default=300, description="这步的超时时间（秒）")
    inputs: Dict[str, Any] = Field(default_factory=dict, description="填好后的真实入参")
    outputs: Optional[Dict[str, Any]] = Field(default=None, description="产出（成功后回填）")
    output_refs: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description=(
            "产出「去哪儿看」的引用（**展示用**，不参与下游取参）：运行方上报时带的数组，"
            "元素只有 `kind` + `params`（判据见协议层 `check_output_refs` / `OUTPUT_REF_PARAMS`），"
            "控制台读接口据此给前端渲染入口。**不给内容** —— 正文走运行方那侧的接口。"
        ),
    )
    input_refs: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description=(
            "输入「**从哪儿来**」的引用（**展示用**，不参与下游取参，2026-10-16 加）：与 "
            "`output_refs` 对称 —— 运行方上报时带的数组，元素只有 `kind` + `params`"
            "（判据见协议层 `check_input_refs` / `INPUT_REF_PARAMS`）。"
            "**空 = 没东西可看**（老运行方不上报、或这一步还没终态）。"
        ),
    )
    current_attempt: int = Field(default=1, description="当前第几次尝试（指针，首次=1）")
    execution_id: Optional[str] = Field(default=None, description="管理方生成的受理凭证")
    state_version: int = Field(default=0, description="每行版本号（防重复改）")
    error_message: Optional[str] = Field(default=None, description="失败原因")
    is_deleted: int = Field(default=0, description="假删除标记：0=正常 1=已删（删任务时一并置 1）")
    created_at: Optional[str] = Field(default=None, description="创建时间")
    started_at: Optional[str] = Field(default=None, description="开始时间")
    finished_at: Optional[str] = Field(default=None, description="结束时间")
    updated_at: Optional[str] = Field(default=None, description="更新时间")

    class Config:
        extra = "ignore"


class SubtaskAttemptItem(BaseModel):
    """子任务尝试历史的一条（只追加不覆盖）。"""

    id: str = Field(description="主键")
    tenant_id: Optional[str] = Field(default=None, description="租户")
    space_id: Optional[str] = Field(default=None, description="空间 ID")
    task_id: str = Field(description="属于哪个任务")
    subtask_id: str = Field(description="属于哪个子任务")
    attempt: int = Field(description="第几次尝试（从 1 起）")
    execution_id: Optional[str] = Field(default=None, description="这次尝试的受理凭证")
    status: str = Field(
        description="pending / running / success / failed / timeout（一期没有 skipped）"
    )
    error_message: Optional[str] = Field(default=None, description="这次尝试的失败原因")
    inputs: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "这次尝试的**入参快照**（2026-10-16 加）：与 `outputs` 对称的那一份。\n"
            "⚠️ 与子任务行上的 `inputs` **不是同一份**：那边是「最新一次尝试」的现状、"
            "重试时会被重算；这份是**每次尝试各存一份** —— 「第 N 次尝试吃的是什么」"
            "只有这里查得到。"
        ),
    )
    outputs: Optional[Dict[str, Any]] = Field(default=None, description="这次尝试的产出快照")
    output_refs: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description=(
            "这次尝试的**产出引用**（展示用，2026-10-16 加）：与 `outputs` 配套的那一份。\n"
            "⚠️ 与子任务行上那份（`SubtaskItemResponse.output_refs`）**不是同一份**："
            "那边是「最新一次尝试」的现状，这份是**每次尝试各存一份**。"
        ),
    )
    input_refs: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description=(
            "这次尝试的**输入引用**（展示用，2026-10-16 加）：与 `output_refs` 对称的那一份，"
            "只是挂在**每一次尝试**上。\n"
            "⚠️ 与子任务行上那份（`SubtaskItemResponse.input_refs`）**不是同一份**：那边是"
            "「最新一次尝试」的现状、重试后会按上游现在的产出重算；这份是**每次尝试各存一份**，"
            "所以「第 2 次尝试当时吃的是什么」只有这里查得到。"
        ),
    )
    started_at: Optional[str] = Field(default=None, description="这次尝试开始")
    finished_at: Optional[str] = Field(default=None, description="这次尝试结束")
    created_at: Optional[str] = Field(default=None, description="创建时间")

    class Config:
        extra = "ignore"


class TaskAttemptList(BaseModel):
    """任务运行历史的分页列表（§4.0：`{items, page, page_size, total}`）。"""

    items: List[TaskAttemptItem] = Field(default_factory=list)
    page: int = Field(default=1, description="第几页（从 1 起）")
    page_size: int = Field(default=20, description="每页几条")
    total: int = Field(default=0, description="这个任务一共有几次运行")

    class Config:
        extra = "ignore"


class SubtaskAttemptList(BaseModel):
    """子任务尝试历史的分页列表（§4.0：`{items, page, page_size, total}`）。"""

    items: List[SubtaskAttemptItem] = Field(default_factory=list)
    page: int = Field(default=1, description="第几页（从 1 起）")
    page_size: int = Field(default=20, description="每页几条")
    total: int = Field(default=0, description="这个子任务一共尝试过几次")

    class Config:
        extra = "ignore"


__all__ = [
    "ReportStatusRequest",
    "SubtaskAttemptItem",
    "SubtaskAttemptList",
    "SubtaskItemResponse",
    "TaskAttemptItem",
    "TaskAttemptList",
    "TaskItemResponse",
    "TaskList",
]
