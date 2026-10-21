#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""后台循环：扫到点的定时触发器并起任务 + 扫能跑的子任务派活 + 消化上报 + 拉式对账 + 超时巡检 + 补收口（§7.4、§7.5）。

设计出处：`docs/superpowers/specs/2026-09-09-task-center-design.md` §7.4、§7.5、§十第 7 步。

每轮干**六件事**（§7.5，顺序固定，见 `run_once`）：

- **⓪ 触发抢占**：扫「到点了、还开着」的 `cron` 触发器 → CAS 抢占 → `trig.fire`；
- **① 派活**：扫还能跑的任务（`tasks.status` 是 `pending` / `running`）→ 看它下面能跑的子
  任务 → 抢占 → 派活。**没够着运行方时在出站那一段里同步重发**（§4.1 ①，最多 3 次，
  `_dispatch_with_retry`）。**同一趟里还带上「防僵尸」**（就绪的 `pending` 卡太久 → 置
  `timeout`，§7.5 D2b）——它跟派活看的是同一批候选，只是「派不出去」和「卡太久」两种归宿；
- **② 消化上报**：扫「运行方报了、还没消化」的上报（`subtask_reports`）→ 逐条在任务锁内
  推进子任务状态。**排在超时巡检之前**：先消化完，巡检要「让路」的情况就少一些（§7.5 ③）。
  这一趟是「上报只要被受理就一定处理到底」的落点 —— 「处理到底」的兜底出口是**判死信**
  （连续失败达 `TASK_CENTER_REPORT_MAX_ATTEMPTS` 就出队 + ERROR 告警），见
  `_record_report_failure`。那里还先分一档：**Redis / DB 不可用这类基础设施故障不计数**
  （不是这条上报的错，Redis 一恢复照常处理完），只有「这条上报本身消化不了」才够得着死信。
  ⚠️ **「不计数」那一档另有一条绝对上限**（`TASK_CENTER_REPORT_INFRA_MAX_HOURS`，默认
  24 小时：躺过就一律判死信出队）—— 少了它那一档会变成死锁：不计数 ⇒ 永远留在收件箱
  ⇒ 超时巡检**永久让路** ⇒ 那条子任务再也判不了超时（详见 `_record_report_failure`）；
- **③ 拉式对账**（2026-10-11 加，`_query_pass`）：主动去问运行方「这一步现在什么情况」
  （§4.1 ④ `query_step_status`），把问到终态的结果**当一条上报塞进收件箱**、走 ② 的消化
  路径 —— 专兜「运行方既没回执也没上报」的那些**沉默**子任务；日常仍靠 ② 的推式上报，
  这一趟只是兜底（问到 `unknown` 时**不改状态**，交给超时巡检兜）。⚠️ **这一趟是 2026-10-11
  新加的，下面前四条 ⓪①② 之外的编号据此重排** —— 设计文档 §7.5 的圈号尚未同步（那边仍是
  「③ 超时巡检 / ④ 补收口」），以本文件的 `run_once` 顺序为准；
- **④ 超时巡检**：跑着超过 `timeout_seconds` 的子任务置 `timeout` 收口；
- **⑤ 补收口**（2026-09-29 加）：把「当次运行早跑完、任务快照却没收到口」的任务补成终态
  （`_settle_pass`）—— 那一格从前只能人工改库。

三件要看清的事：

**一、不选主。** 每个副本都跑自己的循环，扫描本身没有互斥，多个副本必然扫到同一批活。
互斥分三层落下来（§7.2 ②）：**先 Redis 任务锁**（`task_center:lock:task:{task_id}`，
同一条任务同一时刻只有一个副本在动）→ **再 CAS** —— 触发抢占靠 `next_fire_at` 的 CAS
（§7.5 ⓪）、派活靠 `claim_pending` 的 CAS（§7.5 ①）、判超时靠 `running → timeout` 的
CAS（§7.5 ③）。**锁和 CAS 任一层拦住，都算本轮跳过**：锁没抢到就跳过这条看下一条
（扫描类调用的固定处置），CAS 命中 0 行同理。任何一个副本挂了都不影响其余副本。

> **锁的边界写死**（§7.2「锁不跨出站调用」）：**没出站的动作**（防僵尸、`_timeout_one`
> 单条判死）拿锁盖住整段「读—改—写」就完事；**要出站的**（派活、以及 ③ 超时巡检判死前
> 那一问）一律把出站排在**拿锁之前** —— 调运行方那一段**不持锁、不占事务**
> （见 `_dispatch_one` / `_patrol_pass`）。

**二、扫描和动手分两个事务。** 扫描只取一批 id（跨租户，扫完就结束），
每个 id 再单开一个会话重新取行、单独提交。这样一个子任务出问题只回滚它自己，
不会把同一轮的其它活一起拖下水——扫描里读到的 ORM 行也不会因为别处的回滚而被过期掉。

**三、触发抢占跟 `trig.fire` 也必须分成两个事务。** 抢占那一步要**先提交**：抢占成功时
`next_fire_at` 已经推到下一次，所以即使紧接着的 `trig.fire` 失败，也**不会同一时刻反复重
触发**（漏了就等下一个点，§7.5 ⓪ 的 P3）。两者放进同一个事务的话，`fire` 一抛异常就把
抢占一起回滚，下一轮又扫到同一个点 —— 跟这条保证正好相反。

**每轮上限**：`TASK_CENTER_SCHEDULER_BATCH`（默认 10）。**⓪ ① ② 各自用这个上限、各算各的
10 条**（不是几边加起来 10 条）。

**不受它约束的有三处**，各有各的道理：

- ① 里的**防僵尸**（`list_stuck_pending`）：它是「派活一路失败、状态又退不回去」那条死路
  的兜底，卡上限等于把兜底砍掉（§7.5 D2b、附录 A.4）；
- ③ **超时巡检**（`list_timed_out_for_patrol`）：同族的兜底判死，卡上限只会让积压的
  `running` 排队等判死、排多久没有上限。（⚠️ 它 2026-10-16 起**判死前会先出站问一次**
  运行方，见 `_patrol_pass`；但一次只问一条、问到终态就收口，量级仍与派活不可同日而语。）
- ① 的**扫描面**：放宽到 `BATCH × TASK_CENTER_SCHEDULER_SCAN_FACTOR`（默认 5）。
  这是**「派活受 BATCH 限制」的正确读法** —— `BATCH` 卡的是**一轮真派出去几条**，
  不是「一轮扫几条候选」。被 `max_concurrency` 挡下的候选一个字都不写库、**排序也不动**，
  下一轮还在原位；扫描面若就等于 `BATCH`，队头那几条一旦全卡在同一个瓶颈上，**每轮都
  被它们吃满名额**，后面的活连窗口都进不去（详见 `_dispatch_pass`）。**排序语义没动。**
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional

from redis.exceptions import RedisError
from sqlalchemy.exc import DisconnectionError, InterfaceError, OperationalError

from jonex_core.common.config import get_config
from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import (
    InvalidParameterError,
    JonexException,
    ResourceNotFoundError,
)
from jonex_core.task_protocol.models import ExecutionDispatch
from jonex_core.task_protocol.results import (
    TASK_CENTER_RETRYABLE_CODES,
    DispatchResult,
)

from ..flows.runner_adapter import RunnerAdapter, call_with_retry
from ..models import MisfirePolicy
from jonex_core.task_protocol.models import (
    STEP_STATUS_PENDING_DISPATCH,
    STEP_STATUS_RUNNING,
    STEP_STATUS_SUCCEEDED,
    STEP_STATUS_UNKNOWN,
    ExecutionEnvelope,
    check_error_reason,
    check_output_refs,
)
from ..repository.execution_repository import SubtaskRepository, TaskRepository
from ..repository.report_repository import SubtaskReportRepository
from ..repository.trigger_repository import TaskTriggerRepository
from .cron import CronExprError, compute_next_fire_at
from .task_lock import TaskLockBusy, task_lock
from .task_service import TaskService
from .task_trigger_service import TaskTriggerService

logger = logging.getLogger(__name__)


