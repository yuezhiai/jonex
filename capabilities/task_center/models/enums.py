#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""任务中心枚举取值。

设计出处：`docs/superpowers/specs/2026-09-09-task-center-design.md` §三、§六。

取值以裸字符串常量形式给出，与数据库 CHECK 约束、编排 JSONB 里的字面量一一对应；
ORM 与 DTO 都直接引用这里，避免同一个取值在多处手写字面量写歪。
"""

from enum import Enum


class DefinitionScope(str, Enum):
    """定义 / 版本的三级归属（§3.1、§3.2）。

    `system` 系统级：平台自己的定义，无租户、无空间，对所有租户可见可用（只有平台能改）；
    `tenant` 租户级：有租户、无空间，只有该租户能改、能引用；
    `space`  空间级：租户 + 空间都有，只有那一个空间能改、能引用。
    """

    SYSTEM = "system"
    TENANT = "tenant"
    SPACE = "space"


class VersionStatus(str, Enum):
    """任务定义版本状态机（§6.6：draft → published 单向，published 是终点）。"""

    DRAFT = "draft"
    PUBLISHED = "published"


class TriggerStatus(str, Enum):
    """触发状态（§3.3）。删除走状态位，不真删（§3.1）。

    `paused` 的语义是「**不被调度循环扫到**」（扫描条件带 `status='active'`）——
    暂停的是「自动到点」，**不是「禁止手动」**：`paused` 的触发器照样能 `trig.fire`（§4.3）。
    触发器**只等于定时**（`manual` 类型已砍，`trigger_type` 列已删），所以这一列的语义
    现在很纯粹：`pause` 就是「定时那一路先别自动跑」，而临时补跑一次照旧不受影响。
    """

    ACTIVE = "active"
    PAUSED = "paused"
    DELETED = "deleted"


class MisfirePolicy(str, Enum):
    """错过了触发点怎么办（§3.3.2 ④、§7.5 ⓪）。

    「错过」的判据是 **`现在 - next_fire_at > 这条触发器自己的一个周期`**（也就是
    「下一次该跑的时刻 − 这一次该跑的时刻」，**不是调度循环的心跳**；正常抖动不算错过）：

    - `skip`：**不补跑**，只把 `next_fire_at` 推到下一个点（常规场景：昨天没跑成就算了）；
    - `catch_up`：**补跑一次**，`next_fire_at` 同样推到下一个点（**只补一次，不按错过次数补**）。
    """

    SKIP = "skip"
    CATCH_UP = "catch_up"


class TaskStatus(str, Enum):
    """任务实例「最新一次运行」的状态快照（§7.2）。

    `stopped` 与 `failed` **都是终态，但含义不许合并**：`failed` 是「跑挂了」，
    `stopped` 是「被人停的」。分开是为了让控制台一眼看出「这条不是我这边出错、
    是用户自己停的」，也是为了让收尾表（§7.2）有话可说 —— 塞进 `failed` 的话，
    用户点完「停止」看到的仍是「已失败」，那正是这一档要治的病。
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


class AttemptStatus(str, Enum):
    """任务尝试（一次运行）的状态（§3.5）。

    `stopped` 与 `TaskStatus.STOPPED` 同值同义：任务被用户停掉时，**运行行与任务快照
    一起收口**（`_settle_stopped`），两边保持同一个字面量，排障时不会撞见
    「任务说已停止、运行记录说已失败」。
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


class SubtaskStatus(str, Enum):
    """子任务状态（§6.4 状态流转）。

    **六个取值**——没有 `skipped`：失败 / 超时一律 hold 下游（不改下游状态、
    也不派活），不会被「按策略跳过」（§6.3、§4.3 附录 A.7）。

    `stopped` 是**管理方置的终态**（用户停整条任务时判给还没开始的那些步骤，或停某一步时
    按 `downstream_on_stop` 连带判给下游），运行方报不了它（见 `RUNTIME_REPORTABLE_STATUSES`）。
    它**不进「失败」那一族**（`SUBTASK_FAILED_STATUSES`）：进去的话任务收尾会把
    「被人停的」算成「跑失败的」，任务状态又回到 `failed`。
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    STOPPED = "stopped"


class SubtaskAttemptStatus(str, Enum):
    """子任务单次尝试的状态（诞生时先插一条 pending）。

    取值与 `SubtaskStatus` **逐字对齐**（含 `stopped`）：子任务那一行被置停时，
    它本次尝试的历史行同一时刻收成同一个终态（§7.2），两边不对齐就会出现
    「子任务说已停止、它的第 N 次尝试说已失败」。
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    STOPPED = "stopped"


# 「结束态」判定用（§7.2：结束状态一旦写入就改不回去）。
# `stopped` 在这里（它确实是终态：不会再被派活、也不许再改），
# 但它**不在下面的失败集合里** —— 两件事别混。
SUBTASK_TERMINAL_STATUSES = frozenset(
    {
        SubtaskStatus.SUCCESS.value,
        SubtaskStatus.FAILED.value,
        SubtaskStatus.TIMEOUT.value,
        SubtaskStatus.STOPPED.value,
    }
)
# 「依赖已满足」判定用（§6.3：依赖里每个节点都 success 才算就绪；一期没有 skipped）。
# **`stopped` 不算满足**：被停掉的那一步没有产出，下游拿不到入参，也就跑不起来 ——
# 这正是「停一步、下游跟着停」那条联动的判据基础。
SUBTASK_SATISFIED_STATUSES = frozenset({SubtaskStatus.SUCCESS.value})
# 任务终态推导里算「失败」的集合（§7.2 收尾表：timeout 算失败、一样是「没跑成」）。
# ⚠️ **`stopped` 刻意不进这里**：这是本轮最容易写反的一处 —— 加进去之后，收尾表会把
# 「被人停的」判成「跑失败的」，任务状态又变回 `failed`，整个「已停止」需求当场白做。
SUBTASK_FAILED_STATUSES = frozenset(
    {SubtaskStatus.FAILED.value, SubtaskStatus.TIMEOUT.value}
)
# 运行方能上报的状态（§4.2 ①：只有成功 / 失败两种）。
#
# **这里没有 `running`**：`execution_id` 是派活抢占时跟「`pending → running`」在同一条
# UPDATE 里写下的（`claim_pending`），所以运行方拿到凭证时子任务**必然已经是 `running`** ——
# 它再报一句 `running` 改变不了任何东西（既不推进状态、也没有任何判据在读那一下时间戳）。
# 所以干脆把这条路关掉：报 `running` 跟报别的非法值一样，当场拒。
#
# `pending` / `timeout` 由管理方自己置（诞生或重试、超时巡检、防僵尸），运行方也报不了。
RUNTIME_REPORTABLE_STATUSES = frozenset(
    {
        SubtaskStatus.SUCCESS.value,
        SubtaskStatus.FAILED.value,
    }
)

__all__ = [
    "AttemptStatus",
    "DefinitionScope",
    "MisfirePolicy",
    "RUNTIME_REPORTABLE_STATUSES",
    "SUBTASK_FAILED_STATUSES",
    "SUBTASK_SATISFIED_STATUSES",
    "SUBTASK_TERMINAL_STATUSES",
    "SubtaskAttemptStatus",
    "SubtaskStatus",
    "TaskStatus",
    "TriggerStatus",
    "VersionStatus",
]
