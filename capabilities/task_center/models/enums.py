#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""任务中心枚举取值。

设计出处：`docs/superpowers/specs/2026-09-09-task-center-design.md` §三、§六。

取值以裸字符串常量形式给出，与数据库 CHECK 约束、编排 JSONB 里的字面量一一对应；
ORM 与 DTO 都直接引用这里，避免同一个取值在多处手写字面量写歪。
"""

from enum import Enum


class DefinitionScope(str, Enum):
    """定义 / 版本的归属（§3.1、§3.2）。

    `system` 系统级：平台自己的定义，无租户、无空间，对所有租户可见可用（只有平台能改）；
    `space`  空间级：租户 + 空间都有，只有那一个空间能改、能引用。

    ⚠️ **2026-11-18 起删除 `tenant`（租户级）**：需求要求 scope 只保留 system / space。
    存量库只有系统级定义，无租户级数据。
    """

    SYSTEM = "system"
    SPACE = "space"


class DefinitionStatus(str, Enum):
    """任务定义状态（2026-11-18 加，需求 1）。

    - `draft` 草稿：新建默认态；还没有可执行的已发布编排；
    - `published` 已发布：可执行（`current_version_no` 指向最新已发布版本）；
    - `disabled` 已停用：不可执行；已有任务继续跑（快照），重试/停止不受影响。

    状态与版本状态是两层：定义状态表达「这条定义整体能不能执行」，
    版本状态表达「这一版编排是否已固化」。保存草稿编排不会把定义状态改回 draft。
    """

    DRAFT = "draft"
    PUBLISHED = "published"
    DISABLED = "disabled"


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

    **2026-11-18 起七个取值**：新增 `skipped`（节点重试耗尽后按
    `on_retry_exhausted="skip"` 跳过并继续）。

    `stopped` 是**管理方置的终态**（用户停整条任务时判给还没开始的那些步骤，或停某一步时
    按 `downstream_on_stop` 连带判给下游），运行方报不了它（见 `RUNTIME_REPORTABLE_STATUSES`）。
    它**不进「失败」那一族**（`SUBTASK_FAILED_STATUSES`）：进去的话任务收尾会把
    「被人停的」算成「跑失败的」，任务状态又回到 `failed`。

    `skipped` 同样是**管理方置的终态**，运行方报不了。它**进「依赖已满足」**那一族
    （`SUBTASK_SATISFIED_STATUSES`）—— 否则下游永远等不到依赖、`skip` 等于没实现；
    它**不进「失败」那一族**，所以任务不会因为跳过一个节点而判 `failed`。
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    STOPPED = "stopped"
    SKIPPED = "skipped"


class SubtaskAttemptStatus(str, Enum):
    """子任务单次尝试的状态（诞生时先插一条 pending）。

    取值与 `SubtaskStatus` **逐字对齐**（含 `stopped` / `skipped`）：子任务那一行被置停
    或跳过的同一时刻，它本次尝试的历史行收成同一个终态（§7.2），两边不对齐就会出现
    「子任务说已跳过、它的第 N 次尝试说已失败」。
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    STOPPED = "stopped"
    SKIPPED = "skipped"


# 「结束态」判定用（§7.2：结束状态一旦写入就改不回去）。
# `stopped` / `skipped` 都在这里（它们确实是终态：不会再被派活、也不许再改），
# 但**都不在下面的失败集合里** —— 三件事别混。
SUBTASK_TERMINAL_STATUSES = frozenset(
    {
        SubtaskStatus.SUCCESS.value,
        SubtaskStatus.FAILED.value,
        SubtaskStatus.TIMEOUT.value,
        SubtaskStatus.STOPPED.value,
        SubtaskStatus.SKIPPED.value,
    }
)
# 「依赖已满足」判定用（§6.3：依赖里每个节点都 success 才算就绪）。
# **`stopped` 不算满足**：被停掉的那一步没有产出，下游拿不到入参，也就跑不起来 ——
# 这正是「停一步、下游跟着停」那条联动的判据基础。
# **`skipped` 算满足**（2026-11-18）：这是「跳过并继续」成立的前提 —— 不算满足的话
# 下游永远等不到依赖，策略等于没实现。代价是被跳过节点的产出为空，下游引用它时
# 会拿到空值（已在发版校验与前端提示）。
SUBTASK_SATISFIED_STATUSES = frozenset(
    {SubtaskStatus.SUCCESS.value, SubtaskStatus.SKIPPED.value}
)
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
    "DefinitionStatus",
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
