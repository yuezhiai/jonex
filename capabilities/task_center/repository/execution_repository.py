#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""执行侧仓储：任务实例 / 任务尝试 / 子任务 / 子任务尝试历史。

设计出处：`docs/superpowers/specs/2026-09-09-task-center-design.md` §3.5、§七。

这里有两类查询，写法完全不一样，别混：

- **租户内读写**（业务侧经 service 走）：一律带 `tenant_id` 条件。
- **后台循环扫描**（scheduler 走）：后台循环没有租户上下文，且要一次扫全租户，
  所以这类方法**故意不带租户条件**——调用方拿到的每行自带 `tenant_id`，
  后续动作按行内的租户显式走。方法名以 `_for_dispatch` / `_for_patrol` 结尾作区分。

**"当前运行"是执行侧的核心口径**（§7.2）：`tasks` 是稳定身份 + 最新一次运行的快照，
最新一次运行由 `tasks.current_attempt` 指向 `task_attempts.attempt`；那一次运行挂的
子任务由 `subtasks.task_attempt_id` 认领。旧运行留下的行是**冻结的历史**，一切查询与
状态推进都只认当前运行 —— **只有两个显式例外**：`count_unfinished_by_task`（数并发
名额，故意不分运行）与 `list_timed_out_for_patrol`（超时巡检，故意不按运行筛，否则
旧运行留下的那条 `running` 永远收不掉）。两处都在各自的方法里写明了理由。

所有状态推进都走带版本号的 CAS（`§7.2`）：`state_version` 没对上、或者当前状态
不在合法前驱集合里，就命中 0 行 = 这次推进被拒。结束态一旦写入，任何反向/迟到的
推进都命中 0 行，**永远改不回去**。

