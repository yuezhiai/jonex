# -*- coding: utf-8 -*-
"""[jonex] 本体编译（ontology-compile）对宿主的依赖契约。

P1（纯搬迁）由 raganything 的 TaskManager._compile_deps() 提供全部实现；
P2（独立容器 + 灰度）把宿主任务层类型换成编译服务自带模型，并把表网格化
四件套从 raganything.utils 正式迁入（或另择归属）。

设计依据：本体编译拆分设计 §4.2/§4.4。

## 宿主任务对象契约（task）

被搬迁代码消费的 `task` 是 **duck-typed** 的（不 import 宿主 TaskInfo），共 15 个字段，
逐字列如下——P2 必须逐字段复刻，否则失败模式是「容器跑起来之后才 AttributeError」：

```text
config_snapshot / current_step / document_id / file_path / kb_id / name /
ontology_data / ontology_error / ontology_schema / ontology_status /
progress / status / task_id / tenant_id / timeline
```

此外 `task.timeline` 的**元素形状**也是契约的一部分：由 `deps.stage_timing`
（即宿主的 `StageTiming`）构造后 append。P1 由 raganything `TaskInfo` 提供；
P2 需自带等价模型（含 timeline 元素模型）。

## 装配路径（P2 起两条并存，至 P4 收敛）

P2 后本模块的字段改由 `build_deps()` 提供**包内实现**（`HttpLightRagClient` /
`CompileTaskStore` / 本包的 `table_grid` / 本包的 `extract` + `ingest` hook），
不 import raganything、不 import lightrag。与此同时 raganything 的
`TaskManager._compile_deps()` 仍以注入方式装配（字段与顺序与本模块一致），
rag 链行为不变。两条路径并存到 P4 raganything 退役。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TableGrid:
    """表网格化四件套。

    P1 由宿主注入 `raganything.utils` 的同名函数（实现逐字未改，约 830 行不搬）；
    P2 定归属后替换为平台实现。
    """

    get_table_body: Callable[[dict], Any]
    normalize_table_rows: Callable[[Any], Any]
    normalize_table_grid: Callable[..., Any]
    enabled: Callable[[], bool]


@dataclass(frozen=True)
class FlowHooks:
    """流程协作点：编译流程在 4 处把控制权交回宿主。

    P1 由 TaskManager 传入自身同名方法（既有 122 个测试通过 patch 这些方法
    观测调用顺序与入参，故保留为显式注入点而非内部直调）；P2 由编译服务
    自身的 worker/任务层提供。
    """

    ingest_engine_chunks: Callable[..., Any]
    run_ontology_extraction: Callable[..., Any]
    merge_table_objects: Callable[..., Any]
    writeback_custom_kg: Callable[..., Any]


@dataclass
class CompileDeps:
    """一次编译任务的全部外部依赖。"""

    # 宿主 HTTP 客户端（upload_text / batch_track_status / get_document_chunks / insert_custom_kg）
    http_client: Any

    # 宿主任务层回调：原 self._repo.save / self._fail_task / 模块级 _log_ingest_timing_v2
    save_task: Callable[..., Awaitable[Any]]
    fail_task: Callable[..., Awaitable[Any]]
    log_ingest_timing: Callable[..., Any]

    # 宿主任务层类型：原 TaskCancelledError / TaskStatus.COMPLETED /
    # ErrorCode.CONFIG_INVALID / StageTiming
    cancelled_error: type
    status_completed: Any
    error_config_invalid: Any
    stage_timing: Any

    # 表网格化注入（见 TableGrid）
    table_grid: TableGrid

    # 流程协作点（见 FlowHooks）
    hooks: FlowHooks

    async def aclose(self) -> None:
        """释放本实例持有的外部资源（当前只有 http_client 的连接池）。

        ⚠️ 所有权契约：`build_deps()` 每次调用**新建**一个 `HttpLightRagClient`，
        因此**调用方**负责在 finally 里 `await deps.aclose()`；否则每个任务漏一个连接池。
        （宿主路径无需关心 —— 它的 http_client 是长生命周期对象，由宿主自己 close。）
        """
        close = getattr(self.http_client, "close", None)
        if close is not None:
            await close()


def build_deps(*, task_store=None, ingest_timing_sink=None):
    """构造 `CompileDeps` 的**包内默认实现**（编译服务容器入口）。

    与 raganything `TaskManager._compile_deps()` 的装配语义逐项等价，但全部指向本包实现
    —— 不 import raganything、不 import lightrag。

    ⚠️ **没有 tenant_id / kb_id 参数**（曾有两个死参数，函数体从不引用，且会误导调用方以为
    「返回的 deps 已按租户绑定」）。真正的租户作用域是：`HttpLightRagClient` **逐调用**传参、
    `get_task_store()` 是**进程级单例**、`CompileTaskStore` 不做租户过滤。

    调用方负责生命周期：用完 `await deps.aclose()`。
    """
    from functools import partial

    from .deps import CompileDeps, FlowHooks, TableGrid
    from . import extract as _extract
    from . import ingest as _ingest
    from .lightrag_http import HttpLightRagClient
    from .table_grid import (get_table_body, normalize_table_grid,
                             normalize_table_rows, table_grid_v2_enabled)
    from .task_model import (ERROR_CONFIG_INVALID, STATUS_COMPLETED,
                             TaskCancelledError, StageTiming)
    from .task_store import get_task_store
    from .timing import log_ingest_timing

    store = task_store or get_task_store()

    deps = CompileDeps(
        http_client=HttpLightRagClient(),
        save_task=store.save,
        fail_task=_fail_task_default(store),
        log_ingest_timing=ingest_timing_sink or log_ingest_timing,
        cancelled_error=TaskCancelledError,
        status_completed=STATUS_COMPLETED,
        error_config_invalid=ERROR_CONFIG_INVALID,
        stage_timing=StageTiming,
        table_grid=TableGrid(
            get_table_body=get_table_body,
            normalize_table_rows=normalize_table_rows,
            normalize_table_grid=normalize_table_grid,
            enabled=table_grid_v2_enabled,
        ),
        hooks=None,  # type: ignore[arg-type]  # 紧随其后**就地**填入（见下）
    )
    # ⚠️ 两条硬约束，缺一条容器路径就断：
    #
    # ① **必须 partial(fn, deps) 绑定 deps**：FlowHooks 的契约是「调用点元数、不含 deps」
    #    （宿主 _compile_deps() 注入的是 self._ingest_engine_chunks(task, chunks) 这类已绑定的包装方法），
    #    而本包这 4 个实现函数都以 deps 作第一形参。
    #    不绑 ⇒ 首次调用 TypeError: missing 1 required positional argument。
    #
    # ② **必须就地赋值，不要用 dataclasses.replace**：hook 实现体内会解引用 deps.hooks.*
    #    （extract.py:214 的 merge_table_objects、extract.py:315 的 writeback_custom_kg）。
    #    replace() 会新建对象，若 partial 绑的是替换**之前**那个 hooks=None 的旧对象，
    #    运行期就是 AttributeError: 'NoneType' object has no attribute 'merge_table_objects'
    #    —— 且它在 extract.py:221-225 被 except Exception 静默吞掉（表格抽取无声丢失），
    #    回写那条则直接判任务 failed。宿主 _compile_deps() 没这个问题，因为它的 hook 包装每次调用都重建 deps。
    deps.hooks = FlowHooks(
        ingest_engine_chunks=partial(_ingest.ingest_engine_chunks, deps),
        run_ontology_extraction=partial(_extract.run_ontology_extraction, deps),
        merge_table_objects=partial(_extract.merge_table_objects, deps),
        writeback_custom_kg=partial(_extract.writeback_custom_kg, deps),
    )
    return deps


def _fail_task_default(store):
    """包内 fail_task：复刻 raganything `_fail_task` 的状态字段与日志。

    **不带**宿主的 R5-c cleanup 复位（`delete_pending_ids` / `compensate_pending_ids`）
    —— 那是 raganything 任务层特有的"清理阶段"概念，编译服务没有该阶段。
    两条路径各自自洽：rag 链仍走宿主注入的 `self._fail_task`，行为不变。
    """
    async def _fail(task, error_code, message: str) -> None:
        task.status = "failed"
        task.error_code = error_code
        task.error_message = message
        now = datetime.now(timezone.utc)
        task.completed_at = now
        task.updated_at = now
        logger.error("Compile task failed: task_id=%s error_code=%s message=%s",
                     getattr(task, "task_id", ""), error_code, message)
        await store.save(task)
    return _fail
