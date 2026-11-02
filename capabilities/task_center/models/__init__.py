#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""任务中心 ORM 导出（十张表，§二）。

加载该包时，下列模型会在各自模块的类定义处注册到 SQLAlchemy 元数据。
任一模型导入或注册失败时，模块级 import 抛出的异常会直接向上传播，
终止整个 models 包的加载并指示失败的模型，从而不会注册部分模型。

建表 DDL 见 `deploy/postgres/migrations/008_task_center.sql`。

**ORM 与 DDL 的一处已知差异**（有意保留的，**别当 bug 去「修」**）：

1. **默认值**：**只对 DDL 里确实有 `DEFAULT` 的那 4 列**写 `server_default`
   （`task_catalog_steps` / `runner_definitions` 各自的 `created_at` / `updated_at`，
   DDL 给的是 `DEFAULT CURRENT_TIMESTAMP`），**其余列一律不写**
   —— 业务代码一律显式写值、从不依赖数据库默认值（`create` 都是把字段拼全了才 insert），
   而生产建表只走 DDL。

   ⚠️ **2026-09-30 改准**：这一段从前写的是「这里的列**一律**不写 `server_default`……
   **别为了让本地 `create_all`「跟 DDL 一样」去补**」—— **那句是错的，而且方向正好反了**：
   那 4 列本来就写着 `server_default`，而 `test_task_center_schema_parity.py` 有一条判据
   （「DDL 有 `DEFAULT` 而 ORM 没有」就红）**正是要求它们留着**。照旧话去删，测试当场红。
   反过来，给**别的**列补 `server_default` 才是该拦的（DDL 里它们没有 `DEFAULT`）。
   准确的判据是「**与 DDL 逐列对齐**」，不是「一律不写」。

> ⚠️ **曾经的「第 2 处差异」是索引，已经收掉了**：`TenantMixin` / `SoftDeleteMixin`
> （公共内核 `jonex_core/common/entity.py`）给 `tenant_id` / `is_deleted` 各带一条
> `index=True`，于是 `create_all` 会比 DDL 多建 6 条单列索引（`tasks` / `subtasks` 各
> 两条、`task_triggers` / `task_definitions` 各一条）。那几条在 DDL 里**是有意不建的**
> （`tenant_id` 的查询已被组合索引的前缀覆盖、`is_deleted` 单独一列几乎用不上），
> 所以收法是**以 DDL 为准**：这四个类不再走那两个 mixin，`tenant_id` / `is_deleted`
> 各自直接声明（定义照抄 mixin）。原因、判据与「为什么不动公共内核」见 `execution.py`
> 的 `Task.__table_args__`。**新加的表/列照这个规矩来**：先看 DDL 有没有索引，再决定
> 用不用 mixin。对账测试（`tests/unit/task_center/test_task_center_schema_parity.py`）
> 现在把索引也纳入比对，走错就会红。
"""

from .catalog import TaskCatalogStep
from .definition import TaskDefinition, TaskDefinitionVersion
from .enums import (
    AttemptStatus,
    DefinitionScope,
    MisfirePolicy,
    RUNTIME_REPORTABLE_STATUSES,
    SUBTASK_FAILED_STATUSES,
    SUBTASK_SATISFIED_STATUSES,
    SUBTASK_TERMINAL_STATUSES,
    SubtaskAttemptStatus,
    SubtaskStatus,
    TaskStatus,
    TriggerStatus,
    VersionStatus,
)
from .execution import Subtask, SubtaskAttempt, Task, TaskAttempt
from .report import SubtaskReport
from .runner import RunnerDefinition
from .trigger import TaskTrigger

__all__ = [
    "AttemptStatus",
    "DefinitionScope",
    "MisfirePolicy",
    "RUNTIME_REPORTABLE_STATUSES",
    "RunnerDefinition",
    "SUBTASK_FAILED_STATUSES",
    "SUBTASK_SATISFIED_STATUSES",
    "SUBTASK_TERMINAL_STATUSES",
    "Subtask",
    "SubtaskAttempt",
    "SubtaskAttemptStatus",
    "SubtaskReport",
    "SubtaskStatus",
    "Task",
    "TaskAttempt",
    "TaskCatalogStep",
    "TaskDefinition",
    "TaskDefinitionVersion",
    "TaskStatus",
    "TaskTrigger",
    "TriggerStatus",
    "VersionStatus",
]
