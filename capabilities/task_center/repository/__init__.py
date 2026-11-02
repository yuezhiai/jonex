#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""任务中心仓储层导出。

分层对应 §九 的目录结构：

- 内置清单（步骤 / 运行方）：平台预置、只读、不带租户 → `*_shared` 系列；
- 定义 + 版本：按租户隔离，版本表的写操作全部带 `status='draft'` 条件；
- 触发：按租户隔离，删除走状态位；
- 任务 + 任务尝试 + 子任务 + 子任务尝试：状态推进一律走带版本号的 CAS。
"""

from .catalog_repository import TaskCatalogStepRepository
from .definition_repository import (
    TaskDefinitionRepository,
    TaskDefinitionVersionRepository,
)
from .execution_repository import (
    SubtaskAttemptRepository,
    SubtaskRepository,
    TaskAttemptRepository,
    TaskRepository,
)
from .report_repository import SubtaskReportRepository
from .runner_repository import RunnerDefinitionRepository
from .space_repository import SpaceRepository
from .trigger_repository import TaskTriggerRepository

__all__ = [
    "RunnerDefinitionRepository",
    "SpaceRepository",
    "SubtaskAttemptRepository",
    "SubtaskReportRepository",
    "SubtaskRepository",
    "TaskAttemptRepository",
    "TaskCatalogStepRepository",
    "TaskDefinitionRepository",
    "TaskDefinitionVersionRepository",
    "TaskRepository",
    "TaskTriggerRepository",
]