async def _dispatch_with_retry(
    adapter: Any,
    capability_id: str,
    dispatch: ExecutionDispatch,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> DispatchResult:
    """③ 段：出站派活 + **传送层重发**（§4.1 ①）。

    **重发循环本体不在这儿** —— 在 `call_with_retry`（`flows/runner_adapter.py`）：重发
    范围（只有「没够着运行方」）、预算（3 次 / 退避 **2s、5s**，见
    `OUTBOUND_RETRY_BACKOFF_SEC`）、三档判决口径，**与删除方向（§4.1 ②）共用同一份**。
    这里只做两件事：把「派活」这个动作绑成零参调用；给日志加一个认得出是哪条子任务的前缀。

    **用完预算之后怎么处置，由调用方定**：③ 段把最后一次的 `DELIVERY_UNKNOWN` 交回
    `_finalize_one` → `TaskService.finalize_dispatch`，那里**按成因分两档**：

    - **`dead_after_retry` 那一类 → 判死**（两种成因：网关连撞 5xx —— 这一侧压根没通、
      且 5xx 里混着「能力没配端点」这类装配错；运行方自己回了失败码 —— 请求到了、
      对方明确说它失败了）；
    - **其余未知 → 不判死**（子任务停在 `running`，由超时巡检兜底，§7.4）—— 运行方可能
      已经在跑了，判死会杀掉一个正在跑的活。

    判据是 `DispatchResult.dead_after_retry`（适配器在两支上标：5xx 那一支、以及
    「运行方自己回了失败码」那一支），**本函数不参与判定**。

    ── 为什么是模块级函数，不是 `TaskScheduler` 的方法 ──

    `TaskScheduler.__init__` 把 `RunnerAdapter()` 写死在里面（适配器在循环生命周期里只有
    一份、连接池复用），**没有注入口** —— 写成方法就没法注入假适配器单测。做成模块级函数
    后 `adapter` 与 `sleep` 都是入参，重发次数与退避序列可以零 IO 钉死。
    """
    return await call_with_retry(
        lambda: adapter.dispatch(capability_id, dispatch),
        what=f"派活 execution={dispatch.execution_id} capability={capability_id}",
        sleep=sleep,
        # 采集上下文：`request_id` 用 `execution_id` —— 它正是这次重发循环的幂等键
        # （重发的就是同一个 `execution_id`，见 `call_with_retry` 的 docstring），
        # 也是运行方那边能认出「这是重派」的那个 id。租户从报文里取（它必然合法：
        # 派活报文在更早的步骤里已经过 `require_tenant`）。`target` 记这次派给谁 ——
        # N1 的「目标对象」列就是它。
        request_id=dispatch.execution_id,
        tenant_id=dispatch.tenant_id,
        target=capability_id,
    )


#: 「**不是这条上报的错**」的异常：Redis / 数据库 / 网络这些基础设施不可用。
#:
#: 它们与「这条上报本身消化不了」（报文触发了约束冲突、`_absorb` 里的代码 bug）必须分开 ——
#: 前者抗一阵子就自己好了，后者每轮必然重现。分开的后果只有一个，但很大：
#: **基础设施故障期间不能把队列里的上报一批批判死信**（Redis 抖 15 秒 ≈ 5 轮，正好够把
#: 当时队列里每一条都算到上限、永久出队）。见 `_record_report_failure`。
#:
#: `CacheError` 不在这个元组里，它走上面的 `TASK_CENTER_RETRYABLE_CODES` 分支
#: （`task_lock._try_acquire` 已经把裸的 `RedisError` 翻译成了它）。
#:
#: ⚠️ **刻意不收 `SQLAlchemyError` / `DBAPIError` 这两个基类**：`IntegrityError` 也是它们
#: 的子类，而「插入撞约束」正是「这条上报本身处理不了」的典型 —— 收进来会让它永远不判死信、
#: 永远占着队列名额。只认连接层面那几个。
_INFRA_FAILURES: tuple = (
    OperationalError,  # 连不上 / 连接被服务端掐断
    InterfaceError,  # 连接本身不可用（不是 SQL 的问题）
    DisconnectionError,  # 连接池里那条已经死了
    RedisError,
    ConnectionError,
    TimeoutError,  # 内置 TimeoutError（Py3.11 起 asyncio.TimeoutError 就是它）
    OSError,
)


def _is_infra_failure(exc: BaseException) -> bool:
    """这个异常是「基础设施不可用」还是「这条上报消化不了」？（见 `_INFRA_FAILURES`）

    两条判据，命中任一条即算基础设施故障：

    1. 异常本身是 `_INFRA_FAILURES` 里那几种（裸的 Redis / DB 连接异常）；
    2. 是 `JonexException` 且 `code` 落在 `TASK_CENTER_RETRYABLE_CODES` —— 平台内部各层
       把基础设施故障**翻译过**之后的样子（如 `task_lock` 把 Redis 故障译成 `CacheError`）。
       复用协议层那张表是有意的：它表达的就是「这个错重发是对的」，与「别算这条上报的账」
       是同一件事。

    **判不出基础设施故障的一律按「这条上报的错」算**（保守方向选对了）：漏判的代价是
    一条瞬时故障多记一次失败（还有下次机会），错判的代价是一条真·坏上报永远占着队列名额。
    """
    if isinstance(exc, _INFRA_FAILURES):
        return True
    if isinstance(exc, JonexException):
        return exc.code in TASK_CENTER_RETRYABLE_CODES
    return False


def _hours_since(moment: Optional[datetime]) -> Optional[float]:
    """`moment` 距现在多少小时；取不到时刻就返回 `None`。

    `None` 的语义是「**算不出它等了多久**」（`reported_at` 理论上不会空 —— 落库时
    必然写值，列也是 `NOT NULL`），调用方按「**没超龄**」处置：拿不准就别判死，
    下一轮还会再看到它。
    """
    if moment is None:
        return None
    return (datetime.now() - moment).total_seconds() / 3600.0


class TaskScheduler:
    """任务中心后台循环：一轮一轮地扫「到点的触发器」「能跑的子任务」「跑超时的子任务」。"""

    def __init__(
        self,
        *,
        interval: Optional[float] = None,
        batch: Optional[int] = None,
        scan_factor: Optional[int] = None,
    ):
        config = get_config()
        self._interval = (
            interval if interval is not None else config.TASK_CENTER_SCHEDULER_INTERVAL
        )
        self._batch = batch if batch is not None else config.TASK_CENTER_SCHEDULER_BATCH
        # ⚠️ 这是**倍数**、不是秒数（§7.5）：阈值 = 每步自己的 `timeout_seconds` × 这个数。
        self._stuck_multiplier = config.TASK_CENTER_PENDING_STUCK_TIMEOUT
        # 一条上报连续消化失败多少次就判死信（§7.5 ②）。**没有注入口** —— 它是
        # 「兜底出口」的阈值，不是需要按环境调的性能参数；单测要压到 1 的话直接改
        # 这个属性（`scheduler._report_max_attempts = 1`）比多开一个构造参数干净。
        self._report_max_attempts = config.TASK_CENTER_REPORT_MAX_ATTEMPTS
        # 基础设施故障那一档的**绝对上限**（小时，§7.5 ②）：躺过这个时长就一律判死信
        # 出队，不管是谁的错（见 `_record_report_failure` 与那条配置的说明）。
        self._report_infra_max_hours = config.TASK_CENTER_REPORT_INFRA_MAX_HOURS
        # 派活**扫描面**的放大倍数（默认 5）：一轮扫 `BATCH × 它` 条候选，真派满 `BATCH`
        # 条就收工。为什么不能「扫几条就派几条」见 `_dispatch_pass` 的注释。
        self._scan_factor = (
            scan_factor
            if scan_factor is not None
            else config.TASK_CENTER_SCHEDULER_SCAN_FACTOR
        )
        # 适配器在整个循环生命周期里只建一份，连接池复用；停循环时一起关掉。
        self._adapter = RunnerAdapter()
        self._task: Optional[asyncio.Task] = None

    # ── 生命周期 ──────────────────────────────────────────

    async def start(self) -> None:
        """起循环（启动时立刻跑一轮，不等第一个间隔）。"""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "[TaskCenter] 后台循环已启动（间隔 %.1fs，每轮最多派 %d 条，扫描面 %d 条）",
            self._interval,
            self._batch,
            self._batch * self._scan_factor,
        )

    async def stop(self) -> None:
        """停循环并释放连接池。"""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._adapter.close()
        logger.info("[TaskCenter] 后台循环已停止")

    # ── 循环体 ────────────────────────────────────────────

    async def _loop(self) -> None:
        """循环外壳：定时跑 `run_once`。

        ⚠️ 这里这一层**只是兜底**（循环不能死）—— 真正的逐趟兜异常在 `run_once` 里。
        从前六趟是一条直线、只有这一层兜，于是「一趟炸 = 后面几趟全跳过」；现在
        `run_once` 自己把每一趟都包住了，能冒到这里的只剩循环壳本身的问题（如取配置失败）。
        """
        first_run = True
        while True:
            try:
                if not first_run:
                    await asyncio.sleep(self._interval)
                first_run = False
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 循环绝不能死，下一轮接着来
                logger.warning("[TaskCenter] 后台循环本轮出错，跳过本轮：%s", exc)

    async def run_once(self) -> None:
        """跑一轮：触发抢占 → 派活（含防僵尸）→ 消化上报 → **对账（拉）** → 超时巡检 → 补收口。
        单测直接调这个方法，不必等定时器。

        ⚠️ **每趟各兜各的异常**（2026-10-14 修）：从前这里是一条直线，任何一趟抛出异常都会
        冲出 `run_once`，而 `_loop` 只在**轮**这一层兜 —— 于是**一趟炸、同一轮后面的趟次
        一律不跑**。生产上实测到的样子：

        > 16:20:38 某一趟抛了异常，此后 **39 分钟内一次库写入都没有**
        > （`16:20:38 → 16:59:39`）：卡在 `running` 的子任务没人判死（超时巡检是第 5 趟）、
        > 上报没人消化、任务中心整体停摆 —— 而现场**只留一条 warning**，
        > 看上去像「这一轮只是空跑」。

        现在逐趟兜：一趟炸只损失它自己这一轮的工作，其余照跑。**绝不让某一趟的失败决定
        别的趟次能不能跑** —— 六趟本来就是各扫各的表，彼此之间没有「前一趟成功才有意义」
        的依赖（只有 `_query_pass` → 下一轮 `_report_pass` 那种**跨轮**的顺序约定）。

        `CancelledError` **照旧穿透**：它是「停循环」的信号，不是业务错误。
        """
        # 顺序固定、不许调：对账（拉）排在「消化上报」**之后** —— 它塞进收件箱的那些，
        # 下一轮由 `_report_pass` 消化，走的是与上报**完全相同**的一条路（§4.1 ④）。
        passes = (
            ("触发抢占", self._fire_pass),
            ("派活", self._dispatch_pass),
            ("消化上报", self._report_pass),
            ("对账（拉）", self._query_pass),
            ("超时巡检", self._patrol_pass),
            ("补收口", self._settle_pass),
        )
        for pass_name, run_pass in passes:
            try:
                await run_pass()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "[TaskCenter] 本轮「%s」这趟出错，跳过它、继续跑后面的趟次：%s",
                    pass_name,
                    exc,
                )

    # ── 触发抢占（§7.5 ⓪）─────────────────────────────────

    async def _fire_pass(self) -> None:
        """扫「到点了、还开着」的触发器，逐个抢占 + 起任务（§7.5 ⓪）。

        扫描条件就是 §7.5 ⓪ 那条 SQL（`status='active'` + `next_fire_at <= 现在`，
        按 `next_fire_at` 升序、`LIMIT :batch`）——**带 `status='active'`，所以
        `paused` 的不会被扫到**（P1 / P6）。从前谓词里还有 `trigger_type='cron'`，
        那一列已随「触发器只等于定时」删掉。

        **`list_due_cron` 是跨租户的**，扫到的每行自带 `tenant_id`，后面的动作按行内的
        租户显式走（后台循环没有租户上下文）。
        """
        now = datetime.now()
        async with get_db_session() as session:
            due = await TaskTriggerRepository(session).list_due_cron(now, self._batch)
            # 扫描事务只取"要处理哪几条"（连同算抢占条件要用的原值），取完就结束。
            targets = [
                (row.tenant_id, row.id, row.next_fire_at, row.cron_expr, row.misfire_policy)
                for row in due
            ]
        for tenant_id, trigger_id, expect_next_fire_at, cron_expr, misfire_policy in targets:
            # **逐条兜异常**（与派活 / 消化上报 / 超时巡检三趟同构）。
            # 这一趟跑在六趟的**最前面**，异常一旦冲出去，同一轮的另外五趟**一趟都不会跑**
            # （`_loop` 只按「轮」兜异常，兜完就跳过整轮）—— 一行毒触发器 + 每轮必现，
            # 任务中心就整体停摆：子任务卡在 `running`、上报积压、超时巡检不再判死。
            try:
                await self._fire_one(
                    tenant_id=tenant_id,
                    trigger_id=trigger_id,
                    expect_next_fire_at=expect_next_fire_at,
                    cron_expr=cron_expr,
                    misfire_policy=misfire_policy,
                    now=now,
                )
            except Exception as exc:
                logger.warning(
                    "[TaskCenter] 触发抢占出错 trigger=%s tenant=%s：%s",
                    trigger_id,
                    tenant_id,
                    exc,
                )

    async def _fire_one(
        self,
        *,
        tenant_id: str,
        trigger_id: str,
        expect_next_fire_at: datetime,
        cron_expr: Optional[str],
        misfire_policy: Optional[str],
        now: datetime,
    ) -> None:
        """处理一条到点的触发器：算下一次 → 抢占 → 按 `misfire_policy` 决定起不起任务。

        三步（§7.5 ⓪ 的表，第 3 步已合并进第 1 步）：

        1. **先算 `:新值`** —— 算不出来（理论上不可达，§3.3.2 ⑤）就走兜底：**记一条告警
           日志 + 把它置 `paused`**（`next_fire_at` 不动、`last_fired_at` 也不刷，因为这一轮
           根本没触发任何东西），然后跳过这条。**绝不能写 `next_fire_at = NULL`** ——
           这一列是 `NOT NULL`、会拒（从前那条按类型分流的 `ck_tc_trig_cron` 已随
           `trigger_type` 列一起删除，现在由列约束直接表达；附录 B.2 第 5 条）；
        2. **CAS 抢占**（**独立事务、先提交**）：`UPDATE ... SET next_fire_at=:新值,
           last_fired_at=:现在, state_version=state_version+1 WHERE id=:id AND
           next_fire_at=:原值 AND status='active'` —— **命中 0 行 = 别的副本刚抢走 → 本轮
           跳过这条**；
        3. **按 `misfire_policy` 决定起不起任务**：先判「真的错过了吗」（`now - 原值 >
           **这条触发器自己的一个周期**`，晚得没超过它自己的一个周期就算正常抖动、照常
           触发）；真的错过了且策略是 `skip` → **不起任务**（`next_fire_at` 已经推到下一个
           点了）；`catch_up` 或没真的错过 → 调 `trig.fire` 补跑一次（**只补一次，不按
           错过次数补**）。⚠️ **尺子是触发器自己的周期、不是调度循环的心跳** —— 理由见
           下面第 ③ 步的注释。

        **`trig.fire` 本身不拿任务锁**：新任务还没有 `id`，「同一条触发器只被触发一次」已经
        由第 2 步的 CAS 保证了（§7.5 ⓪ 第 2 步）。
        """
        # ① 先算下一次——抢占要跟它一次写入（"先算好新值再抢"是这套设计的关键）。
        try:
            next_value = compute_next_fire_at(cron_expr, after=now) if cron_expr else None
        except CronExprError as exc:
            logger.warning(
                "[TaskCenter] 触发器 %s 的 cron_expr 解析失败，跳过本轮：%s", trigger_id, exc
            )
            next_value = None
        if next_value is None:
            # 兜底（§3.3.2 ⑤ ①）：告警 + 置 paused，等人来改 cron_expr 再 resume。
            async with get_db_session() as session:
                await TaskTriggerRepository(session).pause_on_cron_failure(
                    tenant_id, trigger_id
                )
            logger.warning(
                "[TaskCenter] 触发器 %s 算不出下一次触发时刻（cron_expr=%s），已置 paused",
                trigger_id,
                cron_expr,
            )
            return

        # ② CAS 抢占：推 next_fire_at + 记 last_fired_at + 版本号 +1，一次写。
        #    **单独事务、先提交** —— 抢到就把点推走了，后面 fire 失败也不会同时刻重触发。
        async with get_db_session() as session:
            claimed = await TaskTriggerRepository(session).claim_fire(
                tenant_id,
                trigger_id,
                expect_next_fire_at=expect_next_fire_at,
                next_fire_at=next_value,
                fired_at=now,
            )
        if not claimed:
            return  # 别的副本刚抢走，本轮跳过这条

        # ③ 正常抖动不算错过：**迟到不超过它自己的一个调度周期**就照常触发（§7.5 ⓪ 的判据）。
        #
        # ⚠️ 这里量的必须是**这条触发器自己的周期**（下一次该跑的时刻 − 这一次该跑的时刻），
        # **不是调度循环的心跳**。从前用的是后者 —— 门槛成了 `TASK_CENTER_SCHEDULER_INTERVAL`
        # （默认 3 秒），于是「一圈没扫到」就够判成错过，两条路都会踩到：
        #   ① ⓪ 趟每轮最多处理 `TASK_CENTER_SCHEDULER_BATCH`（默认 10）条 —— 同时到点的
        #      触发器一挤，排在后面的要等下一轮（约 6 秒后）才被扫到 → 迟到 3 秒多 →
        #      **被判成「真错过」**；
        #   ② 循环某一轮自己跑慢几秒（那一轮同时在派活、消化上报），这一轮里到点的触发器
        #      全都中。
        # 对「每分钟」的触发器，晚 3 秒根本不算错过（离下一个点还有 57 秒）。而判成错过、
        # 策略又是 `skip` 时，只留一条 `logger.info` —— 等于**静默丢点**。
        # 换成它自己的周期之后，「每分钟」容忍 60 秒、「每小时」容忍 3600 秒：**迟到超过
        # 一整个自己的周期**，才说明后台真的漏过了一整个调度窗口。
        #
        # 尺子 = **`cron_expr` 的间隔本身**：从「**这一次该跑的时刻**」往后算一格
        # （`compute_next_fire_at` 的 `after` 传 `expect_next_fire_at`，**不是 `now`**）。
        # ⚠️ 从 `now` 算出来的那一格**总是 ≥ 迟到量**（往后找的第一格必然晚于 `now`），
        # 拿它当尺子会让判据**恒为「没错过」** —— `skip` 就成了永不跳过的空策略，
        # 正好是这段阈值本来要防的那件事。从原定时刻算，得到的才是「这个 cron 多久跑
        # 一次」：`*/5 * * * *` 恒为 300 秒，跟迟到多久无关。
        # 算不出来那种情况在 ① 已经 `return` 过（同一个表达式、同一类失败），这里只做
        # 兜底：真算不出就按「抖动」处理 —— 宁可照常跑一次，也不静默丢点。
        try:
            # ⚠️ **算出来的可能是 `None`**（`compute_next_fire_at` 的契约：语法非法抛
            # `CronExprError`、**算不出返回 `None`**）—— 直接相减会抛 `TypeError`。
            # 这一支实际到不了（从 `expect` 往后搜必然搜得到一个 ≥ 从 `now` 往后那个解），
            # 但兜底不能因此写成「接错异常类型」：只 `except CronExprError` 的话，真出现
            # `None` 就是一条 `TypeError` 冒到 `_fire_pass` 的逐条 except —— 而那时**抢占
            # CAS 已经提交、点已经推走**，这一次到点就静默不触发了。
            next_from_expect = compute_next_fire_at(cron_expr, after=expect_next_fire_at)
            own_period = (
                None
                if next_from_expect is None
                else next_from_expect - expect_next_fire_at
            )
        except CronExprError:
            own_period = None
        missed = own_period is not None and (now - expect_next_fire_at) > own_period
        if missed and misfire_policy == MisfirePolicy.SKIP.value:
            logger.info(
                "[TaskCenter] 触发器 %s 错过了 %s（策略 skip），本次不补跑，下次 %s",
                trigger_id,
                expect_next_fire_at,
                next_value,
            )
            return

        # 起任务：跟人点「立即触发」走的是同一个方法，只是 `origin` 标成 cron。
        try:
            async with get_db_session() as session:
                await TaskTriggerService(session).fire_trigger(
                    trigger_id, tenant_id, origin="cron"
                )
        except Exception as exc:
            # 抢占已经提交、点也推走了 —— 这条就认"漏一次"，等下一个点（§7.5 ⓪ 的 P3）。
            #
            # ⚠️ **但"配置/数据类"的失败不能只记一条 log 就完事**（2026-10-09 补）：
            # `fire` 里那些「定义还没发过版」「版本查不到」「入参缺必填」是**永远不会自己
            # 好**的（其中「先把触发器配好、定义随后发版」还是刻意允许的顺序）。只记日志
            # 的后果是这个触发器**每个点都空响一次、一条任务都不产** —— 而 `last_fired_at`
            # 照常刷新，页面上"上次触发时间"一直在动，用户根本看不出来（`trig.runs` 才是空的）。
            # 处置接**现成**的那套熔断（与「算不出下一次」同一处 `pause_on_cron_failure`）：
            # 置 `paused` + 记一条 ERROR，等人改好配置再 resume。
            #
            # 判据取「异常码 ∈ {1001 参数, 1002 找不到}」= 配置/数据类。
            # **其它异常按瞬态处置、不暂停** —— 暂停一个健康触发器要人来救，代价更大。
            # **刻意不做「连续 N 次才暂停」**：本表没有失败计数器列，加列 + 迁移的代价
            # 远大于收益，而按「配置类」判已经精确命中这一档、且不会误伤瞬态。
            # ⚠️ **取异常常量、不写字面量**（2026-10-13 改）：从前这里写死 `(1001, 1002)`，
            # 而模块内别处（`task_lock.py` / `results.py`）一律取 `SomeError.code` ——
            # 哪天那两个异常的码变了，这里会**静默失配**（配置类失败不再触发熔断，
            # 退化成「每个点空响一次」）。
            code = getattr(exc, "code", None)
            if code in (InvalidParameterError.code, ResourceNotFoundError.code):
                async with get_db_session() as session:
                    await TaskTriggerRepository(session).pause_on_cron_failure(
                        tenant_id, trigger_id
                    )
                logger.error(
                    "[TaskCenter] 触发器 %s 到点起任务失败，且是**配置/数据类**（code=%s，"
                    "重试无用）→ 已置 paused，改好配置后 resume：%s",
                    trigger_id,
                    code,
                    exc,
                )
                return
            logger.error(
                "[TaskCenter] 触发器 %s 到点但起任务失败（按瞬态处置，不暂停）：%s",
                trigger_id,
                exc,
            )

    # ── 派活（§7.5 ①）─────────────────────────────────────

    async def _dispatch_pass(self) -> None:
        """扫「能跑的子任务」，逐个试着派活；再扫一遍「卡太久的就绪 `pending`」清僵尸。

        扫描（`list_pending_for_dispatch`）只捞「真能跑的排队行」——所属任务还在跑、
        属于任务当前那次运行、且依赖都 `success`（§6.3）。进场顺序就是派活顺序
        （先来先服务，§6.3）。

        **防僵尸紧跟在派活之后**（§7.5 D2b）：两趟看的是**同一批候选**（
        `list_stuck_pending` 复用的就是派活那条 SQL），只是派活那趟选中的、这趟已经不在
        `pending` 了。**必须同源** —— 否则会把「上游失败留下的、故意 hold 的 `pending`」
        也判成僵尸，那等于悄悄改掉 §6.3 的 hold 语义。

        **两趟的批量口径不一样，别顺手统一**：派活受 `self._batch` 限制（§7.5 ①，
        §7.5「单轮上限 10 条」），**防僵尸不受**（附录 A.4 D2b 原话：「它每轮会把
        「过了自己阈值」的就绪 `pending` 一次全置 `timeout`，而且**这个清退不受
        `BATCH` 限制**」）—— 这里以前给防僵尸也传了 `self._batch`，等于把兜底砍成了
        "一轮最多清 `BATCH` 条"，积压大时要排很多轮才轮得到，**正是 D2b 要防的场景**。
        所以这条不传 `limit`（仓储层默认不设上限）。

        **`self._batch` 卡的是「一轮真派出去几条」，不是「一轮扫几条候选」**（这是
        「派活队头阻塞」的修法，两条口径曾经是同一个数、会互相拖死）：

        - **扫描面放宽到 `BATCH × TASK_CENTER_SCHEDULER_SCAN_FACTOR`**；
        - 每派出去一条记一个数，**够 `BATCH` 条就提前收工**，后面没轮到的候选留到下一轮。

        为什么必须分开：**被 `max_concurrency` 挡下的候选一个字都不写库** ——
        `_over_concurrency` 的判据在 `claim_for_dispatch` 里、**排在 `claim_pending`
        之前**（见 `task_service.claim_for_dispatch`），所以它还是 `pending`、
        `updated_at` 没动、**排序键也没动**
        → 下一轮**还在候选集里、还在原来的位置**。若扫描面就等于 `BATCH`，队头那 10 条
        一旦全卡在同一个瓶颈上（比如 `ingest` 上限 2 且配额占满），**每一轮都被它们吃满
        名额**，排在它们后面、配额明明空着的活（别的目录、别的 `step_code`）连窗口都进不去
        —— 一直堵到队头被防僵尸判死（`timeout_seconds × 3`，内置步骤 = 21 小时）。
        这不是猜的：`list_stuck_pending` 自己的 docstring 就承认「满载时连『正常在排队』
        的也会被它判掉 —— 这是有意的取舍」，那条取舍本来是防「派活一路失败、状态退不
        回去」的 `pending` 永远堵着队头，不该连带把正常排队的也一起等 21 小时。

        分开之后：被挡下的候选**只消耗扫描名额、不消耗派活名额**，一轮照样能派满
        `BATCH` 条。**排序语义（先来先服务）一个字没动** —— 还是同一条 SQL、同一个
        `order_by`，只是 `LIMIT` 放宽了。
        """
        scan_limit = self._batch * self._scan_factor
        async with get_db_session() as session:
            repository = SubtaskRepository(session)
            pending = await repository.list_pending_for_dispatch(scan_limit)
            subtask_ids = [row.id for row in pending]
            stuck = await repository.list_stuck_pending(
                multiplier=self._stuck_multiplier
            )
            stuck_ids = [row.id for row in stuck]
        dispatched = 0
        examined = 0
        for subtask_id in subtask_ids:
            if dispatched >= self._batch:
                # 派满这一轮的额度了：剩下的候选（含这一轮没轮到的）留给下一轮。
                # 它们都还是 `pending`、排序不变，下一轮从头扫照样会扫到，不会漏。
                break
            examined += 1
            if await self._dispatch_one(subtask_id):
                dispatched += 1
        # 没派满额度时留一行日志：说明候选**全看完了还是凑不满** `BATCH`（并发被占满 /
        # 依赖没就绪 / 锁被占），属于正常运转下也会出现的状态，所以是 `debug`；
        # 但排查「扫描面够不够宽」「是不是堵住了」时，`examined == len(候选)` 这个数
        # 就是唯一线索 —— 它等于扫描上限且 `dispatched` 远小于 `BATCH`，就该调大
        # `TASK_CENTER_SCHEDULER_SCAN_FACTOR` 了。
        if dispatched < self._batch:
            logger.debug(
                "[TaskCenter] 派活本轮派了 %d 条（上限 %d），看完 %d 条候选",
                dispatched,
                self._batch,
                examined,
            )
        for subtask_id in stuck_ids:
            await self._stuck_one(subtask_id)

    async def _locate_task_id(self, subtask_id: str) -> Optional[str]:
        """**锁外**只读一次，拿这条子任务所属的 `task_id` —— 加锁要用它（§7.2）。

        必须先读这一下：不知道锁哪条任务就无从加锁。这一份只用来算锁的 key，
        **不能拿来裁决** —— 锁内都要重读（等锁期间这行可能已经被别的副本改过）。
        """
        async with get_db_session() as session:
            located = await SubtaskRepository(session).get_any_by_id(subtask_id)
            return None if located is None else located.task_id

    async def _dispatch_one(self, subtask_id: str) -> bool:
        """派一个子任务：**拆成四段**，因为「锁不跨出站调用拿」（§7.2）。

        **返回「这一条算不算占用了本轮的派活名额」**（`True` = 占了）——调用方
        `_dispatch_pass` 用它来卡 `self._batch`。判据是**有没有走到第 ③ 段（出站派活）**：
        那一步才是这一轮真正的开销（加锁 + 一次最长 30 秒的 HTTP）。前两段半途而废的
        （定位不到 / 锁被占 / 没就绪 / **并发到顶** / ①② 抛异常）**一律不算** ——
        它们没出站、也没改库，下一轮还会被扫到，不该吃掉本轮的额度。**并发到顶那条
        尤其重要**：它一个字都没写库，算进去就会重新变成「队头堵死」的老毛病。

        ① **锁外只读定位**拿 `task_id`；
        ② **事务 A（持锁）**：判就绪 / 判并发上限 / 抢占 —— **提交之后才放锁**；
        ③ **出站派活**（**不持锁、不占事务**）：没够着运行方就**在这一段里同步重发**
        （`_dispatch_with_retry`，最多 3 次）；
        ④ **事务 B（重新拿锁）**：按判决收尾 —— `ACCEPTED` → 任务转 `running`；
        `REJECTED`（运行方明确收不了）→ 子任务置 `failed`；`DELIVERY_UNKNOWN` 用完预算
        → **再分两档**：网关连撞 5xx 判死（同 `REJECTED` 的处置）、其余未知**一个字都不改**、
        停在 `running` 交超时巡检（§7.4）。

        **② 抢不到锁 → 跳过这条、看下一条**（§7.2 ② 第 2 层）：这是扫描类调用，下一轮
        还会扫到它，天然可重来。**④ 不能再跳过**，所以走 `patient=True` 继续等 ——
        跳到这一步等于「运行方已经明说收不了、却没人把这个回执记下来」，子任务会白占着
        名额一直卡到超时判死，真实原因也丢了（那就是 §7.2 说的「不能静默丢弃」）。

        **为什么不把 ②③④ 合进一个事务**（这是本方法唯一反直觉的地方）：出站耗时不可控
        （运行方卡 30 秒），合起来会同时坏两件事 —— 整个事务被吊着不提交、以及 Redis 锁
        被拖到自动过期（等于没锁，别的副本会同时进来改同一条任务）。拆开之后两次拿锁
        之间别的副本可能插进来，兜底是 `state_version` 的 CAS：`finalize_dispatch` 会
        **重读**这一行、并认「`execution_id` 还是不是我这次抢下的那个」，对不上就整单作废。

        **③ 与 ④ 的异常处置必须分开**（曾经一个 `except` 把四段全罩住，是错的）：③ 现在
        **不抛了** —— 出站能出的错都在 `_dispatch_with_retry` 里被翻成判决并进了重发循环
        （包括「调用自己炸了」那一档，它归 `DELIVERY_UNKNOWN`）。④ 是**运行方已经收下之后
        我们收尾失败**，它抛异常**绝不能当成「运行方收不了」**：此刻判 `failed` 会把运行方
        真在跑的那一步做成孤儿（它之后上报成功也会被终态闸丢掉），只能留在 `running` 等
        上报/超时兜底。④ 那段单独兜异常的道理见 `_finalize_one`。

        ── ⚠️ 已知缺口：进程在 ② 与 ③ 之间被杀掉（**2026-09-30 决定「不修、只记下来」**）──

        ② 是「在数据库上标成 `running` 并提交」，③ 才是「去告诉运行方」。**在这两步之间
        进程被杀**（重启 / 崩溃），那一步就**永远停在 `running`** —— 运行方压根没收到过这单活，
        所以它不会上报、也不会在自己那条 6 小时的看门狗里判死（它连这步都不知道），
        只剩**本平台的超时巡检**在 `timeout_seconds`（内置步骤 25200 秒 = 7 小时）时收它。

        这段时间里：那条**任务**重试不了（要求终态）、也停不了（要求当轮还有没开始的步骤）；
        而且那个 `running` **一直占着一个并发名额** —— 注意额度是**按 `step_code` 全局共用**
        的（跨库跨租户同一份，内置步骤各 10 个），占一个就是少 10%。

        **为什么决定不修**：窗口只有「标记提交完 → 请求发到运行方」那几十毫秒到几秒，
        要正好在这一瞬被杀才中招；攒够 10 个（把某一步在**本目录**堵死）需要十次精确命中，
        实际上不可能。为它加一列「派活已确认送达」不划算。

        **万一真碰上，手工恢复只有一步**：把那一步的状态改成 `failed` 即可 ——
        **后台的「补收口」趟次会自动接手**（那一步成为终态后，当次运行就没有未完成的步骤了，
        `_settle_pass` 扫到它、把任务收成终态），然后重试 / 删除全都通了。
        一句话：

            UPDATE task_center.subtasks SET status='failed', error_message='人工清理：派活未送达'
             WHERE id='<那一步的 id>';

        ⚠️ **别用「换顺序」去根治**（先通知、再标记）：那会拆掉 ② 那次 CAS 的互斥 ——
        两个副本会各自生成**不同的 `execution_id`** 各通知一次，运行方的去重是按 `execution_id`
        的、挡不住 → **同一份活跑两遍**；而且运行方在标记落库之前就上报的话，那条上报
        按 `execution_id` 定位不到任何行，会被当成「找不到对应的活」丢掉。
        """
        try:
            # ① 锁外只读定位
            task_id = await self._locate_task_id(subtask_id)
            if task_id is None:
                return False

            # ② 事务 A（持锁）：判就绪 / 判上限 / 抢占，**先提交再放锁**
            try:
                async with task_lock(task_id):
                    async with get_db_session() as session:
                        prepared = await TaskService(
                            session, runner_adapter=self._adapter
                        ).claim_for_dispatch(subtask_id)
            except TaskLockBusy:
                logger.info(
                    "[TaskCenter] 任务 %s 的锁被占，本轮跳过子任务 %s、看下一条（§7.2）",
                    task_id,
                    subtask_id,
                )
                return False
            if prepared is None:
                return False
        except Exception as exc:
            # ①② 里出错：**这一轮什么都没抢下**（要么没提交、要么提交失败已回滚），
            # 下一轮照常会扫到它，所以这里只记日志、什么都不用收拾。
            logger.warning("[TaskCenter] 派活前半段出错 subtask=%s：%s", subtask_id, exc)
            return False

        # ③ 出站派活（**不持锁、不占事务**）：**没够着运行方就在这一段里同步重发**
        #    （§4.1 ①）—— 只有「不知道对方收没收到」才重发，运行方给了话（收下 / 明确
        #    拒收）都当场收工。重发的是**同一个 `execution_id`**，运行方那侧的执行登记
        #    认得出是重派、直接回成功、不会重复干活（这就是重发安全的前提，§4.5）。
        #    预算用尽仍是「没够着」时**分两档**（交回 ④ 段处置）：网关 5xx 那一类判死、
        #    其余未知原地不动、停在 `running`（判据与理由见 `_dispatch_with_retry`）。
        result = await _dispatch_with_retry(
            self._adapter, prepared.capability_id, prepared.dispatch
        )

        # ④ 事务 B（**重新**拿锁）：收尾。单独兜异常 —— 见下面的注释，这里不能置 failed。
        await self._finalize_one(prepared, result)
        # 走到这里 = 第 ③ 段真的出站了（回执拿到也好、半路炸了也好），这一条算占了
        # 本轮的派活名额 —— 上面那句注释里的「①② 出错不算」，边界正好画在这儿。
        return True

    async def _finalize_one(self, prepared, result) -> None:
        """派活的第 ④ 段：把出站判决收口（收下 → 任务转 `running`；明确拒收 → 子任务 `failed`；
        **没够着 → 网关 5xx 判死 / 其余原地不动**，见 `TaskService.finalize_dispatch`）。

        **这一段抛异常绝不能当成「运行方收不了」**：出站调用已经**正常返回**了，运行方
        多半已经收下这一步、甚至已经在跑。此刻判 `failed` 会把那个真在跑的子任务做成
        孤儿 —— 它之后上报成功也会被「终态闸」丢掉（闸③ 终态最先拦），等于把一次成功
        记成失败。所以这里只记日志：子任务留在 `running`，由运行方上报正常推进；真没
        动静就由超时巡检兜底（§7.4）—— 这跟「没响应」是同一个兜底口径。

        （跟 `_stuck_one` / `_timeout_one` 一样**按子任务粒度兜异常**，但**这里不能
        `except TaskLockBusy` 跳过**：跳到这一步等于「运行方已经给了回执、却没人把它记
        下来」，所以拿锁用的是 `patient=True` —— 见 `_dispatch_one` 的开头。）

        ⚠️ **`patient=True` 是「有心等」，不是「等到底」**：它是 `1 + 3×50ms + 30×100ms
        ≈ 3.15 秒` 的**有界窗口**（§7.2 ①、`TASK_CENTER_LOCK_WAIT*`），窗口内等不到就抛
        `TaskLockBusy`、落进下面那个 `except Exception` **只记一条 warning** —— 这一次的
        回执也就丢了。**这是已知且接受的代价**：收敛不受影响（子任务停在 `running`，
        运行方上报正常推进；真没动静由超时巡检按 `timeout_seconds` 收口），只是在那条
        上报落地之前，控制台会看到「任务 `pending`、子任务 `running`」并存 —— §7.2 那句
        「任务状态跟着子任务走」在**这个窗口里**不成立。要彻底堵上得在这里记一笔「待补
        收尾」，收益不抵复杂度，一期不做。
        """
        try:
            async with task_lock(prepared.task_id, patient=True):
                async with get_db_session() as session:
                    await TaskService(
                        session, runner_adapter=self._adapter
                    ).finalize_dispatch(prepared, result)
        except Exception as exc:
            logger.warning(
                "[TaskCenter] 派活收尾出错 task=%s node=%s execution=%s（运行方可能已收下，"
                "子任务留在 running 等它上报、真没动静由超时巡检兜底）：%s",
                prepared.task_id,
                prepared.node_id,
                prepared.execution_id,
                exc,
            )

    # ── 消化上报（§7.5 ②）────────────────────────────────

    # ── 对账（拉）：问运行方「那一步现在什么情况」（§4.1 ④，2026-10-11 加）──

    async def _query_pass(self) -> None:
        """扫「**派出去很久、却一直没消息**」的子任务，逐个问运行方（§7.5）。

        ## 为什么要有这一趟

        推进编排原来**只能等运行方主动上报**（`report_status`）—— 上报是「单次尝试 +
        最多 3 次重发」，全丢了子任务就一直挂 `running`，直到编排硬超时（预置 7 小时）
        才被兜。**而丢的原因多半不在网络**：运行方那侧「什么时候该上报」的判断错了、
        或者它压根没走到那一步。

        **这一趟是「拉」**：问不到就下一拍再问，**不存在永久丢**。

        ## 为什么把答案「塞进收件箱」而不是自己收口

        **收口只有一条路才不会有分叉**：`_report_pass` 消化 `subtask_reports` 那一趟
        负责裁决、落库、填下游、收尾。这里把答案**写成同样形状的一行**塞进去，两者走的是
        **同一段代码** —— 不是为了省代码，是为了**保证「推走的」和「拉回来的」结果一模一样**
        （同样的下游派活、同样的聚合、同样的收尾）。

        ## 只问「沉默够久、而且真派出去过」的

        口径全在 `ExecutionRepository.list_silent_in_flight` 里（`execution_id IS NOT NULL`
        那条尤其要紧：没派过活的问了也白问）。
        """
        quiet = int(getattr(get_config(), "TASK_CENTER_QUERY_QUIET_SEC", 120) or 120)
        try:
            async with get_db_session() as session:
                rows = await SubtaskRepository(session).list_silent_in_flight(
                    quiet_seconds=quiet, limit=self._batch,
                )
                # 扫描事务只取「要问哪几条」，取完就结束（跟另外几趟同一条纪律）。
                targets = [
                    (
                        r.id, r.tenant_id, r.task_id, r.task_attempt_id,
                        r.node_id, r.step_code, r.execution_id, r.space_id,
                    )
                    for r in rows
                ]
        except Exception as exc:  # noqa: BLE001
            logger.warning("[TaskCenter] 对账扫描出错，本轮跳过：%s", exc)
            return
        for target in targets:
            # **逐条兜异常**（另外几趟同构）：一条问不动不该拖垮整轮。
            try:
                await self._query_one(*target)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[TaskCenter] 对账问一条出错 task=%s node=%s：%s",
                    target[2], target[4], exc,
                )
            finally:
                # ⚠️ **无论问到什么都没关系，都要记一笔「问过了」**：
                # 问到「还在跑」也算问过 —— 不记的话它还留在队头，
                # 下一拍又问同一批，把名额一直占住（`list_silent_in_flight` 有说明）。
                try:
                    async with get_db_session() as session:
                        await SubtaskRepository(session).mark_queried(target[0])
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[TaskCenter] 记对账时间戳失败 subtask=%s：%s", target[0], exc,
                    )

    async def _query_one(
        self,
        subtask_id: str,
        tenant_id: str,
        task_id: str,
        task_attempt_id: str,
        node_id: str,
        step_code: str,
        execution_id: str,
        space_id: str,
    ) -> bool:
        """问一条，问到终态就**当一条上报塞进收件箱**（下一拍由 `_report_pass` 消化）。

        **返回「这条是不是已经有结论了」**（2026-10-16 加）：`True` = 问到终态并成功入队，
        `False` = 没问到 / 还在跑 / 运行方说不归它 / 报文不合格 —— 一律「本轮不改结论」。
        ⚠️ 调用方（超时巡检）拿它决定「这一条还要不要照常判超时」：**只有 `True` 才跳过**。

        三档处置：

        - **没问到 / 还在跑 / 还没开始跑** → 什么都不做，下一拍再来（**这就是「拉」的意义**）；
        - **`unknown`（运行方说「这活不归我」）** → **只记一条 warning、不塞收件箱** ——
          它不是失败（理由见协议层 `STEP_STATUS_UNKNOWN`）。真把「找错运行方」写成失败，
          一次**配置错误**就变成了一条**业务失败**；
        - **`succeeded` / `failed`** → 把它带回的那份报文（**形状就是上报接口的入参**）
          塞进收件箱，走与上报**完全相同**的消化路径。
        """
        # ① 找这一步该谁干（与派活同一套解析：编排快照里的 runner → 在册的 capability_id）
        async with get_db_session() as session:
            svc = TaskService(session, runner_adapter=self._adapter)
            ctx = await svc._load_context(tenant_id, task_id, task_attempt_id)
            if ctx is None:
                return
            subtask = next((s for s in ctx.subtasks if s.node_id == node_id), None)
            if subtask is None:
                return
            capability_id = await svc._resolve_runner_capability(ctx, subtask)
        if capability_id is None:
            # 这一步没绑到在册的运行方 —— 那是派活那侧的问题（它有自己的判死），
            # 对账这一趟不该替它处置。
            return False

        # ② 问。**不入参 `inputs`**（`ExecutionEnvelope` 就是派活报文去掉它的那个形状）。
        answer = await self._adapter.query_step_status(
            capability_id,
            ExecutionEnvelope(
                tenant_id=tenant_id,
                space_id=space_id,
                task_id=task_id,
                step_code=step_code,
                node_id=node_id,
                execution_id=execution_id,
            ),
        )

        # ③ 处置
        if answer is None or answer.status in (
            STEP_STATUS_PENDING_DISPATCH, STEP_STATUS_RUNNING,
        ):
            return False  # 没问到 / 还早 —— 下一拍再来
        if answer.status == STEP_STATUS_UNKNOWN:
            logger.warning(
                "[TaskCenter] 对账：运行方说这活不归它（按配置错处理，不当失败）"
                "task=%s node=%s detail=%s",
                task_id, node_id, answer.detail,
            )
            return False
        report = dict(answer.report or {})
        # ⚠️ **状态以 `answer.status`（五档那个）为准，不信 `report["status"]`**：
        # 后者是运行方按**上报接口**的词汇填的，但运行方完全可能回它**自己的词汇**
        # （比如 `"succeeded"`）—— 那种值入队后在**消化那一步被静默丢弃**
        # （`_absorb` 闸④ 判「状态取值非法」只记一条 warning），子任务一路挂到硬超时，
        # 而表面上「对账已经问到了」。这里归一化，非法值**不可能入队**。
        report["status"] = (
            "success" if answer.status == STEP_STATUS_SUCCEEDED else "failed"
        )
        if report["status"] == "success" and not isinstance(report.get("outputs"), dict):
            # **成功必须带产出**：管理方写完「成功」就要拿 `outputs` 按 `bind_inputs`
            # 填下一步的输入，没有产出 = 派不出下游 = 卡在下一步。
            # 按 `ERROR` 记（这是协议/实现错，不该静默）。
            logger.error(
                "[TaskCenter] 对账：运行方答成功却没带产出（派不出下游），不入队 "
                "task=%s node=%s report=%r",
                task_id, node_id, str(answer.report)[:200],
            )
            return False
        # ⚠️ **拉式通道也要过与推式同一套协议校验**（2026-10-13 补）：推式上报在受理点会跑
        # `_check_report_payload`（内含 `check_error_reason` / `check_output_refs`），
        # 而这条「管理方主动问回来」的路从前**只归一化 `status`、其余一字不校验** ——
        # 运行方在 `StepStatusQuery.report` 里填个自造 / 拼错的 `error_code`、或形状错误的
        # `output_refs`，会被**原样落库**，读侧按码渲染时翻不出来（正是 `ERROR_REASON_CODES`
        # 那段注释要防的「往管理方界面上写任意文本」）。判据只有一份，这里复用、不另写。
        _proto_problem = check_error_reason(
            report.get("error_code"), report.get("error_params")
        )
        if _proto_problem is None:
            _proto_problem = check_output_refs(report.get("output_refs"))
        if _proto_problem is not None:
            logger.error(
                "[TaskCenter] 对账：运行方回的报文没通过协议校验，不入队 "
                "task=%s node=%s reason=%s",
                task_id, node_id, _proto_problem[0],
            )
            return False
        async with get_db_session() as session:
            await SubtaskReportRepository(session).enqueue(
                tenant_id=tenant_id,
                space_id=space_id,
                execution_id=execution_id,
                task_id=task_id,
                status=report.get("status"),
                outputs=report.get("outputs"),
                output_refs=report.get("output_refs"),
                error_message=report.get("error_message"),
                error_code=report.get("error_code"),
                error_params=report.get("error_params"),
                reported_at=datetime.now(),
            )
        logger.info(
            "[TaskCenter] 对账：问到终态，已当一条上报入队（下一拍消化）"
            "task=%s node=%s status=%s",
            task_id, node_id, report.get("status"),
        )
        return True

    async def _report_pass(self) -> None:
        """扫「运行方报了、还没消化」的上报，逐条消化（§7.5 ②）。

        扫描跨租户、按 `reported_at` 升序（**先收到的先消化**），每行自带 `tenant_id`
        —— 跟派活、巡检一套口径：后台循环没有租户上下文，扫出来再按行内的租户显式走。

        **这一趟是「上报只要被受理就一定处理到底」的落点**：入口（`report_status`）只做
        报文级校验就回「受理」，真正的裁决、落库、填下游、收尾全在这里。
        """
        async with get_db_session() as session:
            pending = await SubtaskReportRepository(session).list_pending(self._batch)
            report_ids = [row.id for row in pending]
        for report_id in report_ids:
            await self._report_one(report_id)

    async def _report_one(self, report_id: str) -> None:
        """消化一条上报。形状照 `_dispatch_one`：**锁外只读定位 → 持锁做完整段**。

        跟派活的区别是这条**不出站**（消化就是本地写库），所以不必拆成两段 ——
        「拿锁 + 一个事务」就盖住了整段「读—改—写」，跟 `_stuck_one` / `_timeout_one`
        同构（§7.2「锁不跨出站调用」）。

        **抢不到锁 → 跳过这条、看下一条**（扫描类调用的固定处置）：这条上报的
        `is_deleted` 还是 0，下一轮照样会被扫到，所以「跳过一次」不等于「永远不处理」。
        这正是它跟从前「回可重试码让运行方自己重发」的区别 —— **重试的责任留在管理方这边**。

        **消化和「标已消化」必须在同一个事务里**（它们共用一个 `async with
        get_db_session()`）：只成一半就会出现「状态推进了、上报还挂着」（下轮重复消化，
        靠闸③ 兜住）或者「上报没了、状态没动」（真丢了）。
        """
        try:
            # ① 锁外只读定位：找这条上报，再由凭证找它挂的子任务（加锁要用 `task_id`）。
            async with get_db_session() as session:
                report = await SubtaskReportRepository(session).get_any_by_id(report_id)
                if report is None or report.is_deleted != 0:
                    return  # 记录没了，或者已经被别的副本消化掉
                tenant_id = report.tenant_id
                located = await SubtaskRepository(session).get_by_execution_id(
                    report.tenant_id, report.space_id, report.execution_id
                )
                task_id = None if located is None else located.task_id

            if task_id is None:
                # ② 凭证定位不到 = 旧尝试的凭证 / 任务已被删 → 从前 ① 的「丢弃」（§4.2 ①）。
                #    **丢弃也要标掉**：不标的话每轮都会再捞到它，白转一辈子。
                #    这里可以单独一个事务 —— 它不推进任何状态，最坏情况是两个副本都标，
                #    `mark_consumed` 的 CAS 只让一个命中。
                async with get_db_session() as session:
                    await SubtaskReportRepository(session).mark_consumed(
                        tenant_id, report_id
                    )
                logger.info(
                    "[TaskCenter] 上报被丢弃（凭证定位不到 / 任务已删）report=%s", report_id
                )
                return

            # ③ 持锁 + 一个事务：消化 → 标已消化，一起成或一起不成。
            try:
                async with task_lock(task_id):
                    async with get_db_session() as session:
                        reports = SubtaskReportRepository(session)
                        # 等锁期间可能已经被别的副本消化了：重读一次，已消化的直接放行。
                        report = await reports.get_any_by_id(report_id)
                        if report is None or report.is_deleted != 0:
                            return
                        await TaskService(
                            session, runner_adapter=self._adapter
                        ).absorb_report(report)
                        await reports.mark_consumed(report.tenant_id, report_id)
            except TaskLockBusy:
                # ⚠️ **这条分支刻意不走 `_record_report_failure`**（它是唯一这样的一条）：
                # 锁竞争是**正常现象**、不是这条上报有问题，而 `_record_report_failure`
                # 会顺手写一次 `last_error`（基础设施档虽然不递增 `attempts`，但那一写躲不掉）。
                #
                # 代价是它拿不到基础设施档那条 24 小时绝对上限 —— 从前的后果是一条死锁链：
                # 某个任务的锁被**一直**占着 ⇒ 这条上报永远留在收件箱 ⇒ 超时巡检的「让路」
                # 判据永远命中 ⇒ 那条 `running` 永远判不了超时 ⇒ 本目录共用的并发名额收不回来。
                # **今天（拿不到锁本身）到不了**：锁有 `PX 30000` 自然过期、持有段又都是
                # 不跨出站的短事务，竞争是瞬时的。但这一档**不能因此不设兜底** ——
                # 兜底是给「我们没想到的那种一直占着」准备的。
                #
                # 处置：**单独**做一次超龄检查（`_dead_letter_if_overdue`）——只读一次、
                # 只在真的超龄时才写。**不并进 `_record_report_failure`**（那会给每次正常
                # 抢锁都加一次写库），也**不动 `TASK_CENTER_RETRYABLE_CODES` 里 3007 那一项**
                # （锁竞争之所以不会被判死信，靠的正是「3007 在可重试表里 ⇒ 归基础设施档」）。
                logger.info(
                    "[TaskCenter] 任务 %s 的锁被占，本轮跳过消化上报 %s、看下一条（§7.2）",
                    task_id,
                    report_id,
                )
                await self._dead_letter_if_overdue(report_id)
        except Exception as exc:
            logger.warning("[TaskCenter] 消化上报出错 report=%s：%s", report_id, exc)
            await self._record_report_failure(report_id, exc)

    async def _dead_letter_if_overdue(self, report_id: str) -> None:
        """「拿不到任务锁」那一档的兜底：在收件箱里躺够绝对上限就判死信出队（2026-10-09 加）。

        为什么单开一支、不并进 `_record_report_failure`：那条路每次调用都要写一次
        `last_error`，而锁竞争是**正常现象**（每轮都可能碰上），不该因此多写库。
        这里**先读一次、只在真超龄时才写**，正常抢锁的代价只有一次 SELECT。

        不判死的后果是一条死锁链（完整链条写在 `_report_one` 那条 `except` 的注释上）：
        这条上报永远留在收件箱 ⇒ 超时巡检永久给它「让路」⇒ 那条 `running` 永远判不了超时
        ⇒ 本目录共用的并发名额收不回来。

        **任何异常都只记 warning** —— 兜底本身不能把调度循环带崩（与 `_record_report_failure`
        同一条纪律）。
        """
        try:
            async with get_db_session() as session:
                reports = SubtaskReportRepository(session)
                report = await reports.get_any_by_id(report_id)
                if report is None or report.is_deleted != 0:
                    return
                waited = _hours_since(report.reported_at)
                if waited is None or waited < self._report_infra_max_hours:
                    return
                overdue = (
                    f"拿不到任务锁；在收件箱里已躺 {waited:.1f} 小时，"
                    f"超过上限 {self._report_infra_max_hours} 小时"
                )
                marked = await reports.mark_dead_lettered(
                    report.tenant_id, report_id, overdue
                )
            if marked:
                logger.error(
                    "[TaskCenter] 上报在收件箱里躺了 %.1f 小时、锁也一直抢不到"
                    "（上限 %.1f 小时），判死信并出队：report=%s execution=%s",
                    waited,
                    self._report_infra_max_hours,
                    report_id,
                    report.execution_id,
                )
        except Exception as exc:  # noqa: BLE001 —— 兜底不能反过来把循环带崩
            logger.warning(
                "[TaskCenter] 锁忙档的超龄兜底检查失败 report=%s：%s", report_id, exc
            )

    async def _record_report_failure(self, report_id: str, exc: BaseException) -> None:
        """消化失败时记一笔，**另开一个会话**；连续失败达上限则**判死信**（§7.5 ②）。

        失败时上面那个事务已经坏了（回滚 / 连接不可用），在它里面写必然再失败一次。

        **先分成两档，再按 `attempts` 分归宿**：

        **第一档 —— 基础设施故障**（`_is_infra_failure`：Redis / DB / 网络不可用）：
        **只写 `last_error`、不递增 `attempts`**，那条留在未消化、下轮照常再试。
        **这类故障不是这条上报的错** —— 算进 `attempts` 等于把「Redis 抖十几秒」变成
        「此刻队列里的上报一起被判死信、永久出队」，而且它还什么都没做错。Redis 一恢复，
        这些上报照常处理完。代价是故障期间队列确实会积压 —— 但那时 Redis / DB 不可用，
        整个后台循环本来就什么都干不了，积压不是新增的问题。

        ⚠️ **但这一档也必须有一条绝对上限**（`TASK_CENTER_REPORT_INFRA_MAX_HOURS`，
        默认 24 小时）。没有它这一档就是**死锁**：

        > 不计数 ⇒ 永远到不了下面那个阈值 ⇒ **永远留在收件箱里** ⇒ 超时巡检的「让路」
        > 判据**永远命中**（`list_timed_out_for_patrol` 只看「收件箱里还有没有这条
        > 子任务的未消化上报」）⇒ 那条子任务**永远判不了超时** ⇒ `running` 收不掉
        > ⇒ 任务永远不终态（`retry_task` / `task.delete` 全都要求终态，**那条任务
        > 彻底无法处置**）⇒ 那个 `running` 还占着并发名额（按「目录 + `step_code`」共用
        > 额度，攒够上限就**这一步在本目录派不出活**）。

        判据是 **`now - reported_at`**（= 这条上报自己等了多久，它进了收件箱就是
        「待处理」）。判死时**一并出队**（`is_deleted=1`）—— 让路随即失效、超时巡检
        该判就判。这就是「死信必须一起出队」那条规矩在基础设施档上的又一次生效。

        **第二档 —— 这条上报本身消化不了**（其余全部：报文撞约束、`_absorb` 里的代码
        问题），按 `attempts` 分两种归宿：

        - **没到上限** → `attempts + 1` / 写 `last_error`，那条**留在未消化**、下轮再来。
          这是「上报只要被受理就一定处理到底」的兜底，瞬时错（网络抖一下）走这条；
        - **到了上限**（`TASK_CENTER_REPORT_MAX_ATTEMPTS`，默认 5）→ **判死信**：一条
          UPDATE 把 `dead_lettered=1` + `is_deleted=1` + `processed_at` 一起写掉（**出队**），
          并打 ERROR 告警（这类需要有人看一眼）。

        **为什么必须有个上限**：`_absorb` 判不出来的失败会每轮重现，而
        `_report_pass` 每轮只捞 `list_pending(self._batch)` 的前 `_batch` 条（按
        `reported_at` 升序）—— 一条永远失败的上报只要排在最前面，就**每轮占着一个名额**，
        后面的正常上报全得排队，严重时整条队列堵住。判死信之后它出队，名额让出来。

        **死信不是把上报丢掉**：记录永远留着，`attempts` / `last_error` / `dead_lettered`
        都在，「管理方到底收到没、卡在哪」查得出来（§4.4 没有为它开接口，靠这张表和日志）。

        **判据用刚读出来的 `attempts`，不是 `RETURNING` 回来的新值**：两个副本同时给
        同一条记失败时，最多让阈值早一轮或晚一轮到达 —— 反正都会判死，不值得为这点
        偏差多一次往返。递增本身仍然在 SQL 里做（`attempts = attempts + 1`），不会丢计数。

        **本来就不会抛异常的那些情形不归这里管**：`_absorb` 能自己判定的情况一律自己
        收口（告警日志 + `return`，标已消化），压根进不了队列 —— 那条路上「每轮重试、
        永远出不去」的隐患早就被堵住了。
        """
        message = f"{type(exc).__name__}: {exc}"
        infra = _is_infra_failure(exc)
        try:
            async with get_db_session() as session:
                reports = SubtaskReportRepository(session)
                report = await reports.get_any_by_id(report_id)
                if report is None:
                    return
                if infra:
                    # 基础设施故障：只留痕、**不动 attempts** —— 下轮重来就是了。
                    # ⚠️ 但躺过绝对上限就**一律判死信出队**：不判死的话它会永远留在
                    # 收件箱里 → 超时巡检**永久**给那条子任务让路 → 它再也判不了超时
                    # （死锁，完整链条见方法 docstring）。判死 → 出队 → 让路失效。
                    waited = _hours_since(report.reported_at)
                    if waited is not None and waited >= self._report_infra_max_hours:
                        overdue = (
                            f"{message}（基础设施档；在收件箱里已躺 {waited:.1f} 小时，"
                            f"超过上限 {self._report_infra_max_hours} 小时）"
                        )
                        marked = await reports.mark_dead_lettered(
                            report.tenant_id, report_id, overdue
                        )
                        if marked:
                            logger.error(
                                "[TaskCenter] 上报在收件箱里躺了 %.1f 小时仍消化不掉"
                                "（基础设施档上限 %.1f 小时），判死信并出队："
                                "report=%s execution=%s 最后错误=%s",
                                waited,
                                self._report_infra_max_hours,
                                report_id,
                                report.execution_id,
                                message,
                            )
                        return
                    await reports.record_failure(
                        report.tenant_id, report_id, message, bump_attempts=False
                    )
                    logger.warning(
                        "[TaskCenter] 上报消化遇到基础设施故障，本轮不计数、下轮重来："
                        "report=%s execution=%s 原因=%s",
                        report_id,
                        report.execution_id,
                        message,
                    )
                    return
                next_attempts = (report.attempts or 0) + 1
                if next_attempts >= self._report_max_attempts:
                    marked = await reports.mark_dead_lettered(
                        report.tenant_id, report_id, message
                    )
                    if marked:
                        logger.error(
                            "[TaskCenter] 上报消化连续失败 %d 次（上限 %d），判死信并出队："
                            "report=%s execution=%s 最后错误=%s",
                            next_attempts,
                            self._report_max_attempts,
                            report_id,
                            report.execution_id,
                            message,
                        )
                else:
                    await reports.record_failure(
                        report.tenant_id, report_id, message
                    )
        except Exception as inner:  # 连"记失败"都失败，只剩日志
            logger.warning(
                "[TaskCenter] 记上报消化失败时又出错 report=%s：%s", report_id, inner
            )

    # ── 超时巡检（§7.4）───────────────────────────────────

    async def _patrol_pass(self) -> None:
        """扫「跑着且已过超时时刻」的子任务，逐个置 `timeout` 并收敛。

        超时时刻读 `subtasks.timeout_seconds`（子任务诞生时从编排固化），不写死在代码里。
        多个副本同时扫到同一批也无所谓：判超时走 `running → timeout` 的 CAS，
        后到的命中 0 行被拒（§7.5 ③）。超时是终态、一期不自动重试（§6.4）。

        **这一趟不受 `BATCH` 约束**（`limit` 不传，仓储层默认不设上限）—— 跟同一趟
        循环里的防僵尸（`list_stuck_pending`）一个口径。两条都是「兜底判死」，凭什么
        一个不受限、一个受限：

        - 设计文档点「单轮上限（10 条）」时只点了 ⓪ 触发抢占 / ① 派活 / ② 消化上报
          **三趟**，巡检**不在那三条里**；
        - 判死一条就离开候选集，所以卡 `BATCH` 的话积压 N 条要 `ceil(N / BATCH)` 轮
          × `interval` 秒才消化完，而且**没有上限** —— 积压越大拖越久。运行方整体
          重启一次，几十上百条子任务一起堆进来是真实场景；
        - 而这一趟原来**完全不出站**（拿锁 + 一个事务，见 `_timeout_one`），比派活便宜
          得多，一轮多判几条不构成负担。

        ⚠️ **2026-10-16 起这一趟会先出站问一次**（下面那段）：判死之前先问运行方
        「这一步现在什么情况」—— 为的是接住「**运行方早就干完了、只是上报没送到**」那种情形。
        出站排在拿锁之前（§7.2「锁不跨出站调用」），所以锁那一段还是原来那么短。

        判死不是判错：真卡住的任务早收口一分钟，用户早一分钟能重试 / 重传。
        """
        now = datetime.now()
        async with get_db_session() as session:
            running = await SubtaskRepository(session).list_timed_out_for_patrol(now)
            # ⚠️ **把「问」要用的字段一并抄出来**：下面要出站，而这段会话一关这些 ORM 行
            # 就 detached（同 `_StepSnapshot` 那条纪律），出站之后再读它们的属性会炸。
            # 所以这里不留 id 列表，直接抄成标量元组。
            pending = [
                (
                    row.id,
                    row.tenant_id,
                    row.task_id,
                    row.task_attempt_id,
                    row.node_id,
                    row.step_code,
                    row.execution_id,
                    row.space_id,
                )
                for row in running
            ]
        for (
            subtask_id,
            tenant_id,
            task_id,
            task_attempt_id,
            node_id,
            step_code,
            execution_id,
            space_id,
        ) in pending:
            # ✅ **判死之前先问一次**（2026-10-16 用户口径）：
            #   · 运行方说「早就完事了」→ 问到终态、当一条上报入队（`_query_one` 回 True）
            #     ⇒ **这一条本轮不判超时**（下一拍消化那条上报，状态按实数落）；
            #   · 说「还在跑」/ 没问到 / 说不归它 → 照常判超时（**这才是超时该有的结论**）。
            # 为什么必须问：运行方的上报可能压根没送到（网络、代理、它自己被重启……），
            # 而它那侧已经收口、接管行也交还了 —— 只盯着本地超时那一列，就会把一条
            # **其实早已成功**的活判成超时。
            # ⚠️ **没派出去的不问**（`execution_id` 为空）：那是「还没轮到它」，
            # 运行方压根没收到这一步，问了也只回 `unknown`（白打扰一次）。
            if execution_id and await self._query_one(
                subtask_id,
                tenant_id,
                task_id,
                task_attempt_id,
                node_id,
                step_code,
                execution_id,
                space_id,
            ):
                continue
            await self._timeout_one(subtask_id, now)

    # ── 防僵尸（§7.5 D2b）─────────────────────────────────

    async def _stuck_one(self, subtask_id: str) -> None:
        """把一个「就绪但卡太久」的 `pending` 置 `timeout`。单独一个事务。

        跟派活同一批候选（见 `_dispatch_pass`）：能派出去的在上一趟已经派出去了，
        这趟剩下的才是真卡住的。`handle_stuck_pending` 自己会复核「还是不是 `pending`」，
        所以「刚被派走」这种只会命中 0 行、不会误判。

        **全程只用数据库、不出站**，所以「拿锁 + 一个事务」就盖住了整段「读—改—写」
        （§7.2）—— 不用像派活那样拆两段。**抢不到锁 → 跳过这条、看下一条**（扫描类调用，
        下一轮还会扫到；锁内 `handle_stuck_pending` 会复核「还是不是 `pending`」，
        跳过也不会漏判）。
        """
        try:
            task_id = await self._locate_task_id(subtask_id)
            if task_id is None:
                return
            try:
                async with task_lock(task_id):
                    async with get_db_session() as session:
                        subtask = await SubtaskRepository(session).get_any_by_id(
                            subtask_id
                        )
                        if subtask is None:
                            return
                        await TaskService(
                            session, runner_adapter=self._adapter
                        ).handle_stuck_pending(subtask)
            except TaskLockBusy:
                logger.info(
                    "[TaskCenter] 任务 %s 的锁被占，本轮跳过防僵尸检查 %s（§7.2）",
                    task_id,
                    subtask_id,
                )
        except Exception as exc:
            logger.warning("[TaskCenter] 防僵尸出错 subtask=%s：%s", subtask_id, exc)

    async def _timeout_one(self, subtask_id: str, now: datetime) -> None:
        """超时巡检**判死一条**子任务（⚠️ 调用方 `_patrol_pass` 会先出站问一次运行方，
        那一问在**锁外**；这里只管判死这一段）。跟防僵尸同构：**这一步不出站** → 锁盖住整个「读—改—写」，
        抢不到锁就跳过这条、看下一条（§7.2）。
        """
        try:
            task_id = await self._locate_task_id(subtask_id)
            if task_id is None:
                return
            try:
                async with task_lock(task_id):
                    async with get_db_session() as session:
                        subtask = await SubtaskRepository(session).get_any_by_id(
                            subtask_id
                        )
                        if subtask is None:
                            return
                        await TaskService(
                            session, runner_adapter=self._adapter
                        ).handle_timeout(subtask, now)
            except TaskLockBusy:
                logger.info(
                    "[TaskCenter] 任务 %s 的锁被占，本轮跳过超时巡检 %s（§7.2）",
                    task_id,
                    subtask_id,
                )
        except Exception as exc:
            logger.warning("[TaskCenter] 超时巡检出错 subtask=%s：%s", subtask_id, exc)

    # ── 补收口（§7.5，2026-09-29 加）──────────────────────

    async def _settle_pass(self) -> None:
        """扫「当次运行早跑完、任务快照却还停在活跃态」的任务，补收成终态（§7.5）。

        **要救的是哪一格**：`_aggregate` 收口那一刻，**任务快照那次 CAS 落空**（锁提前
        过期 / 锁外有人写 `tasks`）会留下「运行行已收口、任务快照还在 `running`」。
        那之后：

        - 后台四趟一趟都不碰它（派活与防僵尸找**当次运行**的 `pending`、超时巡检找
          `running`、消化上报要有人上报）；
        - `retry_task` / `retry_subtask` / `retry_subtasks` 要求任务终态 → 拒；
        - `task.stop` 要求「当次运行还有没开始的步骤」→ 拒；
        - `task.delete` 数「还有没有没开始的步骤」时**跨所有运行**数，会被旧运行里那些
          「永远不会再跑」的 `pending` 挡住 → 也拒。

        三扇门全关 + 后台全不管 = 只能人工改库。这一趟就是把那一格收回来。

        **不受 `BATCH` 约束**（与防僵尸 / 超时巡检同口径，理由见 `_patrol_pass` 那段）：
        它同样完全不出站（拿锁 + 一个事务，见 `_settle_one`），卡上限只会让积压排队等收口。

        **扫描是保守的、判定交给 `_aggregate`**：这里不重算依赖（重写一遍就会把 §6.3 的
        hold 语义复制到第二个地方），所以偶尔会有「看了不用管」的任务进来 —— 它们在
        `_aggregate` 里被 `_run_settled` 挡下、什么都不做。误报只花一次拿锁 + 一次查询，
        漏报才是没救。

        ⚠️ **两个口径别搞混**（候选判据 2026-10-09 换过一次，从「当次运行一个
        `pending` 都没有」换成「当次运行那一行**已经收口**」）：
        · **进不来的**：「刚建好、正等派活」那种（运行行还在 `pending` / `running`）、
          没有子任务的空任务；
        · **进得来但什么都不做的**：SQL 只看「运行行收口了没有 + 有没有 `running`」，
          而「该不该收口」还要看依赖 —— 差集就是这一档（典型是「下游被上游 hold 住」）。
        ⚠️ 换判据**不是为了多扫东西**：健康任务（含刚建好那种）的运行行都还没收口，
        所以跟派活抢锁的候选没变多；换的是**把「当次运行还剩被上游 hold 住的 `pending`」
        那种残格捞进来** —— 从前它只能靠用户点「停止」。

        ⚠️ **本趟排在每轮最后、且不受 `BATCH` 约束**（与防僵尸 / 超时巡检同口径，理由
        见 `_patrol_pass`）。代价：**积压多少条，这一轮就串行做多少次「拿锁 + 两个会话」**，
        而 `_loop` 是「跑完一轮才 sleep」，所以**积压会直接拖慢整轮**（其余四趟一起等）。
        接受它的理由：候选常态是 0（只在收口 CAS 落空那种异常后才有），而这一趟完全不出站。
        """
        stale_before = datetime.now() - timedelta(
            seconds=get_config().TASK_CENTER_SETTLE_STALE_SEC
        )
        async with get_db_session() as session:
            rows = await TaskRepository(session).list_tasks_needing_settle(stale_before)
            targets = list(rows)
        for tenant_id, task_id in targets:
            await self._settle_one(tenant_id, task_id)

    async def _settle_one(self, tenant_id: str, task_id: str) -> None:
        """补收口一条任务。跟超时巡检**判死那一段**同构：**不出站** → 锁盖住整个「读—改—写」，
        抢不到锁就跳过这条、看下一条（§7.2）。
        """
        try:
            try:
                async with task_lock(task_id):
                    async with get_db_session() as session:
                        settled = await TaskService(
                            session, runner_adapter=self._adapter
                        ).settle_stale_task(tenant_id, task_id)
                if settled:
                    # **报 WARNING 而不是 INFO**：这一趟**本来该什么都不做** —— 它能收到口
                    # 就说明前面某一次收口失败了（任务快照那次 CAS 落空）。能自愈不等于没问题。
                    logger.warning(
                        "[TaskCenter] 补收口：任务 %s 的当次运行早已结束、任务快照却没跟上，"
                        "本次已补成终态（*前面某次收口落空过，值得查一眼*）",
                        task_id,
                    )
            except TaskLockBusy:
                logger.info(
                    "[TaskCenter] 任务 %s 的锁被占，本轮跳过补收口（§7.2）", task_id
                )
        except Exception as exc:
            logger.warning("[TaskCenter] 补收口出错 task=%s：%s", task_id, exc)


# ============================================================
# 进程内的单例（能力模块启动 / 关闭时用）
# ============================================================

_scheduler: Optional[TaskScheduler] = None


def get_scheduler() -> TaskScheduler:
    """取本进程的后台循环（首次调用时创建）。"""
    global _scheduler
    if _scheduler is None:
        _scheduler = TaskScheduler()
    return _scheduler


async def start_scheduler() -> None:
    """起后台循环。**恒开、不设开关** —— 这**六趟**是任务中心的心脏，没有「关掉」这个状态。"""
    await get_scheduler().start()


async def stop_scheduler() -> None:
    """停后台循环（未起过就什么都不做）。"""
    global _scheduler
    if _scheduler is None:
        return
    await _scheduler.stop()
    _scheduler = None


__all__ = [
    "TaskScheduler",
    "get_scheduler",
    "start_scheduler",
    "stop_scheduler",
]
