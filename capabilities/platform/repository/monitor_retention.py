"""monitor 明细表的保留期清理写口。

``monitor.call_log`` / ``monitor.call_attempt`` 是 append-only 明细表，
没有清理机制就会无限增长（050 建表脚本当时明写「不含清理语句，保留期待冻结」）。
本模块补上删除侧，调度形状对齐 ``audit_log_repository.delete_expired``。

口径：

- ``retention_days <= 0`` ⇒ **不清理**（0 = 不自动到期，不是「立即删除」）。
- 判据用 ``created_at``（**非空**，且 ``call_log`` 已有 created_at 索引）：
  业务时间列 ``logical_started_at`` / ``attempt_started_at`` 允许 NULL，
  用它们做 cutoff 会让无时间戳的行永远删不掉。保留期的语义是「记录落库后保留多久」。
- 分批删除（默认每批 1000 条），每批单独 ``commit()`` 释放锁；循环到删 0 行为止。
  单条 DELETE 在积压量大时会长时间持锁并放大 WAL。

⚠️ **本方法会提交调用方的事务**（每批一次 ``commit()``）。只允许在独占
session 上调用（调度器 ``run_once`` 自己开 session）；不要塞进别人的业务事务。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Callable, Dict, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

#: 每批删除行数。与审计清理保持同一量级，避免单事务持锁过久。
DELETE_BATCH_SIZE = 1000

_DELETE_SQL = {
    "call_log": text(
        """
        DELETE FROM monitor.call_log
        WHERE id IN (
            SELECT id FROM monitor.call_log
            WHERE created_at < :cutoff
            ORDER BY created_at, id
            LIMIT :batch
        )
        """
    ),
    "call_attempt": text(
        """
        DELETE FROM monitor.call_attempt
        WHERE id IN (
            SELECT id FROM monitor.call_attempt
            WHERE created_at < :cutoff
            ORDER BY created_at, id
            LIMIT :batch
        )
        """
    ),
}


class MonitorRetentionRepository:
    """删除超过保留期的监控明细行。"""

    def __init__(
        self,
        session: AsyncSession,
        *,
        now_fn: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.session = session
        # 注入口：单测不需要卡在真实时钟上。
        self._now_fn = now_fn or datetime.now

    async def delete_expired(
        self, retention_days: int, batch_size: int = DELETE_BATCH_SIZE
    ) -> Dict[str, int]:
        """删除过期明细，返回 ``{"call_log": n, "call_attempt": n}``。

        ``retention_days <= 0`` 时直接返回 0，不执行任何 SQL。
        """
        result = {"call_log": 0, "call_attempt": 0}
        if retention_days <= 0:
            return result

        cutoff = self._now_fn() - timedelta(days=retention_days)
        for table, stmt in _DELETE_SQL.items():
            result[table] = await self._delete_table(stmt, cutoff, batch_size)
        return result

    async def _delete_table(self, stmt, cutoff: datetime, batch_size: int) -> int:
        total = 0
        while True:
            # 每批一个事务：删完立刻释放锁与 WAL。
            outcome = await self.session.execute(
                stmt, {"cutoff": cutoff, "batch": batch_size}
            )
            await self.session.commit()
            deleted = outcome.rowcount or 0
            total += deleted
            if deleted < batch_size:
                break
            # 让出事件循环：连续大批量删除不饿死同进程的读请求。
            await asyncio.sleep(0)
        return total
