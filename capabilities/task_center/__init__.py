#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""任务中心能力模块（`business.task_center.v1`）。

设计出处：`docs/superpowers/specs/2026-09-09-task-center-design.md`。

任务中心是**管理方**：它拿编排把任务拆成一张子任务图，按图派活给运行方（§4.1 ①），
再收运行方的上报推进状态（§4.2 ①）。它自己**不执行任何一步业务动作**，也**不自己开
REST 路由**（`register_routes` 用基类默认的空实现）；调用面只有三个方向 —— 对内的
函数集（§4.3）、后台循环的出站派活，以及 §4.4 开给前端的**六个**接口
（两个只读 `task.console_page` / `task.console_subtask_list`，加**四个写**
`task.stop` / `task.console_subtask_stop` / `task.console_task_retry` /
`task.console_subtask_retry` —— 后三个 2026-09-29 补齐），经 Gateway →
Sidecar `/invoke` 进来。⚠️ **别照旧口径（「只开了三个」）去判断边界**：
再加接口之前先回设计文档 §4.4 对一遍。

能力服务启动脚本按 `capabilities.{name}` + `{Name}Capability` 反射加载本包，
所以这里必须导出 `TaskCenterCapability`。
"""

from capabilities.task_center.capability import TaskCenterCapability
from capabilities.task_center.models import (
    RunnerDefinition,
    Subtask,
    SubtaskAttempt,
    SubtaskReport,
    Task,
    TaskAttempt,
    TaskCatalogStep,
    TaskDefinition,
    TaskDefinitionVersion,
    TaskTrigger,
)

__all__ = [
    "TaskCenterCapability",
    # Models
    "RunnerDefinition",
    "Subtask",
    "SubtaskAttempt",
    "Task",
    "TaskAttempt",
    "TaskCatalogStep",
    "TaskDefinition",
    "TaskDefinitionVersion",
    "TaskTrigger",
]
