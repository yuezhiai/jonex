import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jonex_wiki.kernel.errors import ConflictError, NotFoundError
from jonex_wiki.kernel.task import TaskKind, TaskStatus
from jonex_wiki.storage.models import QueryTaskRow, TaskRow, _now
from jonex_wiki.storage.repos import QueryTaskRepo
from jonex_wiki.storage.layout import Layout

log = logging.getLogger(__name__)
_ADVISORY_KEY = 730203  # 固定 app 级锁 key(spec §8)


class TaskExecutor:
    def __init__(self, session_factory: async_sessionmaker, settings,
                 parse_runner: Callable[[TaskRow], Awaitable[None]] | None = None,
                 compile_runner: Callable[[TaskRow], Awaitable[None]] | None = None,
                 query_runner: Callable[[QueryTaskRow], Awaitable[dict]] | None = None,
                 outline_runner: Callable[[TaskRow], Awaitable[None]] | None = None):
        self.session_factory = session_factory
        self.settings = settings
        self.parse_runner = parse_runner
        self.compile_runner = compile_runner
        self.query_runner = query_runner
        self.outline_runner = outline_runner
        self._parse_sem = asyncio.Semaphore(settings.api_parse_concurrency)
        self._compile_sem = asyncio.Semaphore(settings.api_compile_concurrency)
        self._query_sem = asyncio.Semaphore(settings.api_query_concurrency)
        self._outline_sem = asyncio.Semaphore(settings.api_outline_concurrency)
        self._cancel_flags: dict[str, asyncio.Event] = {}
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._layout = Layout(settings.data_root)
        self._resume_events: dict[tuple[str, str], asyncio.Event] = {}
        self._pause_locks: dict[str, asyncio.Lock] = {}
        self._compile_slot_held: dict[str, bool] = {}

    @property
    def query_semaphore(self) -> asyncio.Semaphore:
        return self._query_sem

    def submit(self, row: TaskRow) -> None:
        if row.task_id in self._running_tasks:
            return
        self._cancel_flags[row.task_id] = asyncio.Event()
        self._running_tasks[row.task_id] = asyncio.create_task(self._run(row))

    def submit_query(self, row: QueryTaskRow, *, thinking_mode: str | None = None) -> None:
        """提交后台查询任务（独立 query_tasks 表,不复用 tasks 生命周期）。"""
        self._cancel_flags[row.query_id] = asyncio.Event()
        self._running_tasks[row.query_id] = asyncio.create_task(
            self._run_query(row, thinking_mode=thinking_mode))

    def _dispatch(self, row: TaskRow) -> tuple:
        """kind → (runner, semaphore, 运行中状态)。outline 用独立 sem（不占编译槽）。"""
        if row.kind == TaskKind.parse.value:
            return self.parse_runner, self._parse_sem, TaskStatus.parsing.value
        if row.kind == TaskKind.outline.value:
            return self.outline_runner, self._outline_sem, TaskStatus.running.value
        return self.compile_runner, self._compile_sem, TaskStatus.compiling.value

    async def _run(self, row: TaskRow) -> None:
        runner, sem, running_status = self._dispatch(row)
        owns_slot = row.kind == TaskKind.compile.value and getattr(runner, "owns_compile_slot", False)
        try:
            if runner is None:
                async with self.session_factory() as s:
                    await self._transition(s, row.task_id, TaskStatus.failed.value, error="runner not wired")
                    await s.commit()
                return
            if row.kind == TaskKind.compile.value:
                await self.wait_compile_resume(row.task_id, row.tenant_id, row.kb_id)
            if not owns_slot:
                await sem.acquire()
            if row.kind == TaskKind.compile.value and not owns_slot:
                self._compile_slot_held[row.task_id] = True
            try:
                # A compile waiting for its KB's compile lock must remain
                # queued so later parse completions can coalesce into it.
                # The compile runner marks it compiling after taking that lock.
                if row.kind != TaskKind.compile.value:
                    async with self.session_factory() as s:
                        await self._transition(s, row.task_id, running_status)
                        await s.commit()
                try:
                    await runner(row)
                except asyncio.CancelledError:
                    async with self.session_factory() as s:
                        interrupted_pause = (row.kind == TaskKind.compile.value
                            and self._layout.compilation_paused(row.tenant_id, row.kb_id)
                            and not self.is_cancelled(row.task_id))
                        await self._transition(s, row.task_id,
                            TaskStatus.paused.value if interrupted_pause else TaskStatus.cancelled.value,
                            error=None if interrupted_pause else "cancelled")
                        await s.commit()
                    raise
                except Exception as e:  # noqa: BLE001 —— 后台任务边界,一切异常转 failed
                    log.exception("task failed task_id=%s", row.task_id)
                    async with self.session_factory() as s:
                        await self._transition(s, row.task_id, TaskStatus.failed.value,
                                               error=f"{type(e).__name__}: {e}"[:512])
                        await s.commit()
                else:
                    async with self.session_factory() as s:
                        await self._transition(s, row.task_id, TaskStatus.done.value)
                        await s.commit()
            finally:
                if row.kind != TaskKind.compile.value or self._compile_slot_held.pop(row.task_id, False):
                    sem.release()
        finally:
            self._cancel_flags.pop(row.task_id, None)
            self._running_tasks.pop(row.task_id, None)
            self._pause_locks.pop(row.task_id, None)

    async def wait_compile_resume(self, task_id: str, tenant_id: str, kb_id: str) -> None:
        """Pause at an async boundary, retaining the current coroutine and checkpoints.

        An active paused compile keeps its KB lock but releases the global
        compile slot. Parallel model workers share one slot transition.
        """
        async with self._pause_locks.setdefault(task_id, asyncio.Lock()):
            if not self._layout.compilation_paused(tenant_id, kb_id):
                return
            event = self._resume_events.setdefault((tenant_id, kb_id), asyncio.Event())
            event.clear()
            active = task_id in self._compile_slot_held
            if active and self._compile_slot_held[task_id]:
                self._compile_slot_held[task_id] = False
                self._compile_sem.release()
            while self._layout.compilation_paused(tenant_id, kb_id):
                await event.wait()
                event.clear()
            if active:
                await self._compile_sem.acquire()
                self._compile_slot_held[task_id] = True
                async with self.session_factory() as s:
                    await self._transition(s, task_id, TaskStatus.compiling.value)
                    await s.commit()

    async def acquire_compile_slot(self, task_id: str) -> None:
        # Called after taking the KB lock. A queued follow-up must never take
        # the slot that the suspended owner needs to resume.
        await self._compile_sem.acquire()
        self._compile_slot_held[task_id] = True

    async def set_compilation_paused(self, tenant_id: str, kb_id: str, paused: bool) -> dict:
        self._layout.set_compilation_paused(tenant_id, kb_id, paused)
        async with self.session_factory() as s:
            rows = (await s.execute(select(TaskRow).where(
                TaskRow.tenant_id == tenant_id, TaskRow.kb_id == kb_id,
                TaskRow.kind == TaskKind.compile.value,
                TaskRow.status.in_([TaskStatus.queued.value, TaskStatus.compiling.value,
                                   TaskStatus.paused.value])))).scalars().all()
            for row in rows:
                if paused and row.status == TaskStatus.compiling.value:
                    row.status = TaskStatus.paused.value
                elif not paused and row.status == TaskStatus.paused.value:
                    # Do not collide with the separate coalesced queued task.
                    row.status = TaskStatus.compiling.value
            await s.commit()
        if not paused:
            self._resume_events.setdefault((tenant_id, kb_id), asyncio.Event()).set()
            for row in sorted(rows, key=lambda row: row.status == TaskStatus.queued.value):
                self.submit(row)
        return {"kb_id": kb_id, "paused": paused,
                "task_ids": [row.task_id for row in rows]}

    async def _run_query(self, row: QueryTaskRow, *, thinking_mode: str | None = None) -> None:
        """后台查询生命周期(§6.3/§6.4):queued→running→done/failed/cancelled。

        query_tasks 只读高频、无 stage 恢复重放;不落 tasks 表。
        """
        try:
            if self.query_runner is None:
                async with self.session_factory() as s:
                    await self._transition_query(s, row.tenant_id, row.query_id,
                                                 TaskStatus.failed.value, error="query runner not wired")
                    await s.commit()
                return
            async with self._query_sem:
                async with self.session_factory() as s:
                    await self._transition_query(s, row.tenant_id, row.query_id,
                                                 TaskStatus.running.value)
                    await s.commit()
                try:
                    result = (await self.query_runner(row) if thinking_mode is None else
                              await self.query_runner(row, thinking_mode=thinking_mode))
                except asyncio.CancelledError:
                    async with self.session_factory() as s:
                        await self._transition_query(s, row.tenant_id, row.query_id,
                                                     TaskStatus.cancelled.value, error="cancelled")
                        await s.commit()
                    raise
                except Exception as e:  # noqa: BLE001
                    log.exception("query failed query_id=%s", row.query_id)
                    async with self.session_factory() as s:
                        await self._transition_query(s, row.tenant_id, row.query_id,
                                                     TaskStatus.failed.value,
                                                     error=f"{type(e).__name__}: {e}"[:512])
                        await s.commit()
                else:
                    async with self.session_factory() as s:
                        await self._transition_query(s, row.tenant_id, row.query_id,
                                                     TaskStatus.done.value, result=result)
                        await s.commit()
        finally:
            self._cancel_flags.pop(row.query_id, None)
            self._running_tasks.pop(row.query_id, None)

    async def _transition_query(self, s: AsyncSession, tenant_id: str, query_id: str,
                                status: str, *, error: str | None = None,
                                result: dict | None = None) -> None:
        row = await QueryTaskRepo().get(s, tenant_id, query_id)
        if row is None:
            return
        if row.status in (TaskStatus.done.value, TaskStatus.failed.value, TaskStatus.cancelled.value):
            return
        row.status = status
        if error is not None:
            row.error = error
        if status == TaskStatus.running.value and row.started_at is None:
            row.started_at = _now()
        if status in (TaskStatus.done.value, TaskStatus.failed.value, TaskStatus.cancelled.value):
            row.finished_at = _now()
        if result is not None:
            row.result = result

    async def set_stage(self, task_id: str, stage: str, *, summary: dict | None = None) -> None:
        """供 runner 更新细粒度阶段(spec §3.3/§8);自开短 session。"""
        async with self.session_factory() as s:
            row = await s.get(TaskRow, task_id)
        if row is not None and row.kind == TaskKind.compile.value:
            await self.wait_compile_resume(task_id, row.tenant_id, row.kb_id)
        async with self.session_factory() as s:
            await self._transition(s, task_id, TaskStatus.compiling.value,
                                   stage=stage, summary=summary)
            await s.commit()

    async def claim_compile(self, task_id: str) -> bool:
        """Mark a queued compile as running only after its KB lock is acquired."""
        async with self.session_factory() as s:
            result = await s.execute(update(TaskRow).where(
                TaskRow.task_id == task_id,
                TaskRow.kind == TaskKind.compile.value,
                TaskRow.status.in_([TaskStatus.queued.value, TaskStatus.compiling.value]),
            ).values(status=TaskStatus.compiling.value, stage="sources"))
            await s.commit()
        return result.rowcount == 1

    async def _transition(self, s: AsyncSession, task_id: str, status: str, *, error: str | None = None,
                          stage: str | None = None, summary: dict | None = None) -> None:
        row = await s.get(TaskRow, task_id)
        if row is None:
            return
        if row.status in (TaskStatus.done.value, TaskStatus.failed.value, TaskStatus.cancelled.value):
            return  # 终态不回退
        if (status == TaskStatus.compiling.value and row.kind == TaskKind.compile.value
                and self._layout.compilation_paused(row.tenant_id, row.kb_id)):
            status = TaskStatus.paused.value
        row.status = status
        if error is not None:
            row.error = error
        if stage is not None:
            row.stage = stage
        if summary is not None:
            row.summary = {**(row.summary or {}), **summary}

    async def cancel(self, session: AsyncSession, tenant_id: str, task_id: str) -> TaskRow:
        row = await session.get(TaskRow, task_id)
        if row is None or row.tenant_id != tenant_id:
            raise NotFoundError(f"任务 {task_id} 不存在")
        if row.status in (TaskStatus.done.value, TaskStatus.failed.value, TaskStatus.cancelled.value):
            raise ConflictError(f"任务已处于终态 {row.status},不可取消")
        if row.status == TaskStatus.queued.value:
            row.status = TaskStatus.cancelled.value
            row.error = "cancelled"
        elif task_id not in self._running_tasks:
            row.status = TaskStatus.cancelled.value
            row.error = "cancelled"
        else:
            flag = self._cancel_flags.get(task_id)
            if flag is not None:
                flag.set()
            task = self._running_tasks.get(task_id)
            if task is not None:
                task.cancel()   # 触发 CancelledError → _run 转 cancelled(spec §8 协作式取消)
        await session.commit()
        return row

    def is_cancelled(self, task_id: str) -> bool:
        flag = self._cancel_flags.get(task_id)
        return bool(flag and flag.is_set())

    async def cancel_query(self, session: AsyncSession, tenant_id: str, query_id: str) -> QueryTaskRow:
        row = await QueryTaskRepo().get(session, tenant_id, query_id)
        if row is None:
            raise NotFoundError(f"任务 {query_id} 不存在")
        if row.status in (TaskStatus.done.value, TaskStatus.failed.value, TaskStatus.cancelled.value):
            return row
        row.status = TaskStatus.cancelled.value
        row.error = "cancelled"
        row.finished_at = _now()
        await session.commit()
        flag = self._cancel_flags.get(query_id)
        if flag is not None:
            flag.set()
        task = self._running_tasks.get(query_id)
        if task is not None:
            task.cancel()
        return row

    async def cancel_kb_queries(self, tenant_id: str, kb_id: str) -> None:
        async with self.session_factory() as s:
            rows = (await s.execute(select(QueryTaskRow).where(
                QueryTaskRow.tenant_id == tenant_id, QueryTaskRow.kb_id == kb_id,
                QueryTaskRow.status.in_([TaskStatus.queued.value, TaskStatus.running.value])))).scalars().all()
            for row in rows:
                row.status = TaskStatus.cancelled.value
                row.error = "source deleted during query"
                row.finished_at = _now()
                flag = self._cancel_flags.get(row.query_id)
                if flag is not None:
                    flag.set()
                task = self._running_tasks.get(row.query_id)
                if task is not None:
                    task.cancel()
            await s.commit()

    def check_cancel(self, task_id: str) -> None:
        """domain 层检查点:spec §8——编译在分配批次与页面任务边界、解析在轮询之间调用。"""
        if self.is_cancelled(task_id):
            raise asyncio.CancelledError()

    async def recover(self, session: AsyncSession) -> None:
        """spec §3.3 启动恢复:中断态标记 failed(interrupted);queued 重新入队。"""
        rows = (await session.execute(select(TaskRow))).scalars().all()
        requeue: list[TaskRow] = []
        for row in rows:
            if row.kind == TaskKind.compile.value and self._layout.compilation_paused(row.tenant_id, row.kb_id):
                if row.status == TaskStatus.compiling.value:
                    row.status = TaskStatus.paused.value
                continue
            if row.status in (TaskStatus.parsing.value, TaskStatus.compiling.value,
                              TaskStatus.running.value):
                row.status = TaskStatus.failed.value
                row.error = f"interrupted: {row.status} 时服务重启"
            elif row.status == TaskStatus.queued.value:
                requeue.append(row)
        await session.flush()
        for row in requeue:
            self.submit(row)

    async def recover_queries(self, session: AsyncSession) -> None:
        """query_tasks 启动恢复:running → failed(interrupted);queued 不重放(丢弃)。"""
        rows = (await session.execute(select(QueryTaskRow))).scalars().all()
        for row in rows:
            if row.status == TaskStatus.running.value:
                row.status = TaskStatus.failed.value
                row.error = "interrupted: running 时服务重启"
                row.finished_at = _now()
        await session.flush()

    async def purge_expired_queries(self, session: AsyncSession, *, ttl_seconds: int) -> int:
        """启动 GC:清除 done/failed/cancelled 且 finished_at 早于 TTL 窗口的 query_tasks 行。"""
        from jonex_wiki.storage.repos import QueryTaskRepo
        before = _now() - timedelta(seconds=ttl_seconds)
        return await QueryTaskRepo().delete_expired(session, before=before)
