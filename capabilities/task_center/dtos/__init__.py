#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""任务中心 DTO 导出（§八）。

DTO 是接口参数、以及响应里 `data` 字段的类型外壳，也是「运行方约定」的报文结构
（§4.1 派活 `ExecutionDispatch`、§4.2 上报 `ReportStatusRequest`）。**不是网络接口的请求体**。
配套的统一响应壳是 `jonex_core.capability.response.ApiResponse`。
"""

from .catalog import TaskCatalogStepItem
from .console import SubtaskConsoleItem, TaskConsoleItem, TaskConsolePage
from .definition import (
    FieldSpec,
    FlowConfig,
    FlowStep,
    TaskDefinitionDetail,
    TaskDefinitionItem,
    TaskDefinitionList,
    VersionDetail,
    VersionSummary,
)
from .execution import (
    ReportStatusRequest,
    SubtaskAttemptItem,
    SubtaskAttemptList,
    SubtaskItemResponse,
    TaskAttemptItem,
    TaskAttemptList,
    TaskItemResponse,
    TaskList,
)
from .runner import (
    ExecutionDispatch,
    ExecutionEnvelope,
    RemoveTaskDispatch,
    TaskStoppedDispatch,
    RunnerItem,
)
from .trigger import TaskTriggerCreate, TaskTriggerItem, TaskTriggerList, TaskTriggerUpdate

__all__ = [
    "ExecutionDispatch",
    "ExecutionEnvelope",
    "FieldSpec",
    "FlowConfig",
    "FlowStep",
    "RemoveTaskDispatch",
    "TaskStoppedDispatch",
    "ReportStatusRequest",
    "RunnerItem",
    "SubtaskAttemptItem",
    "SubtaskAttemptList",
    "SubtaskConsoleItem",
    "SubtaskItemResponse",
    "TaskAttemptItem",
    "TaskAttemptList",
    "TaskCatalogStepItem",
    "TaskConsoleItem",
    "TaskConsolePage",
    "TaskDefinitionDetail",
    "TaskDefinitionItem",
    "TaskDefinitionList",
    "TaskItemResponse",
    "TaskList",
    "TaskTriggerCreate",
    "TaskTriggerItem",
    "TaskTriggerList",
    "TaskTriggerUpdate",
    "VersionDetail",
    "VersionSummary",
]
