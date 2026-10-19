"""审计保留清理调度器（闹钟）。

背景：``AUDIT_RETENTION_DAYS`` 与 ``AuditLogService.cleanup_expired()`` /
``AuditLogRepository.delete_expired()`` 早已存在，但**全仓没有任何调用方** ——
保留天数是个死配置，审计表只会无限增长。本模块补上「闹钟」：按固定间隔调用
清理服务。

形状对齐 ``identity_sync_scheduler`` / ``retention_scheduler``：

- 公开 ``run_once()`` 供单测直接调用，不必等定时器；
- 构造器把 ``interval`` / ``session_factory`` / ``service_factory`` 做成注入口，
  单测不依赖真实 DB；
- ``start()`` 幂等、``stop()`` 未启动也安全、``is_running`` 只读运行态；
- ``_loop`` **首轮立即跑**，一轮异常只记 warning，不退出循环。

``interval <= 0`` = 显式关闭（不启动后台任务），用于本地/测试环境关掉清理。

## 单副本假设（无分布式互斥）

本调度器**假定单容器单进程**（当前部署形态）。清理按 cutoff 幂等，多个副本同时
执行不会误删，只会重复扫表、放大 WAL。若未来要多副本互斥，**不能**在本 session
上直接加 session-level advisory lock：``delete_expired`` 每批 ``commit()`` 会把
连接归还池，``finally`` 的 unlock 可能落在另一条连接上 ⇒ 锁泄漏在池里、后续所有
轮次静默跳过。正确做法是整轮持有**独立连接**（``engine.connect()`` + ``finally``
unlock 并检查返回值），或改用带租约的 CAS 行锁。本期不做。

## 可观测性

``_loop`` 不丢弃 ``run_once()`` 的返回值：删除条数 > 0 时打 INFO，并写一条
``log_type=SYSTEM`` 的系统审计（``tenant_id=None`` ⇒ ``record_system``）。
``retention_days`` 在同一轮只读一次，清理与审计用同一个值。
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
PURGE_ACTION = "system.audit.retention.purge"


class AuditRetentionScheduler:
    """审计保留清理后台循环：每隔 ``interval`` 秒删一次过期审计日志。"""

    def __init__(
        self,
        *,
        interval: Optional[float] = None,
        session_factory: Optional[Callable[[], AsyncContextManager[Any]]] = None,
        service_factory: Optional[Callable[[Any], Any]] = None,
    ) -> None:
        config = get_config()
        self._interval = float(
            interval
            if interval is not None
            else config.AUDIT_RETENTION_SCHEDULER_INTERVAL_SECONDS
        )
        # 两个注入口：单测替换真实 DB / 真实服务。
        self._session_factory = session_factory or get_db_session
        self._service_factory = service_factory or self._default_service_factory
        self._task: Optional[asyncio.Task] = None

    @staticmethod
    def _default_service_factory(session: Any) -> Any:
        """默认服务工厂（惰性 import，避免模块导入期循环依赖）。"""
        from .audit_log_service import AuditLogService

        return AuditLogService(session)

    # ── 生命周期 ──────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        """后台循环是否在跑（未启动 / 已停止 / 已退出都返回 False）。"""
        return self._task is not None and not self._task.done()

    async def start(self) -> bool:
        """起循环，返回是否处于运行态。

        - ``interval <= 0``：显式关闭，返回 False，不起后台任务；
        - 已在跑：幂等，返回 True。
        """
        if self._interval <= 0:
            logger.info(
                "[AuditRetention] 调度器已关闭"
                "（AUDIT_RETENTION_SCHEDULER_INTERVAL_SECONDS<=0）"
            )
            return False
        if self.is_running:
            return True
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "[AuditRetention] 审计保留清理调度器已启动（间隔 %.1fs）",
            self._interval,
        )
        return True

    async def stop(self) -> None:
        """停循环（未起过就什么都不做）。"""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            logger.info("[AuditRetention] 审计保留清理调度器已停止")

    async def _loop(self) -> None:
        first_run = True
        while True:
            try:
                if not first_run:
                    await asyncio.sleep(self._interval)
                first_run = False
                deleted, retention_days = await self.run_once()
                if deleted > 0:
                    logger.info(
                        "[AuditRetention] 本轮删除 %d 条过期审计日志", deleted
                    )
                    self._emit_purge_audit(deleted, retention_days)
            except asyncio.CancelledError:
                raise
            except Exception:  # 一轮失败不该让循环死掉，下一轮接着来
                logger.warning(
                    "[AuditRetention] 本轮清理出错，跳过本轮", exc_info=True
                )

    # ── 一轮 ──────────────────────────────────────────────

    async def run_once(self) -> Tuple[int, int]:
        """跑一轮清理，返回 ``(删除条数, 保留天数)``。单测直接调，不必等定时器。

        ``retention_days`` 只在这里读一次：清理与随后的系统审计共用同一个值，
        避免配置在中途变化导致「删了 90 天、审计写了 30 天」。

        ⚠️ 无分布式互斥，假定单副本（见模块 docstring）。
        """
        days = int(get_config().AUDIT_RETENTION_DAYS)
        async with self._session_factory() as session:
            service = self._service_factory(session)
            deleted = await service.cleanup_expired(days)
        return deleted, days

    @staticmethod
    def _emit_purge_audit(deleted: int, retention_days: int) -> None:
        """写一条系统审计（无租户，``record_system``）。调度失败不影响清理。"""
        schedule_emit(
            {
                "user_id": None,
                "username": "audit-retention-scheduler",
                "log_type": "SYSTEM",
                "action": PURGE_ACTION,
                "outcome": "SUCCESS",
                "service_name": "platform",
                "request_params": {
                    "deleted": deleted,
                    "retention_days": retention_days,
                },
            }
        )


# ============================================================
# 进程内单例（能力模块 initialize / shutdown 时用）
# ============================================================

_scheduler: Optional[AuditRetentionScheduler] = None


def get_scheduler() -> AuditRetentionScheduler:
    """取本进程的审计保留清理调度器（首次调用时创建）。"""
    global _scheduler
    if _scheduler is None:
        _scheduler = AuditRetentionScheduler()
    return _scheduler


async def start_scheduler() -> Optional[AuditRetentionScheduler]:
    """起调度器；``interval <= 0`` 时返回 None（显式关闭）。"""
    scheduler = get_scheduler()
    return scheduler if await scheduler.start() else None


async def stop_scheduler() -> None:
    """停调度器（未起过就什么都不做）。"""
    global _scheduler
    if _scheduler is None:
        return
    await _scheduler.stop()
    _scheduler = None


__all__ = [
    "AuditRetentionScheduler",
    "PURGE_ACTION",
    "get_scheduler",
    "start_scheduler",
    "stop_scheduler",
]