`subtasks` 与 `subtask_attempts` 的时间字段同步（§7.2）：派活置 `running` 时两表的
`started_at`、结束置终态时两表的 `finished_at` 必须是**同一个值**——所以这里凡是
写时间的方法都接受调用方显式传入的 `started_at` / `finished_at`，调用方一次算好、
两边传同一个值，别让两个仓库各取一次 `datetime.now()`。
"""

from datetime import datetime, timedelta
from typing import Tuple

from sqlalchemy import exists, func, literal, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from jonex_core.common.repository import BaseRepository

from ..models import (
    AttemptStatus,
    SUBTASK_SATISFIED_STATUSES,
    Subtask,
    SubtaskAttempt,
    SubtaskReport,
    SubtaskStatus,
    Task,
    TaskAttempt,
    TaskStatus,
)

# 「这次运行还在跑」的任务状态：只有这两种才值得扫子任务派活（§7.5）
TASK_ACTIVE_STATUSES = (TaskStatus.PENDING.value, TaskStatus.RUNNING.value)

#: **运行行「已经收口」的几个状态**（`task_attempts.status`）。
#: 给「补收口」当候选判据用（`list_tasks_needing_settle`）：运行行收了口、任务快照还停在
#: 活跃态，就是那一趟专治的格子。健康在跑的任务，运行行还是 `pending` / `running`。
#: `stopped` 在册：被用户停掉的运行**同样已经收口**（`_settle_stopped` 写的就是它），
#: 漏掉它会让「运行行已停、任务快照却没跟上」那一格**永远进不了补收口的候选** ——
#: 而它的成因（收口那一刻任务快照的 CAS 落空）与 `failed` 那一档一模一样。
ATTEMPT_TERMINAL_STATUSES = (
    AttemptStatus.COMPLETED.value,
    AttemptStatus.FAILED.value,
    AttemptStatus.STOPPED.value,
)


def _current_task_attempt_id_subquery():
    """「该任务当前那次运行」的子查询：`tasks.current_attempt` 指向的 `task_attempts.id`。

    调度、查询、状态推进一律认它（§7.2）——任务重跑过之后，旧运行那套 `subtasks` 就是
    冻结的历史，不该再被捞起来。
    """
    return (
        select(TaskAttempt.id)
        .where(
            TaskAttempt.task_id == Task.id,
            TaskAttempt.attempt == Task.current_attempt,
        )
        .correlate(Task)
        .scalar_subquery()
    )


class TaskRepository(BaseRepository[Task]):
    """任务实例仓储：`tasks` 一行 = 稳定身份 + 最新一次运行的状态快照。"""

    model = Task

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    # task.list / task.get 支持的筛选项（可组合，§4.3）
    _FILTER_FIELDS = ("definition_id", "status", "space_id", "trigger_id", "task_source")

    def _filter_conditions(self, filters: dict | None) -> list:
        conditions = []
        for field in self._FILTER_FIELDS:
            value = (filters or {}).get(field)
            if value:
                conditions.append(getattr(Task, field) == value)
        return conditions

    async def exists_by_id(self, task_id: str) -> bool:
        """主键上有没有这一行 —— **不带租户、不带软删**。

        回答的是「调用方自带的这个 `task_id` 还能不能用」。`tasks.id` 是**全局主键**：
        别的租户的任务、已经被假删的任务，一样占着这个 id，插进去照样撞主键。所以这里
        故意绕开 `BaseRepository` 自带的那两套条件（`get_by_id` / `list_all` 都带租户 +
        `is_deleted=0`）—— 用它们判会漏掉「软删过的」这一种占用。

        ⚠️ **「先查再插」不是原子的**：两个人同时拿同一个 id 进来，两边都可能查到「没有」，
        然后第二个人插的时候撞主键 —— 那会是一个未分类的数据库错误（`DatabaseError`
        4001），不是「id 已被占用」。这个窗口接受现状：id 由调用方用 uuid 生成，撞车概率
        极低；真实场景是「同一次上传重试用了同一个 id」，那是串行的，这里一定查得到。
        """
        result = await self.session.execute(
            select(literal(1)).select_from(Task).where(Task.id == task_id).limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def list_tasks(
        self,
        tenant_id: str,
        filters: dict | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> list[Task]:
        """列任务实例。`BaseRepository.list_all` 已滤掉假删（`is_deleted=1` 不出列表）。"""
        return await self.list_all(
            tenant_id=tenant_id,
            offset=offset,
            limit=limit,
            extra_conditions=self._filter_conditions(filters),
        )

    async def count_tasks(self, tenant_id: str, filters: dict | None = None) -> int:
        return await self.count(
            tenant_id=tenant_id,
            extra_conditions=self._filter_conditions(filters),
        )

    # ── 管理端只读列表（§4.4）──────────────────────────────
    #
    # 跟上面 `list_tasks` 那套（等值筛白名单）是**两条路，别合并**：`task.list` 是
    # 内部契约，筛选口径定稿在 §4.3；这里是给前端页面的，要关键字模糊、时间范围、
    # 状态多值，塞不进那个白名单，硬改又会牵动接入方。

    def _console_conditions(
        self,
        *,
        keyword: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        statuses: list[str] | None = None,
        space_id: str | None = None,
        allowed_space_ids: list[str] | None = None,
        task_source: str | None = None,
    ) -> list:
        """管理端列表的筛选条件（**不含租户与软删**，那两个由调用方补）。

        `statuses` 只在分页与总数上用：各状态数量的统计（`count_console_by_status`）
        按 §4.4 的口径**不带 status 条件** —— 那是给页签角标用的，切页签时得稳住。

        **`space_id` 与 `allowed_space_ids` 是两件事，别合并**：

          · `space_id` —— **用户显式选的**那个空间，精确匹配（网关收的对外参数就叫这个名）；
          · `allowed_space_ids` —— **权限收敛后的可见范围**（授权，不是筛选），落成
            `IN (...)`。服务层算好传下来，`None` = 不过滤（租户管理员 / 无用户身份的内部
            链路），`[]` = 一个都不给看（翻成恒假；「他没有可见空间」必须是 0 行，不是全部）。

        两者都跟着用户的筛选走，所以**三个查询都要带**（角标也得跟着变）。
        """
        conditions = []
        if keyword:
            like = f"%{keyword}%"
            # 任务名或任务 id，命中一个就算。`task_name` 允许为空（发起时没给），
            # NULL 走 ILIKE 结果是 NULL，自然落到「不命中」，不用额外判空。
            conditions.append(or_(Task.task_name.ilike(like), Task.id.ilike(like)))
        if start_time is not None:
            conditions.append(Task.created_at >= start_time)
        if end_time is not None:
            conditions.append(Task.created_at <= end_time)
        if statuses:
            conditions.append(Task.status.in_(statuses))
        if space_id:
            # 任务表的 `space_id` 是 **NOT NULL**（§3.5 执行侧一律空间级），所以不需要像
            # 定义表那样考虑 `scope=system/tenant` 留下的 NULL 行。
            conditions.append(Task.space_id == space_id)
        if allowed_space_ids is not None:
            conditions.append(Task.space_id.in_(allowed_space_ids))
        if task_source:
            # 任务来源（2026-10-13 加）。**可空列**，所以「筛某个来源」永远筛不到
            # 平台侧自己发起的任务（它们的值为 NULL）—— 那是刻意的：来源是「运行方
            # 从哪个入口提交的」，NULL 表示「不是运行方提交的」，不是一个来源。
            # 在不在册由服务层判（非法值 1001），这里只拼条件。
            conditions.append(Task.task_source == task_source)
        return conditions

    def _console_base_conditions(self, tenant_id: str, filters: dict) -> list:
        """租户 + 软删 + 筛选条件。三个管理端查询共用，口径才不会各写各的。"""
        return [
            *self._tenant_conditions(tenant_id),
            *self._soft_delete_conditions(),
            *self._console_conditions(**filters),
        ]

    async def page_console_tasks(
        self,
        tenant_id: str,
        *,
        keyword: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        statuses: list[str] | None = None,
        space_id: str | None = None,
        allowed_space_ids: list[str] | None = None,
        task_source: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> list[Task]:
        """列管理端那一页任务，最新建的在前（`created_at DESC`）。

        排序带上 `id DESC` 做 tie-break：同一秒建出来的任务**顺序才是稳的**，
        否则翻页时同一行可能重复出现、也可能整个漏掉。
        """
        conditions = self._console_base_conditions(
            tenant_id,
            {
                "keyword": keyword,
                "start_time": start_time,
                "end_time": end_time,
                "statuses": statuses,
                "space_id": space_id,
                "allowed_space_ids": allowed_space_ids,
                "task_source": task_source,
            },
        )
        stmt = (
            select(Task)
            .where(*conditions)
            .order_by(Task.created_at.desc(), Task.id.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def count_console_tasks(
        self,
        tenant_id: str,
        *,
        keyword: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        statuses: list[str] | None = None,
        space_id: str | None = None,
        allowed_space_ids: list[str] | None = None,
        task_source: str | None = None,
    ) -> int:
        """总数 —— 条件跟 `page_console_tasks` **完全一致**（含 status），否则对不上页数。"""
        conditions = self._console_base_conditions(
            tenant_id,
            {
                "keyword": keyword,
                "start_time": start_time,
                "end_time": end_time,
                "statuses": statuses,
                "space_id": space_id,
                "allowed_space_ids": allowed_space_ids,
                "task_source": task_source,
            },
        )
        result = await self.session.execute(
            select(func.count()).select_from(Task).where(*conditions)
        )
        return result.scalar_one()

    async def count_console_by_status(
        self,
        tenant_id: str,
        *,
        keyword: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        space_id: str | None = None,
        allowed_space_ids: list[str] | None = None,
        task_source: str | None = None,
    ) -> dict[str, int]:
        """各状态各多少条（§4.4）。

        **故意不收 `statuses`**：这个数是页签角标，口径是「只看其它筛选条件」，
        切页签时不能跟着变。库里没有的状态不会出现在返回的字典里，由调用方补 0。
        `space_id` / `allowed_space_ids` / `task_source` **要收** —— 它们一换，
        四个角标都该跟着变（它们是「其它筛选条件」，不是页签）。
        """
        conditions = self._console_base_conditions(
            tenant_id,
            {
                "keyword": keyword,
                "start_time": start_time,
                "end_time": end_time,
                "space_id": space_id,
                "allowed_space_ids": allowed_space_ids,
                "task_source": task_source,
            },
        )
        result = await self.session.execute(
            select(Task.status, func.count())
            .select_from(Task)
            .where(*conditions)
            .group_by(Task.status)
        )
        return {row[0]: row[1] for row in result}

    async def get_task(self, tenant_id: str, task_id: str) -> Task | None:
        """按 id 取任务：已假删的按「找不到」处理（`get_by_id` 默认过滤 is_deleted）。"""
        return await self.get_by_id(task_id, tenant_id)

    async def get_task_fresh(self, tenant_id: str, task_id: str) -> Task | None:
        """按 id 取任务，并**强制绕过会话身份映射**（`populate_existing`）。

        收口时要拿最新的 `state_version` 去做 CAS：`_load_context` 早先把任务行装进过
        这个会话，普通 `select` 只会把那份旧对象还回来，CAS 会白挨一次 0 行。
        """
        stmt = (
            select(Task)
            .where(
                Task.id == task_id,
                Task.tenant_id == tenant_id,
                Task.is_deleted == 0,
            )
            .execution_options(populate_existing=True)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def count_unfinished_by_definition(
        self, tenant_id: str | None, definition_id: str
    ) -> int:
        """定义下还有没有没跑完的实例（`def.delete` 拿它决定拒不拒绝，§4.3）。

        只看 `pending` / `running`——已到终态（completed / failed）的实例不挡删除。

        ⚠️ **`tenant_id=None` 是合法入参，语义是「跨全部租户数」**：`scope='system'` 的
        定义 `tenant_id` 是 NULL（§3.1），可它底下的实例却分属各个租户 —— 删这种定义
        之前必须把**所有租户**的活跃实例都数上，漏掉哪个租户就等于把一条正在跑的任务
        连定义一起删了。
        **不能**把这个 `None` 原样交给 `self.count()`：那条路会把 `None` 读成「调用方
        没给租户」、回落到 `TenantContext`（平台身份 / 后台循环下恒为空），一路走到
        `require_tenant(None)` 抛 `TenantIsolationError` —— 结果是**每一条系统级定义都
        删不掉，而报出来的还是「禁止使用默认租户」**，跟真实原因（还有实例在跑）毫无
        关系。`count_shared` 正是「不叠租户条件」的那条写法。
        """
        conditions = [
            Task.definition_id == definition_id,
            Task.status.in_(list(TASK_ACTIVE_STATUSES)),
        ]
        if tenant_id is None:
            return await self.count_shared(extra_conditions=conditions)
        return await self.count(tenant_id=tenant_id, extra_conditions=conditions)

    async def update_snapshot_cas(
        self,
        tenant_id: str,
        task_id: str,
        expect_version: int,
        *,
        require_active: bool = False,
        expect_current_attempt: int | None = None,
        **values,
    ) -> bool:
        """带版本号更新任务快照。命中 0 行 = 别的副本已推进，本次作废。

        ⚠️ **条件里刻意没有 `is_deleted == 0`，这不是漏了**（别顺手补上 —— 补了也是
        死条件，还会让人以为「假删的任务会被这里改到」）：假删只有 `mark_deleted` 一条路，
        它在同一条 UPDATE 里 `state_version + 1`，所以任务一旦被删，**任何拿旧版本号来的
        CAS 必然命中 0 行**；而拿新版本号来调用的前提是先读到这一行 —— 读的入口
        （`get_task` / `get_task_fresh` / `_load_context`）走 `get_by_id` 与显式条件，
        都过滤 `is_deleted`，已删的行根本交不回来。**这条安全性依赖 `mark_deleted` 那句
        `state_version + 1`**：去掉它这里就会开始改写已删任务的快照。

        ── 两个可选闸门：给「汇总/收口」那条路用的（`require_active` / `expect_current_attempt`）──

        默认都不带（重跑那两条路要的正是「从终态改回 `pending` / `running`」，带闸门就
        写不进去了）。**汇总/收口必须带**，理由是一条「版本号取最新、状态判据却是陈旧的」
        的缝：

        `_aggregate` / `_settle_stopped` 都是「先按手上那份（可能是几秒前读的）上下文判
        『该收口了』→ 再 `get_task_fresh` 取**最新**版本号 → 拿它当 CAS 基准」。只要任务锁
        在这个窗口里**提前失效**（`task_lock` 不续期，租期 30 秒），别的副本就能把这条任务
        推到别处去 —— 最典型的是用户点了「重跑」：任务被 `reset_for_task_retry` 改成
        `pending` + 新 `current_attempt`，而这次汇总会**命中最新版本号**、把新那一轮
        **当场写回 `failed`**（`error_message` 还是上一轮的原因）。此后新一轮那套 `pending`
        子任务永远不会被派活（`_runnable_pending_stmt` 要求任务活跃），用户看到的是
        「重跑成功、任务立刻又失败」—— 一次**静默的「重跑等于没跑」**。

        闸门写进 `WHERE` 才是原子的（只加 Python 侧 `if status not in (...): return` 仍是
        「读—判—写」，锁一丢照样被骗）—— 这与 `mark_running` 里那句「状态闸门必须写进
        `WHERE`」是同一条纪律。
        """
        conditions = [
            Task.id == task_id,
            Task.tenant_id == tenant_id,
            Task.state_version == expect_version,
        ]
        if require_active:
            conditions.append(Task.status.in_(list(TASK_ACTIVE_STATUSES)))
        if expect_current_attempt is not None:
            conditions.append(Task.current_attempt == expect_current_attempt)
        stmt = (
            update(Task)
            .where(*conditions)
            .values(
                state_version=Task.state_version + 1,
                updated_at=datetime.now(),
                **values,
            )
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount > 0

    async def mark_running(
        self,
        tenant_id: str,
        task_id: str,
        expect_version: int,
        started_at: datetime | None = None,
    ) -> bool:
        """任务转「跑着」（§7.2：一旦有子任务被派出去，任务转 `running`）。

        `started_at` 是**首次开始时间**，已经写过就不覆盖（重跑回来时它仍是第一次那个值）。

        ⚠️ 传了 `started_at` 时，调用方**不能在这次调用之后再读同一个 ORM 对象的
        `started_at`**：`func.coalesce(...)` 是 `synchronize_session='evaluate'` 评估不了
        的表达式，SQLAlchemy 会把这一列**作废**（expired）留待下次访问回读 —— 而异步
        session 里那次「回读」是同步 lazy-load，会抛 `MissingGreenlet`。
        需要原值就在调用**之前**取到本地变量。`TaskAttemptRepository.mark_running` 同理。

        ⚠️ 这里**不能复用 `update_snapshot_cas`**：那个方法只比版本号，不看当前状态。
        派活是「锁外出站 → 回来再收尾」（§7.4），出站那段时间里任务完全可能已经被
        收成终态 —— 典型是用户在这段窗口里点了「停止」（`stop_task` 会把任务收成
        `failed`）。收尾时拿到的 `expect_version` 是**刚刚重读的收口后版本**，CAS 会
        如实命中，于是把一个已经失败的任务**翻回 `running`**，而 `error_message` 还
        挂着「用户手动停止」；此后运行行的终态闸门让 `_aggregate` 永远早退，重跑 /
        删除要求终态被拒，再点停止又因为「没有未开始的子任务」被拒 —— 任务永久卡死。

        所以状态闸门必须写进 `WHERE`：**只从 `pending` / `running` 出发**
        （与 `TaskAttemptRepository.mark_running` 的 `status == PENDING` 同构）。
        命中 0 行 = 任务已经不是活跃态，这次「转 running」作废 —— 派活本身照样成立
        （子任务确实在运行方那边跑起来了），由运行方后续上报收口。
        """
        values: dict = {
            "status": TaskStatus.RUNNING.value,
            "state_version": Task.state_version + 1,
            "updated_at": datetime.now(),
        }
        if started_at is not None:
            values["started_at"] = func.coalesce(Task.started_at, literal(started_at))
        stmt = (
            update(Task)
            .where(
                Task.id == task_id,
                Task.tenant_id == tenant_id,
                Task.state_version == expect_version,
                Task.status.in_(list(TASK_ACTIVE_STATUSES)),
            )
            .values(**values)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount > 0

    async def reset_for_task_retry(
        self, tenant_id: str, task_id: str, expect_version: int, current_attempt: int
    ) -> bool:
        """任务重跑：快照回「待执行」并指向新的那次运行（§4.3 `task.retry`）。

        - `current_attempt` 指向刚插的那条 `task_attempts`；
        - `status` 回 `pending`、清 `error_message`、清 `finished_at`；
        - `started_at`（首次开始时间）**不动**；
        - `tasks.inputs` / `definition_version_no` 都不动（复用同一次编排）。
        """
        return await self.update_snapshot_cas(
            tenant_id,
            task_id,
            expect_version,
            current_attempt=current_attempt,
            status=TaskStatus.PENDING.value,
            # 重跑 = 把「上一轮为什么失败」整组清掉：**三个一起清**。只清 `error_message`
            # 的话，读接口按 `error_code` 渲染会把上一轮的原因又渲染回来。
            error_message=None,
            error_code=None,
            error_params=None,
            finished_at=None,
        )

    async def reset_for_subtask_retry(
        self,
        tenant_id: str,
        task_id: str,
        expect_version: int,
        operator_id: str | None = None,
    ) -> bool:
        """子任务重试：把**任务拉回「跑着」**（§4.3 `task.subtask_retry` 第①步）。

        重试意味着"又有活要跑了"，任务不该停在终态；清 `finished_at`，
        `current_attempt` 不动（还是同一次运行，只是某个子任务换了个尝试）。

        ⚠️ **`error_message` 必须跟着一起清**（与 `reset_for_task_retry` 一致）：
        那是**上一轮**的失败原因，这轮还没跑完就把它留着，`task.get` / 控制台会读到
        「状态 `running` + 一条陈旧的错误」这种自相矛盾的快照，直到 `_aggregate` 收口
        才被清掉。文档 §4.3 卡片、签名表、A.9 I4 三处都写明了要清。

        ⚠️ **这一清还有第二个作用**（走的是 `task.stop` 那条路）：用户停止之后再对某个
        子任务点重试时，**正因为它先把这条原因清了**，后到的上报（`_aggregate` 照旧会把
        任务快照改成新进度）才没有「把『用户手动停止』改写掉」这一说 —— §4.4「为什么运行
        那一行必须一起收」那段两道闸，前提就是这个字符串**已经不在库里了**。所以
        **别为了「保留失败原因」把这一清去掉**；`test_task_center_retry_clears_stop_reason.py`
        钉着它。

        **`operator_id` 只在有值时才带上 `updated_by`**（与 `_settle_stopped` 同一条纪律）：
        `update_snapshot_cas` 是「传了什么就写什么」，带一个 `None` 进去会把上一次记的人
        抹掉 —— 那是丢信息，不是「没记」。它是**纯记账**，不参与这里的任何判定。

        （`updated_by` 挂在这一条 UPDATE 上、不另开一次写：这条 CAS 用的是
        `expect_version`，另外写一次会把版本号再推一格、让调用方手上那份快照作废。）
        """
        values = {
            "status": TaskStatus.RUNNING.value,
            # 与 `error_message` 一组清掉（读接口会按 `error_code` 重渲染，留着就是旧原因）
            "error_message": None,
            "error_code": None,
            "error_params": None,
            "finished_at": None,
        }
        if operator_id:
            values["updated_by"] = operator_id
        return await self.update_snapshot_cas(
            tenant_id, task_id, expect_version, **values
        )

    async def mark_deleted(
        self, tenant_id: str, task_id: str, expect_version: int
    ) -> bool:
        """假删任务（`task.delete` 在运行方删除成功后调，§4.3 第 ③ 步）。

        ```sql
        UPDATE tasks SET is_deleted = 1, state_version = state_version + 1
         WHERE id = :id AND is_deleted = 0 AND state_version = :锁内重读到的值
        ```

        **`expect_version` 必须传"锁内重读到的那个值"**，不能拿第一次（没拿锁的）预检里
        读到的值 —— 那个值可能已经过期，用它当 CAS 基准会**误中止**一次本该成功的删除。

        ⚠️ **这里从前还带一句 `status IN ('completed','failed')`，已删** —— 那一条正是
        本轮要治的病灶的一半：它要求**任务快照本身是终态**，于是「子任务全都结束了、任务
        快照却还停在 `pending` / `running`」那条**永久卡死**的任务**连删都删不掉**。
        现在「能不能删」这件事**整体**由 `TaskService._require_deletable` 判（判据是**子任务**
        的状态，不是任务快照的状态），并且在**第 ③ 步拿锁之后复查一遍** —— 检查与更新
        在**同一把锁**里，中间没有别人能插进来，所以 CAS 只留版本号这一道就够
        （**每一个写 `tasks` 的方法都会 `state_version + 1`**，版本没动就等于这行没被动过）。

        **命中 0 行仍然中止删除** —— 那表示有人在这两次拿锁之间动过这一行
        （旧口径里最典型的是 `retry_task` 把它拉回去跑了；现在那一档会被锁内的
        `_require_deletable` 复查更早拦下）。运行方侧虽然已经清干净，任务中心这边不删；
        这条任务跑完以后再删一次即可。

        只改 `is_deleted`，不动状态快照与历史；子任务的级联假删见
        `SubtaskRepository.mark_deleted_by_task`。
        """
        stmt = (
            update(Task)
            .where(
                Task.id == task_id,
                Task.tenant_id == tenant_id,
                Task.is_deleted == 0,
                Task.state_version == expect_version,
            )
            .values(
                is_deleted=1,
                state_version=Task.state_version + 1,
                updated_at=datetime.now(),
            )
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount > 0

    async def list_tasks_needing_settle(
        self, stale_before: datetime, limit: int | None = None
    ) -> list[Tuple[str, str]]:
        """扫「**该收口却没收到口**」的任务，返回 `(tenant_id, task_id)`（§7.5 新增一趟）。

        要救的是这一格：**任务快照还停在 `pending` / `running`，但它的当次运行早就跑完了**
        —— 成因是 `_aggregate` 收口那一刻**任务快照那次 CAS 落空**（见 `_aggregate` ⑤）。
        它一旦发生，后台四趟**一趟都不碰它**（派活找当前运行的 `pending`、超时巡检找
        `running`、防僵尸找当前运行的 `pending`、消化上报要有人上报），而 `retry_*` /
        `task.stop` / `task.delete` 三扇门也各自拒它 —— 从前只剩人工改库。

        **判据要够准**（这一趟每轮都要对候选**拿一次任务锁** —— 拿的正是派活那一趟要的
        同一把，所以「多扫一个健康任务」不只是白查一次，还会跟派活抢锁）：

        - 任务未被假删、状态还在活跃态（`pending` / `running`）；
        - **当次运行里至少有一行子任务**（空任务不救，理由见下面那条 SQL 上的注释）；
        - **当次运行里一个 `running` 都没有**；
        - **当次运行那一行（`task_attempts`）已经收口**（`completed` / `failed`）
          —— **这条才是「要救的那一格」的精确判据**（见下）；
        - 任务行**已经至少 `stale_before` 没被动过**（降噪）。⚠️ 它**只管降噪、不管正确性**。

        ⚠️ **2026-10-09 那次放开**：从前这条判据是「当次运行里 `running` 与 `pending`
        **一个都没有**」，于是**当次运行还剩被上游 hold 住的 `pending`** 那种形态
        （最常见的失败形态：上游 failed、下游 hold 成 `pending`）**进不来** —— 而它恰恰是
        「收口 CAS 落空」残格最常见的子形态，只能靠用户点「停止」出去。现在换成
        「**运行行已经收口**」：⚠️ **它不会顺手捞进健康任务** —— 健康任务的运行行还是
        `pending` / `running`（「刚建好正等派活」也是），所以跟派活抢锁的候选没变多。

        ⚠️ **仍然不加「有 `failed` 就进来」那一支**（2026-09-29 试过又撤了，别再加回来）：
        它会顺手把「下游被停掉、上游还在排队」这种**完全健康**的形态每轮扫进来拿锁，
        而那种任务本来就该继续跑（计划中的 `parse_1` 还没派活）——
        这与上面那条**不是一回事**：那条问的是「这次运行**收口了没有**」（收了口 = 没有任何东西
        还会动它），这条问的是「有没有失败」（还在跑的任务里当然可能有失败）。

        ⚠️ **只看「当次运行」**（`_current_task_attempt_id_subquery`）：旧运行里那些
        「被上游失败卡住、永远不会再跑」的 `pending`，与「这条任务还该不该继续跑」无关
        —— 它们既不会被派活、也不会被重试（两条路都只认当次运行）。拿它们当判据，正是
        「卡死时连删除都拒」那个缺口的成因。
        """
        current_attempt = _current_task_attempt_id_subquery()

        def _has_in_current_run(status: str):
            """当次运行里「有没有这个状态的子任务」。"""
            return exists().where(
                Subtask.task_attempt_id == current_attempt,
                Subtask.is_deleted == 0,
                Subtask.status == status,
            )

        def _has_running_subtask_in_current_run():
            """当次运行里有 `running` 的行 —— 有就说明还有活在动，不能收。"""
            return _has_in_current_run(SubtaskStatus.RUNNING.value)

        # 当次运行那一行**已经收口**（`completed` / `failed`）= 「该收口却没收到口」那一格的
        # 精确判据：`_aggregate` ④ 把运行行收了口、⑤ 的任务快照那次 CAS 落空，留下的正是它。
        # 健康任务（含「刚建好等派活」）的运行行还停在 `pending` / `running`，一律不满足。
        current_run_is_closed = exists().where(
            TaskAttempt.id == current_attempt,
            TaskAttempt.status.in_(ATTEMPT_TERMINAL_STATUSES),
        )

        stmt = (
            select(Task.tenant_id, Task.id)
            .where(
                Task.is_deleted == 0,
                Task.status.in_(TASK_ACTIVE_STATUSES),
                Task.updated_at <= stale_before,
                # ⚠️ **当次运行至少得有一行子任务** —— 否则「一行都没有的空任务」也会满足
                #    下面那些条件，被本趟收成 `completed`（`_run_settled([])` 恒真）。
                #    那种任务意味着**创建时哪里就错了**（正常路径不可达：发版校验 N0 要求
                #    `steps` 非空、每步必有 `node_id`），把它标成「完成」等于把问题**静默藏掉**；
                #    留在非终态至少是可见的，而 `task.stop` 有一条专门处理它的分支（「空任务照收」）。
                #    **本趟只救「该收没收」的，不替创建环节的 bug 擦屁股。**
                exists().where(
                    Subtask.task_attempt_id == current_attempt,
                    Subtask.is_deleted == 0,
                ),
                ~_has_running_subtask_in_current_run(),
                current_run_is_closed,
            )
            .order_by(Task.updated_at)
        )
        # `limit=None` = 不设上限（与 `list_timed_out_for_patrol` / `list_stuck_pending` 同口径：
        # 这一趟是「兜底收口」且完全不出站，卡上限只会让积压排队等收口）。
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self.session.execute(stmt)
        return [(row[0], row[1]) for row in result.all()]


class TaskAttemptRepository(BaseRepository[TaskAttempt]):
    """任务尝试仓储：每次运行（含重跑）一条，只追加不覆盖。"""

    model = TaskAttempt

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def next_attempt_no(self, task_id: str) -> int:
        stmt = select(func.max(TaskAttempt.attempt)).where(TaskAttempt.task_id == task_id)
        result = await self.session.execute(stmt)
        return (result.scalar_one_or_none() or 0) + 1

    async def get_by_attempt_no(
        self, tenant_id: str, task_id: str, attempt: int
    ) -> TaskAttempt | None:
        """按「第几次运行」取一条（`subtask_list` 指定 attempt 时的入口）。"""
        stmt = select(TaskAttempt).where(
            TaskAttempt.tenant_id == tenant_id,
            TaskAttempt.task_id == task_id,
            TaskAttempt.attempt == attempt,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_for_update(
        self, tenant_id: str, task_attempt_id: str
    ) -> TaskAttempt | None:
        """取一次运行并**锁住它那一行**（`SELECT … FOR UPDATE`），收口前用（§7.2）。

        收口（`_aggregate`）必须先串行化：同一运行的两个子任务几乎同时上报时，两个事务
        会各自读到「对面那个兄弟还在 `running`」，双双判定「还没收敛」而放弃收口 ——
        此后没有任何机制再汇总（派活扫描要 `pending`、超时巡检要 `running`，都扫不到），
        任务就永远停在 `running`，而重跑 / 删除都要求终态，只能人工改库。

        锁在这一行（而不是子任务行）上：每个事务都先改自己的子任务、再来抢它，
        谁最后抢到谁就必然看得见前面所有人的提交，由它完成收口。子任务行不能锁——
        两个事务会各持一行、再互等另一行，直接死锁。
        """
        stmt = (
            select(TaskAttempt)
            .where(
                TaskAttempt.id == task_attempt_id,
                TaskAttempt.tenant_id == tenant_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_task(
        self, tenant_id: str, task_id: str, offset: int = 0, limit: int = 20
    ) -> list[TaskAttempt]:
        """列某任务的运行历史（每次运行一条，只追加；查历史用它）。

        `limit` 由调用方按 `page_size` 给足——**这里不设隐含上限**（§4.0 分页约定）。
        """
        stmt = (
            select(TaskAttempt)
            .where(TaskAttempt.tenant_id == tenant_id, TaskAttempt.task_id == task_id)
            .order_by(TaskAttempt.attempt.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def count_by_task(self, tenant_id: str, task_id: str) -> int:
        """配套 `list_by_task` 的总数（§4.0：返回里一定有 `total`）。"""
        return await self.count(
            tenant_id=tenant_id,
            extra_conditions=[TaskAttempt.task_id == task_id],
        )

    async def mark_running(
        self,
        tenant_id: str,
        task_attempt_id: str,
        started_at: datetime | None = None,
    ) -> bool:
        """这次运行转「跑着」（第一个子任务被派出去时）。

        只从 `pending` 出发：重复调用命中 0 行，`started_at` 不会被打乱。
        """
        values: dict = {"status": AttemptStatus.RUNNING.value}
        if started_at is not None:
            values["started_at"] = func.coalesce(
                TaskAttempt.started_at, literal(started_at)
            )
        stmt = (
            update(TaskAttempt)
            .where(
                TaskAttempt.id == task_attempt_id,
                TaskAttempt.tenant_id == tenant_id,
                TaskAttempt.status == AttemptStatus.PENDING.value,
            )
            .values(**values)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount > 0

    async def reopen_for_subtask_retry(
        self, tenant_id: str, task_attempt_id: str
    ) -> bool:
        """子任务重试第①步的另一半：**把这次运行一起拉回「跑着」**（§4.3）。

        文档 §4.3 `task.subtask_retry` 只写了「把任务拉回 `running`」，但 `task_attempts`
        的那一行已经随任务收口成了 `completed` / `failed`。不一起拉回来的话，重试完这次
        运行再收尾时 `finish_attempt_cas` 只认 `pending` / `running` 前驱，会命中 0 行 ——
        任务就永远停在 `running` 收不掉了。所以这一步是那次收口的**逆操作**：
        终态 → `running`，清 `finished_at` 与 `error_message`（`started_at` 不动）。

        ⚠️ **`error_message` 一起清**：理由同 `reset_for_subtask_retry` —— 它是上一轮
        留下来的，跟「运行正在跑」并存会读出一个自相矛盾的快照。

        只从终态出发：还在 `pending` / `running` 的运行改不动（那种情况本来就不该走到
        子任务重试这条路——重试的前提是任务已终态）。
        """
        stmt = (
            update(TaskAttempt)
            .where(
                TaskAttempt.id == task_attempt_id,
                TaskAttempt.tenant_id == tenant_id,
                TaskAttempt.status.in_(
                    [AttemptStatus.COMPLETED.value, AttemptStatus.FAILED.value]
                ),
            )
            .values(
                status=AttemptStatus.RUNNING.value,
                error_message=None,
                error_code=None,
                error_params=None,
                finished_at=None,
            )
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount > 0

    async def finish_attempt_cas(
        self,
        tenant_id: str,
        task_attempt_id: str,
        next_status: str,
        finished_at: datetime | None = None,
        **values,
    ) -> bool:
        """收口一次运行。只有还没到终态的尝试才收得住，重复收口命中 0 行。"""
        stmt = (
            update(TaskAttempt)
            .where(
                TaskAttempt.id == task_attempt_id,
                TaskAttempt.tenant_id == tenant_id,
                TaskAttempt.status.in_(
                    [AttemptStatus.PENDING.value, AttemptStatus.RUNNING.value]
                ),
            )
            .values(
                status=next_status,
                finished_at=finished_at or datetime.now(),
                **values,
            )
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount > 0


class SubtaskRepository(BaseRepository[Subtask]):
    """子任务仓储：一行存「该子任务最新一次尝试」。"""

    model = Subtask

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def create_subtasks(self, rows: list[dict]) -> list[Subtask]:
        """一次运行诞生时批量建子任务树（全部 pending）。"""
        objects = [Subtask(**row) for row in rows]
        self.session.add_all(objects)
        await self.session.flush()
        return objects

    async def get_any_by_id(self, subtask_id: str) -> Subtask | None:
        """按主键取一行，**不带租户条件**（后台循环用）。

        后台循环的扫描是跨租户的，扫出来的只是一批 id；真正动手时每个 id 单独开一个
        会话重新取行——这时没有租户上下文可用，所以由本方法按主键取。行自带
        `tenant_id`，后续动作都按行内的租户显式走，跟 `list_*_for_*` 是一套口径。

        已假删的行（扫到之后任务刚被删）按「找不到」返回，派活 / 超时巡检据此跳过。
        """
        stmt = select(Subtask).where(
            Subtask.id == subtask_id,
            Subtask.is_deleted == 0,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_execution_id(
        self,
        tenant_id: str,
        space_id: str,
        execution_id: str,
        *,
        refresh: bool = False,
    ) -> Subtask | None:
        """按受理凭证定位那唯一一行（`report_status` 用，§4.2、§7.2）。

        `execution_id` 在**同一租户 + 同一空间**内唯一（`uq_tc_subtask_exec` 是
        `(tenant_id, space_id, execution_id)` 的部分唯一索引），所以三者一起给、
        直接命中唯一那一行。已假删的行（任务被删后运行方迟到的上报）按「找不到」
        返回，调用方据此丢弃这条上报、不推进任何状态。

        `refresh=True` 强制绕过会话身份映射（`populate_existing`）—— **`_absorb` 那次
        重读必须开**（`report_status` **自己不拿任务锁**，它是后台消化那条路
        `_report_one` 持锁调进来的）：锁外那次「只读定位」已经把同一行装进过这个会话，
        不刷新的话读到的是那份**旧对象**，「等锁期间被别人改过」就永远看不见。
        """
        stmt = select(Subtask).where(
            Subtask.tenant_id == tenant_id,
            Subtask.space_id == space_id,
            Subtask.execution_id == execution_id,
            Subtask.is_deleted == 0,
        )
        if refresh:
            stmt = stmt.execution_options(populate_existing=True)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_attempt(
        self, tenant_id: str, task_attempt_id: str
    ) -> list[Subtask]:
        stmt = (
            select(Subtask)
            .where(
                Subtask.tenant_id == tenant_id,
                Subtask.task_attempt_id == task_attempt_id,
                Subtask.is_deleted == 0,
            )
            .order_by(Subtask.sort, Subtask.node_id)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def list_current_by_task(self, tenant_id: str, task_id: str) -> list[Subtask]:
        """列最**新一次运行**的子任务（`subtask_list` 不给 attempt 时的默认口径）。

        用 `tasks.current_attempt` 指的那次运行定位，而不是"子任务里最大的
        task_attempt_id"——`retry_task` 之后新建的那套才是"最新"，旧那套是冻结的历史。
        """
        stmt = (
            select(Subtask)
            .where(
                Subtask.tenant_id == tenant_id,
                Subtask.task_id == task_id,
                Subtask.is_deleted == 0,
                Subtask.task_attempt_id
                == select(TaskAttempt.id)
                .join(Task, Task.id == TaskAttempt.task_id)
                .where(
                    TaskAttempt.task_id == task_id,
                    TaskAttempt.attempt == Task.current_attempt,
                )
                .scalar_subquery(),
            )
            .order_by(Subtask.sort, Subtask.node_id)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def get_subtask(
        self, tenant_id: str, task_attempt_id: str, node_id: str
    ) -> Subtask | None:
        """在某一次运行里按节点取子任务（`node_id` 在同一次运行内唯一）。

        子任务的身份是「**哪一次运行 + 哪个节点**」（§3.5）：`node_id` 只在**同一次运行内**
        唯一，跨运行会重复出现（`retry_task` 之后新那套里同一个 `parse_1` 又在），
        所以必须带上 `task_attempt_id` 才定位得到唯一一行。
        """
        stmt = select(Subtask).where(
            Subtask.tenant_id == tenant_id,
            Subtask.task_attempt_id == task_attempt_id,
            Subtask.node_id == node_id,
            Subtask.is_deleted == 0,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def transition_cas(
        self,
        tenant_id: str,
        subtask_id: str,
        *,
        expect_statuses: list[str] | tuple[str, ...],
        next_status: str,
        expect_version: int | None = None,
        expect_execution_id: str | None = None,
        **values,
    ) -> bool:
        """子任务状态推进的唯一落库口（§7.2）。

        - `expect_statuses`：合法前驱状态集合；结束态不在任何前驱集合里，
          所以「结束状态一旦写入就改不回去」是这条 SQL 自然的结果。
        - `expect_version`：乐观锁，不带就是「不校验版本」（仅内部明确知道安全时用）。
        - `expect_execution_id`：幂等裁决，对不上就是旧尝试/别的执行的迟到上报。

        `tenant_id` 取自调用方手上那行子任务（`subtask.tenant_id`）：后台循环虽然
        跨租户扫描，但每一行都自带租户，动手时按行内的租户显式带上。
        """
        conditions = [
            Subtask.tenant_id == tenant_id,
            Subtask.id == subtask_id,
            Subtask.status.in_(list(expect_statuses)),
            # **假删除的子任务一律推不动**。这是「删任务」与「派活」擦肩而过时的兜底：
            # 删任务把整批子任务标成 `is_deleted=1`，而它的事务提交在任务锁**放掉之后**
            # （`capability._execute` 的会话在 action 返回后才提交）—— 另一个副本正好在这个
            # 缝里抢到锁、读到未提交的旧像（子任务还是 `pending`、`state_version` 未变），
            # 就会把它 CAS 成 `running` 并**真的派给运行方去跑**；而任务已经删了，没人收
            # 它的结果。状态闸门本来就不该对已删的行放行。
            Subtask.is_deleted == 0,
        ]
        if expect_version is not None:
            conditions.append(Subtask.state_version == expect_version)
        if expect_execution_id is not None:
            conditions.append(Subtask.execution_id == expect_execution_id)

        stmt = (
            update(Subtask)
            .where(*conditions)
            .values(
                status=next_status,
                state_version=Subtask.state_version + 1,
                updated_at=datetime.now(),
                **values,
            )
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount > 0

    async def claim_pending(
        self,
        tenant_id: str,
        subtask_id: str,
        expect_version: int,
        execution_id: str,
        started_at: datetime | None = None,
    ) -> bool:
        """派活抢占：`pending → running`，**同一条 UPDATE 里**回填 `execution_id` 并
        `state_version + 1`（§7.5 ①）。

        命中 0 行 = 别的副本已经派了，本轮跳过。`execution_id` 是调用方**抢占之前**
        就生成好的，这里一并落库，随后随 `execute` 传给运行方。

        `started_at` 由调用方一次算好并传入——它要与同一次尝试在
        `subtask_attempts.started_at` 里的值**完全相同**（§7.2），所以不能让两侧
        各取一次 `datetime.now()`。不传则本方法兜一个当前时刻。
        """
        return await self.transition_cas(
            tenant_id,
            subtask_id,
            expect_statuses=[SubtaskStatus.PENDING.value],
            next_status=SubtaskStatus.RUNNING.value,
            expect_version=expect_version,
            execution_id=execution_id,
            started_at=started_at or datetime.now(),
        )

    async def reset_for_retry(
        self,
        tenant_id: str,
        subtask_id: str,
        expect_version: int,
        *,
        depends_on: list[str] | None = None,
        next_steps: list[str] | None = None,
    ) -> bool:
        """子任务重试：`subtasks` 行重置回「排队中」（`current_attempt`+1、清产出与凭证）。

        **只对已到终态的 `success` / `failed` / `timeout` 开放**（§4.3）：
        `pending` / `running` 都还没结束，不在合法前驱里，这条 SQL 自然命中 0 行。

        回到 `pending` 而不是直接置 `running`：这样 `claim_pending` 仍是**唯一**
        进「跑着」的路径（§7.5 ①），重试与首次派活走完全同一条路，只是
        `current_attempt` 记着「这是第几次」。`execution_id` 与两侧时间一并清掉
        （新尝试会重新生成 / 重写）。

        旧尝试的记录不动，留在 `subtask_attempts` 里供回溯。

        ── `depends_on` / `next_steps`（两个都默认 `None` = 一个字不动那一列）──

        只有「批量重跑」（`TaskService.retry_subtasks`）会传它们：那一批的**顺序**（第 i 步
        等第 i-1 步）就落在这条 UPDATE 里 —— 与「重置」**同一条语句**，不留「重置了但顺序
        还没连上」的中间态（那个中间态里下游的依赖当场是满足的，下一轮派活就会把它跟
        上游一起派出去）。

        ⚠️ **两张图必须一起改，只写一张会静默出错**：调度的「谁先谁后」看 `depends_on`，
        而「上游成功后重算下游入参」看 `next_steps`（`RunContext.downstream_closure` 顺着它
        摸）。只补 `depends_on` 的话，下游**会等**上游、却**不会拿到**上游的新产出 ——
        它会带着上一轮的旧入参跑，正是「重跑」这套接口要防的事。

        ⚠️ **这两列在这一行上是「本次运行的连线」，不是定义的一部分**：传进来的必须是
        **算好的整份新列表**（原值 + 新边），不是增量 —— 这里不做 JSONB 拼接，也不
        去重（拼接语义在 SQL 里写一遍、又在 Python 里判一遍，两处口径迟早分叉；调用方
        手上本来就有整行）。
        """
        values: dict = dict(
            current_attempt=Subtask.current_attempt + 1,
            execution_id=None,
            outputs=None,
            # ⚠️ **`output_refs` 跟 `outputs` 一起清**（2026-10-09 补）：两列是同一族的
            # 「产出」字段（一个放正文、一个放「去哪儿看」的引用）。只清前者的话，
            # 一个**还没跑**的新尝试上仍然挂着**上一轮**的「查看产物」入口 ——
            # 用户点进去看到的是上一轮的东西，会把「重跑了」误判成「重跑没用」。
            # （上报路径那处「`None` 就不动这一列」是**另一条路**、语义不同：
            # 那是「老运行方不上报时保持原值」，不是「重置」。）
            output_refs=None,
            # ⚠️ **`input_refs` 同理一起清**（2026-10-16 加）：它是**输入**那一侧的引用，
            # 而重试会把这一步的 `inputs` 按上游现在的产出**重算一遍**（见
            # `_fill_inputs`）—— 旧引用指向的是旧输入，留着就是「重跑了、输入入口还是老的」。
            input_refs=None,
            # 失败原因**一组三个一起清**（`error_message` / `error_code` / `error_params`）——
            # 与上面「产出那两个一起清」是同一条理由：只清一半，读接口会把上一轮的原因
            # 按代号重新渲染出来，用户看到「重跑了、原因还是老的」。
            error_message=None,
            error_code=None,
            error_params=None,
            started_at=None,
            finished_at=None,
            # ⚠️ **`last_queried_at` 跟 `started_at` 一起清**（2026-10-13 补）：对账候选
            # （`list_silent_in_flight`）的判据与排序都用 `COALESCE(last_queried_at, started_at)`。
            # 只清 `started_at` 的话，这一行还留着**上一轮尝试**被问过的旧时间戳 → `COALESCE`
            # 取到旧值 → 新尝试一派活（`started_at` 已是新值），只要旧值早于 `now - quiet_sec`，
            # 它**下一拍就被立刻问一次**（白打一次运行方），排序还被推到队头。
            # 清掉后 `COALESCE` 落到新的 `started_at`，与「新尝试还没被问过」的语义一致。
            last_queried_at=None,
        )
        if depends_on is not None:
            values["depends_on"] = depends_on
        if next_steps is not None:
            values["next_steps"] = next_steps
        return await self.transition_cas(
            tenant_id,
            subtask_id,
            expect_statuses=[
                SubtaskStatus.SUCCESS.value,
                SubtaskStatus.FAILED.value,
                SubtaskStatus.TIMEOUT.value,
                # `stopped` 与上面三个同档：被停掉的那一步**已经结束了**，重跑它是
                # 「把这一步重新排队」—— 与重跑一个失败步骤没有任何区别。
                # 少了它，用户停错一步就再也点不动「重跑」（CAS 命中 0 行 → 报状态冲突）。
                SubtaskStatus.STOPPED.value,
            ],
            next_status=SubtaskStatus.PENDING.value,
            expect_version=expect_version,
            **values,
        )

    def _runnable_pending_stmt(self):
        """「能跑的 `pending`」的公共查询体 —— **派活（§7.5 ①）与防僵尸（§7.5）两处必须同源**。

        判据（缺一不可）：`status = pending` 且子任务 / 所属任务都未被假删；任务还在跑
        （`pending` / `running`）；属于任务当前那次运行；**真的能跑**（`depends_on` 里每个
        节点都 `success`，等价地写进 SQL）。

        **抽成一个方法而不是各写一遍**：这两处的「能跑」判据一旦分叉，防僵尸那一路就会
        把**上游失败留下来的 `pending`**（按设计要永远 hold、一直不动）也判成超时，
        等于悄悄改掉了 §6.3 的 hold 语义。同一个来源，谁也改不歪。
        """
        # 「依赖没全成功」的等价写法：依赖里存在某个节点（同一次运行里的那个 node_id）
        # 它的状态不是 success。deps 用 jsonb_array_elements_text 展开成行集，
        # 关联到外层候选行的 depends_on；up 是内层那个"依赖节点"的别名。
        # 连线与引用一律按 node_id（§3.5），所以这里比的是 up.node_id。
        #
        # ⚠️ **与 Python 侧 `_deps_satisfied` 有一处已知差异，是刻意留下的**：
        #    `depends_on` 里写了一个**不存在的 `node_id`** 时（发版校验 N2 本该打回，
        #    只有手工改库 / 改 JSON 才碰得到），这里 `NOT EXISTS` 为真 → 判「能跑」；
        #    而 Python 侧 `sibling is None` 判「不满足」。**影响良性、不构成死锁**，
        #    但**别把它读成「防僵尸会兜底把它清掉」**（早先这里就是那么写的，是错的）。
        #    这条行的实际归宿分三段：
        #
        #    1. **派不出去**：进锁后被 `claim_for_dispatch` 的同一把 Python 尺子拒掉。
        #       代价是**每轮白占一个扫描名额 + 拿一次任务锁**；`_dispatch_one` 返回
        #       False 不进 `dispatched`，所以**不吃 `BATCH` 派活名额**（不是「占一个
        #       派活名额」——`BATCH` 管的是「一轮真派出去几条」）。
        #    2. **拦不住收尾**：`_run_settled` 认的也是 Python 那把尺子
        #       （`_is_runnable_pending` → `_deps_satisfied` 判 False），**不把它算作
        #       「还能跑」**，所以任务照常收口（没有别的问题时收成 `completed`，
        #       而这一步**从没跑过**）。
        #    3. **任务一离线它就没人管了**：本方法谓词里有
        #       `Task.status.in_(TASK_ACTIVE_STATUSES)`，任务离开活跃态后这条行
        #       **同时从派活与防僵尸两份候选集里消失**；而超时巡检只扫 `running`
        #       —— 三趟都够不着它，它永远停在 `pending`。
        #
        #    所以「防僵尸会不会清掉它」**只取决于任务在阈值（`timeout_seconds × 3`）
        #    之前收没收口**：任务迟迟不收口（坏依赖挂在入口节点上、别的步骤拿不到输入，
        #    任务一直是活跃态）→ 它留在候选里、到期判 `timeout`；任务先收口 → 永远 `pending`。
        #
        #    没把这个差异写进 SQL，是因为「每个依赖名都有一行 `success`」要再嵌一层
        #    相关子查询（先展开成行集、再对行集逐个 NOT EXISTS）——为一个正常路径上
        #    不可达的分支去改派活主查询，风险大于收益。**两侧的注释都指向这里**，
        #    将来真要收口口径，改的是这一段。
        #
        # 子任务诞生时就插了一条 pending 的历史行，所以下面的 created_at 一定取得到；
        # 外连接 + nulls_last 只是给「这条历史行缺失」的异常数据留个后路，不让它整批排不出去。
        up = aliased(Subtask)
        # `.correlate(Subtask)` 不能省：这一层嵌在 exists() 里（比外层主查询深一级），
        # 自动关联看不到外层的 subtasks，会把 subtasks 塞进自己的 FROM 变成一个笛卡尔积。
        dep_nodes = select(
            func.jsonb_array_elements_text(
                func.coalesce(Subtask.depends_on, text("'[]'::jsonb"))
            )
        ).correlate(Subtask)
        has_unsatisfied_dep = (
            select(literal(1))
            .select_from(up)
            .where(
                up.task_attempt_id == Subtask.task_attempt_id,
                up.node_id.in_(dep_nodes),
                up.status.notin_(list(SUBTASK_SATISFIED_STATUSES)),
            )
            .exists()
        )
        return (
            select(Subtask)
            .join(Task, Task.id == Subtask.task_id)
            .join(TaskAttempt, TaskAttempt.id == Subtask.task_attempt_id)
            .outerjoin(
                SubtaskAttempt,
                (SubtaskAttempt.subtask_id == Subtask.id)
                & (SubtaskAttempt.attempt == Subtask.current_attempt),
            )
            .where(
                Subtask.status == SubtaskStatus.PENDING.value,
                Subtask.is_deleted == 0,
                Task.is_deleted == 0,
                Task.status.in_(list(TASK_ACTIVE_STATUSES)),
                Subtask.task_attempt_id == _current_task_attempt_id_subquery(),
                ~has_unsatisfied_dep,
            )
        )

    async def list_stuck_pending(
        self,
        now: datetime | None = None,
        *,
        multiplier: int = 3,
        limit: int | None = None,
    ) -> list[Subtask]:
        """扫「**就绪了却一直没被派出去**」的僵尸 `pending`（跨租户，后台循环用，§7.5）。

        判据：`现在 − updated_at > timeout_seconds × multiplier`，**乘数是配置项**
        （`TASK_CENTER_PENDING_STUCK_TIMEOUT`，默认 3 —— **配的是倍数、不是秒数**；
        `timeout_seconds` 每个子任务各不相同，所以阈值也各不相同）。

        **候选集必须跟派活完全一致**（同一份 `_runnable_pending_stmt`）：只有"真能跑却没跑成"
        的才算僵尸。上游失败 / 超时留下来的 `pending` 按设计要**永远 hold**，
        把它们判成超时等于改掉 §6.3 的语义。

        **⚠️ 满载时连"正常在排队"的也会被它判掉 —— 这是有意的取舍**（附录 A.4 D2b）：
        没有这个判据，「派活一路失败但状态回退不了」的 `pending` 会永远占着队头。

        **默认不设上限**（`limit=None`）—— 这是文档钉死的（附录 A.4 D2b）：「D2b 自己清退」
        是队列排空的第二条路，它每轮会把「过了自己阈值」的就绪 `pending` **一次全置
        `timeout`**，而且**这个清退不受 `BATCH` 限制**。`BATCH` 只管派活那条路
        （§7.5 的"单轮上限 10 条"点的是 ⓪ 触发抢占和 ① 派活）。
        **卡到 `BATCH` 会把 D2b 的兜底能力直接砍掉** —— 积压远大于 `BATCH` 时，
        每一轮只清掉队头那几条，剩下的僵尸要排很多轮才轮得到，正是它要防的场景。
        **保留 `limit` 参数**是给单测和将来可能的限流留口子，生产调用不传。
        """
        now = now or datetime.now()
        deadline = Subtask.updated_at + func.make_interval(
            0, 0, 0, 0, 0, 0, Subtask.timeout_seconds * multiplier
        )
        stmt = (
            self._runnable_pending_stmt()
            .where(deadline <= now)
            .order_by(Subtask.updated_at)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def list_silent_in_flight(
        self,
        now: datetime | None = None,
        *,
        quiet_seconds: int,
        limit: int = 50,
    ) -> list[Subtask]:
        """扫「**派出去了、却一直没消息**」的在途子任务（跨租户，后台循环用，§7.5）。

        这是**对账**（拉）的输入：管理方主动去问运行方「这一步现在什么情况」。

        ## 判据

        - `status IN ('pending', 'running')` —— 在途；
        - **`execution_id IS NOT NULL`** —— **派出过活**。空的那是「还没派」，
          问运行方没有任何意义（它压根没收到这一步）；
        - `现在 − started_at > quiet_seconds` —— **沉默够久**才问。

        ⚠️ **`quiet_seconds` 是一个固定秒数，不是按子任务的 `timeout_seconds` 算倍率**
        （与 `list_stuck_pending` 那种「各算各的」刻意不同）：对账问的是
        「**你那边到底怎么样了**」—— 问早一点没有代价（问到就收口、问到「还在跑」就再等），
        唯一的成本是**打扰运行方**，所以定一个全局节拍就够了。

        ⚠️ **`limit` 必须有**（与 `list_stuck_pending` 的「默认不限」相反）：
        对账每问一条就是**一次跨服务调用**，不限量会在积压时把运行方打爆。
        """
        now = now or datetime.now()
        deadline = now - timedelta(seconds=quiet_seconds)
        # **判据与排序都用 `COALESCE(last_queried_at, started_at)`**：
        # 问过就往后排（自然轮转），没问过就用派活时刻当起点。
        # ⚠️ 只用 `started_at` 会「每拍重问最老的那批、新的永远排不进来」
        # （详见模型上 `last_queried_at` 那一列的说明）。
        last_touch = func.coalesce(Subtask.last_queried_at, Subtask.started_at)
        stmt = (
            select(Subtask)
            .join(Task, Task.id == Subtask.task_id)
            .where(
                Subtask.status.in_(
                    [SubtaskStatus.PENDING.value, SubtaskStatus.RUNNING.value]
                ),
                Subtask.execution_id.isnot(None),
                Subtask.started_at.isnot(None),
                last_touch <= deadline,
                Task.is_deleted == 0,
                # ⚠️ **只要活跃任务**（与 `list_stuck_pending` / `list_pending_for_dispatch`
                # 同口径）：`task.stop` 只判死**还没开始**的步骤，**正在跑的那一步仍是
                # `running`** —— 那种残留如果进了候选，会**每拍问一次、而运行方恒答
                # 「这活不归我」**（接管行早被 `task_stopped` 交还了），一直刷到硬超时。
                Task.status.in_(list(TASK_ACTIVE_STATUSES)),
            )
            .order_by(last_touch)
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def mark_queried(self, subtask_id: str, *, at: datetime | None = None) -> None:
        """记下「对账在 `at` 这个时刻问过这条子任务」（`_query_pass` 专用）。

        ⚠️ **无论问到什么都必须调**（问到「还在跑」也算问过）—— 否则它还留在队头，
        下一拍又问同一批（`list_silent_in_flight` 的注释里讲了后果）。

        ⚠️ 这是**对账这条只读通路唯一的写**，写的是「我什么时候问过」这种**运行痕迹**，
        不是任何业务状态 —— 别把它当成「查询可以有副作用」的先例。
        """
        await self.session.execute(
            update(Subtask)
            .where(Subtask.id == subtask_id)
            .values(last_queried_at=at or datetime.now())
        )
        await self.session.flush()

    async def list_pending_for_dispatch(self, limit: int = 10) -> list[Subtask]:
        """扫「能跑的子任务」（跨租户，后台循环用，§6.3、§7.5 ①）。

        候选条件见 `_runnable_pending_stmt`（派活与防僵尸共用同一份判据）。
        **判据与 `task_service` 的 `_deps_satisfied` / `_is_runnable_pending` 同源** ——
        重试不额外开口子（`retry_subtask` 自己就会校验上游，§4.3），分叉会变成死锁。
        **上游失败 / 超时留下来的 `pending`**（依赖永远满足不了）不在其中 —— 它不进候选，
        也就不会把任务永远卡在 `running`（§7.2 那张收尾表第 2 条）。

        出场顺序就是派活顺序（先来先服务，§6.3）：先按主任务的运行创建时间
        `task_attempts.created_at` 升序（**早触发的任务优先**），同一任务内再按
        子任务尝试创建时间 `subtask_attempts.created_at` 升序（**早进队列的步骤先派**）。

        **`limit` 是「一轮扫几条候选」，不等于「一轮派出去几条」**：调用方
        （`TaskScheduler._dispatch_pass`）传的是
        `TASK_CENTER_SCHEDULER_BATCH × TASK_CENTER_SCHEDULER_SCAN_FACTOR` ——
        **故意比 `BATCH` 宽**。因为被 `max_concurrency` 挡下的候选一个字都不写库、
        连排序键都没动，下一轮还在这批候选的原位上；扫描面若就等于 `BATCH`，队头那几条
        一旦全卡在同一个瓶颈上就会**每轮吃满名额**，把后面配额空着的活饿死。
        排出去的条数由调用方自己数，到 `BATCH` 就停 —— **本方法不管那个数**。
        """
        stmt = (
            self._runnable_pending_stmt()
            .order_by(
                TaskAttempt.created_at.asc(),
                SubtaskAttempt.created_at.asc().nulls_last(),
            )
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def count_running_by_catalog_step(
        self, step_code: str, task_catalog_code: str
    ) -> int:
        """数「**同一个内置目录下的同一个步骤**」当前有多少个在跑（并发上限判定用，§6.3）。

        口径：`subtasks.status = running`、`subtasks.step_code` 相同、**且所属任务的
        `tasks.task_catalog_code` 相同**、且两侧都未被假删。

        ── 为什么是「目录 + 步骤」，不是「全平台 + 步骤」（2026-10-14 改）──

        改之前只按 `step_code` 数：**全平台所有目录、所有租户共用一份名额**。后果实测过：

        > `file_processing_jonex_wiki` 的 10 个 `compile` 卡住不结束，
        > **另一个目录 `file_processing_ontology` 的 `compile` 一个都派不出去** ——
        > 两者互不相干，却排在同一份额度后面，队头一堵全体饿死（2026-10-14 那次
        > 三条任务从头到尾没被派发，就是这么来的）。

        现在：**每个目录的每个步骤各有一份名额**，上限数值取该目录那一行配置的
        `max_concurrency`（`ctx.catalog_steps` 本来就是按本任务的目录查出来的）。
        同一个目录内、同一个步骤挂在多个节点上（`parse_1` / `parse_2`）**仍然共享**
        这一份 —— **这一条没变**，变的是「跨目录不再互相占位」。

        ⚠️ **总量会翻**：全平台最大并发从「N」变成「Σ(每个目录各 N)」。这是 2026-10-14
        明确接受的代价（上限数值仍按目录各配 10）。要收紧就得逐目录调那个数。

        ── 为什么现在要 join `tasks` ──

        目录码在 `tasks` 上，`subtasks` 没有这一列，所以必须 join（改之前是「不 join
        任何表」）。join 之后顺手带上 `Task.is_deleted == 0`：「已删任务的残留不吃名额」
        这条保证从**只靠 `delete_task` 的级联**，变成**级联 + 本查询显式过滤**两道。
        多这一道不改变正常行为，只是把那条依赖显式写出来。

        ⚠️ 仍然要盯的可见状态：**「任务已假删、子任务还在跑」**（级联漏了才会出现）——
        此时本查询数不到它（好事，不吃名额），**但超时巡检也收不到它**（那边内连接
        `tasks` 且要求未假删），那行就再没有任何出口。**动 `delete_task` 的级联之前
        先回来看这里。**
        """
        stmt = (
            select(func.count())
            .select_from(Subtask)
            .join(Task, Task.id == Subtask.task_id)
            .where(
                Subtask.step_code == step_code,
                Task.task_catalog_code == task_catalog_code,
                Subtask.status == SubtaskStatus.RUNNING.value,
                Subtask.is_deleted == 0,
                Task.is_deleted == 0,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def count_unfinished_by_task(
        self, tenant_id: str, task_id: str
    ) -> Tuple[int, int]:
        """数任务下**还没结束**的子任务：回 `(在跑数, 没开始数)`（`task.delete` 的前置判据，§4.3）。

        ⚠️ **不分运行**（不按 `task_attempt_id` 筛）—— 这一处**故意**破了本模块「只认当前运行」
        的总口径，理由在**删除的粒度**：`remove_task` 清的是**整个 `task_id`** 在运行方侧的
        残留（§4.1 ②），所以**旧运行遗留的那条 `running` 一样算「可能有活还在跑」** ——
        它在被超时巡检收口之前一直算数（`list_timed_out_for_patrol` 也不按运行筛）。

        两个数怎么用、为什么要分开看，见 `TaskService._require_deletable`；本方法只负责数。
        `subtasks` 自带 `task_id` / `tenant_id` 列，不需要 join。
        """
        stmt = (
            select(Subtask.status, func.count())
            .select_from(Subtask)
            .where(
                Subtask.task_id == task_id,
                Subtask.tenant_id == tenant_id,
                Subtask.is_deleted == 0,
                Subtask.status.in_(
                    [SubtaskStatus.RUNNING.value, SubtaskStatus.PENDING.value]
                ),
            )
            .group_by(Subtask.status)
        )
        result = await self.session.execute(stmt)
        counts = dict(result.all())
        return (
            counts.get(SubtaskStatus.RUNNING.value, 0),
            counts.get(SubtaskStatus.PENDING.value, 0),
        )

    async def list_timed_out_for_patrol(
        self, now: datetime | None = None, *, limit: int | None = None
    ) -> list[Subtask]:
        """扫「跑着且已超时」的子任务（跨租户，后台循环用，§7.4）。

        超时判定读 `subtasks.started_at` + `subtasks.timeout_seconds`（子任务诞生时
        从编排固化到这一列），不写死在代码里。

        **让路（§7.5 ③）**：这条子任务还挂着「**在它的超时点之前**送达、且尚未消化」
        的上报时，这一轮先不判它超时。上报进了队列就在排队等消化，什么时候轮到不封顶；
        巡检要是抢先把它判成 `timeout`（终态），那条 `success` 到了也只能丢（闸③ 终态
        最先拦）—— 一步真跑成了却被记成超时。

        让路是**自解的**：队列把那批处理完之后（`is_deleted` 置 1 —— **正常消化和判死信
        都会置**，后者见 `SubtaskReportRepository.mark_dead_lettered`），下一轮巡检就正常
        判了，不会永久卡住。**晚于超时点才到的上报不让路** —— 那会儿子任务确实已经超时，
        该判就判。

        `reported_at` 是**管理方收到上报**的时刻，跟 `started_at` 用的是同一个钟
        （都是管理方本机的 `datetime.now()`），所以能直接比。

        **只扫任务没被软删的行** —— 跟 `list_stuck_pending` 走 `_runnable_pending_stmt()`
        时的两道过滤不同口径：那一道要**当前运行**（派活只认当前运行的行），这一道
        **不按运行筛**（`task_service.handle_timeout` 里也照着复核）。

        不筛运行的理由是**旧运行遗留的 `running` 必须能被收掉**：它照样占着
        `count_running_by_catalog_step` 数出来的并发名额（那个统计只认 `step_code`、
        目录码与 `running`，**不分运行**），而除了这一趟它再没有别的出路 ——
        派活只捞当前运行的 `pending`、防僵尸只捞 `pending`、控制台只列当前运行。
        典型来源：任务被「停止」时那个杀不掉的 `running`（§4.4），用户随后 `retry_task`
        换了运行，旧那行就永远挂在 `running`。早先这里是**按当前运行筛**的，理由写的
        是「旧运行只是改一行没人再看的历史数据」—— 那句话漏掉了并发名额这一层，
        它并不是"没人再看"。

        收口范围由 `handle_timeout` 把关：旧运行的行**只收它自己那一行**，`_aggregate`
        开头的闸让这次收口碰不到任务快照。

        ⚠️ 这里**故意不看 `Task.status`**：被「停止」的任务已经收成 `failed`（非活跃态），
        但它那个正在跑的子任务**杀不掉**（§4.4：运行方没有停止能力），只能靠这一趟兜底 ——
        加一道「任务得活跃」的过滤，它就会永远挂在 `running`。

        `Task.is_deleted == 0` 这道留下（任务被真删掉时，它的子任务也一并置了假删，
        本来就数不进并发名额，不必再判）。

        **默认不设上限**（`limit=None`）—— 跟同族的 `list_stuck_pending` 一个口径。
        设计文档点「单轮上限（10 条）」时只点了 ⓪ 触发抢占 / ① 派活 / ② 消化上报
        **三趟**，巡检不在那三条里；而且判死一条就离开候选集，卡 `BATCH` 的话积压 N 条
        要 `ceil(N / BATCH)` 轮 × `interval` 秒才消化完、**且没有上限**（运行方整体重启
        会让几十上百条一起堆进来）。这一趟**完全不出站**，比派活便宜得多。
        **保留 `limit` 参数**是给单测和将来可能的限流留口子，生产调用不传。
        """
        now = now or datetime.now()
        deadline = Subtask.started_at + func.make_interval(
            0, 0, 0, 0, 0, 0, Subtask.timeout_seconds
        )
        report = aliased(SubtaskReport)
        pending_report = (
            select(literal(1))
            .select_from(report)
            .where(
                report.tenant_id == Subtask.tenant_id,
                report.space_id == Subtask.space_id,
                report.execution_id == Subtask.execution_id,
                report.is_deleted == 0,
                report.reported_at <= deadline,
            )
            .exists()
        )
        stmt = (
            select(Subtask)
            .join(Task, Task.id == Subtask.task_id)
            .where(
                Subtask.status == SubtaskStatus.RUNNING.value,
                Subtask.started_at.isnot(None),
                Subtask.is_deleted == 0,
                Task.is_deleted == 0,
                deadline <= now,
                ~pending_report,
            )
            .order_by(Subtask.started_at)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def list_by_attempt_run(
        self,
        tenant_id: str,
        task_id: str,
        task_attempt_id: str,
        *,
        refresh: bool = False,
    ) -> list[Subtask]:
        """取某次运行下全部子任务：汇总任务终态时用（§7.2 收尾判定）。

        `refresh=True` 强制绕过会话身份映射（`populate_existing`）。收口时必须开：
        `_load_context` 早先已把同一批子任务装进过这个会话，普通 `select` 只会把当时
        那份**旧对象**还回来——并发的兄弟事务即使已经提交了终态，这里也照样读成
        `running`，收口会被误判成「还没收敛」而丢掉。
        """
        stmt = (
            select(Subtask)
            .where(
                Subtask.tenant_id == tenant_id,
                Subtask.task_id == task_id,
                Subtask.task_attempt_id == task_attempt_id,
                Subtask.is_deleted == 0,
            )
            .order_by(Subtask.sort, Subtask.node_id)
        )
        if refresh:
            stmt = stmt.execution_options(populate_existing=True)
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def mark_deleted_by_task(self, tenant_id: str, task_id: str) -> int:
        """把整个任务下的子任务一次置假删（`task.delete` 用，§4.3）。

        「任务删了，子任务必定也全删」——这一条是删除一定要做到的，所以用一条
        批量 UPDATE 覆盖任务下**所有运行**的子任务，而不是逐个改。返回改动行数。
        """
        stmt = (
            update(Subtask)
            .where(
                Subtask.tenant_id == tenant_id,
                Subtask.task_id == task_id,
                Subtask.is_deleted == 0,
            )
            .values(
                is_deleted=1,
                # **一并推版本号**：让任何「拿假删前那个版本号」的 CAS 都落空。主防御是
                # `transition_cas` 条件里的 `is_deleted == 0`，这一条防的是别的还没带上
                # 那道闸的 CAS 路径（不改版本号的话，假删对乐观锁是完全隐形的）。
                state_version=Subtask.state_version + 1,
                updated_at=datetime.now(),
            )
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount


class SubtaskAttemptRepository(BaseRepository[SubtaskAttempt]):
    """子任务尝试历史仓储：每次尝试一条，只追加不覆盖。"""

    model = SubtaskAttempt

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def create_attempts(self, rows: list[dict]) -> list[SubtaskAttempt]:
        """一次运行诞生时批量插历史行（每条都是 `pending` 排队态，§7.2）。

        子任务**一诞生**就先插一条 `attempt=1`，是为了让「它什么时候进的队列」
        从那一刻起就有据可查——派活排队排序（§6.3）用的正是这一列的 `created_at`。
        """
        objects = [SubtaskAttempt(**row) for row in rows]
        self.session.add_all(objects)
        await self.session.flush()
        return objects

    async def next_attempt_no(self, subtask_id: str) -> int:
        stmt = select(func.max(SubtaskAttempt.attempt)).where(
            SubtaskAttempt.subtask_id == subtask_id
        )
        result = await self.session.execute(stmt)
        return (result.scalar_one_or_none() or 0) + 1

    async def set_status_cas(
        self,
        tenant_id: str,
        subtask_id: str,
        attempt: int,
        *,
        expect_statuses: list[str] | tuple[str, ...],
        next_status: str,
        **values,
    ) -> bool:
        """按「第几次尝试」推进那条历史行的状态（§7.2 两侧状态要同步）。

        `subtasks` 行是「最新一次尝试」的现状、`subtask_attempts` 是每次尝试的轨迹；
        同一次尝试（`subtasks.current_attempt` = `subtask_attempts.attempt`）的状态变更
        ——派活置 `running`、结束置 `success` / `failed` / `timeout`（一期没有 `skipped`）
        ——都经这里，条件对不上就命中 0 行。

        派活置 `running` 时随 `values` 一起带上 `execution_id` 与 `started_at`，
        且 `started_at` 要与 `subtasks.started_at` **传同一个值**；结束置终态时同理
        带上与 `subtasks.finished_at` 同值的 `finished_at`（§7.2）。
        """
        stmt = (
            update(SubtaskAttempt)
            .where(
                SubtaskAttempt.tenant_id == tenant_id,
                SubtaskAttempt.subtask_id == subtask_id,
                SubtaskAttempt.attempt == attempt,
                SubtaskAttempt.status.in_(list(expect_statuses)),
            )
            .values(status=next_status, **values)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount > 0

    async def list_by_subtask(
        self, tenant_id: str, subtask_id: str, offset: int = 0, limit: int = 20
    ) -> list[SubtaskAttempt]:
        """列某子任务的尝试历史（每次尝试一条，只追加；查历史用它）。

        `limit` 由调用方按 `page_size` 给足——**这里不设隐含上限**（§4.0 分页约定）。
        """
        stmt = (
            select(SubtaskAttempt)
            .where(
                SubtaskAttempt.tenant_id == tenant_id,
                SubtaskAttempt.subtask_id == subtask_id,
            )
            .order_by(SubtaskAttempt.attempt.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def count_by_subtask(self, tenant_id: str, subtask_id: str) -> int:
        """配套 `list_by_subtask` 的总数（§4.0：返回里一定有 `total`）。"""
        return await self.count(
            tenant_id=tenant_id,
            extra_conditions=[SubtaskAttempt.subtask_id == subtask_id],
        )


__all__ = [
    "SubtaskAttemptRepository",
    "SubtaskRepository",
    "TaskAttemptRepository",
    "TaskRepository",
]
