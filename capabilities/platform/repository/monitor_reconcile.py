"""monitor ``call_log.attempt_count`` 的周期对账写口。

不变量：``monitor.call_log.attempt_count`` 必须等于该 ``request_id`` 在
``monitor.call_attempt`` 中的行数（一行 = 一次物理尝试）。但两张表由**不同进程**
写入：``call_log``（逻辑调用主记录）由 sidecar 写，``call_attempt``（物理尝试
记录）由 gateway 写。两个写事务在时间上重叠（实测 2~15ms）时互相看不到对方
未提交的行，各批只做一次「补正」，于是 ``attempt_count`` 可能永久停在 0 ——
重试发生在 ``call_log`` 已落库之后，再没有第二次机会修正。

本模块提供一条**周期对账**语句，把最近窗口内的 ``attempt_count`` 收敛到实际
``call_attempt`` 行数。口径：

- **只升不降**：``GREATEST(attempt_count, cnt)`` + ``attempt_count < cnt`` 闸门。
  对账不与实时写入竞争，也不会把采集器刚写入的更大值抹回旧值；重复执行结果不变
  （幂等）。
- **只改最近窗口**：两张表都按 ``created_at`` 过滤（两表均有 created_at 索引），
  不用可空的 ``logical_started_at``（空值会让该行永远对不上）。
- **边界**：回看窗口（lookback）之外的历史偏差不在这里处理，由一次性回填 SQL
  负责；本方法只保证「最近还在被读到的数据」是对的。
- **不提交事务**：session 生命周期由调用方 ``get_db_session()`` 管理。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

#: 单条幂等 UPDATE：只把偏小的 attempt_count 抬到实际行数。
_RECONCILE_SQL = text(
    """
    UPDATE monitor.call_log cl
    SET attempt_count = GREATEST(cl.attempt_count, sub.cnt),
        updated_at = NOW()
    FROM (
        SELECT request_id, COUNT(*) AS cnt
        FROM monitor.call_attempt
        WHERE created_at >= :since
        GROUP BY request_id
    ) sub
    WHERE cl.request_id = sub.request_id
      AND cl.created_at >= :since
      AND cl.attempt_count < sub.cnt
    """
)


class MonitorReconcileRepository:
    """把 ``call_log.attempt_count`` 收敛到最近窗口的实际 attempt 行数。"""

    def __init__(self, session: AsyncSession) -> None:
        # 窗口起点（since）由调用方计算后传入，本仓储不自行取时钟。
        self.session = session

    async def reconcile_attempt_counts(self, since: datetime) -> int:
        """对 ``created_at >= since`` 的调用做一次只升不降的对账。

        返回受影响行数（本次被抬高的 ``call_log`` 行数）。``rowcount`` 缺失时
        按 0 处理；异常不在这里兜底，交由调用方决定是否记日志继续。
        """
        result = await self.session.execute(_RECONCILE_SQL, {"since": since})
        return result.rowcount or 0
