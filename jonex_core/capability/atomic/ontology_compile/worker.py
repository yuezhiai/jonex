# jonex_core/capability/atomic/ontology_compile/worker.py
# -*- coding: utf-8 -*-
"""[jonex] 编译任务 worker（P2）：轮询 PG 取 queued → 跑编译 → 落终态。

**为什么轮询而非内存队列**：设计 §4.6 要求「服务重启：queued/processing 任务可续跑」，
轮询 PG 天然满足；内存队列则要在进程外再维护一份镜像。

**「待跑」状态用 `queued` 而非 `pending`**：`TaskStatus` 枚举只有
`CREATED/QUEUED/PROCESSING/COMPLETED/FAILED/CANCELLED`（值对齐 raganything，逐字不动），
`pending` 在阶段 1 的模型里**不存在**——按新枚举值实现会需要改枚举，故统一取 `QUEUED`。

**孤儿复位**：worker 崩溃时任务停在 `processing`，下次启动必须能拾起 —— 用
`updated_at` 超时判据（`stale_after_seconds`）区分「崩溃遗留」与「别的 worker 正在跑」。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from .deps import build_deps
from .service import run_ontology_only
from .task_model import STATUS_COMPLETED, TaskStatus

logger = logging.getLogger(__name__)


class CompileWorker:
    """编译任务 worker。`run_once()` 供测试同步驱动；`start()/stop()` 供容器常驻。"""

    def __init__(self, store, *, concurrency: int = 2, poll_interval: float = 2.0,
                 stale_after_seconds: int = 1800) -> None:
        self._store = store
        self._concurrency = max(1, int(concurrency))
        self._poll_interval = float(poll_interval)
        self._stale_after = int(stale_after_seconds)
        self._tasks: list[asyncio.Task] = []
        self._stopping = asyncio.Event()

    # ── 生命周期 ──────────────────────────────────────────────
    async def start(self) -> None:
        await self._reset_stale()
        self._stopping.clear()
        for i in range(self._concurrency):
            self._tasks.append(asyncio.create_task(self._loop(i), name=f"oc-worker-{i}"))
        logger.info("compile worker 已启动 concurrency=%d poll=%ss", self._concurrency, self._poll_interval)

    async def stop(self) -> None:
        self._stopping.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("compile worker 已停止")

    # ── 主循环 ────────────────────────────────────────────────
    async def _loop(self, idx: int) -> None:
        while not self._stopping.is_set():
            try:
                n = await self.run_once()
            except Exception:  # noqa: BLE001 — 循环不能因单轮异常退出
                logger.exception("compile worker #%d 轮询异常", idx)
                n = 0
            if n == 0:
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=self._poll_interval)
                except asyncio.TimeoutError:
                    pass

    async def run_once(self) -> int:
        """**原子认领**一条待跑任务并跑完。返回处理条数（0 表示这轮没活）。"""
        task = await self._store.claim_one(TaskStatus.QUEUED.value)
        if task is None:
            return 0

        deps = None
        try:
            deps = build_deps(task_store=self._store)
            cancel_event = asyncio.Event()
            await run_ontology_only(deps, task, cancel_event)
            task.status = STATUS_COMPLETED
            task.progress = 1.0
            task.current_step = "done"
        except Exception as exc:  # noqa: BLE001 — 失败必须落终态，不能只打日志
            task.status = TaskStatus.FAILED
            task.ontology_status = "failed"
            task.ontology_error = str(exc)[:500]
            logger.error("compile 任务失败 task=%s: %s", task.task_id, exc)
        finally:
            # 嵌套 try/finally：save 抛错也必须 aclose（否则每任务漏一个连接池）
            try:
                task.updated_at = datetime.now(timezone.utc)
                task.completed_at = task.updated_at
                await self._store.save(task)
            finally:
                if deps is not None:
                    await deps.aclose()
        return 1

    # ── 孤儿复位 ──────────────────────────────────────────────
    async def _reset_stale(self) -> None:
        rows = await self._store.list_by_status(TaskStatus.PROCESSING.value, limit=200)
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self._stale_after)
        for t in rows:
            updated = t.updated_at
            if updated is not None and updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            if updated is None or updated < cutoff:
                t.status = TaskStatus.QUEUED
                t.updated_at = datetime.now(timezone.utc)
                await self._store.save(t)
                logger.warning("复位陈旧 processing 任务 task=%s", t.task_id)
