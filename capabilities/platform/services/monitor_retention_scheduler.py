"""监控明细保留清理调度器（闹钟）。

背景：``monitor.call_log`` / ``monitor.call_attempt`` 是 append-only 明细表，
没有清理机制就会无限增长。本模块按固定间隔调用
``MonitorRetentionRepository.delete_expired``。

形状对齐 ``audit_retention_scheduler``：

- 公开 ``run_once()`` 供单测直接调用，不必等定时器；
- 构造器把 ``interval`` / ``session_factory`` / ``repository_factory`` 做成注入口，
  单测不依赖真实 DB；
- ``start()`` 幂等、``stop()`` 未启动也安全、``is_running`` 只读运行态；
- ``_loop`` **首轮立即跑**，一轮异常只记 warning，不退出循环；
- ``interval <= 0`` = 显式关闭（不启动后台任务）。

## 单副本假设（无分布式互斥）

同审计调度器：当前部署形态是单容器单进程。清理按 cutoff 幂等，多副本同时执行
不会误删，只会重复扫表、放大 WAL。本期不做分布式互斥。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncContextManager, Callable, Optional, Tuple

from jonex_core.common.audit import schedule_emit
from jonex_core.common.config import get_config
from jonex_core.common.database import get_db_session

logger = logging.getLogger(__name__)

#: 系统审计条目的操作名。保留天数为 0（关闭）时不会产生任何审计。
PURGE_ACTION = "system.monitor.retention.purge"


class MonitorRetentionScheduler:
    """监控明细保留清理后台循环。"""

    def __init__(
        self,
        *,
        interval: Optional[float] = None,
        session_factory: Optional[Callable[[], AsyncContextManager[Any]]] = None,
        repository_factory: Optional[Callable[[Any], Any]] = None,
    ) -> None:
        config = get_config()
        self._interval = float(
            interval
            if interval is not None
            else config.MONITOR_RETENTION_SCHEDULER_INTERVAL_SECONDS
        )
        self._session_factory = session_factory or get_db_session
        self._repository_factory = repository_factory or self._default_factory
        self._task: Optional[asyncio.Task] = None

    @staticmethod
    def _default_factory(session: Any) -> Any:
        from ..repository.monitor_retention import MonitorRetentionRepository

        return MonitorRetentionRepository(session)

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> bool:
        if self._interval <= 0:
            logger.info(
                "[MonitorRetention] 调度器已关闭"
                "（MONITOR_RETENTION_SCHEDULER_INTERVAL_SECONDS<=0）"
            )
            return False
        if self.is_running:
            return True
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "[MonitorRetention] 监控明细清理调度器已启动（间隔 %.1fs）",
            self._interval,
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
            logger.info("[MonitorRetention] 监控明细清理调度器已停止")

    async def _loop(self) -> None:
        first_run = True
        while True:
            try:
                if not first_run:
                    await asyncio.sleep(self._interval)
                first_run = False
                deleted, retention_days, detail = await self.run_once()
                if deleted > 0:
                    logger.info(
                        "[MonitorRetention] 本轮删除 %d 条过期监控明细"
                        "（call_log=%d, call_attempt=%d）",
                        deleted,
                        detail["call_log"],
                        detail["call_attempt"],
                    )
                    self._emit_purge_audit(detail, retention_days)
            except asyncio.CancelledError:
                raise
            except Exception:  # 一轮失败不该让循环死掉
                logger.warning(
                    "[MonitorRetention] 本轮清理出错，跳过本轮", exc_info=True
                )

    async def run_once(self) -> Tuple[int, int, dict]:
        """跑一轮清理，返回 ``(删除总数, 保留天数, 分表明细)``。"""
        days = int(get_config().MONITOR_RETENTION_DAYS)
        async with self._session_factory() as session:
            repository = self._repository_factory(session)
            detail = await repository.delete_expired(days)
        return sum(detail.values()), days, detail

    @staticmethod
    def _emit_purge_audit(detail: dict, retention_days: int) -> None:
        """写一条系统审计（无租户，``record_system``）。失败不影响清理。"""
        schedule_emit(
            {
                "user_id": None,
                "username": "monitor-retention-scheduler",
                "log_type": "SYSTEM",
                "action": PURGE_ACTION,
                "outcome": "SUCCESS",
                "service_name": "platform",
                "request_params": {
                    "deleted": sum(detail.values()),
                    "call_log": detail.get("call_log", 0),
                    "call_attempt": detail.get("call_attempt", 0),
                    "retention_days": retention_days,
                },
            }
        )


# ============================================================
# 进程内单例（能力模块 initialize / shutdown 时用）
# ============================================================

_scheduler: Optional[MonitorRetentionScheduler] = None


def get_scheduler() -> MonitorRetentionScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = MonitorRetentionScheduler()
    return _scheduler


async def start_scheduler() -> Optional[MonitorRetentionScheduler]:
    scheduler = get_scheduler()
    return scheduler if await scheduler.start() else None


async def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is None:
        return
    await _scheduler.stop()
    _scheduler = None
