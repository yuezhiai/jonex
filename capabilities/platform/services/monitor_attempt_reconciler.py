"""监控调用次数对账调度器（闹钟）。

背景：``monitor.call_log``（逻辑调用主记录，含 ``attempt_count``）与
``monitor.call_attempt``（物理尝试记录，每次尝试一行）由不同进程写入。两个
写事务时间重叠时互相看不到对方未提交的行，双方各自的「批内补正」只会跑一次且
都落空，``attempt_count`` 因而永久失配（实测入站 2311 行坏 113 行）。

本模块按固定间隔（默认 30s）把**最近窗口**（默认 10 分钟）的
``attempt_count`` 收敛到 ``call_attempt`` 的实际行数 —— 这是**跨进程竞态的最终一致性兜底**，
不是主写入路径：正常写入仍由各自的补正逻辑负责，调度器只负责「事后把账抹平」。
历史存量数据由另一份一次性 SQL 回填，不在本模块范围内。

形状对齐 ``monitor_retention_scheduler``：

- 公开 ``run_once()`` 供单测直接调用，不必等定时器；
- 构造器把 ``interval`` / ``lookback_minutes`` / ``session_factory`` /
  ``repository_factory`` / ``now_fn`` 做成注入口，单测不依赖真实 DB 与真实时钟；
- ``start()`` 幂等、``stop()`` 未启动也安全、``is_running`` 只读运行态；
- ``_loop`` **首轮立即跑**，一轮异常只记 warning，不退出循环；
- ``interval <= 0`` = 显式关闭（不启动后台任务）；
- 不写审计（对账是运维收敛动作，不是用户可感知的业务操作）。

## 单副本假设（无分布式互斥）

当前部署形态是 platform 单容器单进程。对账按 ``since`` 窗口幂等（把窗口内行
重算成 ``call_attempt`` 行数），多副本同时执行不会算错，只会重复扫表；本期不做
分布式互斥。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncContextManager, Callable, Optional

from jonex_core.common.config import get_config
from jonex_core.common.database import get_db_session

logger = logging.getLogger(__name__)


class MonitorAttemptReconcileScheduler:
    """``attempt_count`` 周期对账后台循环。"""

    def __init__(
        self,
        *,
        interval: Optional[float] = None,
        lookback_minutes: Optional[int] = None,
        session_factory: Optional[Callable[[], AsyncContextManager[Any]]] = None,
        repository_factory: Optional[Callable[[Any], Any]] = None,
        now_fn: Optional[Callable[[], datetime]] = None,
    ) -> None:
        config = get_config()
        self._interval = float(
            interval
            if interval is not None
            else config.MONITOR_ATTEMPT_RECONCILE_INTERVAL_SECONDS
        )
        self._lookback_minutes = int(
            lookback_minutes
            if lookback_minutes is not None
            else config.MONITOR_ATTEMPT_RECONCILE_LOOKBACK_MINUTES
        )
        self._session_factory = session_factory or get_db_session
        self._repository_factory = repository_factory or self._default_factory
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._task: Optional[asyncio.Task] = None

    @staticmethod
    def _default_factory(session: Any) -> Any:
        # 延迟导入：单测注入假工厂时不依赖仓储模块存在。
        from ..repository.monitor_reconcile import MonitorReconcileRepository

        return MonitorReconcileRepository(session)

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> bool:
        if self._interval <= 0:
            logger.info(
                "[MonitorAttemptReconcile] 调度器已关闭"
                "（MONITOR_ATTEMPT_RECONCILE_INTERVAL_SECONDS<=0）"
            )
            return False
        if self.is_running:
            return True
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "[MonitorAttemptReconcile] attempt_count 对账调度器已启动"
            "（间隔 %.1fs，回看 %d 分钟）",
            self._interval,
            self._lookback_minutes,
        )
        return True

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            logger.info("[MonitorAttemptReconcile] 调度器已停止")

    async def _loop(self) -> None:
        first_run = True
        while True:
            try:
                if not first_run:
                    await asyncio.sleep(self._interval)
                first_run = False
                fixed = await self.run_once()
                if fixed > 0:
                    logger.info(
                        "[MonitorAttemptReconcile] 本轮修正 %d 行 attempt_count"
                        "（回看 %d 分钟）",
                        fixed,
                        self._lookback_minutes,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:  # 一轮失败不该让循环死掉
                logger.warning(
                    "[MonitorAttemptReconcile] 本轮对账出错，跳过本轮", exc_info=True
                )

    async def run_once(self) -> int:
        """跑一轮对账，返回修正的行数。"""
        since = self._now_fn() - timedelta(minutes=self._lookback_minutes)
        async with self._session_factory() as session:
            repository = self._repository_factory(session)
            return await repository.reconcile_attempt_counts(since)


# ============================================================
# 进程内单例（能力模块 initialize / shutdown 时用）
# ============================================================

_scheduler: Optional[MonitorAttemptReconcileScheduler] = None


def get_scheduler() -> MonitorAttemptReconcileScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = MonitorAttemptReconcileScheduler()
    return _scheduler


async def start_scheduler() -> Optional[MonitorAttemptReconcileScheduler]:
    scheduler = get_scheduler()
    return scheduler if await scheduler.start() else None


async def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is None:
        return
    await _scheduler.stop()
    _scheduler = None
