#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""上报收件箱仓储：入队 / 扫未消化 / 标记已消化（§4.2 ①、§7.5）。

设计出处：`docs/superpowers/specs/2026-09-09-task-center-design.md` §4.2 ①、§7.5。

跟 `execution_repository.py` 一样分两类查询，别混：

- **入队**（请求线程走）：带 `tenant_id`，同步阶段校验通过就落一行；
- **扫描**（后台循环走）：**故意不带租户条件** —— 后台循环没有租户上下文、要一次扫
  全租户。扫出来的每行自带 `tenant_id`，后续动作按行内的租户显式走
  （跟 `list_pending_for_dispatch` / `list_timed_out_for_patrol` 是一套口径）。

**「未消化」就是 `is_deleted = 0`**：本表没有 `state` 列，也没清理任务 ——
处理过的记录永远留着（`is_deleted=1`），供事后对账「管理方到底收到没」。
`is_deleted=1` 里还分两种（正常消化 / 判死信），由 `dead_lettered` 区分，
**死信同样 `is_deleted=1`**（出队，否则超时巡检会永远给它让路）。
"""

from datetime import datetime
from typing import Any, Dict

from sqlalchemy import literal, select, update

from jonex_core.common.repository import BaseRepository

from ..models.report import SubtaskReport


class SubtaskReportRepository(BaseRepository[SubtaskReport]):
    """上报收件箱仓储。"""

    model = SubtaskReport

    async def enqueue(
        self,
        *,
        tenant_id: str,
        space_id: str,
        execution_id: str,
        task_id: str,
        status: str,
        outputs: dict | None = None,
        error_message: str | None = None,
        error_code: str | None = None,
        error_params: dict | None = None,
        ts: int | None = None,
        output_refs: list | None = None,
        input_refs: list | None = None,
        reported_at: datetime | None = None,
    ) -> SubtaskReport:
        """收下一条上报，落一行「未消化」。

        调用方（`report_status`）**已经把校验做完了**（报文级 + 身份一致性），所以这里
        不再判 `status` 的取值 —— 传进来的 `status` 必然是 `success` / `failed` 之一。

        `task_id` 是报文里那条任务 id，**只是个随行记录**：定位靠 `tenant_id + space_id
        + execution_id`，它不参与任何查询（本表也没为它建索引），用途见模型上那一列。

        `reported_at` 显式传一个时刻是为了让调用方**一次算好**（跟 `started_at`
        两边同一个值的口径一致）；不传就取当前时刻。
        """
        return await self.create(
            tenant_id=tenant_id,
            space_id=space_id,
            execution_id=execution_id,
            task_id=task_id,
            status=status,
            outputs=outputs,
            error_message=error_message,
            # 失败原因的代号 + 参数（2026-10-10 加）：**收件箱也得存** —— 消化是另一趟
            # （后台循环），那一刻拿不到原始报文；不存就退化成「只有渲染好的文本」。
            error_code=error_code,
            error_params=error_params,
            ts=ts,
            output_refs=output_refs,
            # 输入引用同理（2026-10-16 加）：跟着报文走，消化那一趟只有收件箱这一份。
            input_refs=input_refs,
            reported_at=reported_at or datetime.now(),
            attempts=0,
            dead_lettered=0,
            is_deleted=0,
        )

    async def list_pending(self, limit: int = 10) -> list[SubtaskReport]:
        """扫还没消化的上报（跨租户，后台循环用，§7.5）。

        按 `reported_at` 升序 = **先收到的先消化**（FIFO）。这也让「早于超时点到达
        的上报」优先被处理掉，减少超时巡检让路的次数。

        ⚠️ **条件只有 `is_deleted = 0` 一个，别加 `dead_lettered = 0`**：判死信那一步
        是把两个标志一起写、**当场出队**的，所以死信根本不会留在候选集里。加上去看似
        更保险，实则是在假设「死信还挂在队列里」—— 一旦有人照着这个假设去改判死信那边
        （只置 `dead_lettered`、不置 `is_deleted`），超时巡检的让路判据就会永久命中，
        那条子任务再也判不了超时。
        """
        stmt = (
            select(SubtaskReport)
            .where(SubtaskReport.is_deleted == 0)
            .order_by(SubtaskReport.reported_at)
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def has_pending_before(
        self,
        tenant_id: str,
        space_id: str,
        execution_id: str,
        *,
        received_before: datetime,
    ) -> bool:
        """这次执行还挂着「在 `received_before` 之前送达、**尚未消化**」的上报吗？（§7.5 ③ 让路）

        **这是超时巡检那条「让路」判据的单行复核版**，条件与
        `SubtaskRepository.list_timed_out_for_patrol` 里那个 EXISTS **逐条同口径**：

        ```
        tenant_id + space_id + execution_id + is_deleted = 0 + reported_at <= 超时点
        ```

        ⚠️ **两处必须一起改**，`is_deleted = 0` 这一条尤其别动 —— 「未消化 ⟺
        `is_deleted = 0`」是让路能**自解**的全部依据（`mark_consumed` 与
        `mark_dead_lettered` 都会置 1，见本模块 docstring）。

        ── 为什么扫描侧筛过一遍、这里还要再筛 ──

        扫描（`list_timed_out_for_patrol`）到真正动手之间隔着取锁、重读上下文，队列
        随时可能又收到一条上报 —— 也可能是原来那条刚被消化、只剩新来的这条。不补这一次
        复核，就会把一条**还在排队等消化的 `success`** 判成 `timeout`（终态），那条上报
        到了也只能丢（闸③ 终态最先拦）：一步真跑成了、却被记成超时。

        **命中就这一轮不判**，返回 `False` 走人 —— 让路是自解的：队列把那条处理掉
        （正常消化或判死信）就置 `is_deleted = 1`，下一轮巡检正常判，不会永久卡住。
        """
        stmt = (
            select(literal(1))
            .select_from(SubtaskReport)
            .where(
                SubtaskReport.tenant_id == tenant_id,
                SubtaskReport.space_id == space_id,
                SubtaskReport.execution_id == execution_id,
                SubtaskReport.is_deleted == 0,
                SubtaskReport.reported_at <= received_before,
            )
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def get_any_by_id(self, report_id: str) -> SubtaskReport | None:
        """按主键取一行，**不带租户条件**（后台循环用）。

        跟 `SubtaskRepository.get_any_by_id` 一个口径：后台循环扫出来的只是一批 id，
        真正动手时每个 id 单独开会话重新取行 —— 这时没有租户上下文，所以按主键取，
        行自带 `tenant_id`，后续动作按行内的租户显式走。

        **已消化的行照样取得到**（不像 `subtasks` 那样按「找不到」处理）：后台循环要
        拿它复核「还是不是未消化」，被别的副本抢先消化掉时据此跳过。
        """
        stmt = select(SubtaskReport).where(SubtaskReport.id == report_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def mark_consumed(
        self,
        tenant_id: str,
        report_id: str,
        *,
        at: datetime | None = None,
    ) -> bool:
        """把一条上报标成「已消化」（`is_deleted=1` + 记 `processed_at`）。

        带 `is_deleted = 0` 条件 = **CAS**：两个副本同时消化同一条时只有一个命中，
        另一个 0 行。跟状态推进走 `transition_cas` 是同一个理由（§7.2）。

        **调用方要在「处理这条上报」的同一个事务里调它** —— 两件事必须一起成或一起不成，
        否则会出现「状态变了但上报还挂着」（下轮重复消化）或「上报没了但状态没变」（丢了）。
        """
        stmt = (
            update(SubtaskReport)
            .where(
                SubtaskReport.id == report_id,
                SubtaskReport.tenant_id == tenant_id,
                SubtaskReport.is_deleted == 0,
            )
            .values(is_deleted=1, processed_at=at or datetime.now())
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount > 0

    async def record_failure(
        self,
        tenant_id: str,
        report_id: str,
        error: str | None,
        *,
        bump_attempts: bool = True,
    ) -> bool:
        """记一次消化失败：`attempts + 1` + 写 `last_error`，**不动 `is_deleted`**。

        消化失败（DB 挂了之类）时这条**留在未消化**、下一轮再来 —— 这正是「上报只要
        被受理就一定处理到底」的兜底。这是纯观测，不参与任何裁决。

        **`bump_attempts=False` 是「基础设施故障」那一档**：Redis / DB 连不上**不是
        这条上报的错**，算进 `attempts` 等于把「Redis 抖十几秒」变成「当时队列里的
        上报一起被判死信、永久出队」。那一档只留 `last_error` 供排障、**不计数**，
        下轮照常再试 —— Redis 一恢复就处理完了。判据见
        `scheduler_service._record_report_failure`。

        **要单独开一个会话调**：失败时原来那个事务已经坏了（回滚/连接不可用），
        在它里面写必然再失败一次。

        `last_error` **不截断**：写进来的是 `f"{异常类名}: {异常消息}"`，本来就不含
        traceback，完整存下来才看得出「卡在哪」（列类型是 `TEXT`）。

        **本方法恒不判死信**：达阈值那一步走 `mark_dead_lettered`（它要动 `is_deleted`）。
        这里保持「绝不动 `is_deleted`」这一条，两种归宿分得清清楚楚。
        """
        values: Dict[str, Any] = {"last_error": (error or "") or None}
        if bump_attempts:
            values["attempts"] = SubtaskReport.attempts + 1
        stmt = (
            update(SubtaskReport)
            .where(
                SubtaskReport.id == report_id,
                SubtaskReport.tenant_id == tenant_id,
                SubtaskReport.is_deleted == 0,
            )
            .values(**values)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount > 0

    async def mark_dead_lettered(
        self,
        tenant_id: str,
        report_id: str,
        error: str | None,
        *,
        at: datetime | None = None,
    ) -> bool:
        """判死信：**放弃重试并出队**，一条 UPDATE 把五件事一起写掉。

        ```
        attempts + 1 / last_error / dead_lettered=1 / is_deleted=1 / processed_at
        ```

        **为什么 `is_deleted` 也要一起置**（这是本方法唯一容易改错的地方）：超时巡检的
        「让路」判据（`list_timed_out_for_patrol` 里那个 EXISTS）只看 `is_deleted = 0`
        —— 而死信的上报**不可能再被消化**，留在队列里就是让那条子任务**永远**被让路、
        永远判不了超时（`running` 收不掉、并发名额不回）。出队之后「未消化 ⟺
        `is_deleted = 0`」这条等式原样成立，队列那一侧（`list_pending` / 部分索引 /
        `mark_consumed` 的 CAS）一个字都不用改。

        **死信不是删掉**：这一行永远留着，「管理方到底收到没」照样查得到，还多了
        `dead_lettered` / `attempts` / `last_error` 说清「收到了、但消化不掉、卡在哪」。

        **CAS 是那四个条件**（在 `record_failure` 三条的基础上加 `dead_lettered = 0`）：
        两个副本同时给同一条判死时只有一个命中，不会把 `attempts` 多加一次。
        并发下 `attempts` 本来就可能差一两次（递增在 SQL 里做、阈值判断在 Python 侧），
        这只影响「早一轮还是晚一轮判死」，不影响收口。

        **要单独开一个会话调**：跟 `record_failure` 同一个理由 —— 这时原来那个事务
        已经坏了。

        `last_error` 的口径与 `record_failure` 一致：不截断，存 `f"{异常类名}: {异常消息}"`。
        """
        stmt = (
            update(SubtaskReport)
            .where(
                SubtaskReport.id == report_id,
                SubtaskReport.tenant_id == tenant_id,
                SubtaskReport.is_deleted == 0,
                SubtaskReport.dead_lettered == 0,
            )
            .values(
                attempts=SubtaskReport.attempts + 1,
                last_error=(error or "") or None,
                dead_lettered=1,
                is_deleted=1,
                processed_at=at or datetime.now(),
            )
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount > 0


__all__ = ["SubtaskReportRepository"]
